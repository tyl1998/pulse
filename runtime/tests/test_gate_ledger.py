"""P1.6 control gate and durable action ledger, including a real process kill."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.gate.control import ControlGate, GateError
from pulse_runtime.gate.ledger import ActionLedger, LedgerError
from runtime.tests.test_actions import ActionBackend, ActionFactory, body, source
from runtime.tests.test_device_connections import SERIAL_A, Gate
from runtime.tests.test_device_connections import (
    forbid_external_io as forbid_external_io,
)
from runtime.tests.test_observations import ExchangeFactory

SERIAL = "PULSE_GATE"
SESSION = "11111111-1111-4111-8111-111111111111"
LIFECYCLE = "22222222-2222-4222-8222-222222222222"
RUN_A = "33333333-3333-4333-8333-333333333333"
RUN_B = "44444444-4444-4444-8444-444444444444"
ACTION = "55555555-5555-4555-8555-555555555555"
TARGET = "66666666-6666-4666-8666-666666666666"


def prepare(ledger: ActionLedger, action_id: str = ACTION, device_id: str = SERIAL) -> bool:
    return ledger.prepare(
        action_id=action_id,
        device_id=device_id,
        session_id=SESSION,
        run_id=None,
        step_id="step-1",
        control_epoch=0,
        source="auto",
        kind="node_click",
        side_effect=True,
        target_observation_id=TARGET,
        expected_package="com.pulse.target",
        agent_lifecycle_id=LIFECYCLE,
        agent_pid=1234,
    )


def test_ledger_is_private_wal_and_keeps_bounded_action_fields(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "ledger.sqlite3"
    with ActionLedger(path) as ledger:
        assert prepare(ledger)
        assert not prepare(ledger)
        ledger.mark_dispatched(ACTION, agent_lifecycle_id=LIFECYCLE, agent_pid=1234)
        ledger.mark_acknowledged(ACTION, error_code=None)
        ledger.finish(ACTION, "verified", post_observation_available=True)
        record = ledger.action(ACTION)
        assert record is not None
        assert record["state"] == "verified"
        assert record["side_effect"] is True
        assert record["agent_pid"] == 1234
        assert set(record) == {
            "action_id",
            "device_id",
            "session_id",
            "run_id",
            "step_id",
            "control_epoch",
            "source",
            "kind",
            "side_effect",
            "target_observation_id",
            "expected_package",
            "state",
            "error_code",
            "post_observation_available",
            "agent_lifecycle_id",
            "agent_pid",
            "created_at_ms",
            "updated_at_ms",
            "dispatched_at_ms",
            "acknowledged_at_ms",
            "finished_at_ms",
            "recovered_at_ms",
        }
        with pytest.raises(LedgerError, match="invalid_transition"):
            ledger.finish(ACTION, "failed")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_prepared_before_dispatch_is_closed_without_device_recovery(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    first = ActionLedger(path, recover=False)
    assert prepare(first)
    first.close()
    with ActionLedger(path) as recovered:
        record = recovered.action(ACTION)
        assert record is not None
        assert record["state"] == "failed"
        assert record["error_code"] == "runtime_crashed_before_dispatch"
        assert recovered.control_state(SERIAL) is None


def test_kill_9_after_dispatch_marks_uncertain_and_never_replays(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    script = f"""
import os
from pulse_runtime.gate.ledger import ActionLedger

ledger = ActionLedger({str(path)!r}, recover=False)
ledger.prepare(
    action_id={ACTION!r},
    device_id={SERIAL!r},
    session_id={SESSION!r},
    run_id={RUN_A!r},
    step_id="step-1",
    control_epoch=0,
    source="auto",
    kind="node_click",
    side_effect=True,
    target_observation_id={TARGET!r},
    expected_package="com.pulse.target",
    agent_lifecycle_id={LIFECYCLE!r},
    agent_pid=1234,
)
ledger.start_run({RUN_A!r}, {SERIAL!r}, 0)
ledger.mark_dispatched({ACTION!r}, agent_lifecycle_id={LIFECYCLE!r}, agent_pid=1234)
os._exit(9)
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 9, result.stderr

    with ActionLedger(path) as recovered:
        record = recovered.action(ACTION)
        assert record is not None
        assert record["state"] == "uncertain"
        assert record["error_code"] == "runtime_crashed"
        state = recovered.control_state(SERIAL)
        assert state is not None and state["recovery_required"] is True
        assert recovered.run_state(RUN_A) == "interrupted"
        # The same durable id can never become a fresh intent after recovery.
        assert prepare(recovered) is False
        gate = ControlGate(recovered)
        with pytest.raises(GateError, match="recovery_required"):
            gate.authorize(
                SERIAL,
                session_id=SESSION,
                source="auto",
                control_epoch=0,
                run_id=RUN_A,
            )
        recovered.acknowledge_recovery(SERIAL, session_id=SESSION)
        assert recovered.control_state(SERIAL)["recovery_required"] is False


