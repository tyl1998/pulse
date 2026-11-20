import type { Case, Flow, Target } from '@pulse/schema';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { simulatedInspectorProjectState } from '../inspector/inspectorFixtures';
import { CaseProblemsPanel, CaseProblemsProvider } from './CaseProblems';
import {
  createCaseDraftStore,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
} from './caseDraftStore';
import { copyDraftCase } from './caseDraftValidation';
import { caseTextRange } from './caseText';
import type { createMonacoCaseEditor } from './monacoCaseEditor';

const monaco = vi.hoisted(() => ({ create: vi.fn(), dispose: vi.fn() }));
vi.mock('./monacoCaseEditor', () => ({ createMonacoCaseEditor: monaco.create }));
const stores: CaseDraftStore[] = [];
const target: Target = {
  locator: {
    candidates: [{ by: 'text', value: 'Target' }],
    expect: { unique: true, visible: true },
  },
};

function source(overrides: Partial<Case> = {}): Case {
  return copyDraftCase({
    $schema: 'urn:pulse:schema:1-draft:case',
    case_id: 'starter',
    name: 'Starter',
    goal: 'Fixed criteria',
    profile_refs: ['android-fixture'],
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

beforeEach(() => {
  window.localStorage.clear();
  monaco.create.mockReset();
  monaco.dispose.mockReset();
  monaco.create.mockImplementation(
    (host: HTMLElement, options: Parameters<typeof createMonacoCaseEditor>[1]) => {
      const input = document.createElement('textarea');
      input.className = 'monaco-editor';
      input.dataset['testid'] = 'mock-monaco';
      input.setAttribute('aria-label', '用例 JSON 文本');
      input.value = options.text;
      input.readOnly = options.readOnly;
      input.onchange = () => options.onChange(input.value);
      input.onblur = options.onBlur;
      host.append(input);
      return {
        setText: (text: string) => {
          input.value = text;
        },
        setReadOnly: (readOnly: boolean) => {
          input.readOnly = readOnly;
        },
        setIssue: (issue: unknown) => input.setAttribute('aria-invalid', String(!!issue)),
        focusText: (start: number, end: number, backward: boolean) => {
          input.focus();
          input.setSelectionRange(start, end, backward ? 'backward' : 'forward');
        },
        layout: () => undefined,
        dispose: () => {
          monaco.dispose();
          input.remove();
        },
      };
    },
  );
});

afterEach(() => {
  cleanup();
  stores.splice(0).forEach((store) => store.dispose());
  vi.restoreAllMocks();
});

async function setup({
  original = source(),
  initialView = 'steps',
  flows = {},
  flowIds = Object.keys(flows),
  storage = window.localStorage,
  editor = true,
}: {
  original?: Case;
  initialView?: 'steps' | 'three' | 'json';
  flows?: Record<string, Flow>;
  flowIds?: string[];
  storage?: CaseDraftStoreDeps['storage'];
  editor?: boolean;
} = {}) {
  let state = simulatedInspectorProjectState();
  if (!state.project) throw new Error('Missing project fixture');
  state = {
    ...state,
    selectedCaseId: 'starter',
    project: {
      ...state.project,
      case_ids: ['starter', 'other'],
      entry_cases: ['starter'],
      flow_ids: flowIds,
      manifest: { constraints: { forbidden_actions: ['clear'] }, secret_refs: [], resources: {} },
    },
  };
  const listeners = new Set<() => void>();
  const projects = {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    select: (caseId: string) => {
      state = { ...state, selectedCaseId: caseId };
      listeners.forEach((listener) => listener());
    },
  };
  const getFlow = vi.fn(async (_projectId: string, id: string) => {
    const flow = Object.hasOwn(flows, id) ? flows[id] : undefined;
    if (!flow) throw new Error('Synthetic offline dependency');
    return flow;
  });
  const store = createCaseDraftStore({
    projects,
    storage,
    now: () => 1000,
    getClient: async () => ({
      getProject: async () => state.project,
      getCase: async (_projectId, caseId) => ({ ...original, case_id: caseId, name: caseId }),
      getFlow,
      verifyConnection: async () => undefined,
    }),
  });
  stores.push(store);
  await store.load();
  const DraftPanel = editor ? (await import('./CaseDraftPanel')).CaseDraftPanel : null;
  const view = render(
    <main>
      <h1>Case editor</h1>
      <h2>Editor dock</h2>
      <CaseProblemsProvider store={store} project={state.project}>
        {DraftPanel ? <DraftPanel store={store} initialView={initialView} /> : null}
        <CaseProblemsPanel />
      </CaseProblemsProvider>
    </main>,
  );
  return { store, projects, getFlow, view };
}

const panel = () => screen.getByRole('region', { name: '用例问题' });
const jsonInput = async () => await screen.findByTestId<HTMLTextAreaElement>('mock-monaco');
const selectedText = (input: HTMLTextAreaElement) =>
  input.value.slice(input.selectionStart, input.selectionEnd);

test('lists all rejected JSON schema errors and locates the selected field across views without discarding text', async () => {
  const { store } = await setup({ initialView: 'json' });
  const original = store.getSnapshot().draft;
  const input = await jsonInput();
  const text = JSON.stringify(
    {
      ...original,
      steps: [
        { type: 'action', id: 'launch', timeout_ms: 0, action: { kind: 'launch', app_id: 42 } },
      ],
    },
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  expect(within(panel()).getAllByRole('button', { name: /^定位 Schema 错误/ })).toHaveLength(2);
  expect(store.getSnapshot().draft).toBe(original);
  await userEvent.click(screen.getByRole('tab', { name: '步骤' }));
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 Schema 错误：Case\/steps\/0\/action\/app_id/,
    }),
  );
  expect(screen.getByRole('tab', { name: 'JSON' })).toHaveAttribute('data-state', 'active');
  expect(input).toHaveFocus();
  expect(selectedText(input)).toBe('42');
  expect(input.value).toBe(text);
  await userEvent.click(screen.getByRole('tab', { name: '步骤' }));
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveAttribute('data-state', 'active');
  await userEvent.click(
    within(panel()).getByRole('button', { name: /定位 Schema 错误：Case\/steps\/0\/timeout_ms/ }),
  );
  expect(selectedText(input)).toBe('0');
  await userEvent.click(
    within(screen.getByRole('tabpanel', { name: 'JSON' })).getByRole('button', {
      name: '恢复已保存文本',
    }),
  );
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
  expect(store.getSnapshot().draft).toBe(original);
});

test('lists and independently locates both errors inside an inline locator union', async () => {
  const { store } = await setup({ initialView: 'json' });
  const input = await jsonInput();
  const text = JSON.stringify(
    {
      ...store.getSnapshot().draft,
      steps: [
        {
          type: 'action',
          id: 'tap',
          action: {
            kind: 'tap',
            target: {
              locator: {
                candidates: [
                  { by: 'text', value: '' },
                  { by: 'desc', value: '' },
                ],
                expect: { unique: true, visible: true },
              },
            },
          },
        },
      ],
    },
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  expect(within(panel()).getAllByRole('button', { name: /^定位 Schema 错误/ })).toHaveLength(2);
  await userEvent.click(within(panel()).getByRole('button', { name: /candidates\/1\/value/ }));
  const first = text.indexOf('""');
  expect(input.selectionStart).toBe(text.indexOf('""', first + 2));
  expect(selectedText(input)).toBe('""');
});

test('locates a nested three-section error in its retained array and clears it only on restore', async () => {
  const { store } = await setup({ initialView: 'three' });
  const original = store.getSnapshot().draft;
  const input = screen.getByRole<HTMLTextAreaElement>('textbox', { name: '执行文本' });
  const text = JSON.stringify(
    [
      {
        type: 'loop',
        id: 'loop',
        max_iterations: 1,
        steps: [
          {
            type: 'action',
            id: 'nested',
            timeout_ms: 0,
            action: { kind: 'launch', app_id: 'com.pulse.target' },
          },
        ],
      },
    ],
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  await userEvent.click(screen.getByRole('tab', { name: '步骤' }));
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 Schema 错误：Case\/steps\/0\/steps\/0\/timeout_ms/,
    }),
  );
  expect(screen.getByRole('tab', { name: '三段式' })).toHaveAttribute('data-state', 'active');
  expect(input).toHaveFocus();
  expect(selectedText(input)).toBe('0');
  expect(store.getSnapshot().draft).toBe(original);
  const section = screen.getByRole('region', { name: '执行分区' });
  await userEvent.click(within(section).getByRole('button', { name: '恢复已保存文本' }));
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
});

