"""Binary envelope for one video packet on the WS (Runtime -> relay -> GUI).

All big-endian, 32-byte header followed by the raw Annex-B payload from MediaCodec:

    off size field
    0   u8   version (=1)
    1   u8   kind: 1 config (SPS/PPS), 2 key frame, 3 delta frame
    2   u16  flags (reserved, 0)
    4   u32  seq (per capture session, starts at 0)
    8   i64  pts_us (device CLOCK_MONOTONIC µs; 0 for config)
    16  f64  rt_recv_ms (host epoch ms: Runtime finished reading the packet from adb)
    24  f64  rt_send_ms (host epoch ms: Runtime handed the message to this WS)

The relay only looks at byte 1 (kind) to drop to the next key frame; it never parses H.264.
Mirror: apps/desktop/src/renderer/shared/stream/envelope.ts and apps/relay/pulse_relay/app.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from pulse_runtime.drivers.android.scrcpy_video import PacketKind

VERSION = 1
HEADER = struct.Struct(">BBHIqdd")
HEADER_SIZE = HEADER.size  # 32


@dataclass(frozen=True)
class Envelope:
    kind: PacketKind
    seq: int
    pts_us: int
    rt_recv_ms: float
    rt_send_ms: float
    payload: bytes


def encode(env: Envelope) -> bytes:
    head = HEADER.pack(
        VERSION, int(env.kind), 0, env.seq & 0xFFFFFFFF, env.pts_us, env.rt_recv_ms, env.rt_send_ms
    )
    return head + env.payload


def decode(data: bytes) -> Envelope:
    if len(data) < HEADER_SIZE:
        raise ValueError("envelope too short")
    version, kind, _flags, seq, pts, recv, send = HEADER.unpack_from(data)
    if version != VERSION:
        raise ValueError(f"unsupported envelope version {version}")
    return Envelope(PacketKind(kind), seq, pts, recv, send, data[HEADER_SIZE:])
