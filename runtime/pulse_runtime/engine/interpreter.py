"""Bounded case interpreter.

The interpreter is the only owner of step order, lexical variables, flow calls, branches,
loops, final checkpoints and nested deadlines. A driver can perform exactly one device read
or one device write; completion of a write is never a business assertion.
"""

from __future__ import annotations

import asyncio
import re
import string
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pulse_runtime.engine.assertions import MAX_PRECISION, supported_unit
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.conditions import ConditionResult, evaluate
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    CaseDriver,
    ControlSource,
    DebugHook,
    DriverFault,
    Element,
    ExecutionEnvironment,
    ReportSink,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_runtime.engine.errors import EngineStop
from pulse_runtime.engine.evidence import EvidenceError, EvidenceRecorder, expectation
from pulse_runtime.engine.values import (
    Scope,
    TypedValue,
    checked,
    flow_scope,
    resolve_value,
    seed_scope,
)
from pulse_runtime.experience.local import ExperienceKey, ExperienceStore, experience_key
from pulse_schema import models as m
from pulse_schema.validation import Project, preflight

_SCREENSHOT_TIMEOUT_S = 20.0
_PATH_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
KEY_CODES = {
    "BACK": 4,
    "HOME": 3,
    "ENTER": 66,
    "DEL": 67,
    "MENU": 82,
    "APP_SWITCH": 187,
}
_SOURCE = {
    "native": "tree",
    "webview": "tree",
    "ocr": "ocr",
    "vision": "screenshot",
}
_INTERRUPTED_REJECTIONS = frozenset(
    {
        "recovery_required",
        "stale_session",
        "stale_control_session",
        "stale_control_epoch",
        "control_owned_by_other_source",
        "run_not_active",
        "action_request_reused",
        "action_operation_reused",
        "stale_lifecycle",
        "generation_changed",
        "agent_lifecycle_changed",
        "disconnected",
    }
)


@dataclass
class CheckResult:
    id: str
    required: bool
    status: Literal["passed", "failed", "blocked"]
    operator: str
    source: str
    code: str | None = None
    actual: dict[str, object] = field(default_factory=dict)
    expected: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "required": self.required,
            "status": self.status,
            "operator": self.operator,
            "source": self.source,
            "error": {"code": self.code} if self.code else None,
            "expected": dict(self.expected),
            "actual": dict(self.actual),
        }


@dataclass
class StepResult:
    id: str
    type: str
    status: Literal["passed", "failed", "blocked", "interrupted", "canceled"]
    code: str | None = None
    elapsed_ms: float = 0.0
    details: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "error": {"code": self.code} if self.code else None,
            "elapsed_ms": round(self.elapsed_ms, 3),
            **self.details,
        }


@dataclass
class RunResult:
    run_id: str
    case_id: str
    profile_id: str
    status: RunStatus
    reason: str | None = None
    preconditions: list[CheckResult] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    cleanup: dict[str, object] = field(default_factory=dict)
    preflight_issues: list[dict[str, str]] = field(default_factory=list)
    started_at_ms: int = 0
    finished_at_ms: int = 0
    elapsed_ms: float = 0.0
    business_success: bool | None = None
    evidence: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "contract": "pulse-runtime-interpreter-draft",
            "run_id": self.run_id,
            "case_id": self.case_id,
            "profile_id": self.profile_id,
            "status": self.status,
            "reason": self.reason,
            "business_success": self.business_success,
            "preconditions": [item.as_dict() for item in self.preconditions],
            "steps": [item.as_dict() for item in self.steps],
            "checks": [item.as_dict() for item in self.checks],
            "cleanup": dict(self.cleanup),
            "preflight_issues": list(self.preflight_issues),
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "evidence": self.evidence,
        }


@dataclass
class _Context:
    project: Project
    case: m.Case
    profile: m.Profile
    driver: CaseDriver
    lease: RunLease
    deadline: Deadline
    secrets: Mapping[str, str] | None
    activities: Mapping[str, str]
    allow_clear: bool
    progress: Callable[[str, dict[str, object]], None] | None = None
    suite_id: str | None = None
    recorder: EvidenceRecorder | None = None
    sink: ReportSink | None = None
    experience: ExperienceStore | None = None
    page: str | None = None
    debug: DebugHook | None = None

    def locators(self, procedure: m.Case | m.Flow) -> dict[str, m.Locator]:
        return {**self.profile.locators, **procedure.locators}

    def experience_key(self, target_ref: str | None) -> ExperienceKey | None:
        if self.experience is None or self.page is None:
            return None
        return experience_key(
            project_id=self.project.manifest.project_id,
            app_id=self.profile.app_id,
            page=self.page,
            intent=self.case.case_id,
            target=target_ref or "inline",
        )


def _step_status(stop: EngineStop) -> Literal["failed", "blocked", "interrupted", "canceled"]:
    return stop.status


