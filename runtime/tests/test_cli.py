"""P1.10 CLI: one headless case on one device with terminal-state exit codes.

The device is a fake Manager (no phone, ADB, Agent, public REST endpoint or background
Runtime). Offline paths exercise the real argument parser and the real project loader; the
run path exercises the real interpreter and the real report shape over a scripted driver.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from pulse_runtime.engine import cli
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    ExecutionEnvironment,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_runtime.engine.errors import EngineStop
from pulse_runtime.engine.interpreter import RunResult
from pulse_runtime.gate.lock import LOCK_SUFFIX, LedgerLease
from pulse_schema import models as m
from pulse_schema.validation import Project, load_project
from runtime.tests.test_actions import action_rig as action_rig

APP = "com.pulse.target"
SESSION = "00000000-0000-4000-8000-000000000001"
FIXTURE = Path(__file__).parents[2] / "fixtures/golden-cases/interpreter-physical"


# --------------------------------------------------------------------------- project on disk


def _case_body(case_id: str, expected: str, **extra: object) -> dict[str, object]:
    return {
        "$schema": "urn:pulse:schema:1-draft:case",
        "case_id": case_id,
        "name": case_id,
        "goal": "CLI wiring",
        "parameters": {},
        "profile_refs": ["fake"],
        "inputs": {},
        "locators": {},
        "preconditions": [],
        "steps": [
            {
                "type": "wait",
                "id": "ready",
                "timeout_ms": 5000,
                "condition": {
                    "operator": "exists",
                    "target": {"locator_ref": "title"},
                    "source": "native",
                    "timeout_ms": 5000,
                },
            }
        ],
        "checks": [
            {
                "id": "title",
                "required": True,
                "condition": {
                    "operator": "text_equals",
                    "target": {"locator_ref": "title"},
                    "source": "native",
                    "expected": expected,
                    "timeout_ms": 5000,
                },
            }
        ],
        **extra,
    }


def write_project(root: Path, *, default_strategy: str = "rule_only") -> Path:
    (root / "cases").mkdir(parents=True)
    (root / "profiles").mkdir()
    manifest = {
        "$schema": "urn:pulse:schema:1-draft:manifest",
        "project_id": "cli",
        "name": "P1.10 CLI",
        "revision": "1",
        "cases": {
            "happy": "cases/happy.json",
            "ai": "cases/ai.json",
            "needs-input": "cases/needs-input.json",
        },
        "flows": {},
        "data": {},
        "profiles": {"fake": "profiles/fake.json"},
        "entry_cases": ["happy"],
        "default_strategy": default_strategy,
        "platforms": ["android"],
        "required_capabilities": ["observe"],
        "constraints": {"allowed_apps": [APP], "forbidden_actions": []},
        "dependencies": [],
        "budgets": {"case_timeout_ms": 30000, "suite_timeout_ms": 60000},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (root / "profiles" / "fake.json").write_text(
        json.dumps(
            {
                "$schema": "urn:pulse:schema:1-draft:profile",
                "profile_id": "fake",
                "platform": "android",
                "environment": "test",
                "app_id": APP,
                "capabilities": ["observe"],
                "locators": {
                    "title": {
                        "candidates": [{"by": "res_id", "value": "com.pulse.target:id/title"}],
                        "expect": {"unique": True, "visible": True},
                    }
                },
            }
        ),
        "utf-8",
    )
    (root / "cases" / "happy.json").write_text(json.dumps(_case_body("happy", "用户信息")), "utf-8")
    (root / "cases" / "ai.json").write_text(
        json.dumps(
            _case_body(
                "ai",
                "x",
                steps=[
                    {
                        "type": "ai_step",
                        "id": "guess",
                        "goal": "find the title",
                        "timeout_ms": 5000,
                        "checks": [
                            {
                                "id": "ai-title",
                                "required": True,
                                "condition": {
                                    "operator": "exists",
                                    "target": {"locator_ref": "title"},
                                    "source": "native",
                                    "timeout_ms": 5000,
                                },
                            }
                        ],
                    },
                    {
                        "type": "wait",
                        "id": "ready",
                        "timeout_ms": 5000,
                        "condition": {
                            "operator": "exists",
                            "target": {"locator_ref": "title"},
                            "source": "native",
                            "timeout_ms": 5000,
                        },
                    },
                ],
            )
        ),
        "utf-8",
    )
    (root / "cases" / "needs-input.json").write_text(
        json.dumps(
            _case_body(
                "needs-input",
                "x",
                parameters={"who": {"type": "string", "required": True}},
            )
        ),
        "utf-8",
    )
    return root


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return write_project(tmp_path / "project")


# --------------------------------------------------------------------------- fakes


class TitleDriver:
    """Scripted driver for the title-only case; no image, action or control gate needed."""

    app_id = APP

    def __init__(self, text: str = "用户信息") -> None:
        self.text = text
        self.finished: list[RunStatus] = []

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        return RunLease(run_id, 0)

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        if not isinstance(target, m.LocatorRef) or target.locator_ref != "title":
            return Resolution("not_found", "no_match")
        return Resolution(
            "resolved",
            None,
            (Element("tree", self.text, True, {"enabled": True, "visible": True}),),
        )

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        raise EngineStop("blocked", "no_action_in_this_case")

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        self.finished.append(state)
        return {"status": "cleaned", "state": state}

    def environment(self) -> ExecutionEnvironment:
        return ExecutionEnvironment(platform="android", device_serial="PULSE_A")


class FakeControl:
    def __init__(self) -> None:
        self.session_id: str | None = SESSION
        self.holder_source: str | None = "auto"
        self.run_id: str | None = None
        self.epoch = 1
        self.device_id = "PULSE_A"
        self.recovery_required = False
        self.recovery_action_id: str | None = None

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


class FakeGate:
    def __init__(self) -> None:
        self.control = FakeControl()

    def snapshot(self, serial: str) -> FakeControl:
        return self.control


class FakeManager:
    """The minimum of DeviceConnectionManager the CLI touches on the happy path."""

    def __init__(self, *, ready: bool = True, connect_code: str | None = None) -> None:
        self.ready = ready
        self.connect_code = connect_code
        self.gate = FakeGate()
        self.closed = False
        self.disconnected: list[tuple[str, str]] = []
        self.released: list[dict[str, object]] = []
        self._slot = SimpleNamespace(
            session=SimpleNamespace(id=SESSION) if ready else None,
            status="ready" if ready else "disconnected",
            code=None if ready else "device_disconnected",
        )

    def slot(self, serial: str) -> SimpleNamespace:
        return self._slot

    async def connect(
        self,
        serial: str,
        *,
        timeout: float = 30,  # noqa: ASYNC109 - mirrors the real signature
    ) -> dict[str, object]:
        if self.connect_code is not None:
            from pulse_runtime.drivers.android.agent_transport import ConnectionFault

            raise ConnectionFault(self.connect_code)
        return {"ready": self.ready}

    async def release_control(
        self, serial: str, *, session_id: str, control_epoch: int, run_id: str | None
    ) -> dict[str, object]:
        self.released.append(
            {"serial": serial, "session_id": session_id, "epoch": control_epoch, "run_id": run_id}
        )
        self.gate.control.holder_source = None
        return {"serial": serial}

    async def disconnect(self, serial: str, session_id: str) -> dict[str, object]:
        self.disconnected.append((serial, session_id))
        return {"status": "disconnected"}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Patch the CLI's manager/driver factories so the real interpreter still runs."""
    state: dict[str, Any] = {"manager": FakeManager(), "driver": TitleDriver()}

    def manager_factory(*_: object, **__: object) -> FakeManager:
        return state["manager"]

    def driver_factory(*_: object, **__: object) -> TitleDriver:
        return state["driver"]

    monkeypatch.setattr(cli, "DeviceConnectionManager", manager_factory)
    monkeypatch.setattr(cli, "AndroidCaseDriver", driver_factory)
    yield state


