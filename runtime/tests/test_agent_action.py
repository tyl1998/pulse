from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from pulse_runtime.drivers.android import agent_action as aa
from pulse_runtime.drivers.android import agent_transport as at
from pulse_runtime.drivers.android.actions import ActionBody
from runtime.tests.test_agent_observe import (
    TOKEN,
    encoded,
    fake_agent,
    response_headers,
    send,
    terminal,
    wait_session,
)
from runtime.tests.test_agent_observe import (
    no_physical_adb as no_physical_adb,
)

PRIVATE_INPUT = "private-action-input-\u6d4b\u8bd5"
PRIVATE_ERROR = "private-action-error"
PACKAGE = "com.pulse.target"
PARAMS: dict[str, dict[str, object]] = {
    "tap": {"kind": "tap", "expected_package": PACKAGE, "x": 20, "y": 40, "duration_ms": 80},
    "swipe": {
        "kind": "swipe",
        "expected_package": PACKAGE,
        "x": 20,
        "y": 40,
        "end_x": 80,
        "end_y": 100,
        "duration_ms": 300,
    },
    "set_text": {
        "kind": "set_text",
        "expected_package": PACKAGE,
        "node_path": [0, 1],
        "text": PRIVATE_INPUT,
    },
    "node_click": {"kind": "node_click", "expected_package": PACKAGE, "node_path": [0, 1]},
}
COMPLETION_SOURCES = {
    "tap": "gesture_callback",
    "swipe": "gesture_callback",
    "set_text": "accessibility_action_return",
    "node_click": "accessibility_action_return",
}


