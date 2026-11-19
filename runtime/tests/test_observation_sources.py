import base64
import json
import logging
import struct
import zlib
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from pulse_runtime.drivers.android import agent_transport
from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.perception.observation import (
    _to_original_bounds,
    failed_sources,
    normalize_observation,
)

NAMES = {
    "tree": "accessibility",
    "screenshot": "accessibility_screenshot",
    "ocr": "mlkit_chinese",
}
REQUESTED = {"tree": True, "screenshot": True, "ocr": True}
SECRET = "private-error-secret-do-not-return"


@pytest.fixture(autouse=True)
def no_device_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_transport, "adb", AsyncMock(side_effect=AssertionError("physical ADB forbidden"))
    )
    monkeypatch.setattr(
        agent_transport,
        "host_output",
        AsyncMock(side_effect=AssertionError("external process forbidden")),
    )


def capabilities() -> dict[str, object]:
    return {
        "accessibility": "available",
        "screenshot": "available",
        "ocr": "available",
        "ocr_engine": "mlkit_chinese_bundled",
        "ocr_languages": ["zh", "en"],
        "actions": ["tap", "swipe", "set_text", "node_click"],
        "platform_operation_pending": False,
        "recovery_required": False,
        "minimum_android_api": 30,
        "display_scope": "default_only",
    }


def chunk(kind: bytes, data: bytes = b"") -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_bytes(width: int = 20, height: int = 10, *, pixels: bytes | None = None) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = pixels if pixels is not None else (b"\0" + b"\x12\x34\x56\xff" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND")
    )


def rectangle() -> dict[str, int]:
    return {"left": 2, "top": 4, "right": 12, "bottom": 8}


def node(path: list[int] | None = None) -> dict[str, object]:
    return {
        "node_path": [] if path is None else path,
        "source": "accessibility",
        "view_id": "com.pulse.target:id/submit",
        "class_name": "android.widget.Button",
        "package_name": "com.pulse.target",
        "text": "Submit",
        "content_description": None,
        "text_redacted": False,
        "bounds": rectangle(),
        "states": {
            "enabled": False,
            "checked": None,
            "checkable": False,
            "clickable": True,
            "editable": False,
            "focused": False,
            "showing_hint_text": None,
            "selected": False,
            "visible": True,
        },
    }


def image_data() -> dict[str, object]:
    return {
        "width": 20,
        "height": 10,
        "original_width": 40,
        "original_height": 20,
        "coordinate_space": "screenshot_pixels",
        "scale_x": 0.5,
        "scale_y": 0.5,
        "display_id": 0,
        "protected_content": "not_verified",
        "captured_at_uptime_ms": 100_000_999,
    }


def raw_source(key: str, status: str = "available", *, requested: bool = True) -> dict[str, object]:
    if not requested:
        status = "unavailable"
    data = None
    if status == "available":
        if key == "tree":
            data = {
                "window_id": 7,
                "display_id": 0,
                "coordinate_space": "display_pixels",
                "coverage": "exposed_accessibility_nodes_only",
                "nodes": [node()],
            }
        else:
            data = image_data()
            if key == "screenshot":
                data.update(format="png", base64=base64.b64encode(png_bytes()).decode("ascii"))
            else:
                data["blocks"] = [{"text": "Submit", "bounds": rectangle(), "confidence": None}]
    code = {
        "tree": "window_unavailable" if status == "unavailable" else "platform_failed",
        "screenshot": "screenshot_unavailable" if status == "unavailable" else "platform_failed",
        "ocr": "ocr_unavailable" if status == "unavailable" else "ocr_failed",
    }[key]
    times = {"tree": (10, 12), "screenshot": (20, 24), "ocr": (30, 39)}[key]
    return {
        "source": NAMES[key],
        "status": status,
        "requested": requested,
        "started_at_uptime_ms": times[0] if status == "available" else None,
        "finished_at_uptime_ms": times[1] if status == "available" else None,
        "completeness": {
            "status": "partial" if status == "available" else "unavailable",
            "reasons": [],
        },
        "data": data,
        "error": None
        if status == "available"
        else {
            "code": code if requested else "not_requested",
            "phase": "observe",
            "retryable": False,
        },
    }


def raw_observation(requested: dict[str, bool] | None = None) -> dict[str, object]:
    options = REQUESTED if requested is None else requested
    return {
        "scope": "active_window",
        "sources": {
            key: raw_source(key, requested=options[key] or (key == "screenshot" and options["ocr"]))
            for key in NAMES
        },
        "capabilities": capabilities(),
        "completeness": {"status": "partial", "reasons": []},
        "revision_start": 41,
        "revision_end": 41,
        "atomic_snapshot": False,
    }


def normalize(
    raw: dict[str, object], requested: dict[str, bool] | None = None
) -> dict[str, object]:
    return normalize_observation(
        raw, requested=REQUESTED if requested is None else requested, capabilities=capabilities()
    )


def test_output_is_closed_source_local_and_never_a_fused_page() -> None:
    raw = raw_observation()
    raw.update(elements=[SECRET], page=SECRET, rotation=0)
    raw["capabilities"]["private_extra"] = SECRET
    raw["sources"]["tree"]["private_extra"] = SECRET
    result = normalize(raw)
    assert set(result) == {
        "sources",
        "completeness",
        "revision_start",
        "revision_end",
        "atomic_snapshot",
        "agent_reported_scope",
        "capabilities",
        "capabilities_at_request",
    }
    assert set(result["sources"]) == set(NAMES)
    assert SECRET not in json.dumps(result)
    for key, source in result["sources"].items():
        assert set(source) == {
            "source",
            "requested",
            "capability",
            "status",
            "data",
            "error",
            "time",
            "coordinates",
            "scope",
            "completeness",
        }
        assert source["source"] == NAMES[key]
        assert source["requested"] is True
        assert source["capability"] == "available"
        assert source["status"] == "available" and source["error"] is None
        assert source["completeness"]["status"] == "partial"
    assert result["agent_reported_scope"] == "active_window"
    assert result["sources"]["tree"]["scope"]["kind"] == "active_window"
    for key in ("screenshot", "ocr"):
        assert result["sources"][key]["scope"]["kind"] == "default_display"
        assert result["sources"][key]["scope"]["target_app_verified"] is None
        assert (
            "target_app_ownership_unverified" in result["sources"][key]["completeness"]["reasons"]
        )


