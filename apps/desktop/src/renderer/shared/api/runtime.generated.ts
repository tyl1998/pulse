/* Generated from FastAPI OpenAPI. Do not edit. Run pnpm runtime:client. */
/* eslint-disable */

export namespace PulseRuntimeApiSchemas {
  export type ManualRecoveryConfirmed = boolean;
  export type OperationId = string | null;
  export type TimeoutMs = number;
  export type DisplayId = number;
  export type ForAction = boolean;
  export type Ocr = boolean;
  export type RequestId = string;
  export type Screenshot = boolean;
  export type SessionId = string;
  export type TimeoutMs1 = number;
  export type Tree = boolean;
  export type EnableAccessibility = boolean;
  export type Install = boolean;
  export type ManualRecoveryConfirmed1 = boolean;
  export type OperationId1 = string | null;
  export type TimeoutMs2 = number;
  export type RunId = string | null;
  export type SessionId1 = string;
  export type Source = string;
  export type Confirmed = boolean;
  export type SessionId2 = string;
  export type ControlEpoch = number;
  export type RunId1 = string | null;
  export type SessionId3 = string;
  export type WaitForIdleMs = number;
  export type RunId2 = string | null;
  export type SessionId4 = string;
  export type Source1 = string;
  export type TimeoutMs3 = number;
  /**
   * @maxItems 256
   */
  export type Breakpoints = string[];
  export type Breakpoints1 = string[];
  export type Id = string;
  export type Kind = 'step' | 'check';
  export type Type = string | null;
  export type Paused = boolean;
  export type RunId3 = string;
  export type State =
    | 'queued'
    | 'preparing'
    | 'running'
    | 'paused'
    | 'stopping'
    | 'passed'
    | 'failed'
    | 'blocked'
    | 'canceled'
    | 'interrupted';
  export type StepMode = boolean;
  export type Breakpoints2 = string[];
  export type Paused1 = boolean;
  export type StepMode1 = boolean;
  export type DeviceId = string | null;
  export type MsgId = string;
  export type RunId4 = string | null;
  export type Seq = number;
  export type Ts = number;
  export type Type1 = string;
  export type V = 1;
  export type Events = EventEnvelope[];
  export type Gap = boolean;
  export type LatestSeq = number;
  export type OldestSeq = number;
  export type Directory = string | null;
  export type Issues = {
    [k: string]: unknown | undefined;
  }[];
  export type Status = 'pending' | 'written' | 'incomplete' | 'unavailable';
  export type Location = (string | number)[];
  export type Message = string;
  export type ErrorType = string;
  export type Detail = ValidationError[];
  export type Pid = number;
  export type Status1 = 'ok';
  export type UptimeS = number;
  export type Version = string;
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Identifier".
   */
  export type Identifier = string;
  export type PngBase64 = string;
  /**
   * @minItems 1
   */
  export type Candidates = [
    LocatorCandidate | PointCandidate,
    ...(LocatorCandidate | PointCandidate)[],
  ];
  export type By = 'res_id' | 'text' | 'desc' | 'xpath' | 'ocr_text' | 'image';
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Text".
   */
  export type Text = string;
  export type By1 = 'point';
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "PositiveInt".
   */
  export type PositiveInt = number;
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "NonNegativeInt".
   */
  export type NonNegativeInt = number;
  export type Unique = boolean;
  export type Visible = boolean;
  export type Code = string;
  export type Message1 = string;
  export type Path = string;
  export type AtomicWithObservation = false;
  export type Code1 = string;
  export type DisplayId1 = 0;
  export type Height = number;
  export type Rotation = 0 | 90 | 180 | 270;
  export type Width = number;
  export type Source2 = 'adb_input_viewport';
  export type Status2 = 'available' | 'unavailable';
  export type CoordinateSpace = 'display_pixels';
  export type DisplayId2 = 0;
  export type ExpectedPackage = string;
  /**
   * @maxItems 32
   */
  export type NodePath = number[];
  export type ObservationId = string;
  export type Source3 = 'tree';
  export type By2 = 'res_id' | 'text' | 'desc' | 'xpath' | 'ocr_text' | 'image' | 'point';
  export type Index = number;
  export type MatchCount = number;
  export type Status3 = 'resolved' | 'ambiguous' | 'not_found' | 'indeterminate';
  export type Bottom = number;
  export type Left = number;
  export type Right = number;
  export type Top = number;
  export type Reasons = string[];
  export type Status4 = 'complete' | 'partial' | 'unavailable';
  export type Status5 = 'available' | 'unavailable';
  export type EofReceived = boolean;
  export type ProtocolVersion = 1;
  export type SourceDataValidated = boolean;
  export type TerminalReceived = boolean;
  export type Validated = boolean;
  export type Error = string;
  export type BlockIndex = number | null;
  export type CoordinateSpace1 = 'display_pixels' | 'screenshot_pixels';
  export type DisplayId3 = 0;
  export type NodePath1 = number[] | null;
  export type Score = number | null;
  export type Source4 = 'tree' | 'screenshot' | 'ocr';
  export type Checkable = boolean | null;
  export type Checked = boolean | null;
  export type Clickable = boolean | null;
  export type Editable = boolean | null;
  export type Enabled = boolean | null;
  export type Focused = boolean | null;
  export type LongClickable = boolean | null;
  export type Password = boolean | null;
  export type Scrollable = boolean | null;
  export type Selected = boolean | null;
  export type ShowingHintText = boolean | null;
  export type Visible1 = boolean | null;
  export type VisibilityEvidence = 'native_state' | 'bitmap_bounds';
  export type Visible2 = boolean | null;
  export type AgentLifecycleId = string;
  export type AtomicSnapshot = false;
  export type Ocr1 = boolean;
  export type Screenshot1 = boolean;
  export type Tree1 = boolean;
  export type RequestedDisplayId = number;
  export type RevisionEnd = number | null;
  export type RevisionStart = number | null;
  export type DurationMs = number;
  export type FinishedAtEpochMs = number;
  export type StartedAtEpochMs = number;
  export type Capability = ('available' | 'unavailable') | null;
  export type CauseCode = string | null;
  export type Code2 = string;
  export type Purpose = 'ocr_dependency';
  export type Status6 = 'available' | 'unavailable' | 'failed';
  export type Calibrated = false;
  export type CaptureClock = 'platform_as_reported' | null;
  export type CaptureClockCalibrated = false | null;
  export type CapturedAtUptimeMs = number | null;
  export type Clock = 'android_uptime_ms';
  export type FinishedAtUptimeMs = number | null;
  export type StartedAtUptimeMs = number | null;
  export type CoordinateSpace2 = 'display_pixels' | 'screenshot_pixels';
  export type DisplayId4 = number | null;
  export type Height1 = number | null;
  export type OriginalHeight = number | null;
  export type OriginalWidth = number | null;
  export type RequestedDisplayId1 = number;
  export type Rotation1 = null;
  export type ScaleX = number | null;
  export type ScaleY = number | null;
  export type Width1 = number | null;
  export type Requested = boolean;
  export type DisplayId5 = number | null;
  export type Kind1 = 'active_window' | 'default_display';
  export type TargetAppVerified = null;
  export type WindowId = number | null;
  export type Source5 = 'accessibility' | 'accessibility_screenshot' | 'mlkit_chinese';
  export type Status7 = 'available' | 'unavailable' | 'failed';
  export type Status8 = 'completed' | 'failed';
  export type X = number;
  export type Y = number;
  export type Error1 = 'invalid_request' | 'request_too_large';
  export type Status9 = 'rejected';
  export type DisplayId6 = number;
  export type ExpectedPackage1 = string;
  export type ImageThreshold = number;
  export type RequestId1 = string;
  export type Serial = string;
  export type SessionId5 = string;
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Target".
   */
  export type Target = LocatorRef | InlineLocator;
  export type TimeoutMs4 = number;
  export type Attempts = LocateAttempt[];
  export type AutomaticReplay = false;
  export type BusinessSuccess = null;
  export type CandidateBy =
    ('res_id' | 'text' | 'desc' | 'xpath' | 'ocr_text' | 'image' | 'point') | null;
  export type CandidateIndex = number | null;
  export type Contract = 'pulse-runtime-locator-draft';
  export type MatchCount1 = number;
  export type Matches = LocateMatch[];
  export type ObservationId1 = string | null;
  export type QueryComplete = boolean | null;
  export type RequestId2 = string;
  export type Serial1 = string;
  export type SessionId6 = string;
  export type Status10 = 'resolved' | 'ambiguous' | 'not_found' | 'indeterminate';
  export type CaseId = string;
  export type ModelAvailable = boolean;
  export type ProfileId = string | null;
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Strategy".
   */
  export type Strategy = 'rule_only' | 'ai_on_locator_failure' | 'ai_led';
  export type AiRequirements = IssueModel[];
  export type Issues1 = IssueModel[];
  export type OfflineReady = boolean;
  export type AllowedApps = string[];
  export type CaseIds = string[];
  export type Contract1 = 'pulse-runtime-api-draft';
  export type DataIds = string[];
  export type EntryCases = string[];
  export type FlowIds = string[];
  export type Name = string;
  export type Platforms = string[];
  export type ProfileIds = string[];
  export type ProjectId = string;
  export type RequiredCapabilities = string[];
  export type Revision = string;
  export type Root = string;
  export type AllowedApps1 = string[];
  export type CaseIds1 = string[];
  export type Contract2 = 'pulse-runtime-api-draft';
  export type DataIds1 = string[];
  export type EntryCases1 = string[];
  export type FlowIds1 = string[];
  export type Name1 = string;
  export type Platforms1 = string[];
  export type ProfileIds1 = string[];
  export type ProjectId1 = string;
  export type RequiredCapabilities1 = string[];
  export type Revision1 = string;
  export type Root1 = string;
  export type Projects = ProjectSummary[];
  export type Path1 = string;
  export type ActionCompleted = boolean;
  export type ActionId = string;
  export type Kind2 = string;
  export type OperationId2 = string;
  export type PostObservationId = string | null;
  export type RecordedAtMs = number;
  export type Source6 = 'recorder';
  export type Status11 = string;
  export type TargetObservationId = string;
  export type TargetSource = string;
  export type Contract3 = 'pulse-recording-draft';
  export type CreatedAtMs = number;
  export type Draft = {
    [k: string]: unknown | undefined;
  } | null;
  export type EventCount = number;
  export type Events1 = RecordedAction[];
  export type RecordingId = string;
  export type Serial2 = string;
  export type SessionId7 = string;
  export type State1 = 'recording' | 'paused' | 'stopped';
  export type UpdatedAtMs = number;
  export type Contract4 = 'pulse-recording-draft';
  export type CreatedAtMs1 = number;
  export type EventCount1 = number;
  export type RecordingId1 = string;
  export type Serial3 = string;
  export type SessionId8 = string;
  export type State2 = 'recording' | 'paused' | 'stopped';
  export type UpdatedAtMs1 = number;
  export type Recordings = RecordingSummary[];
  export type Serial4 = string;
  export type SessionId9 = string;
  export type BitRate = number;
  export type Channel = string;
  export type MaxFps = number;
  export type MaxSize = number;
  export type RelayUrl = string;
  export type Token = string;
  export type PublisherId = string;
  export type BusinessSuccess1 = boolean | null;
  export type CaseId1 = string;
  export type Contract5 = 'pulse-runtime-api-draft';
  export type CreatedAtMs2 = number;
  export type Device = string;
  export type ElapsedMs = number | null;
  export type Code3 = string;
  export type Message2 = string | null;
  export type FinishedAtMs = number | null;
  export type ProfileId1 = string;
  export type ProjectId2 = string;
  export type Reason = string | null;
  export type Result = {
    [k: string]: unknown | undefined;
  } | null;
  export type RunId5 = string;
  export type SessionId10 = string | null;
  export type StartedAtMs = number | null;
  export type State3 =
    | 'queued'
    | 'preparing'
    | 'running'
    | 'paused'
    | 'stopping'
    | 'passed'
    | 'failed'
    | 'blocked'
    | 'canceled'
    | 'interrupted';
  export type BusinessSuccess2 = boolean | null;
  export type CaseId2 = string;
  export type Contract6 = 'pulse-runtime-api-draft';
  export type CreatedAtMs3 = number;
  export type Device1 = string;
  export type ElapsedMs1 = number | null;
  export type FinishedAtMs1 = number | null;
  export type ProfileId2 = string;
  export type ProjectId3 = string;
  export type Reason1 = string | null;
  export type RunId6 = string;
  export type StartedAtMs1 = number | null;
  export type State4 =
    | 'queued'
    | 'preparing'
    | 'running'
    | 'paused'
    | 'stopping'
    | 'passed'
    | 'failed'
    | 'blocked'
    | 'canceled'
    | 'interrupted';
  export type Runs = RunSummary[];
  export type AllowClear = boolean;
  export type AutoConnect = boolean;
  /**
   * @maxItems 256
   */
  export type Breakpoints3 = string[];
  export type CaseId3 = string;
  export type ConnectTimeoutMs = number;
  export type Device2 = string;
  export type IncludeScreenshot = boolean;
  export type ProfileId3 = string | null;
  export type ProjectId4 = string;
  export type Result1 = {
    [k: string]: unknown | undefined;
  } | null;
  export type RunId7 = string;
  export type State5 =
    | 'queued'
    | 'preparing'
    | 'running'
    | 'paused'
    | 'stopping'
    | 'passed'
    | 'failed'
    | 'blocked'
    | 'canceled'
    | 'interrupted';
  export type Code4 = string;
  export type Guidance = string | null;
  export type ObservedAtMs = number | null;
  export type Package = 'com.pulse.target';
  export type Serial5 = string;
  export type Status12 = 'installed' | 'missing' | 'unavailable' | 'failed';
  export type VersionCode = number | null;
  export type VersionName = string | null;
  export type Code5 = number;
  export type Kind3 = string;
  export type X1 = number;
  export type Y1 = number;

