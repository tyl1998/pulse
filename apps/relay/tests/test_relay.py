import asyncio
import json
import logging
import struct
from collections.abc import AsyncIterator, Iterator
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request

from pulse_observability import JsonFormatter, bind_context, current_context, new_trace_id
from pulse_relay import app
from pulse_relay.app import Delivery, DropQueue, Relay, kind_of, parse_route, start


def frame(kind: int, n: int) -> bytes:
    return struct.pack(">BBHI", 1, kind, 0, n) + b"\0" * 24 + b"payload"


def test_parse_route() -> None:
    assert parse_route("/pub/dev-1?token=a") == ("pub", "dev-1", "a")
    assert parse_route("/sub/ch") == ("sub", "ch", None)
    assert parse_route("/other/ch") is None
    assert parse_route("/sub/") is None


def test_kind_of_only_reads_envelope_byte() -> None:
    assert kind_of("text") is None
    assert kind_of(frame(2, 0)) == 2


def test_drop_queue_drops_to_next_key() -> None:
    q = DropQueue(2)
    assert not q.push(frame(3, 0))  # no key yet
    assert q.push(frame(1, 1))  # config always
    assert q.push(frame(2, 2))
    assert q.push(frame(3, 3))
    assert q.push("meta")  # text never dropped
    assert not q.push(frame(3, 4))  # overflow -> drop backlog, skip
    assert not q.push(frame(3, 5))
    assert q.push(frame(2, 6))
    out = []
    while (m := q.pop()) is not None:
        out.append(m.message)
    assert out == [frame(1, 1), "meta", frame(2, 6)]


def test_routing_isolated_per_channel_with_late_join_cache() -> None:
    relay = Relay("t")
    a = relay.add_sub("A")
    b = relay.add_sub("B")
    relay.forward("A", "hello")
    relay.forward("A", frame(1, 0))
    relay.forward("A", frame(2, 1))
    relay.forward("A", frame(3, 2))
    assert len(a.queue.items) == 5  # relay status + publisher messages
    assert len(b.queue.items) == 1  # relay waiting only, no A data
    late = relay.add_sub("A")
    assert [m.message for m in list(late.queue.items)[1:]] == [
        "hello",
        frame(1, 0),
        frame(2, 1),
        frame(3, 2),
    ]
    relay.remove_sub("B", b)
    assert "B" not in relay.channels


