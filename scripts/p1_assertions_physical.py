"""P1.8 assertion-operator evidence on the exported FormActivity.

No client tap, system-dialog handling, Agent restart, APK change or pin write is performed.
The device must already be awake and unlocked. The script only opens the existing exported
FormActivity, collapses the notification shade, runs one case whose expectations match and
one whose expectations contradict (plus unverifiable cells), then writes a fresh evidence
directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.interpreter import RunResult, run_case
from pulse_schema.validation import load_project

PROJECT = Path("fixtures/golden-cases/assertions-physical")
PACKAGE = "com.pulse.target"
COMPONENT = f"{PACKAGE}/.FormActivity"
PASS_CHECKS = {
    "exists": "passed",
    "not_exists": "passed",
    "visible": "passed",
    "number_cmp": "passed",
    "graphic-empty": "blocked",
    "enabled": "failed",
    "checked": "passed",
    "text_equals": "failed",
    "contains": "failed",
    "regex": "failed",
    "count_eq": "passed",
    "count_gt": "passed",
    "count_lt": "passed",
    "page_is": "passed",
}
FAIL_CHECKS = {
    "exists": "failed",
    "not_exists": "failed",
    "visible": "failed",
    "number_cmp": "passed",
    "graphic-empty": "blocked",
    "enabled": "failed",
    "checked": "passed",
    "text_equals": "failed",
    "contains": "failed",
    "regex": "failed",
    "count_eq": "passed",
    "count_gt": "passed",
    "count_lt": "passed",
    "page_is": "passed",
}


def adb(serial: str, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed local adb for physical evidence
        ["adb", "-s", serial, *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def focus_state(serial: str) -> dict[str, object]:
    window = adb(serial, "shell", "dumpsys", "window")
    power = adb(serial, "shell", "dumpsys", "power")
    lines = [
        line.strip()
        for line in window.splitlines()
        if "mCurrentFocus=" in line or "mFocusedApp=" in line
    ]
    return {
        "lines": lines,
        "awake": any("mWakefulness=Awake" in line for line in power.splitlines()),
    }


def show_progress(event: str, data: dict[str, object]) -> None:
    clock = time.strftime("%H:%M:%S")
    if event == "run_start":
        print(f"[{clock}] RUN  {data['case_id']}", flush=True)
    elif event == "step_start":
        print(f"[{clock}]   -> {data['case_id']} step {data['id']} ({data['type']})", flush=True)
    elif event == "step_finish":
        elapsed = float(cast(float, data.get("elapsed_ms", 0))) / 1000
        code = f" code={data['code']}" if data.get("code") else ""
        print(
            f"[{clock}]   <- {data['case_id']} step {data['id']} "
            f"{data['status']} {elapsed:.1f}s{code}",
            flush=True,
        )
    elif event == "check_finish":
        code = f" code={data['code']}" if data.get("code") else ""
        print(
            f"[{clock}]   == {data['case_id']} check {data['id']} {data['status']}{code}",
            flush=True,
        )
    elif event == "run_finish":
        elapsed = float(cast(float, data.get("elapsed_ms", 0))) / 1000
        print(
            f"[{clock}] DONE {data['case_id']} {data['status']} "
            f"{elapsed:.1f}s reason={data.get('reason')}",
            flush=True,
        )


def check_statuses(result: RunResult) -> dict[str, str]:
    return {item.id: item.status for item in result.checks}


def observed_values(result: RunResult) -> dict[str, dict[str, object]]:
    return {item.id: dict(item.actual) for item in result.checks}


async def run(serial: str, project_path: Path, evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project = load_project(project_path)
    before = focus_state(serial)
    result: dict[str, object] = {
        "contract": "pulse-runtime-assertions-physical-evidence-draft",
        "status": "started",
        "started_at_ms": started,
        "device": {
            "serial": serial,
            "model": adb(serial, "shell", "getprop", "ro.product.model"),
            "android_api": adb(serial, "shell", "getprop", "ro.build.version.sdk"),
        },
        "physical_phone_verified": True,
        "background_runtime_untouched": True,
        "agent_restarted": False,
        "apk_changed": False,
        "pin_changed": False,
        "focus_before": before,
    }
    if before["awake"] is not True:
        result["status"] = "manual_preparation_required"
        result["reason"] = "wake_and_unlock_device"
        result["finished_at_ms"] = time.time_ns() // 1_000_000
        return result
    adb(serial, "shell", "cmd", "statusbar", "collapse")
    adb(serial, "shell", "am", "start", "-W", "--user", "0", "-n", COMPONENT)
    await asyncio.sleep(1)
    result["focus_after_prepare"] = focus_state(serial)
    prepared = cast(dict[str, object], result["focus_after_prepare"])
    if prepared["awake"] is not True or not any(
        "FormActivity" in line and PACKAGE in line for line in cast(list[str], prepared["lines"])
    ):
        result["status"] = "manual_preparation_required"
        result["reason"] = "dismiss_overlay_and_show_form_activity"
        result["finished_at_ms"] = time.time_ns() // 1_000_000
        return result
    manager = DeviceConnectionManager(
        poll_interval=0.5, ledger_path=evidence / "runtime" / "ledger.sqlite3"
    )
    runs: list[dict[str, object]] = []
    foreign_focus: list[dict[str, object]] = []

    async def guard_focus(phase: str) -> dict[str, object] | None:
        """Refuse to keep evaluating while another app owns the screen."""
        state = focus_state(serial)
        lines = cast(list[str], state["lines"])
        if any("mCurrentFocus=null" in line for line in lines) or not any(
            "mCurrentFocus=Window{" in line and PACKAGE in line for line in lines
        ):
            foreign_focus.append({"phase": phase, "lines": lines})
            return state
        return None

    try:
        print(f"[{time.strftime('%H:%M:%S')}] connecting {serial}", flush=True)
        connected = await manager.connect(serial, timeout=30)
        slot = manager.slot(serial)
        session = slot.session
        if session is None:
            raise RuntimeError("connect returned no session")
        result["connect"] = {
            "ready": connected.get("ready"),
            "session_id": session.id,
            "control": manager.gate.snapshot(serial).as_dict(),
        }
        driver = AndroidCaseDriver(
            manager,
            serial=serial,
            session_id=session.id,
            profile=project.profiles["target-form"],
        )
        suite = Deadline.after(1800)
        expected_runs = (
            ("physical-assert-pass", "passed", PASS_CHECKS),
            ("physical-assert-fail", "failed", FAIL_CHECKS),
        )
        for case_id, expected, expected_checks in expected_runs:
            lost = await guard_focus(f"{case_id}:start")
            if lost is not None:
                result["status"] = "manual_preparation_required"
                result["reason"] = "foreign_app_owns_the_screen"
                result["foreign_focus"] = foreign_focus
                result["finished_at_ms"] = time.time_ns() // 1_000_000
                return result
            run = await run_case(
                project=project,
                driver=driver,
                case_id=case_id,
                profile_id="target-form",
                activities={PACKAGE: ".FormActivity"},
                source="auto",
                suite_deadline=suite,
                progress=show_progress,
            )
            observed = check_statuses(run)
            runs.append(
                {
                    "case_id": case_id,
                    "expected": expected,
                    "actual": run.status,
                    "reason": run.reason,
                    "expected_checks": expected_checks,
                    "actual_checks": observed,
                    "observed": observed_values(run),
                    "checks_match": observed == expected_checks,
                    "result": run.as_dict(),
                    "control_after": manager.gate.snapshot(serial).as_dict(),
                }
            )
        result["runs"] = runs
        lost = await guard_focus("final")
        if lost is not None:
            result["foreign_focus"] = foreign_focus
        result["ledger_actions"] = manager.ledger.list_actions(serial)
        result["control_final"] = manager.gate.snapshot(serial).as_dict()
        control_final = cast(dict[str, object], result["control_final"])
        ledger_actions = cast(list[dict[str, object]], result["ledger_actions"])
        checks = {
            "physical_pass": runs[0]["actual"] == "passed" and runs[0]["reason"] is None,
            "physical_fail": runs[1]["actual"] == "failed" and runs[1]["reason"] == "check_failed",
            "pass_operators_match": runs[0]["checks_match"] is True,
            "fail_operators_match": runs[1]["checks_match"] is True,
            "collection_failure_blocked": (
                runs[0]["actual_checks"].get("number_cmp") == "passed"  # type: ignore[union-attr]
                and runs[1]["actual_checks"].get("graphic-empty") == "blocked"  # type: ignore[union-attr]
            ),
            "control_released": control_final["holder_source"] is None,
            "all_ledger_verified": bool(ledger_actions)
            and all(item.get("state") == "verified" for item in ledger_actions),
            "no_foreign_app": not foreign_focus,
        }
        result["checks"] = checks
        result["status"] = (
            "physical_assertion_golden" if all(checks.values()) else "physical_assertion_failed"
        )
    except Exception as exc:
        result["status"] = "physical_assertion_failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
    finally:
        try:
            await manager.close()
            result["cleanup"] = {"status": "closed"}
        except Exception as exc:
            result["cleanup"] = {"status": "failed", "type": type(exc).__name__}
        result["focus_final"] = focus_state(serial)
        result["finished_at_ms"] = time.time_ns() // 1_000_000
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--project", default=str(PROJECT))
    args = parser.parse_args()
    evidence = Path(args.evidence)
    if evidence.exists():
        print(f"evidence directory already exists: {evidence}", flush=True)
        return 1
    evidence.mkdir(parents=True)
    result = asyncio.run(run(args.serial, Path(args.project), evidence))
    (evidence / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result.get("checks", {}), ensure_ascii=False))
    print(json.dumps({"status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "physical_assertion_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
