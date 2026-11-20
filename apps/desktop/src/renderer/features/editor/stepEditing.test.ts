import type { Case, Checkpoint, IfStep, Locator, Step } from '@pulse/schema';
import { describe, expect, test, vi } from 'vitest';
import aiFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/ai.json';
import controlFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import operatorsFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/operators.json';
import { assertDraftCase, copyDraftCase } from './caseDraftValidation';
import {
  createCheckpoint,
  createStep,
  editStepList,
  findStep,
  replaceStep,
  ROOT_STEPS,
  STEP_TYPES,
  type StepListLocation,
} from './stepEditing';

const control = copyDraftCase(controlFixture);
const operators = copyDraftCase(operatorsFixture);
const ai = copyDraftCase(aiFixture);
const types = ['action', 'wait', 'check', 'extract', 'flow', 'if', 'loop', 'ai_step'] as const;
const fallbackLocator: Locator = {
  candidates: [{ by: 'text', value: '目标文本' }],
  expect: { unique: true, visible: true },
};

function goldenStep<T extends Step['type']>(
  document: Case,
  id: string,
  type: T,
): Extract<Step, { type: T }> {
  const step = findStep(document.steps, id);
  if (!step || step.type !== type) throw new Error(`Missing golden ${type} step ${id}`);
  return step as Extract<Step, { type: T }>;
}

function allSteps(steps: readonly Step[]): Step[] {
  return steps.flatMap((step) => [
    step,
    ...(step.type === 'if'
      ? [...allSteps(step.then), ...allSteps(step.else ?? [])]
      : step.type === 'loop'
        ? allSteps(step.steps)
        : []),
  ]);
}

function procedureIds(document: Case) {
  const steps = allSteps(document.steps);
  const checkpoints = [...document.preconditions, ...document.checks];
  for (const step of steps) {
    if (step.type === 'check') checkpoints.push(step.checkpoint);
    if (step.type === 'ai_step') checkpoints.push(...step.checks);
  }
  return {
    steps: steps.map((step) => step.id),
    checks: checkpoints.map((checkpoint) => checkpoint.id),
  };
}

const locations: { name: string; location: StepListLocation; list: (document: Case) => Step[] }[] =
  [
    { name: 'root', location: ROOT_STEPS, list: (document) => document.steps },
    {
      name: 'then',
      location: { parentId: 'banner-branch', branch: 'then' },
      list: (document) => goldenStep(document, 'banner-branch', 'if').then,
    },
    {
      name: 'else',
      location: { parentId: 'banner-branch', branch: 'else' },
      list: (document) => goldenStep(document, 'banner-branch', 'if').else ?? [],
    },
    {
      name: 'loop',
      location: { parentId: 'bounded-observation', branch: 'steps' },
      list: (document) => goldenStep(document, 'bounded-observation', 'loop').steps,
    },
  ];

function deeplyNestedDocument(): Case {
  const branch = goldenStep(control, 'banner-branch', 'if');
  const loop = goldenStep(control, 'bounded-observation', 'loop');
  const absence = operators.checks.find(
    (checkpoint) => checkpoint.condition.operator === 'not_exists',
  );
  if (!absence) throw new Error('Missing absence fixture');
  const nested: IfStep = {
    type: 'if',
    id: 'inner_branch',
    timeout_ms: 8700,
    condition: absence.condition,
    then: branch.then,
  };
  const outer: IfStep = {
    ...branch,
    timeout_ms: 9100,
    then: [{ ...loop, timeout_ms: 8200, steps: [nested, ...loop.steps] }],
  };
  delete outer.else;
  return copyDraftCase({
    ...control,
    steps: [outer, ...control.steps.filter((step) => step.id !== branch.id && step.id !== loop.id)],
  });
}

