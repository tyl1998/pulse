"""Per-subscriber bounded queue that only drops at key-frame boundaries.

Rules (implementation-plan §7.6):
- config packets (SPS/PPS) are never dropped;
- once any frame is dropped, every following delta is dropped until the next key frame, so the
  decoder never sees a frame whose reference was lost;
- on overflow we keep the newest key frame already queued (and what follows it) when possible.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from pulse_runtime.drivers.android.scrcpy_video import PacketKind


class HasKind(Protocol):
    @property
    def kind(self) -> PacketKind: ...


@dataclass
class DropStats:
    dropped: int = 0
    resyncs: int = 0


class FrameQueue[T: HasKind]:
    def __init__(self, max_frames: int, *, wait_for_key: bool = True) -> None:
        if max_frames < 1:
            raise ValueError("max_frames must be >= 1")
        self.max_frames = max_frames
        self.items: deque[T] = deque()
        self.skipping = wait_for_key
        self.stats = DropStats()

    def __len__(self) -> int:
        return len(self.items)

    def video_count(self) -> int:
        return sum(1 for f in self.items if f.kind != PacketKind.CONFIG)

    def push(self, frame: T) -> bool:
        """Queue ``frame``; return False if it was dropped."""
        if frame.kind == PacketKind.CONFIG:
            self.items.append(frame)
            return True
        if self.skipping:
            if frame.kind != PacketKind.KEY:
                self.stats.dropped += 1
                return False
            self.skipping = False
        if self.video_count() >= self.max_frames:
            if frame.kind == PacketKind.KEY:
                self._drop_video(keep_from=None)
            else:
                last_key = self._last_key_index()
                self._drop_video(keep_from=last_key)
                if last_key is None or self.video_count() >= self.max_frames:
                    self._drop_video(keep_from=None)
                    self.skipping = True
                    self.stats.dropped += 1
                    self.stats.resyncs += 1
                    return False
            self.stats.resyncs += 1
        self.items.append(frame)
        return True

    def pop(self) -> T | None:
        return self.items.popleft() if self.items else None

    def reset(self) -> None:
        """A capture/configuration boundary invalidates all queued references, including SPS/PPS."""
        self.stats.dropped += self.video_count()
        self.items.clear()
        self.skipping = True

    def _last_key_index(self) -> int | None:
        for i in range(len(self.items) - 1, -1, -1):
            if self.items[i].kind == PacketKind.KEY:
                return i
        return None

    def _drop_video(self, keep_from: int | None) -> None:
        kept: deque[T] = deque()
        for i, f in enumerate(self.items):
            if f.kind == PacketKind.CONFIG or (keep_from is not None and i >= keep_from):
                kept.append(f)
            else:
                self.stats.dropped += 1
        self.items = kept
