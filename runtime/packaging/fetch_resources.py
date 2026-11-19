"""Download pinned device-side resources (scrcpy-server) and verify sha256.

Usage: uv run python runtime/packaging/fetch_resources.py
Pins live in runtime/pulse_runtime/drivers/android/resources/scrcpy.json.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "pulse_runtime/drivers/android/resources"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url: str, dest: Path) -> None:
    part = dest.with_suffix(".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})  # noqa: S310
    with urllib.request.urlopen(req, timeout=60) as resp, part.open("ab" if have else "wb") as f:  # noqa: S310
        if have and resp.status != 206:
            f.truncate(0)
        while chunk := resp.read(1 << 16):
            f.write(chunk)
            print(f"\r{dest.name}: {f.tell()} bytes", end="", flush=True)
    print()
    part.replace(dest)


def main() -> int:
    pins = json.loads((RES / "scrcpy.json").read_text(encoding="utf-8"))
    for key, pin in pins.items():
        dest = RES / pin["file"]
        if dest.is_file() and sha256(dest) == pin["sha256"]:
            print(f"{key}: ok ({pin['version']})")
            continue
        if not pin.get("url"):
            print(f"{key}: missing or mismatched and has no url", file=sys.stderr)
            return 1
        print(f"{key}: downloading {pin['url']}")
        fetch(pin["url"], dest)
        actual = sha256(dest)
        if actual != pin["sha256"]:
            dest.unlink()
            print(f"{key}: sha256 mismatch {actual}", file=sys.stderr)
            return 1
        print(f"{key}: ok ({pin['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