describe('editStepList', () => {
  test.each(locations)(
    'edits only the $name list and retains every other golden field',
    ({ location, list }) => {
      const originalList = list(control);
      const first = originalList[0];
      if (!first) throw new Error('Missing list fixture');
      const replacement: Step = { ...first, timeout_ms: 12345 };
      const edit = vi.fn((items: Step[]) =>
        items.map((step, index) => (index === 0 ? replacement : step)),
      );
      const edited = editStepList(control, location, edit);
      const expected = structuredClone(control);
      list(expected)[0] = replacement;
      expect(edit).toHaveBeenCalledOnce();
      expect(edit).toHaveBeenCalledWith(originalList);
      expect(edited).toEqual(expected);
      expect(control).toEqual(controlFixture);
      assertDraftCase(edited);
    },
  );

  test.each(locations.filter(({ name }) => name === 'root' || name === 'loop'))(
    'reorders the $name list without losing per-step or document fields',
    ({ location, list }) => {
      const originalList = list(control);
      const reversed = [...originalList].reverse();
      const edited = editStepList(control, location, (items) => [...items].reverse());
      const expected = structuredClone(control);
      const expectedList = list(expected);
      expectedList.splice(0, expectedList.length, ...reversed);
      expect(edited).toEqual(expected);
      assertDraftCase(edited);
    },
  );

  test('reaches a then block inside a loop inside an if without injecting absent else blocks', () => {
    const document = deeplyNestedDocument();
    const inner = goldenStep(document, 'inner_branch', 'if');
    const replacement = { ...inner.then[0], timeout_ms: 6400 };
    const edit = vi.fn((steps: Step[]) => [replacement, ...steps.slice(1)]);
    const edited = editStepList(document, { parentId: inner.id, branch: 'then' }, edit);
    const expected = structuredClone(document);
    goldenStep(expected, inner.id, 'if').then[0] = replacement;
    expect(edit).toHaveBeenCalledOnce();
    expect(edit).toHaveBeenCalledWith(inner.then);
    expect(edited).toEqual(expected);
    expect(Object.hasOwn(goldenStep(edited, 'banner-branch', 'if'), 'else')).toBe(false);
    expect(Object.hasOwn(goldenStep(edited, inner.id, 'if'), 'else')).toBe(false);
    assertDraftCase(edited);
  });

  test('opens an omitted else as an empty list and creates only the requested nested branch', () => {
    const document = deeplyNestedDocument();
    const added: Step = {
      type: 'action',
      id: 'new_else_key',
      timeout_ms: 6700,
      action: { kind: 'key', key: 'BACK' },
    };
    const edit = vi.fn((steps: Step[]) => [...steps, added]);
    const edited = editStepList(document, { parentId: 'inner_branch', branch: 'else' }, edit);
    const expected = structuredClone(document);
    goldenStep(expected, 'inner_branch', 'if').else = [added];
    expect(edit).toHaveBeenCalledOnce();
    expect(edit).toHaveBeenCalledWith([]);
    expect(edited).toEqual(expected);
    expect(Object.hasOwn(goldenStep(edited, 'banner-branch', 'if'), 'else')).toBe(false);
    expect(Object.hasOwn(goldenStep(document, 'inner_branch', 'if'), 'else')).toBe(false);
    assertDraftCase(edited);
  });

  test.each([
    { parentId: null, branch: 'then' },
    { parentId: null, branch: 'else' },
    { parentId: 'banner-branch', branch: 'steps' },
    { parentId: 'bounded-observation', branch: 'then' },
    { parentId: 'bounded-observation', branch: 'else' },
    { parentId: 'launch', branch: 'steps' },
    { parentId: 'tap-banner', branch: 'then' },
    { parentId: 'wait-page', branch: 'else' },
    { parentId: 'search-product', branch: 'steps' },
  ] satisfies StepListLocation[])(
    'rejects the wrong branch $parentId/$branch before invoking the edit',
    (location) => {
      const edit = vi.fn((steps: Step[]) => steps);
      expect(() => editStepList(control, location, edit)).toThrow(
        location.parentId === null ? '根步骤块无效' : '步骤块与控制流类型不匹配',
      );
      expect(edit).not.toHaveBeenCalled();
      expect(control).toEqual(controlFixture);
    },
  );

  test('rejects a removed parent rather than falling back to a root or another branch', () => {
    const document = editStepList(control, ROOT_STEPS, (steps) =>
      steps.filter((step) => step.id !== 'banner-branch'),
    );
    assertDraftCase(document);
    const edit = vi.fn((steps: Step[]) => steps);
    expect(() =>
      editStepList(document, { parentId: 'banner-branch', branch: 'then' }, edit),
    ).toThrow('步骤块已变化，请重新打开编辑');
    expect(edit).not.toHaveBeenCalled();
    expect(findStep(document.steps, 'tap-banner')).toBeUndefined();
    expect(document).toEqual({
      ...control,
      steps: control.steps.filter((step) => step.id !== 'banner-branch'),
    });
  });

  test('rejects an unknown parent even when a valid-looking branch was requested', () => {
    const edit = vi.fn((steps: Step[]) => steps);
    expect(() =>
      editStepList(control, { parentId: 'missing_parent', branch: 'steps' }, edit),
    ).toThrow('步骤块已变化，请重新打开编辑');
    expect(edit).not.toHaveBeenCalled();
  });
});