async def run_direct(
    project: Project,
    *,
    case_id: str = "happy",
    strategy: str | None = None,
    evidence: Path | None = None,
) -> tuple[dict[str, object], int]:
    return await cli._run_on_device(
        project,
        case_id=case_id,
        profile_id="fake",
        serial="PULSE_A",
        strategy=strategy,  # type: ignore[arg-type]
        inputs={},
        activities={},
        allow_clear=False,
        evidence_root=evidence,
        include_screenshot=False,
        experience_path=None,
        progress_stream=io.StringIO(),
    )


# --------------------------------------------------------------------------- exit-code contract


def test_terminal_map_is_explicit_and_distinct() -> None:
    assert cli.EXIT_CODES == {
        "passed": 0,
        "failed": 1,
        "blocked": 2,
        "interrupted": 3,
        "canceled": 4,
    }
    assert len(set(cli.EXIT_CODES.values())) == len(cli.EXIT_CODES)
    assert cli.SELECTION == 5 and cli.PROJECT_INVALID == 6 and cli.INVOCATION == 7


# --------------------------------------------------------------------------- offline refusals


def test_parser_refusal_is_one_json_document_with_exit_seven(capsys) -> None:
    code = cli.main(["--bogus", "nowhere"])
    out = capsys.readouterr().out
    body = json.loads(out)
    assert code == 7
    assert body == {
        "contract": cli.CONTRACT,
        "status": "refused",
        "reason": "invalid_arguments",
        "stage": "arguments",
        "exit_code": 7,
    }
    assert out.count("\n") == 1  # exactly one document, one trailing newline


