import asyncio
import io
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from pulse_observability import JsonFormatter, bind_context, current_context
from pulse_runtime.api import app as app_module
from pulse_runtime.api.app import create_app
from pulse_runtime.api.telemetry import ApiMetrics, RequestTelemetry
from pulse_runtime.drivers.android import agent_transport


@pytest.fixture
def logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("runtime"))
    logger = logging.getLogger("pulse_runtime")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        handler.close()


async def test_concurrent_asgi_requests_and_child_tasks_keep_separate_traces(
    logs: io.StringIO,
) -> None:
    entered = [asyncio.Event(), asyncio.Event()]
    observed: list[dict[str, str | None]] = []
    logger = logging.getLogger("pulse_runtime.request_test")

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        index = int(scope["path"][-1])
        scope["route"] = SimpleNamespace(path="/test/{serial}")
        scope["path_params"] = {"serial": f"S{index}"}
        entered[index].set()
        await entered[1 - index].wait()

        async def child() -> None:
            await asyncio.sleep(0)
            observed.append(current_context())
            logger.info("Background operation", extra={"event": "test.child"})

        with bind_context(serial=f"S{index}"):
            await asyncio.create_task(child())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    metrics = ApiMetrics()
    middleware = RequestTelemetry(app, metrics)
    responses: list[list[Message]] = [[], []]

    async def request(index: int) -> None:
        async def send(message: Message) -> None:
            responses[index].append(message)

        async def receive() -> Message:
            return {"type": "http.request", "body": b"private phone content"}

        scope: Scope = {
            "type": "http",
            "method": "GET",
            "path": f"/test/{index}",
            "headers": [
                (b"x-trace-id", str(index + 1).encode() * 32),
                (b"authorization", b"Bearer hidden-token"),
            ],
            "query_string": b"token=hidden-token",
        }
        with bind_context(
            trace_id="f" * 32, run_id="unrelated", session_id="old-session", channel="old-channel"
        ):
            await middleware(scope, receive, send)
            assert current_context()["session_id"] == "old-session"

    await asyncio.gather(request(0), request(1))
    assert current_context() == {}
    assert metrics.http_requests_total == 2 and metrics.http_active == 0
    assert metrics.http_duration.count == 2
    for index in range(2):
        assert dict(responses[index][0]["headers"])[b"x-trace-id"] == str(index + 1).encode() * 32
        context = next(value for value in observed if value["serial"] == f"S{index}")
        assert context["trace_id"] == str(index + 1) * 32
        assert (
            context["run_id"] is None
            and context["session_id"] is None
            and context["channel"] is None
        )
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert len(entries) == 4
    assert "hidden-token" not in logs.getvalue() and "private phone content" not in logs.getvalue()
    assert all(entry["serial"] == f"S{int(entry['trace_id'][0]) - 1}" for entry in entries)


def test_http_ws_traces_rejections_exceptions_and_metrics(logs: io.StringIO) -> None:
    app = create_app("hidden-token")

    @app.get("/test/error")
    async def fail() -> None:
        raise ValueError("failed wss://localhost/ws?ToKeN=hidden-token")

    with TestClient(app, raise_server_exceptions=False) as client:
        rejected = client.get(
            "/health?token=hidden-token", headers={"Origin": "http://localhost:5173"}
        )
        assert rejected.status_code == 401
        assert rejected.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert len(rejected.headers["x-trace-id"]) == 32
        auth = {"Authorization": "Bearer hidden-token", "X-Trace-ID": "a" * 32}
        response = client.get("/test/error", headers=auth)
        assert response.status_code == 500
        assert response.headers["x-trace-id"] == "a" * 32
        assert response.text == "Internal Server Error"
        with client.websocket_connect(
            "/ws/heartbeat?token=hidden-token&trace_id=" + "b" * 32
        ) as ws:
            assert ws.receive_json()["type"] == "heartbeat"
            assert dict(ws.extra_headers)[b"x-trace-id"] == b"b" * 32
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/ws/heartbeat?token=wrong&trace_id=malformed"),
        ):
            pass
        snapshot = client.get("/debug/stream", headers=auth).json()["api"]
        assert snapshot["http_requests_total"] == 3
        assert snapshot["http_errors_total"] == 2
        assert snapshot["ws_connections_total"] == 2
        assert snapshot["ws_rejected_total"] == 1 and snapshot["ws_active"] == 0
        assert snapshot["http_duration_ms"]["count"] == 2
    raw = logs.getvalue()
    assert "hidden-token" not in raw
    entries = [json.loads(line) for line in raw.splitlines()]
    failed = next(entry for entry in entries if entry["exception"] is not None)
    assert failed["trace_id"] == "a" * 32 and failed["error_category"] == "internal"
    assert failed["exception"]["type"] == "ValueError"
    assert "Traceback" in failed["exception"]["stack"]
    accepted = next(entry for entry in entries if entry["event"] == "ws.opened")
    closed = next(entry for entry in entries if entry["event"] == "ws.closed")
    assert accepted["trace_id"] == closed["trace_id"] == "b" * 32
    assert closed["duration_ms"] >= 0
    denied = next(entry for entry in entries if entry["event"] == "ws.rejected")
    assert len(denied["trace_id"]) == 32 and denied["trace_id"] != "malformed"
    assert denied["error_category"] == "authentication"
    assert all(entry["run_id"] is None for entry in entries)