def _uses_secret(value: m.Value, ctx: _Context) -> bool:
    """Whether a resolved input value came from the secret namespace, directly or via data."""
    if isinstance(value, m.SecretRef):
        return value.secret_ref in (ctx.secrets or {})
    if isinstance(value, m.DataRef):
        data = ctx.project.data.get(value.data_ref)
        item = data.values.get(value.key) if data is not None else None
        return isinstance(item, m.SecretRef) and item.secret_ref in (ctx.secrets or {})
    return False


def _conditions_of(
    steps: Sequence[m.Step],
    into: list[m.Predicate],
    flows: Mapping[str, m.Flow],
    visited: set[str],
) -> None:
    for step in steps:
        if isinstance(step, m.WaitStep):
            into.append(step.condition)
        elif isinstance(step, m.CheckStep):
            into.append(step.checkpoint.condition)
        elif isinstance(step, m.IfStep):
            into.append(step.condition)
            _conditions_of(step.then, into, flows, visited)
            _conditions_of(step.else_, into, flows, visited)
        elif isinstance(step, m.LoopStep):
            if step.condition is not None:
                into.append(step.condition)
            _conditions_of(step.steps, into, flows, visited)
        elif isinstance(step, m.FlowStep) and step.flow_ref not in visited:
            visited.add(step.flow_ref)
            _conditions_of(flows[step.flow_ref].steps, into, flows, visited)


def number_issues(case: m.Case, flows: Mapping[str, m.Flow] | None = None) -> list[dict[str, str]]:
    """Refuse ``number_cmp`` units/precisions that have no exact normalization rule yet."""
    conditions: list[m.Predicate] = [item.condition for item in case.preconditions]
    _conditions_of(case.steps, conditions, flows or {}, set())
    conditions.extend(item.condition for item in case.checks)
    issues: list[dict[str, str]] = []
    for condition in conditions:
        if not isinstance(condition, m.NumberCondition):
            continue
        if not supported_unit(condition.unit):
            issues.append(
                {
                    "code": "number_unit_unsupported",
                    "path": "number_cmp",
                    "message": "unit has no exact normalization rule",
                }
            )
        if condition.precision > MAX_PRECISION:
            issues.append(
                {
                    "code": "number_precision_unsupported",
                    "path": "number_cmp",
                    "message": "precision exceeds the exact half-up limit",
                }
            )
    return issues


def _emit(
    progress: Callable[[str, dict[str, object]], None] | None,
    event: str,
    **fields: object,
) -> None:
    if progress is not None:
        progress(event, fields)


def _emit_step(ctx: _Context, event: str, **fields: object) -> None:
    _emit(ctx.progress, event, case_id=ctx.case.case_id, **fields)


def _snapshot(ctx: _Context, name: str, payload: dict[str, object]) -> None:
    recorder = ctx.recorder
    if recorder is None:
        return
    recorder.snapshot(name, payload)


async def _screenshot(
    ctx: _Context, step_id: str, *, secret_used: bool = False
) -> dict[str, object] | None:
    """Store one post-action screenshot when allowed; never blocks or alters the verdict.

    A screenshot cannot be redacted, so a step that just sent a known secret is not captured.
    The capture also stops at the remaining case budget; a slow capture must not spend the
    time later business checks still need.
    """
    if ctx.sink is None or ctx.recorder is None:
        return None
    if secret_used:
        return {"status": "unavailable", "error": {"code": "secret_input_screenshot_skipped"}}
    timeout = min(_SCREENSHOT_TIMEOUT_S, ctx.deadline.remaining())
    if timeout <= 0:
        return {"status": "unavailable", "error": {"code": "deadline_exceeded"}}
    try:
        payload = await asyncio.wait_for(ctx.sink.screenshot(ctx.lease.run_id, step_id), timeout)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return {"status": "unavailable", "error": {"code": "screenshot_timeout"}}
    except Exception:
        return {"status": "unavailable", "error": {"code": "screenshot_failed"}}
    if payload.get("status") != "captured" or not ctx.recorder.attach_screenshot(
        payload, step_id=step_id
    ):
        code = payload.get("error")
        return {
            "status": "unavailable",
            "error": code if isinstance(code, dict) else {"code": "screenshot_invalid"},
        }
    return {
        "status": "captured",
        "file": f"attachments/{payload['name']}",
        "sha256": payload.get("sha256"),
        "byte_length": payload.get("byte_length"),
    }


def _path_identifier(value: str) -> str | None:
    """Run-directory identifier check without depending on regex full-match helpers."""
    if not 1 <= len(value) <= 128:
        return None
    if any(char not in _PATH_CHARS for char in value):
        return None
    return value


def _source_matches(resolution: Resolution, source: m.ObservationSource) -> bool:
    expected = _SOURCE[source]
    return bool(resolution.matches) and all(item.source == expected for item in resolution.matches)