async def test_end_to_end_forwarding_and_auth() -> None:
    relay = Relay("secret")
    server = await start(relay, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    try:
        async with connect(f"{base}/sub/x?token=bad") as bad:
            with pytest.raises(ConnectionClosed) as exc:
                await bad.recv()
            assert exc.value.rcvd is not None and exc.value.rcvd.code == 4401

        async with (
            connect(f"{base}/sub/ch1?token=secret") as sub,
            connect(f"{base}/sub/ch2?token=secret") as other,
            connect(f"{base}/pub/ch1?token=secret") as pub,
        ):
            await pub.send("meta")
            await pub.send(frame(1, 0))
            await pub.send(frame(2, 1))
            got = []
            sidecars = []
            while len(got) < 3:
                msg = await asyncio.wait_for(sub.recv(), 2)
                if isinstance(msg, bytes) or msg == "meta":
                    got.append(msg)
                elif json.loads(msg)["type"] == "relay-frame":
                    sidecars.append(json.loads(msg))
            assert got == ["meta", frame(1, 0), frame(2, 1)]  # byte-for-byte, including envelope
            assert [s["seq"] for s in sidecars] == [0, 1]
            assert all(s["queue_ms"] >= 0 and s["dropped"] == 0 for s in sidecars)
            assert json.loads(await other.recv())["state"] == "waiting"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(other.recv(), 0.2)
    finally:
        server.close()
        await server.wait_closed()


async def receive_type(ws: ClientConnection, wanted: str) -> dict[str, object]:
    async with asyncio.timeout(3):
        while True:
            msg = await ws.recv()
            if isinstance(msg, str):
                parsed = json.loads(msg)
                if parsed.get("type") == wanted:
                    return dict(parsed)


async def receive_binary(ws: ClientConnection) -> bytes:
    async with asyncio.timeout(4):
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                return msg


async def test_publisher_disconnect_clears_cache_and_notifies_existing_viewer() -> None:
    relay = Relay("t")
    server = await start(relay, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    base = f"ws://127.0.0.1:{port}"
    try:
        async with connect(f"{base}/sub/x?token=t") as sub:
            assert (await receive_type(sub, "relay-status"))["state"] == "waiting"
            async with connect(f"{base}/pub/x?token=t") as pub:
                assert (await receive_type(sub, "relay-status"))["state"] == "connected"
                await pub.send(frame(1, 1))
                await pub.send(frame(2, 2))
                assert await receive_binary(sub) == frame(1, 1)
                assert await receive_binary(sub) == frame(2, 2)
            assert (await receive_type(sub, "relay-status"))["state"] == "disconnected"
            assert relay.channels["x"].config is None
            async with connect(f"{base}/pub/x?token=t") as pub:
                assert (await receive_type(sub, "relay-status"))["state"] == "connected"
                await pub.send(frame(3, 3))  # must not inherit the previous publisher's references
                await pub.send(frame(1, 4))
                await pub.send(frame(2, 5))
                assert await receive_binary(sub) == frame(1, 4)
                assert await receive_binary(sub) == frame(2, 5)
    finally:
        server.close()
        await server.wait_closed()


def test_late_viewer_metadata_not_evicted_by_clock_and_stats_updates() -> None:
    relay = Relay("t")
    relay.forward("x", '{"type":"session","width":720}')
    for i in range(30):
        relay.forward("x", json.dumps({"type": "clock", "n": i}))
        relay.forward("x", json.dumps({"type": "stats", "n": i}))
    sub = relay.add_sub("x")
    texts = [json.loads(m.message) for m in sub.queue.items if isinstance(m.message, str)]
    assert {"type": "session", "width": 720} in texts
    assert {"type": "clock", "n": 29} in texts
    assert len(texts) == 4  # relay-status + latest snapshot of each metadata type


def test_configuration_boundary_and_non_video_backlog_are_bounded() -> None:
    q = DropQueue(2)
    for kind, seq in [(1, 0), (2, 1), (3, 2), (1, 3), (3, 4), (2, 5)]:
        q.push(frame(kind, seq))
    assert [m.message for m in q.items] == [frame(1, 3), frame(2, 5)]
    for _ in range(1000):
        q.push("not a lossy control event")
    assert q.overflow and len(q.items) == 0


@pytest.fixture
def json_logs(caplog: pytest.LogCaptureFixture) -> Iterator[StringIO]:
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("relay"))
    caplog.set_level(logging.INFO, logger="pulse_relay")
    app.log.addHandler(handler)
    try:
        yield output
    finally:
        app.log.removeHandler(handler)
        handler.close()


def log_entries(output: StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def stub_connection(path: str, trace_header: str | None = None) -> MagicMock:
    ws = MagicMock(spec=ServerConnection)
    ws.id = new_trace_id()
    ws.close_code = 1000
    headers = Headers({"X-Trace-ID": trace_header}) if trace_header is not None else Headers()
    ws.request = Request(path, headers)
    ws.__aiter__.return_value = []
    return ws


@pytest.mark.parametrize(
    ("header", "query", "expected"),
    [
        ("A" * 32, "b" * 32, "a" * 32),
        (None, "B" * 32, "b" * 32),
        ("invalid", "c" * 32, "c" * 32),
        ("0" * 32, "d" * 32, "d" * 32),
        (None, None, None),
        ("bad", "0" * 32, None),
    ],
)
async def test_handler_trace_scope_and_restore(
    header: str | None, query: str | None, expected: str | None, json_logs: StringIO
) -> None:
    relay = Relay("secret")
    path = "/pub/device-like-name?token=secret"
    if query is not None:
        path += f"&trace_id={query}"
    ws = stub_connection(path, header)
    original_headers = list(ws.request.headers.raw_items())
    with bind_context(
        trace_id="e" * 32,
        run_id="unrelated-run",
        serial="unrelated-device",
        session_id="unrelated-session",
        channel="unrelated-channel",
    ):
        before = current_context()
        await relay.handler(ws)
        assert current_context() == before

    entries = log_entries(json_logs)
    assert [entry["event"] for entry in entries] == ["relay.connected", "relay.disconnected"]
    trace = entries[0]["trace_id"]
    if expected:
        assert trace == expected
    else:
        assert trace != "e" * 32
    assert new_trace_id(trace) == trace and int(trace, 16) != 0
    for entry in entries:
        assert entry["component"] == "relay"
        assert entry["trace_id"] == trace
        assert entry["channel"] == "device-like-name"
        assert entry["run_id"] is entry["serial"] is entry["session_id"] is None
        assert entry["data"]["connection_id"] == ws.id
    assert entries[-1]["data"]["metrics"] == {
        "scope": "connection",
        "received_messages": 0,
        "received_binary_bytes": 0,
    }
    assert entries[-1]["duration_ms"] >= 0
    assert list(ws.request.headers.raw_items()) == original_headers
    assert relay.channels == {}


async def test_rejection_metrics_do_not_allocate_channels(json_logs: StringIO) -> None:
    relay = Relay("server-credential")
    for path, code, reason in [
        ("/sub/x?token=wrong-credential", 4401, "unauthorized"),
        ("/sub/y", 4401, "unauthorized"),
        ("/unknown/z?token=server-credential", 4404, "unknown route"),
    ]:
        ws = stub_connection(path, "1" * 32)
        await relay.handler(ws)
        ws.close.assert_awaited_once_with(code, reason)

    metrics = relay.snapshot()
    assert metrics["connections"] == 3
    assert metrics["authentication_failures"] == 2
    assert metrics["validation_failures"] == 1
    assert metrics["received_messages"] == metrics["sent_messages"] == 0
    assert metrics["channels"] == {}
    entries = log_entries(json_logs)
    assert all(entry["event"] == "relay.rejected" for entry in entries)
    assert [entry["error_category"] for entry in entries] == [
        "authentication",
        "authentication",
        "validation",
    ]
    assert [entry["channel"] for entry in entries] == ["x", "y", None]
    assert all(entry["trace_id"] == "1" * 32 for entry in entries)
    assert "server-credential" not in json_logs.getvalue()
    assert "wrong-credential" not in json_logs.getvalue()


async def test_concurrent_connections_keep_context_and_replacement_metrics(
    json_logs: StringIO,
) -> None:
    relay = Relay("relay-credential")
    with bind_context(
        trace_id="f" * 32, run_id="parent-run", serial="parent-device", session_id="parent-session"
    ):
        before = current_context()
        server = await start(relay, "127.0.0.1", 0)
        port = next(iter(server.sockets)).getsockname()[1]
        base = f"ws://127.0.0.1:{port}"
        try:
            async with (
                connect(f"{base}/sub/x?token=relay-credential&trace_id={'1' * 32}") as sub,
                connect(f"{base}/sub/x?token=relay-credential&trace_id={'2' * 32}") as same,
                connect(f"{base}/sub/y?token=relay-credential&trace_id={'3' * 32}") as other,
            ):
                assert (await receive_type(sub, "relay-status"))["state"] == "waiting"
                assert (await receive_type(same, "relay-status"))["state"] == "waiting"
                assert (await receive_type(other, "relay-status"))["state"] == "waiting"
                async with connect(
                    f"{base}/pub/x?token=relay-credential&trace_id={'9' * 32}",
                    additional_headers={"X-Trace-ID": "4" * 32},
                ) as old:
                    assert (await receive_type(sub, "relay-status"))["state"] == "connected"
                    assert (await receive_type(same, "relay-status"))["state"] == "connected"
                    async with connect(
                        f"{base}/pub/x?token=relay-credential",
                        additional_headers={"X-Trace-ID": "5" * 32},
                    ) as publisher:
                        with pytest.raises(ConnectionClosed) as exc:
                            await old.recv()
                        assert exc.value.rcvd is not None and exc.value.rcvd.code == 4409
                        await publisher.send(frame(2, 7))
                        assert await receive_binary(sub) == frame(2, 7)
                        assert await receive_binary(same) == frame(2, 7)
                        snapshot = relay.snapshot()
                        assert snapshot["connections"] == 5
                        assert snapshot["active_publishers"] == 1
                        assert snapshot["active_subscribers"] == 3
                        assert snapshot["received_messages"] == 1
                        assert snapshot["received_binary_bytes"] == len(frame(2, 7))
                        assert snapshot["publisher_replacements"] == 1
                        assert relay.channels["x"].publisher_replacements == 1
                        assert relay.channels["y"].received_messages == 0
        finally:
            server.close()
            await server.wait_closed()
        assert current_context() == before

    entries = log_entries(json_logs)
    connected = {
        entry["data"]["connection_id"]: entry
        for entry in entries
        if entry["event"] == "relay.connected"
    }
    disconnected = [entry for entry in entries if entry["event"] == "relay.disconnected"]
    assert len(connected) == len(disconnected) == 5
    assert {entry["trace_id"] for entry in connected.values()} == {n * 32 for n in "12345"}
    assert (
        len(
            {
                entry["data"]["subscriber_id"]
                for entry in connected.values()
                if entry["data"]["role"] == "sub"
            }
        )
        == 3
    )
    for entry in entries:
        original = connected[entry["data"]["connection_id"]]
        assert entry["trace_id"] == original["trace_id"]
        assert entry["channel"] == ("y" if entry["trace_id"] == "3" * 32 else "x")
        assert entry["run_id"] is entry["serial"] is entry["session_id"] is None
        assert entry["event"] != "relay.error"
    replacement = next(entry for entry in entries if entry["event"] == "relay.replaced")
    assert connected[replacement["data"]["replaced_connection_id"]]["trace_id"] == "4" * 32
    assert relay.snapshot()["channels"] == {}
    assert relay.snapshot()["active_subscribers"] == relay.snapshot()["active_publishers"] == 0
    sub_totals = [
        entry["data"]["metrics"] for entry in disconnected if entry["data"]["role"] == "sub"
    ]
    assert relay.sent_messages == sum(metrics["sent_messages"] for metrics in sub_totals)
    assert relay.queue_ms.count == sum(metrics["queue_ms"]["count"] for metrics in sub_totals) == 2
    assert "relay-credential" not in json_logs.getvalue()


def test_metrics_keep_process_totals_but_gc_channel_history() -> None:
    relay = Relay("t", max_frames=1)
    first = relay.add_sub("x")
    for kind, seq in [(3, 0), (2, 1), (3, 2)]:
        relay.forward("x", frame(kind, seq))
    relay.forward("x", "metadata")
    late = relay.add_sub("x")
    relay.forward("x", frame(3, 3))
    before = relay.snapshot()
    assert before["received_messages"] == 5
    assert before["received_binary_bytes"] == 4 * len(frame(2, 1))
    assert before["sent_messages"] == 0
    assert before["dropped"] == first.queue.dropped + late.queue.dropped == 5
    assert before["queued_messages"] == 4
    assert first.snapshot()["scope"] == "subscriber"

    relay.remove_sub("x", first)
    relay.remove_sub("x", first)  # retirement is idempotent
    assert relay.channels["x"].snapshot()["dropped"] == relay.snapshot()["dropped"] == 5
    relay.remove_sub("x", late)
    assert relay.snapshot()["channels"] == {}
    assert relay.snapshot()["dropped"] == 5
    assert relay.snapshot()["queued_messages"] == 0
    assert before["queued_messages"] == 4  # snapshots don't retain mutable queue objects

    for name in ["x", *(f"channel-{i}" for i in range(100))]:
        sub = relay.add_sub(name)
        assert relay.channels[name].received_messages == 0
        assert relay.channels[name].snapshot()["dropped"] == 0
        relay.forward(name, frame(3, 0))
        relay.remove_sub(name, sub)
    assert relay.channels == {}
    assert relay.snapshot()["dropped"] == 106
    assert relay.snapshot()["received_messages"] == 106
    assert relay.snapshot()["received_binary_bytes"] == 105 * len(frame(3, 0))


async def test_queue_ms_matches_successful_sidecars_and_send_counts(
    monkeypatch: pytest.MonkeyPatch, json_logs: StringIO
) -> None:
    relay = Relay("t")
    sub = relay.add_sub("x")
    channel = relay.channels["x"]
    sub.push("metadata")
    sub.push(Delivery(frame(1, 0), recv_ms=1000.0, recv_mono=12.0))
    sub.push(Delivery(frame(2, 1), recv_ms=1100.0, recv_mono=12.25))
    sub.push(b"x")  # too short for a sidecar, still a successfully sent binary message
    monkeypatch.setattr(relay, "add_sub", lambda name: sub)
    # Patch the module reference, not asyncio's underlying monotonic clock.
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 12.5, time=lambda: 123.456))
    ws = stub_connection("/sub/x?token=t", "a" * 32)
    finished = asyncio.Event()
    send_contexts = []

    async def incoming() -> AsyncIterator[str]:
        await finished.wait()
        yield "ignored subscriber input"

    async def send(msg: str | bytes) -> None:
        send_contexts.append(current_context())
        if msg == b"x":
            finished.set()

    ws.__aiter__.side_effect = incoming
    ws.send.side_effect = send
    await asyncio.wait_for(relay.handler(ws), 2)
    sent = [call.args[0] for call in ws.send.await_args_list]
    sidecars = [json.loads(msg) for msg in sent if isinstance(msg, str) and "relay-frame" in msg]
    assert [sidecar["queue_ms"] for sidecar in sidecars] == [500.0, 250.0]
    assert [sidecar["recv_ms"] for sidecar in sidecars] == [1000.0, 1100.0]
    assert all(sidecar["send_ms"] == 123456.0 for sidecar in sidecars)
    assert [msg for msg in sent if isinstance(msg, bytes)] == [frame(1, 0), frame(2, 1), b"x"]
    for stats in (relay, channel, sub):
        assert stats.sent_messages == len(sent) == 7
        assert stats.queue_ms.snapshot() == {
            "count": 2,
            "total_ms": 750.0,
            "max_ms": 500.0,
            "last_ms": 250.0,
        }
    assert relay.received_messages == 0  # subscriber input and replay aren't publisher ingress
    assert relay.channels == {}
    assert all(
        context
        == {
            "trace_id": "a" * 32,
            "run_id": None,
            "serial": None,
            "session_id": None,
            "channel": "x",
        }
        for context in send_contexts
    )
    entries = log_entries(json_logs)
    assert [entry["event"] for entry in entries] == ["relay.connected", "relay.disconnected"]
    assert entries[-1]["data"]["metrics"] == sub.snapshot()
    assert entries[-1]["trace_id"] == "a" * 32


