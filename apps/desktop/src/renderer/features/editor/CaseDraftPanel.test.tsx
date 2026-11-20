import type { ActionStep, Case, CheckStep } from '@pulse/schema';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, expect, test, vi } from 'vitest';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import fillSearch from '../../../../../../fixtures/golden-cases/v1-draft/flows/fill-search.json';
import type { PulseBridge } from '../../../shared/ipc';
import { CaseTree } from '../projects/CaseTree';
import {
  createProjectStore,
  getProjectStore,
  PROJECT_STORAGE_KEY,
  resetProjectStores,
} from '../projects/projectStore';
import { CaseDraftPanel } from './CaseDraftPanel';
import {
  caseDraftStorageKey,
  createCaseDraftStore,
  getCaseDraftStore,
  resetCaseDraftStores,
  type CaseDraftRuntimeClient,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
  useCaseDraft,
} from './caseDraftStore';
import { copyDraftCase } from './caseDraftValidation';
import { findStep, replaceStep } from './stepEditing';

const stores: CaseDraftStore[] = [];
const detail = {
  allowed_apps: ['com.pulse.target'],
  case_ids: ['starter', 'other'],
  data_ids: [],
  default_strategy: 'rule_only',
  entry_cases: ['starter'],
  flow_ids: ['fill-search'],
  manifest: {},
  name: 'Demo project',
  platforms: ['android'],
  profile_ids: ['android-local'],
  project_id: 'demo',
  required_capabilities: [],
  revision: '1',
  root: '/tmp/demo',
};

function sourceCase(caseId = 'starter'): Case {
  return {
    $schema: 'urn:pulse:schema:1-draft:case',
    case_id: caseId,
    name: caseId === 'starter' ? 'Starter case' : 'Other case',
    goal: 'Keep the original checks and inputs',
    parameters: {},
    inputs: {},
    profile_refs: ['android-local'],
    locators: {},
    preconditions: [],
    steps: [
      { type: 'action', id: 'launch', action: { kind: 'launch', app_id: 'com.pulse.target' } },
    ],
    checks: [],
  };
}

const insertedTap = {
  type: 'action',
  id: 'inspector-tap',
  action: {
    kind: 'tap',
    target: {
      locator: {
        candidates: [{ by: 'res_id', value: 'com.pulse.target:id/submit' }],
        expect: { unique: true, visible: true },
      },
    },
  },
} satisfies ActionStep;

const insertedCheck: CheckStep = {
  type: 'check',
  id: 'inspector-check',
  checkpoint: {
    id: 'inspector-exists',
    required: true,
    condition: { operator: 'exists', source: 'native', target: insertedTap.action.target },
  },
};

function bridge(): PulseBridge {
  return {
    app: { getInfo: async () => ({ version: '1', platform: 'darwin' }) },
    runtime: {
      getConnection: async () => ({ port: 4311, token: 'local-token', pid: 9, version: '1.0.0' }),
      getStatus: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
      ensureRunning: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
    },
    projects: {
      pickDirectory: async () => '/tmp/demo',
      createProject: async () => ({
        root: '/tmp/demo',
        projectId: 'demo',
        fileCount: 1,
        totalBytes: 1,
      }),
      importProject: async () => null,
      exportProject: async () => null,
    },
  };
}

function mockRuntime() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    if (path === '/projects' && init?.method === 'POST')
      return Response.json(detail, { status: 201 });
    if (path === '/projects/demo') return Response.json(detail);
    if (path === '/projects/demo/cases')
      return Response.json({ starter: sourceCase(), other: sourceCase('other') });
    if (path === '/projects/demo/cases/starter') return Response.json(sourceCase());
    if (path === '/projects/demo/cases/other') return Response.json(sourceCase('other'));
    throw new Error(`Unexpected Runtime request: ${init?.method} ${path}`);
  });
  window.pulse = bridge();
  vi.stubGlobal('fetch', fetchMock);
  window.localStorage.setItem(
    PROJECT_STORAGE_KEY,
    JSON.stringify({ version: 1, root: '/tmp/demo', projectId: 'demo' }),
  );
  return fetchMock;
}

function InsertControl({ store: injected }: { store?: CaseDraftStore }) {
  const { state, store } = useCaseDraft(injected);
  return (
    <button
      type="button"
      disabled={!state.canInsert}
      title={state.reason ?? undefined}
      onClick={() => store.insertStep(insertedTap)}
    >
      插入 tap 步骤
    </button>
  );
}

