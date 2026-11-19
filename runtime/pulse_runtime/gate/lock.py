"""Single-writer lease for one local ledger file.

A second Runtime process opening the same ledger would run crash recovery against the first
process's live actions. The lease makes that a bounded refusal instead of corrupting state.
The OS releases the lock when the process exits, so no stale epoch or PID check is needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from pulse_runtime.gate.ledger import LedgerError
from pulse_runtime.paths import ensure_private_dir

LOCK_SUFFIX = ".lock"


class LedgerLease:
    """Exclusive ownership of ``<ledger>.lock`` for the lifetime of the object."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: int | None = None
        try:
            ensure_private_dir(path.parent)
            handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise LedgerError("ledger_in_use") from None
        try:
            if sys.platform == "win32":
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(handle)
            raise LedgerError("ledger_in_use") from None
        self.handle = handle

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(handle)
