"""P1.5 source-local locator resolution over one fresh Runtime observation.

This consumes the existing 1-draft locator model, not a second case schema. Resolution is a
read, not an action or a business assertion. Neither ranking nor coordinate fallback can
hide ambiguity, and bitmap matches never acquire native control states or display targets.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulse_runtime.drivers.android.actions import (
    PACKAGE_PATTERN,
    SOURCE_MAX_AGE,
    UUID_PATTERN,
    ActionTarget,
    ObservationBinding,
)
from pulse_runtime.drivers.android.agent_transport import canonical_uuid
from pulse_runtime.perception.image_match import ImageMatchError, decode_png, match_image
from pulse_runtime.perception.xpath import Predicate, parse_xpath
from pulse_schema import models as m

_NATIVE_FIELDS = {"res_id": "view_id", "text": "text", "desc": "content_description"}
_INCOMPLETE = {
    "tree": {
        "node_limit",
        "depth_limit",
        "missing_child",
        "text_truncated",
        "text_redaction_unverified",
    },
    "ocr": {"ocr_block_limit", "ocr_text_truncated"},
    # A positive exact bitmap match still exists; only absence needs protection evidence.
    "screenshot": set(),
}
_ABSENCE_UNVERIFIED: dict[str, set[str]] = {
    # Limited WebView/Flutter coverage means an empty query is not proof of absence.
    "tree": {"webview_semantics_limited", "flutter_or_surface_semantics_limited"},
    "ocr": set(),
    "screenshot": {"protected_content_not_verified"},
}
_UNSAFE = {
    "page_changed_during_capture",
    "unknown_completeness_reason",
    "capability_changed_during_capture",
    "capability_result_conflict",
    "capability_declaration_unverified",
    "source_geometry_conflict",
}
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
    "password",
    "long_clickable",
    "scrollable",
)


class ImageResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    png_base64: str = Field(min_length=1, max_length=1_398_104, repr=False)


class LocateBody(BaseModel):
    """A bounded read API envelope. Resource payloads are explicit, never filesystem paths."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    serial: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    session_id: str = Field(pattern=UUID_PATTERN)
    request_id: str = Field(pattern=UUID_PATTERN)
    expected_package: str = Field(min_length=3, max_length=255)
    target: m.Target = Field(repr=False)
    locators: dict[m.Identifier, m.Locator] = Field(default_factory=dict, repr=False)
    resources: dict[m.Identifier, ImageResource] = Field(default_factory=dict, repr=False)
    display_id: int = Field(default=0, ge=0, le=255)
    timeout_ms: int = Field(default=15_000, ge=1000, le=30_000)
    image_threshold: float = Field(default=0.999, ge=0.98, le=1.0)

    @model_validator(mode="after")
    def validate_locators(self) -> LocateBody:
        canonical_uuid(self.session_id)
        canonical_uuid(self.request_id)
        if re.fullmatch(PACKAGE_PATTERN, self.expected_package) is None:
            raise ValueError("invalid_package")
        if len(self.locators) > 64 or len(self.resources) > 8:
            raise ValueError("locator_limit")
        if isinstance(self.target, m.LocatorRef) and self.target.locator_ref not in self.locators:
            raise ValueError("locator_reference")
        definitions = list(self.locators.values())
        if isinstance(self.target, m.InlineLocator):
            definitions.append(self.target.locator)
        if sum(len(locator.candidates) for locator in definitions) > 256:
            raise ValueError("locator_limit")
        for locator in definitions:
            if not 1 <= len(locator.candidates) <= 16:
                raise ValueError("locator_limit")
            points = [i for i, item in enumerate(locator.candidates) if item.by == "point"]
            if points and (len(locator.candidates) == 1 or points != [len(locator.candidates) - 1]):
                raise ValueError("point_fallback_required")
            for candidate in locator.candidates:
                if candidate.scope is not None and candidate.scope not in self.locators:
                    raise ValueError("locator_scope")
                if isinstance(candidate, m.PointCandidate):
                    point = candidate.value
                    if not (point.x < point.width <= 8192 and point.y < point.height <= 8192):
                        raise ValueError("point_bounds")
                elif len(candidate.value) > 1024:
                    raise ValueError("locator_limit")
                elif candidate.by == "xpath":
                    parse_xpath(candidate.value)
        visiting: set[str] = set()
        depths: dict[str, int] = {}

        def visit(name: str) -> int:
            if name in visiting:
                raise ValueError("locator_cycle_or_depth")
            if name in depths:
                return depths[name]
            visiting.add(name)
            depth = 1 + max(
                (
                    visit(candidate.scope)
                    for candidate in self.locators[name].candidates
                    if candidate.scope is not None
                ),
                default=0,
            )
            if depth > 16:
                raise ValueError("locator_cycle_or_depth")
            visiting.remove(name)
            depths[name] = depth
            return depth

        for name in self.locators:
            visit(name)
        return self

    def requested_sources(self, *, scope: m.Target | None = None) -> dict[str, bool]:
        target = (
            self.locators[self.target.locator_ref]
            if isinstance(self.target, m.LocatorRef)
            else self.target.locator
        )
        pending = [target]
        if scope is not None:
            pending.append(
                self.locators[scope.locator_ref]
                if isinstance(scope, m.LocatorRef)
                else scope.locator
            )
        visited: set[str] = set()
        kinds: set[str] = set()
        while pending:
            for candidate in pending.pop().candidates:
                kinds.add(candidate.by)
                if candidate.scope is not None and candidate.scope not in visited:
                    visited.add(candidate.scope)
                    pending.append(self.locators[candidate.scope])
        return {"tree": True, "screenshot": "image" in kinds, "ocr": "ocr_text" in kinds}


