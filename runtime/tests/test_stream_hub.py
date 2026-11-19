import asyncio
import io
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect

from pulse_observability import JsonFormatter, bind_context
from pulse_relay.app import Relay
from pulse_relay.app import start as start_relay
from pulse_runtime.api.app import create_app, video_options
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.drivers.android.scrcpy_video import (
    ClockSample,
    Packet,
    PacketKind,
    SessionMeta,
    SourceError,
    StreamInfo,
    VideoOptions,
)
from pulse_runtime.stream import hub as hub_mod
from pulse_runtime.stream.envelope import HEADER_SIZE, Envelope, decode, encode
from pulse_runtime.stream.hub import CaptureOrientationConflict, StreamHub, Subscriber
from pulse_runtime.stream.relay_publisher import RelayPublisher
from runtime.tests.test_device_connections import FakeAndroidBackend

CONFIG = b"\0\0\0\1" + bytes([0x67, 0x64, 0x00, 0x1F]) + b"\0\0\0\1" + bytes([0x68, 0xEE])


@dataclass
class FakeSource:
    """Feeds scripted events; a ``None`` entry waits until ``release`` is set."""

    script: list[SessionMeta | Packet | Exception | None]
    info: StreamInfo | None = field(default_factory=lambda: StreamInfo("fake", 0))
    stopped: bool = False
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def events(self) -> AsyncIterator[SessionMeta | Packet]:
        for item in self.script:
            if item is None:
                await self.release.wait()
            elif isinstance(item, Exception):
                raise item
            else:
                yield item
                await asyncio.sleep(0)
        await asyncio.Event().wait()  # stream stays open

    async def stop(self) -> None:
        self.stopped = True


def pkt(kind: PacketKind, pts: int, data: bytes = b"x") -> Packet:
    return Packet(kind, pts, CONFIG if kind == PacketKind.CONFIG else data, 1000.0)


def standard_script() -> list[SessionMeta | Packet | Exception | None]:
    return [
        SessionMeta(720, 1600, False),
        pkt(PacketKind.CONFIG, 0),
        pkt(PacketKind.KEY, 1),
        pkt(PacketKind.DELTA, 2),
    ]


async def take(sub: Subscriber, n: int) -> list[str | bytes]:
    out: list[str | bytes] = []
    agen = sub.messages(stats_interval=60)
    while len(out) < n:
        out.append(await asyncio.wait_for(agen.__anext__(), 2))
    await agen.aclose()
    return out


def kinds(msgs: list[str | bytes]) -> list[str]:
    return [
        json.loads(m)["type"] if isinstance(m, str) else decode(m).kind.name.lower() for m in msgs
    ]


def test_envelope_roundtrip() -> None:
    env = Envelope(PacketKind.KEY, 7, 123456789, 1.5, 2.5, b"payload")
    data = encode(env)
    assert len(data) == HEADER_SIZE + 7
    assert data[1] == 2  # relay reads only this byte
    assert decode(data) == env


async def test_shared_capture_and_late_joiner_gets_config_and_gop() -> None:
    sources: list[FakeSource] = []

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        sources.append(FakeSource(standard_script()))
        return sources[-1]

    hub = StreamHub(factory, clock_factory=None)
    stream, a = hub.subscribe("S1", VideoOptions())
    first = kinds(await take(a, 9))
    assert first[:2] == ["hello", "status"]
    assert [k for k in first if k in ("config", "key", "delta")] == ["config", "key", "delta"]
    _, b = hub.subscribe("S1", VideoOptions(max_size=720))
    late = kinds(await take(b, 6))
    assert late[:3] == ["hello", "status", "session"]
    assert late[3:] == ["config", "key", "delta"]
    assert len(sources) == 1  # one capture shared by both subscribers

    await hub.unsubscribe(stream, a)
    assert not sources[0].stopped
    await hub.unsubscribe(stream, b)
    assert sources[0].stopped  # last viewer gone -> capture stops
    assert "S1" not in hub.streams


