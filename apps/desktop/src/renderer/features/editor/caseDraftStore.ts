import type { Case, Step } from '@pulse/schema';
import { useSyncExternalStore } from 'react';
import { createRuntimeClient } from '../../shared/api/runtimeClient';
import { withTimeout } from '../../shared/stream/timeout';
import { getProjectStore, type ProjectStore } from '../projects/projectStore';
import { copyDraftCase, isRecord, sameJson } from './caseDraftValidation';

export const CASE_DRAFT_STORAGE_PREFIX = 'pulse:case-draft:v1:';
export const CASE_DRAFT_LOAD_TIMEOUT_MS = 5000;

export interface CaseDraftScope {
  root: string;
  projectId: string;
  caseId: string;
}

export interface CaseDraftState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  scope: CaseDraftScope | null;
  original: Case | null;
  draft: Case | null;
  persistence: 'none' | 'saved' | 'error';
  savedAt: number | null;
  canInsert: boolean;
  reason: string | null;
  error: string | null;
}

export interface CaseDraftRuntimeClient {
  getProject(projectId: string): Promise<unknown>;
  getCase(projectId: string, caseId: string): Promise<unknown>;
  verifyConnection(): Promise<void>;
}

export interface CaseDraftStoreDeps {
  projects: Pick<ProjectStore, 'getSnapshot' | 'subscribe'>;
  getClient: (signal: AbortSignal) => Promise<CaseDraftRuntimeClient>;
  storage: Pick<Storage, 'getItem' | 'setItem'> | null;
  now: () => number;
}

export interface CaseDraftStore {
  getSnapshot: () => CaseDraftState;
  subscribe: (listener: () => void) => () => void;
  load: () => Promise<void>;
  insertStep: (step: Step) => boolean;
  dispose: () => void;
}

export function caseDraftStorageKey(scope: CaseDraftScope): string {
  return `${CASE_DRAFT_STORAGE_PREFIX}${JSON.stringify([scope.root, scope.projectId, scope.caseId])}`;
}

function initialState(reason = '尚未打开工程'): CaseDraftState {
  return {
    status: 'idle',
    scope: null,
    original: null,
    draft: null,
    persistence: 'none',
    savedAt: null,
    canInsert: false,
    reason,
    error: null,
  };
}

export function defaultCaseDraftStoreDeps(): CaseDraftStoreDeps {
  let storage: Storage | null = null;
  try {
    storage = typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    // A restricted renderer may still display the original, but cannot claim to save drafts.
  }
  return {
    projects: getProjectStore(),
    getClient: async (signal) => {
      const bridge = typeof window === 'undefined' ? undefined : window.pulse;
      if (!bridge) throw new Error('当前页面未连接 Electron 主进程，无法读取原始用例');
      const connection = await bridge.runtime.getConnection();
      signal.throwIfAborted();
      if (!connection) throw new Error('本机 Runtime 未运行，无法读取原始用例');
      const client = createRuntimeClient(connection, (input, init) =>
        fetch(input, { ...init, signal }),
      );
      return {
        getProject: (projectId) => client.getProject(projectId),
        getCase: (projectId, caseId) => client.getCase(projectId, caseId),
        verifyConnection: async () => {
          const current = await bridge.runtime.getConnection();
          signal.throwIfAborted();
          if (
            !current ||
            current.pid !== connection.pid ||
            current.port !== connection.port ||
            current.token !== connection.token ||
            current.version !== connection.version
          ) {
            throw new Error('Runtime 已更换或连接未确认，未使用旧工程的用例内容');
          }
        },
      };
    },
    storage,
    now: () => Date.now(),
  };
}

