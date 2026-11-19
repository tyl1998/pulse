import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pulse_runtime import __version__
from pulse_runtime.api.app import create_app
from pulse_runtime.paths import (
    ConnectionInfo,
    read_connection,
    remove_connection_if_owned,
    runtime_dir,
    write_connection,
)
from pulse_runtime.probe import probe_health

TOKEN = "t0k3n"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(TOKEN, heartbeat_interval=0.05))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# ------------------------------------------------------------------ HTTP auth


def test_health_requires_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    res = client.get("/health", headers=auth())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["pid"] == os.getpid()


def test_unknown_routes_also_require_token(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 401


def test_cors_allows_renderer_origins(client: TestClient) -> None:
    for origin in ("http://localhost:5173", "null"):
        res = client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert res.status_code == 200
        assert res.headers["access-control-allow-origin"] == origin


def test_cors_rejects_foreign_origin(client: TestClient) -> None:
    res = client.get("/health", headers={**auth(), "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in res.headers


# ------------------------------------------------------------------ WS


def test_ws_rejects_bad_token(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect("/ws/heartbeat?token=bad") as ws,
    ):
        ws.receive_json()
    assert exc.value.code == 4401


def test_ws_heartbeat_and_ping(client: TestClient) -> None:
    with client.websocket_connect(f"/ws/heartbeat?token={TOKEN}") as ws:
        first = ws.receive_json()
        assert first["type"] == "heartbeat"
        assert first["seq"] == 1
        ws.send_json({"type": "ping", "id": 7})
        seen = [ws.receive_json() for _ in range(4)]
        assert any(m["type"] == "pong" and m["id"] == 7 for m in seen)
        assert any(m["type"] == "heartbeat" and m["seq"] >= 2 for m in seen)


# ------------------------------------------------------------------ paths / connection file


def test_runtime_dir_per_platform() -> None:
    home = {"HOME": "/h"}
    assert runtime_dir("darwin", home) == Path("/h/Library/Application Support/Pulse/runtime")
    assert runtime_dir("linux", home) == Path("/h/.local/state/pulse/runtime")
    assert runtime_dir("linux", {**home, "XDG_STATE_HOME": "/s"}) == Path("/s/pulse/runtime")
    assert runtime_dir("win32", {"USERPROFILE": "/u", "LOCALAPPDATA": "/la"}) == Path(
        "/la/Pulse/runtime"
    )
    assert runtime_dir("linux", {**home, "PULSE_RUNTIME_DIR": "/x"}) == Path("/x")


def test_connection_file_roundtrip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "rt" / "runtime.json"
    info = ConnectionInfo(port=1234, token="abc", pid=42, version="v", started_at=1.0)
    write_connection(info, path)
    assert read_connection(path) == info
    assert json.loads(path.read_text())["token"] == "abc"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(".runtime-*")) == []

    remove_connection_if_owned(path, pid=99)
    assert path.exists()
    remove_connection_if_owned(path, pid=42)
    assert not path.exists()


def test_read_connection_tolerates_garbage(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    assert read_connection(path) is None
    path.write_text("{not json")
    assert read_connection(path) is None
    path.write_text('{"port": 1}')
    assert read_connection(path) is None


# ------------------------------------------------------------------ real process


def test_serve_process_end_to_end(tmp_path: Path) -> None:
    """Spawn `python -m pulse_runtime serve`, read runtime.json, probe /health, stop."""
    env = {**os.environ, "PULSE_RUNTIME_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "pulse_runtime", "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        path = tmp_path / "runtime.json"
        deadline = time.monotonic() + 20
        info = None
        while time.monotonic() < deadline and info is None:
            info = read_connection(path)
            time.sleep(0.1)
        assert info is not None, "runtime.json not written"
        assert info.pid == proc.pid
        health = None
        while time.monotonic() < deadline and health is None:
            health = probe_health(info, timeout=1)
        assert health is not None
        assert health["pid"] == proc.pid
        bad = ConnectionInfo(info.port, "wrong", info.pid, info.version, info.started_at)
        assert probe_health(bad, timeout=1) is None

        # A second instance detects the live one and exits cleanly without clobbering it.
        second = subprocess.run(
            [sys.executable, "-m", "pulse_runtime", "serve"],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert second.returncode == 0
        assert read_connection(path) == info
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    if sys.platform != "win32":  # Windows terminate() is a hard kill: no cleanup runs
        assert not (tmp_path / "runtime.json").exists()
