"""Bounded, source-local evidence from the existing Android Agent observation wire."""

from __future__ import annotations

import base64
import binascii
import math
import struct
import zlib
from typing import cast

from pulse_runtime.drivers.android.agent_transport import ConnectionFault

_NAMES = {
    "tree": "accessibility",
    "screenshot": "accessibility_screenshot",
    "ocr": "mlkit_chinese",
}
_CAPABILITIES = {"tree": "accessibility", "screenshot": "screenshot", "ocr": "ocr"}
_STATUSES = {"available", "unavailable", "failed"}
_MAX_PNG_BYTES = 4 * 1024 * 1024
_MAX_OUTPUT_PIXELS = 1_500_000
_MAX_ORIGINAL_PIXELS = 8_000_000
_MAX_INT = (1 << 31) - 1
_MAX_TIME = (1 << 63) - 1
_STATES = (
    "enabled",
    "checked",
    "checkable",
    "clickable",
    "editable",
    "focused",
    "showing_hint_text",
    "selected",
    "visible",
)
_WIRE_ERRORS = {
    "not_requested",
    "accessibility_unavailable",
    "accessibility_interrupted",
    "window_unavailable",
    "unsupported_display",
    "platform_failed",
    "screenshot_unavailable",
    "screenshot_rate_limited",
    "screen_protected",
    "image_too_large",
    "ocr_unavailable",
    "ocr_failed",
    "busy",
    "timeout",
    "cancelled",
    "disconnected",
    "stopped",
    "response_too_large",
    "internal_error",
}
_COMMON_ERRORS = _WIRE_ERRORS - {
    "not_requested",
    "screenshot_unavailable",
    "screenshot_rate_limited",
    "screen_protected",
    "image_too_large",
    "ocr_unavailable",
    "ocr_failed",
}
_SOURCE_ERRORS = {
    "tree": _COMMON_ERRORS,
    "screenshot": _COMMON_ERRORS
    | {
        "screenshot_unavailable",
        "screenshot_rate_limited",
        "screen_protected",
        "image_too_large",
    },
    "ocr": _COMMON_ERRORS | {"screenshot_unavailable", "ocr_unavailable", "ocr_failed"},
}
_FAILURE_CODES = _WIRE_ERRORS | {
    "invalid_observation",
    "invalid_source_data",
    "source_unavailable",
    "source_failed",
    "model_unavailable",
    "agent_unavailable",
    "device_disconnected",
    "authentication_failed",
    "forward_replaced",
    "forward_unavailable",
    "generation_changed",
    "recovery_required",
    "action_uncertain",
    "connection_unavailable",
    "invalid_message",
    "wire_incompatible",
    "observation_rejected",
    "output_limit",
    "credential_unavailable",
    "stale_session",
    "connection_changed",
    "agent_lifecycle_changed",
    "agent_observation_rejected",
    "invalid_framing",
    "observation_protocol_invalid",
    "observation_output_limit",
    "observation_truncated",
    "observation_timeout",
    "observation_failed",
    "platform_operation_pending",
    "runtime_stopped",
    "adb_unauthorized",
    "adb_offline",
    "unsupported_android_api",
    "unsupported_android_user",
    "agent_missing",
    "version_incompatible",
    "build_incompatible",
    "build_signature_incompatible",
    "agent_package_already_exists",
    "release_configuration_unsupported",
    "run_as_unsupported",
    "accessibility_disabled",
    "permission_revoked",
    "accessibility_enable_failed",
    "accessibility_policy_blocked",
    "accessibility_setting_denied",
    "accessibility_settings_changed",
    "accessibility_not_connected",
    "peer_configuration_missing",
    "peer_configuration_invalid",
    "peer_configuration_stale",
    "configuration_in_use",
    "manual_recovery_not_observed",
    "agent_not_connected",
}
_SOURCE_REASONS = {
    "tree": {
        "native_semantics_only_visual_coverage_unverified",
        "webview_semantics_limited",
        "flutter_or_surface_semantics_limited",
        "node_limit",
        "depth_limit",
        "missing_child",
        "text_truncated",
    },
    "screenshot": {"protected_content_not_verified"},
    "ocr": {"ocr_text_only_no_native_states", "ocr_block_limit", "ocr_text_truncated"},
}
_WIRE_REASONS = (
    set().union(*_SOURCE_REASONS.values())
    | {"page_changed_during_capture"}
    | {f"{name}_{status}" for name in _NAMES.values() for status in ("unavailable", "failed")}
)


