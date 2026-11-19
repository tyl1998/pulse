from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pulse_runtime.drivers.android import agent_observe as ao
from pulse_runtime.drivers.android import agent_transport as at

TOKEN = "a" * 64
PARAMS = {"tree": True, "screenshot": False, "ocr": False}
Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@pytest.fixture(autouse=True)
def no_physical_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = AsyncMock(side_effect=AssertionError("physical ADB forbidden"))
    monkeypatch.setattr(at, "adb", forbidden)
    monkeypatch.setattr(at, "host_output", forbidden)
    monkeypatch.setattr(at, "find_adb", lambda: "forbidden-adb")


class LoopbackBackend(at.AndroidBackend):
    def __init__(self, serial: str) -> None:
        super().__init__()
        self.serial = serial
        self.port = 0
        self.present = True
        self.verifications = 0
        self.verify_entered: asyncio.Event | None = None
        self.verify_release: asyncio.Event | None = None

    async def command(
        self,
        serial: str,
        *args: str,
        timeout: float = 5,  # noqa: ASYNC109
    ) -> str:
        assert serial == self.serial
        assert args == ("forward", "--list")
        self.verifications += 1
        if self.verify_entered is not None:
            self.verify_entered.set()
        if self.verify_release is not None:
            await self.verify_release.wait()
        return f"{serial} tcp:{self.port} {at.SOCKET}\n" if self.present else ""


