"""Minimal scrcpy video source (P0.7 spike): video only, no audio, no control.

Pinned server: scrcpy-server v5.0.1 (see ``resources/scrcpy.json``). The wire protocol is parsed
here; the locally installed scrcpy client is never used.

Wire protocol (forward tunnel, video socket only, scrcpy >= 4.0):

1. dummy byte (``0x00``) - lets the client detect a dead forward (connect always succeeds)
2. device meta: 64 bytes, NUL padded UTF-8 device name
3. codec id: u32 BE (``0x68323634`` = "h264")
4. stream of 12-byte headers, each followed by an optional payload:
   - session packet (MSB of byte 0 set): ``u32 flags | u32 width | u32 height``; no payload.
     Sent at every capture session start (rotation, resize).
   - media packet: ``u64 pts_and_flags | u32 size`` then ``size`` bytes.
     bit 62 = config (SPS/PPS, pts is 0), bit 61 = key frame, low 61 bits = PTS in µs.
     For surface input the PTS is the device CLOCK_MONOTONIC (``System.nanoTime()/1000``).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import json
import logging
import os
import secrets
import shutil
import struct
import time
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

RESOURCES = Path(__file__).with_name("resources")
DEVICE_SERVER_PATH = "/data/local/tmp/pulse-scrcpy-server.jar"
DEVICE_CLOCK_PATH = "/data/local/tmp/pulse-clock.jar"

HEADER_SIZE = 12
DEVICE_NAME_LENGTH = 64
CODEC_H264 = 0x68323634
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1
MAX_PACKET_SIZE = 32 * 1024 * 1024


class ProtocolError(Exception):
    pass


class SourceError(Exception):
    """Capture failed (adb missing, device gone, server crashed, ...)."""


# --------------------------------------------------------------------------- pinned resources


@dataclass(frozen=True)
class PinnedFile:
    name: str
    version: str
    sha256: str
    url: str | None

    @property
    def path(self) -> Path:
        return RESOURCES / self.name


def pinned() -> dict[str, PinnedFile]:
    raw = json.loads((RESOURCES / "scrcpy.json").read_text(encoding="utf-8"))
    return {
        key: PinnedFile(v["file"], v["version"], v["sha256"], v.get("url"))
        for key, v in raw.items()
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_resource(key: str) -> PinnedFile:
    item = pinned()[key]
    if not item.path.is_file():
        raise SourceError(f"{item.name} missing; run runtime/packaging/fetch_resources.py")
    actual = sha256_file(item.path)
    if actual != item.sha256:
        raise SourceError(f"{item.name} sha256 mismatch: {actual} != {item.sha256}")
    return item


# --------------------------------------------------------------------------- protocol (pure)


class PacketKind(enum.IntEnum):
    CONFIG = 1
    KEY = 2
    DELTA = 3


@dataclass(frozen=True)
class SessionMeta:
    width: int
    height: int
    client_resized: bool


@dataclass(frozen=True)
class FrameHeader:
    kind: PacketKind
    pts_us: int
    size: int


def parse_header(header: bytes) -> SessionMeta | FrameHeader:
    if len(header) != HEADER_SIZE:
        raise ProtocolError(f"header must be {HEADER_SIZE} bytes, got {len(header)}")
    first, size = struct.unpack(">QI", header)
    if first & FLAG_SESSION:
        flags, width, height = struct.unpack(">III", header)
        if not 0 < width <= 8192 or not 0 < height <= 8192:
            raise ProtocolError(f"invalid video dimensions: {width}x{height}")
        return SessionMeta(width=width, height=height, client_resized=bool(flags & 1))
    if not 0 < size <= MAX_PACKET_SIZE:
        raise ProtocolError(f"invalid packet size: {size}")
    if first & FLAG_CONFIG:
        return FrameHeader(PacketKind.CONFIG, 0, size)
    kind = PacketKind.KEY if first & FLAG_KEY_FRAME else PacketKind.DELTA
    return FrameHeader(kind, first & PTS_MASK, size)


def parse_device_name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex-B byte stream into NAL units (start codes removed)."""
    nals: list[bytes] = []
    i, n, start = 0, len(data), -1
    while i + 2 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start >= 0:
                end = i - 1 if i > 0 and data[i - 1] == 0 else i
                nals.append(data[start:end])
            i += 3
            start = i
        else:
            i += 1
    if start >= 0 and start < n:
        nals.append(data[start:])
    return [nal for nal in nals if nal]


