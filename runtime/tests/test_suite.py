"""P1 exit suite runner: deterministic plan, per-device serial execution, aggregate report.

No phone, ADB, Agent, public REST endpoint or background Runtime is contacted. The plan and
the aggregate report are pure functions; the execution path runs the real interpreter over a
scripted driver with the CLI's fake device manager, so gate release, evidence and refusal
handling are the production code paths.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pulse_runtime.engine import cli
from pulse_runtime.engine.driver import Resolution
from pulse_runtime.engine.suite import (
    EXIT_CODES,
    MAX_ATTEMPTS,
    SuitePlanError,
    SuiteTotals,
    build_plan,
    build_suite_report,
    plan_by_device,
    run_suite,
    suite_exit_code,
    write_suite_report,
)
from pulse_schema.validation import load_project
from runtime.tests.test_cli import FakeManager, TitleDriver, write_project

APP = "com.pulse.target"


@pytest.fixture(name="project_dir")
def project_dir_fixture(tmp_path: Path) -> Path:
    return write_project(tmp_path / "project")


def plan_args(project_dir: Path, *extra: str) -> list[str]:
    return [str(project_dir), *extra]


def test_plan_is_devices_then_cases_then_repeats(project_dir: Path) -> None:
    project = load_project(project_dir)
    plan = build_plan(project, devices=["B", "A"], cases=["happy"], repeats=2)
    assert plan.devices == ("B", "A")
    assert plan.repeats == 2
    assert plan.total == 4
    assert [(item.serial, item.repeat) for item in plan.attempts] == [
        ("B", 1),
        ("B", 2),
        ("A", 1),
        ("A", 2),
    ]
    assert all(item.profile_id == "fake" for item in plan.attempts)
    groups = plan_by_device(plan)
    assert [name for name, _ in groups] == ["B", "A"]
    assert all(item.serial == name for name, entries in groups for item in entries)


@pytest.mark.parametrize(
    ("devices", "cases", "repeats", "code"),
    [
        ([], ["happy"], 1, "device_required"),
        (["A", "A"], ["happy"], 1, "duplicate_device"),
        (["bad serial"], ["happy"], 1, "invalid_device"),
        (["A"], [], 1, "case_required"),
        (["A"], ["happy", "happy"], 1, "duplicate_case"),
        (["A"], ["ghost"], 1, "unknown_case"),
        (["A"], ["happy"], 0, "invalid_repeat"),
        (["A"], ["happy"], 21, "invalid_repeat"),
    ],
)
def test_plan_refusals(project_dir: Path, devices, cases, repeats, code: str) -> None:
    project = load_project(project_dir)
    with pytest.raises(SuitePlanError) as caught:
        build_plan(project, devices=devices, cases=cases, repeats=repeats)
    assert caught.value.code == code


def test_plan_size_is_bounded(project_dir: Path) -> None:
    project = load_project(project_dir)
    devices = [f"dev-{index:02d}" for index in range(13)]
    with pytest.raises(SuitePlanError) as caught:
        build_plan(project, devices=devices, cases=["happy"], repeats=20)
    assert caught.value.code == "plan_too_large"
    assert MAX_ATTEMPTS < 13 * 20


def test_totals_keep_rates_separate() -> None:
    totals = SuiteTotals()
    for status, elapsed in (("passed", 10.0), ("failed", 20.0), ("blocked", 30.0)):
        totals.add(status, elapsed)
    totals.add("refused", None)
    data = totals.as_dict()
    assert data["attempts"] == 4
    assert data["terminal"] == 3
    assert data["terminal_rate"] == 0.75
    assert data["pass_rate"] == 0.25
    assert data["elapsed_ms"] == 60.0
    assert data["refused"] == 1


@pytest.mark.parametrize(
    ("counts", "missing", "aborted", "code"),
    [
        ({"passed": 3}, 0, None, 0),
        ({"passed": 2, "failed": 1}, 0, None, 1),
        ({"passed": 2, "failed": 1, "blocked": 1}, 0, None, 2),
        ({"passed": 1, "blocked": 1, "interrupted": 1}, 0, None, 3),
        ({"passed": 1, "canceled": 1}, 0, None, 4),
        ({"passed": 2}, 1, None, 5),
        ({"passed": 2}, 0, "device_disconnected", 5),
        ({"passed": 1, "refused": 1}, 0, None, 5),
    ],
)
def test_suite_exit_code_severity(counts, missing, aborted, code) -> None:
    totals = SuiteTotals(counts=dict(counts), attempts=sum(counts.values()))
    assert suite_exit_code(totals, missing=missing, aborted=aborted) == code


def test_report_separates_rates_and_keeps_unrun_attempts(tmp_path: Path) -> None:
    project = load_project(write_project(tmp_path / "project"))
    plan = build_plan(project, devices=["A", "B"], cases=["happy"], repeats=2)
    observations = [
        {
            "serial": "A",
            "case_id": "happy",
            "profile_id": "fake",
            "repeat": 1,
            "status": "passed",
            "elapsed_ms": 12.0,
        },
        {
            "serial": "A",
            "case_id": "happy",
            "profile_id": "fake",
            "repeat": 2,
            "status": "failed",
            "elapsed_ms": 13.0,
        },
    ]
    report = build_suite_report(
        project=project,
        plan=plan,
        observations=observations,
        suite_id="00000000-0000-4000-8000-00000000000a",
        evidence_root=tmp_path / "evidence",
        aborted="device_disconnected",
    )
    assert report["plan"]["attempts"] == 4
    assert report["plan"]["executed"] == 2
    assert report["plan"]["not_run"] == 2
    assert report["totals"]["passed"] == 1
    assert report["totals"]["failed"] == 1
    assert report["totals"]["pass_rate"] == 0.5
    assert report["by_case"]["happy"]["attempts"] == 2
    assert report["by_device"]["A"]["attempts"] == 2
    assert report["by_device"]["B"]["attempts"] == 0
    assert report["aborted"] == "device_disconnected"
    assert report["exit_code"] == 5
    path = tmp_path / "evidence" / "suite" / "report.json"
    write_suite_report(path, report)
    assert json.loads(path.read_text("utf-8"))["suite_id"] == report["suite_id"]
    assert not list(path.parent.glob(".*.tmp"))


@pytest.fixture(name="wired")
def wired_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"managers": [], "drivers": []}

    def manager_factory(*_: object, **__: object) -> FakeManager:
        manager = FakeManager()
        state["managers"].append(manager)
        return manager

    def driver_factory(*_: object, **kwargs: object) -> TitleDriver:
        driver = TitleDriver()
        state["drivers"].append(driver)
        return driver

    monkeypatch.setattr(cli, "DeviceConnectionManager", manager_factory)
    monkeypatch.setattr(cli, "AndroidCaseDriver", driver_factory)
    yield state


def _namespace(project_dir: Path, **overrides: object) -> Any:
    base: dict[str, object] = {
        "project": str(project_dir),
        "devices": ["PULSE_A"],
        "cases": ["happy"],
        "repeat": 1,
        "strategy": None,
        "input": [],
        "activity": [],
        "evidence_root": None,
        "experience": None,
        "no_experience": True,
        "no_screenshot": True,
        "allow_clear": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_suite_runs_the_whole_plan_and_aggregates(
    wired: dict[str, Any], project_dir: Path, capsys
) -> None:
    stream = io.StringIO()
    code = run_suite(_namespace(project_dir, repeat=3), progress_stream=stream)
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["plan"] == {
        "devices": ["PULSE_A"],
        "cases": ["happy"],
        "repeats": 3,
        "attempts": 3,
        "executed": 3,
        "not_run": 0,
    }
    assert body["totals"]["passed"] == 3
    assert body["totals"]["refused"] == 0
    assert [item["repeat"] for item in body["attempts"]] == [1, 2, 3]
    assert all(item["status"] == "passed" for item in body["attempts"])
    assert all(item["control"]["holder_source"] is None for item in body["attempts"])
    # One connection per device; every attempt still releases the gate on its own.
    assert len(wired["managers"]) == 1
    assert len(wired["drivers"]) == 3
    assert wired["managers"][0].disconnected == []
    assert wired["managers"][0].closed is True
    assert stream.getvalue().count("attempt_start") == 0  # human text, not event names
    assert stream.getvalue().count("attempt ") == 6


def test_suite_aborts_the_plan_when_the_ledger_is_busy(
    wired: dict[str, Any], project_dir: Path, capsys, monkeypatch
) -> None:
    from pulse_runtime.engine.suite import _device_lease  # noqa: F401 - import for clarity

    def busy(*_: object, **__: object):
        raise cli.CliError("device", "ledger_busy", cli.SELECTION)

    monkeypatch.setattr("pulse_runtime.engine.suite._device_lease", busy)
    code = run_suite(_namespace(project_dir, repeat=2), progress_stream=io.StringIO())
    body = json.loads(capsys.readouterr().out)
    assert code == 5
    assert body["aborted"] == "ledger_busy"
    assert body["plan"]["executed"] == 1
    assert body["plan"]["not_run"] == 1
    assert body["attempts"][0]["status"] == "refused"


def test_suite_refusals_print_one_document(project_dir: Path, capsys) -> None:
    assert run_suite(_namespace(project_dir, cases=["ghost"])) == 5
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "refused" and body["reason"] == "unknown_case"
    assert run_suite(_namespace(project_dir, repeat=0)) == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_repeat"
    assert run_suite(_namespace(project_dir, devices=[])) == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "device_required"


def test_suite_project_errors_are_stage_project(tmp_path: Path, capsys) -> None:
    assert run_suite(_namespace(tmp_path / "missing")) == 6
    assert json.loads(capsys.readouterr().out)["reason"] == "project_unreadable"
    broken = write_project(tmp_path / "broken")
    (broken / "cases" / "happy.json").write_text("{not json", "utf-8")
    assert run_suite(_namespace(broken)) == 6
    assert json.loads(capsys.readouterr().out)["reason"] == "project_invalid"


def test_suite_ai_policy_is_refused_before_any_device(
    wired: dict[str, Any], project_dir: Path, capsys
) -> None:
    code = run_suite(_namespace(project_dir, cases=["ai"], strategy="rule_only"))
    assert code == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "ai_policy"
    assert wired["managers"] == []


def test_suite_writes_aggregate_report_under_evidence_root(
    wired: dict[str, Any], project_dir: Path, capsys, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"
    code = run_suite(
        _namespace(project_dir, repeat=2, evidence_root=str(evidence)),
        progress_stream=io.StringIO(),
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    suite_file = evidence / "suite" / f"{body['suite_id']}.json"
    assert suite_file.is_file()
    written = json.loads(suite_file.read_text("utf-8"))
    assert written["suite_id"] == body["suite_id"]
    for item in body["attempts"]:
        run_dir = evidence / str(item["run_id"])
        assert (run_dir / "result.json").is_file()


def test_suite_never_reports_a_run_as_passed_without_a_terminal_state(
    wired: dict[str, Any], project_dir: Path, capsys, monkeypatch
) -> None:
    class BlockedDriver(TitleDriver):
        async def resolve(self, target, locators, *, timeout_ms, scope=None, primary=None):
            return Resolution("indeterminate", "observation_unavailable", query_complete=False)

    monkeypatch.setattr(cli, "AndroidCaseDriver", lambda *a, **k: BlockedDriver())
    code = run_suite(_namespace(project_dir, repeat=2), progress_stream=io.StringIO())
    body = json.loads(capsys.readouterr().out)
    assert code == 2
    assert body["totals"]["blocked"] == 2
    assert body["totals"]["passed"] == 0
    assert all(item["status"] == "blocked" for item in body["attempts"])


def test_exit_codes_are_the_shared_terminal_contract() -> None:
    assert EXIT_CODES == {"passed": 0, "failed": 1, "blocked": 2, "interrupted": 3, "canceled": 4}


class FailingBeginDriver(TitleDriver):
    """The device is gone before the lease exists: this must never read as passed."""

    def __init__(self, *_: object, **__: object) -> None:
        super().__init__()
        self.begins = 0

    async def begin(self, run_id: str, *, source: str):
        from pulse_runtime.engine.driver import DriverFault

        self.begins += 1
        raise DriverFault("interrupted", "stale_session")


def test_suite_counts_a_lease_failure_as_interrupted_not_passed(
    wired: dict[str, Any], project_dir: Path, capsys
) -> None:
    cli.AndroidCaseDriver = lambda *a, **k: FailingBeginDriver()  # type: ignore[assignment]
    code = run_suite(_namespace(project_dir, repeat=2), progress_stream=io.StringIO())
    body = json.loads(capsys.readouterr().out)
    assert body["totals"]["passed"] == 0
    assert body["totals"]["interrupted"] == 2
    assert body["totals"]["pass_rate"] == 0.0
    assert [item["reason"] for item in body["attempts"]] == ["stale_session", "stale_session"]
    assert code == 3


def test_suite_attempt_reports_the_evidence_of_a_lease_failure(
    wired: dict[str, Any], project_dir: Path, capsys, tmp_path: Path
) -> None:
    cli.AndroidCaseDriver = lambda *a, **k: FailingBeginDriver()  # type: ignore[assignment]
    evidence = tmp_path / "evidence"
    run_suite(_namespace(project_dir, evidence_root=str(evidence)), progress_stream=io.StringIO())
    body = json.loads(capsys.readouterr().out)
    attempt = body["attempts"][0]
    assert attempt["status"] == "interrupted"
    report = json.loads((evidence / str(attempt["run_id"]) / "result.json").read_text("utf-8"))
    assert report["run"]["status"] == "interrupted"
    assert report["run"]["business_success"] is None
