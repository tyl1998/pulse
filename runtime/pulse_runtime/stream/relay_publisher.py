"""Push one device stream to a pulse-relay channel (Runtime -> relay leg of the P0.7 path)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from contextvars import Context
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import connect

from pulse_observability import bind_context, current_context, new_trace_id, redact_text
from pulse_runtime.drivers.android.scrcpy_video import VideoOptions
from pulse_runtime.stream.hub import StreamHub

log = logging.getLogger(__name__)


def publish_url(relay_url: str, channel: str, token: str) -> str:
    return f"{relay_url.rstrip('/')}/pub/{quote(channel, safe='')}?token={quote(token, safe='')}"


@dataclass
class RelayPublisher:
    hub: StreamHub
    serial: str
    options: VideoOptions
    relay_url: str
    channel: str
    token: str
    state: dict[str, Any] = field(default_factory=lambda: {"state": "idle"})
    task: asyncio.Task[None] | None = None
    publisher_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    lease_seconds: float = 20.0
    expires_at: float = 0.0
    _renewed: asyncio.Event = field(default_factory=asyncio.Event)
    trace_id: str = field(default_factory=lambda: new_trace_id(current_context().get("trace_id")))
    capture_trace_id: str | None = None
    connect_attempts: int = 0
    errors: int = 0
    sent: int = 0
    binary_bytes_sent: int = 0
    started: float = field(default_factory=time.monotonic)

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "connect_attempts_total": self.connect_attempts,
            "reconnects_total": max(0, self.connect_attempts - 1),
            "errors_total": self.errors,
            "messages_sent_total": self.sent,
            "binary_bytes_sent_total": self.binary_bytes_sent,
        }

    def log_metrics(self) -> None:
        log.info(
            "Publisher metrics",
            extra={
                "event": "publisher.metrics",
                "scope": "publisher",
                "trace_id": self.trace_id,
                "run_id": None,
                "serial": self.serial,
                "session_id": None,
                "channel": self.channel,
                "publisher_id": self.publisher_id,
                "capture_trace_id": self.capture_trace_id,
                "state": self.state.get("state"),
                "metrics": self.metrics_snapshot(),
            },
        )

    def renew(self) -> bool:
        if self.task is not None and self.task.done():
            return False
        self.expires_at = time.monotonic() + self.lease_seconds
        self._renewed.set()
        return True

    def start(self) -> None:
        self.renew()
        self.started = time.monotonic()
        self.task = asyncio.create_task(self._run(), context=Context())

    async def stop(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.state = {"state": "stopped"}
        log.info(
            "Publisher stopped",
            extra={
                "event": "publisher.stopped",
                "trace_id": self.trace_id,
                "run_id": None,
                "serial": self.serial,
                "session_id": None,
                "channel": self.channel,
                "publisher_id": self.publisher_id,
            },
        )

    async def _run(self) -> None:
        with bind_context(
            trace_id=self.trace_id,
            run_id=None,
            serial=self.serial,
            session_id=None,
            channel=self.channel,
        ):
            try:
                await self._run_scoped()
            finally:
                self.log_metrics()

    async def _run_scoped(self) -> None:
        forwarding = asyncio.create_task(self._forward())

        async def lease() -> None:
            while (remaining := self.expires_at - time.monotonic()) > 0:
                self._renewed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._renewed.wait(), remaining)

        expiry = asyncio.create_task(lease())
        try:
            done, _ = await asyncio.wait({forwarding, expiry}, return_when=asyncio.FIRST_COMPLETED)
            if forwarding in done:
                await forwarding
            self.state = {"state": "expired"}
            log.info(
                "Publisher lease expired",
                extra={
                    "event": "publisher.expired",
                    "publisher_id": self.publisher_id,
                    "duration_ms": (time.monotonic() - self.started) * 1000,
                },
            )
        finally:
            forwarding.cancel()
            expiry.cancel()
            await asyncio.gather(forwarding, expiry, return_exceptions=True)

    async def _forward(self) -> None:
        stream, sub = self.hub.subscribe(self.serial, self.options)
        self.capture_trace_id = stream.trace_id
        try:
            while True:
                try:
                    self.connect_attempts += 1
                    url = publish_url(self.relay_url, self.channel, self.token)
                    async with connect(
                        url,
                        max_size=4096,
                        compression=None,
                        open_timeout=5,
                        close_timeout=2,
                        additional_headers={"X-Trace-ID": self.trace_id},
                    ) as ws:
                        self.state = {"state": "connected", "channel": self.channel}
                        log.info(
                            "Publisher connected",
                            extra={
                                "event": "publisher.connected",
                                "publisher_id": self.publisher_id,
                                "capture_trace_id": stream.trace_id,
                            },
                        )
                        stream.bootstrap(sub)
                        async for msg in sub.messages():
                            async with asyncio.timeout(5):
                                await ws.send(msg)
                                self.sent += 1
                                if isinstance(msg, bytes):
                                    self.binary_bytes_sent += len(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.errors += 1
                    self.state = {"state": "error", "message": redact_text(str(exc))}
                    log.warning(
                        "Publisher connection failed",
                        exc_info=True,
                        extra={
                            "event": "publisher.failed",
                            "publisher_id": self.publisher_id,
                            "error_category": "timeout"
                            if isinstance(exc, TimeoutError)
                            else "transport",
                        },
                    )
                    await asyncio.sleep(1.0)
        finally:
            await self.hub.unsubscribe(stream, sub)
