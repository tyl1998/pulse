"""P1.5 query regressions using normalized synthetic evidence only, never a phone."""

from __future__ import annotations

import base64
import copy
import json
import time
from dataclasses import replace
from uuid import uuid4

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from pulse_runtime.drivers.android.actions import ActionBody, bind_observation, validate_source
from pulse_runtime.perception.image_match import ImageMatchError, decode_png, match_image
from pulse_runtime.perception.locators import LocateBody, resolve_locator
from pulse_runtime.perception.observation import normalize_observation
from pulse_runtime.perception.xpath import parse_xpath
from runtime.tests.test_observation_sources import capabilities, node, raw_observation
from runtime.tests.test_observation_sources import no_device_operations as no_device_operations

PACKAGE = "com.pulse.target"
GEOMETRY = {"display_id": 0, "width": 40, "height": 20, "rotation": 0}
PRIVATE = "private-locator-value-never-log"


def rectangle(left=0, top=0, right=40, bottom=20):
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def native(path, name, text, rect, *, cls="android.widget.Button", visible=True):
    value = node(path)
    value.update(
        view_id=f"{PACKAGE}:id/{name}",
        text=text,
        content_description="Confirm" if name == "submit" else "Row",
        class_name=cls,
        bounds=rect,
    )
    value["states"].update(enabled=True, visible=visible)
    return value


def native_tree():
    return [
        native([], "page", "Page", rectangle(), cls="android.widget.FrameLayout"),
        native([0], "left", "Left", rectangle(0, 0, 20, 10), cls="android.widget.LinearLayout"),
        native([0, 0], "row", "Duplicate", rectangle(2, 4, 12, 8)),
        native([1], "right", "Right", rectangle(20, 0, 40, 10), cls="android.widget.LinearLayout"),
        native([1, 0], "row", "Duplicate", rectangle(22, 4, 32, 8)),
        native([2], "submit", "Submit", rectangle(2, 12, 12, 18)),
    ]


def locator(*candidates, unique=True, visible=True):
    return {"candidates": list(candidates), "expect": {"unique": unique, "visible": visible}}


def candidate(by="res_id", value=f"{PACKAGE}:id/submit", **extra):
    return {"by": by, "value": value, **extra}


def request(*candidates, unique=True, visible=True, locators=None, target=None, **extra):
    value = {
        "serial": "PULSE_A",
        "session_id": str(uuid4()),
        "request_id": str(uuid4()),
        "expected_package": PACKAGE,
        "target": target
        or {"locator": locator(*(candidates or (candidate(),)), unique=unique, visible=visible)},
        "locators": locators or {},
        **extra,
    }
    return LocateBody.model_validate(value)


def observation(body, *, nodes=None, modify=None, geometry=GEOMETRY):
    raw = raw_observation()
    raw["sources"]["tree"]["data"]["nodes"] = native_tree() if nodes is None else nodes
    if modify:
        modify(raw)
    value = normalize_observation(
        raw, requested={"tree": True, "screenshot": True, "ocr": True}, capabilities=capabilities()
    )
    value.update(
        serial=body.serial,
        session_id=body.session_id,
        request_id=body.request_id,
        agent_lifecycle_id=str(uuid4()),
        requested_display_id=0,
        status="completed",
        framing=dict.fromkeys(
            ("validated", "source_data_validated", "terminal_received", "eof_received"), True
        ),
    )
    return value, bind_observation(value, time.monotonic(), geometry)


def resolve(body, **options):
    value, binding = observation(body, **options)
    return resolve_locator(body, value, binding=binding)


def point(**extra):
    return candidate("point", {"x": 5, "y": 5, "width": 40, "height": 20}, scope="page", **extra)


def coordinate_request(first=None, **extra):
    return request(
        first or candidate("text", "Missing"),
        point(),
        locators={"page": locator(candidate("res_id", f"{PACKAGE}:id/page"))},
        **extra,
    )