describe('findStep and replaceStep', () => {
  test.each(locations)(
    'finds and replaces an exact $name step while preserving the complete procedure',
    ({ list }) => {
      const original = list(control)[0];
      if (!original) throw new Error('Missing replacement fixture');
      expect(findStep(control.steps, original.id)).toBe(original);
      const replacement = { ...original, id: `${original.id}_edited`, timeout_ms: 14500 };
      const edited = replaceStep(control, original, replacement);
      const expected = structuredClone(control);
      list(expected)[0] = replacement;
      expect(edited).toEqual(expected);
      expect(findStep(edited.steps, original.id)).toBeUndefined();
      expect(findStep(edited.steps, replacement.id)).toEqual(replacement);
      assertDraftCase(edited);
    },
  );

  test('replaces a deeply nested step without materializing missing else or changing parent metadata', () => {
    const document = deeplyNestedDocument();
    const original = goldenStep(document, 'iteration-label', 'extract');
    const replacement = { ...original, as: 'edited_iteration_label' };
    const edited = replaceStep(document, original, replacement);
    const expected = structuredClone(document);
    goldenStep(expected, 'bounded-observation', 'loop').steps[1] = replacement;
    expect(edited).toEqual(expected);
    expect(Object.hasOwn(goldenStep(edited, 'banner-branch', 'if'), 'else')).toBe(false);
    expect(Object.hasOwn(goldenStep(edited, 'inner_branch', 'if'), 'else')).toBe(false);
    assertDraftCase(edited);
  });

  test('accepts an unchanged detached original instead of requiring the same object reference', () => {
    const original = goldenStep(control, 'search-product', 'flow');
    const detached = structuredClone(original);
    const replacement = { ...original, bind: { ...original.bind, label: 'edited_label' } };
    expect(detached).not.toBe(original);
    const edited = replaceStep(control, detached, replacement);
    const expected = structuredClone(control);
    expected.steps[1] = replacement;
    expect(edited).toEqual(expected);
    assertDraftCase(edited);
  });

  test.each(types)(
    'rejects an externally changed same-ID %s step instead of overwriting it',
    (type) => {
      const original = [...allSteps(control.steps), ...allSteps(ai.steps)].find(
        (step) => step.type === type,
      );
      if (!original) throw new Error(`Missing ${type} fixture`);
      const document = copyDraftCase({ ...control, steps: [{ ...original, timeout_ms: 6000 }] });
      const replacement = { ...original, timeout_ms: 9000 };
      expect(() => replaceStep(document, original, replacement)).toThrow(
        '此步骤已被修改，请恢复已保存内容后再编辑',
      );
      expect(document.steps[0]).toEqual({ ...original, timeout_ms: 6000 });
      assertDraftCase(document);
    },
  );

  const input = goldenStep(control, 'use-return', 'action');
  const flow = goldenStep(control, 'search-product', 'flow');
  const branch = goldenStep(control, 'banner-branch', 'if');
  const loop = goldenStep(control, 'bounded-observation', 'loop');
  const check = goldenStep(control, 'confirm-check', 'check');
  const aiStep = goldenStep(ai, 'inspect-summary', 'ai_step');
  test.each([
    {
      name: 'input value',
      original: input,
      changed: {
        ...input,
        action: {
          kind: 'input',
          target: { locator_ref: 'search' },
          mode: 'replace',
          text: 'Changed externally',
        },
      },
    },
    {
      name: 'flow argument',
      original: flow,
      changed: { ...flow, arguments: { ...flow.arguments, query: 'Changed externally' } },
    },
    {
      name: 'flow return binding',
      original: flow,
      changed: { ...flow, bind: { ...flow.bind, label: 'external_label' } },
    },
    {
      name: 'then child',
      original: branch,
      changed: { ...branch, then: [{ ...branch.then[0], timeout_ms: 6700 }] },
    },
    {
      name: 'loop condition',
      original: loop,
      changed: {
        ...loop,
        condition: {
          operator: 'count_lt',
          source: 'native',
          target: { locator_ref: 'items' },
          expected: 4,
        },
      },
    },
    {
      name: 'loop body',
      original: loop,
      changed: { ...loop, steps: [{ ...loop.steps[0], timeout_ms: 6900 }, ...loop.steps.slice(1)] },
    },
    {
      name: 'checkpoint metadata',
      original: check,
      changed: { ...check, checkpoint: { ...check.checkpoint, required: false } },
    },
    {
      name: 'AI fixed expectation',
      original: aiStep,
      changed: {
        ...aiStep,
        checks: [
          {
            ...aiStep.checks[0],
            condition: {
              operator: 'semantic',
              source: 'vision',
              target: { locator_ref: 'page' },
              expected: 'Changed externally',
              timeout_ms: 10000,
            },
          },
        ],
      },
    },
  ] satisfies { name: string; original: Step; changed: Step }[])(
    'rejects same-ID external changes to nested $name fields',
    ({ original, changed }) => {
      const document = copyDraftCase({ ...control, steps: [changed] });
      expect(() => replaceStep(document, original, { ...original, timeout_ms: 12000 })).toThrow(
        '此步骤已被修改，请恢复已保存内容后再编辑',
      );
      expect(document.steps[0]).toEqual(changed);
      assertDraftCase(document);
    },
  );

  test('rejects replacement after a nested step has been removed', () => {
    const original = goldenStep(control, 'iteration-label', 'extract');
    const document = editStepList(
      control,
      { parentId: 'bounded-observation', branch: 'steps' },
      (steps) => steps.filter((step) => step.id !== original.id),
    );
    assertDraftCase(document);
    expect(() => replaceStep(document, original, { ...original, as: 'edited_value' })).toThrow(
      '此步骤已被修改，请恢复已保存内容后再编辑',
    );
    expect(findStep(document.steps, original.id)).toBeUndefined();
  });

  test('findStep returns undefined for a missing ID and does not invent an omitted branch', () => {
    const document = deeplyNestedDocument();
    expect(findStep(document.steps, 'missing_step')).toBeUndefined();
    expect(findStep(document.steps, 'inner_branch')?.type).toBe('if');
    expect(findStep(document.steps, 'tap-banner')?.type).toBe('action');
    expect(Object.hasOwn(goldenStep(document, 'inner_branch', 'if'), 'else')).toBe(false);
  });
});