@pytest.mark.parametrize("key", NAMES)
@pytest.mark.parametrize("status", ("available", "unavailable", "failed"))
def test_each_source_retains_three_states_independently(key: str, status: str) -> None:
    raw = raw_observation()
    raw["sources"][key] = raw_source(key, status)
    source = normalize(raw)["sources"][key]
    assert source["status"] == status
    if status == "available":
        assert source["data"] is not None and source["error"] is None
    else:
        assert source["data"] is None
        assert source["error"] == {"code": raw["sources"][key]["error"]["code"]}


@pytest.mark.parametrize("key", ("tree", "ocr"))
def test_successful_zero_nodes_or_blocks_is_available(key: str) -> None:
    raw = raw_observation()
    field = "nodes" if key == "tree" else "blocks"
    raw["sources"][key]["data"][field] = []
    source = normalize(raw)["sources"][key]
    assert source["status"] == "available"
    assert source["data"][field] == [] and source["error"] is None


@pytest.mark.parametrize("key", NAMES)
def test_not_requested_never_exposes_evidence_or_infers_capability_failure(key: str) -> None:
    options = dict(REQUESTED, **{key: False})
    if key == "screenshot":
        options["ocr"] = False
    result = normalize(raw_observation(options), options)
    source = result["sources"][key]
    assert source["requested"] is False and source["capability"] == "available"
    assert source["status"] == "unavailable" and source["data"] is None
    assert source["error"] == {"code": "not_requested"}
    assert source["coordinates"]["display_id"] is None


def test_all_unrequested_and_all_failed_do_not_claim_a_page() -> None:
    options = dict.fromkeys(NAMES, False)
    empty = normalize(raw_observation(options), options)
    assert empty["completeness"]["status"] == "unavailable"
    raw = raw_observation()
    raw["sources"] = {key: raw_source(key, "failed") for key in NAMES}
    failed = normalize(raw)
    assert failed["completeness"]["status"] == "unavailable"
    assert failed["atomic_snapshot"] is False
    assert all(source["data"] is None for source in failed["sources"].values())


@pytest.mark.parametrize("status", ("failed", "unavailable"))
def test_failed_sources_returns_only_map_and_keeps_request_and_declaration(status: str) -> None:
    options = {"tree": True, "screenshot": False, "ocr": True}
    sources = failed_sources(options, {}, "observation_timeout", status=status)
    assert set(sources) == set(NAMES)
    assert sources["tree"]["status"] == sources["ocr"]["status"] == status
    assert sources["tree"]["error"] == {"code": "observation_timeout"}
    assert sources["screenshot"]["error"] == {"code": "not_requested"}
    assert all(source["data"] is None for source in sources.values())
    assert all(source["capability"] is None for source in sources.values())


@pytest.mark.parametrize("code", (SECRET, None, {}, [], 1, True))
def test_failed_sources_sanitizes_foreign_error_categories(code: object) -> None:
    result = failed_sources(REQUESTED, capabilities(), code)
    assert all(source["error"] == {"code": "invalid_source_data"} for source in result.values())
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "code",
    (
        "generation_changed",
        "agent_lifecycle_changed",
        "stale_session",
        "observation_timeout",
        "observation_truncated",
        "observation_protocol_invalid",
        "permission_revoked",
        "platform_operation_pending",
        "runtime_stopped",
        "agent_observation_rejected",
    ),
)
def test_failed_sources_preserves_parent_closed_failure_codes(code: str) -> None:
    assert failed_sources(REQUESTED, {}, code)["tree"]["error"]["code"] == code


def test_request_capabilities_are_separate_from_validated_final_declarations() -> None:
    raw = raw_observation()
    raw["capabilities"].update(
        ocr="unavailable", platform_operation_pending=True, recovery_required=True
    )
    result = normalize(raw)
    assert result["capabilities"]["recovery_required"] is True
    assert result["capabilities"]["platform_operation_pending"] is True
    assert result["capabilities_at_request"]["recovery_required"] is False
    assert result["sources"]["ocr"]["capability"] == "available"
    assert result["sources"]["ocr"]["status"] == "available"
    assert (
        "capability_changed_during_capture" in result["sources"]["ocr"]["completeness"]["reasons"]
    )
    raw["capabilities"] = {}
    unknown = normalize_observation(raw, requested=REQUESTED, capabilities={})
    assert unknown["capabilities"]["recovery_required"] is None
    assert unknown["capabilities"]["platform_operation_pending"] is None
    assert unknown["sources"]["tree"]["capability"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("accessibility", True),
        ("screenshot", "failed"),
        ("ocr", []),
        ("platform_operation_pending", 0),
        ("recovery_required", "false"),
        ("minimum_android_api", True),
        ("minimum_android_api", 29),
        ("display_scope", SECRET),
        ("ocr_engine", SECRET),
        ("actions", [SECRET]),
        ("actions", ["tap", "tap"]),
        ("ocr_languages", [{}]),
    ),
)
def test_capabilities_reject_wrong_types_and_nonclosed_fields(field: str, value: object) -> None:
    raw = raw_observation()
    raw["capabilities"][field] = value
    with pytest.raises(ConnectionFault) as caught:
        normalize(raw)
    assert caught.value.code == "invalid_observation" and SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "state",
    (
        "enabled",
        "checked",
        "checkable",
        "clickable",
        "editable",
        "focused",
        "showing_hint_text",
        "selected",
        "visible",
    ),
)
@pytest.mark.parametrize("value", (True, False, None))
def test_native_states_retain_true_false_and_unknown(state: str, value: bool | None) -> None:
    raw = raw_observation()
    states = raw["sources"]["tree"]["data"]["nodes"][0]["states"]
    states.update(checkable=True, checked=None)
    states[state] = value
    result = normalize(raw)["sources"]["tree"]["data"]["nodes"][0]["states"]
    assert result[state] is value


