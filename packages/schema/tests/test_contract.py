from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from pulse_schema import models as m
from pulse_schema.__main__ import main
from pulse_schema.generate import schemas
from pulse_schema.validation import (
    ContractError,
    load_project,
    parse_json,
    preflight,
    validate_document,
)

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "fixtures/golden-cases/v1-draft"
CONTROL = "cases/control-flow.json"
OPERATORS = "cases/operators.json"
FLOW = "flows/fill-search.json"
PROFILE = "profiles/android-fixture.json"
DATA = "data/cart.json"
DELETE = object()
ARTIFACT = parse_json((ROOT / "packages/schema/json/document.schema.json").read_text("utf-8"))
JSON_VALIDATOR = Draft202012Validator(ARTIFACT)
DOCUMENTS = ["manifest.json", CONTROL, OPERATORS, "cases/ai.json", FLOW, PROFILE, DATA]


def read(directory: Path, relative: str) -> Any:
    return parse_json((directory / relative).read_text("utf-8"))


def save(directory: Path, relative: str, value: Any) -> None:
    (directory / relative).write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def edit(value: Any, path: str, replacement: Any) -> None:
    parts = path.split("/")
    parent = value
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key: int | str = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    if replacement is DELETE:
        del parent[key]
    else:
        parent[key] = copy.deepcopy(replacement)


def accepts_model(value: Any) -> bool:
    try:
        m.DOCUMENT_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(GOLDEN, destination)
    return destination


@pytest.mark.parametrize("relative", DOCUMENTS)
def test_golden_document_matches_pydantic_and_generated_json_schema(relative: str) -> None:
    document = read(GOLDEN, relative)
    assert accepts_model(document)
    JSON_VALIDATOR.validate(document)
    for backend in ("pydantic", "jsonschema"):
        validated = validate_document(document, backend=backend)
        JSON_VALIDATOR.validate(
            validated.model_dump(mode="json", by_alias=True, exclude_unset=True)
        )


def required_fields() -> Iterator[tuple[str, str]]:
    for relative in DOCUMENTS:
        document = validate_document(read(GOLDEN, relative))
        for name in document.model_json_schema()["required"]:
            yield relative, name


@pytest.mark.parametrize(("relative", "name"), list(required_fields()))
def test_missing_required_document_fields_rejected_by_both(relative: str, name: str) -> None:
    document = read(GOLDEN, relative)
    del document[name]
    assert not accepts_model(document)
    assert not JSON_VALIDATOR.is_valid(document)