@pytest.fixture(autouse=True)
def no_host_subprocess(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    forbidden = AsyncMock(side_effect=AssertionError("host subprocess forbidden"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    yield
    forbidden.assert_not_awaited()


async def read_request(reader: asyncio.StreamReader) -> tuple[bytes, dict[str, object]]:
    # Observe's reader intentionally excludes /action. Keep the same bounded wire checks here.
    head = await reader.readuntil(b"\r\n\r\n")
    assert len(head) <= 8192
    lines = head.split(b"\r\n")
    assert lines[0] in {b"POST /v1/action HTTP/1.1", b"POST /v1/cancel HTTP/1.1"}
    pairs = [line.split(b":", 1) for line in lines[1:-2]]
    headers = dict(pairs)
    assert len(headers) == len(pairs)
    assert set(headers) == {
        b"Host",
        b"Authorization",
        b"Content-Type",
        b"Content-Length",
        b"Connection",
    }
    assert headers[b"Host"].strip() == b"127.0.0.1"
    assert headers[b"Content-Type"].strip() == b"application/json"
    assert headers[b"Connection"].strip() == b"close"
    length = headers[b"Content-Length"].strip()
    assert length.isdigit() and not length.startswith(b"0")
    size = int(length)
    assert 1 <= size <= 65536
    raw = await reader.readexactly(size)
    body = json.loads(raw.decode("utf-8", errors="strict"))
    assert isinstance(body, dict)
    assert set(body) == {"protocol_version", "request_id", "operation", "timeout_ms", "params"}
    assert type(body["protocol_version"]) is int and body["protocol_version"] == 1
    assert type(body["timeout_ms"]) is int
    assert isinstance(body["params"], dict)
    at.canonical_uuid(body["request_id"])
    assert lines[0] == f"POST /v1/{body['operation']} HTTP/1.1".encode("ascii")
    return head, body


def action_event(
    request_id: str,
    session_id: str,
    status: str,
    *,
    kind: str = "tap",
    code: str = "dispatch_rejected",
    phase: str = "dispatch",
) -> dict[str, object]:
    result: dict[str, object] = {"status": status}
    if status == "completed":
        result["completion_source"] = COMPLETION_SOURCES[kind]
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "session_id": session_id,
        "operation": "action",
        "success": status in {"dispatched", "completed"},
        "terminal": status != "dispatched",
        "result": result,
        "error": None
        if status in {"dispatched", "completed"}
        else {"code": code, "phase": phase, "retryable": False},
    }


def assert_private_safe(exchange: aa.ActionExchange, value: object, log: str = "") -> None:
    output = str(value) + repr(value) + repr(exchange) + repr(exchange.cleanup) + log
    for private in (TOKEN, "1" * 64, "2" * 64, PRIVATE_INPUT, PRIVATE_ERROR):
        assert private not in output
    assert set(exchange.cleanup) == {"cancel_acknowledged", "cancel_code", "connection_closed"}


class PendingActionAgent:
    def __init__(self, token: str = TOKEN, case: str = "valid") -> None:
        self.token = token
        self.case = case
        self.session_id = str(uuid4())
        self.requests: list[tuple[bytes, dict[str, object]]] = []
        self.entered = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.cancel_entered = asyncio.Event()
        self.late_terminal_written = asyncio.Event()
        self.cancel_release: asyncio.Event | None = None
        self.send_session = True
        self.send_dispatch = True
        self.send_terminal = False
        self.action_writer: asyncio.StreamWriter | None = None
        self.action_request_id: str | None = None
        self.kind = "tap"

    @property
    def cancels(self) -> list[tuple[bytes, dict[str, object]]]:
        return [(head, body) for head, body in self.requests if body["operation"] == "cancel"]

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head, body = await read_request(reader)
        assert b"Authorization: Bearer " + self.token.encode() + b"\r\n" in head
        self.requests.append((head, body))
        request_id = cast(str, body["request_id"])
        if body["operation"] == "action":
            self.action_writer = writer
            self.action_request_id = request_id
            self.kind = cast(str, cast(dict[str, object], body["params"])["kind"])
            if self.send_session:
                data = response_headers(self.session_id)
                if self.send_dispatch:
                    data += encoded(action_event(request_id, self.session_id, "dispatched")) + b"\n"
                if self.send_terminal:
                    data += (
                        encoded(
                            action_event(request_id, self.session_id, "completed", kind=self.kind)
                        )
                        + b"\n"
                    )
                await send(writer, data)
            self.entered.set()
            assert await reader.read(1) == b""
            self.disconnected.set()
            return
        assert body["params"] == {
            "target_session_id": self.session_id,
            "target_request_id": self.action_request_id,
        }
        assert request_id != self.action_request_id
        self.cancel_entered.set()
        if self.cancel_release is not None:
            await self.cancel_release.wait()
        if self.case == "late_terminal":
            assert self.action_writer is not None and self.action_request_id is not None
            await send(
                self.action_writer,
                encoded(
                    action_event(
                        self.action_request_id, self.session_id, "completed", kind=self.kind
                    )
                )
                + b"\n",
            )
            self.late_terminal_written.set()
        control_session = self.session_id if self.case == "original_session" else str(uuid4())
        reply = terminal(request_id, control_session, "cancel")
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
        elif self.case in {"unknown_error", "bad_error_phase"}:
            reply.update(
                success=False,
                result=None,
                error={
                    "code": PRIVATE_ERROR if self.case == "unknown_error" else "busy",
                    "phase": PRIVATE_ERROR if self.case == "bad_error_phase" else "protocol",
                    "retryable": False,
                    "message": self.token + " " + PRIVATE_INPUT,
                },
            )
        raw = encoded(reply)
        if self.case == "duplicate_json":
            raw = raw.replace(b'"cancelled":true', b'"cancelled":true,"cancelled":false')
        elif self.case == "invalid_utf8":
            raw = b"\xff"
        size = 65537 if self.case == "oversize" else len(raw)
        head = response_headers(
            None,
            status=int(self.case[-3:]) if self.case.startswith("auth_") else 200,
            content_type="application/json",
            content_length=None if self.case == "missing_length" else size,
            extra=b"Content-Type: application/json\r\n" if self.case == "duplicate_header" else b"",
        )
        if self.case == "truncated":
            raw = raw[:-1]
        elif self.case == "trailing":
            raw += PRIVATE_ERROR.encode()
        await send(writer, head + raw)
        if self.case == "no_eof":
            assert await reader.read(1) == b""


@pytest.mark.parametrize("kind", PARAMS)
@pytest.mark.parametrize(
    "content_type", ["application/x-ndjson", "application/x-ndjson; charset=utf-8"]
)
async def test_each_kind_requires_dispatch_then_completed_and_eof_without_half_close(
    kind: str, content_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)
    dispatched, completed = asyncio.Event(), asyncio.Event()
    complete_release, eof_release = asyncio.Event(), asyncio.Event()
    requests: list[tuple[bytes, dict[str, object]]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await read_request(reader))
        disconnect = asyncio.create_task(reader.read(1))
        try:
            await send(
                writer,
                response_headers(session_id, content_type=content_type)
                + encoded(action_event(request_id, session_id, "dispatched"))
                + b"\n",
            )
            dispatched.set()
            await complete_release.wait()
            assert not disconnect.done(), "normal request must not half-close or append bytes"
            await send(
                writer,
                encoded(action_event(request_id, session_id, "completed", kind=kind)) + b"\n",
            )
            completed.set()
            await eof_release.wait()
            assert not disconnect.done(), "terminal is not permission to half-close before EOF"
            writer.close()
            assert await disconnect == b""
        finally:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

    with caplog.at_level(logging.DEBUG):
        async with fake_agent(serve) as forward:
            port = forward.port
            task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS[kind]), 1000))
            await asyncio.wait_for(dispatched.wait(), 1)
            done, _pending = await asyncio.wait({task}, timeout=0.03)
            assert not done
            assert exchange.sent and exchange.dispatched_received
            assert exchange.wire_session_id == session_id
            assert not exchange.terminal_received and not exchange.eof_received
            assert exchange.terminal_status is None
            complete_release.set()
            await asyncio.wait_for(completed.wait(), 1)
            done, _pending = await asyncio.wait({task}, timeout=0.03)
            assert not done
            assert exchange.terminal_received and not exchange.eof_received
            assert exchange.terminal_status == "completed"
            eof_release.set()
            reply = await asyncio.wait_for(task, 1)
            with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
                await exchange.run(forward, TOKEN, dict(PARAMS[kind]), 1000)
            assert forward.owned and forward.port == port and forward.serial == "S1"
    assert len(requests) == 1
    head, body = requests[0]
    assert b"Authorization: Bearer " + TOKEN.encode() + b"\r\n" in head
    assert body == {
        "protocol_version": 1,
        "request_id": request_id,
        "operation": "action",
        "timeout_ms": 1000,
        "params": PARAMS[kind],
    }
    assert reply == aa.ActionReply(session_id, "completed", None, COMPLETION_SOURCES[kind])
    assert exchange.sent and exchange.dispatched_received and exchange.terminal_received
    assert exchange.eof_received
    assert exchange.cleanup == {
        "cancel_acknowledged": None,
        "cancel_code": None,
        "connection_closed": True,
    }
    assert exchange._writer is None and exchange._cancel_writer is None
    assert_private_safe(exchange, reply, caplog.text)
    assert "verified" not in repr(reply)
    with pytest.raises(FrozenInstanceError):
        reply.status = "verified"  # type: ignore[misc]


async def test_runtime_long_maps_to_duration_tap_not_a_new_native_kind() -> None:
    body = ActionBody(
        serial="S1",
        session_id=str(uuid4()),
        request_id=str(uuid4()),
        operation_id=str(uuid4()),
        kind="long",
        target={
            "observation_id": str(uuid4()),
            "source": "tree",
            "coordinate_space": "display_pixels",
            "display_id": 0,
            "expected_package": PACKAGE,
            "node_path": [],
        },
        params={"x": 20, "y": 40},
        agent_timeout_ms=1000,
    )
    session_id = str(uuid4())
    requests: list[tuple[bytes, dict[str, object]]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await read_request(reader))
        await send(
            writer,
            response_headers(session_id)
            + encoded(action_event(body.request_id, session_id, "dispatched"))
            + b"\n"
            + encoded(action_event(body.request_id, session_id, "completed"))
            + b"\n",
        )

    exchange = aa.ActionExchange(body.request_id)
    async with fake_agent(serve) as forward:
        reply = await exchange.run(forward, TOKEN, body.wire_params(), body.agent_timeout_ms)
    assert requests[0][1]["params"] == {
        "kind": "tap",
        "expected_package": PACKAGE,
        "x": 20,
        "y": 40,
        "duration_ms": 600,
    }
    assert reply == aa.ActionReply(session_id, "completed", None, "gesture_callback")
    assert_private_safe(exchange, reply)