def _integer(value: object, low: int = 0, high: int = _MAX_INT) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConnectionFault("invalid_source_data")
    return value


def _boolean(value: object) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ConnectionFault("invalid_source_data")
    return value


def _text(value: object, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or len(value) > 256:
        raise ConnectionFault("invalid_source_data")
    try:
        if len(value.encode("utf-16-le")) > 512:
            raise ConnectionFault("invalid_source_data")
    except UnicodeError:
        raise ConnectionFault("invalid_source_data") from None
    return value


def _bounds(value: object) -> dict[str, int]:
    if type(value) is not dict:
        raise ConnectionFault("invalid_source_data")
    result = {
        key: _integer(value.get(key), -(1 << 31)) for key in ("left", "top", "right", "bottom")
    }
    if result["left"] > result["right"] or result["top"] > result["bottom"]:
        raise ConnectionFault("invalid_source_data")
    return result


def _closed_code(value: object, fallback: str = "invalid_source_data") -> str:
    return (
        value if type(value) is str and len(value) <= 64 and value in _FAILURE_CODES else fallback
    )


def _capabilities(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ConnectionFault("invalid_observation")
    result: dict[str, object] = {}
    for key in ("accessibility", "screenshot", "ocr"):
        item = value.get(key)
        if item is not None and (type(item) is not str or item not in {"available", "unavailable"}):
            raise ConnectionFault("invalid_observation")
        result[key] = item
    for key in ("platform_operation_pending", "recovery_required"):
        item = value.get(key)
        if item is not None and type(item) is not bool:
            raise ConnectionFault("invalid_observation")
        result[key] = item
    for key, expected in (
        ("minimum_android_api", 30),
        ("display_scope", "default_only"),
        ("ocr_engine", "mlkit_chinese_bundled"),
    ):
        item = value.get(key)
        if item is not None and (type(item) is not type(expected) or item != expected):
            raise ConnectionFault("invalid_observation")
        result[key] = item
    for key, allowed in (
        ("actions", {"tap", "swipe", "set_text", "node_click"}),
        ("ocr_languages", {"zh", "en"}),
    ):
        items = value.get(key)
        if items is None:
            result[key] = None
        elif (
            type(items) is not list
            or len(items) > len(allowed)
            or any(type(item) is not str or item not in allowed for item in items)
            or len(set(items)) != len(items)
        ):
            raise ConnectionFault("invalid_observation")
        else:
            result[key] = list(items)
    return result


def _options(requested: object, display_id: object) -> dict[str, bool]:
    if (
        type(requested) is not dict
        or len(requested) != len(_NAMES)
        or any(key not in requested or type(requested[key]) is not bool for key in _NAMES)
        or type(display_id) is not int
        or not 0 <= display_id <= _MAX_INT
    ):
        raise ConnectionFault("invalid_observation")
    return dict(requested)


def _completeness(value: object, allowed: set[str]) -> tuple[str, list[str]]:
    if type(value) is not dict:
        raise ConnectionFault("invalid_source_data")
    status, reasons = value.get("status"), value.get("reasons")
    if (
        type(status) is not str
        or status not in {"complete", "partial", "unavailable"}
        or type(reasons) is not list
        or len(reasons) > 32
    ):
        raise ConnectionFault("invalid_source_data")
    safe: list[str] = []
    for reason in reasons:
        item = (
            reason
            if type(reason) is str and len(reason) <= 128 and reason in allowed
            else "unknown_completeness_reason"
        )
        if item not in safe:
            safe.append(item)
    return status, safe


def _time(value: dict[str, object], *, available: bool) -> dict[str, object]:
    start, end = value.get("started_at_uptime_ms"), value.get("finished_at_uptime_ms")
    if available or start is not None or end is not None:
        start = _integer(start, high=_MAX_TIME)
        end = _integer(end, high=_MAX_TIME)
        if end < start:
            raise ConnectionFault("invalid_source_data")
    return {
        "clock": "android_uptime_ms",
        "calibrated": False,
        "started_at_uptime_ms": start,
        "finished_at_uptime_ms": end,
    }


def _empty_source(
    key: str,
    requested: bool,
    capability: object,
    display_id: int,
    code: str,
    status: str = "failed",
) -> dict[str, object]:
    if not requested:
        status, code = "unavailable", "not_requested"
    coordinates: dict[str, object] = {
        "coordinate_space": "display_pixels" if key == "tree" else "screenshot_pixels",
        "requested_display_id": display_id,
        "display_id": None,
        "width": None,
        "height": None,
        "original_width": None,
        "original_height": None,
        "scale_x": None,
        "scale_y": None,
        "rotation": None,
    }
    if key != "tree":
        coordinates["to_original_bitmap"] = {
            "status": "unavailable",
            "error": {"code": "original_dimensions_unknown"},
        }
        coordinates["to_display"] = {
            "status": "unavailable",
            "error": {"code": "rotation_unverified"},
        }
    return {
        "source": _NAMES[key],
        "requested": requested,
        "capability": capability,
        "status": status,
        "data": None,
        "error": {"code": code},
        "time": _time({}, available=False),
        "coordinates": coordinates,
        "scope": {
            "kind": "active_window" if key == "tree" else "default_display",
            "display_id": None,
            "window_id": None,
            "target_app_verified": None,
        },
        "completeness": {"status": "unavailable", "reasons": [code, "non_atomic_snapshot"]},
    }


def failed_sources(
    requested: dict[str, bool],
    capabilities: dict[str, object],
    code: str,
    *,
    status: str = "failed",
    display_id: int = 0,
) -> dict[str, object]:
    """Return only the three source records; never manufacture empty successful evidence."""
    options, declared = _options(requested, display_id), _capabilities(capabilities)
    if type(status) is not str or status not in {"failed", "unavailable"}:
        raise ConnectionFault("invalid_observation")
    safe = _closed_code(code)
    if safe == "not_requested":
        safe = "invalid_source_data"
    return {
        key: _empty_source(
            key, options[key], declared[_CAPABILITIES[key]], display_id, safe, status
        )
        for key in _NAMES
    }


def _display(value: object, display_id: int) -> int:
    actual = _integer(value)
    if actual != display_id or actual != 0:
        raise ConnectionFault("unsupported_display")
    return actual


def _tree(value: dict[str, object], display_id: int) -> dict[str, object]:
    actual = _display(value.get("display_id"), display_id)
    window_id = _integer(value.get("window_id"), -(1 << 31))
    if (
        value.get("coordinate_space") != "display_pixels"
        or value.get("coverage") != "exposed_accessibility_nodes_only"
    ):
        raise ConnectionFault("invalid_source_data")
    raw_nodes = value.get("nodes")
    if type(raw_nodes) is not list or len(raw_nodes) > 512:
        raise ConnectionFault("invalid_source_data")
    nodes: list[dict[str, object]] = []
    seen: set[tuple[int, ...]] = set()
    budget = 0
    for node in raw_nodes:
        if type(node) is not dict or node.get("source") != _NAMES["tree"]:
            raise ConnectionFault("invalid_source_data")
        path = node.get("node_path")
        if type(path) is not list or len(path) > 32:
            raise ConnectionFault("invalid_source_data")
        indices = tuple(_integer(index, high=511) for index in path)
        if indices in seen or (indices and indices[:-1] not in seen) or (not seen and indices):
            raise ConnectionFault("invalid_source_data")
        seen.add(indices)
        redacted = _boolean(node.get("text_redacted"))
        item: dict[str, object] = {"node_path": list(indices), "source": _NAMES["tree"]}
        for field in ("view_id", "class_name", "package_name", "text", "content_description"):
            # Unknown redaction is not permission to return potentially secret text.
            text = (
                None
                if field in {"text", "content_description"} and redacted is not False
                else _text(node.get(field))
            )
            item[field] = text
            if text is not None:
                budget += len(text.encode("utf-16-le")) // 2
        if budget > 32768:
            raise ConnectionFault("invalid_source_data")
        raw_states = node.get("states", {})
        if type(raw_states) is not dict:
            raise ConnectionFault("invalid_source_data")
        states = {name: _boolean(raw_states.get(name)) for name in _STATES}
        if states["checkable"] is False and states["checked"] is not None:
            raise ConnectionFault("invalid_source_data")
        states.update(password=None, long_clickable=None, scrollable=None)
        item.update(text_redacted=redacted, bounds=_bounds(node.get("bounds")), states=states)
        nodes.append(item)
    return {
        "window_id": window_id,
        "display_id": actual,
        "coordinate_space": "display_pixels",
        "coverage": "exposed_accessibility_nodes_only",
        "nodes": nodes,
    }


def _scale_error(value: dict[str, object]) -> str | None:
    width, height = value.get("width"), value.get("height")
    original_width, original_height = value.get("original_width"), value.get("original_height")
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height > _MAX_OUTPUT_PIXELS
    ):
        return "invalid_source_data"
    if original_width is None or original_height is None:
        return "original_dimensions_unknown"
    if (
        type(original_width) is not int
        or type(original_height) is not int
        or original_width <= 0
        or original_height <= 0
        or original_width * original_height > _MAX_ORIGINAL_PIXELS
    ):
        return "invalid_source_data"
    x, y = value.get("scale_x"), value.get("scale_y")
    if x is None or y is None:
        return "scale_unknown"
    if type(x) not in (int, float) or type(y) not in (int, float):
        return "scale_inconsistent"
    x, y = cast(float, x), cast(float, y)
    if (
        not 0 < x <= 1
        or not 0 < y <= 1
        or not math.isclose(x, width / original_width, rel_tol=1e-9, abs_tol=1e-12)
        or not math.isclose(y, height / original_height, rel_tol=1e-9, abs_tol=1e-12)
    ):
        return "scale_inconsistent"
    return None


def _to_original_bounds(
    bounds: dict[str, object],
    geometry: dict[str, object],
    *,
    display_id: int | None = 0,
    rotation_start: int | None = None,
    rotation_end: int | None = None,
) -> dict[str, object]:
    """Invert scaling within one bitmap only, never into native display coordinates."""
    error = None
    if type(geometry) is not dict:
        error = "invalid_source_data"
    elif (
        type(display_id) is not int
        or display_id != 0
        or type(geometry.get("display_id")) is not int
        or geometry["display_id"] != display_id
    ):
        error = "display_unverified"
    elif geometry.get("coordinate_space") != "screenshot_pixels":
        error = "invalid_source_data"
    elif (
        geometry.get("protected_content") is True
        or geometry.get("protected_content") == "protected"
    ):
        error = "screen_protected"
    elif rotation_start is not None or rotation_end is not None:
        if (
            type(rotation_start) is not int
            or type(rotation_end) is not int
            or rotation_start not in {0, 90, 180, 270}
            or rotation_end not in {0, 90, 180, 270}
        ):
            error = "rotation_unverified"
        elif rotation_start != rotation_end:
            error = "rotation_changed"
    if error is None:
        error = _scale_error(geometry)
    result: dict[str, object] = {
        "status": "unavailable",
        "data": None,
        "error": None,
        "coordinate_space": "screenshot_pixels",
        "bitmap": "original",
        "display_id": display_id if type(display_id) is int and display_id == 0 else None,
    }
    if error is None:
        try:
            rect = _bounds(bounds)
        except ConnectionFault:
            error = "invalid_bounds"
        else:
            width, height = cast(int, geometry["width"]), cast(int, geometry["height"])
            original_width = cast(int, geometry["original_width"])
            original_height = cast(int, geometry["original_height"])
            scale_x, scale_y = cast(float, geometry["scale_x"]), cast(float, geometry["scale_y"])
            if (
                rect["left"] < 0
                or rect["top"] < 0
                or rect["right"] > width
                or rect["bottom"] > height
            ):
                error = "bounds_out_of_range"
            else:
                result.update(
                    status="available",
                    data={
                        key: min(
                            rect[key] / (scale_x if key in {"left", "right"} else scale_y),
                            original_width if key in {"left", "right"} else original_height,
                        )
                        for key in rect
                    },
                )
    if error is not None:
        result["error"] = {"code": error}
    return result


def _png(value: object, width: int, height: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) % 4
        or len(value) > ((_MAX_PNG_BYTES + 2) // 3) * 4
    ):
        raise ConnectionFault("invalid_source_data")
    try:
        image = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ConnectionFault("invalid_source_data") from None
    if (
        len(image) > _MAX_PNG_BYTES
        or base64.b64encode(image).decode("ascii") != value
        or image[:8] != b"\x89PNG\r\n\x1a\n"
    ):
        raise ConnectionFault("invalid_source_data")
    offset, chunks, seen_idat, ended_idat = 8, 0, False, False
    decoded, row_size, expected = 0, 0, 0
    inflater = zlib.decompressobj()
    while offset < len(image):
        chunks += 1
        if chunks > 4096 or len(image) - offset < 12:
            raise ConnectionFault("invalid_source_data")
        length = struct.unpack_from(">I", image, offset)[0]
        if length > len(image) - offset - 12:
            raise ConnectionFault("invalid_source_data")
        kind = image[offset + 4 : offset + 8]
        payload = memoryview(image)[offset + 8 : offset + 8 + length]
        crc = struct.unpack_from(">I", image, offset + 8 + length)[0]
        if (
            len(kind) != 4
            or any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind)
            or not 65 <= kind[2] <= 90
            or zlib.crc32(payload, zlib.crc32(kind)) != crc
        ):
            raise ConnectionFault("invalid_source_data")
        offset += length + 12
        if chunks == 1:
            if kind != b"IHDR" or length != 13:
                raise ConnectionFault("invalid_source_data")
            w, h, depth, color, compression, filters, interlace = struct.unpack(">IIBBBBB", payload)
            # The Agent encodes software ARGB_8888 bitmaps, not arbitrary PNG assets.
            if (
                (w, h) != (width, height)
                or depth != 8
                or color not in {2, 6}
                or (compression, filters, interlace) != (0, 0, 0)
            ):
                raise ConnectionFault("invalid_source_data")
            row_size = 1 + width * (3 if color == 2 else 4)
            expected = row_size * height
        elif kind == b"IHDR":
            raise ConnectionFault("invalid_source_data")
        elif kind == b"IDAT":
            if ended_idat or (inflater.eof and length):
                raise ConnectionFault("invalid_source_data")
            seen_idat = True
            try:
                for start in range(0, length, 65536):
                    pending = bytes(payload[start : start + 65536])
                    while pending:
                        output = inflater.decompress(pending, min(65536, expected - decoded + 1))
                        if decoded + len(output) > expected or inflater.unused_data:
                            raise ConnectionFault("invalid_source_data")
                        if any(
                            output[i] > 4
                            for i in range((-decoded) % row_size, len(output), row_size)
                        ):
                            raise ConnectionFault("invalid_source_data")
                        decoded += len(output)
                        pending = inflater.unconsumed_tail
            except zlib.error:
                raise ConnectionFault("invalid_source_data") from None
        elif kind == b"IEND":
            if (
                length
                or offset != len(image)
                or not seen_idat
                or not inflater.eof
                or decoded != expected
            ):
                raise ConnectionFault("invalid_source_data")
            return value
        else:
            if kind[0] < 97 and kind != b"PLTE":
                raise ConnectionFault("invalid_source_data")
            if kind == b"PLTE" and (seen_idat or not 0 < length <= 768 or length % 3):
                raise ConnectionFault("invalid_source_data")
            ended_idat = seen_idat
    raise ConnectionFault("invalid_source_data")


