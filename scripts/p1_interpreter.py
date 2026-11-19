"""P1.7 offline interpreter evidence over the in-repo synthetic driver.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. Every
attempt must use a new evidence directory; existing files are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

from pulse_runtime.engine.interpreter import run_case
from pulse_schema.validation import load_project
from runtime.tests.test_interpreter_golden import EXPECTED, FIXTURE, FakeDriver


def check_result(result, expected: dict[str, object]) -> dict[str, object]:
    steps = {item.id: item.status for item in result.steps}
    checks = {item.id: item.status for item in result.checks}
    mismatches: list[str] = []
    if result.status != expected["status"]:
        mismatches.append(f"status:{result.status}!={expected['status']}")
    if result.reason != expected["reason"]:
        mismatches.append(f"reason:{result.reason}!={expected['reason']}")
    for name, wanted in expected.get("step_status", {}).items():
        if steps.get(name) != wanted:
            mismatches.append(f"step:{name}:{steps.get(name)}!={wanted}")
    for name, wanted in expected.get("check_status", {}).items():
        if checks.get(name) != wanted:
            mismatches.append(f"check:{name}:{checks.get(name)}!={wanted}")
    return {
        "case_id": result.case_id,
        "expected_status": expected["status"],
        "actual_status": result.status,
        "expected_reason": expected["reason"],
        "actual_reason": result.reason,
        "completed": not mismatches,
        "mismatches": mismatches,
        "steps": steps,
        "checks": checks,
        "result": result.as_dict(),
    }


def show_progress(event: str, data: dict[str, object]) -> None:
    clock = time.strftime("%H:%M:%S")
    if event == "run_start":
        print(f"[{clock}] RUN  {data['case_id']}", flush=True)
    elif event == "step_start":
        print(f"[{clock}]   -> {data['id']} ({data['type']})", flush=True)
    elif event == "step_finish":
        elapsed = float(cast(float, data.get("elapsed_ms", 0))) / 1000
        print(f"[{clock}]   <- {data['id']} {data['status']} {elapsed:.3f}s", flush=True)
    elif event == "check_finish":
        print(f"[{clock}]   == {data['id']} {data['status']}", flush=True)
    elif event == "run_finish":
        print(f"[{clock}] DONE {data['case_id']} {data['status']} {data.get('reason')}", flush=True)


async def run(evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project = load_project(FIXTURE)
    cases = {}
    for case_id, expected in EXPECTED.items():
        driver = FakeDriver(expected.get("world", {}))
        result = await run_case(
            project=project,
            driver=driver,
            case_id=case_id,
            profile_id="fake-android",
            run_id=str(uuid4()),
            progress=show_progress,
        )
        cases[case_id] = check_result(result, expected)
    passed = sum(1 for item in cases.values() if item["completed"])
    return {
        "contract": "pulse-runtime-interpreter-evidence-draft",
        "evidence": "P1.7",
        "started_at_ms": started,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "fixture": str(FIXTURE.relative_to(FIXTURE.parents[2])),
        "driver": "synthetic_scripted_driver",
        "physical_phone_verified": False,
        "checks": passed,
        "total": len(cases),
        "status": "offline_interpreter_golden" if passed == len(cases) else "failed",
        "cases": cases,
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
    result = asyncio.run(run(evidence))
    (evidence / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({key: result[key] for key in ("checks", "total", "status")}, ensure_ascii=False)
    )
    return 0 if result["status"] == "offline_interpreter_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