def test_unusable_strategy_choice_is_parser_refusal(project_dir: Path, capsys) -> None:
    code = cli.main(
        ["run", str(project_dir), "--case", "happy", "--device", "X", "--strategy", "x"]
    )
    assert code == 7
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_arguments"


def test_unknown_project_is_stage_project_exit_six(tmp_path: Path, capsys) -> None:
    code = cli.main([str(tmp_path / "missing"), "--case", "happy", "--device", "X"])
    body = json.loads(capsys.readouterr().out)
    assert code == 6
    assert body["stage"] == "project" and body["reason"] == "project_unreadable"


def test_invalid_document_is_stage_project_exit_six(project_dir: Path, capsys) -> None:
    (project_dir / "cases" / "happy.json").write_text("{not json", "utf-8")
    code = cli.main([str(project_dir), "--case", "happy", "--device", "X"])
    body = json.loads(capsys.readouterr().out)
    assert code == 6 and body["reason"] == "project_invalid"


def test_unknown_case_multi_case_and_missing_device(project_dir: Path, capsys) -> None:
    base = [str(project_dir), "--case", "happy", "--device", "X"]
    assert cli.main([str(project_dir), "--case", "ghost", "--device", "X"]) == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "unknown_case"
    assert cli.main([base[0], "--case", "happy", "--case", "wrong", "--device", "X"]) == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "single_case_required"
    assert cli.main([str(project_dir), "--case", "happy"]) == 7
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_arguments"


def test_ai_case_under_rule_only_is_refused_before_device(project_dir: Path, capsys) -> None:
    code = cli.main([str(project_dir), "--case", "ai", "--device", "X", "--strategy", "rule_only"])
    assert code == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "ai_policy"


def test_missing_required_input_is_refused_before_device(project_dir: Path, capsys) -> None:
    code = cli.main([str(project_dir), "--case", "needs-input", "--device", "X"])
    assert code == 5
    assert json.loads(capsys.readouterr().out)["reason"] == "input_required"


def test_unknown_case_is_refused_without_any_device(project_dir: Path) -> None:
    project = load_project(project_dir)
    with pytest.raises(cli.CliError) as info:
        cli._case_and_profile(project, SimpleNamespace(cases=["ghost"], device="X", strategy=None))
    assert info.value.code == "unknown_case"


# --------------------------------------------------------------------------- preflight selection


def test_preflight_selection_returns_first_refusal(project_dir: Path) -> None:
    project = load_project(project_dir)
    assert cli.preflight_selection(project, "happy", "fake", "rule_only", {}) is None
    assert (
        cli.preflight_selection(project, "needs-input", "fake", "rule_only", {}) == "input_required"
    )
    assert cli.preflight_selection(project, "ai", "fake", "rule_only", {}) == "ai_policy"
    assert cli.preflight_selection(project, "ai", "fake", "ai_led", {}) == "model_unavailable"


