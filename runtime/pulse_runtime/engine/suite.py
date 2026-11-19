"""P1 exit suite runner: many cases x many devices x repeats, one aggregate report.

This is the P1.10 ``run`` semantics applied N times with a deterministic plan, not a scheduler:

* the plan is the full cartesian product ``devices x cases x repeats`` in that order, so the
  same arguments always produce the same list of attempts;
* one device is used at a time (the plan is grouped per device). Each attempt still acquires
  and releases the P1.6 control gate on its own, so a crash or takeover is handled exactly as
  in a single run and never leaves a foreign holder behind;
* every attempt keeps its own exclusive P1.9 evidence directory below
  ``<evidence-root>/runs/<run_id>``; the suite report is written after the last attempt;
* a failed attempt never stops the plan: an exit code is a terminal state, not a reason to
  skip the remaining repetitions. Only a *refusal* (device gone, ledger busy, project
  unusable) ends the plan early, because nothing else can run.

The aggregate report separates rates the docs require to stay separate: attempt completion,
business pass rate and per-case/per-device breakdowns. A cache or a retry can never turn a
business failure into a pass, so ``passed`` is never recomputed from timings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, cast
from uuid import uuid4

from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.cli import (
    SELECTION,
    CliError,
    _device_lease,
    _execute_once,
    _refusal,
)
from pulse_runtime.engine.driver import RunStatus
from pulse_runtime.experience.local import LocalExperience
from pulse_runtime.gate.ledger import LedgerError
from pulse_schema import models as m
from pulse_schema.validation import ContractError, Project, load_project

SUITE_CONTRACT = "pulse-runtime-suite-draft"
EXIT_CODES: Mapping[RunStatus, int] = {
    "passed": 0,
    "failed": 1,
    "blocked": 2,
    "interrupted": 3,
    "canceled": 4,
}
MIN_REPEAT = 1
MAX_REPEAT = 20
MAX_ATTEMPTS = 240


class SuitePlanError(Exception):
    """A plan that cannot be built from the arguments; always a refusal, never a run."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Attempt:
    serial: str
    case_id: str
    profile_id: str
    repeat: int

    @property
    def key(self) -> str:
        return f"{self.serial}/{self.case_id}#{self.repeat}"


@dataclass(frozen=True)
class SuitePlan:
    devices: tuple[str, ...]
    cases: tuple[str, ...]
    repeats: int
    attempts: tuple[Attempt, ...]

    @property
    def total(self) -> int:
        return len(self.attempts)


def case_profile(project: Project, case_id: str) -> str:
    """The first declared profile of a case; the same rule the single-case CLI uses."""
    case = project.cases[case_id]
    if not case.profile_refs:
        raise SuitePlanError("unknown_profile")
    profile_id = case.profile_refs[0]
    if profile_id not in project.profiles:
        raise SuitePlanError("unknown_profile")
    return profile_id


def build_plan(
    project: Project,
    *,
    devices: Sequence[str],
    cases: Sequence[str] | None,
    repeats: int,
) -> SuitePlan:
    """Validate the selection and expand the deterministic attempt list.

    Refusals happen before any device is contacted: unknown/duplicate device, unknown case,
    unknown profile, an empty case list, a repeat count outside ``1..20`` and a plan larger
    than ``240`` attempts (which a bounded P1 exit run never needs).
    """
    device_list = [str(item) for item in devices]
    if not device_list:
        raise SuitePlanError("device_required")
    if len(set(device_list)) != len(device_list):
        raise SuitePlanError("duplicate_device")
    for serial in device_list:
        if (
            not serial
            or len(serial) > 64
            or any(not (char.isalnum() or char in "._:-") for char in serial)
        ):
            raise SuitePlanError("invalid_device")
    if isinstance(repeats, bool) or not isinstance(repeats, int):
        raise SuitePlanError("invalid_repeat")
    if not MIN_REPEAT <= repeats <= MAX_REPEAT:
        raise SuitePlanError("invalid_repeat")
    selected = list(cases or [])
    if not selected:
        raise SuitePlanError("case_required")
    if len(set(selected)) != len(selected):
        raise SuitePlanError("duplicate_case")
    for case_id in selected:
        if case_id not in project.cases:
            raise SuitePlanError("unknown_case")
        case_profile(project, case_id)
    if len(device_list) * len(selected) * repeats > MAX_ATTEMPTS:
        raise SuitePlanError("plan_too_large")
    attempts: list[Attempt] = []
    for serial in device_list:
        for case_id in selected:
            profile_id = case_profile(project, case_id)
            for repeat in range(1, repeats + 1):
                attempts.append(Attempt(serial, case_id, profile_id, repeat))
    return SuitePlan(tuple(device_list), tuple(selected), repeats, tuple(attempts))


