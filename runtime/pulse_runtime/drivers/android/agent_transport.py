"""Device-qualified plumbing for the existing Agent wire v1, never an action driver."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from pulse_runtime.drivers.android.scrcpy_video import SourceError, adb, find_adb

SERIAL_PATTERN = r"[A-Za-z0-9._:-]{1,64}"
PACKAGE = "com.pulse.agent"
TARGET_APP_PACKAGE: Final = "com.pulse.target"
ACCESSIBILITY_COMPONENT = f"{PACKAGE}/com.pulse.agent.service.AgentAccessibilityService"
SOCKET = "localabstract:pulse-default"
PEER_DIRECTORY = "files/pulse-runtime-peer"


@dataclass(frozen=True)
class AgentBuild:
    """An exact, signed debug artifact, not an inferred version/signing compatibility policy."""

    package: str = PACKAGE
    version_name: str = "0.1.0"
    version_code: int = 1
    minimum_api: int = 30
    protocol_version: int = 1
    apk_sha256: str = "41c01d1cab85f428338b31f7abf883d3b962761aebb1868fcec7c6c42aeb4b4e"
    signer_sha256: str = "a8f9210917f9a4de6a02fbd6e92af83f181af67c7bdd7e5f07624bd221152acf"


SUPPORTED_BUILD = AgentBuild()


class ConnectionFault(Exception):
    """Only a closed category crosses the subprocess/socket boundary. No raw output or cause."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_serial(serial: str) -> None:
    if re.fullmatch(SERIAL_PATTERN, serial) is None:
        raise ConnectionFault("invalid_serial")


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ConnectionFault("invalid_message")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ConnectionFault("invalid_message") from None
    if str(parsed) != value:
        raise ConnectionFault("invalid_message")
    return value


def parse_action_geometry(output: str) -> dict[str, int]:
    """Only a known active internal input viewport, not a guessed wm size or video frame."""
    geometries: set[tuple[int, int, int]] = set()
    values = [(match[1], True) for match in re.finditer(r"DisplayViewport\{([^{}\n]*)\}", output)]
    values += [(match[1], False) for match in re.finditer(r"Viewport INTERNAL: ([^\n]*)", output)]
    for value, wrapped in values:
        if re.search(r"\bdisplayId=0(?:,|\s|$)", value) is None:
            continue
        active = (
            re.search(r"\bisActive=true\b", value)
            if wrapped
            else re.search(r"\bisActive=\[1\]", value)
        )
        if active is None:
            # A stale inactive copy is not geometry evidence; never guess from it.
            continue
        # The native line's "Viewport INTERNAL:" prefix already establishes its type.
        required = (r"\btype=INTERNAL\b", r"\bvalid=true\b") if wrapped else ()
        if any(re.search(pattern, value) is None for pattern in required):
            raise ConnectionFault("display_geometry_unavailable")
        orientation = re.search(r"\borientation=([0-3])(?:,|\s|$)", value)
        frame = re.search(r"\blogicalFrame=\[0,\s*0,\s*([0-9]+),\s*([0-9]+)\]", value)
        if orientation is None or frame is None:
            raise ConnectionFault("display_geometry_unavailable")
        width, height = int(frame[1]), int(frame[2])
        if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 8_000_000:
            raise ConnectionFault("display_geometry_unavailable")
        geometries.add((width, height, int(orientation[1]) * 90))
    if len(geometries) != 1:
        raise ConnectionFault("display_geometry_unavailable")
    width, height, rotation = geometries.pop()
    return {"display_id": 0, "width": width, "height": height, "rotation": rotation}


async def finish_cleanup[T](awaitable: Awaitable[T]) -> T:
    """Repeated caller cancellation cannot interrupt a bounded child cleanup/creation command."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def complete_mutation[T](awaitable: Awaitable[T]) -> T:
    """Finish one issued bounded mutation before propagating cancellation; never replay it."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(ConnectionFault):
            await finish_cleanup(task)
        raise