  export interface PulseRuntimeApiSchemas {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "AgentConnectBody".
   */
  export interface AgentConnectBody {
    manual_recovery_confirmed?: ManualRecoveryConfirmed;
    operation_id?: OperationId;
    timeout_ms?: TimeoutMs;
  }
  /**
   * Observation-only draft, not a case schema or a general device operation.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "AgentObserveBody".
   */
  export interface AgentObserveBody {
    display_id?: DisplayId;
    for_action?: ForAction;
    ocr?: Ocr;
    request_id: RequestId;
    screenshot?: Screenshot;
    session_id: SessionId;
    timeout_ms?: TimeoutMs1;
    tree?: Tree;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "AgentPrepareBody".
   */
  export interface AgentPrepareBody {
    enable_accessibility?: EnableAccessibility;
    install?: Install;
    manual_recovery_confirmed?: ManualRecoveryConfirmed1;
    operation_id?: OperationId1;
    timeout_ms?: TimeoutMs2;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ControlAcquireBody".
   */
  export interface ControlAcquireBody {
    run_id?: RunId;
    session_id: SessionId1;
    source: Source;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ControlRecoveryBody".
   */
  export interface ControlRecoveryBody {
    confirmed: Confirmed;
    session_id: SessionId2;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ControlReleaseBody".
   */
  export interface ControlReleaseBody {
    control_epoch: ControlEpoch;
    run_id?: RunId1;
    session_id: SessionId3;
    wait_for_idle_ms?: WaitForIdleMs;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ControlTakeoverBody".
   */
  export interface ControlTakeoverBody {
    run_id?: RunId2;
    session_id: SessionId4;
    source: Source1;
    timeout_ms?: TimeoutMs3;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "DebugBreakpointsRequest".
   */
  export interface DebugBreakpointsRequest {
    breakpoints?: Breakpoints;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "DebugSnapshot".
   */
  export interface DebugSnapshot {
    breakpoints: Breakpoints1;
    current?: RunCurrent | null;
    paused: Paused;
    run_id: RunId3;
    state: State;
    step_mode: StepMode;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunCurrent".
   */
  export interface RunCurrent {
    id: Id;
    kind: Kind;
    type?: Type;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "DebugState".
   */
  export interface DebugState {
    breakpoints?: Breakpoints2;
    current?: RunCurrent | null;
    paused?: Paused1;
    step_mode?: StepMode1;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "EventEnvelope".
   */
  export interface EventEnvelope {
    body: Body;
    device_id?: DeviceId;
    msg_id: MsgId;
    run_id?: RunId4;
    seq: Seq;
    ts: Ts;
    type: Type1;
    v?: V;
    [k: string]: unknown | undefined;
  }
  export interface Body {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "EventPage".
   */
  export interface EventPage {
    events: Events;
    gap: Gap;
    latest_seq: LatestSeq;
    oldest_seq: OldestSeq;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "EvidenceSummary".
   */
  export interface EvidenceSummary {
    directory?: Directory;
    issues?: Issues;
    status?: Status;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "HTTPValidationError".
   */
  export interface HTTPValidationError {
    detail?: Detail;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ValidationError".
   */
  export interface ValidationError {
    ctx?: Context;
    input?: Input;
    loc: Location;
    msg: Message;
    type: ErrorType;
    [k: string]: unknown | undefined;
  }
  export interface Context {
    [k: string]: unknown | undefined;
  }
  export interface Input {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "HealthResponse".
   */
  export interface HealthResponse {
    pid: Pid;
    status: Status1;
    uptime_s: UptimeS;
    version: Version;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ImageResource".
   */
  export interface ImageResource {
    png_base64: PngBase64;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "InlineLocator".
   */
  export interface InlineLocator {
    locator: Locator;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Locator".
   */
  export interface Locator {
    candidates: Candidates;
    expect: LocatorExpect;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocatorCandidate".
   */
  export interface LocatorCandidate {
    by: By;
    scope?: Identifier | null;
    value: Text;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "PointCandidate".
   */
  export interface PointCandidate {
    by: By1;
    scope?: Identifier | null;
    value: Point;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "Point".
   */
  export interface Point {
    height: PositiveInt;
    width: PositiveInt;
    x: NonNegativeInt;
    y: NonNegativeInt;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocatorExpect".
   */
  export interface LocatorExpect {
    unique: Unique;
    visible: Visible;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "IssueModel".
   */
  export interface IssueModel {
    code: Code;
    message: Message1;
    path: Path;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateActionContext".
   */
  export interface LocateActionContext {
    atomic_with_observation: AtomicWithObservation;
    error: LocateError | null;
    geometry: LocateActionGeometry | null;
    source: Source2;
    status: Status2;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateError".
   */
  export interface LocateError {
    code: Code1;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateActionGeometry".
   */
  export interface LocateActionGeometry {
    display_id: DisplayId1;
    height: Height;
    rotation: Rotation;
    width: Width;
  }
  /**
   * Only a native display target can be returned by the existing resolver.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateActionTarget".
   */
  export interface LocateActionTarget {
    coordinate_space: CoordinateSpace;
    display_id: DisplayId2;
    expected_package: ExpectedPackage;
    node_path: NodePath;
    observation_id: ObservationId;
    source: Source3;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateAttempt".
   */
  export interface LocateAttempt {
    by: By2;
    error: LocateError | null;
    index: Index;
    match_count: MatchCount;
    status: Status3;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateBounds".
   */
  export interface LocateBounds {
    bottom: Bottom;
    left: Left;
    right: Right;
    top: Top;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateCompleteness".
   */
  export interface LocateCompleteness {
    reasons: Reasons;
    status: Status4;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateCoordinateMapping".
   */
  export interface LocateCoordinateMapping {
    error: LocateError | null;
    status: Status5;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateFraming".
   */
  export interface LocateFraming {
    eof_received: EofReceived;
    protocol_version: ProtocolVersion;
    source_data_validated: SourceDataValidated;
    terminal_received: TerminalReceived;
    validated: Validated;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateHttpError".
   */
  export interface LocateHttpError {
    error: Error;
  }
  /**
   * Public match metadata; text is available only to the Runtime-internal probe.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateMatch".
   */
  export interface LocateMatch {
    block_index: BlockIndex;
    bounds: LocateBounds | null;
    coordinate_space: CoordinateSpace1;
    display_id: DisplayId3;
    node_path: NodePath1;
    score: Score;
    source: Source4;
    states: LocateStates;
    visibility_evidence: VisibilityEvidence;
    visible: Visible2;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateStates".
   */
  export interface LocateStates {
    checkable: Checkable;
    checked: Checked;
    clickable: Clickable;
    editable: Editable;
    enabled: Enabled;
    focused: Focused;
    long_clickable: LongClickable;
    password: Password;
    scrollable: Scrollable;
    selected: Selected;
    showing_hint_text: ShowingHintText;
    visible: Visible1;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateObservation".
   */
  export interface LocateObservation {
    action_context: LocateActionContext;
    agent_lifecycle_id: AgentLifecycleId;
    atomic_snapshot: AtomicSnapshot;
    completeness: LocateCompleteness;
    error: LocateError | null;
    framing: LocateFraming;
    requested: LocateRequestedSources;
    requested_display_id: RequestedDisplayId;
    revision_end: RevisionEnd;
    revision_start: RevisionStart;
    runtime_time: LocateRuntimeTime;
    sources: LocateObservationSources;
    status: Status8;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateRequestedSources".
   */
  export interface LocateRequestedSources {
    ocr: Ocr1;
    screenshot: Screenshot1;
    tree: Tree1;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateRuntimeTime".
   */
  export interface LocateRuntimeTime {
    duration_ms: DurationMs;
    finished_at_epoch_ms: FinishedAtEpochMs;
    started_at_epoch_ms: StartedAtEpochMs;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateObservationSources".
   */
  export interface LocateObservationSources {
    ocr: LocateObservationSource;
    screenshot: LocateObservationSource;
    tree: LocateObservationSource;
  }
  /**
   * Source-local evidence without the tree/OCR/bitmap data payload.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateObservationSource".
   */
  export interface LocateObservationSource {
    capability: Capability;
    collection?: LocateSourceCollection | null;
    completeness: LocateCompleteness;
    coordinates: LocateSourceCoordinates;
    error: LocateSourceError | null;
    requested: Requested;
    scope: LocateSourceScope;
    source: Source5;
    status: Status7;
    time: LocateSourceTime;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateSourceCollection".
   */
  export interface LocateSourceCollection {
    error: LocateSourceError | null;
    purpose: Purpose;
    status: Status6;
    time: LocateSourceTime;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateSourceError".
   */
  export interface LocateSourceError {
    cause_code?: CauseCode;
    code: Code2;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateSourceTime".
   */
  export interface LocateSourceTime {
    calibrated: Calibrated;
    capture_clock?: CaptureClock;
    capture_clock_calibrated?: CaptureClockCalibrated;
    captured_at_uptime_ms?: CapturedAtUptimeMs;
    clock: Clock;
    finished_at_uptime_ms: FinishedAtUptimeMs;
    started_at_uptime_ms: StartedAtUptimeMs;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateSourceCoordinates".
   */
  export interface LocateSourceCoordinates {
    coordinate_space: CoordinateSpace2;
    display_id: DisplayId4;
    height: Height1;
    original_height: OriginalHeight;
    original_width: OriginalWidth;
    requested_display_id: RequestedDisplayId1;
    rotation: Rotation1;
    scale_x: ScaleX;
    scale_y: ScaleY;
    to_display?: LocateCoordinateMapping | null;
    to_original_bitmap?: LocateCoordinateMapping | null;
    width: Width1;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateSourceScope".
   */
  export interface LocateSourceScope {
    display_id: DisplayId5;
    kind: Kind1;
    target_app_verified: TargetAppVerified;
    window_id: WindowId;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocatePoint".
   */
  export interface LocatePoint {
    x: X;
    y: Y;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateRejected".
   */
  export interface LocateRejected {
    error: Error1;
    status: Status9;
  }
  /**
   * HTTP envelope with the resolver's existing validation and defaults unchanged.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateRequest".
   */
  export interface LocateRequest {
    display_id?: DisplayId6;
    expected_package: ExpectedPackage1;
    image_threshold?: ImageThreshold;
    locators?: Locators;
    request_id: RequestId1;
    resources?: Resources;
    serial: Serial;
    session_id: SessionId5;
    target: Target;
    timeout_ms?: TimeoutMs4;
  }
  export interface Locators {
    [k: string]: Locator | undefined;
  }
  export interface Resources {
    [k: string]: ImageResource | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocatorRef".
   */
  export interface LocatorRef {
    locator_ref: Identifier;
  }
  /**
   * One locator query, not an action receipt or a business assertion.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "LocateResult".
   */
  export interface LocateResult {
    action_target: LocateActionTarget | null;
    action_target_error: LocateError | null;
    attempts: Attempts;
    automatic_replay: AutomaticReplay;
    business_success: BusinessSuccess;
    candidate_by: CandidateBy;
    candidate_index: CandidateIndex;
    contract: Contract;
    error: LocateError | null;
    match_count: MatchCount1;
    matches: Matches;
    observation: LocateObservation;
    observation_id: ObservationId1;
    point: LocatePoint | null;
    query_complete: QueryComplete;
    request_id: RequestId2;
    serial: Serial1;
    session_id: SessionId6;
    status: Status10;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "PreflightRequest".
   */
  export interface PreflightRequest {
    case_id: CaseId;
    inputs?: Inputs;
    model_available?: ModelAvailable;
    profile_id?: ProfileId;
    strategy?: Strategy | null;
  }
  export interface Inputs {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "PreflightResponse".
   */
  export interface PreflightResponse {
    ai_requirements: AiRequirements;
    issues: Issues1;
    offline_ready: OfflineReady;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ProjectDetail".
   */
  export interface ProjectDetail {
    allowed_apps: AllowedApps;
    case_ids: CaseIds;
    contract?: Contract1;
    data_ids: DataIds;
    default_strategy: Strategy;
    entry_cases: EntryCases;
    flow_ids: FlowIds;
    manifest: Manifest;
    name: Name;
    platforms: Platforms;
    profile_ids: ProfileIds;
    project_id: ProjectId;
    required_capabilities: RequiredCapabilities;
    revision: Revision;
    root: Root;
    [k: string]: unknown | undefined;
  }
  export interface Manifest {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ProjectList".
   */
  export interface ProjectList {
    projects: Projects;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ProjectSummary".
   */
  export interface ProjectSummary {
    allowed_apps: AllowedApps1;
    case_ids: CaseIds1;
    contract?: Contract2;
    data_ids: DataIds1;
    default_strategy: Strategy;
    entry_cases: EntryCases1;
    flow_ids: FlowIds1;
    name: Name1;
    platforms: Platforms1;
    profile_ids: ProfileIds1;
    project_id: ProjectId1;
    required_capabilities: RequiredCapabilities1;
    revision: Revision1;
    root: Root1;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "ProjectOpenRequest".
   */
  export interface ProjectOpenRequest {
    path: Path1;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RecordedAction".
   */
  export interface RecordedAction {
    action_completed: ActionCompleted;
    action_id: ActionId;
    kind: Kind2;
    operation_id: OperationId2;
    params: Params;
    post_observation_id?: PostObservationId;
    recorded_at_ms: RecordedAtMs;
    source: Source6;
    status: Status11;
    target_observation_id: TargetObservationId;
    target_source: TargetSource;
    [k: string]: unknown | undefined;
  }
  export interface Params {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RecordingDetail".
   */
  export interface RecordingDetail {
    contract?: Contract3;
    created_at_ms: CreatedAtMs;
    draft?: Draft;
    event_count?: EventCount;
    events?: Events1;
    recording_id: RecordingId;
    serial: Serial2;
    session_id: SessionId7;
    state: State1;
    updated_at_ms: UpdatedAtMs;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RecordingList".
   */
  export interface RecordingList {
    recordings: Recordings;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RecordingSummary".
   */
  export interface RecordingSummary {
    contract?: Contract4;
    created_at_ms: CreatedAtMs1;
    event_count?: EventCount1;
    recording_id: RecordingId1;
    serial: Serial3;
    session_id: SessionId8;
    state: State2;
    updated_at_ms: UpdatedAtMs1;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RecordingStartRequest".
   */
  export interface RecordingStartRequest {
    serial: Serial4;
    session_id: SessionId9;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RelayBody".
   */
  export interface RelayBody {
    bit_rate?: BitRate;
    channel: Channel;
    max_fps?: MaxFps;
    max_size?: MaxSize;
    relay_url: RelayUrl;
    token: Token;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RelayKeepalive".
   */
  export interface RelayKeepalive {
    publisher_id: PublisherId;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunDetail".
   */
  export interface RunDetail {
    business_success?: BusinessSuccess1;
    case_id: CaseId1;
    contract?: Contract5;
    created_at_ms: CreatedAtMs2;
    debug?: DebugState;
    device: Device;
    elapsed_ms?: ElapsedMs;
    error?: RunError | null;
    evidence?: EvidenceSummary;
    finished_at_ms?: FinishedAtMs;
    profile_id: ProfileId1;
    project_id: ProjectId2;
    reason?: Reason;
    result?: Result;
    run_id: RunId5;
    session_id?: SessionId10;
    started_at_ms?: StartedAtMs;
    state: State3;
    strategy: Strategy;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunError".
   */
  export interface RunError {
    code: Code3;
    message?: Message2;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunList".
   */
  export interface RunList {
    runs: Runs;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunSummary".
   */
  export interface RunSummary {
    business_success?: BusinessSuccess2;
    case_id: CaseId2;
    contract?: Contract6;
    created_at_ms: CreatedAtMs3;
    debug?: DebugState;
    device: Device1;
    elapsed_ms?: ElapsedMs1;
    error?: RunError | null;
    evidence?: EvidenceSummary;
    finished_at_ms?: FinishedAtMs1;
    profile_id: ProfileId2;
    project_id: ProjectId3;
    reason?: Reason1;
    run_id: RunId6;
    started_at_ms?: StartedAtMs1;
    state: State4;
    strategy: Strategy;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunRequest".
   */
  export interface RunRequest {
    activities?: Activities;
    allow_clear?: AllowClear;
    auto_connect?: AutoConnect;
    breakpoints?: Breakpoints3;
    case_id: CaseId3;
    connect_timeout_ms?: ConnectTimeoutMs;
    device: Device2;
    include_screenshot?: IncludeScreenshot;
    inputs?: Inputs1;
    profile_id?: ProfileId3;
    project_id: ProjectId4;
    strategy?: Strategy | null;
  }
  export interface Activities {
    [k: string]: string | undefined;
  }
  export interface Inputs1 {
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "RunResultResponse".
   */
  export interface RunResultResponse {
    result?: Result1;
    run_id: RunId7;
    state: State5;
    [k: string]: unknown | undefined;
  }
  /**
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "TargetAppDiagnostic".
   */
  export interface TargetAppDiagnostic {
    code: Code4;
    guidance: Guidance;
  }
  /**
   * Installation evidence only, not Agent readiness or target-case/build verification.
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "TargetAppResponse".
   */
  export interface TargetAppResponse {
    diagnostic: TargetAppDiagnostic;
    observed_at_ms: ObservedAtMs;
    package: Package;
    serial: Serial5;
    status: Status12;
    version_code: VersionCode;
    version_name: VersionName;
  }
  /**
   * Debug-only visible change for latency measurement (no general control channel: P5).
   *
   * This interface was referenced by `PulseRuntimeApiSchemas`'s JSON-Schema
   * via the `definition` "TriggerBody".
   */
  export interface TriggerBody {
    code?: Code5;
    kind: Kind3;
    x?: X1;
    y?: Y1;
    [k: string]: unknown | undefined;
  }
}

export interface RuntimeApiClientOptions {
  baseUrl: string;
  token: string;
  fetch?: typeof fetch;
}

export class RuntimeApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`Runtime API HTTP ${status}`);
    this.name = 'RuntimeApiError';
    this.status = status;
    this.detail = detail;
  }
}

export class RuntimeApiClient {
  constructor(private readonly options: RuntimeApiClientOptions) {}

