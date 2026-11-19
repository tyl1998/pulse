"""Fake-only P1.2 manager regressions, not physical dual-device acceptance."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pytest

from pulse_runtime.drivers.android import agent_transport, devices, scrcpy_video
from pulse_runtime.drivers.android.agent_transport import (
    ACCESSIBILITY_COMPONENT,
    SOCKET,
    SUPPORTED_BUILD,
    AccessibilitySettings,
    AdbdPeer,
    AgentHealth,
    AndroidBackend,
    ConnectionFault,
    DeviceRow,
    ForwardLease,
    InstalledAgent,
    validate_serial,
)
from pulse_runtime.drivers.android.devices import DeviceConnectionManager

SERIAL_A = "PULSE_A"
SERIAL_B = "PULSE_B"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
OTHER_SERVICE = "org.example.reader/.AccessibilityService"
ACTIVE_SETTINGS = AccessibilitySettings(f"{OTHER_SERVICE}:{ACCESSIBILITY_COMPONENT}", "1")
SUPPORTED_INSTALLED = InstalledAgent(
    SUPPORTED_BUILD.version_name,
    SUPPORTED_BUILD.version_code,
    True,
    SUPPORTED_BUILD.apk_sha256,
)
MUTATIONS = {
    "install",
    "claim_peer",
    "update_peer",
    "release_peer",
    "set_permissions",
    "restore_permissions",
}
Call = tuple[str, str | None, tuple[object, ...]]
Mapping = tuple[str, str, str]


def healthy_capabilities() -> dict[str, object]:
    return {
        "accessibility": "available",
        "screenshot": "available",
        "ocr": "available",
        "ocr_engine": "mlkit_chinese_bundled",
        "ocr_languages": ["zh", "en"],
        "actions": ["tap", "swipe", "set_text", "node_click"],
        "platform_operation_pending": False,
        "recovery_required": False,
        "minimum_android_api": 30,
        "display_scope": "default_only",
    }


@dataclass
class DeviceSnapshot:
    serial: str
    state: str = "device"
    present: bool = True
    transport_id: str = "1"
    model: str = "Fake_A"
    api: int = 36
    user: int = 0
    package: InstalledAgent | None = SUPPORTED_INSTALLED
    run_as: bool = True
    credential: str | None = TOKEN_A
    agent_process: int | None = 701
    adbd: AdbdPeer = field(default_factory=lambda: AdbdPeer(2000, 501))
    pin: str | None = "2000:501"
    settings: AccessibilitySettings = ACTIVE_SETTINGS
    lifecycle: str = field(default_factory=lambda: str(uuid4()))
    capabilities: dict[str, object] = field(default_factory=healthy_capabilities)
    bind_on_enable: bool = True
    settings_write_succeeds: bool = True
    loopback_verified: bool = True

    @property
    def row(self) -> DeviceRow:
        return DeviceRow(self.serial, self.state, self.transport_id, self.model)

    def stop_service(self, original: AccessibilitySettings) -> None:
        self.credential = None
        self.agent_process = None
        self.pin = None
        self.settings = original


@dataclass
class Gate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.finished.set()


class FakeAndroidBackend(AndroidBackend):
    """Device state is high-level; command accepts forwarding/get-state only."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshots = {
            SERIAL_A: DeviceSnapshot(SERIAL_A),
            SERIAL_B: DeviceSnapshot(
                SERIAL_B,
                transport_id="2",
                model="Fake_B",
                credential=TOKEN_B,
                agent_process=702,
                adbd=AdbdPeer(2000, 502),
                pin="2000:502",
            ),
        }
        self.calls: list[Call] = []
        self.forwards: set[Mapping] = set()
        self.next_port = 45000
        self.forced_port: dict[str, int] = {}
        self.creation_output: dict[str, str] = {}
        self.replace_creation: dict[str, tuple[str, str]] = {}
        self.failed_removals: set[str] = set()
        self.ineffective_removals: set[str] = set()
        self.faults: dict[tuple[str | None, str], str] = {}
        self.gates: dict[tuple[str, str], Gate] = {}
        self.peer_leases: dict[str, tuple[str, str | None, str]] = {}
        self.wire_sessions: list[str] = []

    async def visit(self, serial: str, name: str, *args: object) -> DeviceSnapshot:
        validate_serial(serial)
        assert serial in self.snapshots, "Device-targeting must use a known explicit serial"
        self.calls.append((name, serial, args))
        if gate := self.gates.get((serial, name)):
            await gate.wait()
        if code := self.faults.get((serial, name)):
            raise ConnectionFault(code)
        return self.snapshots[serial]

    def calls_for(self, name: str, serial: str | None = None) -> list[Call]:
        return [
            call for call in self.calls if call[0] == name and (serial is None or call[1] == serial)
        ]

    @property
    def mutations(self) -> list[Call]:
        return [call for call in self.calls if call[0] in MUTATIONS]

    async def inventory(self) -> list[DeviceRow]:
        self.calls.append(("inventory", None, ()))
        if code := self.faults.get((None, "inventory")):
            raise ConnectionFault(code)
        return [snapshot.row for snapshot in self.snapshots.values() if snapshot.present]

    async def command(
        self,
        serial: str,
        *args: str,
        timeout: float = 5.0,  # noqa: ASYNC109
    ) -> str:
        if args == ("get-state",):
            snapshot = await self.visit(serial, "get-state")
            return snapshot.state if snapshot.present else "absent"
        if args == ("forward", "--list"):
            await self.visit(serial, "forward_list")
            return "\n".join(" ".join(row) for row in sorted(self.forwards))
        if args == ("forward", "--no-rebind", "tcp:0", SOCKET):
            await self.visit(serial, "forward_create")
            port = self.forced_port.get(serial, self.next_port)
            self.next_port += 1
            endpoint = f"tcp:{port}"
            if not any(row[1] == endpoint for row in self.forwards):
                target_serial, target_socket = self.replace_creation.get(serial, (serial, SOCKET))
                self.forwards.add((target_serial, endpoint, target_socket))
            return self.creation_output.get(serial, str(port))
        if len(args) == 3 and args[:2] == ("forward", "--remove"):
            await self.visit(serial, "forward_remove", args[2])
            if serial in self.failed_removals:
                raise ConnectionFault("adb_offline")
            if serial not in self.ineffective_removals:
                self.forwards = {
                    row for row in self.forwards if not (row[0] == serial and row[1] == args[2])
                }
            return ""
        pytest.fail(f"Forbidden non-forward/get-state command: {serial} {args!r}")

    async def platform(self, serial: str) -> tuple[int, int]:
        snapshot = await self.visit(serial, "platform")
        return snapshot.api, snapshot.user

    async def installed(self, serial: str) -> InstalledAgent | None:
        return (await self.visit(serial, "installed")).package

    async def require_run_as(self, serial: str) -> None:
        if not (await self.visit(serial, "run_as")).run_as:
            raise ConnectionFault("run_as_unsupported")

    async def token(self, serial: str) -> str | None:
        return (await self.visit(serial, "token")).credential

    async def peer(self, serial: str) -> str | None:
        return (await self.visit(serial, "peer")).pin

    async def agent_pid(self, serial: str) -> int | None:
        return (await self.visit(serial, "agent_pid")).agent_process

    async def adbd_peer(self, serial: str) -> AdbdPeer:
        return (await self.visit(serial, "adbd_peer")).adbd

    async def permissions(self, serial: str) -> AccessibilitySettings:
        return (await self.visit(serial, "permissions")).settings

    async def claim_peer(self, serial: str, owner: str, peer: AdbdPeer) -> None:
        snapshot = await self.visit(serial, "claim_peer", owner, peer)
        if serial in self.peer_leases:
            raise ConnectionFault("configuration_in_use")
        self.peer_leases[serial] = owner, snapshot.pin, peer.config
        snapshot.pin = peer.config

    async def update_peer(self, serial: str, owner: str, peer: AdbdPeer) -> None:
        snapshot = await self.visit(serial, "update_peer", owner, peer)
        lease = self.peer_leases.get(serial)
        if lease is None or lease[0] != owner or snapshot.pin != lease[2]:
            raise ConnectionFault("configuration_in_use")
        self.peer_leases[serial] = owner, lease[1], peer.config
        snapshot.pin = peer.config

    async def release_peer(self, serial: str, owner: str) -> bool:
        snapshot = await self.visit(serial, "release_peer", owner)
        lease = self.peer_leases.get(serial)
        if lease is not None and lease[0] == owner:
            if snapshot.pin == lease[2]:
                snapshot.pin = lease[1]
            del self.peer_leases[serial]
        return True

    async def set_permissions(
        self, serial: str, expected: AccessibilitySettings, target: AccessibilitySettings
    ) -> bool:
        snapshot = await self.visit(serial, "set_permissions", expected, target)
        if snapshot.settings != expected or not snapshot.settings_write_succeeds:
            return False
        snapshot.settings = target
        if target.active and snapshot.bind_on_enable:
            snapshot.credential = TOKEN_A if serial == SERIAL_A else TOKEN_B
            snapshot.agent_process = 701 if serial == SERIAL_A else 702
        return True

    async def restore_permissions(
        self, serial: str, original: AccessibilitySettings, expected: AccessibilitySettings
    ) -> bool:
        snapshot = await self.visit(serial, "restore_permissions", original, expected)
        if snapshot.settings.services == expected.services:
            enabled = (
                original.enabled
                if snapshot.settings.enabled in {original.enabled, expected.enabled}
                else snapshot.settings.enabled
            )
            snapshot.settings = AccessibilitySettings(original.services, enabled)
        return True

    async def install(self, serial: str, apk: Path | None) -> None:
        snapshot = await self.visit(serial, "install", apk)
        assert snapshot.package is None, "A prepare request must not replace an installed package"
        snapshot.package = SUPPORTED_INSTALLED

    async def loopback(self, port: int) -> None:
        matching = [row for row in self.forwards if row[1] == f"tcp:{port}"]
        assert len(matching) == 1
        if not (await self.visit(matching[0][0], "loopback", port)).loopback_verified:
            raise ConnectionFault("forward_not_loopback")

    async def health(self, forward: ForwardLease, token: str) -> AgentHealth:
        snapshot = await self.visit(forward.serial, "health", forward.port)
        assert forward.mapping in self.forwards
        if token != snapshot.credential:
            raise ConnectionFault("authentication_failed")
        capabilities = copy.deepcopy(snapshot.capabilities)
        capabilities["error_log_session_id"] = snapshot.lifecycle
        wire_session = str(uuid4())
        self.wire_sessions.append(wire_session)
        return AgentHealth(snapshot.lifecycle, capabilities, wire_session)