test('locates the correct checkpoint form field while JSON still contains only the saved criteria', async () => {
  const original = source({
    checks: [0, 1].map((index) => ({
      id: `amount-${index}`,
      required: true,
      condition: {
        operator: 'number_cmp',
        source: 'native',
        target,
        comparison: 'eq',
        expected: 1,
        unit: 'CNY',
        precision: 2,
      },
    })),
  });
  const { store } = await setup({ original });
  await userEvent.click(screen.getByRole('button', { name: '编辑验收检查' }));
  const form = screen.getByRole('form', { name: '编辑验收检查' });
  const fields = within(form).getAllByRole<HTMLInputElement>('spinbutton', { name: '精度' });
  const second = fields[1];
  if (!second) throw new Error('Missing second checkpoint');
  fireEvent.change(second, { target: { value: '2.5' } });
  fireEvent.blur(second);
  const problem = within(panel()).getByRole('button', {
    name: /定位 Schema 错误：Case\/checks\/1\/condition\/precision/,
  });
  await userEvent.click(screen.getByRole('tab', { name: 'JSON' }));
  const json = await jsonInput();
  expect(JSON.parse(json.value).checks[1].condition.precision).toBe(2);
  await userEvent.click(problem);
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveAttribute('data-state', 'active');
  expect(second).toHaveFocus();
  fireEvent.change(second, { target: { value: '3' } });
  fireEvent.blur(second);
  expect(store.getSnapshot().draft?.checks[1]?.condition).toHaveProperty('precision', 3);
  expect(store.getSnapshot().draft?.checks[0]?.condition).toHaveProperty('precision', 2);
  expect(
    within(panel()).queryByRole('button', { name: /^定位 Schema 错误/ }),
  ).not.toBeInTheDocument();
});

