"""Interruptible Python regex search in a disposable spawned process."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import re
import socket

_SPAWN = multiprocessing.get_context("spawn")


def _worker(channel: socket.socket, pattern: str, text: str) -> None:
    with channel:
        try:
            reply: list[object] = [re.search(pattern, text) is not None]
        except re.error as error:
            reply = ["regex", error.msg, error.pos]
        except (RecursionError, OverflowError) as error:
            reply = [type(error).__name__, str(error)]
        except Exception:
            # Do not let a child traceback expose the input or exception details.
            reply = ["worker_error"]
        channel.sendall(json.dumps(reply).encode("utf-8"))


async def search(pattern: str, text: str, *, timeout: float) -> bool:  # noqa: ASYNC109
    """Return whether ``re.search`` matches, counting spawn/IPC time in the budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    if timeout <= 0:
        raise TimeoutError
    reader, writer = socket.socketpair()
    with reader, writer:
        process = _SPAWN.Process(target=_worker, args=(writer, pattern, text), daemon=True)
        try:
            async with asyncio.timeout_at(deadline):
                process.start()
                writer.close()
                reader.setblocking(False)
                payload = bytearray()
                # Async reads remain interruptible even for a large regex error message.
                while chunk := await loop.sock_recv(reader, 65536):
                    payload.extend(chunk)
                if loop.time() >= deadline:
                    raise TimeoutError
                if not payload:
                    raise RuntimeError("Regex worker exited without a result")
                reply = json.loads(payload)
                if len(reply) == 1 and type(reply[0]) is bool:
                    return reply[0]
                if reply[0] == "regex":
                    raise re.error(reply[1], pattern, reply[2])
                if reply[0] == "RecursionError":
                    raise RecursionError(reply[1])
                if reply[0] == "OverflowError":
                    raise OverflowError(reply[1])
                raise RuntimeError("Regex worker failed")
        finally:
            # Synchronous kill/reap cannot itself be interrupted by repeated task cancellation.
            # The child owns no application resources or graceful-shutdown work.
            if process.pid is not None:
                if process.is_alive():
                    process.kill()
                process.join()
            process.close()