  private async request<T>(
    method: string,
    path: string,
    query?: Record<string, unknown>,
    body?: unknown,
  ): Promise<T> {
    const url = new URL(path, this.options.baseUrl);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
      }
    }
    const headers: Record<string, string> = { Authorization: `Bearer ${this.options.token}` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const doFetch = this.options.fetch ?? fetch;
    const response = await doFetch(url, init);
    if (!response.ok) {
      const text = await response.text();
      let detail: unknown = text;
      try {
        detail = JSON.parse(text);
      } catch {
        // Preserve plain-text and malformed JSON errors without re-reading the response body.
      }
      throw new RuntimeApiError(response.status, detail);
    }
    if (response.status === 204) return undefined as T;
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.includes('application/json')) return (await response.text()) as T;
    return (await response.json()) as T;
  }

  async getStreamDebug(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>('GET', `/debug/stream`);
  }

  async startRelay(
    serial: string,
    body: PulseRuntimeApiSchemas.RelayBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/debug/stream/${encodeURIComponent(serial)}/relay`,
      undefined,
      body,
    );
  }

  async stopRelay(
    serial: string,
    query?: { publisher_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'DELETE',
      `/debug/stream/${encodeURIComponent(serial)}/relay`,
      query,
    );
  }

  async renewRelay(
    serial: string,
    body: PulseRuntimeApiSchemas.RelayKeepalive,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/debug/stream/${encodeURIComponent(serial)}/relay/keepalive`,
      undefined,
      body,
    );
  }

