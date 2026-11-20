import type {
  ActionStep,
  Case,
  Checkpoint,
  Locator,
  Predicate,
  Step,
  Target,
  TextValue,
  Value,
} from '@pulse/schema';
import { Component, useState, type ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, describe, expect, test, vi } from 'vitest';
import aiFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/ai.json';
import controlFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import operatorsFixture from '../../../../../../fixtures/golden-cases/v1-draft/cases/operators.json';
import { assertDraftCase, copyDraftCase } from './caseDraftValidation';
import { findStep, replaceStep } from './stepEditing';
import { StepFields, type FieldChange } from './StepFields';

const control = copyDraftCase(controlFixture);
const operators = copyDraftCase(operatorsFixture);
const ai = copyDraftCase(aiFixture);
const inlineLocator: Locator = {
  candidates: [
    { by: 'text', value: 'Coffee', scope: 'page' },
    { by: 'point', value: { x: 120, y: 240, width: 1080, height: 2400 }, scope: null },
  ],
  expect: { unique: true, visible: false },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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

class FieldsBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  override render() {
    return this.state.failed ? <p role="alert">字段组件异常</p> : this.props.children;
  }
}

function renderFields(document: Case, id = document.steps[0].id) {
  const source = copyDraftCase(document);
  const selected = findStep(source.steps, id);
  if (!selected) throw new Error(`Missing fixture step ${id}`);
  const original = selected;
  let last = original;
  const change = vi.fn<FieldChange<Step>>();
  const blur = vi.fn<(step: Step) => void>();
  const submit = vi.fn<(step: Step) => void>();
  const caught = vi.fn<(error: unknown) => void>();
  function Harness() {
    const [value, setValue] = useState(original);
    return (
      <form
        aria-label="隔离步骤字段"
        onBlur={(event) => {
          if (
            event.target instanceof HTMLInputElement ||
            event.target instanceof HTMLTextAreaElement
          )
            blur(value);
        }}
        onSubmit={(event) => {
          event.preventDefault();
          submit(value);
        }}
      >
        <StepFields
          value={value}
          document={source}
          onChange={(next, save) => {
            last = next;
            change(next, save);
            setValue(next);
          }}
        />
        <button type="submit">提交字段</button>
      </form>
    );
  }
  const rendered = render(
    <FieldsBoundary>
      <Harness />
    </FieldsBoundary>,
    { onCaughtError: (error) => caught(error) },
  );
  return {
    ...rendered,
    source,
    original,
    change,
    blur,
    submit,
    caught,
    last: () => last,
    validated: () => {
      expect(caught).not.toHaveBeenCalled();
      const edited = replaceStep(source, original, last);
      assertDraftCase(edited);
      return edited;
    },
  };
}

function documentWithStep(step: Step, document = control): Case {
  return copyDraftCase({ ...document, steps: [step] });
}

function inputDocument(
  text: TextValue = 'Coffee',
  target: Target = { locator_ref: 'search' },
): Case {
  const original = findStep(control.steps, 'use-return');
  if (original?.type !== 'action' || original.action.kind !== 'input')
    throw new Error('Missing golden input');
  return copyDraftCase(
    replaceStep(control, original, { ...original, action: { ...original.action, text, target } }),
  );
}

function checkpointDocument(checkpoint: Checkpoint): Case {
  return copyDraftCase({
    ...operators,
    steps: [{ type: 'check', id: `check_${checkpoint.id}`, checkpoint }],
    checks: operators.checks.filter((item) => item.id !== checkpoint.id),
  });
}

function conditionDocument(condition: Predicate): Case {
  return documentWithStep({ type: 'wait', id: 'condition_fields', condition });
}

async function choose(
  user: ReturnType<typeof userEvent.setup>,
  trigger: HTMLElement,
  value: string,
) {
  await user.click(trigger);
  await user.click(await screen.findByRole('option', { name: value }));
}

function edit(field: HTMLElement, value: string | number) {
  fireEvent.change(field, { target: { value: String(value) } });
}

function expectValueFields(group: HTMLElement, value: Value) {
  const fields = within(group);
  const mode =
    typeof value === 'object'
      ? Object.keys(value)[0]
      : typeof value === 'string'
        ? 'text'
        : typeof value;
  expect(fields.getByRole('combobox', { name: '值类型' })).toHaveTextContent(mode ?? '');
  if (typeof value === 'string')
    expect(fields.getByRole('textbox', { name: '文本值' })).toHaveValue(value);
  else if (typeof value === 'number')
    expect(fields.getByRole('spinbutton', { name: '数值' })).toHaveValue(value);
  else if (typeof value === 'boolean') {
    const checkbox = fields.getByRole('checkbox', { name: '布尔值' });
    if (value) expect(checkbox).toBeChecked();
    else expect(checkbox).not.toBeChecked();
  } else if ('data_ref' in value) {
    expect(fields.getByRole('textbox', { name: '数据集引用' })).toHaveValue(value.data_ref);
    expect(fields.getByRole('textbox', { name: '数据键' })).toHaveValue(value.key);
  } else {
    const label = 'param_ref' in value ? '参数引用' : 'var_ref' in value ? '变量引用' : '秘密引用';
    expect(fields.getByRole('textbox', { name: label })).toHaveValue(Object.values(value)[0]);
  }
}

function expectPointFields(
  group: HTMLElement,
  point: { x: number; y: number; width: number; height: number },
) {
  for (const key of ['x', 'y', 'width', 'height'] as const)
    expect(within(group).getByRole('spinbutton', { name: key })).toHaveValue(point[key]);
}

function expectTargetFields(group: HTMLElement, target: Target) {
  const fields = within(group);
  expect(fields.getByRole('combobox', { name: '目标类型' })).toHaveTextContent(
    'locator_ref' in target ? 'locator_ref' : 'locator',
  );
  if ('locator_ref' in target) {
    expect(fields.getByRole('combobox', { name: '定位器引用' })).toHaveValue(target.locator_ref);
    return;
  }
  target.locator.candidates.forEach((candidate, index) => {
    const candidateFields = within(fields.getByRole('group', { name: `候选 ${index + 1}` }));
    expect(candidateFields.getByRole('combobox', { name: '定位方式' })).toHaveTextContent(
      candidate.by,
    );
    expect(candidateFields.getByRole('textbox', { name: '候选作用域（可选）' })).toHaveValue(
      candidate.scope ?? '',
    );
    if (candidate.by === 'point')
      expectPointFields(
        candidateFields.getByRole('group', { name: '坐标与基准尺寸' }),
        candidate.value,
      );
    else
      expect(candidateFields.getByRole('textbox', { name: '定位值' })).toHaveValue(candidate.value);
  });
  for (const [label, checked] of [
    ['要求唯一', target.locator.expect.unique],
    ['要求可见', target.locator.expect.visible],
  ] as const) {
    if (checked) expect(fields.getByRole('checkbox', { name: label })).toBeChecked();
    else expect(fields.getByRole('checkbox', { name: label })).not.toBeChecked();
  }
}