# --------------------------------------------------------------------------- run path


async def test_passed_case_returns_zero_and_a_clean_scriptable_report(
    wired: dict[str, Any], project_dir: Path
) -> None:
    report, code = await run_direct(load_project(project_dir))
    manager: FakeManager = wired["manager"]
    driver: TitleDriver = wired["driver"]
    assert code == 0
    assert report["status"] == "passed"
    assert report["business_success"] is True
    assert report["case_id"] == "happy" and report["profile_id"] == "fake"
    assert report["counts"] == {"preconditions": 0, "steps": 1, "checks": 1}
    assert report["evidence"] == {
        "status": None,
        "directory": None,
        "directory_origin": None,
        "issues": [],
    }
    assert report["exit_code"] == 0
    assert report["preflight_issues"] == []
    assert report["cleanup"]["connection"] == "disconnected"
    assert report["control"]["holder_source"] is None
    assert manager.disconnected == [("PULSE_A", SESSION)]
    assert manager.closed is True
    assert driver.finished == ["passed"]
    json.dumps(report)


async def test_failed_check_returns_one_with_business_false(
    wired: dict[str, Any], project_dir: Path
) -> None:
    wired["driver"] = TitleDriver("Coffee")  # expected 用户信息
    report, code = await run_direct(load_project(project_dir))
    assert code == 1
    assert report["status"] == "failed" and report["business_success"] is False
    assert report["reason"] == "check_failed"


async def test_device_not_ready_is_a_selection_refusal(
    wired: dict[str, Any], project_dir: Path
) -> None:
    wired["manager"] = FakeManager(ready=False)
    with pytest.raises(cli.CliError) as info:
        await run_direct(load_project(project_dir))
    assert (info.value.stage, info.value.code, info.value.exit_code) == (
        "device",
        "device_disconnected",
        5,
    )
    assert wired["manager"].closed is True


async def test_connect_fault_is_a_selection_refusal(
    wired: dict[str, Any], project_dir: Path
) -> None:
    wired["manager"] = FakeManager(connect_code="adb_unauthorized")
    with pytest.raises(cli.CliError) as info:
        await run_direct(load_project(project_dir))
    assert info.value.code == "adb_unauthorized" and info.value.exit_code == 5


async def test_leftover_control_held_by_this_session_is_released(
    wired: dict[str, Any], project_dir: Path
) -> None:
    manager: FakeManager = wired["manager"]
    manager.gate.control.holder_source = "auto"
    manager.gate.control.run_id = str(uuid4())
    await run_direct(load_project(project_dir))
    assert manager.released == [
        {
            "serial": "PULSE_A",
            "session_id": SESSION,
            "epoch": 1,
            "run_id": manager.released[0]["run_id"],
        }
    ]


async def test_foreign_control_holder_is_never_released(
    wired: dict[str, Any], project_dir: Path
) -> None:
    wired["manager"].gate.control.session_id = "another-session"
    await run_direct(load_project(project_dir))
    assert wired["manager"].released == []


async def test_evidence_root_writes_a_run_directory_and_reports_it(
    wired: dict[str, Any], project_dir: Path, tmp_path: Path
) -> None:
    evidence = tmp_path / "evidence"
    report, code = await run_direct(load_project(project_dir), evidence=evidence)
    assert code == 0
    run_id = report["run_id"]
    directory = evidence / run_id
    assert (directory / "result.json").is_file()
    assert (directory / "result.html").is_file()
    assert report["evidence"]["status"] == "written"
    assert report["evidence"]["issues"] == []
    assert report["evidence"]["directory"] == str(directory)
    assert report["evidence"]["directory_origin"] == "evidence_root/run_id"


def test_report_surfaces_nested_evidence_issues(tmp_path: Path) -> None:
    project = _minimal_project()
    run = RunResult(str(uuid4()), "happy", "fake", "passed")
    run.evidence = {
        "run": {"status": "passed"},
        "evidence": {"issues": [{"code": "attachment_limit", "file": "attachments"}]},
    }
    (tmp_path / run.run_id).mkdir()
    (tmp_path / run.run_id / "result.json").write_text("{}", "utf-8")
    report = cli._report(run, project, "happy", "fake", tmp_path)
    assert report["evidence"]["status"] == "written"
    assert report["evidence"]["issues"] == [{"code": "attachment_limit", "file": "attachments"}]


