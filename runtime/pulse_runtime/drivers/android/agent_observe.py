"""Bounded, borrowed Agent observation sockets, not a general action transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from pulse_runtime.drivers.android.agent_transport import (
    ConnectionFault,
    ForwardLease,
    canonical_uuid,
    finish_cleanup,
)

_HEADER_BYTES = 8192
_REQUEST_BYTES = 65536
_EVENT_BYTES = 8388608
_CONTROL_BYTES = 65536
_CHUNK_BYTES = 65536
_ERROR_PHASES = frozenset(
    {"transport", "auth", "protocol", "queue", "observe", "dispatch", "completion", "lifecycle"}
)
_ERROR_CODES = frozenset(
    {
        "unauthorized",
        "forbidden_peer",
        "provisioning_required",
        "invalid_message",
        "message_too_large",
        "unsupported_protocol",
        "not_found",
        "method_not_allowed",
        "busy",
        "timeout",
        "cancelled",
        "disconnected",
        "stopped",
        "accessibility_unavailable",
        "accessibility_interrupted",
        "window_unavailable",
        "unsupported_display",
        "target_changed",
        "target_unavailable",
        "dispatch_rejected",
        "gesture_cancelled",
        "action_uncertain",
        "platform_failed",
        "not_requested",
        "screenshot_unavailable",
        "screenshot_rate_limited",
        "screen_protected",
        "image_too_large",
        "ocr_unavailable",
        "ocr_failed",
        "response_too_large",
        "internal_error",
    }
)
_CANCEL_FAULT_CODES = _ERROR_CODES | {
    "wire_incompatible",
    "authentication_failed",
    "forward_unavailable",
    "forward_replaced",
    "agent_unavailable",
    "agent_observation_rejected",
}


@dataclass(frozen=True)
class ObserveReply:
    wire_session_id: str
    success: bool
    result: dict[str, object] | None = field(repr=False)
    error_code: str | None


def _safe_code(value: object) -> str:
    return (
        value if isinstance(value, str) and value in _ERROR_CODES else "agent_observation_rejected"
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConnectionFault("invalid_message")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ConnectionFault("invalid_message")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ConnectionFault("invalid_message")
    return result


def _parse_reply(
    raw: bytes,
    request_id: str,
    session_id: str | None,
    operation: Literal["observe", "cancel"],
    *,
    keep_observation_result: bool = False,
) -> ObserveReply:
    try:
        envelope: object = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ConnectionFault("invalid_message") from None
    if not isinstance(envelope, dict) or set(envelope) != {
        "protocol_version",
        "request_id",
        "session_id",
        "operation",
        "success",
        "terminal",
        "result",
        "error",
    }:
        raise ConnectionFault("invalid_message")
    if type(envelope["protocol_version"]) is not int or envelope["protocol_version"] != 1:
        raise ConnectionFault("wire_incompatible")
    envelope_session = canonical_uuid(envelope["session_id"])
    if (
        canonical_uuid(envelope["request_id"]) != request_id
        or (session_id is not None and envelope_session != session_id)
        or envelope["operation"] != operation
        or envelope["terminal"] is not True
        or type(envelope["success"]) is not bool
    ):
        raise ConnectionFault("invalid_message")
    success = envelope["success"]
    result = envelope["result"]
    error = envelope["error"]
    if success:
        if not isinstance(result, dict) or error is not None:
            raise ConnectionFault("invalid_message")
        return ObserveReply(envelope_session, True, result, None)
    if (
        not isinstance(error, dict)
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("phase"), str)
        or error["phase"] not in _ERROR_PHASES
        or type(error.get("retryable")) is not bool
    ):
        raise ConnectionFault("invalid_message")
    if result is not None and not (
        keep_observation_result and operation == "observe" and isinstance(result, dict)
    ):
        raise ConnectionFault("invalid_message")
    # Failed NDJSON observations can still contain source evidence. HTTP errors cannot.
    return ObserveReply(envelope_session, False, result, _safe_code(error["code"]))


def _post(
    operation: Literal["observe", "action", "cancel"], token: str, payload: dict[str, object]
) -> bytes:
    body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 1 <= len(body) <= _REQUEST_BYTES:
        raise ConnectionFault("message_too_large")
    return (
        f"POST /v1/{operation} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode("ascii") + body


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > _HEADER_BYTES:
        raise ConnectionFault("response_too_large")
    lines = head[:-4].decode("ascii", errors="strict").split("\r\n")
    status = re.fullmatch(r"HTTP/1\.1 ([1-5][0-9]{2}) [ -~]*", lines[0])
    if status is None or len(lines) > 33 or any(len(line) > 2048 for line in lines):
        raise ConnectionFault("invalid_message")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, raw_value = line.partition(":")
        key = name.lower()
        value = raw_value.strip(" \t")
        if (
            not separator
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None
            or key in headers
            or any(not 32 <= ord(char) <= 126 for char in value)
        ):
            raise ConnectionFault("invalid_message")
        headers[key] = value
    return int(status[1]), headers


def _content_type(headers: dict[str, str], media: str) -> bool:
    return headers.get("content-type", "").lower() in {media, f"{media}; charset=utf-8"}


def _json_length(headers: dict[str, str]) -> int:
    length = headers.get("content-length", "")
    if (
        re.fullmatch(r"[1-9][0-9]{0,9}", length) is None
        or "transfer-encoding" in headers
        or "expect" in headers
        or not _content_type(headers, "application/json")
        or headers.get("connection", "close").lower() != "close"
    ):
        raise ConnectionFault("invalid_message")
    size = int(length)
    if size > _CONTROL_BYTES:
        raise ConnectionFault("response_too_large")
    return size


async def _read_event(reader: asyncio.StreamReader) -> tuple[bytes, bool]:
    data = bytearray()
    while True:
        chunk = await reader.read(min(_CHUNK_BYTES, _EVENT_BYTES + 1 - len(data)))
        if not chunk:
            raise asyncio.IncompleteReadError(bytes(data), None)
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            if b"\r" in data:
                raise ConnectionFault("invalid_message")
            return bytes(data), newline + 1 != len(chunk)
        data.extend(chunk)
        if len(data) > _EVENT_BYTES:
            raise ConnectionFault("response_too_large")


async def _close_writer(writer: asyncio.StreamWriter, deadline: float) -> bool:
    deadline = min(deadline, asyncio.get_running_loop().time() + 1)
    try:
        writer.close()
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        async with asyncio.timeout_at(deadline):
            await writer.wait_closed()
        return True
    except Exception:
        with contextlib.suppress(Exception):
            writer.transport.abort()
        return False


class OwnedAgentExchange:
    def __init__(self, request_id: str) -> None:
        self._request_id = canonical_uuid(request_id)
        self.wire_session_id: str | None = None
        self.sent = False
        self.terminal_received = False
        self.eof_received = False
        self.cleanup: dict[str, object] = {
            "cancel_acknowledged": None,
            "cancel_code": None,
            "connection_closed": False,
        }
        self._writer: asyncio.StreamWriter | None = None
        self._cancel_writer: asyncio.StreamWriter | None = None
        self._started = False
        self._invalidated = False
        self._cancel_attempted = False

    def invalidate(self) -> None:
        self._invalidated = True
        for writer in (self._writer, self._cancel_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()

    def _same_forward(self, forward: ForwardLease, port: int, serial: str) -> bool:
        return forward.owned and forward.port == port and forward.serial == serial

    def _require_current(self, forward: ForwardLease, port: int, serial: str) -> None:
        if self._invalidated:
            raise ConnectionFault("agent_observation_rejected")
        if not self._same_forward(forward, port, serial):
            raise ConnectionFault("forward_replaced")

    async def _observe(
        self, forward: ForwardLease, token: str, params: dict[str, bool], timeout_ms: int
    ) -> ObserveReply:
        if self._started:
            raise ConnectionFault("agent_observation_rejected")
        self._started = True
        port = forward.port
        serial = forward.serial
        try:
            if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
                raise ConnectionFault("invalid_message")
            if (
                not isinstance(params, dict)
                or set(params) != {"tree", "screenshot", "ocr"}
                or any(type(value) is not bool for value in params.values())
                or not any(params.values())
            ):
                raise ConnectionFault("invalid_message")
            if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
                raise ConnectionFault("credential_unavailable")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ConnectionFault("forward_unavailable")
            self._require_current(forward, port, serial)
            request = _post(
                "observe",
                token,
                {
                    "protocol_version": 1,
                    "request_id": self._request_id,
                    "operation": "observe",
                    "timeout_ms": timeout_ms,
                    "params": dict(params),
                },
            )
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000 + 2
            async with asyncio.timeout_at(deadline):
                reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port, limit=_HEADER_BYTES
                )
                self._require_current(forward, port, serial)
                self._writer.write(request)
                self.sent = True
                await self._writer.drain()
                status, headers = await _read_headers(reader)
                if status in {401, 403}:
                    raise ConnectionFault("authentication_failed")
                self.wire_session_id = canonical_uuid(headers.get("x-pulse-session"))
                if status == 200:
                    if (
                        not _content_type(headers, "application/x-ndjson")
                        or "content-length" in headers
                        or "transfer-encoding" in headers
                        or "expect" in headers
                        or headers.get("connection", "close").lower() != "close"
                    ):
                        raise ConnectionFault("invalid_message")
                    raw, trailing = await _read_event(reader)
                    reply = _parse_reply(
                        raw,
                        self._request_id,
                        self.wire_session_id,
                        "observe",
                        keep_observation_result=True,
                    )
                    self.terminal_received = True
                    if trailing:
                        raise ConnectionFault("invalid_message")
                else:
                    if status < 400:
                        raise ConnectionFault("agent_observation_rejected")
                    raw = await reader.readexactly(_json_length(headers))
                    reply = _parse_reply(raw, self._request_id, self.wire_session_id, "observe")
                    if reply.success:
                        raise ConnectionFault("invalid_message")
                    self.terminal_received = True
                if await reader.read(1):
                    raise ConnectionFault("invalid_message")
                self.eof_received = True
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                self._require_current(forward, port, serial)
                return reply
        except asyncio.CancelledError:
            if port is not None:
                await finish_cleanup(self._cancel(forward, token, port, serial))
            raise
        except TimeoutError:
            if port is not None:
                await finish_cleanup(self._cancel(forward, token, port, serial))
            raise ConnectionFault("observation_timeout") from None
        except asyncio.IncompleteReadError:
            self.eof_received = True
            raise ConnectionFault("invalid_message") from None
        except asyncio.LimitOverrunError:
            raise ConnectionFault("response_too_large") from None
        except UnicodeError:
            raise ConnectionFault("invalid_message") from None
        except OSError:
            raise ConnectionFault("agent_unavailable") from None
        except ConnectionFault:
            raise
        except Exception:
            raise ConnectionFault("agent_observation_rejected") from None
        finally:
            writer = self._writer
            if writer is None:
                self.cleanup["connection_closed"] = True
            else:
                closing = asyncio.create_task(
                    _close_writer(writer, asyncio.get_running_loop().time() + 1)
                )
                try:
                    self.cleanup["connection_closed"] = await asyncio.shield(closing)
                except asyncio.CancelledError:
                    self.cleanup["connection_closed"] = await finish_cleanup(closing)
                    raise
                finally:
                    self._writer = None

    async def _cancel(self, forward: ForwardLease, token: str, port: int, serial: str) -> None:
        if (
            self._invalidated
            or self._cancel_attempted
            or not self.sent
            or self.wire_session_id is None
            or self.eof_received
        ):
            return
        self._cancel_attempted = True
        deadline = asyncio.get_running_loop().time() + 1
        writer = None
        try:
            async with asyncio.timeout_at(deadline):
                self._require_current(forward, port, serial)
                await forward.verify()
                self._require_current(forward, port, serial)
                cancel_id = str(uuid4())
                request = _post(
                    "cancel",
                    token,
                    {
                        "protocol_version": 1,
                        "request_id": cancel_id,
                        "operation": "cancel",
                        "timeout_ms": 2000,
                        "params": {
                            "target_session_id": self.wire_session_id,
                            "target_request_id": self._request_id,
                        },
                    },
                )
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, limit=_HEADER_BYTES
                )
                self._cancel_writer = writer
                self._require_current(forward, port, serial)
                writer.write(request)
                await writer.drain()
                status, headers = await _read_headers(reader)
                if status in {401, 403}:
                    raise ConnectionFault("authentication_failed")
                if status != 200:
                    raise ConnectionFault("agent_observation_rejected")
                # Ordinary JSON control responses carry the connection session in the body;
                # only NDJSON stream headers carry X-Pulse-Session.
                raw = await reader.readexactly(_json_length(headers))
                reply = _parse_reply(raw, cancel_id, None, "cancel")
                if reply.wire_session_id == self.wire_session_id:
                    raise ConnectionFault("invalid_message")
                if await reader.read(1):
                    raise ConnectionFault("invalid_message")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                self._require_current(forward, port, serial)
                if not reply.success:
                    self.cleanup["cancel_code"] = reply.error_code
                    return
                if reply.result is None or type(reply.result.get("cancelled")) is not bool:
                    raise ConnectionFault("invalid_message")
                # A correlated acknowledgement is not evidence of platform idleness.
                self.cleanup["cancel_acknowledged"] = reply.result["cancelled"]
        except TimeoutError:
            self.cleanup["cancel_code"] = "timeout"
        except (asyncio.IncompleteReadError, UnicodeError):
            self.cleanup["cancel_code"] = "invalid_message"
        except asyncio.LimitOverrunError:
            self.cleanup["cancel_code"] = "response_too_large"
        except OSError:
            self.cleanup["cancel_code"] = "agent_unavailable"
        except ConnectionFault as exc:
            self.cleanup["cancel_code"] = (
                exc.code if exc.code in _CANCEL_FAULT_CODES else "agent_observation_rejected"
            )
        except Exception:
            self.cleanup["cancel_code"] = "agent_observation_rejected"
        finally:
            if writer is not None:
                await _close_writer(writer, deadline)
                self._cancel_writer = None


class ObserveExchange(OwnedAgentExchange):
    async def run(
        self, forward: ForwardLease, token: str, params: dict[str, bool], timeout_ms: int
    ) -> ObserveReply:
        return await self._observe(forward, token, params, timeout_ms)