async function editorCase(
  original = sourceCase(),
  storage: CaseDraftStoreDeps['storage'] = window.localStorage,
) {
  const fetchMock = mockRuntime();
  const projects = getProjectStore();
  await projects.hydrate();
  const getFlow = vi
    .fn<NonNullable<CaseDraftRuntimeClient['getFlow']>>()
    .mockResolvedValue(fillSearch);
  const store = createCaseDraftStore({
    projects,
    getClient: async () => ({
      getProject: async () => detail,
      getCase: async (_projectId, caseId) => ({ ...original, case_id: caseId }),
      getFlow,
      verifyConnection: async () => undefined,
    }),
    storage,
    now: () => 1000,
  });
  stores.push(store);
  await store.load();
  render(<CaseDraftPanel store={store} />);
  return { store, projects, getFlow, fetchMock };
}

function currentDraft(store: CaseDraftStore) {
  const draft = store.getSnapshot().draft;
  if (!draft) throw new Error('missing loaded draft');
  return draft;
}

async function choose(form: HTMLElement, label: string, value: string) {
  await userEvent.click(within(form).getByRole('combobox', { name: label }));
  await userEvent.click(screen.getByRole('option', { name: value }));
}

afterEach(() => {
  cleanup();
  stores.splice(0).forEach((store) => store.dispose());
  resetCaseDraftStores();
  resetProjectStores();
  window.localStorage.clear();
  delete window.pulse;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test('shows no-project state and disables insertion instead of fabricating a blank Case', () => {
  render(
    <>
      <CaseDraftPanel />
      <InsertControl />
    </>,
  );
  expect(screen.getByRole('region', { name: '用例草稿' })).toBeVisible();
  expect(screen.getByText('尚未打开工程')).toBeVisible();
  expect(screen.getByRole('button', { name: '插入 tap 步骤' })).toBeDisabled();
  expect(screen.queryByRole('list', { name: '草稿步骤' })).not.toBeInTheDocument();
});

test('one-click insertion through the default store renders real steps, preserves the original, and reloads the draft', async () => {
  const fetchMock = mockRuntime();
  await getProjectStore().hydrate();
  render(
    <>
      <CaseTree />
      <CaseDraftPanel />
      <InsertControl />
    </>,
  );
  const panel = screen.getByRole('region', { name: '用例草稿' });
  expect(await within(panel).findByRole('heading', { name: 'Starter case' })).toBeVisible();
  expect(within(panel).getByText('原始用例')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '插入 tap 步骤' }));
  expect(within(panel).getByText('草稿已保存')).toBeVisible();
  expect(within(panel).getByText('inspector-tap')).toBeVisible();
  expect(within(panel).getByText('原始 1 步 / 新增 1 步')).toBeVisible();
  act(() => {
    expect(getCaseDraftStore().insertStep(insertedCheck)).toBe(true);
  });
  expect(within(panel).getByText(/check \/ exists \/ native/)).toBeVisible();
  expect(getCaseDraftStore().getSnapshot().draft?.steps).toEqual([
    ...sourceCase().steps,
    insertedTap,
    insertedCheck,
  ]);
  expect(getCaseDraftStore().getSnapshot().original).toEqual(sourceCase());
  expect(within(panel).getByRole('status', { name: '草稿保存状态' })).toHaveTextContent(
    '工程原始用例未修改，步骤不会执行',
  );

  await userEvent.click(screen.getByRole('button', { name: /Other case/ }));
  expect(await within(panel).findByRole('heading', { name: 'Other case' })).toBeVisible();
  expect(within(panel).queryByText('inspector-tap')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: /Starter case/ }));
  expect(await within(panel).findByText('inspector-tap')).toBeVisible();

  cleanup();
  resetCaseDraftStores();
  resetProjectStores();
  await getProjectStore().hydrate();
  render(<CaseDraftPanel />);
  expect(await screen.findByText('inspector-tap')).toBeVisible();
  expect(screen.getByText('inspector-check')).toBeVisible();
  expect(screen.getByText('草稿已保存')).toBeVisible();
  const record: unknown = JSON.parse(
    window.localStorage.getItem(
      caseDraftStorageKey({ root: '/tmp/demo', projectId: 'demo', caseId: 'starter' }),
    ) ?? 'null',
  );
  expect(record).toMatchObject({
    original: sourceCase(),
    draft: { ...sourceCase(), steps: [...sourceCase().steps, insertedTap, insertedCheck] },
  });
  for (const [input, init] of fetchMock.mock.calls) {
    const path = new URL(String(input)).pathname;
    expect(init?.method).toBe(path === '/projects' ? 'POST' : 'GET');
    expect(init?.headers).toMatchObject({ Authorization: 'Bearer local-token' });
  }
});