@pytest.mark.parametrize("leave_again", [False, True], ids=["rejoin", "rejoin-and-leave"])
async def test_rejoin_waits_for_slow_source_stop_without_interrupting_cleanup(
    leave_again: bool,
) -> None:
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()
    stop_interrupted = asyncio.Event()
    second_remove_started = asyncio.Event()
    sources: list[FakeSource] = []

    class SlowStopSource(FakeSource):
        async def stop(self) -> None:
            stop_started.set()
            try:
                await allow_stop.wait()
            except asyncio.CancelledError:
                stop_interrupted.set()
                raise
            self.stopped = True

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        if sources:
            assert sources[-1].stopped  # never overlap captures for this serial
            source = FakeSource(standard_script())
        else:
            source = SlowStopSource(standard_script())
        sources.append(source)
        return source

    hub = StreamHub(factory, clock_factory=None)
    stream, first = hub.subscribe("S1", VideoOptions())
    removals: list[asyncio.Task[None]] = []
    try:
        await take(first, 9)
        capture_task = stream.task
        assert capture_task is not None
        removals.append(asyncio.create_task(hub.unsubscribe(stream, first)))
        await asyncio.wait_for(stop_started.wait(), 2)

        rejoined_stream, rejoined = hub.subscribe("S1", VideoOptions(max_size=720))
        assert rejoined_stream is stream and stream.task is capture_task
        assert len(sources) == 1

        if leave_again:

            async def remove_again() -> None:
                second_remove_started.set()
                await hub.unsubscribe(stream, rejoined)

            removals.append(asyncio.create_task(remove_again()))
            await asyncio.wait_for(second_remove_started.wait(), 2)
            await asyncio.sleep(0)
            assert not stop_interrupted.is_set()
            assert not capture_task.done()

        allow_stop.set()
        await asyncio.wait_for(asyncio.gather(*removals), 2)
        assert sources[0].stopped and not stop_interrupted.is_set()

        if leave_again:
            assert len(sources) == 1 and stream.task is None
            assert "S1" not in hub.streams
        else:
            await take(rejoined, 9)
            assert len(sources) == 2
            assert stream.options.max_size == 720
            await hub.unsubscribe(stream, rejoined)
    finally:
        allow_stop.set()
        await asyncio.gather(*removals, return_exceptions=True)
        await hub.close()


async def test_source_failure_reports_error_and_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_mod, "BACKOFF_MIN", 0.01)
    attempts: list[FakeSource] = []

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        script = standard_script() if attempts else [SourceError("device offline")]
        attempts.append(FakeSource(script))
        return attempts[-1]

    hub = StreamHub(factory, clock_factory=None)
    stream, sub = hub.subscribe("S1", VideoOptions())
    statuses: list[dict[str, object]] = []
    agen = sub.messages(stats_interval=60)
    while not any(s.get("state") == "streaming" for s in statuses):
        msg = await asyncio.wait_for(agen.__anext__(), 2)
        if isinstance(msg, str) and json.loads(msg)["type"] == "status":
            statuses.append(json.loads(msg))
    await agen.aclose()
    states = [s["state"] for s in statuses]
    assert "error" in states
    err = next(s for s in statuses if s["state"] == "error")
    assert "device offline" in str(err["message"])
    assert len(attempts) == 2 and attempts[0].stopped
    await hub.unsubscribe(stream, sub)


def test_video_options_from_query() -> None:
    o = video_options({"max_size": "720", "bit_rate": "8000000", "max_fps": "30"})
    assert (o.max_size, o.video_bit_rate, o.max_fps) == (720, 8_000_000, 30)
    with pytest.raises(ValueError):
        video_options({"bit_rate": "abc"})


def test_capture_orientation_query_is_finite_and_defaults_to_display() -> None:
    assert video_options({}).capture_orientation == "display"
    for profile in ("display", "natural"):
        assert video_options({"capture_orientation": profile}).capture_orientation == profile
    for invalid in ("", "@0", "@90", "landscape"):
        with pytest.raises(ValueError, match="capture_orientation"):
            video_options({"capture_orientation": invalid})