@pytest.mark.parametrize(
    "item",
    [
        candidate(),
        candidate("text", "Submit"),
        candidate("desc", "Confirm"),
        candidate("xpath", "//android.widget.Button[@text='Submit']"),
    ],
)
def test_native_selectors_resolve_same_bound_node_without_business_success(item):
    body = request(item)
    value, binding = observation(body)
    result = resolve_locator(body, value, binding=binding)
    assert result["status"] == "resolved" and result["candidate_index"] == 0
    assert result["matches"][0]["node_path"] == [2]
    assert result["action_target"]["observation_id"] == body.request_id
    assert result["business_success"] is None and result["automatic_replay"] is False
    action = ActionBody(
        serial=body.serial,
        session_id=body.session_id,
        request_id=str(uuid4()),
        operation_id=str(uuid4()),
        kind="node_click",
        target=result["action_target"],
        params={},
    )
    validate_source(action, binding)


def test_candidate_order_is_preserved_and_does_not_apply_an_implicit_rank():
    result = resolve(request(candidate("text", "Submit"), candidate()))
    assert result["candidate_by"] == "text" and len(result["attempts"]) == 1
    fallback = resolve(request(candidate("text", "Missing"), candidate()))
    assert fallback["candidate_index"] == 1
    assert [item["status"] for item in fallback["attempts"]] == ["not_found", "resolved"]


@pytest.mark.parametrize(
    "item",
    [
        candidate("text", "Duplicate"),
        candidate("res_id", f"{PACKAGE}:id/row"),
        candidate("desc", "Row"),
        candidate("xpath", "//node[@text='Duplicate']"),
    ],
)
def test_p15_dod_duplicate_targets_are_ambiguous_and_cannot_fall_back(item):
    # The later unique ID and coordinate are intentional traps: neither can hide ambiguity.
    body = request(
        item,
        candidate(),
        point(),
        locators={"page": locator(candidate("res_id", f"{PACKAGE}:id/page"))},
    )
    result = resolve(body)
    assert result["status"] == "ambiguous" and result["error"]["code"] == "ambiguous_target"
    assert result["match_count"] >= 2 and len(result["attempts"]) == 1
    assert result["action_target"] is None and result["point"] is None


def test_nonunique_queries_return_the_collection_without_choosing_a_target():
    result = resolve(request(candidate("text", "Duplicate"), unique=False))
    assert result["status"] == "resolved" and result["match_count"] == 2
    assert [item["node_path"] for item in result["matches"]] == [[0, 0], [1, 0]]
    assert result["action_target"] is None
    assert result["action_target_error"]["code"] == "single_target_required"


@pytest.mark.parametrize(
    "visible,required,status",
    [
        (True, True, "resolved"),
        (False, True, "not_found"),
        (None, True, "indeterminate"),
        (False, False, "resolved"),
        (None, False, "resolved"),
    ],
)
def test_visibility_true_false_unknown_and_expect_false_are_distinct(visible, required, status):
    nodes = native_tree()
    nodes[-1]["states"]["visible"] = visible
    result = resolve(request(visible=required), nodes=nodes)
    assert result["status"] == status
    if visible is not True:
        assert result["action_target"] is None


def test_known_invisible_target_does_not_turn_into_a_coordinate_click():
    nodes = native_tree()
    nodes[-1]["states"]["visible"] = False
    result = resolve(coordinate_request(candidate()), nodes=nodes)
    assert result["status"] == "not_found" and result["error"]["code"] == "target_not_visible"
    assert len(result["attempts"]) == 1 and result["point"] is None


def test_visibility_filters_before_uniqueness_but_unknown_cannot_prove_unique():
    nodes = native_tree()
    nodes[4]["states"]["visible"] = False
    assert resolve(request(candidate("text", "Duplicate")), nodes=nodes)["status"] == "resolved"
    nodes[4]["states"]["visible"] = None
    assert (
        resolve(request(candidate("text", "Duplicate")), nodes=nodes)["status"] == "indeterminate"
    )