test('shows saving failure without an added step or success badge and permits explicit read retry', async () => {
  mockRuntime();
  await getProjectStore().hydrate();
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  setItem.mockImplementationOnce(() => {
    throw new Error('QuotaExceededError');
  });
  const store = createCaseDraftStore({
    projects: getProjectStore(),
    getClient: async () => ({
      getProject: async () => detail,
      getCase: async () => sourceCase(),
      verifyConnection: async () => undefined,
    }),
    storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
    now: () => 1000,
  });
  stores.push(store);
  await store.load();
  render(
    <>
      <CaseDraftPanel store={store} />
      <InsertControl store={store} />
    </>,
  );
  await userEvent.click(screen.getByRole('button', { name: '插入 tap 步骤' }));
  expect(screen.getByRole('alert')).toHaveTextContent('草稿保存失败或未确认');
  expect(screen.queryByText('inspector-tap')).not.toBeInTheDocument();
  expect(screen.queryByText('草稿已保存')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '插入 tap 步骤' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '重新读取用例草稿' }));
  await vi.waitFor(() =>
    expect(screen.getByRole('button', { name: '插入 tap 步骤' })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole('button', { name: '插入 tap 步骤' }));
  expect(screen.getByText('草稿已保存')).toBeVisible();
  expect(screen.getByText('inspector-tap')).toBeVisible();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('renders nested steps with structured editing controls but no execution controls', async () => {
  const projects = createProjectStore({
    bridge,
    clientFor: () => ({
      openProject: async () => ({
        ...detail,
        default_strategy: 'rule_only',
        platforms: ['android'],
      }),
      getProject: async () => ({
        ...detail,
        default_strategy: 'rule_only',
        platforms: ['android'],
      }),
      listCases: async () => ({ starter: sourceCase() }),
    }),
    storage: window.localStorage,
  });
  await projects.openFromDialog();
  const original: Case = {
    ...sourceCase(),
    steps: [
      {
        type: 'if',
        id: 'branch',
        condition: insertedCheck.checkpoint.condition,
        then: [insertedTap],
        else: [
          {
            type: 'loop',
            id: 'repeat',
            max_iterations: 2,
            steps: [{ type: 'action', id: 'nested-key', action: { kind: 'key', key: 'BACK' } }],
          },
        ],
      },
    ],
  };
  const store = createCaseDraftStore({
    projects,
    getClient: async () => ({
      getProject: async () => detail,
      getCase: async () => original,
      verifyConnection: async () => undefined,
    }),
    storage: window.localStorage,
    now: () => 1000,
  });
  stores.push(store);
  await store.load();
  render(<CaseDraftPanel store={store} />);
  expect(screen.getByText('branch')).toBeVisible();
  expect(screen.getByText('inspector-tap')).toBeVisible();
  expect(screen.getByText('repeat')).toBeVisible();
  expect(screen.getByText('nested-key')).toBeVisible();
  expect(screen.getAllByText('原始步骤')).toHaveLength(4);
  expect(screen.getByRole('button', { name: '编辑步骤 nested-key' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '删除步骤 nested-key' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: /运行|执行|录制|播放/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 nested-key' }));
  const form = screen.getByRole('form', { name: '编辑步骤 nested-key' });
  const key = within(form).getByRole('textbox', { name: '按键' });
  await userEvent.clear(key);
  await userEvent.type(key, 'HOME');
  await userEvent.tab();
  expect(store.getSnapshot().draft?.steps[0]).toMatchObject({
    else: [{ steps: [{ action: { kind: 'key', key: 'HOME' } }] }],
  });
  expect(store.getSnapshot().original).toEqual(original);
});

test('the default client rejects another Runtime root with the same project and case IDs', async () => {
  const fetchMock = mockRuntime();
  await getProjectStore().hydrate();
  const pulse = window.pulse;
  if (!pulse) throw new Error('missing synthetic bridge');
  pulse.runtime.getConnection = async () => ({
    port: 5311,
    pid: 10,
    token: 'runtime-b-synthetic',
    version: '1.0.0',
  });
  fetchMock.mockImplementation(async (input) => {
    const path = new URL(String(input)).pathname;
    if (path === '/projects/demo') return Response.json({ ...detail, root: '/tmp/runtime-b' });
    throw new Error(`Wrong-root Case should not be requested: ${path}`);
  });
  const store = createCaseDraftStore();
  stores.push(store);
  await store.load();
  expect(store.getSnapshot()).toMatchObject({
    scope: { root: '/tmp/demo', projectId: 'demo', caseId: 'starter' },
    status: 'error',
    original: null,
    draft: null,
    canInsert: false,
  });
  expect(store.getSnapshot().error).toMatch(/工程来源/);
  expect(
    window.localStorage.getItem(
      caseDraftStorageKey({ root: '/tmp/demo', projectId: 'demo', caseId: 'starter' }),
    ),
  ).toBeNull();
});

test.each([
  { port: 5311, pid: 9, token: 'local-token', version: '1.0.0' },
  { port: 4311, pid: 10, token: 'local-token', version: '1.0.0' },
  { port: 4311, pid: 9, token: 'rotated-synthetic', version: '1.0.0' },
  { port: 4311, pid: 9, token: 'local-token', version: '2.0.0' },
])(
  'the default client discards Case bytes when Runtime identity changes during reading $port/$pid/$version',
  async (replaced) => {
    const fetchMock = mockRuntime();
    await getProjectStore().hydrate();
    let changed = false;
    const pulse = window.pulse;
    if (!pulse) throw new Error('missing synthetic bridge');
    pulse.runtime.getConnection = async () =>
      changed ? replaced : { port: 4311, pid: 9, token: 'local-token', version: '1.0.0' };
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input)).pathname;
      if (path === '/projects/demo') return Response.json(detail);
      if (path === '/projects/demo/cases/starter') {
        changed = true;
        return Response.json(sourceCase());
      }
      throw new Error(`Unexpected synthetic request: ${path}`);
    });
    const store = createCaseDraftStore();
    stores.push(store);
    await store.load();
    expect(store.getSnapshot()).toMatchObject({
      status: 'error',
      original: null,
      draft: null,
      canInsert: false,
    });
    expect(store.getSnapshot().error).toMatch(/Runtime 已更换/);
    expect(store.insertStep(insertedTap)).toBe(false);
  },
);

