import type { ActionStep, Case, CheckStep } from '@pulse/schema';
import { act, cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
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
  type CaseDraftStore,
  useCaseDraft,
} from './caseDraftStore';

const stores: CaseDraftStore[] = [];
const detail = {
  allowed_apps: ['com.pulse.target'],
  case_ids: ['starter', 'other'],
  data_ids: [],
  default_strategy: 'rule_only',
  entry_cases: ['starter'],
  flow_ids: [],
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

afterEach(() => {
  cleanup();
  stores.splice(0).forEach((store) => store.dispose());
  resetCaseDraftStores();
  resetProjectStores();
  window.localStorage.clear();
  delete window.pulse;
  vi.unstubAllGlobals();
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
  expect(within(panel).getByRole('status')).toHaveTextContent('工程原始用例未修改，步骤不会执行');

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

test('renders existing nested steps without adding arbitrary editing or execution controls', async () => {
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
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
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