def test_missing_native_states_and_unwired_password_scroll_fields_are_unknown() -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"][0]["states"] = {
        "enabled": False,
        "password": True,
        "long_clickable": True,
        "scrollable": False,
    }
    states = normalize(raw)["sources"]["tree"]["data"]["nodes"][0]["states"]
    assert states["enabled"] is False
    assert all(value is None for name, value in states.items() if name != "enabled")
    del raw["sources"]["tree"]["data"]["nodes"][0]["states"]
    assert all(
        value is None
        for value in normalize(raw)["sources"]["tree"]["data"]["nodes"][0]["states"].values()
    )


def test_ocr_never_overrides_or_copies_neighboring_native_states() -> None:
    raw = raw_observation()
    native = raw["sources"]["tree"]["data"]["nodes"][0]
    native["states"].update(enabled=False, checked=True, checkable=True)
    raw["sources"]["ocr"]["data"]["blocks"][0].update(
        enabled=True,
        checked=False,
        checkable=True,
        states={"enabled": True},
        confidence=0.99,
    )
    result = normalize(raw)["sources"]
    assert result["tree"]["data"]["nodes"][0]["states"]["checked"] is True
    assert result["tree"]["data"]["nodes"][0]["states"]["enabled"] is False
    block = result["ocr"]["data"]["blocks"][0]
    assert block == {"text": "Submit", "bounds": rectangle(), "confidence": None}
    assert set(result["ocr"]["data"]) == set(image_data()) | {"blocks"}


@pytest.mark.parametrize("redacted", (True, None))
def test_password_or_unknown_redaction_cannot_leak_wire_text(redacted: bool | None) -> None:
    raw = raw_observation()
    native = raw["sources"]["tree"]["data"]["nodes"][0]
    native.update(text_redacted=redacted, text=SECRET * 1000, content_description=SECRET * 1000)
    result = normalize(raw)
    safe = result["sources"]["tree"]["data"]["nodes"][0]
    assert safe["text_redacted"] is redacted
    assert safe["text"] is None and safe["content_description"] is None
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("key", NAMES)
@pytest.mark.parametrize("status", ("unavailable", "failed"))
def test_bad_failure_data_is_null_and_does_not_erase_primary_failure(key: str, status: str) -> None:
    raw = raw_observation()
    raw["sources"][key] = raw_source(key, status)
    primary = raw["sources"][key]["error"]["code"]
    raw["sources"][key]["data"] = {"blocks": [], "nodes": [SECRET], "base64": SECRET}
    result = normalize(raw)["sources"][key]
    assert result["status"] == status and result["data"] is None
    assert result["error"] == {"code": primary}
    assert "invalid_source_data" in result["completeness"]["reasons"]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("key", NAMES)
