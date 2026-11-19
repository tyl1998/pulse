"""ASGI-boundary correlation, without reading headers/bodies into logs or changing auth/CORS."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from starlette.datastructures import Headers, QueryParams
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pulse_observability import DurationStats, bind_context, new_trace_id

log = logging.getLogger(__name__)


@dataclass
class ApiMetrics:
    http_requests_total: int = 0
    http_errors_total: int = 0
    http_active: int = 0
    ws_connections_total: int = 0
    ws_rejected_total: int = 0
    ws_active: int = 0
    http_duration: DurationStats = field(default_factory=DurationStats)
    ws_duration: DurationStats = field(default_factory=DurationStats)

    def snapshot(self) -> dict[str, object]:
        return {
            "http_requests_total": self.http_requests_total,
            "http_errors_total": self.http_errors_total,
            "http_active": self.http_active,
            "ws_connections_total": self.ws_connections_total,
            "ws_rejected_total": self.ws_rejected_total,
            "ws_active": self.ws_active,
            "http_duration_ms": self.http_duration.snapshot(),
            "ws_duration_ms": self.ws_duration.snapshot(),
        }


class RequestTelemetry:
    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope["type"]
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        candidate = Headers(scope=scope).get("x-trace-id")
        if kind == "websocket" and candidate is None:
            candidate = QueryParams(scope.get("query_string", b"")).get("trace_id")
        trace = new_trace_id(candidate)
        scope.setdefault("state", {})["pulse_trace_id"] = trace
        started = time.monotonic()
        status, accepted, close_code = 500, False, None
        error: Exception | None = None
        metrics = self.metrics
        if kind == "http":
            metrics.http_requests_total += 1
            metrics.http_active += 1
        else:
            metrics.ws_connections_total += 1

        def fields() -> dict[str, object]:
            serial = scope.get("path_params", {}).get("serial")
            return {
                "route": getattr(scope.get("route"), "path", "<unmatched>"),
                "serial": serial
                if isinstance(serial, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", serial)
                else None,
            }

        async def observed_send(message: Message) -> None:
            nonlocal status, accepted, close_code
            if message["type"] in {"http.response.start", "websocket.accept"}:
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-trace-id", trace.encode("ascii")),
                    ],
                }
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "websocket.accept":
                accepted = True
                metrics.ws_active += 1
                log.info("WebSocket accepted", extra={"event": "ws.opened", **fields()})
            elif message["type"] == "websocket.close":
                close_code = message.get("code", 1000)
            await send(message)

        async def observed_receive() -> Message:
            nonlocal close_code
            message = await receive()
            if message["type"] == "websocket.disconnect":
                close_code = message.get("code")
            return message

        with bind_context(trace_id=trace, run_id=None, serial=None, session_id=None, channel=None):
            try:
                await self.app(scope, observed_receive, observed_send)
            except Exception as exc:
                error = exc
                raise
            finally:
                duration = (time.monotonic() - started) * 1000
                category = "internal" if error else None
                if isinstance(error, TimeoutError):
                    category = "timeout"
                extra = {**fields(), "duration_ms": duration, "error_category": category}
                if kind == "http":
                    metrics.http_active -= 1
                    metrics.http_errors_total += int(status >= 400 or error is not None)
                    metrics.http_duration.observe(duration)
                    extra.update(event="http.completed", method=scope["method"], status_code=status)
                    if not error and status >= 400:
                        extra["error_category"] = (
                            "authentication"
                            if status == 401
                            else "validation"
                            if status < 500
                            else "internal"
                        )
                    level = (
                        logging.ERROR
                        if error
                        else logging.WARNING
                        if status >= 400
                        else logging.DEBUG
                        if extra["route"] == "/health"
                        else logging.INFO
                    )
                    log.log(level, "HTTP request completed", extra=extra, exc_info=error)
                else:
                    metrics.ws_active -= int(accepted)
                    metrics.ws_rejected_total += int(not accepted)
                    metrics.ws_duration.observe(duration)
                    extra.update(
                        event="ws.closed" if accepted else "ws.rejected", close_code=close_code
                    )
                    if not error and not accepted:
                        extra["error_category"] = (
                            "authentication" if close_code == 4401 else "validation"
                        )
                    log.log(
                        logging.ERROR if error else logging.INFO if accepted else logging.WARNING,
                        "WebSocket finished",
                        extra=extra,
                        exc_info=error,
                    )