test('an earlier JSON step jump cannot steal focus from a later form problem', async () => {
  const original = source({
    steps: [
      {
        type: 'check',
        id: 'check',
        checkpoint: {
          id: 'amount',
          required: true,
          condition: {
            operator: 'number_cmp',
            source: 'native',
            target,
            comparison: 'eq',
            expected: 1,
            unit: 'CNY',
            precision: 2,
          },
        },
      },
    ],
  });
  await setup({ original, initialView: 'json' });
  await jsonInput();
  await userEvent.click(screen.getByRole('combobox', { name: 'JSON 对应步骤' }));
  await userEvent.click(screen.getByRole('option', { name: 'check' }));
  await userEvent.click(screen.getByRole('button', { name: '跳转到对应步骤' }));
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 check' }));
  const precision = within(
    screen.getByRole('form', { name: '编辑步骤 check' }),
  ).getByRole<HTMLInputElement>('spinbutton', { name: '精度' });
  fireEvent.change(precision, { target: { value: '2.5' } });
  fireEvent.blur(precision);
  await userEvent.click(screen.getByRole('tab', { name: 'JSON' }));
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /Case\/steps\/0\/checkpoint\/condition\/precision/,
    }),
  );
  expect(precision).toHaveFocus();
});

test.each(['candidate', 'swipe', 'scope'] as const)(
  'locates the second repeated field in a %s form',
  async (scenario) => {
    const original =
      scenario === 'candidate'
        ? source({
            steps: [
              {
                type: 'action',
                id: 'step',
                action: {
                  kind: 'tap',
                  target: {
                    locator: {
                      candidates: [
                        { by: 'text', value: 'First' },
                        { by: 'desc', value: 'Second' },
                      ],
                      expect: { unique: true, visible: true },
                    },
                  },
                },
              },
            ],
          })
        : scenario === 'swipe'
          ? source({
              steps: [
                {
                  type: 'action',
                  id: 'step',
                  action: {
                    kind: 'swipe',
                    duration_ms: 500,
                    start: { x: 0, y: 0, width: 100, height: 100 },
                    end: { x: 50, y: 50, width: 100, height: 100 },
                  },
                },
              ],
            })
          : source({
              steps: [
                {
                  type: 'wait',
                  id: 'step',
                  condition: {
                    operator: 'not_exists',
                    source: 'native',
                    target,
                    scope: target,
                    observation_window_ms: 1000,
                  },
                },
              ],
            });
    await setup({ original });
    await userEvent.click(screen.getByRole('button', { name: '编辑步骤 step' }));
    const form = screen.getByRole('form', { name: '编辑步骤 step' });
    const fields =
      scenario === 'swipe'
        ? within(form).getAllByRole<HTMLInputElement>('spinbutton', { name: 'width' })
        : within(form).getAllByRole<HTMLInputElement>('textbox', { name: '定位值' });
    const second = fields[1];
    if (!second) throw new Error('Missing repeated field');
    fireEvent.change(second, { target: { value: scenario === 'swipe' ? '0' : '' } });
    fireEvent.blur(second);
    const path =
      scenario === 'candidate'
        ? 'Case/steps/0/action/target/locator/candidates/1/value'
        : scenario === 'swipe'
          ? 'Case/steps/0/action/end/width'
          : 'Case/steps/0/condition/scope/locator/candidates/0/value';
    await userEvent.click(
      within(panel()).getByRole('button', { name: new RegExp(`定位 Schema 错误：${path}`) }),
    );
    expect(second).toHaveFocus();
    expect(fields[0]).not.toHaveFocus();
  },
);

