"""Offline structural and cross-document checks. Never observes or controls a device."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError
from pydantic import TypeAdapter, ValidationError

from pulse_schema import models as m
from pulse_schema.generate import schemas

type Backend = Literal["pydantic", "jsonschema"]
EXTRACT_TYPES: dict[str, m.ScalarType] = {
    "text": "string",
    "count": "integer",
    "enabled": "boolean",
    "checked": "boolean",
}
SCALAR_ADAPTER = TypeAdapter(m.Scalar)


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str


class ContractError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = list(dict.fromkeys(issues))
        super().__init__("; ".join(f"{i.path}: {i.code}: {i.message}" for i in self.issues))


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValueError("non-finite numbers are not JSON")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("number exceeds finite JSON representation")
    return parsed


def parse_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)


def validate_document(value: Any, *, backend: Backend = "pydantic") -> m.Document:
    if backend == "jsonschema":
        Draft202012Validator(schemas()["document"]).validate(value)
    elif backend != "pydantic":
        raise ValueError("unknown structural validator")
    return m.DOCUMENT_ADAPTER.validate_python(value)


@dataclass
class Project:
    manifest: m.Manifest
    cases: dict[str, m.Case]
    flows: dict[str, m.Flow]
    data: dict[str, m.DataSet]
    profiles: dict[str, m.Profile]


def load_project(directory: Path, *, backend: Backend = "pydantic") -> Project:
    root = directory.resolve(strict=True)
    seen: set[Path] = set()

    def asset(relative: str) -> Path:
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise ContractError([Issue("path_escape", relative, "asset leaves project directory")])
        if candidate in seen:
            raise ContractError([Issue("duplicate_asset", relative, "asset is declared twice")])
        if not candidate.is_file():
            raise ContractError([Issue("missing_asset", relative, "declared file does not exist")])
        seen.add(candidate)
        return candidate

    def document(relative: str) -> m.Document:
        try:
            return validate_document(
                parse_json(asset(relative).read_text("utf-8")), backend=backend
            )
        except ContractError:
            raise
        except (OSError, ValueError, JsonSchemaError) as exc:
            # ValidationError may include literal secret values. Diagnostics identify the file,
            # not the raw exception or input. Detailed model errors remain available to callers.
            raise ContractError(
                [Issue("invalid_document", relative, "invalid JSON or document structure")]
            ) from exc

    manifest = document("manifest.json")
    if not isinstance(manifest, m.Manifest):
        raise ContractError([Issue("document_kind", "manifest.json", "expected a manifest")])
    collections: dict[str, dict[str, Any]] = {}
    for kind, paths, model, id_field in (
        ("cases", manifest.cases, m.Case, "case_id"),
        ("flows", manifest.flows, m.Flow, "flow_id"),
        ("data", manifest.data, m.DataSet, "data_id"),
        ("profiles", manifest.profiles, m.Profile, "profile_id"),
    ):
        collection: dict[str, Any] = {}
        for name, relative in paths.items():
            item = document(relative)
            if not isinstance(item, model) or getattr(item, id_field) != name:
                raise ContractError(
                    [Issue("asset_identity", relative, "kind or ID differs from manifest entry")]
                )
            collection[name] = item
        collections[kind] = collection
    for relative in manifest.resources.values():
        asset(relative)
    project = Project(manifest, **collections)
    validate_semantics(project)
    return project


@dataclass(frozen=True)
class ValueInfo:
    type: m.ScalarType
    observed: bool = False
    secret: bool = False
    value: m.Scalar | None = None


def scalar_type(value: m.Scalar) -> m.ScalarType:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if (isinstance(value, int) or value.is_integer()) and abs(value) <= m.MAX_SAFE_INTEGER:
        return "integer"
    return "number"


def compatible(actual: m.ScalarType, expected: m.ScalarType) -> bool:
    return actual == expected or (expected == "number" and actual == "integer")


@dataclass
class _Validator:
    project: Project
    issues: list[Issue] = field(default_factory=list)
    ai_requirements: list[Issue] = field(default_factory=list)
    observation_requirements: list[Issue] = field(default_factory=list)
    policy_requirements: list[Issue] = field(default_factory=list)
    secrets: set[str] = field(default_factory=set)

    def error(self, code: str, path: str, message: str) -> None:
        self.issues.append(Issue(code, path, message))

    def value(
        self,
        value: m.Value,
        parameters: dict[str, ValueInfo],
        variables: dict[str, ValueInfo],
        path: str,
    ) -> ValueInfo | None:
        if isinstance(value, m.ParameterRef):
            result = parameters.get(value.param_ref)
        elif isinstance(value, m.VariableRef):
            result = variables.get(value.var_ref)
        elif isinstance(value, m.DataRef):
            data = self.project.data.get(value.data_ref)
            item = data.values.get(value.key) if data else None
            if item is not None:
                return self.value(item, {}, {}, path)
            result = None
        elif isinstance(value, m.SecretRef):
            if value.secret_ref not in self.project.manifest.secret_refs:
                self.error("unknown_secret", path, "secret reference is not declared")
            self.secrets.add(value.secret_ref)
            return ValueInfo("string", secret=True)
        else:
            return ValueInfo(scalar_type(value), value=value)
        if result is None:
            self.error(
                "unresolved_reference", path, "reference is missing or outside lexical scope"
            )
        return result

    def parameters(self, procedure: m.Procedure, path: str) -> dict[str, ValueInfo]:
        result = {}
        for name, parameter in procedure.parameters.items():
            has_default = "default" in parameter.model_fields_set
            value = parameter.default
            if parameter.required == has_default or (has_default and value is None):
                self.error(
                    "parameter_default",
                    f"{path}/parameters/{name}",
                    "only optional parameters have a default, which must be a scalar",
                )
            if value is not None and not compatible(scalar_type(value), parameter.type):
                self.error("parameter_type", f"{path}/parameters/{name}", "default type mismatch")
            result[name] = ValueInfo(parameter.type, value=value)
        return result

    def point(self, value: m.Point, path: str) -> None:
        if value.x >= value.width or value.y >= value.height:
            self.error("point_bounds", path, "point is outside its declared screen dimensions")

    def locator(self, locator: m.Locator, known: set[str], path: str) -> None:
        if all(isinstance(candidate, m.PointCandidate) for candidate in locator.candidates):
            self.error("point_only", path, "portable locators need a non-coordinate candidate")
        for index, candidate in enumerate(locator.candidates):
            where = f"{path}/candidates/{index}"
            if candidate.scope is not None and candidate.scope not in known:
                self.error("locator_scope", where, "scope locator does not exist")
            if isinstance(candidate, m.PointCandidate):
                self.point(candidate.value, where)
            elif candidate.by == "image" and candidate.value not in self.project.manifest.resources:
                self.error("image_reference", where, "image resource is not declared")

    def locators(
        self, locators: dict[str, m.Locator], path: str, known: set[str] | None = None
    ) -> None:
        known = set(locators) if known is None else known
        for name, locator in locators.items():
            self.locator(locator, known, f"{path}/locators/{name}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                self.error("locator_cycle", path, "locator scopes contain a cycle")
                return
            if name in visited or name not in locators:
                return
            visiting.add(name)
            for candidate in locators[name].candidates:
                if candidate.scope is not None:
                    visit(candidate.scope)
            visiting.remove(name)
            visited.add(name)

        for name in locators:
            visit(name)

    def target(self, target: m.Target, locators: dict[str, m.Locator], path: str) -> None:
        if isinstance(target, m.LocatorRef):
            if target.locator_ref not in locators:
                self.error("locator_reference", path, "locator is not defined in this context")
        else:
            self.locator(target.locator, set(locators), path)

    def condition(
        self,
        condition: m.Predicate,
        parameters: dict[str, ValueInfo],
        variables: dict[str, ValueInfo],
        locators: dict[str, m.Locator],
        timeout: int,
        path: str,
    ) -> None:
        self.target(condition.target, locators, f"{path}/target")
        if isinstance(condition, m.AbsenceCondition):
            self.target(condition.scope, locators, f"{path}/scope")
            if condition.observation_window_ms > min(condition.timeout_ms, timeout):
                self.error(
                    "observation_window", path, "absence window exceeds an enclosing deadline"
                )
            if condition.poll_interval_ms > condition.observation_window_ms:
                self.error("poll_interval", path, "sampling interval exceeds absence window")
        if isinstance(
            condition, (m.TextCondition, m.SemanticCondition, m.CountCondition, m.NumberCondition)
        ):
            info = self.value(condition.expected, parameters, variables, f"{path}/expected")
            expected_type: m.ScalarType = (
                "string"
                if isinstance(condition, (m.TextCondition, m.SemanticCondition))
                else "integer"
                if isinstance(condition, m.CountCondition)
                else "number"
            )
            if info is not None:
                if not compatible(info.type, expected_type):
                    self.error("expected_type", path, "expected value has the wrong type")
                if info.observed or info.secret:
                    self.error(
                        "mutable_expectation",
                        path,
                        "expected values cannot come from observations or secrets",
                    )
                if (
                    isinstance(condition, m.CountCondition)
                    and isinstance(info.value, (int, float))
                    and not isinstance(info.value, bool)
                    and info.value < 0
                ):
                    self.error("expected_count", path, "expected count cannot be negative")
                if (
                    condition.operator in ("semantic", "regex")
                    and isinstance(info.value, str)
                    and not info.value.strip()
                ):
                    self.error("empty_criterion", path, "criterion must not be empty")
                if condition.operator == "contains" and info.value == "":
                    self.error("empty_criterion", path, "contains criterion must not be empty")
        if condition.operator == "semantic":
            self.ai_requirements.append(
                Issue("model_required", path, "semantic checkpoint requires a model")
            )
        if condition.operator in ("enabled", "checked") and condition.source in ("ocr", "vision"):
            self.observation_requirements.append(
                Issue(
                    "state_evidence", path, "OCR/vision alone cannot establish native control state"
                )
            )

    def procedure(
        self,
        procedure: m.Case | m.Flow,
        profile: m.Profile | None,
        parameters: dict[str, ValueInfo],
        timeout: int,
        stack: tuple[str, ...] = (),
    ) -> dict[str, ValueInfo]:
        path = (
            self.project.manifest.cases[procedure.case_id]
            if isinstance(procedure, m.Case)
            else self.project.manifest.flows[procedure.flow_id]
        )
        profile_locators = (
            profile.locators
            if profile
            else {
                key: value
                for item in self.project.profiles.values()
                for key, value in item.locators.items()
            }
        )
        if profile is not None and procedure.locators.keys() & profile_locators.keys():
            self.error(
                "locator_shadow", path, "procedure and profile locator names must not overlap"
            )
        locators = {**profile_locators, **procedure.locators}
        if profile is not None:
            self.locators(locators, path)
        else:
            # Unbound flows have no selected platform mapping. Check global name existence,
            # not collisions with an unrelated profile the calling case does not select.
            self.locators(procedure.locators, path, set(locators))
        step_ids: set[str] = set()
        check_ids: set[str] = set()

        def checkpoint(
            check: m.Checkpoint, variables: dict[str, ValueInfo], budget: int, where: str
        ) -> None:
            if check.id in check_ids:
                self.error(
                    "duplicate_checkpoint", where, "checkpoint IDs must be unique in a procedure"
                )
            check_ids.add(check.id)
            self.condition(check.condition, parameters, variables, locators, budget, where)

        def assign(variables: dict[str, ValueInfo], name: str, info: ValueInfo, where: str) -> None:
            if name in variables:
                self.error(
                    "variable_redefinition",
                    where,
                    "variables are single-assignment within lexical scope",
                )
            else:
                variables[name] = info

        def steps(
            items: list[m.Step], variables: dict[str, ValueInfo], budget: int, where: str
        ) -> None:
            for index, step in enumerate(items):
                current = f"{where}/{index}"
                remaining = min(budget, step.timeout_ms)
                if step.id in step_ids:
                    self.error("duplicate_step", current, "step IDs must be unique in a procedure")
                step_ids.add(step.id)
                if isinstance(step, m.ActionStep):
                    action = step.action
                    constraints = self.project.manifest.constraints
                    if (
                        action.kind in constraints.forbidden_actions
                        or step.id in constraints.forbidden_actions
                    ):
                        self.policy_requirements.append(
                            Issue(
                                "forbidden_action",
                                current,
                                "action conflicts with manifest restrictions",
                            )
                        )
                    if (
                        isinstance(action, m.AppAction)
                        and action.app_id not in constraints.allowed_apps
                    ):
                        self.policy_requirements.append(
                            Issue(
                                "app_not_allowed",
                                current,
                                "action application is outside allowed scope",
                            )
                        )
                    if isinstance(action, (m.TapAction, m.LongAction, m.InputAction)):
                        self.target(action.target, locators, current)
                    if isinstance(action, m.InputAction):
                        info = self.value(action.text, parameters, variables, current)
                        if info and info.type != "string":
                            self.error("input_type", current, "input action text must be a string")
                    if (
                        isinstance(action, (m.LongAction, m.SwipeAction))
                        and action.duration_ms > remaining
                    ):
                        self.error(
                            "action_duration",
                            current,
                            "action duration exceeds an enclosing deadline",
                        )
                    if isinstance(action, m.SwipeAction):
                        self.point(action.start, current)
                        self.point(action.end, current)
                        if (action.start.width, action.start.height) != (
                            action.end.width,
                            action.end.height,
                        ):
                            self.error(
                                "swipe_screen",
                                current,
                                "swipe points use different screen dimensions",
                            )
                elif isinstance(step, m.FlowStep):
                    flow = self.project.flows.get(step.flow_ref)
                    if flow is None:
                        self.error("flow_reference", current, "flow does not exist")
                        continue
                    if step.flow_ref in stack:
                        self.error("flow_cycle", current, "recursive flow calls are not allowed")
                        continue
                    bindings = self.parameters(flow, self.project.manifest.flows[step.flow_ref])
                    for name, argument in step.arguments.items():
                        info = self.value(argument, parameters, variables, current)
                        if name not in flow.parameters:
                            self.error("flow_argument", current, "unknown flow argument")
                        elif info:
                            if not compatible(info.type, flow.parameters[name].type):
                                self.error(
                                    "flow_argument_type", current, "flow argument type mismatch"
                                )
                            bindings[name] = info
                    for name, parameter in flow.parameters.items():
                        if parameter.required and name not in step.arguments:
                            self.error(
                                "flow_argument_missing",
                                current,
                                "required flow argument is missing",
                            )
                    returns = self.procedure(
                        flow, profile, bindings, remaining, (*stack, step.flow_ref)
                    )
                    for name, variable in step.bind.items():
                        if name not in returns:
                            self.error(
                                "flow_return_reference", current, "unknown or invalid flow return"
                            )
                        else:
                            assign(variables, variable, returns[name], current)
                elif isinstance(step, m.WaitStep):
                    self.condition(
                        step.condition, parameters, variables, locators, remaining, current
                    )
                elif isinstance(step, m.CheckStep):
                    checkpoint(step.checkpoint, variables, remaining, current)
                elif isinstance(step, m.ExtractStep):
                    self.target(step.target, locators, current)
                    extracted_type = EXTRACT_TYPES[step.attribute]
                    assign(variables, step.as_, ValueInfo(extracted_type, observed=True), current)
                    if step.attribute in ("enabled", "checked") and step.source in (
                        "ocr",
                        "vision",
                    ):
                        self.observation_requirements.append(
                            Issue(
                                "state_evidence",
                                current,
                                "extraction needs native control-state evidence",
                            )
                        )
                elif isinstance(step, m.IfStep):
                    self.condition(
                        step.condition, parameters, variables, locators, remaining, current
                    )
                    steps(step.then, dict(variables), remaining, f"{current}/then")
                    steps(step.else_, dict(variables), remaining, f"{current}/else")
                elif isinstance(step, m.LoopStep):
                    if step.condition is not None:
                        self.condition(
                            step.condition, parameters, variables, locators, remaining, current
                        )
                    # Visit the syntax once, not max_iterations times. This is not an interpreter.
                    steps(step.steps, dict(variables), remaining, f"{current}/steps")
                elif isinstance(step, m.AiStep):
                    self.ai_requirements.append(
                        Issue("model_required", current, "ai_step requires a model")
                    )
                    for index, check in enumerate(step.checks):
                        checkpoint(check, variables, remaining, f"{current}/checks/{index}")

        variables: dict[str, ValueInfo] = {}
        if isinstance(procedure, m.Case):
            for index, check in enumerate(procedure.preconditions):
                checkpoint(check, variables, timeout, f"{path}/preconditions/{index}")
        steps(procedure.steps, variables, timeout, f"{path}/steps")
        if isinstance(procedure, m.Case):
            for index, check in enumerate(procedure.checks):
                checkpoint(check, variables, timeout, f"{path}/checks/{index}")
            return {}
        returns = {}
        for name, output in procedure.returns.items():
            info = self.value(output.value, parameters, variables, f"{path}/returns/{name}")
            if info:
                if not compatible(info.type, output.type):
                    self.error("flow_return_type", f"{path}/returns/{name}", "return type mismatch")
                returns[name] = info
        return returns

    def case_parameters(
        self, case: m.Case, *, overrides: dict[str, m.Scalar] | None = None
    ) -> dict[str, ValueInfo]:
        path = self.project.manifest.cases[case.case_id]
        parameters = self.parameters(case, path)
        for name, value in case.inputs.items():
            if overrides is not None and name in overrides:
                continue
            if isinstance(value, (m.ParameterRef, m.VariableRef)):
                self.error(
                    "case_input_reference",
                    f"{path}/inputs/{name}",
                    "case inputs must be immutable literals, data, or secret references",
                )
            info = self.value(value, {}, {}, f"{path}/inputs/{name}")
            if name not in case.parameters:
                self.error("case_input", f"{path}/inputs/{name}", "unknown input parameter")
            elif info:
                if not compatible(info.type, case.parameters[name].type):
                    self.error(
                        "case_input_type", f"{path}/inputs/{name}", "input parameter type mismatch"
                    )
                parameters[name] = info
        return parameters


def validate_semantics(project: Project) -> None:
    validator = _Validator(project)
    manifest = project.manifest
    for name, values in (
        ("entry_cases", manifest.entry_cases),
        ("platforms", manifest.platforms),
        ("secret_refs", manifest.secret_refs),
        ("required_capabilities", manifest.required_capabilities),
    ):
        if len(set(values)) != len(values):
            validator.error("duplicate_entry", f"manifest.json/{name}", "duplicate list entry")
    for entry in manifest.entry_cases:
        if entry not in project.cases:
            validator.error(
                "entry_reference", "manifest.json/entry_cases", "entry case does not exist"
            )
    for data in project.data.values():
        for value in data.values.values():
            validator.value(value, {}, {}, manifest.data[data.data_id])
    for profile in project.profiles.values():
        validator.locators(profile.locators, manifest.profiles[profile.profile_id])
        if profile.platform not in manifest.platforms:
            validator.error(
                "profile_platform",
                manifest.profiles[profile.profile_id],
                "platform is not declared in manifest",
            )
    # Unused flows are checked too. Profile-specific resolution is repeated in each case context.
    for flow in project.flows.values():
        validator.procedure(
            flow,
            None,
            validator.parameters(flow, manifest.flows[flow.flow_id]),
            manifest.budgets.case_timeout_ms,
            (flow.flow_id,),
        )
    for case in project.cases.values():
        parameters = validator.case_parameters(case)
        if len(set(case.profile_refs)) != len(case.profile_refs):
            validator.error(
                "duplicate_profile", manifest.cases[case.case_id], "duplicate profile reference"
            )
        for name in case.profile_refs:
            selected_profile = project.profiles.get(name)
            if selected_profile is None:
                validator.error(
                    "profile_reference", manifest.cases[case.case_id], "profile does not exist"
                )
            else:
                validator.procedure(
                    case, selected_profile, parameters, manifest.budgets.case_timeout_ms
                )
    if validator.issues:
        raise ContractError(validator.issues)


@dataclass(frozen=True)
class Preflight:
    issues: tuple[Issue, ...]
    ai_requirements: tuple[Issue, ...]

    @property
    def offline_ready(self) -> bool:
        return not self.issues


def preflight(
    project: Project,
    case_id: str,
    profile_id: str,
    *,
    strategy: m.Strategy | None = None,
    inputs: dict[str, m.Scalar] | None = None,
    model_available: bool = False,
    available_secret_refs: frozenset[str] = frozenset(),
    available_dependencies: frozenset[tuple[str, str, str | None]] = frozenset(),
) -> Preflight:
    """Check declared inputs/dependencies only. offline_ready never means a device is ready."""
    validate_semantics(project)
    validator = _Validator(project)
    case = project.cases.get(case_id)
    profile = project.profiles.get(profile_id)
    if case is None or profile is None or profile_id not in case.profile_refs:
        return Preflight(
            (Issue("selection", "preflight", "case/profile selection is not declared"),), ()
        )
    supplied = inputs or {}
    parameters = validator.case_parameters(case, overrides=supplied)
    for name, value in supplied.items():
        if name not in parameters:
            validator.error("input_unknown", f"inputs/{name}", "input parameter is not declared")
        else:
            # Strict scalar validation prevents Python bool/int coercion and non-JSON objects.
            try:
                scalar = SCALAR_ADAPTER.validate_python(value)
            except ValidationError:
                validator.error(
                    "input_invalid",
                    f"inputs/{name}",
                    "input must be a finite JSON scalar",
                )
                continue
            if not compatible(scalar_type(scalar), parameters[name].type):
                validator.error("input_type", f"inputs/{name}", "input parameter type mismatch")
            parameters[name] = ValueInfo(scalar_type(scalar), value=scalar)
    for name, parameter in case.parameters.items():
        if parameter.required and name not in supplied and name not in case.inputs:
            validator.error("input_required", f"inputs/{name}", "required input is not supplied")
    validator.procedure(case, profile, parameters, project.manifest.budgets.case_timeout_ms)
    policy = strategy or project.manifest.default_strategy
    if policy not in ("rule_only", "ai_on_locator_failure", "ai_led"):
        validator.error("strategy", "preflight", "unknown strategy")
    if validator.ai_requirements and policy != "ai_led":
        validator.error(
            "ai_policy",
            "preflight",
            "ai_step/semantic need ai_led; locator recovery is insufficient",
        )
    if (validator.ai_requirements or policy == "ai_led") and not model_available:
        validator.error("model_unavailable", "preflight", "required model is unavailable")
    for secret in sorted(validator.secrets - available_secret_refs):
        validator.error("secret_unavailable", f"secrets/{secret}", "declared secret is unavailable")
    for dependency in project.manifest.dependencies:
        if (dependency.kind, dependency.ref, dependency.version) not in available_dependencies:
            validator.error(
                "dependency_unavailable",
                f"dependencies/{dependency.ref}",
                "declared dependency has not been supplied; compatibility is not inferred",
            )
    for capability in sorted(
        set(project.manifest.required_capabilities) - set(profile.capabilities)
    ):
        validator.error(
            "capability_unavailable",
            f"profiles/{profile_id}",
            f"profile does not declare capability {capability}",
        )
    if profile.app_id not in project.manifest.constraints.allowed_apps:
        validator.error(
            "app_not_allowed",
            f"profiles/{profile_id}",
            "profile application is outside allowed scope",
        )
    validator.issues.extend(validator.observation_requirements)
    validator.issues.extend(validator.policy_requirements)
    return Preflight(
        tuple(dict.fromkeys(validator.issues)), tuple(dict.fromkeys(validator.ai_requirements))
    )