def _one_element(resolution: Resolution, source: m.ObservationSource) -> Element:
    if resolution.status not in ("resolved", "ambiguous"):
        raise EngineStop(
            "failed" if resolution.status == "not_found" else "blocked",
            resolution.error_code or "target_unavailable",
        )
    if not _source_matches(resolution, source):
        raise EngineStop("blocked", "source_mismatch")
    if len(resolution.matches) != 1:
        raise EngineStop("blocked", "ambiguous_target")
    return resolution.matches[0]


def _child_plan(
    action: m.Action,
    step_id: str,
    *,
    resolution: Resolution | None,
    target_ref: str | None,
    env: Scope,
    ctx: _Context,
    timeout_ms: int,
) -> ActionPlan:
    element = resolution.matches[0] if resolution and len(resolution.matches) == 1 else None
    if isinstance(action, (m.TapAction, m.LongAction, m.InputAction)):
        if resolution is None:
            raise EngineStop("blocked", "action_target_unavailable")
        if resolution.status == "not_found":
            raise EngineStop("failed", "target_not_found")
        if resolution.status == "indeterminate":
            raise EngineStop("blocked", resolution.error_code or "action_target_unavailable")
        if resolution.status == "ambiguous":
            raise EngineStop("blocked", "ambiguous_target")
        if resolution.action_target is None:
            raise EngineStop(
                "blocked", resolution.action_target_error or "action_target_unavailable"
            )
        if element is None or element.bounds is None:
            raise EngineStop("blocked", "action_bounds_unavailable")
        bounds = element.bounds
        x = (bounds["left"] + bounds["right"]) // 2
        y = (bounds["top"] + bounds["bottom"]) // 2
        if isinstance(action, m.TapAction):
            return ActionPlan("tap", {"x": x, "y": y}, target_ref, resolution, step_id, timeout_ms)
        if isinstance(action, m.LongAction):
            return ActionPlan(
                "long",
                {"x": x, "y": y, "duration_ms": action.duration_ms},
                target_ref,
                resolution,
                step_id,
                timeout_ms,
                max(5000, action.duration_ms + 250),
            )
        text = resolve_value(action.text, project=ctx.project, scope=env, secrets=ctx.secrets)
        if text.type != "string" or type(text.value) is not str:
            raise EngineStop("blocked", "input_type")
        value = text.value
        if action.mode == "append":
            if element.text is None:
                raise EngineStop("blocked", "text_unverified")
            value = element.text + value
        return ActionPlan("set_text", {"text": value}, target_ref, resolution, step_id, timeout_ms)
    if isinstance(action, m.SwipeAction):
        if (action.start.width, action.start.height) != (action.end.width, action.end.height):
            raise EngineStop("blocked", "swipe_screen")
        return ActionPlan(
            "swipe",
            {
                "x": action.start.x,
                "y": action.start.y,
                "end_x": action.end.x,
                "end_y": action.end.y,
                "duration_ms": action.duration_ms,
            },
            None,
            None,
            step_id,
            timeout_ms,
            max(5000, action.duration_ms + 250),
            screen_size=(action.start.width, action.start.height),
        )
    if isinstance(action, m.KeyAction):
        code = KEY_CODES.get(action.key.upper())
        if code is None:
            raise EngineStop("blocked", "key_unavailable")
        return ActionPlan("key", {"code": code}, None, None, step_id, timeout_ms)
    if action.app_id not in ctx.project.manifest.constraints.allowed_apps:
        raise EngineStop("blocked", "app_not_allowed")
    if action.kind == "launch":
        activity = ctx.activities.get(action.app_id)
        if activity is None:
            raise EngineStop("blocked", "launch_activity_unavailable")
        return ActionPlan(
            "launch",
            {"package": action.app_id, "activity": activity},
            None,
            None,
            step_id,
            timeout_ms,
        )
    if action.kind == "stop":
        return ActionPlan("stop", {"package": action.app_id}, None, None, step_id, timeout_ms)
    if not ctx.allow_clear:
        raise EngineStop("blocked", "destructive_action_not_authorized")
    return ActionPlan(
        "clear",
        {"package": action.app_id, "confirmed": True},
        None,
        None,
        step_id,
        timeout_ms,
    )


def _action_failure(result: ActionResult) -> EngineStop:
    if result.status == "uncertain":
        return EngineStop("interrupted", result.error_code or "action_uncertain")
    if result.status == "failed":
        return EngineStop("failed", result.error_code or "action_failed")
    code = result.error_code or "action_rejected"
    if code in _INTERRUPTED_REJECTIONS:
        return EngineStop("interrupted", code)
    return EngineStop("blocked", code)