  async triggerStream(
    serial: string,
    body: PulseRuntimeApiSchemas.TriggerBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/debug/stream/${encodeURIComponent(serial)}/trigger`,
      undefined,
      body,
    );
  }

  async listDevices(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>('GET', `/devices`);
  }

  async getDevice(serial: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>('GET', `/devices/${encodeURIComponent(serial)}`);
  }

  async performAction(serial: string): Promise<unknown> {
    return this.request<unknown>('POST', `/devices/${encodeURIComponent(serial)}/agent/action`);
  }

  async cancelAction(
    serial: string,
    query?: { session_id: string; request_id: string; operation_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/action/cancel`,
      query,
    );
  }

  async cancelConnection(
    serial: string,
    query?: { operation_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/cancel`,
      query,
    );
  }

  async getAgentCapabilities(
    serial: string,
    query?: { session_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/agent/capabilities`,
      query,
    );
  }

  async connectAgent(
    serial: string,
    body: PulseRuntimeApiSchemas.AgentConnectBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/connect`,
      undefined,
      body,
    );
  }

  async disconnectAgent(
    serial: string,
    query?: { session_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'DELETE',
      `/devices/${encodeURIComponent(serial)}/agent/connection`,
      query,
    );
  }

  async getControl(
    serial: string,
    query?: { session_id?: string | null },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/agent/control`,
      query,
    );
  }

