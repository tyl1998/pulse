"""Offline v1 candidate. No device actions, expression evaluation, or runtime state.

Serialization details remain draft pending the decisions in docs/test-project.md section 16.
Only this module defines the wire shapes; JSON Schema and TypeScript are generated from it.
"""

from __future__ import annotations

import math
import re
import sys
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, TypeAdapter

CONTRACT_VERSION = "1-draft"
CONTRACT_STATUS = "draft"
SCHEMA_PREFIX = "urn:pulse:schema:1-draft:"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
END = r"$(?![\s\S])"


def json_integer(value: object) -> object:
    """JSON Schema integers include 1.0, but never booleans or numeric strings."""
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


type Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*" + END))
]
type Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
type Integer = Annotated[
    int,
    Field(strict=True, ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER),
    BeforeValidator(json_integer),
]
type PositiveInt = Annotated[
    int, Field(strict=True, gt=0, le=MAX_SAFE_INTEGER), BeforeValidator(json_integer)
]
type NonNegativeInt = Annotated[
    int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER), BeforeValidator(json_integer)
]
type Number = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-sys.float_info.max, le=sys.float_info.max),
]
type Scalar = (
    Annotated[str, Field(strict=True)] | Integer | Number | Annotated[bool, Field(strict=True)]
)
type ScalarType = Literal["string", "integer", "number", "boolean"]
type Platform = Literal["android", "ios"]
type Strategy = Literal["rule_only", "ai_on_locator_failure", "ai_led"]
type ObservationSource = Literal["native", "webview", "ocr", "vision"]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, validate_default=True
    )


class ParameterRef(ContractModel):
    param_ref: Identifier


class VariableRef(ContractModel):
    var_ref: Identifier


class DataRef(ContractModel):
    data_ref: Identifier
    key: Identifier


class SecretRef(ContractModel):
    secret_ref: Identifier


type Value = Scalar | ParameterRef | VariableRef | DataRef | SecretRef
type TextExpected = Annotated[str, Field(strict=True)] | ParameterRef | DataRef
type TextValue = TextExpected | VariableRef | SecretRef
type NumberExpected = Number | ParameterRef | DataRef
type CountExpected = NonNegativeInt | ParameterRef | DataRef


class Parameter(ContractModel):
    type: ScalarType
    required: bool
    # Presence is significant; null is not a scalar default (checked by both semantic entrypoints).
    default: Scalar | None = Field(default=None, exclude_if=lambda value: value is None)


class ReturnValue(ContractModel):
    type: ScalarType
    value: Value


class Point(ContractModel):
    x: NonNegativeInt
    y: NonNegativeInt
    width: PositiveInt
    height: PositiveInt


class LocatorCandidate(ContractModel):
    by: Literal["res_id", "text", "desc", "xpath", "ocr_text", "image"]
    value: Text
    scope: Identifier | None = None


class PointCandidate(ContractModel):
    by: Literal["point"]
    value: Point
    scope: Identifier | None = None


class LocatorExpect(ContractModel):
    unique: bool
    visible: bool


class Locator(ContractModel):
    candidates: Annotated[
        list[Annotated[LocatorCandidate | PointCandidate, Field(discriminator="by")]],
        Field(min_length=1),
    ]
    expect: LocatorExpect


class LocatorRef(ContractModel):
    locator_ref: Identifier


class InlineLocator(ContractModel):
    locator: Locator


type Target = LocatorRef | InlineLocator


class Condition(ContractModel):
    target: Target
    source: ObservationSource
    timeout_ms: PositiveInt = 10_000


class PresenceCondition(Condition):
    operator: Literal["exists", "page_is"]


class AbsenceCondition(Condition):
    operator: Literal["not_exists"]
    scope: Target
    observation_window_ms: PositiveInt
    poll_interval_ms: PositiveInt = 500


class StateCondition(Condition):
    operator: Literal["visible", "enabled", "checked"]
    expected: bool


class TextCondition(Condition):
    operator: Literal["text_equals", "contains", "regex"]
    expected: TextExpected


class SemanticCondition(Condition):
    operator: Literal["semantic"]
    expected: TextExpected
    timeout_ms: PositiveInt = Field(...)


class CountCondition(Condition):
    operator: Literal["count_eq", "count_gt", "count_lt"]
    expected: CountExpected


class NumberCondition(Condition):
    operator: Literal["number_cmp"]
    comparison: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    expected: NumberExpected
    unit: Text
    precision: NonNegativeInt


type Predicate = Annotated[
    PresenceCondition
    | AbsenceCondition
    | StateCondition
    | TextCondition
    | SemanticCondition
    | CountCondition
    | NumberCondition,
    Field(discriminator="operator"),
]


class Checkpoint(ContractModel):
    id: Identifier
    required: bool
    condition: Predicate


class TapAction(ContractModel):
    kind: Literal["tap"]
    target: Target


class LongAction(ContractModel):
    kind: Literal["long"]
    target: Target
    duration_ms: PositiveInt


class InputAction(ContractModel):
    kind: Literal["input"]
    target: Target
    text: TextValue
    mode: Literal["replace", "append"]


class SwipeAction(ContractModel):
    kind: Literal["swipe"]
    start: Point
    end: Point
    duration_ms: PositiveInt


class KeyAction(ContractModel):
    kind: Literal["key"]
    key: Text


class AppAction(ContractModel):
    kind: Literal["launch", "stop", "clear"]
    app_id: Text


type Action = Annotated[
    TapAction | LongAction | InputAction | SwipeAction | KeyAction | AppAction,
    Field(discriminator="kind"),
]


class StepBase(ContractModel):
    id: Identifier
    timeout_ms: PositiveInt = 30_000