def h264_nal_types(data: bytes) -> list[int]:
    return [nal[0] & 0x1F for nal in split_annexb(data)]


def h264_codec_string(config: bytes) -> str | None:
    """RFC 6381 ``avc1.PPCCLL`` from the SPS in a config packet."""
    for nal in split_annexb(config):
        if nal[0] & 0x1F == 7 and len(nal) >= 4:
            return f"avc1.{nal[1]:02x}{nal[2]:02x}{nal[3]:02x}"
    return None


@dataclass(frozen=True)
class Packet:
    kind: PacketKind
    pts_us: int
    data: bytes
    recv_ms: float  # host epoch ms when the full packet was read from the adb socket


@dataclass(frozen=True)
class StreamInfo:
    device_name: str
    codec_id: int


async def read_handshake(reader: asyncio.StreamReader, *, dummy_read: bool = False) -> StreamInfo:
    if not dummy_read:
        dummy = await reader.readexactly(1)
        if dummy != b"\0":
            raise ProtocolError(f"unexpected dummy byte {dummy!r}")
    name = parse_device_name(await reader.readexactly(DEVICE_NAME_LENGTH))
    (codec_id,) = struct.unpack(">I", await reader.readexactly(4))
    if codec_id in (0, 1):
        raise SourceError("device disabled the video stream" + (" (error)" if codec_id else ""))
    if codec_id != CODEC_H264:
        raise ProtocolError(f"unsupported codec id {codec_id:#x}")
    return StreamInfo(name, codec_id)


