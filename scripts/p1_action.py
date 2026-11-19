"""Controlled P1.4 acceptance against an already running, independent Runtime.

Run only with an explicitly prepared physical fixture. Never installs, starts Runtime,
restarts Agent, uses blind navigation, or clears application data. Evidence is metadata,
counts and boolean checks only; source payloads and original form values stay in memory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from httpx2 import AsyncClient
from p1_observe import EVIDENCE_ROOT as OBSERVE_EVIDENCE_ROOT
from p1_observe import baseline, collected, protocol_observation
from p1_observe import log_privacy as observation_log_privacy
from p1_observe import safe_observation as observation_summary

from pulse_runtime.drivers.android.agent_transport import (
    AndroidBackend,
    ConnectionFault,
    validate_serial,
)
from pulse_runtime.paths import ENV_RUNTIME_DIR, connection_file, read_connection, runtime_dir

EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "build" / "p1-action-20261104"
PACKAGE = "com.pulse.target"
COMPONENT = PACKAGE + "/.FormActivity"
PREFIX = PACKAGE + ":id/"
SOURCES = ("tree", "screenshot", "ocr")
TREE_ONLY = {"tree": True, "screenshot": False, "ocr": False}
KINDS = {"tap", "long", "swipe", "set_text", "node_click", "key", "launch", "stop", "clear"}
KEY_CODES = {3, 4, 66, 67, 82, 187}
STATUSES = {"dispatched", "completed", "rejected", "failed", "uncertain"}
ERROR_CODES = {
    "cancelled",
    "action_failed",
    "action_timeout",
    "action_uncertain",
    "action_protocol_invalid",
    "action_terminal_missing",
    "action_output_limit",
    "action_source_stale",
    "page_changed",
    "action_source_unavailable",
    "action_source_incomplete",
    "action_target_unavailable",
    "action_coordinates_out_of_range",
    "coordinate_space_mismatch",
    "coordinate_mapping_unavailable",
    "rotation_unverified",
    "unsupported_display",
    "display_geometry_unavailable",
    "display_geometry_changed",
    "sensitive_target",
    "protected_target",
    "target_changed",
    "unsupported_action",
    "platform_operation_pending",
    "recovery_required",
    "stale_session",
    "generation_changed",
    "agent_lifecycle_changed",
    "authentication_failed",
    "agent_unavailable",
    "accessibility_unavailable",
    "cleanup_required",
    "operation_in_progress",
    "action_in_progress",
    "action_request_reused",
    "action_operation_reused",
    "action_request_limit",
    "serial_mismatch",
    "adb_action_rejected",
    "post_observation_timeout",
    "post_observation_unavailable",
    "invalid_source_data",
    "invalid_observation",
    "invalid_request",
    "invalid_message",
    "cancel_window_not_observed",
    "cancel_api_cleanup_unverified",
    "acceptance_transport_or_response_failed",
    "acceptance_response_limit",
    "action_api_unavailable",
    "action_context_unavailable",
    "action_receipt_or_business_failed",
    "manual_prepare_required",
    "manual_restore_required",
    "independent_runtime_unavailable",
    "independent_runtime_session_already_owned",
    "device_unavailable",
    "agent_not_ready",
    "manual_authorization_or_connection_required",
    "original_state_unverifiable",
    "script_interrupted",
    "log_privacy_unverifiable",
    "physical_clear_disabled",
    "invalid_action_arguments",
}


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def choice(value: object, allowed: set[str] | tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return value if str(UUID(value)) == value else None
    except ValueError:
        return None


def error_code(value: object) -> str | None:
    code = mapping(value).get("code") if isinstance(value, dict) else value
    if code is None:
        return None
    return choice(code, ERROR_CODES) or "unrecognized_error"


def boolean_fields(value: object, fields: tuple[str, ...]) -> dict[str, bool | None]:
    data = mapping(value)
    return {key: data.get(key) if type(data.get(key)) is bool else None for key in fields}


def sourcecounts(value: dict[str, Any]) -> dict[str, dict[str, int | bool]]:
    """Reuse P1.3's summary, but retain only its derived counts, not nested metadata."""
    try:
        sources = observation_summary(value)["sources"]
    except (AttributeError, TypeError, ValueError):
        sources = {}
    result = {}
    for name in SOURCES:
        data = mapping(mapping(sources).get(name))
        current: dict[str, int | bool] = {"data_present": data.get("data_present") is True}
        for field in ("node_count", "redacted_node_count", "block_count", "png_base64_char_count"):
            count = data.get(field)
            if type(count) is int and count >= 0:
                current[field] = count
        result[name] = current
    return result


def safe_view(value: dict[str, Any]) -> dict[str, object]:
    context = mapping(value.get("action_context"))
    return {
        "contract": choice(value.get("contract"), {"pulse-runtime-observation-draft"}),
        **{
            key: identifier(value.get(key))
            for key in ("session_id", "request_id", "agent_lifecycle_id", "agent_http_session_id")
        },
        "status": choice(value.get("status"), {"available", "partial", "unavailable", "failed"}),
        **boolean_fields(value, ("success", "atomic_snapshot")),
        "error_code": error_code(value.get("error")),
        "sourcecounts": sourcecounts(value),
        "action_context": {
            "status": choice(context.get("status"), {"available", "unavailable"}),
            "source": choice(context.get("source"), {"adb_input_viewport"}),
            **boolean_fields(context, ("atomic_with_observation",)),
            "error_code": error_code(context.get("error")),
        },
    }


