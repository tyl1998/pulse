"""Runtime observation isolation/lifecycle/API tests, exclusively synthetic Android evidence."""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import importlib.util
import json
import struct
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx2 import ASGITransport, AsyncClient

from pulse_runtime.api import app as api_module
from pulse_runtime.api.app import AgentObserveBody, create_app
from pulse_runtime.drivers.android import devices
from pulse_runtime.drivers.android.agent_observe import ObserveExchange, ObserveReply
from pulse_runtime.drivers.android.agent_transport import (
    AccessibilitySettings,
    AdbdPeer,
    ConnectionFault,
    ForwardLease,
)
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.drivers.android.scrcpy_video import VideoOptions
from pulse_runtime.stream.hub import StreamHub
from runtime.tests.test_device_connections import (
    OTHER_SERVICE,
    SERIAL_A,
    SERIAL_B,
    TOKEN_A,
    TOKEN_B,
    FakeAndroidBackend,
    Gate,
    session_id,
)
from runtime.tests.test_device_connections import (
    forbid_external_io as forbid_external_io,
)
from runtime.tests.test_device_connections_api import FakeVideoSource

_acceptance_spec = importlib.util.spec_from_file_location(
    "p1_observe_acceptance", Path(__file__).resolve().parents[2] / "scripts" / "p1_observe.py"
)
assert _acceptance_spec is not None and _acceptance_spec.loader is not None
_acceptance = importlib.util.module_from_spec(_acceptance_spec)
_acceptance_spec.loader.exec_module(_acceptance)
safe_observation = _acceptance.safe_observation

API_TOKEN = "observe-runtime-secret"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


def png() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    image = b"\x89PNG\r\n\x1a\n"
    image += chunk(b"IHDR", struct.pack("!IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    image += chunk(
        b"IDAT", zlib.compress(b"\0" + b"\x11\x22\x33" * 2 + b"\0" + b"\x44\x55\x66" * 2)
    )
    image += chunk(b"IEND", b"")
    return base64.b64encode(image).decode()


def raw_observation(serial: str, params: dict[str, bool], capabilities: dict[str, object]):
    geometry = {
        "width": 2,
        "height": 2,
        "original_width": 4,
        "original_height": 4,
        "scale_x": 0.5,
        "scale_y": 0.5,
        "display_id": 0,
        "coordinate_space": "screenshot_pixels",
        "protected_content": "not_verified",
        "captured_at_uptime_ms": 104,
    }
    data = {
        "tree": {
            "window_id": 1,
            "display_id": 0,
            "coordinate_space": "display_pixels",
            "coverage": "exposed_accessibility_nodes_only",
            "nodes": [
                {
                    "node_path": [],
                    "source": "accessibility",
                    "view_id": "com.pulse.target:id/submit",
                    "class_name": "android.widget.Button",
                    "package_name": "com.pulse.target",
                    "text": f"CONTROLLED_NATIVE_{serial}",
                    "content_description": None,
                    "text_redacted": False,
                    "bounds": {"left": 0, "top": 0, "right": 4, "bottom": 4},
                    "states": {
                        "enabled": False,
                        "checked": None,
                        "checkable": False,
                        "clickable": True,
                        "editable": False,
                        "focused": False,
                        "showing_hint_text": False,
                        "selected": False,
                        "visible": True,
                    },
                }
            ],
        },
        "screenshot": {**geometry, "format": "png", "base64": png()},
        "ocr": {
            **geometry,
            "blocks": [
                {
                    "text": f"CONTROLLED_OCR_{serial}",
                    "bounds": {"left": 0, "top": 0, "right": 2, "bottom": 2},
                    "confidence": None,
                    "enabled": True,
                    "checked": True,
                }
            ],
        },
    }
    names = {
        "tree": "accessibility",
        "screenshot": "accessibility_screenshot",
        "ocr": "mlkit_chinese",
    }
    reasons = {
        "tree": "native_semantics_only_visual_coverage_unverified",
        "screenshot": "protected_content_not_verified",
        "ocr": "ocr_text_only_no_native_states",
    }
    sources = {
        key: {
            "source": names[key],
            "status": "available" if params[key] else "unavailable",
            "requested": params[key],
            "started_at_uptime_ms": 100 if params[key] else None,
            "finished_at_uptime_ms": 110 if params[key] else None,
            "data": data[key] if params[key] else None,
            "error": None
            if params[key]
            else {"code": "not_requested", "message": "not requested", "retryable": False},
            "completeness": {
                "status": "partial" if params[key] else "unavailable",
                "reasons": [reasons[key]] if params[key] else ["not_requested"],
            },
        }
        for key in names
    }
    return {
        "scope": "active_window",
        "sources": sources,
        "capabilities": copy.deepcopy(capabilities),
        "completeness": {"status": "partial", "reasons": ["sources_not_atomic"]},
        "revision_start": 12,
        "revision_end": 12,
        "atomic_snapshot": False,
    }


@dataclass
class ExchangeFactory:
    backend: FakeAndroidBackend
    gate: dict[str, Gate] = field(default_factory=dict)
    swallow_cancel: set[str] = field(default_factory=set)
    faults: dict[str, Exception] = field(default_factory=dict)
    before_reply: dict[str, object] = field(default_factory=dict)
    modify_result: dict[str, object] = field(default_factory=dict)
    calls: list[tuple[str, str, int | None, dict[str, bool]]] = field(default_factory=list)
    exchanges: dict[tuple[str, str], FakeExchange] = field(default_factory=dict)

    def __call__(self, identifier: str) -> FakeExchange:
        return FakeExchange(identifier, self)


class FakeExchange(ObserveExchange):
    def __init__(self, identifier: str, factory: ExchangeFactory):
        super().__init__(identifier)
        self.identifier = identifier
        self.factory = factory
        self.invalidated = False
        self.invalidated_event = asyncio.Event()
        self.cancelled = asyncio.Event()

    def invalidate(self) -> None:
        self.invalidated = True
        self.invalidated_event.set()

    async def run(
        self, forward: ForwardLease, token: str, params: dict[str, bool], timeout_ms: int
    ) -> ObserveReply:
        serial = forward.serial
        snapshot = self.factory.backend.snapshots[serial]
        assert token == snapshot.credential
        assert forward.mapping in self.factory.backend.forwards
        assert 100 <= timeout_ms <= 10000
        self.factory.calls.append((serial, self.identifier, forward.port, dict(params)))
        self.factory.exchanges[serial, self.identifier] = self
        result = raw_observation(serial, params, snapshot.capabilities)
        self.sent = True
        self.wire_session_id = str(uuid4())
        try:
            if gate := self.factory.gate.get(serial):
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    if serial not in self.factory.swallow_cancel:
                        raise
                    await gate.release.wait()
            if fault := self.factory.faults.get(serial):
                raise fault
            if callback := self.factory.before_reply.get(serial):
                callback(snapshot)
            if modify := self.factory.modify_result.get(serial):
                modify(result)
            self.terminal_received = self.eof_received = True
            success = any(
                source["requested"] and source["status"] == "available"
                for source in result["sources"].values()
            )
            return ObserveReply(
                self.wire_session_id, success, result, None if success else "window_unavailable"
            )
        finally:
            self.cleanup = {
                "connection_closed": True,
                "cancel_acknowledged": True if self.cancelled.is_set() else None,
                "native_idle_verified": False,
            }


@pytest.fixture(autouse=True)
def forbid_api_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args, **kwargs):
        pytest.fail("Observation must not issue debug input, navigation, or physical ADB")

    monkeypatch.setattr(api_module, "adb", forbidden)


