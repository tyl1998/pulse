"""Real spawned-worker checks, with no device or Runtime service interaction."""

from __future__ import annotations

import asyncio
import multiprocessing
import re
import socket
import time
from collections.abc import Iterator
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

from pulse_runtime.engine import regex

_ROOT = str(Path(__file__).resolve().parents[2])


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BaseProcess]]:
    created: list[BaseProcess] = []
    channels: list[socket.socket] = []
    original_process = regex._SPAWN.Process
    original_socketpair = socket.socketpair

    def process(**kwargs: object) -> BaseProcess:
        child = original_process(**kwargs)
        created.append(child)
        return child

    def socketpair() -> tuple[socket.socket, socket.socket]:
        pair = original_socketpair()
        channels.extend(pair)
        return pair

    monkeypatch.setattr(regex._SPAWN, "Process", process)
    monkeypatch.setattr(regex.socket, "socketpair", socketpair)
    yield created
    assert all(child._closed for child in created)
    assert not any(child in multiprocessing.active_children() for child in created)
    assert all(channel.fileno() == -1 for channel in channels)


@pytest.mark.parametrize(
    ("pattern", "text"),
    [
        ("needle", "prefix needle suffix"),
        ("needle", "no match"),
        (r"(?<=prefix )\w+", "prefix value"),
        (r"(?P<word>\w+)\s+(?P=word)", "same same"),
        (r"(?P<word>\w+)\s+(?P=word)", "same different"),
        (r"(a)?b(?(1)c|d)", "bd"),
        (r"(?ims)^hello.world$", "HELLO\nWORLD"),
        (r"\w+", "\u00e9"),
        (r"(?a)\w+", "\u00e9"),
        (r"(?>a+)a", "aaa"),
        (r"a++a", "aaa"),
        ("", ""),
    ],
)
async def test_python_search_semantics(pattern: str, text: str, workers: list[BaseProcess]) -> None:
    assert await asyncio.wait_for(regex.search(pattern, text, timeout=3), 5) is (
        re.search(pattern, text) is not None
    )
    assert len(workers) == 1


@pytest.mark.parametrize("pattern", ["[", "(?P<name>x)(?P=missing)", "(\n["])
async def test_invalid_regex_preserves_error(pattern: str, workers: list[BaseProcess]) -> None:
    with pytest.raises(re.error) as expected:
        re.search(pattern, "private text")
    with pytest.raises(re.error) as actual:
        await asyncio.wait_for(regex.search(pattern, "private text", timeout=3), 5)
    assert actual.value.msg == expected.value.msg
    assert actual.value.pattern == expected.value.pattern
    assert actual.value.pos == expected.value.pos
    assert actual.value.lineno == expected.value.lineno
    assert actual.value.colno == expected.value.colno
    assert str(actual.value) == str(expected.value)
    assert len(workers) == 1


async def test_large_error_uses_interruptible_ipc(workers: list[BaseProcess]) -> None:
    pattern = "(?P=" + "name" * 40000 + ")"
    with pytest.raises(re.error) as expected:
        re.search(pattern, "")
    with pytest.raises(re.error) as actual:
        await asyncio.wait_for(regex.search(pattern, "", timeout=3), 5)
    assert actual.value.msg == expected.value.msg
    assert actual.value.pos == expected.value.pos
    assert len(workers) == 1


async def test_catastrophic_backtracking_is_bounded(workers: list[BaseProcess]) -> None:
    heartbeat = asyncio.Event()
    timer = asyncio.get_running_loop().call_later(0.05, heartbeat.set)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(regex.search(r"(a+)+$", "a" * 100 + "!", timeout=0.4), 2)
        assert heartbeat.is_set()
        assert time.monotonic() - started < 1.5
        assert len(workers) == 1
    finally:
        timer.cancel()


@pytest.mark.parametrize("budget", [0.0, -1.0])
async def test_exhausted_budget_does_not_spawn(budget: float, workers: list[BaseProcess]) -> None:
    with pytest.raises(TimeoutError):
        await regex.search("a", "a", timeout=budget)
    assert not workers


def _announced_worker(channel: socket.socket, pattern: str, text: str) -> None:
    channel.sendall(b"ready")
    regex._worker(channel, pattern, text)


async def test_cancellation_kills_and_reaps_own_worker(
    monkeypatch: pytest.MonkeyPatch, workers: list[BaseProcess]
) -> None:
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_recv = loop.sock_recv
    received = bytearray()

    async def recv(channel: socket.socket, size: int) -> bytes:
        chunk = await original_recv(channel, size)
        received.extend(chunk)
        if received.startswith(b"ready"):
            entered.set()
        return chunk

    # pytest's importlib mode does not otherwise expose the test namespace to spawn.
    monkeypatch.syspath_prepend(_ROOT)
    monkeypatch.setattr(regex, "_worker", _announced_worker)
    monkeypatch.setattr(loop, "sock_recv", recv)
    task = asyncio.create_task(regex.search(r"(a+)+$", "a" * 100 + "!", timeout=5))
    try:
        # Readiness comes from the actual child, not a startup delay or busy-spin.
        await asyncio.wait_for(entered.wait(), 3)
        assert len(workers) == 1
        assert workers[0].is_alive()
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert time.monotonic() - started < 1
        assert workers[0]._closed
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)


async def test_start_failure_closes_resources(
    monkeypatch: pytest.MonkeyPatch, workers: list[BaseProcess]
) -> None:
    def fail_start(self: BaseProcess) -> None:
        raise OSError("worker start failed")

    monkeypatch.setattr(BaseProcess, "start", fail_start)
    with pytest.raises(OSError, match="worker start failed"):
        await regex.search("a", "a", timeout=3)
    assert len(workers) == 1


async def test_process_construction_failure_closes_ipc(
    monkeypatch: pytest.MonkeyPatch, workers: list[BaseProcess]
) -> None:
    def fail_process(**kwargs: object) -> BaseProcess:
        raise OSError("worker construction failed")

    monkeypatch.setattr(regex._SPAWN, "Process", fail_process)
    with pytest.raises(OSError, match="worker construction failed"):
        await regex.search("a", "a", timeout=3)
    assert not workers
