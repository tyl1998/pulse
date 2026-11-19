"""P1.8 offline assertion evidence over the in-repo synthetic driver.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. Every
attempt must use a new evidence directory; existing files are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_runtime.engine.interpreter import RunResult, run_case
from pulse_schema.validation import load_project
from runtime.tests.assertion_fixture import AssertionDriver, AssertionWorld

FIXTURE = Path("fixtures/golden-cases/assertions")
NON_SEMANTIC = (
    "exists",
    "not_exists",
    "visible",
    "enabled",
    "checked",
    "text_equals",
    "contains",
    "regex",
    "count_eq",
    "count_gt",
    "count_lt",
    "number_cmp",
    "page_is",
)
RUNS = (
    ("all-pass", AssertionWorld(), "passed", {name: "passed" for name in NON_SEMANTIC}),
    ("all-fail", AssertionWorld(), "failed", {name: "failed" for name in NON_SEMANTIC}),
    (
        "amount-normalization",
        AssertionWorld(),
        "passed",
        {
            "cny-symbol": "passed",
            "half-up": "passed",
            "percent": "passed",
            "gt": "passed",
            "foreign-currency": "blocked",
        },
    ),
)


def statuses(result: RunResult) -> dict[str, str]:
    checks: dict[str, str] = {}
    for check in result.checks:
        checks[check.id] = check.status
    return checks


async def run(evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    project = load_project(FIXTURE)
    cases: dict[str, object] = {}
    checks: dict[str, bool] = {}
    for case_id, world, run_status, wanted in RUNS:
        driver = AssertionDriver(world)
        result = await run_case(
            project=project,
            driver=driver,
            case_id=case_id,
            profile_id="fake-android",
            run_id=str(uuid4()),
        )
        observed = statuses(result)
        cases[case_id] = {
            "status": result.status,
            "reason": result.reason,
            "checks": observed,
            "result": result.as_dict(),
        }
        checks[f"{case_id}_status"] = result.status == run_status
        checks[f"{case_id}_operators"] = observed == wanted
        print(f"{case_id}: {result.status} {observed}", flush=True)
    return {
        "contract": "pulse-runtime-assertions-evidence-draft",
        "evidence": "P1.8",
        "started_at_ms": started,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "fixture": str(FIXTURE),
        "driver": "synthetic_assertion_driver",
        "physical_phone_verified": False,
        "operators": list(NON_SEMANTIC),
        "checks": checks,
        "status": "offline_assertion_golden" if all(checks.values()) else "failed",
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
    print(json.dumps({"status": result["status"]}, ensure_ascii=False))
    return 0 if result["status"] == "offline_assertion_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
