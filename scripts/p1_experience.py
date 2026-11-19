"""P1.11 offline local-experience evidence over the in-repo scripted driver.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. The point of
the script is the DoD wording: deleting the cache changes latency-related observations only.
For every case it runs the case with no store, with a store, and with the store deleted again,
and requires the terminal verdict, reasons, checks and the candidate attempt order to be
identical while only the store's own content differs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulse_runtime.engine.interpreter import RunResult, run_case
from pulse_runtime.experience.local import (
    CONTRACT,
    ExperiencedCandidate,
    ExperienceKey,
    LocalExperience,
)
from pulse_schema.validation import load_project
from runtime.tests.experience_fixture import (
    FIXTURE,
    ExperienceDriver,
    expected,
    world_for,
)

SEARCH = "com.pulse.target:id/search"


def verdict(result: RunResult) -> dict[str, object]:
    return {
        "status": result.status,
        "reason": result.reason,
        "checks": [
            {"id": item.id, "status": item.status, "code": item.code} for item in result.checks
        ],
        "steps": [
            {"id": item.id, "status": item.status, "code": item.code} for item in result.steps
        ],
    }


async def attempt(case_id: str, store: LocalExperience | None) -> dict[str, object]:
    driver = ExperienceDriver(world_for(case_id))
    before = store.path.read_bytes() if store is not None and store.path.exists() else None
    result = await run_case(
        project=load_project(FIXTURE),
        driver=driver,
        case_id=case_id,
        profile_id="fake-android",
        experience=store,
    )
    after = store.path.read_bytes() if store is not None and store.path.exists() else None
    return {
        "verdict": verdict(result),
        "attempts": [
            item for item in driver.world.attempts if item != "res_id:com.pulse.target:id/page"
        ],
        "hinted": [item is not None for item in driver.primaries],
        "store_unchanged": store is None or (store.changed is False and after == before),
    }


async def run(evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    runs: dict[str, object] = {}
    for case_id, entry in expected().items():
        path = evidence / "experience" / f"{case_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        plain = await attempt(case_id, None)
        store = LocalExperience(path)
        store.remember(
            ExperienceKey(
                "experience-golden", "com.pulse.target", "exists:page", case_id, "search"
            ),
            ExperiencedCandidate("res_id", SEARCH),
        )
        store.save()
        seeded = await attempt(case_id, LocalExperience(path))
        path.unlink()
        deleted = await attempt(case_id, LocalExperience(path))
        checks = {
            "expected_status": plain["verdict"]["status"] == entry["status"],
            "expected_reason": plain["verdict"]["reason"] == entry["reason"],
            "seeded_does_not_change_verdict": seeded["verdict"] == plain["verdict"],
            "deleted_does_not_change_verdict": deleted["verdict"] == plain["verdict"],
            "attempts_identical": plain["attempts"] == seeded["attempts"] == deleted["attempts"],
            # v0 never offers a remembered candidate, even with a seeded store: the declared
            # order is the only order, so a hit can never change which candidate is evaluated.
            "no_hint_in_any_run": not any(plain["hinted"])
            and not any(seeded["hinted"])
            and not any(deleted["hinted"]),
            "runs_never_rewrite_the_store": seeded["store_unchanged"],
            "cache_file_gone": not path.exists(),
        }
        runs[case_id] = {
            "expected": entry,
            "plain": plain,
            "seeded": seeded,
            "deleted": deleted,
            "checks": checks,
            "completed": all(checks.values()),
        }
    passed = sum(1 for item in runs.values() if item["completed"])
    return {
        "contract": "pulse-runtime-local-experience-draft",
        "evidence": "P1.11",
        "started_at_ms": started,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "fixture": str(FIXTURE.relative_to(FIXTURE.parents[2])),
        "driver": "synthetic_experience_driver",
        "store_contract": CONTRACT,
        "physical_phone_verified": False,
        "checks": passed,
        "total": len(runs),
        "status": "offline_local_experience_golden" if passed == len(runs) else "failed",
        "runs": runs,
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
    return 0 if result["status"] == "offline_local_experience_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