async def read_packets(reader: asyncio.StreamReader) -> AsyncGenerator[SessionMeta | Packet]:
    while True:
        try:
            header = await reader.readexactly(HEADER_SIZE)
        except asyncio.IncompleteReadError as exc:
            raise SourceError("video socket closed") from exc
        parsed = parse_header(header)
        if isinstance(parsed, SessionMeta):
            yield parsed
            continue
        data = await reader.readexactly(parsed.size)
        # Match browser Date.now() for cross-process timestamps. Decoder-local durations remain
        # high resolution; mixing Python's realtime clock with performance.timeOrigin drifts.
        yield Packet(parsed.kind, parsed.pts_us, data, float(time.time_ns() // 1_000_000))


# --------------------------------------------------------------------------- options


@dataclass(frozen=True)
class VideoOptions:
    max_size: int = 1080
    video_bit_rate: int = 4_000_000
    max_fps: int = 60
    i_frame_interval: int = 2  # seconds; scrcpy default is 10, which hurts drop-to-IDR recovery
    capture_orientation: Literal["display", "natural"] = "display"

    def __post_init__(self) -> None:
        if not 0 <= self.max_size <= 8192:
            raise ValueError("max_size out of range")
        if not 100_000 <= self.video_bit_rate <= 100_000_000:
            raise ValueError("video_bit_rate out of range")
        if not 0 <= self.max_fps <= 240:
            raise ValueError("max_fps out of range")
        if not 1 <= self.i_frame_interval <= 60:
            raise ValueError("i_frame_interval out of range")
        if self.capture_orientation not in ("display", "natural"):
            raise ValueError("capture_orientation must be display or natural")

    def as_dict(self) -> dict[str, int | str]:
        return {
            "max_size": self.max_size,
            "video_bit_rate": self.video_bit_rate,
            "max_fps": self.max_fps,
            "i_frame_interval": self.i_frame_interval,
            "capture_orientation": self.capture_orientation,
        }


def server_args(version: str, scid: int, opts: VideoOptions) -> list[str]:
    args = [
        f"CLASSPATH={DEVICE_SERVER_PATH}",
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        version,
        f"scid={scid:08x}",
        "log_level=info",
        "video=true",
        "audio=false",
        "control=false",
        "tunnel_forward=true",
        "cleanup=true",
        "video_codec=h264",
        f"max_size={opts.max_size}",
        f"video_bit_rate={opts.video_bit_rate}",
        f"max_fps={opts.max_fps}",
        f"video_codec_options=i-frame-interval:int={opts.i_frame_interval}",
    ]
    if opts.capture_orientation == "natural":
        args.append("capture_orientation=@0")
    return args


# --------------------------------------------------------------------------- adb helpers


def find_adb() -> str:
    candidates = [os.environ.get("PULSE_ADB")]
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            candidates.append(str(Path(os.environ[var]) / "platform-tools" / "adb"))
    candidates.append(shutil.which("adb"))
    candidates.append(str(Path.home() / "Library/Android/sdk/platform-tools/adb"))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise SourceError("adb not found (set PULSE_ADB or ANDROID_HOME)")


async def adb(serial: str, *args: str, timeout: float = 15.0) -> str:  # noqa: ASYNC109
    proc = await asyncio.create_subprocess_exec(
        find_adb(),
        "-s",
        serial,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        raise SourceError(f"adb {' '.join(args)} timed out") from exc
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise SourceError(f"adb {' '.join(args)} failed: {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


async def push_if_changed(serial: str, local: PinnedFile, remote: str) -> None:
    out = await adb(serial, "shell", f"sha256sum {remote} 2>/dev/null || true")
    if out.split(" ", 1)[0].strip() == local.sha256:
        return
    await adb(serial, "push", str(local.path), remote, timeout=60)


# --------------------------------------------------------------------------- clock sync


@dataclass(frozen=True)
class ClockSample:
    offset_us: float  # host_epoch_us = device_monotonic_us + offset_us
    rtt_us: float
    at_ms: float


@dataclass
class ClockSync:
    """NTP-style offset between host wall clock and device CLOCK_MONOTONIC.

    A persistent ``app_process`` (pulse-clock.jar) echoes ``System.nanoTime()/1000`` for each
    stdin line; RTT/2 of the min-RTT sample is a diagnostic uncertainty estimate, not an
    end-to-end error bound.
    """

    serial: str
    proc: asyncio.subprocess.Process | None = None
    last: ClockSample | None = None

    async def start(self) -> None:
        item = verified_resource("clock")
        await push_if_changed(self.serial, item, DEVICE_CLOCK_PATH)
        self.proc = await asyncio.create_subprocess_exec(
            find_adb(),
            "-s",
            self.serial,
            "shell",
            "-T",
            f"CLASSPATH={DEVICE_CLOCK_PATH} app_process / ClockProbe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if self.proc.stdout is None:
            raise SourceError("clock probe has no stdout")
        line = await asyncio.wait_for(self.proc.stdout.readline(), 10)
        if not line.startswith(b"ready"):
            raise SourceError(f"clock probe failed: {line!r}")

    async def measure(self, rounds: int = 15) -> ClockSample:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise SourceError("clock probe not running")
        best: tuple[float, float] | None = None
        for i in range(rounds):
            t0 = time.time()
            proc.stdin.write(f"{i}\n".encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), 5)
            t1 = time.time()
            parts = line.split()
            if len(parts) != 2 or parts[0] != str(i).encode():
                raise SourceError(f"clock probe bad reply {line!r}")
            rtt = (t1 - t0) * 1e6
            offset = (t0 + t1) / 2 * 1e6 - int(parts[1])
            if best is None or rtt < best[0]:
                best = (rtt, offset)
        if best is None:
            raise SourceError("clock probe: no samples")
        self.last = ClockSample(offset_us=best[1], rtt_us=best[0], at_ms=time.time() * 1000)
        return self.last

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 2)


# --------------------------------------------------------------------------- source


@dataclass
class ScrcpyVideoSource:
    """One scrcpy capture session on one device. ``events()`` yields until failure."""

    serial: str
    options: VideoOptions = field(default_factory=VideoOptions)
    connect_timeout: float = 10.0
    scid: int = field(default_factory=lambda: secrets.randbelow(0x7FFFFFFE) + 1)
    port: int | None = None
    info: StreamInfo | None = None
    _server: asyncio.subprocess.Process | None = None
    _writer: asyncio.StreamWriter | None = None
    _stderr_tail: list[str] = field(default_factory=list)
    _output_task: asyncio.Task[None] | None = None

    @property
    def socket_name(self) -> str:
        return f"scrcpy_{self.scid:08x}"

    async def start(self) -> asyncio.StreamReader:
        server = verified_resource("scrcpy-server")
        state = (await adb(self.serial, "get-state", timeout=5)).strip()
        if state != "device":
            raise SourceError(f"device {self.serial} state={state}")
        await push_if_changed(self.serial, server, DEVICE_SERVER_PATH)
        out = await adb(self.serial, "forward", "tcp:0", f"localabstract:{self.socket_name}")
        self.port = int(out.strip())
        self._server = await asyncio.create_subprocess_exec(
            find_adb(),
            "-s",
            self.serial,
            "shell",
            *server_args(server.version, self.scid, self.options),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._output_task = asyncio.create_task(self._drain_server_output())
        async with asyncio.timeout(self.connect_timeout):
            reader = await self._connect()
            self.info = await read_handshake(reader, dummy_read=True)
        log.info(
            "scrcpy source started",
            extra={
                "event": "source.started",
                "serial": self.serial,
                "scid": self.scid,
                "version": server.version,
                "port": self.port,
                "options": self.options.as_dict(),
            },
        )
        return reader

    async def _drain_server_output(self) -> None:
        proc = self._server
        if proc is None or proc.stdout is None:
            return
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            self._stderr_tail = [*self._stderr_tail[-19:], line]
            log.debug(
                "scrcpy diagnostic output",
                extra={"event": "source.diagnostic", "serial": self.serial, "output": line},
            )

    async def _connect(self) -> asyncio.StreamReader:
        # With a forward tunnel the TCP connect succeeds even before the server listens; the
        # dummy byte tells us the device side is really connected.
        deadline = time.monotonic() + self.connect_timeout
        port = self.port
        if port is None:
            raise SourceError("adb forward not set up")
        while True:
            if self._server is not None and self._server.returncode is not None:
                raise SourceError(f"scrcpy server exited: {' | '.join(self._stderr_tail[-3:])}")
            writer = None
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                first = await asyncio.wait_for(reader.read(1), 2)
                if first == b"\0":
                    self._writer = writer
                    return reader
            except (OSError, TimeoutError):
                pass
            finally:
                if writer is not None and writer is not self._writer:
                    writer.close()
            if time.monotonic() > deadline:
                raise SourceError("timed out connecting to scrcpy server")
            await asyncio.sleep(0.1)

    async def events(self) -> AsyncIterator[SessionMeta | Packet]:
        reader = await self.start()

        async def watch_transport() -> None:
            while True:
                if self._server is None or self._server.returncode is not None:
                    raise SourceError("scrcpy server exited")
                # A static screen may legitimately produce no frames; check adb, not frame rate.
                await asyncio.sleep(3)
                if (await adb(self.serial, "get-state", timeout=3)).strip() != "device":
                    raise SourceError("device disconnected")

        watcher = asyncio.create_task(watch_transport())
        packets = read_packets(reader)
        pending: asyncio.Task[SessionMeta | Packet] | None = None
        try:
            while True:
                pending = asyncio.create_task(anext(packets))
                done, _ = await asyncio.wait(
                    {watcher, pending}, return_when=asyncio.FIRST_COMPLETED
                )
                if watcher in done:
                    await watcher
                yield await pending
        finally:
            watcher.cancel()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await asyncio.gather(watcher, return_exceptions=True)
            await packets.aclose()

    async def stop(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(ProcessLookupError):
                server.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait(), 2)
        if self._output_task is not None:
            self._output_task.cancel()
            await asyncio.gather(self._output_task, return_exceptions=True)
            self._output_task = None
        if self.port is not None:
            local = f"tcp:{self.port}"
            expected = [self.serial, local, f"localabstract:{self.socket_name}"]
            try:
                listing = await adb(self.serial, "forward", "--list", timeout=5)
                # A saved port may have been rebound; removal cannot check ownership itself.
                if any(line.split() == expected for line in listing.splitlines()):
                    await adb(self.serial, "forward", "--remove", local, timeout=5)
                    listing = await adb(self.serial, "forward", "--list", timeout=5)
                    if any(line.split() == expected for line in listing.splitlines()):
                        raise SourceError("scrcpy forward still present after removal")
            except Exception as exc:
                log.warning(
                    "scrcpy forward cleanup failed",
                    extra={
                        "event": "source.cleanup_failed",
                        "serial": self.serial,
                        "scid": self.scid,
                        "port": self.port,
                        "error_category": type(exc).__name__,
                    },
                )
            else:
                self.port = None
