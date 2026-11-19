"""Wire models for the local Runtime API (P2.2).

These models are the source of the generated OpenAPI document and TypeScript client. They
describe the loopback transport only; they are not the case DSL or a remote-node protocol.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pulse_runtime.drivers.android.actions import ActionTarget
from pulse_runtime.perception.locators import LocateBody
from pulse_schema import models as m

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SERIAL_PATTERN = r"^[A-Za-z0-9._:-]{1,64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$"

RunState = Literal[
    "queued",
    "preparing",
    "running",
    "paused",
    "stopping",
    "passed",
    "failed",
    "blocked",
    "canceled",
    "interrupted",
]
RecordingState = Literal["recording", "paused", "stopped"]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    pid: int
    uptime_s: float


class TargetAppDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str
    guidance: str | None


class TargetAppResponse(BaseModel):
    """Installation evidence only, not Agent readiness or target-case/build verification."""

    model_config = ConfigDict(extra="forbid", strict=True)

    serial: str = Field(pattern=SERIAL_PATTERN)
    package: Literal["com.pulse.target"]
    status: Literal["installed", "missing", "unavailable", "failed"]
    version_name: str | None = Field(max_length=128)
    version_code: int | None = Field(ge=0, le=2**63 - 1)
    observed_at_ms: int | None = Field(ge=0)
    diagnostic: TargetAppDiagnostic


class LocateRequest(LocateBody):
    """HTTP envelope with the resolver's existing validation and defaults unchanged."""


LocateStatus = Literal["resolved", "ambiguous", "not_found", "indeterminate"]
LocateCandidateBy = Literal["res_id", "text", "desc", "xpath", "ocr_text", "image", "point"]
LocateSourceStatus = Literal["available", "unavailable", "failed"]


class _LocateResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class LocateError(_LocateResultModel):
    code: str


class LocateSourceError(LocateError):
    cause_code: str | None = None


class LocateHttpError(_LocateResultModel):
    error: str


class LocateRejected(_LocateResultModel):
    status: Literal["rejected"]
    error: Literal["invalid_request", "request_too_large"]


class LocateBounds(_LocateResultModel):
    left: int
    top: int
    right: int
    bottom: int


class LocateStates(_LocateResultModel):
    enabled: bool | None
    checked: bool | None
    checkable: bool | None
    clickable: bool | None
    editable: bool | None
    focused: bool | None
    showing_hint_text: bool | None
    selected: bool | None
    visible: bool | None
    password: bool | None
    long_clickable: bool | None
    scrollable: bool | None


class LocateMatch(_LocateResultModel):
    """Public match metadata; text is available only to the Runtime-internal probe."""

    source: Literal["tree", "screenshot", "ocr"]
    coordinate_space: Literal["display_pixels", "screenshot_pixels"]
    display_id: Literal[0]
    node_path: list[int] | None
    block_index: int | None
    bounds: LocateBounds | None
    visible: bool | None
    visibility_evidence: Literal["native_state", "bitmap_bounds"]
    states: LocateStates
    score: float | None


class LocateAttempt(_LocateResultModel):
    index: int
    by: LocateCandidateBy
    status: LocateStatus
    match_count: int
    error: LocateError | None


class LocatePoint(_LocateResultModel):
    x: int
    y: int


class LocateActionTarget(ActionTarget):
    """Only a native display target can be returned by the existing resolver."""

    source: Literal["tree"]
    coordinate_space: Literal["display_pixels"]
    display_id: Literal[0]


class LocateCompleteness(_LocateResultModel):
    status: Literal["complete", "partial", "unavailable"]
    reasons: list[str]


class LocateSourceTime(_LocateResultModel):
    clock: Literal["android_uptime_ms"]
    calibrated: Literal[False]
    started_at_uptime_ms: int | None
    finished_at_uptime_ms: int | None
    captured_at_uptime_ms: int | None = None
    capture_clock: Literal["platform_as_reported"] | None = None
    capture_clock_calibrated: Literal[False] | None = None


