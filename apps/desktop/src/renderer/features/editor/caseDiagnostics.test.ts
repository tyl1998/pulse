import type { Case, Flow, Predicate, Step, Target } from '@pulse/schema';
import { expect, test } from 'vitest';
import operatorsCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/operators.json';
import {
  caseFlowReferences,
  collectCaseProblems,
  type CaseProblemProject,
} from './caseDiagnostics';
import { caseSchemaIssues, copyDraftCase, DRAFT_SCHEMA_ISSUE_LIMIT } from './caseDraftValidation';

const target: Target = {
  locator: {
    candidates: [{ by: 'text', value: 'Target' }],
    expect: { unique: true, visible: true },
  },
};
const project: CaseProblemProject = {
  root: '/synthetic/fe-p210',
  project_id: 'editor',
  case_ids: ['case'],
  profile_ids: ['android'],
  flow_ids: ['flow', 'nested'],
  data_ids: ['data'],
  allowed_apps: ['com.pulse.target'],
  manifest: {
    constraints: { forbidden_actions: ['clear', 'pay'] },
    secret_refs: ['account'],
    resources: {},
  },
};

function document(overrides: Partial<Case> = {}): Case {
  return copyDraftCase({
    $schema: 'urn:pulse:schema:1-draft:case',
    case_id: 'case',
    name: 'Case',
    goal: 'Fixed criteria',
    profile_refs: ['android'],
    parameters: {},
    inputs: {},
    locators: {},
    preconditions: [],
    checks: [],
    steps: [
      { type: 'action', id: 'launch', action: { kind: 'launch', app_id: 'com.pulse.target' } },
    ],
    ...overrides,
  });
}

const input = (id: string, value: string | { var_ref: string } | { param_ref: string }): Step => ({
  type: 'action',
  id,
  action: { kind: 'input', target, text: value, mode: 'replace' },
});
const exists: Predicate = { operator: 'exists', source: 'native', target };

test('collects independent generated-schema errors with exact escaped JSON pointers', () => {
  const value = {
    ...document(),
    parameters: { 'invalid/name~': { type: 'string', required: 'yes' } },
    steps: [
      { type: 'action', id: 'launch', timeout_ms: 0, action: { kind: 'launch', app_id: 42 } },
    ],
    'extra/path': true,
  };
  expect(caseSchemaIssues(value).map((issue) => issue.path)).toEqual([
    'Case/parameters/invalid~1name~0',
    'Case/parameters/invalid~1name~0/required',
    'Case/steps/0/timeout_ms',
    'Case/steps/0/action/app_id',
    'Case/extra~1path',
  ]);
  expect(() => copyDraftCase(value)).toThrow('Case/parameters/invalid~1name~0');
});

test('reports every duplicate ID at its occurrence, including nested steps and checkpoints', () => {
  const check = { id: 'checkpoint', required: true, condition: exists };
  const value = {
    ...document(),
    preconditions: [check],
    checks: [check],
    steps: [
      {
        type: 'if',
        id: 'branch',
        condition: exists,
        then: [input('same', '')],
        else: [input('same', '')],
      },
    ],
  };
  expect(caseSchemaIssues(value)).toEqual([
    { path: 'Case/steps/0/else/0/id', message: '重复步骤 ID: same' },
    { path: 'Case/checks/0/id', message: '重复检查点 ID: checkpoint' },
  ]);
  expect(() => copyDraftCase(value)).toThrow('重复步骤 ID');
});

test('caps schema diagnostics without silently accepting invalid large documents', () => {
  const value = {
    ...document(),
    checks: Array.from({ length: 150 }, () => ({ id: '', required: 'yes', condition: {} })),
  };
  expect(caseSchemaIssues(value)).toHaveLength(DRAFT_SCHEMA_ISSUE_LIMIT);
  expect(() => copyDraftCase(value)).toThrow('用例结构无效');
});