STRUCTURAL_ERRORS = [
    (CONTROL, "$schema", "urn:pulse:schema:2:case"),
    (CONTROL, "$schema", "urn:pulse:schema:1:case"),
    (CONTROL, "goal", 42),
    (CONTROL, "unexpected", True),
    (CONTROL, "case_id", "control-flow\n"),
    (CONTROL, "steps/0/type", "shell"),
    (CONTROL, "steps/0/action/kind", "scroll_to"),
    (CONTROL, "steps/0/action/app_id", DELETE),
    (CONTROL, "steps/0/action/command", "arbitrary command"),
    (CONTROL, "steps/1/flow_ref", DELETE),
    (CONTROL, "steps/2/action/mode", "fallback"),
    (CONTROL, "steps/2/action/text", 3),
    (CONTROL, "steps/2/action/text", DELETE),
    (CONTROL, "steps/2/action/target", {}),
    (
        CONTROL,
        "steps/2/action/target",
        {
            "locator_ref": "search",
            "locator": {"candidates": [], "expect": {"unique": True, "visible": True}},
        },
    ),
    (CONTROL, "steps/3/then", []),
    (CONTROL, "steps/3/condition/operator", "equals"),
    (CONTROL, "steps/4/max_iterations", DELETE),
    (CONTROL, "steps/4/max_iterations", 0),
    (CONTROL, "steps/4/max_iterations", -1),
    (CONTROL, "steps/4/max_iterations", 1.5),
    (CONTROL, "steps/4/max_iterations", True),
    (CONTROL, "steps/4/max_iterations", "2"),
    (CONTROL, "steps/4/max_iterations", None),
    (CONTROL, "steps/4/max_iterations", m.MAX_SAFE_INTEGER + 1),
    (CONTROL, "steps/4/steps", []),
    (CONTROL, "steps/4/steps/0/attribute", "shell"),
    (CONTROL, "steps/4/steps/1/action/duration_ms", 0),
    (CONTROL, "steps/5/action/start/x", -1),
    (CONTROL, "steps/5/action/start/width", 0),
    (CONTROL, "steps/5/action/duration_ms", "300"),
    (CONTROL, "steps/6/action/key", DELETE),
    (CONTROL, "steps/6/action/key", 4),
    (CONTROL, "steps/7/checkpoint/required", "true"),
    (CONTROL, "steps/7/checkpoint/condition/expected", 1),
    (CONTROL, "checks/1/condition/expected", {"var_ref": "label"}),
    (CONTROL, "parameters/product/required", 1),
    (CONTROL, "parameters/product/type", "object"),
    (OPERATORS, "checks/1/condition/observation_window_ms", DELETE),
    (OPERATORS, "checks/1/condition/scope", DELETE),
    (OPERATORS, "checks/1/condition/poll_interval_ms", 0),
    (OPERATORS, "checks/11/condition/precision", DELETE),
    (OPERATORS, "checks/11/condition/precision", -1),
    (OPERATORS, "checks/11/condition/unit", ""),
    (OPERATORS, "checks/11/condition/comparison", "approximately"),
    (OPERATORS, "checks/11/condition/expected", "12.50"),
    ("cases/ai.json", "steps/0/goal", DELETE),
    ("cases/ai.json", "steps/0/timeout_ms", DELETE),
    ("cases/ai.json", "steps/0/checks", []),
    (PROFILE, "locators/page/candidates", []),
    (PROFILE, "locators/page/expect", DELETE),
    (PROFILE, "locators/page/candidates/0/by", "css"),
    (PROFILE, "locators/page/candidates/0/value", 3),
    (PROFILE, "serial", "a-personal-device"),
    (DATA, "values/quantity", [1]),
    (DATA, "values/quantity", 10**400),
    ("manifest.json", "cases/control-flow", "../private.json"),
    ("manifest.json", "cases/control-flow", "/private/case.json"),
    ("manifest.json", "cases/control-flow", "cases\\case.json"),
    ("manifest.json", "cases/control-flow", "cases/../case.json"),
    ("manifest.json", "cases/control-flow", "cases/control-flow.json\n"),
    (OPERATORS, "checks/13/condition/timeout_ms", DELETE),
    ("manifest.json", "entry_cases", []),
    ("manifest.json", "default_strategy", "automatic"),
]


@pytest.mark.parametrize(("relative", "path", "replacement"), STRUCTURAL_ERRORS)
def test_invalid_wire_inputs_have_identical_structural_verdicts(
    relative: str, path: str, replacement: Any
) -> None:
    document = read(GOLDEN, relative)
    edit(document, path, replacement)
    assert not accepts_model(document), path
    assert not JSON_VALIDATOR.is_valid(document), path


@pytest.mark.parametrize("value", [1, 1.0, 2, m.MAX_SAFE_INTEGER])
def test_json_schema_integral_numbers_match_pydantic_integer_semantics(value: int | float) -> None:
    document = read(GOLDEN, CONTROL)
    edit(document, "steps/4/max_iterations", value)
    assert accepts_model(document)
    assert JSON_VALIDATOR.is_valid(document)