@pytest.fixture
async def observation_rig() -> AsyncIterator:
    backend = FakeAndroidBackend()
    factory = ExchangeFactory(backend)
    manager = DeviceConnectionManager(backend, readiness_wait=0, observation_factory=factory)
    try:
        yield backend, manager, factory
    finally:
        for gate in factory.gate.values():
            gate.release.set()
        await manager.close()


def no_source_data(result):
    assert result["success"] is False
    assert all(source["data"] is None for source in result["sources"].values())


def test_evidence_client_never_retains_controlled_or_private_source_payloads():
    raw = raw_observation(
        SERIAL_A,
        {"tree": True, "screenshot": True, "ocr": True},
        FakeAndroidBackend().snapshots[SERIAL_A].capabilities,
    )
    raw["sources"]["tree"]["data"]["nodes"][0]["text"] = TOKEN_A
    raw["sources"]["tree"]["error"] = {
        "code": "invalid_source_data",
        "message": "PRIVATE_INPUT_RAW_EXCEPTION",
    }
    raw["sources"]["ocr"]["data"]["blocks"][0]["text"] = "PRIVATE_OCR_TEXT"
    raw["sources"]["screenshot"]["data"]["private_input"] = "PRIVATE_INPUT"
    raw["credential"] = TOKEN_B
    summary = safe_observation(raw)
    output = json.dumps(summary)
    for secret in (TOKEN_A, TOKEN_B, "PRIVATE_INPUT", "PRIVATE_OCR_TEXT", png()):
        assert secret not in output
    assert summary["sources"]["tree"]["node_count"] == 1
    assert summary["sources"]["ocr"]["block_count"] == 1
    assert "data" not in summary["sources"]["screenshot"]


async def test_acceptance_baseline_reuses_existing_read_only_backend_interface(monkeypatch):
    backend = FakeAndroidBackend()
    monkeypatch.setattr(_acceptance, "AndroidBackend", lambda: backend)
    settings, peer, mappings, pid = await _acceptance.baseline(SERIAL_A)
    assert settings == backend.snapshots[SERIAL_A].settings
    assert peer == backend.snapshots[SERIAL_A].pin
    assert mappings == set() and pid == backend.snapshots[SERIAL_A].agent_process
    assert backend.mutations == []