def test_missing_report_files_are_not_reported_as_written(tmp_path: Path) -> None:
    project = _minimal_project()
    run = RunResult(str(uuid4()), "happy", "fake", "passed")
    run.evidence = {"run": {"status": "passed"}, "evidence": {"issues": []}}
    (tmp_path / run.run_id).mkdir()
    report = cli._report(run, project, "happy", "fake", tmp_path)
    assert report["evidence"]["status"] == "incomplete"


def test_active_ledger_lease_is_a_device_refusal(project_dir: Path, capsys, monkeypatch) -> None:
    evidence = project_dir / "evidence"
    monkeypatch.setattr(cli, "DeviceConnectionManager", lambda **_: FakeManager())
    monkeypatch.setattr(cli, "AndroidCaseDriver", lambda *a, **k: TitleDriver())
    first, _ = asyncio.run(
        cli._run_on_device(
            load_project(project_dir),
            case_id="happy",
            profile_id="fake",
            serial="PULSE_A",
            strategy="rule_only",
            inputs={},
            activities={},
            allow_clear=False,
            evidence_root=evidence,
            include_screenshot=False,
            experience_path=None,
            progress_stream=io.StringIO(),
        )
    )
    assert first["status"] == "passed"
    lease = LedgerLease(Path(f"{evidence / 'runtime' / 'ledger.sqlite3'}{LOCK_SUFFIX}"))
    try:
        code = cli.main(
            [
                str(project_dir),
                "--case",
                "happy",
                "--device",
                "PULSE_A",
                "--evidence-root",
                str(evidence),
            ]
        )
    finally:
        lease.release()
    body = json.loads(capsys.readouterr().out)
    assert code == 5
    assert body["status"] == "refused"
    assert body["reason"] == "ledger_in_use"


