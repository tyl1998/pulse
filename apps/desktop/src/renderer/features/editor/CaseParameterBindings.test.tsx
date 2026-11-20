import type { Case, Parameter, Value } from '@pulse/schema';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { simulatedInspectorProjectState } from '../inspector/inspectorFixtures';
import { CaseParameterBindings } from './CaseParameterBindings';
import {
  caseDraftStorageKey,
  createCaseDraftStore,
  useCaseDraft,
  type CaseDraftStore,
} from './caseDraftStore';
import { copyDraftCase } from './caseDraftValidation';

const stores: CaseDraftStore[] = [];
function source(overrides: Partial<Case> = {}) {
  return copyDraftCase({
    $schema: 'urn:pulse:schema:1-draft:case',
    case_id: 'starter',
    name: 'Bindings',
    goal: 'Fixed criteria',
    source: 'Source fixture',
    original_text: 'Original authoring input',
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

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  cleanup();
  stores.splice(0).forEach((store) => store.dispose());
  vi.restoreAllMocks();
});

async function setup(original = source(), canEdit = true) {
  let state = simulatedInspectorProjectState();
  if (!state.project) throw new Error('Missing project fixture');
  state = {
    ...state,
    selectedCaseId: 'starter',
    project: { ...state.project, case_ids: ['starter', 'other'], flow_ids: [] },
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
  const getCase = vi.fn(async (_projectId: string, caseId: string) => ({
    ...original,
    case_id: caseId,
  }));
  const store = createCaseDraftStore({
    projects,
    storage: window.localStorage,
    now: () => 1000,
    getClient: async () => ({
      getProject: async () => state.project,
      getCase,
      verifyConnection: async () => undefined,
    }),
  });
  stores.push(store);
  await store.load();
  function Harness() {
    const { state: draft } = useCaseDraft(store);
    return (
      <main>
        <h1>Parameter editor</h1>
        <h2>Editor dock</h2>
        {draft.draft ? (
          <CaseParameterBindings
            key={`${draft.scope?.caseId}:${draft.historyRevision}`}
            document={draft.draft}
            store={store}
            canEdit={canEdit && draft.canInsert}
          />
        ) : null}
      </main>
    );
  }
  const view = render(<Harness />);
  return { store, projects, getCase, view };
}

async function open() {
  await userEvent.click(screen.getByRole('button', { name: '编辑参数绑定' }));
}
async function choose(label: string, option: string) {
  await userEvent.click(screen.getByRole('combobox', { name: label }));
  await userEvent.click(screen.getByRole('option', { name: option }));
}
function draft(store: CaseDraftStore) {
  const current = store.getSnapshot().draft;
  if (!current) throw new Error('Missing draft');
  return current;
}

test('viewing parameters never materializes defaults or initial values', async () => {
  const original = source({
    parameters: {
      query: { type: 'string', required: true },
      count: { type: 'integer', required: false, default: 0 },
    },
    inputs: { query: '' },
  });
  const { store, getCase } = await setup(original);
  await open();
  expect(draft(store)).toEqual(original);
  expect(store.getSnapshot().persistence).toBe('none');
  expect(window.localStorage.length).toBe(0);
  expect(getCase).toHaveBeenCalledTimes(1);
});

test('creates and renames a parameter, edits required/default type, and adds a matching typed initial input', async () => {
  const { store } = await setup();
  const original = draft(store);
  await open();
  await userEvent.click(screen.getByRole('button', { name: '添加参数声明' }));
  const name = screen.getByRole('textbox', { name: '参数名称' });
  await userEvent.clear(name);
  await userEvent.type(name, 'quantity');
  await userEvent.tab();
  await choose('参数类型', 'integer');
  expect(draft(store).parameters.quantity).toEqual({ type: 'integer', required: true });
  await userEvent.click(screen.getByRole('checkbox', { name: '必填参数' }));
  expect(draft(store).parameters.quantity).toEqual({
    type: 'integer',
    required: false,
    default: 0,
  });
  const field = screen.getByRole('spinbutton', { name: '默认值' });
  const confirmed = draft(store);
  fireEvent.change(field, { target: { value: '2.5' } });
  fireEvent.blur(field);
  expect(draft(store)).toBe(confirmed);
  expect(screen.getByRole('alert')).toHaveTextContent('可选参数必须提供与声明类型一致');
  fireEvent.change(field, { target: { value: '3' } });
  fireEvent.blur(field);
  await userEvent.click(screen.getByRole('button', { name: '添加初始输入' }));
  expect(draft(store).parameters.quantity?.default).toBe(3);
  expect(draft(store).inputs.quantity).toBe(0);
  expect(store.getSnapshot().original).toBe(original);
  expect(original.parameters).toEqual({});
  const scope = store.getSnapshot().scope;
  if (!scope) throw new Error('Missing scope');
  expect(
    JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null'),
  ).toMatchObject({ version: 4, format: 'case-parameter-bindings', original });
});

test.each<[{ type: Parameter['type']; mode: string; value: Value }]>([
  [{ type: 'string', mode: 'text', value: '' }],
  [{ type: 'integer', mode: 'number', value: 0 }],
  [{ type: 'number', mode: 'number', value: 3.25 }],
  [{ type: 'boolean', mode: 'boolean', value: false }],
  [{ type: 'string', mode: 'data_ref', value: { data_ref: 'cart', key: 'query' } }],
  [{ type: 'string', mode: 'secret_ref', value: { secret_ref: 'password' } }],
])(
  'edits an initial $mode input without losing scalar types or requesting external values',
  async ({ type, mode, value }) => {
    const { store, getCase } = await setup(
      source({ parameters: { query: { type, required: true } } }),
    );
    await open();
    await userEvent.click(screen.getByRole('button', { name: '添加初始输入' }));
    await choose('值类型', mode);
    if (typeof value === 'number') {
      const field = screen.getByRole('spinbutton', { name: '数值' });
      fireEvent.change(field, { target: { value: String(value) } });
      fireEvent.blur(field);
    } else if (typeof value === 'string') {
      const field = screen.getByRole('textbox', { name: '文本值' });
      fireEvent.change(field, { target: { value } });
      fireEvent.blur(field);
    } else if (typeof value === 'object') {
      const fields =
        'data_ref' in value
          ? ([
              ['数据集引用', value.data_ref],
              ['数据键', value.key],
            ] as const)
          : 'secret_ref' in value
            ? ([['秘密引用', value.secret_ref]] as const)
            : null;
      if (!fields) throw new Error('Unsupported initial reference fixture');
      for (const [label, content] of fields) {
        const field = screen.getByRole('textbox', { name: label });
        fireEvent.change(field, { target: { value: content } });
        fireEvent.blur(field);
      }
    }
    expect(draft(store).inputs.query).toEqual(value);
    expect(getCase).toHaveBeenCalledTimes(1);
  },
);

test('initial-input modes never offer parameter or variable references', async () => {
  await setup(source({ parameters: { query: { type: 'string', required: true } } }));
  await open();
  await userEvent.click(screen.getByRole('button', { name: '添加初始输入' }));
  await userEvent.click(screen.getByRole('combobox', { name: '值类型' }));
  expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
    'text',
    'number',
    'boolean',
    'data_ref',
    'secret_ref',
  ]);
  expect(screen.queryByRole('option', { name: 'param_ref' })).not.toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'var_ref' })).not.toBeInTheDocument();
});

