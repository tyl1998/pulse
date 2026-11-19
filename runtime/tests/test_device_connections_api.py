"""P1.2 HTTP integration with fake Android state and independently owned media."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from pulse_runtime.api import app as app_module
from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android.agent_transport import (
    AccessibilitySettings,
    AgentHealth,
    ForwardLease,
)
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.drivers.android.scrcpy_video import (
    Packet,
    PacketKind,
    SessionMeta,
    StreamInfo,
    VideoOptions,
)
from pulse_runtime.stream.hub import StreamHub
from runtime.tests.test_device_connections import (
    ACTIVE_SETTINGS,
    OTHER_SERVICE,
    SERIAL_A,
    SERIAL_B,
    TOKEN_A,
    TOKEN_B,
    FakeAndroidBackend,
    Gate,
    diagnostic,
    session_id,
)
from runtime.tests.test_device_connections import (
    forbid_external_io as forbid_external_io,
)
from runtime.tests.test_device_connections import rig as rig

API_TOKEN = "runtime-api-secret"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}
VALID_ID = "12345678-1234-4234-8234-123456789abc"
SERIAL_ROUTES = [
    ("GET", "/devices/{serial}", None),
    ("POST", "/devices/{serial}/agent/prepare", {}),
    ("POST", "/devices/{serial}/agent/connect", {}),
    ("GET", f"/devices/{{serial}}/agent/health?session_id={VALID_ID}", None),
    ("GET", f"/devices/{{serial}}/agent/capabilities?session_id={VALID_ID}", None),
    ("POST", f"/devices/{{serial}}/agent/cancel?operation_id={VALID_ID}", None),
    ("DELETE", f"/devices/{{serial}}/agent/connection?session_id={VALID_ID}", None),
]


@pytest.fixture(autouse=True)
def forbid_api_device_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Device connection tests must not issue debug or physical ADB actions")

    monkeypatch.setattr(app_module, "adb", forbidden)


@pytest.fixture
async def api(
    rig,
) -> AsyncIterator[tuple[FakeAndroidBackend, DeviceConnectionManager, AsyncClient]]:
    backend, manager = rig
    app = create_app(API_TOKEN, device_manager=manager)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://pulse.test"
    ) as client:
        yield backend, manager, client


@pytest.mark.parametrize(("method", "route", "body"), [("GET", "/devices", None), *SERIAL_ROUTES])
@pytest.mark.parametrize("authorization", ["missing", "wrong", "query-only"])
async def test_all_device_routes_require_runtime_bearer_before_device_io(
    api, method, route, body, authorization
) -> None:
    backend, manager, client = api
    path = route.format(serial=SERIAL_A)
    headers = {"Authorization": "Bearer wrong"} if authorization == "wrong" else {}
    if authorization == "query-only":
        path += ("&" if "?" in path else "?") + f"token={API_TOKEN}"
    response = await client.request(method, path, json=body, headers=headers)
    assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
    assert API_TOKEN not in response.text
    assert backend.calls == [] and manager.slots == {}


async def test_device_route_cors_preflight_has_no_authorization_or_preparation_side_effect(
    api,
) -> None:
    backend, manager, client = api
    response = await client.options(
        f"/devices/{SERIAL_A}/agent/prepare",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert backend.calls == [] and manager.slots == {}


@pytest.mark.parametrize(("method", "route", "body"), SERIAL_ROUTES)
@pytest.mark.parametrize("serial", ["space serial", "serial;id", "serial\n", "x" * 65])
async def test_every_device_route_rejects_invalid_serial_before_targeted_backend_calls(
    api, method, route, body, serial
) -> None:
    backend, manager, client = api
    response = await client.request(
        method, route.format(serial=quote(serial, safe="")), json=body, headers=AUTH
    )
    assert response.status_code == 400 and response.json() == {"error": "invalid_serial"}
    assert backend.calls == [] and manager.slots == {}


@pytest.mark.parametrize("operation", ["prepare", "connect"])
@pytest.mark.parametrize(
    "body",
    [
        {"timeout_ms": 999},
        {"timeout_ms": 120001},
        {"timeout_ms": 1000.0},
        {"timeout_ms": True},
        {"timeout_ms": "1000"},
        {"manual_recovery_confirmed": "true"},
        {"manual_recovery_confirmed": 1},
        {"operation_id": "not-a-uuid"},
        {"operation_id": VALID_ID.upper()},
        {"token": TOKEN_A},
    ],
    ids=[
        "timeout-small",
        "timeout-large",
        "timeout-float",
        "timeout-bool",
        "timeout-string",
        "confirmation-string",
        "confirmation-int",
        "operation-id-invalid",
        "operation-id-noncanonical",
        "extra-credential",
    ],
)
async def test_connection_body_validation_is_strict_sanitized_and_has_no_device_effects(
    api, operation, body
) -> None:
    backend, manager, client = api
    response = await client.post(f"/devices/{SERIAL_A}/agent/{operation}", json=body, headers=AUTH)
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert TOKEN_A not in response.text and str(body) not in response.text
    assert backend.calls == [] and manager.slots == {}


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("connect", {"install": True}),
        ("connect", {"enable_accessibility": True}),
        ("prepare", {"install": "true"}),
        ("prepare", {"enable_accessibility": "true"}),
    ],
)
async def test_connect_cannot_request_preparation_and_prepare_requires_boolean_options(
    api, operation, body
) -> None:
    backend, manager, client = api
    response = await client.post(f"/devices/{SERIAL_A}/agent/{operation}", json=body, headers=AUTH)
    assert response.status_code == 422 and response.json() == {"error": "invalid_request"}
    assert backend.calls == [] and manager.slots == {}


@pytest.mark.parametrize(
    ("method", "route", "query"),
    [
        ("GET", "health", "session_id"),
        ("GET", "capabilities", "session_id"),
        ("DELETE", "connection", "session_id"),
        ("POST", "cancel", "operation_id"),
    ],
)
async def test_session_and_operation_query_ids_are_canonical_and_invalid_ids_do_not_touch_device(
    api, method, route, query
) -> None:
    backend, manager, client = api
    response = await client.request(
        method, f"/devices/{SERIAL_A}/agent/{route}?{query}=not-a-uuid", headers=AUTH
    )
    assert response.status_code == 400 and response.json() == {"error": "invalid_message"}
    assert backend.calls == [] and manager.slots == {}
    missing = await client.request(method, f"/devices/{SERIAL_A}/agent/{route}", headers=AUTH)
    assert missing.status_code == 422 and missing.json() == {"error": "invalid_request"}
    assert backend.calls == [] and manager.slots == {}


async def test_api_reports_discovery_states_then_authenticated_capabilities_without_credentials(
    api, caplog
) -> None:
    backend, manager, client = api
    caplog.set_level("DEBUG", logger="pulse_runtime")
    backend.snapshots[SERIAL_B].state = "unauthorized"
    discovered = await client.get("/devices", headers=AUTH)
    devices = {entry["serial"]: entry for entry in discovered.json()["devices"]}
    assert devices[SERIAL_A]["status"] == "agent_available" and devices[SERIAL_A]["ready"] is False
    assert devices[SERIAL_B]["status"] == "adb_unauthorized"
    assert diagnostic(devices[SERIAL_B])["code"] == "adb_unauthorized"
    assert "Authorize this computer" in diagnostic(devices[SERIAL_B])["guidance"]
    assert backend.forwards == set() and backend.mutations == []

    connected = await client.post(f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH)
    assert connected.status_code == 200 and connected.json()["ready"] is True
    identifier = session_id(connected.json())
    detail = await client.get(f"/devices/{SERIAL_A}", headers=AUTH)
    health = await client.get(
        f"/devices/{SERIAL_A}/agent/health", params={"session_id": identifier}, headers=AUTH
    )
    capabilities = await client.get(
        f"/devices/{SERIAL_A}/agent/capabilities", params={"session_id": identifier}, headers=AUTH
    )
    assert health.status_code == capabilities.status_code == detail.status_code == 200
    assert health.json()["status"] == "ready"
    assert health.json()["connection"]["health_authenticated"] is True
    assert health.json()["connection"]["loopback_verified"] is True
    assert (
        health.json()["connection"]["agent_lifecycle_id"] == backend.snapshots[SERIAL_A].lifecycle
    )
    assert health.json()["agent"]["build_verified"] is True
    assert health.json()["agent"]["configuration_method"] == "private_debug_run_as"
    assert capabilities.json() == {
        "serial": SERIAL_A,
        "session_id": identifier,
        "protocol_version": 1,
        "status": "ready",
        "capabilities": health.json()["capabilities"],
    }
    assert session_id(detail.json()) == session_id(health.json()) == identifier
    output = "".join(
        response.text for response in [discovered, connected, detail, health, capabilities]
    )
    output += caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert TOKEN_A not in output and TOKEN_B not in output and API_TOKEN not in output
    assert "token" not in health.text and "credential" not in health.text
    assert backend.mutations == [] and len(backend.forwards) == 1
    assert manager.slots[SERIAL_B].session is None


async def test_api_install_and_accessibility_mutations_require_explicit_prepare_options(
    api,
) -> None:
    backend, manager, client = api
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "1")
    snapshot.stop_service(original)
    snapshot.package = None
    default = await client.post(f"/devices/{SERIAL_A}/agent/prepare", json={}, headers=AUTH)
    assert default.json()["status"] == "agent_missing" and backend.mutations == []
    installed = await client.post(
        f"/devices/{SERIAL_A}/agent/prepare",
        json={"install": True, "enable_accessibility": False},
        headers=AUTH,
    )
    assert installed.status_code == 200
    assert installed.json()["status"] == "manual_authorization_required"
    assert diagnostic(installed.json())["awaiting_authorization"] is True
    assert snapshot.pin == snapshot.adbd.config
    assert len(backend.calls_for("install", SERIAL_A)) == 1
    assert backend.calls_for("set_permissions") == [] and snapshot.settings == original
    prepared = await client.post(
        f"/devices/{SERIAL_A}/agent/prepare", json={"enable_accessibility": True}, headers=AUTH
    )
    assert prepared.json()["ready"] is True and len(backend.calls_for("install", SERIAL_A)) == 1
    assert len(backend.calls_for("set_permissions", SERIAL_A)) == 1
    assert backend.snapshots[SERIAL_B].settings == ACTIVE_SETTINGS
    disconnected = await client.delete(
        f"/devices/{SERIAL_A}/agent/connection",
        params={"session_id": session_id(prepared.json())},
        headers=AUTH,
    )
    assert disconnected.json()["status"] == "disconnected"
    assert snapshot.settings == original and snapshot.pin is None
    assert backend.forwards == set() and manager.slots[SERIAL_A].session is None


async def test_api_pending_manual_authorization_id_cancels_only_its_owned_bootstrap(api) -> None:
    backend, manager, client = api
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    backend.snapshots[SERIAL_A].stop_service(original)
    pending = await client.post(
        f"/devices/{SERIAL_A}/agent/prepare", json={"enable_accessibility": False}, headers=AUTH
    )
    identifier = diagnostic(pending.json())["operation_id"]
    assert diagnostic(pending.json())["awaiting_authorization"] is True
    other = await client.post(
        f"/devices/{SERIAL_B}/agent/cancel", params={"operation_id": identifier}, headers=AUTH
    )
    assert other.json() == {"cancelled": False}
    cancelled = await client.post(
        f"/devices/{SERIAL_A}/agent/cancel", params={"operation_id": identifier}, headers=AUTH
    )
    assert cancelled.json() == {"cancelled": True}
    assert backend.snapshots[SERIAL_A].pin is None
    assert backend.snapshots[SERIAL_A].settings == original
    assert manager.slots[SERIAL_A].operation_id is None


@pytest.mark.parametrize(
    ("method", "route"), [("GET", "health"), ("GET", "capabilities"), ("DELETE", "connection")]
)
async def test_api_refuses_stale_and_other_serial_sessions_without_disturbing_live_connections(
    api, method, route
) -> None:
    backend, manager, client = api
    first = await client.post(f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH)
    second = await client.post(f"/devices/{SERIAL_B}/agent/connect", json={}, headers=AUTH)
    first_id, second_id = session_id(first.json()), session_id(second.json())
    before = list(backend.calls)
    crossed = await client.request(
        method,
        f"/devices/{SERIAL_B}/agent/{route}",
        params={"session_id": first_id},
        headers=AUTH,
    )
    assert crossed.status_code == 409 and crossed.json() == {"error": "stale_session"}
    assert backend.calls == before
    backend.snapshots[SERIAL_A].transport_id = "9"
    fresh = await client.get(f"/devices/{SERIAL_A}", headers=AUTH)
    fresh_id = session_id(fresh.json())
    assert fresh_id != first_id
    stale = await client.request(
        method,
        f"/devices/{SERIAL_A}/agent/{route}",
        params={"session_id": first_id},
        headers=AUTH,
    )
    assert stale.status_code == 409 and stale.json() == {"error": "stale_session"}
    assert session_id(await manager.health(SERIAL_A, fresh_id)) == fresh_id
    assert session_id(await manager.health(SERIAL_B, second_id)) == second_id
    assert backend.mutations == [] and len(backend.forwards) == 2


async def test_api_pending_operation_id_concurrent_serial_and_explicit_cancel_are_isolated(
    api,
) -> None:
    backend, manager, client = api
    gate = Gate()
    backend.gates[SERIAL_A, "health"] = gate
    identifier = str(uuid4())
    operation = asyncio.create_task(
        client.post(
            f"/devices/{SERIAL_A}/agent/connect", json={"operation_id": identifier}, headers=AUTH
        )
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        status = await client.get(f"/devices/{SERIAL_A}", headers=AUTH)
        listed = await client.get("/devices", headers=AUTH)
        pending = next(entry for entry in listed.json()["devices"] if entry["serial"] == SERIAL_A)
        assert status.json()["status"] == "connecting" and status.json()["ready"] is False
        assert diagnostic(status.json())["operation_id"] == identifier
        assert diagnostic(pending)["operation_id"] == identifier
        for route in ("prepare", "connect"):
            concurrent = await client.post(
                f"/devices/{SERIAL_A}/agent/{route}", json={}, headers=AUTH
            )
            assert concurrent.json()["operation_error"] == "operation_in_progress"
            assert diagnostic(concurrent.json())["operation_id"] == identifier
        other = await client.post(f"/devices/{SERIAL_B}/agent/connect", json={}, headers=AUTH)
        assert other.json()["ready"] is True
        wrong_serial = await client.post(
            f"/devices/{SERIAL_B}/agent/cancel", params={"operation_id": identifier}, headers=AUTH
        )
        wrong_id = await client.post(
            f"/devices/{SERIAL_A}/agent/cancel", params={"operation_id": str(uuid4())}, headers=AUTH
        )
        assert wrong_serial.json() == wrong_id.json() == {"cancelled": False}
        assert not operation.done() and not gate.cancelled.is_set()
        cancelled = await client.post(
            f"/devices/{SERIAL_A}/agent/cancel", params={"operation_id": identifier}, headers=AUTH
        )
        assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
        await asyncio.gather(operation, return_exceptions=True)
        final = await client.get(f"/devices/{SERIAL_A}", headers=AUTH)
        assert final.json()["ready"] is False
        assert diagnostic(final.json())["operation_id"] is None
        assert manager.slots[SERIAL_A].session is None
        assert gate.cancelled.is_set()
        assert all(row[0] == SERIAL_B for row in backend.forwards)
        assert len(backend.calls_for("forward_create", SERIAL_A)) == 1
        assert backend.mutations == []
    finally:
        gate.release.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_api_manual_recovery_confirmation_does_not_restart_or_replay_and_requires_new_process(
    api,
) -> None:
    backend, manager, client = api
    snapshot = backend.snapshots[SERIAL_A]
    snapshot.capabilities["recovery_required"] = True
    blocked = await client.post(f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH)
    assert blocked.json()["status"] == "recovery_required"
    snapshot.capabilities["recovery_required"] = False
    same = await client.post(
        f"/devices/{SERIAL_A}/agent/connect", json={"manual_recovery_confirmed": True}, headers=AUTH
    )
    assert same.json()["status"] == "recovery_required" and same.json()["ready"] is False
    snapshot.agent_process = 901
    snapshot.credential = "c" * 64
    snapshot.lifecycle = str(uuid4())
    automatic = await client.post(f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH)
    assert automatic.json()["status"] == "recovery_required"
    recovered = await client.post(
        f"/devices/{SERIAL_A}/agent/connect", json={"manual_recovery_confirmed": True}, headers=AUTH
    )
    assert recovered.json()["ready"] is True and manager.slots[SERIAL_A].quarantine is None
    assert backend.mutations == []


async def test_unexpected_backend_error_is_sanitized_in_api_diagnostic_and_logs(
    caplog,
) -> None:
    class BrokenHealthBackend(FakeAndroidBackend):
        async def health(self, forward: ForwardLease, token: str) -> AgentHealth:
            raise RuntimeError(f"private Agent credential: Bearer {token}")

    backend = BrokenHealthBackend()
    manager = DeviceConnectionManager(backend, readiness_wait=0)
    app = create_app(API_TOKEN, device_manager=manager)
    caplog.set_level("DEBUG", logger="pulse_runtime")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            response = await client.post(
                f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH
            )
        assert response.status_code == 200 and response.json()["ready"] is False
        assert diagnostic(response.json())["code"] == "connection_failed"
        output = response.text + caplog.text
        output += json.dumps([record.__dict__ for record in caplog.records], default=str)
        assert TOKEN_A not in output and "private Agent credential" not in output
        assert backend.forwards == set() and backend.mutations == []
    finally:
        await manager.close()


@pytest.mark.parametrize("site", ["inventory", "active-health"])
async def test_unexpected_discovery_or_active_health_error_never_leaks_private_evidence(
    caplog, site
) -> None:
    fail = False
    sentinel = f"Bearer {TOKEN_A} PRIVATE_INPUT RAW_ADB_OUTPUT"

    class SecretErrorBackend(FakeAndroidBackend):
        async def inventory(self):
            if fail and site == "inventory":
                raise RuntimeError(sentinel)
            return await super().inventory()

        async def health(self, forward: ForwardLease, token: str) -> AgentHealth:
            if fail and site == "active-health":
                raise RuntimeError(sentinel)
            return await super().health(forward, token)

    backend = SecretErrorBackend()
    manager = DeviceConnectionManager(backend, readiness_wait=0)
    app = create_app(API_TOKEN, device_manager=manager)
    caplog.set_level("DEBUG", logger="pulse_runtime")
    try:
        connected = await manager.connect(SERIAL_A)
        identifier = session_id(connected)
        fail = True
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            if site == "inventory":
                response = await client.get("/devices", headers=AUTH)
                assert response.status_code == 200 and response.json()["error"] == "adb_unavailable"
                snapshot = response.json()["devices"][0]
                assert snapshot["ready"] is False
                assert diagnostic(snapshot)["code"] == "adb_unavailable"
            else:
                response = await client.get(
                    f"/devices/{SERIAL_A}/agent/health",
                    params={"session_id": identifier},
                    headers=AUTH,
                )
                assert response.status_code == 409 and response.json() == {"error": "stale_session"}
                snapshot = manager.snapshot(manager.slots[SERIAL_A])
                assert (
                    snapshot["ready"] is False
                    and diagnostic(snapshot)["code"] == "connection_failed"
                )
        output = response.text + json.dumps(snapshot) + caplog.text
        output += json.dumps([record.__dict__ for record in caplog.records], default=str)
        assert (
            TOKEN_A not in output
            and "PRIVATE_INPUT" not in output
            and "RAW_ADB_OUTPUT" not in output
        )
        assert backend.forwards == set() and backend.mutations == []
        assert manager.slots[SERIAL_A].session is None
    finally:
        await manager.close()


CONFIG = b"\0\0\0\1\x67\x64\0\x1f\0\0\0\1\x68\xee"


@dataclass
class FakeVideoSource:
    info: StreamInfo = field(default_factory=lambda: StreamInfo("fake-video", 0))
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    continue_frame: asyncio.Event = field(default_factory=asyncio.Event)
    frame_delivered: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def events(self) -> AsyncIterator[SessionMeta | Packet]:
        yield SessionMeta(720, 1600, False)
        yield Packet(PacketKind.CONFIG, 0, CONFIG, 1000.0)
        yield Packet(PacketKind.KEY, 1, b"key", 1001.0)
        self.ready.set()
        await self.continue_frame.wait()
        yield Packet(PacketKind.DELTA, 2, b"continued", 1002.0)
        self.frame_delivered.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stopped.set()


@pytest.mark.parametrize("agent_end", ["disconnect", "recovery-required"])
async def test_agent_disconnect_or_recovery_does_not_stop_or_replace_existing_stream(
    rig, agent_end
) -> None:
    backend, manager = rig
    source = FakeVideoSource()
    hub = StreamHub(lambda _serial, _options: source, clock_factory=None)
    app = create_app(API_TOKEN, device_manager=manager, hub=hub)
    stream, viewer = hub.subscribe(SERIAL_A, VideoOptions())
    try:
        await asyncio.wait_for(source.ready.wait(), 2)
        capture = stream.task
        media_session = dict(stream.session)
        assert stream.frames_received == 1  # CONFIG is not counted as a video frame.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            connected = await client.post(
                f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH
            )
            identifier = session_id(connected.json())
            if agent_end == "disconnect":
                ended = await client.delete(
                    f"/devices/{SERIAL_A}/agent/connection",
                    params={"session_id": identifier},
                    headers=AUTH,
                )
                assert ended.json()["status"] == "disconnected"
            else:
                backend.snapshots[SERIAL_A].capabilities["recovery_required"] = True
                ended = await client.get(f"/devices/{SERIAL_A}", headers=AUTH)
                assert ended.json()["status"] == "recovery_required"
            assert ended.json()["ready"] is False
            assert stream.task is capture and capture is not None and not capture.done()
            assert stream.session == media_session
            assert viewer in stream.subscribers and not source.stopped.is_set()
            source.continue_frame.set()
            await asyncio.wait_for(source.frame_delivered.wait(), 2)
            state = await client.get("/debug/stream", headers=AUTH)
            assert state.json()["streams"][SERIAL_A]["status"]["state"] == "streaming"
            assert state.json()["streams"][SERIAL_A]["session"] == media_session
            assert stream.frames_received == 2
            assert stream.gop[-1].packet.kind == PacketKind.DELTA
            assert stream.gop[-1].packet.data == b"continued"
            assert backend.mutations == []
        await hub.unsubscribe(stream, viewer)
        assert source.stopped.is_set()
    finally:
        await hub.close()


async def test_last_stream_viewer_close_does_not_disconnect_agent_connection(rig) -> None:
    backend, manager = rig
    source = FakeVideoSource()
    hub = StreamHub(lambda _serial, _options: source, clock_factory=None)
    app = create_app(API_TOKEN, device_manager=manager, hub=hub)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            connected = await client.post(
                f"/devices/{SERIAL_A}/agent/connect", json={}, headers=AUTH
            )
            identifier = session_id(connected.json())
            stream, viewer = hub.subscribe(SERIAL_A, VideoOptions())
            await asyncio.wait_for(source.ready.wait(), 2)
            await hub.unsubscribe(stream, viewer)
            assert source.stopped.is_set() and hub.streams == {}
            health = await client.get(
                f"/devices/{SERIAL_A}/agent/health", params={"session_id": identifier}, headers=AUTH
            )
            assert health.json()["ready"] is True and session_id(health.json()) == identifier
            assert len(backend.forwards) == 1 and backend.mutations == []
    finally:
        await hub.close()


def test_existing_stream_websocket_auth_and_media_do_not_require_agent_ready() -> None:
    backend = FakeAndroidBackend()
    backend.snapshots[SERIAL_A].package = None
    manager = DeviceConnectionManager(backend, readiness_wait=0)
    source = FakeVideoSource()
    hub = StreamHub(lambda _serial, _options: source, clock_factory=None)
    client = TestClient(create_app(API_TOKEN, device_manager=manager, hub=hub))
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        client.websocket_connect(f"/ws/stream/{SERIAL_A}?token=wrong"),
    ):
        pass
    assert denied.value.code == 4401 and hub.streams == {}
    with client.websocket_connect(f"/ws/stream/{SERIAL_A}?token={API_TOKEN}") as ws:
        assert ws.receive_json()["type"] == "hello"
        messages = [ws.receive() for _ in range(7)]
        binary = [message["bytes"] for message in messages if "bytes" in message]
        texts = [json.loads(message["text"]) for message in messages if "text" in message]
        assert len(binary) == 2
        assert any(message.get("state") == "streaming" for message in texts)
        assert source.ready.is_set() and not source.stopped.is_set()
        assert manager.slots == {} and backend.calls == []
    assert source.stopped.is_set() and hub.streams == {}


async def test_runtime_lifespan_cleans_only_its_sessions_settings_pin_forward_and_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeAndroidBackend()
    snapshot = backend.snapshots[SERIAL_A]
    original = AccessibilitySettings(OTHER_SERVICE, "0")
    snapshot.stop_service(original)
    snapshot.pin = "2000:400"
    foreign = SERIAL_B, "tcp:44000", "localabstract:foreign-owner"
    backend.forwards.add(foreign)
    owned = DeviceConnectionManager(backend, readiness_wait=0)
    external = DeviceConnectionManager(backend, readiness_wait=0)
    owned_source, external_source = FakeVideoSource(), FakeVideoSource()
    owned_hub = StreamHub(lambda _serial, _options: owned_source, clock_factory=None)
    external_hub = StreamHub(lambda _serial, _options: external_source, clock_factory=None)
    monkeypatch.setattr(app_module, "metrics_interval", lambda: 60.0)
    app = create_app(API_TOKEN, device_manager=owned, hub=owned_hub)
    try:
        prepared = await owned.prepare(SERIAL_A)
        other = await external.connect(SERIAL_B)
        assert prepared["ready"] is True and other["ready"] is True
        external_id = session_id(other)
        owned_hub.subscribe(SERIAL_A, VideoOptions())
        external_stream, external_viewer = external_hub.subscribe(SERIAL_B, VideoOptions())
        await asyncio.wait_for(owned_source.ready.wait(), 2)
        await asyncio.wait_for(external_source.ready.wait(), 2)
        external_capture = external_stream.task
        async with app.router.lifespan_context(app):
            assert app.state.device_connections is owned and owned.watcher is not None
            assert not owned.closed and not external.closed
        assert owned.closed and owned.watcher is None and owned.slots[SERIAL_A].session is None
        assert snapshot.settings == original and snapshot.pin == "2000:400"
        assert backend.peer_leases == {}
        assert owned_source.stopped.is_set() and owned_hub.streams == {}
        assert not external.closed and not external_source.stopped.is_set()
        assert external_stream.task is external_capture
        assert external_capture is not None and not external_capture.done()
        assert external_viewer in external_stream.subscribers
        assert session_id(await external.health(SERIAL_B, external_id)) == external_id
        assert backend.snapshots[SERIAL_B].settings == ACTIVE_SETTINGS
        assert foreign in backend.forwards and len(backend.forwards) == 2
        assert all(call[1] == SERIAL_A for call in backend.mutations)
        before = list(backend.calls)
        await owned.close()
        assert backend.calls == before
    finally:
        await owned.close()
        await external.close()
        await owned_hub.close()
        await external_hub.close()
