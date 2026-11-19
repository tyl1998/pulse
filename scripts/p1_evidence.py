"""P1.9 offline evidence: run-directory isolation, attachments and reports.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. The script
runs the synthetic golden interpreter twice against one evidence root and checks that every
attempt owns an exclusive directory with JSON + HTML reports and a closed attachment manifest.
Every attempt must use a new evidence root; existing files are never overwritten.
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

from pulse_runtime.engine.driver import ExecutionEnvironment
from pulse_runtime.engine.interpreter import run_case
from pulse_schema.validation import load_project
from runtime.tests.test_engine_evidence import SinkDriver
from runtime.tests.test_interpreter_golden import EXPECTED, FIXTURE


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text("utf-8"))


async def run(evidence: Path) -> dict[str, object]:
    started = time.time_ns() // 1_000_000
    root = evidence / "runs"
    root.mkdir()
    project = load_project(FIXTURE)
    runs: dict[str, object] = {}
    for case_id, expected_case in EXPECTED.items():
        expected = expected_case  # one dict per case, never shared between iterations
        driver = SinkDriver(dict(expected.get("world", {})), screenshot=None)
        run_id = str(uuid4())
        result = await run_case(
            project=project,
            driver=driver,
            case_id=case_id,
            profile_id="fake-android",
            run_id=run_id,
            evidence_root=root,
            include_screenshot=True,
        )
        directory = root / run_id
        report = read_json(directory / "result.json")
        manifest = read_json(directory / "attachments.json")
        html = (directory / "result.html").read_text("utf-8")
        run_block = report.get("run")
        run_block = run_block if isinstance(run_block, dict) else {}
        environment = report.get("environment")
        environment = environment if isinstance(environment, dict) else {}
        checks = {
            "status_matches": result.status == expected["status"],
            "reason_matches": result.reason == expected["reason"],
            "report_run_matches": run_block.get("status") == result.status
            and run_block.get("run_id") == run_id,
            "expected_actual_source": all(
                isinstance(item, dict) and {"expected", "actual", "source"}.issubset(item)
                for item in report.get("checks") or []
            ),
            "environment_recorded": environment.get("device", {})
            == {"serial": "FAKE_SERIAL", "model": "Fake_Model", "android_api": "36"},
            "attachments_manifested": isinstance(manifest.get("files"), list),
            "html_self_contained": "<script" not in html.lower()
            and "http://" not in html
            and "https://" not in html,
            "isolation": sorted(item.name for item in root.iterdir())
            == sorted([entry["run_id"] for entry in runs.values()] + [run_id]),
            "no_evidence_issues": report.get("evidence", {}).get("issues") == [],  # type: ignore[union-attr]
        }
        runs[case_id] = {
            "run_id": run_id,
            "expected_status": expected["status"],
            "actual_status": result.status,
            "actual_reason": result.reason,
            "directory": str(directory.relative_to(evidence.parent)),
            "checks": checks,
            "completed": all(checks.values()),
        }
    passed = sum(1 for item in runs.values() if item["completed"])
    return {
        "contract": "pulse-runtime-evidence-report-draft",
        "evidence": "P1.9",
        "started_at_ms": started,
        "finished_at_ms": time.time_ns() // 1_000_000,
        "fixture": str(FIXTURE.relative_to(FIXTURE.parents[2])),
        "driver": "synthetic_scripted_driver",
        "environment_source": ExecutionEnvironment().as_dict(),
        "physical_phone_verified": False,
        "checks": passed,
        "total": len(runs),
        "status": "offline_evidence_golden" if passed == len(runs) else "failed",
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
    return 0 if result["status"] == "offline_evidence_golden" else 1


if __name__ == "__main__":
    raise SystemExit(main())