@pytest.mark.parametrize("profile,conflicting", [("display", "natural"), ("natural", "display")])
async def test_capture_orientation_conflict_preserves_capture_and_same_profile_reconnect(
    profile: Literal["display", "natural"], conflicting: Literal["display", "natural"]
) -> None:
    sources: list[FakeSource] = []

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        sources.append(FakeSource([*standard_script(), None, pkt(PacketKind.DELTA, 3)]))
        return sources[-1]

    hub = StreamHub(factory, clock_factory=None)
    options = VideoOptions(capture_orientation=profile)
    stream, original = hub.subscribe("S1", options)
    try:
        first = await take(original, 9)
        assert json.loads(first[0])["options"]["capture_orientation"] == profile
        sessions = [
            json.loads(msg)
            for msg in first
            if isinstance(msg, str) and json.loads(msg)["type"] == "session"
        ]
        assert sessions and all(msg["capture_orientation"] == profile for msg in sessions)
        task, session, gop = stream.task, stream.session, list(stream.gop)

        with pytest.raises(CaptureOrientationConflict, match="capture_orientation_conflict"):
            hub.subscribe("S1", VideoOptions(capture_orientation=conflicting))

        assert hub.streams["S1"] is stream and stream.subscribers == {original}
        assert stream.task is task and task is not None and not task.done()
        assert stream.options is options and stream.session is session and stream.gop == gop
        assert len(sources) == 1 and not sources[0].stopped

        same_stream, reconnected = hub.subscribe(
            "S1", VideoOptions(max_size=720, capture_orientation=profile)
        )
        replay = await take(reconnected, 6)
        assert same_stream is stream
        assert json.loads(replay[0])["options"] == options.as_dict()
        assert json.loads(replay[2])["capture_orientation"] == profile
        sources[0].release.set()
        for subscriber in (original, reconnected):
            continued = (await take(subscriber, 1))[0]
            assert isinstance(continued, bytes) and decode(continued).pts_us == 3
        assert len(sources) == 1 and not sources[0].stopped
    finally:
        await hub.close()


async def test_capture_orientation_conflict_while_source_cleanup_has_no_subscribers() -> None:
    stop_entered, allow_stop = asyncio.Event(), asyncio.Event()
    sources: list[FakeSource] = []
    used_options: list[VideoOptions] = []

    class SlowStopSource(FakeSource):
        async def stop(self) -> None:
            stop_entered.set()
            await allow_stop.wait()
            self.stopped = True

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        assert not sources or sources[-1].stopped
        source = FakeSource(standard_script()) if sources else SlowStopSource(standard_script())
        sources.append(source)
        used_options.append(opts)
        return source

    hub = StreamHub(factory, clock_factory=None)
    options = VideoOptions(capture_orientation="natural")
    stream, original = hub.subscribe("S1", options)
    removal: asyncio.Task[None] | None = None
    try:
        await take(original, 9)
        task = stream.task
        removal = asyncio.create_task(hub.unsubscribe(stream, original))
        await asyncio.wait_for(stop_entered.wait(), 2)
        assert not stream.subscribers and task is not None and not task.done()
        with pytest.raises(CaptureOrientationConflict):
            hub.subscribe("S1", VideoOptions())
        assert stream.options is options and stream.task is task and not sources[0].stopped
        same_stream, reconnected = hub.subscribe("S1", options)
        assert same_stream is stream and len(sources) == 1
        allow_stop.set()
        await asyncio.wait_for(removal, 2)
        messages = await take(reconnected, 9)
        assert json.loads(messages[0])["options"]["capture_orientation"] == "natural"
        assert stream.session is not None and stream.session["capture_orientation"] == "natural"
        assert len(sources) == 2 and used_options == [options, options]
    finally:
        allow_stop.set()
        if removal is not None:
            await asyncio.gather(removal, return_exceptions=True)
        await hub.close()


async def test_stopped_capture_can_change_profile_without_replaying_old_session() -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    old, original = hub.subscribe("S1", VideoOptions(capture_orientation="natural"))
    try:
        await take(original, 9)
        await old.remove(original)
        assert old.task is None and old.session is not None
        new, subscriber = hub.subscribe("S1", VideoOptions())
        assert new is not old and new.session is None and new.config is None
        assert kinds(list(subscriber.texts)) == ["hello", "status"]
        messages = await take(subscriber, 9)
        assert json.loads(messages[0])["options"]["capture_orientation"] == "display"
        assert new.session is not None and new.session["capture_orientation"] == "display"
    finally:
        await hub.close()


