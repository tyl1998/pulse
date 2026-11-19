"""P1.10 headless case runner: ``pulse-runtime run <project> --case --device``.

The CLI is a thin transport over one already-implemented ``run_case`` call on one connected
P1.2 session. It only reuses the project loader, the P1.5/P1.4/P1.6 Android driver and the P1.9
evidence layer; it does not add DSL fields, a public REST run API, a batch/suite scheduler or
automatic device preparation (no install, no Agent restart, no accessibility toggle).

Exit codes are the terminal-state contract, not a success/failure boolean:

    0  passed
    1  failed
    2  blocked
    3  interrupted
    4  canceled
    5  selection refused before running (arguments, unknown case/profile, preflight not ready,
       device not ready)
    6  project unusable (unreadable directory or invalid documents)
    7  invocation error raised by the argument parser itself

``stdout`` carries exactly one JSON document; human progress goes to ``stderr`` so a pipeline
can consume the result without stripping log lines.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn, TextIO, cast
from uuid import uuid4

from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.driver import ControlSource, RunStatus
from pulse_runtime.engine.interpreter import RunResult, run_case
from pulse_runtime.experience.local import FILE_NAME as EXPERIENCE_FILE
from pulse_runtime.experience.local import LocalExperience
from pulse_runtime.gate.ledger import ActionLedger, LedgerError
from pulse_runtime.gate.lock import LOCK_SUFFIX, LedgerLease
from pulse_runtime.paths import runtime_dir
from pulse_schema import models as m
from pulse_schema.validation import ContractError, Project, load_project
from pulse_schema.validation import preflight as project_preflight

CONTRACT = "pulse-runtime-cli-run-draft"
STRATEGIES = ("rule_only", "ai_on_locator_failure", "ai_led")
INVOCATION = 7
EXIT_CODES: Mapping[RunStatus, int] = {
    "passed": 0,
    "failed": 1,
    "blocked": 2,
    "interrupted": 3,
    "canceled": 4,
}
SELECTION = 5
PROJECT_INVALID = 6
_CONNECT_TIMEOUT_S = 30.0
_CLEANUP_TIMEOUT_S = 10.0
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LEDGER_CODES = frozenset({"ledger_in_use", "ledger_unusable", "ledger_open_failed"})


class CliError(Exception):
    """A refusal that ends the invocation before a terminal state is produced."""

    def __init__(self, stage: str, code: str, exit_code: int) -> None:
        super().__init__(code)
        self.stage = stage
        self.code = code
        self.exit_code = exit_code


class _Parser(argparse.ArgumentParser):
    """Raise instead of exiting so a bad invocation still prints one JSON document."""

    def error(self, message: str) -> NoReturn:
        raise CliError("arguments", "invalid_arguments", INVOCATION)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pulse-runtime run", description="Run one case on one device")
    parser.add_argument("project", help="project directory holding manifest.json")
    parser.add_argument("--case", action="append", dest="cases", metavar="CASE_ID")
    parser.add_argument("--device", metavar="SERIAL")
    parser.add_argument("--strategy", choices=STRATEGIES)
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="override one declared case parameter with a JSON scalar",
    )
    parser.add_argument("--evidence-root", metavar="DIR")
    parser.add_argument(
        "--experience",
        metavar="FILE",
        help="local locator-experience store (default: <runtime dir>/locator-experience.json)",
    )
    parser.add_argument(
        "--no-experience", action="store_true", help="do not read or write local experience"
    )
    parser.add_argument("--no-screenshot", action="store_true")
    parser.add_argument("--allow-clear", action="store_true")
    parser.add_argument(
        "--activity",
        action="append",
        default=[],
        metavar="APP_ID=ACTIVITY",
        help="explicit activity for an allowed app launched by the case",
    )
    return parser


def build_suite_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pulse-runtime run-suite", description="Run a case plan")
    parser.add_argument("project", help="project directory holding manifest.json")
    parser.add_argument(
        "--device",
        action="append",
        dest="devices",
        required=True,
        metavar="SERIAL",
        help="device serial; repeat for multiple devices (devices run one at a time)",
    )
    parser.add_argument("--case", action="append", dest="cases", metavar="CASE_ID")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="how many times each case runs on each device (1..20)",
    )
    parser.add_argument("--strategy", choices=STRATEGIES)
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="override one declared case parameter with a JSON scalar",
    )
    parser.add_argument("--evidence-root", metavar="DIR")
    parser.add_argument("--experience", metavar="FILE")
    parser.add_argument("--no-experience", action="store_true")
    parser.add_argument("--no-screenshot", action="store_true")
    parser.add_argument("--allow-clear", action="store_true")
    parser.add_argument(
        "--activity",
        action="append",
        default=[],
        metavar="APP_ID=ACTIVITY",
        help="explicit activity for an allowed app launched by the case",
    )
    return parser


def suite(argv: Sequence[str] | None = None) -> int:
    """``pulse-runtime run-suite``: same JSON contract, one aggregate document."""
    from pulse_runtime.engine.suite import run_suite

    parser = build_suite_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except CliError as exc:
        print(json.dumps(_refusal(exc.stage, exc.code, exc.exit_code), ensure_ascii=False))
        return exc.exit_code
    return run_suite(args)


def parse_inputs(pairs: Sequence[str]) -> dict[str, m.Scalar]:
    values: dict[str, m.Scalar] = {}
    for pair in pairs:
        name, separator, raw = pair.partition("=")
        if not separator or not name:
            raise CliError("arguments", "invalid_arguments", INVOCATION)
        try:
            values[name] = cast("m.Scalar", json.loads(raw))
        except ValueError:
            raise CliError("arguments", "invalid_arguments", INVOCATION) from None
    return values


def parse_activities(pairs: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        app_id, separator, activity = pair.partition("=")
        if not separator or not app_id or not activity:
            raise CliError("arguments", "invalid_arguments", INVOCATION)
        values[app_id] = activity
    return values


def _case_and_profile(
    project: Project, args: argparse.Namespace
) -> tuple[str, str, m.Strategy | None]:
    cases: list[str] = list(args.cases or [])
    if args.device is None:
        raise CliError("arguments", "invalid_arguments", INVOCATION)
    if not cases:
        raise CliError("arguments", "invalid_arguments", INVOCATION)
    if len(cases) != 1:
        raise CliError("selection", "single_case_required", SELECTION)
    case_id = cases[0]
    case = project.cases.get(case_id)
    if case is None:
        raise CliError("selection", "unknown_case", SELECTION)
    declared: list[str] = list(case.profile_refs)
    if not declared:
        raise CliError("selection", "unknown_profile", SELECTION)
    profile_id = declared[0]
    if profile_id not in project.profiles:
        raise CliError("selection", "unknown_profile", SELECTION)
    strategy = cast("m.Strategy | None", args.strategy)
    policy = strategy or project.manifest.default_strategy
    if policy not in STRATEGIES:
        raise CliError("selection", "strategy_unknown", SELECTION)
    return case_id, profile_id, strategy


def preflight_selection(
    project: Project,
    case_id: str,
    profile_id: str,
    strategy: m.Strategy | None,
    inputs: Mapping[str, m.Scalar],
) -> str | None:
    """Offline gate: refuse an unprepared selection before contacting any device."""
    try:
        ready = project_preflight(
            project,
            case_id,
            profile_id,
            strategy=strategy or project.manifest.default_strategy,
            inputs=dict(inputs),
        )
    except ContractError:
        return "project_invalid"
    if not ready.offline_ready:
        return ready.issues[0].code
    return None


def _progress(event: str, data: dict[str, object], stream: TextIO) -> None:
    clock = time.strftime("%H:%M:%S", time.localtime())
    if event == "run_start":
        print(f"[{clock}] run {data['case_id']} ({data['profile_id']})", file=stream, flush=True)
    elif event == "step_finish":
        code = f" {data['code']}" if data.get("code") else ""
        print(f"[{clock}]   {data['id']} {data['status']}{code}", file=stream, flush=True)
    elif event == "check_finish":
        code = f" {data['code']}" if data.get("code") else ""
        print(f"[{clock}]   check {data['id']} {data['status']}{code}", file=stream, flush=True)
    elif event == "evidence_start":
        print(f"[{clock}] evidence {data['directory']}", file=stream, flush=True)
    elif event == "run_finish":
        print(f"[{clock}] {data['status']} {data.get('reason')}", file=stream, flush=True)


def _ledger_code(exc: LedgerError | None) -> str:
    """Closed refusal code: an active lease is distinct from any other unusable ledger."""
    code = None if exc is None else str(exc)
    return code if code in _LEDGER_CODES else "ledger_unavailable"


def _open_ledger(path: Path, lease: LedgerLease) -> ActionLedger:
    """Open the ledger under an owned lease; a rejected open is a bounded refusal.

    Crash recovery is destructive for a live run, so it runs only after this process owns the
    lease. A pre-existing file that is not a SQLite database cannot hold any run/action state:
    it is discarded, while every other open failure keeps the file untouched for inspection.
    """
    try:
        ActionLedger(path, recover=False).close()
        return ActionLedger(path)
    except LedgerError as exc:
        if str(exc) == "ledger_unusable":
            for suffix in ("", "-wal", "-shm"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
            try:
                return ActionLedger(path)
            except LedgerError as retry:
                exc = retry
        lease.release()
        raise CliError("device", _ledger_code(exc), SELECTION) from None
    except (OSError, sqlite3.Error):
        lease.release()
        raise CliError("device", "ledger_unavailable", SELECTION) from None


def _evidence_issues(evidence: object) -> tuple[str | None, list[dict[str, object]]]:
    """Evidence status and issues from a finished report, or an unavailable envelope."""
    if not isinstance(evidence, dict):
        return None, []
    if evidence.get("status") == "unavailable":
        raw = evidence.get("issues")
        return "unavailable", _dicts(raw)
    inner = evidence.get("evidence")
    raw = inner.get("issues") if isinstance(inner, dict) else None
    return "written", _dicts(raw)


def _dicts(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _report(
    run: RunResult,
    project: Project,
    case_id: str,
    profile_id: str,
    evidence_root: Path | None,
) -> dict[str, object]:
    """Public, script-friendly summary; full detail lives in the run's own report directory."""
    evidence = run.evidence
    directory: str | None = None
    directory_origin: str | None = None
    status, issues = _evidence_issues(evidence)
    report_path: Path | None = None
    if evidence_root is not None and _UUID.fullmatch(run.run_id) is not None:
        run_directory = (evidence_root / run.run_id).absolute()
        if run_directory.is_dir():
            directory = str(run_directory)
            directory_origin = "evidence_root/run_id"
        report_path = run_directory / "result.json"
    if status is not None and not (report_path is not None and report_path.is_file()):
        status = "incomplete" if directory is not None else "unavailable"
    return {
        "contract": CONTRACT,
        "project": {
            "project_id": project.manifest.project_id,
            "revision": project.manifest.revision,
        },
        "case_id": case_id,
        "profile_id": profile_id,
        "run_id": run.run_id,
        "status": run.status,
        "reason": run.reason,
        "business_success": run.business_success,
        "preflight_issues": list(run.preflight_issues),
        "counts": {
            "preconditions": len(run.preconditions),
            "steps": len(run.steps),
            "checks": len(run.checks),
        },
        "elapsed_ms": round(run.elapsed_ms, 3),
        "evidence": {
            "status": status,
            "directory": directory,
            "directory_origin": directory_origin,
            "issues": issues,
        },
        "exit_code": EXIT_CODES[run.status],
    }


