"""Summarize one P1.8 physical attempt without rewriting its evidence.

The evidence directory is left untouched: this only prints the observed per-check values so
the hand-authored expectations can be corrected from real observation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    result = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    for run in result.get("runs", []):
        print(f"== {run['case_id']} {run['actual']} {run.get('reason')}")
        expected = run.get("expected_checks", {})
        for name, observed in run.get("observed", {}).items():
            keep = {
                key: observed[key]
                for key in ("status", "text", "value", "unit", "raw", "match_count", "sources")
                if key in observed
            }
            print(f"  {name}: expected={expected.get(name)} actual={keep}")
    print(json.dumps(result.get("checks", {}), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