@pytest.mark.parametrize(
    "options",
    [(True, False, False), (False, True, False), (False, False, True), (True, True, True)],
)
async def test_dual_serial_source_options_and_http_runtime_lifecycle_are_independent(
    observation_rig, options
):
    backend, manager, factory = observation_rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    identifiers = [session_id(first), session_id(second)]
    request_id = str(uuid4())
    tree, screenshot, ocr = options
    results = await asyncio.gather(
        *(
            manager.observe(
                serial, identifier, request_id, tree=tree, screenshot=screenshot, ocr=ocr
            )
            for serial, identifier in zip((SERIAL_A, SERIAL_B), identifiers, strict=True)
        )
    )
    for serial, identifier, result in zip((SERIAL_A, SERIAL_B), identifiers, results, strict=True):
        assert result["success"] is True and result["status"] == "completed"
        assert result["serial"] == serial and result["session_id"] == identifier
        assert result["agent_lifecycle_id"] == backend.snapshots[serial].lifecycle
        assert result["agent_http_session_id"] != identifier
        assert result["atomic_snapshot"] is False
        assert result["framing"]["validated"] is True
        assert result["clock_mapping"]["data"] is None
        for key, enabled in zip(("tree", "screenshot", "ocr"), options, strict=True):
            source = result["sources"][key]
            assert source["requested"] is enabled
            assert source["status"] == ("available" if enabled else "unavailable")
            if not enabled:
                assert source["data"] is None
        if tree:
            node = result["sources"]["tree"]["data"]["nodes"][0]
            assert node["text"] == f"CONTROLLED_NATIVE_{serial}"
            assert node["states"]["enabled"] is False and node["states"]["checked"] is None
        if ocr:
            block = result["sources"]["ocr"]["data"]["blocks"][0]
            assert block["text"] == f"CONTROLLED_OCR_{serial}"
            assert "enabled" not in block and "checked" not in block
    assert results[0]["agent_http_session_id"] != results[1]["agent_http_session_id"]
    assert all(
        call[3] == {"tree": tree, "screenshot": screenshot or ocr, "ocr": ocr}
        for call in factory.calls
    )
    assert len(backend.forwards) == 2 and backend.mutations == []


async def test_stale_cross_serial_and_reused_request_ids_fail_before_observation_io(
    observation_rig,
):
    backend, manager, factory = observation_rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    identifier = session_id(first)
    before = list(backend.calls)
    with pytest.raises(ConnectionFault, match="stale_session"):
        await manager.observe(SERIAL_B, identifier, str(uuid4()))
    assert backend.calls == before and factory.calls == []
    request_id = str(uuid4())
    await manager.observe(SERIAL_A, identifier, request_id)
    before = list(backend.calls)
    with pytest.raises(ConnectionFault, match="observation_request_reused"):
        await manager.observe(SERIAL_A, identifier, request_id)
    assert backend.calls == before and len(factory.calls) == 1
    assert session_id(await manager.health(SERIAL_B, session_id(second))) == session_id(second)
    assert backend.mutations == []