@dataclass(frozen=True)
class Match:
    source: str
    bounds: dict[str, int] | None
    node_path: tuple[int, ...] | None = None
    block_index: int | None = None
    visible: bool | None = None
    states: dict[str, bool | None] = field(default_factory=lambda: dict.fromkeys(_STATES))
    score: float | None = None
    text: str | None = field(default=None, repr=False)

    def public(self, *, include_text: bool = False) -> dict[str, object]:
        return {
            "source": self.source,
            **({"text": self.text} if include_text else {}),
            "coordinate_space": "display_pixels" if self.source == "tree" else "screenshot_pixels",
            "display_id": 0,
            "node_path": list(self.node_path) if self.node_path is not None else None,
            "block_index": self.block_index,
            "bounds": copy.deepcopy(self.bounds),
            "visible": self.visible,
            "visibility_evidence": "native_state" if self.source == "tree" else "bitmap_bounds",
            "states": dict(self.states),
            "score": self.score,
        }


@dataclass
class Outcome:
    status: str
    code: str | None = None
    matches: list[Match] = field(default_factory=list)
    candidate_index: int | None = None
    candidate_by: str | None = None
    point: dict[str, int] | None = None
    complete: bool = True
    attempts: list[dict[str, object]] = field(default_factory=list)


class _Resolver:
    def __init__(
        self, body: LocateBody, observation: dict[str, object], binding: ObservationBinding | None
    ) -> None:
        self.body, self.observation, self.binding = body, observation, binding
        self.sources = cast(dict[str, dict[str, object]], observation.get("sources", {}))
        data = self.sources.get("tree", {}).get("data")
        self.nodes = (
            cast(list[dict[str, object]], data.get("nodes", [])) if isinstance(data, dict) else []
        )
        self.by_path = {tuple(cast(list[int], node["node_path"])): node for node in self.nodes}
        self.scopes: dict[str, Outcome] = {}
        self.images: dict[str, NDArray[np.uint8]] = {}
        self.root_scope: Match | None = None

    def source(self, key: str) -> tuple[dict[str, object] | None, str | None, bool, bool]:
        source = self.sources.get(key, {})
        data, coordinates, completeness = (
            source.get("data"),
            source.get("coordinates"),
            source.get("completeness"),
        )
        if source.get("status") != "available" or not isinstance(data, dict):
            return None, "source_unavailable", False, False
        if not isinstance(coordinates, dict) or coordinates.get("display_id") != 0:
            return None, "unsupported_display", False, False
        expected = "display_pixels" if key == "tree" else "screenshot_pixels"
        if coordinates.get("coordinate_space") != expected:
            return None, "coordinate_space_mismatch", False, False
        if not isinstance(completeness, dict) or completeness.get("status") not in {
            "partial",
            "complete",
        }:
            return None, "source_incomplete", False, False
        reasons = cast(list[str], completeness.get("reasons", []))
        if _UNSAFE.intersection(reasons):
            return None, "source_unverified", False, False
        return (
            data,
            None,
            not bool(_INCOMPLETE[key].intersection(reasons)),
            not bool(_ABSENCE_UNVERIFIED[key].intersection(reasons)),
        )

    def scope(self, name: str | None) -> tuple[Match | None, Outcome | None]:
        if name is None:
            return None, None
        if name not in self.scopes:
            self.scopes[name] = self.resolve(self.body.locators[name])
        outcome = self.scopes[name]
        if outcome.status == "ambiguous" or len(outcome.matches) > 1:
            return None, Outcome("ambiguous", "ambiguous_scope")
        if outcome.status != "resolved" or not outcome.complete or len(outcome.matches) != 1:
            return None, Outcome("indeterminate", "scope_unavailable")
        if outcome.candidate_by == "point":
            return None, Outcome("indeterminate", "coordinate_scope_unverified")
        return outcome.matches[0], None

    def native_match(self, node: dict[str, object]) -> Match:
        states = cast(dict[str, bool | None], node["states"])
        redacted = node.get("text_redacted")
        return Match(
            "tree",
            cast(dict[str, int], node["bounds"]),
            node_path=tuple(cast(list[int], node["node_path"])),
            visible=states.get("visible"),
            states={name: states.get(name) for name in _STATES},
            text=cast(str | None, node.get("text")) if redacted is False else None,
        )

    def attribute(self, node: dict[str, object], field_name: str) -> tuple[str | None, bool]:
        if field_name in {"text", "content_description"}:
            redacted = node.get("text_redacted")
            if redacted is not False:
                return None, redacted is None
        if field_name in _STATES:
            value = cast(dict[str, object], node["states"]).get(field_name)
            return ("true" if value else "false", False) if type(value) is bool else (None, True)
        if field_name == "index":
            path = cast(list[int], node["node_path"])
            return str(path[-1] if path else 0), False
        value = node.get(field_name)
        return cast(str | None, value), False

    def predicate(self, node: dict[str, object], terms: tuple[Predicate, ...]) -> bool | None:
        unknown = False
        for term in terms:
            actual, missing = self.attribute(node, term.attribute)
            if missing:
                unknown = True
                continue
            if actual is None or not (
                actual == term.value
                if term.operator == "equals"
                else term.value in actual
                if term.operator == "contains"
                else actual.startswith(term.value)
            ):
                return False
        return None if unknown else True

    def xpath(self, expression: str, scope: Match | None) -> tuple[list[Match], bool]:
        paths = list(self.by_path)
        contexts: list[tuple[int, ...] | None] = [scope.node_path if scope else None]
        unknown = False
        for index, step in enumerate(parse_xpath(expression)):
            selected: list[tuple[int, ...]] = []
            for context in contexts:
                groups: dict[tuple[int, ...] | None, list[tuple[int, ...]]] = {}
                for path in paths:
                    if step.axis in {"self", "root"}:
                        included = path == (context if context is not None else ())
                    elif step.axis == "child":
                        included = (
                            not path if context is None else bool(path) and path[:-1] == context
                        )
                    else:
                        included = context is None or (
                            path[: len(context)] == context and (path != context or index == 0)
                        )
                    if not included:
                        continue
                    node = self.by_path[path]
                    if step.name not in {"node", "*"} and node.get("class_name") != step.name:
                        unknown |= node.get("class_name") is None
                        continue
                    parent = path[:-1] if path else None
                    groups.setdefault(parent, []).append(path)
                for group in groups.values():
                    for predicate in step.predicates:
                        if isinstance(predicate, int):
                            group = group[predicate - 1 : predicate]
                        else:
                            filtered = []
                            for path in group:
                                result = self.predicate(self.by_path[path], predicate)
                                unknown |= result is None
                                if result is True:
                                    filtered.append(path)
                            group = filtered
                    selected.extend(group)
            contexts = list(dict.fromkeys(selected))
        return [
            self.native_match(self.by_path[path]) for path in contexts if path is not None
        ], unknown

    def visual_scope(self, source: str, scope: Match | None) -> str | None:
        if scope is None:
            return None
        if scope.source == "tree":
            return "coordinate_mapping_unavailable"
        if scope.bounds is None:
            return "scope_bounds_unavailable"
        if scope.source == source:
            return None
        first = self.sources[scope.source].get("data")
        second = self.sources[source].get("data")
        fields = (
            "width",
            "height",
            "original_width",
            "original_height",
            "scale_x",
            "scale_y",
            "display_id",
            "captured_at_uptime_ms",
        )
        if (
            not isinstance(first, dict)
            or not isinstance(second, dict)
            or any(
                first.get(name) is None or first.get(name) != second.get(name) for name in fields
            )
        ):
            return "bitmap_scope_unverified"
        return None

    @staticmethod
    def inside(bounds: dict[str, int] | None, region: dict[str, int] | None) -> bool:
        return (
            bounds is not None
            and region is not None
            and (
                region["left"] <= bounds["left"] < bounds["right"] <= region["right"]
                and region["top"] <= bounds["top"] < bounds["bottom"] <= region["bottom"]
            )
        )

    def query(
        self, candidate: m.LocatorCandidate | m.PointCandidate, scope: Match | None
    ) -> Outcome:
        key = (
            "ocr"
            if candidate.by == "ocr_text"
            else "screenshot"
            if candidate.by == "image"
            else "tree"
        )
        data, error, complete, absence_verified = self.source(key)
        if error is not None or data is None:
            return Outcome("indeterminate", error)
        root = self.root_scope
        if root is not None:
            if key == "tree" and root.source != "tree":
                return Outcome("indeterminate", "coordinate_mapping_unavailable")
            if key != "tree" and self.visual_scope(key, root) is not None:
                return Outcome("indeterminate", "coordinate_mapping_unavailable")
            if scope is None:
                scope = root
        if isinstance(candidate, m.PointCandidate):
            return self.point(candidate, scope, complete)
        matches: list[Match] = []
        unknown = False
        if key == "tree":
            if scope is not None and scope.source != "tree":
                return Outcome("indeterminate", "coordinate_mapping_unavailable")
            if candidate.by == "xpath":
                matches, unknown = self.xpath(candidate.value, scope)
            else:
                field_name = _NATIVE_FIELDS[candidate.by]
                for node in self.nodes:
                    path = tuple(cast(list[int], node["node_path"]))
                    if root and (
                        root.node_path is None or path[: len(root.node_path)] != root.node_path
                    ):
                        continue
                    if scope and (
                        scope.node_path is None or path[: len(scope.node_path)] != scope.node_path
                    ):
                        continue
                    actual, missing = self.attribute(node, field_name)
                    unknown |= missing
                    if actual == candidate.value:
                        matches.append(self.native_match(node))
        else:
            if error := self.visual_scope(key, scope):
                return Outcome("indeterminate", error)
            viewport = {
                "left": 0,
                "top": 0,
                "right": cast(int, data["width"]),
                "bottom": cast(int, data["height"]),
            }
            if key == "ocr":
                for index, block in enumerate(cast(list[dict[str, object]], data["blocks"])):
                    if block["text"] != candidate.value:
                        continue
                    bounds = cast(dict[str, int] | None, block["bounds"])
                    if scope and not self.inside(bounds, scope.bounds):
                        if bounds is None:
                            unknown = True
                        continue
                    if root and not self.inside(bounds, root.bounds):
                        continue
                    visible = True if self.inside(bounds, viewport) else None
                    matches.append(
                        Match(
                            "ocr",
                            bounds,
                            block_index=index,
                            visible=visible,
                            text=block["text"],
                        )
                    )
            else:
                resource = self.body.resources.get(candidate.value)
                if resource is None:
                    return Outcome("indeterminate", "image_resource_unavailable")
                try:
                    if key not in self.images:
                        self.images[key] = decode_png(cast(str, data["base64"]))
                    image = self.images[key]
                    template = decode_png(resource.png_base64, template=True)
                    region = scope.bounds if scope else viewport
                    if root is not None and region is not None and root.bounds is not None:
                        region = {
                            "left": max(region["left"], root.bounds["left"]),
                            "top": max(region["top"], root.bounds["top"]),
                            "right": min(region["right"], root.bounds["right"]),
                            "bottom": min(region["bottom"], root.bounds["bottom"]),
                        }
                    if region is None or not self.inside(region, viewport):
                        return Outcome("indeterminate", "scope_bounds_unavailable")
                    left, top = region["left"], region["top"]
                    found = match_image(
                        image[top : region["bottom"], left : region["right"]],
                        template,
                        self.body.image_threshold,
                    )
                    for bounds, score in found:
                        bounds = {
                            name: value + (left if name in {"left", "right"} else top)
                            for name, value in bounds.items()
                        }
                        matches.append(Match("screenshot", bounds, visible=True, score=score))
                except ImageMatchError as exc:
                    return Outcome("indeterminate", str(exc))
        if unknown:
            return Outcome("indeterminate", "attribute_unverified", matches, complete=False)
        if not matches and not absence_verified:
            return Outcome("indeterminate", "source_incomplete", complete=False)
        return Outcome(
            "resolved" if matches else "not_found",
            None if matches else "no_match",
            matches,
            complete=complete,
        )

    def point(self, candidate: m.PointCandidate, scope: Match | None, complete: bool) -> Outcome:
        if scope is None or scope.source != "tree" or scope.node_path is None:
            return Outcome("indeterminate", "point_scope_required")
        binding = self.binding
        if not complete or binding is None or binding.fingerprint is None:
            return Outcome("indeterminate", "coordinate_fallback_unverified")
        geometry = binding.geometry
        if geometry is None or geometry.get("rotation") not in {0, 90, 180, 270}:
            return Outcome("indeterminate", "display_geometry_unavailable")
        point = candidate.value
        if (point.width, point.height) != (geometry.get("width"), geometry.get("height")):
            return Outcome("indeterminate", "point_dimensions_mismatch")
        if scope.bounds is None or not (
            scope.bounds["left"] <= point.x < scope.bounds["right"]
            and scope.bounds["top"] <= point.y < scope.bounds["bottom"]
        ):
            return Outcome("indeterminate", "point_outside_scope")
        matches: list[Match] = []
        for node in self.nodes:
            rect = cast(dict[str, int], node["bounds"])
            if not (
                rect["left"] <= point.x < rect["right"] and rect["top"] <= point.y < rect["bottom"]
            ):
                continue
            if node.get("text_redacted") is not False:
                return Outcome("indeterminate", "sensitive_target")
            path = tuple(cast(list[int], node["node_path"]))
            if path[: len(scope.node_path)] != scope.node_path:
                continue
            states = cast(dict[str, object], node["states"])
            # Existence and visibility are the locator's question. Operability is separate:
            # a visible but disabled control still exists, and only action_target drops out.
            if states.get("visible") is None:
                return Outcome("indeterminate", "attribute_unverified")
            if states.get("visible") is True:
                matches.append(self.native_match(node))
        # Ancestors and their descendants form one geometric hit stack. Overlapping sibling
        # stacks remain separate targets; depth/order cannot resolve that ambiguity.
        matches = [
            item
            for item in matches
            if not any(
                other.node_path != item.node_path
                and other.node_path is not None
                and item.node_path is not None
                and other.node_path[: len(item.node_path)] == item.node_path
                for other in matches
            )
        ]
        return Outcome(
            "resolved" if matches else "not_found",
            None if matches else "no_match",
            matches,
            point={"x": point.x, "y": point.y},
        )

    def resolve(self, locator: m.Locator) -> Outcome:
        attempts: list[dict[str, object]] = []
        uncertain = False
        uncertain_code: str | None = None
        for index, candidate in enumerate(locator.candidates):
            scope, outcome = self.scope(candidate.scope)
            if outcome is None:
                outcome = (
                    Outcome("indeterminate", "coordinate_fallback_unverified")
                    if candidate.by == "point" and uncertain
                    else self.query(candidate, scope)
                )
            raw = outcome.matches
            if outcome.status in {"resolved", "indeterminate"} and raw:
                eligible = (
                    [item for item in raw if item.visible is True]
                    if locator.expect.visible
                    else raw
                )
                if locator.expect.unique and len(eligible) > 1:
                    outcome = Outcome("ambiguous", "ambiguous_target", eligible)
                elif outcome.status == "indeterminate":
                    outcome.matches = eligible
                elif locator.expect.visible and any(item.visible is None for item in raw):
                    outcome = Outcome(
                        "indeterminate", "visibility_unverified", eligible, complete=False
                    )
                elif not eligible and not outcome.complete:
                    outcome = Outcome("indeterminate", "source_incomplete", complete=False)
                elif not eligible:
                    outcome = Outcome("not_found", "target_not_visible")
                elif (
                    outcome.status == "resolved" and not outcome.complete and locator.expect.unique
                ):
                    outcome = Outcome(
                        "indeterminate", "source_incomplete", eligible, complete=False
                    )
                else:
                    outcome.matches = eligible
            elif outcome.status == "not_found" and not outcome.complete:
                outcome = Outcome("indeterminate", "source_incomplete", complete=False)
            outcome.candidate_index, outcome.candidate_by = index, candidate.by
            attempts.append(
                {
                    "index": index,
                    "by": candidate.by,
                    "status": outcome.status,
                    "match_count": len(outcome.matches),
                    "error": {"code": outcome.code} if outcome.code else None,
                }
            )
            if outcome.status in {"resolved", "ambiguous"} or outcome.code == "target_not_visible":
                if uncertain:
                    return Outcome(
                        "indeterminate", uncertain_code or "locator_unverified", attempts=attempts
                    )
                outcome.attempts = attempts
                return outcome
            if outcome.status == "indeterminate":
                uncertain = True
                uncertain_code = outcome.code or uncertain_code
        return Outcome(
            "indeterminate" if uncertain else "not_found",
            (uncertain_code or "locator_unverified") if uncertain else "no_match",
            attempts=attempts,
        )


