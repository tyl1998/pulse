"""Stdlib-only client helpers to check a running Runtime (used by CLI status and smoke tests)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from pulse_runtime.paths import ConnectionInfo


def probe_health(info: ConnectionInfo, timeout: float = 3.0) -> dict[str, Any] | None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{info.port}/health",
        headers={"Authorization": f"Bearer {info.token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") == "ok":
        return data
    return None