class LocateCoordinateMapping(_LocateResultModel):
    status: Literal["available", "unavailable"]
    error: LocateError | None


class LocateSourceCoordinates(_LocateResultModel):
    coordinate_space: Literal["display_pixels", "screenshot_pixels"]
    requested_display_id: int
    display_id: int | None
    width: int | None
    height: int | None
    original_width: int | None
    original_height: int | None
    scale_x: float | None
    scale_y: float | None
    rotation: None
    to_original_bitmap: LocateCoordinateMapping | None = None
    to_display: LocateCoordinateMapping | None = None


class LocateSourceScope(_LocateResultModel):
    kind: Literal["active_window", "default_display"]
    display_id: int | None
    window_id: int | None
    target_app_verified: None


class LocateSourceCollection(_LocateResultModel):
    status: LocateSourceStatus
    error: LocateSourceError | None
    time: LocateSourceTime
    purpose: Literal["ocr_dependency"]


class LocateObservationSource(_LocateResultModel):
    """Source-local evidence without the tree/OCR/bitmap data payload."""

    source: Literal["accessibility", "accessibility_screenshot", "mlkit_chinese"]
    requested: bool
    capability: Literal["available", "unavailable"] | None
    status: LocateSourceStatus
    error: LocateSourceError | None
    time: LocateSourceTime
    coordinates: LocateSourceCoordinates
    scope: LocateSourceScope
    completeness: LocateCompleteness
    collection: LocateSourceCollection | None = None


class LocateObservationSources(_LocateResultModel):
    tree: LocateObservationSource
    screenshot: LocateObservationSource
    ocr: LocateObservationSource


class LocateFraming(_LocateResultModel):
    protocol_version: Literal[1]
    validated: bool
    source_data_validated: bool
    terminal_received: bool
    eof_received: bool


class LocateRuntimeTime(_LocateResultModel):
    started_at_epoch_ms: int
    finished_at_epoch_ms: int
    duration_ms: float


class LocateActionGeometry(_LocateResultModel):
    display_id: Literal[0]
    width: int
    height: int
    rotation: Literal[0, 90, 180, 270]


class LocateActionContext(_LocateResultModel):
    status: Literal["available", "unavailable"]
    source: Literal["adb_input_viewport"]
    geometry: LocateActionGeometry | None
    error: LocateError | None
    atomic_with_observation: Literal[False]


class LocateRequestedSources(_LocateResultModel):
    tree: bool
    screenshot: bool
    ocr: bool


class LocateObservation(_LocateResultModel):
    status: Literal["completed", "failed"]
    error: LocateError | None
    framing: LocateFraming
    agent_lifecycle_id: str
    runtime_time: LocateRuntimeTime
    completeness: LocateCompleteness
    revision_start: int | None
    revision_end: int | None
    action_context: LocateActionContext
    requested: LocateRequestedSources
    requested_display_id: int
    atomic_snapshot: Literal[False]
    sources: LocateObservationSources


class LocateResult(_LocateResultModel):
    """One locator query, not an action receipt or a business assertion."""

    contract: Literal["pulse-runtime-locator-draft"]
    serial: str
    session_id: str
    request_id: str
    observation_id: str | None
    status: LocateStatus
    error: LocateError | None
    candidate_index: int | None
    candidate_by: LocateCandidateBy | None
    match_count: int
    matches: list[LocateMatch]
    query_complete: bool | None
    attempts: list[LocateAttempt]
    point: LocatePoint | None
    action_target: LocateActionTarget | None
    action_target_error: LocateError | None
    business_success: None
    automatic_replay: Literal[False]
    observation: LocateObservation


class IssueModel(BaseModel):
    code: str
    path: str
    message: str


class ProjectSummary(BaseModel):
    contract: Literal["pulse-runtime-api-draft"] = "pulse-runtime-api-draft"
    root: str
    project_id: str
    name: str
    revision: str
    entry_cases: list[str]
    case_ids: list[str]
    flow_ids: list[str]
    data_ids: list[str]
    profile_ids: list[str]
    platforms: list[str]
    default_strategy: m.Strategy
    required_capabilities: list[str]
    allowed_apps: list[str]