@pytest.mark.parametrize("kind", PARAMS)
@pytest.mark.parametrize("code", ["dispatch_rejected", "target_changed", "busy"])
async def test_each_kind_can_be_rejected_without_dispatch(kind: str, code: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer,
            response_headers(session_id)
            + encoded(
                action_event(
                    request_id,
                    session_id,
                    "rejected",
                    code=code,
                    phase="queue" if code == "busy" else "dispatch",
                )
            )
            + b"\n",
        )

    async with fake_agent(serve) as forward:
        reply = await exchange.run(forward, TOKEN, dict(PARAMS[kind]), 100)
    assert reply == aa.ActionReply(session_id, "rejected", code)
    assert exchange.sent and not exchange.dispatched_received
    assert exchange.terminal_received and exchange.eof_received
    assert exchange.terminal_status == "rejected"
    assert exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, reply)


@pytest.mark.parametrize("kind", PARAMS)
@pytest.mark.parametrize("code", ["gesture_cancelled", "action_uncertain"])
async def test_each_kind_failed_terminal_is_not_completed(kind: str, code: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer,
            response_headers(session_id)
            + encoded(action_event(request_id, session_id, "dispatched"))
            + b"\n"
            + encoded(action_event(request_id, session_id, "failed", code=code, phase="completion"))
            + b"\n",
        )

    async with fake_agent(serve) as forward:
        reply = await exchange.run(forward, TOKEN, dict(PARAMS[kind]), 100)
    assert reply == aa.ActionReply(session_id, "failed", code)
    assert exchange.dispatched_received and exchange.terminal_received and exchange.eof_received
    assert exchange.terminal_status == "failed"
    assert exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, reply)