def _image(value: dict[str, object], key: str, display_id: int) -> dict[str, object]:
    required = {
        "width",
        "height",
        "original_width",
        "original_height",
        "coordinate_space",
        "scale_x",
        "scale_y",
        "display_id",
        "protected_content",
        "captured_at_uptime_ms",
    }
    if not required.issubset(value) or value.get("coordinate_space") != "screenshot_pixels":
        raise ConnectionFault("invalid_source_data")
    data: dict[str, object] = {"coordinate_space": "screenshot_pixels"}
    data["display_id"] = _display(value["display_id"], display_id)
    width, height = _integer(value["width"], 1), _integer(value["height"], 1)
    if width * height > _MAX_OUTPUT_PIXELS:
        raise ConnectionFault("invalid_source_data")
    data.update(width=width, height=height)
    for field in ("original_width", "original_height"):
        item = value[field]
        data[field] = None if item is None else _integer(item, 1, _MAX_ORIGINAL_PIXELS)
    original_width, original_height = data["original_width"], data["original_height"]
    if (
        type(original_width) is int
        and type(original_height) is int
        and original_width * original_height > _MAX_ORIGINAL_PIXELS
    ):
        raise ConnectionFault("invalid_source_data")
    for field in ("scale_x", "scale_y"):
        item = value[field]
        if item is not None:
            if type(item) not in (int, float):
                raise ConnectionFault("invalid_source_data")
            scale = cast(float, item)
            if not 0 < scale <= 1 or not math.isfinite(scale):
                raise ConnectionFault("invalid_source_data")
        data[field] = item
    protected = value["protected_content"]
    if protected is True or protected == "protected":
        raise ConnectionFault("screen_protected")
    if protected is not None and protected is not False and protected != "not_verified":
        raise ConnectionFault("invalid_source_data")
    data["protected_content"] = "not_verified" if protected == "not_verified" else None
    captured = value["captured_at_uptime_ms"]
    data["captured_at_uptime_ms"] = None if captured is None else _integer(captured, high=_MAX_TIME)
    if key == "screenshot":
        if value.get("format") != "png":
            raise ConnectionFault("invalid_source_data")
        data.update(format="png", base64=_png(value.get("base64"), width, height))
    else:
        blocks = value.get("blocks")
        if type(blocks) is not list or len(blocks) > 256:
            raise ConnectionFault("invalid_source_data")
        safe: list[dict[str, object]] = []
        for block in blocks:
            if type(block) is not dict:
                raise ConnectionFault("invalid_source_data")
            safe.append(
                {
                    "text": _text(block.get("text"), nullable=False),
                    "bounds": None if block.get("bounds") is None else _bounds(block["bounds"]),
                    "confidence": None,
                }
            )
        data["blocks"] = safe
    return data