test('syntax errors remain locatable when Monaco cannot load', async () => {
  monaco.create.mockImplementation(() => {
    throw new Error('Synthetic worker unavailable');
  });
  const { store } = await setup({ initialView: 'json' });
  await screen.findByText(/Monaco 加载失败/);
  const input = screen.getByRole<HTMLTextAreaElement>('textbox', { name: '用例 JSON 文本' });
  const text = '{"steps": [}';
  fireEvent.change(input, { target: { value: text } });
  await userEvent.click(
    within(panel()).getByRole('button', { name: /定位 Schema 错误：Case JSON 语法无效/ }),
  );
  expect(input).toHaveFocus();
  expect(input.selectionStart).toBeGreaterThan(0);
  expect(store.getSnapshot().persistence).toBe('none');
  expect(input.value).toBe(text);
});

test('canonical unresolved references are locatable, refresh after edits, and return on undo', async () => {
  const original = source({
    steps: [
      {
        type: 'action',
        id: 'input',
        action: { kind: 'input', target, text: { param_ref: 'query' }, mode: 'replace' },
      },
    ],
  });
  const { store } = await setup({ original });
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 未解析引用：Case\/steps\/0\/action\/text\/param_ref/,
    }),
  );
  const input = await jsonInput();
  await waitFor(() => expect(input).toHaveFocus());
  expect(selectedText(input)).toBe('"query"');
  const next = {
    ...store.getSnapshot().draft,
    parameters: { query: { type: 'string', required: false, default: 'Coffee' } },
  };
  fireEvent.change(input, { target: { value: JSON.stringify(next, null, 2) } });
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
  act(() => {
    store.undo();
  });
  expect(within(panel()).getByRole('button', { name: /参数 query 未声明/ })).toBeVisible();
  act(() => {
    store.redo();
  });
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
});

test('confirmed reference problems follow original step IDs rather than reordered unsaved indices', async () => {
  const original = source({
    steps: [
      {
        type: 'action',
        id: 'input',
        action: { kind: 'input', target, text: { param_ref: 'query' }, mode: 'replace' },
      },
    ],
  });
  const { store } = await setup({ original, initialView: 'json' });
  const input = await jsonInput();
  const text = JSON.stringify(
    {
      ...store.getSnapshot().draft,
      steps: [
        {
          type: 'action',
          id: 'new',
          timeout_ms: 0,
          action: { kind: 'input', target, text: { param_ref: 'other' }, mode: 'replace' },
        },
        ...original.steps,
      ],
    },
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 未解析引用：Case\/steps\/0\/action\/text\/param_ref/,
    }),
  );
  expect(selectedText(input)).toBe('"query"');
  expect(input.value).toBe(text);
  expect(store.getSnapshot().draft?.steps).toEqual(original.steps);
});

