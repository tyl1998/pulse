"""Device control gate: one source, one epoch, one active run per device.

The gate is checked for every write action before an intent is recorded. A source that
does not hold the device cannot act by choosing a new epoch; an explicit takeover must
increment the epoch first. The gate state is persisted by :class:`ActionLedger`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pulse_runtime.gate.ledger import ActionLedger, LedgerError


def _as_int(value: object) -> int:
    if type(value) is not int:
        raise LedgerError("invalid_control_state")
    return value


class GateError(Exception):
    """A bounded control-gate rejection, mapped to an HTTP conflict at the API edge."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ControlSnapshot:
    device_id: str
    epoch: int
    holder_source: str | None
    run_id: str | None
    session_id: str | None
    recovery_required: bool
    recovery_action_id: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "epoch": self.epoch,
            "holder_source": self.holder_source,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "recovery_required": self.recovery_required,
            "recovery_action_id": self.recovery_action_id,
        }


class ControlGate:
    def __init__(self, ledger: ActionLedger) -> None:
        self.ledger = ledger

    def snapshot(self, device_id: str) -> ControlSnapshot:
        state = self.ledger.control_state(device_id)
        if state is None:
            return ControlSnapshot(device_id, 0, None, None, None, False, None)
        return ControlSnapshot(
            device_id=device_id,
            epoch=_as_int(state["epoch"]),
            holder_source=None if state["holder_source"] is None else str(state["holder_source"]),
            run_id=None if state["run_id"] is None else str(state["run_id"]),
            session_id=(None if state["session_id"] is None else str(state["session_id"])),
            recovery_required=bool(state["recovery_required"]),
            recovery_action_id=(
                None if state["recovery_action_id"] is None else str(state["recovery_action_id"])
            ),
        )

    def recovery_required(self, device_id: str) -> bool:
        return self.snapshot(device_id).recovery_required

    def bind_session(self, device_id: str, session_id: str) -> ControlSnapshot:
        """A new Runtime connection may not silently continue an old run.

        The connection generation is not the control epoch. A clean session replacement
        keeps the epoch; if a run was active, it is interrupted and the epoch advances so
        late actions from the old connection cannot pass.
        """
        current = self.snapshot(device_id)
        if current.session_id == session_id:
            return current
        if current.run_id is not None:
            self.ledger.set_run_state(current.run_id, "interrupted")
            updated = self.ledger.update_control(
                device_id,
                epoch=current.epoch + 1,
                holder_source=None,
                run_id=None,
                session_id=session_id,
                event="control.session_replaced",
                detail=f"interrupted_run={current.run_id}",
            )
        else:
            updated = self.ledger.update_control(
                device_id,
                session_id=session_id,
                event="control.session_bound",
            )
        return self._snapshot(device_id, updated)

    def authorize(
        self,
        device_id: str,
        *,
        session_id: str,
        source: str,
        control_epoch: int,
        run_id: str | None,
    ) -> ControlSnapshot:
        state = self.snapshot(device_id)
        if state.recovery_required:
            raise GateError("recovery_required")
        if state.session_id is not None and state.session_id != session_id:
            raise GateError("stale_control_session")
        if control_epoch != state.epoch:
            raise GateError("stale_control_epoch")
        if state.holder_source is None:
            state = self._claim(device_id, session_id, source, state.epoch, run_id)
        elif state.holder_source != source:
            raise GateError("control_owned_by_other_source")
        if state.run_id is not None and run_id != state.run_id:
            raise GateError("run_not_active")
        if run_id is not None:
            run = self.ledger.run_state(run_id)
            if run is None:
                self.ledger.start_run(run_id, device_id, state.epoch)
            elif run != "active":
                raise GateError("run_not_active")
            if state.run_id is None:
                state = self._snapshot(
                    device_id,
                    self.ledger.update_control(device_id, run_id=run_id),
                )
        return state

    def acquire(
        self,
        device_id: str,
        *,
        session_id: str,
        source: str,
        run_id: str | None,
    ) -> ControlSnapshot:
        state = self.snapshot(device_id)
        if state.recovery_required:
            raise GateError("recovery_required")
        if state.session_id is not None and state.session_id != session_id:
            raise GateError("stale_control_session")
        if state.holder_source is None:
            return self._claim(device_id, session_id, source, state.epoch, run_id)
        if state.holder_source != source:
            raise GateError("control_owned_by_other_source")
        if state.run_id is not None and run_id != state.run_id:
            raise GateError("run_not_active")
        if run_id is not None:
            if self.ledger.run_state(run_id) is None:
                self.ledger.start_run(run_id, device_id, state.epoch)
            elif self.ledger.run_state(run_id) != "active":
                raise GateError("run_not_active")
            if state.run_id is None:
                state = self._snapshot(
                    device_id, self.ledger.update_control(device_id, run_id=run_id)
                )
        return state

    def _claim(
        self,
        device_id: str,
        session_id: str,
        source: str,
        epoch: int,
        run_id: str | None,
    ) -> ControlSnapshot:
        if source not in {"auto", "manual", "recorder", "ai"}:
            raise GateError("invalid_source")
        if run_id is not None:
            if self.ledger.run_state(run_id) is not None:
                raise GateError("run_not_active")
            self.ledger.start_run(run_id, device_id, epoch)
        updated = self.ledger.update_control(
            device_id,
            holder_source=source,
            run_id=run_id,
            session_id=session_id,
            event="control.claimed",
            detail=f"source={source};epoch={epoch}",
        )
        return self._snapshot(device_id, updated)

    def take_over(
        self,
        device_id: str,
        *,
        session_id: str,
        source: str,
        run_id: str | None,
    ) -> tuple[ControlSnapshot, str | None, int]:
        if source not in {"auto", "manual", "recorder", "ai"}:
            raise GateError("invalid_source")
        current = self.snapshot(device_id)
        if current.recovery_required:
            raise GateError("recovery_required")
        if current.session_id is not None and current.session_id != session_id:
            raise GateError("stale_control_session")
        try:
            previous = self.ledger.begin_takeover(
                device_id, session_id=session_id, source=source, run_id=run_id
            )
        except LedgerError as exc:
            if str(exc) in {"run_not_active", "recovery_required"}:
                raise GateError(str(exc)) from None
            raise
        previous_run = None if previous["run_id"] is None else str(previous["run_id"])
        return self.snapshot(device_id), previous_run, _as_int(previous["previous_epoch"])

    def release(
        self,
        device_id: str,
        *,
        session_id: str,
        control_epoch: int,
        run_id: str | None,
    ) -> ControlSnapshot:
        state = self.snapshot(device_id)
        if state.recovery_required:
            raise GateError("recovery_required")
        if state.session_id is not None and state.session_id != session_id:
            raise GateError("stale_control_session")
        if control_epoch != state.epoch:
            raise GateError("stale_control_epoch")
        if state.run_id is not None and run_id != state.run_id:
            raise GateError("run_not_active")
        if state.run_id is not None:
            self.ledger.set_run_state(state.run_id, "finished")
        updated = self.ledger.update_control(
            device_id,
            holder_source=None,
            run_id=None,
            event="control.released",
            detail=f"source={state.holder_source};epoch={state.epoch}",
        )
        return self._snapshot(device_id, updated)

    def close_run(
        self,
        device_id: str,
        *,
        session_id: str,
        control_epoch: int,
        run_id: str,
        state: str,
    ) -> ControlSnapshot:
        """Finish the caller's run and release its holder.

        Only the exact session/epoch/run may do this. A takeover or replacement has already
        advanced the epoch, so a late interpreter cannot clear the new holder's control.
        """
        if state not in {"finished", "canceled", "interrupted"}:
            raise GateError("invalid_run_state")
        current = self.snapshot(device_id)
        if current.session_id is not None and current.session_id != session_id:
            raise GateError("stale_control_session")
        if control_epoch != current.epoch:
            raise GateError("stale_control_epoch")
        if current.run_id is not None and current.run_id != run_id:
            raise GateError("run_not_active")
        if current.run_id == run_id:
            self.ledger.set_run_state(run_id, state)
            updated = self.ledger.update_control(
                device_id,
                holder_source=None,
                run_id=None,
                event="control.run_finished",
                detail=f"run_state={state};epoch={current.epoch}",
            )
            return self._snapshot(device_id, updated)
        return current

    def acknowledge_recovery(self, device_id: str, *, session_id: str) -> ControlSnapshot:
        state = self.snapshot(device_id)
        if state.session_id is not None and state.session_id != session_id:
            raise GateError("stale_control_session")
        updated = self.ledger.acknowledge_recovery(device_id, session_id=session_id)
        return self._snapshot(device_id, updated)

    @staticmethod
    def _snapshot(device_id: str, value: dict[str, object]) -> ControlSnapshot:
        try:
            return ControlSnapshot(
                device_id=device_id,
                epoch=_as_int(value["epoch"]),
                holder_source=(
                    None if value["holder_source"] is None else str(value["holder_source"])
                ),
                run_id=None if value["run_id"] is None else str(value["run_id"]),
                session_id=(None if value["session_id"] is None else str(value["session_id"])),
                recovery_required=bool(value["recovery_required"]),
                recovery_action_id=(
                    None
                    if value["recovery_action_id"] is None
                    else str(value["recovery_action_id"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError("invalid_control_state") from exc