async def test_request_tombstones_are_bounded_without_reusing_retired_ids(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    slot = manager.slots[SERIAL_A]
    slot.session.observation_ids.update(str(uuid4()) for _ in range(256))
    before = list(backend.calls)
    with pytest.raises(ConnectionFault, match="observation_request_limit"):
        await manager.observe(SERIAL_A, session_id(connected), str(uuid4()))
    assert factory.calls == [] and backend.calls == before


async def test_http200_failed_terminal_retains_actual_source_unavailable_and_failed_states(
    observation_rig,
):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier = session_id(connected)

    def fail_sources(result):
        for key, status, code in (
            ("tree", "unavailable", "window_unavailable"),
            ("screenshot", "failed", "screenshot_failed"),
            ("ocr", "unavailable", "ocr_unavailable"),
        ):
            result["sources"][key].update(
                status=status,
                data=None,
                started_at_uptime_ms=None,
                finished_at_uptime_ms=None,
                error={"code": code, "message": "PRIVATE_INPUT_RAW_EXCEPTION", "retryable": False},
                completeness={"status": "unavailable", "reasons": [code]},
            )

    factory.modify_result[SERIAL_A] = fail_sources
    result = await manager.observe(SERIAL_A, identifier, str(uuid4()), screenshot=True, ocr=True)
    no_source_data(result)
    assert result["agent_terminal_success"] is False and result["status"] == "completed"
    assert result["error"]["code"] == "window_unavailable"
    assert [result["sources"][key]["status"] for key in ("tree", "screenshot", "ocr")] == [
        "unavailable",
        "failed",
        "unavailable",
    ]
    assert all(source["capability"] == "available" for source in result["sources"].values())
    assert result["framing"]["validated"] is True
    assert "PRIVATE_INPUT" not in json.dumps(result)
    assert manager.slots[SERIAL_A].session.id == identifier and backend.mutations == []


async def test_successful_zero_native_and_ocr_results_are_not_source_failures(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)

    def zero_results(result):
        result["sources"]["tree"]["data"]["nodes"] = []
        result["sources"]["ocr"]["data"]["blocks"] = []

    factory.modify_result[SERIAL_A] = zero_results
    result = await manager.observe(SERIAL_A, session_id(connected), str(uuid4()), ocr=True)
    assert result["success"] is True
    assert result["sources"]["tree"]["data"]["nodes"] == []
    assert result["sources"]["ocr"]["data"]["blocks"] == []
    assert result["sources"]["screenshot"]["data"] is None
    assert result["atomic_snapshot"] is False and backend.mutations == []


async def test_unknown_display_does_not_collect_or_reuse_default_display_evidence(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    result = await manager.observe(
        SERIAL_A, session_id(connected), str(uuid4()), display_id=1, screenshot=True, ocr=True
    )
    no_source_data(result)
    assert result["error"]["code"] == "unsupported_display"
    assert result["requested_display_id"] == 1
    assert all(source["status"] == "unavailable" for source in result["sources"].values())
    assert factory.calls == [] and backend.mutations == []


async def test_busy_or_quarantined_agent_is_not_restarted_or_queued_for_observation(
    observation_rig,
):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier = session_id(connected)
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["platform_operation_pending"] = True
    busy = await manager.observe(SERIAL_A, identifier, str(uuid4()))
    no_source_data(busy)
    assert busy["error"]["code"] == "platform_operation_pending"
    assert busy["capabilities"]["platform_operation_pending"] is True
    assert busy["capabilities_at_request"]["platform_operation_pending"] is False
    assert manager.slots[SERIAL_A].session.id == identifier
    snapshot.capabilities["platform_operation_pending"] = False
    snapshot.capabilities["recovery_required"] = True
    blocked = await manager.observe(SERIAL_A, identifier, str(uuid4()))
    no_source_data(blocked)
    assert blocked["error"]["code"] == "recovery_required"
    assert blocked["capabilities"]["recovery_required"] is True
    assert manager.slots[SERIAL_A].quarantine is not None
    snapshot.capabilities["recovery_required"] = False
    snapshot.agent_process = 999
    snapshot.credential = "c" * 64
    snapshot.lifecycle = str(uuid4())
    assert (await manager.connect(SERIAL_A))["status"] == "recovery_required"
    assert factory.calls == [] and backend.mutations == []


async def test_cancel_targets_runtime_session_request_and_serial_and_preserves_connection(
    observation_rig,
):
    backend, manager, factory = observation_rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    identifier, request_id = session_id(first), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    worker = asyncio.create_task(
        manager.observe(SERIAL_A, identifier, request_id, screenshot=True, ocr=True)
    )
    await asyncio.wait_for(gate.entered.wait(), 2)
    exchange = factory.exchanges[SERIAL_A, request_id]
    for serial, session, request in (
        (SERIAL_B, session_id(second), request_id),
        (SERIAL_A, session_id(second), request_id),
        (SERIAL_A, identifier, str(uuid4())),
    ):
        assert (await manager.cancel_observation(serial, session, request))["cancelled"] is False
    assert not exchange.cancelled.is_set()
    assert await manager.cancel_observation(SERIAL_A, identifier, request_id) == {
        "cancelled": True,
        "cleanup_complete": True,
    }
    result = await asyncio.wait_for(worker, 2)
    no_source_data(result)
    assert result["error"]["code"] == "cancelled" and exchange.cancelled.is_set()
    assert result["cleanup"]["native_idle_verified"] is False
    assert manager.slots[SERIAL_A].observation is None
    assert manager.slots[SERIAL_A].session.id == identifier
    assert session_id(await manager.health(SERIAL_B, session_id(second))) == session_id(second)
    assert len(backend.forwards) == 2 and backend.mutations == []


async def test_same_serial_observe_and_prepare_cannot_replace_an_active_read(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier, request_id = session_id(connected), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id))
    await asyncio.wait_for(gate.entered.wait(), 2)
    before = list(backend.calls)
    with pytest.raises(ConnectionFault, match="observation_in_progress"):
        await manager.observe(SERIAL_A, identifier, str(uuid4()))
    for preparing in (True, False):
        response = await (manager.prepare(SERIAL_A) if preparing else manager.connect(SERIAL_A))
        assert response["operation_error"] == "operation_in_progress"
    assert backend.calls == before
    await manager.cancel_observation(SERIAL_A, identifier, request_id)
    await worker


@pytest.mark.parametrize(
    "change",
    [
        "transport",
        "pid",
        "token",
        "adbd",
        "lifecycle",
        "permissions",
        "offline",
        "absent",
        "forward",
    ],
)
async def test_invalidated_late_results_never_enter_fresh_runtime_or_other_device(
    observation_rig, change
):
    backend, manager, factory = observation_rig
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    identifier, request_id = session_id(first), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    factory.swallow_cancel.add(SERIAL_A)
    worker = asyncio.create_task(
        manager.observe(SERIAL_A, identifier, request_id, screenshot=True, ocr=True)
    )
    await asyncio.wait_for(gate.entered.wait(), 2)
    snapshot = backend.snapshots[SERIAL_A]
    if change == "transport":
        snapshot.transport_id = "99"
    elif change == "pid":
        snapshot.agent_process = 999
    elif change == "token":
        snapshot.credential = "c" * 64
    elif change == "adbd":
        snapshot.adbd = AdbdPeer(2000, 999)
        snapshot.pin = snapshot.adbd.config
    elif change == "lifecycle":
        snapshot.lifecycle = str(uuid4())
    elif change == "permissions":
        snapshot.settings = AccessibilitySettings(OTHER_SERVICE, "1")
    elif change == "offline":
        snapshot.state = "offline"
    elif change == "absent":
        snapshot.present = False
    else:
        original = next(row for row in backend.forwards if row[0] == SERIAL_A)
        backend.forwards.remove(original)
        backend.forwards.add((SERIAL_A, original[1], "localabstract:foreign-video"))
    # Simulate the lifespan watcher's fresh inventory/probe, not the in-flight status snapshot.
    statuses = await manager.list_devices()
    status = next(device for device in statuses["devices"] if device["serial"] == SERIAL_A)
    exchange = factory.exchanges[SERIAL_A, request_id]
    await asyncio.wait_for(exchange.cancelled.wait(), 2)
    assert exchange.invalidated and exchange.cancelled.is_set()
    if status["ready"]:
        fresh_id = session_id(status)
        assert fresh_id != identifier
        with pytest.raises(ConnectionFault, match="observation_in_progress"):
            await manager.observe(SERIAL_A, fresh_id, str(uuid4()))
    gate.release.set()
    result = await asyncio.wait_for(worker, 2)
    no_source_data(result)
    assert "CONTROLLED_NATIVE" not in json.dumps(result)
    assert "CONTROLLED_OCR" not in json.dumps(result)
    assert result["session_id"] == identifier
    assert session_id(await manager.health(SERIAL_B, session_id(second))) == session_id(second)
    assert backend.mutations == []
    if change == "forward":
        assert any(row[2] == "localabstract:foreign-video" for row in backend.forwards)


async def test_observation_waits_for_a_bounded_read_only_inventory_lock(observation_rig):
    _, manager, _ = observation_rig
    connected = await manager.connect(SERIAL_A)
    slot = manager.slots[SERIAL_A]
    await slot.lock.acquire()
    worker = asyncio.create_task(manager.observe(SERIAL_A, session_id(connected), str(uuid4())))
    await asyncio.sleep(0.2)
    slot.lock.release()
    result = await asyncio.wait_for(worker, 3)
    assert result["success"] is True
    assert slot.native_cleanup_guard is False


async def test_observation_does_not_wait_indefinitely_for_a_foreign_operation(observation_rig):
    _, manager, _ = observation_rig
    connected = await manager.connect(SERIAL_A)
    slot = manager.slots[SERIAL_A]
    await slot.lock.acquire()
    try:
        with pytest.raises(ConnectionFault, match="operation_in_progress"):
            await manager.observe(SERIAL_A, session_id(connected), str(uuid4()), timeout=1.0)
    finally:
        slot.lock.release()
    assert slot.native_cleanup_guard is False


async def test_cancelled_observation_keeps_native_guard_until_fresh_idle_proof(observation_rig):
    _, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier, request_id = session_id(connected), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id, ocr=True))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await manager.cancel_observation(SERIAL_A, identifier, request_id)
    await worker
    assert manager.slots[SERIAL_A].native_cleanup_guard is True


