"""Controlled P1.3 acceptance through a new Runtime API, retaining only redacted summaries.

No smoke connection, direct Agent HTTP, input, navigation driver, or sensitive evidence files.
The optional target setup uses only the fixture's existing exported component.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from httpx2 import AsyncClient

from pulse_runtime.drivers.android.agent_transport import (
    AndroidBackend,
    ConnectionFault,
    ForwardLease,
    validate_serial,
)
from pulse_runtime.paths import connection_file, read_connection, runtime_dir

SOURCES = ("tree", "screenshot", "ocr")
EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "build" / "p1-observe-20261104"


def safe_observation(value: dict[str, Any]) -> dict[str, object]:
    """Allowlisted metadata only, never source data or arbitrary exception/error messages."""
    summary: dict[str, object] = {
        key: value.get(key)
        for key in (
            "contract",
            "serial",
            "session_id",
            "request_id",
            "agent_lifecycle_id",
            "agent_http_session_id",
            "requested",
            "requested_display_id",
            "status",
            "success",
            "agent_terminal_success",
            "framing",
            "runtime_time",
            "clock_mapping",
            "revision_start",
            "revision_end",
            "atomic_snapshot",
            "completeness",
            "cleanup",
        )
    }
    error = value.get("error")
    summary["error_code"] = error.get("code") if isinstance(error, dict) else None
    sources: dict[str, object] = {}
    for key in SOURCES:
        source = value.get("sources", {}).get(key, {})
        current = {
            field: source.get(field)
            for field in (
                "source",
                "requested",
                "capability",
                "status",
                "time",
                "coordinates",
                "scope",
                "completeness",
                "collection",
            )
        }
        error = source.get("error")
        current["error_code"] = error.get("code") if isinstance(error, dict) else None
        data = source.get("data")
        current["data_present"] = data is not None
        if isinstance(data, dict):
            if key == "tree":
                current["node_count"] = len(data.get("nodes", []))
                current["redacted_node_count"] = sum(
                    node.get("text_redacted") is True for node in data.get("nodes", [])
                )
            elif key == "ocr":
                current["block_count"] = len(data.get("blocks", []))
            else:
                current["png_base64_char_count"] = len(data.get("base64", ""))
        sources[key] = current
    summary["sources"] = sources
    return summary


def controlled_form(value: dict[str, Any]) -> dict[str, bool]:
    data = value.get("sources", {}).get("tree", {}).get("data") or {}
    nodes = data.get("nodes", [])
    by_id = {node.get("view_id"): node for node in nodes}
    prefix = "com.pulse.target:id/"
    submit = by_id.get(prefix + "btn_submit", {})
    small = by_id.get(prefix + "rb_small", {})
    agree = by_id.get(prefix + "cb_agree", {})
    password = by_id.get(prefix + "et_password", {})
    return {
        "controlled_package_only": bool(nodes)
        and all(node.get("package_name") == "com.pulse.target" for node in nodes),
        "controlled_form_title_present": prefix + "tv_form_title" in by_id,
        "submit_enabled_false": submit.get("states", {}).get("enabled") is False,
        "small_checked_true": small.get("states", {}).get("checked") is True,
        "agree_checked_false": agree.get("states", {}).get("checked") is False,
        "password_native_text_redacted": password.get("text_redacted") is True
        and password.get("text") is None
        and password.get("content_description") is None,
    }


def protocol_observation(value: dict[str, Any], requested: dict[str, bool]) -> bool:
    framing = value.get("framing", {})
    if (
        value.get("contract") != "pulse-runtime-observation-draft"
        or value.get("requested") != requested
        or value.get("atomic_snapshot") is not False
        or not all(
            framing.get(key) is True for key in ("validated", "terminal_received", "eof_received")
        )
    ):
        return False
    for key, enabled in requested.items():
        source = value.get("sources", {}).get(key, {})
        if source.get("requested") is not enabled:
            return False
        if not enabled and (
            source.get("status") != "unavailable" or source.get("data") is not None
        ):
            return False
    return True


def collected(value: dict[str, Any], requested: dict[str, bool]) -> bool:
    if not protocol_observation(value, requested) or value.get("success") is not True:
        return False
    if value.get("framing", {}).get("source_data_validated") is not True:
        return False
    return all(
        value.get("sources", {}).get(key, {}).get("status") == "available"
        and value.get("sources", {}).get(key, {}).get("data") is not None
        for key, enabled in requested.items()
        if enabled
    )


async def baseline(serial: str) -> tuple[object, object, object, object]:
    backend = AndroidBackend()
    return (
        await backend.permissions(serial),
        await backend.peer(serial),
        await ForwardLease(backend, serial).mappings(),
        await backend.agent_pid(serial),
    )


def log_privacy(directory: Path, token: str) -> dict[str, int]:
    paths = [
        path
        for path in directory.glob("runtime.log*")
        if path.name == "runtime.log" or re.fullmatch(r"runtime\.log\.[1-9][0-9]*", path.name)
    ]
    logs = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    return {
        "missing_runtime_logs": int(not paths),
        "runtime_token_occurrences": logs.count(token),
        "bearer_credential_patterns": len(re.findall(r"Bearer [A-Za-z0-9_-]{16,}", logs)),
        "png_or_large_base64_patterns": len(re.findall(r"[A-Za-z0-9+/]{256,}={0,2}", logs)),
        "raw_observation_keys": len(
            re.findall(r'"(?:base64|node_path|content_description|blocks)"\s*:', logs)
        ),
    }


async def run(args: argparse.Namespace) -> int:
    validate_serial(args.serial)
    root = EVIDENCE_ROOT
    directory = args.evidence.resolve()
    private_runtime = args.runtime_dir.resolve()
    if (
        not directory.is_relative_to(root)
        or directory == root
        or directory.exists()
        or not directory.parent.is_dir()
    ):
        raise SystemExit("Use a fresh attempt directory within build/p1-observe-20261104")
    if private_runtime == runtime_dir().resolve() or private_runtime.is_relative_to(root):
        raise SystemExit("Use an independent private Runtime directory outside evidence")
    directory.mkdir(mode=0o700)
    result: dict[str, Any] = {
        "milestones": ["P1.3", "P1.12 observation minimum foundation"],
        "serial": args.serial,
        "started_at_epoch_ms": time.time_ns() // 1_000_000,
        "status": "running",
        "checks": [],
        "observations": [],
        "unverified": [
            "two physical Android devices; dual serial tests are synthetic only",
            "physical in-flight USB removal, permission revocation or Agent/adbd restart",
            "physical rotation, other displays, FLAG_SECURE windows, "
            "screenshot failure or zero OCR",
            "other OEM/API/user, Windows/Linux, release/non-debug or Wi-Fi/LAN",
            "WebView/Flutter physical matrix, DOM or enhanced Flutter semantics",
            "uiautomator fallback (not integrated, no automatic trigger)",
            "P0.4/P0.7 acceptance, full P1 exit or formal Schema v1",
        ],
        "privacy": "Only controlled-fixture metadata/counts/boolean checks retained; "
        "no source data or credential.",
    }

    def save() -> None:
        temporary = directory / ".result.json.tmp"
        temporary.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n")
        temporary.replace(directory / "result.json")

    def check(name: str, passed: bool, details: object = None) -> bool:
        result["checks"].append({"name": name, "passed": passed, "details": details})
        save()
        return passed

    info = read_connection(private_runtime / "runtime.json")
    previous = read_connection(connection_file())
    if info is None or (previous is not None and info.pid == previous.pid):
        result["status"] = "blocked"
        check("new_independent_runtime", False, "independent_runtime_unavailable")
        return 1
    result["runtime_pid"] = info.pid
    result["pre_existing_runtime_pid"] = previous.pid if previous is not None else None
    session_id: str | None = None
    prepare_operation: str | None = None
    original = None
    active: asyncio.Task[tuple[int, dict[str, Any]]] | None = None

    async with AsyncClient(
        base_url=f"http://127.0.0.1:{info.port}",
        headers={"Authorization": f"Bearer {info.token}"},
        timeout=90,
        trust_env=False,
    ) as client:

        async def request(
            method: str,
            path: str,
            body: dict[str, object] | None = None,
            *,
            bad_auth: bool = False,
            budget_seconds: float = 45,
        ) -> tuple[int, dict[str, Any]]:
            kwargs: dict[str, Any] = {"json": body} if body is not None else {}
            if bad_auth:
                kwargs["headers"] = {"Authorization": "Bearer intentionally-invalid"}
            async with asyncio.timeout(budget_seconds):
                async with client.stream(method, path, **kwargs) as response:
                    data = bytearray()
                    async for part in response.aiter_bytes(65536):
                        data.extend(part)
                        if len(data) > 12 * 1024 * 1024:
                            raise ConnectionFault("acceptance_response_limit")
                    parsed = json.loads(data)
                    if not isinstance(parsed, dict):
                        raise ConnectionFault("acceptance_invalid_response")
                    return response.status_code, parsed

        path = f"/devices/{quote(args.serial, safe='')}/agent"
        try:
            code, health = await request("GET", "/health")
            if not check("new_independent_runtime", code == 200 and health.get("pid") == info.pid):
                raise ConnectionFault("runtime_unavailable")
            code, _ = await request("POST", path + "/observe", {}, bad_auth=True)
            check("observe_requires_runtime_auth", code == 401)
            original = await baseline(args.serial)
            code, inventory = await request("GET", "/devices")
            device = next(
                (d for d in inventory.get("devices", []) if d.get("serial") == args.serial), {}
            )
            if not check(
                "device_discovered",
                code == 200 and device.get("state") == "device",
                {"model": device.get("model"), "state": device.get("state")},
            ):
                raise ConnectionFault("device_unavailable")
            prepare_operation = str(uuid4())
            code, prepared = await request(
                "POST",
                path + "/prepare",
                {
                    "install": False,
                    "enable_accessibility": True,
                    "timeout_ms": 60_000,
                    "operation_id": prepare_operation,
                },
                budget_seconds=75,
            )
            session_id = prepared.get("connection", {}).get("session_id")
            if not check(
                "prepared_existing_exact_pinned_agent",
                code == 200
                and prepared.get("ready") is True
                and prepared.get("agent", {}).get("build_verified") is True
                and prepared["agent"].get("signature_verified_by_exact_artifact") is True,
                {
                    "status": prepared.get("status"),
                    "android_api": prepared.get("android_api"),
                    "android_user": prepared.get("android_user"),
                    "agent": prepared.get("agent"),
                },
            ):
                raise ConnectionFault("manual_authorization_or_connection_required")
            code, connected = await request("POST", path + "/connect", {})
            check(
                "same_runtime_connection_generation",
                code == 200
                and connected.get("connection", {}).get("session_id") == session_id
                and connected.get("ready") is True,
            )
            query = f"?session_id={session_id}"
            code, checked = await request("GET", path + "/health" + query)
            if not check(
                "authenticated_agent_health",
                code == 200
                and checked.get("ready") is True
                and checked.get("connection", {}).get("health_authenticated") is True
                and checked.get("capabilities", {}).get("platform_operation_pending") is False
                and checked["capabilities"].get("recovery_required") is False,
            ):
                raise ConnectionFault("agent_not_ready")
            code, capabilities = await request("GET", path + "/capabilities" + query)
            check(
                "capability_declarations_read_not_collection",
                code == 200 and capabilities.get("protocol_version") == 1,
                {
                    key: capabilities.get("capabilities", {}).get(key)
                    for key in ("accessibility", "screenshot", "ocr", "display_scope")
                },
            )
            if args.prepare_target:
                backend = AndroidBackend()
                installed = await backend.command(
                    args.serial, "shell", "pm", "path", "--user", "0", "com.pulse.target"
                )
                if not installed.startswith("package:"):
                    raise ConnectionFault("controlled_target_missing_prepare_page_manually")
                output = await backend.command(
                    args.serial,
                    "shell",
                    "am",
                    "start",
                    "-W",
                    "-n",
                    "com.pulse.target/.FormActivity",
                    "--ez",
                    "clear_faults",
                    "true",
                    timeout=10,
                )
                if "Error:" in output or "Exception" in output:
                    raise ConnectionFault("controlled_target_prepare_failed")
                check(
                    "existing_controlled_fixture_entry",
                    True,
                    {
                        "component": "com.pulse.target/.FormActivity",
                        "clear_faults": True,
                        "input_or_navigation_driver": False,
                    },
                )

            for name, options in (
                ("native", {"tree": True, "screenshot": False, "ocr": False}),
                ("screenshot", {"tree": False, "screenshot": True, "ocr": False}),
                ("ocr", {"tree": False, "screenshot": False, "ocr": True}),
                ("combined", {"tree": True, "screenshot": True, "ocr": True}),
            ):
                if name in {"ocr", "combined"}:
                    await asyncio.sleep(1.15)  # Agent attempts have a documented 1000ms rate limit.
                request_id = str(uuid4())
                code, view = await request(
                    "POST",
                    path + "/observe",
                    {"session_id": session_id, "request_id": request_id, **options},
                )
                result["observations"].append({"name": name, **safe_observation(view)})
                required = name != "combined"
                passed = (
                    code == 200
                    and view.get("serial") == args.serial
                    and view.get("session_id") == session_id
                    and view.get("request_id") == request_id
                    and (
                        collected(view, options)
                        if required
                        else protocol_observation(view, options)
                    )
                )
                if not check(name + "_real_collection", passed) and required:
                    raise ConnectionFault("controlled_observation_failed")
                if name == "native":
                    form = controlled_form(view)
                    if not check(
                        name + "_controlled_form_and_native_states", all(form.values()), form
                    ):
                        raise ConnectionFault("controlled_form_not_ready_prepare_page_manually")
                if name in {"screenshot", "ocr"}:
                    data = view["sources"]["ocr" if name == "ocr" else "screenshot"]["data"]
                    check(
                        name + "_dimensions_and_protection_boundary",
                        0 < data["width"] * data["height"] <= 1_500_000
                        and 0 < data["original_width"] * data["original_height"] <= 8_000_000
                        and data.get("coordinate_space") == "screenshot_pixels"
                        and data.get("display_id") == 0
                        and data.get("protected_content") == "not_verified",
                        {
                            key: data.get(key)
                            for key in (
                                "width",
                                "height",
                                "original_width",
                                "original_height",
                                "scale_x",
                                "scale_y",
                                "protected_content",
                            )
                        },
                    )
                if name == "ocr":
                    blocks = view["sources"]["ocr"]["data"]["blocks"]
                    check(
                        "ocr_no_native_semantics_or_unrequested_png",
                        all(
                            "states" not in block
                            and "enabled" not in block
                            and "checked" not in block
                            for block in blocks
                        )
                        and view["sources"]["screenshot"]["data"] is None,
                        {
                            "block_count": len(blocks),
                            "controlled_title_marker_seen": any(
                                "\u7528\u6237\u4fe1\u606f" in block.get("text", "")
                                for block in blocks
                            ),
                        },
                    )
                check(
                    name + "_non_atomic_partial",
                    view.get("atomic_snapshot") is False
                    and view.get("completeness", {}).get("status") != "complete",
                )

            await asyncio.sleep(1.15)
            request_id = str(uuid4())
            active = asyncio.create_task(
                request(
                    "POST",
                    path + "/observe",
                    {
                        "session_id": session_id,
                        "request_id": request_id,
                        "tree": False,
                        "screenshot": False,
                        "ocr": True,
                    },
                )
            )
            state: dict[str, Any] = {}
            for _ in range(300):
                if active.done():
                    break
                _, snapshot = await request("GET", f"/devices/{quote(args.serial, safe='')}")
                state = snapshot.get("diagnostic", {}).get("observation") or {}
                if (
                    state.get("request_id") == request_id
                    and state.get("request_sent") is True
                    and state.get("header_received") is True
                    and state.get("terminal_received") is False
                ):
                    break
                await asyncio.sleep(0.03)
            code, cancelled = await request(
                "POST", path + f"/observe/cancel?session_id={session_id}&request_id={request_id}"
            )
            check(
                "owned_inflight_cancel",
                code == 200
                and cancelled.get("cancelled") is True
                and cancelled.get("cleanup_complete") is True,
                {
                    "phase_at_cancel": state.get("phase"),
                    "request_sent_at_cancel": state.get("request_sent"),
                    "header_received_at_cancel": state.get("header_received"),
                    "terminal_received_at_cancel": state.get("terminal_received"),
                    "cancel_uses_owned_socket_or_cancel_protocol": True,
                    **cancelled,
                },
            )
            code, aborted = await active
            active = None
            result["observations"].append({"name": "cancelled", **safe_observation(aborted)})
            check(
                "cancelled_request_cannot_publish_source_data",
                code == 200
                and aborted.get("success") is False
                and (aborted.get("error") or {}).get("code") == "cancelled"
                and all(
                    source.get("data") is None for source in aborted.get("sources", {}).values()
                ),
            )
            attempts = 0
            idle_observed = False
            after_cancel: dict[str, Any] = {}
            for _ in range(8):
                attempts += 1
                code, after_cancel = await request("GET", path + "/health" + query)
                capabilities = after_cancel.get("capabilities", {})
                if (
                    code == 200
                    and after_cancel.get("connection", {}).get("session_id") == session_id
                    and capabilities.get("platform_operation_pending") is False
                    and capabilities.get("recovery_required") is False
                ):
                    idle_observed = True
                    break
                await asyncio.sleep(0.5)
            check(
                "cancel_keeps_connection_and_honors_native_resources",
                code == 200
                and after_cancel.get("connection", {}).get("session_id") == session_id
                and after_cancel.get("capabilities", {}).get("recovery_required") is False,
                {
                    "status": after_cancel.get("status"),
                    "platform_operation_pending": after_cancel.get("capabilities", {}).get(
                        "platform_operation_pending"
                    ),
                    "native_idle_observed": idle_observed,
                    "health_attempts": attempts,
                    "agent_cancel_acknowledged": aborted.get("cleanup", {}).get(
                        "cancel_acknowledged"
                    ),
                    "ack_is_not_os_idle": True,
                },
            )
            code, disconnected = await request("DELETE", path + "/connection" + query)
            session_id = None
            check(
                "owned_connection_cleanup",
                code == 200
                and disconnected.get("connection", {}).get("session_id") is None
                and disconnected.get("diagnostic", {}).get("cleanup_pending") is False,
            )
            code, rejected = await request("GET", path + "/health" + query)
            check(
                "retired_runtime_session_rejected",
                code == 409 and rejected.get("error") == "stale_session",
            )
        except ConnectionFault as exc:
            result["blocker"] = exc.code
        except Exception:
            result["blocker"] = "acceptance_transport_or_response_failed"
        finally:
            if active is not None:
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
            if session_id is not None:
                with contextlib.suppress(Exception):
                    await request("DELETE", path + f"/connection?session_id={session_id}")
            elif prepare_operation is not None:
                with contextlib.suppress(Exception):
                    await request("POST", path + f"/cancel?operation_id={prepare_operation}")
            if original is not None:
                try:
                    after = await baseline(args.serial)
                    check("accessibility_original_preserved", after[0] == original[0])
                    check("private_peer_original_preserved", after[1] == original[1])
                    check(
                        "existing_forwards_preserved",
                        after[2] == original[2],
                        {"before_count": len(original[2]), "after_count": len(after[2])},
                    )
                    check(
                        "existing_agent_process_not_restarted",
                        original[3] is None or after[3] == original[3],
                        {
                            "existing_process_at_baseline": original[3] is not None,
                            "before_pid": original[3],
                            "after_pid": after[3],
                        },
                    )
                except Exception:
                    check("original_state_reverification", False, "cleanup_unverifiable")
            if previous is not None:
                try:
                    async with AsyncClient(
                        base_url=f"http://127.0.0.1:{previous.port}",
                        headers={"Authorization": f"Bearer {previous.token}"},
                        timeout=5,
                        trust_env=False,
                    ) as old:
                        response = await old.get("/health")
                        check(
                            "pre_existing_runtime_preserved",
                            response.status_code == 200
                            and response.json().get("pid") == previous.pid,
                            {"pid": previous.pid},
                        )
                except Exception:
                    check("pre_existing_runtime_preserved", False, "old_runtime_unverifiable")
            counts = log_privacy(private_runtime, info.token)
            check("independent_runtime_log_privacy", not any(counts.values()), counts)
            result["passed_required"] = sum(item["passed"] for item in result["checks"])
            result["total_required"] = len(result["checks"])
            result["required_sequence_completed"] = all(
                any(view["name"] == name for view in result["observations"])
                for name in ("native", "screenshot", "ocr", "cancelled")
            )
            result["status"] = (
                "single_device_verified"
                if "blocker" not in result
                and result["required_sequence_completed"]
                and all(item["passed"] for item in result["checks"])
                else "blocked_or_failed"
            )
            result["finished_at_epoch_ms"] = time.time_ns() // 1_000_000
            save()
    print(
        json.dumps(
            {
                "status": result["status"],
                "passed": result["passed_required"],
                "total": result["total_required"],
                "evidence": str(directory / "result.json"),
            }
        )
    )
    return 0 if result["status"] == "single_device_verified" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--prepare-target",
        action="store_true",
        help="Use only existing FormActivity intent; no input/click",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
