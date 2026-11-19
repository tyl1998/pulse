"""P1.5 offline evidence: one collect+resolve read over the real Runtime HTTP API.

The device is the existing in-repo synthetic Android backend. It intercepts ADB and the Agent
wire, so this proves validation/lease/session/action-provenance integration but not a phone.
Every attempt needs a new evidence directory; no file, Runtime, device or Agent is started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import cast

import httpx2 as httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android.actions import bind_observation
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from pulse_runtime.perception.locators import LocateBody, resolve_locator
from pulse_runtime.perception.observation import normalize_observation
from runtime.tests.test_actions import ActionBackend, ActionFactory
from runtime.tests.test_actions import body as action_body
from runtime.tests.test_device_connections import SERIAL_A, session_id
from runtime.tests.test_locators import (
    candidate as locate_candidate,
)
from runtime.tests.test_locators import (
    locator as locate_locator,
)
from runtime.tests.test_locators import (
    native_tree,
    png,
)
from runtime.tests.test_locators import (
    point as locate_point,
)
from runtime.tests.test_observation_sources import capabilities, raw_observation
from runtime.tests.test_observations import ExchangeFactory

PACKAGE = "com.pulse.target"
GEOMETRY = {"display_id": 0, "width": 40, "height": 20, "rotation": 0}
TOKEN = "p1-locator-evidence-token"  # noqa: S105 - synthetic offline surface only


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    case: str
    expected: str
    actual: str
    code: str | None
    match_count: int
    action_target: bool
    source_available: bool
    passed: bool


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract: str = "pulse-p1.5-locator-offline-evidence"
    serial: str
    device: str = "synthetic_in_repo_backend"
    physical_phone_verified: bool = False
    collection_reads: int = 0
    checks: int = 0
    passed: int = 0
    failed: int = 0
    results: list[Check] = Field(default_factory=list)


def body_for(
    session: str, suffix: int, target: list[dict[str, object]], *, locators=None
) -> LocateBody:
    return LocateBody.model_validate(
        {
            "serial": SERIAL_A,
            "session_id": session,
            "request_id": f"00000000-0000-4000-8000-{suffix:012d}",
            "expected_package": PACKAGE,
            "target": {"locator": locate_locator(*target)},
            "locators": locators or {},
        }
    )


def snapshot(body: LocateBody, *, tree_failure: bool = False) -> dict[str, object]:
    raw = raw_observation()
    raw["sources"]["screenshot"]["data"]["base64"] = png(
        np.array([[[0, 255, 0], [50, 0, 180], [220, 40, 0]]] * 3, dtype=np.uint8)
    )
    raw["sources"]["ocr"]["data"]["blocks"] = [
        {"text": "Submit", "bounds": {"left": 1, "top": 1, "right": 40, "bottom": 19}}
    ]
    raw["sources"]["tree"]["data"]["nodes"] = native_tree()
    if tree_failure:
        raw["sources"]["tree"].update(
            status="unavailable",
            data=None,
            error={"code": "window_unavailable", "phase": "observe", "retryable": False},
        )
    value = normalize_observation(
        raw, requested=body.requested_sources(), capabilities=capabilities()
    )
    value.update(
        serial=body.serial,
        session_id=body.session_id,
        request_id=body.request_id,
        agent_lifecycle_id="offline-lifecycle",
        requested_display_id=0,
        status="completed",
        framing=dict.fromkeys(
            ("validated", "source_data_validated", "terminal_received", "eof_received"), True
        ),
    )
    return value


def check(name: str, expected: str, result: dict[str, object]) -> Check:
    status = str(result["status"])
    target = result["action_target"] is not None
    error = result["error"]
    code = error["code"] if isinstance(error, dict) else None
    passed = status == expected and target == (expected == "resolved")
    if expected == "ambiguous":
        passed = passed and code == "ambiguous_target"
    return Check(
        case=name,
        expected=expected,
        actual=status,
        code=code,
        match_count=int(result["match_count"]),
        action_target=target,
        source_available=True,
        passed=passed,
    )


async def execute() -> Summary:
    backend = ActionBackend()
    actions = ActionFactory(backend)
    observations = ExchangeFactory(backend)
    manager = DeviceConnectionManager(
        backend, readiness_wait=0, observation_factory=observations, action_factory=actions
    )
    summary = Summary(serial=SERIAL_A)
    try:
        session = session_id(await manager.connect(SERIAL_A))
        unique = body_for(session, 1, [locate_candidate()])
        duplicates = body_for(
            session,
            2,
            [locate_candidate("text", "Duplicate"), locate_candidate(), locate_point()],
            locators={"page": locate_locator(locate_candidate("res_id", f"{PACKAGE}:id/page"))},
        )
        live = body_for(session, 3, [locate_candidate()])
        observations.modify_result[SERIAL_A] = lambda value: actions.source(SERIAL_A, value)
        for name, expected, request, tree_failure in (
            ("unique_native_target", "resolved", unique, False),
            ("duplicate_before_id_and_point", "ambiguous", duplicates, False),
            ("source_unavailable_is_not_absence", "indeterminate", unique, True),
        ):
            value = snapshot(request, tree_failure=tree_failure)
            tree = cast(dict[str, dict[str, object]], value["sources"])["tree"]
            # Use the same fresh binding a live collection publishes, including P1.4 geometry.
            binding = bind_observation(value, time.monotonic(), GEOMETRY)
            item = check(name, expected, resolve_locator(request, value, binding=binding, age_ms=0))
            summary.results.append(
                item.model_copy(update={"source_available": tree["status"] == "available"})
            )
        app = create_app(TOKEN, device_manager=manager)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://offline",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            response = await client.post(
                f"/devices/{SERIAL_A}/agent/locate", json=live.model_dump()
            )
        summary.collection_reads = len(observations.calls)
        payload = cast(dict[str, object], response.json())
        summary.results.append(
            check("http_collect_and_resolve", "resolved", payload)
            if response.status_code == 200
            else Check(
                case="http_collect_and_resolve",
                expected="resolved",
                actual=f"http_{response.status_code}",
                code="http_error",
                match_count=0,
                action_target=False,
                source_available=False,
                passed=False,
            )
        )
        target = payload.get("action_target")
        receipt = await manager.action(
            SERIAL_A,
            action_body(
                {"serial": SERIAL_A, "session_id": session, "request_id": live.request_id},
                target=target if isinstance(target, dict) else None,
            ),
        )
        receipt_error = cast(dict[str, object] | None, receipt["error"])
        verification = cast(dict[str, object], receipt["verification"])
        summary.results.append(
            Check(
                case="located_target_uses_p1.4_checks",
                expected="completed",
                actual=str(receipt["status"]),
                code=None if receipt_error is None else str(receipt_error["code"]),
                match_count=1,
                action_target=receipt["business_success"] is None,
                source_available=verification["status"] == "available",
                passed=receipt["status"] == "completed" and receipt["business_success"] is None,
            )
        )
    finally:
        await manager.close()
    summary.checks = len(summary.results)
    summary.passed = sum(item.passed for item in summary.results)
    summary.failed = summary.checks - summary.passed
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.evidence.exists():
        print(f"evidence directory must be new: {args.evidence}", file=sys.stderr)
        return 2
    summary = asyncio.run(execute())
    args.evidence.mkdir(parents=True)
    (args.evidence / "result.json").write_text(
        json.dumps(summary.model_dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "checks": summary.checks,
                "passed": summary.passed,
                "failed": summary.failed,
                "collection_reads": summary.collection_reads,
            }
        )
    )
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
