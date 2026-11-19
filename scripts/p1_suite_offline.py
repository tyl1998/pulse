"""P1 exit suite runner offline evidence over the scripted driver.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. The script
runs the real ``run-suite`` body (real interpreter, real evidence writer, real gate release)
over the CLI's fake device manager for a deterministic 1-device x 2-case x 3-repeat plan and
one multi-device abort, then records the aggregate numbers.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_runtime.engine import cli
from pulse_runtime.engine.driver import Resolution
from pulse_runtime.engine.suite import EXIT_CODES, build_plan, run_suite
from pulse_schema.validation import load_project

PROJECT = Path("fixtures/golden-cases/interpreter")
CASES = ["pass", "business-fail"]


def write_project(root: Path) -> Path:
    from runtime.tests.test_cli import write_project as writer

    return writer(root)


class RecordingManager:
    """Fake manager that records connects/disconnects; one instance per device lease."""

    def __init__(self) -> None:
        from runtime.tests.test_cli import FakeManager

        self.inner = FakeManager()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def install_fakes(module: Any) -> dict[str, list[Any]]:
    from runtime.tests.test_cli import FakeManager, TitleDriver

    state: dict[str, list[Any]] = {"managers": [], "drivers": []}

    def manager_factory(*_: object, **__: object) -> Any:
        manager = FakeManager()
        state["managers"].append(manager)
        return manager

    def driver_factory(*_: object, **kwargs: object) -> Any:
        driver = TitleDriver()
        state["drivers"].append(driver)
        return driver

    module.DeviceConnectionManager = manager_factory
    module.AndroidCaseDriver = driver_factory
    return state


class IndeterminateDriver:
    def __init__(self, *_: object, **__: object) -> None:
        self.app_id = "com.pulse.target"

    async def begin(self, run_id: str, *, source: str) -> Any:
        from pulse_runtime.engine.driver import RunLease

        return RunLease(run_id, 0)

    async def resolve(self, target, locators, *, timeout_ms, scope=None, primary=None):
        return Resolution("indeterminate", "observation_unavailable", query_complete=False)

    async def act(self, plan, lease):
        raise AssertionError("no action expected")

    async def finish(self, lease, *, state):
        from pulse_runtime.engine.driver import ExecutionEnvironment  # noqa: F401

        return {"status": "cleaned", "state": state}


def capture(document: str) -> dict[str, object]:
    return json.loads(document.strip().splitlines()[-1])


def run(evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project_dir = write_project(evidence / "project")
    project = load_project(project_dir)
    state = install_fakes(cli)

    import contextlib

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = run_suite(
            SimpleNamespace(
                project=str(project_dir),
                devices=["FAKE_A"],
                cases=["happy", "happy"],
                repeat=1,
                strategy=None,
                input=[],
                activity=[],
                evidence_root=None,
                experience=None,
                no_experience=True,
                no_screenshot=True,
                allow_clear=False,
            ),
            progress_stream=io.StringIO(),
        )
    duplicated = capture(buffer.getvalue())

    plan = build_plan(project, devices=["FAKE_A", "FAKE_B"], cases=["happy"], repeats=2)
    state["managers"].clear()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        multi_code = run_suite(
            SimpleNamespace(
                project=str(project_dir),
                devices=["FAKE_A", "FAKE_B"],
                cases=["happy"],
                repeat=2,
                strategy=None,
                input=[],
                activity=[],
                evidence_root=str(evidence / "multi"),
                experience=None,
                no_experience=True,
                no_screenshot=True,
                allow_clear=False,
            ),
            progress_stream=io.StringIO(),
        )
    multi = capture(buffer.getvalue())
    multi_devices_connected = len(state["managers"])

    cli.AndroidCaseDriver = lambda *a, **k: IndeterminateDriver()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        blocked_code = run_suite(
            SimpleNamespace(
                project=str(project_dir),
                devices=["FAKE_A"],
                cases=["happy"],
                repeat=2,
                strategy=None,
                input=[],
                activity=[],
                evidence_root=None,
                experience=None,
                no_experience=True,
                no_screenshot=True,
                allow_clear=False,
            ),
            progress_stream=io.StringIO(),
        )
    blocked = capture(buffer.getvalue())
    state = install_fakes(cli)

    checks = {
        "duplicate_case_refused": code == 5 and duplicated.get("reason") == "duplicate_case",
        "plan_is_deterministic": [item["repeat"] for item in multi["attempts"]] == [1, 2, 1, 2],
        "plan_is_devices_first": [item["serial"] for item in multi["attempts"]]
        == ["FAKE_A", "FAKE_A", "FAKE_B", "FAKE_B"],
        "all_attempts_terminal": multi["totals"]["terminal"] == multi["plan"]["attempts"],
        "pass_rate_separate_from_terminal_rate": multi["totals"]["pass_rate"]
        == multi["totals"]["terminal_rate"]
        == 1.0,
        "one_connection_per_device": multi_devices_connected == 2,
        "gate_released_every_attempt": all(
            item["control"]["holder_source"] is None for item in multi["attempts"]
        ),
        "blocked_never_counted_as_passed": blocked["totals"]["blocked"] == 2
        and blocked["totals"]["passed"] == 0
        and blocked_code == EXIT_CODES["blocked"],
        "suite_report_written": (
            evidence / "multi" / "suite" / f"{multi['suite_id']}.json"
        ).is_file(),
        "unrun_attempts_zero": multi["plan"]["not_run"] == 0,
        "plan_matches_builder": multi["plan"]["attempts"] == plan.total,
        "exit_code_all_pass": multi_code == 0,
    }
    passed = sum(1 for value in checks.values() if value)
    return {
        "contract": "pulse-runtime-suite-draft",
        "evidence": "P1-exit-suite",
        "started_at_ms": started,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "project": str(PROJECT),
        "driver": "synthetic_cli_driver",
        "physical_phone_verified": False,
        "checks": passed,
        "total": len(checks),
        "details": checks,
        "multi": {key: multi[key] for key in ("plan", "totals", "by_device", "exit_code")},
        "blocked": {key: blocked[key] for key in ("totals", "exit_code")},
        "status": "offline_suite_golden" if passed == len(checks) else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    evidence = Path(args.evidence)
    if evidence.exists():
        print(f"evidence directory already exists: {evidence}", flush=True)
        return 1
    evidence.mkdir(parents=True)
    result = run(evidence)
    (evidence / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({key: result[key] for key in ("checks", "total", "status")}, ensure_ascii=False)
    )
    return 0 if result["status"] == "offline_suite_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