test('autosaves inline edits, restores them after reload, and never writes the Runtime source', async () => {
  const original = sourceCase();
  const { store, fetchMock } = await editorCase(original);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const form = screen.getByRole('form', { name: '编辑步骤 launch' });
  const app = within(form).getByRole('textbox', { name: '应用 ID' });
  await userEvent.clear(app);
  await userEvent.type(app, 'com.example.changed');
  expect(store.getSnapshot().persistence).toBe('none');
  await userEvent.tab();
  expect(currentDraft(store).steps[0]).toMatchObject({ action: { app_id: 'com.example.changed' } });
  expect(store.getSnapshot().persistence).toBe('saved');
  expect(store.getSnapshot().original).toEqual(original);
  const record = JSON.parse(
    window.localStorage.getItem(
      caseDraftStorageKey({ root: detail.root, projectId: detail.project_id, caseId: 'starter' }),
    ) ?? 'null',
  ) as { version: number; draft: Case };
  expect(record.version).toBe(2);
  expect(record.draft).toEqual(currentDraft(store));
  await act(() => store.load());
  expect(currentDraft(store).steps[0]).toMatchObject({ action: { app_id: 'com.example.changed' } });
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  for (const [input, init] of fetchMock.mock.calls) {
    expect(init?.method).toBe(new URL(String(input)).pathname === '/projects' ? 'POST' : 'GET');
  }
});

test('persists explicit input references and preserves unrelated golden-case fields', async () => {
  const original = copyDraftCase({ ...controlFlowCase, case_id: 'starter' });
  const { store } = await editorCase(original);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 use-return' }));
  const form = screen.getByRole('form', { name: '编辑步骤 use-return' });
  await choose(form, '值类型', 'data_ref');
  const data = within(form).getByRole('textbox', { name: '数据集引用' });
  await userEvent.clear(data);
  await userEvent.type(data, 'cart');
  await userEvent.tab();
  const key = within(form).getByRole('textbox', { name: '数据键' });
  await userEvent.clear(key);
  await userEvent.type(key, 'product');
  await userEvent.tab();
  const before = findStep(original.steps, 'use-return');
  const after = findStep(currentDraft(store).steps, 'use-return');
  if (!before || !after) throw new Error('missing input step');
  expect(after).toEqual({
    ...before,
    action: {
      kind: 'input',
      target: { locator_ref: 'search' },
      text: { data_ref: 'cart', key: 'product' },
      mode: 'replace',
    },
  });
  expect(currentDraft(store)).toEqual(replaceStep(original, before, after));
  expect(store.getSnapshot().original).toEqual(original);
});