function expectPredicateFields(group: HTMLElement, condition: Predicate) {
  const fields = within(group);
  expect(fields.getByRole('combobox', { name: '检查运算符' })).toHaveTextContent(
    condition.operator,
  );
  expect(fields.getByRole('combobox', { name: '观察来源' })).toHaveTextContent(condition.source);
  expect(fields.getByRole('spinbutton', { name: '条件超时（ms）' })).toHaveValue(
    condition.timeout_ms ?? null,
  );
  expectTargetFields(fields.getByRole('group', { name: '目标' }), condition.target);
  if (condition.operator === 'not_exists') {
    expectTargetFields(fields.getByRole('group', { name: '消失检查作用域' }), condition.scope);
    expect(fields.getByRole('spinbutton', { name: '观察窗口（ms）' })).toHaveValue(
      condition.observation_window_ms,
    );
    expect(fields.getByRole('spinbutton', { name: '轮询间隔（ms）' })).toHaveValue(
      condition.poll_interval_ms ?? null,
    );
  }
  if ('expected' in condition) {
    if (typeof condition.expected === 'boolean') {
      if (condition.expected)
        expect(fields.getByRole('checkbox', { name: '期望为真' })).toBeChecked();
      else expect(fields.getByRole('checkbox', { name: '期望为真' })).not.toBeChecked();
    } else expectValueFields(fields.getByRole('group', { name: '固定期望' }), condition.expected);
  }
  if (condition.operator === 'number_cmp') {
    expect(fields.getByRole('combobox', { name: '数值比较' })).toHaveTextContent(
      condition.comparison,
    );
    expect(fields.getByRole('textbox', { name: '单位' })).toHaveValue(condition.unit);
    expect(fields.getByRole('spinbutton', { name: '精度' })).toHaveValue(condition.precision);
  }
}

function expectCheckpointFields(group: HTMLElement, checkpoint: Checkpoint) {
  const fields = within(group);
  expect(fields.getByRole('textbox', { name: '检查点 ID' })).toHaveValue(checkpoint.id);
  if (checkpoint.required) expect(fields.getByRole('checkbox', { name: '必须通过' })).toBeChecked();
  else expect(fields.getByRole('checkbox', { name: '必须通过' })).not.toBeChecked();
  expectPredicateFields(group, checkpoint.condition);
}

function expectStepFields(step: Step) {
  const form = screen.getByRole('form', { name: '隔离步骤字段' });
  const fields = within(form);
  expect(fields.getByRole('textbox', { name: '步骤 ID' })).toHaveValue(step.id);
  expect(fields.getByRole('spinbutton', { name: '步骤超时（ms）' })).toHaveValue(
    step.timeout_ms ?? null,
  );
  switch (step.type) {
    case 'action': {
      const action = step.action;
      expect(fields.getByRole('combobox', { name: '动作类型' })).toHaveTextContent(action.kind);
      if ('target' in action)
        expectTargetFields(fields.getByRole('group', { name: '目标' }), action.target);
      if ('duration_ms' in action)
        expect(fields.getByRole('spinbutton', { name: '动作时长（ms）' })).toHaveValue(
          action.duration_ms,
        );
      if (action.kind === 'input') {
        expect(fields.getByRole('combobox', { name: '输入模式' })).toHaveTextContent(action.mode);
        expectValueFields(fields.getByRole('group', { name: '输入内容' }), action.text);
      }
      if (action.kind === 'swipe') {
        expectPointFields(fields.getByRole('group', { name: '滑动起点' }), action.start);
        expectPointFields(fields.getByRole('group', { name: '滑动终点' }), action.end);
      }
      if (action.kind === 'key')
        expect(fields.getByRole('textbox', { name: '按键' })).toHaveValue(action.key);
      if ('app_id' in action)
        expect(fields.getByRole('textbox', { name: '应用 ID' })).toHaveValue(action.app_id);
      break;
    }
    case 'wait':
    case 'if':
      expectPredicateFields(form, step.condition);
      break;
    case 'check':
      expectCheckpointFields(form, step.checkpoint);
      break;
    case 'extract':
      expectTargetFields(fields.getByRole('group', { name: '目标' }), step.target);
      expect(fields.getByRole('combobox', { name: '提取来源' })).toHaveTextContent(step.source);
      expect(fields.getByRole('combobox', { name: '提取属性' })).toHaveTextContent(step.attribute);
      expect(fields.getByRole('textbox', { name: '保存为变量' })).toHaveValue(step.as);
      break;
    case 'flow': {
      expect(fields.getByRole('textbox', { name: '公共流程引用' })).toHaveValue(step.flow_ref);
      const args = within(fields.getByRole('group', { name: '流程参数' }));
      Object.entries(step.arguments ?? {}).forEach(([key, value], index) => {
        expect(args.getAllByRole('textbox', { name: '绑定名称' })[index]).toHaveValue(key);
        const group = args.getAllByRole('group', { name: '参数值' })[index];
        if (!group || value === undefined) throw new Error('Missing argument fields');
        expectValueFields(group, value);
      });
      const bind = within(fields.getByRole('group', { name: '流程返回绑定' }));
      Object.entries(step.bind ?? {}).forEach(([key, value], index) => {
        expect(bind.getAllByRole('textbox', { name: '绑定名称' })[index]).toHaveValue(key);
        expect(bind.getAllByRole('textbox', { name: '调用端变量' })[index]).toHaveValue(value);
      });
      break;
    }
    case 'loop':
      expect(fields.getByRole('spinbutton', { name: '最多迭代次数' })).toHaveValue(
        step.max_iterations,
      );
      if (step.condition)
        expect(fields.getByRole('checkbox', { name: '使用循环条件' })).toBeChecked();
      else expect(fields.getByRole('checkbox', { name: '使用循环条件' })).not.toBeChecked();
      if (step.condition) expectPredicateFields(form, step.condition);
      break;
    case 'ai_step':
      expect(fields.getByRole('textbox', { name: 'AI 步骤目标' })).toHaveValue(step.goal);
      step.checks.forEach((checkpoint, index) =>
        expectCheckpointFields(
          fields.getByRole('group', { name: `检查点 ${index + 1}` }),
          checkpoint,
        ),
      );
      break;
  }
}