async def test_generation_is_checked_after_terminal_not_only_before_first_frame(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    factory.before_reply[SERIAL_A] = lambda snapshot: setattr(snapshot, "credential", "c" * 64)
    result = await manager.observe(
        SERIAL_A, session_id(connected), str(uuid4()), screenshot=True, ocr=True
    )
    no_source_data(result)
    assert result["error"]["code"] == "generation_changed"
    assert result["framing"]["terminal_received"] is True
    assert result["framing"]["source_data_validated"] is False
    assert result["capabilities"]["platform_operation_pending"] is None
    assert result["capabilities"]["recovery_required"] is None
    assert manager.slots[SERIAL_A].session is None and backend.mutations == []


async def test_final_health_cannot_publish_evidence_after_same_port_foreign_rebind(
    observation_rig, monkeypatch
):
    backend, manager, _factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    read_health = backend.health
    count = 0

    async def rebind_after_health(forward, token):
        nonlocal count
        payload = await read_health(forward, token)
        count += 1
        if count == 2:
            original = next(row for row in backend.forwards if row[0] == SERIAL_A)
            backend.forwards.remove(original)
            backend.forwards.add((SERIAL_A, original[1], "localabstract:foreign-video"))
        return payload

    monkeypatch.setattr(backend, "health", rebind_after_health)
    result = await manager.observe(SERIAL_A, session_id(connected), str(uuid4()), ocr=True)
    no_source_data(result)
    assert result["error"]["code"] == "forward_replaced"
    assert result["framing"]["source_data_validated"] is False
    assert manager.slots[SERIAL_A].session is None
    assert any(row[2] == "localabstract:foreign-video" for row in backend.forwards)


@pytest.mark.parametrize("fault", [ConnectionFault("invalid_message"), RuntimeError("PRIVATE_RAW")])
@pytest.mark.parametrize("cancellation", ["explicit", "peer"])
async def test_second_cancel_cannot_skip_fault_cleanup_waiting_for_slot_lock(
    observation_rig, fault, cancellation
):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier, request_id = session_id(connected), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    factory.faults[SERIAL_A] = fault
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id))
    await asyncio.wait_for(gate.entered.wait(), 2)
    exchange = factory.exchanges[SERIAL_A, request_id]
    slot = manager.slots[SERIAL_A]
    await slot.lock.acquire()
    cancel = None
    try:
        gate.release.set()
        await asyncio.wait_for(exchange.invalidated_event.wait(), 2)
        if cancellation == "explicit":
            cancel = asyncio.create_task(
                manager.cancel_observation(SERIAL_A, identifier, request_id)
            )
        else:
            worker.cancel()
        await asyncio.sleep(0)
        assert slot.observation is not None and not slot.observation.done.is_set()
    finally:
        slot.lock.release()
    result = await asyncio.wait_for(worker, 2)
    if cancel is not None:
        assert (await cancel)["cleanup_complete"] is True
    no_source_data(result)
    assert slot.session is None and slot.observation is None
    assert slot.last_invalidation in {"invalid_message", "observation_failed"}
    assert not backend.forwards