test('edits procedure checkpoints and declared locator candidates through the same draft', async () => {
  const original = copyDraftCase({
    ...sourceCase(),
    checks: [insertedCheck.checkpoint],
    locators: {
      submit: {
        candidates: [
          { by: 'res_id', value: 'com.pulse.target:id/submit' },
          { by: 'desc', value: 'Submit', scope: null },
        ],
        expect: { unique: true, visible: true },
      },
    },
  });
  const { store } = await editorCase(original);
  await userEvent.click(screen.getByRole('button', { name: '编辑验收检查' }));
  const checkForm = screen.getByRole('form', { name: '编辑验收检查' });
  await userEvent.click(within(checkForm).getByRole('checkbox', { name: '必须通过' }));
  expect(currentDraft(store).checks).toEqual([{ ...insertedCheck.checkpoint, required: false }]);
  await userEvent.click(within(checkForm).getByRole('button', { name: '保存并收起' }));
  await userEvent.click(screen.getByRole('button', { name: '编辑用例定位器' }));
  const form = screen.getByRole('form', { name: '编辑用例定位器' });
  const candidate = within(form).getAllByRole('textbox', { name: '定位值' })[0];
  if (!candidate) throw new Error('missing declared locator candidate');
  await userEvent.clear(candidate);
  await userEvent.type(candidate, 'com.example:id/submit');
  await userEvent.tab();
  await userEvent.click(within(form).getByRole('button', { name: '上移候选 2' }));
  await userEvent.click(within(form).getByRole('checkbox', { name: '要求唯一' }));
  expect(currentDraft(store).locators['submit']).toEqual({
    candidates: [
      { by: 'desc', value: 'Submit', scope: null },
      { by: 'res_id', value: 'com.example:id/submit' },
    ],
    expect: { unique: false, visible: true },
  });
  expect(currentDraft(store).steps).toEqual(original.steps);
  expect(currentDraft(store).inputs).toEqual(original.inputs);
  expect(currentDraft(store).preconditions).toEqual(original.preconditions);
  expect(store.getSnapshot().original).toEqual(original);
  await act(() => store.load());
  expect(currentDraft(store).checks[0]?.required).toBe(false);
  expect(currentDraft(store).locators['submit']?.expect.unique).toBe(false);
});

test('structured fields expose accessible labels and valid control semantics', async () => {
  await editorCase(copyDraftCase({ ...controlFlowCase, case_id: 'starter' }));
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 confirm-check' }));
  const panel = screen.getByRole('region', { name: '用例草稿' });
  const result = await axe.run(panel, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations).toEqual([]);
});

test('reorders within root and nested blocks and confirms deletion without emptying required blocks', async () => {
  const original = copyDraftCase({ ...controlFlowCase, case_id: 'starter' });
  const { store } = await editorCase(original);
  await userEvent.click(screen.getByRole('button', { name: '下移步骤 launch' }));
  expect(
    currentDraft(store)
      .steps.slice(0, 2)
      .map((step) => step.id),
  ).toEqual(['search-product', 'launch']);
  await userEvent.click(screen.getByRole('button', { name: '上移步骤 long-product' }));
  expect(findStep(currentDraft(store).steps, 'bounded-observation')).toMatchObject({
    steps: [{ id: 'long-product' }, { id: 'iteration-label' }],
  });
  expect(screen.getByRole('button', { name: '删除步骤 tap-banner' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '添加步骤到 banner-branch else' }));
  expect(findStep(currentDraft(store).steps, 'banner-branch')).toMatchObject({
    else: [{ id: 'wait-page' }, { type: 'action' }],
  });
  await userEvent.click(screen.getByRole('button', { name: '删除步骤 wait-page' }));
  const dialog = screen.getByRole('dialog', { name: '删除步骤 wait-page' });
  expect(findStep(currentDraft(store).steps, 'wait-page')).toBeDefined();
  await userEvent.click(within(dialog).getByRole('button', { name: '确认从草稿删除' }));
  expect(findStep(currentDraft(store).steps, 'wait-page')).toBeUndefined();
  const branch = findStep(original.steps, 'banner-branch');
  if (branch?.type !== 'if') throw new Error('missing original branch');
  expect(findStep(currentDraft(store).steps, 'banner-branch')).toMatchObject({ then: branch.then });
  expect(store.getSnapshot().original).toEqual(original);
  await act(() => store.load());
  expect(findStep(currentDraft(store).steps, 'wait-page')).toBeUndefined();
});