class ProjectDetail(ProjectSummary):
    manifest: dict[str, Any]


class ProjectList(BaseModel):
    projects: list[ProjectSummary]


class ProjectOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    profile_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    strategy: m.Strategy | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    model_available: bool = False


class PreflightResponse(BaseModel):
    offline_ready: bool
    issues: list[IssueModel]
    ai_requirements: list[IssueModel]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=IDENTIFIER_PATTERN)
    case_id: str = Field(pattern=IDENTIFIER_PATTERN)
    profile_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    device: str = Field(pattern=SERIAL_PATTERN)
    strategy: m.Strategy | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    activities: dict[str, str] = Field(default_factory=dict)
    allow_clear: bool = False
    include_screenshot: bool = True
    auto_connect: bool = True
    connect_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    breakpoints: list[str] = Field(default_factory=list, max_length=256)


class RunCurrent(BaseModel):
    kind: Literal["step", "check"]
    id: str
    type: str | None = None


class DebugState(BaseModel):
    paused: bool = False
    step_mode: bool = False
    breakpoints: list[str] = Field(default_factory=list)
    current: RunCurrent | None = None


class EvidenceSummary(BaseModel):
    status: Literal["pending", "written", "incomplete", "unavailable"] = "pending"
    directory: str | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)


class RunError(BaseModel):
    code: str
    message: str | None = None


class RunSummary(BaseModel):
    contract: Literal["pulse-runtime-api-draft"] = "pulse-runtime-api-draft"
    run_id: str
    project_id: str
    case_id: str
    profile_id: str
    device: str
    strategy: m.Strategy
    state: RunState
    reason: str | None = None
    business_success: bool | None = None
    created_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: float | None = None
    debug: DebugState = Field(default_factory=DebugState)
    evidence: EvidenceSummary = Field(default_factory=EvidenceSummary)
    error: RunError | None = None


class RunDetail(RunSummary):
    session_id: str | None = None
    result: dict[str, Any] | None = None


class RunList(BaseModel):
    runs: list[RunSummary]


class RunResultResponse(BaseModel):
    run_id: str
    state: RunState
    result: dict[str, Any] | None = None


class DebugBreakpointsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    breakpoints: list[str] = Field(default_factory=list, max_length=256)


class DebugSnapshot(BaseModel):
    run_id: str
    state: RunState
    paused: bool
    step_mode: bool
    breakpoints: list[str]
    current: RunCurrent | None = None


class RecordingStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial: str = Field(pattern=SERIAL_PATTERN)
    session_id: str = Field(pattern=UUID_PATTERN)


class RecordedAction(BaseModel):
    action_id: str
    operation_id: str
    kind: str
    source: Literal["recorder"]
    target_source: str
    target_observation_id: str
    params: dict[str, Any]
    status: str
    action_completed: bool
    recorded_at_ms: int
    post_observation_id: str | None = None


class RecordingSummary(BaseModel):
    contract: Literal["pulse-recording-draft"] = "pulse-recording-draft"
    recording_id: str
    serial: str
    session_id: str
    state: RecordingState
    created_at_ms: int
    updated_at_ms: int
    event_count: int = 0


class RecordingDetail(RecordingSummary):
    events: list[RecordedAction] = Field(default_factory=list)
    draft: dict[str, Any] | None = None


class RecordingList(BaseModel):
    recordings: list[RecordingSummary]


class EventEnvelope(BaseModel):
    v: Literal[1] = 1
    type: str
    msg_id: str
    seq: int
    ts: float
    run_id: str | None = None
    device_id: str | None = None
    body: dict[str, Any]


class EventPage(BaseModel):
    events: list[EventEnvelope]
    oldest_seq: int
    latest_seq: int
    gap: bool