async def test_verified_terminal_recovery_is_not_replaced_with_request_cache_false(observation_rig):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)

    def recovery(result):
        result["capabilities"]["recovery_required"] = True

    factory.modify_result[SERIAL_A] = recovery
    result = await manager.observe(SERIAL_A, session_id(connected), str(uuid4()))
    no_source_data(result)
    assert result["capabilities"]["recovery_required"] is True
    assert result["capabilities_at_request"]["recovery_required"] is False
    assert manager.slots[SERIAL_A].quarantine is not None and backend.mutations == []


@pytest.mark.parametrize("ending", ["disconnect", "close"])
@pytest.mark.parametrize("native_state", ["pending", "recovery", "unknown"])
async def test_native_not_confirmed_idle_keeps_owned_permission_and_peer_leases(
    observation_rig, ending, native_state, monkeypatch
):
    backend, manager, factory = observation_rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.settings = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.pin = snapshot.credential = None
    connected = await manager.prepare(SERIAL_A)
    identifier, request_id = session_id(connected), str(uuid4())
    slot = manager.slots[SERIAL_A]
    assert slot.accessibility_lease is not None and slot.peer_owner is not None
    gate = factory.gate[SERIAL_A] = Gate()
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id, ocr=True))
    await asyncio.wait_for(gate.entered.wait(), 2)
    if native_state == "pending":
        snapshot.capabilities["platform_operation_pending"] = True
    elif native_state == "recovery":
        snapshot.capabilities["recovery_required"] = True
    else:

        async def unavailable_health(_forward, _token):
            raise ConnectionFault("agent_unavailable")

        monkeypatch.setattr(backend, "health", unavailable_health)
    mutations = list(backend.mutations)
    if ending == "disconnect":
        view = await manager.disconnect(SERIAL_A, identifier)
    else:
        await manager.close()
        view = manager.snapshot(slot)
    no_source_data(await asyncio.wait_for(worker, 2))
    assert backend.mutations == mutations
    assert snapshot.settings.active and snapshot.pin == snapshot.adbd.config
    assert slot.accessibility_lease is not None and slot.peer_owner is not None
    assert view["diagnostic"]["cleanup_pending"] is True
    assert view["diagnostic"]["code"] == "native_cleanup_pending"
    assert view["ready"] is False
    await manager.restore(slot)
    assert backend.mutations == mutations  # A watcher cannot bypass the same restoration guard.


async def test_owned_leases_restore_only_after_fresh_native_idle_proof(observation_rig):
    backend, manager, factory = observation_rig
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.settings = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.pin = snapshot.credential = None
    original = snapshot.settings
    connected = await manager.prepare(SERIAL_A)
    identifier, request_id = session_id(connected), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id, ocr=True))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await manager.cancel_observation(SERIAL_A, identifier, request_id)
    await worker
    view = await manager.disconnect(SERIAL_A, identifier)
    assert snapshot.settings == original and snapshot.pin is None
    assert manager.slots[SERIAL_A].accessibility_lease is None
    assert manager.slots[SERIAL_A].peer_owner is None
    assert view["diagnostic"]["cleanup_pending"] is False


def test_privacy_checks_include_rotated_runtime_logs_and_do_not_pass_missing_logs(tmp_path):
    assert _acceptance.log_privacy(tmp_path, TOKEN_A)["missing_runtime_logs"] == 1
    (tmp_path / "runtime.log.1").write_text(TOKEN_A + '\n{"blocks": []}\n')
    counts = _acceptance.log_privacy(tmp_path, TOKEN_A)
    assert counts["runtime_token_occurrences"] == 1
    assert counts["raw_observation_keys"] == 1
    assert counts["missing_runtime_logs"] == 0