def observation_metadata(observation: dict[str, object]) -> dict[str, object]:
    """A diagnostic projection without native text, OCR text, or bitmap payloads."""
    sources = cast(dict[str, dict[str, object]], observation.get("sources", {}))
    return {
        **{
            name: copy.deepcopy(observation.get(name))
            for name in (
                "status",
                "error",
                "framing",
                "agent_lifecycle_id",
                "runtime_time",
                "completeness",
                "revision_start",
                "revision_end",
                "action_context",
                "requested",
                "requested_display_id",
            )
        },
        "atomic_snapshot": False,
        "sources": {
            name: copy.deepcopy({key: value for key, value in source.items() if key != "data"})
            for name, source in sources.items()
        },
    }


def resolve_locator(
    body: LocateBody,
    observation: dict[str, object],
    *,
    binding: ObservationBinding | None = None,
    age_ms: float = 0.0,
    include_text: bool = False,
    scope_target: m.Target | None = None,
) -> dict[str, object]:
    """Resolve only a trusted normalized snapshot; the HTTP API never accepts a client snapshot."""
    resolver = _Resolver(body, observation, binding)
    framing = observation.get("framing", {})
    if any(
        observation.get(name) != getattr(body, name)
        for name in ("serial", "session_id", "request_id")
    ):
        outcome = Outcome("indeterminate", "observation_binding_mismatch")
    elif body.display_id != 0 or observation.get("requested_display_id") != 0:
        outcome = Outcome("indeterminate", "unsupported_display")
    elif not math.isfinite(age_ms) or not 0 <= age_ms <= SOURCE_MAX_AGE * 1000:
        outcome = Outcome("indeterminate", "observation_stale")
    elif (
        observation.get("status") != "completed"
        or not isinstance(framing, dict)
        or any(
            framing.get(name) is not True
            for name in ("validated", "source_data_validated", "terminal_received", "eof_received")
        )
    ):
        outcome = Outcome("indeterminate", "observation_unavailable")
    elif "page_changed_during_capture" in cast(
        list[str], cast(dict[str, object], observation.get("completeness", {})).get("reasons", [])
    ):
        outcome = Outcome("indeterminate", "page_changed")
    elif resolver.nodes and resolver.nodes[0].get("package_name") != body.expected_package:
        outcome = Outcome("indeterminate", "target_app_mismatch")
    elif binding is not None and (
        binding.request_id != body.request_id
        or binding.session_id != body.session_id
        or binding.lifecycle_id != observation.get("agent_lifecycle_id")
    ):
        outcome = Outcome("indeterminate", "observation_binding_mismatch")
    else:
        locator = (
            body.locators[body.target.locator_ref]
            if isinstance(body.target, m.LocatorRef)
            else body.target.locator
        )
        if scope_target is None:
            outcome = resolver.resolve(locator)
        else:
            scope_locator = (
                body.locators[scope_target.locator_ref]
                if isinstance(scope_target, m.LocatorRef)
                else scope_target.locator
            )
            scoped = resolver.resolve(scope_locator)
            if (
                scoped.status != "resolved"
                or not scoped.complete
                or len(scoped.matches) != 1
                or scoped.candidate_by == "point"
            ):
                outcome = Outcome("indeterminate", "scope_unavailable", complete=False)
            else:
                resolver.root_scope = scoped.matches[0]
                resolver.scopes.clear()
                outcome = resolver.resolve(locator)
    action_target: dict[str, object] | None = None
    target_error = "locator_unresolved"
    if outcome.status == "resolved":
        if len(outcome.matches) != 1:
            target_error = "single_target_required"
        else:
            match = outcome.matches[0]
            node = resolver.by_path.get(match.node_path) if match.node_path is not None else None
            if match.source != "tree":
                target_error = "coordinate_mapping_unavailable"
            elif binding is None or binding.fingerprint is None or not outcome.complete:
                target_error = "action_source_incomplete"
            elif node is None or node.get("package_name") != body.expected_package:
                target_error = "target_app_mismatch"
            elif node.get("text_redacted") is not False:
                target_error = "sensitive_target"
            elif match.visible is not True or match.states.get("enabled") is not True:
                target_error = "action_target_unavailable"
            else:
                action_target = ActionTarget(
                    observation_id=body.request_id,
                    source="tree",
                    coordinate_space="display_pixels",
                    display_id=0,
                    expected_package=body.expected_package,
                    node_path=list(match.node_path or ()),
                ).model_dump()
    return {
        "contract": "pulse-runtime-locator-draft",
        "serial": body.serial,
        "session_id": body.session_id,
        "request_id": body.request_id,
        "observation_id": observation.get("request_id"),
        "status": outcome.status,
        "error": {"code": outcome.code} if outcome.code else None,
        "candidate_index": outcome.candidate_index,
        "candidate_by": outcome.candidate_by,
        "match_count": len(outcome.matches),
        "matches": [item.public(include_text=include_text) for item in outcome.matches],
        "query_complete": outcome.complete if outcome.status == "resolved" else None,
        "attempts": outcome.attempts,
        "point": outcome.point if outcome.status == "resolved" else None,
        "action_target": action_target,
        "action_target_error": None if action_target else {"code": target_error},
        "business_success": None,
        "automatic_replay": False,
        "observation": observation_metadata(observation),
    }