@pytest.mark.parametrize("code", (SECRET, {}, [], True))
def test_raw_error_messages_and_unknown_codes_are_never_returned_or_logged(
    key: str,
    code: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = raw_observation()
    raw["sources"][key] = raw_source(key, "failed")
    raw["sources"][key]["error"] = {
        "code": code,
        "message": SECRET,
        "phase": SECRET,
        "cause": SECRET,
    }
    raw["sources"][key]["completeness"]["reasons"] = [SECRET]
    raw["completeness"]["reasons"] = [SECRET]
    with caplog.at_level(logging.DEBUG):
        result = normalize(raw)
    assert result["sources"][key]["error"] == {"code": "source_failed"}
    assert "unknown_completeness_reason" in result["completeness"]["reasons"]
    assert SECRET not in json.dumps(result) + caplog.text


def test_real_source_names_are_recognized_in_global_completeness() -> None:
    raw = raw_observation()
    raw["completeness"]["reasons"] = [
        "accessibility_failed",
        "mlkit_chinese_unavailable",
        "tree_failed",
    ]
    reasons = normalize(raw)["completeness"]["reasons"]
    assert "accessibility_failed" in reasons and "mlkit_chinese_unavailable" in reasons
    assert "tree_failed" not in reasons and "unknown_completeness_reason" in reasons


@pytest.mark.parametrize(
    "reason",
    (
        "native_semantics_only_visual_coverage_unverified",
        "webview_semantics_limited",
        "flutter_or_surface_semantics_limited",
        "node_limit",
        "depth_limit",
        "missing_child",
        "text_truncated",
    ),
)
def test_partial_native_coverage_reasons_are_retained(reason: str) -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["completeness"]["reasons"] = [reason]
    result = normalize(raw)
    assert reason in result["sources"]["tree"]["completeness"]["reasons"]
    assert reason in result["completeness"]["reasons"]


def test_revisions_never_prove_atomicity_and_page_change_applies_to_each_evidence_source() -> None:
    raw = raw_observation()
    raw["completeness"] = {"status": "complete", "reasons": []}
    for source in raw["sources"].values():
        source["completeness"] = {"status": "complete", "reasons": []}
    same = normalize(raw)
    assert same["atomic_snapshot"] is False and same["completeness"]["status"] == "partial"
    assert all(source["completeness"]["status"] == "partial" for source in same["sources"].values())
    raw["revision_end"] = 42
    changed = normalize(raw)
    assert changed["revision_start"] == 41 and changed["revision_end"] == 42
    assert "page_changed_during_capture" in changed["completeness"]["reasons"]
    for source in changed["sources"].values():
        assert "page_changed_during_capture" in source["completeness"]["reasons"]


def test_source_times_are_uptime_and_platform_timestamp_is_uncalibrated_as_reported() -> None:
    result = normalize(raw_observation())
    assert "source_capture_times_differ" in result["completeness"]["reasons"]
    for key, source in result["sources"].items():
        assert source["time"]["clock"] == "android_uptime_ms"
        assert source["time"]["calibrated"] is False
        assert (
            source["time"]["started_at_uptime_ms"] == {"tree": 10, "screenshot": 20, "ocr": 30}[key]
        )
        assert "host_epoch_ms" not in source["time"]
        if key != "tree":
            assert source["time"]["captured_at_uptime_ms"] == 100_000_999
            assert source["time"]["capture_clock"] == "platform_as_reported"
            assert source["time"]["capture_clock_calibrated"] is False


def test_source_geometry_conflict_is_not_fused_or_backfilled() -> None:
    raw = raw_observation()
    raw["sources"]["ocr"]["data"].update(original_width=42, scale_x=20 / 42)
    result = normalize(raw)
    assert "source_geometry_conflict" in result["completeness"]["reasons"]
    assert result["sources"]["screenshot"]["data"]["original_width"] == 40
    assert result["sources"]["ocr"]["data"]["original_width"] == 42
    native = result["sources"]["tree"]["coordinates"]
    assert native["width"] is native["height"] is native["rotation"] is None
    assert native["original_width"] is native["scale_x"] is None


def test_unwired_native_geometry_and_image_rotation_are_not_inferred() -> None:
    raw = raw_observation()
    for key in NAMES:
        raw["sources"][key]["data"].update(rotation=0, native_width=40, native_height=20)
    raw["sources"]["tree"]["data"].update(width=40, height=20, scale_x=1, scale_y=1)
    result = normalize(raw)["sources"]
    assert result["tree"]["coordinates"]["width"] is None
    assert result["tree"]["coordinates"]["height"] is None
    assert all(source["coordinates"]["rotation"] is None for source in result.values())
    assert "rotation" not in result["screenshot"]["data"]


def test_unknown_capability_is_not_evidence_of_a_capability_change() -> None:
    raw = raw_observation()
    raw["capabilities"] = {}
    local = normalize(raw)["sources"]["tree"]["completeness"]["reasons"]
    assert "capability_declaration_unverified" in local
    assert "capability_changed_during_capture" not in local


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("scope", SECRET),
        ("atomic_snapshot", True),
        ("atomic_snapshot", 0),
        ("sources", []),
        ("capabilities", []),
        ("revision_start", True),
        ("revision_start", -1),
        ("revision_end", 1.5),
        ("revision_end", 1 << 63),
        ("completeness", {"status": SECRET, "reasons": [SECRET]}),
    ),
)
def test_invalid_global_fields_raise_only_closed_connection_faults(
    field: str, value: object
) -> None:
    raw = raw_observation()
    raw[field] = value
    with pytest.raises(ConnectionFault) as caught:
        normalize(raw)
    assert caught.value.code == "invalid_observation"
    assert SECRET not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("requested", False),
        ("requested", 1),
        ("source", "mlkit_chinese"),
        ("status", SECRET),
        ("status", []),
        ("error", {"code": "platform_failed", "message": SECRET}),
        ("data", None),
        ("data", {}),
        ("started_at_uptime_ms", True),
        ("finished_at_uptime_ms", 9),
        ("completeness", {"status": "unavailable", "reasons": []}),
    ),
)
def test_malformed_success_is_failed_without_erasing_other_sources(
    field: str, value: object
) -> None:
    raw = raw_observation()
    raw["sources"]["tree"][field] = value
    result = normalize(raw)
    assert result["sources"]["tree"]["status"] == "failed"
    assert result["sources"]["tree"]["data"] is None
    assert result["sources"]["tree"]["error"] == {"code": "invalid_source_data"}
    assert result["sources"]["ocr"]["status"] == "available"
    assert SECRET not in repr(result)


def test_missing_source_does_not_borrow_old_or_neighboring_evidence() -> None:
    raw = raw_observation()
    del raw["sources"]["tree"]
    result = normalize(raw)["sources"]
    assert result["tree"]["status"] == "failed" and result["tree"]["data"] is None
    assert result["screenshot"]["status"] == result["ocr"]["status"] == "available"
    raw["sources"]["tree"] = raw_source("tree", "failed")
    fresh = normalize(raw)["sources"]["tree"]
    assert fresh["data"] is None and fresh["error"]["code"] == "platform_failed"


@pytest.mark.parametrize(
    "field", ("error", "completeness", "started_at_uptime_ms", "finished_at_uptime_ms")
)
def test_success_requires_explicit_source_wire_fields(field: str) -> None:
    raw = raw_observation()
    del raw["sources"]["tree"][field]
    assert normalize(raw)["sources"]["tree"]["error"] == {"code": "invalid_source_data"}


def test_source_specific_errors_do_not_mislabel_an_unrelated_failure() -> None:
    raw = raw_observation()
    raw["sources"]["tree"] = raw_source("tree", "failed")
    raw["sources"]["tree"]["error"]["code"] = "ocr_failed"
    assert normalize(raw)["sources"]["tree"]["error"] == {"code": "source_failed"}


