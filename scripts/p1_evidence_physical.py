"""P1.9 evidence/report physical check on the exported FormActivity.

No client tap, system-dialog handling, Agent restart, APK change or pin write is performed.
The device must already be awake and unlocked. The script only opens the existing exported
FormActivity, collapses the notification shade, then runs one read-only case with the
evidence root enabled and screenshots on, and inspects the produced run directory. It never
asserts business expectations beyond that case's own result: the point is the evidence
artifacts, not a new assertion pass.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
CASE_ID = "physical-pass"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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
        print(f"[{clock}]   -> {data['id']} ({data['type']})", flush=True)
    elif event == "step_finish":
        elapsed = float(cast(float, data.get("elapsed_ms", 0))) / 1000
        print(f"[{clock}]   <- {data['id']} {data['status']} {elapsed:.1f}s", flush=True)
    elif event == "check_finish":
        print(f"[{clock}]   == {data['id']} {data['status']}", flush=True)
    elif event == "evidence_start":
        print(f"[{clock}]   ev {data['directory']}", flush=True)
    elif event == "run_finish":
        print(f"[{clock}] DONE {data['status']} {data.get('reason')}", flush=True)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text("utf-8"))


def inspect_evidence(directory: Path, manifest: dict[str, object]) -> dict[str, object]:
    """Verify layout, digests, HTML self-containment and screenshot bytes on disk."""
    problems: list[str] = []
    report = read_json(directory / "result.json")
    written = read_json(directory / "attachments.json")
    html = (directory / "result.html").read_text("utf-8")
    files = written.get("files")
    files = files if isinstance(files, list) else []
    for item in files:
        if not isinstance(item, dict):
            problems.append("manifest_item_invalid")
            continue
        path = directory / str(item.get("file"))
        if not path.is_file() or path.is_symlink():
            problems.append(f"missing:{item.get('file')}")
            continue
        data = path.read_bytes()
        if len(data) != item.get("byte_length"):
            problems.append(f"size_mismatch:{item.get('file')}")
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            problems.append(f"digest_mismatch:{item.get('file')}")
        if item.get("kind") == "screenshot" and data[:8] != _PNG_MAGIC:
            problems.append(f"png_header_invalid:{item.get('file')}")
    shots = [item for item in files if isinstance(item, dict) and item.get("kind") == "screenshot"]
    report_checks = report.get("checks")
    report_checks = report_checks if isinstance(report_checks, list) else []
    steps = report.get("steps")
    steps = steps if isinstance(steps, list) else []
    environment = report.get("environment")
    environment = environment if isinstance(environment, dict) else {}
    device = environment.get("device")
    device = device if isinstance(device, dict) else {}
    run_block = report.get("run")
    run_block = run_block if isinstance(run_block, dict) else {}
    checks = {
        "manifest_matches_report": written.get("run_id") == manifest.get("run_id"),
        "files_intact": not problems,
        "screenshots_present": bool(shots),
        "screenshots_per_verified_action": len(shots) >= len(manifest["verified_actions"]),
        "html_self_contained": "<script" not in html.lower()
        and "http://" not in html
        and "https://" not in html,
        "expected_actual_source": all(
            isinstance(item, dict) and {"expected", "actual", "source"}.issubset(item)
            for item in report_checks
        ),
        "environment_recorded": device.get("serial") == manifest.get("serial")
        and device.get("android_api") is not None,
        "agent_pid_recorded": isinstance(environment.get("agent_pid"), int),
        "run_status_recorded": run_block.get("status") == manifest.get("status"),
        "step_attachments": any(
            isinstance(item, dict) and item.get("attachments") for item in steps
        ),
        "no_evidence_issues": (report.get("evidence") or {}).get("issues") == []
        if isinstance(report.get("evidence"), dict)
        else False,
    }
    return {"problems": problems, "checks": checks, "screenshots": len(shots)}


async def run(serial: str, project_path: Path, evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project = load_project(project_path)
    before = focus_state(serial)
    result: dict[str, object] = {
        "contract": "pulse-runtime-evidence-physical-draft",
        "evidence": "P1.9",
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
    run_root = evidence / "runs"
    try:
        print(f"[{time.strftime('%H:%M:%S')}] connecting {serial}", flush=True)
        connected = await manager.connect(serial, timeout=30)
        session = manager.slot(serial).session
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
        run_id = str(uuid4())
        capture_started = time.monotonic()
        run = await run_case(
            project=project,
            driver=driver,
            case_id=CASE_ID,
            profile_id="target-form",
            run_id=run_id,
            activities={PACKAGE: ".FormActivity"},
            source="auto",
            suite_deadline=Deadline.after(1800),
            progress=show_progress,
            evidence_root=run_root,
            include_screenshot=True,
        )
        elapsed = time.monotonic() - capture_started
        ledger = manager.ledger.list_actions(serial)
        ledger = ledger if isinstance(ledger, list) else []
        directory = run_root / run_id
        result["run_elapsed_s"] = round(elapsed, 3)
        result["evidence_directory"] = str(directory)
        report = read_json(directory / "result.json")
        run_block = report.get("run")
        run_block = run_block if isinstance(run_block, dict) else {}
        public_evidence = {
            key: report.get(key)
            for key in ("contract", "run", "environment", "project", "evidence")
        }
        result["run_status"] = run_block.get("status")
        result["public_report"] = public_evidence
        result["ledger_actions"] = ledger
        result["control_final"] = manager.gate.snapshot(serial).as_dict()
        if not directory.is_dir():
            result["status"] = "physical_evidence_failed"
            result["reason"] = "evidence_directory_missing"
            return result
        manifest = {
            "run_id": run_id,
            "serial": serial,
            "status": run.status,
            "verified_actions": [
                item
                for item in ledger
                if isinstance(item, dict) and item.get("state") == "verified"
            ],
        }
        inspected = inspect_evidence(directory, manifest)
        result["inspection"] = inspected
        control_final = cast(dict[str, object], result["control_final"])
        checks = cast(dict[str, bool], inspected["checks"])
        disk_checks = {
            **checks,
            "control_released": control_final["holder_source"] is None,
            "all_ledger_verified": bool(ledger)
            and all(item.get("state") == "verified" for item in ledger),
        }
        result["checks"] = disk_checks
        result["status"] = (
            "physical_evidence_report_verified"
            if all(disk_checks.values())
            else "physical_evidence_failed"
        )
    except Exception as exc:
        result["status"] = "physical_evidence_failed"
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
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    evidence = Path(args.evidence)
    if not args.summary_only and evidence.exists():
        print(f"evidence directory already exists: {evidence}", flush=True)
        return 1
    if args.summary_only:
        result = read_json(evidence / "result.json")
    else:
        evidence.mkdir(parents=True)
        result = asyncio.run(run(args.serial, Path(args.project), evidence))
        (evidence / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result.get("checks", {}), ensure_ascii=False))
    print(json.dumps({"status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "physical_evidence_report_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