def _source(
    key: str,
    raw: object,
    requested: bool,
    wire_requested: bool,
    capability: object,
    display_id: int,
) -> dict[str, object]:
    source = _empty_source(key, wire_requested, capability, display_id, "invalid_source_data")
    try:
        if (
            type(raw) is not dict
            or raw.get("source") != _NAMES[key]
            or raw.get("requested") is not wire_requested
            or type(raw.get("status")) is not str
            or raw["status"] not in _STATUSES
        ):
            raise ConnectionFault("invalid_source_data")
        status = raw["status"]
        if not wire_requested and (
            status != "unavailable"
            or type(raw.get("error")) is not dict
            or raw["error"].get("code") != "not_requested"
            or raw.get("data") is not None
        ):
            raise ConnectionFault("invalid_source_data")
        if status != "available":
            error = raw.get("error")
            if type(error) is not dict or "code" not in error:
                raise ConnectionFault("invalid_source_data")
            code = error["code"]
            if wire_requested and code == "not_requested":
                raise ConnectionFault("invalid_source_data")
            code = (
                code
                if type(code) is str
                and len(code) <= 64
                and (
                    code in _SOURCE_ERRORS[key] or (not wire_requested and code == "not_requested")
                )
                else f"source_{status}"
            )
            source.update(status=status, error={"code": code})
            reasons = [code, "non_atomic_snapshot"]
            if error.get("phase") != "observe" or type(error.get("retryable")) is not bool:
                reasons.append("invalid_source_data")
            # Bad extra data must not erase a genuine primary failure, nor escape as evidence.
            if raw.get("data") is not None:
                reasons.append("invalid_source_data")
            try:
                source["time"] = _time(raw, available=False)
                completeness, extra = _completeness(raw.get("completeness"), _SOURCE_REASONS[key])
                if completeness != "unavailable":
                    raise ConnectionFault("invalid_source_data")
                reasons.extend(extra)
            except ConnectionFault:
                reasons.append("invalid_source_data")
            source["completeness"] = {
                "status": "unavailable",
                "reasons": list(dict.fromkeys(reasons)),
            }
        else:
            if (
                not {
                    "source",
                    "status",
                    "requested",
                    "started_at_uptime_ms",
                    "finished_at_uptime_ms",
                    "completeness",
                    "data",
                    "error",
                }.issubset(raw)
                or raw.get("error") is not None
                or type(raw.get("data")) is not dict
            ):
                raise ConnectionFault("invalid_source_data")
            completeness, reasons = _completeness(raw.get("completeness"), _SOURCE_REASONS[key])
            if completeness == "unavailable":
                raise ConnectionFault("invalid_source_data")
            time = _time(raw, available=True)
            data = (
                _tree(raw["data"], display_id)
                if key == "tree"
                else _image(raw["data"], key, display_id)
            )
            source.update(status="available", error=None, data=data, time=time)
            coordinates = cast(dict[str, object], source["coordinates"])
            scope = cast(dict[str, object], source["scope"])
            coordinates["display_id"] = scope["display_id"] = data["display_id"]
            if key == "tree":
                scope["window_id"] = data["window_id"]
                reasons.extend(
                    (
                        "native_semantics_only_visual_coverage_unverified",
                        "native_display_geometry_unknown",
                    )
                )
                if any(
                    node["text_redacted"] is None
                    for node in cast(list[dict[str, object]], data["nodes"])
                ):
                    reasons.append("text_redaction_unverified")
            else:
                for field in (
                    "width",
                    "height",
                    "original_width",
                    "original_height",
                    "scale_x",
                    "scale_y",
                ):
                    coordinates[field] = data[field]
                scale_error = _scale_error(data)
                coordinates["to_original_bitmap"] = {
                    "status": "available" if scale_error is None else "unavailable",
                    "error": None if scale_error is None else {"code": scale_error},
                }
                if scale_error is not None:
                    reasons.append(scale_error)
                time.update(
                    captured_at_uptime_ms=data["captured_at_uptime_ms"],
                    capture_clock="platform_as_reported",
                    capture_clock_calibrated=False,
                )
                reasons.extend(
                    (
                        "protected_content_not_verified",
                        "target_app_ownership_unverified",
                        "capture_timestamp_clock_unverified",
                    )
                )
                if key == "ocr":
                    reasons.append("ocr_text_only_no_native_states")
            reasons.extend(("non_atomic_snapshot", "rotation_unverified", "clock_uncalibrated"))
            source["completeness"] = {"status": "partial", "reasons": list(dict.fromkeys(reasons))}
    except ConnectionFault as exc:
        code = _closed_code(exc.code)
        source = _empty_source(
            key,
            wire_requested,
            capability,
            display_id,
            code,
            "unavailable" if code in {"unsupported_display", "screen_protected"} else "failed",
        )
    if not requested:
        hidden = _empty_source(key, False, capability, display_id, "not_requested")
        if wire_requested:
            hidden["collection"] = {
                "status": source["status"],
                "error": source["error"],
                "time": source["time"],
                "purpose": "ocr_dependency",
            }
        return hidden
    return source