def test_stream_ws_requires_token_and_streams() -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    client = TestClient(create_app("tok", hub=hub))
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws/stream/S1?token=bad") as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 4401
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws/stream/S1?token=tok&max_size=-1") as ws,
    ):
        ws.receive_text()
    assert exc.value.code == 4400

    with client.websocket_connect("/ws/stream/S1?token=tok&max_size=720") as ws:
        hello = ws.receive_json()
        assert hello == {
            "type": "hello",
            "serial": "S1",
            "options": VideoOptions(max_size=720).as_dict(),
        }
        seen: list[PacketKind] = []
        while PacketKind.DELTA not in seen:
            msg = ws.receive()
            if msg.get("bytes"):
                seen.append(decode(msg["bytes"]).kind)
        assert seen == [PacketKind.CONFIG, PacketKind.KEY, PacketKind.DELTA]


@pytest.mark.parametrize("profile,conflicting", [("display", "natural"), ("natural", "display")])
def test_stream_ws_profile_conflict_and_original_profile_reconnect(
    profile: Literal["display", "natural"], conflicting: Literal["display", "natural"]
) -> None:
    sources: list[FakeSource] = []

    def factory(serial: str, opts: VideoOptions) -> FakeSource:
        sources.append(FakeSource(standard_script()))
        return sources[-1]

    hub = StreamHub(factory, clock_factory=None)
    manager = DeviceConnectionManager(FakeAndroidBackend(), readiness_wait=0)
    route = f"/ws/stream/S1?token=tok&capture_orientation={profile}"
    rejected_route = "/ws/stream/S1?token=tok"
    if conflicting == "natural":
        rejected_route += "&capture_orientation=natural"
    with TestClient(create_app("tok", hub=hub, device_manager=manager)) as client:
        with client.websocket_connect(route) as original:
            assert original.receive_json()["options"]["capture_orientation"] == profile
            messages = [original.receive() for _ in range(8)]
            sessions = [
                json.loads(msg["text"])
                for msg in messages
                if "text" in msg and json.loads(msg["text"])["type"] == "session"
            ]
            assert sessions and all(msg["capture_orientation"] == profile for msg in sessions)
            stream = hub.streams["S1"]
            task = stream.task
            with (
                pytest.raises(WebSocketDisconnect) as denied,
                client.websocket_connect(rejected_route) as rejected,
            ):
                rejected.receive_json()
            assert denied.value.code == 1008
            assert denied.value.reason == "capture_orientation_conflict"
            assert stream.task is task and len(stream.subscribers) == 1
            assert len(sources) == 1 and not sources[0].stopped
            with client.websocket_connect(route) as reconnected:
                assert reconnected.receive_json()["options"]["capture_orientation"] == profile
                assert reconnected.receive_json()["type"] == "status"
                assert reconnected.receive_json()["capture_orientation"] == profile
            assert not sources[0].stopped and stream.task is task
        assert sources[0].stopped and not hub.streams
        with client.websocket_connect(rejected_route) as changed:
            assert changed.receive_json()["options"]["capture_orientation"] == conflicting
    assert len(sources) == 2 and sources[1].stopped and not hub.streams


@pytest.mark.parametrize(
    "query",
    [
        "capture_orientation=@0",
        "capture_orientation=",
        "capture_orientation=display&capture_orientation=natural",
    ],
)
def test_stream_ws_rejects_invalid_or_duplicate_capture_orientation(query: str) -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    client = TestClient(create_app("tok", hub=hub))
    with (
        pytest.raises(WebSocketDisconnect) as denied,
        client.websocket_connect(f"/ws/stream/S1?token=tok&{query}") as rejected,
    ):
        rejected.receive_json()
    assert denied.value.code == 4400 and "capture_orientation" in denied.value.reason
    assert not hub.streams


