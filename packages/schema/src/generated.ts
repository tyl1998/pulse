/* Generated from Pydantic via JSON Schema. Do not edit. Offline draft only. */

export type PulseDocument = Manifest | Case | Flow | DataSet | Profile;
export type $Schema = 'urn:pulse:schema:1-draft:manifest';
export type CasePath = string;
/**
 * @minItems 1
 */
export type AllowedApps = [Text, ...Text[]];
export type Text = string;
export type Identifier = string;
export type ForbiddenActions = Identifier[];
export type DataPath = string;
export type Strategy = 'rule_only' | 'ai_on_locator_failure' | 'ai_led';
export type Kind = 'runtime' | 'protocol' | 'driver' | 'model' | 'network' | 'data';
export type Dependencies = Dependency[];
/**
 * @minItems 1
 */
export type EntryCases = [Identifier, ...Identifier[]];
export type FlowPath = string;
/**
 * @minItems 1
 */
export type Platforms = [Platform, ...Platform[]];
export type Platform = 'android' | 'ios';
export type ProfilePath = string;
export type RequiredCapabilities = Text[];
export type ResourcePath = string;
export type SecretRefs = Identifier[];
export type $Schema1 = 'urn:pulse:schema:1-draft:case';
export type Predicate =
  | PresenceCondition
  | AbsenceCondition
  | StateCondition
  | TextCondition
  | SemanticCondition
  | CountCondition
  | NumberCondition;
export type Operator = 'exists' | 'page_is';
export type ObservationSource = 'native' | 'webview' | 'ocr' | 'vision';
export type Target = LocatorRef | InlineLocator;
/**
 * @minItems 1
 */
export type Candidates = [
  LocatorCandidate | PointCandidate,
  ...(LocatorCandidate | PointCandidate)[],
];
export type By = 'res_id' | 'text' | 'desc' | 'xpath' | 'ocr_text' | 'image';
export type By1 = 'point';
export type PositiveInt = number;
export type NonNegativeInt = number;
export type Unique = boolean;
export type Visible = boolean;
export type Operator1 = 'not_exists';
export type Expected = boolean;
export type Operator2 = 'visible' | 'enabled' | 'checked';
export type TextExpected = string | ParameterRef | DataRef;
export type Operator3 = 'text_equals' | 'contains' | 'regex';
export type Operator4 = 'semantic';
export type CountExpected = NonNegativeInt | ParameterRef | DataRef;
export type Operator5 = 'count_eq' | 'count_gt' | 'count_lt';
export type Comparison = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte';
export type NumberExpected = Number | ParameterRef | DataRef;
export type Number = number;
export type Operator6 = 'number_cmp';
export type Required = boolean;
export type Checks = Checkpoint[];
export type Value = Scalar | ParameterRef | VariableRef | DataRef | SecretRef;
export type Scalar = string | Integer | Number | boolean;
export type Integer = number;
export type OriginalText = string | null;
export type Required1 = boolean;
export type ScalarType = 'string' | 'integer' | 'number' | 'boolean';
export type Preconditions = Checkpoint[];
/**
 * @minItems 1
 */
export type ProfileRefs = [Identifier, ...Identifier[]];
/**
 * @minItems 1
 */
export type Steps = [Step, ...Step[]];
export type Step =
  ActionStep | FlowStep | WaitStep | CheckStep | ExtractStep | IfStep | LoopStep | AiStep;
export type Action = TapAction | LongAction | InputAction | SwipeAction | KeyAction | AppAction;
export type Kind1 = 'tap';
export type Kind2 = 'long';
export type Kind3 = 'input';
export type Mode = 'replace' | 'append';
export type TextValue = TextExpected | VariableRef | SecretRef;
export type Kind4 = 'swipe';
export type Kind5 = 'key';
export type Kind6 = 'launch' | 'stop' | 'clear';
export type Type = 'action';
export type Type1 = 'flow';
export type Type2 = 'wait';
export type Type3 = 'check';
export type Attribute = 'text' | 'count' | 'enabled' | 'checked';
export type Type4 = 'extract';
export type Else = Step[];
/**
 * @minItems 1
 */
