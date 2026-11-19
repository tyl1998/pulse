"""Validate an offline project, without importing Pulse Runtime or contacting services."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from pulse_schema.models import CONTRACT_STATUS, CONTRACT_VERSION
from pulse_schema.validation import ContractError, load_project, preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--backend", choices=("pydantic", "jsonschema"), default="pydantic")
    parser.add_argument("--case")
    parser.add_argument("--profile")
    parser.add_argument("--strategy", choices=("rule_only", "ai_on_locator_failure", "ai_led"))
    args = parser.parse_args(argv)
    if bool(args.case) != bool(args.profile):
        parser.error("--case and --profile must be provided together")
    try:
        project = load_project(args.project, backend=args.backend)
    except ContractError as error:
        print(
            json.dumps(
                {
                    "valid": False,
                    "executed": False,
                    "issues": [asdict(i) for i in error.issues],
                }
            )
        )
        return 1
    except (OSError, ValueError):
        print(
            json.dumps(
                {
                    "valid": False,
                    "executed": False,
                    "error": "project could not be read or validated",
                }
            )
        )
        return 1
    result: dict[str, object] = {
        "valid": True,
        "contract_version": CONTRACT_VERSION,
        "contract_status": CONTRACT_STATUS,
        "executed": False,
    }
    code = 0
    if args.case:
        report = preflight(project, args.case, args.profile, strategy=args.strategy)
        result.update(
            {
                "offline_ready": report.offline_ready,
                "issues": [asdict(i) for i in report.issues],
                "ai_requirements": [asdict(i) for i in report.ai_requirements],
            }
        )
        code = 0 if report.offline_ready else 2
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