@pytest.mark.parametrize("enabled", [False, None])
def test_presence_does_not_invent_operability(enabled):
    nodes = native_tree()
    nodes[-1]["states"]["enabled"] = enabled
    result = resolve(request(), nodes=nodes)
    assert result["status"] == "resolved" and result["action_target"] is None
    assert result["matches"][0]["states"]["enabled"] is enabled


@pytest.mark.parametrize(
    "by,value",
    [
        ("text", "Duplicate"),
        ("res_id", f"{PACKAGE}:id/row"),
        ("xpath", "//android.widget.Button[@text='Duplicate']"),
    ],
)
def test_native_scope_is_a_real_subtree_including_the_anchor(by, value):
    body = request(
        candidate(by, value, scope="left"),
        locators={"left": locator(candidate("res_id", f"{PACKAGE}:id/left"))},
    )
    result = resolve(body)
    assert result["status"] == "resolved" and result["matches"][0]["node_path"] == [0, 0]
    anchored = request(candidate("text", "Left", scope="left"), locators=body.locators)
    assert resolve(anchored)["matches"][0]["node_path"] == [0]


@pytest.mark.parametrize("unique", [True, False])
def test_ambiguous_scope_cannot_be_silently_unioned_or_ignored(unique):
    body = request(
        candidate("text", "Submit", scope="rows"),
        candidate(),
        locators={"rows": locator(candidate("text", "Duplicate"), unique=unique)},
    )
    result = resolve(body)
    assert result["status"] == "ambiguous" and result["error"]["code"] == "ambiguous_scope"
    assert result["action_target"] is None and len(result["attempts"]) == 1


def test_missing_scope_is_indeterminate_not_a_global_negative_query():
    result = resolve(
        request(candidate(scope="scope"), locators={"scope": locator(candidate("text", "Missing"))})
    )
    assert result["status"] == "indeterminate"
    assert result["attempts"][0]["error"]["code"] == "scope_unavailable"


def test_point_fallback_binds_the_deepest_hit_in_one_native_stack():
    body = coordinate_request()
    value, binding = observation(body)
    result = resolve_locator(body, value, binding=binding)
    assert result["status"] == "resolved" and result["candidate_by"] == "point"
    assert result["matches"][0]["node_path"] == [0, 0]
    assert result["point"] == {"x": 5, "y": 5}
    action = ActionBody(
        serial=body.serial,
        session_id=body.session_id,
        request_id=str(uuid4()),
        operation_id=str(uuid4()),
        kind="tap",
        target=result["action_target"],
        params=result["point"],
    )
    validate_source(action, binding)


def test_point_fallback_does_not_rank_overlapping_siblings_by_depth_or_order():
    nodes = native_tree()
    nodes.insert(3, native([0, 1], "overlap", "Other", rectangle(2, 4, 12, 8)))
    result = resolve(coordinate_request(), nodes=nodes)
    assert result["status"] == "ambiguous" and result["match_count"] == 2
    assert result["action_target"] is None and result["point"] is None


@pytest.mark.parametrize(
    "case,code",
    [
        ("size", "point_dimensions_mismatch"),
        ("geometry", "display_geometry_unavailable"),
        ("rotation", "display_geometry_unavailable"),
        ("scope", "point_outside_scope"),
        ("unscoped", "point_scope_required"),
        ("visibility_unknown", "attribute_unverified"),
        ("sensitive", "sensitive_target"),
    ],
)
def test_coordinate_inapplicability_is_explicit(case, code):
    body = coordinate_request()
    nodes, geometry = native_tree(), dict(GEOMETRY)
    payload = body.model_dump()
    if case == "size":
        payload["target"]["locator"]["candidates"][-1]["value"]["width"] = 41
    elif case == "geometry":
        geometry = None
    elif case == "rotation":
        geometry["rotation"] = -1
    elif case == "scope":
        payload["locators"]["page"] = locator(candidate("res_id", f"{PACKAGE}:id/right"))
    elif case == "unscoped":
        payload["target"]["locator"]["candidates"][-1]["scope"] = None
    elif case == "visibility_unknown":
        nodes[2]["states"]["visible"] = None
    else:
        nodes[2]["text_redacted"] = True
    body = LocateBody.model_validate(payload)
    result = resolve(body, nodes=nodes, geometry=geometry)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["attempts"][-1]["error"]["code"] == code