def _refusal(stage: str, code: str, exit_code: int) -> dict[str, object]:
    return {
        "contract": CONTRACT,
        "status": "refused",
        "reason": code,
        "stage": stage,
        "exit_code": exit_code,
    }


async def _release_own_control(
    manager: DeviceConnectionManager, serial: str, session_id: str
) -> str | None:
    """Release a control holder this run left behind; never take over someone else's."""
    control = manager.gate.snapshot(serial)
    if control.session_id != session_id or control.holder_source is None:
        return None
    if control.epoch < 0:
        return None
    await manager.release_control(
        serial,
        session_id=session_id,
        control_epoch=control.epoch,
        run_id=control.run_id,
    )
    return control.holder_source


@asynccontextmanager
async def _device_lease(
    serial: str, evidence_root: Path | None
) -> AsyncIterator[tuple[DeviceConnectionManager, str]]:
    """One owned ledger + one connected P1.2 session for exactly as long as the bracket.

    The suite reuses this bracket to keep a device connected across repeated runs; each run
    still acquires and releases the P1.6 control gate on its own, so runs stay serial and a
    crash cannot leave a foreign holder behind.
    """
    ledger_path = evidence_root / "runtime" / "ledger.sqlite3" if evidence_root else None
    lease: LedgerLease | None = None
    ledger: ActionLedger | None = None
    manager: DeviceConnectionManager | None = None
    try:
        if ledger_path is not None:
            try:
                lease = LedgerLease(Path(f"{ledger_path}{LOCK_SUFFIX}"))
            except LedgerError as exc:
                # Another CLI or Runtime owns this ledger; recovery must not touch its live run.
                raise CliError("device", _ledger_code(exc), SELECTION) from None
            ledger = _open_ledger(ledger_path, lease)
        try:
            manager = DeviceConnectionManager(
                poll_interval=0.5, ledger_path=ledger_path, ledger=ledger
            )
        except LedgerError as exc:
            raise CliError("device", _ledger_code(exc), SELECTION) from None
        except (OSError, sqlite3.Error):
            raise CliError("device", "ledger_unavailable", SELECTION) from None
        try:
            connected = await manager.connect(serial, timeout=_CONNECT_TIMEOUT_S)
        except ConnectionFault as exc:
            raise CliError("device", exc.code, SELECTION) from None
        slot = manager.slot(serial)
        session = slot.session
        if connected.get("ready") is not True or session is None:
            raise CliError("device", slot.code or slot.status or "device_not_ready", SELECTION)
        yield manager, session.id
    finally:
        try:
            if manager is not None:
                await manager.close()
        finally:
            if lease is not None:
                lease.release()