export type Then = [Step, ...Step[]];
export type Type5 = 'if';
/**
 * @minItems 1
 */
export type Steps1 = [Step, ...Step[]];
export type Type6 = 'loop';
/**
 * @minItems 1
 */
export type Checks1 = [Checkpoint, ...Checkpoint[]];
export type Type7 = 'ai_step';
export type $Schema2 = 'urn:pulse:schema:1-draft:flow';
/**
 * @minItems 1
 */
export type Steps2 = [Step, ...Step[]];
export type $Schema3 = 'urn:pulse:schema:1-draft:data';
export type $Schema4 = 'urn:pulse:schema:1-draft:profile';
export type Capabilities = Text[];

export interface Manifest {
  $schema: $Schema;
  budgets?: Budgets;
  cases: Cases;
  constraints: Constraints;
  data: Data;
  default_strategy: Strategy;
  dependencies: Dependencies;
  entry_cases: EntryCases;
  flows: Flows;
  name: Text;
  platforms: Platforms;
  profiles: Profiles;
  project_id: Identifier;
  required_capabilities: RequiredCapabilities;
  resources?: Resources;
  revision: Text;
  secret_refs?: SecretRefs;
}
export interface Budgets {
  ai_case_model_calls?: number;
  authorization_timeout_ms?: number;
  case_timeout_ms?: number;
  model_request_timeout_ms?: number;
  recovery_actions?: number;
  recovery_model_calls?: number;
  recovery_timeout_ms?: number;
  stop_timeout_ms?: number;
  suite_timeout_ms?: number;
}
export interface Cases {
  [k: string]: CasePath | undefined;
}
export interface Constraints {
  allowed_apps: AllowedApps;
  forbidden_actions: ForbiddenActions;
}
export interface Data {
  [k: string]: DataPath | undefined;
}
export interface Dependency {
  kind: Kind;
  ref: Identifier;
  version?: Text | null;
}
export interface Flows {
  [k: string]: FlowPath | undefined;
}
export interface Profiles {
  [k: string]: ProfilePath | undefined;
}
export interface Resources {
  [k: string]: ResourcePath | undefined;
}
export interface Case {
  $schema: $Schema1;
  case_id: Identifier;
  checks: Checks;
  goal: Text;
  inputs: Inputs;
  locators: Locators;
  name: Text;
  original_text?: OriginalText;
  parameters: Parameters;
  preconditions: Preconditions;
  profile_refs: ProfileRefs;
  source?: Text | null;
  steps: Steps;
}
export interface Checkpoint {
  condition: Predicate;
  id: Identifier;
  required: Required;
}
export interface PresenceCondition {
  operator: Operator;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
}
export interface LocatorRef {
  locator_ref: Identifier;
}
export interface InlineLocator {
  locator: Locator;
}
export interface Locator {
  candidates: Candidates;
  expect: LocatorExpect;
}
export interface LocatorCandidate {
  by: By;
  scope?: Identifier | null;
  value: Text;
}
export interface PointCandidate {
  by: By1;
  scope?: Identifier | null;
  value: Point;
}
export interface Point {
  height: PositiveInt;
  width: PositiveInt;
  x: NonNegativeInt;
  y: NonNegativeInt;
}
export interface LocatorExpect {
  unique: Unique;
  visible: Visible;
}
export interface AbsenceCondition {
  observation_window_ms: PositiveInt;
  operator: Operator1;
  poll_interval_ms?: number;
  scope: Target;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
}
export interface StateCondition {
  expected: Expected;
  operator: Operator2;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
}
export interface TextCondition {
  expected: TextExpected;
  operator: Operator3;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
}
export interface ParameterRef {
  param_ref: Identifier;
}
export interface DataRef {
  data_ref: Identifier;
  key: Identifier;
}
export interface SemanticCondition {
  expected: TextExpected;
  operator: Operator4;
  source: ObservationSource;
  target: Target;
  timeout_ms: PositiveInt;
}
export interface CountCondition {
  expected: CountExpected;
  operator: Operator5;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
}
export interface NumberCondition {
  comparison: Comparison;
  expected: NumberExpected;
  operator: Operator6;
  precision: NonNegativeInt;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
  unit: Text;
}
export interface Inputs {
  [k: string]: Value | undefined;
}
export interface VariableRef {
  var_ref: Identifier;
}
export interface SecretRef {
  secret_ref: Identifier;
}
export interface Locators {
  [k: string]: Locator | undefined;
}
export interface Parameters {
  [k: string]: Parameter | undefined;
}
export interface Parameter {
  default?: Scalar | null;
  required: Required1;
  type: ScalarType;
}
export interface ActionStep {
  action: Action;
  id: Identifier;
  timeout_ms?: number;
  type: Type;
}
export interface TapAction {
  kind: Kind1;
  target: Target;
}
export interface LongAction {
  duration_ms: PositiveInt;
  kind: Kind2;
  target: Target;
}
export interface InputAction {
  kind: Kind3;
  mode: Mode;
  target: Target;
  text: TextValue;
}
export interface SwipeAction {
  duration_ms: PositiveInt;
  end: Point;
  kind: Kind4;
  start: Point;
}
export interface KeyAction {
  key: Text;
  kind: Kind5;
}
export interface AppAction {
  app_id: Text;
  kind: Kind6;
}
export interface FlowStep {
  arguments?: Arguments;
  bind?: Bind;
  flow_ref: Identifier;
  id: Identifier;
  timeout_ms?: number;
  type: Type1;
}
export interface Arguments {
  [k: string]: Value | undefined;
}
export interface Bind {
  [k: string]: Identifier | undefined;
}
export interface WaitStep {
  condition: Predicate;
  id: Identifier;
  timeout_ms?: number;
  type: Type2;
}
export interface CheckStep {
  checkpoint: Checkpoint;
  id: Identifier;
  timeout_ms?: number;
  type: Type3;
}
export interface ExtractStep {
  as: Identifier;
  attribute: Attribute;
  id: Identifier;
  source: ObservationSource;
  target: Target;
  timeout_ms?: number;
  type: Type4;
}
export interface IfStep {
  condition: Predicate;
  else?: Else;
  id: Identifier;
  then: Then;
  timeout_ms?: number;
  type: Type5;
}
export interface LoopStep {
  condition?: Predicate | null;
  id: Identifier;
  max_iterations: PositiveInt;
  steps: Steps1;
  timeout_ms?: number;
  type: Type6;
}
export interface AiStep {
  checks: Checks1;
  goal: Text;
  id: Identifier;
  timeout_ms: PositiveInt;
  type: Type7;
}
export interface Flow {
  $schema: $Schema2;
  flow_id: Identifier;
  locators: Locators1;
  name: Text;
  parameters: Parameters1;
  returns: Returns;
  steps: Steps2;
}
export interface Locators1 {
  [k: string]: Locator | undefined;
}
export interface Parameters1 {
  [k: string]: Parameter | undefined;
}
export interface Returns {
  [k: string]: ReturnValue | undefined;
}
export interface ReturnValue {
  type: ScalarType;
  value: Value;
}
export interface DataSet {
  $schema: $Schema3;
  data_id: Identifier;
  values: Values;
}
export interface Values {
  [k: string]: Scalar | SecretRef | undefined;
}
export interface Profile {
  $schema: $Schema4;
  app_id: Text;
  capabilities: Capabilities;
  environment: Identifier;
  locators: Locators2;
  platform: Platform;
  profile_id: Identifier;
}
export interface Locators2 {
  [k: string]: Locator | undefined;
}

export const CONTRACT_VERSION = '1-draft' as const;
export const CONTRACT_STATUS = 'draft' as const;
export const SCHEMA_IDS = {
  case: 'urn:pulse:schema:1-draft:case',
  data: 'urn:pulse:schema:1-draft:data',
  document: 'urn:pulse:schema:1-draft:document',
  flow: 'urn:pulse:schema:1-draft:flow',
  manifest: 'urn:pulse:schema:1-draft:manifest',
  profile: 'urn:pulse:schema:1-draft:profile',
} as const;