export function createCaseDraftStore(
  deps: CaseDraftStoreDeps = defaultCaseDraftStoreDeps(),
): CaseDraftStore {
  let state = Object.freeze(initialState());
  let generation = 0;
  let disposed = false;
  let selectionSignature = '';
  let observedProject = deps.projects.getSnapshot().project;
  let persistedValue: string | null = null;
  let pending: { key: string; promise: Promise<void> } | null = null;
  let loadController: AbortController | null = null;
  const listeners = new Set<() => void>();
  const update = (partial: Partial<CaseDraftState>) => {
    state = Object.freeze({ ...state, ...partial });
    for (const listener of listeners) listener();
  };
  const selection = (): { scope: CaseDraftScope | null; reason: string | null } => {
    const projectState = deps.projects.getSnapshot();
    if (
      projectState.status === 'loading' ||
      (projectState.busy !== null && projectState.busy !== 'export')
    ) {
      return { scope: null, reason: '正在打开或刷新工程，暂不能插入步骤' };
    }
    const project = projectState.project;
    if (!project) return { scope: null, reason: '尚未打开工程' };
    if (
      projectState.status !== 'ready' ||
      !project.root.trim() ||
      !project.project_id ||
      projectState.scope?.root !== project.root ||
      projectState.scope.projectId !== project.project_id
    ) {
      return { scope: null, reason: '当前工程范围无效，不能读取用例草稿' };
    }
    const caseId = projectState.selectedCaseId;
    if (!caseId) return { scope: null, reason: '请先在用例树中选择用例' };
    if (!project.case_ids.includes(caseId)) {
      return { scope: null, reason: '选中用例不属于当前工程' };
    }
    return { scope: { root: project.root, projectId: project.project_id, caseId }, reason: null };
  };

  const load = (): Promise<void> => {
    if (disposed) return Promise.resolve();
    const selected = selection();
    if (!selected.scope) {
      generation++;
      loadController?.abort();
      loadController = null;
      pending = null;
      persistedValue = null;
      update(initialState(selected.reason ?? undefined));
      return Promise.resolve();
    }
    const scope = Object.freeze(selected.scope);
    const key = caseDraftStorageKey(scope);
    if (pending?.key === key) return pending.promise;
    const request = ++generation;
    loadController?.abort();
    const controller = new AbortController();
    loadController = controller;
    const current = () => !disposed && request === generation && !controller.signal.aborted;
    persistedValue = null;
    update({
      ...initialState('正在读取选中用例，读取完成后才能插入步骤'),
      scope,
      status: 'loading',
    });
    const promise = (async () => {
      let original: Case | null = null;
      try {
        const readSource = async () => {
          const client = await deps.getClient(controller.signal);
          if (!current()) return null;
          const checkProject = (project: unknown) => {
            if (
              !isRecord(project) ||
              project['root'] !== scope.root ||
              project['project_id'] !== scope.projectId ||
              !Array.isArray(project['case_ids']) ||
              !project['case_ids'].includes(scope.caseId)
            ) {
              throw new Error(
                'Runtime 工程来源与当前 root / project / case 范围不一致，不能读取草稿',
              );
            }
          };
          const project = await client.getProject(scope.projectId);
          if (!current()) return null;
          checkProject(project);
          await client.verifyConnection();
          if (!current()) return null;
          const rawCase = await client.getCase(scope.projectId, scope.caseId);
          if (!current()) return null;
          const after = await client.getProject(scope.projectId);
          if (!current()) return null;
          checkProject(after);
          await client.verifyConnection();
          return current() ? copyDraftCase(rawCase) : null;
        };
        const validated = await withTimeout(
          readSource(),
          CASE_DRAFT_LOAD_TIMEOUT_MS,
          '用例读取超过 5 秒，来源未确认；可重新读取草稿。',
          controller.signal,
        );
        if (!validated || !current()) return;
        if (validated.case_id !== scope.caseId)
          throw new Error('Runtime 返回的用例 ID 与选中用例不一致');
        original = validated;
        const saved = deps.storage?.getItem(key) ?? null;
        let draft = original;
        let savedAt: number | null = null;
        if (saved !== null) {
          const record: unknown = JSON.parse(saved);
          if (
            !isRecord(record) ||
            record['version'] !== 1 ||
            !sameJson(record['scope'], scope) ||
            typeof record['savedAt'] !== 'number' ||
            !Number.isSafeInteger(record['savedAt']) ||
            record['savedAt'] < 0
          ) {
            throw new Error('本地草稿记录无效，已保留记录并停止插入');
          }
          const base = copyDraftCase(record['original']);
          draft = copyDraftCase(record['draft']);
          if (!sameJson(base, original)) {
            throw new Error('原始用例已变化，与本地草稿来源冲突；已保留草稿并停止插入');
          }
          if (
            !sameJson({ ...draft, steps: original.steps }, original) ||
            draft.steps.length < original.steps.length ||
            !original.steps.every((item, index) => sameJson(item, draft.steps[index]))
          ) {
            throw new Error('本地草稿修改了原始字段或步骤，已保留记录并停止插入');
          }
          savedAt = record['savedAt'];
        }
        if (disposed || request !== generation) return;
        persistedValue = saved;
        const reason = deps.storage ? null : '本地草稿存储不可用，不能插入；工程原始用例未修改';
        update({
          status: 'ready',
          original,
          draft,
          savedAt,
          persistence: reason ? 'error' : saved === null ? 'none' : 'saved',
          canInsert: !reason,
          reason,
          error: reason,
        });
      } catch (error) {
        if (disposed || request !== generation) return;
        const message = error instanceof Error ? error.message : String(error);
        update({
          status: 'error',
          original,
          draft: null,
          canInsert: false,
          reason: message,
          error: message,
        });
      } finally {
        controller.abort();
        if (loadController === controller) loadController = null;
      }
    })();
    pending = { key, promise };
    void promise.then(() => {
      if (pending?.promise === promise) pending = null;
    });
    return promise;
  };

  const syncSelection = () => {
    if (disposed) return;
    const selected = selection();
    const signature = selected.scope ? caseDraftStorageKey(selected.scope) : selected.reason;
    const project = deps.projects.getSnapshot().project;
    if (signature === selectionSignature && project === observedProject) return;
    selectionSignature = signature ?? '';
    observedProject = project;
    generation++;
    pending = null;
    void load();
  };
  const unsubscribeProject = deps.projects.subscribe(syncSelection);
  syncSelection();

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    load,
    insertStep: (step) => {
      if (disposed) return false;
      syncSelection();
      const scope = state.scope;
      const original = state.original;
      const draft = state.draft;
      if (
        !state.canInsert ||
        state.status !== 'ready' ||
        !scope ||
        !original ||
        !draft ||
        !deps.storage
      ) {
        update({ error: state.reason ?? '选中用例尚未加载，不能插入步骤' });
        return false;
      }
      let next: Case;
      try {
        next = copyDraftCase({ ...draft, steps: [...draft.steps, step] });
      } catch (error) {
        update({
          error: `步骤插入失败: ${error instanceof Error ? error.message : String(error)}`,
        });
        return false;
      }
      const key = caseDraftStorageKey(scope);
      const request = generation;
      try {
        if (deps.storage.getItem(key) !== persistedValue) {
          throw new Error('本地草稿已被其他页面修改，请重新读取后再插入');
        }
        const savedAt = deps.now();
        if (!Number.isSafeInteger(savedAt) || savedAt < 0) throw new Error('草稿保存时间无效');
        const serialized = JSON.stringify({ version: 1, scope, original, draft: next, savedAt });
        deps.storage.setItem(key, serialized);
        if (deps.storage.getItem(key) !== serialized) throw new Error('无法确认本地草稿已完整保存');
        if (disposed || request !== generation) return false;
        persistedValue = serialized;
        update({ draft: next, persistence: 'saved', savedAt, error: null, reason: null });
        return true;
      } catch (error) {
        if (disposed || request !== generation) return false;
        const message = `草稿保存失败或未确认: ${error instanceof Error ? error.message : String(error)}`;
        update({
          persistence: 'error',
          canInsert: false,
          reason: '请重新读取用例草稿后再插入',
          error: message,
        });
        return false;
      }
    },
    dispose: () => {
      disposed = true;
      generation++;
      loadController?.abort();
      loadController = null;
      pending = null;
      unsubscribeProject();
      update(initialState('用例草稿已关闭'));
      listeners.clear();
    },
  };
}

let defaultStore: CaseDraftStore | null = null;

export function getCaseDraftStore(): CaseDraftStore {
  defaultStore ??= createCaseDraftStore();
  return defaultStore;
}

/** Tests only: release subscriptions without deleting persisted drafts. */
export function resetCaseDraftStores(): void {
  defaultStore?.dispose();
  defaultStore = null;
}

export function useCaseDraft(store: CaseDraftStore = getCaseDraftStore()) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return { state, store };
}