async def _check_point(
    checkpoint: m.Checkpoint,
    *,
    ctx: _Context,
    env: Scope,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
) -> CheckResult:
    _emit_step(
        ctx,
        "check_start",
        id=checkpoint.id,
        required=checkpoint.required,
        operator=checkpoint.condition.operator,
    )
    condition = await evaluate(
        checkpoint.condition,
        driver=ctx.driver,
        locators=locators,
        deadline=deadline,
        project=ctx.project,
        scope=env,
        secrets=ctx.secrets,
    )
    status: Literal["passed", "failed", "blocked"] = (
        "passed"
        if condition.verdict == "true"
        else "failed"
        if condition.verdict == "false"
        else "blocked"
    )
    _emit_step(
        ctx,
        "check_finish",
        id=checkpoint.id,
        required=checkpoint.required,
        status=status,
        code=condition.code,
        operator=condition.operator,
        source=condition.source,
    )
    expected = expectation(checkpoint.condition)
    _snapshot(
        ctx,
        f"check-{checkpoint.id}",
        {
            "id": checkpoint.id,
            "required": checkpoint.required,
            "status": status,
            "code": condition.code,
            "operator": condition.operator,
            "source": condition.source,
            "expected": expected,
            "actual": condition.actual,
        },
    )
    return CheckResult(
        checkpoint.id,
        checkpoint.required,
        status,
        condition.operator,
        condition.source,
        condition.code,
        condition.actual,
        expected,
    )


async def _run_checks(
    checks: Sequence[m.Checkpoint],
    *,
    ctx: _Context,
    env: Scope,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
    stop_on_required: bool,
    results: list[CheckResult],
) -> None:
    for checkpoint in checks:
        result = await _check_point(
            checkpoint, ctx=ctx, env=env, locators=locators, deadline=deadline
        )
        results.append(result)
        if stop_on_required and result.required:
            if result.status == "failed":
                raise EngineStop("failed", result.code or f"check_failed:{result.id}")
            if result.status == "blocked":
                raise EngineStop("blocked", result.code or f"check_blocked:{result.id}")


async def _wait_step(
    step: m.WaitStep,
    *,
    ctx: _Context,
    env: Scope,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
) -> tuple[ConditionResult, str | None]:
    last: ConditionResult | None = None
    while True:
        if deadline.expired:
            if last is not None and last.verdict == "false":
                return last, "wait_timeout"
            raise EngineStop("blocked", "deadline_exceeded")
        last = await evaluate(
            step.condition,
            driver=ctx.driver,
            locators=locators,
            deadline=deadline,
            project=ctx.project,
            scope=env,
            secrets=ctx.secrets,
        )
        if last.verdict == "true":
            return last, None
        if last.verdict == "unknown":
            return last, last.code or "condition_unverified"
        remaining = deadline.remaining()
        if remaining <= 0:
            return last, "wait_timeout"
        await asyncio.sleep(min(0.5, remaining))