async def _cleanup_device(
    manager: DeviceConnectionManager, serial: str, session_id: str, *, disconnect: bool
) -> tuple[dict[str, object], dict[str, object]]:
    """Release only this session's control, then (optionally) close the private connection."""
    cleanup: dict[str, object] = {}
    try:
        released = await asyncio.wait_for(
            _release_own_control(manager, serial, session_id), _CLEANUP_TIMEOUT_S
        )
        cleanup["released_source"] = released
    except ConnectionFault as exc:
        cleanup["release_error"] = exc.code
    except TimeoutError:
        cleanup["release_error"] = "cleanup_timeout"
    if disconnect:
        try:
            await asyncio.wait_for(manager.disconnect(serial, session_id), _CLEANUP_TIMEOUT_S)
            cleanup["connection"] = "disconnected"
        except ConnectionFault as exc:
            cleanup["connection"] = f"failed:{exc.code}"
        except TimeoutError:
            cleanup["connection"] = "failed:timeout"
    return cleanup, manager.gate.snapshot(serial).as_dict()


async def _execute_once(
    project: Project,
    *,
    case_id: str,
    profile_id: str,
    manager: DeviceConnectionManager,
    serial: str,
    session_id: str,
    strategy: m.Strategy | None,
    inputs: Mapping[str, m.Scalar],
    activities: Mapping[str, str],
    allow_clear: bool,
    evidence_root: Path | None,
    include_screenshot: bool,
    experience: LocalExperience | None,
    progress_stream: TextIO,
    suite_deadline: Deadline | None = None,
) -> dict[str, object]:
    """One ``run_case`` on an already-connected session, with its report and cleanup."""
    profile = project.profiles[profile_id]
    deadline = suite_deadline or Deadline.after(project.manifest.budgets.suite_timeout_ms / 1000)
    driver = AndroidCaseDriver(manager, serial=serial, session_id=session_id, profile=profile)
    source: ControlSource = "auto"
    run = await run_case(
        project=project,
        driver=driver,
        case_id=case_id,
        profile_id=profile_id,
        run_id=str(uuid4()),
        inputs=dict(inputs),
        activities=dict(activities),
        allow_clear=allow_clear,
        source=source,
        strategy=strategy,
        suite_deadline=deadline,
        progress=lambda event, data: _progress(event, data, progress_stream),
        evidence_root=evidence_root,
        include_screenshot=include_screenshot,
        experience=experience,
    )
    report = _report(run, project, case_id, profile_id, evidence_root)
    cleanup, control = await _cleanup_device(manager, serial, session_id, disconnect=False)
    report["cleanup"] = cleanup
    report["control"] = control
    return report


