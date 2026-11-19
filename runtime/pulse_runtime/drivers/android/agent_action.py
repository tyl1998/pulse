"""One-shot action exchanges borrowing P1.2 forward/credentials and P1.3 owned cleanup."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import cast

from pulse_runtime.drivers.android.actions import KEY_CODES, PACKAGE_PATTERN
from pulse_runtime.drivers.android.agent_observe import (
    _ERROR_CODES,
    _ERROR_PHASES,
    OwnedAgentExchange,
    _close_writer,
    _content_type,
    _finite_float,
    _json_length,
    _post,
    _read_headers,
    _reject_constant,
    _unique_object,
)
from pulse_runtime.drivers.android.agent_transport import (
    AndroidBackend,
    ConnectionFault,
    ForwardLease,
    canonical_uuid,
    complete_mutation,
    finish_cleanup,
)

_RESPONSE_BYTES = 65536
UNCERTAIN_CODES = frozenset(
    {
        "action_uncertain",
        "timeout",
        "cancelled",
        "disconnected",
        "stopped",
        "accessibility_interrupted",
        "accessibility_unavailable",
        "platform_failed",
        "internal_error",
    }
)


@dataclass(frozen=True)
class ActionReply:
    wire_session_id: str | None
    status: str
    error_code: str | None
    completion_source: str | None = None


class ActionExchange(OwnedAgentExchange):
    """Reuse socket ownership/cancel v1, but validate the action's two-frame state machine."""

    def __init__(self, request_id: str) -> None:
        super().__init__(request_id)
        self.dispatched_received = False
        self.terminal_status: str | None = None

    def parse_event(self, raw: bytes, kind: str, *, http_error: bool = False) -> ActionReply:
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            if not isinstance(value, dict) or set(value) != {
                "protocol_version",
                "request_id",
                "session_id",
                "operation",
                "success",
                "terminal",
                "result",
                "error",
            }:
                raise ConnectionFault("action_protocol_invalid")
            session = canonical_uuid(value["session_id"])
            if (
                type(value["protocol_version"]) is not int
                or value["protocol_version"] != 1
                or canonical_uuid(value["request_id"]) != self._request_id
                or value["operation"] != "action"
                or (self.wire_session_id is not None and session != self.wire_session_id)
                or type(value["success"]) is not bool
                or type(value["terminal"]) is not bool
            ):
                raise ConnectionFault("action_protocol_invalid")
            result, error = value["result"], value["error"]
            if value["terminal"] is False:
                if (
                    http_error
                    or self.dispatched_received
                    or value["success"] is not True
                    or result != {"status": "dispatched"}
                    or error is not None
                ):
                    raise ConnectionFault("action_protocol_invalid")
                self.dispatched_received = True
                return ActionReply(session, "dispatched", None)
            if self.terminal_received:
                raise ConnectionFault("action_protocol_invalid")
            completion = None
            if value["success"]:
                completion = (
                    "gesture_callback"
                    if kind in {"tap", "swipe"}
                    else "accessibility_action_return"
                )
                if (
                    http_error
                    or not self.dispatched_received
                    or error is not None
                    or result != {"status": "completed", "completion_source": completion}
                ):
                    raise ConnectionFault("action_protocol_invalid")
                status, code = "completed", None
            else:
                if (
                    not isinstance(error, dict)
                    or set(error) != {"code", "phase", "retryable"}
                    or not isinstance(error["code"], str)
                    or error["code"] not in _ERROR_CODES
                    or not isinstance(error["phase"], str)
                    or error["phase"] not in _ERROR_PHASES
                    or type(error["retryable"]) is not bool
                ):
                    raise ConnectionFault("action_protocol_invalid")
                status = "failed" if self.dispatched_received else "rejected"
                if result != {"status": status} and not (http_error and result is None):
                    raise ConnectionFault("action_protocol_invalid")
                code = error["code"]
            self.terminal_received = True
            self.terminal_status = status
            return ActionReply(session, status, code, completion)
        except (ValueError, UnicodeError, RecursionError, KeyError, TypeError):
            raise ConnectionFault("action_protocol_invalid") from None

    async def run(
        self, forward: ForwardLease, token: str, params: dict[str, object], timeout_ms: int
    ) -> ActionReply:
        if self._started:
            raise ConnectionFault("action_request_reused")
        self._started = True
        port, serial = forward.port, forward.serial
        try:
            if (
                type(timeout_ms) is not int
                or not 100 <= timeout_ms <= 10_000
                or not isinstance(params.get("kind"), str)
                or params["kind"] not in {"tap", "swipe", "set_text", "node_click"}
            ):
                raise ConnectionFault("invalid_message")
            if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
                raise ConnectionFault("credential_unavailable")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ConnectionFault("forward_unavailable")
            self._require_current(forward, port, serial)
            request = _post(
                "action",
                token,
                {
                    "protocol_version": 1,
                    "request_id": self._request_id,
                    "operation": "action",
                    "timeout_ms": timeout_ms,
                    "params": params,
                },
            )
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000 + 2
            async with asyncio.timeout_at(deadline):
                reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port, limit=_RESPONSE_BYTES
                )
                self._require_current(forward, port, serial)
                self._writer.write(request)
                self.sent = True
                await self._writer.drain()
                status, headers = await _read_headers(reader)
                if status in {401, 403}:
                    raise ConnectionFault("authentication_failed")
                if status == 200:
                    self.wire_session_id = canonical_uuid(headers.get("x-pulse-session"))
                    if (
                        not _content_type(headers, "application/x-ndjson")
                        or any(
                            key in headers
                            for key in ("content-length", "transfer-encoding", "expect")
                        )
                        or headers.get("connection", "close").lower() != "close"
                    ):
                        raise ConnectionFault("action_protocol_invalid")
                    size = 0
                    for _ in range(2):
                        raw = await reader.readuntil(b"\n")
                        size += len(raw)
                        if size > _RESPONSE_BYTES:
                            raise ConnectionFault("action_output_limit")
                        if b"\r" in raw:
                            raise ConnectionFault("action_protocol_invalid")
                        reply = self.parse_event(raw[:-1], str(params["kind"]))
                        if self.terminal_received:
                            break
                    if not self.terminal_received:
                        raise ConnectionFault("action_terminal_missing")
                else:
                    if status < 400:
                        raise ConnectionFault("action_protocol_invalid")
                    raw = await reader.readexactly(_json_length(headers))
                    reply = self.parse_event(raw, str(params["kind"]), http_error=True)
                if await reader.read(1):
                    raise ConnectionFault("action_protocol_invalid")
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
            raise ConnectionFault("action_timeout") from None
        except asyncio.IncompleteReadError:
            self.eof_received = True
            raise ConnectionFault("action_terminal_missing") from None
        except asyncio.LimitOverrunError:
            raise ConnectionFault("action_output_limit") from None
        except (UnicodeError, ValueError):
            raise ConnectionFault("action_protocol_invalid") from None
        except OSError:
            raise ConnectionFault("agent_unavailable") from None
        except ConnectionFault:
            raise
        except Exception:
            raise ConnectionFault("action_protocol_invalid") from None
        finally:
            writer = self._writer
            if writer is not None:
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
            else:
                self.cleanup["connection_closed"] = True


