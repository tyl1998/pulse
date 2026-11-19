"""PyInstaller entry point (kept outside the package so it is a plain script)."""

from pulse_runtime.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