function reservedIdsDocument(): Case {
  const checkpoint = (id: string): Checkpoint => ({
    id,
    required: true,
    condition: { operator: 'exists', source: 'native', target: { locator_ref: 'page' } },
  });
  const condition = checkpoint('unused').condition;
  return copyDraftCase({
    ...control,
    preconditions: [...control.preconditions, checkpoint('checkpoint_1')],
    checks: [...control.checks, checkpoint('checkpoint_2')],
    steps: [
      {
        type: 'if',
        id: 'if_1',
        condition,
        then: [
          {
            type: 'loop',
            id: 'loop_1',
            max_iterations: 2,
            condition,
            steps: [
              {
                type: 'action',
                id: 'action_1',
                action: { kind: 'tap', target: { locator_ref: 'product' } },
              },
              { type: 'check', id: 'check_1', checkpoint: checkpoint('checkpoint_3') },
            ],
          },
          { type: 'wait', id: 'wait_1', condition },
        ],
        else: [
          {
            type: 'extract',
            id: 'extract_1',
            target: { locator_ref: 'product' },
            source: 'native',
            attribute: 'text',
            as: 'label',
          },
          {
            type: 'flow',
            id: 'flow_1',
            flow_ref: 'fill-search',
            arguments: { query: { param_ref: 'product' } },
            bind: { label: 'label' },
          },
          {
            type: 'ai_step',
            id: 'ai_step_1',
            timeout_ms: 30000,
            goal: 'Inspect the declared product.',
            checks: [checkpoint('checkpoint_4')],
          },
          {
            type: 'if',
            id: 'if_2',
            condition,
            then: [{ type: 'check', id: 'check_2', checkpoint: checkpoint('checkpoint_5') }],
          },
        ],
      },
    ],
  });
}