test('a confirmed step missing from rejected JSON is located in saved steps without discarding the buffer', async () => {
  const original = source({
    steps: [
      {
        type: 'action',
        id: 'input',
        action: { kind: 'input', target, text: { param_ref: 'query' }, mode: 'replace' },
      },
    ],
  });
  const { store } = await setup({ original, initialView: 'json' });
  const input = await jsonInput();
  const text = JSON.stringify(
    {
      ...store.getSnapshot().draft,
      steps: [
        {
          type: 'action',
          id: 'new',
          timeout_ms: 0,
          action: { kind: 'launch', app_id: 'com.pulse.target' },
        },
      ],
    },
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 未解析引用：Case\/steps\/0\/action\/text\/param_ref/,
    }),
  );
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveAttribute('data-state', 'active');
  expect(document.activeElement).toHaveAttribute('data-step-id', 'input');
  expect(input.value).toBe(text);
  await userEvent.click(screen.getByRole('tab', { name: 'JSON' }));
  expect(input.value).toBe(text);
});

test('legal duplicate locator candidates still locate their own confirmed JSON ranges', async () => {
  const candidate = { by: 'point' as const, value: { x: 100, y: 0, width: 100, height: 100 } };
  const original = source({
    locators: {
      duplicate: { candidates: [candidate, candidate], expect: { unique: true, visible: true } },
    },
  });
  await setup({ original });
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /Case\/locators\/duplicate\/candidates\/1\/value/,
    }),
  );
  const input = await jsonInput();
  await waitFor(() => expect(input).toHaveFocus());
  expect(screen.getByRole('tab', { name: 'JSON' })).toHaveAttribute('data-state', 'active');
  const path = 'Case/locators/duplicate/candidates/1/value';
  const range = caseTextRange(input.value, path);
  expect(input.selectionStart).toBe(range.offset);
  expect(input.selectionEnd).toBe(range.offset + range.length);
});

test('old saved-checkpoint fallback focus cannot override a new form problem', async () => {
  const original = source({
    checks: [
      {
        id: 'check',
        required: true,
        condition: {
          operator: 'text_equals',
          source: 'native',
          target,
          expected: { param_ref: 'missing' },
        },
      },
    ],
  });
  const { store } = await setup({ original, initialView: 'json' });
  const input = await jsonInput();
  fireEvent.change(input, {
    target: {
      value: JSON.stringify({
        ...store.getSnapshot().draft,
        checks: [],
        steps: [{ ...original.steps[0], timeout_ms: 0 }],
      }),
    },
  });
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /定位 未解析引用：Case\/checks\/0\/condition\/expected\/param_ref/,
    }),
  );
  expect(document.activeElement).toHaveAttribute('data-case-field', 'checks');
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const timeout = within(screen.getByRole('form', { name: '编辑步骤 launch' })).getByRole(
    'spinbutton',
    { name: '步骤超时（ms）' },
  );
  fireEvent.change(timeout, { target: { value: '0' } });
  fireEvent.blur(timeout);
  await userEvent.click(screen.getByRole('tab', { name: 'JSON' }));
  await userEvent.click(
    within(panel()).getByRole('button', {
      name: /Case\/steps\/0\/timeout_ms.*未保存 · 编辑步骤 launch/,
    }),
  );
  expect(timeout).toHaveFocus();
});

