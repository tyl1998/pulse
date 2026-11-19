import { useMemo, useSyncExternalStore } from 'react';
import type {
  ProjectCreateRequest,
  ProjectExportResult,
  PulseBridge,
  RuntimeConnection,
} from '../../../shared/ipc';
import type { PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import { createRuntimeClient } from '../../shared/api/runtimeClient';

type ProjectDetail = PulseRuntimeApiSchemas.ProjectDetail;

export const PROJECT_STORAGE_KEY = 'pulse:project:v1';

export interface ProjectScope {
  root: string;
  projectId: string | null;
}

export interface ProjectCaseSummary {
  caseId: string;
  name: string;
  goal: string | null;
}

export type ProjectStatus = 'idle' | 'loading' | 'ready' | 'error';
export type ProjectBusyAction = 'open' | 'create' | 'import' | 'export' | 'refresh' | null;

export interface ProjectState {
  version: 1;
  status: ProjectStatus;
  scope: ProjectScope | null;
  project: ProjectDetail | null;
  cases: Record<string, ProjectCaseSummary>;
  selectedCaseId: string | null;
  error: string | null;
  notice: string | null;
  busy: ProjectBusyAction;
  dialogOpen: boolean;
}

export interface ProjectRuntimeClient {
  openProject(body: { path: string }): Promise<ProjectDetail>;
  getProject(projectId: string): Promise<ProjectDetail>;
  listCases(projectId: string): Promise<Record<string, unknown>>;
}

export interface ProjectStoreDeps {
  bridge: () => PulseBridge | undefined;
  clientFor: (connection: RuntimeConnection) => ProjectRuntimeClient;
  storage: Storage | null;
}

export interface ProjectStore {
  getSnapshot: () => ProjectState;
  subscribe: (listener: () => void) => () => void;
  hydrate: () => Promise<void>;
  refresh: () => Promise<void>;
  openFromDialog: () => Promise<void>;
  createProject: (request: Omit<ProjectCreateRequest, 'parentDir'>) => Promise<void>;
  importProject: () => Promise<void>;
  exportProject: () => Promise<void>;
  selectCase: (caseId: string) => void;
  openDialog: () => void;
  closeDialog: () => void;
}

const initialState = (): ProjectState => ({
  version: 1,
  status: 'idle',
  scope: null,
  project: null,
  cases: {},
  selectedCaseId: null,
  error: null,
  notice: null,
  busy: null,
  dialogOpen: false,
});

function resolveStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function defaultProjectStoreDeps(): ProjectStoreDeps {
  return {
    bridge: () => (typeof window === 'undefined' ? undefined : window.pulse),
    clientFor: (connection) => createRuntimeClient(connection),
    storage: resolveStorage(),
  };
}

export function readProjectScope(storage: Storage | null): ProjectScope | null {
  if (!storage) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(storage.getItem(PROJECT_STORAGE_KEY) ?? 'null') as unknown;
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const record = parsed as Record<string, unknown>;
  if (record['version'] !== 1) return null;
  const root = record['root'];
  const projectId = record['projectId'];
  if (typeof root !== 'string' || !root) return null;
  if (projectId !== null && projectId !== undefined && typeof projectId !== 'string') return null;
  return { root, projectId: typeof projectId === 'string' && projectId ? projectId : null };
}

function writeProjectScope(storage: Storage | null, scope: ProjectScope): void {
  try {
    storage?.setItem(PROJECT_STORAGE_KEY, JSON.stringify({ version: 1, ...scope }));
  } catch {
    // Losing the recent-project pointer must not break the currently open workspace.
  }
}

function formatError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.replace(/^Error invoking remote method '[^']+': (?:Error: )?/, '');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function summarizeCases(raw: Record<string, unknown>): Record<string, ProjectCaseSummary> {
  const cases: Record<string, ProjectCaseSummary> = {};
  for (const [caseId, value] of Object.entries(raw)) {
    const record = isRecord(value) ? value : {};
    const name =
      typeof record['name'] === 'string' && record['name'].trim() ? record['name'] : caseId;
    const goal = typeof record['goal'] === 'string' ? record['goal'] : null;
    cases[caseId] = { caseId, name, goal };
  }
  return cases;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 102.4) / 10} KiB`;
  return `${Math.round(bytes / 1024 / 102.4) / 10} MiB`;
}

export function createProjectStore(
  deps: ProjectStoreDeps = defaultProjectStoreDeps(),
): ProjectStore {
  let state = initialState();
  let hydrated = false;
  const listeners = new Set<() => void>();

  const update = (partial: Partial<ProjectState>) => {
    state = { ...state, ...partial };
    for (const listener of listeners) listener();
  };

  const requireBridge = (): PulseBridge => {
    const bridge = deps.bridge();
    if (!bridge) {
      throw new Error('当前页面未连接 Electron 主进程，无法访问本地工程');
    }
    return bridge;
  };

  const requireClient = async (): Promise<ProjectRuntimeClient> => {
    const bridge = requireBridge();
    const connection = await bridge.runtime.getConnection();
    if (!connection) {
      throw new Error('本机 Runtime 未运行，请先启动 Runtime');
    }
    return deps.clientFor(connection);
  };

  const loadScope = async (scope: ProjectScope, client: ProjectRuntimeClient): Promise<void> => {
    let detail: ProjectDetail;
    try {
      detail = await client.openProject({ path: scope.root });
    } catch (openError) {
      if (!scope.projectId) throw openError;
      detail = await client.getProject(scope.projectId);
    }
    let rawCases: Record<string, unknown>;
    try {
      rawCases = await client.listCases(detail.project_id);
    } catch {
      rawCases = {};
    }
    const cases = summarizeCases(rawCases);
    const selectedCaseId =
      state.selectedCaseId &&
      (state.selectedCaseId in cases || detail.case_ids.includes(state.selectedCaseId))
        ? state.selectedCaseId
        : (detail.entry_cases[0] ?? detail.case_ids[0] ?? null);
    const nextScope: ProjectScope = { root: detail.root, projectId: detail.project_id };
    writeProjectScope(deps.storage, nextScope);
    update({
      status: 'ready',
      scope: nextScope,
      project: detail,
      cases,
      selectedCaseId,
      error: null,
    });
  };

  const perform = async (busy: Exclude<ProjectBusyAction, null>, task: () => Promise<void>) => {
    if (state.busy !== null) return;
    update({ busy, error: null, notice: null });
    try {
      await task();
      update({ busy: null });
    } catch (error) {
      update({
        busy: null,
        status: state.project ? 'ready' : 'error',
        error: formatError(error),
      });
    }
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    hydrate: async () => {
      if (hydrated) return;
      hydrated = true;
      const scope = readProjectScope(deps.storage);
      if (!scope) return;
      update({ scope, status: 'loading' });
      try {
        const client = await requireClient();
        await loadScope(scope, client);
      } catch (error) {
        update({ status: 'error', error: formatError(error) });
      }
    },
    refresh: async () => {
      await perform('refresh', async () => {
        const scope =
          state.scope ??
          (state.project
            ? { root: state.project.root, projectId: state.project.project_id }
            : null);
        if (!scope) throw new Error('尚未打开工程');
        update({ status: 'loading' });
        await loadScope(scope, await requireClient());
      });
    },
    openFromDialog: async () => {
      await perform('open', async () => {
        const bridge = requireBridge();
        const root = await bridge.projects.pickDirectory('选择 Pulse 测试工程目录');
        if (!root) return;
        update({ status: 'loading' });
        await loadScope({ root, projectId: null }, await requireClient());
      });
    },
    createProject: async (request) => {
      await perform('create', async () => {
        const bridge = requireBridge();
        const parentDir = await bridge.projects.pickDirectory('选择新工程所在文件夹');
        if (!parentDir) return;
        const workspace = await bridge.projects.createProject({ ...request, parentDir });
        update({ status: 'loading' });
        await loadScope(
          { root: workspace.root, projectId: workspace.projectId },
          await requireClient(),
        );
        update({
          notice: `已创建工程 ${workspace.projectId}（${workspace.fileCount} 个文件）`,
        });
      });
    },
    importProject: async () => {
      await perform('import', async () => {
        const bridge = requireBridge();
        const workspace = await bridge.projects.importProject();
        if (!workspace) return;
        update({ status: 'loading' });
        await loadScope(
          { root: workspace.root, projectId: workspace.projectId },
          await requireClient(),
        );
        update({
          notice: `已导入工程 ${workspace.projectId}（${workspace.fileCount} 个文件）`,
        });
      });
    },
    exportProject: async () => {
      await perform('export', async () => {
        const root = state.project?.root;
        if (!root) throw new Error('尚未打开工程，无法导出');
        const bridge = requireBridge();
        const result: ProjectExportResult | null = await bridge.projects.exportProject(root);
        if (!result) return;
        update({
          notice: `已导出 ${result.fileCount} 个文件（${formatBytes(result.totalBytes)}）到 ${result.targetPath}`,
        });
      });
    },
    selectCase: (caseId) => {
      update({ selectedCaseId: caseId });
    },
    openDialog: () => update({ dialogOpen: true }),
    closeDialog: () => update({ dialogOpen: false, error: null }),
  };
}

let defaultStore: ProjectStore | null = null;

export function getProjectStore(): ProjectStore {
  defaultStore ??= createProjectStore();
  return defaultStore;
}

/** Tests only: drop the singleton without deleting persisted data. */
export function resetProjectStores(): void {
  defaultStore = null;
}

export function useProjectStore() {
  const store = useMemo(() => getProjectStore(), []);
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return { state, store };
}