test('keeps invalid IDs and loop bounds in the editor without overwriting the saved draft', async () => {
  const original = copyDraftCase({ ...controlFlowCase, case_id: 'starter' });
  const { store } = await editorCase(original);
  const before = currentDraft(store);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 bounded-observation' }));
  const form = screen.getByRole('form', { name: '编辑步骤 bounded-observation' });
  const bound = within(form).getByRole('spinbutton', { name: '最多迭代次数' });
  await userEvent.clear(bound);
  await userEvent.type(bound, '0');
  await userEvent.tab();
  expect(within(form).getByRole('alert')).toHaveTextContent('用例结构无效');
  expect(currentDraft(store)).toBe(before);
  expect(store.getSnapshot().canInsert).toBe(true);
  await userEvent.click(within(form).getByRole('button', { name: '恢复已保存' }));
  expect(within(form).getByRole('spinbutton', { name: '最多迭代次数' })).toHaveValue(2);
  const id = within(form).getByRole('textbox', { name: '步骤 ID' });
  await userEvent.clear(id);
  await userEvent.type(id, 'launch');
  await userEvent.tab();
  expect(within(form).getByRole('alert')).toHaveTextContent('重复步骤 ID');
  expect(currentDraft(store)).toBe(before);
  await userEvent.click(within(form).getByRole('button', { name: '放弃未保存编辑并收起' }));
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(
    window.localStorage.getItem(
      caseDraftStorageKey({ root: detail.root, projectId: detail.project_id, caseId: 'starter' }),
    ),
  ).toBeNull();
});

test('an open editor rebases unrelated inspector inserts but refuses a changed same-ID step', async () => {
  const original = copyDraftCase({ ...controlFlowCase, case_id: 'starter' });
  const { store } = await editorCase(original);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 back' }));
  const form = screen.getByRole('form', { name: '编辑步骤 back' });
  const key = within(form).getByRole('textbox', { name: '按键' });
  await userEvent.clear(key);
  await userEvent.type(key, 'HOME');
  act(() => {
    expect(store.insertStep(insertedTap)).toBe(true);
  });
  await userEvent.tab();
  expect(findStep(currentDraft(store).steps, 'back')).toMatchObject({ action: { key: 'HOME' } });
  expect(findStep(currentDraft(store).steps, 'inspector-tap')).toEqual(insertedTap);
  await userEvent.clear(key);
  await userEvent.type(key, 'UNSAVED');
  act(() => {
    const draft = currentDraft(store);
    const before = findStep(draft.steps, 'back');
    if (before?.type !== 'action') throw new Error('missing original key step');
    expect(
      store.editDraft(
        replaceStep(draft, before, { ...before, action: { kind: 'key', key: 'EXTERNAL' } }),
        draft,
      ),
    ).toBe(true);
  });
  await userEvent.tab();
  expect(within(form).getByRole('alert')).toHaveTextContent('此内容已被修改');
  expect(findStep(currentDraft(store).steps, 'back')).toMatchObject({
    action: { key: 'EXTERNAL' },
  });
  await userEvent.click(within(form).getByRole('button', { name: '恢复已保存' }));
  expect(within(form).getByRole('textbox', { name: '按键' })).toHaveValue('EXTERNAL');
  expect(within(form).queryByRole('alert')).not.toBeInTheDocument();
});

test('a failed inline save retains the confirmed source and disables edits until explicit reload', async () => {
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  setItem.mockImplementationOnce(() => {
    throw new Error('QuotaExceededError');
  });
  const { store } = await editorCase(sourceCase(), {
    getItem: (key) => window.localStorage.getItem(key),
    setItem,
  });
  const before = currentDraft(store);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const form = screen.getByRole('form', { name: '编辑步骤 launch' });
  const app = within(form).getByRole('textbox', { name: '应用 ID' });
  await userEvent.clear(app);
  await userEvent.type(app, 'com.example.unsaved');
  await userEvent.tab();
  expect(currentDraft(store)).toBe(before);
  expect(screen.getByText('草稿未确认保存')).toBeVisible();
  expect(screen.queryByText('草稿已保存')).not.toBeInTheDocument();
  expect(app).toBeDisabled();
  expect(screen.getByRole('button', { name: '编辑步骤 launch' })).toBeDisabled();
  expect(setItem).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole('button', { name: '重新读取用例草稿' }));
  await vi.waitFor(() =>
    expect(screen.getByRole('button', { name: '编辑步骤 launch' })).toBeEnabled(),
  );
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(currentDraft(store)).toEqual(sourceCase());
});