test('scalar input errors locate their value rather than a same-named parameter declaration', async () => {
  const original = source({
    parameters: {
      first: { type: 'integer', required: true },
      second: { type: 'integer', required: true },
    },
    inputs: { first: 1, second: 2 },
  });
  const { store } = await setup({ original });
  await userEvent.click(screen.getByRole('button', { name: '编辑参数绑定' }));
  const values = within(screen.getByRole('group', { name: '初始输入' })).getAllByRole(
    'spinbutton',
    { name: '数值' },
  );
  const first = values[0];
  const second = values[1];
  if (!first || !second) throw new Error('Missing input values');
  fireEvent.change(first, { target: { value: '' } });
  fireEvent.change(second, { target: { value: '' } });
  fireEvent.blur(second);
  await userEvent.click(
    within(panel()).getByRole('button', { name: /定位 Schema 错误：Case\/inputs\/first/ }),
  );
  expect(first).toHaveFocus();
  expect(store.getSnapshot().draft?.inputs).toEqual(original.inputs);
});

test('flow bindings and transitive model dependencies are diagnosed only from confirmed read-only definitions', async () => {
  const flow: Flow = {
    $schema: 'urn:pulse:schema:1-draft:flow',
    flow_id: 'flow',
    name: 'Flow',
    parameters: { query: { type: 'string', required: true } },
    locators: {},
    returns: {},
    steps: [{ type: 'flow', id: 'nested-call', flow_ref: 'nested' }],
  };
  const nested: Flow = {
    ...flow,
    flow_id: 'nested',
    parameters: {},
    steps: [
      {
        type: 'ai_step',
        id: 'ai',
        timeout_ms: 1000,
        goal: 'Goal',
        checks: [
          {
            id: 'exists',
            required: true,
            condition: { operator: 'exists', source: 'native', target },
          },
        ],
      },
    ],
  };
  const original = source({ steps: [{ type: 'flow', id: 'call', flow_ref: 'flow' }] });
  const { store, getFlow } = await setup({ original, flows: { flow, nested }, editor: false });
  await within(panel()).findByRole('button', { name: /缺少必填实参 query/ });
  expect(
    within(panel()).getByRole('button', { name: /公共流程 nested 的步骤 ai 需要模型参与/ }),
  ).toBeVisible();
  expect(getFlow.mock.calls.map((call) => call[1])).toEqual(['flow', 'nested']);
  expect(store.getSnapshot().persistence).toBe('none');
});

test('flow chains beyond the local depth limit remain explicitly unverified', async () => {
  const flows = Object.fromEntries(
    Array.from({ length: 17 }, (_, index) => [
      `F${index}`,
      {
        $schema: 'urn:pulse:schema:1-draft:flow',
        flow_id: `F${index}`,
        name: `F${index}`,
        parameters: {},
        locators: {},
        returns: {},
        steps:
          index < 16
            ? [{ type: 'flow', id: 'nested', flow_ref: `F${index + 1}` }]
            : [
                {
                  type: 'ai_step',
                  id: 'ai',
                  timeout_ms: 1000,
                  goal: 'Goal',
                  checks: [
                    {
                      id: 'check',
                      required: true,
                      condition: { operator: 'exists', source: 'native', target },
                    },
                  ],
                },
              ],
      },
    ]),
  ) as Record<string, Flow>;
  const original = source({ steps: [{ type: 'flow', id: 'call', flow_ref: 'F0' }] });
  const { getFlow } = await setup({ original, flows, editor: false });
  await within(panel()).findByRole('button', { name: /公共流程 F16 超过本地读取深度上限/ });
  expect(getFlow).toHaveBeenCalledTimes(16);
  expect(within(panel()).getByText(/AI 依赖与禁止动作尚未验证/)).toBeVisible();
  expect(within(panel()).getByRole('status')).not.toHaveTextContent('正在只读核对');
});

test('a failed local-draft restore does not pretend flow definitions are still loading', async () => {
  const original = source({ steps: [{ type: 'flow', id: 'call', flow_ref: 'flow' }] });
  const storage = { getItem: () => 'not-json', setItem: () => undefined };
  const { store, getFlow } = await setup({ original, flowIds: ['flow'], storage, editor: false });
  expect(store.getSnapshot().status).toBe('error');
  expect(store.getSnapshot().original).not.toBeNull();
  expect(within(panel()).getByRole('status')).toHaveTextContent(
    '草稿读取或恢复未确认，未核对公共流程',
  );
  expect(getFlow).not.toHaveBeenCalled();
});