def test_gate_epoch_takeover_source_and_run_ownership() -> None:
    with ActionLedger() as ledger:
        gate = ControlGate(ledger)
        gate.bind_session(SERIAL, SESSION)
        assert gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_A).epoch == 0
        assert (
            gate.authorize(
                SERIAL, session_id=SESSION, source="auto", control_epoch=0, run_id=RUN_A
            ).run_id
            == RUN_A
        )
        with pytest.raises(GateError, match="control_owned_by_other_source"):
            gate.authorize(
                SERIAL, session_id=SESSION, source="manual", control_epoch=0, run_id=None
            )

        current, previous_run, previous_epoch = gate.take_over(
            SERIAL, session_id=SESSION, source="manual", run_id=RUN_B
        )
        assert current.epoch == 1 and current.holder_source == "manual"
        assert previous_epoch == 0 and previous_run == RUN_A
        assert ledger.run_state(RUN_A) == "stopping"
        with pytest.raises(GateError, match="stale_control_epoch"):
            gate.authorize(SERIAL, session_id=SESSION, source="auto", control_epoch=0, run_id=RUN_A)
        with pytest.raises(GateError, match="control_owned_by_other_source"):
            gate.authorize(SERIAL, session_id=SESSION, source="auto", control_epoch=1, run_id=RUN_A)
        with pytest.raises(GateError, match="run_not_active"):
            gate.authorize(
                SERIAL, session_id=SESSION, source="manual", control_epoch=1, run_id=RUN_A
            )
        assert (
            gate.authorize(
                SERIAL, session_id=SESSION, source="manual", control_epoch=1, run_id=RUN_B
            ).epoch
            == 1
        )
        released = gate.release(SERIAL, session_id=SESSION, control_epoch=1, run_id=RUN_B)
        assert released.holder_source is None and released.run_id is None
        assert ledger.run_state(RUN_B) == "finished"


def test_new_connection_interrupts_active_run_and_advances_epoch() -> None:
    other = str(uuid4())
    with ActionLedger() as ledger:
        gate = ControlGate(ledger)
        gate.bind_session(SERIAL, SESSION)
        gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_A)
        rebound = gate.bind_session(SERIAL, other)
        assert rebound.epoch == 1 and rebound.holder_source is None
        assert rebound.run_id is None and rebound.session_id == other
        assert ledger.run_state(RUN_A) == "interrupted"
        with pytest.raises(GateError, match="stale_control_epoch"):
            gate.authorize(SERIAL, session_id=other, source="auto", control_epoch=0, run_id=None)