describe('golden structured step fields', () => {
  const goldenSteps = [control, operators, ai].flatMap((document) =>
    allSteps(document.steps).map((step) => ({
      document,
      step,
      caseId: document.case_id,
      id: step.id,
      type: step.type,
    })),
  );
  test.each(goldenSteps)(
    '$caseId/$id presents every $type field and preserves hidden fields without defaults',
    ({ document, step }) => {
      const h = renderFields(document, step.id);
      expectStepFields(step);
      expect(h.change).not.toHaveBeenCalled();
      expect(h.last()).toEqual(step);
      edit(screen.getByRole('textbox', { name: '步骤 ID' }), `${step.id}_edited`);
      expect(h.last()).toEqual({ ...step, id: `${step.id}_edited` });
      h.validated();
      expect(h.source).toEqual(document);
      expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
    },
  );

  test.each(operators.checks)(
    'presents every golden $id checkpoint field without injecting defaults',
    (checkpoint) => {
      const h = renderFields(checkpointDocument(checkpoint));
      expectCheckpointFields(screen.getByRole('form'), checkpoint);
      expect(h.change).not.toHaveBeenCalled();
      edit(screen.getByRole('textbox', { name: '检查点 ID' }), `${checkpoint.id}_edited`);
      expect(h.last()).toEqual({
        ...h.original,
        checkpoint: { ...checkpoint, id: `${checkpoint.id}_edited` },
      });
      h.validated();
    },
  );

  test.each([
    { type: 'flow', id: 'minimal_flow', flow_ref: 'fill-search' },
    {
      type: 'if',
      id: 'minimal_if',
      condition: { operator: 'exists', source: 'native', target: { locator_ref: 'page' } },
      then: [{ type: 'action', id: 'child', action: { kind: 'key', key: 'BACK' } }],
    },
    {
      type: 'loop',
      id: 'minimal_loop',
      max_iterations: 2,
      steps: [{ type: 'action', id: 'child', action: { kind: 'key', key: 'BACK' } }],
    },
    {
      type: 'loop',
      id: 'null_condition_loop',
      max_iterations: 2,
      condition: null,
      steps: [{ type: 'action', id: 'child', action: { kind: 'key', key: 'BACK' } }],
    },
  ] satisfies Step[])('preserves absent optional fields and explicit null in $id', (step) => {
    const h = renderFields(documentWithStep(step));
    expect(h.change).not.toHaveBeenCalled();
    edit(screen.getByRole('textbox', { name: '步骤 ID' }), `${step.id}_edited`);
    expect(h.last()).toEqual({ ...step, id: `${step.id}_edited` });
    h.validated();
  });
});

describe('input values', () => {
  test.each([
    { mode: 'text', fields: [['文本值', 'Coffee\nNo sugar']], expected: 'Coffee\nNo sugar' },
    { mode: 'param_ref', fields: [['参数引用', 'product']], expected: { param_ref: 'product' } },
    { mode: 'var_ref', fields: [['变量引用', 'label']], expected: { var_ref: 'label' } },
    {
      mode: 'data_ref',
      fields: [
        ['数据集引用', 'cart'],
        ['数据键', 'product'],
      ],
      expected: { data_ref: 'cart', key: 'product' },
    },
    {
      mode: 'secret_ref',
      fields: [['秘密引用', 'checkout_password']],
      expected: { secret_ref: 'checkout_password' },
    },
  ])(
    'edits $mode as a typed input value, not serialized JSON',
    async ({ mode, fields, expected }) => {
      const user = userEvent.setup();
      const h = renderFields(
        inputDocument(mode === 'text' ? { param_ref: 'product' } : 'Coffee'),
        'use-return',
      );
      await choose(user, screen.getByRole('combobox', { name: '值类型' }), mode);
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      h.validated();
      for (const [label, value] of fields) {
        if (!label || value === undefined) throw new Error('Missing value field fixture');
        edit(screen.getByRole('textbox', { name: label }), value);
      }
      expect(h.last()).toEqual({
        ...h.original,
        action: {
          kind: 'input',
          target: { locator_ref: 'search' },
          mode: 'replace',
          text: expected,
        },
      });
      expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
      h.validated();
      await choose(user, screen.getByRole('combobox', { name: '输入模式' }), 'append');
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      expect(h.last()).toMatchObject({ action: { mode: 'append', text: expected } });
      h.validated();
    },
  );
});

describe('declared parameter hints', () => {
  test('offers only compatible input parameters and commits the selected typed reference', async () => {
    const user = userEvent.setup();
    const document = copyDraftCase({
      ...inputDocument({ param_ref: 'product' }),
      parameters: {
        product: { type: 'string', required: true },
        alternate: { type: 'string', required: false, default: '' },
        quantity: { type: 'integer', required: false, default: 0 },
      },
    });
    const h = renderFields(document, 'use-return');
    expect(screen.getByText(/声明类型：string，必填/)).toBeVisible();
    await user.click(screen.getByRole('combobox', { name: '已声明参数' }));
    expect(screen.queryByRole('option', { name: 'quantity (integer)' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'alternate (string)' }));
    expect(h.last()).toMatchObject({ action: { text: { param_ref: 'alternate' } } });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    expect(screen.getByText(/声明类型：string，有默认值/)).toBeVisible();
    h.validated();
  });

  test.each(['constructor', 'toString', '__proto__'])(
    'does not infer a declared parameter from an inherited %s property',
    (name) => {
      const h = renderFields(inputDocument({ param_ref: name }), 'use-return');
      expect(
        screen.getByText('参数尚未声明，请在参数绑定中定义。；此处需要 string。'),
      ).toBeVisible();
      expect(h.caught).not.toHaveBeenCalled();
      expect(h.change).not.toHaveBeenCalled();
    },
  );
});

