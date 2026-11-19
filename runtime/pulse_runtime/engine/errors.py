"""Internal bounded stop shared by interpreter helpers."""

from __future__ import annotations

from pulse_runtime.engine.driver import StopStatus


class EngineStop(Exception):
    def __init__(self, status: StopStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