async def test_acceptance_prepare_timeout_is_aligned_and_cancels_known_operation(
    tmp_path, monkeypatch
):
    root = tmp_path / "evidence"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (private / "runtime.log").write_text("controlled-log-only\n")
    calls = []
    constructors = []

    class Response:
        def __init__(self, code, payload):
            self.status_code, self.payload = code, payload

        async def aiter_bytes(self, _size):
            yield json.dumps(self.payload).encode()

    class Stream:
        def __init__(self, method, path, kwargs):
            calls.append((method, path, kwargs))
            self.path = path

        async def __aenter__(self):
            if self.path.endswith("/prepare"):
                raise TimeoutError
            if self.path == "/health":
                return Response(200, {"pid": 999})
            if self.path.endswith("/observe"):
                return Response(401, {})
            if self.path == "/devices":
                return Response(200, {"devices": [{"serial": SERIAL_A, "state": "device"}]})
            return Response(200, {"cancelled": True})

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, **kwargs):
            constructors.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, path, **kwargs):
            return Stream(method, path, kwargs)

    async def fake_baseline(_serial):
        return "settings", "peer", set(), 100

    info = SimpleNamespace(pid=999, port=1234, token=TOKEN_A)
    records = iter((info, None))
    monkeypatch.setattr(_acceptance, "EVIDENCE_ROOT", root)
    monkeypatch.setattr(_acceptance, "read_connection", lambda _path: next(records))
    monkeypatch.setattr(_acceptance, "baseline", fake_baseline)
    monkeypatch.setattr(_acceptance, "AsyncClient", Client)
    assert (
        await _acceptance.run(
            argparse.Namespace(
                serial=SERIAL_A,
                runtime_dir=private,
                evidence=root / "attempt",
                prepare_target=False,
            )
        )
        == 1
    )
    preparation = next(call for call in calls if call[1].endswith("/prepare"))[2]["json"]
    operation = preparation["operation_id"]
    assert preparation["timeout_ms"] == 60_000 and constructors[0]["timeout"] > 60
    assert any(path.endswith(f"/cancel?operation_id={operation}") for _, path, _ in calls)
    saved = json.loads((root / "attempt" / "result.json").read_text())
    assert saved["status"] == "blocked_or_failed" and saved["required_sequence_completed"] is False
    assert TOKEN_A not in json.dumps(saved)


async def test_timeout_discards_data_and_keeps_pending_native_resource_limit(
    observation_rig, monkeypatch
):
    backend, manager, factory = observation_rig
    connected = await manager.connect(SERIAL_A)
    identifier = session_id(connected)
    gate = factory.gate[SERIAL_A] = Gate()
    deadline = asyncio.timeout(None)
    timeout_at = devices.asyncio.timeout_at
    monkeypatch.setattr(devices.asyncio, "timeout_at", lambda _when: deadline)
    worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, str(uuid4()), ocr=True))
    await asyncio.wait_for(gate.entered.wait(), 2)
    deadline.reschedule(asyncio.get_running_loop().time())
    result = await asyncio.wait_for(worker, 2)
    no_source_data(result)
    assert result["error"]["code"] == "observation_timeout"
    assert gate.cancelled.is_set() and manager.slots[SERIAL_A].observation is None
    backend.snapshots[SERIAL_A].capabilities["platform_operation_pending"] = True
    factory.gate.pop(SERIAL_A)
    monkeypatch.setattr(devices.asyncio, "timeout_at", timeout_at)
    next_result = await manager.observe(SERIAL_A, identifier, str(uuid4()))
    assert next_result["error"]["code"] == "platform_operation_pending"
    assert len(factory.calls) == 1 and backend.mutations == []


@pytest.mark.parametrize(
    "fault",
    [
        RuntimeError("PRIVATE_INPUT RAW_TREE RAW_OCR RAW_SCREENSHOT"),
        ConnectionFault("PRIVATE_INPUT RAW_TREE RAW_OCR RAW_SCREENSHOT"),
    ],
)
async def test_observation_errors_and_logs_never_publish_lower_exception_or_evidence(
    observation_rig, caplog, fault
):
    backend, manager, factory = observation_rig
    caplog.set_level("DEBUG", logger="pulse_runtime")
    connected = await manager.connect(SERIAL_A)
    identifier = session_id(connected)
    good = await manager.observe(SERIAL_A, identifier, str(uuid4()), screenshot=True, ocr=True)
    assert good["success"] is True
    log_output = caplog.text + json.dumps(
        [record.__dict__ for record in caplog.records], default=str
    )
    assert "CONTROLLED_NATIVE" not in log_output and "CONTROLLED_OCR" not in log_output
    assert png() not in log_output
    factory.faults[SERIAL_A] = fault
    result = await manager.observe(SERIAL_A, identifier, str(uuid4()), screenshot=True, ocr=True)
    no_source_data(result)
    output = (
        json.dumps(result)
        + caplog.text
        + json.dumps([record.__dict__ for record in caplog.records], default=str)
    )
    for secret in (
        TOKEN_A,
        TOKEN_B,
        API_TOKEN,
        "PRIVATE_INPUT",
        "RAW_TREE",
        "RAW_OCR",
        "RAW_SCREENSHOT",
    ):
        assert secret not in output
    assert backend.mutations == []