test('retains every leaf error inside identifiable inline-target and nullable-condition unions', () => {
  const invalidTarget = {
    locator: {
      candidates: [
        { by: 'text', value: '' },
        { by: 'desc', value: '' },
      ],
      expect: { unique: true, visible: true },
    },
  };
  const action = {
    ...document(),
    steps: [{ type: 'action', id: 'tap', action: { kind: 'tap', target: invalidTarget } }],
  };
  expect(caseSchemaIssues(action).map((issue) => issue.path)).toEqual([
    'Case/steps/0/action/target/locator/candidates/0/value',
    'Case/steps/0/action/target/locator/candidates/1/value',
  ]);
  const loop = {
    ...document(),
    steps: [
      {
        type: 'loop',
        id: 'loop',
        max_iterations: 1,
        condition: { operator: 'exists', source: 'invalid', target: invalidTarget },
        steps: [input('nested', '')],
      },
    ],
  };
  expect(caseSchemaIssues(loop).map((issue) => issue.path)).toEqual([
    'Case/steps/0/condition/source',
    'Case/steps/0/condition/target/locator/candidates/0/value',
    'Case/steps/0/condition/target/locator/candidates/1/value',
  ]);
  expect(() => copyDraftCase(action)).toThrow(
    'Case/steps/0/action/target/locator/candidates/0/value',
  );
});

test('all fourteen generated operators remain valid and semantic stays an explicit AI dependency', () => {
  const value = copyDraftCase(operatorsCase);
  const problems = collectCaseProblems(value, null);
  expect(problems.filter((problem) => problem.category === 'schema')).toEqual([]);
  expect(problems.filter((problem) => problem.category === 'ai')).toHaveLength(1);
  expect(new Set(value.checks.map((check) => check.condition.operator)).size).toBe(14);
});

test('distinguishes binding semantics from generated-schema errors and allows required runtime overrides', () => {
  const value = document({
    parameters: {
      missing: { type: 'string', required: true },
      wrong: { type: 'integer', required: false, default: false },
      optional: { type: 'number', required: false },
      required: { type: 'string', required: true, default: 'bad' },
    },
    inputs: { wrong: 'not a number', absent: false },
  });
  const problems = collectCaseProblems(value, project);
  expect(problems.filter((problem) => problem.category === 'schema')).toEqual([]);
  expect(
    problems.filter((problem) => problem.category === 'semantic').map((problem) => problem.path),
  ).toEqual([
    'Case/parameters/wrong/default',
    'Case/parameters/optional',
    'Case/parameters/required',
    'Case/inputs/wrong',
  ]);
  expect(problems.some((problem) => problem.path.includes('missing'))).toBe(false);
  expect(problems.find((problem) => problem.path === 'Case/inputs/absent')?.category).toBe(
    'reference',
  );
});

test('checks parameter and immutable initial-input references without exposing secret values', () => {
  const value = document({
    parameters: {
      password: { type: 'string', required: true },
      self: { type: 'string', required: true },
    },
    inputs: { password: { secret_ref: 'undeclared' }, self: { param_ref: 'self' } },
    steps: [
      input('unknown', { param_ref: 'absent' }),
      {
        type: 'flow',
        id: 'missing-flow',
        flow_ref: 'missing',
        arguments: { data: { data_ref: 'absent', key: 'key' } },
      },
    ],
  });
  const problems = collectCaseProblems(value, project);
  expect(problems.map((problem) => problem.path)).toEqual([
    'Case/inputs/password/secret_ref',
    'Case/inputs/self',
    'Case/steps/0/action/text/param_ref',
    'Case/steps/1/flow_ref',
    'Case/steps/1/arguments/data/data_ref',
  ]);
  expect(problems.every((problem) => problem.category !== 'schema')).toBe(true);
});

test('variables are visible only after assignment and do not escape if or loop scopes', () => {
  const value = document({
    steps: [
      input('before', { var_ref: 'observed' }),
      { type: 'extract', id: 'read', target, source: 'native', attribute: 'text', as: 'observed' },
      input('after', { var_ref: 'observed' }),
      {
        type: 'if',
        id: 'branch',
        condition: exists,
        then: [
          {
            type: 'extract',
            id: 'branch-read',
            target,
            source: 'native',
            attribute: 'text',
            as: 'branch-only',
          },
        ],
        else: [input('else-input', { var_ref: 'branch-only' })],
      },
      {
        type: 'loop',
        id: 'loop',
        max_iterations: 2,
        steps: [
          {
            type: 'extract',
            id: 'loop-read',
            target,
            source: 'native',
            attribute: 'count',
            as: 'loop-only',
          },
        ],
      },
      input('outside', { var_ref: 'branch-only' }),
      input('loop-outside', { var_ref: 'loop-only' }),
    ],
  });
  expect(
    collectCaseProblems(value, project)
      .filter((problem) => problem.category === 'reference')
      .map((problem) => problem.path),
  ).toEqual([
    'Case/steps/0/action/text/var_ref',
    'Case/steps/3/else/0/action/text/var_ref',
    'Case/steps/5/action/text/var_ref',
    'Case/steps/6/action/text/var_ref',
  ]);
});

