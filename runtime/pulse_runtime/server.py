"""Runtime server process: bind loopback random port, publish runtime.json, serve until signaled."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from pulse_observability import configure_logging
from pulse_runtime import __version__
from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.gate.ledger import ledger_location
from pulse_runtime.paths import (
    ConnectionInfo,
    connection_file,
    ensure_private_dir,
    read_connection,
    remove_connection_if_owned,
    runtime_dir,
    write_connection,
)
from pulse_runtime.probe import probe_health

LOOPBACK = "127.0.0.1"
log = logging.getLogger("pulse_runtime")


def bind_loopback(port: int = 0) -> socket.socket:
    """Bind 127.0.0.1 only; port 0 lets the OS pick a free random port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LOOPBACK, port))
    sock.listen(128)
    sock.set_inheritable(False)
    return sock


def existing_instance(path: Path) -> ConnectionInfo | None:
    """Return the live instance described by runtime.json, if it answers /health."""
    info = read_connection(path)
    if info is None or info.pid == os.getpid():
        return None
    return info if probe_health(info, timeout=1.5) is not None else None


def setup_logging(directory: Path) -> None:
    ensure_private_dir(directory)
    configure_logging("runtime", directory)


class RuntimeServer(uvicorn.Server):
    """uvicorn re-raises SIGTERM/SIGINT after shutdown, so ``finally`` blocks never run.
    Clean up runtime.json at the end of graceful shutdown instead."""

    def __init__(self, config: uvicorn.Config, on_shutdown: Callable[[], None]) -> None:
        super().__init__(config)
        self.on_shutdown = on_shutdown

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().shutdown(sockets)
        finally:
            self.on_shutdown()


def serve(directory: Path | None = None) -> int:
    directory = directory or runtime_dir()
    path = connection_file(directory)

    other = existing_instance(path)
    if other is not None:
        # A duplicate invocation must not rotate or write the live process's file sink.
        configure_logging("runtime", stderr=True)
        log.info(
            "Runtime already running",
            extra={
                "event": "runtime.already_running",
                "runtime_pid": other.pid,
                "port": other.port,
            },
        )
        return 0

    setup_logging(directory)
    token = secrets.token_urlsafe(32)
    sock = bind_loopback()
    port = sock.getsockname()[1]
    info = ConnectionInfo(
        port=port, token=token, pid=os.getpid(), version=__version__, started_at=time.time()
    )
    device_manager = DeviceConnectionManager(ledger_path=ledger_location(directory))
    config = uvicorn.Config(
        create_app(token, device_manager=device_manager),
        log_config=None,
        http="h11",
        ws="websockets-sansio",
        loop="asyncio",
        lifespan="on",
        ws_per_message_deflate=False,
        timeout_graceful_shutdown=10,
        access_log=False,
    )

    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        remove_connection_if_owned(path, info.pid)
        if not cleaned:
            cleaned = True
            log.info("Runtime stopped", extra={"event": "runtime.stopped"})

    server = RuntimeServer(config, cleanup)
    write_connection(info, path)
    log.info(
        "Runtime listening",
        extra={"event": "runtime.started", "version": __version__, "host": LOOPBACK, "port": port},
    )
    try:
        server.run(sockets=[sock])
    finally:
        cleanup()  # idempotent; covers startup failures
        sock.close()
    return 0