@pytest.fixture(autouse=True)
def forbid_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("P1.2 regressions must not invoke physical ADB, processes, or host sockets")

    async def forbidden_async(*args: object, **kwargs: object) -> None:
        forbidden(*args, **kwargs)

    monkeypatch.setattr(agent_transport, "adb", forbidden_async)
    monkeypatch.setattr(agent_transport, "host_output", forbidden_async)
    monkeypatch.setattr(agent_transport, "find_adb", forbidden)
    monkeypatch.setattr(agent_transport, "read_health", forbidden_async)
    monkeypatch.setattr(scrcpy_video, "adb", forbidden_async)
    monkeypatch.setattr(scrcpy_video, "find_adb", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(asyncio, "open_connection", forbidden_async)


@pytest.fixture
async def rig() -> AsyncIterator[tuple[FakeAndroidBackend, DeviceConnectionManager]]:
    backend = FakeAndroidBackend()
    manager = DeviceConnectionManager(backend, apk=Path("/fake/Pulse-Agent.apk"), readiness_wait=0)
    try:
        yield backend, manager
    finally:
        for gate in backend.gates.values():
            gate.release.set()
        await manager.close()


def session_id(snapshot: dict[str, object]) -> str:
    connection = snapshot["connection"]
    assert isinstance(connection, dict)
    identifier = connection["session_id"]
    assert isinstance(identifier, str)
    return identifier


def diagnostic(snapshot: dict[str, object]) -> dict[str, object]:
    result = snapshot["diagnostic"]
    assert isinstance(result, dict)
    return result


async def test_dual_serial_sessions_credentials_capabilities_and_disconnect_are_isolated(
    rig,
) -> None:
    backend, manager = rig
    backend.snapshots[SERIAL_B].capabilities["ocr"] = "unavailable"
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    assert first["ready"] is True and second["ready"] is True
    first_id, second_id = session_id(first), session_id(second)
    assert first_id != second_id
    assert first["capabilities"]["ocr"] == "available"
    assert second["capabilities"]["ocr"] == "unavailable"
    assert first["connection"]["adbd_pid"] == 501
    assert second["connection"]["adbd_pid"] == 502
    assert len(backend.forwards) == 2
    assert {row[0] for row in backend.forwards} == {SERIAL_A, SERIAL_B}

    backend.calls.clear()
    await manager.health(SERIAL_A, first_id)
    assert {call[1] for call in backend.calls if call[1] is not None} == {SERIAL_A}
    before = list(backend.calls)
    for method in (manager.health, manager.disconnect):
        with pytest.raises(ConnectionFault, match=r"^stale_session$"):
            await method(SERIAL_B, first_id)
    assert backend.calls == before
    await manager.disconnect(SERIAL_A, first_id)
    assert all(row[0] == SERIAL_B for row in backend.forwards)
    assert session_id(await manager.health(SERIAL_B, second_id)) == second_id
    assert backend.mutations == []


@pytest.mark.parametrize(
    ("changes", "code", "status"),
    [
        ({"state": "unauthorized"}, "adb_unauthorized", "adb_unauthorized"),
        ({"state": "offline"}, "adb_offline", "offline"),
        ({"present": False}, "device_disconnected", "disconnected"),
        ({"api": 29}, "unsupported_android_api", "unsupported_android_api"),
        ({"user": 10}, "unsupported_android_user", "unsupported_configuration"),
        ({"package": None}, "agent_missing", "agent_missing"),
        (
            {"package": replace(SUPPORTED_INSTALLED, version_name="9.0.0")},
            "version_incompatible",
            "agent_incompatible",
        ),
        (
            {"package": replace(SUPPORTED_INSTALLED, version_code=99)},
            "version_incompatible",
            "agent_incompatible",
        ),
        (
            {"package": replace(SUPPORTED_INSTALLED, apk_sha256="c" * 64)},
            "build_signature_incompatible",
            "agent_incompatible",
        ),
        (
            {"package": replace(SUPPORTED_INSTALLED, debuggable=False)},
            "release_configuration_unsupported",
            "unsupported_configuration",
        ),
        ({"run_as": False}, "run_as_unsupported", "unsupported_configuration"),
    ],
    ids=[
        "unauthorized",
        "offline",
        "absent",
        "api-below-30",
        "secondary-user",
        "missing-agent",
        "version-name",
        "version-code",
        "artifact-build-signature",
        "release",
        "run-as",
    ],
)
async def test_incompatible_or_unavailable_device_fails_closed_without_targeting_other_serial(
    rig, changes: dict[str, object], code: str, status: str
) -> None:
    backend, manager = rig
    for name, value in changes.items():
        setattr(backend.snapshots[SERIAL_A], name, value)
    result = await manager.connect(SERIAL_A)
    assert result["ready"] is False
    assert result["status"] == status
    assert diagnostic(result)["code"] == code
    assert result["connection"]["session_id"] is None
    assert backend.mutations == [] and backend.forwards == set()
    assert {call[1] for call in backend.calls if call[1] is not None} <= {SERIAL_A}
    assert (await manager.connect(SERIAL_B))["ready"] is True


@pytest.mark.parametrize(
    "code", ["build_incompatible", "wire_incompatible", "version_incompatible"]
)
async def test_backend_compatibility_faults_are_manual_non_ready_and_never_install(
    rig, code
) -> None:
    backend, manager = rig
    backend.faults[SERIAL_A, "health" if code != "build_incompatible" else "installed"] = code
    result = await manager.prepare(SERIAL_A, install=True)
    assert result["ready"] is False and result["status"] == "agent_incompatible"
    assert diagnostic(result)["code"] == code
    assert diagnostic(result)["manual_required"] is True
    assert "will not uninstall or replace" in diagnostic(result)["guidance"]
    assert backend.calls_for("install") == []
    assert backend.forwards == set()


@pytest.mark.parametrize(
    "package",
    [
        replace(SUPPORTED_INSTALLED, version_name="older"),
        replace(SUPPORTED_INSTALLED, debuggable=False),
        replace(SUPPORTED_INSTALLED, apk_sha256="c" * 64),
    ],
    ids=["version", "release", "artifact"],
)
async def test_explicit_install_does_not_replace_incompatible_existing_agent(rig, package) -> None:
    backend, manager = rig
    backend.snapshots[SERIAL_A].package = package
    result = await manager.prepare(SERIAL_A, install=True)
    assert result["ready"] is False
    assert backend.snapshots[SERIAL_A].package == package
    assert backend.mutations == []


async def test_missing_agent_install_requires_explicit_prepare_opt_in(rig) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    snapshot.package = None
    assert (await manager.connect(SERIAL_A))["status"] == "agent_missing"
    assert (await manager.prepare(SERIAL_A, install=False))["status"] == "agent_missing"
    assert backend.mutations == []
    prepared = await manager.prepare(SERIAL_A, install=True)
    assert prepared["ready"] is True
    assert len(backend.calls_for("install", SERIAL_A)) == 1
    assert snapshot.settings.services.split(":") == [OTHER_SERVICE, ACCESSIBILITY_COMPONENT]
    assert all(call[1] == SERIAL_A for call in backend.mutations)


@pytest.mark.parametrize("failure", ["health-unavailable", "never-binds"])
async def test_successful_accessibility_setting_write_is_not_agent_readiness(rig, failure) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    if failure == "health-unavailable":
        snapshot.capabilities["accessibility"] = "unavailable"
    else:
        snapshot.bind_on_enable = False
    result = await manager.prepare(SERIAL_A)
    assert result["ready"] is False
    assert result["status"] == "manual_authorization_required"
    assert diagnostic(result)["code"] == "accessibility_not_connected"
    assert diagnostic(result)["manual_required"] is True
    assert "Settings > Accessibility" in diagnostic(result)["guidance"]
    assert "Runtime never clicks dialogs" in diagnostic(result)["guidance"]
    assert len(backend.calls_for("set_permissions", SERIAL_A)) == 1
    assert backend.calls_for("set_permissions", SERIAL_A)[0][2] == (
        original,
        original.with_agent(),
    )
    assert snapshot.settings == original and snapshot.pin == snapshot.adbd.config
    assert diagnostic(result)["awaiting_authorization"] is True
    assert SERIAL_A in backend.peer_leases and backend.forwards == set()
    assert await manager.cancel(SERIAL_A, diagnostic(result)["operation_id"]) is True
    assert snapshot.pin is None and backend.peer_leases == {}


@pytest.mark.parametrize(
    "original",
    [
        AccessibilitySettings(OTHER_SERVICE, "1"),
        AccessibilitySettings(OTHER_SERVICE, "0"),
        AccessibilitySettings("null", "null"),
        AccessibilitySettings("", "0"),
    ],
    ids=["other-active", "other-disabled", "unset", "empty"],
)
async def test_auto_enable_denied_has_manual_guidance_and_preserves_original_settings(
    rig, original
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.stop_service(original)
    snapshot.pin = "2000:400"
    snapshot.settings_write_succeeds = False
    result = await manager.prepare(SERIAL_A)
    assert result["status"] == "manual_authorization_required" and result["ready"] is False
    assert diagnostic(result)["code"] == "accessibility_enable_failed"
    assert "restricted settings" in diagnostic(result)["guidance"]
    assert snapshot.settings == original and snapshot.pin == snapshot.adbd.config
    assert backend.snapshots[SERIAL_B].settings == ACTIVE_SETTINGS
    assert backend.snapshots[SERIAL_B].pin == "2000:502"
    assert SERIAL_A in backend.peer_leases and backend.forwards == set()
    assert len(backend.calls_for("set_permissions", SERIAL_A)) == 1
    assert await manager.cancel(SERIAL_A, diagnostic(result)["operation_id"]) is True
    assert snapshot.pin == "2000:400" and snapshot.settings == original
    assert backend.peer_leases == {}


@pytest.mark.parametrize(
    "code",
    [
        "accessibility_policy_blocked",
        "accessibility_setting_denied",
        "accessibility_settings_changed",
    ],
)
async def test_enable_policy_denial_or_concurrent_edit_is_manual_and_never_retried(
    rig, code
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    backend.faults[SERIAL_A, "set_permissions"] = code
    result = await manager.prepare(SERIAL_A)
    assert result["status"] == "manual_authorization_required" and result["ready"] is False
    assert diagnostic(result)["code"] == code and diagnostic(result)["manual_required"] is True
    assert "Runtime never clicks dialogs" in diagnostic(result)["guidance"]
    assert snapshot.settings == original and snapshot.pin == snapshot.adbd.config
    assert diagnostic(result)["awaiting_authorization"] is True
    assert len(backend.calls_for("set_permissions", SERIAL_A)) == 1
    before = list(backend.mutations)
    await manager.list_devices()
    assert backend.mutations == before


async def test_prepare_can_leave_accessibility_for_manual_authorization_without_setting_writes(
    rig,
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    result = await manager.prepare(SERIAL_A, enable_accessibility=False)
    assert result["ready"] is False and result["status"] == "manual_authorization_required"
    assert backend.calls_for("set_permissions") == []
    assert snapshot.settings == original and snapshot.pin == snapshot.adbd.config
    assert await manager.cancel(SERIAL_A, diagnostic(result)["operation_id"]) is True
    assert snapshot.pin is None and snapshot.settings == original


async def test_first_bootstrap_readiness_uses_the_requested_timeout_not_the_poll_interval(
    rig, monkeypatch
) -> None:
    """A fresh Agent reloads its pin and mints a token; 3s of polling is not enough for that."""
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    waits: list[float] = []
    real = manager.prepare_once

    async def recording(*args, **kwargs):
        waits.append(kwargs["readiness_wait"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(manager, "prepare_once", recording)
    await manager.prepare(SERIAL_A, enable_accessibility=False, timeout=20)
    assert waits == [15.0]
    waits.clear()
    await manager.prepare(SERIAL_A, enable_accessibility=False, timeout=5)
    assert waits == [5.0]


async def test_owned_prepare_cleanup_preserves_subsequent_person_or_foreign_owner_changes(
    rig,
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    snapshot.pin = "2000:400"
    connected = await manager.prepare(SERIAL_A)
    assert connected["ready"] is True
    later = AccessibilitySettings(f"{snapshot.settings.services}:org.example.new/.Service", "1")
    snapshot.settings = later
    snapshot.pin = "2000:999"
    old_lease = backend.peer_leases[SERIAL_A]
    foreign_owner = str(uuid4())
    backend.peer_leases[SERIAL_A] = foreign_owner, old_lease[1], "2000:999"
    disconnected = await manager.disconnect(SERIAL_A, session_id(connected))
    assert disconnected["status"] == "disconnected"
    assert snapshot.settings == later and snapshot.pin == "2000:999"
    assert backend.peer_leases[SERIAL_A][0] == foreign_owner
    assert backend.snapshots[SERIAL_B].settings == ACTIVE_SETTINGS


async def test_disconnect_and_close_restore_only_runtime_owned_original_settings_and_pin(
    rig,
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    snapshot.stop_service(original)
    snapshot.pin = "2000:400"
    foreign = (SERIAL_B, "tcp:44000", "localabstract:scrcpy_foreign")
    backend.forwards.add(foreign)
    connected = await manager.prepare(SERIAL_A)
    await manager.disconnect(SERIAL_A, session_id(connected))
    assert snapshot.settings == original and snapshot.pin == "2000:400"
    assert backend.forwards == {foreign}
    assert backend.peer_leases == {}
    before = list(backend.mutations)
    await manager.close()
    await manager.close()
    assert backend.mutations == before and backend.forwards == {foreign}
    assert backend.snapshots[SERIAL_B].settings == ACTIVE_SETTINGS


@pytest.mark.parametrize("source", ["settings", "bound-health"])
async def test_permissions_revocation_invalidates_only_that_serial_and_rejects_old_session(
    rig, source
) -> None:
    backend, manager = rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    first_id, second_id = session_id(first), session_id(second)
    if source == "settings":
        backend.snapshots[SERIAL_A].settings = AccessibilitySettings(OTHER_SERVICE, "1")
    else:
        backend.snapshots[SERIAL_A].capabilities["accessibility"] = "unavailable"
    result = await manager.status(SERIAL_A)
    assert result["status"] == "permission_revoked" and result["ready"] is False
    assert diagnostic(result)["last_invalidation"] == "permission_revoked"
    assert diagnostic(result)["manual_required"] is True
    with pytest.raises(ConnectionFault, match=r"^stale_session$"):
        await manager.health(SERIAL_A, first_id)
    assert session_id(await manager.health(SERIAL_B, second_id)) == second_id
    assert all(row[0] == SERIAL_B for row in backend.forwards)
    assert backend.mutations == []


@pytest.mark.parametrize("change", ["transport", "agent-pid", "token", "adbd", "log-lifecycle"])
async def test_lifecycle_changes_replace_runtime_session_without_replaying_preparation(
    rig, change
) -> None:
    backend, manager = rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    first_id, second_id = session_id(first), session_id(second)
    snapshot = backend.snapshots[SERIAL_A]
    if change == "transport":
        snapshot.transport_id = "9"
    elif change == "agent-pid":
        snapshot.agent_process = 901
    elif change == "token":
        snapshot.credential = "c" * 64
    elif change == "adbd":
        snapshot.adbd = AdbdPeer(2000, 801)
        snapshot.pin = snapshot.adbd.config  # A person prepared the new Agent pin, not Runtime.
    else:
        snapshot.lifecycle = str(uuid4())
    result = await manager.status(SERIAL_A)
    assert result["ready"] is True
    assert session_id(result) != first_id
    assert diagnostic(result)["last_invalidation"] == (
        "agent_lifecycle_changed" if change == "log-lifecycle" else "generation_changed"
    )
    for method in (manager.health, manager.disconnect):
        with pytest.raises(ConnectionFault, match=r"^stale_session$"):
            await method(SERIAL_A, first_id)
    assert session_id(await manager.health(SERIAL_B, second_id)) == second_id
    assert len(backend.forwards) == 2 and backend.mutations == []


async def test_wire_request_session_changes_do_not_invalidate_agent_lifecycle(rig) -> None:
    backend, manager = rig
    connected = await manager.connect(SERIAL_A)
    identifier = session_id(connected)
    first = await manager.health(SERIAL_A, identifier)
    second = await manager.health(SERIAL_A, identifier)
    assert session_id(first) == session_id(second) == identifier
    assert len(set(backend.wire_sessions)) == 3
    assert diagnostic(second)["last_invalidation"] is None
    assert len(backend.calls_for("forward_create")) == 1


async def test_restarted_agent_without_the_pin_file_is_adopted_not_rejected_as_stale(
    rig,
) -> None:
    """First bootstrap: the pin file is gone but the Agent already minted a token for it."""
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    # The previous session's release removed the file; the Agent keeps its token and pin.
    snapshot.pin = None
    result = await manager.prepare(SERIAL_A, enable_accessibility=True)
    assert result["ready"] is True
    assert snapshot.pin == snapshot.adbd.config
    assert snapshot.credential == TOKEN_A


async def test_adbd_pid_change_with_stale_pin_requires_manual_configuration_without_rewrite(
    rig,
) -> None:
    backend, manager = rig
    connected = await manager.connect(SERIAL_A)
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.adbd = AdbdPeer(2000, 801)
    result = await manager.status(SERIAL_A)
    assert result["status"] == "configuration_required" and result["ready"] is False
    assert diagnostic(result)["code"] == "peer_configuration_stale"
    assert diagnostic(result)["last_invalidation"] == "peer_configuration_stale"
    assert "cannot reload its private adbd pin" in diagnostic(result)["guidance"]
    prepared = await manager.prepare(SERIAL_A, install=True, enable_accessibility=True)
    assert prepared["ready"] is False
    assert snapshot.pin == "2000:501"
    assert snapshot.credential == TOKEN_A and snapshot.settings == ACTIVE_SETTINGS
    assert backend.mutations == []
    with pytest.raises(ConnectionFault, match=r"^stale_session$"):
        await manager.health(SERIAL_A, session_id(connected))


async def test_discovery_never_prepares_installs_or_connects_unrequested_devices(rig) -> None:
    backend, manager = rig
    backend.snapshots[SERIAL_B].package = None
    result = await manager.list_devices()
    by_serial = {entry["serial"]: entry for entry in result["devices"]}
    assert by_serial[SERIAL_A]["status"] == "agent_available"
    assert by_serial[SERIAL_A]["ready"] is False
    assert by_serial[SERIAL_B]["status"] == "agent_missing"
    assert backend.forwards == set() and backend.mutations == []


async def test_monitor_usb_reconnect_is_read_only_except_owned_transport_leases(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, manager = rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    first_id, second_id = session_id(first), session_id(second)
    ticks = [Gate(), Gate(), Gate()]
    tick_iterator = iter(ticks)

    async def event_tick(interval: float) -> None:
        assert interval == manager.poll_interval
        await next(tick_iterator).wait()

    monkeypatch.setattr(devices.asyncio, "sleep", event_tick)
    manager.start()
    try:
        await asyncio.wait_for(ticks[0].entered.wait(), 2)
        backend.snapshots[SERIAL_A].state = "offline"
        ticks[0].release.set()
        await asyncio.wait_for(ticks[1].entered.wait(), 2)
        offline = manager.snapshot(manager.slots[SERIAL_A])
        assert offline["status"] == "offline" and offline["ready"] is False
        assert session_id(manager.snapshot(manager.slots[SERIAL_B])) == second_id
        backend.snapshots[SERIAL_A].state = "device"
        backend.snapshots[SERIAL_A].transport_id = "9"
        ticks[1].release.set()
        await asyncio.wait_for(ticks[2].entered.wait(), 2)
        reconnected = manager.snapshot(manager.slots[SERIAL_A])
        assert reconnected["ready"] is True and reconnected["transport_id"] == "9"
        assert session_id(reconnected) != first_id
        assert session_id(manager.snapshot(manager.slots[SERIAL_B])) == second_id
        assert backend.mutations == []
        assert len(backend.calls_for("forward_create", SERIAL_A)) == 2
        with pytest.raises(ConnectionFault, match=r"^stale_session$"):
            await manager.health(SERIAL_A, first_id)
    finally:
        await manager.close()


@pytest.mark.parametrize("fault", ["recovery_required", "action_uncertain", "health-capability"])
async def test_recovery_latch_blocks_monitor_prepare_and_connect_without_explicit_confirmation(
    rig, fault
) -> None:
    backend, manager = rig
    connected = await manager.connect(SERIAL_A)
    snapshot = backend.snapshots[SERIAL_A]
    if fault == "health-capability":
        snapshot.capabilities["recovery_required"] = True
    else:
        backend.faults[SERIAL_A, "health"] = fault
    blocked = await manager.status(SERIAL_A)
    assert blocked["status"] == "recovery_required" and blocked["ready"] is False
    assert diagnostic(blocked)["manual_required"] is True
    assert "Check the actual device state" in diagnostic(blocked)["guidance"]
    assert "Runtime never restarts it" in diagnostic(blocked)["guidance"]
    assert manager.slots[SERIAL_A].quarantine is not None
    backend.faults.pop((SERIAL_A, "health"), None)
    snapshot.capabilities["recovery_required"] = False
    snapshot.agent_process = 901
    snapshot.credential = "c" * 64
    snapshot.lifecycle = str(uuid4())
    snapshot.package = None
    calls = len(backend.calls_for("forward_create"))
    assert (await manager.list_devices())["devices"][0]["status"] == "recovery_required"
    snapshot.package = SUPPORTED_INSTALLED
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    assert (await manager.prepare(SERIAL_A, install=True))["status"] == "recovery_required"
    assert len(backend.calls_for("forward_create")) == calls
    assert backend.mutations == []
    with pytest.raises(ConnectionFault, match=r"^stale_session$"):
        await manager.health(SERIAL_A, session_id(connected))


@pytest.mark.parametrize(
    ("changed", "confirmed", "ready"),
    [(False, False, False), (False, True, False), (True, False, False), (True, True, True)],
    ids=["no-confirmation", "same-generation", "new-without-confirmation", "confirmed-new"],
)
async def test_manual_recovery_requires_confirmation_and_observable_healthy_new_generation(
    rig, changed, confirmed, ready
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["recovery_required"] = True
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    snapshot.capabilities["recovery_required"] = False
    if changed:
        snapshot.agent_process = 901
        snapshot.credential = "c" * 64
        snapshot.lifecycle = str(uuid4())
    result = await manager.connect(SERIAL_A, manual_recovery_confirmed=confirmed)
    assert result["ready"] is ready
    assert result["status"] == ("ready" if ready else "recovery_required")
    assert (manager.slots[SERIAL_A].quarantine is None) is ready
    assert backend.mutations == []


@pytest.mark.parametrize("unchanged", ["agent-process", "log-lifecycle"])
async def test_token_or_listener_changes_alone_are_not_manual_process_recovery(
    rig, unchanged
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["recovery_required"] = True
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    snapshot.capabilities["recovery_required"] = False
    snapshot.credential = "c" * 64
    snapshot.transport_id = "9"
    if unchanged == "agent-process":
        snapshot.lifecycle = str(uuid4())
    else:
        snapshot.agent_process = 901
    result = await manager.connect(SERIAL_A, manual_recovery_confirmed=True)
    assert result["status"] == "recovery_required" and result["ready"] is False
    assert diagnostic(result)["code"] == "manual_recovery_not_observed"
    assert manager.slots[SERIAL_A].quarantine is not None and backend.mutations == []


@pytest.mark.parametrize("recovering", [False, True], ids=["connect", "manual-recovery"])
async def test_generation_change_during_health_never_publishes_ready_or_clears_latch(
    rig, recovering
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    if recovering:
        snapshot.capabilities["recovery_required"] = True
        assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
        snapshot.capabilities["recovery_required"] = False
        snapshot.agent_process = 901
        snapshot.credential = "c" * 64
        snapshot.lifecycle = str(uuid4())
    gate = Gate()
    backend.gates[SERIAL_A, "health"] = gate
    operation = asyncio.create_task(manager.connect(SERIAL_A, manual_recovery_confirmed=recovering))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        snapshot.agent_process = 902
        gate.release.set()
        result = await asyncio.wait_for(operation, 2)
        assert result["ready"] is False and result["connection"]["session_id"] is None
        assert diagnostic(result)["code"] == "connection_changed"
        assert result["status"] == ("recovery_required" if recovering else "connection_failed")
        assert (manager.slots[SERIAL_A].quarantine is not None) is recovering
        assert backend.forwards == set() and backend.mutations == []
        if recovering:
            assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    finally:
        gate.release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("fault", ["recovery_required", "action_uncertain"])
async def test_initial_uncertain_health_without_known_lifecycle_cannot_recover_same_generation(
    rig, fault
) -> None:
    backend, manager = rig
    backend.faults[SERIAL_A, "health"] = fault
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    backend.faults.pop((SERIAL_A, "health"))
    same = await manager.connect(SERIAL_A, manual_recovery_confirmed=True)
    assert same["ready"] is False and same["status"] == "recovery_required"
    assert manager.slots[SERIAL_A].quarantine is not None
    backend.snapshots[SERIAL_A].agent_process = 901
    backend.snapshots[SERIAL_A].credential = "c" * 64
    fresh = await manager.connect(SERIAL_A, manual_recovery_confirmed=True)
    assert fresh["ready"] is True and manager.slots[SERIAL_A].quarantine is None
    assert backend.mutations == []


@pytest.mark.parametrize("unhealthy", ["accessibility-unavailable", "platform-operation-pending"])
async def test_non_ready_manual_recovery_candidate_must_not_clear_uncertainty_latch(
    rig, unhealthy
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["recovery_required"] = True
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    snapshot.capabilities["recovery_required"] = False
    snapshot.agent_process = 901
    snapshot.credential = "c" * 64
    snapshot.lifecycle = str(uuid4())
    if unhealthy == "accessibility-unavailable":
        snapshot.capabilities["accessibility"] = "unavailable"
    else:
        snapshot.capabilities["platform_operation_pending"] = True
    result = await manager.connect(SERIAL_A, manual_recovery_confirmed=True)
    assert result["ready"] is False and result["status"] == "recovery_required"
    assert manager.slots[SERIAL_A].quarantine is not None
    assert diagnostic(result)["manual_required"] is True
    snapshot.capabilities["accessibility"] = "available"
    snapshot.capabilities["platform_operation_pending"] = False
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    assert (await manager.connect(SERIAL_A, manual_recovery_confirmed=True))["ready"] is True
    assert backend.mutations == []


async def test_missing_token_in_quarantined_same_process_does_not_authorize_prepare_or_restart(
    rig,
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["recovery_required"] = True
    await manager.connect(SERIAL_A)
    snapshot.credential = None
    snapshot.settings = AccessibilitySettings(OTHER_SERVICE, "1")
    result = await manager.prepare(SERIAL_A, manual_recovery_confirmed=True, install=True)
    assert result["status"] == "recovery_required" and result["ready"] is False
    assert diagnostic(result)["code"] == "manual_recovery_not_observed"
    assert backend.mutations == []
    assert snapshot.agent_process == 701 and snapshot.pin == "2000:501"


async def test_platform_operation_pending_is_busy_not_ready_and_keeps_same_session(rig) -> None:
    backend, manager = rig
    backend.snapshots[SERIAL_A].capabilities["platform_operation_pending"] = True
    busy = await manager.connect(SERIAL_A)
    assert busy["status"] == "agent_busy" and busy["ready"] is False
    assert diagnostic(busy)["code"] == "platform_operation_pending"
    identifier = session_id(busy)
    backend.snapshots[SERIAL_A].capabilities["platform_operation_pending"] = False
    ready = await manager.health(SERIAL_A, identifier)
    assert ready["ready"] is True and session_id(ready) == identifier
    assert backend.mutations == []


@pytest.mark.parametrize("owner", [SERIAL_A, SERIAL_B])
async def test_forward_conflict_never_deletes_existing_or_foreign_mapping(rig, owner) -> None:
    backend, manager = rig
    foreign = owner, "tcp:44000", SOCKET if owner == SERIAL_A else "localabstract:foreign"
    backend.forwards.add(foreign)
    backend.forced_port[SERIAL_A] = 44000
    result = await manager.connect(SERIAL_A)
    assert result["ready"] is False and diagnostic(result)["code"] == "forward_conflict"
    assert backend.forwards == {foreign}
    assert backend.calls_for("forward_remove") == []
    await manager.close()
    assert backend.forwards == {foreign}


@pytest.mark.parametrize("replacement_serial", [SERIAL_A, SERIAL_B])
async def test_replaced_forward_during_connect_is_not_adopted_or_cleaned_as_owned(
    rig, replacement_serial
) -> None:
    backend, manager = rig
    backend.replace_creation[SERIAL_A] = replacement_serial, "localabstract:foreign"
    result = await manager.connect(SERIAL_A)
    assert result["ready"] is False and diagnostic(result)["code"] == "forward_replaced"
    assert backend.forwards == {(replacement_serial, "tcp:45000", "localabstract:foreign")}
    assert backend.calls_for("health") == [] and backend.calls_for("forward_remove") == []


async def test_foreign_replacement_of_connected_forward_is_preserved_on_invalidation_and_close(
    rig,
) -> None:
    backend, manager = rig
    connected = await manager.connect(SERIAL_A)
    owned = next(iter(backend.forwards))
    foreign = SERIAL_A, owned[1], "localabstract:scrcpy_foreign"
    backend.forwards = {foreign}
    with pytest.raises(ConnectionFault, match=r"^stale_session$"):
        await manager.health(SERIAL_A, session_id(connected))
    snapshot = manager.snapshot(manager.slots[SERIAL_A])
    assert snapshot["ready"] is False and diagnostic(snapshot)["code"] == "forward_replaced"
    assert diagnostic(snapshot)["last_invalidation"] == "forward_replaced"
    assert backend.forwards == {foreign} and backend.calls_for("forward_remove") == []
    await manager.close()
    assert backend.forwards == {foreign}


async def test_ambiguous_forward_creation_requires_manual_cleanup_without_ownership_guess(
    rig,
) -> None:
    backend, manager = rig
    backend.creation_output[SERIAL_A] = ""
    result = await manager.connect(SERIAL_A)
    assert result["ready"] is False and diagnostic(result)["code"] == "forward_creation_uncertain"
    assert diagnostic(result)["manual_required"] is True
    assert "not delete unknown or foreign forwards" in diagnostic(result)["guidance"]
    assert backend.calls_for("forward_remove") == []
    assert backend.forwards == {(SERIAL_A, "tcp:45000", SOCKET)}
    await manager.close()
    assert backend.forwards == {(SERIAL_A, "tcp:45000", SOCKET)}


async def test_unverified_loopback_forward_never_becomes_ready_and_owned_forward_is_cleaned(
    rig,
) -> None:
    backend, manager = rig
    backend.snapshots[SERIAL_A].loopback_verified = False
    result = await manager.connect(SERIAL_A)
    assert result["ready"] is False and diagnostic(result)["code"] == "forward_not_loopback"
    assert backend.calls_for("health") == [] and backend.forwards == set()
    assert len(backend.calls_for("forward_remove", SERIAL_A)) == 1


@pytest.mark.parametrize("failure", ["command-fails", "mapping-remains"])
async def test_failed_owned_forward_cleanup_is_visible_and_retried_without_touching_foreign(
    rig, failure
) -> None:
    backend, manager = rig
    foreign = SERIAL_B, "tcp:44000", "localabstract:foreign"
    backend.forwards.add(foreign)
    connected = await manager.connect(SERIAL_A)
    if failure == "command-fails":
        backend.failed_removals.add(SERIAL_A)
    else:
        backend.ineffective_removals.add(SERIAL_A)
    result = await manager.disconnect(SERIAL_A, session_id(connected))
    assert result["ready"] is False and diagnostic(result)["cleanup_pending"] is True
    assert manager.slots[SERIAL_A].pending_forwards
    backend.failed_removals.clear()
    backend.ineffective_removals.clear()
    await manager.close()
    assert backend.forwards == {foreign}
    assert manager.slots[SERIAL_A].pending_forwards == []
    assert diagnostic(manager.snapshot(manager.slots[SERIAL_A]))["cleanup_pending"] is False


@pytest.mark.parametrize("failed_cleanup", ["release_peer", "restore_permissions"])
async def test_forward_retry_does_not_hide_pending_private_configuration_or_settings_cleanup(
    rig, failed_cleanup
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    snapshot.stop_service(original)
    prepared = await manager.prepare(SERIAL_A)
    backend.faults[SERIAL_A, failed_cleanup] = "adb_offline"
    result = await manager.disconnect(SERIAL_A, session_id(prepared))
    assert diagnostic(result)["cleanup_pending"] is True and backend.forwards == set()
    slot = manager.slots[SERIAL_A]
    await manager.clean_forwards(slot)
    assert diagnostic(manager.snapshot(slot))["cleanup_pending"] is True
    if failed_cleanup == "release_peer":
        assert slot.peer_owner is not None
    else:
        assert slot.accessibility_lease is not None
    backend.faults.pop((SERIAL_A, failed_cleanup))
    await manager.close()
    assert snapshot.settings == original and snapshot.pin is None
    assert diagnostic(manager.snapshot(slot))["cleanup_pending"] is False


@pytest.mark.parametrize("phase", ["forward_create", "set_permissions", "install"])
async def test_cancel_finishes_one_issued_mutation_cleans_owned_state_and_never_replays(
    rig, phase
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    preparing = phase != "forward_create"
    if preparing:
        snapshot.stop_service(original)
    if phase == "install":
        snapshot.package = None
    gate = Gate()
    backend.gates[SERIAL_A, phase] = gate
    identifier = str(uuid4())
    operation = asyncio.create_task(
        manager.prepare(SERIAL_A, install=True, operation_id=identifier)
        if preparing
        else manager.connect(SERIAL_A, operation_id=identifier)
    )
    await asyncio.wait_for(gate.entered.wait(), 2)
    pending = await manager.status(SERIAL_A)
    assert diagnostic(pending)["operation_id"] == identifier
    assert pending["ready"] is False
    assert await manager.cancel(SERIAL_A, str(uuid4())) is False
    assert await manager.cancel(SERIAL_B, identifier) is False
    other = await manager.connect(SERIAL_B)
    assert other["ready"] is True
    blocked = await manager.prepare(SERIAL_A)
    assert blocked["operation_error"] == "operation_in_progress"
    assert diagnostic(blocked)["operation_id"] == identifier
    cancel_started = asyncio.Event()

    async def cancel() -> bool:
        cancel_started.set()
        return await manager.cancel(SERIAL_A, identifier)

    cancelling = asyncio.create_task(cancel())
    try:
        await asyncio.wait_for(cancel_started.wait(), 2)
        assert not gate.cancelled.is_set() and not cancelling.done()
        gate.release.set()
        assert await asyncio.wait_for(cancelling, 2) is True
        with pytest.raises(asyncio.CancelledError):
            await operation
        cancelled = manager.snapshot(manager.slots[SERIAL_A])
        assert cancelled["status"] == "cancelled" and cancelled["ready"] is False
        assert diagnostic(cancelled)["operation_id"] is None
        assert diagnostic(cancelled)["cleanup_pending"] is False
        assert len(backend.calls_for(phase, SERIAL_A)) == 1
        assert gate.finished.is_set() and not gate.cancelled.is_set()
        assert all(row[0] == SERIAL_B for row in backend.forwards)
        if preparing:
            assert snapshot.settings == original and snapshot.pin is None
        assert session_id(await manager.health(SERIAL_B, session_id(other))) == session_id(other)
    finally:
        gate.release.set()
        await asyncio.gather(operation, cancelling, return_exceptions=True)


@pytest.mark.parametrize("preparing", [False, True], ids=["connect", "prepare"])
async def test_event_expired_operation_timeout_clears_pending_id_and_cleans_owned_forward(
    rig, monkeypatch: pytest.MonkeyPatch, preparing
) -> None:
    backend, manager = rig
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    if preparing:
        backend.snapshots[SERIAL_A].stop_service(original)
    gate = Gate()
    backend.gates[SERIAL_A, "health"] = gate
    deadline = asyncio.timeout(None)
    monkeypatch.setattr(devices.asyncio, "timeout", lambda _seconds: deadline)
    identifier = str(uuid4())
    operation = asyncio.create_task(
        manager.prepare(SERIAL_A, operation_id=identifier)
        if preparing
        else manager.connect(SERIAL_A, operation_id=identifier)
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        pending = await manager.status(SERIAL_A)
        assert diagnostic(pending)["operation_id"] == identifier
        assert pending["status"] == ("preparing" if preparing else "connecting")
        assert pending["ready"] is False
        deadline.reschedule(asyncio.get_running_loop().time())
        result = await asyncio.wait_for(operation, 2)
        assert deadline.expired() and gate.cancelled.is_set()
        assert result["status"] == "connection_timeout" and result["ready"] is False
        assert diagnostic(result)["code"] == "timeout"
        assert diagnostic(result)["operation_id"] is None
        assert backend.forwards == set()
        if preparing:
            assert backend.snapshots[SERIAL_A].settings == original
            assert backend.snapshots[SERIAL_A].pin is None
        else:
            assert backend.mutations == []
        assert len(backend.calls_for("forward_create", SERIAL_A)) == 1
        assert len(backend.calls_for("health", SERIAL_A)) == 1
    finally:
        gate.release.set()
        await asyncio.gather(operation, return_exceptions=True)


async def test_close_cancels_pending_operation_and_only_cleans_this_runtime_owned_resources(
    rig,
) -> None:
    backend, manager = rig
    foreign = SERIAL_B, "tcp:44000", "localabstract:foreign"
    backend.forwards.add(foreign)
    gate = Gate()
    backend.gates[SERIAL_A, "health"] = gate
    operation = asyncio.create_task(manager.connect(SERIAL_A, operation_id=str(uuid4())))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await asyncio.wait_for(manager.close(), 2)
    assert operation.cancelled() and gate.cancelled.is_set()
    assert backend.forwards == {foreign} and backend.mutations == []
    assert manager.slots[SERIAL_A].operation is None
    assert manager.slots[SERIAL_A].operation_id is None
    assert manager.slots[SERIAL_A].session is None
    with pytest.raises(ConnectionFault, match=r"^manager_closed$"):
        await manager.connect(SERIAL_A)


async def test_new_serial_during_slow_discovery_failure_does_not_break_invalidation(rig) -> None:
    backend, manager = rig
    await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    backend.faults[None, "inventory"] = "adb_unavailable"
    gate = Gate()
    backend.gates[SERIAL_A, "forward_list"] = gate
    listing = asyncio.create_task(manager.list_devices())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        added = await manager.status("NEW_REQUEST")
        assert added["ready"] is False
        gate.release.set()
        result = await asyncio.wait_for(listing, 2)
        assert result["error"] == "adb_unavailable"
        assert all(entry["ready"] is False for entry in result["devices"])
        assert backend.forwards == set()
    finally:
        gate.release.set()
        await asyncio.gather(listing, return_exceptions=True)


async def test_new_serial_during_slow_cleanup_does_not_terminate_watcher(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, manager = rig
    connected = await manager.connect(SERIAL_A)
    await manager.connect(SERIAL_B)
    backend.failed_removals.add(SERIAL_A)
    await manager.disconnect(SERIAL_A, session_id(connected))
    backend.failed_removals.clear()
    gate = Gate()
    backend.gates[SERIAL_A, "forward_list"] = gate
    ticks = [Gate(), Gate()]
    iterator = iter(ticks)

    async def event_tick(_interval: float) -> None:
        await next(iterator).wait()

    monkeypatch.setattr(devices.asyncio, "sleep", event_tick)
    manager.start()
    try:
        await asyncio.wait_for(ticks[0].entered.wait(), 2)
        ticks[0].release.set()
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert (await manager.status("NEW_REQUEST"))["ready"] is False
        gate.release.set()
        await asyncio.wait_for(ticks[1].entered.wait(), 2)
        assert manager.watcher is not None and not manager.watcher.done()
        assert manager.slots[SERIAL_A].cleanup_pending is False
        assert all(mapping[0] == SERIAL_B for mapping in backend.forwards)
    finally:
        gate.release.set()
        await manager.close()


async def test_snapshots_copy_capabilities_and_never_expose_agent_credential(rig, caplog) -> None:
    backend, manager = rig
    caplog.set_level("DEBUG", logger="pulse_runtime.drivers.android.devices")
    connected = await manager.connect(SERIAL_A)
    connected["capabilities"]["actions"].clear()
    fresh = await manager.health(SERIAL_A, session_id(connected))
    assert fresh["capabilities"]["actions"] == ["tap", "swipe", "set_text", "node_click"]
    output = json.dumps(await manager.list_devices()) + repr(manager.slots) + caplog.text
    assert TOKEN_A not in output and TOKEN_B not in output
    assert "credential" not in json.dumps(fresh) and "token" not in json.dumps(fresh)
    assert backend.mutations == []


@pytest.mark.parametrize("phase", ["watcher-drain", "slot-lock"])
async def test_cancelled_close_finishes_all_owned_cleanup_and_remains_retryable(rig, phase) -> None:
    backend, manager = rig
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    backend.snapshots[SERIAL_A].stop_service(original)
    assert (await manager.prepare(SERIAL_A))["ready"] is True
    await manager.connect(SERIAL_B)
    foreign = SERIAL_B, "tcp:44000", "localabstract:foreign"
    backend.forwards.add(foreign)
    gate = Gate()
    slot = manager.slots[SERIAL_A]
    if phase == "watcher-drain":
        started = asyncio.Event()

        async def slow_watcher() -> None:
            started.set()
            try:
                await asyncio.Future()
            finally:
                await devices.finish_cleanup(gate.wait())

        manager.watcher = asyncio.create_task(slow_watcher())
        await started.wait()
    else:

        class ObservedLock(asyncio.Lock):
            async def __aenter__(self) -> None:
                gate.entered.set()
                await super().__aenter__()

        slot.lock = ObservedLock()
        await slot.lock.acquire()
    closing = asyncio.create_task(manager.close())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        closing.cancel()
        if phase == "slot-lock":
            slot.lock.release()
        else:
            gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)
        await manager.close()
        assert backend.forwards == {foreign}
        assert backend.snapshots[SERIAL_A].settings == original
        assert backend.snapshots[SERIAL_A].pin is None
        assert backend.peer_leases == {}
        assert all(value.session is None for value in manager.slots.values())
        assert all(not value.cleanup_pending for value in manager.slots.values())
    finally:
        gate.release.set()
        if phase == "slot-lock" and slot.lock.locked():
            slot.lock.release()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("change", ["unpinned-build", "unsupported-user"])
async def test_compatibility_is_reverified_for_generation_accepted_after_inspection(
    rig, change
) -> None:
    backend, manager = rig
    gate = Gate()
    backend.gates[SERIAL_A, "adbd_peer"] = gate
    connecting = asyncio.create_task(manager.connect(SERIAL_A))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        snapshot = backend.snapshots[SERIAL_A]
        if change == "unpinned-build":
            snapshot.package = replace(SUPPORTED_INSTALLED, apk_sha256="c" * 64)
        else:
            snapshot.user = 10
        gate.release.set()
        result = await asyncio.wait_for(connecting, 2)
        assert result["ready"] is False
        assert result["connection"]["session_id"] is None
        assert result["status"] == (
            "agent_incompatible" if change == "unpinned-build" else "unsupported_configuration"
        )
        assert backend.forwards == set() and backend.mutations == []
    finally:
        gate.release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.parametrize("mode", ["manual-request", "auto-denied", "service-unbound"])
async def test_owned_manual_authorization_bootstrap_survives_until_user_connects(rig, mode) -> None:
    backend, manager = rig
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.stop_service(original)
    snapshot.settings_write_succeeds = mode != "auto-denied"
    snapshot.bind_on_enable = mode != "service-unbound"
    identifier = str(uuid4())
    pending = await manager.prepare(
        SERIAL_A, enable_accessibility=mode != "manual-request", operation_id=identifier
    )
    assert pending["ready"] is False
    assert pending["status"] == "manual_authorization_required"
    assert diagnostic(pending)["awaiting_authorization"] is True
    assert diagnostic(pending)["operation_id"] == identifier
    assert snapshot.pin == snapshot.adbd.config and SERIAL_A in backend.peer_leases
    assert snapshot.settings == original
    before = list(backend.mutations)
    # The operator enables the service after receiving guidance. Agent loads this retained pin.
    snapshot.settings = ACTIVE_SETTINGS
    snapshot.credential = TOKEN_A
    snapshot.agent_process = 801
    snapshot.lifecycle = str(uuid4())
    connected = await manager.connect(SERIAL_A)
    assert connected["ready"] is True
    assert diagnostic(connected)["awaiting_authorization"] is False
    assert diagnostic(connected)["operation_id"] is None
    assert backend.mutations == before
    assert await manager.cancel(SERIAL_A, identifier) is False
    await manager.disconnect(SERIAL_A, session_id(connected))
    assert snapshot.pin is None
    assert snapshot.settings == ACTIVE_SETTINGS  # Preserve the operator's settings change.


async def test_pending_manual_authorization_can_be_cancelled_by_its_owned_id(rig) -> None:
    backend, manager = rig
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    backend.snapshots[SERIAL_A].stop_service(original)
    identifier = str(uuid4())
    pending = await manager.prepare(SERIAL_A, enable_accessibility=False, operation_id=identifier)
    assert diagnostic(pending)["operation_id"] == identifier
    assert await manager.cancel(SERIAL_B, identifier) is False
    assert await manager.cancel(SERIAL_A, str(uuid4())) is False
    assert await manager.cancel(SERIAL_A, identifier) is True
    assert manager.slots[SERIAL_A].desired is False
    assert manager.slots[SERIAL_A].operation_id is None
    assert backend.snapshots[SERIAL_A].pin is None
    assert backend.snapshots[SERIAL_A].settings == original
    assert backend.peer_leases == {} and backend.forwards == set()


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
async def test_cancel_or_timeout_during_manual_handshake_releases_pending_bootstrap(
    rig, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    backend, manager = rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.stop_service(AccessibilitySettings(OTHER_SERVICE, "0"))
    await manager.prepare(SERIAL_A, enable_accessibility=False)
    snapshot.settings = ACTIVE_SETTINGS
    snapshot.credential = TOKEN_A
    snapshot.agent_process = 801
    gate = Gate()
    backend.gates[SERIAL_A, "health"] = gate
    deadline = asyncio.timeout(None)
    if failure == "timeout":
        monkeypatch.setattr(devices.asyncio, "timeout", lambda _seconds: deadline)
    connecting = asyncio.create_task(manager.connect(SERIAL_A))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        if failure == "cancel":
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(connecting, 2)
        else:
            deadline.reschedule(asyncio.get_running_loop().time())
            result = await asyncio.wait_for(connecting, 2)
            assert result["status"] == "connection_timeout"
        assert backend.peer_leases == {} and snapshot.pin is None
        assert backend.forwards == set()
        assert manager.slots[SERIAL_A].awaiting_authorization is False
        assert manager.slots[SERIAL_A].operation_id is None
        assert snapshot.settings == ACTIVE_SETTINGS
    finally:
        gate.release.set()
        await asyncio.gather(connecting, return_exceptions=True)