@pytest.mark.parametrize("stage", ["dispatched", "completed"])
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("protocol_version", True, "action_protocol_invalid"),
        ("protocol_version", 2, "action_protocol_invalid"),
        ("protocol_version", "1", "action_protocol_invalid"),
        ("request_id", str(uuid4()), "action_protocol_invalid"),
        ("request_id", None, "invalid_message"),
        ("request_id", "123E4567-E89B-12D3-A456-426614174000", "invalid_message"),
        ("session_id", str(uuid4()), "action_protocol_invalid"),
        ("session_id", None, "invalid_message"),
        ("session_id", "123E4567-E89B-12D3-A456-426614174000", "invalid_message"),
        ("operation", "observe", "action_protocol_invalid"),
        ("operation", "cancel", "action_protocol_invalid"),
        ("operation", "health", "action_protocol_invalid"),
        ("success", 1, "action_protocol_invalid"),
        ("success", "true", "action_protocol_invalid"),
        ("success", None, "action_protocol_invalid"),
        ("terminal", 0, "action_protocol_invalid"),
        ("terminal", 1, "action_protocol_invalid"),
        ("terminal", "true", "action_protocol_invalid"),
        ("terminal", None, "action_protocol_invalid"),
        ("result", None, "action_protocol_invalid"),
        ("result", [], "action_protocol_invalid"),
        ("private_field", PRIVATE_ERROR, "action_protocol_invalid"),
        (
            "error",
            {"code": "busy", "phase": "queue", "retryable": False},
            "action_protocol_invalid",
        ),
    ],
)
async def test_dispatch_and_completed_require_exact_correlated_envelope(
    stage: str, field: str, value: object, code: str
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = action_event(request_id, session_id, stage, kind="set_text")
        body[field] = value
        prefix = (
            encoded(action_event(request_id, session_id, "dispatched")) + b"\n"
            if stage == "completed"
            else b""
        )
        await send(writer, response_headers(session_id) + prefix + encoded(body) + b"\n")

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
    assert exchange.dispatched_received is (stage == "completed")
    assert not exchange.terminal_received and exchange.terminal_status is None
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize("stage", ["rejected", "failed"])
@pytest.mark.parametrize(
    "error",
    [
        None,
        [],
        {"code": "busy"},
        {"code": "busy", "phase": "queue"},
        {"code": "busy", "retryable": False},
        {"code": 1, "phase": "queue", "retryable": False},
        {"code": PRIVATE_ERROR, "phase": "queue", "retryable": False},
        {"code": TOKEN, "phase": "queue", "retryable": False},
        {"code": "busy", "phase": PRIVATE_INPUT, "retryable": False},
        {"code": "busy", "phase": ["queue"], "retryable": False},
        {"code": "busy", "phase": "queue", "retryable": 0},
        {"code": "busy", "phase": "queue", "retryable": "false"},
        {"code": "busy", "phase": "queue", "retryable": None},
        {"code": "busy", "phase": "queue", "retryable": False, "message": PRIVATE_ERROR + TOKEN},
    ],
)
async def test_failed_envelopes_require_closed_error_fields_and_strict_retryable(
    stage: str, error: object
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = action_event(request_id, session_id, stage)
        body["error"] = error
        prefix = (
            encoded(action_event(request_id, session_id, "dispatched")) + b"\n"
            if stage == "failed"
            else b""
        )
        await send(writer, response_headers(session_id) + prefix + encoded(body) + b"\n")

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=r"^action_protocol_invalid$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    assert not exchange.terminal_received and exchange.terminal_status is None
    assert exchange.dispatched_received is (stage == "failed")
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("non_object", "action_protocol_invalid"),
        ("invalid_utf8", "action_protocol_invalid"),
        ("utf16", "action_protocol_invalid"),
        ("bom", "action_protocol_invalid"),
        ("duplicate_top", "invalid_message"),
        ("duplicate_result", "invalid_message"),
        ("duplicate_error", "invalid_message"),
        ("nan", "invalid_message"),
        ("infinity", "invalid_message"),
        ("negative_infinity", "invalid_message"),
        ("float_overflow", "invalid_message"),
        ("missing_field", "action_protocol_invalid"),
        ("trailing_document", "action_protocol_invalid"),
        ("unknown_result_field", "action_protocol_invalid"),
        ("deep_json", "action_protocol_invalid"),
    ],
)
async def test_action_json_is_strict_and_private_data_never_becomes_error_output(
    case: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = action_event(request_id, session_id, "rejected")
        raw = encoded(body)
        if case == "non_object":
            raw = b"[]"
        elif case == "invalid_utf8":
            raw = raw.replace(b"dispatch_rejected", b"\xff" + PRIVATE_ERROR.encode())
        elif case == "utf16":
            raw = raw.decode().encode("utf-16-le")
        elif case == "bom":
            raw = b"\xef\xbb\xbf" + raw
        elif case == "duplicate_top":
            raw = b'{"terminal":true,' + raw[1:]
        elif case == "duplicate_result":
            raw = raw.replace(b'"status":"rejected"', b'"status":"rejected","status":"failed"')
        elif case == "duplicate_error":
            raw = raw.replace(b'"retryable":false', b'"retryable":false,"retryable":true')
        elif case in {"nan", "infinity", "negative_infinity", "float_overflow", "deep_json"}:
            replacement = {
                "nan": b"NaN",
                "infinity": b"Infinity",
                "negative_infinity": b"-Infinity",
                "float_overflow": b"1e10000",
                "deep_json": b"[" * 2000 + b"0" + b"]" * 2000,
            }[case]
            raw = raw.replace(b"false", replacement)
        elif case == "missing_field":
            del body["error"]
            raw = encoded(body)
        elif case == "trailing_document":
            raw += encoded({"private": PRIVATE_ERROR + TOKEN + PRIVATE_INPUT})
        elif case == "unknown_result_field":
            body["result"] = {
                "status": "rejected",
                "private": PRIVATE_ERROR + TOKEN + PRIVATE_INPUT,
            }
            raw = encoded(body)
        await send(writer, response_headers(session_id) + raw + b"\n")

    async with fake_agent(serve) as forward:
        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught,
        ):
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
    assert not exchange.terminal_received and not exchange.dispatched_received
    assert exchange.cleanup["connection_closed"] is True
    assert caught.value.__cause__ is None
    assert_private_safe(exchange, caught.value, caplog.text)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("empty_http_200", "action_terminal_missing"),
        ("dispatch_only", "action_terminal_missing"),
        ("completed_without_dispatch", "action_protocol_invalid"),
        ("failed_without_dispatch", "action_protocol_invalid"),
        ("rejected_after_dispatch", "action_protocol_invalid"),
        ("duplicate_dispatch", "action_protocol_invalid"),
        ("duplicate_terminal", "action_protocol_invalid"),
        ("rejected_then_completed", "action_protocol_invalid"),
        ("dispatch_after_terminal", "action_protocol_invalid"),
        ("missing_dispatch_lf", "action_terminal_missing"),
        ("missing_terminal_lf", "action_terminal_missing"),
        ("truncated_dispatch", "action_protocol_invalid"),
        ("truncated_terminal", "action_protocol_invalid"),
        ("truncated_terminal_without_lf", "action_terminal_missing"),
        ("empty_line", "action_protocol_invalid"),
        ("extra_lf", "action_protocol_invalid"),
        ("dispatch_crlf", "action_protocol_invalid"),
        ("terminal_crlf", "action_protocol_invalid"),
        ("trailing_bytes", "action_protocol_invalid"),
        ("accepted_instead_of_dispatched", "action_protocol_invalid"),
    ],
)
async def test_http_200_requires_exact_two_frame_state_machine_and_clean_eof(
    case: str, code: str
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)
    requests: list[tuple[bytes, dict[str, object]]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await read_request(reader))
        dispatched = encoded(action_event(request_id, session_id, "dispatched"))
        completed = encoded(action_event(request_id, session_id, "completed"))
        rejected = encoded(action_event(request_id, session_id, "rejected"))
        failed = encoded(action_event(request_id, session_id, "failed"))
        data = {
            "empty_http_200": b"",
            "dispatch_only": dispatched + b"\n",
            "completed_without_dispatch": completed + b"\n",
            "failed_without_dispatch": failed + b"\n",
            "rejected_after_dispatch": dispatched + b"\n" + rejected + b"\n",
            "duplicate_dispatch": dispatched + b"\n" + dispatched + b"\n" + completed + b"\n",
            "duplicate_terminal": dispatched + b"\n" + completed + b"\n" + completed + b"\n",
            "rejected_then_completed": rejected + b"\n" + completed + b"\n",
            "dispatch_after_terminal": dispatched + b"\n" + completed + b"\n" + dispatched + b"\n",
            "missing_dispatch_lf": dispatched,
            "missing_terminal_lf": dispatched + b"\n" + completed,
            "truncated_dispatch": dispatched[:-1] + b"\n",
            "truncated_terminal": dispatched + b"\n" + completed[:-1] + b"\n",
            "truncated_terminal_without_lf": dispatched + b"\n" + completed[:-1],
            "empty_line": b"\n",
            "extra_lf": dispatched + b"\n" + completed + b"\n\n",
            "dispatch_crlf": dispatched + b"\r\n" + completed + b"\n",
            "terminal_crlf": dispatched + b"\n" + completed + b"\r\n",
            "trailing_bytes": dispatched + b"\n" + completed + b"\n" + PRIVATE_ERROR.encode(),
            "accepted_instead_of_dispatched": dispatched.replace(b'"dispatched"', b'"accepted"')
            + b"\n",
        }[case]
        await send(writer, response_headers(session_id) + data)

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
        with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    assert len(requests) == 1
    assert exchange.cleanup["connection_closed"] is True
    assert exchange.cleanup["cancel_acknowledged"] is None
    if case in {
        "empty_http_200",
        "dispatch_only",
        "missing_dispatch_lf",
        "missing_terminal_lf",
        "truncated_terminal_without_lf",
    }:
        assert exchange.eof_received and not exchange.terminal_received
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize("kind", PARAMS)
async def test_completion_source_must_match_the_native_kind(kind: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        completed = action_event(request_id, session_id, "completed", kind=kind)
        completed["result"] = {
            "status": "completed",
            "completion_source": "accessibility_action_return"
            if kind in {"tap", "swipe"}
            else "gesture_callback",
        }
        await send(
            writer,
            response_headers(session_id)
            + encoded(action_event(request_id, session_id, "dispatched"))
            + b"\n"
            + encoded(completed)
            + b"\n",
        )

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=r"^action_protocol_invalid$"):
            await exchange.run(forward, TOKEN, dict(PARAMS[kind]), 100)
    assert exchange.dispatched_received and not exchange.terminal_received
    assert exchange.terminal_status is None and exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_action_output_limit_is_total_utf8_wire_bytes_including_both_lfs(
    extra_bytes: int,
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        first = encoded(action_event(request_id, session_id, "dispatched")) + b"\n"
        last = encoded(action_event(request_id, session_id, "completed")) + b"\n"
        data = first + b" " * (65536 + extra_bytes - len(first) - len(last)) + last
        assert len(data) == 65536 + extra_bytes
        await send(writer, response_headers(session_id) + data)

    async with fake_agent(serve) as forward:
        if extra_bytes:
            with pytest.raises(at.ConnectionFault, match=r"^action_output_limit$"):
                await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 1000)
            assert exchange.dispatched_received and not exchange.terminal_received
        else:
            reply = await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 1000)
            assert reply == aa.ActionReply(session_id, "completed", None, "gesture_callback")
            assert exchange.terminal_received and exchange.eof_received
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("stage", ["dispatched", "completed"])
async def test_a_single_oversized_action_line_is_bounded(stage: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        raw = encoded(action_event(request_id, session_id, stage))
        raw = b" " * (65537 - len(raw)) + raw + b"\n"
        prefix = (
            encoded(action_event(request_id, session_id, "dispatched")) + b"\n"
            if stage == "completed"
            else b""
        )
        await send(writer, response_headers(session_id) + prefix + raw)

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=r"^action_output_limit$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 1000)
    assert not exchange.terminal_received and exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing_session", "invalid_message"),
        ("invalid_session", "invalid_message"),
        ("duplicate_session", "invalid_message"),
        ("duplicate_type", "invalid_message"),
        ("duplicate_unknown", "invalid_message"),
        ("bad_name", "invalid_message"),
        ("folded", "invalid_message"),
        ("non_ascii", "action_protocol_invalid"),
        ("bare_lf", "invalid_message"),
        ("wrong_version", "invalid_message"),
        ("content_length", "action_protocol_invalid"),
        ("chunked", "action_protocol_invalid"),
        ("expect", "action_protocol_invalid"),
        ("wrong_type", "action_protocol_invalid"),
        ("wrong_charset", "action_protocol_invalid"),
        ("keep_alive", "action_protocol_invalid"),
        ("oversize", "response_too_large"),
        ("truncated", "action_terminal_missing"),
    ],
)
async def test_action_http_stream_headers_are_strict_and_bounded(case: str, code: str) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        head = response_headers(session_id)
        additions = {
            "duplicate_session": f"x-pulse-session: {session_id}\r\n".encode(),
            "duplicate_type": b"Content-Type: application/x-ndjson\r\n",
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
        elif case == "wrong_type":
            head = response_headers(session_id, content_type="application/json")
        elif case == "wrong_charset":
            head = response_headers(session_id, content_type="application/x-ndjson; charset=latin1")
        elif case == "keep_alive":
            head = head.replace(b"Connection: close", b"Connection: keep-alive")
        elif case == "truncated":
            head = head[:-2]
        data = (
            b""
            if case == "truncated"
            else encoded(action_event(request_id, session_id, "rejected")) + b"\n"
        )
        await send(writer, head + data)

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    assert not exchange.dispatched_received and not exchange.terminal_received
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_body_is_not_parsed_or_exposed(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer,
            response_headers(None, status=status)
            + b"\xff"
            + encoded({"private": TOKEN + PRIVATE_INPUT + PRIVATE_ERROR}),
        )

    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(serve) as forward:
        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(at.ConnectionFault, match=r"^authentication_failed$") as caught,
        ):
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
    assert not exchange.dispatched_received and not exchange.terminal_received
    assert exchange.wire_session_id is None and exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value, caplog.text)