def test_stream_ws_rejects_conflict_with_pending_publisher_without_hub_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def pending_forward(self: RelayPublisher) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(RelayPublisher, "_forward", pending_forward)
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    manager = DeviceConnectionManager(FakeAndroidBackend(), readiness_wait=0)
    headers = {"Authorization": "Bearer tok"}
    route = "/debug/stream/S1/relay"
    with TestClient(create_app("tok", hub=hub, device_manager=manager)) as client:
        response = client.post(
            route,
            json={"relay_url": "ws://127.0.0.1:1", "channel": "test", "token": "t"},
            headers=headers,
        )
        assert response.status_code == 200
        publisher_id = response.json()["publisher_id"]
        assert client.portal is not None
        client.portal.call(asyncio.wait_for, entered.wait(), 2)
        assert not hub.streams
        with (
            pytest.raises(WebSocketDisconnect) as denied,
            client.websocket_connect("/ws/stream/S1?token=tok&capture_orientation=natural") as ws,
        ):
            ws.receive_json()
        assert denied.value.code == 1008
        assert denied.value.reason == "capture_orientation_conflict"
        assert not stopped.is_set() and not hub.streams
        assert client.post(
            route + "/keepalive", json={"publisher_id": publisher_id}, headers=headers
        ).json() == {"renewed": True}
        assert client.delete(
            route, params={"publisher_id": publisher_id}, headers=headers
        ).json() == {"stopped": True}
        assert stopped.is_set()


def test_relay_start_conflict_does_not_stop_existing_natural_viewer() -> None:
    source = FakeSource(standard_script())
    hub = StreamHub(lambda s, o: source, clock_factory=None)
    manager = DeviceConnectionManager(FakeAndroidBackend(), readiness_wait=0)
    headers = {"Authorization": "Bearer tok"}
    with (
        TestClient(create_app("tok", hub=hub, device_manager=manager)) as client,
        client.websocket_connect("/ws/stream/S1?token=tok&capture_orientation=natural") as ws,
    ):
        assert ws.receive_json()["options"]["capture_orientation"] == "natural"
        for _ in range(8):
            ws.receive()
        stream = hub.streams["S1"]
        task = stream.task
        response = client.post(
            "/debug/stream/S1/relay",
            json={"relay_url": "ws://127.0.0.1:1", "channel": "test", "token": "t"},
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "capture_orientation_conflict"}
        assert stream.task is task and len(stream.subscribers) == 1 and not source.stopped
        assert client.get("/debug/stream", headers=headers).json()["publishers"] == {}
    assert source.stopped and not hub.streams


async def test_bootstrap_replays_configuration_and_complete_gop_after_transport_reconnect() -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    stream, sub = hub.subscribe("S1", VideoOptions())
    await take(sub, 9)
    stream.bootstrap(sub)
    replay = await take(sub, 6)
    assert kinds(replay)[-3:] == ["config", "key", "delta"]
    assert [decode(m).seq for m in replay if isinstance(m, bytes)] == [0, 1, 2]
    await hub.unsubscribe(stream, sub)


async def test_clock_failure_invalidates_old_offset_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hub_mod, "CLOCK_INTERVAL", 0.01)
    probes: list[object] = []

    class FlakyClock:
        calls = 0

        async def start(self) -> None:
            probes.append(self)

        async def measure(self, rounds: int = 15) -> ClockSample:
            self.calls += 1
            if len(probes) == 1 and self.calls > 1:
                raise SourceError("clock disconnected")
            return ClockSample(10.0, 1000.0, time.time() * 1000)

        async def stop(self) -> None:
            pass

    hub = StreamHub(lambda s, o: FakeSource(standard_script()), lambda s: FlakyClock())
    stream, sub = hub.subscribe("S1", VideoOptions())
    clocks = []
    messages = sub.messages()
    async with asyncio.timeout(2):
        while len(clocks) < 3:
            msg = await anext(messages)
            if isinstance(msg, str) and json.loads(msg)["type"] == "clock":
                clocks.append(json.loads(msg))
    assert [c["valid"] for c in clocks] == [True, False, True]
    assert "offset_us" not in clocks[1]
    assert clocks[2]["max_age_ms"] == 15_000
    await messages.aclose()
    await hub.unsubscribe(stream, sub)