def test_point_resolves_a_visible_disabled_control_without_an_action_target():
    """Existence and visibility are locator facts; operability belongs to action_target."""
    body = coordinate_request()
    nodes = native_tree()
    nodes[2]["states"].update(visible=True, enabled=False)
    result = resolve(body, nodes=nodes)
    assert result["status"] == "resolved" and result["candidate_by"] == "point"
    assert result["matches"][0]["node_path"] == [0, 0]
    assert result["matches"][0]["states"]["enabled"] is False
    assert result["action_target"] is None
    assert result["action_target_error"]["code"] == "action_target_unavailable"


def test_unverified_earlier_candidate_blocks_coordinate_fallback():
    def unavailable(raw):
        raw["sources"]["ocr"].update(
            status="unavailable",
            data=None,
            error={"code": "ocr_unavailable", "phase": "observe", "retryable": False},
        )

    result = resolve(coordinate_request(candidate("ocr_text", "Missing")), modify=unavailable)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["attempts"][-1]["error"]["code"] == "coordinate_fallback_unverified"


@pytest.mark.parametrize("reason", ["node_limit", "depth_limit", "missing_child", "text_truncated"])
def test_incomplete_tree_cannot_prove_absence_or_uniqueness(reason):
    def incomplete(raw):
        raw["sources"]["tree"]["completeness"]["reasons"].append(reason)

    assert resolve(request(), modify=incomplete)["status"] == "indeterminate"
    assert (
        resolve(request(candidate("text", "Missing")), modify=incomplete)["status"]
        == "indeterminate"
    )
    assert (
        resolve(request(candidate("text", "Duplicate")), modify=incomplete)["status"] == "ambiguous"
    )
    collection = resolve(request(candidate("text", "Duplicate"), unique=False), modify=incomplete)
    assert collection["status"] == "resolved" and collection["query_complete"] is False
    assert collection["action_target"] is None


@pytest.mark.parametrize("source", ["tree", "ocr", "screenshot"])
@pytest.mark.parametrize("status", ["failed", "unavailable"])
def test_source_failure_is_not_an_empty_successful_query(source, status):
    item = (
        candidate()
        if source == "tree"
        else candidate("ocr_text", "Submit")
        if source == "ocr"
        else candidate("image", "marker")
    )

    def fail(raw):
        raw["sources"][source].update(
            status=status,
            data=None,
            error={"code": "platform_failed", "phase": "observe", "retryable": False},
        )

    result = resolve(request(item), modify=fail)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["observation"]["sources"][source]["status"] == status


def test_valid_empty_tree_is_not_found_only_inside_the_exposed_source_scope():
    result = resolve(request(), nodes=[])
    assert result["status"] == "not_found" and result["error"]["code"] == "no_match"
    assert result["observation"]["sources"]["tree"]["scope"]["kind"] == "active_window"
    assert result["observation"]["completeness"]["status"] == "partial"


@pytest.mark.parametrize(
    "case", ["serial", "session_id", "request_id", "lifecycle", "display", "framing", "page", "age"]
)
def test_snapshot_identity_validity_and_page_change_are_not_bypassed(case):
    body = request()
    value, binding = observation(body)
    age = 0
    if case in {"serial", "session_id", "request_id"}:
        value[case] = "OTHER" if case == "serial" else str(uuid4())
    elif case == "lifecycle":
        binding = replace(binding, lifecycle_id=str(uuid4()))
    elif case == "display":
        value["requested_display_id"] = 1
    elif case == "framing":
        value["framing"]["eof_received"] = False
    elif case == "page":
        value["completeness"]["reasons"].append("page_changed_during_capture")
    else:
        age = 30_001
    result = resolve_locator(body, value, binding=binding, age_ms=age)
    assert result["status"] == "indeterminate" and result["action_target"] is None


