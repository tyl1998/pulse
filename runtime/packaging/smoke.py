"""Smoke test for a built Runtime executable (used locally and in CI).

    python runtime/packaging/smoke.py dist/runtime/pulse-runtime/pulse-runtime[.exe]

Starts ``<exe> serve`` with an isolated PULSE_RUNTIME_DIR, waits for runtime.json, checks
/health (with and without token), the WS heartbeat (with and without token), then stops it.
Exits non-zero on any failure. Uses stdlib + websockets only.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

HTTP_TRACE = "1" * 32
WS_TRACE = "2" * 32


def fail(msg: str) -> None:
    raise SystemExit(f"SMOKE FAIL: {msg}")


def get(port: int, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers["X-Trace-ID"] = HTTP_TRACE
    req = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            if resp.headers.get("X-Trace-ID") != HTTP_TRACE:
                fail("HTTP trace header missing")
            body = json.loads(resp.read())
            if body.get("status") != "ok":
                fail(f"unexpected body {body}")
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        if exc.headers.get("X-Trace-ID") != HTTP_TRACE:
            fail("rejected HTTP trace header missing")
        return exc.code


async def ws_check(port: int, token: str) -> None:
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/heartbeat?token={token}&trace_id={WS_TRACE}"
    ) as ws:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if msg.get("type") != "heartbeat":
            fail(f"unexpected ws message {msg}")
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/heartbeat?token=bad") as ws:
            await asyncio.wait_for(ws.recv(), 5)
        fail("ws accepted bad token")
    except websockets.ConnectionClosed as exc:
        if exc.rcvd is None or exc.rcvd.code != 4401:
            fail(f"expected close 4401, got {exc.rcvd}")
    except websockets.InvalidStatus:
        pass


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            **os.environ,
            "PULSE_RUNTIME_DIR": tmp,
            "PULSE_LOG_LEVEL": "DEBUG",
            "PULSE_LOG_STDERR": "0",
            "PULSE_LOG_MAX_BYTES": str(5 * 1024 * 1024),
            "PULSE_LOG_BACKUP_COUNT": "3",
        }
        t0 = time.monotonic()
        proc = subprocess.Popen([str(exe), "serve"], env=env)  # noqa: S603
        try:
            conn = Path(tmp) / "runtime.json"
            while not conn.exists():
                if proc.poll() is not None:
                    fail(f"process exited early rc={proc.returncode}")
                if time.monotonic() - t0 > 60:
                    fail("runtime.json not written within 60s")
                time.sleep(0.1)
            time.sleep(0.2)
            info = json.loads(conn.read_text())
            if os.name != "nt" and (conn.stat().st_mode & 0o777) != 0o600:
                fail(f"runtime.json mode {oct(conn.stat().st_mode & 0o777)}")
            port, token = info["port"], info["token"]
            if get(port, None) != 401 or get(port, "bad") != 401:
                fail("health accessible without valid token")
            if get(port, token) != 200:
                fail("health with token failed")
            asyncio.run(ws_check(port, token))
            print(
                f"SMOKE OK: pid={info['pid']} port={port} version={info['version']} "
                f"startup={time.monotonic() - t0:.2f}s"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=20)
        raw = (Path(tmp) / "runtime.log").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in raw.splitlines()]
        required = {
            "timestamp",
            "level",
            "component",
            "event",
            "trace_id",
            "run_id",
            "serial",
            "session_id",
            "channel",
            "duration_ms",
        }
        if not entries or any(required - entry.keys() for entry in entries):
            fail("structured log fields missing")
        if token in raw or any(entry["run_id"] is not None for entry in entries):
            fail("log exposed credentials or fabricated a run")
        if not any(
            entry["event"] == "ws.opened" and entry["trace_id"] == WS_TRACE for entry in entries
        ):
            fail("WS trace was not logged")
        if not any(entry["logger"] == "uvicorn.error" for entry in entries):
            fail("Uvicorn log sink not connected")
        if any(
            entry["level"] == "DEBUG" and entry["logger"].startswith(("uvicorn", "websockets"))
            for entry in entries
        ):
            fail("transport wire logging was enabled")
        print(
            f"SMOKE LOG OK: {len(entries)} JSON records, trace correlation and redaction verified"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