@pytest.mark.parametrize("path", ([True], [512], [-1], [0] * 33, [0, 0]))
def test_tree_paths_are_typed_bounded_and_have_captured_parents(path: list[int]) -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"].append(node(path))
    assert normalize(raw)["sources"]["tree"]["status"] == "failed"


def test_tree_depth_32_and_node_512_limits_are_inclusive() -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"] = [node([0] * depth) for depth in range(33)]
    assert normalize(raw)["sources"]["tree"]["status"] == "available"
    raw["sources"]["tree"]["data"]["nodes"] = [node()] + [node([i]) for i in range(511)]
    for native in raw["sources"]["tree"]["data"]["nodes"]:
        native.update(view_id=None, class_name=None, package_name=None)
    assert normalize(raw)["sources"]["tree"]["status"] == "available"
    raw["sources"]["tree"]["data"]["nodes"].append(node([511]))
    assert normalize(raw)["sources"]["tree"]["status"] == "failed"


def test_duplicate_tree_paths_and_contradictory_checked_state_are_invalid() -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"].append(node())
    assert normalize(raw)["sources"]["tree"]["status"] == "failed"
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"][0]["states"]["checked"] = True
    assert normalize(raw)["sources"]["tree"]["status"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("text", "x" * 257),
        ("class_name", "x" * 257),
        ("text", "\U0001f600" * 129),
        ("package_name", True),
        ("text", "\ud800"),
        ("text_redacted", 1),
        ("bounds", {"left": True, "top": 0, "right": 1, "bottom": 1}),
        ("bounds", {"left": 2, "top": 0, "right": 1, "bottom": 1}),
        ("states", {"enabled": 0}),
    ),
)
def test_invalid_native_fields_are_not_coerced_or_truncated_into_success(
    field: str, value: object
) -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"][0][field] = value
    source = normalize(raw)["sources"]["tree"]
    assert source["status"] == "failed" and source["data"] is None


def test_tree_utf16_field_and_total_string_budgets() -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"][0]["text"] = "\U0001f600" * 128
    assert normalize(raw)["sources"]["tree"]["status"] == "available"
    nodes = [node()] + [node([i]) for i in range(110)]
    for native in nodes:
        for field in ("text", "view_id", "class_name", "package_name", "content_description"):
            native[field] = "x" * 64
    raw["sources"]["tree"]["data"]["nodes"] = nodes
    assert normalize(raw)["sources"]["tree"]["status"] == "failed"


def test_tree_agent_text_budget_accepts_exact_32768_and_rejects_32769() -> None:
    raw = raw_observation()
    nodes = [node()] + [node([i]) for i in range(127)]
    for native in nodes:
        native.update(view_id=None, class_name=None, package_name=None, text="x" * 256)
    raw["sources"]["tree"]["data"]["nodes"] = nodes
    raw["sources"]["tree"]["completeness"]["reasons"] = ["text_truncated"]
    accepted = normalize(raw)["sources"]["tree"]
    assert accepted["status"] == "available" and accepted["completeness"]["status"] == "partial"
    assert "text_truncated" in accepted["completeness"]["reasons"]
    nodes[-1]["content_description"] = "x"
    rejected = normalize(raw)["sources"]["tree"]
    assert rejected["status"] == "failed" and rejected["data"] is None
    assert rejected["error"] == {"code": "invalid_source_data"}


@pytest.mark.parametrize("key", ("screenshot", "ocr"))
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("width", True),
        ("height", 0),
        ("width", 20.0),
        ("width", 1_500_001),
        ("original_width", True),
        ("original_width", 8_000_001),
        ("scale_x", True),
        ("scale_x", 0),
        ("scale_x", float("nan")),
        ("scale_y", float("inf")),
        ("scale_x", 10**1000),
        ("captured_at_uptime_ms", True),
        ("coordinate_space", "display_pixels"),
        ("protected_content", SECRET),
    ),
)
def test_image_geometry_is_typed_and_bounded(key: str, field: str, value: object) -> None:
    raw = raw_observation()
    raw["sources"][key]["data"][field] = value
    source = normalize(raw)["sources"][key]
    assert source["status"] == "failed" and source["data"] is None
    assert source["error"] == {"code": "invalid_source_data"}
    assert SECRET not in repr(source)


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"original_width": None}, "original_dimensions_unknown"),
        ({"original_height": None}, "original_dimensions_unknown"),
        ({"scale_x": None}, "scale_unknown"),
        ({"scale_y": None}, "scale_unknown"),
        ({"scale_x": 0.6}, "scale_inconsistent"),
        ({"original_width": 41}, "scale_inconsistent"),
    ),
)
def test_unknown_or_inconsistent_scale_does_not_default_to_one(
    changes: dict[str, object], code: str
) -> None:
    raw = raw_observation()
    raw["sources"]["screenshot"]["data"].update(changes)
    source = normalize(raw)["sources"]["screenshot"]
    assert source["status"] == "available"
    assert source["coordinates"]["to_original_bitmap"] == {
        "status": "unavailable",
        "error": {"code": code},
    }
    converted = _to_original_bounds(rectangle(), source["data"])
    assert converted["status"] == "unavailable" and converted["data"] is None
    assert converted["error"]["code"] == code