@pytest.mark.parametrize("age", [-1, float("nan"), float("inf")])
def test_invalid_age_never_makes_an_observation_fresh(age):
    body = request()
    value, binding = observation(body)
    assert (
        resolve_locator(body, value, binding=binding, age_ms=age)["error"]["code"]
        == "observation_stale"
    )


@pytest.mark.parametrize(
    "expression",
    [
        "/hierarchy/node/node[@resource-id='com.pulse.target:id/left']/node",
        "//android.widget.Button[@text='Duplicate'][1]",
        "//node[@text='Duplicate' and @class='android.widget.Button']",
        "//node[contains(@text, 'plic')][starts-with(@text, 'Dup')]",
    ],
)
def test_xpath_paths_predicates_and_positions_preserve_scope(expression):
    body = request(
        candidate("xpath", expression, scope="left")
        if expression.startswith("//")
        else candidate("xpath", expression),
        locators={"left": locator(candidate("res_id", f"{PACKAGE}:id/left"))},
    )
    result = resolve(body)
    assert result["status"] == "resolved" and result["matches"][0]["node_path"] == [0, 0]


def test_xpath_position_is_per_parent_not_a_global_first_match():
    result = resolve(request(candidate("xpath", "//android.widget.Button[1]")))
    assert result["status"] == "ambiguous" and result["match_count"] == 3


def test_xpath_absolute_path_cannot_escape_a_native_scope():
    body = request(
        candidate("xpath", "/hierarchy/node/node[@text='Duplicate']", scope="left"),
        locators={"left": locator(candidate("res_id", f"{PACKAGE}:id/left"))},
    )
    assert resolve(body)["matches"][0]["node_path"] == [0, 0]


def test_xpath_literal_slashes_brackets_and_conjunction_words_are_not_code():
    nodes = native_tree()
    nodes[-1]["text"] = "a/b] and c"
    body = request(candidate("xpath", "//node[@text='a/b] and c']"))
    assert resolve(body, nodes=nodes)["matches"][0]["node_path"] == [2]


def test_xpath_unknown_state_cannot_become_false_or_disambiguate_position():
    nodes = native_tree()
    nodes[2]["states"]["enabled"] = None
    nodes.insert(3, native([0, 1], "other", "Other", rectangle(2, 0, 12, 3)))
    body = request(
        candidate("xpath", "//android.widget.Button[@enabled='true'][1]", scope="left"),
        locators={"left": locator(candidate("res_id", f"{PACKAGE}:id/left"))},
    )
    assert resolve(body, nodes=nodes)["status"] == "indeterminate"
    false = request(
        candidate("xpath", "//android.widget.Button[@enabled='false']", scope="left"),
        locators=body.locators,
    )
    assert resolve(false, nodes=nodes)["status"] == "indeterminate"


@pytest.mark.parametrize(
    "expression",
    [
        "..",
        "//node/..",
        "//node[0]",
        "//node[513]",
        "//node[@bogus='x']",
        "//node[@text=concat('a','b')]",
        "//node[@text='a' or @text='b']",
        "//node[",
        "//node[@text='a'",
        "//node[//node]",
        "//node | //node",
        "//node/",
        "//node[1.0]",
        "ancestor::node",
        "",
        "x" * 1025,
    ],
)
def test_unsupported_xpath_is_rejected_not_interpreted_as_absence(expression):
    with pytest.raises(ValueError):
        parse_xpath(expression)


def test_ocr_has_visual_bounds_but_no_native_states_or_action_target():
    result = resolve(request(candidate("ocr_text", "Submit")))
    assert result["status"] == "resolved" and result["matches"][0]["visible"] is True
    assert all(value is None for value in result["matches"][0]["states"].values())
    assert result["matches"][0]["coordinate_space"] == "screenshot_pixels"
    assert result["action_target"] is None
    assert result["action_target_error"]["code"] == "coordinate_mapping_unavailable"