@pytest.mark.parametrize(
    ("status", "error_code"),
    [(400, "invalid_message"), (409, "busy"), (500, "internal_error"), (503, "platform_failed")],
)
async def test_ordinary_http_error_json_gets_session_from_body_not_stream_header(
    status: int, error_code: str
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)
    requests: list[tuple[bytes, dict[str, object]]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await read_request(reader))
        body = action_event(request_id, session_id, "rejected", code=error_code, phase="queue")
        body["result"] = None
        raw = encoded(body)
        head = response_headers(
            None, status=status, content_type="application/json", content_length=len(raw)
        )
        assert b"X-Pulse-Session" not in head
        await send(writer, head + raw)

    async with fake_agent(serve) as forward:
        reply = await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
        with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    assert len(requests) == 1
    assert reply == aa.ActionReply(session_id, "rejected", error_code)
    assert not exchange.dispatched_received and exchange.terminal_received and exchange.eof_received
    assert exchange.terminal_status == "rejected"
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, reply)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("success", "action_protocol_invalid"),
        ("nonterminal", "action_protocol_invalid"),
        ("wrong_request", "action_protocol_invalid"),
        ("wrong_session", "invalid_message"),
        ("wrong_operation", "action_protocol_invalid"),
        ("wrong_protocol", "action_protocol_invalid"),
        ("private_result", "action_protocol_invalid"),
        ("trailing", "action_protocol_invalid"),
        ("truncated", "action_terminal_missing"),
        ("missing_length", "invalid_message"),
        ("duplicate_length", "invalid_message"),
        ("zero_length", "invalid_message"),
        ("leading_zero_length", "invalid_message"),
        ("oversize", "response_too_large"),
        ("chunked", "invalid_message"),
        ("wrong_type", "invalid_message"),
        ("redirect", "action_protocol_invalid"),
        ("accepted", "action_protocol_invalid"),
    ],
)
async def test_http_error_json_requires_correlated_terminal_exact_framing_and_eof(
    case: str, code: str
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        body = action_event(request_id, session_id, "rejected", code="busy", phase="queue")
        body["result"] = None
        if case == "success":
            body.update(
                success=True,
                result={"status": "completed", "completion_source": "gesture_callback"},
                error=None,
            )
        elif case == "nonterminal":
            body.update(terminal=False, success=True, result={"status": "dispatched"}, error=None)
        elif case == "wrong_request":
            body["request_id"] = str(uuid4())
        elif case == "wrong_session":
            body["session_id"] = "not-a-uuid"
        elif case == "wrong_operation":
            body["operation"] = "observe"
        elif case == "wrong_protocol":
            body["protocol_version"] = True
        elif case == "private_result":
            body["result"] = {"private": PRIVATE_ERROR + TOKEN + PRIVATE_INPUT}
        raw = encoded(body)
        length: int | str | None = len(raw)
        if case == "missing_length":
            length = None
        elif case == "zero_length":
            length = 0
        elif case == "leading_zero_length":
            length = f"0{len(raw)}"
        elif case == "oversize":
            length = 65537
        extra = {
            "duplicate_length": b"content-length: 1\r\n",
            "chunked": b"Transfer-Encoding: chunked\r\n",
        }.get(case, b"")
        status = 302 if case == "redirect" else 202 if case == "accepted" else 500
        head = response_headers(
            None,
            status=status,
            content_type="application/x-ndjson" if case == "wrong_type" else "application/json",
            content_length=length,
            extra=extra,
        )
        if case == "truncated":
            raw = raw[:-1]
        elif case == "trailing":
            raw += PRIVATE_ERROR.encode()
        await send(writer, head + raw)

    async with fake_agent(serve) as forward:
        with pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
    assert not exchange.dispatched_received and exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize(
    "request_id",
    [
        "bad",
        "123E4567-E89B-12D3-A456-426614174000",
        "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
        None,
    ],
)
def test_action_request_id_is_a_caller_canonical_uuid(request_id: object) -> None:
    with pytest.raises(at.ConnectionFault, match=r"^invalid_message$"):
        aa.ActionExchange(cast(str, request_id))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("params", {"kind": "long"}, "invalid_message"),
        ("params", {"kind": "key"}, "invalid_message"),
        ("params", {"kind": "launch"}, "invalid_message"),
        ("params", {}, "invalid_message"),
        ("params", {"kind": 1}, "invalid_message"),
        ("timeout_ms", 99, "invalid_message"),
        ("timeout_ms", 10001, "invalid_message"),
        ("timeout_ms", True, "invalid_message"),
        ("token", "A" * 64, "credential_unavailable"),
        ("token", "a" * 63, "credential_unavailable"),
        ("token", TOKEN + "\r\n" + PRIVATE_INPUT, "credential_unavailable"),
        ("port", None, "forward_unavailable"),
        ("port", True, "forward_unavailable"),
        ("port", 65536, "forward_unavailable"),
        ("owned", False, "forward_replaced"),
    ],
)
async def test_invalid_action_admission_opens_no_socket_and_still_consumes_exchange(
    field: str, value: object, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect = AsyncMock(side_effect=AssertionError("unexpected connection"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    forward = at.ForwardLease(at.AndroidBackend(), "S1", port=1234, owned=True)
    kwargs: dict[str, object] = {
        "forward": forward,
        "token": TOKEN,
        "params": dict(PARAMS["tap"]),
        "timeout_ms": 100,
    }
    if field in {"port", "owned"}:
        setattr(forward, field, value)
    else:
        kwargs[field] = value
    exchange = aa.ActionExchange(str(uuid4()))
    with pytest.raises(at.ConnectionFault, match=f"^{code}$") as caught:
        await exchange.run(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
        await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    connect.assert_not_awaited()
    assert not exchange.sent and not exchange.dispatched_received
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


@pytest.mark.parametrize("cancelled", [True, False])
async def test_cancel_targets_only_original_agent_http_session_and_request(cancelled: bool) -> None:
    agent = PendingActionAgent(case="valid" if cancelled else "cancelled_false")
    request_id = str(uuid4())
    exchange = aa.ActionExchange(request_id)
    async with fake_agent(agent.serve) as forward:
        port = forward.port
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 10000))
        await wait_session(exchange)
        assert exchange.dispatched_received and not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
        assert forward.owned and forward.port == port
        assert forward.backend.verifications == 1
        with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
    assert len(agent.cancels) == 1
    head, body = agent.cancels[0]
    assert b"Authorization: Bearer " + TOKEN.encode() + b"\r\n" in head
    assert body["request_id"] != request_id
    at.canonical_uuid(body["request_id"])
    assert body["timeout_ms"] == 2000
    assert body["params"] == {
        "target_session_id": agent.session_id,
        "target_request_id": request_id,
    }
    assert exchange.wire_session_id == agent.session_id
    assert exchange.cleanup == {
        "cancel_acknowledged": cancelled,
        "cancel_code": None,
        "connection_closed": True,
    }
    assert not exchange.terminal_received and not exchange.eof_received
    assert exchange.terminal_status is None
    assert exchange._writer is None and exchange._cancel_writer is None
    assert_private_safe(exchange, None)


@pytest.mark.parametrize("stage", ["before_headers", "before_dispatch", "after_dispatch"])
async def test_caller_cancellation_only_sends_cancel_when_session_is_known(stage: str) -> None:
    agent = PendingActionAgent()
    agent.send_session = stage != "before_headers"
    agent.send_dispatch = stage == "after_dispatch"
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 10000))
        await asyncio.wait_for(agent.entered.wait(), 1)
        if agent.send_session:
            await wait_session(exchange)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
        assert forward.backend.verifications == (1 if agent.send_session else 0)
    assert len(agent.cancels) == (1 if agent.send_session else 0)
    assert not exchange.terminal_received and not exchange.eof_received
    assert exchange.dispatched_received is (stage == "after_dispatch")
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, None)