describe('targets and candidates', () => {
  test('edits a declared reference, converts it to an inline locator, and leaves the declaration intact', async () => {
    const user = userEvent.setup();
    const document = copyDraftCase({
      ...inputDocument(),
      locators: {
        search: inlineLocator,
        product: {
          ...inlineLocator,
          candidates: [{ by: 'res_id', value: 'com.pulse.target:id/product' }],
        },
      },
    });
    const h = renderFields(document, 'use-return');
    const reference = screen.getByRole('combobox', { name: '定位器引用' });
    const list = window.document.getElementById(reference.getAttribute('list') ?? '');
    expect(list?.querySelectorAll('option')).toHaveLength(2);
    edit(reference, 'product');
    expect(h.last()).toMatchObject({ action: { target: { locator_ref: 'product' } } });
    h.validated();
    await choose(user, screen.getByRole('combobox', { name: '目标类型' }), 'locator');
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    expect(h.last()).toMatchObject({
      action: { target: { locator: document.locators['product'] } },
    });
    edit(screen.getByRole('textbox', { name: '定位值' }), 'com.pulse.target:id/updated');
    h.validated();
    expect(h.source.locators).toEqual(document.locators);
    await choose(user, screen.getByRole('combobox', { name: '目标类型' }), 'locator_ref');
    expect(h.last()).toMatchObject({ action: { target: { locator_ref: 'search' } } });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    h.validated();
  });

  test.each(['constructor', 'toString', '__proto__'])(
    'converts a legal prototype-named reference %s using a fallback rather than an inherited property',
    async (reference) => {
      const user = userEvent.setup();
      const h = renderFields(inputDocument('Coffee', { locator_ref: reference }), 'use-return');
      h.validated();
      expect(h.source.locators).toEqual({});
      await choose(user, screen.getByRole('combobox', { name: '目标类型' }), 'locator');
      expect(h.caught).not.toHaveBeenCalled();
      expect(h.last()).toEqual({
        ...h.original,
        action: {
          kind: 'input',
          mode: 'replace',
          text: 'Coffee',
          target: {
            locator: {
              candidates: [{ by: 'text', value: '目标文本' }],
              expect: { unique: true, visible: true },
            },
          },
        },
      });
      h.validated();
    },
  );

  test.each(['res_id', 'text', 'desc', 'xpath', 'ocr_text', 'image'] as const)(
    'changes a string candidate to %s without dropping its optional scope',
    async (by) => {
      const user = userEvent.setup();
      const locator: Locator = {
        ...inlineLocator,
        candidates: [{ by: by === 'text' ? 'res_id' : 'text', value: 'Coffee', scope: 'page' }],
      };
      const h = renderFields(inputDocument('Coffee', { locator }), 'use-return');
      await choose(user, screen.getByRole('combobox', { name: '定位方式' }), by);
      expect(h.last()).toMatchObject({
        action: {
          target: {
            locator: {
              candidates: [{ by, value: 'Coffee', scope: 'page' }],
              expect: locator.expect,
            },
          },
        },
      });
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      edit(screen.getByRole('textbox', { name: '定位值' }), 'Updated target');
      edit(screen.getByRole('textbox', { name: '候选作用域（可选）' }), 'content');
      h.validated();
      edit(screen.getByRole('textbox', { name: '候选作用域（可选）' }), '');
      expect(h.last()).toMatchObject({
        action: { target: { locator: { candidates: [{ by, value: 'Updated target' }] } } },
      });
      const last = h.last();
      if (
        last.type !== 'action' ||
        !('target' in last.action) ||
        !('locator' in last.action.target)
      )
        throw new Error('Missing inline target');
      expect(Object.hasOwn(last.action.target.locator.candidates[0], 'scope')).toBe(false);
      h.validated();
    },
  );

  test('converts text to an editable point and back without stringifying coordinates or losing scope', async () => {
    const user = userEvent.setup();
    const locator: Locator = {
      ...inlineLocator,
      candidates: [{ by: 'text', value: 'Coffee', scope: 'page' }],
    };
    const h = renderFields(inputDocument('Coffee', { locator }), 'use-return');
    await choose(user, screen.getByRole('combobox', { name: '定位方式' }), 'point');
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    const point = { x: 0, y: 360, width: 720, height: 1600 };
    const group = screen.getByRole('group', { name: '坐标与基准尺寸' });
    for (const key of ['x', 'y', 'width', 'height'] as const)
      edit(within(group).getByRole('spinbutton', { name: key }), point[key]);
    expect(h.last()).toMatchObject({
      action: {
        target: { locator: { candidates: [{ by: 'point', value: point, scope: 'page' }] } },
      },
    });
    h.validated();
    await choose(user, screen.getByRole('combobox', { name: '定位方式' }), 'text');
    expect(h.last()).toMatchObject({
      action: {
        target: { locator: { candidates: [{ by: 'text', value: '目标文本', scope: 'page' }] } },
      },
    });
    h.validated();
  });

  test('adds, reorders and removes candidates with boundary guards and independent expect switches', async () => {
    const user = userEvent.setup();
    const h = renderFields(inputDocument('Coffee', { locator: inlineLocator }), 'use-return');
    expectTargetFields(screen.getByRole('group', { name: '目标' }), {
      locator: inlineLocator,
    });
    expect(screen.getByRole('button', { name: '上移候选 1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下移候选 2' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '下移候选 1' }));
    expect(h.last()).toMatchObject({
      action: {
        target: {
          locator: {
            candidates: [...inlineLocator.candidates].reverse(),
            expect: inlineLocator.expect,
          },
        },
      },
    });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    h.validated();
    await user.click(screen.getByRole('button', { name: '上移候选 2' }));
    await user.click(screen.getByRole('button', { name: '添加定位候选' }));
    expect(h.last()).toMatchObject({
      action: {
        target: {
          locator: { candidates: [...inlineLocator.candidates, { by: 'text', value: '目标文本' }] },
        },
      },
    });
    h.validated();
    await user.click(screen.getByRole('button', { name: '删除候选 3' }));
    await user.click(screen.getByRole('button', { name: '删除候选 1' }));
    expect(screen.getByRole('button', { name: '删除候选 1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '上移候选 1' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下移候选 1' })).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: '要求唯一' }));
    await user.click(screen.getByRole('checkbox', { name: '要求可见' }));
    expect(h.last()).toMatchObject({
      action: {
        target: {
          locator: {
            candidates: [inlineLocator.candidates[1]],
            expect: { unique: false, visible: true },
          },
        },
      },
    });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    h.validated();
  });
});

