"""User-level runtime directories (per OS) and the connection-info file.

The connection file ``runtime.json`` is the only discovery mechanism for the GUI:
``{port, token, pid, version, started_at}``. It is written atomically with mode 0600.
``PULSE_RUNTIME_DIR`` overrides the location (tests, CI smoke, multiple instances).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

ENV_RUNTIME_DIR = "PULSE_RUNTIME_DIR"
CONNECTION_FILE = "runtime.json"


def runtime_dir(platform: str = sys.platform, env: dict[str, str] | None = None) -> Path:
    """Directory holding runtime.json and logs. Must match apps/desktop runtime-paths.ts."""
    env = dict(os.environ) if env is None else env
    override = env.get(ENV_RUNTIME_DIR)
    if override:
        return Path(override)
    home = Path(env.get("HOME") or env.get("USERPROFILE") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Pulse" / "runtime"
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "Pulse" / "runtime"
    state = env.get("XDG_STATE_HOME") or str(home / ".local" / "state")
    return Path(state) / "pulse" / "runtime"


def connection_file(directory: Path | None = None) -> Path:
    return (directory or runtime_dir()) / CONNECTION_FILE


@dataclass(frozen=True)
class ConnectionInfo:
    port: int
    token: str
    pid: int
    version: str
    started_at: float

    @classmethod
    def from_json(cls, raw: str) -> ConnectionInfo:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("connection file is not an object")
        try:
            return cls(
                port=int(data["port"]),
                token=str(data["token"]),
                pid=int(data["pid"]),
                version=str(data["version"]),
                started_at=float(data["started_at"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid connection file: {exc}") from exc


def ensure_private_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        directory.chmod(0o700)


def write_connection(info: ConnectionInfo, path: Path) -> None:
    """Atomically write runtime.json with 0600 permissions (created 0600, never wider)."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".runtime-", suffix=".json", dir=path.parent)
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_connection(path: Path) -> ConnectionInfo | None:
    try:
        return ConnectionInfo.from_json(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return None


def remove_connection_if_owned(path: Path, pid: int) -> None:
    """Delete runtime.json only if it still belongs to ``pid`` (a newer instance may own it)."""
    current = read_connection(path)
    if current is not None and current.pid == pid:
        path.unlink(missing_ok=True)