@pytest.mark.parametrize(
    "raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}']
)
def test_json_transport_rejects_duplicate_keys_and_non_finite_numbers(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_json(raw)


SEMANTIC_ERRORS = [
    (CONTROL, "steps/1/flow_ref", "missing", "flow_reference"),
    (CONTROL, "steps/2/action/target/locator_ref", "missing", "locator_reference"),
    (CONTROL, "inputs/product/data_ref", "missing", "unresolved_reference"),
    (CONTROL, "inputs/product/key", "missing", "unresolved_reference"),
    (CONTROL, "profile_refs", ["missing"], "profile_reference"),
    (CONTROL, "steps/1/arguments", {}, "flow_argument_missing"),
    (CONTROL, "steps/1/arguments/query", 1, "flow_argument_type"),
    (CONTROL, "steps/1/arguments/unknown", "Coffee", "flow_argument"),
    (CONTROL, "steps/1/bind", {"missing": "label"}, "flow_return_reference"),
    (CONTROL, "steps/2/action/text", {"var_ref": "iteration_label"}, "unresolved_reference"),
    (CONTROL, "steps/2/action/text", {"var_ref": "query"}, "unresolved_reference"),
    (CONTROL, "steps/3/then/0/id", "launch", "duplicate_step"),
    (CONTROL, "checks/1/id", "selection-checked", "duplicate_checkpoint"),
    (CONTROL, "parameters/quantity/default", "one", "parameter_type"),
    (CONTROL, "parameters/quantity/default", 1e30, "parameter_type"),
    (CONTROL, "parameters/product/default", "Coffee", "parameter_default"),
    (CONTROL, "parameters/quantity/default", DELETE, "parameter_default"),
    (CONTROL, "parameters/quantity/default", None, "parameter_default"),
    (CONTROL, "parameters/quantity/default", -1, "expected_count"),
    (CONTROL, "inputs/unknown", "Coffee", "case_input"),
    (CONTROL, "inputs/product", {"param_ref": "product"}, "case_input_reference"),
    (CONTROL, "steps/4/steps/0/as", "label", "variable_redefinition"),
    (CONTROL, "steps/5/action/end/width", 1200, "swipe_screen"),
    (CONTROL, "steps/5/action/duration_ms", 30_001, "action_duration"),
    (FLOW, "returns/label/type", "integer", "flow_return_type"),
    (FLOW, "steps/0/action/text", {"var_ref": "label"}, "unresolved_reference"),
    (FLOW, "steps/0/action/text", {"param_ref": "product"}, "unresolved_reference"),
    (PROFILE, "locators/product/candidates/0/scope", "missing", "locator_scope"),
    (PROFILE, "locators/product/candidates/2/value", "missing", "image_reference"),
    (PROFILE, "locators/product/candidates/3/value/x", 1080, "point_bounds"),
    (PROFILE, "locators/page/candidates/0/scope", "product", "locator_cycle"),
    (
        PROFILE,
        "locators/product/candidates",
        [{"by": "point", "value": {"x": 1, "y": 1, "width": 10, "height": 10}}],
        "point_only",
    ),
    (OPERATORS, "checks/1/condition/observation_window_ms", 10_001, "observation_window"),
    (OPERATORS, "checks/1/condition/poll_interval_ms", 1001, "poll_interval"),
    (OPERATORS, "checks/6/condition/expected", "", "empty_criterion"),
    (OPERATORS, "checks/13/condition/expected", " ", "empty_criterion"),
    (
        OPERATORS,
        "checks/5/condition/expected",
        {"data_ref": "cart", "key": "account"},
        "mutable_expectation",
    ),
    ("manifest.json", "entry_cases", ["missing"], "entry_reference"),
    ("manifest.json", "entry_cases", ["operators", "operators"], "duplicate_entry"),
    ("manifest.json", "secret_refs", [], "unknown_secret"),
]


@pytest.mark.parametrize(("relative", "path", "replacement", "code"), SEMANTIC_ERRORS)
def test_cross_document_semantics_are_shared_by_both_entrypoints(
    project_dir: Path, relative: str, path: str, replacement: Any, code: str
) -> None:
    document = read(project_dir, relative)
    edit(document, path, replacement)
    assert accepts_model(document)
    assert JSON_VALIDATOR.is_valid(document)
    save(project_dir, relative, document)
    results = []
    for backend in ("pydantic", "jsonschema"):
        with pytest.raises(ContractError) as caught:
            load_project(project_dir, backend=backend)
        assert code in {issue.code for issue in caught.value.issues}
        results.append(caught.value.issues)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("check_index", "expected"),
    [(5, ""), (6, " ")],
    ids=("text-equals-empty", "contains-whitespace"),
)
def test_only_an_empty_contains_criterion_is_rejected(
    project_dir: Path, check_index: int, expected: str
) -> None:
    case = read(project_dir, OPERATORS)
    case["checks"][check_index]["condition"]["expected"] = expected
    save(project_dir, OPERATORS, case)
    for backend in ("pydantic", "jsonschema"):
        assert load_project(project_dir, backend=backend).cases["operators"]


