"""Shared capture per device with fan-out to many subscribers (local WS clients, relay publisher).

One ``DeviceStream`` owns at most one scrcpy session per serial. It starts on the first
subscriber, restarts with backoff on failure (device unplugged, server killed), and stops as soon
as the last subscriber leaves (P5.1: no viewer, no capture).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextvars import Context
from dataclasses import dataclass, field
from typing import Any, Protocol

from pulse_observability import (
    DurationStats,
    bind_context,
    current_context,
    new_trace_id,
    redact_text,
)
from pulse_runtime.drivers.android.scrcpy_video import (
    ClockSample,
    ClockSync,
    Packet,
    PacketKind,
    ProtocolError,
    ScrcpyVideoSource,
    SessionMeta,
    SourceError,
    StreamInfo,
    VideoOptions,
    h264_codec_string,
)
from pulse_runtime.stream.envelope import Envelope, encode
from pulse_runtime.stream.frame_queue import FrameQueue

log = logging.getLogger(__name__)

MAX_GOP_CACHE = 600  # frames; bounds memory if the encoder never sends a key frame
MAX_GOP_BYTES = 8 * 1024 * 1024
DEFAULT_QUEUE_FRAMES = 30
BACKOFF_MIN = 0.5
BACKOFF_MAX = 5.0
CLOCK_INTERVAL = 5.0
CLOCK_MAX_AGE_MS = 15_000


class VideoSource(Protocol):
    info: StreamInfo | None

    def events(self) -> AsyncIterator[SessionMeta | Packet]: ...

    async def stop(self) -> None: ...


class Clock(Protocol):
    async def start(self) -> None: ...

    async def measure(self, rounds: int = ...) -> ClockSample: ...

    async def stop(self) -> None: ...


@dataclass(frozen=True)
class Frame:
    kind: PacketKind
    seq: int
    packet: Packet


def frame_message(frame: Frame) -> bytes:
    p = frame.packet
    return encode(
        Envelope(
            frame.kind, frame.seq, p.pts_us, p.recv_ms, float(time.time_ns() // 1_000_000), p.data
        )
    )


@dataclass(eq=False)
class Subscriber:
    queue: FrameQueue[Frame]
    # Diagnostic state snapshots only, never control/results. A stalled viewer cannot grow this
    # indefinitely while the device is offline and retrying.
    texts: deque[str] = field(default_factory=lambda: deque(maxlen=64))
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    sent: int = 0
    subscriber_id: str = field(default_factory=lambda: secrets.token_hex(8))
    trace_id: str = field(default_factory=lambda: new_trace_id(current_context().get("trace_id")))
    started: float = field(default_factory=time.monotonic)

    def metrics_snapshot(self) -> dict[str, int]:
        return {
            "queue_packets": len(self.queue),
            "dropped_frames_total": self.queue.stats.dropped,
            "resyncs_total": self.queue.stats.resyncs,
            "dequeued_packets_total": self.sent,
        }

    def push_text(self, msg: dict[str, Any]) -> None:
        self.texts.append(json.dumps(msg))
        self.wake.set()

    def push_frame(self, frame: Frame) -> None:
        self.queue.push(frame)
        self.wake.set()

    async def messages(self, stats_interval: float = 1.0) -> AsyncIterator[str | bytes]:
        """Yield text (never dropped) before queued frames; emit stats periodically."""
        next_stats = time.monotonic() + stats_interval
        while True:
            if not self.texts and not self.queue.items:
                timeout = max(0.0, next_stats - time.monotonic())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout)
                self.wake.clear()
            if time.monotonic() >= next_stats:
                next_stats = time.monotonic() + stats_interval
                yield json.dumps(
                    {
                        "type": "stats",
                        "dropped": self.queue.stats.dropped,
                        "resyncs": self.queue.stats.resyncs,
                        "queue": len(self.queue),
                        "sent": self.sent,
                    }
                )
            while self.texts:
                yield self.texts.popleft()
            frame = self.queue.pop()
            if frame is not None:
                self.sent += 1
                yield frame_message(frame)


SourceFactory = Callable[[str, VideoOptions], VideoSource]
ClockFactory = Callable[[str], Clock]


def default_source(serial: str, options: VideoOptions) -> VideoSource:
    return ScrcpyVideoSource(serial, options)


def default_clock(serial: str) -> Clock:
    return ClockSync(serial)


class CaptureOrientationConflict(Exception):
    """A different orientation profile still owns this serial's capture lifecycle."""