async def test_manager_blocks_actions_after_crash_until_manual_recovery(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    crashed = ActionLedger(path, recover=False)
    assert prepare(crashed, device_id=SERIAL_A)
    crashed.mark_dispatched(ACTION, agent_lifecycle_id=LIFECYCLE, agent_pid=1234)
    crashed.close()

    backend = ActionBackend()
    actions = ActionFactory(backend)
    observations = ExchangeFactory(backend)
    observations.modify_result[SERIAL_A] = lambda value: actions.source(SERIAL_A, value)
    manager = DeviceConnectionManager(
        backend,
        ledger_path=path,
        readiness_wait=0,
        observation_factory=observations,
        action_factory=actions,
    )
    try:
        connected = await manager.connect(SERIAL_A)
        view = await source(manager)
        blocked = await manager.action(SERIAL_A, body(view))
        assert blocked["status"] == "rejected"
        assert blocked["error"]["code"] == "recovery_required"
        assert actions.calls == []
        control = await manager.control(SERIAL_A)
        assert control["control"]["recovery_required"] is True
        assert control["interrupted_actions"][0]["action_id"] == ACTION

        recovered = await manager.acknowledge_recovery(
            SERIAL_A, session_id=connected["connection"]["session_id"], confirmed=True
        )
        assert recovered["acknowledged"] is True
        assert recovered["control"]["recovery_required"] is False

        view = await source(manager)
        completed = await manager.action(SERIAL_A, body(view))
        assert completed["status"] == "completed"
        assert completed["ledger"]["state"] == "verified"
        assert len(actions.calls) == 1
    finally:
        await manager.close()


def test_finished_uncertain_latches_atomically_and_ack_does_not_relatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.sqlite3"
    with ActionLedger(path) as ledger:
        gate = ControlGate(ledger)
        gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_A)
        ledger.prepare(
            action_id=ACTION,
            device_id=SERIAL,
            session_id=SESSION,
            run_id=RUN_A,
            step_id="step-1",
            control_epoch=0,
            source="auto",
            kind="node_click",
            side_effect=True,
            target_observation_id=TARGET,
            expected_package="com.pulse.target",
            agent_lifecycle_id=LIFECYCLE,
            agent_pid=1234,
        )
        ledger.mark_dispatched(ACTION, agent_lifecycle_id=LIFECYCLE, agent_pid=1234)
        ledger.mark_acknowledged(ACTION, error_code=None)

        def fail_control(*_args: object) -> None:
            raise LedgerError("control_write_failed")

        with monkeypatch.context() as patch:
            patch.setattr(ledger, "_ensure_control", fail_control)
            with pytest.raises(LedgerError, match="control_write_failed"):
                ledger.finish(ACTION, "uncertain")
        assert ledger.action(ACTION)["state"] == "acknowledged"
        assert ledger.run_state(RUN_A) == "active"
        assert gate.recovery_required(SERIAL) is False

        ledger.finish(ACTION, "uncertain", error_code="action_uncertain")
        assert ledger.run_state(RUN_A) == "interrupted"
        assert gate.snapshot(SERIAL).recovery_action_id == ACTION
        assert gate.recovery_required(SERIAL) is True

    with ActionLedger(path) as restarted:
        gate = ControlGate(restarted)
        assert gate.recovery_required(SERIAL) is True
        assert restarted.run_state(RUN_A) == "interrupted"
        with pytest.raises(GateError, match="recovery_required"):
            gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_B)
        gate.acknowledge_recovery(SERIAL, session_id=SESSION)
        # Repeated finalization must not resurrect a manually acknowledged latch either.
        restarted.finish(ACTION, "uncertain")
        assert gate.recovery_required(SERIAL) is False

    with ActionLedger(path) as acknowledged_restart:
        assert ControlGate(acknowledged_restart).recovery_required(SERIAL) is False
        assert acknowledged_restart.action(ACTION)["state"] == "uncertain"
        assert acknowledged_restart.run_state(RUN_A) == "interrupted"


@pytest.mark.parametrize("terminal", ["finished", "canceled", "interrupted"])
@pytest.mark.parametrize(
    "operation", ["acquire", "authorize", "take_over", "start_run", "begin_takeover"]
)
def test_terminal_run_ids_are_rejected_without_control_mutation(terminal, operation) -> None:
    with ActionLedger() as ledger:
        gate = ControlGate(ledger)
        gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_A)
        gate.close_run(SERIAL, session_id=SESSION, control_epoch=0, run_id=RUN_A, state=terminal)
        if operation in {"take_over", "begin_takeover"}:
            gate.acquire(SERIAL, session_id=SESSION, source="auto", run_id=RUN_B)
        before = ledger.control_state(SERIAL)
        with pytest.raises(
            LedgerError if operation in {"start_run", "begin_takeover"} else GateError,
            match="run_not_active",
        ):
            if operation == "start_run":
                ledger.start_run(RUN_A, SERIAL, 99)
            elif operation == "begin_takeover":
                ledger.begin_takeover(SERIAL, session_id=SESSION, source="manual", run_id=RUN_A)
            elif operation == "authorize":
                gate.authorize(
                    SERIAL, session_id=SESSION, source="auto", control_epoch=0, run_id=RUN_A
                )
            else:
                getattr(gate, operation)(SERIAL, session_id=SESSION, source="auto", run_id=RUN_A)
        assert ledger.control_state(SERIAL) == before
        assert ledger.run_state(RUN_A) == terminal
        if operation in {"take_over", "begin_takeover"}:
            assert ledger.run_state(RUN_B) == "active"


async def test_no_action_takeover_cancels_old_run_in_ledger() -> None:
    backend = ActionBackend()
    manager = DeviceConnectionManager(backend, readiness_wait=0)
    try:
        connected = await manager.connect(SERIAL_A)
        session_id = connected["connection"]["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        result = await manager.take_over_control(
            SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
        )
        assert result["stopped_confirmed"] is True
        assert result["previous_run_state"] == "canceled"
        assert manager.ledger.run_state(RUN_A) == "canceled"
        assert manager.ledger.run_state(RUN_B) == "active"
        assert result["control"]["epoch"] == 1
        assert backend.mutations == []
    finally:
        await manager.close()