async def host_output(*args: str, timeout: float = 5.0) -> str:  # noqa: ASYNC109
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            raise ConnectionFault("host_command_failed")
        if len(out) > 1_048_576:
            raise ConnectionFault("output_limit")
        return out.decode("utf-8", errors="strict")
    except TimeoutError:
        raise ConnectionFault("timeout") from None
    except (OSError, UnicodeError):
        raise ConnectionFault("host_command_failed") from None
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await finish_cleanup(proc.wait())


@dataclass(frozen=True)
class DeviceRow:
    serial: str
    state: str
    transport_id: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class InstalledAgent:
    version_name: str
    version_code: int
    debuggable: bool
    apk_sha256: str


@dataclass(frozen=True)
class InstalledTargetApp:
    version_name: str | None
    version_code: int | None


@dataclass(frozen=True)
class AdbdPeer:
    uid: int
    pid: int

    @property
    def config(self) -> str:
        return f"{self.uid}:{self.pid}"


@dataclass(frozen=True)
class AccessibilitySettings:
    services: str
    enabled: str

    @property
    def agent_selected(self) -> bool:
        return any(
            component in {ACCESSIBILITY_COMPONENT, f"{PACKAGE}/.service.AgentAccessibilityService"}
            for component in self.components
        )

    @property
    def components(self) -> list[str]:
        if self.services in {"", "null"}:
            return []
        return self.services.split(":")

    @property
    def active(self) -> bool:
        return self.enabled == "1" and self.agent_selected

    def with_agent(self) -> AccessibilitySettings:
        components = self.components
        if not self.agent_selected:
            components.append(ACCESSIBILITY_COMPONENT)
        return AccessibilitySettings(":".join(components), "1")


@dataclass(frozen=True)
class AgentHealth:
    lifecycle_id: str
    capabilities: dict[str, object]
    wire_session_id: str


def parse_health(body: bytes, build: AgentBuild = SUPPORTED_BUILD) -> AgentHealth:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConnectionFault("invalid_message")
            result[key] = value
        return result

    try:
        envelope = json.loads(body, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectionFault("invalid_message") from None
    if not isinstance(envelope, dict):
        raise ConnectionFault("invalid_message")
    if type(envelope.get("protocol_version")) is not int or envelope["protocol_version"] != 1:
        raise ConnectionFault("wire_incompatible")
    if envelope.get("success") is not True or envelope.get("terminal") is not True:
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise ConnectionFault(
            code
            if isinstance(code, str) and code in {"recovery_required", "action_uncertain"}
            else "health_rejected"
        )
    if envelope.get("operation") != "health" or envelope.get("error") is not None:
        raise ConnectionFault("invalid_message")
    canonical_uuid(envelope.get("request_id"))
    session_id = canonical_uuid(envelope.get("session_id"))
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ConnectionFault("invalid_message")
    if result.get("agent_version") != build.version_name:
        raise ConnectionFault("version_incompatible")
    raw = result.get("capabilities")
    if not isinstance(raw, dict):
        raise ConnectionFault("invalid_message")
    capabilities: dict[str, object] = {}
    for key in ("accessibility", "screenshot", "ocr"):
        if not isinstance(raw.get(key), str) or raw[key] not in {"available", "unavailable"}:
            raise ConnectionFault("invalid_message")
        capabilities[key] = raw[key]
    for key in ("platform_operation_pending", "recovery_required"):
        if type(raw.get(key)) is not bool:
            raise ConnectionFault("invalid_message")
        capabilities[key] = raw[key]
    if (
        type(raw.get("minimum_android_api")) is not int
        or raw["minimum_android_api"] != build.minimum_api
        or raw.get("display_scope") != "default_only"
        or raw.get("ocr_engine") != "mlkit_chinese_bundled"
    ):
        raise ConnectionFault("invalid_message")
    for key, allowed in (
        ("actions", {"tap", "swipe", "set_text", "node_click"}),
        ("ocr_languages", {"zh", "en"}),
    ):
        values = raw.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(v, str) or v not in allowed for v in values)
            or len(values) != len(set(values))
        ):
            raise ConnectionFault("invalid_message")
        capabilities[key] = list(values)
    lifecycle_id = canonical_uuid(raw.get("error_log_session_id"))
    for key in ("minimum_android_api", "display_scope", "ocr_engine"):
        capabilities[key] = raw[key]
    capabilities["error_log_session_id"] = lifecycle_id
    return AgentHealth(lifecycle_id, capabilities, session_id)