async def _run_on_device(
    project: Project,
    *,
    case_id: str,
    profile_id: str,
    serial: str,
    strategy: m.Strategy | None,
    inputs: Mapping[str, m.Scalar],
    activities: Mapping[str, str],
    allow_clear: bool,
    evidence_root: Path | None,
    include_screenshot: bool,
    experience_path: Path | None,
    progress_stream: TextIO,
) -> tuple[dict[str, object], int]:
    """One case on one device: connect, run, release, disconnect, close."""
    experience = LocalExperience(experience_path) if experience_path is not None else None
    async with _device_lease(serial, evidence_root) as (manager, session_id):
        report = await _execute_once(
            project,
            case_id=case_id,
            profile_id=profile_id,
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
        )
        report["experience"] = _experience_report(experience)
        cleanup, control = await _cleanup_device(manager, serial, session_id, disconnect=True)
        report["cleanup"] = cleanup
        report["control"] = control
        return report, cast(int, report["exit_code"])


def _experience_report(experience: LocalExperience | None) -> dict[str, object]:
    """Speed-only cache summary: a hit count is never a business verdict."""
    if experience is None:
        return {"status": "disabled", "path": None, "scope": None, "entries": 0}
    try:
        experience.save()
        saved = True
    except OSError:
        saved = False
    return {
        "status": "corrupt" if experience.broken else "ready",
        "path": str(experience.path),
        "scope": "project+app+page+intent+target",
        "entries": len(experience.records),
        "saved": saved,
    }