def test_unusable_ledger_file_is_replaced_instead_of_crashing(
    project_dir: Path, capsys, monkeypatch
) -> None:
    evidence = project_dir / "evidence"
    ledger = evidence / "runtime" / "ledger.sqlite3"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("not a sqlite database", "utf-8")
    monkeypatch.setattr(cli, "DeviceConnectionManager", lambda **_: FakeManager())
    monkeypatch.setattr(cli, "AndroidCaseDriver", lambda *a, **k: TitleDriver())
    code = cli.main(
        [
            str(project_dir),
            "--case",
            "happy",
            "--device",
            "PULSE_A",
            "--evidence-root",
            str(evidence),
        ]
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["status"] == "passed"
    assert ledger.read_bytes().startswith(b"SQLite format 3")


def test_closed_ledger_is_a_bounded_refusal(project_dir: Path, capsys, monkeypatch) -> None:
    from pulse_runtime.gate.ledger import LedgerError

    def broken(**_: object) -> FakeManager:
        raise LedgerError("ledger_open_failed")

    monkeypatch.setattr(cli, "DeviceConnectionManager", broken)
    code = cli.main([str(project_dir), "--case", "happy", "--device", "PULSE_A"])
    body = json.loads(capsys.readouterr().out)
    assert code == 5
    assert body == {
        "contract": cli.CONTRACT,
        "status": "refused",
        "reason": "ledger_open_failed",
        "stage": "device",
        "exit_code": 5,
    }


async def test_real_manager_backend_runs_the_case_and_releases_control(
    monkeypatch: pytest.MonkeyPatch, project_dir: Path, action_rig
) -> None:
    """The real DeviceConnectionManager fake backend, not a hand-rolled stub."""
    from runtime.tests.test_device_connections import SERIAL_A

    backend, manager, _, _ = action_rig
    backend.geometry = {"display_id": 0, "width": 20, "height": 10, "rotation": 0}
    monkeypatch.setattr(cli, "DeviceConnectionManager", lambda **_: manager)
    monkeypatch.setattr(cli, "AndroidCaseDriver", lambda *a, **k: TitleDriver())
    report, code = await cli._run_on_device(
        load_project(project_dir),
        case_id="happy",
        profile_id="fake",
        serial=SERIAL_A,
        strategy="rule_only",
        inputs={},
        activities={},
        allow_clear=False,
        evidence_root=None,
        include_screenshot=False,
        experience_path=None,
        progress_stream=io.StringIO(),
    )
    assert code == 0, report
    assert report["status"] == "passed"
    assert report["control"]["holder_source"] is None
    assert report["control"]["run_id"] is None
    assert report["cleanup"]["connection"] == "disconnected"
    assert report["cleanup"]["released_source"] is None
    assert manager.closed is True


# --------------------------------------------------------------------------- argument parsing


def test_parse_inputs_accepts_json_scalars_and_rejects_bare_words() -> None:
    assert cli.parse_inputs(["count=3", "ratio=1.5", "flag=true", 'name="x"']) == {
        "count": 3,
        "ratio": 1.5,
        "flag": True,
        "name": "x",
    }
    with pytest.raises(cli.CliError) as info:
        cli.parse_inputs(["name"])
    assert info.value.exit_code == 7


def test_parse_activities_pairs() -> None:
    assert cli.parse_activities(["com.pulse.target=.FormActivity"]) == {
        "com.pulse.target": ".FormActivity"
    }
    with pytest.raises(cli.CliError):
        cli.parse_activities(["nope"])


def test_progress_writes_human_lines_to_the_given_stream() -> None:
    stream = io.StringIO()
    cli._progress("run_start", {"case_id": "happy", "profile_id": "fake"}, stream)
    cli._progress("check_finish", {"id": "title", "status": "passed", "code": None}, stream)
    cli._progress("run_finish", {"status": "passed", "reason": None}, stream)
    text = stream.getvalue()
    assert "run happy (fake)" in text
    assert "check title passed" in text
    assert "passed None" in text


def test_report_carries_exit_code_for_every_terminal_state() -> None:
    project = _minimal_project()
    for status, expected in cli.EXIT_CODES.items():
        run = RunResult(str(uuid4()), "happy", "fake", status)
        report = cli._report(run, project, "happy", "fake", None)
        assert report["contract"] == cli.CONTRACT
        assert report["exit_code"] == expected
        json.dumps(report)


def _minimal_project() -> Project:
    profile = m.Profile.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:profile",
            "profile_id": "fake",
            "platform": "android",
            "environment": "test",
            "app_id": APP,
            "capabilities": ["observe"],
            "locators": {},
        }
    )
    case = m.Case.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:case",
            "case_id": "happy",
            "name": "happy",
            "goal": "CLI wiring",
            "parameters": {},
            "profile_refs": ["fake"],
            "inputs": {},
            "locators": {},
            "preconditions": [],
            "steps": [
                {
                    "type": "wait",
                    "id": "ready",
                    "timeout_ms": 5000,
                    "condition": {
                        "operator": "exists",
                        "target": {"locator_ref": "title"},
                        "source": "native",
                        "timeout_ms": 5000,
                    },
                }
            ],
            "checks": [],
        }
    )
    manifest = m.Manifest.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:manifest",
            "project_id": "cli",
            "name": "P1.10 CLI",
            "revision": "1",
            "cases": {"happy": "cases/happy.json"},
            "flows": {},
            "data": {},
            "profiles": {"fake": "profiles/fake.json"},
            "entry_cases": ["happy"],
            "default_strategy": "rule_only",
            "platforms": ["android"],
            "required_capabilities": [],
            "constraints": {"allowed_apps": [APP], "forbidden_actions": []},
            "dependencies": [],
        }
    )
    return Project(manifest, {"happy": case}, {}, {}, {"fake": profile})


def test_physical_fixture_loads_and_selects_the_expected_profile() -> None:
    """The P1.7 form project is a valid CLI target without touching a device."""
    project = load_project(FIXTURE)
    case_id, profile_id, _ = cli._case_and_profile(
        project, SimpleNamespace(cases=["physical-pass"], device="X", strategy="rule_only")
    )
    assert (case_id, profile_id) == ("physical-pass", "target-form")
    assert cli.preflight_selection(project, case_id, profile_id, "rule_only", {}) is None