test('dependency-read failures are unverified warnings rather than a successful preflight', async () => {
  const original = source({ steps: [{ type: 'flow', id: 'call', flow_ref: 'flow' }] });
  const { store } = await setup({ original, flowIds: ['flow'], editor: false });
  await within(panel()).findByRole('button', { name: /公共流程 flow 读取未确认/ });
  expect(within(panel()).getByText(/实参、返回值与模型依赖尚未验证/)).toBeVisible();
  expect(store.getSnapshot().canInsert).toBe(true);
  expect(store.getSnapshot().persistence).toBe('none');
});

test('a legal prototype-like flow ID does not crash the problem panel before or after its read', async () => {
  const flow: Flow = {
    $schema: 'urn:pulse:schema:1-draft:flow',
    flow_id: 'constructor',
    name: 'Flow',
    parameters: {},
    locators: {},
    returns: {},
    steps: [
      { type: 'action', id: 'launch', action: { kind: 'launch', app_id: 'com.pulse.target' } },
    ],
  };
  const original = source({ steps: [{ type: 'flow', id: 'call', flow_ref: 'constructor' }] });
  const { getFlow } = await setup({ original, flows: { constructor: flow }, editor: false });
  await waitFor(() => expect(getFlow).toHaveBeenCalledWith(expect.any(String), 'constructor'));
  expect(panel()).toBeVisible();
  await within(panel()).findByText('未发现可确认的草稿问题。');
});

test('save failures retain the local text and publish a focusable unsaved-edit problem', async () => {
  const storage = {
    getItem: () => null,
    setItem: () => {
      throw new Error('Synthetic disk full');
    },
  };
  const { store } = await setup({ initialView: 'json', storage });
  const original = store.getSnapshot().draft;
  const input = await jsonInput();
  const text = JSON.stringify(
    {
      ...original,
      steps: [
        {
          type: 'action',
          id: 'replacement',
          action: { kind: 'launch', app_id: 'com.pulse.target' },
        },
      ],
    },
    null,
    2,
  );
  fireEvent.change(input, { target: { value: text } });
  expect(store.getSnapshot().draft).toBe(original);
  expect(store.getSnapshot().persistence).toBe('error');
  await userEvent.click(within(panel()).getByRole('button', { name: /^定位 未保存编辑/ }));
  expect(input).toHaveFocus();
  expect(input.value).toBe(text);
});

test('switching the selected case clears rejected-buffer diagnostics and never writes into the new case', async () => {
  const { store, projects } = await setup({ initialView: 'json' });
  const input = await jsonInput();
  fireEvent.change(input, { target: { value: '{' } });
  expect(within(panel()).getByRole('button', { name: /^定位 Schema 错误/ })).toBeVisible();
  await act(async () => {
    projects.select('other');
    await store.load();
  });
  expect(store.getSnapshot().draft?.case_id).toBe('other');
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
  expect(store.getSnapshot().persistence).toBe('none');
  expect(input.isConnected).toBe(false);
});

test('the issue list is keyboard-operable, category-filtered and accessible', async () => {
  const original = source({
    steps: [{ type: 'action', id: 'clear', action: { kind: 'clear', app_id: 'com.pulse.target' } }],
  });
  const { view } = await setup({ original });
  const problem = within(panel()).getByRole('button', { name: /^定位 禁止动作冲突/ });
  act(() => problem.focus());
  await userEvent.keyboard('{Enter}');
  expect(screen.getByRole('tab', { name: 'JSON' })).toHaveAttribute('data-state', 'active');
  await jsonInput();
  await userEvent.click(within(panel()).getByRole('combobox', { name: '问题分类' }));
  await userEvent.click(screen.getByRole('option', { name: 'Schema 错误' }));
  expect(within(panel()).getByText('此分类下没有问题。')).toBeVisible();
  expect(within(panel()).queryByRole('button', { name: /^定位/ })).not.toBeInTheDocument();
  const result = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations).toEqual([]);
});