@pytest.mark.parametrize("swallow_cancel", [False, True])
async def test_observation_takeover_cancels_and_waits_for_read_cleanup(swallow_cancel) -> None:
    backend = ActionBackend()
    observations = ExchangeFactory(backend)
    manager = DeviceConnectionManager(backend, readiness_wait=0, observation_factory=observations)
    pending = takeover = None
    blocked = Gate()
    try:
        connected = await manager.connect(SERIAL_A)
        session_id = connected["connection"]["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        observations.gate[SERIAL_A] = blocked
        if swallow_cancel:
            observations.swallow_cancel.add(SERIAL_A)
        request_id = str(uuid4())
        pending = asyncio.create_task(manager.observe(SERIAL_A, session_id, request_id))
        await asyncio.wait_for(blocked.entered.wait(), 5)
        lease = manager.slot(SERIAL_A).observation
        takeover = asyncio.create_task(
            manager.take_over_control(
                SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
            )
        )
        await asyncio.wait_for(lease.exchange.cancelled.wait(), 5)
        if swallow_cancel:
            assert takeover.done() is False
            assert lease.done.is_set() is False
            assert manager.ledger.run_state(RUN_A) == "stopping"
            blocked.release.set()
        result = await asyncio.wait_for(takeover, 5)
        read = await asyncio.wait_for(pending, 5)
        assert read["success"] is False and read["error"]["code"] == "cancelled"
        assert read["cleanup"]["connection_closed"] is True
        assert result["stopped_confirmed"] is True
        assert result["previous_run_state"] == "canceled"
        assert manager.ledger.run_state(RUN_A) == "canceled"
        assert manager.slot(SERIAL_A).observation is None
        assert manager.slot(SERIAL_A).session.id == session_id
        assert manager.slot(SERIAL_A).session.latest_observation is None
        assert backend.mutations == []
    finally:
        blocked.release.set()
        for task in (pending, takeover):
            if task is not None:
                await asyncio.wait_for(task, 5)
        await manager.close()


async def test_takeover_does_not_relabel_an_already_interrupted_run() -> None:
    backend = ActionBackend()
    manager = DeviceConnectionManager(backend, readiness_wait=0)
    try:
        connected = await manager.connect(SERIAL_A)
        session_id = connected["connection"]["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        manager.ledger.set_run_state(RUN_A, "interrupted")
        result = await manager.take_over_control(
            SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
        )
        assert result["previous_run_state"] == "interrupted"
        assert result["status"] == "interrupted" and result["stopped_confirmed"] is False
        assert result["recovery_required"] is False
        assert manager.ledger.run_state(RUN_A) == "interrupted"
        assert backend.mutations == []
    finally:
        await manager.close()


async def test_takeover_does_not_confirm_stop_before_observation_cleanup() -> None:
    backend = ActionBackend()
    observations = ExchangeFactory(backend)
    manager = DeviceConnectionManager(backend, readiness_wait=0, observation_factory=observations)
    pending = None
    blocked = Gate()
    try:
        connected = await manager.connect(SERIAL_A)
        session_id = connected["connection"]["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        observations.gate[SERIAL_A] = blocked
        observations.swallow_cancel.add(SERIAL_A)
        pending = asyncio.create_task(manager.observe(SERIAL_A, session_id, str(uuid4())))
        await asyncio.wait_for(blocked.entered.wait(), 5)
        result = await manager.take_over_control(
            SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=0.01
        )
        assert result["status"] == "stopping" and result["stopped_confirmed"] is False
        assert manager.ledger.run_state(RUN_A) == "stopping"
        assert pending.done() is False
        assert manager.slot(SERIAL_A).observation is not None
        assert backend.mutations == []
    finally:
        blocked.release.set()
        if pending is not None:
            await asyncio.wait_for(pending, 5)
        await manager.close()


async def test_dispatched_takeover_preserves_persistent_uncertain_isolation(tmp_path: Path) -> None:
    backend = ActionBackend()
    actions = ActionFactory(backend)
    observations = ExchangeFactory(backend)
    observations.modify_result[SERIAL_A] = lambda value: actions.source(SERIAL_A, value)
    path = tmp_path / "ledger.sqlite3"
    manager = DeviceConnectionManager(
        backend,
        ledger_path=path,
        readiness_wait=0,
        observation_factory=observations,
        action_factory=actions,
    )
    pending = None
    blocked = Gate()
    try:
        view = await source(manager)
        session_id = view["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        request = body(view, run_id=RUN_A)
        actions.gate = blocked
        pending = asyncio.create_task(manager.action(SERIAL_A, request))
        await asyncio.wait_for(blocked.entered.wait(), 5)
        result = await manager.take_over_control(
            SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
        )
        action = await asyncio.wait_for(pending, 5)
        assert action["status"] == "uncertain"
        assert result["status"] == "interrupted" and result["stopped_confirmed"] is False
        assert result["recovery_required"] is True
        assert result["control"]["recovery_required"] is True
        assert manager.ledger.run_state(RUN_A) == "interrupted"
        with pytest.raises(ConnectionFault, match="recovery_required"):
            await manager.acquire_control(
                SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B
            )
        assert len(actions.calls) == 1 and backend.mutations == []
    finally:
        blocked.release.set()
        if pending is not None:
            await asyncio.wait_for(pending, 5)
        await manager.close()
    with ActionLedger(path) as restarted:
        assert ControlGate(restarted).recovery_required(SERIAL_A) is True
        assert restarted.run_state(RUN_A) == "interrupted"


@pytest.mark.parametrize("post_observation", [False, True])
async def test_action_observation_takeover_does_not_invalidate_settled_session(
    post_observation,
) -> None:
    backend = ActionBackend()
    actions = ActionFactory(backend)
    observations = ExchangeFactory(backend)
    observations.modify_result[SERIAL_A] = lambda value: actions.source(SERIAL_A, value)
    manager = DeviceConnectionManager(
        backend, readiness_wait=0, observation_factory=observations, action_factory=actions
    )
    blocked = Gate()
    pending = None
    try:
        view = await source(manager)
        session_id = view["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        if post_observation:
            observations.before_reply[SERIAL_A] = lambda _snapshot: observations.gate.setdefault(
                SERIAL_A, blocked
            )
        else:
            observations.gate[SERIAL_A] = blocked
        pending = asyncio.create_task(manager.action(SERIAL_A, body(view, run_id=RUN_A)))
        await asyncio.wait_for(blocked.entered.wait(), 5)
        result = await manager.take_over_control(
            SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
        )
        action = await asyncio.wait_for(pending, 5)
        assert action["status"] == ("completed" if post_observation else "rejected")
        assert action["lifecycle_status"] == "control_epoch_changed"
        assert action["post_observation"] is None
        assert result["stopped_confirmed"] is True
        assert result["recovery_required"] is False
        assert manager.ledger.run_state(RUN_A) == "canceled"
        assert manager.slot(SERIAL_A).session.id == session_id
        assert manager.slot(SERIAL_A).action is None
        assert manager.slot(SERIAL_A).observation is None
        assert len(actions.calls) == int(post_observation)
        assert backend.mutations == []
    finally:
        blocked.release.set()
        if pending is not None:
            await asyncio.wait_for(pending, 5)
        await manager.close()


@pytest.mark.parametrize("recovery", ["memory", "persistent"])
async def test_takeover_recovery_response_combines_memory_and_ledger(recovery) -> None:
    backend = ActionBackend()
    observations = ExchangeFactory(backend)
    manager = DeviceConnectionManager(backend, readiness_wait=0, observation_factory=observations)
    blocked = Gate()
    pending = takeover = None
    try:
        connected = await manager.connect(SERIAL_A)
        session_id = connected["connection"]["session_id"]
        await manager.acquire_control(SERIAL_A, session_id=session_id, source="auto", run_id=RUN_A)
        observations.gate[SERIAL_A] = blocked
        observations.swallow_cancel.add(SERIAL_A)
        pending = asyncio.create_task(manager.observe(SERIAL_A, session_id, str(uuid4())))
        await asyncio.wait_for(blocked.entered.wait(), 5)
        slot = manager.slot(SERIAL_A)
        lease = slot.observation
        takeover = asyncio.create_task(
            manager.take_over_control(
                SERIAL_A, session_id=session_id, source="manual", run_id=RUN_B, timeout=1
            )
        )
        await asyncio.wait_for(lease.exchange.cancelled.wait(), 5)
        if recovery == "memory":
            slot.quarantine = slot.session.generation
        else:
            manager.ledger.update_control(SERIAL_A, recovery_required=True)
        blocked.release.set()
        result = await asyncio.wait_for(takeover, 5)
        assert result["status"] == "interrupted"
        assert result["recovery_required"] is True
        assert result["control"]["recovery_required"] is (recovery == "persistent")
        assert result["stopped_confirmed"] is False
        assert manager.ledger.run_state(RUN_A) == "interrupted"
        assert backend.mutations == []
    finally:
        blocked.release.set()
        for task in (pending, takeover):
            if task is not None:
                await asyncio.wait_for(task, 5)
        await manager.close()