def test_known_bitmap_inverse_scaling_never_becomes_native_display_mapping() -> None:
    result = normalize(raw_observation())["sources"]
    for key in ("screenshot", "ocr"):
        source = result[key]
        converted = _to_original_bounds(rectangle(), source["data"])
        assert converted["status"] == "available" and converted["error"] is None
        assert converted["data"] == {"left": 4, "top": 8, "right": 24, "bottom": 16}
        assert (
            converted["coordinate_space"] == "screenshot_pixels"
            and converted["bitmap"] == "original"
        )
        assert source["coordinates"]["rotation"] is None
        assert source["coordinates"]["to_display"] == {
            "status": "unavailable",
            "error": {"code": "rotation_unverified"},
        }
    assert result["tree"]["data"]["nodes"][0]["bounds"] == rectangle()


@pytest.mark.parametrize(
    ("start", "end", "code"),
    (
        (0, 90, "rotation_changed"),
        (270, 0, "rotation_changed"),
        (None, 0, "rotation_unverified"),
        (True, 0, "rotation_unverified"),
    ),
)
def test_converter_rejects_changed_or_partly_unknown_rotation(
    start: object, end: object, code: str
) -> None:
    result = _to_original_bounds(rectangle(), image_data(), rotation_start=start, rotation_end=end)
    assert result["status"] == "unavailable" and result["error"] == {"code": code}


@pytest.mark.parametrize("display", (None, True, 1))
def test_converter_rejects_unknown_or_foreign_display(display: object) -> None:
    result = _to_original_bounds(rectangle(), image_data(), display_id=display)
    assert result["status"] == "unavailable" and result["error"]["code"] == "display_unverified"
    geometry = image_data()
    geometry["display_id"] = display
    assert _to_original_bounds(rectangle(), geometry)["status"] == "unavailable"


def test_converter_does_not_convert_explicitly_protected_bitmap_geometry() -> None:
    geometry = image_data()
    geometry["protected_content"] = True
    result = _to_original_bounds(rectangle(), geometry)
    assert result["status"] == "unavailable" and result["data"] is None
    assert result["error"] == {"code": "screen_protected"}


@pytest.mark.parametrize(
    "bounds",
    (
        {"left": -1, "top": 0, "right": 10, "bottom": 5},
        {"left": 0, "top": 0, "right": 21, "bottom": 5},
        {"left": 0, "top": False, "right": 10, "bottom": 5},
    ),
)
def test_converter_is_bounded_and_does_not_clip_invalid_bounds(bounds: dict[str, object]) -> None:
    assert _to_original_bounds(bounds, image_data())["status"] == "unavailable"


@pytest.mark.parametrize("key", NAMES)
def test_foreign_source_display_never_borrows_default_display_evidence(key: str) -> None:
    raw = raw_observation()
    raw["sources"][key]["data"]["display_id"] = 1
    source = normalize(raw)["sources"][key]
    assert source["status"] == "unavailable" and source["data"] is None
    assert source["error"] == {"code": "unsupported_display"}
    result = normalize_observation(
        raw_observation(), requested=REQUESTED, capabilities=capabilities(), display_id=1
    )
    assert all(source["data"] is None for source in result["sources"].values())
    assert all(
        source["error"]["code"] == "unsupported_display" for source in result["sources"].values()
    )


@pytest.mark.parametrize("key", ("screenshot", "ocr"))
@pytest.mark.parametrize("protected", (True, "protected"))
def test_positive_protection_evidence_does_not_return_pixels_or_ocr_text(
    key: str, protected: object
) -> None:
    raw = raw_observation()
    raw["sources"][key]["data"]["protected_content"] = protected
    source = normalize(raw)["sources"][key]
    assert source["status"] == "unavailable" and source["error"]["code"] == "screen_protected"
    assert source["data"] is None


def test_successful_png_or_false_protection_flag_never_proves_flag_secure_coverage() -> None:
    raw = raw_observation()
    for key in ("screenshot", "ocr"):
        raw["sources"][key]["data"]["protected_content"] = False
    result = normalize(raw)
    for key in ("screenshot", "ocr"):
        source = result["sources"][key]
        assert source["data"]["protected_content"] is None
        assert source["completeness"]["status"] == "partial"
        assert "protected_content_not_verified" in source["completeness"]["reasons"]


@pytest.mark.parametrize("field", ("width", "height"))
def test_png_ihdr_dimensions_must_match_wire_output(field: str) -> None:
    raw = raw_observation()
    raw["sources"]["screenshot"]["data"][field] += 1
    assert normalize(raw)["sources"]["screenshot"]["status"] == "failed"


@pytest.mark.parametrize("encoded", ("", "not base64", "AAAA=", "AAAA===", "\u00e9", "AAAA\n"))
def test_base64_is_strict_and_noncanonical_payloads_are_rejected(encoded: str) -> None:
    raw = raw_observation()
    raw["sources"]["screenshot"]["data"]["base64"] = encoded
    source = normalize(raw)["sources"]["screenshot"]
    assert source["status"] == "failed" and source["error"]["code"] == "invalid_source_data"


def test_png_requires_complete_crc_checked_chunks_and_a_valid_finished_idat_stream() -> None:
    good = png_bytes()
    header = good[:33]
    bad_crc = bytearray(good)
    bad_crc[29] ^= 1
    invalid_filter = png_bytes(pixels=(b"\x05" + b"\0" * 80) * 10)
    incomplete_pixels = png_bytes(pixels=b"\0" * 800)
    oversized_pixels = png_bytes(pixels=b"\0" * 8_000_000)
    bad_length = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0xFFFFFFFF) + b"IHDR"
    payloads = (
        good[:8],
        header,
        header + chunk(b"IEND"),
        good[:-12],
        good[:-1],
        good + b"x",
        bytes(bad_crc),
        bad_length,
        invalid_filter,
        incomplete_pixels,
        oversized_pixels,
        header + chunk(b"IDAT", b"not-zlib") + chunk(b"IEND"),
        header + chunk(b"IDAT", zlib.compress(b"\0" * 810)[:-1]) + chunk(b"IEND"),
        header + chunk(b"IDAT", zlib.compress(b"\0" * 810) + b"trailing") + chunk(b"IEND"),
        header + chunk(b"IHDR", good[16:29]) + good[33:],
        good[:-12] + chunk(b"IEND", b"x"),
    )
    for payload in payloads:
        raw = raw_observation()
        raw["sources"]["screenshot"]["data"]["base64"] = base64.b64encode(payload).decode("ascii")
        source = normalize(raw)["sources"]["screenshot"]
        assert source["status"] == "failed" and source["data"] is None
        assert source["error"]["code"] == "invalid_source_data"