@pytest.mark.parametrize(
    "stage", ["before_headers", "before_dispatch", "dispatched", "terminal_without_eof"]
)
async def test_absolute_action_timeout_never_returns_success_and_closes_owned_socket(
    stage: str,
) -> None:
    agent = PendingActionAgent()
    agent.send_session = stage != "before_headers"
    agent.send_dispatch = stage in {"dispatched", "terminal_without_eof"}
    agent.send_terminal = stage == "terminal_without_eof"
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        started = asyncio.get_running_loop().time()
        with pytest.raises(at.ConnectionFault, match=r"^action_timeout$") as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
        elapsed = asyncio.get_running_loop().time() - started
        assert 2 <= elapsed < 3.2
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == (1 if agent.send_session else 0)
    assert exchange.dispatched_received is agent.send_dispatch
    assert exchange.terminal_received is agent.send_terminal
    assert not exchange.eof_received and exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value)


async def test_partial_header_progress_cannot_extend_the_absolute_deadline() -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        for byte in b"HTTP/1.1 200 Status\r\n":
            await send(writer, bytes([byte]))
            await asyncio.sleep(0.2)

    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(serve) as forward:
        started = asyncio.get_running_loop().time()
        with pytest.raises(at.ConnectionFault, match=r"^action_timeout$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
        assert 2 <= asyncio.get_running_loop().time() - started < 3.2
    assert exchange.wire_session_id is None
    assert not exchange.dispatched_received and not exchange.terminal_received
    assert exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("reason", ["caller_cancelled", "timeout"])
async def test_terminal_sent_after_cancellation_begins_cannot_turn_into_success(
    reason: str,
) -> None:
    agent = PendingActionAgent(case="late_terminal")
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(
            exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100 if reason == "timeout" else 10000)
        )
        await wait_session(exchange)
        assert exchange.dispatched_received
        if reason == "caller_cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(at.ConnectionFault, match=r"^action_timeout$"):
                await task
        await asyncio.wait_for(agent.late_terminal_written.wait(), 1)
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == 1
    assert exchange.dispatched_received and not exchange.terminal_received
    assert exchange.terminal_status is None
    assert exchange.cleanup["cancel_acknowledged"] is True
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, None)


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
        ("bad_error_phase", "invalid_message"),
        ("missing_length", "invalid_message"),
        ("oversize", "response_too_large"),
        ("truncated", "invalid_message"),
        ("trailing", "invalid_message"),
        ("no_eof", "timeout"),
    ],
)
async def test_bad_cancel_reply_is_bounded_private_and_never_acknowledged_or_retried(
    case: str, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    agent = PendingActionAgent(case=case)
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        with caplog.at_level(logging.DEBUG):
            task = asyncio.create_task(
                exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 10000)
            )
            await wait_session(exchange)
            started = asyncio.get_running_loop().time()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert asyncio.get_running_loop().time() - started < 1.8
            await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == 1
    assert exchange.cleanup == {
        "cancel_acknowledged": None,
        "cancel_code": code,
        "connection_closed": True,
    }
    assert not exchange.terminal_received
    assert exchange._writer is None and exchange._cancel_writer is None
    assert_private_safe(exchange, None, caplog.text)