class ActionStep(StepBase):
    type: Literal["action"]
    action: Action


class FlowStep(StepBase):
    type: Literal["flow"]
    flow_ref: Identifier
    arguments: dict[Identifier, Value] = Field(default_factory=dict)
    bind: dict[Identifier, Identifier] = Field(default_factory=dict)


class WaitStep(StepBase):
    type: Literal["wait"]
    condition: Predicate


class CheckStep(StepBase):
    type: Literal["check"]
    checkpoint: Checkpoint


class ExtractStep(StepBase):
    type: Literal["extract"]
    target: Target
    source: ObservationSource
    attribute: Literal["text", "count", "enabled", "checked"]
    as_: Identifier = Field(alias="as")


class IfStep(StepBase):
    type: Literal["if"]
    condition: Predicate
    then: Annotated[list[Step], Field(min_length=1)]
    else_: list[Step] = Field(default_factory=list, alias="else")


class LoopStep(StepBase):
    type: Literal["loop"]
    max_iterations: PositiveInt
    steps: Annotated[list[Step], Field(min_length=1)]
    condition: Predicate | None = None


class AiStep(StepBase):
    type: Literal["ai_step"]
    timeout_ms: PositiveInt = Field(...)
    goal: Text
    checks: Annotated[list[Checkpoint], Field(min_length=1)]


type Step = Annotated[
    ActionStep | FlowStep | WaitStep | CheckStep | ExtractStep | IfStep | LoopStep | AiStep,
    Field(discriminator="type"),
]


class Budgets(ContractModel):
    case_timeout_ms: PositiveInt = 600_000
    suite_timeout_ms: PositiveInt = 3_600_000
    model_request_timeout_ms: PositiveInt = 45_000
    recovery_timeout_ms: PositiveInt = 60_000
    recovery_model_calls: PositiveInt = 2
    recovery_actions: PositiveInt = 3
    ai_case_model_calls: PositiveInt = 20
    authorization_timeout_ms: PositiveInt = 600_000
    stop_timeout_ms: PositiveInt = 30_000


class Constraints(ContractModel):
    allowed_apps: Annotated[list[Text], Field(min_length=1)]
    forbidden_actions: list[Identifier]


class Dependency(ContractModel):
    kind: Literal["runtime", "protocol", "driver", "model", "network", "data"]
    ref: Identifier
    version: Text | None = None


type CasePath = Annotated[
    str,
    StringConstraints(pattern=re.compile(r"^cases/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.json" + END)),
]
type FlowPath = Annotated[
    str,
    StringConstraints(pattern=re.compile(r"^flows/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.json" + END)),
]
type DataPath = Annotated[
    str,
    StringConstraints(pattern=re.compile(r"^data/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.json" + END)),
]
type ProfilePath = Annotated[
    str,
    StringConstraints(
        pattern=re.compile(r"^profiles/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.json" + END)
    ),
]
type ResourcePath = Annotated[
    str,
    StringConstraints(
        pattern=re.compile(r"^resources/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.[A-Za-z0-9]+" + END)
    ),
]


class Manifest(ContractModel):
    schema_: Literal["urn:pulse:schema:1-draft:manifest"] = Field(alias="$schema")
    project_id: Identifier
    name: Text
    revision: Text
    cases: dict[Identifier, CasePath]
    flows: dict[Identifier, FlowPath]
    data: dict[Identifier, DataPath]
    profiles: dict[Identifier, ProfilePath]
    entry_cases: Annotated[list[Identifier], Field(min_length=1)]
    default_strategy: Strategy
    platforms: Annotated[list[Platform], Field(min_length=1)]
    required_capabilities: list[Text]
    constraints: Constraints
    dependencies: list[Dependency]
    resources: dict[Identifier, ResourcePath] = Field(default_factory=dict)
    secret_refs: list[Identifier] = Field(default_factory=list)
    budgets: Budgets = Field(default_factory=Budgets)


class Procedure(ContractModel):
    name: Text
    parameters: dict[Identifier, Parameter]
    locators: dict[Identifier, Locator]
    steps: Annotated[list[Step], Field(min_length=1)]


class Case(Procedure):
    schema_: Literal["urn:pulse:schema:1-draft:case"] = Field(alias="$schema")
    case_id: Identifier
    goal: Text
    profile_refs: Annotated[list[Identifier], Field(min_length=1)]
    inputs: dict[Identifier, Value]
    preconditions: list[Checkpoint]
    checks: list[Checkpoint]
    # Original authoring text is data, never an executable fallback.
    source: Text | None = None
    original_text: str | None = None


class Flow(Procedure):
    schema_: Literal["urn:pulse:schema:1-draft:flow"] = Field(alias="$schema")
    flow_id: Identifier
    returns: dict[Identifier, ReturnValue]


class DataSet(ContractModel):
    schema_: Literal["urn:pulse:schema:1-draft:data"] = Field(alias="$schema")
    data_id: Identifier
    values: dict[Identifier, Scalar | SecretRef]


class Profile(ContractModel):
    schema_: Literal["urn:pulse:schema:1-draft:profile"] = Field(alias="$schema")
    profile_id: Identifier
    platform: Platform
    environment: Identifier
    app_id: Text
    capabilities: list[Text]
    locators: dict[Identifier, Locator]


type Document = Annotated[
    Manifest | Case | Flow | DataSet | Profile, Field(discriminator="schema_")
]

DOCUMENT_MODELS: dict[str, type[ContractModel]] = {
    "manifest": Manifest,
    "case": Case,
    "flow": Flow,
    "data": DataSet,
    "profile": Profile,
}

for _model in (IfStep, LoopStep, Case, Flow):
    _model.model_rebuild()

DOCUMENT_ADAPTER = TypeAdapter(Document)