@pytest.mark.parametrize("required", [True, False])
def test_ocr_without_bounds_cannot_prove_visibility(required):
    def missing(raw):
        raw["sources"]["ocr"]["data"]["blocks"][0]["bounds"] = None

    result = resolve(request(candidate("ocr_text", "Submit"), visible=required), modify=missing)
    assert result["status"] == ("indeterminate" if required else "resolved")
    assert result["action_target"] is None


def test_ocr_scope_uses_the_same_bitmap_region_not_native_scaling():
    def regions(raw):
        raw["sources"]["ocr"]["data"]["blocks"] = [
            {"text": "Region", "bounds": rectangle(0, 0, 10, 10)},
            {"text": "Submit", "bounds": rectangle(2, 2, 4, 4)},
            {"text": "Submit", "bounds": rectangle(12, 2, 14, 4)},
        ]

    body = request(
        candidate("ocr_text", "Submit", scope="region"),
        locators={"region": locator(candidate("ocr_text", "Region"))},
    )
    result = resolve(body, modify=regions)
    assert result["status"] == "resolved" and result["matches"][0]["block_index"] == 1
    native_scope = request(
        candidate("ocr_text", "Submit", scope="page"),
        locators={"page": locator(candidate("res_id", f"{PACKAGE}:id/page"))},
    )
    result = resolve(native_scope, modify=regions)
    assert result["status"] == "indeterminate"
    assert result["attempts"][0]["error"]["code"] == "coordinate_mapping_unavailable"


def png(array):
    success, data = cv2.imencode(".png", array)
    assert success
    return base64.b64encode(data).decode("ascii")


def image_fixture(count=1):
    template = np.array(
        [
            [[0, 255, 0], [50, 0, 180], [220, 40, 0]],
            [[30, 80, 190], [255, 20, 0], [10, 90, 220]],
            [[0, 0, 255], [80, 160, 20], [100, 0, 100]],
        ],
        dtype=np.uint8,
    )
    image = np.full((10, 20, 3), 255, dtype=np.uint8)
    for x in (2, 14)[:count]:
        image[2:5, x : x + 3] = template
    return image, template


def image_observation(image):
    def change(raw):
        raw["sources"]["screenshot"]["data"]["base64"] = png(image)

    return change


@pytest.mark.parametrize("count,status", [(0, "indeterminate"), (1, "resolved"), (2, "ambiguous")])
def test_image_templates_match_at_fixed_bitmap_scale_and_preserve_ambiguity(count, status):
    image, template = image_fixture(count)
    body = request(
        candidate("image", "marker"), resources={"marker": {"png_base64": png(template)}}
    )
    result = resolve(body, modify=image_observation(image))
    assert result["status"] == status and result["match_count"] == count
    if count == 0:
        # A protected-content-unverified capture cannot prove a template is absent.
        assert result["error"]["code"] == "source_incomplete"
    assert result["action_target"] is None
    if count:
        assert result["matches"][0]["bounds"] == rectangle(2, 2, 5, 5)
        assert all(value is None for value in result["matches"][0]["states"].values())


def test_image_absence_is_decidable_only_when_protection_is_verified():
    image, template = image_fixture(0)
    body = request(
        candidate("image", "marker"), resources={"marker": {"png_base64": png(template)}}
    )

    value, binding = observation(body, modify=image_observation(image))
    reasons = value["sources"]["screenshot"]["completeness"]["reasons"]
    reasons[:] = [item for item in reasons if item != "protected_content_not_verified"]
    assert value["sources"]["screenshot"]["data"]["protected_content"] == "not_verified"
    result = resolve_locator(body, value, binding=binding)
    assert result["status"] == "not_found" and result["error"]["code"] == "no_match"