describe('checkpoint operator fields', () => {
  const convertedFields: { operator: Predicate['operator']; fields: Record<string, unknown> }[] = [
    { operator: 'exists', fields: {} },
    {
      operator: 'not_exists',
      fields: {
        scope: { locator_ref: 'product' },
        observation_window_ms: 1000,
        poll_interval_ms: 500,
      },
    },
    { operator: 'visible', fields: { expected: true } },
    { operator: 'enabled', fields: { expected: true } },
    { operator: 'checked', fields: { expected: true } },
    { operator: 'text_equals', fields: { expected: '' } },
    { operator: 'contains', fields: { expected: '' } },
    { operator: 'regex', fields: { expected: '' } },
    { operator: 'count_eq', fields: { expected: 1 } },
    { operator: 'count_gt', fields: { expected: 1 } },
    { operator: 'count_lt', fields: { expected: 1 } },
    {
      operator: 'number_cmp',
      fields: { expected: 0, comparison: 'eq', unit: 'unit', precision: 2 },
    },
    { operator: 'page_is', fields: {} },
    { operator: 'semantic', fields: { expected: '' } },
  ];
  test.each(convertedFields)(
    'converts to $operator with only its schema fields and preserves common fields',
    async ({ operator, fields }) => {
      const user = userEvent.setup();
      const base = { target: { locator_ref: 'product' }, source: 'ocr', timeout_ms: 7500 } as const;
      const initial: Predicate =
        operator === 'number_cmp'
          ? {
              ...base,
              operator: 'not_exists',
              scope: { locator_ref: 'page' },
              observation_window_ms: 1500,
              poll_interval_ms: 250,
            }
          : {
              ...base,
              operator: 'number_cmp',
              expected: -12.5,
              comparison: 'gte',
              unit: 'CNY',
              precision: 3,
            };
      const h = renderFields(conditionDocument(initial));
      await choose(user, screen.getByRole('combobox', { name: '检查运算符' }), operator);
      expect(h.last()).toEqual({ ...h.original, condition: { ...base, operator, ...fields } });
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      h.validated();
    },
  );

  test.each([
    {
      initial: { operator: 'text_equals', expected: { param_ref: 'product' } },
      operator: 'contains',
      expected: { param_ref: 'product' },
    },
    {
      initial: { operator: 'regex', expected: { data_ref: 'cart', key: 'product' } },
      operator: 'semantic',
      expected: { data_ref: 'cart', key: 'product' },
    },
    {
      initial: { operator: 'count_eq', expected: { param_ref: 'quantity' } },
      operator: 'number_cmp',
      expected: { param_ref: 'quantity' },
    },
    {
      initial: {
        operator: 'number_cmp',
        expected: { data_ref: 'cart', key: 'total' },
        comparison: 'eq',
        unit: 'CNY',
        precision: 2,
      },
      operator: 'count_gt',
      expected: { data_ref: 'cart', key: 'total' },
    },
    {
      initial: { operator: 'number_cmp', expected: 0, comparison: 'eq', unit: 'CNY', precision: 2 },
      operator: 'count_eq',
      expected: 0,
    },
    {
      initial: {
        operator: 'number_cmp',
        expected: -1,
        comparison: 'eq',
        unit: 'CNY',
        precision: 2,
      },
      operator: 'count_lt',
      expected: 1,
    },
    {
      initial: {
        operator: 'number_cmp',
        expected: 1.5,
        comparison: 'eq',
        unit: 'CNY',
        precision: 2,
      },
      operator: 'count_eq',
      expected: 1,
    },
    { initial: { operator: 'visible', expected: false }, operator: 'checked', expected: false },
  ] as const)(
    'preserves compatible expectations from $initial.operator to $operator',
    async ({ initial, operator, expected }) => {
      const user = userEvent.setup();
      const h = renderFields(
        conditionDocument({
          ...initial,
          source: 'native',
          target: { locator_ref: 'product' },
        } as Predicate),
      );
      await choose(user, screen.getByRole('combobox', { name: '检查运算符' }), operator);
      expect(h.last()).toMatchObject({ condition: { operator, expected } });
      h.validated();
    },
  );

  test('edits not_exists target and scope independently, including its observation window and optional intervals', async () => {
    const user = userEvent.setup();
    const checkpoint = operators.checks.find((item) => item.condition.operator === 'not_exists');
    if (!checkpoint) throw new Error('Missing absence fixture');
    const h = renderFields(checkpointDocument(checkpoint));
    edit(
      within(screen.getByRole('group', { name: '目标' })).getByRole('combobox', {
        name: '定位器引用',
      }),
      'banner_new',
    );
    edit(
      within(screen.getByRole('group', { name: '消失检查作用域' })).getByRole('combobox', {
        name: '定位器引用',
      }),
      'page_new',
    );
    edit(screen.getByRole('spinbutton', { name: '观察窗口（ms）' }), 1800);
    edit(screen.getByRole('spinbutton', { name: '轮询间隔（ms）' }), 250);
    edit(screen.getByRole('spinbutton', { name: '条件超时（ms）' }), 5000);
    expect(h.last()).toMatchObject({
      checkpoint: {
        condition: {
          operator: 'not_exists',
          target: { locator_ref: 'banner_new' },
          scope: { locator_ref: 'page_new' },
          observation_window_ms: 1800,
          poll_interval_ms: 250,
          timeout_ms: 5000,
        },
      },
    });
    h.validated();
    edit(screen.getByRole('spinbutton', { name: '轮询间隔（ms）' }), '');
    edit(screen.getByRole('spinbutton', { name: '条件超时（ms）' }), '');
    expect(h.last()).toEqual({
      ...h.original,
      checkpoint: {
        ...checkpoint,
        condition: {
          operator: 'not_exists',
          source: 'native',
          target: { locator_ref: 'banner_new' },
          scope: { locator_ref: 'page_new' },
          observation_window_ms: 1800,
        },
      },
    });
    h.validated();
    await choose(user, screen.getByRole('combobox', { name: '观察来源' }), 'webview');
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    h.validated();
  });

  test('edits number_cmp comparison, decimal expectation, unit and zero precision without losing checkpoint metadata', async () => {
    const user = userEvent.setup();
    const checkpoint = operators.checks.find((item) => item.condition.operator === 'number_cmp');
    if (!checkpoint) throw new Error('Missing numeric fixture');
    const h = renderFields(checkpointDocument(checkpoint));
    await choose(user, screen.getByRole('combobox', { name: '数值比较' }), 'lte');
    edit(screen.getByRole('textbox', { name: '单位' }), 'USD');
    edit(screen.getByRole('spinbutton', { name: '精度' }), 0);
    await choose(user, screen.getByRole('combobox', { name: '值类型' }), 'number');
    edit(screen.getByRole('spinbutton', { name: '数值' }), -4.5);
    expect(h.last()).toEqual({
      ...h.original,
      checkpoint: {
        ...checkpoint,
        condition: {
          ...checkpoint.condition,
          comparison: 'lte',
          unit: 'USD',
          precision: 0,
          expected: -4.5,
        },
      },
    });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
    h.validated();
  });

  test('materializes the required semantic timeout when switching from a condition with no explicit timeout', async () => {
    const user = userEvent.setup();
    const h = renderFields(
      conditionDocument({ operator: 'exists', source: 'vision', target: { locator_ref: 'page' } }),
    );
    await choose(user, screen.getByRole('combobox', { name: '检查运算符' }), 'semantic');
    expect(h.last()).toEqual({
      ...h.original,
      condition: {
        operator: 'semantic',
        source: 'vision',
        target: { locator_ref: 'page' },
        expected: '',
        timeout_ms: 10000,
      },
    });
    const timeout = screen.getByRole('spinbutton', { name: '条件超时（ms）' });
    expect(timeout).toHaveAttribute('min', '1');
    expect(timeout).not.toHaveAttribute('placeholder');
    expect(screen.getByRole('spinbutton', { name: '步骤超时（ms）' })).toHaveValue(null);
    edit(timeout, 12000);
    edit(
      screen.getByRole('textbox', { name: '文本值' }),
      'The summary contains the declared product.',
    );
    h.validated();
  });

  test.each(
    operators.checks.filter(
      (item) => 'expected' in item.condition && typeof item.condition.expected !== 'boolean',
    ),
  )(
    'restricts fixed $id expectations to literal, parameter and data modes, never variable or secret',
    async (checkpoint) => {
      const user = userEvent.setup();
      const h = renderFields(checkpointDocument(checkpoint));
      await user.click(screen.getByRole('combobox', { name: '值类型' }));
      const options = within(await screen.findByRole('listbox'))
        .getAllByRole('option')
        .map((option) => option.textContent);
      expect(options).toEqual(
        checkpoint.condition.operator.startsWith('count_') ||
          checkpoint.condition.operator === 'number_cmp'
          ? ['number', 'param_ref', 'data_ref']
          : ['text', 'param_ref', 'data_ref'],
      );
      expect(screen.queryByRole('option', { name: 'var_ref' })).not.toBeInTheDocument();
      expect(screen.queryByRole('option', { name: 'secret_ref' })).not.toBeInTheDocument();
      await user.keyboard('{Escape}');
      expect(h.change).not.toHaveBeenCalled();
      h.validated();
    },
  );

  test.each(['text_equals', 'count_eq'] as const)(
    'edits permitted literal and reference expectations for %s',
    async (operator) => {
      const user = userEvent.setup();
      const checkpoint = operators.checks.find((item) => item.condition.operator === operator);
      if (!checkpoint) throw new Error('Missing fixed expectation fixture');
      const h = renderFields(checkpointDocument(checkpoint));
      await choose(user, screen.getByRole('combobox', { name: '值类型' }), 'param_ref');
      edit(
        screen.getByRole('textbox', { name: '参数引用' }),
        operator === 'count_eq' ? 'quantity' : 'product',
      );
      h.validated();
      await choose(user, screen.getByRole('combobox', { name: '值类型' }), 'data_ref');
      edit(screen.getByRole('textbox', { name: '数据集引用' }), 'cart');
      edit(
        screen.getByRole('textbox', { name: '数据键' }),
        operator === 'count_eq' ? 'quantity' : 'product',
      );
      expect(h.last()).toMatchObject({
        checkpoint: {
          condition: {
            expected: { data_ref: 'cart', key: operator === 'count_eq' ? 'quantity' : 'product' },
          },
        },
      });
      h.validated();
      await choose(
        user,
        screen.getByRole('combobox', { name: '值类型' }),
        operator === 'count_eq' ? 'number' : 'text',
      );
      edit(
        screen.getByRole(operator === 'count_eq' ? 'spinbutton' : 'textbox', {
          name: operator === 'count_eq' ? '数值' : '文本值',
        }),
        operator === 'count_eq' ? 3 : 'Coffee',
      );
      h.validated();
    },
  );

  test.each(['visible', 'enabled', 'checked'] as const)(
    'edits %s boolean expectation and checkpoint required independently with immediate save',
    async (operator) => {
      const user = userEvent.setup();
      const checkpoint = operators.checks.find((item) => item.condition.operator === operator);
      if (!checkpoint) throw new Error('Missing state fixture');
      const h = renderFields(checkpointDocument(checkpoint));
      await user.click(screen.getByRole('checkbox', { name: '期望为真' }));
      await user.click(screen.getByRole('checkbox', { name: '必须通过' }));
      expect(h.last()).toEqual({
        ...h.original,
        checkpoint: {
          ...checkpoint,
          required: false,
          condition: { ...checkpoint.condition, expected: false },
        },
      });
      expect(h.change.mock.calls.every(([, save]) => save === true)).toBe(true);
      h.validated();
    },
  );
});