test('a mismatched initial scalar stays local and does not overwrite the last confirmed binding', async () => {
  const { store } = await setup(
    source({
      parameters: { query: { type: 'string', required: true } },
      inputs: { query: 'Saved' },
    }),
  );
  await open();
  const confirmed = draft(store);
  await choose('值类型', 'boolean');
  expect(draft(store)).toBe(confirmed);
  expect(screen.getByRole('alert')).toHaveTextContent('初始输入与声明的参数类型不匹配');
  expect(store.getSnapshot().canInsert).toBe(true);
  await userEvent.click(screen.getByRole('button', { name: '恢复已保存' }));
  expect(screen.getByRole('textbox', { name: '文本值' })).toHaveValue('Saved');
});

test('required and optional boolean defaults remain explicit and typed', async () => {
  const { store } = await setup(
    source({ parameters: { flag: { type: 'boolean', required: true } } }),
  );
  await open();
  await userEvent.click(screen.getByRole('checkbox', { name: '必填参数' }));
  expect(draft(store).parameters.flag).toEqual({
    type: 'boolean',
    required: false,
    default: false,
  });
  await userEvent.click(screen.getByRole('checkbox', { name: '默认值' }));
  expect(draft(store).parameters.flag?.default).toBe(true);
  await userEvent.click(screen.getByRole('checkbox', { name: '必填参数' }));
  expect(draft(store).parameters.flag).toEqual({ type: 'boolean', required: true });
  expect(screen.queryByRole('checkbox', { name: '默认值' })).not.toBeInTheDocument();
});

test('a parameter-name collision cannot overwrite another declaration', async () => {
  const original = source({
    parameters: {
      query: { type: 'string', required: true },
      count: { type: 'integer', required: false, default: 0 },
    },
  });
  const { store } = await setup(original);
  await open();
  const group = within(screen.getByRole('group', { name: '参数声明' }));
  const name = group.getAllByRole('textbox', { name: '参数名称' })[0];
  if (!name) throw new Error('Missing name field');
  fireEvent.change(name, { target: { value: 'count' } });
  fireEvent.blur(name);
  expect(draft(store).parameters).toEqual(original.parameters);
  expect(group.getByRole('alert')).toHaveTextContent('绑定名称重复');
  expect(window.localStorage.length).toBe(0);
});

test('undo/redo and case changes cannot keep stale parameter edits in the current form', async () => {
  const { store, projects } = await setup();
  await open();
  await userEvent.click(screen.getByRole('button', { name: '添加参数声明' }));
  expect(Object.keys(draft(store).parameters)).toEqual(['parameter_1']);
  act(() => {
    store.undo();
  });
  expect(draft(store).parameters).toEqual({});
  act(() => {
    store.redo();
  });
  expect(Object.keys(draft(store).parameters)).toEqual(['parameter_1']);
  await open();
  const name = screen.getByRole('textbox', { name: '参数名称' });
  fireEvent.change(name, { target: { value: '' } });
  await act(async () => {
    projects.select('other');
    await store.load();
  });
  expect(draft(store).case_id).toBe('other');
  expect(draft(store).parameters).toEqual({});
  expect(name.isConnected).toBe(false);
});

test('read-only parameter summaries do not expose editable controls', async () => {
  const { store } = await setup(
    source({ parameters: { query: { type: 'string', required: true } } }),
    false,
  );
  expect(screen.getByRole('button', { name: '编辑参数绑定' })).toBeDisabled();
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(store.getSnapshot().persistence).toBe('none');
});

test('the expanded parameter form has accessible labels and controls', async () => {
  const { view } = await setup(
    source({ parameters: { query: { type: 'string', required: true } }, inputs: { query: '' } }),
  );
  await open();
  const result = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations).toEqual([]);
});