async def test_observe_api_auth_strict_body_session_and_real_collected_result(observation_rig):
    backend, manager, factory = observation_rig
    app = create_app(API_TOKEN, device_manager=manager)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://pulse.test"
    ) as client:
        route = f"/devices/{SERIAL_A}/agent/observe"
        before = list(backend.calls)
        invalid_id = str(uuid4())
        for method, path, body in (
            ("POST", route, {}),
            (
                "POST",
                route + "/cancel?session_id=" + invalid_id + "&request_id=" + invalid_id,
                None,
            ),
        ):
            response = await client.request(method, path, json=body)
            assert response.status_code == 401
        assert backend.calls == before
        connected = await manager.connect(SERIAL_A)
        identifier = session_id(connected)
        base = {"session_id": identifier, "request_id": str(uuid4())}
        invalid = [
            {**base, "token": TOKEN_A},
            {**base, "action": "tap"},
            {**base, "tree": "true"},
            {**base, "screenshot": 1},
            {**base, "timeout_ms": 999},
            {**base, "timeout_ms": 30001},
            {**base, "timeout_ms": 1000.0},
            {**base, "display_id": True},
            {**base, "display_id": -1},
            {**base, "request_id": "not-a-uuid"},
            {**base, "tree": False, "screenshot": False, "ocr": False},
        ]
        before = list(backend.calls)
        for body in invalid:
            response = await client.post(route, json=body, headers=AUTH)
            assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
            assert TOKEN_A not in response.text
        assert backend.calls == before and factory.calls == []
        response = await client.post(route, json={**base, "session_id": str(uuid4())}, headers=AUTH)
        assert response.status_code == 409 and response.json() == {"error": "stale_session"}
        assert backend.calls == before
        response = await client.post(
            route, json={**base, "screenshot": True, "ocr": True}, headers=AUTH
        )
        assert response.status_code == 200 and response.json()["success"] is True
        assert response.json()["framing"]["terminal_received"] is True
        assert "native_states" not in response.json()["sources"]["ocr"]["data"]
        assert len(factory.calls) == 1 and backend.mutations == []


async def test_api_explicit_cancel_and_peer_disconnect_finish_only_their_observation(
    observation_rig,
):
    backend, manager, factory = observation_rig
    app = create_app(API_TOKEN, device_manager=manager)
    first, second = await asyncio.gather(manager.connect(SERIAL_A), manager.connect(SERIAL_B))
    identifier, request_id = session_id(first), str(uuid4())
    gate = factory.gate[SERIAL_A] = Gate()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://pulse.test"
    ) as client:
        worker = asyncio.create_task(
            client.post(
                f"/devices/{SERIAL_A}/agent/observe",
                json={"session_id": identifier, "request_id": request_id, "ocr": True},
                headers=AUTH,
            )
        )
        await asyncio.wait_for(gate.entered.wait(), 2)
        cancelled = await client.post(
            f"/devices/{SERIAL_A}/agent/observe/cancel",
            params={"session_id": identifier, "request_id": request_id},
            headers=AUTH,
        )
        assert cancelled.json() == {"cancelled": True, "cleanup_complete": True}
        response = await asyncio.wait_for(worker, 2)
        assert response.status_code == 200
        no_source_data(response.json())
    gate = factory.gate[SERIAL_A] = Gate()
    request_id = str(uuid4())
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/devices/{serial}/agent/observe"
    )
    worker = asyncio.create_task(
        endpoint(
            SERIAL_A,
            AgentObserveBody(session_id=identifier, request_id=request_id, ocr=True),
            SimpleNamespace(receive=receive),
        )
    )
    await asyncio.wait_for(gate.entered.wait(), 2)
    disconnect.set()
    result = await asyncio.wait_for(worker, 2)
    no_source_data(result)
    assert gate.cancelled.is_set() and manager.slots[SERIAL_A].observation is None
    assert session_id(await manager.health(SERIAL_B, session_id(second))) == session_id(second)
    assert len(backend.forwards) == 2 and backend.mutations == []


@pytest.mark.parametrize("ending", ["cancel", "disconnect", "runtime-close"])
async def test_observation_cleanup_does_not_replace_or_stop_independently_owned_video(
    observation_rig, ending
):
    backend, manager, factory = observation_rig
    source = FakeVideoSource()
    hub = StreamHub(lambda _serial, _options: source, clock_factory=None)
    stream, viewer = hub.subscribe(SERIAL_A, VideoOptions())
    try:
        await asyncio.wait_for(source.ready.wait(), 2)
        capture, media_session = stream.task, copy.deepcopy(stream.session)
        connected = await manager.connect(SERIAL_A)
        identifier, request_id = session_id(connected), str(uuid4())
        gate = factory.gate[SERIAL_A] = Gate()
        worker = asyncio.create_task(manager.observe(SERIAL_A, identifier, request_id, ocr=True))
        await asyncio.wait_for(gate.entered.wait(), 2)
        if ending == "cancel":
            await manager.cancel_observation(SERIAL_A, identifier, request_id)
        elif ending == "disconnect":
            await manager.disconnect(SERIAL_A, identifier)
        else:
            await manager.close()
        no_source_data(await asyncio.wait_for(worker, 2))
        assert stream.task is capture and not capture.done()
        assert stream.session == media_session and viewer in stream.subscribers
        assert not source.stopped.is_set()
        source.continue_frame.set()
        await asyncio.wait_for(source.frame_delivered.wait(), 2)
        assert stream.frames_received == 2
        assert backend.mutations == []
    finally:
        await hub.close()