def plan_by_device(plan: SuitePlan) -> list[tuple[str, list[Attempt]]]:
    """Group attempts per device while keeping the declared order; devices run in sequence."""
    groups: list[tuple[str, list[Attempt]]] = []
    for serial in plan.devices:
        entries = [item for item in plan.attempts if item.serial == serial]
        groups.append((serial, entries))
    return groups


@dataclass
class SuiteTotals:
    counts: dict[str, int] = field(default_factory=dict)
    attempts: int = 0
    durations_ms: float = 0.0

    def add(self, status: object, elapsed_ms: object) -> None:
        name = status if isinstance(status, str) else "unknown"
        self.counts[name] = self.counts.get(name, 0) + 1
        self.attempts += 1
        if isinstance(elapsed_ms, int | float) and not isinstance(elapsed_ms, bool):
            self.durations_ms += float(elapsed_ms)

    def as_dict(self) -> dict[str, object]:
        terminal = sum(count for name, count in self.counts.items() if name in EXIT_CODES)
        return {
            "attempts": self.attempts,
            "terminal": terminal,
            "terminal_rate": round(terminal / self.attempts, 4) if self.attempts else 0.0,
            "passed": self.counts.get("passed", 0),
            "failed": self.counts.get("failed", 0),
            "blocked": self.counts.get("blocked", 0),
            "interrupted": self.counts.get("interrupted", 0),
            "canceled": self.counts.get("canceled", 0),
            "refused": self.counts.get("refused", 0),
            # A pass rate over attempts, never over "attempts that did not crash".
            "pass_rate": round(self.counts.get("passed", 0) / self.attempts, 4)
            if self.attempts
            else 0.0,
            "elapsed_ms": round(self.durations_ms, 3),
        }


def build_suite_report(
    *,
    project: Project,
    plan: SuitePlan,
    observations: Sequence[Mapping[str, object]],
    suite_id: str,
    evidence_root: Path | None,
    aborted: str | None = None,
) -> dict[str, object]:
    """Aggregate the per-attempt reports into the one document printed on stdout."""
    by_case: dict[str, SuiteTotals] = {case_id: SuiteTotals() for case_id in plan.cases}
    by_device: dict[str, SuiteTotals] = {serial: SuiteTotals() for serial in plan.devices}
    overall = SuiteTotals()
    rows: list[dict[str, object]] = []
    for item in observations:
        status = item.get("status")
        case_id = item.get("case_id")
        serial = item.get("serial")
        overall.add(status, item.get("elapsed_ms"))
        if isinstance(case_id, str) and case_id in by_case:
            by_case[case_id].add(status, item.get("elapsed_ms"))
        if isinstance(serial, str) and serial in by_device:
            by_device[serial].add(status, item.get("elapsed_ms"))
        rows.append(dict(item))
    missing = plan.total - len(observations)
    return {
        "contract": SUITE_CONTRACT,
        "project": {
            "project_id": project.manifest.project_id,
            "revision": project.manifest.revision,
        },
        "suite_id": suite_id,
        "plan": {
            "devices": list(plan.devices),
            "cases": list(plan.cases),
            "repeats": plan.repeats,
            "attempts": plan.total,
            "executed": len(observations),
            "not_run": missing,
        },
        "totals": overall.as_dict(),
        "by_case": {name: totals.as_dict() for name, totals in by_case.items()},
        "by_device": {name: totals.as_dict() for name, totals in by_device.items()},
        "attempts": rows,
        "aborted": aborted,
        "evidence_root": None if evidence_root is None else str(evidence_root),
        "exit_code": suite_exit_code(overall, missing=missing, aborted=aborted),
    }