def test_png_noncontiguous_idat_and_unknown_critical_chunks_are_rejected() -> None:
    header = png_bytes()[:33]
    compressed = zlib.compress(b"\0" * 810)
    for middle in (
        chunk(b"IDAT", compressed[:4]) + chunk(b"tEXt", b"x") + chunk(b"IDAT", compressed[4:]),
        chunk(b"ABCD", b"x") + chunk(b"IDAT", compressed),
    ):
        raw = raw_observation()
        raw["sources"]["screenshot"]["data"]["base64"] = base64.b64encode(
            header + middle + chunk(b"IEND")
        ).decode("ascii")
        assert normalize(raw)["sources"]["screenshot"]["status"] == "failed"


def test_png_split_idat_valid_pixels_and_decoded_byte_limit() -> None:
    header = png_bytes()[:33]
    compressed = zlib.compress(b"\0" * 810)
    image = (
        header + chunk(b"IDAT", compressed[:4]) + chunk(b"IDAT", compressed[4:]) + chunk(b"IEND")
    )
    raw = raw_observation()
    raw["sources"]["screenshot"]["data"]["base64"] = base64.b64encode(image).decode("ascii")
    assert normalize(raw)["sources"]["screenshot"]["status"] == "available"
    raw["sources"]["screenshot"]["data"]["base64"] = base64.b64encode(
        b"x" * (4 * 1024 * 1024 + 1)
    ).decode("ascii")
    assert normalize(raw)["sources"]["screenshot"]["status"] == "failed"


def test_rgb_png_is_accepted_and_other_png_pixel_layouts_fail_closed() -> None:
    for color, depth, interlace, expected_status in (
        (2, 8, 0, "available"),
        (0, 8, 0, "failed"),
        (6, 16, 0, "failed"),
        (6, 8, 1, "failed"),
    ):
        header = struct.pack(">IIBBBBB", 20, 10, depth, color, 0, 0, interlace)
        pixels = (b"\0" + b"\x12\x34\x56" * 20) * 10
        image = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND")
        )
        raw = raw_observation()
        raw["sources"]["screenshot"]["data"]["base64"] = base64.b64encode(image).decode("ascii")
        assert normalize(raw)["sources"]["screenshot"]["status"] == expected_status


def test_nonzero_base64_padding_bits_cannot_alias_a_canonical_png() -> None:
    image = png_bytes(1, 1)
    encoded = base64.b64encode(image).decode("ascii")
    assert encoded.endswith("=")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    index = len(encoded.rstrip("=")) - 1
    changed = encoded[:index] + alphabet[alphabet.index(encoded[index]) | 1] + encoded[index + 1 :]
    assert base64.b64decode(changed, validate=True) == image
    raw = raw_observation()
    data = raw["sources"]["screenshot"]["data"]
    data.update(width=1, height=1, original_width=1, original_height=1, scale_x=1, scale_y=1)
    data["base64"] = changed
    assert normalize(raw)["sources"]["screenshot"]["status"] == "failed"


def test_oversized_completeness_lists_fail_closed_and_long_reasons_are_generic() -> None:
    raw = raw_observation()
    raw["sources"]["tree"]["completeness"]["reasons"] = [SECRET] * 33
    assert normalize(raw)["sources"]["tree"]["error"] == {"code": "invalid_source_data"}
    raw["sources"]["tree"]["completeness"]["reasons"] = [SECRET * 100]
    result = normalize(raw)
    assert "unknown_completeness_reason" in result["completeness"]["reasons"]
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("text", "x" * 257),
        ("text", "\U0001f600" * 129),
        ("text", None),
        ("bounds", {"left": 0, "top": 0, "right": True, "bottom": 1}),
    ),
)
def test_ocr_blocks_are_typed_and_utf16_bounded(field: str, value: object) -> None:
    raw = raw_observation()
    raw["sources"]["ocr"]["data"]["blocks"][0][field] = value
    assert normalize(raw)["sources"]["ocr"]["status"] == "failed"


def test_ocr_block_limit_null_bounds_and_valid_utf16_text() -> None:
    raw = raw_observation()
    block = {"text": "\U0001f600" * 128, "bounds": None, "confidence": None}
    raw["sources"]["ocr"]["data"]["blocks"] = [deepcopy(block) for _ in range(256)]
    result = normalize(raw)["sources"]["ocr"]
    assert result["status"] == "available" and len(result["data"]["blocks"]) == 256
    raw["sources"]["ocr"]["data"]["blocks"].append(block)
    assert normalize(raw)["sources"]["ocr"]["status"] == "failed"


def test_ocr_only_collects_wire_screenshot_dependency_without_returning_image() -> None:
    options = {"tree": False, "screenshot": False, "ocr": True}
    raw = raw_observation(options)
    encoded = raw["sources"]["screenshot"]["data"]["base64"]
    assert raw["sources"]["screenshot"]["requested"] is True
    result = normalize(raw, options)
    source = result["sources"]["screenshot"]
    assert source["requested"] is False and source["status"] == "unavailable"
    assert source["error"] == {"code": "not_requested"} and source["data"] is None
    assert set(source["collection"]) == {"status", "error", "time", "purpose"}
    assert source["collection"]["status"] == "available" and source["collection"]["error"] is None
    assert source["collection"]["purpose"] == "ocr_dependency"
    assert source["coordinates"]["width"] is None
    assert encoded not in json.dumps(result)
    assert result["sources"]["ocr"]["status"] == "available"