@pytest.mark.parametrize(
    ("error_type", "category"),
    [(TimeoutError, "timeout"), (OSError, "transport"), (RuntimeError, "internal")],
)
@pytest.mark.parametrize("successful_sends", [1, 2])
async def test_send_errors_keep_stack_and_count_only_successful_messages(
    error_type: type[Exception],
    category: str,
    successful_sends: int,
    monkeypatch: pytest.MonkeyPatch,
    json_logs: StringIO,
    caplog: pytest.LogCaptureFixture,
) -> None:
    relay = Relay("t")
    sub = relay.add_sub("x")
    sub.push(frame(2, 1))
    monkeypatch.setattr(relay, "add_sub", lambda name: sub)
    ws = stub_connection("/sub/x?token=t", "b" * 32)

    async def incoming() -> AsyncIterator[str]:
        await asyncio.Event().wait()
        yield "unreachable"

    ws.__aiter__.side_effect = incoming
    ws.send.side_effect = [None] * successful_sends + [
        error_type("send failed token=private-credential")
    ]
    with bind_context(trace_id="f" * 32, run_id="outer-run"):
        before = current_context()
        if error_type is TimeoutError:
            await asyncio.wait_for(relay.handler(ws), 2)
        else:
            with pytest.raises(error_type, match="send failed"):
                await asyncio.wait_for(relay.handler(ws), 2)
        assert current_context() == before

    assert sub.sent_messages == relay.sent_messages == successful_sends
    assert relay.queue_ms.count == sub.queue_ms.count == successful_sends - 1
    assert relay.slow_subscribers == int(error_type is TimeoutError)
    assert relay.channels == {}
    entries = log_entries(json_logs)
    failure = next(entry for entry in entries if entry["error_category"] == category)
    assert failure["trace_id"] == "b" * 32
    assert failure["exception"]["type"] == error_type.__name__
    assert "Traceback" in failure["exception"]["stack"]
    assert "private-credential" not in json_logs.getvalue()
    assert "[REDACTED]" in failure["exception"]["message"]
    assert any(
        record.exc_info is not None for record in caplog.records if record.name == "pulse_relay"
    )
    final = next(entry for entry in entries if entry["event"] == "relay.disconnected")
    assert final["data"]["metrics"] == sub.snapshot()
    assert final["run_id"] is final["serial"] is final["session_id"] is None
    if error_type is not TimeoutError:
        raw = next(
            record for record in caplog.records if getattr(record, "event", None) == "relay.error"
        )
        assert "private-credential" not in raw.__dict__["error"]