async def test_repeated_caller_cancel_finishes_one_control_request_and_owned_cleanup() -> None:
    agent = PendingActionAgent()
    agent.cancel_release = asyncio.Event()
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 10000))
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
    assert exchange._writer is None and exchange._cancel_writer is None


async def test_concurrent_run_of_same_exchange_does_not_open_a_second_action_socket() -> None:
    agent = PendingActionAgent()
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 10000))
        await wait_session(exchange)
        with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["swipe"]), 1000)
        assert len(agent.requests) == 1 and not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert len(agent.cancels) == 1


@pytest.mark.parametrize(
    "replacement",
    ["none", "port", "serial", "unowned", "mapping", "invalidate", "foreign_and_invalidate"],
)
async def test_two_serials_with_same_request_id_cancel_and_invalidate_independently(
    replacement: str,
) -> None:
    first, second = PendingActionAgent("1" * 64), PendingActionAgent("2" * 64)
    request_id = str(uuid4())
    first_exchange, second_exchange = aa.ActionExchange(request_id), aa.ActionExchange(request_id)
    async with (
        fake_agent(first.serve, "S1") as first_forward,
        fake_agent(second.serve, "S2") as second_forward,
    ):
        first_task = asyncio.create_task(
            first_exchange.run(first_forward, first.token, dict(PARAMS["tap"]), 10000)
        )
        second_task = asyncio.create_task(
            second_exchange.run(second_forward, second.token, dict(PARAMS["tap"]), 10000)
        )
        await asyncio.gather(wait_session(first_exchange), wait_session(second_exchange))
        if replacement in {"port", "foreign_and_invalidate"}:
            first_forward.port = second_forward.port
        elif replacement == "serial":
            first_forward.serial = second_forward.serial
        elif replacement == "unowned":
            first_forward.owned = False
        elif replacement == "mapping":
            first_forward.backend.present = False
        if replacement in {"invalidate", "foreign_and_invalidate"}:
            first_exchange.invalidate()
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await asyncio.wait_for(first.disconnected.wait(), 1)
        assert not second.cancels and not second.disconnected.is_set() and not second_task.done()
        assert second_forward.owned and second_forward.serial == "S2"
        assert len(second.requests) == 1
        assert len(first.cancels) == (1 if replacement == "none" else 0)
        if replacement == "none":
            assert first.cancels[0][1]["params"] == {
                "target_session_id": first.session_id,
                "target_request_id": request_id,
            }
            assert first_exchange.cleanup["cancel_acknowledged"] is True
        elif replacement in {"port", "serial", "unowned", "mapping"}:
            assert first_exchange.cleanup["cancel_code"] == "forward_replaced"
        else:
            assert first_exchange.cleanup["cancel_acknowledged"] is None
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task
        await asyncio.wait_for(second.disconnected.wait(), 1)
        assert len(second.cancels) == 1
        assert second.cancels[0][1]["params"] == {
            "target_session_id": second.session_id,
            "target_request_id": request_id,
        }
        assert second_exchange.cleanup["cancel_acknowledged"] is True
    assert first_exchange.wire_session_id != second_exchange.wire_session_id
    assert first_exchange.cleanup["connection_closed"] is True
    assert second_exchange.cleanup["connection_closed"] is True
    assert_private_safe(first_exchange, None)
    assert_private_safe(second_exchange, None)


async def test_invalidate_during_forward_verification_closes_action_and_forbids_control_write() -> (
    None
):
    agent = PendingActionAgent()
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        backend = forward.backend
        backend.verify_entered, backend.verify_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 10000))
        await wait_session(exchange)
        task.cancel()
        await asyncio.wait_for(backend.verify_entered.wait(), 1)
        exchange.invalidate()
        await asyncio.wait_for(agent.disconnected.wait(), 0.5)
        backend.verify_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not agent.cancels and exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True