describe('step and checkpoint templates', () => {
  test('offers exactly the eight v1-draft step types', () => {
    expect(STEP_TYPES).toEqual(types);
  });

  test.each(types)('creates a legal complete %s template with bounded required blocks', (type) => {
    const created = createStep(control, type);
    const target = { locator: fallbackLocator };
    const condition = { operator: 'exists', source: 'native', target };
    const checkpoint = { id: 'checkpoint_1', required: true, condition };
    const child = { type: 'action', id: 'action_1', action: { kind: 'tap', target } };
    const fields = {
      action: { action: { kind: 'tap', target } },
      wait: { condition },
      check: { checkpoint },
      extract: { target, source: 'native', attribute: 'text', as: 'value' },
      flow: { flow_ref: 'flow', arguments: {}, bind: {} },
      if: { condition, then: [child], else: [] },
      loop: { max_iterations: 1, steps: [child] },
      ai_step: { goal: '描述需要完成的目标', timeout_ms: 30000, checks: [checkpoint] },
    };
    expect(created).toEqual({ type, id: `${type}_1`, ...fields[type] });
    assertDraftCase({ ...control, steps: [...control.steps, created] });
    expect(control).toEqual(controlFixture);
  });

  test.each(types)('uses a declared locator when creating a legal %s template', (type) => {
    const document = copyDraftCase({
      ...control,
      locators: {
        declared: fallbackLocator,
        secondary: { ...fallbackLocator, candidates: [{ by: 'text', value: 'Secondary' }] },
      },
    });
    const created = createStep(document, type);
    for (const step of allSteps([created])) {
      if (step.type === 'action' && 'target' in step.action)
        expect(step.action.target).toEqual({ locator_ref: 'declared' });
      if (step.type === 'wait' || step.type === 'if')
        expect(step.condition.target).toEqual({ locator_ref: 'declared' });
      if (step.type === 'extract') expect(step.target).toEqual({ locator_ref: 'declared' });
      if (step.type === 'check')
        expect(step.checkpoint.condition.target).toEqual({ locator_ref: 'declared' });
      if (step.type === 'ai_step')
        step.checks.forEach((checkpoint) =>
          expect(checkpoint.condition.target).toEqual({ locator_ref: 'declared' }),
        );
    }
    assertDraftCase({ ...document, steps: [...document.steps, created] });
    expect(document.locators).toEqual({
      declared: fallbackLocator,
      secondary: { ...fallbackLocator, candidates: [{ by: 'text', value: 'Secondary' }] },
    });
  });

  test('allocates step and checkpoint IDs uniquely across root, then, else, loop bodies and all procedure checkpoints', () => {
    let document = reservedIdsDocument();
    for (let round = 0; round < 2; round++) {
      for (const type of types) {
        const before = procedureIds(document);
        const created = createStep(document, type);
        const added = allSteps([created]);
        expect(added.every((step) => !before.steps.includes(step.id))).toBe(true);
        expect(new Set(added.map((step) => step.id)).size).toBe(added.length);
        document = copyDraftCase({ ...document, steps: [...document.steps, created] });
        const after = procedureIds(document);
        expect(new Set(after.steps).size).toBe(after.steps.length);
        expect(new Set(after.checks).size).toBe(after.checks.length);
        expect(after.steps.length).toBe(before.steps.length + added.length);
        assertDraftCase(document);
      }
    }
    expect(document.preconditions).toEqual(reservedIdsDocument().preconditions);
    expect(document.checks).toEqual(reservedIdsDocument().checks);
  });

  test('checkpoint IDs also avoid unsaved pending checkpoints without changing them or the document', () => {
    const document = reservedIdsDocument();
    const template = operators.checks[0];
    if (!template) throw new Error('Missing checkpoint fixture');
    const pending = [
      { ...template, id: 'checkpoint_6' },
      { ...template, id: 'checkpoint_8' },
    ];
    const before = structuredClone(pending);
    const created = createCheckpoint(document, pending);
    expect(created).toEqual({
      id: 'checkpoint_7',
      required: true,
      condition: { operator: 'exists', source: 'native', target: { locator: fallbackLocator } },
    });
    expect(pending).toEqual(before);
    expect(document).toEqual(reservedIdsDocument());
    assertDraftCase({
      ...document,
      steps: [
        ...document.steps,
        {
          type: 'ai_step',
          id: 'pending_ai',
          timeout_ms: 30000,
          goal: 'Inspect the declared product.',
          checks: [...pending, created],
        },
      ],
    });
  });
});

describe('schema boundary for required blocks', () => {
  test.each(locations.filter(({ name }) => name !== 'else'))(
    'rejects an empty mandatory $name block instead of treating an edited tree as valid',
    ({ location }) => {
      const edited = editStepList(control, location, () => []);
      expect(() => assertDraftCase(edited)).toThrow(/用例结构无效/);
      expect(control).toEqual(controlFixture);
    },
  );

  test('allows an empty optional else without deleting any other field', () => {
    const edited = editStepList(control, { parentId: 'banner-branch', branch: 'else' }, () => []);
    const expected = structuredClone(control);
    goldenStep(expected, 'banner-branch', 'if').else = [];
    expect(edited).toEqual(expected);
    assertDraftCase(edited);
  });

  test('rejects an empty AI checks block', () => {
    const step = createStep(control, 'ai_step');
    expect(() => assertDraftCase({ ...control, steps: [{ ...step, checks: [] }] })).toThrow(
      /用例结构无效/,
    );
  });

  test.each([0, -1, 1.5])('rejects the unbounded/invalid loop maximum %s', (max_iterations) => {
    const step = goldenStep(control, 'bounded-observation', 'loop');
    expect(() => assertDraftCase({ ...control, steps: [{ ...step, max_iterations }] })).toThrow(
      /用例结构无效/,
    );
  });
});