async def test_backlog_limit_logs_once_and_preserves_close_code(
    monkeypatch: pytest.MonkeyPatch, json_logs: StringIO
) -> None:
    relay = Relay("t")
    sub = relay.add_sub("x")
    channel = relay.channels["x"]
    for _ in range(300):
        relay.forward("x", "non-lossy metadata")
    monkeypatch.setattr(relay, "add_sub", lambda name: sub)
    ws = stub_connection("/sub/x?token=t")

    async def incoming() -> AsyncIterator[str]:
        await asyncio.Event().wait()
        yield "unreachable"

    ws.__aiter__.side_effect = incoming
    await asyncio.wait_for(relay.handler(ws), 2)
    ws.close.assert_any_await(1013, "subscriber backlog limit")
    assert relay.slow_subscribers == channel.slow_subscribers == 1
    assert relay.received_messages == 300 and relay.sent_messages == 0
    assert relay.channels == {}
    entries = log_entries(json_logs)
    assert [entry["event"] for entry in entries] == [
        "relay.connected",
        "relay.slow_subscriber",
        "relay.disconnected",
    ]
    assert entries[1]["data"]["reason"] == "backlog_limit"
    assert entries[-1]["data"]["metrics"]["slow"] is True
    assert entries[-1]["data"]["metrics"]["queued_messages"] == 0