test('fixed expectations retain declared types and cannot resolve to secret or empty criteria', () => {
  const value = document({
    parameters: {
      text: { type: 'string', required: true },
      numeric: { type: 'number', required: false, default: 2.5 },
      empty: { type: 'string', required: false, default: '' },
    },
    inputs: { text: { secret_ref: 'account' } },
    checks: [
      {
        id: 'secret',
        required: true,
        condition: {
          operator: 'text_equals',
          source: 'native',
          target,
          expected: { param_ref: 'text' },
        },
      },
      {
        id: 'count',
        required: true,
        condition: {
          operator: 'count_eq',
          source: 'native',
          target,
          expected: { param_ref: 'numeric' },
        },
      },
      {
        id: 'empty',
        required: true,
        condition: {
          operator: 'contains',
          source: 'native',
          target,
          expected: { param_ref: 'empty' },
        },
      },
      {
        id: 'allowed-empty',
        required: true,
        condition: { operator: 'text_equals', source: 'native', target, expected: '' },
      },
    ],
  });
  expect(collectCaseProblems(value, project).map((problem) => problem.path)).toEqual([
    'Case/checks/0/condition/expected',
    'Case/checks/1/condition/expected',
    'Case/checks/2/condition/expected',
  ]);
});

test('keeps unresolved environment locators unverified rather than pretending a profile was read', () => {
  const value = document({
    checks: [
      {
        id: 'check',
        required: true,
        condition: { ...exists, target: { locator_ref: 'profile-locator' } },
      },
    ],
  });
  const problem = collectCaseProblems(value, project)[0];
  expect(problem).toMatchObject({
    category: 'reference',
    severity: 'warning',
    path: 'Case/checks/0/condition/target/locator_ref',
  });
  expect(problem?.message).toContain('需复查环境定位器');
  const withoutProfile = { ...project, profile_ids: [] };
  expect(collectCaseProblems(value, withoutProfile).map((item) => item.severity)).toEqual([
    'error',
    'error',
  ]);
});

test.each(['h', 'min', 's', 'cny', '¥', '￥', '$', '€', '£', '元', '%', '折', '杯'])(
  'matches Runtime supported numeric unit %s',
  (unit) => {
    const value = document({
      checks: [
        {
          id: 'amount',
          required: true,
          condition: {
            operator: 'number_cmp',
            source: 'native',
            target,
            comparison: 'eq',
            expected: 1,
            unit,
            precision: 6,
          },
        },
      ],
    });
    expect(collectCaseProblems(value, project)).toEqual([]);
  },
);

test.each(['CNY ', 'H', 'MIN', 'S', 'kg'])(
  'does not silently normalize unsupported numeric unit %s',
  (unit) => {
    const value = document({
      checks: [
        {
          id: 'amount',
          required: true,
          condition: {
            operator: 'number_cmp',
            source: 'native',
            target,
            comparison: 'eq',
            expected: 1,
            unit,
            precision: 7,
          },
        },
      ],
    });
    expect(collectCaseProblems(value, project).map((problem) => problem.path)).toEqual([
      'Case/checks/0/condition/unit',
      'Case/checks/0/condition/precision',
    ]);
  },
);