async def test_request_failure_after_response_start_counts_as_error(logs: io.StringIO) -> None:
    async def broken(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200})
        raise TimeoutError("Bearer hidden-token")

    async def send(message: Message) -> None:
        pass

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    metrics = ApiMetrics()
    with pytest.raises(TimeoutError):
        await RequestTelemetry(broken, metrics)(
            {"type": "http", "method": "GET", "headers": []}, receive, send
        )
    assert metrics.http_errors_total == 1 and metrics.http_active == 0
    value = json.loads(logs.getvalue())
    assert value["error_category"] == "timeout" and value["data"]["status_code"] == 200
    assert "hidden-token" not in logs.getvalue()


def test_default_500_response_returns_generated_trace(logs: io.StringIO) -> None:
    app = create_app("test")

    @app.get("/test/error")
    async def fail() -> None:
        raise RuntimeError("simulated failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/error", headers={"Authorization": "Bearer test"})
    assert response.status_code == 500
    trace = response.headers["x-trace-id"]
    assert len(trace) == 32
    failure = next(
        json.loads(line)
        for line in logs.getvalue().splitlines()
        if '"event":"http.completed"' in line
    )
    assert failure["trace_id"] == trace


def test_adb_discovery_stderr_is_captured_classified_and_redacted(
    logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = SimpleNamespace(
        returncode=1, communicate=AsyncMock(return_value=(b"", b"Bearer hidden-token"))
    )
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(agent_transport, "find_adb", lambda: "fake-adb")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with TestClient(create_app("test")) as client:
        response = client.get("/devices", headers={"Authorization": "Bearer test"})
    assert response.json()["devices"] == []
    assert "hidden-token" not in response.text and "hidden-token" not in logs.getvalue()
    assert launch.call_args.kwargs["stderr"] == asyncio.subprocess.PIPE
    entries = [json.loads(line) for line in logs.getvalue().splitlines()]
    failure = next(entry for entry in entries if entry["event"] == "devices.failed")
    assert failure["error_category"] == "device" and failure["exception"] is None
    assert response.json()["error"] == "adb_unavailable"


async def test_periodic_metrics_are_process_scoped_and_stopped_with_lifespan(
    logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "metrics_interval", lambda: 0.01)
    observed = asyncio.Event()

    class Notify(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "event", None) == "runtime.metrics":
                observed.set()

    logger = logging.getLogger("pulse_runtime")
    handler = Notify()
    logger.addHandler(handler)
    app = create_app("test")
    try:
        with bind_context(trace_id="a" * 32, run_id="not-an-engine-run", serial="not-a-capture"):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(observed.wait(), 2)
        entries = [json.loads(line) for line in logs.getvalue().splitlines()]
        assert len(entries) == 2  # one periodic snapshot, one final snapshot
        assert all(entry["event"] == "runtime.metrics" for entry in entries)
        assert all(
            entry["trace_id"] is None and entry["run_id"] is None and entry["serial"] is None
            for entry in entries
        )
    finally:
        logger.removeHandler(handler)