  async acquireControl(
    serial: string,
    body: PulseRuntimeApiSchemas.ControlAcquireBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/control/acquire`,
      undefined,
      body,
    );
  }

  async acknowledgeRecovery(
    serial: string,
    body: PulseRuntimeApiSchemas.ControlRecoveryBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/control/recovery`,
      undefined,
      body,
    );
  }

  async releaseControl(
    serial: string,
    body: PulseRuntimeApiSchemas.ControlReleaseBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/control/release`,
      undefined,
      body,
    );
  }

  async takeOverControl(
    serial: string,
    body: PulseRuntimeApiSchemas.ControlTakeoverBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/control/takeover`,
      undefined,
      body,
    );
  }

  async getAgentGeometry(
    serial: string,
    query?: { session_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/agent/geometry`,
      query,
    );
  }

  async getAgentHealth(
    serial: string,
    query?: { session_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/agent/health`,
      query,
    );
  }

  async listLedgerActions(
    serial: string,
    query?: { limit?: number },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/agent/ledger`,
      query,
    );
  }

  async locate(
    serial: string,
    body: PulseRuntimeApiSchemas.LocateRequest,
  ): Promise<PulseRuntimeApiSchemas.LocateResult> {
    return this.request<PulseRuntimeApiSchemas.LocateResult>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/locate`,
      undefined,
      body,
    );
  }