async def _execute_steps(
    steps: Sequence[m.Step],
    *,
    ctx: _Context,
    procedure: m.Case | m.Flow,
    env: Scope,
    deadline: Deadline,
    results: list[StepResult],
) -> None:
    locators = ctx.locators(procedure)
    for step in steps:
        if ctx.debug is not None:
            try:
                async with asyncio.timeout(deadline.remaining()):
                    await ctx.debug.before_step(step.id, step.type)
            except TimeoutError:
                raise EngineStop("blocked", "deadline_exceeded") from None
        started = time.monotonic()
        step_deadline = Deadline.after(step.timeout_ms / 1000, deadline)
        result = StepResult(step.id, step.type, "passed")
        _emit_step(ctx, "step_start", id=step.id, type=step.type)
        try:
            if step_deadline.expired:
                raise EngineStop("blocked", "deadline_exceeded")
            if isinstance(step, m.ActionStep):
                constraints = ctx.project.manifest.constraints
                if (
                    step.action.kind in constraints.forbidden_actions
                    or step.id in constraints.forbidden_actions
                ):
                    raise EngineStop("blocked", "forbidden_action")
                target_ref, resolution = await _resolve_action_target(
                    step, ctx, locators, step_deadline
                )
                timeout = min(step_deadline.limit_ms(step.timeout_ms), 90_000)
                plan = _child_plan(
                    step.action,
                    step.id,
                    resolution=resolution,
                    target_ref=target_ref,
                    env=env,
                    ctx=ctx,
                    timeout_ms=max(1000, timeout),
                )
                try:
                    async with asyncio.timeout(step_deadline.remaining() + 0.1):
                        outcome = await ctx.driver.act(plan, ctx.lease)
                except TimeoutError:
                    raise EngineStop("interrupted", "action_deadline_exceeded") from None
                if outcome.status != "completed":
                    raise _action_failure(outcome)
                result.details["action_id"] = outcome.action_id
                result.details["observation_id"] = outcome.observation_id
                secret_used = isinstance(step.action, m.InputAction) and _uses_secret(
                    step.action.text, ctx
                )
                attachment = await _screenshot(ctx, step.id, secret_used=secret_used)
                if attachment is not None:
                    result.details["attachments"] = [attachment]
            elif isinstance(step, m.FlowStep):
                flow = ctx.project.flows[step.flow_ref]
                arguments: dict[str, TypedValue] = {}
                for name, value in step.arguments.items():
                    arguments[name] = resolve_value(
                        value, project=ctx.project, scope=env, secrets=ctx.secrets
                    )
                local = flow_scope(flow, arguments)
                nested_start = len(results)
                await _execute_steps(
                    flow.steps,
                    ctx=ctx,
                    procedure=flow,
                    env=local,
                    deadline=step_deadline,
                    results=results,
                )
                result.details["steps"] = [item.as_dict() for item in results[nested_start:]]
                returns: dict[str, TypedValue] = {}
                for name, output in flow.returns.items():
                    returns[name] = checked(
                        resolve_value(
                            output.value, project=ctx.project, scope=local, secrets=ctx.secrets
                        ),
                        output.type,
                    )
                for return_name, variable in step.bind.items():
                    if return_name not in returns:
                        raise EngineStop("blocked", "flow_return_reference")
                    env.define(variable, returns[return_name])
                result.details["bind"] = sorted(step.bind)
            elif isinstance(step, m.WaitStep):
                condition, code = await _wait_step(
                    step, ctx=ctx, env=env, locators=locators, deadline=step_deadline
                )
                result.details["condition"] = {
                    "operator": condition.operator,
                    "source": condition.source,
                    "verdict": condition.verdict,
                    "error": {"code": condition.code} if condition.code else None,
                    "actual": condition.actual,
                }
                if code is not None:
                    raise EngineStop(
                        "blocked" if condition.verdict == "unknown" else "failed", code
                    )
            elif isinstance(step, m.CheckStep):
                check = await _check_point(
                    step.checkpoint, ctx=ctx, env=env, locators=locators, deadline=step_deadline
                )
                result.details["check"] = check.as_dict()
                if check.status != "passed" and check.required:
                    raise EngineStop(
                        "failed" if check.status == "failed" else "blocked",
                        check.code or f"check_{check.status}:{check.id}",
                    )
            elif isinstance(step, m.ExtractStep):
                try:
                    async with asyncio.timeout(step_deadline.remaining()):
                        resolution = await _resolve(
                            ctx,
                            step.target,
                            locators,
                            timeout_ms=step_deadline.limit_ms(step.timeout_ms),
                        )
                except TimeoutError:
                    raise EngineStop("blocked", "deadline_exceeded") from None
                if step_deadline.expired:
                    raise EngineStop("blocked", "deadline_exceeded")
                extracted = _extract(step, resolution)
                env.define(step.as_, extracted)
                result.details["variable"] = step.as_
            elif isinstance(step, m.IfStep):
                condition = await evaluate(
                    step.condition,
                    driver=ctx.driver,
                    locators=locators,
                    deadline=step_deadline,
                    project=ctx.project,
                    scope=env,
                    secrets=ctx.secrets,
                )
                result.details["condition"] = {
                    "operator": condition.operator,
                    "source": condition.source,
                    "verdict": condition.verdict,
                    "error": {"code": condition.code} if condition.code else None,
                    "actual": condition.actual,
                }
                if condition.verdict == "unknown":
                    raise EngineStop("blocked", condition.code or "condition_unverified")
                branch = step.then if condition.verdict == "true" else step.else_
                if branch:
                    nested_start = len(results)
                    await _execute_steps(
                        branch,
                        ctx=ctx,
                        procedure=procedure,
                        env=env.child(),
                        deadline=step_deadline,
                        results=results,
                    )
                    result.details["branch"] = "then" if condition.verdict == "true" else "else"
                    result.details["steps"] = [item.as_dict() for item in results[nested_start:]]
                else:
                    result.details["branch"] = "then" if condition.verdict == "true" else "else"
                    result.details["steps"] = []
            elif isinstance(step, m.LoopStep):
                result.details.update(
                    await _loop(
                        step,
                        ctx=ctx,
                        procedure=procedure,
                        env=env,
                        deadline=step_deadline,
                        results=results,
                    )
                )
            else:
                raise EngineStop("blocked", "ai_step_unavailable")
        except EngineStop as stop:
            result.status = _step_status(stop)
            result.code = stop.code
        except DriverFault as fault:
            result.status = fault.status
            result.code = fault.code
        result.elapsed_ms = (time.monotonic() - started) * 1000
        results.append(result)
        _emit_step(
            ctx,
            "step_finish",
            id=step.id,
            type=step.type,
            status=result.status,
            code=result.code,
            elapsed_ms=round(result.elapsed_ms, 3),
        )
        if result.status != "passed":
            raise EngineStop(result.status, result.code or "step_failed")


