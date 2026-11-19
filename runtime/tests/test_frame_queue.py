from dataclasses import dataclass

from pulse_runtime.drivers.android.scrcpy_video import PacketKind
from pulse_runtime.stream.frame_queue import FrameQueue

C, K, D = PacketKind.CONFIG, PacketKind.KEY, PacketKind.DELTA


@dataclass(frozen=True)
class F:
    kind: PacketKind
    n: int


def drain(q: FrameQueue[F]) -> list[int]:
    out = []
    while (f := q.pop()) is not None:
        out.append(f.n)
    return out


def test_waits_for_key_frame_first() -> None:
    q: FrameQueue[F] = FrameQueue(4)
    assert q.push(F(C, 0))
    assert not q.push(F(D, 1))
    assert q.push(F(K, 2))
    assert q.push(F(D, 3))
    assert drain(q) == [0, 2, 3]
    assert q.stats.dropped == 1


def test_overflow_drops_until_next_key() -> None:
    q: FrameQueue[F] = FrameQueue(3)
    for i, kind in enumerate([K, D, D]):
        q.push(F(kind, i))
    assert not q.push(F(D, 3))  # overflow, no newer key queued -> drop all, skip
    assert len(q) == 0
    assert not q.push(F(D, 4))  # still skipping: reference lost
    assert q.push(F(K, 5))
    assert q.push(F(D, 6))
    assert drain(q) == [5, 6]
    assert q.stats.dropped == 5


def test_overflow_keeps_latest_queued_key_and_configs() -> None:
    q: FrameQueue[F] = FrameQueue(4)
    for i, kind in enumerate([C, K, D, C, K, D]):
        q.push(F(kind, i))
    assert q.push(F(D, 6))  # drops 1,2 (old GOP), keeps configs and GOP starting at 4
    assert drain(q) == [0, 3, 4, 5, 6]
    assert q.stats.dropped == 2


def test_overflow_with_new_key_replaces_backlog() -> None:
    q: FrameQueue[F] = FrameQueue(2)
    q.push(F(K, 0))
    q.push(F(D, 1))
    assert q.push(F(K, 2))
    assert drain(q) == [2]


def test_never_drops_configs() -> None:
    q: FrameQueue[F] = FrameQueue(1)
    for i in range(5):
        assert q.push(F(C, i))
    assert drain(q) == [0, 1, 2, 3, 4]