def safe_action(value: dict[str, Any]) -> dict[str, object]:
    """Closed, recursively allowlisted projection; no params, raw sources, or error prose."""
    verification = mapping(value.get("verification"))
    receipts = value.get("receipts")
    post = value.get("post_observation")
    return {
        "contract": choice(value.get("contract"), {"pulse-runtime-action-draft"}),
        **{
            key: identifier(value.get(key))
            for key in (
                "session_id",
                "request_id",
                "operation_id",
                "agent_lifecycle_id",
                "agent_http_session_id",
                "target_observation_id",
                "pre_observation_id",
            )
        },
        "kind": choice(value.get("kind"), KINDS),
        "backend": choice(value.get("backend"), {"agent_v1", "runtime_adb"}),
        "target_source": choice(value.get("target_source"), SOURCES),
        "status": choice(value.get("status"), STATUSES),
        "completion_source": choice(
            value.get("completion_source"),
            {"gesture_callback", "accessibility_action_return", "adb_command_return"},
        ),
        **boolean_fields(value, ("action_completed", "automatic_replay", "recovery_required")),
        "business_success": None,
        "error_code": error_code(value.get("error")),
        "receipts": [
            {
                "status": choice(mapping(receipt).get("status"), STATUSES),
                "source": choice(
                    mapping(receipt).get("source"),
                    {"agent_v1", "runtime_adb", "runtime", "runtime_precheck"},
                ),
                "error_code": error_code(mapping(receipt).get("error")),
            }
            for receipt in receipts[:8]
        ]
        if isinstance(receipts, list)
        else [],
        "framing": boolean_fields(
            value.get("framing"),
            (
                "validated",
                "request_sent",
                "dispatched_received",
                "terminal_received",
                "eof_received",
            ),
        ),
        "cleanup": boolean_fields(
            value.get("cleanup"),
            ("connection_closed", "cancel_requested", "cancel_acknowledged", "cleanup_complete"),
        ),
        "verification": {
            "status": choice(
                verification.get("status"), {"available", "unavailable", "not_performed"}
            ),
            "business_status": choice(verification.get("business_status"), {"not_evaluated"}),
            "error_code": error_code(verification.get("error")),
        },
        "post_observation": safe_view(post) if isinstance(post, dict) else None,
    }


def log_privacy(
    directory: Path, token: str, private_inputs: list[str], other_tokens: tuple[str, ...] = ()
) -> dict[str, int]:
    counts = observation_log_privacy(directory, token)
    paths = [path for path in directory.glob("runtime.log*") if path.is_file()]
    logs = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    counts.update(
        {
            "missing_runtime_logs": int(not paths),
            "runtime_token_occurrences": logs.count(token),
            "other_runtime_token_occurrences": sum(
                logs.count(item) for item in other_tokens if item
            ),
            "private_test_input_occurrences": sum(
                logs.count(item) for item in set(private_inputs) if item
            ),
            "bearer_credential_patterns": len(re.findall(r"Bearer [A-Za-z0-9_-]{16,}", logs)),
            "png_or_large_base64_patterns": len(re.findall(r"[A-Za-z0-9+/]{256,}={0,2}", logs)),
            "traceback_markers": logs.count("Traceback (most recent call last)"),
            "raw_observation_keys": len(
                re.findall(r'"(?:base64|node_path|content_description|blocks|nodes)"\s*:', logs)
            ),
            "raw_action_or_private_keys": len(
                re.findall(
                    r'"(?:params|text|private_input|raw_tree|raw_ocr|raw_screenshot|token|credential|'
                    r'Authorization|exception|traceback)"\s*:\s*(?!null\b)',
                    logs,
                )
            ),
        }
    )
    return counts


def for_action_geometry(value: dict[str, Any]) -> bool:
    context = mapping(value.get("action_context"))
    geometry = mapping(context.get("geometry"))
    return (
        context.get("status") == "available"
        and context.get("source") == "adb_input_viewport"
        and context.get("atomic_with_observation") is False
        and geometry.get("display_id") == 0
        and type(geometry.get("rotation")) is int
        and geometry["rotation"] in {0, 90, 180, 270}
        and all(
            type(geometry.get(key)) is int and 1 <= geometry[key] <= 8192
            for key in ("width", "height")
        )
    )


def for_action_observation(value: dict[str, Any]) -> bool:
    coordinates = mapping(mapping(mapping(value.get("sources")).get("tree")).get("coordinates"))
    return (
        collected(value, TREE_ONLY)
        and for_action_geometry(value)
        and coordinates.get("coordinate_space") == "display_pixels"
        and coordinates.get("display_id") == 0
    )


def nodes(value: dict[str, Any]) -> list[dict[str, Any]]:
    data = mapping(mapping(mapping(value.get("sources")).get("tree")).get("data"))
    values = data.get("nodes")
    return [node for node in values if isinstance(node, dict)] if isinstance(values, list) else []


