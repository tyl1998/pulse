import { beforeEach, expect, test, vi } from 'vitest';
import type { ProjectExportResult, PulseBridge, RuntimeConnection } from '../../../shared/ipc';
import type { PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import {
  createProjectStore,
  PROJECT_STORAGE_KEY,
  readProjectScope,
  type ProjectRuntimeClient,
} from './projectStore';

type ProjectDetail = PulseRuntimeApiSchemas.ProjectDetail;

const connection: RuntimeConnection = { port: 4311, token: 'token', pid: 9, version: '1.0.0' };

function detail(root = '/tmp/demo', projectId = 'demo'): ProjectDetail {
  return {
    allowed_apps: ['com.pulse.target'],
    case_ids: ['starter', 'other'],
    contract: 'pulse-runtime-api-draft',
    data_ids: [],
    default_strategy: 'rule_only',
    entry_cases: ['starter'],
    flow_ids: ['login'],
    manifest: {},
    name: 'Demo project',
    platforms: ['android'],
    profile_ids: ['android-local'],
    project_id: projectId,
    required_capabilities: [],
    revision: '7',
    root,
  };
}

function bridge(overrides: Partial<PulseBridge['projects']> = {}, connected = true): PulseBridge {
  return {
    app: { getInfo: async () => ({ version: '1', platform: 'darwin' }) },
    runtime: {
      getConnection: async () => (connected ? connection : null),
      getStatus: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
      ensureRunning: async () => ({ kind: 'not-running', launchable: false, detail: 'test' }),
    },
    projects: {
      pickDirectory: vi.fn(async () => '/tmp/picked'),
      createProject: vi.fn(async () => ({
        root: '/tmp/created',
        projectId: 'created',
        fileCount: 3,
        totalBytes: 100,
      })),
      importProject: vi.fn(async () => ({
        root: '/tmp/imported',
        projectId: 'imported',
        fileCount: 4,
        totalBytes: 200,
      })),
      exportProject: vi.fn(async (): Promise<ProjectExportResult> => ({
        targetPath: '/tmp/export.pulse-project.json',
        fileCount: 3,
        totalBytes: 150,
      })),
      ...overrides,
    },
  };
}

function client(open = detail()): ProjectRuntimeClient & {
  openProject: ReturnType<typeof vi.fn>;
  getProject: ReturnType<typeof vi.fn>;
  listCases: ReturnType<typeof vi.fn>;
} {
  return {
    openProject: vi.fn(async () => open),
    getProject: vi.fn(async () => open),
    listCases: vi.fn(async () => ({
      starter: { name: 'Starter', goal: 'Launch app' },
      other: { name: 'Other case' },
    })),
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

test('hydrates a persisted project from Runtime and selects the first entry case', async () => {
  window.localStorage.setItem(
    PROJECT_STORAGE_KEY,
    JSON.stringify({ version: 1, root: '/tmp/demo', projectId: 'demo' }),
  );
  const runtime = client();
  const store = createProjectStore({
    bridge: () => bridge(),
    clientFor: () => runtime,
    storage: window.localStorage,
  });
  await store.hydrate();

  const state = store.getSnapshot();
  expect(state.status).toBe('ready');
  expect(state.project?.name).toBe('Demo project');
  expect(state.selectedCaseId).toBe('starter');
  expect(state.cases['other']?.name).toBe('Other case');
  expect(readProjectScope(window.localStorage)).toEqual({ root: '/tmp/demo', projectId: 'demo' });
});

test('opens a picked directory, creates and imports projects, then exports', async () => {
  const bridgeHandle = bridge();
  const runtime = client();
  const store = createProjectStore({
    bridge: () => bridgeHandle,
    clientFor: () => runtime,
    storage: window.localStorage,
  });

  store.openFromDialog();
  await vi.waitFor(() => expect(store.getSnapshot().status).toBe('ready'));
  expect(bridgeHandle.projects.pickDirectory).toHaveBeenCalledWith('选择 Pulse 测试工程目录');
  expect(runtime.openProject).toHaveBeenCalledWith({ path: '/tmp/picked' });

  await store.createProject({ name: 'Created', projectId: 'created' });
  expect(bridgeHandle.projects.createProject).toHaveBeenCalledWith({
    name: 'Created',
    projectId: 'created',
    parentDir: '/tmp/picked',
  });
  expect(store.getSnapshot().notice).toMatch(/已创建工程/);

  await store.importProject();
  expect(bridgeHandle.projects.importProject).toHaveBeenCalled();
  expect(store.getSnapshot().notice).toMatch(/已导入工程/);

  await store.exportProject();
  expect(bridgeHandle.projects.exportProject).toHaveBeenCalledWith('/tmp/demo');
  expect(store.getSnapshot().notice).toMatch(/已导出 3 个文件/);
});

test('reports a useful error when Runtime is not connected', async () => {
  const store = createProjectStore({
    bridge: () => bridge({}, false),
    clientFor: () => client(),
    storage: window.localStorage,
  });
  window.localStorage.setItem(
    PROJECT_STORAGE_KEY,
    JSON.stringify({ version: 1, root: '/tmp/demo', projectId: 'demo' }),
  );
  await store.hydrate();
  expect(store.getSnapshot().status).toBe('error');
  expect(store.getSnapshot().error).toMatch(/Runtime 未运行/);
});

test('reports an error when the page has no preload bridge', async () => {
  const store = createProjectStore({
    bridge: () => undefined,
    clientFor: () => client(),
    storage: window.localStorage,
  });
  window.localStorage.setItem(
    PROJECT_STORAGE_KEY,
    JSON.stringify({ version: 1, root: '/tmp/demo', projectId: 'demo' }),
  );
  await store.hydrate();
  expect(store.getSnapshot().error).toMatch(/未连接 Electron 主进程/);
});