def suite_exit_code(totals: SuiteTotals, *, missing: int, aborted: str | None) -> int:
    """One code for the whole plan, ordered by severity so a pipeline cannot miss a failure.

    A truncated plan or an unexpected error is a selection-level refusal (5). Otherwise the
    worst terminal state wins; an all-passed plan returns 0.
    """
    if aborted is not None or missing > 0 or totals.counts.get("refused"):
        return 5
    for status in ("interrupted", "canceled", "blocked", "failed"):
        if totals.counts.get(status):
            return EXIT_CODES[status]
    if totals.counts.get("passed") == totals.attempts and totals.attempts:
        return 0
    return 5


def write_suite_report(path: Path, report: Mapping[str, object]) -> None:
    """Atomic write; the suite report never shares a directory with a run's evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def suite_deadline(project: Project) -> Deadline:
    return Deadline.after(project.manifest.budgets.suite_timeout_ms / 1000)


async def execute_plan(
    project: Project,
    plan: SuitePlan,
    *,
    evidence_root: Path | None,
    strategy: m.Strategy | None,
    inputs: Mapping[str, m.Scalar],
    activities: Mapping[str, str],
    allow_clear: bool,
    include_screenshot: bool,
    experience_path: Path | None,
    progress_stream: TextIO,
) -> tuple[list[dict[str, object]], str | None]:
    """Run the plan per device, in order, one attempt at a time.

    Returns the observed attempts and an optional abort code. A run that reaches a terminal
    state is recorded and the plan continues; a refusal (device, ledger, connection) or the
    suite deadline aborts the remaining attempts instead of pretending they ran.
    """
    observations: list[dict[str, object]] = []
    experience = LocalExperience(experience_path) if experience_path is not None else None
    deadline = suite_deadline(project)
    aborted: str | None = None
    for serial, entries in plan_by_device(plan):
        if aborted is not None:
            break
        try:
            async with _device_lease(serial, evidence_root) as (manager, session_id):
                for item in entries:
                    if deadline.expired:
                        aborted = "suite_deadline_exceeded"
                        break
                    _suite_progress(progress_stream, item, "attempt_start")
                    try:
                        report = await _execute_once(
                            project,
                            case_id=item.case_id,
                            profile_id=item.profile_id,
                            manager=manager,
                            serial=serial,
                            session_id=session_id,
                            strategy=strategy,
                            inputs=inputs,
                            activities=activities,
                            allow_clear=allow_clear,
                            evidence_root=evidence_root,
                            include_screenshot=include_screenshot,
                            experience=experience,
                            progress_stream=progress_stream,
                            suite_deadline=deadline,
                        )
                    except CliError as exc:
                        aborted = exc.code
                        observations.append(_refused_attempt(item, exc.code))
                        break
                    except ConnectionFault as exc:
                        aborted = exc.code
                        observations.append(_refused_attempt(item, exc.code))
                        break
                    except (LedgerError, sqlite3.Error, OSError) as exc:
                        aborted = "device_unavailable"
                        observations.append(
                            _refused_attempt(item, f"device_unavailable:{type(exc).__name__}")
                        )
                        break
                    observations.append(_attempt_row(item, report))
                    _suite_progress(
                        progress_stream, item, "attempt_finish", status=report.get("status")
                    )
        except CliError as exc:
            aborted = exc.code
            observations.append(_refused_attempt(entries[0], exc.code))
        except (LedgerError, sqlite3.Error, OSError) as exc:
            aborted = "device_unavailable"
            observations.append(
                _refused_attempt(entries[0], f"device_unavailable:{type(exc).__name__}")
            )
    if experience is not None:
        with suppress(OSError):
            experience.save()
    return observations, aborted


def _attempt_row(item: Attempt, report: Mapping[str, object]) -> dict[str, object]:
    return {
        "serial": item.serial,
        "case_id": item.case_id,
        "profile_id": item.profile_id,
        "repeat": item.repeat,
        "run_id": report.get("run_id"),
        "status": report.get("status"),
        "reason": report.get("reason"),
        "exit_code": report.get("exit_code"),
        "elapsed_ms": report.get("elapsed_ms"),
        "evidence": report.get("evidence"),
        "cleanup": report.get("cleanup"),
        "control": report.get("control"),
    }


def _refused_attempt(item: Attempt, code: str) -> dict[str, object]:
    return {
        "serial": item.serial,
        "case_id": item.case_id,
        "profile_id": item.profile_id,
        "repeat": item.repeat,
        "run_id": None,
        "status": "refused",
        "reason": code,
        "exit_code": SELECTION,
        "elapsed_ms": None,
        "evidence": None,
        "cleanup": None,
        "control": None,
    }


def _suite_progress(stream: TextIO, item: Attempt, event: str, *, status: object = None) -> None:
    clock = time.strftime("%H:%M:%S", time.localtime())
    if event == "attempt_start":
        print(
            f"[{clock}] attempt {item.key} ({item.profile_id})",
            file=stream,
            flush=True,
        )
    else:
        print(f"[{clock}] attempt {item.key} {status}", file=stream, flush=True)


def run_suite(args: argparse.Namespace, *, progress_stream: TextIO = sys.stderr) -> int:
    """CLI body: load, plan, execute, print one aggregate JSON document."""
    namespace = args
    try:
        inputs = _parse_inputs(namespace.input)
        activities = _parse_activities(namespace.activity)
        project = load_project(Path(namespace.project))
        plan = build_plan(
            project,
            devices=cast(Sequence[str], namespace.devices),
            cases=cast(Sequence[str] | None, namespace.cases),
            repeats=cast(int, namespace.repeat),
        )
        strategy = cast("m.Strategy | None", namespace.strategy)
        refused = _preflight(project, plan, strategy, inputs)
        if refused is not None:
            raise CliError("selection", refused, SELECTION)
        evidence_root = Path(namespace.evidence_root) if namespace.evidence_root else None
        experience = (
            None
            if namespace.no_experience
            else Path(namespace.experience)
            if namespace.experience
            else _default_experience_path()
        )
        suite_id = str(uuid4())
        observations, aborted = asyncio.run(
            execute_plan(
                project,
                plan,
                evidence_root=evidence_root,
                strategy=strategy,
                inputs=inputs,
                activities=activities,
                allow_clear=bool(namespace.allow_clear),
                include_screenshot=not bool(namespace.no_screenshot),
                experience_path=experience,
                progress_stream=progress_stream,
            )
        )
        report = build_suite_report(
            project=project,
            plan=plan,
            observations=observations,
            suite_id=suite_id,
            evidence_root=evidence_root,
            aborted=aborted,
        )
        if evidence_root is not None:
            try:
                write_suite_report(evidence_root / "suite" / f"{suite_id}.json", report)
            except OSError:
                report = {**report, "suite_report": {"status": "unavailable"}}
    except SuitePlanError as exc:
        print(json.dumps(_refusal("selection", exc.code, SELECTION), ensure_ascii=False))
        return SELECTION
    except CliError as exc:
        print(json.dumps(_refusal(exc.stage, exc.code, exc.exit_code), ensure_ascii=False))
        return exc.exit_code
    except ContractError:
        print(json.dumps(_refusal("project", "project_invalid", 6), ensure_ascii=False))
        return 6
    except OSError:
        print(json.dumps(_refusal("project", "project_unreadable", 6), ensure_ascii=False))
        return 6
    print(json.dumps(report, ensure_ascii=False))
    return cast(int, report["exit_code"])


def _preflight(
    project: Project,
    plan: SuitePlan,
    strategy: m.Strategy | None,
    inputs: Mapping[str, m.Scalar],
) -> str | None:
    from pulse_runtime.engine.cli import preflight_selection

    policy = strategy or project.manifest.default_strategy
    if policy not in ("rule_only", "ai_on_locator_failure", "ai_led"):
        return "strategy_unknown"
    for case_id in plan.cases:
        refused = preflight_selection(
            project, case_id, case_profile(project, case_id), strategy, inputs
        )
        if refused is not None:
            return refused
    return None


def _parse_inputs(pairs: object) -> dict[str, m.Scalar]:
    from pulse_runtime.engine.cli import parse_inputs

    return parse_inputs(cast(Sequence[str], pairs or []))


def _parse_activities(pairs: object) -> dict[str, str]:
    from pulse_runtime.engine.cli import parse_activities

    return parse_activities(cast(Sequence[str], pairs or []))


def _default_experience_path() -> Path:
    from pulse_runtime.experience.local import FILE_NAME
    from pulse_runtime.paths import runtime_dir

    return runtime_dir() / FILE_NAME


__all__ = [
    "EXIT_CODES",
    "MAX_ATTEMPTS",
    "Attempt",
    "SuitePlan",
    "SuitePlanError",
    "build_plan",
    "build_suite_report",
    "execute_plan",
    "plan_by_device",
    "run_suite",
    "suite_exit_code",
    "write_suite_report",
]