describe('flow arguments and return bindings', () => {
  function flowDocument() {
    const step = findStep(control.steps, 'search-product');
    if (step?.type !== 'flow') throw new Error('Missing flow fixture');
    return copyDraftCase(
      replaceStep(control, step, {
        ...step,
        arguments: { query: { param_ref: 'product' }, quantity: 2, binding_1: false },
        bind: { label: 'label', amount: 'amount', binding_1: 'existing' },
      }),
    );
  }

  test('edits flow reference, argument names and typed values, and return names and variables without touching other entries', async () => {
    const user = userEvent.setup();
    const h = renderFields(flowDocument(), 'search-product');
    edit(screen.getByRole('textbox', { name: '公共流程引用' }), 'fill-search-updated');
    const args = within(screen.getByRole('group', { name: '流程参数' }));
    const name = args.getAllByRole('textbox', { name: '绑定名称' })[0];
    if (!name) throw new Error('Missing query binding');
    edit(name, 'search_query');
    const valueGroup = args.getAllByRole('group', { name: '参数值' })[0];
    if (!valueGroup) throw new Error('Missing query value');
    await choose(user, within(valueGroup).getByRole('combobox', { name: '值类型' }), 'data_ref');
    edit(within(valueGroup).getByRole('textbox', { name: '数据集引用' }), 'cart');
    edit(within(valueGroup).getByRole('textbox', { name: '数据键' }), 'product');
    const bind = within(screen.getByRole('group', { name: '流程返回绑定' }));
    const bindName = bind.getAllByRole('textbox', { name: '绑定名称' })[0];
    const variable = bind.getAllByRole('textbox', { name: '调用端变量' })[0];
    if (!bindName || !variable) throw new Error('Missing return binding');
    edit(bindName, 'result_label');
    edit(variable, 'selected_label');
    expect(h.last()).toEqual({
      ...h.original,
      flow_ref: 'fill-search-updated',
      arguments: {
        search_query: { data_ref: 'cart', key: 'product' },
        quantity: 2,
        binding_1: false,
      },
      bind: { result_label: 'selected_label', amount: 'amount', binding_1: 'existing' },
    });
    h.validated();
  });

  test('edits numeric and boolean argument scalars without changing their types or other bindings', async () => {
    const user = userEvent.setup();
    const h = renderFields(flowDocument(), 'search-product');
    const fields = within(screen.getByRole('group', { name: '流程参数' }));
    const quantity = fields.getAllByRole('group', { name: '参数值' })[1];
    if (!quantity) throw new Error('Missing numeric argument');
    edit(within(quantity).getByRole('spinbutton', { name: '数值' }), 3);
    expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
    await user.click(fields.getByRole('checkbox', { name: '布尔值' }));
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    expect(h.last()).toEqual({
      ...h.original,
      arguments: { query: { param_ref: 'product' }, quantity: 3, binding_1: true },
    });
    h.validated();
  });

  test.each([
    { group: '流程参数', existing: 'quantity', original: 'query' },
    { group: '流程返回绑定', existing: 'amount', original: 'label' },
  ])(
    'rejects a $group name collision without changing either entry and exposes an accessible error',
    ({ group, existing, original }) => {
      const h = renderFields(flowDocument(), 'search-product');
      const fields = within(screen.getByRole('group', { name: group }));
      const name = fields.getAllByRole('textbox', { name: '绑定名称' })[0];
      if (!name) throw new Error('Missing binding name');
      edit(name, existing);
      expect(fields.getByRole('alert')).toHaveTextContent('绑定名称重复，未覆盖已有绑定。');
      expect(fields.getAllByRole('textbox', { name: '绑定名称' })[1]).toHaveValue(existing);
      expect(h.change).not.toHaveBeenCalled();
      expect(h.last()).toEqual(h.original);
      h.validated();
      edit(name, `${original}_updated`);
      expect(fields.queryByRole('alert')).not.toBeInTheDocument();
      expect(name).toHaveValue(`${original}_updated`);
      h.validated();
    },
  );

  test.each([
    {
      group: '流程参数',
      intended: 'query2',
      field: 'arguments',
      expected: { query: { param_ref: 'product' }, quantity: 2, query2: false },
    },
    {
      group: '流程返回绑定',
      intended: 'label2',
      field: 'bind',
      expected: { label: 'label', amount: 'amount', label2: 'existing' },
    },
  ] as const)(
    'preserves every typed character when a legal $group name temporarily shares an existing prefix',
    async ({ group, intended, field, expected }) => {
      const user = userEvent.setup();
      const h = renderFields(flowDocument(), 'search-product');
      const fields = within(screen.getByRole('group', { name: group }));
      const name = fields.getAllByRole('textbox', { name: '绑定名称' })[2];
      if (!name) throw new Error('Missing binding to rename');
      await user.clear(name);
      await user.type(name, intended);
      await user.tab();
      h.validated();
      expect(name).toHaveValue(intended);
      expect(h.last()).toEqual({ ...h.original, [field]: expected });
      expect(fields.queryByRole('alert')).not.toBeInTheDocument();
      expect(h.change.mock.calls.every(([, save]) => save === undefined)).toBe(true);
    },
  );

  test.each(['流程参数', '流程返回绑定'] as const)(
    'adds a noncolliding %s name and removes only that binding with save=true',
    async (group) => {
      const user = userEvent.setup();
      const h = renderFields(flowDocument(), 'search-product');
      const fields = within(screen.getByRole('group', { name: group }));
      await user.click(fields.getByRole('button', { name: `添加${group}` }));
      expect(
        fields
          .getAllByRole('textbox', { name: '绑定名称' })
          .map((field) => (field as HTMLInputElement).value),
      ).toEqual(
        group === '流程参数'
          ? ['query', 'quantity', 'binding_1', 'binding_2']
          : ['label', 'amount', 'binding_1', 'binding_2'],
      );
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      h.validated();
      await user.click(fields.getByRole('button', { name: '删除绑定 binding_2' }));
      expect(h.last()).toEqual(h.original);
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      h.validated();
    },
  );
});