async def test_invalidate_during_cancel_closes_both_owned_sockets_without_retry() -> None:
    agent = PendingActionAgent()
    agent.cancel_release = asyncio.Event()
    exchange = aa.ActionExchange(str(uuid4()))
    async with fake_agent(agent.serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 10000))
        await wait_session(exchange)
        task.cancel()
        await asyncio.wait_for(agent.cancel_entered.wait(), 1)
        exchange.invalidate()
        await asyncio.wait_for(agent.disconnected.wait(), 0.5)
        task.cancel()
        agent.cancel_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(agent.cancels) == 1
    assert exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True
    assert exchange._writer is None and exchange._cancel_writer is None
    assert_private_safe(exchange, None)


async def test_invalidated_exchange_cannot_start_even_on_a_new_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = AsyncMock(side_effect=AssertionError("unexpected connection"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    exchange = aa.ActionExchange(str(uuid4()))
    exchange.invalidate()
    forward = at.ForwardLease(at.AndroidBackend(), "S2", port=1234, owned=True)
    with pytest.raises(at.ConnectionFault, match=r"^agent_observation_rejected$"):
        await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    with pytest.raises(at.ConnectionFault, match=r"^action_request_reused$"):
        await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
    connect.assert_not_awaited()
    assert not exchange.sent and exchange.cleanup["connection_closed"] is True


@pytest.mark.parametrize("replacement", ["port", "serial", "unowned", "invalidate"])
async def test_complete_terminal_from_replaced_forward_cannot_commit_success(
    replacement: str,
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)
    written, release = asyncio.Event(), asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer,
            response_headers(session_id)
            + encoded(action_event(request_id, session_id, "dispatched"))
            + b"\n"
            + encoded(action_event(request_id, session_id, "completed"))
            + b"\n",
        )
        written.set()
        await release.wait()

    async with fake_agent(serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 1000))
        await asyncio.wait_for(written.wait(), 1)
        done, _pending = await asyncio.wait({task}, timeout=0.03)
        assert not done and exchange.terminal_received and not exchange.eof_received
        if replacement == "port":
            forward.port = 1 if forward.port != 1 else 2
        elif replacement == "serial":
            forward.serial = "S2"
        elif replacement == "unowned":
            forward.owned = False
        else:
            exchange.invalidate()
        release.set()
        code = "agent_observation_rejected" if replacement == "invalidate" else "forward_replaced"
        with pytest.raises(at.ConnectionFault, match=f"^{code}$"):
            await task
    assert exchange.terminal_status == "completed"
    assert exchange.cleanup["cancel_acknowledged"] is None
    assert exchange.cleanup["connection_closed"] is True


async def test_unexpected_parser_error_is_sanitized_and_still_closes_owned_socket(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = PendingActionAgent()
    agent.send_terminal = True
    exchange = aa.ActionExchange(str(uuid4()))

    def fail(*_args: object, **_kwargs: object) -> aa.ActionReply:
        raise RuntimeError(PRIVATE_ERROR + " " + TOKEN + " " + PRIVATE_INPUT)

    monkeypatch.setattr(exchange, "parse_event", fail)
    async with fake_agent(agent.serve) as forward:
        with caplog.at_level(logging.DEBUG), pytest.raises(at.ConnectionFault) as caught:
            await exchange.run(forward, TOKEN, dict(PARAMS["set_text"]), 100)
        await asyncio.wait_for(agent.disconnected.wait(), 1)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert not exchange.terminal_received and not agent.cancels
    assert exchange.cleanup["connection_closed"] is True
    assert_private_safe(exchange, caught.value, caplog.text)


@pytest.mark.parametrize("cancel_during_close", [False, True])
async def test_owned_close_is_bounded_and_does_not_swallow_caller_cancellation(
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
            writer,
            response_headers(session_id)
            + encoded(action_event(request_id, session_id, "dispatched"))
            + b"\n"
            + encoded(action_event(request_id, session_id, "completed"))
            + b"\n",
        )

    monkeypatch.setattr(asyncio, "open_connection", connect)
    exchange = aa.ActionExchange(request_id)
    async with fake_agent(serve) as forward:
        task = asyncio.create_task(exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 1000))
        await asyncio.wait_for(entered.wait(), 1)
        assert exchange.terminal_received and exchange.eof_received and not task.done()
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
            assert reply.status == "completed" and exchange.cleanup["connection_closed"] is False
        assert asyncio.get_running_loop().time() - started < 1.8
    assert exchange._writer is None and exchange._cancel_writer is None


async def test_terminal_decoded_after_absolute_deadline_cannot_return_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, session_id = str(uuid4()), str(uuid4())
    exchange = aa.ActionExchange(request_id)
    completed = encoded(action_event(request_id, session_id, "completed")) + b"\n"
    real_connect, real_parse = asyncio.open_connection, exchange.parse_event

    async def connect(
        *args: object, **kwargs: object
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await real_connect(*args, **kwargs)
        real_readuntil = reader.readuntil

        async def buffered_readuntil(separator: bytes = b"\n") -> bytes:
            raw = await real_readuntil(separator)
            if raw == completed:
                # Buffer the real peer EOF before synchronous decoding stalls the event loop.
                assert await reader.read(1) == b""
            return raw

        monkeypatch.setattr(reader, "readuntil", buffered_readuntil)
        return reader, writer

    def delayed_parse(raw: bytes, kind: str, *, http_error: bool = False) -> aa.ActionReply:
        if raw + b"\n" == completed:
            # An expired timer cannot run while decoding. Commit must check the clock itself.
            time.sleep(2.2)
        return real_parse(raw, kind, http_error=http_error)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await read_request(reader)
        await send(
            writer,
            response_headers(session_id)
            + encoded(action_event(request_id, session_id, "dispatched"))
            + b"\n"
            + completed,
        )

    monkeypatch.setattr(asyncio, "open_connection", connect)
    monkeypatch.setattr(exchange, "parse_event", delayed_parse)
    async with fake_agent(serve) as forward:
        started = asyncio.get_running_loop().time()
        with pytest.raises(at.ConnectionFault, match=r"^action_timeout$"):
            await exchange.run(forward, TOKEN, dict(PARAMS["tap"]), 100)
        assert asyncio.get_running_loop().time() - started >= 2.2
    assert exchange.cleanup["connection_closed"] is True
