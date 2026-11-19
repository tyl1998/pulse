"""Durable local action/control ledger (SQLite, WAL, single Runtime directory).

The ledger records intent before a device submission, then every validated transition.
A process crash can therefore leave ``dispatched``/``acknowledged`` side-effect actions
visible and blocks them as ``uncertain`` on the next startup. It never replays an action.

SQLite is local disk only. Never point this at a network filesystem (D05).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from pulse_runtime.paths import ensure_private_dir, runtime_dir

ENV_LEDGER_PATH = "PULSE_LEDGER_PATH"
LEDGER_FILE = "ledger.sqlite3"
ACTION_STATES = frozenset(
    {"prepared", "dispatched", "acknowledged", "verified", "failed", "uncertain"}
)
FINAL_STATES = frozenset({"verified", "failed", "uncertain"})
RUN_STATES = frozenset({"active", "stopping", "interrupted", "canceled", "finished"})


class LedgerError(RuntimeError):
    """A bounded local persistence failure or an illegal transition."""


def ledger_location(directory: Path | None = None) -> Path:
    """Resolve the ledger path without creating anything.

    ``PULSE_LEDGER_PATH`` is for tests and isolated acceptance runtimes. Otherwise the
    ledger lives beside ``runtime.json`` so a crash-restart uses the same durable state.
    """
    override = os.environ.get(ENV_LEDGER_PATH)
    if override:
        return Path(override)
    return (directory or runtime_dir()) / LEDGER_FILE


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _private_file(path: Path) -> None:
    ensure_private_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o600)


def _private_sidecars(path: Path) -> None:
    if os.name == "nt":
        return
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.chmod(0o600)


def _require(value: object) -> sqlite3.Row:
    if value is None:
        raise LedgerError("control_row_missing")
    return cast(sqlite3.Row, value)


def _row(value: sqlite3.Row) -> dict[str, object]:
    record = dict(value)
    if "side_effect" in record:
        record["side_effect"] = bool(record["side_effect"])
    if "recovery_required" in record:
        record["recovery_required"] = bool(record["recovery_required"])
    return record


class ActionLedger:
    """One SQLite connection per Runtime process. All calls are short and process-local."""

    def __init__(self, path: Path | str | None = None, *, recover: bool = True) -> None:
        self._lock = threading.RLock()
        self._closed = False
        self._path: Path | None = None
        if path is None or str(path) == ":memory:":
            target = ":memory:"
        else:
            self._path = Path(path)
            _private_file(self._path)
            target = str(self._path)
        try:
            self._conn = sqlite3.connect(
                target, timeout=5.0, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            raise LedgerError("ledger_open_failed") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            if self._path is not None:
                mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if mode is None or str(mode[0]).lower() != "wal":
                    raise LedgerError("ledger_wal_unavailable")
                self._conn.execute("PRAGMA synchronous=FULL")
            self._migrate()
            if recover:
                self.recover_interrupted()
            if self._path is not None:
                _private_sidecars(self._path)
        except sqlite3.Error as exc:
            # A file that is not a SQLite database is unusable, never a crash-recovery case.
            self._conn.close()
            raise LedgerError("ledger_unusable") from exc
        except BaseException:
            self._conn.close()
            raise

    @property
    def path(self) -> Path | None:
        return self._path

    def _migrate(self) -> None:
        with self._lock:
            if self._closed:
                raise LedgerError("ledger_closed")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    step_id TEXT,
                    control_epoch INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    side_effect INTEGER NOT NULL,
                    target_observation_id TEXT,
                    expected_package TEXT,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    post_observation_available INTEGER,
                    agent_lifecycle_id TEXT,
                    agent_pid INTEGER,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    dispatched_at_ms INTEGER,
                    acknowledged_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    recovered_at_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS actions_device_created
                    ON actions(device_id, created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS actions_unsettled
                    ON actions(state, side_effect);
                CREATE TABLE IF NOT EXISTS control_state (
                    device_id TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL,
                    holder_source TEXT,
                    run_id TEXT,
                    session_id TEXT,
                    recovery_required INTEGER NOT NULL DEFAULT 0,
                    recovery_action_id TEXT,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    control_epoch INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_device_state ON runs(device_id, state);
                CREATE TABLE IF NOT EXISTS control_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT,
                    created_at_ms INTEGER NOT NULL
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise LedgerError("ledger_closed")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise LedgerError("ledger_busy") from exc
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._path is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
                if self._path is not None:
                    _private_sidecars(self._path)
            except sqlite3.Error:
                pass

    def __enter__(self) -> ActionLedger:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- actions

    def prepare(
        self,
        *,
        action_id: str,
        device_id: str,
        session_id: str,
        run_id: str | None,
        step_id: str | None,
        control_epoch: int,
        source: str,
        kind: str,
        side_effect: bool,
        target_observation_id: str | None,
        expected_package: str | None,
        agent_lifecycle_id: str | None,
        agent_pid: int | None,
    ) -> bool:
        """Persist intent before any device submission. False means the id already exists."""
        now = _now_ms()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO actions (
                    action_id, device_id, session_id, run_id, step_id, control_epoch,
                    source, kind, side_effect, target_observation_id, expected_package,
                    state, agent_lifecycle_id, agent_pid, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?)
                """,
                (
                    action_id,
                    device_id,
                    session_id,
                    run_id,
                    step_id,
                    control_epoch,
                    source,
                    kind,
                    int(side_effect),
                    target_observation_id,
                    expected_package,
                    agent_lifecycle_id,
                    agent_pid,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def mark_dispatched(
        self, action_id: str, *, agent_lifecycle_id: str | None, agent_pid: int | None
    ) -> None:
        now = _now_ms()
        with self._transaction() as conn:
            current = self._state(conn, action_id)
            if current == "dispatched":
                return
            if current != "prepared":
                raise LedgerError("invalid_transition")
            updated = conn.execute(
                "UPDATE actions SET state = 'dispatched', updated_at_ms = ?, "
                "dispatched_at_ms = ?, agent_lifecycle_id = ?, agent_pid = ? "
                "WHERE action_id = ? AND state = 'prepared'",
                (now, now, agent_lifecycle_id, agent_pid, action_id),
            )
            if updated.rowcount != 1:
                raise LedgerError("invalid_transition")

    def mark_acknowledged(self, action_id: str, *, error_code: str | None) -> None:
        now = _now_ms()
        with self._transaction() as conn:
            current = self._state(conn, action_id)
            if current == "acknowledged":
                return
            if current != "dispatched":
                raise LedgerError("invalid_transition")
            updated = conn.execute(
                "UPDATE actions SET state = 'acknowledged', updated_at_ms = ?, "
                "acknowledged_at_ms = ?, error_code = ? "
                "WHERE action_id = ? AND state = 'dispatched'",
                (now, now, error_code, action_id),
            )
            if updated.rowcount != 1:
                raise LedgerError("invalid_transition")

    def finish(
        self,
        action_id: str,
        state: Literal["verified", "failed", "uncertain"],
        *,
        error_code: str | None = None,
        post_observation_available: bool | None = None,
    ) -> None:
        if state not in FINAL_STATES:
            raise LedgerError("invalid_final_state")
        now = _now_ms()
        with self._transaction() as conn:
            current = self._state(conn, action_id)
            if current == state:
                return
            if current not in {"prepared", "dispatched", "acknowledged"}:
                raise LedgerError("invalid_transition")
            updated = conn.execute(
                "UPDATE actions SET state = ?, updated_at_ms = ?, finished_at_ms = ?, "
                "error_code = ?, post_observation_available = ? WHERE action_id = ? "
                "AND state IN ('prepared', 'dispatched', 'acknowledged')",
                (
                    state,
                    now,
                    now,
                    error_code,
                    None if post_observation_available is None else int(post_observation_available),
                    action_id,
                ),
            )
            if updated.rowcount != 1:
                raise LedgerError("invalid_transition")

            if state == "uncertain":
                action = _require(
                    conn.execute(
                        "SELECT device_id, run_id FROM actions WHERE action_id = ?", (action_id,)
                    ).fetchone()
                )
                self._ensure_control(conn, action["device_id"], now)
                conn.execute(
                    "UPDATE control_state SET recovery_required = 1, recovery_action_id = ?, "
                    "updated_at_ms = ? WHERE device_id = ?",
                    (action_id, now, action["device_id"]),
                )
                conn.execute(
                    "UPDATE runs SET state = 'interrupted', updated_at_ms = ? WHERE run_id = ?",
                    (now, action["run_id"]),
                )

    @staticmethod
    def _state(conn: sqlite3.Connection, action_id: str) -> str:
        row = conn.execute("SELECT state FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise LedgerError("action_not_found")
        return str(row["state"])

    def action(self, action_id: str) -> dict[str, object] | None:
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT * FROM actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return None if row is None else _row(row)

    def list_actions(self, device_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 200:
            raise LedgerError("invalid_limit")
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE device_id = ? ORDER BY created_at_ms DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def interrupted_actions(self, device_id: str) -> list[dict[str, object]]:
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE device_id = ? AND state = 'uncertain' "
                "ORDER BY created_at_ms DESC",
                (device_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def recover_interrupted(self) -> list[str]:
        """Mark crash-left side-effect submissions uncertain and latch their devices.

        Only actions that reached ``dispatched`` or ``acknowledged`` may have touched the
        device. ``prepared`` actions were never submitted and are closed as failed.
        """
        now = _now_ms()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT action_id, device_id, run_id FROM actions "
                "WHERE side_effect = 1 AND state IN ('dispatched', 'acknowledged')"
            ).fetchall()
            devices = sorted({cast(str, row["device_id"]) for row in rows})
            for row in rows:
                conn.execute(
                    "UPDATE actions SET state = 'uncertain', error_code = ?, "
                    "updated_at_ms = ?, finished_at_ms = ?, recovered_at_ms = ? "
                    "WHERE action_id = ?",
                    ("runtime_crashed", now, now, now, row["action_id"]),
                )
            conn.execute(
                "UPDATE actions SET state = 'failed', error_code = ?, updated_at_ms = ?, "
                "finished_at_ms = ? WHERE side_effect = 1 AND state = 'prepared'",
                ("runtime_crashed_before_dispatch", now, now),
            )
            for device in devices:
                self._ensure_control(conn, device, now)
                first = next(row["action_id"] for row in rows if row["device_id"] == device)
                conn.execute(
                    "UPDATE control_state SET recovery_required = 1, recovery_action_id = ?, "
                    "updated_at_ms = ? WHERE device_id = ?",
                    (first, now, device),
                )
                conn.execute(
                    "UPDATE runs SET state = 'interrupted', updated_at_ms = ? "
                    "WHERE device_id = ? AND state IN ('active', 'stopping')",
                    (now, device),
                )
                conn.execute(
                    "INSERT INTO control_events (device_id, kind, detail, created_at_ms) "
                    "VALUES (?, 'ledger.recovered', ?, ?)",
                    (device, f"uncertain={len(rows)}", now),
                )
        return devices

    # ---------------------------------------------------------------- control

    def ensure_control(self, device_id: str) -> dict[str, object]:
        now = _now_ms()
        with self._transaction() as conn:
            row = self._ensure_control(conn, device_id, now)
        return _row(row) if row is not None else self.control_state(device_id) or {}

    @staticmethod
    def _ensure_control(conn: sqlite3.Connection, device_id: str, now: int) -> sqlite3.Row:
        conn.execute(
            "INSERT OR IGNORE INTO control_state (device_id, epoch, updated_at_ms) "
            "VALUES (?, 0, ?)",
            (device_id, now),
        )
        return _require(
            conn.execute("SELECT * FROM control_state WHERE device_id = ?", (device_id,)).fetchone()
        )

    def control_state(self, device_id: str) -> dict[str, object] | None:
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT * FROM control_state WHERE device_id = ?", (device_id,)
            ).fetchone()
        return None if row is None else _row(row)

    def update_control(
        self,
        device_id: str,
        *,
        epoch: int | None = None,
        holder_source: str | Literal["keep"] | None = "keep",
        run_id: str | Literal["keep"] | None = "keep",
        session_id: str | Literal["keep"] | None = "keep",
        recovery_required: bool | None = None,
        recovery_action_id: str | Literal["keep"] | None = "keep",
        event: str | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        now = _now_ms()
        with self._transaction() as conn:
            self._ensure_control(conn, device_id, now)
            current = _require(
                conn.execute(
                    "SELECT * FROM control_state WHERE device_id = ?", (device_id,)
                ).fetchone()
            )
            next_epoch = cast(int, current["epoch"]) if epoch is None else epoch
            next_holder = current["holder_source"] if holder_source == "keep" else holder_source
            next_run = current["run_id"] if run_id == "keep" else run_id
            next_session = current["session_id"] if session_id == "keep" else session_id
            next_recovery = (
                bool(current["recovery_required"])
                if recovery_required is None
                else recovery_required
            )
            next_action = (
                current["recovery_action_id"]
                if recovery_action_id == "keep"
                else recovery_action_id
            )
            conn.execute(
                "UPDATE control_state SET epoch = ?, holder_source = ?, run_id = ?, "
                "session_id = ?, recovery_required = ?, recovery_action_id = ?, "
                "updated_at_ms = ? WHERE device_id = ?",
                (
                    next_epoch,
                    next_holder,
                    next_run,
                    next_session,
                    int(next_recovery),
                    next_action,
                    now,
                    device_id,
                ),
            )
            if event is not None:
                conn.execute(
                    "INSERT INTO control_events (device_id, kind, detail, created_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (device_id, event, detail, now),
                )
            row = _require(
                conn.execute(
                    "SELECT * FROM control_state WHERE device_id = ?", (device_id,)
                ).fetchone()
            )
            return _row(row)

    def begin_takeover(
        self,
        device_id: str,
        *,
        session_id: str,
        source: str,
        run_id: str | None,
    ) -> dict[str, object]:
        """Increment epoch atomically and return the previous holder/run."""
        now = _now_ms()
        with self._transaction() as conn:
            self._ensure_control(conn, device_id, now)
            current = _require(
                conn.execute(
                    "SELECT * FROM control_state WHERE device_id = ?", (device_id,)
                ).fetchone()
            )
            previous = _row(current)
            previous_epoch = cast(int, current["epoch"])
            previous_run = cast(str | None, current["run_id"])
            if current["recovery_required"]:
                raise LedgerError("recovery_required")
            if run_id is not None:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO runs (run_id, device_id, state, control_epoch, "
                    "created_at_ms, updated_at_ms) VALUES (?, ?, 'active', ?, ?, ?)",
                    (run_id, device_id, previous_epoch + 1, now, now),
                )
                if inserted.rowcount != 1:
                    raise LedgerError("run_not_active")
            if previous_run is not None:
                conn.execute(
                    "UPDATE runs SET state = 'stopping', updated_at_ms = ? "
                    "WHERE run_id = ? AND state = 'active'",
                    (now, previous_run),
                )
            conn.execute(
                "UPDATE control_state SET epoch = ?, holder_source = ?, run_id = ?, "
                "session_id = ?, recovery_required = 0, recovery_action_id = NULL, "
                "updated_at_ms = ? WHERE device_id = ?",
                (previous_epoch + 1, source, run_id, session_id, now, device_id),
            )
            conn.execute(
                "INSERT INTO control_events (device_id, kind, detail, created_at_ms) "
                "VALUES (?, 'control.takeover', ?, ?)",
                (
                    device_id,
                    f"from_epoch={previous_epoch};to_epoch={previous_epoch + 1};source={source}",
                    now,
                ),
            )
            previous["previous_epoch"] = previous_epoch
            return previous

    def finish_takeover(self, run_id: str | None, state: str) -> None:
        if state not in RUN_STATES:
            raise LedgerError("invalid_run_state")
        if run_id is None:
            return
        now = _now_ms()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET state = ?, updated_at_ms = ? WHERE run_id = ? "
                "AND state IN ('active', 'stopping')",
                (state, now, run_id),
            )

    def start_run(self, run_id: str, device_id: str, control_epoch: int) -> None:
        now = _now_ms()
        with self._transaction() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, device_id, state, control_epoch, "
                "created_at_ms, updated_at_ms) VALUES (?, ?, 'active', ?, ?, ?)",
                (run_id, device_id, control_epoch, now, now),
            )
            if inserted.rowcount != 1:
                raise LedgerError("run_not_active")

    def run_state(self, run_id: str) -> str | None:
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute(
                "SELECT state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else cast(str, row["state"])

    def set_run_state(self, run_id: str, state: str) -> None:
        if state not in RUN_STATES:
            raise LedgerError("invalid_run_state")
        now = _now_ms()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET state = ?, updated_at_ms = ? WHERE run_id = ? "
                "AND state IN ('active', 'stopping')",
                (state, now, run_id),
            )

    def acknowledge_recovery(self, device_id: str, *, session_id: str) -> dict[str, object]:
        now = _now_ms()
        with self._transaction() as conn:
            self._ensure_control(conn, device_id, now)
            conn.execute(
                "UPDATE control_state SET recovery_required = 0, recovery_action_id = NULL, "
                "session_id = ?, updated_at_ms = ? WHERE device_id = ?",
                (session_id, now, device_id),
            )
            conn.execute(
                "INSERT INTO control_events (device_id, kind, detail, created_at_ms) "
                "VALUES (?, 'ledger.recovery_acked', ?, ?)",
                (device_id, f"session={session_id}", now),
            )
            row = _require(
                conn.execute(
                    "SELECT * FROM control_state WHERE device_id = ?", (device_id,)
                ).fetchone()
            )
            return _row(row)