class DeviceStream:
    def __init__(
        self,
        serial: str,
        options: VideoOptions,
        source_factory: SourceFactory = default_source,
        clock_factory: ClockFactory | None = default_clock,
    ) -> None:
        self.serial = serial
        self.options = options
        self.source_factory = source_factory
        self.clock_factory = clock_factory
        self.subscribers: set[Subscriber] = set()
        self.task: asyncio.Task[None] | None = None
        self.config: Frame | None = None
        self.gop: list[Frame] = []
        self.gop_bytes = 0
        self.status: dict[str, Any] = {"type": "status", "state": "stopped"}
        self.session: dict[str, Any] | None = None
        self.clock: dict[str, Any] | None = None
        self.seq = 0
        self.restarts = 0
        self.trace_id = new_trace_id()
        self.capture_attempts = 0
        self.capture_sessions = 0
        self.frames_received = 0
        self.bytes_received = 0
        self.clock_samples = 0
        self.clock_failures = 0
        self.capture_setup = DurationStats()
        self._attempt_started = time.monotonic()

    def metrics_snapshot(self) -> dict[str, object]:
        clock = self.clock or {}
        at_ms, rtt_us = clock.get("at_ms"), clock.get("rtt_us")
        age = (
            time.time() * 1000 - at_ms
            if isinstance(at_ms, int | float) and math.isfinite(at_ms)
            else None
        )
        valid = (
            clock.get("valid") is True
            and not clock.get("error")
            and age is not None
            and -5 <= age <= CLOCK_MAX_AGE_MS
            and isinstance(rtt_us, int | float)
            and math.isfinite(rtt_us)
            and rtt_us >= 0
        )
        return {
            "capture_attempts_total": self.capture_attempts,
            "capture_restarts_total": self.restarts,
            "capture_sessions_total": self.capture_sessions,
            "frames_received_total": self.frames_received,
            "payload_bytes_received_total": self.bytes_received,
            "subscribers": len(self.subscribers),
            "gop_frames": len(self.gop),
            "gop_bytes": self.gop_bytes,
            "clock_samples_total": self.clock_samples,
            "clock_failures_total": self.clock_failures,
            "clock_valid": valid,
            "clock_age_ms": max(0.0, age) if age is not None else None,
            "clock_rtt_ms": rtt_us / 1000 if valid and isinstance(rtt_us, int | float) else None,
            "capture_setup_ms": self.capture_setup.snapshot(),
        }

    def log_metrics(self) -> None:
        log.info(
            "Capture metrics",
            extra={
                "event": "stream.metrics",
                "scope": "capture",
                "trace_id": self.trace_id,
                "run_id": None,
                "serial": self.serial,
                "session_id": self.session.get("session_id") if self.session else None,
                "channel": None,
                "metrics": self.metrics_snapshot(),
            },
        )
        for sub in self.subscribers:
            log.info(
                "Subscriber metrics",
                extra={
                    "event": "stream.subscriber_metrics",
                    "scope": "subscription",
                    "trace_id": sub.trace_id,
                    "run_id": None,
                    "serial": self.serial,
                    "session_id": self.session.get("session_id") if self.session else None,
                    "channel": None,
                    "capture_trace_id": self.trace_id,
                    "subscriber_id": sub.subscriber_id,
                    "metrics": sub.metrics_snapshot(),
                },
            )

    def hello(self) -> dict[str, Any]:
        return {"type": "hello", "serial": self.serial, "options": self.options.as_dict()}

    def add(self, sub: Subscriber) -> None:
        self.subscribers.add(sub)
        log.info(
            "Subscriber joined",
            extra={
                "event": "stream.subscribed",
                "trace_id": sub.trace_id,
                "serial": self.serial,
                "capture_trace_id": self.trace_id,
                "subscriber_id": sub.subscriber_id,
            },
        )
        self.bootstrap(sub)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._supervise(), context=Context())

    def bootstrap(self, sub: Subscriber) -> None:
        """Start a new transport with a coherent snapshot, not a partially consumed GOP."""
        sub.queue.reset()
        sub.texts.clear()
        sub.push_text(self.hello())
        sub.push_text(self.status)
        if self.session:
            sub.push_text(self.session)
        if self.clock:
            sub.push_text(self.clock)
        if self.config is not None:
            sub.push_frame(self.config)
        # If the cached GOP cannot fit, wait for the next IDR rather than replaying a truncated one.
        if len(self.gop) <= sub.queue.max_frames:
            for f in self.gop:
                sub.push_frame(f)

    async def remove(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            log.info(
                "Subscriber left",
                extra={
                    "event": "stream.unsubscribed",
                    "trace_id": sub.trace_id,
                    "serial": self.serial,
                    "session_id": self.session.get("session_id") if self.session else None,
                    "capture_trace_id": self.trace_id,
                    "subscriber_id": sub.subscriber_id,
                    "duration_ms": (time.monotonic() - sub.started) * 1000,
                    "metrics": sub.metrics_snapshot(),
                },
            )
        self.subscribers.discard(sub)
        if not self.subscribers and self.task is not None:
            task = self.task
            # Repeated cancellation would interrupt the old source's finally cleanup.
            if not task.cancelling():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self.task is task:
                self.task = None
                # A new viewer may join while stop() is cleaning up the old adb forward/server.
                if self.subscribers:
                    self.task = asyncio.create_task(self._supervise(), context=Context())

    def _text(self, msg: dict[str, Any]) -> None:
        for s in self.subscribers:
            s.push_text(msg)

    def _set_status(self, state: str, **extra: Any) -> None:
        self.status = {"type": "status", "state": state, "restarts": self.restarts, **extra}
        log.log(
            logging.WARNING if state == "error" else logging.INFO,
            "Capture state changed",
            extra={
                "event": "stream.state",
                "serial": self.serial,
                "session_id": self.session.get("session_id") if self.session else None,
                "state": state,
                "restarts": self.restarts,
                "details": extra,
            },
        )
        self._text(self.status)

    def _frame(self, packet: Packet) -> None:
        self.frames_received += int(packet.kind != PacketKind.CONFIG)
        self.bytes_received += len(packet.data)
        frame = Frame(packet.kind, self.seq, packet)
        self.seq += 1
        if packet.kind == PacketKind.CONFIG:
            self.config = frame
            self.gop = []
            self.gop_bytes = 0
        elif packet.kind == PacketKind.KEY:
            self.gop = [frame]
            self.gop_bytes = len(packet.data)
        elif (
            self.gop
            and len(self.gop) < MAX_GOP_CACHE
            and self.gop_bytes + len(packet.data) <= MAX_GOP_BYTES
        ):
            self.gop.append(frame)
            self.gop_bytes += len(packet.data)
        else:
            self.gop = []
            self.gop_bytes = 0
        for s in self.subscribers:
            s.push_frame(frame)

    async def _clock_loop(self) -> None:
        if self.clock_factory is None:
            return
        while True:
            clock = self.clock_factory(self.serial)
            try:
                await clock.start()
                while True:
                    sample = await clock.measure()
                    was_valid = self.clock is not None and self.clock.get("valid")
                    self.clock_samples += 1
                    self.clock = {
                        "type": "clock",
                        "valid": True,
                        "offset_us": sample.offset_us,
                        "rtt_us": sample.rtt_us,
                        "at_ms": sample.at_ms,
                        "max_age_ms": CLOCK_MAX_AGE_MS,
                    }
                    self._text(self.clock)
                    if not was_valid:
                        log.info(
                            "Capture clock valid",
                            extra={
                                "event": "clock.valid",
                                "session_id": self.session.get("session_id")
                                if self.session
                                else None,
                                "rtt_ms": sample.rtt_us / 1000,
                            },
                        )
                    await asyncio.sleep(CLOCK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # diagnostics failure invalidates timing, not the video
                self.clock_failures += 1
                log.warning(
                    "Capture clock invalid",
                    exc_info=True,
                    extra={
                        "event": "clock.invalid",
                        "session_id": self.session.get("session_id") if self.session else None,
                        "error_category": "clock",
                    },
                )
                self.clock = {"type": "clock", "valid": False, "error": redact_text(str(exc))}
                self._text(self.clock)
            finally:
                await clock.stop()
            await asyncio.sleep(CLOCK_INTERVAL)

    def _clear_capture(self) -> None:
        self.config, self.gop, self.gop_bytes, self.session, self.clock = None, [], 0, None, None
        for sub in self.subscribers:
            sub.queue.reset()

    async def _run_once(self) -> None:
        source = self.source_factory(self.serial, self.options)
        clock_task: asyncio.Task[None] | None = None
        try:
            async for event in source.events():
                if isinstance(event, SessionMeta):
                    self.capture_sessions += 1
                    self.config, self.gop, self.gop_bytes = None, [], 0
                    for sub in self.subscribers:
                        sub.queue.reset()
                    self.session = {
                        "type": "session",
                        "session_id": secrets.token_hex(8),
                        "width": event.width,
                        "height": event.height,
                        "capture_orientation": self.options.capture_orientation,
                        "device_name": source.info.device_name if source.info else "",
                        "codec": None,
                        "scid": source.scid if isinstance(source, ScrcpyVideoSource) else None,
                    }
                    self._text(self.session)
                    first_session = self.status.get("state") != "streaming"
                    setup_ms = (
                        (time.monotonic() - self._attempt_started) * 1000 if first_session else None
                    )
                    if setup_ms is not None:
                        self.capture_setup.observe(setup_ms)
                    log.info(
                        "Capture session started",
                        extra={
                            "event": "stream.session_started",
                            "session_id": self.session["session_id"],
                            "width": event.width,
                            "height": event.height,
                            "duration_ms": setup_ms,
                        },
                    )
                    if self.status.get("state") != "streaming":
                        self._set_status("streaming")
                    if clock_task is None:
                        clock_task = asyncio.get_running_loop().create_task(self._clock_loop())
                    continue
                if event.kind == PacketKind.CONFIG and self.session is not None:
                    codec = h264_codec_string(event.data)
                    if codec and self.session.get("codec") != codec:
                        self.session = {**self.session, "codec": codec}
                        self._text(self.session)
                self._frame(event)
        finally:
            if clock_task is not None:
                clock_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await clock_task
            await source.stop()

    async def _supervise(self) -> None:
        # Shared capture outlives a viewer and may serve several request traces. It must never
        # inherit the first viewer's run/session/channel, including during cleanup or retries.
        with bind_context(
            trace_id=self.trace_id, run_id=None, serial=self.serial, session_id=None, channel=None
        ):
            try:
                await self._capture_loop()
            finally:
                self._set_status("stopped")
                self.log_metrics()

    async def _capture_loop(self) -> None:
        backoff = BACKOFF_MIN
        while self.subscribers:
            self._clear_capture()
            self._set_status("starting")
            started = time.monotonic()
            self._attempt_started = started
            self.capture_attempts += 1
            try:
                await self._run_once()
                message = "stream ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = redact_text(f"{type(exc).__name__}: {exc}")
                category = (
                    "timeout"
                    if isinstance(exc, TimeoutError)
                    else "protocol"
                    if isinstance(exc, ProtocolError)
                    else "device"
                    if isinstance(exc, SourceError)
                    else "transport"
                    if isinstance(exc, OSError)
                    else "internal"
                )
                log.warning(
                    "Capture attempt failed",
                    exc_info=True,
                    extra={
                        "event": "stream.failed",
                        "session_id": self.session.get("session_id") if self.session else None,
                        "duration_ms": (time.monotonic() - started) * 1000,
                        "error_category": category,
                    },
                )
            self._clear_capture()
            if time.monotonic() - started > 10:
                backoff = BACKOFF_MIN
            self.restarts += 1
            self._set_status("error", message=message, retry_in_ms=int(backoff * 1000))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)


class StreamHub:
    def __init__(
        self,
        source_factory: SourceFactory = default_source,
        clock_factory: ClockFactory | None = default_clock,
    ) -> None:
        self.source_factory = source_factory
        self.clock_factory = clock_factory
        self.streams: dict[str, DeviceStream] = {}

    def check_capture_orientation(self, serial: str, options: VideoOptions) -> None:
        stream = self.streams.get(serial)
        if (
            stream is not None
            and stream.options.capture_orientation != options.capture_orientation
            and (stream.subscribers or (stream.task is not None and not stream.task.done()))
        ):
            raise CaptureOrientationConflict("capture_orientation_conflict")

    def subscribe(
        self, serial: str, options: VideoOptions, *, max_frames: int = DEFAULT_QUEUE_FRAMES
    ) -> tuple[DeviceStream, Subscriber]:
        self.check_capture_orientation(serial, options)
        stream = self.streams.get(serial)
        if stream is None or stream.options.capture_orientation != options.capture_orientation:
            # A stopped profile's cached session/GOP must not bootstrap a different profile.
            stream = DeviceStream(serial, options, self.source_factory, self.clock_factory)
            self.streams[serial] = stream
        elif not stream.subscribers:
            # Reuse the lifecycle slot until old cleanup finishes; never overlap two captures.
            stream.options = options
        sub = Subscriber(FrameQueue(max_frames, wait_for_key=True))
        stream.add(sub)
        return stream, sub

    async def unsubscribe(self, stream: DeviceStream, sub: Subscriber) -> None:
        await stream.remove(sub)
        if not stream.subscribers and self.streams.get(stream.serial) is stream:
            del self.streams[stream.serial]

    async def close(self) -> None:
        for stream in list(self.streams.values()):
            for sub in list(stream.subscribers):
                await stream.remove(sub)
        self.streams.clear()
