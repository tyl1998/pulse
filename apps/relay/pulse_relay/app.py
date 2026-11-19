"""Minimal byte relay (P0.7 spike).

Routes, all WebSocket:
- ``/pub/{channel}?token=...``  one publisher per channel (a newer publisher replaces the old one)
- ``/sub/{channel}?token=...``  any number of subscribers

The relay forwards each publisher message unchanged to every subscriber of the channel. It does
not parse video: it reads only the transport envelope kind/sequence to bound per-subscriber
backlogs at key-frame boundaries. Payload bytes are unchanged. A ``relay-frame`` text sidecar
precedes each binary message with relay-local queue time and dropped count; no host clock sync
is needed to measure this duration. JSON metadata is replayed by its generic ``type`` key.

Spike scope: one shared token (``--token`` / ``PULSE_RELAY_TOKEN``). Per-subscription tokens bound
to device+epoch are P5.2.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import struct
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from pulse_observability import (
    DurationStats,
    bind_context,
    configure_logging,
    metrics_interval,
    new_trace_id,
    redact_text,
)

log = logging.getLogger("pulse_relay")

KIND_CONFIG = 1
KIND_KEY = 2
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_REPLACED = 4409
MAX_GOP_CACHE = 600
TEXT_REPLAY = 8
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_QUEUE_MESSAGES = 256

Message = str | bytes


def kind_of(msg: Message) -> int | None:
    """Envelope kind for binary video messages, None for text (control/metadata)."""
    if isinstance(msg, str) or len(msg) < 2:
        return None
    return msg[1]


def parse_route(path: str) -> tuple[str, str, str | None] | None:
    """Return (role, channel, token) for ``/pub/<ch>`` or ``/sub/<ch>``."""
    parts = urlsplit(path)
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) != 2 or segs[0] not in ("pub", "sub") or not segs[1]:
        return None
    token = parse_qs(parts.query).get("token", [None])[0]
    return segs[0], unquote(segs[1]), token


@dataclass(frozen=True)
class Delivery:
    message: Message
    recv_ms: float = field(default_factory=lambda: time.time() * 1000)
    recv_mono: float = field(default_factory=time.monotonic)


class DropQueue:
    """Bounded per-subscriber queue; drops only at key-frame boundaries (mirrors the Runtime)."""

    def __init__(self, max_frames: int) -> None:
        if max_frames < 1:
            raise ValueError("max_frames must be positive")
        self.max_frames = max_frames
        self.items: deque[Delivery] = deque()
        self.skipping = True
        self.dropped = 0
        self.overflow = False

    def _video(self) -> int:
        return sum(1 for m in self.items if kind_of(m.message) not in (None, KIND_CONFIG))

    def reset(self) -> None:
        self.dropped += self._video()
        self.items.clear()
        self.skipping = True

    def push(self, msg: Message | Delivery) -> bool:
        delivery = msg if isinstance(msg, Delivery) else Delivery(msg)
        if self.overflow:
            return False
        kind = kind_of(delivery.message)
        if len(self.items) >= MAX_QUEUE_MESSAGES or len(delivery.message) > MAX_CACHE_BYTES:
            # Non-video messages are not lossy: reject this slow subscriber instead of leaking.
            self.reset()
            self.overflow = True
            return False
        if kind is None or kind == KIND_CONFIG:
            if kind == KIND_CONFIG:
                # A new configuration invalidates the preceding GOP. Keep pending text order.
                self.dropped += self._video()
                self.items = deque(m for m in self.items if isinstance(m.message, str))
                self.skipping = True
            self.items.append(delivery)
            return True
        if self.skipping:
            if kind != KIND_KEY:
                self.dropped += 1
                return False
            self.skipping = False
        if (
            self._video() >= self.max_frames
            or sum(len(m.message) for m in self.items) + len(delivery.message) > MAX_CACHE_BYTES
        ):
            keep_key = kind == KIND_KEY
            kept: deque[Delivery] = deque()
            for m in self.items:
                if kind_of(m.message) in (None, KIND_CONFIG):
                    kept.append(m)
                else:
                    self.dropped += 1
            self.items = kept
            if not keep_key:
                self.skipping = True
                self.dropped += 1
                return False
        self.items.append(delivery)
        return True

    def pop(self) -> Delivery | None:
        return self.items.popleft() if self.items else None


@dataclass(eq=False)
class Sub:
    queue: DropQueue
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    subscriber_id: str = field(default_factory=new_trace_id)
    sent_messages: int = 0
    queue_ms: DurationStats = field(default_factory=DurationStats)
    slow: bool = False

    def push(self, msg: Message | Delivery) -> None:
        self.queue.push(msg)
        self.wake.set()

    def snapshot(self) -> dict[str, object]:
        return {
            "scope": "subscriber",
            "sent_messages": self.sent_messages,
            "queued_messages": len(self.queue.items),
            "dropped": self.queue.dropped,
            "queue_ms": self.queue_ms.snapshot(),
            "slow": self.slow,
        }


@dataclass
class Channel:
    publisher: ServerConnection | None = None
    subs: set[Sub] = field(default_factory=set)
    config: Delivery | None = None
    gop: list[Delivery] = field(default_factory=list)
    gop_bytes: int = 0
    texts: dict[str, Delivery] = field(default_factory=dict)
    received_messages: int = 0
    received_binary_bytes: int = 0
    sent_messages: int = 0
    publisher_replacements: int = 0
    slow_subscribers: int = 0
    retired_dropped: int = 0
    queue_ms: DurationStats = field(default_factory=DurationStats)

    def snapshot(self) -> dict[str, object]:
        return {
            "scope": "channel",
            "received_messages": self.received_messages,
            "received_binary_bytes": self.received_binary_bytes,
            "sent_messages": self.sent_messages,
            "publisher_replacements": self.publisher_replacements,
            "slow_subscribers": self.slow_subscribers,
            "publisher_connected": self.publisher is not None,
            "queued_messages": sum(len(sub.queue.items) for sub in self.subs),
            "dropped": self.retired_dropped + sum(sub.queue.dropped for sub in self.subs),
            "queue_ms": self.queue_ms.snapshot(),
            "subscribers": {sub.subscriber_id: sub.snapshot() for sub in self.subs},
        }

    def clear(self) -> None:
        self.config, self.gop, self.gop_bytes = None, [], 0
        self.texts.clear()
        for sub in self.subs:
            sub.queue.reset()

    def remember(self, delivery: Delivery) -> None:
        msg = delivery.message
        kind = kind_of(msg)
        if isinstance(msg, str):
            try:
                data = json.loads(msg)
                key = str(data.get("type", msg)) if isinstance(data, dict) else msg
            except ValueError:
                key = msg
            # Coalesce metadata for late joiners only. Current viewers still get every message.
            self.texts.pop(key, None)
            self.texts[key] = delivery
            if len(self.texts) > TEXT_REPLAY:
                del self.texts[next(iter(self.texts))]
        elif kind == KIND_CONFIG:
            self.config, self.gop, self.gop_bytes = delivery, [], 0
        elif kind == KIND_KEY:
            self.gop = [delivery] if len(msg) <= MAX_CACHE_BYTES else []
            self.gop_bytes = len(msg) if self.gop else 0
        elif (
            self.gop
            and len(self.gop) < MAX_GOP_CACHE
            and self.gop_bytes + len(msg) <= MAX_CACHE_BYTES
        ):
            self.gop.append(delivery)
            self.gop_bytes += len(msg)
        else:
            self.gop = []
            self.gop_bytes = 0

    def replay(self, sub: Sub) -> None:
        for t in self.texts.values():
            sub.push(t)
        if self.config is not None:
            sub.push(self.config)
        if len(self.gop) <= sub.queue.max_frames:
            for m in self.gop:
                sub.push(m)


class Relay:
    def __init__(self, token: str, *, max_frames: int = 30) -> None:
        self.token = token
        self.max_frames = max_frames
        self.channels: dict[str, Channel] = {}
        self.connections = 0
        self.authentication_failures = 0
        self.validation_failures = 0
        self.received_messages = 0
        self.received_binary_bytes = 0
        self.sent_messages = 0
        self.publisher_replacements = 0
        self.slow_subscribers = 0
        self.retired_dropped = 0
        self.queue_ms = DurationStats()

    def snapshot(self) -> dict[str, object]:
        """Process counters survive channel GC; only live channel/subscriber state is retained."""
        subs = [sub for ch in self.channels.values() for sub in ch.subs]
        return {
            "scope": "process",
            "connections": self.connections,
            "authentication_failures": self.authentication_failures,
            "validation_failures": self.validation_failures,
            "received_messages": self.received_messages,
            "received_binary_bytes": self.received_binary_bytes,
            "sent_messages": self.sent_messages,
            "publisher_replacements": self.publisher_replacements,
            "slow_subscribers": self.slow_subscribers,
            "active_publishers": sum(ch.publisher is not None for ch in self.channels.values()),
            "active_subscribers": len(subs),
            "queued_messages": sum(len(sub.queue.items) for sub in subs),
            "dropped": self.retired_dropped + sum(sub.queue.dropped for sub in subs),
            "queue_ms": self.queue_ms.snapshot(),
            "channels": {name: ch.snapshot() for name, ch in self.channels.items()},
        }

    def channel(self, name: str) -> Channel:
        return self.channels.setdefault(name, Channel())

    def forward(self, name: str, msg: Message) -> None:
        ch = self.channel(name)
        for stats in (self, ch):
            stats.received_messages += 1
            if isinstance(msg, bytes):
                # Includes the transport envelope, not merely the Runtime's video payload.
                stats.received_binary_bytes += len(msg)
        delivery = Delivery(msg)
        ch.remember(delivery)
        for sub in ch.subs:
            sub.push(delivery)

    def add_sub(self, name: str) -> Sub:
        ch = self.channel(name)
        sub = Sub(DropQueue(self.max_frames))
        ch.subs.add(sub)
        sub.push(
            json.dumps(
                {"type": "relay-status", "state": "connected" if ch.publisher else "waiting"}
            )
        )
        ch.replay(sub)
        return sub

    def remove_sub(self, name: str, sub: Sub) -> None:
        ch = self.channels.get(name)
        if ch is None or sub not in ch.subs:
            return
        self.retired_dropped += sub.queue.dropped
        ch.retired_dropped += sub.queue.dropped
        ch.subs.remove(sub)
        self._gc(name, ch)

    def _gc(self, name: str, ch: Channel) -> None:
        if ch.publisher is None and not ch.subs:
            self.channels.pop(name, None)

    async def handler(self, ws: ServerConnection) -> None:
        started = time.monotonic()
        path = ws.request.path if ws.request else ""
        route = parse_route(path)
        candidates = list(ws.request.headers.get_all("X-Trace-ID")) if ws.request else []
        candidates += parse_qs(urlsplit(path).query).get("trace_id", [])
        trace_id = None
        for candidate in candidates:
            normalized = new_trace_id(candidate)
            if normalized == candidate.lower():
                trace_id = normalized
                break
        with bind_context(
            trace_id=trace_id or new_trace_id(),
            run_id=None,
            serial=None,
            session_id=None,
            channel=route[1] if route else None,
        ):
            self.connections += 1
            extra = {"connection_id": str(ws.id), "role": route[0] if route else None}
            try:
                if route is None:
                    self.validation_failures += 1
                    log.warning(
                        "relay connection rejected",
                        extra={
                            **extra,
                            "event": "relay.rejected",
                            "error_category": "validation",
                            "close_code": CLOSE_NOT_FOUND,
                            "duration_ms": (time.monotonic() - started) * 1000,
                        },
                    )
                    await ws.close(CLOSE_NOT_FOUND, "unknown route")
                    return
                role, name, token = route
                if token is None or not hmac.compare_digest(token.encode(), self.token.encode()):
                    self.authentication_failures += 1
                    log.warning(
                        "relay connection rejected",
                        extra={
                            **extra,
                            "event": "relay.rejected",
                            "error_category": "authentication",
                            "close_code": CLOSE_UNAUTHORIZED,
                            "duration_ms": (time.monotonic() - started) * 1000,
                        },
                    )
                    await ws.close(CLOSE_UNAUTHORIZED, "unauthorized")
                    return
                if role == "pub":
                    await self._publish(ws, name)
                else:
                    await self._subscribe(ws, name)
            except Exception as exc:
                category = (
                    "timeout"
                    if isinstance(exc, TimeoutError)
                    else "transport"
                    if isinstance(exc, (OSError, ConnectionClosed))
                    else "internal"
                )
                log.error(
                    "relay connection failed",
                    extra={
                        **extra,
                        "event": "relay.error",
                        "error_category": category,
                        "error": redact_text(str(exc)),
                    },
                    exc_info=True,
                )
                raise

    async def _publish(self, ws: ServerConnection, name: str) -> None:
        started = time.monotonic()
        received_messages = received_binary_bytes = 0
        extra = {"role": "pub", "connection_id": str(ws.id)}
        ch = self.channel(name)
        old, ch.publisher = ch.publisher, ws
        ch.clear()
        for sub in ch.subs:
            sub.push(json.dumps({"type": "relay-status", "state": "connected"}))
        log.info("relay publisher connected", extra={**extra, "event": "relay.connected"})
        try:
            if old is not None:
                self.publisher_replacements += 1
                ch.publisher_replacements += 1
                log.info(
                    "relay publisher replaced",
                    extra={
                        **extra,
                        "event": "relay.replaced",
                        "replaced_connection_id": str(old.id),
                    },
                )
                await old.close(CLOSE_REPLACED, "replaced by a newer publisher")
            async for msg in ws:
                if ch.publisher is not ws:
                    break
                received_messages += 1
                if isinstance(msg, bytes):
                    received_binary_bytes += len(msg)
                self.forward(name, msg)
        except ConnectionClosed as exc:
            if ch.publisher is ws and isinstance(exc, ConnectionClosedError):
                log.warning(
                    "relay publisher transport failed",
                    extra={
                        **extra,
                        "event": "relay.error",
                        "error_category": "transport",
                        "error": redact_text(str(exc)),
                    },
                    exc_info=True,
                )
        finally:
            replaced = ch.publisher is not ws
            if ch.publisher is ws:
                ch.publisher = None
                ch.clear()
                for sub in ch.subs:
                    sub.push(json.dumps({"type": "relay-status", "state": "disconnected"}))
                self._gc(name, ch)
            log.info(
                "relay publisher disconnected",
                extra={
                    **extra,
                    "event": "relay.disconnected",
                    "reason": "replaced" if replaced else "closed",
                    "close_code": ws.close_code,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "metrics": {
                        "scope": "connection",
                        "received_messages": received_messages,
                        "received_binary_bytes": received_binary_bytes,
                    },
                },
            )

    async def _subscribe(self, ws: ServerConnection, name: str) -> None:
        started = time.monotonic()
        sub = self.add_sub(name)
        ch = self.channels[name]
        extra = {
            "role": "sub",
            "connection_id": str(ws.id),
            "subscriber_id": sub.subscriber_id,
        }
        log.info("relay subscriber connected", extra={**extra, "event": "relay.connected"})

        async def send(msg: Message) -> None:
            await ws.send(msg)
            for stats in (self, ch, sub):
                stats.sent_messages += 1

        def slow_subscriber(reason: str, *, exc_info: bool = False) -> None:
            if not sub.slow:
                sub.slow = True
                self.slow_subscribers += 1
                ch.slow_subscribers += 1
            log.warning(
                "relay subscriber is too slow",
                extra={
                    **extra,
                    "event": "relay.slow_subscriber",
                    "reason": reason,
                    "error_category": "timeout" if exc_info else "transport",
                    "metrics": sub.snapshot(),
                },
                exc_info=exc_info,
            )

        async def drain_incoming() -> None:
            async for _ in ws:  # subscribers have nothing to say; detect close
                pass

        async def send_messages() -> None:
            while True:
                if sub.queue.overflow:
                    slow_subscriber("backlog_limit")
                    await ws.close(1013, "subscriber backlog limit")
                    return
                delivery = sub.queue.pop()
                if delivery is None:
                    sub.wake.clear()
                    try:
                        await asyncio.wait_for(sub.wake.wait(), 1)
                    except TimeoutError:
                        async with asyncio.timeout(5):
                            await send(
                                json.dumps(
                                    {"type": "relay-heartbeat", "dropped": sub.queue.dropped}
                                )
                            )
                    continue
                msg = delivery.message
                async with asyncio.timeout(5):
                    if isinstance(msg, bytes) and len(msg) >= 8:
                        # Read the transport sequence only, never NAL/codec data. Timestamp the
                        # start of the send, not completion or physical display presentation.
                        queue_ms = (time.monotonic() - delivery.recv_mono) * 1000
                        await send(
                            json.dumps(
                                {
                                    "type": "relay-frame",
                                    "seq": struct.unpack_from(">I", msg, 4)[0],
                                    "recv_ms": delivery.recv_ms,
                                    "send_ms": time.time() * 1000,
                                    "queue_ms": queue_ms,
                                    "dropped": sub.queue.dropped,
                                }
                            )
                        )
                        for stats in (self, ch, sub):
                            stats.queue_ms.observe(queue_ms)
                    await send(msg)

        reader = asyncio.create_task(drain_incoming())
        sender = asyncio.create_task(send_messages())
        try:
            done, _ = await asyncio.wait({reader, sender}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except ConnectionClosed as exc:
            if not sub.slow and isinstance(exc, ConnectionClosedError):
                log.warning(
                    "relay subscriber transport failed",
                    extra={
                        **extra,
                        "event": "relay.error",
                        "error_category": "transport",
                        "error": redact_text(str(exc)),
                    },
                    exc_info=True,
                )
        except TimeoutError:
            slow_subscriber("send_timeout", exc_info=True)
        finally:
            reader.cancel()
            sender.cancel()
            await asyncio.gather(reader, sender, return_exceptions=True)
            self.remove_sub(name, sub)
            try:
                await ws.close()
            finally:
                log.info(
                    "relay subscriber disconnected",
                    extra={
                        **extra,
                        "event": "relay.disconnected",
                        "close_code": ws.close_code,
                        "duration_ms": (time.monotonic() - started) * 1000,
                        "metrics": sub.snapshot(),
                    },
                )


async def start(relay: Relay, host: str, port: int) -> Server:
    return await serve(
        relay.handler,
        host,
        port,
        max_size=MAX_CACHE_BYTES,
        max_queue=4,
        compression=None,
        close_timeout=2,
        ping_interval=5,
        ping_timeout=5,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulse-relay")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - relay is meant to be reachable
    parser.add_argument("--port", type=int, default=7788)
    parser.add_argument("--token", default=os.environ.get("PULSE_RELAY_TOKEN"))
    parser.add_argument("--max-frames", type=int, default=30)
    parser.add_argument(
        "--log-dir", type=Path, default=os.environ.get("PULSE_RELAY_LOG_DIR") or None
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.token:
        parser.error("--token or PULSE_RELAY_TOKEN is required")
    configure_logging("relay", directory=args.log_dir)

    async def run() -> None:
        with bind_context(trace_id=None, run_id=None, serial=None, session_id=None, channel=None):
            interval = metrics_interval()
            relay = Relay(args.token, max_frames=args.max_frames)
            server = await start(relay, args.host, args.port)

            async def report_metrics() -> None:
                while True:
                    await asyncio.sleep(interval)
                    log.info(
                        "relay metrics",
                        extra={"event": "relay.metrics", "metrics": relay.snapshot()},
                    )

            reporter = asyncio.create_task(report_metrics())
            try:
                log.info(
                    "relay listening",
                    extra={"event": "relay.listening", "host": args.host, "port": args.port},
                )
                await server.serve_forever()
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
                server.close()
                await server.wait_closed()
                log.info(
                    "relay metrics",
                    extra={
                        "event": "relay.metrics",
                        "reason": "shutdown",
                        "metrics": relay.snapshot(),
                    },
                )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
