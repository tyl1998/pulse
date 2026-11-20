import type { Case, Flow, Step } from '@pulse/schema';
import { enablePatches, produceWithPatches, type Patch } from 'immer';
import { useSyncExternalStore } from 'react';
import { createRuntimeClient } from '../../shared/api/runtimeClient';
import { withTimeout } from '../../shared/stream/timeout';
import { getProjectStore, type ProjectStore } from '../projects/projectStore';
import { copyDraftCase, copyDraftFlow, isRecord, sameJson } from './caseDraftValidation';

export const CASE_DRAFT_STORAGE_PREFIX = 'pulse:case-draft:v1:';
export const CASE_DRAFT_LOAD_TIMEOUT_MS = 5000;
export const CASE_DRAFT_HISTORY_LIMIT = 100;

enablePatches();

const DRAFT_FIELDS = [
  'steps',
  'locators',
  'preconditions',
  'checks',
  'parameters',
  'inputs',
  'original_text',
] as const;

function applyDraftPatches(base: Case, patches: Patch[]): Case {
  const next = { ...base };
  for (const patch of patches) {
    const field = patch.path[0];
    if (
      patch.path.length !== 1 ||
      typeof field !== 'string' ||
      !DRAFT_FIELDS.some((key) => key === field)
    )
      throw new Error('草稿历史包含不支持的字段补丁');
    if (patch.op === 'remove') {
      if (field !== 'original_text') throw new Error('草稿历史不能删除必填字段');
      delete next.original_text;
    } else {
      // Root-field history values must retain legal __proto__ keys instead of invoking a setter.
      Object.defineProperty(next, field, {
        value: patch.value,
        enumerable: true,
        configurable: true,
        writable: true,
      });
    }
  }
  return next;
}

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
  canUndo: boolean;
  canRedo: boolean;
  historyRevision: number;
}

export interface CaseDraftRuntimeClient {
  getProject(projectId: string): Promise<unknown>;
  getCase(projectId: string, caseId: string): Promise<unknown>;
  getFlow?(projectId: string, flowId: string): Promise<unknown>;
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
  editDraft: (next: Case, expected: Case, historyGroup?: object) => boolean;
  editCaseText: (next: Case, expected: Case, historyGroup?: object) => boolean;
  editCaseBindings: (next: Case, expected: Case, historyGroup?: object) => boolean;
  undo: () => boolean;
  redo: () => boolean;
  readFlow: (flowId: string, signal: AbortSignal) => Promise<Flow>;
  dispose: () => void;
}

export function caseDraftStorageKey(scope: CaseDraftScope): string {
  return `${CASE_DRAFT_STORAGE_PREFIX}${JSON.stringify([scope.root, scope.projectId, scope.caseId])}`;
}

function preservesOriginalFields(
  draft: Case,
  original: Case,
  allowAuthoringText = false,
  allowBindings = false,
): boolean {
  const protectedDraft = {
    ...draft,
    steps: original.steps,
    locators: original.locators,
    preconditions: original.preconditions,
    checks: original.checks,
  };
  const protectedOriginal = { ...original };
  if (allowBindings) {
    protectedDraft.parameters = original.parameters;
    protectedDraft.inputs = original.inputs;
  }
  if (allowAuthoringText) {
    // The source Case remains immutable; v3 may revise only its draft authoring text.
    delete protectedDraft.original_text;
    delete protectedOriginal.original_text;
  }
  return sameJson(protectedDraft, protectedOriginal);
}