@pytest.mark.parametrize(
    "reason", ["webview_semantics_limited", "flutter_or_surface_semantics_limited"]
)
def test_limited_semantics_block_absence_but_not_a_found_target(reason):
    def limited(raw):
        raw["sources"]["tree"]["completeness"]["reasons"].append(reason)

    found = resolve(request(), modify=limited)
    assert found["status"] == "resolved" and found["action_target"] is not None
    absent = resolve(request(candidate("text", "Missing")), modify=limited)
    assert absent["status"] == "indeterminate" and absent["error"]["code"] == "source_incomplete"
    collection = resolve(request(candidate("text", "Duplicate"), unique=False), modify=limited)
    assert collection["status"] == "resolved" and collection["query_complete"] is True


def test_image_threshold_is_explicit_and_flat_templates_do_not_fake_uniqueness():
    image = np.zeros((5, 5, 3), dtype=np.uint8)
    template = np.zeros((1, 1, 3), dtype=np.uint8)
    assert len(match_image(image, template, 1.0)) == 25
    noisy = np.full((1, 1, 3), 8, dtype=np.uint8)
    assert match_image(noisy, template, 1.0) == []
    assert len(match_image(noisy, template, 0.98)) == 1
    with pytest.raises(ImageMatchError, match="image_match_limit"):
        match_image(np.zeros((20, 20, 3), dtype=np.uint8), template, 1.0)


@pytest.mark.parametrize(
    "resource", [None, PRIVATE, base64.b64encode(b"<svg>private</svg>").decode("ascii")]
)
def test_missing_or_invalid_image_resource_is_not_absence_and_never_leaks(resource):
    resources = {} if resource is None else {"marker": {"png_base64": resource}}
    result = resolve(request(candidate("image", "marker"), resources=resources))
    assert result["status"] == "indeterminate" and PRIVATE not in json.dumps(result)


def test_transparent_or_broken_pngs_are_explicitly_unsupported():
    transparent = np.zeros((2, 2, 4), dtype=np.uint8)
    with pytest.raises(ImageMatchError, match="image_transparency_unsupported"):
        decode_png(png(transparent), template=True)
    with pytest.raises(ImageMatchError, match="invalid_image"):
        decode_png(base64.b64encode(b"bad png").decode("ascii"), template=True)


def test_response_and_body_repr_do_not_retain_selector_text_or_source_payloads():
    body = request(candidate("text", PRIVATE))
    nodes = native_tree()
    nodes[-1]["text"] = PRIVATE
    result = resolve(body, nodes=nodes)
    assert result["status"] == "resolved"
    assert PRIVATE not in json.dumps(result) and PRIVATE not in repr(body)
    for source in result["observation"]["sources"].values():
        assert "data" not in source


def test_only_reachable_locators_request_additional_sources():
    body = request(
        locators={
            "unused": locator(candidate("ocr_text", "unused")),
            "also_unused": locator(candidate("image", "marker")),
        }
    )
    assert body.requested_sources() == {"tree": True, "screenshot": False, "ocr": False}
    used = request(
        candidate(scope="scope"), locators={"scope": locator(candidate("ocr_text", "Region"))}
    )
    assert used.requested_sources() == {"tree": True, "screenshot": False, "ocr": True}