def test_flow_cycle_is_rejected_even_if_unused(project_dir: Path) -> None:
    flow = read(project_dir, FLOW)
    flow["steps"].append(
        {"type": "flow", "id": "cycle", "flow_ref": "fill-search", "arguments": {"query": "Coffee"}}
    )
    save(project_dir, FLOW, flow)
    for backend in ("pydantic", "jsonschema"):
        with pytest.raises(ContractError, match="flow_cycle"):
            load_project(project_dir, backend=backend)


def test_indirect_flow_cycle_is_rejected(project_dir: Path) -> None:
    manifest = read(project_dir, "manifest.json")
    manifest["flows"]["other"] = "flows/other.json"
    save(project_dir, "manifest.json", manifest)
    flow = read(project_dir, FLOW)
    other = copy.deepcopy(flow)
    other["flow_id"] = "other"
    other["steps"].append(
        {
            "type": "flow",
            "id": "to-first",
            "flow_ref": "fill-search",
            "arguments": {"query": "Coffee"},
        }
    )
    flow["steps"].append(
        {"type": "flow", "id": "to-second", "flow_ref": "other", "arguments": {"query": "Coffee"}}
    )
    save(project_dir, FLOW, flow)
    save(project_dir, "flows/other.json", other)
    with pytest.raises(ContractError, match="flow_cycle"):
        load_project(project_dir)


def test_observed_values_cannot_be_laundered_through_flow_parameters_into_expectations(
    project_dir: Path,
) -> None:
    flow = read(project_dir, FLOW)
    flow["steps"].append(
        {
            "type": "check",
            "id": "fixed-expectation",
            "checkpoint": {
                "id": "expected-query",
                "required": True,
                "condition": {
                    "operator": "text_equals",
                    "target": {"locator_ref": "product"},
                    "source": "native",
                    "expected": {"param_ref": "query"},
                },
            },
        }
    )
    save(project_dir, FLOW, flow)
    case = read(project_dir, CONTROL)
    case["steps"].append(
        {
            "type": "flow",
            "id": "observed-as-expected",
            "flow_ref": "fill-search",
            "arguments": {"query": {"var_ref": "label"}},
        }
    )
    save(project_dir, CONTROL, case)
    for backend in ("pydantic", "jsonschema"):
        with pytest.raises(ContractError, match="mutable_expectation"):
            load_project(project_dir, backend=backend)


