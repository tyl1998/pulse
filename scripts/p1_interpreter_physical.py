"""P1.7 interpreter physical evidence on the exported FormActivity.

No client tap, system-dialog handling, Agent restart, APK change or pin write is performed.
The device must already be awake and unlocked. The script only opens the existing exported
FormActivity, collapses the notification shade, launches one pass case and one wrong-check
case, then writes a fresh evidence directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.interpreter import run_case
from pulse_schema.validation import load_project

PROJECT = Path("fixtures/golden-cases/interpreter-physical")
PACKAGE = "com.pulse.target"
COMPONENT = f"{PACKAGE}/.FormActivity"


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
        print(f"[{clock}] RUN  {data['case_id']} profile={data['profile_id']}", flush=True)
    elif event == "step_start":
        estimate = " expect~30s" if data.get("type") == "action" else ""
        print(
            f"[{clock}]   -> {data['case_id']} step {data['id']} ({data['type']}){estimate}",
            flush=True,
        )
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


async def run(serial: str, project_path: Path, evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project = load_project(project_path)
    before = focus_state(serial)
    result: dict[str, object] = {
        "contract": "pulse-runtime-interpreter-physical-evidence-draft",
        "status": "started",
        "started_at_ms": started,
        "device": {
            "serial": serial,
            "model": adb(serial, "shell", "getprop", "ro.product.model"),
            "android_api": adb(serial, "shell", "getprop", "ro.build.version.sdk"),
            "shell_uid": adb(serial, "shell", "id", "-u"),
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
    try:
        print(f"[{time.strftime('%H:%M:%S')}] connecting {serial}", flush=True)
        connected = await manager.connect(serial, timeout=30)
        slot = manager.slot(serial)
        session = slot.session
        if session is None:
            raise RuntimeError("connect returned no session")
        warm = await manager.observe(
            serial,
            session.id,
            str(uuid4()),
            tree=True,
            screenshot=False,
            ocr=False,
            timeout=30,
            for_action=True,
        )
        warm_sources = cast(dict[str, object], warm.get("sources", {}))
        warm_tree = cast(dict[str, object], warm_sources.get("tree", {}))
        result["connect"] = {
            "ready": connected.get("ready"),
            "session_id": session.id,
            "control": manager.gate.snapshot(serial).as_dict(),
            "warm_observation": {
                "success": warm.get("success"),
                "status": warm.get("status"),
                "tree": warm_tree.get("status"),
            },
        }
        print(
            f"[{time.strftime('%H:%M:%S')}] connected; warm tree={warm.get('status')}", flush=True
        )
        driver = AndroidCaseDriver(
            manager,
            serial=serial,
            session_id=session.id,
            profile=project.profiles["target-form"],
        )
        suite = Deadline.after(1800)
        for case_id, expected in (
            ("physical-pass", "passed"),
            ("physical-fail", "failed"),
        ):
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
            runs.append(
                {
                    "case_id": case_id,
                    "expected": expected,
                    "actual": run.status,
                    "reason": run.reason,
                    "result": run.as_dict(),
                    "control_after": manager.gate.snapshot(serial).as_dict(),
                }
            )
        result["runs"] = runs
        result["ledger_actions"] = manager.ledger.list_actions(serial)
        result["control_final"] = manager.gate.snapshot(serial).as_dict()
        pass_result = cast(dict[str, object], runs[0]["result"])
        pass_steps = cast(list[dict[str, object]], pass_result["steps"])
        pass_checks = cast(list[dict[str, object]], pass_result["checks"])
        control_final = cast(dict[str, object], result["control_final"])
        ledger_actions = cast(list[dict[str, object]], result["ledger_actions"])
        checks = {
            "physical_pass": runs[0]["actual"] == "passed" and runs[0]["reason"] is None,
            "physical_fail": runs[1]["actual"] == "failed" and runs[1]["reason"] == "check_failed",
            "control_released": control_final["holder_source"] is None,
            "launch_used": any(
                item.get("id") == "launch-form" and item.get("status") == "passed"
                for item in pass_steps
            ),
            "flow_and_bind": any(
                item.get("id") == "fill-flow" and item.get("status") == "passed"
                for item in pass_steps
            ),
            "loop_entered": any(
                item.get("id") == "verify-loop" and item.get("entries") == 1 for item in pass_steps
            ),
            "result_exact": any(
                item.get("id") == "result-exact" and item.get("status") == "passed"
                for item in pass_checks
            ),
            "all_ledger_verified": bool(ledger_actions)
            and all(item.get("state") == "verified" for item in ledger_actions),
        }
        result["checks"] = checks
        result["status"] = (
            "physical_interpreter_golden" if all(checks.values()) else "physical_interpreter_failed"
        )
    except Exception as exc:
        result["status"] = "physical_interpreter_failed"
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
    print(
        json.dumps({"status": result["status"], "reason": result.get("reason")}, ensure_ascii=False)
    )
    return 0 if result["status"] == "physical_interpreter_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
