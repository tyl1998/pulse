// Compile-only structural examples for the draft API, not runtime semantic validation.
import { CONTRACT_STATUS, CONTRACT_VERSION, SCHEMA_IDS, SCHEMA_PACKAGE_VERSION } from './index';
import type {
  Action,
  ActionStep,
  AiStep,
  Case,
  Checkpoint,
  CheckStep,
  DataSet,
  Flow,
  FlowStep,
  IfStep,
  Locator,
  LoopStep,
  Manifest,
  Predicate,
  Profile,
  PulseDocument,
  Step,
  Target,
} from './index';

export const draftVersion: '1-draft' = CONTRACT_VERSION;
export const draftStatus: 'draft' = CONTRACT_STATUS;
export const packageVersion: '0.0.0' = SCHEMA_PACKAGE_VERSION;

const locator = {
  candidates: [{ by: 'res_id', value: 'com.pulse.target:id/input' }],
  expect: { unique: true, visible: true },
} satisfies Locator;
const target = { locator_ref: 'input' } satisfies Target;
const condition = { operator: 'exists', source: 'native', target } satisfies Predicate;
const checkpoint = { id: 'input_present', required: true, condition } satisfies Checkpoint;

export const actions = [
  { kind: 'tap', target },
  { kind: 'long', target, duration_ms: 500 },
  { kind: 'input', target, mode: 'replace', text: 'offline' },
  {
    kind: 'swipe',
    start: { x: 100, y: 200, width: 1080, height: 1920 },
    end: { x: 100, y: 800, width: 1080, height: 1920 },
    duration_ms: 300,
  },
  { kind: 'key', key: 'BACK' },
  { kind: 'launch', app_id: 'com.pulse.target' },
  { kind: 'stop', app_id: 'com.pulse.target' },
  { kind: 'clear', app_id: 'com.pulse.target' },
] satisfies Action[];

export const actionStep = {
  type: 'action',
  id: 'enter_username',
  action: { kind: 'input', target, mode: 'replace', text: { param_ref: 'username' } },
} satisfies ActionStep;

export const flowStep = {
  type: 'flow',
  id: 'call_sign_in',
  flow_ref: 'sign_in',
  arguments: {
    username: { param_ref: 'username' },
    password: { secret_ref: 'password' },
  },
  bind: { status: 'sign_in_status' },
} satisfies FlowStep;

export const checkStep = {
  type: 'check',
  id: 'check_input',
  checkpoint,
} satisfies CheckStep;

export const aiStep = {
  type: 'ai_step',
  id: 'locate_input',
  timeout_ms: 30_000,
  goal: 'Locate the username field',
  checks: [checkpoint],
} satisfies AiStep;

export const loopStep = {
  type: 'loop',
  id: 'bounded_retry',
  max_iterations: 3,
  condition,
  steps: [
    actionStep,
    flowStep,
    checkStep,
    {
      type: 'if',
      id: 'inspect_input',
      condition,
      then: [
        { type: 'wait', id: 'wait_for_input', condition },
        {
          type: 'extract',
          id: 'read_username',
          target,
          source: 'native',
          attribute: 'text',
          as: 'observed_username',
        },
      ],
      else: [aiStep],
    },
  ],
} satisfies LoopStep;

export const ifStep = {
  type: 'if',
  id: 'choose_path',
  condition,
  then: [loopStep],
  else: [],
} satisfies IfStep;

export const recursiveStep: Step = ifStep;

export const caseDocument = {
  $schema: SCHEMA_IDS.case,
  case_id: 'offline_example',
  name: 'Offline example',
  goal: 'Exercise the draft step shapes',
  profile_refs: ['android'],
  parameters: { username: { type: 'string', required: true } },
  inputs: { username: { data_ref: 'account', key: 'username' } },
  locators: { input: locator },
  preconditions: [],
  steps: [recursiveStep],
  checks: [checkpoint],
} satisfies Case;

export const flowDocument = {
  $schema: SCHEMA_IDS.flow,
  flow_id: 'sign_in',
  name: 'Sign in',
  parameters: {
    username: { type: 'string', required: true },
    password: { type: 'string', required: true },
  },
  locators: { input: locator },
  steps: [actionStep, checkStep],
  returns: { status: { type: 'string', value: 'ready' } },
} satisfies Flow;

export const dataDocument = {
  $schema: SCHEMA_IDS.data,
  data_id: 'account',
  values: { username: 'offline', password: { secret_ref: 'password' } },
} satisfies DataSet;