async def read_health(port: int, token: str, build: AgentBuild = SUPPORTED_BUILD) -> AgentHealth:
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ConnectionFault("credential_unavailable")
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=8192)
            writer.write(
                (
                    "GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Authorization: Bearer {token}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > 8192:
                raise ConnectionFault("invalid_message")
            lines = head.decode("ascii").split("\r\n")
            status = lines[0].split(" ")
            if len(status) < 2 or status[0] != "HTTP/1.1":
                raise ConnectionFault("invalid_message")
            if status[1] in {"401", "403"}:
                raise ConnectionFault("authentication_failed")
            if status[1] != "200":
                raise ConnectionFault("health_rejected")
            headers: dict[str, str] = {}
            for line in lines[1:-2]:
                name, separator, value = line.partition(":")
                name = name.lower()
                if not separator or name in headers:
                    raise ConnectionFault("invalid_message")
                headers[name] = value.strip()
            length = headers.get("content-length", "")
            if (
                not length.isdecimal()
                or not 1 <= int(length) <= 65536
                or "transfer-encoding" in headers
                or headers.get("content-type")
                not in {"application/json", "application/json; charset=utf-8"}
            ):
                raise ConnectionFault("invalid_message")
            body = await reader.readexactly(int(length))
            if await reader.read(1):
                raise ConnectionFault("invalid_message")
            return parse_health(body, build)
    except TimeoutError:
        raise ConnectionFault("timeout") from None
    except (OSError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise ConnectionFault("agent_unavailable") from None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await finish_cleanup(asyncio.wait_for(writer.wait_closed(), 1))


class AndroidBackend:
    """Reuse the serial-qualified runner; never return its arguments, stderr or exception text."""

    def __init__(self, build: AgentBuild = SUPPORTED_BUILD) -> None:
        self.build = build

    async def command(
        self,
        serial: str,
        *args: str,
        timeout: float = 5.0,  # noqa: ASYNC109
    ) -> str:
        validate_serial(serial)
        try:
            output = await adb(serial, *args, timeout=timeout)
        except SourceError as exc:
            text = str(exc).lower()
            code = (
                "adb_unauthorized"
                if "unauthorized" in text
                else "adb_offline"
                if "offline" in text
                else "timeout"
                if "timed out" in text
                else "device_disconnected"
                if "not found" in text or "no devices" in text
                else "agent_package_already_exists"
                if "install_failed_already_exists" in text
                else "build_signature_incompatible"
                if "install_failed_update_incompatible" in text
                or "install_failed_version_downgrade" in text
                else "accessibility_policy_blocked"
                if "settings" in " ".join(args)
                and any(word in text for word in ("policy", "administrator", "restriction"))
                else "accessibility_setting_denied"
                if "settings" in " ".join(args)
                and any(
                    word in text for word in ("securityexception", "permission denied", "denial")
                )
                else "adb_command_failed"
            )
            raise ConnectionFault(code) from None
        except OSError:
            raise ConnectionFault("adb_unavailable") from None
        if len(output) > 1_048_576:
            raise ConnectionFault("output_limit")
        return output

    async def inventory(self) -> list[DeviceRow]:
        try:
            executable = find_adb()
        except SourceError:
            raise ConnectionFault("adb_unavailable") from None
        # Enumeration is host-global. Every command addressing a device goes through command(-s).
        try:
            output = await host_output(executable, "devices", "-l")
        except ConnectionFault as exc:
            raise ConnectionFault(
                "timeout" if exc.code == "timeout" else "adb_unavailable"
            ) from None
        rows = []
        for line in output.splitlines():
            parts = line.split()
            if (
                len(parts) < 2
                or parts[0] == "List"
                or re.fullmatch(SERIAL_PATTERN, parts[0]) is None
            ):
                continue
            if parts[1] not in {"device", "offline", "unauthorized", "no", "recovery", "sideload"}:
                continue
            properties = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            transport_id = properties.get("transport_id")
            if transport_id is not None and not transport_id.isdecimal():
                raise ConnectionFault("invalid_inventory")
            model = properties.get("model")
            if model is not None and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", model) is None:
                model = None
            rows.append(DeviceRow(parts[0], parts[1], transport_id, model))
        if len(rows) > 64 or len({r.serial for r in rows}) != len(rows):
            raise ConnectionFault("invalid_inventory")
        return rows

    async def platform(self, serial: str) -> tuple[int, int]:
        api = (await self.command(serial, "shell", "getprop ro.build.version.sdk")).strip()
        user = (await self.command(serial, "shell", "am get-current-user")).strip()
        if not api.isdecimal() or not user.isdecimal():
            raise ConnectionFault("platform_unavailable")
        return int(api), int(user)

    async def target_app(self, serial: str) -> InstalledTargetApp | None:
        """Read the fixed fixture for current user 0, without Agent or device mutations."""
        validate_serial(serial)
        try:
            async with asyncio.timeout(5):
                row = next((row for row in await self.inventory() if row.serial == serial), None)
                if row is None or row.state != "device":
                    raise ConnectionFault(
                        "device_disconnected"
                        if row is None
                        else "adb_offline"
                        if row.state == "offline"
                        else "adb_unauthorized"
                        if row.state == "unauthorized"
                        else "adb_permission_denied"
                        if row.state == "no"
                        else "device_unavailable"
                    )
                user = (await self.command(serial, "shell", "am get-current-user 2>&1")).strip()
                if re.fullmatch(r"[0-9]{1,10}", user) is None:
                    raise ConnectionFault("target_app_output_invalid")
                if user != "0":
                    raise ConnectionFault("unsupported_android_user")
                # Never mask a package-manager error as an empty successful enumeration.
                output = await self.command(
                    serial,
                    "shell",
                    f"pm list packages --user 0 {TARGET_APP_PACKAGE} 2>&1 "
                    "&& printf '\\npulse-target-app-list-complete\\n'",
                )
                lines = output.splitlines()
                if not lines or lines[-1] != "pulse-target-app-list-complete":
                    raise ConnectionFault("target_app_output_invalid")
                packages = [line for line in lines[:-1] if line]
                if (
                    len(packages) > 64
                    or len(packages) != len(set(packages))
                    or any(
                        re.fullmatch(
                            r"package:(?:[A-Za-z][A-Za-z0-9_]*\.)+[A-Za-z][A-Za-z0-9_]*", line
                        )
                        is None
                        or TARGET_APP_PACKAGE not in line
                        for line in packages
                    )
                ):
                    raise ConnectionFault("target_app_output_invalid")
                if f"package:{TARGET_APP_PACKAGE}" not in packages:
                    return None
                details = await self.command(
                    serial, "shell", f"dumpsys package {TARGET_APP_PACKAGE} 2>&1"
                )
                headers = list(
                    re.finditer(
                        r"(?m)^[ \t]*Package \[([^\]\r\n]+)\] \([0-9a-fA-F]+\):[ \t]*\r?$",
                        details,
                    )
                )
                if (
                    len(headers) != 1
                    or headers[0][1] != TARGET_APP_PACKAGE
                    or len(re.findall(r"(?m)^[ \t]*Package \[", details)) != 1
                ):
                    raise ConnectionFault("target_app_output_invalid")
                # Only read metadata inside this package's indented block, not resolver/tail data.
                details = re.split(r"(?m)^[^ \t\r\n]", details[headers[0].end() :], maxsplit=1)[0]
                names = re.findall(r"(?m)^[ \t]*versionName=([^\r\n]*)\r?$", details)
                codes = re.findall(r"(?m)^[ \t]*versionCode=([^\s]*)", details)
                if len(names) > 1 or len(codes) > 1:
                    raise ConnectionFault("target_app_output_invalid")
                name = names[0] if names and names[0] not in {"", "null"} else None
                if (
                    name is not None
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", name) is None
                ):
                    raise ConnectionFault("target_app_output_invalid")
                if codes and (
                    re.fullmatch(r"[0-9]{1,19}", codes[0]) is None or int(codes[0]) > 2**63 - 1
                ):
                    raise ConnectionFault("target_app_output_invalid")
                return InstalledTargetApp(name, int(codes[0]) if codes else None)
        except TimeoutError:
            raise ConnectionFault("timeout") from None
        except ConnectionFault:
            raise
        except Exception:
            raise ConnectionFault("target_app_query_failed") from None

    async def action_geometry(self, serial: str) -> dict[str, int]:
        return parse_action_geometry(await self.command(serial, "shell", "dumpsys", "input"))

    async def installed(self, serial: str) -> InstalledAgent | None:
        output = await self.command(serial, "shell", f"pm path --user 0 {PACKAGE} || true")
        paths = [line.removeprefix("package:") for line in output.splitlines() if line]
        if not paths:
            return None
        if len(paths) != 1 or not re.fullmatch(
            r"/data/app/[A-Za-z0-9_./~+=-]+/base\.apk", paths[0]
        ):
            raise ConnectionFault("build_incompatible")
        details = await self.command(serial, "shell", f"dumpsys package {PACKAGE}")
        code = re.search(r"\bversionCode=(\d+)\b", details)
        name = re.search(r"\bversionName=([^\s]+)", details)
        flags = re.search(r"\b(?:pkgFlags|flags)=\[([^\]]*)\]", details)
        if code is None or name is None or flags is None:
            raise ConnectionFault("agent_verification_failed")
        debuggable = "DEBUGGABLE" in flags[1].split()
        digest = (await self.command(serial, "shell", f"sha256sum {shlex.quote(paths[0])}")).split()
        if not digest or re.fullmatch(r"[0-9a-f]{64}", digest[0]) is None:
            raise ConnectionFault("agent_verification_failed")
        return InstalledAgent(name[1], int(code[1]), debuggable, digest[0])

    async def require_run_as(self, serial: str) -> None:
        try:
            value = await self.command(serial, "shell", "-T", "run-as", PACKAGE, "id", "-u")
        except ConnectionFault:
            raise ConnectionFault("run_as_unsupported") from None
        if not value.strip().isdecimal():
            raise ConnectionFault("run_as_unsupported")

    async def private(self, serial: str, script: str) -> str:
        return await self.command(
            serial, "shell", "-T", f"run-as {PACKAGE} sh -c {shlex.quote(script)}"
        )

    async def peer(self, serial: str) -> str | None:
        value = await self.private(
            serial,
            "if [ -L files/adb-peer ]; then printf invalid; "
            "elif [ -f files/adb-peer ]; then dd if=files/adb-peer bs=33 count=1 2>/dev/null; "
            "else printf missing; fi",
        )
        if value == "missing":
            return None
        if re.fullmatch(r"(0|2000):[1-9][0-9]{0,9}\n?", value) is None or len(value) > 32:
            raise ConnectionFault("peer_configuration_invalid")
        if int(value.rstrip("\n").split(":")[1]) > 2_147_483_647:
            raise ConnectionFault("peer_configuration_invalid")
        return value.rstrip("\n")

    async def token(self, serial: str) -> str | None:
        value = await self.private(
            serial,
            "if [ -L files/agent-token ]; then printf invalid; "
            "elif [ -f files/agent-token ]; then stat -c %a files/agent-token; "
            "dd if=files/agent-token bs=65 count=1 2>/dev/null; else printf missing; fi",
        )
        if value == "missing":
            return None
        mode, _, token = value.partition("\n")
        if mode != "600" or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ConnectionFault("credential_unavailable")
        return token

    async def agent_pid(self, serial: str) -> int | None:
        value = (await self.command(serial, "shell", f"pidof {PACKAGE} || true")).strip()
        if not value:
            return None
        if not value.isdecimal() or not 1 <= int(value) <= 2_147_483_647:
            raise ConnectionFault("agent_process_ambiguous")
        return int(value)

    async def adbd_peer(self, serial: str) -> AdbdPeer:
        pid = (await self.command(serial, "shell", "pidof adbd")).strip()
        if not pid.isdecimal() or not 1 <= int(pid) <= 2_147_483_647:
            raise ConnectionFault("adbd_identity_unavailable")
        output = await self.command(serial, "shell", f"ps -p {pid} -o UID,PID,NAME")
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if (
            len(rows) != 2
            or rows[0] != ["UID", "PID", "NAME"]
            or len(rows[1]) != 3
            or rows[1][0] not in {"0", "2000"}
            or rows[1][1] != pid
            or rows[1][2] not in {"adbd", "/system/bin/adbd", "/apex/com.android.adbd/bin/adbd"}
        ):
            raise ConnectionFault("adbd_identity_unavailable")
        return AdbdPeer(int(rows[1][0]), int(pid))

    async def permissions(self, serial: str) -> AccessibilitySettings:
        values = []
        for name in ("enabled_accessibility_services", "accessibility_enabled"):
            value = (
                await self.command(serial, "shell", f"settings --user 0 get secure {name}")
            ).strip()
            if len(value) > 8192 or "\n" in value or "\r" in value:
                raise ConnectionFault("accessibility_settings_unavailable")
            values.append(value)
        return AccessibilitySettings(*values)

    async def set_permissions(
        self, serial: str, expected: AccessibilitySettings, target: AccessibilitySettings
    ) -> bool:
        def write(name: str, value: str) -> str:
            return (
                f"settings --user 0 delete secure {name} >/dev/null"
                if value == "null"
                else f"settings --user 0 put secure {name} {shlex.quote(value)}"
            )

        # Compare-before-write protects edits observed since the snapshot. Android settings has
        # no cross-key transaction; readiness and restoration always read back the actual values.
        script = (
            '[ "$(settings --user 0 get secure enabled_accessibility_services)" = '
            f"{shlex.quote(expected.services)} ] && "
            '[ "$(settings --user 0 get secure accessibility_enabled)" = '
            f"{shlex.quote(expected.enabled)} ] || {{ printf settings_changed; exit; }}; "
            f"{write('enabled_accessibility_services', target.services)} && "
            f"{write('accessibility_enabled', target.enabled)}"
        )
        try:
            output = await self.command(serial, "shell", script)
        except ConnectionFault as exc:
            if exc.code in {
                "timeout",
                "adb_offline",
                "adb_unauthorized",
                "device_disconnected",
                "accessibility_policy_blocked",
                "accessibility_setting_denied",
            }:
                raise
            return False
        if output.strip() == "settings_changed":
            raise ConnectionFault("accessibility_settings_changed")
        lowered = output.lower()
        if any(word in lowered for word in ("policy", "administrator", "restriction")):
            raise ConnectionFault("accessibility_policy_blocked")
        if any(word in lowered for word in ("securityexception", "permission denied", "denial")):
            raise ConnectionFault("accessibility_setting_denied")
        return await self.permissions(serial) == target

    async def restore_permissions(
        self, serial: str, original: AccessibilitySettings, expected: AccessibilitySettings
    ) -> bool:
        current = await self.permissions(serial)
        if current.services != expected.services:
            # A person or another owner changed the list. Do not undo their edit.
            return True
        enabled = (
            original.enabled
            if current.enabled in {original.enabled, expected.enabled}
            else current.enabled
        )
        return await self.set_permissions(
            serial, current, AccessibilitySettings(original.services, enabled)
        )

    async def claim_peer(self, serial: str, owner: str, peer: AdbdPeer) -> None:
        canonical_uuid(owner)
        script = (
            "umask 077; [ ! -L files ] || exit 74; mkdir -p files; "
            "if [ -e files/adb-peer ] || [ -L files/adb-peer ]; then "
            "[ -f files/adb-peer ] && [ ! -L files/adb-peer ] || exit 74; fi; "
            f"mkdir {PEER_DIRECTORY} 2>/dev/null || exit 73; "
            f"printf %s {shlex.quote(owner)} > {PEER_DIRECTORY}/owner; "
            "if [ -f files/adb-peer ]; then "
            f"cp -p files/adb-peer {PEER_DIRECTORY}/prior || exit 74; "
            f"printf present > {PEER_DIRECTORY}/origin; "
            f"else printf absent > {PEER_DIRECTORY}/origin; fi; "
            f"printf '%s\\n' {peer.config} > {PEER_DIRECTORY}/expected && "
            f"cp {PEER_DIRECTORY}/expected {PEER_DIRECTORY}/pending && "
            f"chmod 600 {PEER_DIRECTORY}/pending && mv {PEER_DIRECTORY}/pending files/adb-peer"
        )
        try:
            await self.private(serial, script)
        except ConnectionFault as exc:
            if exc.code in {"timeout", "adb_offline", "adb_unauthorized", "device_disconnected"}:
                raise
            raise ConnectionFault("configuration_in_use") from None

    async def release_peer(self, serial: str, owner: str) -> bool:
        canonical_uuid(owner)
        script = (
            f"[ -f {PEER_DIRECTORY}/owner ] || {{ printf absent; exit; }}; "
            f'[ "$(cat {PEER_DIRECTORY}/owner)" = {shlex.quote(owner)} ] || '
            "{ printf foreign_owner; exit; }; "
            "if [ -f files/adb-peer ] && [ ! -L files/adb-peer ] && { "
            f"cmp -s files/adb-peer {PEER_DIRECTORY}/expected || "
            f"cmp -s files/adb-peer {PEER_DIRECTORY}/next; }}; then "
            f'if [ "$(cat {PEER_DIRECTORY}/origin)" = present ]; then '
            f"mv {PEER_DIRECTORY}/prior files/adb-peer || exit 74; "
            f'elif [ "$(cat {PEER_DIRECTORY}/origin)" = absent ]; then '
            "rm files/adb-peer || exit 74; else exit 74; fi; fi; "
            f"rm -f {PEER_DIRECTORY}/owner {PEER_DIRECTORY}/origin {PEER_DIRECTORY}/prior "
            f"{PEER_DIRECTORY}/expected {PEER_DIRECTORY}/next {PEER_DIRECTORY}/pending && "
            f"rmdir {PEER_DIRECTORY} && printf released"
        )
        return (await self.private(serial, script)).strip() in {
            "released",
            "absent",
            "foreign_owner",
        }

    async def update_peer(self, serial: str, owner: str, peer: AdbdPeer) -> None:
        canonical_uuid(owner)
        script = (
            f'umask 077; [ "$(cat {PEER_DIRECTORY}/owner)" = {shlex.quote(owner)} ] && '
            f"[ ! -L files/adb-peer ] && cmp -s files/adb-peer {PEER_DIRECTORY}/expected "
            "|| exit 73; "
            f"printf '%s\\n' {peer.config} > {PEER_DIRECTORY}/next && "
            f"cp {PEER_DIRECTORY}/next {PEER_DIRECTORY}/pending && "
            f"chmod 600 {PEER_DIRECTORY}/pending && "
            f"mv {PEER_DIRECTORY}/pending files/adb-peer && "
            f"mv {PEER_DIRECTORY}/next {PEER_DIRECTORY}/expected"
        )
        await self.private(serial, script)

    async def install(self, serial: str, apk: Path | None) -> None:
        if apk is None:
            raise ConnectionFault("agent_artifact_unconfigured")
        try:
            with apk.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
        except OSError:
            raise ConnectionFault("agent_artifact_unavailable") from None
        if not hmac.compare_digest(digest, self.build.apk_sha256):
            raise ConnectionFault("build_signature_incompatible")
        # Android defaults to replacement even without -r. -R explicitly forbids it.
        output = await self.command(
            serial, "install", "--no-streaming", "-R", "--user", "0", str(apk), timeout=60
        )
        if not any(line == "Success" for line in output.splitlines()):
            raise ConnectionFault("agent_install_failed")

    async def loopback(self, port: int) -> None:
        if sys.platform == "darwin":
            tool = shutil.which("lsof") or "/usr/sbin/lsof"
            output = await host_output(tool, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "n")
            addresses = [line[1:] for line in output.splitlines() if line.startswith("n")]
        elif sys.platform == "win32":
            output = await host_output("netstat", "-ano", "-p", "tcp")
            addresses = [
                fields[1]
                for line in output.splitlines()
                if len(fields := line.split()) >= 4
                and fields[0] == "TCP"
                and fields[3] == "LISTENING"
                and fields[1].endswith(f":{port}")
            ]
        else:
            output = await host_output("ss", "-ltnH")
            addresses = [
                fields[3]
                for line in output.splitlines()
                if len(fields := line.split()) >= 4 and fields[3].endswith(f":{port}")
            ]
        if not addresses or any(
            address not in {f"127.0.0.1:{port}", f"[::1]:{port}", f"::1:{port}"}
            for address in addresses
        ):
            raise ConnectionFault("forward_not_loopback")

    async def health(self, forward: ForwardLease, token: str) -> AgentHealth:
        if forward.port is None:
            raise ConnectionFault("forward_unavailable")
        return await read_health(forward.port, token, self.build)


@dataclass
class ForwardLease:
    backend: AndroidBackend = field(repr=False)
    serial: str
    port: int | None = None
    owned: bool = False

    async def mappings(self) -> set[tuple[str, str, str]]:
        output = await self.backend.command(self.serial, "forward", "--list")
        rows = set()
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 3 or re.fullmatch(SERIAL_PATTERN, fields[0]) is None:
                raise ConnectionFault("invalid_forward_inventory")
            rows.add((fields[0], fields[1], fields[2]))
        return rows

    @property
    def mapping(self) -> tuple[str, str, str]:
        return self.serial, f"tcp:{self.port}", SOCKET

    async def open(self) -> None:
        baseline = await self.mappings()
        creation = asyncio.create_task(
            self.backend.command(self.serial, "forward", "--no-rebind", "tcp:0", SOCKET)
        )

        async def claim() -> None:
            try:
                value = (await creation).strip()
            except ConnectionFault as exc:
                if exc.code in {"timeout", "adb_command_failed"}:
                    raise ConnectionFault("forward_creation_uncertain") from None
                raise
            if not value.isdecimal() or not 1 <= int(value) <= 65535:
                raise ConnectionFault("forward_creation_uncertain")
            self.port = int(value)
            if self.mapping in baseline or any(r[1] == f"tcp:{self.port}" for r in baseline):
                self.port = None
                raise ConnectionFault("forward_conflict")
            self.owned = True

        claiming = asyncio.create_task(claim())
        try:
            await asyncio.shield(claiming)
            if self.mapping not in await self.mappings():
                raise ConnectionFault("forward_replaced")
            if self.port is None:
                raise ConnectionFault("forward_unavailable")
            await self.backend.loopback(self.port)
        except BaseException:
            # Finish the one already-issued creation, never replay it. Its bounded result is the
            # ownership proof. Missing/ambiguous output must not authorize deleting a foreign map.
            with contextlib.suppress(ConnectionFault):
                await finish_cleanup(claiming)
            with contextlib.suppress(ConnectionFault):
                await finish_cleanup(self.close())
            raise

    async def close(self) -> None:
        if not self.owned or self.port is None:
            return
        current = await self.mappings()
        if self.mapping in current:
            await self.backend.command(self.serial, "forward", "--remove", f"tcp:{self.port}")
            if self.mapping in await self.mappings():
                raise ConnectionFault("forward_cleanup_pending")
        self.owned = False
        self.port = None

    async def verify(self) -> None:
        if not self.owned or self.mapping not in await self.mappings():
            raise ConnectionFault("forward_replaced")


def configured_apk() -> Path | None:
    value = os.environ.get("PULSE_AGENT_APK")
    return Path(value).expanduser() if value else None