@asynccontextmanager
async def fake_agent(handler: Handler, serial: str = "S1") -> AsyncIterator[at.ForwardLease]:
    tasks: list[asyncio.Task[None]] = []
    errors: list[Exception] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.append(task)
        try:
            await handler(reader, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    backend = LoopbackBackend(serial)
    backend.port = server.sockets[0].getsockname()[1]
    forward = at.ForwardLease(backend, serial, port=backend.port, owned=True)
    try:
        yield forward
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not errors


async def read_request(reader: asyncio.StreamReader) -> tuple[bytes, dict[str, object]]:
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith((b"POST /v1/observe HTTP/1.1\r\n", b"POST /v1/cancel HTTP/1.1\r\n"))
    headers = dict(line.split(b":", 1) for line in head.split(b"\r\n")[1:-2])
    assert headers[b"Content-Type"].strip() == b"application/json"
    assert b"Transfer-Encoding" not in headers and b"Expect" not in headers
    size = int(headers[b"Content-Length"])
    assert 1 <= size <= 65536
    raw = await reader.readexactly(size)
    body = json.loads(raw.decode("utf-8"))
    assert isinstance(body, dict)
    assert body["operation"] in {"observe", "cancel"}
    assert body["protocol_version"] == 1 and type(body["protocol_version"]) is int
    at.canonical_uuid(body["request_id"])
    return head, body


def terminal(request_id: str, session_id: str, operation: str = "observe") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "session_id": session_id,
        "operation": operation,
        "success": True,
        "terminal": True,
        "result": {"synthetic_source": "private-source"}
        if operation == "observe"
        else {"cancelled": True},
        "error": None,
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def response_headers(
    session_id: str | None,
    *,
    status: int = 200,
    content_type: str = "application/x-ndjson; charset=utf-8",
    content_length: int | str | None = None,
    extra: bytes = b"",
) -> bytes:
    head = (
        f"HTTP/1.1 {status} Status\r\nContent-Type: {content_type}\r\nConnection: close\r\n"
    ).encode("ascii")
    if session_id is not None:
        head += f"X-Pulse-Session: {session_id}\r\n".encode("ascii")
    if content_length is not None:
        head += f"Content-Length: {content_length}\r\n".encode("ascii")
    return head + extra + b"\r\n"


async def send(writer: asyncio.StreamWriter, data: bytes) -> None:
    for offset in range(0, len(data), 65536):
        writer.write(data[offset : offset + 65536])
        await writer.drain()


async def wait_session(exchange: ao.ObserveExchange) -> None:
    # This internal API exposes a state property, not a header-read notification.
    async with asyncio.timeout(1):
        while exchange.wire_session_id is None:  # noqa: ASYNC110
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "content_type", ["application/x-ndjson", "application/x-ndjson; charset=utf-8"]
)
async def test_success_requires_one_correlated_terminal_and_eof_without_half_close(
    content_type: str,
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = ao.ObserveExchange(request_id)
    written, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await read_request(reader))
        disconnect = asyncio.create_task(reader.read(1))
        try:
            body = terminal(request_id, session_id)
            body["result"] = {"synthetic_source": "private-source", "text": "\u6d4b\u8bd5"}
            await send(
                writer,
                response_headers(session_id, content_type=content_type) + encoded(body) + b"\n",
            )
            written.set()
            await release.wait()
            assert not disconnect.done(), "the client half-closed before the server's EOF"
            writer.close()
            await disconnect
        finally:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    async with fake_agent(serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 1000))
        await written.wait()
        done, _pending = await asyncio.wait({task}, timeout=0.03)
        assert not done
        assert exchange.terminal_received and not exchange.eof_received
        release.set()
        reply = await asyncio.wait_for(task, 1)
        assert forward.owned and forward.port is not None
        assert forward.backend.verifications == 0
    assert reply == ao.ObserveReply(session_id, True, reply.result, None)
    assert reply.result == {"synthetic_source": "private-source", "text": "\u6d4b\u8bd5"}
    assert "private-source" not in repr(reply) + repr(exchange.cleanup)
    with pytest.raises(FrozenInstanceError):
        reply.success = False
    assert exchange.sent and exchange.terminal_received and exchange.eof_received
    assert exchange.cleanup == {
        "cancel_acknowledged": None,
        "cancel_code": None,
        "connection_closed": True,
    }
    head, body = requests[0]
    assert b"Authorization: Bearer " + TOKEN.encode() + b"\r\n" in head
    assert body == {
        "protocol_version": 1,
        "request_id": request_id,
        "operation": "observe",
        "timeout_ms": 1000,
        "params": PARAMS,
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("protocol_version", True, "wire_incompatible"),
        ("protocol_version", 2, "wire_incompatible"),
        ("request_id", str(uuid4()), "invalid_message"),
        ("request_id", None, "invalid_message"),
        ("session_id", str(uuid4()), "invalid_message"),
        ("session_id", "123E4567-E89B-12D3-A456-426614174000", "invalid_message"),
        ("operation", "health", "invalid_message"),
        ("operation", "action", "invalid_message"),
        ("operation", "cancel", "invalid_message"),
        ("terminal", False, "invalid_message"),
        ("terminal", 1, "invalid_message"),
        ("success", 1, "invalid_message"),
        ("success", None, "invalid_message"),
        ("result", None, "invalid_message"),
        ("result", [], "invalid_message"),
        ("error", {"code": "busy", "phase": "queue", "retryable": False}, "invalid_message"),
    ],
)
async def test_invalid_terminal_fields_fail_closed(field: str, value: object, code: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = ao.ObserveExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        body[field] = value
        await send(writer, response_headers(session_id) + encoded(body) + b"\n")

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=f"^{code}$"):
            await exchange.run(forward, TOKEN, PARAMS, 100)
    assert not exchange.terminal_received
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize(
    "case",
    [
        "non_object",
        "invalid_utf8",
        "utf16",
        "bom",
        "duplicate_top",
        "duplicate_nested",
        "nan",
        "infinity",
        "float_overflow",
        "deep_json",
        "missing_field",
        "trailing_document",
        "error_not_object",
        "error_code_not_string",
    ],
)
async def test_strict_json_never_exposes_invalid_source_or_error_data(case: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        raw = encoded(body)
        if case == "non_object":
            raw = b"[]"
        elif case == "invalid_utf8":
            raw = raw.replace(b"private-source", b"\xff")
        elif case == "utf16":
            raw = raw.decode().encode("utf-16")
        elif case == "bom":
            raw = b"\xef\xbb\xbf" + raw
        elif case == "duplicate_top":
            raw = b'{"terminal":true,' + raw[1:]
        elif case == "duplicate_nested":
            raw = raw.replace(b'"private-source"', b'1,"synthetic_source":2')
        elif case in {"nan", "infinity", "float_overflow", "deep_json"}:
            replacement = {
                "nan": b"NaN",
                "infinity": b"Infinity",
                "float_overflow": b"1e10000",
                "deep_json": b"[" * 100000 + b"0" + b"]" * 100000,
            }[case]
            raw = raw.replace(b'"private-source"', replacement)
        elif case == "missing_field":
            del body["error"]
            raw = encoded(body)
        elif case == "trailing_document":
            raw += b"{}"
        else:
            body.update(success=False, result=None)
            body["error"] = (
                []
                if case == "error_not_object"
                else {"code": 1, "phase": "observe", "retryable": False}
            )
            raw = encoded(body)
        await send(writer, response_headers(session_id) + raw + b"\n")

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=r"^invalid_message$") as caught:
            await exchange.run(forward, TOKEN, PARAMS, 100)
    assert "private-source" not in str(caught.value) + repr(exchange.cleanup)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "case",
    [
        "no_terminal",
        "capabilities_only",
        "truncated_json",
        "missing_lf",
        "empty_line",
        "accepted",
        "duplicate",
        "trailing",
        "extra_lf",
        "crlf",
    ],
)
async def test_line_framing_requires_only_one_complete_terminal(case: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = ao.ObserveExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        raw = encoded(terminal(request_id, session_id))
        data = {
            "no_terminal": b"",
            "capabilities_only": b'{"capabilities":{"tree":"available"}}\n',
            "truncated_json": raw[:-1] + b"\n",
            "missing_lf": raw,
            "empty_line": b"\n",
            "accepted": encoded({**terminal(request_id, session_id), "terminal": False})
            + b"\n"
            + raw
            + b"\n",
            "duplicate": raw + b"\n" + raw + b"\n",
            "trailing": raw + b"\nprivate-source",
            "extra_lf": raw + b"\n\n",
            "crlf": raw + b"\r\n",
        }[case]
        await send(writer, response_headers(session_id) + data)

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
            await exchange.run(forward, TOKEN, PARAMS, 100)
    assert exchange.cleanup["connection_closed"] is True
    if case in {"no_terminal", "missing_lf"}:
        assert exchange.eof_received and not exchange.terminal_received


@pytest.mark.parametrize(
    "case",
    [
        "missing_session",
        "invalid_session",
        "duplicate_session",
        "duplicate_type",
        "duplicate_unknown",
        "bad_name",
        "folded",
        "non_ascii",
        "bare_lf",
        "wrong_version",
        "wrong_status",
        "content_length",
        "chunked",
        "expect",
        "wrong_type",
        "wrong_charset",
        "keep_alive",
        "oversize",
        "truncated",
    ],
)
async def test_bounded_strict_http_headers(case: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        head = response_headers(session_id)
        additions = {
            "duplicate_session": f"x-pulse-session: {session_id}\r\n".encode(),
            "duplicate_type": b"content-type: application/x-ndjson\r\n",
            "duplicate_unknown": b"X-Test: 1\r\nx-test: 2\r\n",
            "bad_name": b"Bad Name: value\r\n",
            "folded": b" continuation\r\n",
            "non_ascii": b"X-Test: \xff\r\n",
            "bare_lf": b"X-Test: one\ntwo\r\n",
            "content_length": b"Content-Length: 0\r\n",
            "chunked": b"Transfer-Encoding: chunked\r\n",
            "expect": b"Expect: 100-continue\r\n",
            "oversize": b"X-Pad: " + b"x" * 8192 + b"\r\n",
        }
        if case in additions:
            head = response_headers(session_id, extra=additions[case])
        elif case == "missing_session":
            head = response_headers(None)
        elif case == "invalid_session":
            head = response_headers("not-a-uuid")
        elif case == "wrong_version":
            head = head.replace(b"HTTP/1.1", b"HTTP/1.0")
        elif case == "wrong_status":
            head = head.replace(b"200 Status", b"True Status")
        elif case == "wrong_type":
            head = response_headers(session_id, content_type="application/json")
        elif case == "wrong_charset":
            head = response_headers(session_id, content_type="application/x-ndjson; charset=latin1")
        elif case == "keep_alive":
            head = head.replace(b"Connection: close", b"Connection: keep-alive")
        elif case == "truncated":
            head = head[:-2]
        await send(
            writer,
            head
            + (b"" if case == "truncated" else encoded(terminal(request_id, session_id)) + b"\n"),
        )

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        code = "response_too_large" if case == "oversize" else "invalid_message"
        with pytest.raises(at.ConnectionFault, match=f"^{code}$"):
            await exchange.run(forward, TOKEN, PARAMS, 100)
    assert exchange.cleanup["connection_closed"] is True


async def test_header_size_boundary_is_inclusive() -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        extra = b"".join(
            f"X-Pad-{index}: ".encode().ljust(2048, b"x") + b"\r\n" for index in range(3)
        )
        empty = response_headers(session_id, extra=extra + b"X-Pad: \r\n")
        head = response_headers(
            session_id, extra=extra + b"X-Pad: " + b"x" * (8192 - len(empty)) + b"\r\n"
        )
        assert len(head) == 8192
        assert all(len(line) <= 2048 for line in head[:-4].split(b"\r\n"))
        await send(writer, head + encoded(terminal(request_id, session_id)) + b"\n")

    async with fake_agent(serve) as forward:
        assert (await ao.ObserveExchange(request_id).run(forward, TOKEN, PARAMS, 100)).success


@pytest.mark.parametrize("header_count", [32, 33])
async def test_header_count_boundary_excludes_the_status_line(header_count: int) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        extra = b"".join(f"X-Test-{index}: value\r\n".encode() for index in range(header_count - 3))
        head = response_headers(session_id, extra=extra)
        assert len(head[:-4].split(b"\r\n")) - 1 == header_count
        assert len(head) < 8192
        await send(writer, head + encoded(terminal(request_id, session_id)) + b"\n")

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        if header_count == 32:
            assert (await exchange.run(forward, TOKEN, PARAMS, 100)).success
        else:
            with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
                await exchange.run(forward, TOKEN, PARAMS, 100)
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("line_kind", ["header", "status"])
@pytest.mark.parametrize("size", [2048, 2049])
async def test_single_http_line_size_boundary(line_kind: str, size: int) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        if line_kind == "status":
            _status, rest = response_headers(session_id).split(b"\r\n", 1)
            prefix = b"HTTP/1.1 200 "
            head = prefix + b"x" * (size - len(prefix)) + b"\r\n" + rest
        else:
            prefix = b"X-Pad: "
            head = response_headers(
                session_id, extra=prefix + b"x" * (size - len(prefix)) + b"\r\n"
            )
        assert max(len(line) for line in head[:-4].split(b"\r\n")) == size
        assert len(head) < 8192
        await send(writer, head + encoded(terminal(request_id, session_id)) + b"\n")

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        if size == 2048:
            assert (await exchange.run(forward, TOKEN, PARAMS, 100)).success
        else:
            with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
                await exchange.run(forward, TOKEN, PARAMS, 100)
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_event_size_limit_counts_utf8_bytes_and_allows_one_extra_lf(extra_bytes: int) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = ao.ObserveExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        body["result"] = {"padding": ""}
        raw = encoded(body)
        padding = 8388608 + extra_bytes - len(raw)
        raw = raw.replace(b'"padding":""', b'"padding":"' + b"x" * padding + b'"')
        assert len(raw) == 8388608 + extra_bytes
        await send(writer, response_headers(session_id) + raw + b"\n")

    async with fake_agent(serve) as forward:
        if extra_bytes:
            with pytest.raises(at.ConnectionFault, match=r"^response_too_large$"):
                await exchange.run(forward, TOKEN, PARAMS, 1000)
        else:
            reply = await exchange.run(forward, TOKEN, PARAMS, 1000)
            assert reply.success and exchange.terminal_received and exchange.eof_received
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth_errors_are_closed_without_parsing_error_output(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer, response_headers(None, status=status) + b"private-source " + TOKEN.encode()
        )

    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(serve) as forward:
        with caplog.at_level(logging.DEBUG), pytest.raises(at.ConnectionFault) as caught:
            await exchange.run(forward, TOKEN, PARAMS, 100)
    assert str(caught.value) == "authentication_failed"
    assert not exchange.terminal_received and exchange.cleanup["connection_closed"] is True
    assert TOKEN not in str(caught.value) + repr(exchange.cleanup) + caplog.text
    assert "private-source" not in str(caught.value) + repr(exchange.cleanup) + caplog.text


@pytest.mark.parametrize("error_code", ["busy", "timeout", "private-source"])
async def test_correlated_http_error_json_returns_only_safe_error_code(error_code: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        body.update(
            success=False,
            result=None,
            error={
                "code": error_code,
                "phase": "queue",
                "retryable": error_code == "timeout",
                "message": "private-source",
            },
        )
        raw = encoded(body)
        await send(
            writer,
            response_headers(
                session_id, status=503, content_type="application/json", content_length=len(raw)
            )
            + raw,
        )

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        reply = await exchange.run(forward, TOKEN, PARAMS, 100)
    assert not reply.success and reply.result is None
    assert reply.error_code == (
        error_code if error_code != "private-source" else "agent_observation_rejected"
    )
    assert "private-source" not in repr(reply) + repr(exchange.cleanup)
    assert exchange.terminal_received and exchange.eof_received


async def test_failed_ndjson_observation_preserves_repr_hidden_source_failures() -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    result = {"sources": {"tree": {"status": "unavailable", "data": None}}}

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        body.update(
            success=False,
            result=result,
            error={
                "code": "window_unavailable",
                "phase": "observe",
                "retryable": False,
                "message": "private-source",
            },
        )
        await send(writer, response_headers(session_id) + encoded(body) + b"\n")

    async with fake_agent(serve) as forward:
        reply = await ao.ObserveExchange(request_id).run(forward, TOKEN, PARAMS, 100)
    assert reply == ao.ObserveReply(session_id, False, result, "window_unavailable")
    assert "sources" not in repr(reply) and "private-source" not in repr(reply)


@pytest.mark.parametrize("mode", ["observe", "http_error", "cancel"])
@pytest.mark.parametrize(
    "error",
    [
        {"code": "busy"},
        {"code": "busy", "retryable": False},
        {"code": "busy", "phase": "", "retryable": False},
        {"code": "busy", "phase": ["queue"], "retryable": False},
        {"code": "busy", "phase": "private-source", "retryable": False},
        {"code": "busy", "phase": TOKEN * 64, "retryable": False},
        {"code": "busy", "phase": "queue"},
        {"code": "busy", "phase": "queue", "retryable": 0},
        {"code": "busy", "phase": "queue", "retryable": "false"},
    ],
)
async def test_failed_terminal_requires_pinned_error_shape(
    mode: str, error: dict[str, object]
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    disconnected = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _head, request = await read_request(reader)
        if mode == "cancel" and request["operation"] == "observe":
            await send(writer, response_headers(session_id))
            assert await reader.read(1) == b""
            disconnected.set()
            return
        response_session = str(uuid4()) if mode == "cancel" else session_id
        body = terminal(request["request_id"], response_session, request["operation"])
        body.update(
            success=False,
            result={"sources": {"tree": {"status": "unavailable", "data": None}}}
            if mode == "observe"
            else None,
            error=error,
        )
        raw = encoded(body)
        head = response_headers(
            response_session,
            status=503 if mode == "http_error" else 200,
            content_type="application/x-ndjson" if mode == "observe" else "application/json",
            content_length=None if mode == "observe" else len(raw),
        )
        await send(writer, head + raw + (b"\n" if mode == "observe" else b""))

    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        if mode == "cancel":
            task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
            await wait_session(exchange)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), 1)
            assert exchange.cleanup["cancel_acknowledged"] is None
            assert exchange.cleanup["cancel_code"] == "invalid_message"
        else:
            with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
                await exchange.run(forward, TOKEN, PARAMS, 100)
            assert not exchange.terminal_received
    assert exchange.cleanup["connection_closed"] is True
    assert "private-source" not in repr(exchange.cleanup) and TOKEN not in repr(exchange.cleanup)


@pytest.mark.parametrize(
    "case",
    [
        "error_result",
        "error_success",
        "uncorrelated",
        "truncated",
        "trailing",
        "missing_length",
        "duplicate_length",
        "oversize",
    ],
)
async def test_http_error_json_still_requires_correlated_framing_and_eof(case: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = terminal(request_id, session_id)
        body.update(
            success=False,
            result=None,
            error={
                "code": "busy",
                "phase": "queue",
                "retryable": False,
                "message": "private-source",
            },
        )
        if case == "error_result":
            body["result"] = {"raw": "private-source"}
        elif case == "error_success":
            body.update(success=True, result={}, error=None)
        elif case == "uncorrelated":
            body.update(request_id=None, session_id=None, operation=None)
        raw = encoded(body)
        size = None if case == "missing_length" else 65537 if case == "oversize" else len(raw)
        extra = b"Content-Length: 1\r\n" if case == "duplicate_length" else b""
        head = response_headers(
            session_id,
            status=500,
            content_type="application/json",
            content_length=size,
            extra=extra,
        )
        if case == "truncated":
            raw = raw[:-1]
        elif case == "trailing":
            raw += b"private-source"
        await send(writer, head + raw)

    async with fake_agent(serve) as forward:
        code = "response_too_large" if case == "oversize" else "invalid_message"
        with pytest.raises(at.ConnectionFault, match=f"^{code}$"):
            await ao.ObserveExchange(request_id).run(forward, TOKEN, PARAMS, 100)


@pytest.mark.parametrize(
    "request_id",
    [
        "bad",
        "123E4567-E89B-12D3-A456-426614174000",
        "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
        None,
    ],
)
def test_request_id_must_be_caller_canonical_uuid(request_id: object) -> None:
    with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
        ao.ObserveExchange(request_id)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("params", {"tree": False, "screenshot": False, "ocr": False}, "invalid_message"),
        ("params", {"tree": True}, "invalid_message"),
        ("params", {**PARAMS, "action": True}, "invalid_message"),
        ("params", {**PARAMS, "tree": 1}, "invalid_message"),
        ("timeout_ms", 99, "invalid_message"),
        ("timeout_ms", 10001, "invalid_message"),
        ("timeout_ms", True, "invalid_message"),
        ("token", "A" * 64, "credential_unavailable"),
        ("token", "a" * 63, "credential_unavailable"),
        ("token", TOKEN + "\r\n", "credential_unavailable"),
        ("port", None, "forward_unavailable"),
        ("port", True, "forward_unavailable"),
        ("port", 65536, "forward_unavailable"),
        ("owned", False, "forward_replaced"),
    ],
)
async def test_invalid_inputs_never_open_a_socket(
    field: str, value: object, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = AsyncMock(side_effect=AssertionError("unexpected socket"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    forward = at.ForwardLease(at.AndroidBackend(), "S1", port=1234, owned=True)
    kwargs = {"forward": forward, "token": TOKEN, "params": PARAMS, "timeout_ms": 100}
    if field in {"port", "owned"}:
        setattr(forward, field, value)
    else:
        kwargs[field] = value
    exchange = ao.ObserveExchange(str(uuid4()))
    with pytest.raises(at.ConnectionFault, match=f"^{code}$"):
        await exchange.run(**kwargs)
    connect.assert_not_awaited()
    assert not exchange.sent and exchange.cleanup["connection_closed"] is True


class PendingAgent:
    def __init__(self, token: str = TOKEN, case: str = "valid") -> None:
        self.token = token
        self.case = case
        self.session_id = str(uuid4())
        self.requests: list[tuple[bytes, dict[str, object]]] = []
        self.entered = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.cancel_entered = asyncio.Event()
        self.cancel_release: asyncio.Event | None = None
        self.send_session = True
        self.send_terminal = False

    @property
    def cancels(self) -> list[tuple[bytes, dict[str, object]]]:
        return [(head, body) for head, body in self.requests if body["operation"] == "cancel"]

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, body = await read_request(reader)
        assert b"Authorization: Bearer " + self.token.encode() + b"\r\n" in head
        self.requests.append((head, body))
        if body["operation"] == "observe":
            if self.send_session:
                await send(writer, response_headers(self.session_id))
            if self.send_terminal:
                await send(writer, encoded(terminal(body["request_id"], self.session_id)) + b"\n")
            self.entered.set()
            assert await reader.read(1) == b""
            self.disconnected.set()
            return
        self.cancel_entered.set()
        if self.cancel_release is not None:
            await self.cancel_release.wait()
        session_id = self.session_id if self.case == "original_session" else str(uuid4())
        reply = terminal(body["request_id"], session_id, "cancel")
        if self.case == "wrong_request":
            reply["request_id"] = str(uuid4())
        elif self.case == "wrong_session":
            reply["session_id"] = "not-a-uuid"
        elif self.case == "version_bool":
            reply["protocol_version"] = True
        elif self.case == "terminal_int":
            reply["terminal"] = 1
        elif self.case == "success_int":
            reply["success"] = 1
        elif self.case == "cancelled_int":
            reply["result"] = {"cancelled": 1}
        elif self.case == "cancelled_false":
            reply["result"] = {"cancelled": False}
        elif self.case == "unknown_error":
            reply.update(
                success=False,
                result=None,
                error={
                    "code": "private-source",
                    "phase": "protocol",
                    "retryable": False,
                    "message": self.token,
                },
            )
        raw = encoded(reply)
        if self.case == "duplicate_json":
            raw = raw.replace(b'"cancelled":true', b'"cancelled":true,"cancelled":false')
        elif self.case == "invalid_utf8":
            raw = b"\xff"
        size = len(raw)
        if self.case == "oversize":
            size = 65537
        extra = (
            f"x-pulse-session: {session_id}\r\n".encode()
            if self.case == "duplicate_header"
            else b""
        )
        status = int(self.case[-3:]) if self.case.startswith("auth_") else 200
        head = response_headers(
            session_id,
            status=status,
            content_type="application/json",
            content_length=None if self.case == "missing_length" else size,
            extra=extra,
        )
        if self.case == "truncated":
            raw = raw[:-1]
        elif self.case == "trailing":
            raw += b"private-source"
        await send(writer, head + raw)
        if self.case == "no_eof":
            assert await reader.read(1) == b""


@pytest.mark.parametrize("cancelled", [True, False])
async def test_cancel_uses_authenticated_original_targets_and_its_own_session(
    cancelled: bool,
) -> None:
    agent = PendingAgent(case="valid" if cancelled else "cancelled_false")
    request_id = str(uuid4())
    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
        assert forward.owned and forward.port is not None
        assert forward.backend.verifications == 1
    assert len(agent.cancels) == 1
    _head, body = agent.cancels[0]
    assert set(body) == {"protocol_version", "request_id", "operation", "timeout_ms", "params"}
    assert body["request_id"] != request_id
    assert body["params"] == {
        "target_session_id": agent.session_id,
        "target_request_id": request_id,
    }
    assert exchange.wire_session_id == agent.session_id
    assert exchange.cleanup["cancel_acknowledged"] is cancelled
    assert exchange.cleanup["cancel_code"] is None
    assert exchange.cleanup["connection_closed"] is True
    assert not exchange.terminal_received and "idle" not in exchange.cleanup


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("auth_401", "authentication_failed"),
        ("auth_403", "authentication_failed"),
        ("wrong_request", "invalid_message"),
        ("wrong_session", "invalid_message"),
        ("original_session", "invalid_message"),
        ("version_bool", "wire_incompatible"),
        ("terminal_int", "invalid_message"),
        ("success_int", "invalid_message"),
        ("cancelled_int", "invalid_message"),
        ("duplicate_json", "invalid_message"),
        ("duplicate_header", "invalid_message"),
        ("invalid_utf8", "invalid_message"),
        ("unknown_error", "agent_observation_rejected"),
        ("missing_length", "invalid_message"),
        ("oversize", "response_too_large"),
        ("truncated", "invalid_message"),
        ("trailing", "invalid_message"),
        ("no_eof", "timeout"),
    ],
)
async def test_bad_cancel_reply_never_acknowledges_or_retries(case: str, code: str) -> None:
    agent = PendingAgent(case=case)
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.get_running_loop().time() - started < 1.5
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == 1
    assert exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["cancel_code"] == code
    assert exchange.cleanup["connection_closed"] is True
    assert TOKEN not in repr(exchange.cleanup) and "private-source" not in repr(exchange.cleanup)


async def test_cancellation_before_session_header_only_disconnects() -> None:
    agent = PendingAgent()
    agent.send_session = False
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await agent.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
        assert forward.backend.verifications == 0
    assert exchange.sent and exchange.wire_session_id is None and not agent.cancels
    assert exchange.cleanup["cancel_acknowledged"] is None


@pytest.mark.parametrize("mode", ["before_headers", "missing_eof"])
async def test_absolute_observe_timeout_closes_socket_and_only_cancels_known_session(
    mode: str,
) -> None:
    agent = PendingAgent()
    agent.send_session = mode != "before_headers"
    agent.send_terminal = mode == "missing_eof"
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        started = asyncio.get_running_loop().time()
        with pytest.raises(at.ConnectionFault, match=r"^observation_timeout$"):
            await exchange.run(forward, TOKEN, PARAMS, 100)
        elapsed = asyncio.get_running_loop().time() - started
        assert 2 <= elapsed < 2.8
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == (1 if mode == "missing_eof" else 0)
    assert exchange.terminal_received == (mode == "missing_eof")
    assert not exchange.eof_received and exchange.cleanup["connection_closed"] is True


async def test_progress_does_not_extend_the_absolute_http_deadline() -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        for byte in b"HTTP/1.1 200 Status\r\n":
            await send(writer, bytes([byte]))
            await asyncio.sleep(0.2)
        assert await reader.read(1) == b""

    async with fake_agent(serve) as forward:
        started = asyncio.get_running_loop().time()
        with pytest.raises(at.ConnectionFault, match=r"^observation_timeout$"):
            await ao.ObserveExchange(str(uuid4())).run(forward, TOKEN, PARAMS, 100)
        assert asyncio.get_running_loop().time() - started < 2.8


async def test_repeated_cancel_finishes_one_control_request_and_owned_socket_cleanup() -> None:
    agent = PendingAgent()
    agent.cancel_release = asyncio.Event()
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        task.cancel()
        await asyncio.wait_for(agent.cancel_entered.wait(), 1)
        task.cancel()
        task.cancel()
        agent.cancel_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == 1
    assert exchange.cleanup["cancel_acknowledged"] is True
    assert exchange.cleanup["connection_closed"] is True


async def test_invalidate_immediately_closes_owned_socket_and_forbids_cancel() -> None:
    agent = PendingAgent()
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        exchange.invalidate()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
        assert forward.backend.verifications == 0
    assert not agent.cancels and exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True


async def test_invalidate_during_forward_verification_prevents_control_write() -> None:
    agent = PendingAgent()
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        backend = forward.backend
        backend.verify_entered, backend.verify_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        task.cancel()
        await asyncio.wait_for(backend.verify_entered.wait(), 1)
        exchange.invalidate()
        await asyncio.wait_for(agent.disconnected.wait(), 0.5)
        backend.verify_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not agent.cancels and exchange.cleanup["cancel_acknowledged"] is None


async def test_replaced_mapping_is_not_used_for_cancel() -> None:
    agent = PendingAgent()
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 10000))
        await wait_session(exchange)
        forward.backend.present = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert not agent.cancels and exchange.cleanup["cancel_code"] == "forward_replaced"