@pytest.mark.parametrize(
    "case",
    [
        "unknown_ref",
        "unknown_scope",
        "cycle",
        "point_only",
        "point_first",
        "point_bounds",
        "boolean_point",
        "empty",
        "both_targets",
        "extra",
        "string_bool",
        "candidate_limit",
        "bad_xpath",
        "bad_threshold",
        "unbounded_template",
        "unsafe_package",
    ],
)
def test_runtime_locator_input_is_closed_and_bounded(case):
    value = request().model_dump()
    if case == "unknown_ref":
        value["target"] = {"locator_ref": "missing"}
    elif case == "unknown_scope":
        value["target"]["locator"]["candidates"][0]["scope"] = "missing"
    elif case == "cycle":
        value["locators"] = {"a": locator(candidate(scope="b")), "b": locator(candidate(scope="a"))}
    elif case == "point_only":
        value["target"]["locator"] = locator(
            candidate("point", {"x": 0, "y": 0, "width": 1, "height": 1})
        )
    elif case == "point_first":
        value["target"]["locator"] = locator(
            candidate("point", {"x": 0, "y": 0, "width": 1, "height": 1}), candidate()
        )
    elif case in {"point_bounds", "boolean_point"}:
        value["target"]["locator"] = locator(
            candidate(),
            candidate(
                "point",
                {"x": 1 if case == "point_bounds" else True, "y": 0, "width": 1, "height": 1},
            ),
        )
    elif case == "empty":
        value["target"]["locator"]["candidates"] = []
    elif case == "both_targets":
        value["target"]["locator_ref"] = "also"
    elif case == "extra":
        value["snapshot"] = PRIVATE
    elif case == "string_bool":
        value["target"]["locator"]["expect"]["unique"] = "false"
    elif case == "candidate_limit":
        value["target"]["locator"]["candidates"] = [candidate()] * 17
    elif case == "bad_xpath":
        value["target"]["locator"]["candidates"] = [candidate("xpath", "../node")]
    elif case == "bad_threshold":
        value["image_threshold"] = float("nan")
    elif case == "unbounded_template":
        value["resources"] = {"marker": {"png_base64": "x" * 1_398_105}}
    else:
        value["expected_package"] = "com.pulse.target;input tap"
    with pytest.raises(ValidationError):
        LocateBody.model_validate(value)


def test_scope_depth_limit_is_independent_of_dictionary_insertion_order():
    definitions = {}
    for index in range(17):
        definitions[f"s{index}"] = locator(
            candidate(**({"scope": f"s{index - 1}"} if index else {}))
        )
    with pytest.raises(ValidationError):
        request(locators=definitions)
    with pytest.raises(ValidationError):
        request(locators=dict(reversed(list(definitions.items()))))


def test_no_mutation_of_observation_or_locator_definitions():
    body = request()
    value, binding = observation(body)
    baseline, before = copy.deepcopy(value), body.model_dump()
    result = resolve_locator(body, value, binding=binding)
    result["matches"][0]["bounds"]["left"] = 999
    result["observation"]["sources"]["tree"]["coordinates"]["display_id"] = 99
    assert value == baseline and body.model_dump() == before


def test_unavailable_scope_cannot_fall_back_to_a_global_action_target():
    body = request(
        candidate(scope="missing"),
        candidate(),
        locators={"missing": locator(candidate("text", "Missing"))},
    )
    result = resolve(body)
    assert result["status"] == "indeterminate"
    assert result["error"]["code"] == "scope_unavailable"
    assert result["action_target"] is None


@pytest.mark.parametrize(
    "by,value",
    [
        ("text", "Duplicate"),
        ("xpath", "//node[@text='Duplicate']"),
    ],
)
def test_private_scope_limits_target_and_its_nested_scopes_in_one_observation(by, value):
    body = request(
        candidate(by, value),
        locators={"left": locator(candidate("res_id", f"{PACKAGE}:id/left"))},
    )
    nodes = native_tree()
    nodes[2]["text"] = "Other"
    observed, binding = observation(body, nodes=nodes)
    from pulse_schema import models as m

    result = resolve_locator(
        body, observed, binding=binding, scope_target=m.LocatorRef(locator_ref="left")
    )
    assert result["status"] == "not_found"
    assert result["matches"] == []
    body = request(
        candidate(by, value, scope="right"),
        locators={
            "left": locator(candidate("res_id", f"{PACKAGE}:id/left")),
            "right": locator(candidate("res_id", f"{PACKAGE}:id/right")),
        },
    )
    observed, binding = observation(body, nodes=nodes)
    result = resolve_locator(
        body, observed, binding=binding, scope_target=m.LocatorRef(locator_ref="left")
    )
    assert result["status"] == "indeterminate"
    assert result["action_target"] is None