  async cancelLocate(
    serial: string,
    query?: { session_id: string; request_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/locate/cancel`,
      query,
    );
  }

  async observeAgent(
    serial: string,
    body: PulseRuntimeApiSchemas.AgentObserveBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/observe`,
      undefined,
      body,
    );
  }

  async cancelObservation(
    serial: string,
    query?: { session_id: string; request_id: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/observe/cancel`,
      query,
    );
  }

  async prepareAgent(
    serial: string,
    body: PulseRuntimeApiSchemas.AgentPrepareBody,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'POST',
      `/devices/${encodeURIComponent(serial)}/agent/prepare`,
      undefined,
      body,
    );
  }

  async getTargetApp(serial: string): Promise<PulseRuntimeApiSchemas.TargetAppResponse> {
    return this.request<PulseRuntimeApiSchemas.TargetAppResponse>(
      'GET',
      `/devices/${encodeURIComponent(serial)}/target-app`,
    );
  }

  async getEvents(query?: {
    since?: number;
    run_id?: string | null;
    limit?: number;
  }): Promise<PulseRuntimeApiSchemas.EventPage> {
    return this.request<PulseRuntimeApiSchemas.EventPage>('GET', `/events`, query);
  }

  async getHealth(): Promise<PulseRuntimeApiSchemas.HealthResponse> {
    return this.request<PulseRuntimeApiSchemas.HealthResponse>('GET', `/health`);
  }

  async listProjects(): Promise<PulseRuntimeApiSchemas.ProjectList> {
    return this.request<PulseRuntimeApiSchemas.ProjectList>('GET', `/projects`);
  }

  async openProject(
    body: PulseRuntimeApiSchemas.ProjectOpenRequest,
  ): Promise<PulseRuntimeApiSchemas.ProjectDetail> {
    return this.request<PulseRuntimeApiSchemas.ProjectDetail>('POST', `/projects`, undefined, body);
  }

  async getProject(project_id: string): Promise<PulseRuntimeApiSchemas.ProjectDetail> {
    return this.request<PulseRuntimeApiSchemas.ProjectDetail>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}`,
    );
  }