@pytest.mark.parametrize("replace_port", [False, True])
async def test_two_port_token_observations_and_cancellations_remain_isolated(
    replace_port: bool,
) -> None:
    first, second = PendingAgent("1" * 64), PendingAgent("2" * 64)
    first_id, second_id = str(uuid4()), str(uuid4())
    first_exchange, second_exchange = ao.ObserveExchange(first_id), ao.ObserveExchange(second_id)
    async with (
        fake_agent(first.serve, "S1") as first_forward,
        fake_agent(second.serve, "S2") as second_forward,
    ):
        first_task = asyncio.create_task(
            first_exchange.run(first_forward, first.token, PARAMS, 10000)
        )
        second_task = asyncio.create_task(
            second_exchange.run(second_forward, second.token, PARAMS, 10000)
        )
        await asyncio.gather(wait_session(first_exchange), wait_session(second_exchange))
        if replace_port:
            first_forward.port = second_forward.port
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await asyncio.wait_for(first.disconnected.wait(), 1)
        assert not second.cancels and not second.disconnected.is_set() and not second_task.done()
        assert len(first.cancels) == (0 if replace_port else 1)
        if not replace_port:
            assert first.cancels[0][1]["params"] == {
                "target_session_id": first.session_id,
                "target_request_id": first_id,
            }
        else:
            assert first_exchange.cleanup["cancel_code"] == "forward_replaced"
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task
        await asyncio.wait_for(second.disconnected.wait(), 1)
        assert len(second.cancels) == 1
        assert second.cancels[0][1]["params"] == {
            "target_session_id": second.session_id,
            "target_request_id": second_id,
        }
        assert second_exchange.cleanup["cancel_acknowledged"] is True