@pytest.mark.parametrize("log_dir_source", ["stderr", "env", "cli"])
def test_main_owns_metrics_task_logging_setup_and_shutdown(
    log_dir_source: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, json_logs: StringIO
) -> None:
    monkeypatch.delenv("PULSE_RELAY_LOG_DIR", raising=False)
    monkeypatch.setenv("PULSE_METRICS_INTERVAL_S", "7")
    arguments = ["--token", "t"]
    expected_directory = None
    if log_dir_source in {"env", "cli"}:
        expected_directory = tmp_path / "env"
        monkeypatch.setenv("PULSE_RELAY_LOG_DIR", str(expected_directory))
    if log_dir_source == "cli":
        expected_directory = tmp_path / "cli"
        arguments += ["--log-dir", str(expected_directory)]

    configure = Mock()
    monkeypatch.setattr(app, "configure_logging", configure)
    sleeping = asyncio.Event()
    cancelled = asyncio.Event()
    intervals = []
    server = MagicMock(spec=Server)

    async def metric_sleep(interval: float) -> None:
        intervals.append(interval)
        if len(intervals) == 1:
            return
        sleeping.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def serve_forever() -> None:
        await asyncio.wait_for(sleeping.wait(), 2)
        if log_dir_source == "cli":
            raise RuntimeError("server failed")

    async def wait_closed() -> None:
        assert cancelled.is_set()
        server.close.assert_called_once_with()
        assert len(asyncio.all_tasks()) == 1

    server.serve_forever.side_effect = serve_forever
    server.wait_closed.side_effect = wait_closed
    monkeypatch.setattr(app, "start", AsyncMock(return_value=server))
    monkeypatch.setattr(app.asyncio, "sleep", metric_sleep)
    with bind_context(trace_id="f" * 32, run_id="parent-run", channel="parent-channel"):
        if log_dir_source == "cli":
            with pytest.raises(RuntimeError, match="server failed"):
                app.main(arguments)
        else:
            assert app.main(arguments) == 0

    configure.assert_called_once_with("relay", directory=expected_directory)
    server.wait_closed.assert_awaited_once_with()
    assert intervals == [7.0, 7.0]
    entries = log_entries(json_logs)
    assert [entry["event"] for entry in entries] == [
        "relay.listening",
        "relay.metrics",
        "relay.metrics",
    ]
    assert entries[-1]["data"]["reason"] == "shutdown"
    assert entries[-1]["data"]["metrics"]["scope"] == "process"
    assert entries[-1]["data"]["metrics"]["channels"] == {}
    assert all(
        entry["trace_id"] is entry["run_id"] is entry["channel"] is None for entry in entries
    )