async def test_expired_publisher_stops_capture_without_stopping_runtime() -> None:
    source = FakeSource(standard_script())
    hub = StreamHub(lambda s, o: source, clock_factory=None)
    pub = RelayPublisher(
        hub, "S1", VideoOptions(), "ws://127.0.0.1:1", "test", "t", lease_seconds=0.05
    )
    pub.start()
    assert pub.task is not None
    await asyncio.wait_for(pub.task, 2)
    assert pub.state["state"] == "expired"
    assert not pub.renew()
    assert source.stopped and not hub.streams


def test_relay_keepalive_and_delete_cannot_affect_replacement_publisher() -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    manager = DeviceConnectionManager(FakeAndroidBackend(), readiness_wait=0)
    with TestClient(create_app("tok", hub=hub, device_manager=manager)) as client:
        headers = {"Authorization": "Bearer tok"}
        route = "/debug/stream/S1/relay"
        body = {"relay_url": "ws://127.0.0.1:1", "channel": "test", "token": "t"}
        a = client.post(route, json=body, headers=headers).json()["publisher_id"]
        b = client.post(route, json=body, headers=headers).json()["publisher_id"]
        assert a != b
        assert (
            client.post(route + "/keepalive", json={"publisher_id": a}, headers=headers).status_code
            == 404
        )
        assert client.delete(route, params={"publisher_id": a}, headers=headers).json() == {
            "stopped": False
        }
        assert client.post(
            route + "/keepalive", json={"publisher_id": b}, headers=headers
        ).json() == {"renewed": True}
        assert client.delete(route, params={"publisher_id": b}, headers=headers).json() == {
            "stopped": True
        }
    assert not hub.streams


async def test_disconnected_relay_without_viewers_still_rejects_profile_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = asyncio.Event()

    def offline(*args: object, **kwargs: object) -> None:
        attempted.set()
        raise OSError("fake relay offline")

    monkeypatch.setattr("pulse_runtime.stream.relay_publisher.connect", offline)
    source = FakeSource(standard_script())
    hub = StreamHub(lambda s, o: source, clock_factory=None)
    pub = RelayPublisher(hub, "S1", VideoOptions(), "ws://127.0.0.1:1", "test", "t")
    pub.start()
    try:
        await asyncio.wait_for(attempted.wait(), 2)
        stream = hub.streams["S1"]
        assert len(stream.subscribers) == 1
        await take(next(iter(stream.subscribers)), 9)
        task = stream.task
        with pytest.raises(CaptureOrientationConflict):
            hub.subscribe("S1", VideoOptions(capture_orientation="natural"))
        assert stream.task is task and not source.stopped
        assert pub.task is not None and not pub.task.done() and pub.renew()
    finally:
        await pub.stop()
    assert source.stopped and not hub.streams