def selected_node(value: dict[str, Any], view_id: str | None) -> dict[str, Any]:
    matches = [
        node
        for node in nodes(value)
        if (node.get("node_path") == [] if view_id is None else node.get("view_id") == view_id)
    ]
    if len(matches) != 1:
        raise ConnectionFault("manual_prepare_required")
    node = matches[0]
    path, state = node.get("node_path"), mapping(node.get("states"))
    if (
        not isinstance(path, list)
        or len(path) > 32
        or any(type(index) is not int or not 0 <= index <= 511 for index in path)
        or node.get("text_redacted") is not False
        or state.get("enabled") is not True
        or state.get("visible") is not True
        or (view_id is not None and node.get("package_name") != PACKAGE)
        or not isinstance(node.get("package_name"), str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", node["package_name"])
        is None
    ):
        raise ConnectionFault("manual_prepare_required")
    return node


def form_state(value: dict[str, Any]) -> dict[str, str | bool]:
    if not nodes(value) or any(node.get("package_name") != PACKAGE for node in nodes(value)):
        raise ConnectionFault("manual_prepare_required")
    selected_node(value, PREFIX + "tv_form_title")
    name = selected_node(value, PREFIX + "et_name")
    agree = selected_node(value, PREFIX + "cb_agree")
    notify = selected_node(value, PREFIX + "sw_notify")
    state = mapping(name.get("states"))
    if (
        state.get("editable") is not True
        or type(state.get("showing_hint_text")) is not bool
        or (name.get("text") is not None and not isinstance(name.get("text"), str))
        or any(
            type(mapping(node.get("states")).get("checked")) is not bool for node in (agree, notify)
        )
    ):
        raise ConnectionFault("manual_prepare_required")
    return {
        "name": "" if state["showing_hint_text"] else name.get("text") or "",
        "agree": agree["states"]["checked"],
        "notify": notify["states"]["checked"],
    }


def form_checks(value: dict[str, Any], expected: dict[str, str | bool] | None) -> dict[str, bool]:
    try:
        actual = form_state(value)
    except ConnectionFault:
        return {"controlled_form_and_states_available": False}
    submit = next(
        (node for node in nodes(value) if node.get("view_id") == PREFIX + "btn_submit"), {}
    )
    checks = {
        "controlled_form_and_states_available": True,
        "submit_enabled_matches_agree": mapping(submit.get("states")).get("enabled")
        is actual["agree"],
    }
    if expected is not None:
        checks.update(
            {
                key + "_equals_expected": actual[key] == expected[key]
                for key in ("name", "agree", "notify")
            }
        )
    return checks


def departure_shape(value: dict[str, Any]) -> bool:
    """Identity and geometry proof for a departure read that may legitimately have no tree."""
    return (
        protocol_observation(value, TREE_ONLY)
        and value.get("framing", {}).get("source_data_validated") is True
        and for_action_geometry(value)
        and (value.get("success") is True or not nodes(value))
    )


def departure_checks(value: dict[str, Any]) -> dict[str, bool]:
    """Prove the controlled package left the foreground after HOME or stop.

    The launch target is package-scoped, so the departure observation may legitimately carry
    no tree. In that case the observed departure must be reported as an empty read rather
    than silently treated as a matching page.
    """
    tree = mapping(mapping(value.get("sources")).get("tree"))
    empty = tree.get("status") in {"unavailable", "failed"} or not nodes(value)
    if empty:
        return {"departed_without_controlled_tree": not nodes(value)}
    try:
        root = selected_node(value, None)
    except ConnectionFault:
        return {"new_default_display_root_available": False}
    return {
        "new_default_display_root_available": for_action_observation(value),
        "controlled_package_not_foreground": root["package_name"] != PACKAGE,
        "controlled_form_title_absent": not any(
            node.get("view_id") == PREFIX + "tv_form_title" for node in nodes(value)
        ),
    }


def gesture_params(node: dict[str, Any], kind: str) -> dict[str, int]:
    bounds = mapping(node.get("bounds"))
    if not all(type(bounds.get(key)) is int for key in ("left", "top", "right", "bottom")):
        raise ConnectionFault("manual_prepare_required")
    left, top, right, bottom = (bounds[key] for key in ("left", "top", "right", "bottom"))
    if right - left < 4 or bottom - top < 4:
        raise ConnectionFault("manual_prepare_required")
    x, y = (left + right) // 2, (top + bottom) // 2
    params = {"x": x, "y": y, "duration_ms": 600 if kind == "long" else 80}
    if kind == "swipe":
        # Stay strictly inside the node and travel slowly. A short, fast, mostly vertical
        # drag on the vivo V2364A is claimed by the system pull-down gesture, which left
        # the notification shade covering the form on a previous attempt.
        end_x = min(x + 6, right - 1)
        end_y = y
        params.update(end_x=end_x, end_y=end_y, duration_ms=600)
    return params


def action_body(
    serial: str,
    session_id: str,
    view: dict[str, Any],
    kind: str,
    node: dict[str, Any],
    params: dict[str, object],
    timeout_ms: int = 60_000,
    expected_package: str | None = None,
) -> dict[str, Any]:
    if kind == "clear":
        raise ConnectionFault("physical_clear_disabled")
    package = node["package_name"] if expected_package is None else expected_package
    required = {
        "tap": {"x", "y", "duration_ms"},
        "long": {"x", "y", "duration_ms"},
        "swipe": {"x", "y", "end_x", "end_y", "duration_ms"},
        "set_text": {"text"},
        "node_click": set(),
        "key": {"code"},
        "launch": {"package", "activity"},
        "stop": {"package"},
    }
    if (
        kind not in required
        or set(params) != required[kind]
        or type(timeout_ms) is not int
        or not 1000 <= timeout_ms <= 90_000
        or identifier(session_id) is None
        or identifier(view.get("request_id")) is None
        or view.get("serial") != serial
        or view.get("session_id") != session_id
        or not for_action_observation(view)
        or (node not in nodes(view) if expected_package is None else bool(node))
        or (kind == "key" and (type(params["code"]) is not int or params["code"] not in KEY_CODES))
        or (kind in {"launch", "stop"} and params["package"] != PACKAGE)
        or (kind == "launch" and params["activity"] != ".FormActivity")
        or (kind == "set_text" and not isinstance(params["text"], str))
        or any(
            type(params[key]) is not int or not 0 <= params[key] <= 8191
            for key in ("x", "y", "end_x", "end_y")
            if key in params
        )
        or (
            "duration_ms" in params
            and (
                type(params["duration_ms"]) is not int
                or not (500 if kind == "long" else 50) <= params["duration_ms"] <= 1000
            )
        )
    ):
        raise ConnectionFault("invalid_action_arguments")
    validate_serial(serial)
    return {
        "serial": serial,
        "session_id": session_id,
        "request_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "kind": kind,
        "target": {
            "observation_id": view["request_id"],
            "source": "tree",
            "coordinate_space": "display_pixels",
            "display_id": 0,
            "expected_package": package,
            "node_path": list(node["node_path"]) if node else [],
        },
        "params": dict(params),
        "timeout_ms": timeout_ms,
        "agent_timeout_ms": 5000,
    }


def action_protocol(value: dict[str, Any], body: dict[str, Any]) -> dict[str, bool]:
    post = mapping(value.get("post_observation"))
    verification, framing = mapping(value.get("verification")), mapping(value.get("framing"))
    backend = "runtime_adb" if body["kind"] in {"key", "launch", "stop"} else "agent_v1"
    completion = (
        "adb_command_return"
        if backend == "runtime_adb"
        else "gesture_callback"
        if body["kind"] in {"tap", "long", "swipe"}
        else "accessibility_action_return"
    )
    receipts = value.get("receipts")
    before, after = identifier(value.get("pre_observation_id")), identifier(post.get("request_id"))
    prior = {body["request_id"], body["target"]["observation_id"]}
    return {
        "owned_action_identity": value.get("contract") == "pulse-runtime-action-draft"
        and all(
            value.get(key) == body[key]
            for key in ("serial", "session_id", "request_id", "operation_id", "kind")
        )
        and value.get("target_observation_id") == body["target"]["observation_id"]
        and value.get("target_source") == "tree"
        and value.get("backend") == backend,
        "completion_receipt_not_business_success": value.get("status") == "completed"
        and value.get("action_completed") is True
        and "business_success" in value
        and value["business_success"] is None
        and value.get("completion_source") == completion
        and value.get("automatic_replay") is False
        and value.get("recovery_required") is False,
        "dispatched_then_completed": isinstance(receipts, list)
        and len(receipts) == 2
        and [mapping(receipt).get("status") for receipt in receipts] == ["dispatched", "completed"]
        and all(mapping(receipt).get("source") == backend for receipt in receipts),
        "validated_terminal_and_eof": all(
            framing.get(key) is True
            for key in (
                "validated",
                "request_sent",
                "dispatched_received",
                "terminal_received",
                "eof_received",
            )
        ),
        "fresh_internal_pre_observation": before is not None and before not in prior,
        "new_p13_post_observation": after is not None
        and after not in prior | {before}
        and post.get("serial") == body["serial"]
        and post.get("session_id") == body["session_id"]
        and for_action_observation(post),
        "verification_available_business_not_evaluated": verification.get("status") == "available"
        and verification.get("business_status") == "not_evaluated",
    }


def verified_action(
    value: dict[str, Any], body: dict[str, Any], business_checks: dict[str, bool]
) -> bool:
    return (
        all(action_protocol(value, body).values())
        and bool(business_checks)
        and all(item is True for item in business_checks.values())
    )


def owned_cancellable(state: dict[str, Any], body: dict[str, Any]) -> bool:
    return (
        all(state.get(key) == body[key] for key in ("session_id", "request_id", "operation_id"))
        and state.get("phase") in {"pre_observation", "checking"}
        and state.get("request_sent") is False
    )


def cancel_path(path: str, body: dict[str, Any]) -> str:
    return (
        path
        + "/action/cancel?"
        + urlencode({key: body[key] for key in ("session_id", "request_id", "operation_id")})
    )


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def fresh_attempt(evidence: Path | None, private_runtime: Path) -> Path:
    root = EVIDENCE_ROOT.resolve()
    default = runtime_dir(
        env={key: value for key, value in os.environ.items() if key != ENV_RUNTIME_DIR}
    ).resolve()
    if any(
        overlaps(private_runtime.resolve(), other)
        for other in (default, runtime_dir().resolve(), root, OBSERVE_EVIDENCE_ROOT.resolve())
    ):
        raise SystemExit(
            "Use an independent private Runtime directory outside default and evidence"
        )
    if EVIDENCE_ROOT.is_symlink():
        raise SystemExit("Action evidence root must not be a symlink")
    if evidence is not None:
        directory = evidence.resolve()
        if (
            directory.parent != root
            or re.fullmatch(r"physical-[1-9][0-9]*", directory.name) is None
            or directory.exists()
            or evidence.is_symlink()
        ):
            raise SystemExit("Use a fresh build/p1-action-20261104/physical-N attempt")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if evidence is not None:
        directory.mkdir(mode=0o700)
        return directory
    number = 1
    while True:
        directory = root / f"physical-{number}"
        try:
            directory.mkdir(mode=0o700)
            return directory
        except FileExistsError:
            number += 1


async def prepare_target(serial: str, *, clear_faults: bool = False) -> None:
    backend = AndroidBackend()
    installed = await backend.command(serial, "shell", "pm", "path", "--user", "0", PACKAGE)
    if not installed.startswith("package:"):
        raise ConnectionFault("manual_prepare_required")
    args = ("shell", "am", "start", "-W", "--user", "0", "-n", COMPONENT)
    if clear_faults:
        args += ("--ez", "clear_faults", "true")
    output = await backend.command(serial, *args, timeout=10)
    if "Error:" in output or "Exception" in output or "Status: ok" not in output:
        raise ConnectionFault("manual_prepare_required")


async def run(args: argparse.Namespace) -> int:
    validate_serial(args.serial)
    timeout_ms = getattr(args, "timeout_ms", 60_000)
    if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 90_000:
        raise SystemExit("--timeout-ms must be between 1000 and 90000")
    if getattr(args, "clear_faults", False) and not args.prepare_target:
        raise SystemExit("--clear-faults requires --prepare-target")
    private_runtime = args.runtime_dir.resolve()
    directory = fresh_attempt(args.evidence, private_runtime)
    result: dict[str, Any] = {
        "milestone": "P1.4 action draft",
        "serial": args.serial,
        "status": "running",
        "started_at_epoch_ms": time.time_ns() // 1_000_000,
        "checks": [],
        "observations": [],
        "actions": [],
        "cancellation": {},
        "unverified": [
            {
                "kind": "clear",
                "physical_verified": False,
                "reason": "application_data_cannot_be_losslessly_restored",
            },
            {
                "kind": "stop",
                "physical_process_termination_verified": False,
                "reason": "post_tree_checks_foreground_departure_not_process_termination",
            },
            {
                "scope": "other_key_codes_multi_device_faults_and_platform_matrix",
                "physical_verified": False,
            },
        ],
        "privacy": "allowlisted_action_metadata_boolean_checks_sourcecounts_only",
    }

    def save() -> None:
        temporary = directory / ".result.json.tmp"
        temporary.write_text(
            json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(directory / "result.json")

    def check(name: str, passed: bool) -> bool:
        result["checks"].append({"name": name, "passed": passed is True})
        save()
        return passed is True

    info = read_connection(connection_file(private_runtime))
    previous = read_connection(connection_file())
    original = None
    original_form: dict[str, str | bool] | None = None
    session_id: str | None = None
    prepare_operation: str | None = None
    active: asyncio.Task[tuple[int, dict[str, Any]]] | None = None
    active_body: dict[str, Any] | None = None
    writes_safe, touched, departed = True, False, False
    private_inputs = ["P1Action" + uuid4().hex]
    seen_observations: set[str] = set()

    if info is None or (
        previous is not None and (info.pid == previous.pid or info.port == previous.port)
    ):
        result["blocker"] = "independent_runtime_unavailable"
        check("new_independent_runtime", False)
    else:
        async with AsyncClient(
            base_url=f"http://127.0.0.1:{info.port}",
            headers={"Authorization": f"Bearer {info.token}"},
            timeout=timeout_ms / 1000 + 20,
            trust_env=False,
        ) as client:

            async def request(
                method: str,
                path: str,
                body: dict[str, object] | None = None,
                *,
                bad_auth: bool = False,
                budget_seconds: float = 45,
            ) -> tuple[int, dict[str, Any]]:
                kwargs: dict[str, Any] = {"json": body} if body is not None else {}
                if bad_auth:
                    kwargs["headers"] = {"Authorization": "Bearer intentionally-invalid"}
                async with asyncio.timeout(budget_seconds):
                    async with client.stream(method, path, **kwargs) as response:
                        data = bytearray()
                        async for part in response.aiter_bytes(65536):
                            data.extend(part)
                            if len(data) > 12 * 1024 * 1024:
                                raise ConnectionFault("acceptance_response_limit")
                        parsed = json.loads(data)
                        if not isinstance(parsed, dict):
                            raise ConnectionFault("acceptance_transport_or_response_failed")
                        return response.status_code, parsed

            device_path = f"/devices/{quote(args.serial, safe='')}"
            path = device_path + "/agent"

            async def observe(name: str, *, may_be_empty: bool = False) -> dict[str, Any]:
                request_id = str(uuid4())
                code, view = await request(
                    "POST",
                    path + "/observe",
                    {
                        "session_id": session_id,
                        "request_id": request_id,
                        **TREE_ONLY,
                        "display_id": 0,
                        "timeout_ms": 15_000,
                        "for_action": True,
                    },
                )
                result["observations"].append({"name": name, **safe_view(view)})
                identity = (
                    code == 200
                    and view.get("serial") == args.serial
                    and view.get("session_id") == session_id
                    and view.get("request_id") == request_id
                    and request_id not in seen_observations
                )
                if may_be_empty:
                    # Departure: the launcher may expose no tree at all. Identity, geometry
                    # and source counts are still required to prove this is a fresh read.
                    passed = identity and departure_shape(view)
                else:
                    passed = identity and for_action_observation(view)
                seen_observations.add(request_id)
                if not check(name + "_for_action_observation", passed):
                    raise ConnectionFault(
                        "action_api_unavailable" if code == 404 else "action_context_unavailable"
                    )
                return view

            async def perform(
                name: str,
                kind: str,
                view: dict[str, Any],
                node: dict[str, Any],
                params: dict[str, object],
                business: Callable[[dict[str, Any]], dict[str, bool]],
                *,
                expected_package: str | None = None,
            ) -> dict[str, Any]:
                nonlocal active, active_body, writes_safe, touched, departed
                body = action_body(
                    args.serial,
                    session_id,
                    view,
                    kind,
                    node,
                    params,
                    timeout_ms,
                    expected_package,
                )
                touched = True
                active_body = body
                active = asyncio.create_task(
                    request("POST", path + "/action", body, budget_seconds=timeout_ms / 1000 + 15)
                )
                code, value = await asyncio.shield(active)
                active = active_body = None
                if value.get("recovery_required") is True or value.get("status") == "uncertain":
                    writes_safe = False
                protocol = action_protocol(value, body)
                if kind in {"key", "stop"} and all(protocol.values()):
                    departed = True
                post = mapping(value.get("post_observation"))
                checks = (
                    business(post)
                    if protocol["new_p13_post_observation"]
                    else {"new_post_business_evidence_available": False}
                )
                new_id = identifier(post.get("request_id"))
                protocol["post_not_reused_in_attempt"] = (
                    new_id is not None and new_id not in seen_observations
                )
                if new_id is not None:
                    seen_observations.add(new_id)
                result["actions"].append(
                    {
                        "name": name,
                        **safe_action(value),
                        "protocol_checks": protocol,
                        "business_checks": checks,
                    }
                )
                check(name + "_action_protocol", code == 200 and all(protocol.values()))
                passed = check(name + "_post_business", bool(checks) and all(checks.values()))
                if not (code == 200 and all(protocol.values()) and passed):
                    raise ConnectionFault(
                        "action_api_unavailable"
                        if code == 404
                        else "action_receipt_or_business_failed"
                    )
                if kind == "launch":
                    departed = False
                return post

            async def form_action(
                name: str,
                kind: str,
                field: str,
                params: dict[str, object] | None,
                expected: dict[str, str | bool],
            ) -> dict[str, Any]:
                view = await observe(name + "_before")
                form_state(view)
                node = selected_node(view, PREFIX + field)
                return await perform(
                    name,
                    kind,
                    view,
                    node,
                    gesture_params(node, kind) if params is None else params,
                    lambda post: form_checks(post, expected),
                )

            async def restore_form() -> None:
                nonlocal departed
                view = await observe("restore_snapshot", may_be_empty=departed)
                if departed:
                    view = await perform(
                        "paired_restore_launch",
                        "launch",
                        view,
                        {},
                        {"package": PACKAGE, "activity": ".FormActivity"},
                        lambda post: form_checks(post, None),
                        expected_package=PACKAGE,
                    )
                current = form_state(view)
                for key, field in (
                    ("name", "et_name"),
                    ("agree", "cb_agree"),
                    ("notify", "sw_notify"),
                ):
                    if current[key] != original_form[key]:
                        expected = {**current, key: original_form[key]}
                        view = await form_action(
                            "restore_" + key,
                            "set_text" if key == "name" else "node_click",
                            field,
                            {"text": original_form[key]} if key == "name" else {},
                            expected,
                        )
                        current = form_state(view)
                check(
                    "original_name_checkbox_switch_restored",
                    all(form_checks(view, original_form).values()),
                )

            try:
                code, health = await request("GET", "/health")
                if not check(
                    "new_independent_runtime", code == 200 and health.get("pid") == info.pid
                ):
                    raise ConnectionFault("independent_runtime_unavailable")
                code, schema = await request("GET", "/openapi.json")
                routes = mapping(schema.get("paths"))
                if not check(
                    "action_api_present",
                    code == 200
                    and all(
                        route in routes
                        for route in (
                            "/devices/{serial}/agent/action",
                            "/devices/{serial}/agent/action/cancel",
                        )
                    ),
                ):
                    raise ConnectionFault("action_api_unavailable")
                code, _ = await request("POST", path + "/action", {}, bad_auth=True)
                if not check("action_requires_runtime_auth", code == 401):
                    raise ConnectionFault("authentication_failed")
                original = await baseline(args.serial)
                if not check("existing_agent_pid_at_baseline", original[3] is not None):
                    raise ConnectionFault("manual_prepare_required")
                code, inventory = await request("GET", "/devices")
                if not check(
                    "device_discovered",
                    code == 200
                    and any(
                        device.get("serial") == args.serial and device.get("state") == "device"
                        for device in inventory.get("devices", [])
                        if isinstance(device, dict)
                    ),
                ):
                    raise ConnectionFault("device_unavailable")
                code, state = await request("GET", device_path)
                if code != 200 or mapping(state.get("connection")).get("session_id") is not None:
                    raise ConnectionFault("independent_runtime_session_already_owned")
                prepare_operation = str(uuid4())
                code, prepared = await request(
                    "POST",
                    path + "/prepare",
                    {
                        "install": False,
                        "enable_accessibility": True,
                        "timeout_ms": 60_000,
                        "operation_id": prepare_operation,
                    },
                    budget_seconds=75,
                )
                session_id = identifier(mapping(prepared.get("connection")).get("session_id"))
                agent = mapping(prepared.get("agent"))
                if not check(
                    "existing_exact_pinned_agent_prepared",
                    code == 200
                    and session_id is not None
                    and prepared.get("ready") is True
                    and agent.get("build_verified") is True
                    and agent.get("signature_verified_by_exact_artifact") is True,
                ):
                    raise ConnectionFault("manual_authorization_or_connection_required")
                code, ready = await request(
                    "GET", path + "/health?" + urlencode({"session_id": session_id})
                )
                capabilities = mapping(ready.get("capabilities"))
                if not check(
                    "authenticated_agent_idle",
                    code == 200
                    and ready.get("ready") is True
                    and mapping(ready.get("connection")).get("health_authenticated") is True
                    and capabilities.get("platform_operation_pending") is False
                    and capabilities.get("recovery_required") is False,
                ):
                    raise ConnectionFault("agent_not_ready")
                if args.prepare_target:
                    await prepare_target(
                        args.serial, clear_faults=getattr(args, "clear_faults", False)
                    )
                    check("existing_controlled_form_entry_only", True)
                view = await observe("initial_form")
                original_form = form_state(view)
                if not check(
                    "controlled_form_ready", all(form_checks(view, original_form).values())
                ):
                    raise ConnectionFault("manual_prepare_required")

                for kind in ("tap", "long", "swipe"):
                    await form_action(kind + "_title", kind, "tv_form_title", None, original_form)
                for key, field in (("agree", "cb_agree"), ("notify", "sw_notify")):
                    await form_action(
                        key + "_toggle",
                        "node_click",
                        field,
                        {},
                        {**original_form, key: not original_form[key]},
                    )
                    await form_action(key + "_toggle_back", "node_click", field, {}, original_form)
                await form_action(
                    "set_text_ascii",
                    "set_text",
                    "et_name",
                    {"text": private_inputs[0]},
                    {**original_form, "name": private_inputs[0]},
                )
                await form_action(
                    "set_text_empty",
                    "set_text",
                    "et_name",
                    {"text": ""},
                    {**original_form, "name": ""},
                )
                if original_form["name"]:
                    await form_action(
                        "name_before_departure_restored",
                        "set_text",
                        "et_name",
                        {"text": original_form["name"]},
                        original_form,
                    )

                for name, kind, params in (
                    ("key_home", "key", {"code": 3}),
                    ("stop_target", "stop", {"package": PACKAGE}),
                ):
                    view = await observe(name + "_before")
                    form_state(view)
                    post = await perform(
                        name,
                        kind,
                        view,
                        selected_node(view, PREFIX + "tv_form_title"),
                        params,
                        departure_checks,
                    )
                    # The launcher exposes no tree, so the relaunch targets the package only.
                    await perform(
                        name + "_paired_launch",
                        "launch",
                        post,
                        {},
                        {"package": PACKAGE, "activity": ".FormActivity"},
                        lambda post: form_checks(post, None),
                        expected_package=PACKAGE,
                    )
                    await restore_form()

                view = await observe("cancel_before")
                form_state(view)
                node = selected_node(view, PREFIX + "tv_form_title")
                body = action_body(
                    args.serial,
                    session_id,
                    view,
                    "tap",
                    node,
                    gesture_params(node, "tap"),
                    timeout_ms,
                )
                active_body = body
                active = asyncio.create_task(
                    request("POST", path + "/action", body, budget_seconds=timeout_ms / 1000 + 15)
                )
                cancelled, cancel_attempted, state = False, False, {}
                for _ in range(100):
                    if active.done():
                        break
                    code, snapshot = await request("GET", device_path, budget_seconds=5)
                    state = mapping(mapping(snapshot.get("diagnostic")).get("action"))
                    if code == 200 and owned_cancellable(state, body) and not active.done():
                        cancel_attempted = True
                        code, cancel = await request(
                            "POST", cancel_path(path, body), budget_seconds=10
                        )
                        result["cancellation"] = {
                            "phase_at_cancel": choice(
                                state.get("phase"), {"pre_observation", "checking"}
                            ),
                            "request_sent_at_cancel": False,
                            **boolean_fields(
                                cancel, ("cancelled", "cleanup_complete", "rollback_guaranteed")
                            ),
                        }
                        cancelled = (
                            code == 200
                            and cancel.get("cancelled") is True
                            and cancel.get("cleanup_complete") is True
                            and cancel.get("rollback_guaranteed") is False
                        )
                        break
                    if state.get("request_sent") is True:
                        break
                    await asyncio.sleep(0.01)
                code, value = await asyncio.shield(active)
                active = active_body = None
                writes_safe = (
                    value.get("status") != "uncertain"
                    and value.get("recovery_required") is not True
                )
                result["actions"].append({"name": "owned_cancel_attempt", **safe_action(value)})
                if not result["cancellation"]:
                    result["cancellation"] = {
                        "cancel_attempted": False,
                        "phase_at_check": choice(
                            state.get("phase"),
                            {
                                "checking",
                                "pre_observation",
                                "pre_dispatch",
                                "dispatching",
                                "validating_receipt",
                                "post_observation",
                                "finished",
                            },
                        ),
                        **boolean_fields(state, ("request_sent",)),
                    }
                check("owned_pre_submission_cancel_window", cancel_attempted)
                if cancel_attempted:
                    check("owned_cancel_cleanup_acknowledged_without_rollback", cancelled)
                if cancelled:
                    check(
                        "owned_cancel_did_not_submit_or_claim_rollback",
                        code == 200
                        and all(
                            value.get(key) == body[key]
                            for key in (
                                "serial",
                                "session_id",
                                "request_id",
                                "operation_id",
                            )
                        )
                        and value.get("status") == "rejected"
                        and value.get("action_completed") is False
                        and "business_success" in value
                        and value["business_success"] is None
                        and mapping(value.get("error")).get("code") == "cancelled"
                        and mapping(value.get("framing")).get("request_sent") is False
                        and value.get("post_observation") is None
                        and writes_safe,
                    )
                else:
                    result["blocker"] = (
                        "cancel_api_cleanup_unverified"
                        if cancel_attempted
                        else "cancel_window_not_observed"
                    )
                    check(
                        "uncancelled_title_action_post_business",
                        code == 200
                        and verified_action(
                            value,
                            body,
                            form_checks(mapping(value.get("post_observation")), original_form),
                        ),
                    )
            except ConnectionFault as exc:
                result["blocker"] = error_code(exc.code)
            except asyncio.CancelledError:
                result["blocker"] = "script_interrupted"
            except Exception:
                result["blocker"] = "acceptance_transport_or_response_failed"
            finally:
                if active is not None:
                    try:
                        if active_body is not None and not active.done():
                            code, snapshot = await request("GET", device_path, budget_seconds=5)
                            state = mapping(mapping(snapshot.get("diagnostic")).get("action"))
                            if code == 200 and owned_cancellable(state, active_body):
                                await request(
                                    "POST", cancel_path(path, active_body), budget_seconds=10
                                )
                        # Let submitted actions settle; cancelling them can require manual recovery.
                        async with asyncio.timeout(timeout_ms / 1000 + 15):
                            _, value = await asyncio.shield(active)
                        result["actions"].append(
                            {"name": "settled_interrupted_action", **safe_action(value)}
                        )
                        writes_safe = (
                            value.get("status") != "uncertain"
                            and value.get("recovery_required") is not True
                        )
                        active = active_body = None
                    except Exception:
                        writes_safe = False
                        check("owned_action_settlement_verified", False)
                if original_form is not None and touched:
                    try:
                        if not writes_safe or active is not None:
                            raise ConnectionFault("manual_restore_required")
                        code, ready = await request(
                            "GET", path + "/health?" + urlencode({"session_id": session_id})
                        )
                        caps = mapping(ready.get("capabilities"))
                        if (
                            code != 200
                            or caps.get("platform_operation_pending") is not False
                            or caps.get("recovery_required") is not False
                        ):
                            raise ConnectionFault("manual_restore_required")
                        await restore_form()
                    except Exception:
                        result["manual_restore_required"] = True
                        check("original_name_checkbox_switch_restored", False)
                if session_id is not None and active is None:
                    try:
                        code, disconnected = await request(
                            "DELETE", path + "/connection?" + urlencode({"session_id": session_id})
                        )
                        check(
                            "owned_connection_cleanup",
                            code == 200
                            and mapping(disconnected.get("connection")).get("session_id") is None
                            and mapping(disconnected.get("diagnostic")).get("cleanup_pending")
                            is False,
                        )
                    except Exception:
                        check("owned_connection_cleanup", False)
                elif session_id is None and prepare_operation is not None:
                    with contextlib.suppress(Exception):
                        await request(
                            "POST",
                            path + "/cancel?" + urlencode({"operation_id": prepare_operation}),
                        )
                if original is not None:
                    try:
                        after = await baseline(args.serial)
                        for index, name in enumerate(
                            (
                                "accessibility_original_preserved",
                                "private_peer_original_preserved",
                                "existing_forwards_preserved",
                                "existing_agent_pid_not_restarted",
                            )
                        ):
                            check(name, after[index] == original[index])
                    except Exception:
                        check("original_state_reverification", False)
                try:
                    code, health = await request("GET", "/health", budget_seconds=5)
                    check(
                        "independent_runtime_not_restarted",
                        code == 200
                        and health.get("pid") == info.pid
                        and read_connection(connection_file(private_runtime)) == info,
                    )
                except Exception:
                    check("independent_runtime_not_restarted", False)
                if previous is not None:
                    try:
                        async with AsyncClient(
                            base_url=f"http://127.0.0.1:{previous.port}",
                            headers={"Authorization": f"Bearer {previous.token}"},
                            timeout=5,
                            trust_env=False,
                        ) as old:
                            response = await old.get("/health")
                            check(
                                "pre_existing_runtime_not_restarted",
                                response.status_code == 200
                                and response.json().get("pid") == previous.pid
                                and read_connection(connection_file()) == previous,
                            )
                    except Exception:
                        check("pre_existing_runtime_not_restarted", False)
                else:
                    check(
                        "pre_existing_runtime_absence_preserved",
                        read_connection(connection_file()) is None,
                    )
                try:
                    counts = log_privacy(
                        private_runtime,
                        info.token,
                        private_inputs,
                        (previous.token,) if previous is not None else (),
                    )
                    result["log_privacy_counts"] = counts
                    check("independent_runtime_log_privacy", not any(counts.values()))
                except Exception:
                    check("independent_runtime_log_privacy", False)
    result["passed_required"] = sum(item["passed"] for item in result["checks"])
    result["total_required"] = len(result["checks"])
    result["required_sequence_completed"] = all(
        any(action["name"] == name for action in result["actions"])
        for name in (
            "tap_title",
            "long_title",
            "swipe_title",
            "agree_toggle",
            "agree_toggle_back",
            "notify_toggle",
            "notify_toggle_back",
            "set_text_ascii",
            "set_text_empty",
            "key_home",
            "key_home_paired_launch",
            "stop_target",
            "stop_target_paired_launch",
            "owned_cancel_attempt",
        )
    )
    result["status"] = (
        "single_device_action_verified"
        if "blocker" not in result
        and result["required_sequence_completed"]
        and all(item["passed"] for item in result["checks"])
        else "blocked_or_failed"
    )
    result["finished_at_epoch_ms"] = time.time_ns() // 1_000_000
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
    return 0 if result["status"] == "single_device_action_verified" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument(
        "--evidence", type=Path, help="Fresh physical-N directory; auto-allocated if omitted"
    )
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument(
        "--prepare-target", action="store_true", help="Existing exported FormActivity only"
    )
    parser.add_argument(
        "--clear-faults",
        action="store_true",
        help="Explicit fixture clear_faults extra with --prepare-target",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