test('diagnoses absence windows, native-state evidence, explicit AI steps and forbidden actions', () => {
  const value = document({
    preconditions: [
      {
        id: 'gone',
        required: true,
        condition: {
          operator: 'not_exists',
          source: 'native',
          target,
          scope: target,
          timeout_ms: 100,
          observation_window_ms: 200,
          poll_interval_ms: 300,
        },
      },
    ],
    steps: [
      { type: 'action', id: 'clear-data', action: { kind: 'clear', app_id: 'com.pulse.target' } },
      { type: 'action', id: 'pay', action: { kind: 'tap', target } },
      { type: 'action', id: 'outside', action: { kind: 'launch', app_id: 'other.app' } },
      {
        type: 'ai_step',
        id: 'ai',
        timeout_ms: 1000,
        goal: 'Find product',
        checks: [
          {
            id: 'semantic',
            required: true,
            condition: {
              operator: 'semantic',
              timeout_ms: 500,
              source: 'native',
              target,
              expected: 'Expected',
            },
          },
        ],
      },
    ],
    checks: [
      {
        id: 'enabled',
        required: true,
        condition: { operator: 'enabled', source: 'ocr', target, expected: true },
      },
    ],
  });
  const problems = collectCaseProblems(value, project);
  expect(problems.filter((problem) => problem.category === 'policy')).toHaveLength(3);
  expect(problems.filter((problem) => problem.category === 'ai')).toHaveLength(2);
  expect(
    problems.filter((problem) => problem.category === 'semantic').map((problem) => problem.path),
  ).toEqual([
    'Case/preconditions/0/condition/observation_window_ms',
    'Case/preconditions/0/condition/poll_interval_ms',
    'Case/checks/0/condition/source',
  ]);
});

test('checks source-confirmed flow arguments, returns and transitive AI/policy hazards at the caller', () => {
  const flow: Flow = {
    $schema: 'urn:pulse:schema:1-draft:flow',
    flow_id: 'flow',
    name: 'Flow',
    parameters: { query: { type: 'string', required: true } },
    locators: {},
    steps: [{ type: 'flow', id: 'nested-call', flow_ref: 'nested' }],
    returns: { text: { type: 'string', value: 'fixed' } },
  };
  const nested: Flow = {
    ...flow,
    flow_id: 'nested',
    parameters: {},
    returns: {},
    steps: [
      {
        type: 'ai_step',
        id: 'ai',
        timeout_ms: 1000,
        goal: 'Goal',
        checks: [{ id: 'check', required: true, condition: exists }],
      },
      { type: 'action', id: 'clear', action: { kind: 'clear', app_id: 'com.pulse.target' } },
    ],
  };
  const value = document({
    steps: [
      {
        type: 'flow',
        id: 'call',
        flow_ref: 'flow',
        arguments: { unknown: 1 },
        bind: { absent: 'variable' },
      },
    ],
  });
  const problems = collectCaseProblems(value, project, { flow, nested });
  expect(
    problems.filter((problem) => problem.category === 'reference').map((problem) => problem.path),
  ).toEqual([
    'Case/steps/0/arguments/unknown',
    'Case/steps/0/arguments',
    'Case/steps/0/bind/absent',
  ]);
  expect(problems.find((problem) => problem.category === 'ai')?.path).toBe('Case/steps/0/flow_ref');
  expect(problems.find((problem) => problem.category === 'policy')?.path).toBe(
    'Case/steps/0/flow_ref',
  );
  expect(caseFlowReferences(value.steps)).toEqual(['flow']);
  expect(
    collectCaseProblems(value, null).filter((problem) => problem.category === 'policy'),
  ).toEqual([]);
});

test.each(['constructor', 'toString', '__proto__'])(
  'never resolves prototype members as declared flow or binding %s',
  (name) => {
    const value = document({ steps: [{ type: 'flow', id: 'call', flow_ref: name }] });
    expect(collectCaseProblems(value, { ...project, flow_ids: [name] })).toEqual([]);
    const inputValue = document({ inputs: Object.fromEntries([[name, false]]) });
    expect(collectCaseProblems(inputValue, project).map((problem) => problem.category)).toEqual([
      'reference',
    ]);
    const flow: Flow = {
      $schema: 'urn:pulse:schema:1-draft:flow',
      flow_id: 'flow',
      name: 'Flow',
      parameters: {},
      locators: {},
      returns: {},
      steps: [
        { type: 'action', id: 'launch', action: { kind: 'launch', app_id: 'com.pulse.target' } },
      ],
    };
    const call = document({
      steps: [
        {
          type: 'flow',
          id: 'call',
          flow_ref: 'flow',
          arguments: Object.fromEntries([[name, 'value']]),
          bind: Object.fromEntries([[name, 'variable']]),
        },
      ],
    });
    expect(collectCaseProblems(call, project, { flow }).map((problem) => problem.path)).toEqual([
      `Case/steps/0/arguments/${name}`,
      `Case/steps/0/bind/${name}`,
    ]);
  },
);