async def test_real_relay_reconnect_replays_config_without_restarting_capture() -> None:
    source = FakeSource(standard_script())
    hub = StreamHub(lambda s, o: source, clock_factory=None)
    relay = Relay("t")
    server = await start_relay(relay, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    with bind_context(trace_id="c" * 32):
        pub = RelayPublisher(hub, "S1", VideoOptions(), base, "test", "t")
    pub.start()
    try:
        async with connect(base + "/sub/test?token=t") as viewer:

            async def until_key() -> list[PacketKind]:
                seen = []
                async with asyncio.timeout(5):
                    while PacketKind.KEY not in seen:
                        msg = await viewer.recv()
                        if isinstance(msg, bytes):
                            seen.append(decode(msg).kind)
                return seen

            assert await until_key() == [PacketKind.CONFIG, PacketKind.KEY]
            upstream = relay.channels["test"].publisher
            assert upstream is not None
            assert upstream.request is not None
            assert upstream.request.headers["X-Trace-ID"] == pub.trace_id == "c" * 32
            assert hub.streams["S1"].trace_id != pub.trace_id
            await upstream.close(1012, "injected relay reconnect")
            async with asyncio.timeout(3):
                while True:
                    msg = await viewer.recv()
                    if isinstance(msg, str):
                        status = json.loads(msg)
                        if (
                            status.get("type") == "relay-status"
                            and status["state"] == "disconnected"
                        ):
                            break
            assert await until_key() == [PacketKind.CONFIG, PacketKind.KEY]
            assert not source.stopped
            assert hub.streams["S1"].restarts == 0
            assert pub.metrics_snapshot()["connect_attempts_total"] == 2
            assert pub.metrics_snapshot()["reconnects_total"] == 1
            assert pub.metrics_snapshot()["messages_sent_total"] > 0
    finally:
        await pub.stop()
        server.close()
        await server.wait_closed()


async def test_shared_capture_trace_is_detached_from_subscribers_and_devices() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("runtime"))
    logger = logging.getLogger("pulse_runtime.stream")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    try:
        with bind_context(trace_id="a" * 32, run_id="unrelated", session_id="old", channel="old"):
            first, a = hub.subscribe("S1", VideoOptions())
        with bind_context(trace_id="b" * 32):
            _, b = hub.subscribe("S1", VideoOptions())
            second, c = hub.subscribe("S2", VideoOptions())
        await take(a, 9)
        await take(c, 9)
        assert first.trace_id != second.trace_id
        assert first.trace_id not in {a.trace_id, b.trace_id}
        assert first.metrics_snapshot()["frames_received_total"] == 2
        assert first.metrics_snapshot()["payload_bytes_received_total"] == len(CONFIG) + 2
        assert first.metrics_snapshot()["capture_setup_ms"]["count"] == 1
        first.log_metrics()
        second.log_metrics()
        await hub.unsubscribe(first, a)
        assert first.subscribers == {b}
        await hub.unsubscribe(first, b)
        await hub.unsubscribe(second, c)
        entries = [json.loads(line) for line in output.getvalue().splitlines()]
        capture = [
            entry
            for entry in entries
            if entry["event"] in {"stream.state", "stream.session_started", "stream.metrics"}
        ]
        assert capture
        for entry in capture:
            stream = first if entry["serial"] == "S1" else second
            assert entry["trace_id"] == stream.trace_id
            assert entry["run_id"] is None and entry["channel"] is None
            assert (
                entry["session_id"] is None or entry["session_id"] == stream.session["session_id"]
            )
        linked = [entry for entry in entries if entry["event"] == "stream.subscribed"]
        assert [entry["data"]["capture_trace_id"] for entry in linked] == [
            first.trace_id,
            first.trace_id,
            second.trace_id,
        ]
        assert any(
            entry["event"] == "stream.unsubscribed" and entry["trace_id"] == "a" * 32
            for entry in entries
        )
    finally:
        await hub.close()
        logger.removeHandler(handler)
        logger.setLevel(old_level)


async def test_metrics_reuse_counter_scope_and_detect_expired_clock() -> None:
    hub = StreamHub(lambda s, o: FakeSource(standard_script()), clock_factory=None)
    stream, sub = hub.subscribe("S1", VideoOptions())
    try:
        await take(sub, 9)
        for i in range(100):
            stream._frame(pkt(PacketKind.DELTA, i + 3))
        metrics = stream.metrics_snapshot()
        assert metrics["capture_attempts_total"] == 1 and metrics["capture_sessions_total"] == 1
        assert metrics["frames_received_total"] == 102
        assert sub.metrics_snapshot()["dropped_frames_total"] > 0
        assert sub.metrics_snapshot()["resyncs_total"] > 0
        assert sub.metrics_snapshot()["dequeued_packets_total"] == 3
        stream.clock = {"valid": True, "at_ms": time.time() * 1000, "rtt_us": 1250.0}
        assert stream.metrics_snapshot()["clock_valid"] is True
        assert stream.metrics_snapshot()["clock_rtt_ms"] == 1.25
        stream.clock["at_ms"] -= 16000
        assert stream.metrics_snapshot()["clock_valid"] is False
        assert stream.metrics_snapshot()["clock_rtt_ms"] is None
    finally:
        await hub.unsubscribe(stream, sub)
    assert not hub.streams