def normalize_observation(
    raw_result: dict[str, object],
    *,
    requested: dict[str, bool],
    capabilities: dict[str, object],
    display_id: int = 0,
) -> dict[str, object]:
    """Validate wire data without merging sources, guessing a page, or promoting completeness."""
    options, declared = _options(requested, display_id), _capabilities(capabilities)
    if (
        type(raw_result) is not dict
        or raw_result.get("scope") != "active_window"
        or raw_result.get("atomic_snapshot") is not False
        or type(raw_result.get("sources")) is not dict
    ):
        raise ConnectionFault("invalid_observation")
    final_capabilities = _capabilities(raw_result.get("capabilities"))
    try:
        start = _integer(raw_result.get("revision_start"), high=_MAX_TIME)
        end = _integer(raw_result.get("revision_end"), high=_MAX_TIME)
        _, reasons = _completeness(raw_result.get("completeness"), _WIRE_REASONS)
    except ConnectionFault:
        raise ConnectionFault("invalid_observation") from None
    raw_sources = cast(dict[str, object], raw_result["sources"])
    sources = {
        key: _source(
            key,
            raw_sources.get(key),
            options[key],
            options[key] or (key == "screenshot" and options["ocr"]),
            declared[_CAPABILITIES[key]],
            display_id,
        )
        for key in _NAMES
    }
    screen = cast(dict[str, object], sources["screenshot"].get("collection", sources["screenshot"]))
    screen_error = screen["error"]
    if (
        type(screen_error) is dict
        and screen_error.get("code") == "screen_protected"
        and sources["ocr"]["status"] == "available"
    ):
        # Unlike PNG encoding failure, a protected capture cannot supply this request's OCR bitmap.
        sources["ocr"] = _empty_source(
            "ocr", options["ocr"], declared["ocr"], display_id, "screen_protected", "unavailable"
        )
        completeness = cast(dict[str, object], sources["ocr"]["completeness"])
        cast(list[str], completeness["reasons"]).append("source_protection_conflict")
    ocr_error = sources["ocr"]["error"]
    if (
        options["ocr"]
        and type(ocr_error) is dict
        and ocr_error.get("code") == "screenshot_unavailable"
        and screen["status"] != "available"
        and type(screen_error) is dict
    ):
        ocr_error["cause_code"] = _closed_code(screen_error["code"])
    shared = ["non_atomic_snapshot"]
    if start != end:
        shared.append("page_changed_during_capture")
    visible = [
        source
        for source in sources.values()
        if source["requested"] and source["status"] == "available"
    ]
    times: set[tuple[object, object]] = set()
    for source in visible:
        time = cast(dict[str, object], source["time"])
        times.add((time["started_at_uptime_ms"], time["finished_at_uptime_ms"]))
    if len(times) > 1:
        shared.append("source_capture_times_differ")
    if any(options[key] for key in ("screenshot", "ocr")):
        shared.append("agent_scope_mismatch")
    screenshot_data, ocr_data = sources["screenshot"]["data"], sources["ocr"]["data"]
    if (
        type(screenshot_data) is dict
        and type(ocr_data) is dict
        and any(
            screenshot_data[field] != ocr_data[field]
            for field in (
                "width",
                "height",
                "original_width",
                "original_height",
                "scale_x",
                "scale_y",
                "display_id",
                "captured_at_uptime_ms",
            )
        )
    ):
        shared.append("source_geometry_conflict")
    for key, source in sources.items():
        completeness = cast(dict[str, object], source["completeness"])
        local = cast(list[str], completeness["reasons"])
        if options[key]:
            local.extend(shared)
            capability_key = _CAPABILITIES[key]
            if declared[capability_key] is None or final_capabilities[capability_key] is None:
                local.append("capability_declaration_unverified")
            elif declared[capability_key] != final_capabilities[capability_key]:
                local.append("capability_changed_during_capture")
            if (
                source["status"] == "available"
                and final_capabilities[capability_key] == "unavailable"
            ):
                local.append("capability_result_conflict")
            if source["status"] != "available":
                reasons.append(f"{_NAMES[key]}_{source['status']}")
            reasons.extend(local)
        completeness["reasons"] = list(dict.fromkeys(local))
    reasons.extend(shared)
    return {
        "sources": sources,
        "completeness": {
            "status": "partial" if visible else "unavailable",
            "reasons": list(dict.fromkeys(reasons)),
        },
        "revision_start": start,
        "revision_end": end,
        "atomic_snapshot": False,
        "agent_reported_scope": "active_window",
        "capabilities": final_capabilities,
        "capabilities_at_request": declared,
    }