class AdbActionExchange(ActionExchange):
    """Fixed Android command knowledge only, never a caller-supplied shell command."""

    def __init__(self, request_id: str, backend: AndroidBackend) -> None:
        super().__init__(request_id)
        self.backend = backend

    async def run(
        self, forward: ForwardLease, token: str, params: dict[str, object], timeout_ms: int
    ) -> ActionReply:
        if self._started or self._invalidated:
            raise ConnectionFault("action_request_reused")
        self._started = True
        kind, package = params["kind"], str(params.get("package", ""))
        args: tuple[str, ...]
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10_000:
            raise ConnectionFault("invalid_message")
        if kind != "key" and (
            type(params.get("package")) is not str
            or len(package) > 255
            or re.fullmatch(PACKAGE_PATTERN, package) is None
            or package == self.backend.build.package
        ):
            raise ConnectionFault("protected_target")
        if kind == "key":
            if type(params.get("code")) is not int or params["code"] not in KEY_CODES:
                raise ConnectionFault("unsupported_action")
            args = ("shell", "input", "keyevent", str(params["code"]))
        elif kind == "launch":
            if (
                type(params.get("activity")) is not str
                or len(cast(str, params["activity"])) > 255
                or re.fullmatch(
                    r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                    cast(str, params["activity"]),
                )
                is None
            ):
                raise ConnectionFault("invalid_message")
            args = (
                "shell",
                "am",
                "start",
                "-W",
                "--user",
                "0",
                "-n",
                f"{package}/{params['activity']}",
            )
        elif kind == "stop":
            args = ("shell", "am", "force-stop", "--user", "0", package)
        elif kind == "clear" and params.get("confirmed") is True:
            args = ("shell", "pm", "clear", "--user", "0", package)
        else:
            raise ConnectionFault("unsupported_action")
        port = forward.port
        if type(port) is not int:
            raise ConnectionFault("forward_unavailable")
        await forward.verify()
        self._require_current(forward, port, forward.serial)
        # This is the one-way submission point. A cancelled issued command must settle within
        # its own bound; its exit/connection close does not prove Android effects were stopped.
        self.sent = self.dispatched_received = True
        try:
            output = await complete_mutation(
                self.backend.command(forward.serial, *args, timeout=min(10.0, timeout_ms / 1000))
            )
            failed = (kind == "clear" and output.strip() != "Success") or (
                kind == "launch"
                and ("Error:" in output or "Exception" in output or "Status: ok" not in output)
            )
            self.terminal_received = self.eof_received = True
            self.terminal_status = "failed" if failed else "completed"
            return ActionReply(
                None,
                self.terminal_status,
                "adb_action_rejected" if failed else None,
                "adb_command_return",
            )
        finally:
            self.cleanup["connection_closed"] = True