def experience_path(args: argparse.Namespace) -> Path | None:
    if args.no_experience:
        return None
    return Path(args.experience) if args.experience else runtime_dir() / EXPERIENCE_FILE


def run(args: argparse.Namespace, *, progress_stream: TextIO = sys.stderr) -> int:
    """Load, execute, print one JSON document to stdout, and return the exit code."""
    try:
        inputs = parse_inputs(args.input)
        activities = parse_activities(args.activity)
        project = load_project(Path(args.project))
        case_id, profile_id, strategy = _case_and_profile(project, args)
        refused = preflight_selection(project, case_id, profile_id, strategy, inputs)
        if refused is not None:
            raise CliError("selection", refused, SELECTION)
        report, code = asyncio.run(
            _run_on_device(
                project,
                case_id=case_id,
                profile_id=profile_id,
                serial=args.device,
                strategy=strategy,
                inputs=inputs,
                activities=activities,
                allow_clear=bool(args.allow_clear),
                evidence_root=Path(args.evidence_root) if args.evidence_root else None,
                include_screenshot=not args.no_screenshot,
                experience_path=experience_path(args),
                progress_stream=progress_stream,
            )
        )
    except CliError as exc:
        print(json.dumps(_refusal(exc.stage, exc.code, exc.exit_code), ensure_ascii=False))
        return exc.exit_code
    except ContractError:
        print(
            json.dumps(_refusal("project", "project_invalid", PROJECT_INVALID), ensure_ascii=False)
        )
        return PROJECT_INVALID
    except OSError:
        print(
            json.dumps(
                _refusal("project", "project_unreadable", PROJECT_INVALID), ensure_ascii=False
            )
        )
        return PROJECT_INVALID
    print(json.dumps(report, ensure_ascii=False))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except CliError as exc:
        print(json.dumps(_refusal(exc.stage, exc.code, exc.exit_code), ensure_ascii=False))
        return exc.exit_code
    return run(args)
