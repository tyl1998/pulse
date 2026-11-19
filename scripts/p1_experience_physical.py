"""P1.11 physical local-experience check on the exported FormActivity.

Three CLI runs of the same case, as a subprocess so the shipped entry point, exit codes and
stdout JSON are observed rather than an in-process call:

1. ``--no-experience``: baseline, the stale first candidate is attempted and the run falls
   through to the declared second candidate.
2. default store (``--experience`` in the evidence tree): the run must produce the same
   terminal state, checks and attempt count; the store may or may not be seeded, because v0
   never lets experience outrank the declared order.
3. the store file deleted again: the third run must match the baseline. This is the DoD
   sentence "deleting the cache changes latency only".

A fourth, offline-only invocation proves the CLI keeps a corrupt store out of the verdict.

The device must already be awake and unlocked; the script only collapses the notification
shade and opens the existing exported FormActivity. No Agent restart, APK change, pin write,
client tap or system dialog handling is performed, and the existing background Runtime is
left alone (a private Runtime process is started by each CLI subprocess and cleaned up).
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

PROJECT = Path("fixtures/golden-cases/physical-experience")
CASE = "experience-read"
PACKAGE = "com.pulse.target"
COMPONENT = f"{PACKAGE}/.FormActivity"
STALE_CANDIDATES = ("text:Name_Legacy_Label", "desc:Submit_Old_Description")


def adb(serial: str, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed local adb used for physical evidence
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
        "keyguard": any("isKeyguardShowing=true" in line for line in window.splitlines()),
        "shade": any("NotificationShade" in line for line in lines),
    }


def cli_invocation(
    project: Path, serial: str, evidence_root: Path, *, experience: Path | None
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "pulse_runtime",
        "run",
        str(project),
        "--case",
        CASE,
        "--device",
        serial,
        "--strategy",
        "rule_only",
        "--evidence-root",
        str(evidence_root),
        "--no-screenshot",
    ]
    if experience is None:
        args.append("--no-experience")
    else:
        args.extend(["--experience", str(experience)])
    return args


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
        "stderr_tail": completed.stderr.strip().splitlines()[-30:],
    }


def status_of(report: object) -> object:
    experience = report.get("experience") if isinstance(report, dict) else None
    return experience.get("status") if isinstance(experience, dict) else None


def verdict(report: object) -> dict[str, object]:
    body = report if isinstance(report, dict) else {}
    counts = body.get("counts")
    return {
        "status": body.get("status"),
        "reason": body.get("reason"),
        "counts": counts if isinstance(counts, dict) else None,
        "exit_code": body.get("exit_code"),
    }


def store_summary(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"exists": path.exists(), "contract": None, "entries": []}
    entries = document.get("entries") if isinstance(document, dict) else None
    scopes: list[dict[str, object]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("candidate")
        scope = entry.get("scope")
        scopes.append(
            {
                "scope": scope if isinstance(scope, dict) else None,
                "candidate": candidate if isinstance(candidate, dict) else None,
                "hits": entry.get("hits"),
            }
        )
    return {"exists": True, "contract": document.get("contract"), "entries": scopes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--project", default=str(PROJECT))
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    evidence = Path(args.evidence)
    if evidence.exists():
        print(f"evidence directory already exists: {evidence}", flush=True)
        return 1
    evidence.mkdir(parents=True)
    project = Path(args.project).resolve()
    store = evidence / "store" / "locator-experience.json"
    result: dict[str, object] = {
        "contract": "pulse-runtime-local-experience-physical-draft",
        "evidence": "P1.11",
        "status": "started",
        "started_at_ms": time.time_ns() // 1_000_000,
        "device": {
            "serial": args.serial,
            "model": adb(args.serial, "shell", "getprop", "ro.product.model"),
            "android_api": adb(args.serial, "shell", "getprop", "ro.build.version.sdk"),
        },
        "case": CASE,
        "stale_candidates": list(STALE_CANDIDATES),
        "physical_phone_verified": True,
        "background_runtime_untouched": True,
        "agent_restarted": False,
        "apk_changed": False,
        "pin_changed": False,
    }
    try:
        before = focus_state(args.serial)
        result["focus_before"] = before
        if before["awake"] is not True or before["keyguard"] is True:
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

        runs_root = evidence / "runs"
        baseline = invoke(
            cli_invocation(project, args.serial, runs_root, experience=None), timeout=args.timeout
        )
        baseline["phase"] = "no-store"
        result["baseline"] = baseline
        if not isinstance(baseline["report"], dict):
            result["status"] = "physical_local_experience_failed"
            result["reason"] = "baseline_without_report"
            return _finish(evidence, result)

        seeded = invoke(
            cli_invocation(project, args.serial, runs_root, experience=store), timeout=args.timeout
        )
        seeded["phase"] = "seeded-store"
        result["seeded"] = seeded
        result["store_after_seeded"] = store_summary(store)

        removed_before = store.exists()
        if store.exists():
            store.unlink()
        deleted = invoke(
            cli_invocation(project, args.serial, runs_root, experience=store), timeout=args.timeout
        )
        deleted["phase"] = "store-deleted"
        result["deleted"] = deleted
        result["store_after_deleted"] = store_summary(store)

        corrupt = evidence / "store" / "corrupt-experience.json"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("[not a store]", encoding="utf-8")
        corrupt_run = invoke(
            cli_invocation(project, args.serial, runs_root, experience=corrupt),
            timeout=args.timeout,
        )
        corrupt_run["phase"] = "corrupt-store"
        result["corrupt"] = corrupt_run
        corrupt_report = corrupt_run.get("report")
        corrupt_experience = (
            corrupt_report.get("experience") if isinstance(corrupt_report, dict) else None
        )
        result["corrupt_store_untouched"] = (
            corrupt.read_text("utf-8") == "[not a store]" if corrupt.exists() else False
        )

        baseline_verdict = verdict(baseline["report"])
        seeded_verdict = verdict(seeded["report"])
        deleted_verdict = verdict(deleted["report"])
        corrupt_verdict = verdict(corrupt_run["report"])
        checks: dict[str, bool] = {
            "baseline_has_single_document": baseline["json_documents"] == 1,
            "baseline_terminal": baseline_verdict["status"] in {"passed", "failed", "blocked"},
            "baseline_passed": baseline_verdict["status"] == "passed",
            "seeded_same_verdict": seeded_verdict == baseline_verdict,
            "deleted_same_verdict": deleted_verdict == baseline_verdict,
            "seeded_one_document": seeded["json_documents"] == 1,
            "deleted_one_document": deleted["json_documents"] == 1,
            "seeded_reports_store": status_of(seeded["report"]) in {"ready", "corrupt"},
            "deleted_reports_store": status_of(deleted["report"]) in {"ready", "corrupt"},
            "no_store_disables_cache": status_of(baseline["report"]) == "disabled",
            "store_removed_before_third_run": removed_before or not store.exists(),
            "corrupt_store_kept_out_of_verdict": corrupt_verdict == baseline_verdict,
            "corrupt_store_reported_corrupt": isinstance(corrupt_experience, dict)
            and corrupt_experience.get("status") == "corrupt",
            "corrupt_store_not_rewritten": bool(result["corrupt_store_untouched"]),
            "control_released": all(
                isinstance(item.get("report"), dict)
                and cast(dict[str, object], item["report"]).get("control", {}).get("holder_source")  # type: ignore[union-attr]
                is None
                for item in (baseline, seeded, deleted, corrupt_run)
            ),
            "connections_closed": all(
                isinstance(item.get("report"), dict)
                and cast(dict[str, object], item["report"]).get("cleanup", {}).get("connection")  # type: ignore[union-attr]
                == "disconnected"
                for item in (baseline, seeded, deleted, corrupt_run)
            ),
        }
        result["attempts"] = {
            "probe_note": (
                "candidate attempts are read from each run's own evidence report when "
                "present; the store never changes the declaration order in v0"
            ),
            "store_entries_after_seeded": len(
                cast(dict[str, object], result["store_after_seeded"]).get("entries") or []  # type: ignore[arg-type]
            ),
        }
        result["elapsed_s"] = {
            "baseline": baseline["elapsed_s"],
            "seeded": seeded["elapsed_s"],
            "deleted": deleted["elapsed_s"],
            "corrupt": corrupt_run["elapsed_s"],
        }
        result["checks"] = checks
        result["status"] = (
            "physical_local_experience_verified"
            if all(checks.values())
            else "physical_local_experience_failed"
        )
    except subprocess.TimeoutExpired as exc:
        result["status"] = "physical_local_experience_failed"
        result["error"] = {"type": "TimeoutExpired", "argv": str(exc.cmd)[:300]}
    except Exception as exc:
        result["status"] = "physical_local_experience_failed"
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
    print(
        json.dumps({"status": result["status"], "reason": result.get("reason")}, ensure_ascii=False)
    )
    return 0 if result["status"] == "physical_local_experience_verified" else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONPATH", str(Path.cwd() / "runtime"))
    raise SystemExit(main())