def _page_anchor(case: m.Case) -> str | None:
    """The first required precondition's operator+target names the business page.

    It is only a cache bucket: a case without a required precondition simply does not use
    local experience, so two unrelated pages can never share an entry by accident.
    """
    for checkpoint in case.preconditions:
        if not checkpoint.required:
            continue
        target = checkpoint.condition.target
        name = target.locator_ref if isinstance(target, m.LocatorRef) else "inline"
        return f"{checkpoint.condition.operator}:{name}"
    return None


def _experience_hit(
    target: m.Target, locators: Mapping[str, m.Locator], ctx: _Context
) -> m.LocatorCandidate | None:
    """v0 never offers a remembered candidate: declared order is the only order.

    Reordering would change which candidate each P1.5 attempt evaluates, and ambiguity is
    decided per attempt. A remembered candidate could therefore let a run pass where the
    declared first candidate is ambiguous. Until an explicit ordering policy can prove a
    reorder is verdict-neutral, the hook is inert and the store is neither read nor written.
    """
    return None


async def _resolve(
    ctx: _Context,
    target: m.Target,
    locators: Mapping[str, m.Locator],
    *,
    timeout_ms: int,
    scope: m.Target | None = None,
) -> Resolution:
    primary = _experience_hit(target, locators, ctx)
    resolution = await ctx.driver.resolve(
        target, locators, timeout_ms=timeout_ms, scope=scope, primary=primary
    )
    _remember_resolution(ctx, target, locators, resolution, primary)
    return resolution


def _remember_resolution(
    ctx: _Context,
    target: m.Target,
    locators: Mapping[str, m.Locator],
    resolution: Resolution,
    primary: m.LocatorCandidate | None,
) -> None:
    """v0 records nothing.

    The store must not claim a hit that did not happen. Only a live resolution of an actually
    reordered candidate could justify ``remember``, and v0 never reorders. Writing here was
    also wrong for misses: a failed resolution used to increment the success counter.
    """
    return


async def _resolve_action_target(
    step: m.ActionStep,
    ctx: _Context,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
) -> tuple[str | None, Resolution | None]:
    action = step.action
    target: m.Target | None = None
    target_ref: str | None = None
    if isinstance(action, (m.TapAction, m.LongAction, m.InputAction)):
        target = action.target
        target_ref = target.locator_ref if isinstance(target, m.LocatorRef) else None
    if target is None:
        return None, None
    timeout = max(1, deadline.limit_ms(step.timeout_ms))
    try:
        async with asyncio.timeout(deadline.remaining()):
            return target_ref, await _resolve(ctx, target, locators, timeout_ms=timeout)
    except TimeoutError:
        raise EngineStop("blocked", "deadline_exceeded") from None


def _extract(step: m.ExtractStep, resolution: Resolution) -> TypedValue:
    if step.attribute == "count":
        if resolution.status == "indeterminate" or not resolution.query_complete:
            raise EngineStop("blocked", resolution.error_code or "source_unavailable")
        if not _source_matches(resolution, step.source) and resolution.matches:
            raise EngineStop("blocked", "source_mismatch")
        return TypedValue(len(resolution.matches), "integer")
    element = _one_element(resolution, step.source)
    if step.attribute == "text":
        text = element.text
        if text is None:
            raise EngineStop("blocked", "text_unverified")
        return TypedValue(text, "string")
    value = element.states.get(step.attribute)
    if value is None:
        raise EngineStop("blocked", "attribute_unverified")
    return TypedValue(value, "boolean")


async def _loop(
    step: m.LoopStep,
    *,
    ctx: _Context,
    procedure: m.Case | m.Flow,
    env: Scope,
    deadline: Deadline,
    results: list[StepResult],
) -> dict[str, object]:
    locators = ctx.locators(procedure)
    iterations: list[dict[str, object]] = []
    completed = 0
    while completed < step.max_iterations:
        if deadline.expired:
            raise EngineStop("blocked", "deadline_exceeded")
        if step.condition is not None:
            condition = await evaluate(
                step.condition,
                driver=ctx.driver,
                locators=locators,
                deadline=deadline,
                project=ctx.project,
                scope=env,
                secrets=ctx.secrets,
            )
            if condition.verdict == "unknown":
                raise EngineStop("blocked", condition.code or "condition_unverified")
            if condition.verdict == "false":
                return {"entries": completed, "limit_reached": False, "iterations": iterations}
        nested_start = len(results)
        await _execute_steps(
            step.steps,
            ctx=ctx,
            procedure=procedure,
            env=env.child(),
            deadline=deadline,
            results=results,
        )
        completed += 1
        iterations.append(
            {
                "index": completed,
                "steps": [item.as_dict() for item in results[nested_start:]],
            }
        )
    if step.condition is not None:
        condition = await evaluate(
            step.condition,
            driver=ctx.driver,
            locators=locators,
            deadline=deadline,
            project=ctx.project,
            scope=env,
            secrets=ctx.secrets,
        )
        if condition.verdict == "unknown":
            raise EngineStop("blocked", condition.code or "condition_unverified")
        if condition.verdict == "true":
            raise EngineStop("blocked", "loop_limit_reached")
    return {"entries": completed, "limit_reached": False, "iterations": iterations}