export const profileDocument = {
  $schema: SCHEMA_IDS.profile,
  profile_id: 'android',
  platform: 'android',
  environment: 'offline',
  app_id: 'com.pulse.target',
  capabilities: [],
  locators: { input: locator },
} satisfies Profile;

export const manifest = {
  $schema: SCHEMA_IDS.manifest,
  project_id: 'offline_example',
  name: 'Offline draft example',
  revision: 'draft',
  cases: { offline_example: 'cases/offline_example.json' },
  flows: { sign_in: 'flows/sign_in.json' },
  data: { account: 'data/account.json' },
  profiles: { android: 'profiles/android.json' },
  entry_cases: ['offline_example'],
  default_strategy: 'rule_only',
  platforms: ['android'],
  required_capabilities: [],
  constraints: { allowed_apps: ['com.pulse.target'], forbidden_actions: ['clear'] },
  dependencies: [],
  secret_refs: ['password'],
  budgets: { case_timeout_ms: 60_000 },
} satisfies Manifest;

export const documents = [
  manifest,
  caseDocument,
  flowDocument,
  dataDocument,
  profileDocument,
] satisfies PulseDocument[];

// @ts-expect-error: Package versions are not contract versions.
export const packageAsContract: typeof CONTRACT_VERSION = SCHEMA_PACKAGE_VERSION;
// @ts-expect-error: The draft must not be advertised as a frozen version 1.
export const frozenVersion: typeof CONTRACT_VERSION = '1';

export const wrongManifestVersion = {
  ...manifest,
  // @ts-expect-error: Manifest serialization is explicitly 1-draft, not frozen v1.
  $schema: 'urn:pulse:schema:1:manifest',
} satisfies Manifest;

export const wrongCaseVersion = {
  ...caseDocument,
  // @ts-expect-error: Case documents require the draft case schema ID.
  $schema: 'urn:pulse:schema:1:case',
} satisfies Case;

export const wrongFlowVersion = {
  ...flowDocument,
  // @ts-expect-error: Flow documents require the draft flow schema ID.
  $schema: 'urn:pulse:schema:1:flow',
} satisfies Flow;

export const wrongDataVersion = {
  ...dataDocument,
  // @ts-expect-error: Data documents require the draft data schema ID.
  $schema: 'urn:pulse:schema:1:data',
} satisfies DataSet;

export const wrongProfileVersion = {
  ...profileDocument,
  // @ts-expect-error: Profile documents require the draft profile schema ID.
  $schema: 'urn:pulse:schema:1:profile',
} satisfies Profile;

// @ts-expect-error: A loop requires an explicit max_iterations even through the Step union.
export const unboundedLoop: Step = { type: 'loop', id: 'unbounded', steps: [actionStep] };

export const nestedUnboundedLoop: IfStep = {
  type: 'if',
  id: 'outer_branch',
  condition,
  then: [
    // @ts-expect-error: Recursive Step arrays must still require max_iterations on loops.
    { type: 'loop', id: 'unbounded_child', steps: [actionStep] },
  ],
};

export const wrongInputText: Action = {
  kind: 'input',
  target,
  mode: 'replace',
  // @ts-expect-error: Input text is a string or a typed reference, not a number.
  text: 42,
};

export const wrongTapParams: Action = {
  kind: 'tap',
  target,
  // @ts-expect-error: A duration belongs to long/swipe actions, not tap actions.
  duration_ms: 500,
};

export const wrongLongDuration: Action = {
  kind: 'long',
  target,
  // @ts-expect-error: Durations must be numeric rather than numeric strings.
  duration_ms: '500',
};

export const uncheckedAi: AiStep = {
  type: 'ai_step',
  id: 'unchecked_ai',
  timeout_ms: 30_000,
  goal: 'Locate the username field',
  // @ts-expect-error: AI steps require a non-empty checkpoint list.
  checks: [],
};

// @ts-expect-error: AI timing is explicit, not an undocumented rule-step default.
export const missingAiDeadline: AiStep = {
  type: 'ai_step',
  id: 'ai',
  goal: 'Inspect',
  checks: [checkpoint],
};

export const semanticCondition = {
  operator: 'semantic',
  source: 'vision',
  target,
  expected: 'The summary is complete',
  timeout_ms: 10_000,
} satisfies Predicate;

// @ts-expect-error: Semantic checks require an explicit deadline too.
export const missingSemanticDeadline: Predicate = {
  operator: 'semantic',
  source: 'vision',
  target,
  expected: 'The summary is complete',
};
