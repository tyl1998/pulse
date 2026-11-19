"""Android/P1.5 adapter for the case interpreter.

This is a Runtime-private driver: it reuses one connected P1.2 session, P1.5 resolution,
P1.4 actions and the P1.6 run lease. It does not reconnect, install, restart Agent or accept
client snapshots.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from pulse_runtime.drivers.android.actions import ActionBody, ActionTarget
from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    ControlSource,
    DriverFault,
    Element,
    ExecutionEnvironment,
    ReportSink,
    Resolution,
    RunLease,
    RunStatus,
    StopStatus,
)
from pulse_runtime.engine.evidence import decode_png_payload
from pulse_runtime.gate.ledger import LedgerError
from pulse_runtime.perception.locators import ImageResource, LocateBody
from pulse_schema import models as m

_INTERRUPTED = frozenset(
    {
        "stale_session",
        "generation_changed",
        "stale_control_session",
        "stale_control_epoch",
        "control_owned_by_other_source",
        "run_not_active",
        "recovery_required",
        "disconnected",
        "agent_lifecycle_changed",
    }
)


def _interpreter_fault(code: str) -> DriverFault:
    status: StopStatus = "interrupted" if code in _INTERRUPTED else "blocked"
    return DriverFault(status, code)


def _experience_target(
    target: m.Target,
    definitions: dict[m.Identifier, m.Locator],
    primary: m.LocatorCandidate,
) -> m.Target:
    """Order the remembered declared candidate first; an unknown hint is ignored."""
    locator = (
        definitions.get(target.locator_ref) if isinstance(target, m.LocatorRef) else target.locator
    )
    if locator is None:
        return target
    chosen = next(
        (
            item
            for item in locator.candidates
            if isinstance(item, m.LocatorCandidate)
            and item.by == primary.by
            and item.value == primary.value
            and item.scope == primary.scope
        ),
        None,
    )
    if chosen is None:
        return target
    ordered = [chosen, *(item for item in locator.candidates if item is not chosen)]
    reordered = m.Locator(candidates=ordered, expect=locator.expect)
    if isinstance(target, m.LocatorRef):
        definitions[target.locator_ref] = reordered
        return target
    return m.InlineLocator(locator=reordered)


class AndroidCaseDriver:
    def __init__(
        self,
        manager: DeviceConnectionManager,
        *,
        serial: str,
        session_id: str,
        profile: m.Profile,
        resources: Mapping[str, ImageResource] | None = None,
    ) -> None:
        self.manager = manager
        self.serial = serial
        self.session_id = session_id
        self.profile = profile
        self.app_id = profile.app_id
        self.resources = dict(resources or {})
        self.lease: RunLease | None = None
        self.source: ControlSource = "auto"

    def _check_lease(self) -> None:
        lease = self.lease
        if lease is None:
            return
        try:
            control = self.manager.gate.snapshot(self.serial)
            state = self.manager.ledger.run_state(lease.run_id)
        except LedgerError:
            raise DriverFault("blocked", "control_ledger_unavailable") from None
        if control.recovery_required:
            raise DriverFault("interrupted", "recovery_required")
        if state in ("stopping", "canceled"):
            raise DriverFault("canceled", "control_epoch_changed")
        if control.session_id != self.session_id:
            raise DriverFault("interrupted", "stale_control_session")
        if (
            control.epoch != lease.control_epoch
            or control.run_id != lease.run_id
            or control.holder_source != self.source
            or state != "active"
        ):
            raise DriverFault("interrupted", "run_not_active")

    async def begin(self, run_id: str, *, source: ControlSource) -> RunLease:
        try:
            acquired = await self.manager.acquire_control(
                self.serial, session_id=self.session_id, source=source, run_id=run_id
            )
        except ConnectionFault as exc:
            raise _interpreter_fault(exc.code) from None
        control = acquired.get("control")
        epoch = control.get("epoch") if isinstance(control, dict) else None
        if type(epoch) is not int or epoch < 0:
            raise DriverFault("blocked", "invalid_control_state")
        lease = RunLease(run_id, epoch)
        self.lease = lease
        self.source = source
        return lease

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        self._check_lease()
        definitions = dict(locators)
        target = target
        if primary is not None:
            target = _experience_target(target, definitions, primary)
        body = LocateBody(
            serial=self.serial,
            session_id=self.session_id,
            request_id=str(uuid4()),
            expected_package=self.app_id,
            target=target,
            locators=definitions,
            resources=self.resources,
            display_id=0,
            timeout_ms=max(1000, min(timeout_ms, 30_000)),
        )
        try:
            result = await self.manager.probe(self.serial, body, scope=scope)
        except ConnectionFault as exc:
            raise _interpreter_fault(exc.code) from None
        self._check_lease()
        status = result.get("status")
        if status not in ("resolved", "not_found", "ambiguous", "indeterminate"):
            raise DriverFault("blocked", "invalid_locator_result")
        error = result.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        matches: list[Element] = []
        raw_matches = result.get("matches")
        if isinstance(raw_matches, list):
            for item in raw_matches:
                if not isinstance(item, dict):
                    continue
                states = item.get("states")
                bounds = item.get("bounds")
                matches.append(
                    Element(
                        source=str(item.get("source")),
                        text=item.get("text") if type(item.get("text")) is str else None,
                        visible=item.get("visible") if type(item.get("visible")) is bool else None,
                        states=dict(states) if isinstance(states, dict) else {},
                        bounds=dict(bounds) if isinstance(bounds, dict) else None,
                    )
                )
        action_target = result.get("action_target")
        target_error = result.get("action_target_error")
        observation = result.get("observation")
        observation_id = observation.get("request_id") if isinstance(observation, dict) else None
        index = result.get("candidate_index")
        by = result.get("candidate_by")
        return Resolution(
            status,
            error_code if isinstance(error_code, str) else None,
            tuple(matches),
            dict(action_target) if isinstance(action_target, dict) else None,
            observation_id if isinstance(observation_id, str) else None,
            target_error.get("code") if isinstance(target_error, dict) else None,
            query_complete=result.get("query_complete") is True or status == "not_found",
            candidate_index=index if type(index) is int else None,
            candidate_by=by if isinstance(by, str) else None,
        )

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        if self.lease != lease:
            raise DriverFault("interrupted", "stale_run_lease")
        self._check_lease()
        package = plan.params.get("package")
        expected_package = (
            package
            if plan.kind in {"launch", "stop", "clear"} and isinstance(package, str)
            else self.app_id
        )
        target = (
            ActionTarget.model_validate(plan.target.action_target)
            if plan.target is not None and plan.target.action_target is not None
            else await self._root_target(plan.timeout_ms, expected_package)
        )
        if plan.screen_size is not None:
            session = self.manager.slot(self.serial).session
            binding = session.latest_observation if session is not None else None
            geometry = binding.geometry if binding is not None else None
            if geometry is None or plan.screen_size != (
                geometry.get("width"),
                geometry.get("height"),
            ):
                raise DriverFault("blocked", "swipe_screen_mismatch")
        timeout = max(1000, min(plan.timeout_ms, 90_000))
        request_id = str(uuid4())
        try:
            body = ActionBody(
                serial=self.serial,
                session_id=self.session_id,
                request_id=request_id,
                operation_id=str(uuid4()),
                kind=plan.kind,
                target=target,
                params=plan.params,
                timeout_ms=timeout,
                agent_timeout_ms=max(plan.agent_timeout_ms, 5000),
                source=self.source,
                control_epoch=lease.control_epoch,
                run_id=lease.run_id,
                step_id=plan.step_id,
            )
        except ValueError:
            raise DriverFault("blocked", "invalid_action_plan") from None
        try:
            result = await self.manager.action(self.serial, body)
        except ConnectionFault as exc:
            raise _interpreter_fault(exc.code) from None
        status = result.get("status")
        error = result.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if status not in ("completed", "failed", "rejected", "uncertain"):
            raise DriverFault("blocked", "invalid_action_result")
        if status != "uncertain":
            self._check_lease()
        post = result.get("post_observation")
        observation_id = post.get("request_id") if isinstance(post, dict) else None
        return ActionResult(
            status,
            code if isinstance(code, str) else None,
            request_id,
            observation_id if isinstance(observation_id, str) else None,
        )

    async def _root_target(self, timeout_ms: int, expected_package: str) -> ActionTarget:
        request_id = str(uuid4())
        try:
            result = await self.manager.observe(
                self.serial,
                self.session_id,
                request_id,
                tree=True,
                screenshot=False,
                ocr=False,
                display_id=0,
                timeout=min(15.0, max(1.0, timeout_ms / 1000)),
                for_action=True,
            )
        except ConnectionFault as exc:
            raise _interpreter_fault(exc.code) from None
        if result.get("success") is not True:
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise DriverFault(
                "blocked", code if isinstance(code, str) else "observation_unavailable"
            )
        return ActionTarget(
            observation_id=request_id,
            source="tree",
            coordinate_space="display_pixels",
            display_id=0,
            expected_package=expected_package,
            node_path=[],
        )

    def environment(self) -> ExecutionEnvironment:
        """Only values already observed by P1.2; unknown fields stay ``None``."""
        slot = self.manager.slots.get(self.serial)
        session = slot.session if slot is not None else None
        generation = session.generation if session is not None else None
        pid = getattr(generation, "agent_pid", None)
        version = getattr(self.manager, "version", None)
        return ExecutionEnvironment(
            platform="android",
            device_serial=self.serial,
            device_model=slot.row.model if slot is not None else None,
            android_api=None if slot is None or slot.api is None else str(slot.api),
            agent_pid=pid if type(pid) is int else None,
            runtime_version=version if isinstance(version, str) else None,
        )

    def report_sink(
        self,
        serial: str | None,
        session_id: str | None,
        run_id: str,
        evidence_root: Path | None,
    ) -> ReportSink | None:
        return AndroidReportSink(
            self.manager,
            serial=serial or self.serial,
            session_id=session_id or self.session_id,
            run_id=run_id,
        )

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        try:
            self._check_lease()
        except DriverFault as fault:
            return {
                "status": "cleanup_failed",
                "run_status": fault.status,
                "error": {"code": fault.code},
                "run_id": lease.run_id,
            }
        ledger_state = (
            "interrupted"
            if state == "interrupted"
            else "canceled"
            if state == "canceled"
            else "finished"
        )
        try:
            return await self.manager.finish_run(
                self.serial,
                session_id=self.session_id,
                control_epoch=lease.control_epoch,
                run_id=lease.run_id,
                state=ledger_state,
            )
        except ConnectionFault as exc:
            try:
                self._check_lease()
            except DriverFault as fault:
                return {
                    "status": "cleanup_failed",
                    "run_status": fault.status,
                    "error": {"code": fault.code},
                    "run_id": lease.run_id,
                }
            return {
                "status": "cleanup_failed",
                "error": {"code": exc.code},
                "run_id": lease.run_id,
            }


class AndroidReportSink:
    """One bounded screenshot per step through the existing P1.3 read, never a write."""

    def __init__(
        self, manager: DeviceConnectionManager, *, serial: str, session_id: str, run_id: str
    ) -> None:
        self.manager = manager
        self.serial = serial
        self.session_id = session_id
        self.run_id = run_id
        self.captured = 0

    async def screenshot(self, run_id: str, step_id: str) -> dict[str, object]:
        if run_id != self.run_id:
            return {"status": "unavailable", "error": {"code": "evidence_run_mismatch"}}
        try:
            result = await self.manager.observe(
                self.serial,
                self.session_id,
                str(uuid4()),
                tree=False,
                screenshot=True,
                ocr=False,
                display_id=0,
                timeout=15.0,
            )
        except ConnectionFault as exc:
            return {"status": "unavailable", "error": {"code": exc.code}}
        except Exception:
            return {"status": "unavailable", "error": {"code": "screenshot_failed"}}
        sources = result.get("sources")
        screen = sources.get("screenshot") if isinstance(sources, dict) else None
        data = screen.get("data") if isinstance(screen, dict) else None
        if result.get("success") is not True or not isinstance(data, dict):
            error = result.get("error")
            code = error.get("code") if isinstance(error, dict) else "screenshot_unavailable"
            if isinstance(screen, dict) and isinstance(screen.get("error"), dict):
                code = screen["error"].get("code") or code
            return {"status": "unavailable", "error": {"code": code}}
        self.captured += 1
        try:
            payload = decode_png_payload(data.get("base64"), name=f"{step_id}-{self.captured}.png")
        except Exception:
            return {"status": "unavailable", "error": {"code": "screenshot_invalid"}}
        return {"status": "captured", "source": "screenshot", **payload}
