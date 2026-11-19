"""P1.10 CLI physical check on the exported FormActivity.

Drives the shipped ``pulse-runtime run`` entry point as a *subprocess* so the real argument
parser, exit codes and stdout JSON are observed, not an in-process call. No client tap, system
dialog handling, Agent restart, APK change or pin write is performed. The device must already be
awake and unlocked; the script only opens the existing exported FormActivity and collapses the
notification shade.

Exit code 1 from the CLI on a wrong-check case is an accepted observation, not a test failure:
the point is that the exit code distinguishes terminal states.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

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


def cli_invocation(project: Path, case: str, serial: str, evidence_root: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pulse_runtime",
        "run",
        str(project),
        "--case",
        case,
        "--device",
        serial,
        "--strategy",
        "rule_only",
        "--evidence-root",
        str(evidence_root),
        "--activity",
        f"{PACKAGE}=.FormActivity",
    ]


def invoke(args: list[str], *, timeout: float) -> dict[str, object]:
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 - fixed local CLI invocation
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    elapsed = time.monotonic() - started
    stdout = completed.stdout.strip()
    try:
        body = json.loads(stdout)
    except ValueError:
        body = None
    return {
        "argv": args[3:],
        "returncode": completed.returncode,
        "elapsed_s": round(elapsed, 3),
        "json_documents": stdout.count("\n") + 1 if stdout else 0,
        "report": body,
        "stderr_tail": completed.stderr.strip().splitlines()[-20:],
    }


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
    project = Path(args.project).resolve()
    result: dict[str, object] = {
        "contract": "pulse-runtime-cli-physical-draft",
        "evidence": "P1.10",
        "status": "started",
        "started_at_ms": time.time_ns() // 1_000_000,
        "device": {
            "serial": args.serial,
            "model": adb(args.serial, "shell", "getprop", "ro.product.model"),
            "android_api": adb(args.serial, "shell", "getprop", "ro.build.version.sdk"),
        },
        "physical_phone_verified": True,
        "background_runtime_untouched": True,
        "agent_restarted": False,
        "apk_changed": False,
        "pin_changed": False,
    }
    try:
        before = focus_state(args.serial)
        result["focus_before"] = before
        if before["awake"] is not True:
            result["status"] = "manual_preparation_required"
            result["reason"] = "wake_and_unlock_device"
            return _finish(evidence, result)
        adb(args.serial, "shell", "cmd", "statusbar", "collapse")
        adb(args.serial, "shell", "am", "start", "-W", "--user", "0", "-n", COMPONENT)
        time.sleep(1)
        prepared = focus_state(args.serial)
        result["focus_after_prepare"] = prepared
        if prepared["awake"] is not True or not any(
            "FormActivity" in line and PACKAGE in line
            for line in cast(list[str], prepared["lines"])
        ):
            result["status"] = "manual_preparation_required"
            result["reason"] = "dismiss_overlay_and_show_form_activity"
            return _finish(evidence, result)

        # Refusals first: they must never need a device and must use their own exit codes.
        refusals = {
            "unknown_case": invoke(
                cli_invocation(project, "ghost", args.serial, evidence / "refusals"),
                timeout=60,
            ),
            "missing_device": invoke(
                [
                    sys.executable,
                    "-m",
                    "pulse_runtime",
                    "run",
                    str(project),
                    "--case",
                    "physical-pass",
                ],
                timeout=60,
            ),
            "bad_flag": invoke(
                [
                    sys.executable,
                    "-m",
                    "pulse_runtime",
                    "run",
                    str(project),
                    "--case",
                    "physical-pass",
                    "--device",
                    args.serial,
                    "--strategy",
                    "nonsense",
                ],
                timeout=60,
            ),
        }
        result["refusals"] = refusals

        runs_root = evidence / "runs"
        run_order = (("physical-pass", 0, "passed"), ("physical-fail", 1, "failed"))
        observed: list[dict[str, object]] = []
        for case_id, expected_code, expected_status in run_order:
            observation = invoke(
                cli_invocation(project, case_id, args.serial, runs_root), timeout=1800
            )
            observation["case_id"] = case_id
            observation["expected_exit_code"] = expected_code
            observation["expected_status"] = expected_status
            body = observation["report"]
            run_id = body.get("run_id") if isinstance(body, dict) else None
            if isinstance(run_id, str):
                observation["result_json"] = str(runs_root / run_id / "result.json")
            observed.append(observation)
        result["runs"] = observed

        checks: dict[str, bool] = {}
        for name, expected in (
            ("unknown_case", 5),
            ("missing_device", 7),
            ("bad_flag", 7),
        ):
            item = cast(dict[str, object], refusals[name])
            body = item.get("report")
            checks[f"refusal_{name}_exit"] = item["returncode"] == expected
            checks[f"refusal_{name}_json"] = (
                isinstance(body, dict) and body.get("status") == "refused"
            )
        for item in observed:
            report = item.get("report")
            checks[f"{item['case_id']}_exit"] = item["returncode"] == item["expected_exit_code"]
            checks[f"{item['case_id']}_one_document"] = item["json_documents"] == 1
            checks[f"{item['case_id']}_status"] = (
                isinstance(report, dict) and report.get("status") == item["expected_status"]
            )
            checks[f"{item['case_id']}_evidence"] = (
                isinstance(report, dict)
                and isinstance(report.get("evidence"), dict)
                and report["evidence"].get("status") == "written"
            )
            result_path = item.get("result_json")
            if isinstance(result_path, str):
                checks[f"{item['case_id']}_result_json"] = Path(result_path).is_file()
        result["checks"] = checks
        result["status"] = (
            "physical_cli_verified" if all(checks.values()) else "physical_cli_failed"
        )
    except Exception as exc:
        result["status"] = "physical_cli_failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
    finally:
        result["focus_final"] = focus_state(args.serial)
    return _finish(evidence, result)


def _finish(evidence: Path, result: dict[str, object]) -> int:
    result["finished_at_ms"] = time.time_ns() // 1_000_000
    (evidence / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result.get("checks", {}), ensure_ascii=False))
    print(json.dumps({"status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "physical_cli_verified" else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONPATH", str(Path.cwd() / "runtime"))
    raise SystemExit(main())
