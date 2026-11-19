"""Read-only Agent acceptance through an isolated running Runtime API, with redacted evidence.

Connection preparation/cleanup is performed by Runtime, never smoke_agent.py or this client.
The direct backend calls below only compare original settings, pin and forward inventory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from pulse_runtime.drivers.android.agent_transport import (
    AccessibilitySettings,
    AndroidBackend,
    ConnectionFault,
    ForwardLease,
    validate_serial,
)
from pulse_runtime.paths import read_connection


async def baseline(
    serial: str,
) -> tuple[AccessibilitySettings, str | None, set[tuple[str, str, str]]]:
    backend = AndroidBackend()
    installed = await backend.installed(serial)
    return (
        await backend.permissions(serial),
        await backend.peer(serial) if installed is not None and installed.debuggable else None,
        await ForwardLease(backend, serial).mappings(),
    )


def safe_snapshot(value: dict[str, Any]) -> dict[str, object]:
    return {
        key: value.get(key)
        for key in (
            "serial",
            "state",
            "model",
            "status",
            "ready",
            "android_api",
            "android_user",
            "agent",
            "connection",
            "capabilities",
            "diagnostic",
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--install", action="store_true", help="explicitly install only if missing")
    args = parser.parse_args()
    validate_serial(args.serial)
    root = Path(__file__).resolve().parents[1] / "build" / "p1-device-connect-20261104"
    directory = args.evidence.resolve()
    if not directory.is_relative_to(root) or directory == root or directory.exists():
        raise SystemExit("Use a new attempt directory within build/p1-device-connect-20261104")
    if not args.runtime_dir.resolve().is_relative_to(root):
        raise SystemExit("Use an isolated Runtime directory within the new evidence root")
    directory.mkdir(mode=0o700)
    result: dict[str, Any] = {
        "milestone": "P1.2",
        "scope": "device and connection only, no observations or actions",
        "serial": args.serial,
        "started_at_ms": time.time_ns() // 1_000_000,
        "status": "running",
        "checks": [],
        "unverified": [
            "two physical Android devices",
            "physical USB removal/reinsert in this milestone",
            "physical permission revocation or Agent/adbd restart in this milestone",
            "OEM/enterprise automatic-authorization failure on real devices",
            "other Android APIs/OEMs/users",
            "Windows/Linux",
            "release/non-debug bootstrap",
            "Wi-Fi/cross-machine LAN",
            "P0.4 acceptance",
            "P0.7 performance",
            "full P1 exit",
        ],
    }

    def save() -> None:
        temporary = directory / ".result.json.tmp"
        temporary.write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n")
        temporary.replace(directory / "result.json")

    def check(name: str, passed: bool, details: object = None) -> bool:
        result["checks"].append({"name": name, "passed": passed, "details": details})
        save()
        return passed

    info = read_connection(args.runtime_dir / "runtime.json")
    if info is None:
        result["status"] = "blocked"
        check("runtime_discovery", False, "runtime_unavailable")
        return 1
    result["runtime_pid"] = info.pid

    def request(
        method: str, path: str, body: dict[str, object] | None = None, *, bad_auth: bool = False
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": "Bearer intentionally-invalid" if bad_auth else f"Bearer {info.token}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{info.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:  # noqa: S310
                return response.status, json.loads(response.read(1_048_576))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read(65536))

    session_id: str | None = None
    original = None
    try:
        code, body = request("GET", "/health")
        if not check("runtime_health", code == 200 and body.get("pid") == info.pid):
            raise ConnectionFault("runtime_unavailable")
        code, _body = request("GET", "/devices", bad_auth=True)
        check("runtime_authentication_required", code == 401)
        original = asyncio.run(baseline(args.serial))
        code, body = request("GET", "/devices")
        device = next((d for d in body.get("devices", []) if d.get("serial") == args.serial), {})
        if not check(
            "device_discovery",
            code == 200 and device.get("state") == "device",
            safe_snapshot(device),
        ):
            raise ConnectionFault("device_unavailable")
        path = f"/devices/{urllib.parse.quote(args.serial, safe='')}/agent"
        code, prepared = request(
            "POST",
            f"{path}/prepare",
            {"install": args.install, "enable_accessibility": True, "timeout_ms": 60_000},
        )
        session_id = prepared.get("connection", {}).get("session_id")
        if not check(
            "agent_prepared_ready",
            code == 200 and prepared.get("ready") is True,
            safe_snapshot(prepared),
        ):
            raise ConnectionFault("manual_or_blocked_preparation")
        check(
            "verified_package_version_build_and_signer",
            prepared.get("agent", {}).get("package") == "com.pulse.agent"
            and prepared["agent"].get("version_name") == "0.1.0"
            and prepared["agent"].get("version_code") == 1
            and prepared["agent"].get("debuggable") is True
            and prepared["agent"].get("build_verified") is True
            and prepared["agent"].get("signature_verified_by_exact_artifact") is True,
        )
        code, connected = request("POST", f"{path}/connect", {})
        check(
            "runtime_connection_reuses_current_generation",
            code == 200
            and connected.get("ready") is True
            and connected.get("connection", {}).get("session_id") == session_id,
            safe_snapshot(connected),
        )
        query = urllib.parse.urlencode({"session_id": session_id})
        code, health = request("GET", f"{path}/health?{query}")
        check(
            "authenticated_agent_health",
            code == 200
            and health.get("ready") is True
            and health.get("connection", {}).get("health_authenticated") is True
            and health["connection"].get("loopback_verified") is True
            and health.get("capabilities", {}).get("accessibility") == "available"
            and health["capabilities"].get("recovery_required") is False
            and health["capabilities"].get("platform_operation_pending") is False,
            safe_snapshot(health),
        )
        code, capabilities = request("GET", f"{path}/capabilities?{query}")
        check(
            "read_capabilities_via_runtime",
            code == 200
            and capabilities.get("protocol_version") == 1
            and capabilities.get("capabilities", {}).get("minimum_android_api") == 30
            and capabilities["capabilities"].get("display_scope") == "default_only",
            capabilities,
        )
        wrong = urllib.parse.urlencode({"session_id": str(uuid4())})
        code, rejected = request("GET", f"{path}/health?{wrong}")
        check(
            "foreign_runtime_session_rejected",
            code == 409 and rejected.get("error") == "stale_session",
        )
        code, disconnected = request("DELETE", f"{path}/connection?{query}")
        check(
            "owned_connection_released",
            code == 200
            and disconnected.get("ready") is False
            and disconnected.get("connection", {}).get("session_id") is None
            and disconnected.get("diagnostic", {}).get("cleanup_pending") is False,
            safe_snapshot(disconnected),
        )
        session_id = None
        code, rejected = request("GET", f"{path}/health?{query}")
        check(
            "retired_runtime_session_rejected",
            code == 409 and rejected.get("error") == "stale_session",
        )
    except ConnectionFault as exc:
        result["blocker"] = exc.code
    except (OSError, ValueError, KeyError):
        result["blocker"] = "acceptance_transport_or_response_failed"
    finally:
        if session_id is not None:
            with contextlib.suppress(OSError, ValueError):
                query = urllib.parse.urlencode({"session_id": session_id})
                request("DELETE", f"/devices/{args.serial}/agent/connection?{query}")
        if original is not None:
            try:
                after = asyncio.run(baseline(args.serial))
                check("accessibility_original_preserved", after[0] == original[0])
                check("private_peer_original_preserved", after[1] == original[1])
                check(
                    "existing_forward_inventory_preserved",
                    after[2] == original[2],
                    {
                        "before_count": len(original[2]),
                        "after_count": len(after[2]),
                    },
                )
            except ConnectionFault:
                check("original_state_reverification", False, "cleanup_unverifiable")
        result["passed_required"] = sum(c["passed"] for c in result["checks"])
        result["total_required"] = len(result["checks"])
        result["status"] = (
            "single_device_verified"
            if "blocker" not in result and all(c["passed"] for c in result["checks"])
            else "blocked_or_failed"
        )
        result["finished_at_ms"] = time.time_ns() // 1_000_000
        result["privacy"] = (
            "No token, private input, raw device settings/pin/output, tree or screenshot retained."
        )
        save()
    print(
        json.dumps(
            {
                "status": result["status"],
                "passed": result["passed_required"],
                "total": result["total_required"],
                "evidence": str(directory / "result.json"),
            }
        )
    )
    return 0 if result["status"] == "single_device_verified" else 1


if __name__ == "__main__":
    sys.exit(main())