@pytest.mark.parametrize(
    "cause",
    (
        "screen_protected",
        "screenshot_unavailable",
        "screenshot_rate_limited",
        "platform_failed",
        "unsupported_display",
        "image_too_large",
    ),
)
def test_collapsed_ocr_dependency_failure_retains_real_safe_screenshot_cause(cause: str) -> None:
    options = {"tree": False, "screenshot": False, "ocr": True}
    raw = raw_observation(options)
    raw["sources"]["screenshot"] = raw_source("screenshot", "failed")
    raw["sources"]["screenshot"]["error"]["code"] = cause
    raw["sources"]["ocr"] = raw_source("ocr", "unavailable")
    raw["sources"]["ocr"]["error"]["code"] = "screenshot_unavailable"
    result = normalize(raw, options)["sources"]
    assert result["ocr"]["data"] is None
    assert result["ocr"]["error"] == {"code": "screenshot_unavailable", "cause_code": cause}
    assert result["screenshot"]["data"] is None
    assert result["screenshot"]["collection"]["error"] == {"code": cause}


@pytest.mark.parametrize(
    ("status", "code"),
    (
        ("unavailable", "ocr_unavailable"),
        ("failed", "ocr_failed"),
    ),
)
def test_model_absence_and_recognition_failure_are_not_replaced_by_dependency_failure(
    status: str, code: str
) -> None:
    options = {"tree": False, "screenshot": False, "ocr": True}
    raw = raw_observation(options)
    raw["sources"]["screenshot"] = raw_source("screenshot", "failed")
    raw["sources"]["screenshot"]["error"]["code"] = "platform_failed"
    raw["sources"]["ocr"] = raw_source("ocr", status)
    source = normalize(raw, options)["sources"]["ocr"]
    assert source["status"] == status and source["error"] == {"code": code}
    assert source["data"] is None


def test_png_encoding_failure_does_not_erase_successful_independent_ocr() -> None:
    options = {"tree": False, "screenshot": False, "ocr": True}
    raw = raw_observation(options)
    raw["sources"]["screenshot"] = raw_source("screenshot", "failed")
    source = normalize(raw, options)["sources"]["ocr"]
    assert source["status"] == "available" and source["error"] is None
    assert source["data"]["blocks"][0]["text"] == "Submit"


@pytest.mark.parametrize("visible_screenshot", (True, False))
def test_screen_protection_conflict_cannot_leak_a_successful_ocr_payload(
    visible_screenshot: bool,
) -> None:
    options = {"tree": True, "screenshot": visible_screenshot, "ocr": True}
    raw = raw_observation(options)
    raw["sources"]["screenshot"] = raw_source("screenshot", "failed")
    raw["sources"]["screenshot"]["error"]["code"] = "screen_protected"
    raw["sources"]["ocr"]["data"]["blocks"][0]["text"] = SECRET
    result = normalize(raw, options)
    assert result["sources"]["tree"]["status"] == "available"
    assert result["sources"]["ocr"]["status"] == "unavailable"
    assert result["sources"]["ocr"]["data"] is None
    assert result["sources"]["ocr"]["error"] == {"code": "screen_protected"}
    assert "source_protection_conflict" in result["completeness"]["reasons"]
    assert SECRET not in json.dumps(result)


def test_failure_error_wire_types_are_checked_without_erasing_primary_category() -> None:
    raw = raw_observation()
    raw["sources"]["tree"] = raw_source("tree", "failed")
    raw["sources"]["tree"]["error"].update(phase=SECRET, retryable=1)
    result = normalize(raw)["sources"]["tree"]
    assert result["error"] == {"code": "platform_failed"}
    assert "invalid_source_data" in result["completeness"]["reasons"]
    assert SECRET not in json.dumps(result)


def test_dependency_requested_mismatch_is_not_treated_as_valid_collection() -> None:
    options = {"tree": False, "screenshot": False, "ocr": True}
    raw = raw_observation(options)
    raw["sources"]["screenshot"] = raw_source("screenshot", requested=False)
    result = normalize(raw, options)["sources"]["screenshot"]
    assert result["error"] == {"code": "not_requested"}
    assert result["collection"]["status"] == "failed"
    assert result["collection"]["error"] == {"code": "invalid_source_data"}


@pytest.mark.parametrize(
    ("options", "display"),
    (
        ({"tree": True}, 0),
        ({"tree": 1, "screenshot": False, "ocr": False}, 0),
        ({"tree": True, "screenshot": False, "ocr": False, "other": True}, 0),
        (REQUESTED, True),
        (REQUESTED, None),
        (REQUESTED, -1),
    ),
)
def test_runtime_request_contract_is_strict(options: object, display: object) -> None:
    with pytest.raises(ConnectionFault, match="invalid_observation"):
        normalize_observation(
            raw_observation(), requested=options, capabilities={}, display_id=display
        )


def test_failed_sources_cannot_claim_available_and_normalization_does_not_mutate_input() -> None:
    with pytest.raises(ConnectionFault, match="invalid_observation"):
        failed_sources(REQUESTED, {}, "timeout", status="available")
    raw = raw_observation()
    before = deepcopy(raw)
    result = normalize(raw)
    result["sources"]["tree"]["data"]["nodes"][0]["states"]["enabled"] = True
    result["capabilities"]["actions"].append("private-extra")
    assert raw == before