test('switching cases unmounts pending fields rather than leaking edits into a same-ID step', async () => {
  const { store, projects } = await editorCase();
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const field = screen.getByRole('textbox', { name: '应用 ID' });
  await userEvent.clear(field);
  await userEvent.type(field, 'com.example.old-case');
  await act(async () => {
    projects.selectCase('other');
    await store.load();
  });
  expect(screen.getByRole('heading', { name: 'Starter case' })).toBeVisible();
  expect(store.getSnapshot().scope?.caseId).toBe('other');
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(currentDraft(store).steps[0]).toEqual(sourceCase().steps[0]);
  expect(
    window.localStorage.getItem(
      caseDraftStorageKey({ root: '/tmp/demo', projectId: 'demo', caseId: 'starter' }),
    ),
  ).toBeNull();
});

test('loads flows only on expansion, keeps definitions read-only, and supports an explicit retry', async () => {
  const original = copyDraftCase({ ...controlFlowCase, case_id: 'starter' });
  const { store, getFlow } = await editorCase(original);
  const before = currentDraft(store);
  expect(getFlow).not.toHaveBeenCalled();
  getFlow.mockRejectedValueOnce(new Error('disconnected'));
  await userEvent.click(screen.getByRole('button', { name: '展开流程 search-product' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('流程读取失败: disconnected');
  expect(store.getSnapshot().canInsert).toBe(true);
  await userEvent.click(screen.getByRole('button', { name: '重新读取流程 fill-search' }));
  const preview = await screen.findByRole('region', { name: '公共流程 fill-search' });
  expect(within(preview).getByText('type-query')).toBeVisible();
  expect(within(preview).getByText('read-label')).toBeVisible();
  expect(within(preview).queryByRole('button', { name: /编辑|删除|拖动/ })).not.toBeInTheDocument();
  expect(getFlow).toHaveBeenCalledTimes(2);
  expect(currentDraft(store)).toBe(before);
  expect(store.getSnapshot().persistence).toBe('none');
  await userEvent.click(screen.getByRole('button', { name: '折叠流程 search-product' }));
  expect(screen.queryByRole('region', { name: '公共流程 fill-search' })).not.toBeInTheDocument();
});

test('a cyclic flow stops expansion before making a second read or inventing editable steps', async () => {
  const { store, getFlow } = await editorCase(
    copyDraftCase({ ...controlFlowCase, case_id: 'starter' }),
  );
  getFlow.mockResolvedValue({
    ...fillSearch,
    steps: [{ type: 'flow', id: 'again', flow_ref: 'fill-search' }],
  });
  const before = currentDraft(store);
  await userEvent.click(screen.getByRole('button', { name: '展开流程 search-product' }));
  await userEvent.click(await screen.findByRole('button', { name: '展开流程 again' }));
  expect(screen.getByRole('alert')).toHaveTextContent('流程循环引用');
  expect(getFlow).toHaveBeenCalledTimes(1);
  expect(currentDraft(store)).toBe(before);
});

test('late flow bytes after collapse do not reopen a preview or show an old error', async () => {
  const { getFlow } = await editorCase(copyDraftCase({ ...controlFlowCase, case_id: 'starter' }));
  let resolve!: (flow: unknown) => void;
  getFlow.mockReturnValueOnce(
    new Promise((settle) => {
      resolve = settle;
    }),
  );
  await userEvent.click(screen.getByRole('button', { name: '展开流程 search-product' }));
  await vi.waitFor(() => expect(getFlow).toHaveBeenCalledOnce());
  await userEvent.click(screen.getByRole('button', { name: '折叠流程 search-product' }));
  await act(async () => {
    resolve(fillSearch);
    await Promise.resolve();
  });
  expect(screen.queryByRole('region', { name: '公共流程 fill-search' })).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

test('keyboard dragging reorders a real step block and Escape leaves the confirmed draft unchanged', async () => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    const item = this.closest('li.case-draft-step');
    const index = item?.parentElement ? Array.from(item.parentElement.children).indexOf(item) : 0;
    return new DOMRect(0, Math.max(index, 0) * 100, 400, item ? 80 : 1000);
  });
  const { store } = await editorCase({
    ...sourceCase(),
    steps: [...sourceCase().steps, insertedTap],
  });
  const before = currentDraft(store);
  const handle = screen.getByRole('button', { name: '拖动步骤 launch' });
  act(() => handle.focus());
  await userEvent.keyboard('[Space]');
  expect(handle.closest('li')).toHaveAttribute('data-dragging', 'true');
  await userEvent.keyboard('{ArrowDown}{Escape}');
  expect(currentDraft(store)).toBe(before);
  expect(store.getSnapshot().persistence).toBe('none');
  act(() => handle.focus());
  await userEvent.keyboard('[Space]{ArrowDown}[Space]');
  expect(currentDraft(store).steps.map((step) => step.id)).toEqual(['inspector-tap', 'launch']);
  expect(store.getSnapshot().persistence).toBe('saved');
});

test('binding names retain every typed character and duplicate names cannot autosave or close', async () => {
  const { store } = await editorCase(copyDraftCase({ ...controlFlowCase, case_id: 'starter' }));
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 search-product' }));
  const form = screen.getByRole('form', { name: '编辑步骤 search-product' });
  await userEvent.click(within(form).getByRole('button', { name: '添加流程参数' }));
  const group = within(form).getByRole('group', { name: '流程参数' });
  const name = within(group).getAllByRole('textbox', { name: '绑定名称' })[1];
  if (!name) throw new Error('missing new argument name');
  await userEvent.clear(name);
  await userEvent.type(name, 'query2');
  expect(name).toHaveValue('query2');
  await userEvent.tab();
  expect(findStep(currentDraft(store).steps, 'search-product')).toMatchObject({
    arguments: { query: { param_ref: 'product' }, query2: '' },
  });
  const saved = currentDraft(store);
  await userEvent.clear(name);
  await userEvent.type(name, 'query');
  expect(name).toHaveValue('query');
  await userEvent.tab();
  expect(currentDraft(store)).toBe(saved);
  expect(within(group).getByRole('alert')).toHaveTextContent('绑定名称重复');
  expect(within(form).getByText('有未保存编辑')).toBeVisible();
  await userEvent.click(within(form).getByRole('button', { name: '保存并收起' }));
  expect(form).toBeInTheDocument();
  expect(currentDraft(store)).toBe(saved);
  await userEvent.click(within(form).getByRole('button', { name: '恢复已保存' }));
  const restored = within(form).getByRole('group', { name: '流程参数' });
  expect(
    within(restored)
      .getAllByRole('textbox', { name: '绑定名称' })
      .map((input) => (input as HTMLInputElement).value),
  ).toEqual(['query', 'query2']);
  expect(within(form).queryByRole('alert')).not.toBeInTheDocument();
  await userEvent.click(within(form).getByRole('button', { name: '保存并收起' }));
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
});

test.each(['bounded-observation', 'banner-branch'])(
  'renaming %s cannot unmount an open descendant editor',
  async (parentId) => {
    const { store } = await editorCase(copyDraftCase({ ...controlFlowCase, case_id: 'starter' }));
    const childId = parentId === 'bounded-observation' ? 'long-product' : 'tap-banner';
    await userEvent.click(screen.getByRole('button', { name: `编辑步骤 ${childId}` }));
    const childForm = screen.getByRole('form', { name: `编辑步骤 ${childId}` });
    const timeout = within(childForm).getByRole('spinbutton', { name: '步骤超时（ms）' });
    await userEvent.clear(timeout);
    await userEvent.type(timeout, '0');
    await userEvent.tab();
    expect(within(childForm).getByRole('alert')).toHaveTextContent('用例结构无效');
    await userEvent.click(screen.getByRole('button', { name: `编辑步骤 ${parentId}` }));
    const parentForm = screen.getByRole('form', { name: `编辑步骤 ${parentId}` });
    const id = within(parentForm).getByRole('textbox', { name: '步骤 ID' });
    fireEvent.change(id, { target: { value: 'renamed-parent' } });
    fireEvent.blur(id);
    expect(within(parentForm).getByRole('alert')).toHaveTextContent('子步骤编辑器仍打开');
    expect(childForm).toBeInTheDocument();
    expect(timeout).toHaveValue(0);
    expect(findStep(currentDraft(store).steps, parentId)).toBeDefined();
    expect(findStep(currentDraft(store).steps, 'renamed-parent')).toBeUndefined();
    await userEvent.click(within(childForm).getByRole('button', { name: '放弃未保存编辑并收起' }));
    await userEvent.click(within(parentForm).getByRole('button', { name: '保存并收起' }));
    expect(findStep(currentDraft(store).steps, 'renamed-parent')).toBeDefined();
    expect(findStep(currentDraft(store).steps, childId)?.timeout_ms).toBeUndefined();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
  },
);