describe('action, extraction, loop and AI fields', () => {
  test.each([
    { kind: 'tap', fields: { target: { locator_ref: 'search' } } },
    { kind: 'long', fields: { target: { locator_ref: 'search' }, duration_ms: 500 } },
    { kind: 'input', fields: { target: { locator_ref: 'search' }, text: '', mode: 'replace' } },
    {
      kind: 'swipe',
      fields: {
        start: { x: 0, y: 0, width: 1080, height: 2400 },
        end: { x: 0, y: 0, width: 1080, height: 2400 },
        duration_ms: 500,
      },
    },
    { kind: 'key', fields: { key: 'BACK' } },
    { kind: 'launch', fields: { app_id: 'app.id' } },
    { kind: 'stop', fields: { app_id: 'app.id' } },
    { kind: 'clear', fields: { app_id: 'app.id' } },
  ] as const)(
    'changes action to $kind using its own legal fields, preserving step timeout',
    async ({ kind, fields }) => {
      const user = userEvent.setup();
      const action: ActionStep['action'] =
        kind === 'tap'
          ? {
              kind: 'input',
              target: { locator_ref: 'search' },
              text: { param_ref: 'product' },
              mode: 'append',
            }
          : { kind: 'tap', target: { locator_ref: 'search' } };
      const h = renderFields(
        documentWithStep({ type: 'action', id: 'action_fields', timeout_ms: 6000, action }),
      );
      await choose(user, screen.getByRole('combobox', { name: '动作类型' }), kind);
      expect(h.last()).toEqual({ ...h.original, action: { kind, ...fields } });
      expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
      h.validated();
    },
  );

  test('edits golden long duration without replacing its target or injecting step timeout', () => {
    const h = renderFields(control, 'long-product');
    edit(screen.getByRole('spinbutton', { name: '动作时长（ms）' }), 650);
    expect(h.last()).toEqual({
      type: 'action',
      id: 'long-product',
      action: { kind: 'long', target: { locator_ref: 'product' }, duration_ms: 650 },
    });
    expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
    h.validated();
  });

  test('edits every swipe coordinate and duration as numbers while retaining the other endpoint', () => {
    const h = renderFields(control, 'swipe');
    const start = { x: 0, y: 1200, width: 720, height: 1600 };
    const end = { x: 300, y: 0, width: 1440, height: 3200 };
    for (const [label, point] of [
      ['滑动起点', start],
      ['滑动终点', end],
    ] as const) {
      const fields = within(screen.getByRole('group', { name: label }));
      for (const key of ['x', 'y', 'width', 'height'] as const)
        edit(fields.getByRole('spinbutton', { name: key }), point[key]);
    }
    edit(screen.getByRole('spinbutton', { name: '动作时长（ms）' }), 700);
    expect(h.last()).toEqual({
      ...h.original,
      action: { kind: 'swipe', start, end, duration_ms: 700 },
    });
    h.validated();
  });

  test('edits key text without changing action kind', () => {
    const h = renderFields(control, 'back');
    edit(screen.getByRole('textbox', { name: '按键' }), 'HOME');
    expect(h.last()).toEqual({ ...h.original, action: { kind: 'key', key: 'HOME' } });
    h.validated();
  });

  test.each(['launch', 'stop', 'clear'] as const)(
    'edits %s application ID and preserves it across another application action',
    async (kind) => {
      const user = userEvent.setup();
      const h = renderFields(
        documentWithStep({
          type: 'action',
          id: 'app_fields',
          action: { kind, app_id: 'com.pulse.target' },
        }),
      );
      edit(screen.getByRole('textbox', { name: '应用 ID' }), 'com.pulse.checkout');
      await choose(
        user,
        screen.getByRole('combobox', { name: '动作类型' }),
        kind === 'launch' ? 'stop' : 'launch',
      );
      expect(h.last()).toEqual({
        ...h.original,
        action: { kind: kind === 'launch' ? 'stop' : 'launch', app_id: 'com.pulse.checkout' },
      });
      h.validated();
    },
  );

  test.each(['text', 'count', 'enabled', 'checked'] as const)(
    'edits extract %s attribute, observation source, target and output variable',
    async (attribute) => {
      const user = userEvent.setup();
      const step = findStep(control.steps, 'iteration-label');
      if (step?.type !== 'extract') throw new Error('Missing extract fixture');
      const document = copyDraftCase(
        replaceStep(control, step, { ...step, attribute: attribute === 'text' ? 'count' : 'text' }),
      );
      const h = renderFields(document, step.id);
      await choose(user, screen.getByRole('combobox', { name: '提取属性' }), attribute);
      await choose(user, screen.getByRole('combobox', { name: '提取来源' }), 'ocr');
      edit(screen.getByRole('combobox', { name: '定位器引用' }), 'product_updated');
      edit(screen.getByRole('textbox', { name: '保存为变量' }), 'extracted_value');
      expect(h.last()).toEqual({
        ...step,
        attribute,
        source: 'ocr',
        target: { locator_ref: 'product_updated' },
        as: 'extracted_value',
      });
      h.validated();
    },
  );

  test('edits a bounded loop and toggles its optional condition without altering its body', async () => {
    const user = userEvent.setup();
    const h = renderFields(control, 'bounded-observation');
    if (h.original.type !== 'loop') throw new Error('Missing loop fixture');
    edit(screen.getByRole('spinbutton', { name: '最多迭代次数' }), 3);
    expect(h.last()).toEqual({ ...h.original, max_iterations: 3 });
    h.validated();
    await user.click(screen.getByRole('checkbox', { name: '使用循环条件' }));
    const { condition: omitted, ...withoutCondition } = h.original;
    expect(omitted).toBeDefined();
    expect(h.last()).toEqual({ ...withoutCondition, max_iterations: 3 });
    expect(screen.queryByRole('combobox', { name: '检查运算符' })).not.toBeInTheDocument();
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    h.validated();
    await user.click(screen.getByRole('checkbox', { name: '使用循环条件' }));
    expect(screen.getByRole('combobox', { name: '检查运算符' })).toHaveTextContent('exists');
    await choose(user, screen.getByRole('combobox', { name: '检查运算符' }), 'count_lt');
    edit(screen.getByRole('spinbutton', { name: '数值' }), 3);
    expect(h.last()).toMatchObject({
      max_iterations: 3,
      steps: h.original.steps,
      condition: { operator: 'count_lt', expected: 3 },
    });
    h.validated();
  });

  test('edits AI goal and explicit timeout while preserving full fixed checks, then adds unique checks with a last-check guard', async () => {
    const user = userEvent.setup();
    const h = renderFields(ai);
    if (h.original.type !== 'ai_step') throw new Error('Missing AI fixture');
    const timeout = screen.getByRole('spinbutton', { name: '步骤超时（ms）' });
    expect(timeout).not.toHaveAttribute('placeholder');
    expect(timeout).toHaveAttribute('min', '1');
    expect(screen.getByRole('button', { name: '删除检查点 1' })).toBeDisabled();
    edit(
      screen.getByRole('textbox', { name: 'AI 步骤目标' }),
      'Inspect the declared cart.\nDo not submit or pay.',
    );
    edit(timeout, 45000);
    expect(h.last()).toEqual({
      ...h.original,
      goal: 'Inspect the declared cart.\nDo not submit or pay.',
      timeout_ms: 45000,
    });
    h.validated();
    edit(screen.getByRole('textbox', { name: '检查点 ID' }), 'summary_updated');
    await user.click(screen.getByRole('checkbox', { name: '必须通过' }));
    expect(h.last()).toMatchObject({
      checks: [{ ...h.original.checks[0], id: 'summary_updated', required: false }],
    });
    h.validated();
    await user.click(screen.getByRole('button', { name: '添加检查点' }));
    await user.click(screen.getByRole('button', { name: '添加检查点' }));
    expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
    const last = h.last();
    if (last.type !== 'ai_step') throw new Error('Missing AI edit');
    expect(last.checks.map((item) => item.id)).toEqual([
      'summary_updated',
      'checkpoint_1',
      'checkpoint_2',
    ]);
    expect(last.checks[0]?.condition).toEqual(h.original.checks[0].condition);
    h.validated();
    await user.click(screen.getByRole('button', { name: '删除检查点 2' }));
    await user.click(screen.getByRole('button', { name: '删除检查点 2' }));
    expect(screen.getByRole('button', { name: '删除检查点 1' })).toBeDisabled();
    h.validated();
  });
});