async def test_invalidated_or_used_exchange_cannot_run_on_a_new_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = AsyncMock(side_effect=AssertionError("unexpected connection"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    exchange = ao.ObserveExchange(str(uuid4()))
    exchange.invalidate()
    forward = at.ForwardLease(at.AndroidBackend(), "S1", port=1234, owned=True)
    for _attempt in range(2):
        with pytest.raises(at.ConnectionFault, match=r"^agent_observation_rejected$"):
            await exchange.run(forward, TOKEN, PARAMS, 100)
    connect.assert_not_awaited()


async def test_unexpected_exception_is_safe_and_still_closes_owned_writer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = PendingAgent()
    agent.send_terminal = True

    def fail(*_args: object, **_kwargs: object) -> ao.ObserveReply:
        raise RuntimeError("private-source " + TOKEN)

    monkeypatch.setattr(ao, "_parse_reply", fail)
    exchange = ao.ObserveExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        with caplog.at_level(logging.DEBUG), pytest.raises(at.ConnectionFault) as caught:
            await exchange.run(forward, TOKEN, PARAMS, 100)
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert str(caught.value) == "agent_observation_rejected"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert TOKEN not in str(caught.value) + repr(exchange.cleanup) + caplog.text
    assert "private-source" not in str(caught.value) + repr(exchange.cleanup) + caplog.text
    assert not agent.cancels and exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_owned_writer_close_is_bounded_and_repeated_cancellation_cannot_interrupt_it(
    cancel_during_close: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    entered, release = asyncio.Event(), asyncio.Event()
    real_connect = asyncio.open_connection

    async def connect(
        *args: object, **kwargs: object
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await real_connect(*args, **kwargs)
        real_wait_closed = writer.wait_closed

        async def delayed_close() -> None:
            entered.set()
            await release.wait()
            await real_wait_closed()

        monkeypatch.setattr(writer, "wait_closed", delayed_close)
        return reader, writer

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer, response_headers(session_id) + encoded(terminal(request_id, session_id)) + b"\n"
        )

    monkeypatch.setattr(asyncio, "open_connection", connect)
    exchange = ao.ObserveExchange(request_id)
    async with fake_agent(serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, PARAMS, 1000))
        await asyncio.wait_for(entered.wait(), 1)
        started = asyncio.get_running_loop().time()
        if cancel_during_close:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exchange.cleanup["connection_closed"] is True
        else:
            reply = await task
            assert reply.success and exchange.cleanup["connection_closed"] is False
        assert asyncio.get_running_loop().time() - started < 1.5