function assertRuntimeProject(project: unknown, scope: CaseDraftScope, flowId?: string): void {
  if (
    !isRecord(project) ||
    project['root'] !== scope.root ||
    project['project_id'] !== scope.projectId ||
    !Array.isArray(project['case_ids']) ||
    !project['case_ids'].includes(scope.caseId)
  ) {
    throw new Error('Runtime 工程来源与当前 root / project / case 范围不一致，不能读取草稿或流程');
  }
  if (
    flowId !== undefined &&
    (!Array.isArray(project['flow_ids']) || !project['flow_ids'].includes(flowId))
  ) {
    throw new Error('Runtime 流程不属于当前工程，不能读取流程');
  }
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
    canUndo: false,
    canRedo: false,
    historyRevision: 0,
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
        getFlow: (projectId, flowId) => client.getFlow(projectId, flowId),
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
  let persistedVersion: 1 | 2 | 3 | 4 = 1;
  let pending: { key: string; promise: Promise<void> } | null = null;
  let loadController: AbortController | null = null;
  type HistoryEntry = { patches: Patch[]; inverse: Patch[]; group: object | undefined };
  const past: HistoryEntry[] = [];
  const future: HistoryEntry[] = [];
  let activeHistoryGroup: object | undefined;
  const flowControllers = new Set<AbortController>();
  const abortFlowReads = () => {
    for (const controller of flowControllers) controller.abort();
  };
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
      abortFlowReads();
      loadController?.abort();
      loadController = null;
      pending = null;
      persistedValue = null;
      past.length = future.length = 0;
      activeHistoryGroup = undefined;
      update({
        ...initialState(selected.reason ?? undefined),
        historyRevision: state.historyRevision + 1,
      });
      return Promise.resolve();
    }
    const scope = Object.freeze(selected.scope);
    const key = caseDraftStorageKey(scope);
    if (pending?.key === key) return pending.promise;
    const request = ++generation;
    abortFlowReads();
    loadController?.abort();
    const controller = new AbortController();
    loadController = controller;
    const current = () => !disposed && request === generation && !controller.signal.aborted;
    persistedValue = null;
    past.length = future.length = 0;
    activeHistoryGroup = undefined;
    update({
      ...initialState('正在读取选中用例，读取完成后才能插入步骤'),
      scope,
      status: 'loading',
      historyRevision: state.historyRevision + 1,
    });
    const promise = (async () => {
      let original: Case | null = null;
      try {
        const readSource = async () => {
          const client = await deps.getClient(controller.signal);
          if (!current()) return null;
          const project = await client.getProject(scope.projectId);
          if (!current()) return null;
          assertRuntimeProject(project, scope);
          await client.verifyConnection();
          if (!current()) return null;
          const rawCase = await client.getCase(scope.projectId, scope.caseId);
          if (!current()) return null;
          const after = await client.getProject(scope.projectId);
          if (!current()) return null;
          assertRuntimeProject(after, scope);
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
        let version: 1 | 2 | 3 | 4 = 1;
        if (saved !== null) {
          const record: unknown = JSON.parse(saved);
          if (
            !isRecord(record) ||
            (record['version'] !== 1 &&
              record['version'] !== 2 &&
              record['version'] !== 3 &&
              record['version'] !== 4) ||
            (record['version'] === 3 && record['format'] !== 'case-authoring-text') ||
            (record['version'] === 4 && record['format'] !== 'case-parameter-bindings') ||
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
          // Each stored version retains its original field protections; only v4 permits bindings.
          const allowed =
            record['version'] === 1
              ? sameJson({ ...draft, steps: original.steps }, original) &&
                draft.steps.length >= original.steps.length &&
                original.steps.every((item, index) => sameJson(item, draft.steps[index]))
              : preservesOriginalFields(
                  draft,
                  original,
                  record['version'] >= 3,
                  record['version'] === 4,
                );
          if (!allowed) {
            throw new Error('本地草稿修改了原始字段或步骤，已保留记录并停止插入');
          }
          savedAt = record['savedAt'];
          version = record['version'];
        }
        if (disposed || request !== generation) return;
        persistedValue = saved;
        persistedVersion = version;
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

  const saveDraft = (
    next: Case,
    expected: Case,
    version: 1 | 2 | 3 | 4,
    operation: 'edit' | 'undo' | 'redo' = 'edit',
    group?: object,
  ): boolean => {
    const scope = state.scope;
    const original = state.original;
    const storage = deps.storage;
    const request = generation;
    const current = () =>
      !disposed &&
      request === generation &&
      state.status === 'ready' &&
      state.canInsert &&
      state.scope === scope &&
      state.original === original &&
      state.draft === expected;
    if (!scope || !original || !storage || !current()) return false;
    try {
      if (storage.getItem(caseDraftStorageKey(scope)) !== persistedValue) {
        throw new Error('本地草稿已被其他页面修改，请重新读取后再编辑或插入');
      }
      if (!current()) return false;
      if (sameJson(next, expected)) return true;
      const previous = past.at(-1);
      const grouped =
        operation === 'edit' &&
        group !== undefined &&
        activeHistoryGroup === group &&
        previous?.group === group;
      const base = grouped ? applyDraftPatches(expected, previous.inverse) : expected;
      const [, patches, inverse] = produceWithPatches(base, (draft) => {
        for (const field of DRAFT_FIELDS) {
          if (
            !sameJson(base[field], next[field]) ||
            Object.hasOwn(base, field) !== Object.hasOwn(next, field)
          ) {
            if (Object.hasOwn(next, field)) Object.assign(draft, { [field]: next[field] });
            else delete draft.original_text;
          }
        }
      });
      const savedAt = deps.now();
      if (!Number.isSafeInteger(savedAt) || savedAt < 0) throw new Error('草稿保存时间无效');
      const serialized = JSON.stringify({
        version,
        ...(version === 3 ? { format: 'case-authoring-text' } : {}),
        ...(version === 4 ? { format: 'case-parameter-bindings' } : {}),
        scope,
        original,
        draft: next,
        savedAt,
      });
      if (!current()) return false;
      const key = caseDraftStorageKey(scope);
      storage.setItem(key, serialized);
      if (storage.getItem(key) !== serialized) throw new Error('无法确认本地草稿已完整保存');
      if (!current()) return false;
      persistedValue = serialized;
      persistedVersion = version;
      // History changes only after persistence is confirmed, just like the canonical draft.
      if (operation === 'undo') {
        const entry = past.pop();
        if (entry) future.push(entry);
      } else if (operation === 'redo') {
        const entry = future.pop();
        if (entry) past.push(entry);
      } else {
        if (grouped) past.pop();
        if (patches.length) past.push({ patches, inverse, group });
        if (past.length > CASE_DRAFT_HISTORY_LIMIT) past.shift();
        future.length = 0;
      }
      activeHistoryGroup = operation === 'edit' && patches.length > 0 ? group : undefined;
      update({
        draft: next,
        persistence: 'saved',
        savedAt,
        error: null,
        reason: null,
        canUndo: past.length > 0,
        canRedo: future.length > 0,
        historyRevision: state.historyRevision + (operation === 'edit' ? 0 : 1),
      });
      return true;
    } catch (error) {
      if (!current()) return false;
      const message = `草稿保存失败或未确认: ${error instanceof Error ? error.message : String(error)}`;
      update({
        persistence: 'error',
        canInsert: false,
        canUndo: false,
        canRedo: false,
        reason: '请重新读取用例草稿后再编辑或插入',
        error: message,
      });
      return false;
    }
  };

  const restoreHistory = (operation: 'undo' | 'redo'): boolean => {
    if (disposed) return false;
    syncSelection();
    const expected = state.draft;
    const entry = (operation === 'undo' ? past : future).at(-1);
    if (!state.canInsert || !expected || !entry) return false;
    const next = copyDraftCase(
      applyDraftPatches(expected, operation === 'undo' ? entry.inverse : entry.patches),
    );
    return saveDraft(next, expected, persistedVersion, operation);
  };

  const editDraft = (
    value: Case,
    expected: Case,
    historyGroup?: object,
    allowAuthoringText = false,
    allowBindings = false,
  ): boolean => {
    if (disposed) return false;
    syncSelection();
    const original = state.original;
    if (
      !state.canInsert ||
      state.status !== 'ready' ||
      !state.scope ||
      !original ||
      !state.draft ||
      !deps.storage
    ) {
      update({ error: state.reason ?? '选中用例尚未加载，不能编辑草稿' });
      return false;
    }
    if (expected !== state.draft) {
      update({ error: '草稿版本已变化，请使用当前选中用例的草稿重新编辑' });
      return false;
    }
    let next: Case;
    try {
      next = copyDraftCase(value);
      const base = allowAuthoringText ? original : expected;
      if (
        !preservesOriginalFields(value, base, allowAuthoringText, allowBindings) ||
        !preservesOriginalFields(next, base, allowAuthoringText, allowBindings)
      ) {
        throw new Error(
          allowBindings
            ? '仅允许编辑草稿结构、parameters / inputs 和已授权的创作输入，不能修改来源、身份或其他字段'
            : allowAuthoringText
              ? '仅允许编辑 steps / locators / preconditions / checks / original_text（草稿创作输入），不能修改来源、身份或其他字段'
              : '仅允许编辑 steps / locators / preconditions / checks，不能修改原始其他字段或 ID',
        );
      }
    } catch (error) {
      update({ error: `草稿编辑失败: ${error instanceof Error ? error.message : String(error)}` });
      return false;
    }
    const version =
      persistedVersion === 4 ||
      !sameJson(next.parameters, original.parameters) ||
      !sameJson(next.inputs, original.inputs)
        ? 4
        : persistedVersion === 3 ||
            !sameJson(next.original_text, original.original_text) ||
            Object.hasOwn(next, 'original_text') !== Object.hasOwn(original, 'original_text')
          ? 3
          : 2;
    return saveDraft(next, expected, version, 'edit', historyGroup);
  };

  const readFlow = async (flowId: string, signal: AbortSignal): Promise<Flow> => {
    signal.throwIfAborted();
    const scope = state.scope;
    const original = state.original;
    const project = deps.projects.getSnapshot().project;
    const request = generation;
    if (disposed || state.status !== 'ready' || !scope || !original || !state.draft) {
      throw new Error(state.reason ?? '选中用例尚未加载，不能读取流程');
    }
    const controller = new AbortController();
    const onAbort = () => controller.abort(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    if (signal.aborted) onAbort();
    flowControllers.add(controller);
    const assertCurrent = () => {
      controller.signal.throwIfAborted();
      const selected = selection();
      if (
        disposed ||
        request !== generation ||
        state.status !== 'ready' ||
        state.scope !== scope ||
        state.original !== original ||
        deps.projects.getSnapshot().project !== project ||
        project !== observedProject ||
        !sameJson(selected.scope, scope)
      ) {
        throw new Error('工程或选中用例已变化，流程读取结果已过期');
      }
      assertRuntimeProject(project, scope, flowId);
    };
    try {
      assertCurrent();
      const readSource = async () => {
        const client = await deps.getClient(controller.signal);
        assertCurrent();
        if (typeof client.getFlow !== 'function')
          throw new Error('当前 Runtime 客户端不支持流程读取，流程预览不可用');
        const before = await client.getProject(scope.projectId);
        assertCurrent();
        assertRuntimeProject(before, scope, flowId);
        await client.verifyConnection();
        assertCurrent();
        const rawFlow = await client.getFlow(scope.projectId, flowId);
        assertCurrent();
        const after = await client.getProject(scope.projectId);
        assertCurrent();
        assertRuntimeProject(after, scope, flowId);
        await client.verifyConnection();
        assertCurrent();
        const flow = copyDraftFlow(rawFlow);
        if (flow.flow_id !== flowId) throw new Error('Runtime 返回的流程 ID 与请求流程不一致');
        assertCurrent();
        return flow;
      };
      const flow = await withTimeout(
        readSource(),
        CASE_DRAFT_LOAD_TIMEOUT_MS,
        '流程读取超过 5 秒，来源未确认；可重新读取流程。',
        controller.signal,
      );
      assertCurrent();
      return flow;
    } finally {
      signal.removeEventListener('abort', onAbort);
      controller.abort();
      flowControllers.delete(controller);
    }
  };

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
      return saveDraft(next, draft, persistedVersion);
    },
    editDraft: (value, expected, historyGroup) => editDraft(value, expected, historyGroup),
    editCaseText: (value, expected, historyGroup) =>
      editDraft(value, expected, historyGroup, true, true),
    editCaseBindings: (value, expected, historyGroup) =>
      editDraft(value, expected, historyGroup, false, true),
    undo: () => restoreHistory('undo'),
    redo: () => restoreHistory('redo'),
    readFlow,
    dispose: () => {
      disposed = true;
      generation++;
      abortFlowReads();
      loadController?.abort();
      loadController = null;
      pending = null;
      past.length = future.length = 0;
      activeHistoryGroup = undefined;
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