async def run_case(
    *,
    project: Project,
    driver: CaseDriver,
    case_id: str,
    profile_id: str,
    run_id: str | None = None,
    inputs: Mapping[str, m.Scalar] | None = None,
    secrets: Mapping[str, str] | None = None,
    activities: Mapping[str, str] | None = None,
    allow_clear: bool = False,
    source: ControlSource = "auto",
    strategy: m.Strategy | None = None,
    suite_deadline: Deadline | None = None,
    progress: Callable[[str, dict[str, object]], None] | None = None,
    evidence_root: Path | None = None,
    include_screenshot: bool = False,
    suite_id: str | None = None,
    experience: ExperienceStore | None = None,
    debug: DebugHook | None = None,
    raise_on_cancel: bool = True,
) -> RunResult:
    """Run one selected case against one already-connected driver session.

    Project structure/references must already be valid (``load_project``). Preflight issues
    block before any device read or write; a blocked preflight never starts a run. When
    ``evidence_root`` is given, every attempted run (including a preflight-blocked one) owns
    an exclusive ``<evidence_root>/<run_id>`` directory with JSON/HTML reports. Attachments
    are only written when the driver provides a sink and the run acquired a lease, so
    evidence cannot change a verdict or touch the device on its own.
    The local run manager uses ``raise_on_cancel=False`` to retain the canceled result after
    cleanup and report finalization; other callers keep normal task cancellation semantics.
    """
    identifier = run_id or str(uuid4())
    if re.fullmatch(UUID_PATTERN, identifier) is None:
        raise ValueError("run_id must be a canonical lowercase UUID")
    case = project.cases.get(case_id)
    profile = project.profiles.get(profile_id)
    started_epoch = time.time_ns() // 1_000_000
    started = time.monotonic()
    base = RunResult(identifier, case_id, profile_id, "blocked", started_at_ms=started_epoch)
    safe_case = _path_identifier(case_id)
    safe_profile = _path_identifier(profile_id)
    assets: tuple[EvidenceRecorder, ReportSink | None] | None = None

    def emit(event: str, **fields: object) -> None:
        _emit(
            progress,
            event,
            run_id=identifier,
            case_id=case_id,
            profile_id=profile_id,
            **fields,
        )

    def prepare() -> tuple[EvidenceRecorder, ReportSink | None] | None:
        if evidence_root is None:
            return None
        if safe_case is None or safe_profile is None:
            base.evidence = {
                "status": "unavailable",
                "issues": [{"code": "identifier_not_path_safe"}],
            }
            return None
        try:
            environment = driver.environment()
        except Exception:
            environment = ExecutionEnvironment()
        manifest = project.manifest
        recorder = EvidenceRecorder.create(
            evidence_root,
            run_id=identifier,
            case_id=case_id,
            profile_id=profile_id,
            environment=environment.as_dict(),
            metadata={
                "project_id": manifest.project_id,
                "name": manifest.name,
                "revision": manifest.revision,
                "strategy": str(strategy or manifest.default_strategy),
                "case_goal": case.goal if case is not None else None,
                "flow_refs": sorted(project.flows),
                "data_refs": sorted(project.data),
            },
            secrets=secrets,
        )
        sink: ReportSink | None = None
        serial = environment.device_serial
        if include_screenshot:
            try:
                sink = driver.report_sink(serial, None, identifier, recorder.directory)
            except Exception:
                sink = None
        emit("evidence_start", directory=str(recorder.directory))
        return recorder, sink

    def finalize() -> None:
        base.finished_at_ms = time.time_ns() // 1_000_000
        base.elapsed_ms = (time.monotonic() - started) * 1000
        if assets is None:
            emit("run_finish", status=base.status, reason=base.reason, elapsed_ms=base.elapsed_ms)
            return
        recorder, _ = assets
        try:
            base.evidence = recorder.finish(
                base.as_dict(),
                started_at_ms=started_epoch,
                finished_at_ms=base.finished_at_ms,
                elapsed_ms=base.elapsed_ms,
                suite_id=suite_id,
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, EvidenceError) else "evidence_write_failed"
            base.evidence = {"status": "unavailable", "issues": [{"code": code}]}
        emit("evidence_finish", status=(base.evidence or {}).get("run"))
        emit("run_finish", status=base.status, reason=base.reason, elapsed_ms=base.elapsed_ms)

    emit("run_start", strategy=strategy or project.manifest.default_strategy)
    if evidence_root is not None:
        try:
            assets = prepare()
        except EvidenceError as exc:
            base.evidence = {"status": "unavailable", "issues": [{"code": exc.code}]}
        except OSError:
            base.evidence = {
                "status": "unavailable",
                "issues": [{"code": "evidence_create_failed"}],
            }
    preflight_result = preflight(
        project,
        case_id,
        profile_id,
        strategy=strategy or project.manifest.default_strategy,
        inputs=dict(inputs or {}),
        available_secret_refs=frozenset(secrets or {}),
    )
    if not preflight_result.offline_ready:
        base.preflight_issues = [
            {"code": issue.code, "path": issue.path, "message": issue.message}
            for issue in preflight_result.issues
        ]
        base.reason = preflight_result.issues[0].code
        finalize()
        return base
    if case is None or profile is None:
        base.reason = "selection"
        finalize()
        return base
    issues = number_issues(case, project.flows)
    if issues:
        base.preflight_issues = issues
        base.reason = issues[0]["code"]
        finalize()
        return base
    deadline = Deadline.after(project.manifest.budgets.case_timeout_ms / 1000, suite_deadline)
    try:
        env = seed_scope(case, project=project, inputs=inputs, secrets=secrets)
    except EngineStop as stop:
        base.status = stop.status
        base.reason = stop.code
        finalize()
        return base
    lease: RunLease | None = None
    status: RunStatus = "passed"
    reason: str | None = None
    cancelled = False
    try:
        try:
            async with asyncio.timeout(deadline.remaining()):
                lease = await driver.begin(identifier, source=source)
        except DriverFault as fault:
            base.status = fault.status
            base.reason = fault.code
            finalize()
            return base
        except TimeoutError:
            base.reason = "deadline_exceeded"
            finalize()
            return base
        recorder = assets[0] if assets is not None else None
        sink = assets[1] if assets is not None else None
        ctx = _Context(
            project,
            case,
            profile,
            driver,
            lease,
            deadline,
            secrets,
            activities or {},
            allow_clear,
            progress,
            suite_id,
            recorder,
            sink,
            experience,
            _page_anchor(case),
            debug,
        )
        locators = ctx.locators(case)
        try:
            base.preconditions = []
            await _run_checks(
                case.preconditions,
                ctx=ctx,
                env=env,
                locators=locators,
                deadline=deadline,
                stop_on_required=True,
                results=base.preconditions,
            )
            base.steps = []
            await _execute_steps(
                case.steps,
                ctx=ctx,
                procedure=case,
                env=env,
                deadline=deadline,
                results=base.steps,
            )
            base.checks = []
            await _run_checks(
                case.checks,
                ctx=ctx,
                env=env,
                locators=locators,
                deadline=deadline,
                stop_on_required=False,
                results=base.checks,
            )
            required = [item for item in base.checks if item.required]
            if any(item.status == "failed" for item in required):
                status, reason = "failed", "check_failed"
            elif any(item.status == "blocked" for item in required):
                status, reason = "blocked", "check_blocked"
        except EngineStop as stop:
            status, reason = stop.status, stop.code
        except DriverFault as fault:
            status, reason = fault.status, fault.code
        except TimeoutError:
            status, reason = "blocked", "case_timeout"
    except asyncio.CancelledError:
        status, reason = "canceled", "cancelled"
        cancelled = True
    finally:
        if lease is not None:
            try:
                async with asyncio.timeout(5):
                    base.cleanup = await asyncio.shield(driver.finish(lease, state=status))
                cleanup_status = base.cleanup.get("run_status")
                if cleanup_status in ("canceled", "interrupted") and status != "failed":
                    status = "canceled" if cleanup_status == "canceled" else "interrupted"
                    reason = "control_epoch_changed" if status == "canceled" else "run_interrupted"
            except TimeoutError:
                base.cleanup = {"status": "failed", "error": {"code": "cleanup_timeout"}}
            except asyncio.CancelledError:
                base.cleanup = {"status": "failed", "error": {"code": "cleanup_cancelled"}}
                raise
            except Exception as exc:
                base.cleanup = {"status": "failed", "error": {"code": type(exc).__name__}}
        try:
            # An early return path (a fault from ``driver.begin`` or a deadline before the
            # lease exists) has already recorded a terminal state in ``base``; the default
            # local ``passed`` must not overwrite it.
            if lease is None and base.reason is not None:
                status = base.status
                reason = base.reason
            base.status = status
            base.reason = reason
            base.business_success = (
                True if status == "passed" else False if status == "failed" else None
            )
            finalize()
        except asyncio.CancelledError:
            raise
        except BaseException:
            # A report write must not mask the original cancellation or fault.
            if not cancelled:
                raise
    if cancelled and raise_on_cancel:
        raise asyncio.CancelledError
    return base