def test_branch_variables_do_not_escape_even_if_both_branches_bind_them(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    extract = {
        "type": "extract",
        "id": "then-label",
        "target": {"locator_ref": "product"},
        "source": "native",
        "attribute": "text",
        "as": "branch_label",
    }
    case["steps"][3]["then"] = [extract]
    case["steps"][3]["else"] = [{**extract, "id": "else-label"}]
    case["steps"].append(
        {
            "type": "action",
            "id": "leak",
            "action": {
                "kind": "input",
                "target": {"locator_ref": "search"},
                "text": {"var_ref": "branch_label"},
                "mode": "replace",
            },
        }
    )
    save(project_dir, CONTROL, case)
    with pytest.raises(ContractError, match="unresolved_reference"):
        load_project(project_dir)


def test_enclosing_flow_deadline_caps_observation_window(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["steps"][1]["timeout_ms"] = 500
    save(project_dir, CONTROL, case)
    flow = read(project_dir, FLOW)
    flow["steps"].append(
        {
            "type": "wait",
            "id": "absence",
            "condition": {
                "operator": "not_exists",
                "target": {"locator_ref": "banner"},
                "source": "native",
                "scope": {"locator_ref": "page"},
                "observation_window_ms": 1000,
            },
        }
    )
    save(project_dir, FLOW, flow)
    with pytest.raises(ContractError, match="observation_window"):
        load_project(project_dir)


def test_missing_required_run_input_is_preflight_block_not_schema_failure(
    project_dir: Path,
) -> None:
    case = read(project_dir, CONTROL)
    case["inputs"] = {}
    save(project_dir, CONTROL, case)
    project = load_project(project_dir)
    missing = preflight(project, "control-flow", "android-fixture")
    assert {issue.code for issue in missing.issues} == {"input_required"}
    ready = preflight(project, "control-flow", "android-fixture", inputs={"product": "Coffee"})
    assert ready.offline_ready
    assert not preflight(
        project, "control-flow", "android-fixture", inputs={"product": 1}
    ).offline_ready


@pytest.mark.parametrize(
    "value",
    [
        ["synthetic-secret-canary"],
        {"secret": "synthetic-secret-canary"},
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
    ids=("list", "object", "nan", "positive-infinity", "negative-infinity"),
)
def test_invalid_run_inputs_are_redacted_preflight_issues(project_dir: Path, value: Any) -> None:
    report = preflight(
        load_project(project_dir),
        "control-flow",
        "android-fixture",
        inputs={"product": value},
    )
    assert {issue.code for issue in report.issues} == {"input_invalid"}
    assert "synthetic-secret-canary" not in str(report)


def test_empty_contains_parameter_is_a_preflight_block(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["inputs"] = {}
    case["checks"][0]["condition"]["operator"] = "contains"
    save(project_dir, CONTROL, case)
    project = load_project(project_dir)

    report = preflight(project, "control-flow", "android-fixture", inputs={"product": ""})

    assert {issue.code for issue in report.issues} == {"empty_criterion"}


@pytest.mark.parametrize("case_id", ["ai", "operators"])
@pytest.mark.parametrize("strategy", ["rule_only", "ai_on_locator_failure", "ai_led"])
@pytest.mark.parametrize("model_available", [False, True])
def test_ai_preflight_never_skips_model_dependent_content(
    case_id: str, strategy: m.Strategy, model_available: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("offline validation attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    reports = [
        preflight(
            load_project(GOLDEN, backend=backend),
            case_id,
            "android-fixture",
            strategy=strategy,
            model_available=model_available,
        )
        for backend in ("pydantic", "jsonschema")
    ]
    assert reports[0] == reports[1]
    assert reports[0].ai_requirements
    assert reports[0].offline_ready == (strategy == "ai_led" and model_available)


def test_native_state_cannot_be_proven_by_ocr_is_preflight_block(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["steps"][7]["checkpoint"]["condition"]["source"] = "ocr"
    save(project_dir, CONTROL, case)
    project = load_project(project_dir)
    report = preflight(project, "control-flow", "android-fixture")
    assert {issue.code for issue in report.issues} == {"state_evidence"}


def test_forbidden_action_and_app_scope_are_preflight_blocks(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["steps"].append(
        {
            "type": "action",
            "id": "clear-app",
            "action": {"kind": "clear", "app_id": "com.unapproved.app"},
        }
    )
    save(project_dir, CONTROL, case)
    report = preflight(load_project(project_dir), "control-flow", "android-fixture")
    assert {issue.code for issue in report.issues} == {"forbidden_action", "app_not_allowed"}


def test_declared_dependencies_are_not_assumed_available(project_dir: Path) -> None:
    manifest = read(project_dir, "manifest.json")
    manifest["dependencies"] = [{"kind": "protocol", "ref": "tool", "version": "1"}]
    save(project_dir, "manifest.json", manifest)
    project = load_project(project_dir)
    report = preflight(project, "control-flow", "android-fixture")
    assert {issue.code for issue in report.issues} == {"dependency_unavailable"}
    assert preflight(
        project,
        "control-flow",
        "android-fixture",
        available_dependencies=frozenset({("protocol", "tool", "1")}),
    ).offline_ready


def test_missing_secret_is_a_block_without_secret_resolution(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["parameters"]["account"] = {"type": "string", "required": True}
    case["inputs"]["account"] = {"secret_ref": "qa_account"}
    save(project_dir, CONTROL, case)
    project = load_project(project_dir)
    report = preflight(project, "control-flow", "android-fixture")
    assert {issue.code for issue in report.issues} == {"secret_unavailable"}
    assert preflight(
        project, "control-flow", "android-fixture", available_secret_refs=frozenset({"qa_account"})
    ).offline_ready
    assert preflight(
        project, "control-flow", "android-fixture", inputs={"account": "public-fixture-value"}
    ).offline_ready


def test_default_model_serialization_roundtrips_without_inventing_null_defaults(
    project_dir: Path,
) -> None:
    for relative in DOCUMENTS:
        model = validate_document(read(project_dir, relative))
        save(project_dir, relative, model.model_dump(mode="json", by_alias=True))
    for backend in ("pydantic", "jsonschema"):
        project = load_project(project_dir, backend=backend)
        assert preflight(project, "control-flow", "android-fixture").offline_ready


def test_capability_gap_is_preflight_block(project_dir: Path) -> None:
    profile = read(project_dir, PROFILE)
    profile["capabilities"] = []
    save(project_dir, PROFILE, profile)
    report = preflight(load_project(project_dir), "control-flow", "android-fixture")
    assert {issue.code for issue in report.issues} == {"capability_unavailable"}


@pytest.mark.parametrize("select_conflicting_profile", [False, True])
def test_flow_locator_shadowing_depends_on_selected_profile(
    project_dir: Path, select_conflicting_profile: bool
) -> None:
    profile = read(project_dir, PROFILE)
    profile["profile_id"] = "other-profile"
    profile["locators"]["local-only"] = copy.deepcopy(profile["locators"]["search"])
    save(project_dir, "profiles/other-profile.json", profile)
    manifest = read(project_dir, "manifest.json")
    manifest["profiles"]["other-profile"] = "profiles/other-profile.json"
    save(project_dir, "manifest.json", manifest)
    flow = read(project_dir, FLOW)
    flow["locators"]["local-only"] = copy.deepcopy(profile["locators"]["search"])
    flow["steps"][0]["action"]["target"] = {"locator_ref": "local-only"}
    save(project_dir, FLOW, flow)
    if select_conflicting_profile:
        case = read(project_dir, CONTROL)
        case["profile_refs"].append("other-profile")
        save(project_dir, CONTROL, case)
    for backend in ("pydantic", "jsonschema"):
        if select_conflicting_profile:
            with pytest.raises(ContractError, match="locator_shadow"):
                load_project(project_dir, backend=backend)
        else:
            assert load_project(project_dir, backend=backend).cases["control-flow"]


def test_golden_business_failures_are_legal_cases_not_invalid_inputs() -> None:
    metadata = read(GOLDEN, "outcomes.json")
    project = load_project(GOLDEN)
    checks = {check.id: check for check in project.cases[metadata["case_ref"]].checks}
    assert len(checks) == 14
    for name in checks:
        outcomes = {
            item["expected_outcome"]
            for item in metadata["scenarios"]
            if item["checkpoint_ref"] == name
        }
        assert {"passed", "failed"} <= outcomes
    failed_quantity = next(
        item
        for item in metadata["scenarios"]
        if item["checkpoint_ref"] == "count_eq" and item["expected_outcome"] == "failed"
    )
    assert failed_quantity["observation"]["count"] == 2
    assert checks["count_eq"].condition.expected == 1
    unavailable = next(
        item
        for item in metadata["scenarios"]
        if item["checkpoint_ref"] == "not_exists" and item["expected_outcome"] == "blocked"
    )
    assert unavailable["observation"]["complete"] is False
    # No predicate is evaluated here: the interpreter/observation layer is out of scope.
    assert accepts_model(read(GOLDEN, OPERATORS))
    assert JSON_VALIDATOR.is_valid(read(GOLDEN, OPERATORS))


@pytest.mark.parametrize("backend", ["pydantic", "jsonschema"])
def test_loader_reports_missing_assets(project_dir: Path, backend: str) -> None:
    (project_dir / FLOW).unlink()
    with pytest.raises(ContractError, match="missing_asset"):
        load_project(project_dir, backend=backend)


def test_loader_rejects_duplicate_physical_assets(project_dir: Path) -> None:
    manifest = read(project_dir, "manifest.json")
    manifest["cases"]["alias"] = CONTROL
    save(project_dir, "manifest.json", manifest)
    with pytest.raises(ContractError, match="duplicate_asset"):
        load_project(project_dir)


def test_loader_rejects_manifest_identity_mismatch(project_dir: Path) -> None:
    case = read(project_dir, CONTROL)
    case["case_id"] = "another-case"
    save(project_dir, CONTROL, case)
    with pytest.raises(ContractError, match="asset_identity"):
        load_project(project_dir)


def test_loader_rejects_symlink_escape(project_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    location = project_dir / CONTROL
    location.unlink()
    try:
        location.symlink_to(outside)
    except OSError:
        pytest.skip("host does not permit unprivileged symlink creation")
    with pytest.raises(ContractError, match="path_escape"):
        load_project(project_dir)


@pytest.mark.parametrize("backend", ["pydantic", "jsonschema"])
def test_loader_errors_do_not_echo_input_values(project_dir: Path, backend: str) -> None:
    case = read(project_dir, CONTROL)
    case["unknown"] = "synthetic-secret-canary"
    save(project_dir, CONTROL, case)
    with pytest.raises(ContractError) as caught:
        load_project(project_dir, backend=backend)
    assert "synthetic-secret-canary" not in str(caught.value)
    assert "invalid_document" in str(caught.value)


def test_loader_ignores_unpublished_run_and_sync_directories(project_dir: Path) -> None:
    for name in ("runs", "sync"):
        (project_dir / name).mkdir()
        (project_dir / name / "private.json").write_text("not JSON", encoding="utf-8")
    assert load_project(project_dir).manifest.project_id == "offline-contract"


def test_all_generated_schemas_are_valid_and_match_single_source() -> None:
    for kind, schema in schemas().items():
        Draft202012Validator.check_schema(schema)
        assert read(ROOT / "packages/schema/json", f"{kind}.schema.json") == schema


def test_d07_time_defaults_and_explicit_loop_bound() -> None:
    budgets = m.Budgets()
    assert budgets.case_timeout_ms == 600_000
    assert budgets.model_request_timeout_ms == 45_000
    assert budgets.recovery_timeout_ms == 60_000
    assert budgets.recovery_model_calls == 2
    assert budgets.recovery_actions == 3
    case = validate_document(read(GOLDEN, CONTROL))
    assert case.steps[0].timeout_ms == 30_000
    assert case.steps[4].max_iterations == 2
    assert "max_iterations" in ARTIFACT["$defs"]["LoopStep"]["required"]


@pytest.mark.parametrize(
    "failure",
    ["missing-directory", "parse", "structure", "semantics"],
)
def test_cli_failure_json_is_non_executing(
    failure: str,
    project_dir: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = project_dir
    if failure == "missing-directory":
        target = tmp_path / "missing-project"
    elif failure == "parse":
        (project_dir / "manifest.json").write_text("{", encoding="utf-8")
    elif failure == "structure":
        case = read(project_dir, CONTROL)
        case["unexpected"] = "synthetic-secret-canary"
        save(project_dir, CONTROL, case)
    else:
        case = read(project_dir, CONTROL)
        case["steps"][1]["flow_ref"] = "missing-flow"
        save(project_dir, CONTROL, case)

    assert main([str(target)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    assert result["executed"] is False
    assert "synthetic-secret-canary" not in json.dumps(result)


def test_cli_distinguishes_valid_contract_and_preflight_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([str(GOLDEN), "--backend", "jsonschema"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True and result["executed"] is False
    assert result["contract_status"] == "draft"
    assert main([str(GOLDEN), "--case", "ai", "--profile", "android-fixture"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True and result["offline_ready"] is False
    assert result["executed"] is False