  async closeProject(project_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'DELETE',
      `/projects/${encodeURIComponent(project_id)}`,
    );
  }

  async listCases(project_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}/cases`,
    );
  }

  async getCase(project_id: string, case_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}/cases/${encodeURIComponent(case_id)}`,
    );
  }

  async getDataSet(project_id: string, data_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}/data/${encodeURIComponent(data_id)}`,
    );
  }

  async getFlow(project_id: string, flow_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}/flows/${encodeURIComponent(flow_id)}`,
    );
  }

  async runPreflight(
    project_id: string,
    body: PulseRuntimeApiSchemas.PreflightRequest,
  ): Promise<PulseRuntimeApiSchemas.PreflightResponse> {
    return this.request<PulseRuntimeApiSchemas.PreflightResponse>(
      'POST',
      `/projects/${encodeURIComponent(project_id)}/preflight`,
      undefined,
      body,
    );
  }

  async getProfile(project_id: string, profile_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/projects/${encodeURIComponent(project_id)}/profiles/${encodeURIComponent(profile_id)}`,
    );
  }

  async listRecordings(): Promise<PulseRuntimeApiSchemas.RecordingList> {
    return this.request<PulseRuntimeApiSchemas.RecordingList>('GET', `/recordings`);
  }

  async startRecording(
    body: PulseRuntimeApiSchemas.RecordingStartRequest,
  ): Promise<PulseRuntimeApiSchemas.RecordingSummary> {
    return this.request<PulseRuntimeApiSchemas.RecordingSummary>(
      'POST',
      `/recordings`,
      undefined,
      body,
    );
  }

  async getRecording(recording_id: string): Promise<PulseRuntimeApiSchemas.RecordingDetail> {
    return this.request<PulseRuntimeApiSchemas.RecordingDetail>(
      'GET',
      `/recordings/${encodeURIComponent(recording_id)}`,
    );
  }

  async pauseRecording(recording_id: string): Promise<PulseRuntimeApiSchemas.RecordingSummary> {
    return this.request<PulseRuntimeApiSchemas.RecordingSummary>(
      'POST',
      `/recordings/${encodeURIComponent(recording_id)}/pause`,
    );
  }

  async resumeRecording(recording_id: string): Promise<PulseRuntimeApiSchemas.RecordingSummary> {
    return this.request<PulseRuntimeApiSchemas.RecordingSummary>(
      'POST',
      `/recordings/${encodeURIComponent(recording_id)}/resume`,
    );
  }

  async stopRecording(recording_id: string): Promise<PulseRuntimeApiSchemas.RecordingDetail> {
    return this.request<PulseRuntimeApiSchemas.RecordingDetail>(
      'POST',
      `/recordings/${encodeURIComponent(recording_id)}/stop`,
    );
  }

  async listRuns(): Promise<PulseRuntimeApiSchemas.RunList> {
    return this.request<PulseRuntimeApiSchemas.RunList>('GET', `/runs`);
  }

  async startRun(
    body: PulseRuntimeApiSchemas.RunRequest,
  ): Promise<PulseRuntimeApiSchemas.RunSummary> {
    return this.request<PulseRuntimeApiSchemas.RunSummary>('POST', `/runs`, undefined, body);
  }

  async getRun(run_id: string): Promise<PulseRuntimeApiSchemas.RunDetail> {
    return this.request<PulseRuntimeApiSchemas.RunDetail>(
      'GET',
      `/runs/${encodeURIComponent(run_id)}`,
    );
  }

  async getDebug(run_id: string): Promise<PulseRuntimeApiSchemas.DebugSnapshot> {
    return this.request<PulseRuntimeApiSchemas.DebugSnapshot>(
      'GET',
      `/runs/${encodeURIComponent(run_id)}/debug`,
    );
  }

  async setBreakpoints(
    run_id: string,
    body: PulseRuntimeApiSchemas.DebugBreakpointsRequest,
  ): Promise<PulseRuntimeApiSchemas.DebugSnapshot> {
    return this.request<PulseRuntimeApiSchemas.DebugSnapshot>(
      'PUT',
      `/runs/${encodeURIComponent(run_id)}/debug/breakpoints`,
      undefined,
      body,
    );
  }

  async pauseRun(run_id: string): Promise<PulseRuntimeApiSchemas.DebugSnapshot> {
    return this.request<PulseRuntimeApiSchemas.DebugSnapshot>(
      'POST',
      `/runs/${encodeURIComponent(run_id)}/debug/pause`,
    );
  }

  async resumeRun(run_id: string): Promise<PulseRuntimeApiSchemas.DebugSnapshot> {
    return this.request<PulseRuntimeApiSchemas.DebugSnapshot>(
      'POST',
      `/runs/${encodeURIComponent(run_id)}/debug/resume`,
    );
  }

  async stepRun(run_id: string): Promise<PulseRuntimeApiSchemas.DebugSnapshot> {
    return this.request<PulseRuntimeApiSchemas.DebugSnapshot>(
      'POST',
      `/runs/${encodeURIComponent(run_id)}/debug/step`,
    );
  }

  async getRunEvents(
    run_id: string,
    query?: { since?: number; limit?: number },
  ): Promise<PulseRuntimeApiSchemas.EventPage> {
    return this.request<PulseRuntimeApiSchemas.EventPage>(
      'GET',
      `/runs/${encodeURIComponent(run_id)}/events`,
      query,
    );
  }

  async getRunReport(run_id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      'GET',
      `/runs/${encodeURIComponent(run_id)}/report`,
    );
  }

  async getRunReportHtml(run_id: string): Promise<string> {
    return this.request<string>('GET', `/runs/${encodeURIComponent(run_id)}/report/html`);
  }

  async getRunResult(run_id: string): Promise<PulseRuntimeApiSchemas.RunResultResponse> {
    return this.request<PulseRuntimeApiSchemas.RunResultResponse>(
      'GET',
      `/runs/${encodeURIComponent(run_id)}/result`,
    );
  }

  async stopRun(run_id: string): Promise<PulseRuntimeApiSchemas.RunSummary> {
    return this.request<PulseRuntimeApiSchemas.RunSummary>(
      'POST',
      `/runs/${encodeURIComponent(run_id)}/stop`,
    );
  }
}

export interface RuntimeEventStreamOptions {
  baseUrl: string;
  token: string;
  since?: number;
  runId?: string;
}

export function runtimeEventStreamUrl(options: RuntimeEventStreamOptions): string {
  const url = new URL('/ws/events', options.baseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', options.token);
  if (options.since !== undefined) url.searchParams.set('since', String(options.since));
  if (options.runId !== undefined) url.searchParams.set('run_id', options.runId);
  return url.toString();
}
