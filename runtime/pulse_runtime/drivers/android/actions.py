"""P1.4 action draft: explicit inputs and ephemeral, server-owned source provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulse_runtime.drivers.android.agent_transport import ConnectionFault, canonical_uuid

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
PACKAGE_PATTERN = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
NATIVE_ACTIONS = frozenset({"tap", "swipe", "set_text", "node_click"})
ADB_ACTIONS = frozenset({"key", "launch", "stop", "clear"})
# Actions that name a node and derive geometry or semantics from it.
INTERACTIVE_KINDS = frozenset({"tap", "long", "swipe", "node_click", "set_text"})
KEY_CODES = frozenset({3, 4, 66, 67, 82, 187})
SOURCE_MAX_AGE = 30.0
# Measured from the moment the fresh tree is returned, not from when collection started.
# A real device tree takes 6-12s to collect, so a window shorter than that rejects every
# action by construction rather than by detecting a stale page.
DISPATCH_MAX_AGE = 15.0


class ActionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observation_id: str = Field(pattern=UUID_PATTERN)
    source: Literal["tree", "screenshot", "ocr"]
    coordinate_space: Literal["display_pixels", "screenshot_pixels"]
    display_id: int = Field(ge=0, le=255)
    expected_package: str = Field(min_length=3, max_length=255)
    node_path: list[int] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_target(self) -> ActionTarget:
        canonical_uuid(self.observation_id)
        if re.fullmatch(PACKAGE_PATTERN, self.expected_package) is None or any(
            type(index) is not int or not 0 <= index <= 511 for index in self.node_path
        ):
            raise ValueError("invalid_target")
        return self


class ActionBody(BaseModel):
    """Not a locator, case DSL, control epoch, or arbitrary command interface."""

    model_config = ConfigDict(extra="forbid", strict=True)

    serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    session_id: str = Field(pattern=UUID_PATTERN)
    request_id: str = Field(pattern=UUID_PATTERN)
    operation_id: str = Field(pattern=UUID_PATTERN)
    kind: Literal[
        "tap", "long", "swipe", "set_text", "node_click", "key", "launch", "stop", "clear"
    ]
    target: ActionTarget
    params: dict[str, object] = Field(repr=False)
    timeout_ms: int = Field(default=60_000, ge=1000, le=90_000)
    agent_timeout_ms: int = Field(default=5000, ge=100, le=10_000)
    source: Literal["auto", "manual", "recorder", "ai"] = "auto"
    control_epoch: int = Field(default=0, ge=0, le=2**31 - 1)
    run_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    step_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")

    @model_validator(mode="after")
    def validate_params(self) -> ActionBody:
        for identifier in (self.session_id, self.request_id, self.operation_id):
            canonical_uuid(identifier)
        if self.run_id is not None:
            canonical_uuid(self.run_id)
        required: set[str]
        optional: set[str] = set()
        if self.kind in {"tap", "long", "swipe"}:
            required = {"x", "y"} | ({"end_x", "end_y"} if self.kind == "swipe" else set())
            optional = {"duration_ms"}
        elif self.kind == "set_text":
            required = {"text"}
        elif self.kind == "node_click":
            required = set()
        elif self.kind == "key":
            required = {"code"}
        elif self.kind == "launch":
            required = {"package", "activity"}
        else:
            required = {"package"} | ({"confirmed"} if self.kind == "clear" else set())
        if not required.issubset(self.params) or set(self.params) - required - optional:
            raise ValueError("invalid_params")
        for key in ("x", "y", "end_x", "end_y"):
            if key in self.params and (
                type(self.params[key]) is not int or not 0 <= cast(int, self.params[key]) <= 8191
            ):
                raise ValueError("invalid_coordinates")
        if "duration_ms" in self.params:
            duration = self.params["duration_ms"]
            if (
                type(duration) is not int
                or not (500 if self.kind == "long" else 50) <= duration <= 1000
            ):
                raise ValueError("invalid_duration")
        if (
            self.kind in {"tap", "long", "swipe"}
            and self.agent_timeout_ms < cast(int, self.wire_params()["duration_ms"]) + 250
        ):
            raise ValueError("insufficient_gesture_budget")
        if self.kind == "set_text":
            text = self.params["text"]
            if type(text) is not str:
                raise ValueError("invalid_text")
            try:
                if len(text.encode("utf-16-le")) // 2 > 4096:
                    raise ValueError("invalid_text")
            except UnicodeError:
                raise ValueError("invalid_text") from None
        if self.kind == "key" and (
            type(self.params["code"]) is not int or self.params["code"] not in KEY_CODES
        ):
            raise ValueError("unsupported_key")
        if "package" in self.params and (
            type(self.params["package"]) is not str
            or len(self.params["package"]) > 255
            or re.fullmatch(PACKAGE_PATTERN, self.params["package"]) is None
        ):
            raise ValueError("invalid_package")
        if self.kind == "launch" and (
            type(self.params["activity"]) is not str
            or len(self.params["activity"]) > 255
            or re.fullmatch(
                r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                self.params["activity"],
            )
            is None
        ):
            raise ValueError("invalid_activity")
        if self.kind == "clear" and self.params["confirmed"] is not True:
            raise ValueError("clear_confirmation_required")
        return self

    @property
    def wire_kind(self) -> str:
        return "tap" if self.kind == "long" else self.kind

    def wire_params(self) -> dict[str, object]:
        params = dict(self.params)
        if self.kind in {"tap", "long", "swipe"}:
            params.setdefault(
                "duration_ms", 600 if self.kind == "long" else 300 if self.kind == "swipe" else 80
            )
        if self.kind in {"set_text", "node_click"}:
            params["node_path"] = list(self.target.node_path)
        return {"kind": self.wire_kind, "expected_package": self.target.expected_package, **params}


@dataclass(frozen=True)
class ObservationBinding:
    request_id: str
    session_id: str
    lifecycle_id: str
    captured_monotonic: float
    fingerprint: bytes | None = field(repr=False)
    sources: dict[str, dict[str, object]] = field(repr=False)
    nodes: dict[tuple[int, ...], dict[str, object]] = field(repr=False)
    geometry: dict[str, int] | None = field(repr=False)


def bind_observation(
    value: dict[str, object], captured_monotonic: float, geometry: dict[str, int] | None
) -> ObservationBinding:
    """Retain one bounded metadata projection/digest, never raw text, OCR, or image payloads."""
    sources = cast(dict[str, dict[str, object]], value["sources"])
    tree = sources["tree"]
    native = tree.get("data")
    reasons = cast(dict[str, object], tree["completeness"]).get("reasons", [])
    unsafe = {
        "node_limit",
        "depth_limit",
        "missing_child",
        "text_truncated",
        "text_redaction_unverified",
        "page_changed_during_capture",
        "capability_changed_during_capture",
        "capability_result_conflict",
        "capability_declaration_unverified",
    }
    fingerprint = None
    nodes: dict[tuple[int, ...], dict[str, object]] = {}
    if (
        tree["status"] == "available"
        and isinstance(native, dict)
        and value["revision_start"] == value["revision_end"]
        and isinstance(reasons, list)
        and not unsafe.intersection(reasons)
    ):
        # Revision advances on accessibility events; two identical trees can have different
        # revisions. Bind the tree content, never the event counter.
        fingerprint = hashlib.sha256(
            json.dumps(
                native,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).digest()
        for node in cast(list[dict[str, object]], native["nodes"]):
            nodes[tuple(cast(list[int], node["node_path"]))] = copy.deepcopy(
                {key: node[key] for key in ("package_name", "bounds", "states", "text_redacted")}
            )
    return ObservationBinding(
        cast(str, value["request_id"]),
        cast(str, value["session_id"]),
        cast(str, value["agent_lifecycle_id"]),
        captured_monotonic,
        fingerprint,
        {
            key: copy.deepcopy({field: source[field] for field in ("status", "coordinates")})
            for key, source in sources.items()
        },
        nodes,
        copy.deepcopy(geometry),
    )


def validate_source(body: ActionBody, binding: ObservationBinding) -> None:
    target = body.target
    source = binding.sources[target.source]
    coordinates = cast(dict[str, object], source["coordinates"])
    if target.display_id != 0 or coordinates["display_id"] != 0:
        raise ConnectionFault("unsupported_display")
    if source["status"] != "available":
        raise ConnectionFault("action_source_unavailable")
    if target.coordinate_space != coordinates["coordinate_space"]:
        raise ConnectionFault("coordinate_space_mismatch")
    if target.source != "tree":
        # P1.3 deliberately reports unknown rotation/protection/ownership. Do not invent a map.
        raise ConnectionFault("coordinate_mapping_unavailable")
    interactive = body.kind in INTERACTIVE_KINDS
    # Package-scoped launch/stop/clear name a package, not a node, so they never need a
    # content fingerprint. A live launcher keeps emitting accessibility events, which leaves
    # revision_start != revision_end and therefore no fingerprint, without making the action
    # itself unsafe: nothing in it is derived from node geometry.
    if binding.fingerprint is None and interactive:
        raise ConnectionFault("action_source_incomplete")
    if interactive:
        root, node = binding.nodes.get(()), binding.nodes.get(tuple(target.node_path))
        if root is None or node is None:
            raise ConnectionFault("action_target_unavailable")
        if any(item["package_name"] != target.expected_package for item in (root, node)):
            raise ConnectionFault("target_changed")
        states = cast(dict[str, object], node["states"])
        # Package-scoped key/launch/stop/clear do not claim node semantics.
        if node["text_redacted"] is not False:
            raise ConnectionFault("sensitive_target")
        if states["enabled"] is not True or states["visible"] is not True:
            raise ConnectionFault("action_target_unavailable")
        if (
            body.kind in {"node_click", "set_text"}
            and states["clickable" if body.kind == "node_click" else "editable"] is not True
        ):
            raise ConnectionFault("action_target_unavailable")
    if body.kind in {"stop", "clear"} and body.params["package"] != target.expected_package:
        raise ConnectionFault("target_changed")
    package = body.params.get("package", target.expected_package)
    if package == "com.pulse.agent" or (
        body.kind in {"stop", "clear"}
        and (package == "android" or cast(str, package).startswith("com.android."))
    ):
        raise ConnectionFault("protected_target")
    if body.kind not in {"tap", "long", "swipe"}:
        return
    # Gestures are interactive, so the bound node was required above; re-check for the type checker.
    node = binding.nodes.get(tuple(target.node_path))
    if node is None:
        raise ConnectionFault("action_target_unavailable")
    geometry = binding.geometry
    if geometry is None or geometry.get("rotation") not in {0, 90, 180, 270}:
        raise ConnectionFault("rotation_unverified")
    bounds = cast(dict[str, int], node["bounds"])
    points = [(cast(int, body.params["x"]), cast(int, body.params["y"]))]
    if body.kind == "swipe":
        points.append((cast(int, body.params["end_x"]), cast(int, body.params["end_y"])))
    for x, y in points:
        if not (
            0 <= x < geometry["width"]
            and 0 <= y < geometry["height"]
            and bounds["left"] <= x < bounds["right"]
            and bounds["top"] <= y < bounds["bottom"]
        ):
            raise ConnectionFault("action_coordinates_out_of_range")
    left, right = min(p[0] for p in points), max(p[0] for p in points)
    top, bottom = min(p[1] for p in points), max(p[1] for p in points)
    for item in binding.nodes.values():
        rect = cast(dict[str, int], item["bounds"])
        if item["text_redacted"] is not False and (
            left < rect["right"]
            and right >= rect["left"]
            and top < rect["bottom"]
            and bottom >= rect["top"]
        ):
            raise ConnectionFault("sensitive_target")
