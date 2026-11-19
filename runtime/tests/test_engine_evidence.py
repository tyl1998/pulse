"""P1.9 evidence and report: run-directory isolation, attachments, JSON + HTML reports.

The interpreter runs on its synthetic/manager test drivers only; no phone, Agent, ADB, Runtime
HTTP endpoint or background process is contacted. Every attempt uses a fresh temporary root.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.driver import ExecutionEnvironment
from pulse_runtime.engine.evidence import (
    MAX_ATTACHMENTS,
    MAX_SNAPSHOTS,
    EvidenceRecorder,
    build_report,
    check_evidence,
    decode_png_payload,
    render_html,
)
from pulse_runtime.engine.interpreter import run_case
from pulse_schema import models as m
from pulse_schema.validation import Project, load_project
from runtime.tests.test_actions import action_rig as action_rig
from runtime.tests.test_device_connections import SERIAL_A
from runtime.tests.test_interpreter_golden import EXPECTED, FIXTURE, BlockingDriver, FakeDriver

APP = "com.pulse.target"
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


class SinkDriver(FakeDriver):
    """FakeDriver plus an environment and one scripted screenshot per action."""

    def __init__(self, world: dict[str, object], *, screenshot: str | None = PNG) -> None:
        super().__init__(world)
        self.environment_calls = 0
        self.sink_calls: list[tuple[str, str]] = []
        self.screenshot = screenshot

    def environment(self) -> ExecutionEnvironment:
        self.environment_calls += 1
        return ExecutionEnvironment(
            platform="android",
            device_serial="FAKE_SERIAL",
            device_model="Fake_Model",
            android_api="36",
        )

    def report_sink(
        self,
        serial: str | None,
        session_id: str | None,
        run_id: str,
        evidence_root: Path | None,
    ) -> FakeSink:
        assert evidence_root is not None and evidence_root.name == run_id
        return FakeSink(self)


class FakeSink:
    def __init__(self, driver: SinkDriver) -> None:
        self.driver = driver

    async def screenshot(self, run_id: str, step_id: str) -> dict[str, object]:
        self.driver.sink_calls.append((run_id, step_id))
        if self.driver.screenshot is None:
            return {"status": "unavailable", "error": {"code": "screenshot_unavailable"}}
        return {
            "status": "captured",
            "source": "screenshot",
            **decode_png_payload(
                self.driver.screenshot, name=f"{step_id}-{len(self.driver.sink_calls)}.png"
            ),
        }


class SlowSinkDriver(SinkDriver):
    def report_sink(self, serial, session_id, run_id, evidence_root):
        return SlowSink(self)


class SlowSink:
    def __init__(self, driver: SinkDriver) -> None:
        self.driver = driver

    async def screenshot(self, run_id: str, step_id: str) -> dict[str, object]:
        await asyncio.sleep(30)
        return {"status": "unavailable"}


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text("utf-8"))


async def test_run_directory_is_isolated_and_holds_json_html_and_manifest(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]))
    root = tmp_path / "evidence"
    run_id = str(uuid4())
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=run_id,
        evidence_root=root,
        include_screenshot=False,
    )
    assert result.status == "passed", result.as_dict()
    directory = root / run_id
    files = sorted(item.name for item in directory.iterdir())
    assert files == ["attachments", "attachments.json", "result.html", "result.json", "snapshots"]
    assert driver.environment_calls == 1
    assert driver.sink_calls == []  # attachments are opt-in

    report = read_json(directory / "result.json")
    assert report["contract"] == "pulse-runtime-run-report-draft"
    run = report["run"]
    assert isinstance(run, dict)
    assert run["status"] == "passed" and run["case_id"] == "pass"
    assert run["started_at_ms"] <= run["finished_at_ms"]
    environment = report["environment"]
    assert isinstance(environment, dict)
    assert environment["device"] == {
        "serial": "FAKE_SERIAL",
        "model": "Fake_Model",
        "android_api": "36",
    }
    assert report["project"]["project_id"] == "interpreter-golden"
    checks = report["checks"]
    assert isinstance(checks, list) and checks
    product = next(item for item in checks if item["id"] == "product-name")
    assert product["expected"] == {
        "operator": "text_equals",
        "expected": {"param_ref": "product"},
        "source": "native",
        "target": {"target": {"locator_ref": "product"}},
    }
    assert product["actual"]["text"] == "Coffee"
    assert product["source"] == "native"

    manifest = read_json(directory / "attachments.json")
    kinds = [item["kind"] for item in manifest["files"]]
    assert kinds.count("snapshot") >= 3
    assert "report_json" in kinds and "report_html" in kinds
    assert report["evidence"]["issues"] == []
    html = (directory / "result.html").read_text("utf-8")
    assert "<script" not in html.lower() and "http://" not in html and "https://" not in html
    assert "Coffee" in html


async def test_screenshot_attachment_is_captured_after_verified_action(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=tmp_path / "evidence",
        include_screenshot=True,
    )
    assert result.status == "passed"
    assert driver.sink_calls and all(item[0] == result.run_id for item in driver.sink_calls)
    directory = tmp_path / "evidence" / result.run_id
    pngs = sorted((directory / "attachments").glob("*.png"))
    assert len(pngs) == len(driver.sink_calls)
    assert pngs and all(item.read_bytes()[:4] == b"\x89PNG" for item in pngs)
    manifest = read_json(directory / "attachments.json")
    shots = [item for item in manifest["files"] if item["kind"] == "screenshot"]
    assert shots and all(item["step_id"] and item["source"] == "screenshot" for item in shots)
    steps = read_json(directory / "result.json")["steps"]
    tapped = next(item for item in steps if "attachments" in item)
    assert tapped["attachments"][0]["status"] == "captured"
    assert (directory / tapped["attachments"][0]["file"]).is_file()


async def test_unavailable_screenshot_never_changes_the_verdict(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]), screenshot=None)
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=tmp_path / "evidence",
        include_screenshot=True,
    )
    assert result.status == "passed"
    directory = tmp_path / "evidence" / result.run_id
    assert list((directory / "attachments").iterdir()) == []
    steps = read_json(directory / "result.json")["steps"]
    attached = next(item for item in steps if "attachments" in item)
    assert attached["attachments"] == [
        {"status": "unavailable", "error": {"code": "screenshot_unavailable"}}
    ]


async def test_completed_run_writes_a_report_after_releasing_its_lease(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=tmp_path / "evidence",
    )
    assert driver.finished == ["finished"]
    report = read_json(tmp_path / "evidence" / result.run_id / "result.json")
    assert report["run"]["status"] == "passed"
    assert report["cleanup"] == {"status": "cleaned", "state": "finished"}
    for name in ("preconditions", "steps", "checks"):
        assert isinstance(report[name], list)


async def test_failing_case_report_keeps_expected_actual_and_reason(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["business-fail"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="business-fail",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=tmp_path / "evidence",
    )
    assert result.status == "failed" and result.reason == "check_failed"
    report = read_json(tmp_path / "evidence" / result.run_id / "result.json")
    assert report["run"]["reason"] == "check_failed"
    check = report["checks"][0]
    assert check["status"] == "failed"
    assert check["expected"]["operator"] == "text_equals"
    assert check["actual"]["text"] == "Coffee"
    snapshots = sorted((tmp_path / "evidence" / result.run_id / "snapshots").glob("*.json"))
    assert snapshots and read_json(snapshots[0])["status"] in {"passed", "failed", "blocked"}


async def test_existing_run_directory_is_never_reused(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    run_id = str(uuid4())
    (tmp_path / run_id).mkdir(parents=True)
    result = await run_case(
        project=project,
        driver=SinkDriver(dict(EXPECTED["pass"]["world"])),
        case_id="pass",
        profile_id="fake-android",
        run_id=run_id,
        evidence_root=tmp_path,
    )
    assert result.evidence is not None
    assert result.evidence["status"] == "unavailable"
    assert result.evidence["issues"] == [{"code": "evidence_run_directory_exists"}]
    assert list((tmp_path / run_id).iterdir()) == []
    assert result.status == "passed"


async def test_slow_screenshot_is_bounded_and_never_changes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pulse_runtime.engine.interpreter._SCREENSHOT_TIMEOUT_S", 0.05)
    project = load_project(FIXTURE)
    driver = SlowSinkDriver(dict(EXPECTED["pass"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=tmp_path / "evidence",
        include_screenshot=True,
    )
    assert result.status == "passed"
    steps = read_json(tmp_path / "evidence" / result.run_id / "result.json")["steps"]
    attached = next(item for item in steps if "attachments" in item)
    assert attached["attachments"][0]["error"] == {"code": "screenshot_timeout"}


async def test_no_evidence_root_writes_nothing(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        include_screenshot=True,
    )
    assert result.status == "passed" and result.evidence is None
    assert driver.environment_calls == 0 and driver.sink_calls == []
    assert not tmp_path.is_dir() or not list(tmp_path.iterdir())  # noqa: ASYNC240


def test_recorder_refuses_symlinked_attachment_and_bad_png(tmp_path: Path) -> None:
    recorder = EvidenceRecorder.create(
        tmp_path, run_id=str(uuid4()), case_id="case", profile_id="profile"
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    (recorder.directory / "attachments" / "escape.png").symlink_to(outside)
    assert not recorder.attach_screenshot(
        {
            "status": "captured",
            "name": "escape.png",
            "content": b"\x89PNG\r\n\x1a\nfake",
            "byte_length": 12,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
        step_id="tap",
    )
    with pytest.raises(Exception) as caught:
        decode_png_payload("bm90IGEgcG5n", name="x.png")
    assert getattr(caught.value, "code", "") == "screenshot_invalid"
    codes = [item["code"] for item in recorder.issues]
    assert "screenshot_digest_mismatch" in codes


def test_recorder_bounds_attachment_count(tmp_path: Path) -> None:
    recorder = EvidenceRecorder.create(
        tmp_path, run_id=str(uuid4()), case_id="case", profile_id="profile"
    )
    for index in range(MAX_ATTACHMENTS + 2):
        payload = decode_png_payload(PNG, name=f"shot-{index}.png")
        recorder.attach_screenshot(payload, step_id=f"s{index}")
    finished = [item for item in recorder.entries if item.kind == "screenshot"]
    assert len(finished) == MAX_ATTACHMENTS
    assert recorder.issues[0]["code"] == "attachment_limit"


def test_secrets_never_reach_the_report() -> None:
    predicate = m.TextCondition.model_validate(
        {
            "operator": "text_equals",
            "target": {"locator_ref": "node"},
            "source": "native",
            "expected": "Coffee",
        }
    )
    evidence = check_evidence(
        {"status": "resolved", "password": "hunter2", "text": "Coffee"}, predicate
    )
    report = build_report(
        {"status": "passed", "checks": [evidence]},
        run_id="run",
        case_id="case",
        profile_id="profile",
        suite_id=None,
        environment={},
        metadata={"token": "secret-token-value"},
        manifest={"files": []},
        issues=[],
        started_at_ms=0,
        finished_at_ms=1,
        elapsed_ms=1.0,
    )
    rendered = render_html(report) + json.dumps(report, ensure_ascii=False)
    assert "hunter2" not in rendered and "secret-token-value" not in rendered
    assert evidence["expected"]["expected"] == "Coffee"
    assert evidence["actual"] == {"status": "resolved", "password": "[redacted]", "text": "Coffee"}


def test_known_secret_value_is_redacted_from_ordinary_text() -> None:
    secret = "Known-Secret-Value-42"
    predicate = m.TextCondition.model_validate(
        {
            "operator": "text_equals",
            "target": {"locator_ref": "node"},
            "source": "native",
            "expected": "Coffee",
        }
    )
    evidence = check_evidence(
        {"status": "resolved", "text": f"prefix {secret} suffix"},
        predicate,
        secrets=(secret,),
    )
    assert secret not in json.dumps(evidence)
    assert evidence["actual"]["text"] == "prefix [redacted] suffix"


async def test_known_secret_input_is_redacted_and_not_screenshotted(tmp_path: Path) -> None:
    secret = "Known-Secret-Value-42"
    project = load_project(FIXTURE)
    project.manifest.secret_refs.append("demo")
    case = project.cases["pass"]
    case.steps = [
        m.ActionStep(
            type="action",
            id="secret-input",
            action=m.InputAction(
                kind="input",
                target=m.LocatorRef(locator_ref="product"),
                text=m.SecretRef(secret_ref="demo"),
                mode="replace",
            ),
        )
    ]
    case.checks = [case.checks[0]]
    driver = SinkDriver(dict(EXPECTED["pass"]["world"]))
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        secrets={"demo": secret},
        evidence_root=tmp_path / "evidence",
        include_screenshot=True,
    )
    directory = tmp_path / "evidence" / result.run_id
    assert secret not in (directory / "result.json").read_text("utf-8")
    assert secret not in (directory / "result.html").read_text("utf-8")
    for snapshot in (directory / "snapshots").glob("*.json"):
        assert secret not in snapshot.read_text("utf-8")
    assert driver.sink_calls == []
    steps = read_json(directory / "result.json")["steps"]
    assert steps[0]["attachments"] == [
        {"status": "unavailable", "error": {"code": "secret_input_screenshot_skipped"}}
    ]


def test_snapshot_and_screenshot_quotas_are_independent(tmp_path: Path) -> None:
    recorder = EvidenceRecorder.create(
        tmp_path, run_id=str(uuid4()), case_id="case", profile_id="profile"
    )
    for index in range(MAX_SNAPSHOTS):
        assert recorder.snapshot(f"check-{index}", {"status": "passed"})
    payload = decode_png_payload(PNG, name="shot.png")
    assert recorder.attach_screenshot(payload, step_id="tap")
    assert [item["code"] for item in recorder.issues] == []


def test_repeated_checkpoint_snapshots_do_not_overwrite(tmp_path: Path) -> None:
    recorder = EvidenceRecorder.create(
        tmp_path, run_id=str(uuid4()), case_id="case", profile_id="profile"
    )
    assert recorder.snapshot("check-loop", {"status": "failed"})
    assert recorder.snapshot("check-loop", {"status": "passed"})
    files = sorted(path.name for path in (recorder.directory / "snapshots").glob("check-loop.json"))
    assert files == []
    snapshots = sorted((recorder.directory / "snapshots").glob("*check-loop.json"))
    assert [path.name for path in snapshots] == [
        "0000-check-loop.json",
        "0001-check-loop.json",
    ]
    assert read_json(snapshots[0])["status"] == "failed"
    assert read_json(snapshots[1])["status"] == "passed"


async def test_cancelled_run_still_writes_its_terminal_report(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    run_id = str(uuid4())
    driver = BlockingDriver(EXPECTED["business-fail"]["world"])
    task = asyncio.create_task(
        run_case(
            project=project,
            driver=driver,
            case_id="business-fail",
            profile_id="fake-android",
            run_id=run_id,
            evidence_root=tmp_path / "evidence",
        )
    )
    await asyncio.wait_for(driver.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = tmp_path / "evidence" / run_id
    report = read_json(directory / "result.json")
    assert report["run"]["status"] == "canceled"
    assert report["run"]["reason"] == "cancelled"
    assert report["run"]["business_success"] is None
    assert (directory / "result.html").is_file()
    assert driver.finished == ["canceled"]


async def test_android_environment_and_sink_use_existing_session(
    action_rig, tmp_path: Path
) -> None:
    _, manager, _, _ = action_rig
    backend_build = manager.backend.build
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    _, profile = project()
    driver = AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile)
    environment = driver.environment()
    assert environment.platform == "android"
    assert environment.device_serial == SERIAL_A
    assert environment.device_model == "Fake_A"
    assert environment.android_api == "36"
    assert environment.agent_pid == session.generation.agent_pid
    assert environment.runtime_version in (None, backend_build.version_name)
    sink = driver.report_sink(SERIAL_A, session.id, str(uuid4()), tmp_path)
    assert sink is not None
    mismatch = await sink.screenshot(str(uuid4()), "tap-root")  # type: ignore[attr-defined]
    assert mismatch == {"status": "unavailable", "error": {"code": "evidence_run_mismatch"}}


def test_recorder_stores_the_observation_screenshot_bytes(tmp_path: Path) -> None:
    payload = decode_png_payload(PNG, name="tap-1.png")
    recorder = EvidenceRecorder.create(
        tmp_path, run_id=str(uuid4()), case_id="case", profile_id="profile"
    )
    assert recorder.attach_screenshot({"status": "captured", **payload}, step_id="tap")
    stored = recorder.directory / "attachments" / "tap-1.png"
    assert stored.read_bytes() == payload["content"]


async def test_android_sink_reports_unavailable_for_a_foreign_run(
    action_rig, tmp_path: Path
) -> None:
    _, manager, _, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    _, profile = project()
    driver = AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile)
    sink = driver.report_sink(SERIAL_A, session.id, "run-a", tmp_path)
    assert sink is not None
    assert await sink.screenshot("run-b", "tap-root") == {
        "status": "unavailable",
        "error": {"code": "evidence_run_mismatch"},
    }


def project() -> tuple[Project, m.Profile]:
    selected = load_project(FIXTURE)
    return selected, selected.profiles["fake-android"]


def test_report_renderer_escapes_html() -> None:
    report = build_report(
        {
            "status": "failed",
            "checks": [
                {
                    "id": "<b>id</b>",
                    "status": "failed",
                    "expected": {"operator": "text_equals"},
                    "actual": {"text": "<script>alert(1)</script>"},
                    "source": "native",
                }
            ],
        },
        run_id="run",
        case_id="case",
        profile_id="profile",
        suite_id=None,
        environment={},
        metadata={},
        manifest={"files": []},
        issues=[],
        started_at_ms=0,
        finished_at_ms=1,
        elapsed_ms=1.0,
    )
    rendered = render_html(report)
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


async def test_unwritable_evidence_root_keeps_the_verdict(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    project_ = load_project(FIXTURE)
    result = await run_case(
        project=project_,
        driver=SinkDriver(dict(EXPECTED["pass"]["world"])),
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        evidence_root=blocked / "child",
    )
    assert result.status == "passed"
    assert result.evidence is not None and result.evidence["status"] == "unavailable"
