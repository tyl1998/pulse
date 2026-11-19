"""Asynchronous local run transport (P2.2).

The interpreter remains the only execution owner. This manager starts one ``run_case`` task per
request on an already-connected local session, publishes ordered progress events, and exposes
a small step-boundary debug hook. It does not add a second scheduler or a remote task protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pulse_runtime.api.contracts import (
    DebugBreakpointsRequest,
    DebugSnapshot,
    DebugState,
    EvidenceSummary,
    IssueModel,
    RunCurrent,
    RunDetail,
    RunError,
    RunRequest,
    RunResultResponse,
    RunState,
    RunSummary,
)
from pulse_runtime.api.errors import ApiRefusal
from pulse_runtime.api.events import EventBus
from pulse_runtime.api.projects import ProjectRegistry
from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.driver import CaseDriver, RunStatus
from pulse_runtime.engine.interpreter import RunResult, run_case
from pulse_runtime.paths import runtime_dir
from pulse_schema import models as m
from pulse_schema.validation import ContractError, Issue, Project
from pulse_schema.validation import preflight as project_preflight

TERMINAL_STATES = frozenset({"passed", "failed", "blocked", "canceled", "interrupted"})
MAX_STOP_WAIT_S = 10.0
SHUTDOWN_WAIT_S = 5.0
log = logging.getLogger(__name__)

SessionResolver = Callable[[str, bool, float], Awaitable[str]]
DriverFactory = Callable[[str, str, m.Profile], CaseDriver]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _report_issues(evidence: Mapping[str, object] | None) -> list[dict[str, Any]]:
    if not isinstance(evidence, dict):
        return []
    inner = evidence.get("evidence")
    raw = inner.get("issues") if isinstance(inner, dict) else evidence.get("issues")
    if not isinstance(raw, list):
        return []
    return [
        {str(key): value for key, value in item.items()} for item in raw if isinstance(item, dict)
    ]


def _issue_models(items: object) -> list[IssueModel]:
    if not isinstance(items, list):
        return []
    result: list[IssueModel] = []
    for item in items:
        if isinstance(item, IssueModel):
            result.append(item)
        elif isinstance(item, Issue):
            result.append(IssueModel(code=item.code, path=item.path, message=item.message))
        elif isinstance(item, Mapping):
            code = item.get("code")
            path = item.get("path")
            message = item.get("message")
            if isinstance(code, str):
                result.append(
                    IssueModel(
                        code=code,
                        path=path if isinstance(path, str) else "",
                        message=message if isinstance(message, str) else "",
                    )
                )
    return result


class RunDebugController:
    """Step-boundary pause/continue/step state for one run.

    Budgets keep running while paused (P2.2 limitation); a later debugger task can freeze clocks
    without changing this transport contract.
    """

    def __init__(
        self,
        run_id: str,
        events: EventBus,
        state: DebugState,
        *,
        breakpoints: list[str],
        on_state: Callable[[bool], None],
    ) -> None:
        self.run_id = run_id
        self.events = events
        self.state = state
        self.breakpoints = set(breakpoints)
        self.state.breakpoints = sorted(self.breakpoints)
        self.on_state = on_state
        self.pause_requested = False
        self._resume = asyncio.Event()

    async def before_step(self, step_id: str, step_type: str) -> None:
        should_pause = self.pause_requested or self.state.step_mode or step_id in self.breakpoints
        if not should_pause:
            return
        self.pause_requested = False
        self.state.step_mode = False
        self.state.paused = True
        self.state.current = RunCurrent(kind="step", id=step_id, type=step_type)
        self.events.publish(
            "debug.paused",
            {
                "run_id": self.run_id,
                "step": {"id": step_id, "type": step_type},
                "breakpoint": step_id in self.breakpoints,
            },
            run_id=self.run_id,
        )
        self.on_state(True)
        try:
            await self._resume.wait()
        finally:
            self._resume.clear()
            self.state.paused = False
        self.events.publish(
            "debug.resumed",
            {"run_id": self.run_id, "step": {"id": step_id, "type": step_type}},
            run_id=self.run_id,
        )
        self.on_state(False)

    def request_pause(self) -> None:
        if not self.state.paused:
            self.pause_requested = True

    def request_resume(self) -> None:
        self.pause_requested = False
        self.state.step_mode = False
        if self.state.paused:
            self._resume.set()

    def request_step(self) -> None:
        self.pause_requested = False
        self.state.step_mode = True
        if self.state.paused:
            self._resume.set()

    def set_breakpoints(self, values: list[str]) -> None:
        self.breakpoints = set(values)
        self.state.breakpoints = sorted(self.breakpoints)

    def close(self) -> None:
        self.state.paused = False
        self.state.step_mode = False
        self.pause_requested = False
        self._resume.clear()


@dataclass
class RunRecord:
    summary: RunSummary
    project: Project
    request: RunRequest
    debug: RunDebugController
    session_id: str | None = None
    result: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = None

    @property
    def terminal(self) -> bool:
        return self.summary.state in TERMINAL_STATES


class RunManager:
    def __init__(
        self,
        projects: ProjectRegistry,
        events: EventBus,
        *,
        connections: DeviceConnectionManager | None = None,
        driver_factory: DriverFactory | None = None,
        session_resolver: SessionResolver | None = None,
        evidence_root: Path | None = None,
    ) -> None:
        self.projects = projects
        self.events = events
        self.connections = connections
        self.driver_factory = driver_factory or self._android_driver
        self.session_resolver = session_resolver or self._resolve_local_session
        self.evidence_root = evidence_root or (runtime_dir() / "evidence")
        self._runs: dict[str, RunRecord] = {}

    def start(self, request: RunRequest) -> RunSummary:
        registered = self.projects.get(request.project_id)
        project = registered.project
        case = project.cases.get(request.case_id)
        if case is None:
            raise ApiRefusal("case_not_found", 404, case_id=request.case_id)
        profile_id = request.profile_id or (case.profile_refs[0] if case.profile_refs else None)
        if profile_id is None:
            raise ApiRefusal("unknown_profile", 422, case_id=request.case_id)
        if profile_id not in case.profile_refs or profile_id not in project.profiles:
            raise ApiRefusal(
                "profile_not_declared", 422, case_id=request.case_id, profile_id=profile_id
            )
        strategy = request.strategy or project.manifest.default_strategy
        try:
            ready = project_preflight(
                project,
                request.case_id,
                profile_id,
                strategy=strategy,
                inputs=cast("dict[str, m.Scalar]", request.inputs),
            )
        except ContractError as exc:
            raise ApiRefusal(
                "project_invalid",
                422,
                issues=[issue.model_dump() for issue in _issue_models(list(exc.issues))],
            ) from None
        if not ready.offline_ready:
            raise ApiRefusal(
                "preflight_failed",
                422,
                issues=[issue.model_dump() for issue in _issue_models(list(ready.issues))],
                ai_requirements=[
                    issue.model_dump() for issue in _issue_models(list(ready.ai_requirements))
                ],
            )

        now = _now_ms()
        run_id = str(uuid4())
        summary = RunSummary(
            run_id=run_id,
            project_id=project.manifest.project_id,
            case_id=request.case_id,
            profile_id=profile_id,
            device=request.device,
            strategy=strategy,
            state="queued",
            created_at_ms=now,
        )
        summary.debug.breakpoints = list(request.breakpoints)
        debug = RunDebugController(
            run_id,
            self.events,
            summary.debug,
            breakpoints=list(request.breakpoints),
            on_state=lambda paused: self._debug_state_by_id(run_id, paused),
        )
        record = RunRecord(summary, project, request, debug)
        self._runs[run_id] = record
        self._publish(record, "run.created", summary.model_dump(mode="json"))
        record.task = asyncio.create_task(self._execute(record), name=f"pulse-run-{run_id}")
        return summary.model_copy(deep=True)

    def get(self, run_id: str) -> RunRecord:
        record = self._runs.get(run_id)
        if record is None:
            raise ApiRefusal("run_not_found", 404, run_id=run_id)
        return record

    def list(self) -> list[RunSummary]:
        return [
            record.summary.model_copy(deep=True)
            for record in sorted(
                self._runs.values(), key=lambda item: item.summary.created_at_ms, reverse=True
            )
        ]

    def detail(self, run_id: str) -> RunDetail:
        record = self.get(run_id)
        return RunDetail(
            **record.summary.model_dump(),
            session_id=record.session_id,
            result=record.result,
        )

    def result(self, run_id: str) -> RunResultResponse:
        record = self.get(run_id)
        return RunResultResponse(
            run_id=record.summary.run_id, state=record.summary.state, result=record.result
        )

    def snapshot_debug(self, run_id: str) -> DebugSnapshot:
        record = self.get(run_id)
        state = record.summary.debug
        return DebugSnapshot(
            run_id=record.summary.run_id,
            state=record.summary.state,
            paused=state.paused,
            step_mode=state.step_mode,
            breakpoints=list(state.breakpoints),
            current=state.current,
        )

    def set_breakpoints(self, run_id: str, request: DebugBreakpointsRequest) -> DebugSnapshot:
        record = self.get(run_id)
        self._require_active(record)
        record.debug.set_breakpoints(request.breakpoints)
        return self.snapshot_debug(run_id)

    def pause(self, run_id: str) -> DebugSnapshot:
        record = self.get(run_id)
        self._require_active(record)
        record.debug.request_pause()
        return self.snapshot_debug(run_id)

    def resume(self, run_id: str) -> DebugSnapshot:
        record = self.get(run_id)
        self._require_active(record)
        record.debug.request_resume()
        return self.snapshot_debug(run_id)

    def step(self, run_id: str) -> DebugSnapshot:
        record = self.get(run_id)
        self._require_active(record)
        record.debug.request_step()
        return self.snapshot_debug(run_id)

    async def stop(self, run_id: str) -> RunSummary:
        record = self.get(run_id)
        if record.terminal:
            return record.summary.model_copy(deep=True)
        record.summary.state = "stopping"
        record.summary.reason = "stop_requested"
        self._publish(record, "run.state", {"state": "stopping"})
        task = record.task
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(task, return_exceptions=True)), MAX_STOP_WAIT_S
                )
        if not record.terminal:
            self._finish_terminal(record, "canceled", "stopped")
        return record.summary.model_copy(deep=True)

    async def wait(self, run_id: str) -> RunSummary:
        record = self.get(run_id)
        if record.task is not None:
            await asyncio.shield(record.task)
        return record.summary.model_copy(deep=True)

    async def close(self) -> None:
        tasks = [
            record.task
            for record in self._runs.values()
            if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(SHUTDOWN_WAIT_S):
                    await asyncio.gather(*tasks, return_exceptions=True)
        for record in self._runs.values():
            if not record.terminal:
                self._finish_terminal(record, "interrupted", "runtime_stopped")

    async def _execute(self, record: RunRecord) -> None:
        self._set_state(record, "preparing")
        try:
            record.session_id = await self.session_resolver(
                record.summary.device,
                record.request.auto_connect,
                record.request.connect_timeout_ms / 1000,
            )
            profile = record.project.profiles[record.summary.profile_id]
            driver = self.driver_factory(record.summary.device, record.session_id, profile)
            self._set_state(record, "running")
            result = await run_case(
                project=record.project,
                driver=driver,
                case_id=record.summary.case_id,
                profile_id=record.summary.profile_id,
                run_id=record.summary.run_id,
                inputs=cast("Mapping[str, m.Scalar]", record.request.inputs),
                activities=dict(record.request.activities),
                allow_clear=record.request.allow_clear,
                source="auto",
                strategy=record.request.strategy,
                progress=self._progress(record),
                evidence_root=self.evidence_root,
                include_screenshot=record.request.include_screenshot,
                debug=record.debug,
                raise_on_cancel=False,
            )
            self._finish(record, result)
        except ApiRefusal as exc:
            self._fail(record, "blocked", exc.code)
        except asyncio.CancelledError:
            record.debug.close()
            self._finish_terminal(record, "canceled", "stopped")
        except Exception:
            log.exception(
                "Run failed unexpectedly",
                extra={
                    "event": "run.internal_error",
                    "run_id": record.summary.run_id,
                    "error_category": "internal",
                },
            )
            self._fail(record, "interrupted", "runtime_error")

    def _progress(self, record: RunRecord) -> Callable[[str, dict[str, object]], None]:
        def report(event: str, data: dict[str, object]) -> None:
            self._track_current(record, event, data)
            if event == "run_finish":
                return
            if event == "evidence_start":
                directory = data.get("directory")
                if isinstance(directory, str):
                    record.summary.evidence.directory = directory
            self._publish(record, f"run.{event}", dict(data))

        return report

    def _track_current(self, record: RunRecord, event: str, data: dict[str, object]) -> None:
        identifier = data.get("id")
        if not isinstance(identifier, str):
            return
        if event == "step_start":
            raw_kind = data.get("type")
            kind = raw_kind if isinstance(raw_kind, str) else "step"
            record.summary.debug.current = RunCurrent(
                kind="step", id=identifier, type=kind if kind != "step" else None
            )
        elif event == "check_start":
            record.summary.debug.current = RunCurrent(kind="check", id=identifier, type=None)
        elif event in ("step_finish", "check_finish"):
            current = record.summary.debug.current
            if current is not None and current.id == identifier:
                record.summary.debug.current = None

    def _debug_state(self, record: RunRecord, paused: bool) -> None:
        if record.terminal:
            return
        record.summary.state = "paused" if paused else "running"
        self._publish(record, "run.state", {"state": record.summary.state})

    def _debug_state_by_id(self, run_id: str, paused: bool) -> None:
        record = self._runs.get(run_id)
        if record is not None:
            self._debug_state(record, paused)

    def _set_state(self, record: RunRecord, state: RunState) -> None:
        record.summary.state = state
        if state == "preparing" and record.summary.started_at_ms is None:
            record.summary.started_at_ms = _now_ms()
        self._publish(record, "run.state", {"state": state})

    def _finish(self, record: RunRecord, result: RunResult) -> None:
        record.result = cast("dict[str, Any]", result.as_dict())
        record.summary.finished_at_ms = _now_ms()
        record.summary.elapsed_ms = round(result.elapsed_ms, 3)
        record.summary.reason = result.reason
        record.summary.business_success = result.business_success
        record.summary.evidence = self._evidence_summary(record, result)
        record.summary.debug.paused = False
        record.summary.debug.step_mode = False
        record.summary.debug.current = None
        record.debug.close()
        self._set_state(record, result.status)
        self._publish(
            record,
            "run.finished",
            {
                "run_id": record.summary.run_id,
                "status": result.status,
                "reason": result.reason,
                "business_success": result.business_success,
                "evidence": record.summary.evidence.model_dump(mode="json"),
            },
        )

    def _fail(self, record: RunRecord, state: RunState, reason: str) -> None:
        record.summary.finished_at_ms = _now_ms()
        record.summary.reason = reason
        record.summary.error = RunError(code=reason)
        record.summary.evidence = EvidenceSummary(status="unavailable")
        record.summary.debug.paused = False
        record.summary.debug.step_mode = False
        record.summary.debug.current = None
        record.debug.close()
        self._set_state(record, state)
        self._publish(
            record,
            "run.finished",
            {"run_id": record.summary.run_id, "status": state, "reason": reason},
        )

    def _finish_terminal(self, record: RunRecord, state: RunState, reason: str) -> None:
        if record.result is not None:
            return
        self._fail(record, state, reason)

    def _evidence_summary(self, record: RunRecord, result: RunResult) -> EvidenceSummary:
        directory = self.evidence_root / record.summary.run_id
        proof = directory / "result.json"
        status: Literal["written", "incomplete", "unavailable"]
        if result.evidence is None:
            status = "unavailable"
        elif proof.is_file():
            status = "written"
        elif directory.is_dir():
            status = "incomplete"
        else:
            status = "unavailable"
        return EvidenceSummary(
            status=status,
            directory=str(directory) if directory.is_dir() else None,
            issues=_report_issues(result.evidence),
        )

    def _publish(self, record: RunRecord, event_type: str, body: dict[str, Any]) -> None:
        self.events.publish(
            event_type,
            body,
            run_id=record.summary.run_id,
            device_id=record.summary.device,
        )

    @staticmethod
    def _require_active(record: RunRecord) -> None:
        if record.terminal:
            raise ApiRefusal("run_not_active", 409, state=record.summary.state)

    async def _resolve_local_session(
        self, serial: str, auto_connect: bool, timeout_s: float
    ) -> str:
        if self.connections is None:
            raise ApiRefusal("device_unavailable", 503)
        slot = self.connections.slot(serial)
        session = slot.session
        if session is not None and slot.status == "ready":
            return session.id
        if not auto_connect:
            raise ApiRefusal("device_not_connected", 409, serial=serial, status=slot.status)
        try:
            snapshot = await self.connections.connect(serial, timeout=timeout_s)
        except ConnectionFault as exc:
            raise ApiRefusal(exc.code, 409, serial=serial) from None
        slot = self.connections.slot(serial)
        session = slot.session
        if snapshot.get("ready") is not True or session is None:
            code = slot.code or slot.status or "device_not_ready"
            raise ApiRefusal(code, 409, serial=serial)
        return session.id

    def _android_driver(self, serial: str, session_id: str, profile: m.Profile) -> CaseDriver:
        if self.connections is None:
            raise ApiRefusal("device_unavailable", 503)
        return AndroidCaseDriver(
            self.connections, serial=serial, session_id=session_id, profile=profile
        )


def terminal_status(value: str) -> RunStatus:
    if value in TERMINAL_STATES:
        return cast(RunStatus, value)
    raise ValueError("not a terminal run status")