test('text and numeric changes defer save to the owner blur/submit while choices and switches request save=true', async () => {
  const user = userEvent.setup();
  const h = renderFields(inputDocument('Coffee', { locator: inlineLocator }), 'use-return');
  const text = screen.getByRole('textbox', { name: '文本值' });
  await user.type(text, ' with milk');
  expect(text).toHaveValue('Coffee with milk');
  expect(h.change.mock.calls.every(([, save]) => save === undefined)).toBe(true);
  expect(h.blur).not.toHaveBeenCalled();
  expect(h.submit).not.toHaveBeenCalled();
  await user.tab();
  expect(h.blur).toHaveBeenLastCalledWith(h.last());
  h.change.mockClear();
  edit(screen.getByRole('spinbutton', { name: '步骤超时（ms）' }), 5000);
  expect(h.change).toHaveBeenLastCalledWith(h.last(), undefined);
  edit(screen.getByRole('spinbutton', { name: '步骤超时（ms）' }), '');
  expect(Object.hasOwn(h.last(), 'timeout_ms')).toBe(false);
  h.validated();
  await choose(user, screen.getByRole('combobox', { name: '输入模式' }), 'append');
  expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
  await user.click(screen.getByRole('checkbox', { name: '要求唯一' }));
  expect(h.change).toHaveBeenLastCalledWith(h.last(), true);
  await user.click(screen.getByRole('button', { name: '提交字段' }));
  expect(h.submit).toHaveBeenCalledOnce();
  expect(h.submit).toHaveBeenLastCalledWith(h.last());
  h.validated();
});

test.each([
  {
    name: 'input locator and point fields',
    document: inputDocument('Coffee', { locator: inlineLocator }),
    id: 'use-return',
  },
  { name: 'AI semantic checkpoint fields', document: ai, id: 'inspect-summary' },
])(
  '$name has named controls and passes non-layout accessibility checks',
  async ({ document, id }) => {
    const h = renderFields(document, id);
    const canvas = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    try {
      const result = await axe.run(h.container, {
        rules: { 'color-contrast': { enabled: false } },
      });
      expect(result.violations).toEqual([]);
    } finally {
      canvas.mockRestore();
    }
    h.validated();
  },
);
