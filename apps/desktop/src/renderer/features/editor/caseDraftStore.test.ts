import type {
  ActionStep,
  Case,
  CheckStep,
  LoopStep,
  ObservationSource,
  Step,
  TapAction,
} from '@pulse/schema';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import aiCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/ai.json';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import operatorsCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/operators.json';
import fillSearchFlow from '../../../../../../fixtures/golden-cases/v1-draft/flows/fill-search.json';
import type { ProjectState } from '../projects/projectStore';
import {
  caseDraftStorageKey,
  createCaseDraftStore,
  defaultCaseDraftStoreDeps,
  type CaseDraftRuntimeClient,
  type CaseDraftScope,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
} from './caseDraftStore';
import { assertDraftCase, copyDraftCase, copyDraftFlow } from './caseDraftValidation';

const scope: CaseDraftScope = { root: '/tmp/demo', projectId: 'demo', caseId: 'starter' };
const stores: CaseDraftStore[] = [];

function sourceCase(caseId = 'starter') {
  return {
    ...controlFlowCase,
    case_id: caseId,
    locators: {
      search: {
        candidates: [
          { by: 'res_id', value: 'com.pulse.target:id/search' },
          { by: 'desc', value: 'Search products', scope: null },
        ],
        expect: { unique: true, visible: true },
      },
    },
  };
}

function tap(id = 'insert-tap'): ActionStep & { action: TapAction } {
  return {
    type: 'action',
    id,
    action: {
      kind: 'tap',
      target: {
        locator: {
          candidates: [{ by: 'res_id', value: 'com.pulse.target:id/submit' }],
          expect: { unique: true, visible: true },
        },
      },
    },
  };
}

function check(
  id = 'insert-check',
  checkpointId = 'insert-exists',
  source: ObservationSource = 'native',
): CheckStep {
  return {
    type: 'check',
    id,
    checkpoint: {
      id: checkpointId,
      required: true,
      condition: { operator: 'exists', source, target: { locator_ref: 'search' } },
    },
  };
}

function readyState(selected: CaseDraftScope = scope): ProjectState {
  return {
    version: 1,
    status: 'ready',
    scope: { root: selected.root, projectId: selected.projectId },
    project: {
      root: selected.root,
      project_id: selected.projectId,
      name: 'Demo project',
      revision: '1',
      allowed_apps: ['com.pulse.target'],
      case_ids: ['starter', 'other'],
      data_ids: [],
      default_strategy: 'rule_only',
      entry_cases: ['starter'],
      flow_ids: ['fill-search', 'other-flow'],
      manifest: {},
      platforms: ['android'],
      profile_ids: ['android-fixture'],
      required_capabilities: [],
    },
    cases: {},
    selectedCaseId: selected.caseId,
    busy: null,
    dialogOpen: false,
    error: null,
    notice: null,
  };
}

function projectSource(initial = readyState()) {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    set: (partial: Partial<ProjectState>) => {
      state = { ...state, ...partial };
      for (const listener of listeners) listener();
    },
  };
}

function setup({
  raw = sourceCase(),
  projects = projectSource(),
  storage = window.localStorage,
  now = () => 1000,
}: {
  raw?: unknown;
  projects?: ReturnType<typeof projectSource>;
  storage?: CaseDraftStoreDeps['storage'];
  now?: CaseDraftStoreDeps['now'];
} = {}) {
  const getCase = vi.fn<CaseDraftRuntimeClient['getCase']>().mockResolvedValue(raw);
  const getFlow = vi
    .fn<NonNullable<CaseDraftRuntimeClient['getFlow']>>()
    .mockResolvedValue(fillSearchFlow);
  const getProject = vi.fn<CaseDraftRuntimeClient['getProject']>(
    async () => projects.getSnapshot().project,
  );
  const verifyConnection = vi.fn<CaseDraftRuntimeClient['verifyConnection']>(async () => undefined);
  const entries = new Map<string, ReturnType<typeof deferred<boolean>>>();
  const entered = (caseId: string) => {
    let gate = entries.get(caseId);
    if (!gate) {
      gate = deferred<boolean>();
      entries.set(caseId, gate);
    }
    return gate;
  };
  const getClient = vi.fn<CaseDraftStoreDeps['getClient']>(async () => ({
    getProject,
    verifyConnection,
    getCase: (projectId: string, caseId: string) => {
      entered(caseId).resolve(true);
      return getCase(projectId, caseId);
    },
    getFlow: (projectId: string, flowId: string) => {
      entered(`flow:${flowId}`).resolve(true);
      return getFlow(projectId, flowId);
    },
  }));
  const store = createCaseDraftStore({ projects, getClient, storage, now });
  stores.push(store);
  return {
    store,
    projects,
    getCase,
    getFlow,
    getClient,
    getProject,
    verifyConnection,
    getCaseEntered: (caseId = 'starter') => entered(caseId).promise,
    getFlowEntered: (flowId = 'fill-search') => entered(`flow:${flowId}`).promise,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function draftOf(store: CaseDraftStore): Case {
  const draft = store.getSnapshot().draft;
  if (!draft) throw new Error('test draft missing');
  return draft;
}

const flowReadPhases = [
  'client',
  'project-before',
  'verify-before',
  'flow',
  'project-after',
  'verify-after',
] as const;

function holdFlowRead(h: ReturnType<typeof setup>, phase: (typeof flowReadPhases)[number]) {
  const gate = deferred<unknown>();
  const entered = deferred<boolean>();
  const hold = () => {
    entered.resolve(true);
    return gate.promise;
  };
  const holdVerification = () => hold().then(() => undefined);
  const client: CaseDraftRuntimeClient = {
    getProject: h.getProject,
    getCase: h.getCase,
    getFlow: h.getFlow,
    verifyConnection: h.verifyConnection,
  };
  if (phase === 'client')
    h.getClient.mockImplementationOnce(() => hold() as Promise<CaseDraftRuntimeClient>);
  if (phase === 'project-before') h.getProject.mockImplementationOnce(hold);
  if (phase === 'verify-before') h.verifyConnection.mockImplementationOnce(holdVerification);
  if (phase === 'flow') h.getFlow.mockImplementationOnce(hold);
  if (phase === 'project-after')
    h.getProject
      .mockResolvedValueOnce(h.projects.getSnapshot().project)
      .mockImplementationOnce(hold);
  if (phase === 'verify-after')
    h.verifyConnection.mockResolvedValueOnce(undefined).mockImplementationOnce(holdVerification);
  const pending = () => {
    const mock =
      phase === 'client'
        ? h.getClient
        : phase === 'flow'
          ? h.getFlow
          : phase.startsWith('project')
            ? h.getProject
            : h.verifyConnection;
    return mock.mock.results.at(-1)?.value as Promise<unknown>;
  };
  return {
    entered: entered.promise,
    pending,
    release: () =>
      gate.resolve(
        phase === 'client'
          ? client
          : phase.startsWith('project')
            ? h.projects.getSnapshot().project
            : fillSearchFlow,
      ),
  };
}

function setupDefaultClient() {
  const projects = projectSource();
  const connection = { pid: 1234, port: 43125, token: 'test-token', version: '1-draft' };
  const getConnection = vi.fn<() => Promise<typeof connection | null>>(async () => connection);
  vi.stubGlobal('pulse', { runtime: { getConnection } });
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const path = new URL(String(input)).pathname;
    const value =
      path === '/projects/demo'
        ? projects.getSnapshot().project
        : path === '/projects/demo/cases/starter'
          ? sourceCase()
          : path === '/projects/demo/flows/fill-search'
            ? fillSearchFlow
            : null;
    if (value === null) throw new Error(`Unexpected Runtime request: ${path}`);
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  const store = createCaseDraftStore({ ...defaultCaseDraftStoreDeps(), projects, now: () => 1000 });
  stores.push(store);
  return { store, projects, connection, getConnection, fetchMock };
}

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  stores.splice(0).forEach((store) => store.dispose());
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test.each([
  ['no project', { project: null, scope: null }, /尚未打开工程/],
  ['no case', { selectedCaseId: null }, /选择用例/],
  ['unknown case', { selectedCaseId: 'missing' }, /不属于当前工程/],
  ['wrong root', { scope: { root: '/tmp/other', projectId: 'demo' } }, /工程范围无效/],
  ['wrong project ID', { scope: { root: '/tmp/demo', projectId: 'other' } }, /工程范围无效/],
  ['switching project', { busy: 'open' as const }, /正在打开或刷新工程/],
])(
  'rejects insertion with %s without reading or writing a case',
  async (_name, partial, reason) => {
    const projects = projectSource({ ...readyState(), ...partial });
    const { store, getCase } = setup({ projects });
    await store.load();
    expect(store.getSnapshot().canInsert).toBe(false);
    expect(store.getSnapshot().reason).toMatch(reason);
    expect(store.insertStep(tap())).toBe(false);
    expect(store.getSnapshot().draft).toBeNull();
    expect(getCase).not.toHaveBeenCalled();
    expect(window.localStorage.length).toBe(0);
  },
);

test('coalesces current-case loads and rejects rather than queues insertion while loading', async () => {
  const gate = deferred<unknown>();
  const { store, getCase, getCaseEntered } = setup();
  getCase.mockReturnValue(gate.promise);
  const loading = store.load();
  expect(store.load()).toBe(loading);
  await getCaseEntered();
  expect(getCase).toHaveBeenCalledExactlyOnceWith('demo', 'starter');
  expect(store.getSnapshot().status).toBe('loading');
  expect(store.insertStep(tap())).toBe(false);
  gate.resolve(sourceCase());
  await loading;
  expect(store.getSnapshot()).toMatchObject({
    status: 'ready',
    canInsert: true,
    persistence: 'none',
    error: null,
    scope,
  });
  expect(store.getSnapshot().draft).toEqual(sourceCase());
  expect(window.localStorage.length).toBe(0);
});

test.each([
  ['schema marker', { ...sourceCase(), $schema: 'urn:pulse:schema:case' }],
  ['missing fields', { case_id: 'starter', steps: [tap()] }],
  ['blank name', { ...sourceCase(), name: '  ' }],
  ['blank goal', { ...sourceCase(), goal: '' }],
  ['empty steps', { ...sourceCase(), steps: [] }],
  ['empty profiles', { ...sourceCase(), profile_refs: [] }],
  ['bad locators', { ...sourceCase(), locators: [] }],
  ['bad inputs', { ...sourceCase(), inputs: { product: { param_ref: 'product', extra: true } } }],
  ['wrong case ID', { ...sourceCase(), case_id: 'other' }],
  ['invalid timeout', { ...sourceCase(), steps: [{ ...tap(), timeout_ms: 0 }] }],
  ['unexpected field', { ...sourceCase(), invented_dsl: 'tap' }],
  [
    'checkpoint missing source',
    {
      ...sourceCase(),
      checks: [
        {
          id: 'invalid',
          required: true,
          condition: { operator: 'exists', target: { locator_ref: 'search' } },
        },
      ],
    },
  ],
])('rejects invalid original case: %s', async (_name, raw) => {
  const { store } = setup({ raw });
  await store.load();
  expect(store.getSnapshot()).toMatchObject({
    status: 'error',
    draft: null,
    original: null,
    canInsert: false,
  });
  expect(store.getSnapshot().error).toBeTruthy();
  expect(store.insertStep(tap())).toBe(false);
  expect(window.localStorage.length).toBe(0);
});

test.each([controlFlowCase, operatorsCase, aiCase])(
  'preserves every field of the existing golden case $case_id',
  async (fixture) => {
    const original = { ...fixture, case_id: 'starter' };
    const { store } = setup({ raw: original });
    await store.load();
    expect(store.getSnapshot().draft).toEqual(original);
    expect(store.insertStep(tap())).toBe(true);
    expect(store.getSnapshot().draft).toEqual({ ...original, steps: [...original.steps, tap()] });
    expect(store.getSnapshot().original).toEqual(original);
  },
);

test('inserts actual tap and explicitly sourced exists steps, preserving all original fields and detaching inputs', async () => {
  const raw = sourceCase();
  const original = structuredClone(raw);
  const { store } = setup({ raw });
  await store.load();
  const inserted = tap();
  expect(store.insertStep(inserted)).toBe(true);
  expect(store.insertStep(check())).toBe(true);
  expect(store.insertStep(check('insert-ocr', 'ocr-exists', 'ocr'))).toBe(true);
  const draft = store.getSnapshot().draft;
  expect(draft).toEqual({
    ...original,
    steps: [...original.steps, tap(), check(), check('insert-ocr', 'ocr-exists', 'ocr')],
  });
  expect(store.getSnapshot()).toMatchObject({
    persistence: 'saved',
    savedAt: 1000,
    canInsert: true,
    error: null,
  });
  expect(store.getSnapshot().original).toEqual(original);
  expect(raw).toEqual(original);
  raw.inputs.product.key = 'changed';
  if ('locator' in inserted.action.target) inserted.action.target.locator.candidates[0].by = 'text';
  expect(store.getSnapshot().original).toEqual(original);
  expect(store.getSnapshot().draft).toEqual(draft);
  expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
    version: 1,
    scope,
    original,
    draft,
    savedAt: 1000,
  });
  expect(Object.isFrozen(draft?.steps)).toBe(true);
});

test('restores inserted steps after recreating the store from storage and Runtime original', async () => {
  const first = setup();
  await first.store.load();
  expect(first.store.insertStep(tap())).toBe(true);
  expect(first.store.insertStep(check())).toBe(true);
  first.store.dispose();
  const reloaded = setup();
  await reloaded.store.load();
  expect(reloaded.getCase).toHaveBeenCalledExactlyOnceWith('demo', 'starter');
  expect(reloaded.store.getSnapshot()).toMatchObject({
    status: 'ready',
    persistence: 'saved',
    canInsert: true,
  });
  expect(reloaded.store.getSnapshot().draft?.steps).toEqual([
    ...sourceCase().steps,
    tap(),
    check(),
  ]);
});

test.each(['launch', 'tap-banner', 'wait-page', 'iteration-label', 'long-product'])(
  'rejects existing nested step ID %s',
  async (id) => {
    const { store } = setup();
    await store.load();
    const before = store.getSnapshot().draft;
    expect(store.insertStep(tap(id))).toBe(false);
    expect(store.getSnapshot().draft).toBe(before);
    expect(store.getSnapshot().error).toMatch(/重复步骤 ID/);
    expect(window.localStorage.length).toBe(0);
  },
);

test.each(['page-ready', 'selection-checked', 'product-name', 'quantity'])(
  'rejects existing procedure-wide checkpoint ID %s',
  async (id) => {
    const { store } = setup();
    await store.load();
    expect(store.insertStep(check('new-check', id))).toBe(false);
    expect(store.getSnapshot().error).toMatch(/重复检查点 ID/);
    expect(store.getSnapshot().draft).toEqual(sourceCase());
  },
);

test('rejects checkpoint IDs already used by an AI step', async () => {
  const raw = { ...sourceCase(), steps: [...sourceCase().steps, ...aiCase.steps] };
  const { store } = setup({ raw });
  await store.load();
  expect(store.insertStep(check('new-check', 'summary'))).toBe(false);
  expect(store.getSnapshot().error).toMatch(/重复检查点 ID/);
  expect(store.getSnapshot().draft).toEqual(raw);
});

test('rejects duplicate IDs in the original document, not just newly inserted steps', async () => {
  for (const raw of [
    { ...sourceCase(), steps: [...sourceCase().steps, tap('tap-banner')] },
    {
      ...sourceCase(),
      checks: [...sourceCase().checks, check('new-check', 'page-ready').checkpoint],
    },
  ]) {
    const { store } = setup({ raw });
    await store.load();
    expect(store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
    expect(store.getSnapshot().error).toMatch(/重复.*ID/);
  }
});

test('rejects duplicate new IDs and malformed steps without changing the saved draft', async () => {
  const { store } = setup();
  await store.load();
  expect(store.insertStep(tap())).toBe(true);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  const draft = store.getSnapshot().draft;
  const malformed: unknown[] = [
    tap(),
    { ...tap('no-target'), action: { kind: 'tap' } },
    {
      ...check(),
      checkpoint: {
        id: 'missing-source',
        required: true,
        condition: { operator: 'exists', target: { locator_ref: 'search' } },
      },
    },
    {
      ...tap('bad-target'),
      action: {
        kind: 'tap',
        target: {
          locator_ref: 'search',
          locator: {
            candidates: [{ by: 'text', value: 'Search' }],
            expect: { unique: true, visible: true },
          },
        },
      },
    },
    {
      type: 'if',
      id: 'new-branch',
      condition: check().checkpoint.condition,
      then: [tap('same'), tap('same')],
    },
    { ...tap('unsafe-time'), timeout_ms: Number.MAX_SAFE_INTEGER + 1 },
  ];
  for (const step of malformed) {
    expect(store.insertStep(step as Step)).toBe(false);
    expect(store.getSnapshot().draft).toBe(draft);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  }
});

test.each(['case', 'root', 'projectId'] as const)(
  'isolates persisted drafts by %s and restores the previous scope',
  async (dimension) => {
    const { store, projects, getCase } = setup();
    getCase.mockImplementation(async (_projectId, caseId) => sourceCase(caseId));
    await store.load();
    expect(store.insertStep(tap('first-scope'))).toBe(true);
    const next = {
      ...scope,
      ...(dimension === 'case'
        ? { caseId: 'other' }
        : dimension === 'root'
          ? { root: '/tmp/other-root' }
          : { projectId: 'second' }),
    };
    projects.set(readyState(next));
    await store.load();
    expect(store.getSnapshot().scope).toEqual(next);
    expect(store.getSnapshot().draft?.steps).not.toContainEqual(tap('first-scope'));
    expect(store.insertStep(tap('second-scope'))).toBe(true);
    projects.set(readyState(scope));
    await store.load();
    expect(store.getSnapshot().draft?.steps).toEqual([...sourceCase().steps, tap('first-scope')]);
    expect(caseDraftStorageKey(scope)).not.toBe(caseDraftStorageKey(next));
    expect(window.localStorage.length).toBe(2);
  },
);

test.each(['resolve', 'reject'] as const)(
  'ignores late %s of a previous case load after another draft was inserted',
  async (completion) => {
    const a = deferred<unknown>();
    const b = deferred<unknown>();
    const { store, projects, getCase, getCaseEntered } = setup();
    getCase.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const loadA = store.load();
    await getCaseEntered();
    projects.set({ selectedCaseId: 'other' });
    const loadB = store.load();
    await getCaseEntered('other');
    b.resolve(sourceCase('other'));
    await loadB;
    expect(store.insertStep(tap('other-added'))).toBe(true);
    if (completion === 'resolve') a.resolve(sourceCase());
    else a.reject(new Error('late old-case failure'));
    await loadA;
    expect(store.getSnapshot()).toMatchObject({
      status: 'ready',
      scope: { ...scope, caseId: 'other' },
      error: null,
      persistence: 'saved',
    });
    expect(store.getSnapshot().draft?.steps).toEqual([
      ...sourceCase('other').steps,
      tap('other-added'),
    ]);
  },
);

test('reload/insert concurrency cannot discard a saved step or queue a rejected step', async () => {
  const { store, getCase } = setup();
  await store.load();
  expect(store.insertStep(tap('saved-before-reload'))).toBe(true);
  const gate = deferred<unknown>();
  getCase.mockReturnValueOnce(gate.promise);
  const loading = store.load();
  expect(store.insertStep(tap('must-not-queue'))).toBe(false);
  gate.resolve(sourceCase());
  await loading;
  expect(store.getSnapshot().draft?.steps).toEqual([
    ...sourceCase().steps,
    tap('saved-before-reload'),
  ]);
  expect(store.insertStep(tap('saved-after-reload'))).toBe(true);
});

test('detects source changes even with unchanged scope and preserves the old saved draft', async () => {
  const { store, projects, getCase } = setup();
  await store.load();
  expect(store.insertStep(tap())).toBe(true);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  getCase.mockResolvedValue({ ...sourceCase(), name: 'Changed original case' });
  const project = projects.getSnapshot().project;
  if (!project) throw new Error('test project missing');
  projects.set({ project: { ...project, revision: '2' } });
  await store.load();
  expect(store.getSnapshot()).toMatchObject({
    status: 'error',
    canInsert: false,
    draft: null,
    original: { name: 'Changed original case' },
  });
  expect(store.getSnapshot().error).toMatch(/原始用例已变化/);
  expect(store.insertStep(tap('no-overwrite'))).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

function savedRecord() {
  const original = sourceCase();
  return {
    version: 1,
    scope,
    original,
    draft: { ...original, steps: [...original.steps, tap()] },
    savedAt: 1000,
  };
}

const damagedRecords: Array<[string, (record: ReturnType<typeof savedRecord>) => string]> = [
  ['invalid JSON', () => '{'],
  ['version', (record) => JSON.stringify({ ...record, version: 3 })],
  ['scope', (record) => JSON.stringify({ ...record, scope: { ...scope, root: '/tmp/mismatch' } })],
  ['missing base', (record) => JSON.stringify({ ...record, original: null })],
  ['empty steps', (record) => JSON.stringify({ ...record, draft: { ...record.draft, steps: [] } })],
  [
    'non-step edits',
    (record) => JSON.stringify({ ...record, draft: { ...record.draft, inputs: {} } }),
  ],
  [
    'changed original step',
    (record) =>
      JSON.stringify({
        ...record,
        draft: { ...record.draft, steps: [tap('replacement'), ...record.draft.steps.slice(1)] },
      }),
  ],
  [
    'duplicate checkpoint',
    (record) =>
      JSON.stringify({
        ...record,
        draft: { ...record.draft, steps: [...record.draft.steps, check('new', 'page-ready')] },
      }),
  ],
];

test.each(damagedRecords)('refuses to overwrite damaged saved draft: %s', async (_name, damage) => {
  const saved = damage(savedRecord());
  window.localStorage.setItem(caseDraftStorageKey(scope), saved);
  const { store } = setup();
  await store.load();
  expect(store.getSnapshot()).toMatchObject({ status: 'error', canInsert: false, draft: null });
  if (_name === 'version') expect(store.getSnapshot().reason).toMatch(/本地草稿记录无效/);
  expect(store.insertStep(tap('no-overwrite'))).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

test('detects another writer before saving instead of silently losing its inserted steps', async () => {
  const a = setup();
  const b = setup();
  await Promise.all([a.store.load(), b.store.load()]);
  expect(a.store.insertStep(tap('writer-a'))).toBe(true);
  expect(b.store.insertStep(tap('writer-b'))).toBe(false);
  expect(b.store.getSnapshot().error).toMatch(/其他页面修改/);
  await b.store.load();
  expect(b.store.insertStep(tap('writer-b'))).toBe(true);
  expect(a.store.insertStep(tap('writer-a-again'))).toBe(false);
  expect(b.store.getSnapshot().draft?.steps).toEqual([
    ...sourceCase().steps,
    tap('writer-a'),
    tap('writer-b'),
  ]);
});

test.each(['quota', 'unconfirmed'] as const)(
  'does not claim insertion or saving succeeded with %s storage failure',
  async (failure) => {
    const setItem = vi.fn(() => {
      if (failure === 'quota') throw new Error('QuotaExceededError');
    });
    const { store } = setup({ storage: { getItem: () => null, setItem } });
    await store.load();
    const before = store.getSnapshot().draft;
    expect(store.insertStep(tap())).toBe(false);
    expect(store.getSnapshot()).toMatchObject({
      status: 'ready',
      persistence: 'error',
      savedAt: null,
      canInsert: false,
    });
    expect(store.getSnapshot().draft).toBe(before);
    expect(store.getSnapshot().error).toMatch(/草稿保存失败或未确认/);
    expect(store.insertStep(tap('blocked-after-failure'))).toBe(false);
    expect(setItem).toHaveBeenCalledTimes(1);
  },
);

test('shows the original read-only when local persistence is unavailable', async () => {
  const { store } = setup({ storage: null });
  await store.load();
  expect(store.getSnapshot()).toMatchObject({
    status: 'ready',
    persistence: 'error',
    canInsert: false,
  });
  expect(store.getSnapshot().original).toEqual(sourceCase());
  expect(store.insertStep(tap())).toBe(false);
  expect(store.getSnapshot().draft).toEqual(sourceCase());
  expect(store.getSnapshot().error).toMatch(/存储不可用/);
});

test('storage read refusal cannot hide or overwrite a previously saved draft', async () => {
  const setItem = vi.fn();
  const { store } = setup({
    storage: {
      getItem: () => {
        throw new Error('Storage blocked');
      },
      setItem,
    },
  });
  await store.load();
  expect(store.getSnapshot()).toMatchObject({ status: 'error', canInsert: false, draft: null });
  expect(store.getSnapshot().original).toEqual(sourceCase());
  expect(store.getSnapshot().error).toMatch(/Storage blocked/);
  expect(store.insertStep(tap())).toBe(false);
  expect(setItem).not.toHaveBeenCalled();
});

test('Runtime read failure does not invent a blank Case or recover an unverified source from local storage', async () => {
  window.localStorage.setItem(caseDraftStorageKey(scope), JSON.stringify(savedRecord()));
  const { store, getCase } = setup();
  getCase.mockRejectedValue(new Error('Runtime disconnected'));
  await store.load();
  expect(store.getSnapshot()).toMatchObject({
    status: 'error',
    original: null,
    draft: null,
    canInsert: false,
  });
  expect(store.insertStep(tap())).toBe(false);
  expect(store.getSnapshot().error).toMatch(/Runtime disconnected/);
});

test('validates generated schema without eval or Function compilation', () => {
  const compile = vi.fn(() => {
    throw new Error('unsafe-eval prohibited');
  });
  vi.stubGlobal('eval', compile);
  vi.stubGlobal('Function', compile);
  assertDraftCase(sourceCase());
  copyDraftCase(sourceCase());
  copyDraftFlow(fillSearchFlow);
  expect(compile).not.toHaveBeenCalled();
});

test('preserves deeply nested legal steps while rejecting cyclic structures deterministically', async () => {
  let step: Step = tap('leaf');
  for (let index = 0; index < 24; index++) {
    step = { type: 'loop', id: `loop_${index}`, max_iterations: 1, steps: [step] };
  }
  const raw = { ...sourceCase(), steps: [step] };
  const { store } = setup({ raw });
  await store.load();
  expect(store.getSnapshot().draft).toEqual(raw);
  expect(store.insertStep(tap('appended'))).toBe(true);
  const cyclic: LoopStep = { type: 'loop', id: 'cycle', max_iterations: 1, steps: [tap()] };
  cyclic.steps = [cyclic];
  const invalid = setup({ raw: { ...sourceCase(), steps: [cyclic] } });
  await invalid.store.load();
  expect(invalid.store.getSnapshot()).toMatchObject({
    status: 'error',
    canInsert: false,
    draft: null,
  });
});

test('disposing cancels stale loads and releases the project subscription without deleting drafts', async () => {
  const gate = deferred<unknown>();
  const { store, projects, getCase, getCaseEntered } = setup();
  getCase.mockReturnValue(gate.promise);
  const loading = store.load();
  await getCaseEntered();
  store.dispose();
  gate.resolve(sourceCase());
  await loading;
  projects.set({ selectedCaseId: 'other' });
  expect(store.getSnapshot()).toMatchObject({ status: 'idle', canInsert: false, draft: null });
  expect(store.insertStep(tap())).toBe(false);
  expect(getCase).toHaveBeenCalledTimes(1);
});

test.each([
  { root: '/tmp/runtime-b', project_id: 'demo', case_ids: ['starter'] },
  { root: '/tmp/demo', project_id: 'foreign', case_ids: ['starter'] },
  { root: '/tmp/demo', project_id: 'demo', case_ids: ['other'] },
])(
  'does not read a same-ID Case from an unverified Runtime project $root/$project_id',
  async (remote) => {
    const h = setup();
    h.getProject.mockResolvedValue(remote);
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({
      scope,
      status: 'error',
      original: null,
      draft: null,
      canInsert: false,
    });
    expect(h.store.getSnapshot().error).toMatch(/工程来源/);
    expect(h.getCase).not.toHaveBeenCalled();
    expect(h.store.insertStep(tap())).toBe(false);
    expect(window.localStorage.length).toBe(0);
  },
);

test('a project remap during a Case read discards the result instead of binding it to the old root', async () => {
  const h = setup();
  h.getProject
    .mockResolvedValueOnce(readyState().project)
    .mockResolvedValueOnce({ ...readyState().project, root: '/tmp/remapped' });
  await h.store.load();
  expect(h.getCase).toHaveBeenCalledOnce();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'error',
    original: null,
    draft: null,
    canInsert: false,
  });
  expect(h.store.getSnapshot().error).toMatch(/工程来源/);
});

test('Runtime verification failure after a Case read prevents insertion or persisted source promotion', async () => {
  const h = setup();
  h.getCase.mockImplementation(async () => {
    h.verifyConnection.mockRejectedValueOnce(new Error('Runtime changed'));
    return sourceCase();
  });
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'error',
    original: null,
    draft: null,
    canInsert: false,
  });
  expect(h.store.getSnapshot().error).toMatch(/Runtime changed/);
  expect(window.localStorage.length).toBe(0);
});

test('a never-settling client discovery has a deadline, releases the pending load, and ignores its late client', async () => {
  vi.useFakeTimers();
  const gate = deferred<CaseDraftRuntimeClient>();
  const getClient = vi.fn<CaseDraftStoreDeps['getClient']>(() => gate.promise);
  const projects = projectSource();
  const store = createCaseDraftStore({
    projects,
    getClient,
    storage: window.localStorage,
    now: () => 1000,
  });
  stores.push(store);
  const loading = store.load();
  await vi.advanceTimersByTimeAsync(5001);
  await loading;
  expect(store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
  expect(store.getSnapshot().error).toMatch(/超过 5 秒/);
  const recovered: CaseDraftRuntimeClient = {
    getProject: async () => readyState().project,
    getCase: async () => sourceCase(),
    verifyConnection: async () => undefined,
  };
  getClient.mockResolvedValue(recovered);
  await store.load();
  expect(store.getSnapshot()).toMatchObject({ status: 'ready', canInsert: true });
  const lateProject = vi.fn(async () => readyState().project);
  gate.resolve({ ...recovered, getProject: lateProject });
  await Promise.resolve();
  expect(lateProject).not.toHaveBeenCalled();
});

test('a never-settling Case read is aborted after the shared deadline and supports explicit retry', async () => {
  vi.useFakeTimers();
  const h = setup();
  h.getCase.mockReturnValueOnce(new Promise(() => undefined));
  const loading = h.store.load();
  await h.getCaseEntered();
  await vi.advanceTimersByTimeAsync(5001);
  await loading;
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
  h.getCase.mockResolvedValue(sourceCase());
  await h.store.load();
  expect(h.getCase).toHaveBeenCalledTimes(2);
  expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', canInsert: true });
});

test.each([controlFlowCase, operatorsCase, aiCase])(
  'fully edits and restores golden Case $case_id without changing protected original fields',
  async (fixture) => {
    const original = copyDraftCase(fixture);
    const selected = { ...scope, caseId: original.case_id };
    const initial = readyState(selected);
    if (!initial.project) throw new Error('test project missing');
    initial.project.case_ids = [original.case_id];
    const projects = projectSource(initial);
    const h = setup({ raw: fixture, projects });
    await h.store.load();
    const expected = draftOf(h.store);
    const next = structuredClone(expected);
    next.locators = {
      search: {
        candidates: [{ by: 'text', value: 'Edited query', scope: null }],
        expect: { unique: false, visible: true },
      },
    };
    next.preconditions = [
      ...next.preconditions.map((item) => ({ ...item, required: !item.required })),
      check('unused-before', 'edited-before').checkpoint,
    ];
    next.checks = [
      ...next.checks.map((item) => ({
        ...item,
        required: !item.required,
        condition: { ...item.condition, source: 'ocr' as const },
      })),
      check('unused-after', 'edited-after', 'vision').checkpoint,
    ];
    for (const step of next.steps) {
      step.timeout_ms = 4321;
      if (step.type === 'if') {
        step.then = [tap('edited-then')];
        step.else = [];
        step.condition = { ...step.condition, source: 'webview' };
      }
      if (step.type === 'loop') {
        step.max_iterations = 3;
        step.steps = [tap('edited-loop-child')];
      }
      if (step.type === 'flow') {
        step.arguments = { query: 'Edited query' };
        step.bind = { label: 'edited_label' };
      }
      if (step.type === 'ai_step') {
        step.goal = 'Edited AI description';
        step.checks[0].required = false;
      }
    }
    next.steps.reverse();
    if (next.steps.length > 1) next.steps.splice(1, 1);
    next.steps.push(tap('edited-new'));
    const edited = structuredClone(next);
    expect(h.store.editDraft(next, expected)).toBe(true);
    const draft = draftOf(h.store);
    expect(draft).toEqual(edited);
    expect(draft).not.toBe(next);
    expect(h.store.getSnapshot().original).toBe(expected);
    expect(h.store.getSnapshot().original).toEqual(original);
    expect(fixture).toEqual(original);
    expect(Object.isFrozen(draft)).toBe(true);
    expect(Object.isFrozen(draft.steps[0])).toBe(true);
    if (!draft.locators.search || !draft.preconditions[0] || !draft.checks[0]) {
      throw new Error('edited locator or checkpoints missing');
    }
    expect(Object.isFrozen(draft.locators.search.candidates[0])).toBe(true);
    expect(Object.isFrozen(draft.preconditions[0].condition)).toBe(true);
    expect(Object.isFrozen(draft.checks[0].condition)).toBe(true);
    next.steps[0].id = 'mutated-input';
    if (!next.locators.search) throw new Error('test locator missing');
    next.locators.search.candidates[0].value = 'Mutated input';
    expect(draftOf(h.store)).toEqual(edited);
    expect(
      JSON.parse(window.localStorage.getItem(caseDraftStorageKey(selected)) ?? 'null'),
    ).toEqual({
      version: 2,
      scope: selected,
      original,
      draft: edited,
      savedAt: 1000,
    });
    h.store.dispose();
    const restored = setup({ raw: fixture, projects });
    await restored.store.load();
    expect(restored.store.getSnapshot()).toMatchObject({
      status: 'ready',
      persistence: 'saved',
      canInsert: true,
      original,
      draft: edited,
      savedAt: 1000,
    });
    expect(restored.store.insertStep(tap('inspector-append'))).toBe(true);
    expect(draftOf(restored.store)).toEqual({
      ...edited,
      steps: [...edited.steps, tap('inspector-append')],
    });
    expect(
      JSON.parse(window.localStorage.getItem(caseDraftStorageKey(selected)) ?? 'null').version,
    ).toBe(2);
  },
);

test('keeps loaded v1 append records at v1 until an actual edit, then preserves v2 on insertion', async () => {
  window.localStorage.setItem(caseDraftStorageKey(scope), JSON.stringify(savedRecord()));
  const h = setup();
  await h.store.load();
  expect(h.store.insertStep(tap('v1-append'))).toBe(true);
  expect(
    JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null').version,
  ).toBe(1);
  const expected = draftOf(h.store);
  expect(h.store.editDraft({ ...expected, steps: [tap('v2-replacement')] }, expected)).toBe(true);
  await h.store.load();
  expect(draftOf(h.store).steps).toEqual([tap('v2-replacement')]);
  expect(h.store.insertStep(tap('v2-append'))).toBe(true);
  expect(
    JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null'),
  ).toMatchObject({
    version: 2,
    original: sourceCase(),
    draft: { steps: [tap('v2-replacement'), tap('v2-append')] },
  });
});

const v1OnlyMutations: Array<[string, (draft: Case) => void]> = [
  [
    'locators',
    (draft) => {
      draft.locators = {};
    },
  ],
  [
    'preconditions',
    (draft) => {
      draft.preconditions = [];
    },
  ],
  [
    'checks',
    (draft) => {
      draft.checks = [];
    },
  ],
  [
    'deletion',
    (draft) => {
      draft.steps.splice(0, 1);
    },
  ],
  [
    'reordering',
    (draft) => {
      draft.steps.reverse();
    },
  ],
  [
    'nested replacement',
    (draft) => {
      const branch = draft.steps.find((step) => step.type === 'if');
      if (branch?.type !== 'if') throw new Error('test branch missing');
      branch.then = [tap('replaced-child')];
    },
  ],
];

test.each(v1OnlyMutations)(
  'retains strict v1 tamper rejection for %s edits allowed only in v2',
  async (_name, mutate) => {
    const record = savedRecord();
    const draft = structuredClone(copyDraftCase(record.draft));
    mutate(draft);
    const saved = JSON.stringify({ ...record, draft });
    window.localStorage.setItem(caseDraftStorageKey(scope), saved);
    const h = setup();
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
    expect(h.store.getSnapshot().reason).toMatch(/本地草稿修改了原始字段或步骤/);
    expect(h.store.editDraft(draft, draft)).toBe(false);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  },
);

const protectedEdits = [
  ['case_id', { case_id: 'other' }],
  ['name', { name: 'Changed name' }],
  ['goal', { goal: 'Changed goal' }],
  ['inputs', { inputs: {} }],
  ['parameters', { parameters: {} }],
  ['profile_refs', { profile_refs: ['other-profile'] }],
  ['source', { source: null }],
  ['original_text', { original_text: null }],
  ['schema', { $schema: 'urn:pulse:schema:case' }],
  ['scope', { scope: { ...scope, root: '/tmp/foreign' } }],
] as const;

test.each(protectedEdits)(
  'rejects non-editable %s in both incoming edits and restored v2 records',
  async (_name, fields) => {
    const h = setup();
    await h.store.load();
    const expected = draftOf(h.store);
    const next = { ...expected, ...fields } as Case;
    expect(h.store.editDraft(next, expected)).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot().canInsert).toBe(true);
    expect(h.store.getSnapshot().error).toMatch(/草稿编辑失败/);
    expect(window.localStorage.length).toBe(0);
    const saved = JSON.stringify({ ...savedRecord(), version: 2, draft: next });
    window.localStorage.setItem(caseDraftStorageKey(scope), saved);
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
    expect(h.store.getSnapshot().reason).toMatch(/结构无效|修改了原始字段或步骤/);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  },
);

test.each(['source', 'original_text'] as const)(
  'does not normalize a removed optional protected %s field into null',
  async (field) => {
    const h = setup();
    await h.store.load();
    const expected = draftOf(h.store);
    const next = structuredClone(expected);
    if (field === 'source') delete next.source;
    else delete next.original_text;
    expect(h.store.editDraft(next, expected)).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(window.localStorage.length).toBe(0);
  },
);

test.each(['none', 'v1', 'v2'] as const)(
  'accepts a current-reference no-op with %s persistence without saving or changing the snapshot',
  async (persistence) => {
    const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
    const now = vi.fn(() => 1000);
    const h = setup({
      storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
      now,
    });
    await h.store.load();
    if (persistence === 'v1') expect(h.store.insertStep(tap())).toBe(true);
    if (persistence === 'v2') {
      const expected = draftOf(h.store);
      expect(h.store.editDraft({ ...expected, steps: [tap('edited')] }, expected)).toBe(true);
    }
    setItem.mockClear();
    now.mockClear();
    const before = h.store.getSnapshot();
    const expected = draftOf(h.store);
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    expect(h.store.editDraft(expected, expected)).toBe(true);
    expect(h.store.editDraft(structuredClone(expected), expected)).toBe(true);
    expect(h.store.getSnapshot()).toBe(before);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    expect(setItem).not.toHaveBeenCalled();
    expect(now).not.toHaveBeenCalled();
  },
);

test('rejects equal copies, another store, and stale versions as expected instead of matching by Case ID or JSON', async () => {
  const a = setup();
  const b = setup();
  await Promise.all([a.store.load(), b.store.load()]);
  const first = draftOf(a.store);
  const next: Case = { ...first, steps: [tap('edited')] };
  expect(a.store.editDraft(next, structuredClone(first))).toBe(false);
  expect(a.store.editDraft(next, draftOf(b.store))).toBe(false);
  expect(a.store.editDraft(next, first)).toBe(true);
  const edited = draftOf(a.store);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  expect(a.store.editDraft({ ...first, steps: [tap('stale')] }, first)).toBe(false);
  expect(draftOf(a.store)).toBe(edited);
  expect(a.store.getSnapshot().error).toMatch(/草稿版本已变化/);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  await a.store.load();
  expect(draftOf(a.store)).toEqual(edited);
  expect(draftOf(a.store)).not.toBe(edited);
  expect(a.store.editDraft(edited, edited)).toBe(false);
  expect(a.store.editDraft(edited, draftOf(a.store))).toBe(true);
});

test.each(['case', 'root', 'projectId'] as const)(
  'rejects an editor expected from the previous %s scope even when its next input matches the new Case',
  async (dimension) => {
    const h = setup();
    h.getCase.mockImplementation(async (_projectId, caseId) => sourceCase(caseId));
    await h.store.load();
    const old = draftOf(h.store);
    const selected = {
      ...scope,
      ...(dimension === 'case'
        ? { caseId: 'other' }
        : dimension === 'root'
          ? { root: '/tmp/new-root' }
          : { projectId: 'new-project' }),
    };
    h.projects.set(readyState(selected));
    await h.store.load();
    const current = draftOf(h.store);
    expect(h.store.editDraft({ ...current, steps: [tap('stale-editor')] }, old)).toBe(false);
    expect(draftOf(h.store)).toBe(current);
    expect(h.store.getSnapshot().canInsert).toBe(true);
    expect(window.localStorage.length).toBe(0);
  },
);

test('rejects edits while reloading or disposed without queuing them', async () => {
  const h = setup();
  await h.store.load();
  const expected = draftOf(h.store);
  const gate = deferred<unknown>();
  h.getCase.mockReturnValueOnce(gate.promise);
  const loading = h.store.load();
  expect(h.store.editDraft({ ...expected, steps: [tap('queued')] }, expected)).toBe(false);
  gate.resolve(sourceCase());
  await loading;
  expect(draftOf(h.store)).toEqual(expected);
  expect(h.store.editDraft(expected, expected)).toBe(false);
  const beforeDispose = draftOf(h.store);
  h.store.dispose();
  expect(h.store.editDraft({ ...beforeDispose, steps: [tap()] }, beforeDispose)).toBe(false);
  expect(h.store.getSnapshot()).toMatchObject({ status: 'idle', draft: null, canInsert: false });
  expect(window.localStorage.length).toBe(0);
});

test('rejects invalid nested edits and v2 restores through the same generated schema and procedure-wide ID checks', async () => {
  const raw = { ...sourceCase(), steps: [...sourceCase().steps, ...aiCase.steps] };
  const h = setup({ raw });
  await h.store.load();
  const expected = draftOf(h.store);
  const malformed: unknown[] = [
    { ...expected, steps: [] },
    { ...expected, locators: { bad: { candidates: [], expect: { unique: true, visible: true } } } },
    {
      ...expected,
      steps: [{ type: 'if', id: 'branch', condition: check().checkpoint.condition, then: [] }],
    },
    { ...expected, steps: [{ type: 'loop', id: 'loop', max_iterations: 1, steps: [] }] },
    { ...expected, steps: [{ ...tap(), timeout_ms: 0 }] },
    {
      ...expected,
      steps: [
        {
          type: 'if',
          id: 'branch',
          condition: check().checkpoint.condition,
          then: [tap('duplicate')],
          else: [{ type: 'loop', id: 'nested', max_iterations: 1, steps: [tap('duplicate')] }],
        },
      ],
    },
    { ...expected, steps: [...expected.steps, tap('iteration-label')] },
    { ...expected, checks: [...expected.checks, check('unused', 'summary').checkpoint] },
    { ...expected, steps: [...expected.steps, check('new-check', 'page-ready')] },
    { ...expected, preconditions: [check('unused', 'selection-checked').checkpoint] },
    {
      ...expected,
      checks: [
        {
          id: 'invalid',
          required: true,
          condition: { operator: 'exists', target: { locator_ref: 'search' } },
        },
      ],
    },
  ];
  for (const value of malformed) {
    expect(h.store.editDraft(value as Case, expected)).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot().canInsert).toBe(true);
    expect(h.store.getSnapshot().error).toMatch(/结构无效|重复.*ID/);
    expect(window.localStorage.length).toBe(0);
    const saved = JSON.stringify({ version: 2, scope, original: raw, draft: value, savedAt: 1000 });
    window.localStorage.setItem(caseDraftStorageKey(scope), saved);
    const restored = setup({ raw });
    await restored.store.load();
    expect(restored.store.getSnapshot()).toMatchObject({
      status: 'error',
      draft: null,
      canInsert: false,
    });
    expect(restored.store.getSnapshot().reason).toMatch(/结构无效|重复.*ID/);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    window.localStorage.removeItem(caseDraftStorageKey(scope));
  }
});

test.each(['edit', 'insert'] as const)(
  'does not overwrite another writer after that writer performs %s, and stays closed until reload',
  async (firstOperation) => {
    const a = setup();
    const b = setup();
    await Promise.all([a.store.load(), b.store.load()]);
    const aDraft = draftOf(a.store);
    const bDraft = draftOf(b.store);
    expect(
      firstOperation === 'edit'
        ? a.store.editDraft({ ...aDraft, steps: [...aDraft.steps, tap('writer-a')] }, aDraft)
        : a.store.insertStep(tap('writer-a')),
    ).toBe(true);
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    expect(
      b.store.editDraft({ ...bDraft, steps: [...bDraft.steps, tap('writer-b')] }, bDraft),
    ).toBe(false);
    expect(draftOf(b.store)).toBe(bDraft);
    expect(b.store.getSnapshot()).toMatchObject({ canInsert: false, persistence: 'error' });
    expect(b.store.getSnapshot().error).toMatch(/其他页面修改/);
    expect(b.store.editDraft(bDraft, bDraft)).toBe(false);
    expect(b.store.insertStep(tap('blocked'))).toBe(false);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    await b.store.load();
    const reloaded = draftOf(b.store);
    expect(
      b.store.editDraft({ ...reloaded, steps: [...reloaded.steps, tap('writer-b')] }, reloaded),
    ).toBe(true);
    expect(draftOf(b.store).steps.slice(-2)).toEqual([tap('writer-a'), tap('writer-b')]);
    expect(a.store.insertStep(tap('stale-a'))).toBe(false);
  },
);

test.each([
  'before-read',
  'write',
  'ignored-write',
  'after-write',
  'confirmation-read',
  'overwritten',
] as const)(
  'never promotes an edit or claims success after %s persistence failure',
  async (failure) => {
    let fail = false;
    let written = false;
    const getItem = vi.fn((key: string) => {
      if (fail && (failure === 'before-read' || (failure === 'confirmation-read' && written)))
        throw new Error('Storage read blocked');
      return window.localStorage.getItem(key);
    });
    const setItem = vi.fn((key: string, value: string) => {
      if (fail && failure === 'write') throw new Error('QuotaExceededError');
      if (fail && failure === 'ignored-write') return;
      window.localStorage.setItem(key, value);
      written = true;
      if (fail && failure === 'after-write') throw new Error('Write acknowledgement lost');
      if (fail && failure === 'overwritten')
        window.localStorage.setItem(key, JSON.stringify({ ...savedRecord(), version: 2 }));
    });
    const h = setup({ storage: { getItem, setItem } });
    await h.store.load();
    expect(h.store.insertStep(tap('previously-saved'))).toBe(true);
    const before = h.store.getSnapshot();
    const expected = draftOf(h.store);
    fail = true;
    written = false;
    expect(h.store.editDraft({ ...expected, steps: [tap('not-confirmed')] }, expected)).toBe(false);
    expect(h.store.getSnapshot()).toMatchObject({
      status: 'ready',
      canInsert: false,
      persistence: 'error',
      savedAt: before.savedAt,
    });
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot().original).toBe(before.original);
    expect(h.store.getSnapshot().error).toMatch(/草稿保存失败或未确认/);
    const writes = setItem.mock.calls.length;
    expect(h.store.editDraft(expected, expected)).toBe(false);
    expect(h.store.insertStep(tap('blocked'))).toBe(false);
    expect(setItem).toHaveBeenCalledTimes(writes);
    fail = false;
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', canInsert: true });
  },
);

test.each([-1, 1.5, Number.MAX_SAFE_INTEGER + 1, Number.NaN])(
  'refuses an invalid edit save time %s without a write or memory promotion',
  async (time) => {
    const setItem = vi.fn();
    const h = setup({ storage: { getItem: () => null, setItem }, now: () => time });
    await h.store.load();
    const expected = draftOf(h.store);
    expect(h.store.editDraft({ ...expected, steps: [tap()] }, expected)).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot()).toMatchObject({
      canInsert: false,
      persistence: 'error',
      savedAt: null,
    });
    expect(h.store.getSnapshot().error).toMatch(/草稿保存时间无效/);
    expect(setItem).not.toHaveBeenCalled();
  },
);

test('protects a persisted v2 edit against changed Runtime originals even with the same scope', async () => {
  const h = setup();
  await h.store.load();
  const expected = draftOf(h.store);
  expect(h.store.editDraft({ ...expected, steps: [tap('edited')] }, expected)).toBe(true);
  const edited = draftOf(h.store);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  h.getCase.mockResolvedValue({ ...sourceCase(), name: 'Changed Runtime source' });
  const project = h.projects.getSnapshot().project;
  if (!project) throw new Error('test project missing');
  h.projects.set({ project: { ...project, revision: '2' } });
  expect(h.store.editDraft(edited, edited)).toBe(false);
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'error',
    draft: null,
    canInsert: false,
    original: { name: 'Changed Runtime source' },
  });
  expect(h.store.getSnapshot().reason).toMatch(/原始用例已变化/);
  expect(h.store.editDraft(edited, edited)).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

test('does not allow an edited stored base to replace the Runtime original', async () => {
  const record = savedRecord();
  const foreign = { ...record.original, name: 'Tampered base' };
  const saved = JSON.stringify({
    ...record,
    version: 2,
    original: foreign,
    draft: { ...foreign, steps: [tap('edited')] },
  });
  window.localStorage.setItem(caseDraftStorageKey(scope), saved);
  const h = setup();
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'error',
    draft: null,
    canInsert: false,
    original: sourceCase(),
  });
  expect(h.store.getSnapshot().reason).toMatch(/原始用例已变化/);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

test('reads the golden Flow as a detached frozen document without saving or changing the Case snapshot', async () => {
  const getItem = vi.fn((key: string) => window.localStorage.getItem(key));
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  const h = setup({ storage: { getItem, setItem } });
  await h.store.load();
  const raw = structuredClone(fillSearchFlow);
  h.getFlow.mockResolvedValue(raw);
  const before = h.store.getSnapshot();
  const getCaseCalls = h.getCase.mock.calls.length;
  const projectCalls = h.getProject.mock.calls.length;
  const verificationCalls = h.verifyConnection.mock.calls.length;
  getItem.mockClear();
  setItem.mockClear();
  const flow = await h.store.readFlow('fill-search', new AbortController().signal);
  expect(h.getFlow).toHaveBeenCalledExactlyOnceWith('demo', 'fill-search');
  expect(flow).toEqual(fillSearchFlow);
  expect(flow).not.toBe(raw);
  expect(Object.isFrozen(flow)).toBe(true);
  expect(Object.isFrozen(flow.steps)).toBe(true);
  expect(Object.isFrozen(flow.steps[0])).toBe(true);
  expect(Object.isFrozen(flow.parameters.query)).toBe(true);
  expect(Object.isFrozen(flow.returns.label?.value)).toBe(true);
  if (!raw.steps[0]) throw new Error('test Flow step missing');
  raw.steps[0].id = 'mutated-response';
  raw.parameters.query.required = false;
  expect(flow).toEqual(fillSearchFlow);
  expect(h.store.getSnapshot()).toBe(before);
  expect(h.getCase).toHaveBeenCalledTimes(getCaseCalls);
  expect(h.getProject).toHaveBeenCalledTimes(projectCalls + 2);
  expect(h.verifyConnection).toHaveBeenCalledTimes(verificationCalls + 2);
  expect(getItem).not.toHaveBeenCalled();
  expect(setItem).not.toHaveBeenCalled();
});

test('the default client uses the existing read-only getFlow endpoint with a cancellable fetch signal', async () => {
  const h = setupDefaultClient();
  await h.store.load();
  const before = h.store.getSnapshot();
  h.fetchMock.mockClear();
  const flow = await h.store.readFlow('fill-search', new AbortController().signal);
  expect(flow).toEqual(fillSearchFlow);
  expect(h.fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname)).toEqual([
    '/projects/demo',
    '/projects/demo/flows/fill-search',
    '/projects/demo',
  ]);
  for (const [, init] of h.fetchMock.mock.calls) {
    expect(init?.method).toBe('GET');
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    expect(init?.signal?.aborted).toBe(true);
  }
  expect(h.store.getSnapshot()).toBe(before);
  expect(window.localStorage.length).toBe(0);
});

test.each([
  ['pid', { pid: 4321 }],
  ['port', { port: 43126 }],
  ['token', { token: 'new-token' }],
  ['version', { version: 'new-version' }],
  ['missing connection', null],
] as const)(
  'the default Flow reader rejects changed Runtime %s before and after the read',
  async (_name, fields) => {
    const h = setupDefaultClient();
    await h.store.load();
    const before = h.store.getSnapshot();
    const changed = fields === null ? null : { ...h.connection, ...fields };
    for (const phase of ['before', 'after'] as const) {
      h.fetchMock.mockClear();
      h.getConnection
        .mockResolvedValueOnce(h.connection)
        .mockResolvedValueOnce(phase === 'before' ? changed : h.connection);
      if (phase === 'after') h.getConnection.mockResolvedValueOnce(changed);
      await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
        /Runtime 已更换或连接未确认/,
      );
      const paths = h.fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname);
      expect(paths.includes('/projects/demo/flows/fill-search')).toBe(phase === 'after');
      expect(h.store.getSnapshot()).toBe(before);
      expect(window.localStorage.length).toBe(0);
    }
  },
);

test('a legacy client explicitly reports Flow reading unavailable instead of inventing a Flow', async () => {
  const h = setup();
  await h.store.load();
  h.getClient.mockResolvedValue({
    getProject: h.getProject,
    getCase: h.getCase,
    verifyConnection: h.verifyConnection,
  });
  const before = h.store.getSnapshot();
  await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
    /不支持流程读取.*不可用/,
  );
  expect(h.getFlow).not.toHaveBeenCalled();
  expect(h.store.getSnapshot()).toBe(before);
});

test('Flow preview remains read-only and available when local persistence prevents Case editing', async () => {
  const h = setup({ storage: null });
  await h.store.load();
  const expected = draftOf(h.store);
  expect(h.store.editDraft({ ...expected, steps: [tap()] }, expected)).toBe(false);
  const before = h.store.getSnapshot();
  expect(before.canInsert).toBe(false);
  expect(await h.store.readFlow('fill-search', new AbortController().signal)).toEqual(
    fillSearchFlow,
  );
  expect(h.store.getSnapshot()).toBe(before);
});

test('refuses Flow reads while loading, unselected, or disposed without adding Runtime reads', async () => {
  const h = setup();
  await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
    /正在读取/,
  );
  await h.store.load();
  h.projects.set({ selectedCaseId: null });
  const before = h.store.getSnapshot();
  const discoveryCalls = h.getClient.mock.calls.length;
  await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
    /选择用例/,
  );
  expect(h.store.getSnapshot()).toBe(before);
  h.store.dispose();
  await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
    /已关闭/,
  );
  expect(h.getClient).toHaveBeenCalledTimes(discoveryCalls);
  expect(h.getFlow).not.toHaveBeenCalled();
});

test('rejects nonmember Flow IDs locally before discovery', async () => {
  const h = setup();
  await h.store.load();
  const before = h.store.getSnapshot();
  const discoveryCalls = h.getClient.mock.calls.length;
  for (const flowId of ['missing', '', '../fill-search']) {
    await expect(h.store.readFlow(flowId, new AbortController().signal)).rejects.toThrow(
      /流程不属于当前工程/,
    );
  }
  expect(h.store.getSnapshot()).toBe(before);
  expect(h.getClient).toHaveBeenCalledTimes(discoveryCalls);
  expect(h.getFlow).not.toHaveBeenCalled();
});

const invalidFlowProjects = [
  ['root', { ...readyState().project, root: '/tmp/foreign' }],
  ['project ID', { ...readyState().project, project_id: 'foreign' }],
  ['case membership', { ...readyState().project, case_ids: ['other'] }],
  ['flow membership', { ...readyState().project, flow_ids: [] }],
  ['missing flow membership', { root: '/tmp/demo', project_id: 'demo', case_ids: ['starter'] }],
  ['non-object', null],
] as const;

test.each(invalidFlowProjects)(
  'rejects wrong Runtime %s before and after a Flow read without affecting Case state',
  async (_name, remote) => {
    const h = setup();
    await h.store.load();
    const before = h.store.getSnapshot();
    h.getProject.mockResolvedValueOnce(remote);
    await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
      /工程来源|流程不属于/,
    );
    expect(h.getFlow).not.toHaveBeenCalled();
    h.getProject
      .mockResolvedValueOnce(h.projects.getSnapshot().project)
      .mockResolvedValueOnce(remote);
    await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
      /工程来源|流程不属于/,
    );
    expect(h.getFlow).toHaveBeenCalledOnce();
    expect(h.store.getSnapshot()).toBe(before);
    expect(window.localStorage.length).toBe(0);
  },
);

test('rechecks local flow membership after an in-flight read even without a project notification', async () => {
  const h = setup();
  await h.store.load();
  const gate = deferred<unknown>();
  h.getFlow.mockReturnValueOnce(gate.promise);
  const before = h.store.getSnapshot();
  const reading = h.store.readFlow('fill-search', new AbortController().signal);
  const rejected = expect(reading).rejects.toThrow(/流程不属于当前工程/);
  await h.getFlowEntered();
  const project = h.projects.getSnapshot().project;
  if (!project) throw new Error('test project missing');
  project.flow_ids = [];
  gate.resolve(fillSearchFlow);
  await rejected;
  expect(h.store.getSnapshot()).toBe(before);
});

const invalidFlows: Array<[string, unknown, RegExp]> = [
  ['wrong ID', { ...fillSearchFlow, flow_id: 'other-flow' }, /流程 ID/],
  ['wrong schema', { ...fillSearchFlow, $schema: 'urn:pulse:schema:flow' }, /流程结构无效/],
  ['blank name', { ...fillSearchFlow, name: ' ' }, /流程结构无效/],
  ['missing fields', { flow_id: 'fill-search', steps: [tap()] }, /流程结构无效/],
  ['empty steps', { ...fillSearchFlow, steps: [] }, /流程结构无效/],
  [
    'bad return',
    {
      ...fillSearchFlow,
      returns: { label: { type: 'string', value: { var_ref: 'label', extra: true } } },
    },
    /流程结构无效/,
  ],
  [
    'bad locator',
    {
      ...fillSearchFlow,
      locators: { search: { candidates: [], expect: { unique: true, visible: true } } },
    },
    /流程结构无效/,
  ],
  [
    'missing observation source',
    {
      ...fillSearchFlow,
      steps: [
        {
          type: 'wait',
          id: 'wait',
          condition: { operator: 'exists', target: { locator_ref: 'search' } },
        },
      ],
    },
    /流程结构无效/,
  ],
  [
    'duplicate nested steps',
    {
      ...fillSearchFlow,
      steps: [
        {
          type: 'if',
          id: 'branch',
          condition: check().checkpoint.condition,
          then: [tap('same')],
          else: [{ type: 'loop', id: 'loop', max_iterations: 1, steps: [tap('same')] }],
        },
      ],
    },
    /重复步骤 ID/,
  ],
  [
    'duplicate step across blocks',
    {
      ...fillSearchFlow,
      steps: [
        ...fillSearchFlow.steps,
        { type: 'loop', id: 'loop', max_iterations: 1, steps: [tap('type-query')] },
      ],
    },
    /重复步骤 ID/,
  ],
  [
    'duplicate AI checkpoint',
    {
      ...fillSearchFlow,
      steps: [...fillSearchFlow.steps, ...aiCase.steps, check('check-summary', 'summary')],
    },
    /重复检查点 ID/,
  ],
  [
    'duplicate nested checkpoints',
    {
      ...fillSearchFlow,
      steps: [
        {
          type: 'if',
          id: 'branch',
          condition: check().checkpoint.condition,
          then: [check('then-check', 'same-check')],
          else: [
            {
              type: 'loop',
              id: 'loop',
              max_iterations: 1,
              steps: [check('else-check', 'same-check')],
            },
          ],
        },
      ],
    },
    /重复检查点 ID/,
  ],
];

test.each(invalidFlows)(
  'rejects invalid Flow %s rather than exposing an unchecked Runtime payload',
  async (_name, raw, reason) => {
    const h = setup();
    await h.store.load();
    const before = h.store.getSnapshot();
    h.getFlow.mockResolvedValue(raw);
    await expect(h.store.readFlow('fill-search', new AbortController().signal)).rejects.toThrow(
      reason,
    );
    expect(h.store.getSnapshot()).toBe(before);
    expect(window.localStorage.length).toBe(0);
  },
);

test('revalidates copied Case and Flow JSON so custom serialization cannot bypass ID validation', () => {
  const rawCase = structuredClone(sourceCase());
  Object.defineProperty(rawCase, 'toJSON', {
    value: () => ({ ...sourceCase(), steps: [tap('same'), tap('same')] }),
  });
  expect(() => copyDraftCase(rawCase)).toThrow(/重复步骤 ID/);
  const rawFlow = structuredClone(fillSearchFlow);
  Object.defineProperty(rawFlow, 'toJSON', {
    value: () => ({ ...fillSearchFlow, steps: [tap('same'), tap('same')] }),
  });
  expect(() => copyDraftFlow(rawFlow)).toThrow(/重复步骤 ID/);
});

test('custom serialization cannot hide forbidden original field edits', async () => {
  const h = setup();
  await h.store.load();
  const expected = draftOf(h.store);
  const next = { ...expected, name: 'Forbidden edit' };
  Object.defineProperty(next, 'toJSON', {
    value: () => ({ ...expected, steps: [tap('serialized-edit')] }),
  });
  expect(h.store.editDraft(next, expected)).toBe(false);
  expect(h.store.getSnapshot().error).toMatch(/不能修改原始其他字段或 ID/);
  expect(draftOf(h.store)).toBe(expected);
  expect(window.localStorage.length).toBe(0);
});

test('an already aborted Flow signal rejects before client discovery', async () => {
  const h = setup();
  await h.store.load();
  const before = h.store.getSnapshot();
  const discoveryCalls = h.getClient.mock.calls.length;
  const controller = new AbortController();
  controller.abort(new Error('cancelled by editor'));
  await expect(h.store.readFlow('fill-search', controller.signal)).rejects.toThrow(
    /cancelled by editor/,
  );
  expect(h.getClient).toHaveBeenCalledTimes(discoveryCalls);
  expect(h.getFlow).not.toHaveBeenCalled();
  expect(h.store.getSnapshot()).toBe(before);
});

test.each(flowReadPhases)(
  'aborts an in-flight Flow %s phase and ignores its late continuation',
  async (phase) => {
    const h = setup();
    await h.store.load();
    const before = h.store.getSnapshot();
    const held = holdFlowRead(h, phase);
    const controller = new AbortController();
    const reading = h.store.readFlow('fill-search', controller.signal);
    const rejected = expect(reading).rejects.toThrow(/aborted/);
    await held.entered;
    const pending = held.pending();
    const requestSignal = h.getClient.mock.calls.at(-1)?.[0];
    controller.abort();
    await rejected;
    expect(requestSignal?.aborted).toBe(true);
    const calls = [
      h.getProject.mock.calls.length,
      h.getFlow.mock.calls.length,
      h.verifyConnection.mock.calls.length,
    ];
    held.release();
    await pending;
    expect([
      h.getProject.mock.calls.length,
      h.getFlow.mock.calls.length,
      h.verifyConnection.mock.calls.length,
    ]).toEqual(calls);
    expect(h.store.getSnapshot()).toBe(before);
  },
);

test.each(flowReadPhases)(
  'bounds the entire Flow %s phase to five seconds, cancels it, and supports retry',
  async (phase) => {
    vi.useFakeTimers();
    const h = setup();
    await h.store.load();
    const before = h.store.getSnapshot();
    const held = holdFlowRead(h, phase);
    const reading = h.store.readFlow('fill-search', new AbortController().signal);
    const rejected = expect(reading).rejects.toThrow(/流程读取超过 5 秒/);
    await held.entered;
    const pending = held.pending();
    const requestSignal = h.getClient.mock.calls.at(-1)?.[0];
    await vi.advanceTimersByTimeAsync(5001);
    await rejected;
    expect(requestSignal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    held.release();
    await pending;
    expect(h.store.getSnapshot()).toBe(before);
    expect(await h.store.readFlow('fill-search', new AbortController().signal)).toEqual(
      fillSearchFlow,
    );
    expect(h.store.getSnapshot()).toBe(before);
  },
);

test.each(['case', 'root', 'project', 'reload', 'refresh', 'dispose'] as const)(
  'cancels pending Flow reads on %s changes and rejects late old-source results',
  async (change) => {
    const h = setup();
    h.getCase.mockImplementation(async (_projectId, caseId) => sourceCase(caseId));
    await h.store.load();
    const gate = deferred<unknown>();
    h.getFlow.mockReturnValueOnce(gate.promise);
    const reading = h.store.readFlow('fill-search', new AbortController().signal);
    const rejected = expect(reading).rejects.toThrow(/aborted/);
    await h.getFlowEntered();
    const requestSignal = h.getClient.mock.calls.at(-1)?.[0];
    if (change === 'dispose') h.store.dispose();
    else if (change === 'reload') await h.store.load();
    else if (change === 'refresh') {
      const project = h.projects.getSnapshot().project;
      if (!project) throw new Error('test project missing');
      h.projects.set({ project: { ...project, revision: '2' } });
    } else {
      h.projects.set(
        readyState({
          ...scope,
          ...(change === 'case'
            ? { caseId: 'other' }
            : change === 'root'
              ? { root: '/tmp/new-root' }
              : { projectId: 'new-project' }),
        }),
      );
    }
    await rejected;
    expect(requestSignal?.aborted).toBe(true);
    if (change !== 'dispose') {
      await h.store.load();
      expect(h.store.insertStep(tap('current-case'))).toBe(true);
    }
    const beforeLate = h.store.getSnapshot();
    gate.resolve(fillSearchFlow);
    await gate.promise;
    expect(h.store.getSnapshot()).toBe(beforeLate);
  },
);

test('concurrent Flow previews have independent cancellation and do not change a subsequently edited Case', async () => {
  const h = setup();
  await h.store.load();
  const a = deferred<unknown>();
  const b = deferred<unknown>();
  h.getFlow.mockImplementation(async (_projectId, flowId) =>
    flowId === 'fill-search' ? a.promise : b.promise,
  );
  const controllerA = new AbortController();
  const readingA = h.store.readFlow('fill-search', controllerA.signal);
  const rejectedA = expect(readingA).rejects.toThrow(/aborted/);
  const readingB = h.store.readFlow('other-flow', new AbortController().signal);
  await Promise.all([h.getFlowEntered('fill-search'), h.getFlowEntered('other-flow')]);
  controllerA.abort();
  await rejectedA;
  const expected = draftOf(h.store);
  expect(h.store.editDraft({ ...expected, steps: [tap('edited-during-preview')] }, expected)).toBe(
    true,
  );
  const before = h.store.getSnapshot();
  b.resolve({ ...fillSearchFlow, flow_id: 'other-flow' });
  expect(await readingB).toEqual({ ...fillSearchFlow, flow_id: 'other-flow' });
  a.reject(new Error('late old Flow failure'));
  await a.promise.catch(() => undefined);
  expect(h.store.getSnapshot()).toBe(before);
});

test.each([controlFlowCase, operatorsCase, aiCase])(
  'history repeatedly undoes and redoes mixed APIs for golden Case $case_id without changing its source',
  async (fixture) => {
    const raw = structuredClone({ ...fixture, case_id: 'starter' });
    const source = structuredClone(raw);
    let time = 1000;
    const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
    const h = setup({
      raw,
      storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
      now: () => time++,
    });
    await h.store.load();
    const original = draftOf(h.store);
    const revision = h.store.getSnapshot().historyRevision;
    const drafts = [original];
    const empty = h.store.getSnapshot();
    expect(empty).toMatchObject({ canUndo: false, canRedo: false });
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot()).toBe(empty);
    expect(setItem).not.toHaveBeenCalled();

    expect(h.store.insertStep(tap('history-tap'))).toBe(true);
    drafts.push(draftOf(h.store));
    expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
      version: 1,
      scope,
      original: source,
      draft: { ...source, steps: [...source.steps, tap('history-tap')] },
      savedAt: 1000,
    });
    const reordered = structuredClone(draftOf(h.store));
    reordered.steps.reverse();
    reordered.steps[0].timeout_ms = 4321;
    expect(h.store.editDraft(reordered, draftOf(h.store))).toBe(true);
    const reorderedDraft = draftOf(h.store);
    drafts.push(reorderedDraft);
    expect(draftOf(h.store)).toEqual(reordered);
    reordered.steps[0].id = 'mutated-history-input';
    expect(h.store.insertStep(check('history-check', 'history-exists', 'vision'))).toBe(true);
    drafts.push(draftOf(h.store));
    expect(draftOf(h.store).steps).toEqual([
      ...reorderedDraft.steps,
      check('history-check', 'history-exists', 'vision'),
    ]);
    const removed = structuredClone(draftOf(h.store));
    removed.steps.splice(
      removed.steps.findIndex((step) => step.id === 'history-tap'),
      1,
    );
    expect(h.store.editDraft(removed, draftOf(h.store))).toBe(true);
    drafts.push(draftOf(h.store));
    expect(draftOf(h.store)).toEqual(removed);
    const finalDraft = structuredClone(draftOf(h.store));
    removed.steps[0].id = 'mutated-removed-input';

    let restorations = 0;
    for (let round = 0; round < 2; round++) {
      for (let index = drafts.length - 2; index >= 0; index--) {
        expect(h.store.undo()).toBe(true);
        restorations++;
        const state = h.store.getSnapshot();
        expect(state).toMatchObject({
          draft: drafts[index],
          canUndo: index > 0,
          canRedo: true,
          historyRevision: revision + restorations,
          persistence: 'saved',
          canInsert: true,
          error: null,
        });
        expect(state.original).toBe(original);
        expect(state.original).toEqual(source);
        expect(raw).toEqual(source);
        expect(fixture).toEqual({ ...source, case_id: fixture.case_id });
        expect(Object.isFrozen(state.draft)).toBe(true);
        expect(Object.isFrozen(state.draft?.steps[0])).toBe(true);
        expect(
          JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null'),
        ).toEqual({
          version: 2,
          scope,
          original: source,
          draft: drafts[index],
          savedAt: state.savedAt,
        });
      }
      const undone = h.store.getSnapshot();
      const undoWrites = setItem.mock.calls.length;
      expect(h.store.undo()).toBe(false);
      expect(h.store.getSnapshot()).toBe(undone);
      expect(setItem).toHaveBeenCalledTimes(undoWrites);
      for (let index = 1; index < drafts.length; index++) {
        expect(h.store.redo()).toBe(true);
        restorations++;
        const state = h.store.getSnapshot();
        expect(state).toMatchObject({
          draft: drafts[index],
          canUndo: true,
          canRedo: index < drafts.length - 1,
          historyRevision: revision + restorations,
        });
        expect(state.original).toBe(original);
        expect(state.original).toEqual(source);
        expect(raw).toEqual(source);
        expect(
          JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null'),
        ).toEqual({
          version: 2,
          scope,
          original: source,
          draft: drafts[index],
          savedAt: state.savedAt,
        });
      }
      const redone = h.store.getSnapshot();
      const redoWrites = setItem.mock.calls.length;
      expect(h.store.redo()).toBe(false);
      expect(h.store.getSnapshot()).toBe(redone);
      expect(setItem).toHaveBeenCalledTimes(redoWrites);
      expect(draftOf(h.store)).toEqual(finalDraft);
    }
    expect(setItem).toHaveBeenCalledTimes(20);
  },
);

test('history restores locator and checkpoint edits together with nested if/loop additions, moves, and removals', async () => {
  const h = setup();
  await h.store.load();
  const original = draftOf(h.store);
  const drafts = [original];
  const edit = (mutate: (draft: Case) => void) => {
    const expected = draftOf(h.store);
    const next = structuredClone(expected);
    mutate(next);
    expect(h.store.editDraft(next, expected)).toBe(true);
    expect(draftOf(h.store)).toEqual(next);
    drafts.push(draftOf(h.store));
  };
  edit((draft) => {
    draft.locators.search = {
      candidates: [{ by: 'text', value: 'Edited search', scope: null }],
      expect: { unique: false, visible: true },
    };
    draft.locators.history_target = {
      candidates: [{ by: 'desc', value: 'Nested control' }],
      expect: { unique: true, visible: true },
    };
  });
  edit((draft) => {
    draft.preconditions = [check('unused-pre', 'history-pre', 'webview').checkpoint];
    draft.checks.reverse();
    const checkpoint = draft.checks[0];
    if (!checkpoint) throw new Error('test checkpoint missing');
    checkpoint.required = false;
    checkpoint.condition.source = 'ocr';
    draft.checks.push(check('unused-post', 'history-post', 'vision').checkpoint);
  });
  edit((draft) => {
    const branch = draft.steps.find((step) => step.type === 'if');
    const loop = draft.steps.find((step) => step.type === 'loop');
    if (branch?.type !== 'if' || loop?.type !== 'loop') throw new Error('test controls missing');
    branch.condition = {
      operator: 'exists',
      source: 'webview',
      target: { locator_ref: 'history_target' },
    };
    branch.then.unshift({
      type: 'loop',
      id: 'history-inner-loop',
      max_iterations: 3,
      condition: check().checkpoint.condition,
      steps: [
        check('history-inner-check', 'history-inner-exists', 'ocr'),
        tap('history-inner-tap'),
      ],
    });
    branch.else = [
      ...(branch.else ?? []),
      check('history-else-check', 'history-else-exists', 'vision'),
    ];
    loop.max_iterations = 4;
    loop.steps.reverse();
    loop.steps.push({
      type: 'if',
      id: 'history-loop-if',
      condition: check().checkpoint.condition,
      then: [tap('history-loop-then')],
      else: [check('history-loop-else', 'history-loop-exists', 'webview')],
    });
  });
  edit((draft) => {
    const branch = draft.steps.find((step) => step.type === 'if');
    const loop = draft.steps.find((step) => step.type === 'loop');
    if (branch?.type !== 'if' || loop?.type !== 'loop') throw new Error('test controls missing');
    const inner = branch.then[0];
    const nestedBranch = loop.steps.at(-1);
    if (inner?.type !== 'loop' || nestedBranch?.type !== 'if') {
      throw new Error('test nested controls missing');
    }
    inner.max_iterations = 1;
    delete inner.condition;
    inner.steps.reverse();
    const nestedCheck = inner.steps.find((step) => step.type === 'check');
    if (nestedCheck?.type !== 'check') throw new Error('test nested check missing');
    nestedCheck.checkpoint.required = false;
    nestedCheck.checkpoint.condition.source = 'vision';
    branch.then = [inner];
    branch.else = [];
    loop.steps.splice(0, 1);
    delete nestedBranch.else;
    delete loop.condition;
    draft.steps.reverse();
    delete draft.locators.history_target;
    draft.preconditions = [];
    draft.checks.splice(0, 1);
  });
  const nested: Step = {
    type: 'loop',
    id: 'history-appended-loop',
    max_iterations: 2,
    steps: [
      {
        type: 'if',
        id: 'history-appended-if',
        condition: check().checkpoint.condition,
        then: [check('history-appended-check', 'history-appended-exists', 'native')],
      },
    ],
  };
  const beforeInsertion = draftOf(h.store);
  expect(h.store.insertStep(nested)).toBe(true);
  expect(draftOf(h.store)).toEqual({
    ...beforeInsertion,
    steps: [...beforeInsertion.steps, nested],
  });
  drafts.push(draftOf(h.store));
  nested.steps[0].id = 'mutated-nested-input';
  for (let index = drafts.length - 2; index >= 0; index--) {
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(drafts[index]);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: index > 0, canRedo: true });
    expect(h.store.getSnapshot().original).toBe(original);
    expect(Object.isFrozen(draftOf(h.store).locators)).toBe(true);
    expect(Object.isFrozen(draftOf(h.store).preconditions)).toBe(true);
    expect(Object.isFrozen(draftOf(h.store).checks)).toBe(true);
  }
  expect(h.store.undo()).toBe(false);
  for (let index = 1; index < drafts.length; index++) {
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(drafts[index]);
    expect(h.store.getSnapshot()).toMatchObject({
      canUndo: true,
      canRedo: index < drafts.length - 1,
      original,
    });
    expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
      version: 2,
      scope,
      original,
      draft: drafts[index],
      savedAt: 1000,
    });
  }
  expect(h.store.redo()).toBe(false);
  expect(h.store.getSnapshot().original).toEqual(sourceCase());
});

test('history coalesces consecutive edits with the same group, including an intervening no-op', async () => {
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  const h = setup({ storage: { getItem: (key) => window.localStorage.getItem(key), setItem } });
  await h.store.load();
  const original = draftOf(h.store);
  const group = {};
  const first = structuredClone(original);
  first.steps[0].timeout_ms = 1001;
  expect(h.store.editDraft(first, original, group)).toBe(true);
  const beforeNoOp = h.store.getSnapshot();
  expect(h.store.editDraft(structuredClone(draftOf(h.store)), draftOf(h.store), group)).toBe(true);
  expect(h.store.getSnapshot()).toBe(beforeNoOp);
  expect(setItem).toHaveBeenCalledTimes(1);
  const second = structuredClone(draftOf(h.store));
  const search = second.locators.search;
  if (!search) throw new Error('test locator missing');
  search.candidates[0].value = 'Grouped search';
  expect(h.store.editDraft(second, draftOf(h.store), group)).toBe(true);
  const third = structuredClone(draftOf(h.store));
  const precondition = third.preconditions[0];
  const checkpoint = third.checks[0];
  if (!precondition || !checkpoint) throw new Error('test checkpoints missing');
  precondition.required = false;
  checkpoint.condition.source = 'ocr';
  expect(h.store.editDraft(third, draftOf(h.store), group)).toBe(true);
  const grouped = structuredClone(third);
  search.candidates[0].value = 'Mutated grouped input';
  precondition.id = 'mutated-grouped-checkpoint';
  expect(h.store.getSnapshot()).toMatchObject({ draft: grouped, canUndo: true, canRedo: false });
  expect(setItem).toHaveBeenCalledTimes(3);
  expect(h.store.undo()).toBe(true);
  expect(h.store.getSnapshot()).toMatchObject({ draft: original, canUndo: false, canRedo: true });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(true);
  expect(h.store.getSnapshot()).toMatchObject({ draft: grouped, canUndo: true, canRedo: false });
  expect(h.store.redo()).toBe(false);
  expect(h.store.getSnapshot().original).toBe(original);
  expect(h.store.undo()).toBe(true);
  expect(draftOf(h.store)).toEqual(original);
});

test.each(['another-group', 'ungrouped', 'insert'] as const)(
  'history separates a reused group after an intervening %s operation',
  async (separator) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    const group = { field: 'timeout' };
    for (const timeout of [1001, 1002]) {
      const expected = draftOf(h.store);
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = timeout;
      expect(h.store.editDraft(next, expected, group)).toBe(true);
    }
    const firstGroup = draftOf(h.store);
    if (separator === 'insert') expect(h.store.insertStep(tap('history-separator'))).toBe(true);
    else {
      const expected = draftOf(h.store);
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = 2001;
      expect(
        h.store.editDraft(
          next,
          expected,
          separator === 'another-group' ? { field: 'timeout' } : undefined,
        ),
      ).toBe(true);
    }
    const separated = draftOf(h.store);
    for (const timeout of [3001, 3002]) {
      const expected = draftOf(h.store);
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = timeout;
      expect(h.store.editDraft(next, expected, group)).toBe(true);
    }
    const finalDraft = draftOf(h.store);
    for (const draft of [separated, firstGroup, original]) {
      expect(h.store.undo()).toBe(true);
      expect(draftOf(h.store)).toEqual(draft);
    }
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
    expect(h.store.undo()).toBe(false);
    for (const draft of [firstGroup, separated, finalDraft]) {
      expect(h.store.redo()).toBe(true);
      expect(draftOf(h.store)).toEqual(draft);
    }
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: false });
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot().original).toBe(original);
  },
);

test.each([false, true])(
  'history removes a group that returns to its starting draft while preserving earlier history (%s)',
  async (hasEarlierHistory) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    if (hasEarlierHistory) expect(h.store.insertStep(tap('history-older'))).toBe(true);
    const starting = draftOf(h.store);
    expect(h.store.insertStep(tap('history-abandoned-redo'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    expect(h.store.getSnapshot().canRedo).toBe(true);
    const group = {};
    for (const timeout of [1001, 1002]) {
      const expected = draftOf(h.store);
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = timeout;
      expect(h.store.editDraft(next, expected, group)).toBe(true);
      expect(h.store.getSnapshot().canRedo).toBe(false);
    }
    expect(h.store.editDraft(structuredClone(starting), draftOf(h.store), group)).toBe(true);
    expect(h.store.getSnapshot()).toMatchObject({
      draft: starting,
      canUndo: hasEarlierHistory,
      canRedo: false,
      persistence: 'saved',
    });
    const returned = h.store.getSnapshot();
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot()).toBe(returned);
    expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
      version: 2,
      scope,
      original,
      draft: starting,
      savedAt: 1000,
    });
    if (hasEarlierHistory) {
      expect(h.store.undo()).toBe(true);
      expect(draftOf(h.store)).toEqual(original);
      expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
      expect(h.store.redo()).toBe(true);
      expect(draftOf(h.store)).toEqual(starting);
    } else expect(h.store.undo()).toBe(false);
    expect(h.store.getSnapshot().original).toBe(original);
  },
);

test.each(['undo', 'redo'] as const)(
  'history starts a new group segment when the same token is reused after %s',
  async (operation) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    const group = {};
    const first = structuredClone(original);
    first.steps[0].timeout_ms = 1001;
    expect(h.store.editDraft(first, original, group)).toBe(true);
    const firstGroup = draftOf(h.store);
    if (operation === 'undo') {
      const separator = structuredClone(firstGroup);
      separator.steps[0].timeout_ms = 2001;
      expect(h.store.editDraft(separator, firstGroup)).toBe(true);
      expect(h.store.undo()).toBe(true);
    } else {
      expect(h.store.undo()).toBe(true);
      expect(h.store.redo()).toBe(true);
    }
    expect(draftOf(h.store)).toEqual(firstGroup);
    const next = structuredClone(draftOf(h.store));
    next.steps[0].timeout_ms = 3001;
    expect(h.store.editDraft(next, draftOf(h.store), group)).toBe(true);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: false });
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(firstGroup);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: true });
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(original);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(firstGroup);
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(next);
  },
);

test('history and redo survive no-ops, stale expected values, and invalid edits without phantom entries', async () => {
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  const now = vi.fn(() => 1000);
  const h = setup({
    storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
    now,
  });
  await h.store.load();
  const original = draftOf(h.store);
  expect(h.store.insertStep(tap('history-first'))).toBe(true);
  const first = draftOf(h.store);
  const edited = structuredClone(first);
  edited.steps[0].timeout_ms = 2001;
  expect(h.store.editDraft(edited, first)).toBe(true);
  const second = draftOf(h.store);
  expect(h.store.insertStep(tap('history-redo'))).toBe(true);
  const third = draftOf(h.store);
  expect(h.store.undo()).toBe(true);
  const expected = draftOf(h.store);
  const before = h.store.getSnapshot();
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  setItem.mockClear();
  now.mockClear();
  expect(h.store.editDraft(expected, expected)).toBe(true);
  expect(h.store.editDraft(structuredClone(expected), expected, {})).toBe(true);
  expect(h.store.getSnapshot()).toBe(before);
  const invalid: Array<[unknown, Case]> = [
    [{ ...expected, steps: [...expected.steps, tap('stale-copy')] }, structuredClone(expected)],
    [{ ...expected, steps: [...expected.steps, tap('stale-before-undo')] }, third],
    [{ ...expected, steps: [...expected.steps, tap('stale-earlier')] }, first],
    [{ ...expected, name: 'Forbidden history edit' }, expected],
    [{ ...expected, steps: [] }, expected],
    [{ ...expected, steps: [...expected.steps, tap('tap-banner')] }, expected],
    [
      { ...expected, checks: [...expected.checks, check('unused', 'page-ready').checkpoint] },
      expected,
    ],
    [
      {
        ...expected,
        steps: [{ type: 'loop', id: 'empty-history-loop', max_iterations: 1, steps: [] }],
      },
      expected,
    ],
    [
      {
        ...expected,
        locators: { bad: { candidates: [], expect: { unique: true, visible: true } } },
      },
      expected,
    ],
  ];
  for (const [next, version] of invalid) {
    expect(h.store.editDraft(next as Case, version, {})).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot()).toMatchObject({
      canUndo: true,
      canRedo: true,
      canInsert: true,
      persistence: 'saved',
      savedAt: before.savedAt,
      historyRevision: before.historyRevision,
    });
    expect(h.store.getSnapshot().original).toBe(original);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    expect(setItem).not.toHaveBeenCalled();
    expect(now).not.toHaveBeenCalled();
  }
  expect(h.store.insertStep(tap('tap-banner'))).toBe(false);
  expect(draftOf(h.store)).toBe(expected);
  expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: true });
  expect(setItem).not.toHaveBeenCalled();
  expect(now).not.toHaveBeenCalled();
  expect(h.store.redo()).toBe(true);
  expect(draftOf(h.store)).toEqual(third);
  for (const draft of [second, first, original]) {
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(draft);
  }
  expect(h.store.undo()).toBe(false);
  for (const draft of [first, second, third]) {
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(draft);
  }
  expect(h.store.redo()).toBe(false);
  expect(h.store.getSnapshot().original).toBe(original);
});

test.each(['edit', 'insert'] as const)(
  'history clears only the redo branch when a new effective %s is saved',
  async (operation) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    expect(h.store.insertStep(tap('history-branch-base'))).toBe(true);
    const first = draftOf(h.store);
    expect(h.store.insertStep(tap('history-discarded-branch'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    const expected = draftOf(h.store);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: true });
    if (operation === 'insert') expect(h.store.insertStep(tap('history-new-branch'))).toBe(true);
    else {
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = 3001;
      expect(h.store.editDraft(next, expected)).toBe(true);
    }
    const branch = h.store.getSnapshot();
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    expect(branch).toMatchObject({ canUndo: true, canRedo: false });
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot()).toBe(branch);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(first);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(original);
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(first);
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(branch.draft);
    expect(h.store.redo()).toBe(false);
  },
);

test.each(
  (['undo', 'redo'] as const).flatMap((operation) =>
    (
      [
        'before-read',
        'write',
        'ignored-write',
        'after-write',
        'confirmation-read',
        'overwritten',
      ] as const
    ).map((failure) => [operation, failure] as const),
  ),
)(
  'history %s never promotes a draft or advances its revision after %s persistence failure',
  async (operation, failure) => {
    let fail = false;
    let written = false;
    let time = 1000;
    const getItem = vi.fn((key: string) => {
      if (fail && (failure === 'before-read' || (failure === 'confirmation-read' && written))) {
        throw new Error('Storage read blocked');
      }
      return window.localStorage.getItem(key);
    });
    const setItem = vi.fn((key: string, value: string) => {
      if (fail && failure === 'write') throw new Error('QuotaExceededError');
      if (fail && failure === 'ignored-write') return;
      window.localStorage.setItem(key, value);
      written = true;
      if (fail && failure === 'after-write') throw new Error('Write acknowledgement lost');
      if (fail && failure === 'overwritten') {
        const record = JSON.parse(value);
        window.localStorage.setItem(
          key,
          JSON.stringify({ ...record, savedAt: record.savedAt + 1 }),
        );
      }
    });
    const h = setup({ storage: { getItem, setItem }, now: () => time++ });
    await h.store.load();
    expect(h.store.insertStep(tap('history-first'))).toBe(true);
    const edited = structuredClone(draftOf(h.store));
    edited.steps[0].timeout_ms = 2001;
    expect(h.store.editDraft(edited, draftOf(h.store))).toBe(true);
    expect(h.store.insertStep(tap('history-last'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    const before = h.store.getSnapshot();
    const expected = draftOf(h.store);
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    expect(before).toMatchObject({ canUndo: true, canRedo: true });
    setItem.mockClear();
    fail = true;
    written = false;
    expect(h.store[operation]()).toBe(false);
    expect(h.store.getSnapshot()).toMatchObject({
      status: 'ready',
      canInsert: false,
      canUndo: false,
      canRedo: false,
      persistence: 'error',
      savedAt: before.savedAt,
      historyRevision: before.historyRevision,
    });
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot().original).toBe(before.original);
    expect(h.store.getSnapshot().error).toMatch(/草稿保存失败或未确认/);
    expect(setItem).toHaveBeenCalledTimes(failure === 'before-read' ? 0 : 1);
    if (['before-read', 'write', 'ignored-write'].includes(failure)) {
      expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    }
    const writes = setItem.mock.calls.length;
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(h.store.editDraft(expected, expected, {})).toBe(false);
    expect(h.store.insertStep(tap('history-blocked'))).toBe(false);
    expect(draftOf(h.store)).toBe(expected);
    expect(h.store.getSnapshot().historyRevision).toBe(before.historyRevision);
    expect(setItem).toHaveBeenCalledTimes(writes);
    const storedDraft = JSON.parse(
      window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null',
    ).draft;
    fail = false;
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({
      status: 'ready',
      draft: storedDraft,
      canInsert: true,
      canUndo: false,
      canRedo: false,
    });
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(setItem).toHaveBeenCalledTimes(writes);
  },
);

test.each(['undo', 'redo'] as const)(
  'history %s detects another page writer without overwriting its draft or changing the current draft',
  async (operation) => {
    const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
    const now = vi.fn(() => 1000);
    const a = setup({
      storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
      now,
    });
    await a.store.load();
    expect(a.store.insertStep(tap('history-page-first'))).toBe(true);
    expect(a.store.insertStep(tap('history-page-second'))).toBe(true);
    expect(a.store.undo()).toBe(true);
    const before = a.store.getSnapshot();
    expect(before).toMatchObject({ canUndo: true, canRedo: true });
    const b = setup();
    await b.store.load();
    expect(b.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: false });
    const bExpected = draftOf(b.store);
    expect(
      b.store.editDraft(
        { ...bExpected, steps: [...bExpected.steps, tap('history-other-page')] },
        bExpected,
      ),
    ).toBe(true);
    const foreign = window.localStorage.getItem(caseDraftStorageKey(scope));
    const foreignDraft = draftOf(b.store);
    setItem.mockClear();
    now.mockClear();
    expect(a.store[operation]()).toBe(false);
    expect(a.store.getSnapshot()).toMatchObject({
      canInsert: false,
      canUndo: false,
      canRedo: false,
      persistence: 'error',
      savedAt: before.savedAt,
      historyRevision: before.historyRevision,
    });
    expect(a.store.getSnapshot().draft).toBe(before.draft);
    expect(a.store.getSnapshot().original).toBe(before.original);
    expect(a.store.getSnapshot().error).toMatch(/其他页面修改/);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(foreign);
    expect(a.store.undo()).toBe(false);
    expect(a.store.redo()).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
    expect(now).not.toHaveBeenCalled();
    expect(draftOf(b.store)).toBe(foreignDraft);
    expect(b.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: false });
    await a.store.load();
    expect(a.store.getSnapshot()).toMatchObject({
      draft: foreignDraft,
      canInsert: true,
      canUndo: false,
      canRedo: false,
    });
    expect(a.store.undo()).toBe(false);
    expect(a.store.redo()).toBe(false);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(foreign);
  },
);

test.each(['insert', 'edit', 'undo', 'redo'] as const)(
  'history publishes %s and its availability only after the write and confirming read complete',
  async (operation) => {
    const observed: Array<ReturnType<CaseDraftStore['getSnapshot']>> = [];
    let observe: () => void = () => undefined;
    let time = 1000;
    const h = setup({
      storage: {
        getItem: (key) => {
          observe();
          return window.localStorage.getItem(key);
        },
        setItem: (key, value) => {
          window.localStorage.setItem(key, value);
          observe();
        },
      },
      now: () => time++,
    });
    await h.store.load();
    expect(h.store.insertStep(tap('history-confirm-first'))).toBe(true);
    expect(h.store.insertStep(tap('history-confirm-second'))).toBe(true);
    expect(h.store.insertStep(tap('history-confirm-third'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    const before = h.store.getSnapshot();
    expect(before).toMatchObject({ canUndo: true, canRedo: true });
    observe = () => {
      observed.push(h.store.getSnapshot());
    };
    const published: Array<{ draft: Case | null; stored: Case }> = [];
    const listener = vi.fn(() => {
      published.push({
        draft: h.store.getSnapshot().draft,
        stored: JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null').draft,
      });
    });
    const unsubscribe = h.store.subscribe(listener);
    if (operation === 'insert') expect(h.store.insertStep(tap('history-confirm-new'))).toBe(true);
    else if (operation === 'edit') {
      const expected = draftOf(h.store);
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = 3001;
      expect(h.store.editDraft(next, expected)).toBe(true);
    } else expect(h.store[operation]()).toBe(true);
    expect(observed).toHaveLength(3);
    for (const state of observed) expect(state).toBe(before);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(published).toEqual([{ draft: draftOf(h.store), stored: draftOf(h.store) }]);
    expect(h.store.getSnapshot()).toMatchObject({
      canUndo: true,
      canRedo: operation === 'undo',
      savedAt: (before.savedAt ?? 0) + 1,
      historyRevision:
        before.historyRevision + (operation === 'undo' || operation === 'redo' ? 1 : 0),
    });
    expect(h.store.getSnapshot().original).toBe(before.original);
    unsubscribe();
  },
);

test.each(['case', 'root', 'projectId'] as const)(
  'history isolates both stacks and reused group tokens across %s scope switches',
  async (dimension) => {
    const h = setup();
    h.getCase.mockImplementation(async (_projectId, caseId) => sourceCase(caseId));
    await h.store.load();
    const group = {};
    const original = draftOf(h.store);
    expect(
      h.store.editDraft(
        { ...original, steps: [...original.steps, tap('history-scope-edit')] },
        original,
        group,
      ),
    ).toBe(true);
    const first = draftOf(h.store);
    expect(h.store.insertStep(tap('history-scope-redo'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    const before = h.store.getSnapshot();
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    expect(before).toMatchObject({ canUndo: true, canRedo: true });
    const selected = {
      ...scope,
      ...(dimension === 'case'
        ? { caseId: 'other' }
        : dimension === 'root'
          ? { root: '/tmp/history-root' }
          : { projectId: 'history-project' }),
    };
    h.projects.set(readyState(selected));
    const loading = h.store.getSnapshot();
    expect(loading).toMatchObject({
      status: 'loading',
      scope: selected,
      draft: null,
      canUndo: false,
      canRedo: false,
      historyRevision: before.historyRevision + 1,
    });
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot()).toBe(loading);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    await h.store.load();
    const otherOriginal = draftOf(h.store);
    expect(otherOriginal).toEqual(sourceCase(selected.caseId));
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: false });
    const other: Case = {
      ...otherOriginal,
      steps: [...otherOriginal.steps, tap('history-other-scope')],
    };
    expect(h.store.editDraft(other, otherOriginal, group)).toBe(true);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(otherOriginal);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(other);
    h.projects.set(readyState(scope));
    await h.store.load();
    const restored = draftOf(h.store);
    expect(restored).toEqual(first);
    expect(restored).not.toBe(first);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: false, original });
    const reloaded = h.store.getSnapshot();
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(h.store.getSnapshot()).toBe(reloaded);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    const next: Case = { ...restored, steps: [...restored.steps, tap('history-returned-scope')] };
    expect(h.store.editDraft(next, restored, group)).toBe(true);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(first);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
    expect(window.localStorage.length).toBe(2);
  },
);

test.each(['reload', 'refresh'] as const)(
  'history clears both stacks immediately on %s and never restores them from a saved draft',
  async (operation) => {
    const h = setup();
    await h.store.load();
    const group = {};
    const original = draftOf(h.store);
    expect(
      h.store.editDraft(
        { ...original, steps: [...original.steps, tap('history-reload-edit')] },
        original,
        group,
      ),
    ).toBe(true);
    expect(h.store.insertStep(tap('history-reload-redo'))).toBe(true);
    expect(h.store.undo()).toBe(true);
    const before = h.store.getSnapshot();
    const expected = draftOf(h.store);
    const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
    const entered = deferred<boolean>();
    const gate = deferred<unknown>();
    h.getCase.mockImplementationOnce(() => {
      entered.resolve(true);
      return gate.promise;
    });
    if (operation === 'refresh') {
      const project = h.projects.getSnapshot().project;
      if (!project) throw new Error('test project missing');
      h.projects.set({ project: { ...project, revision: '2' } });
    }
    const loading = h.store.load();
    expect(h.store.load()).toBe(loading);
    await entered.promise;
    expect(h.store.getSnapshot()).toMatchObject({
      status: 'loading',
      draft: null,
      canUndo: false,
      canRedo: false,
      historyRevision: before.historyRevision + 1,
    });
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    gate.resolve(sourceCase());
    await loading;
    const restored = draftOf(h.store);
    expect(restored).toEqual(expected);
    expect(restored).not.toBe(expected);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: false });
    expect(h.store.undo()).toBe(false);
    expect(h.store.redo()).toBe(false);
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
    expect(
      h.store.editDraft(
        { ...expected, steps: [...expected.steps, tap('history-stale-reload')] },
        expected,
        group,
      ),
    ).toBe(false);
    expect(
      h.store.editDraft(
        { ...restored, steps: [...restored.steps, tap('history-fresh-reload')] },
        restored,
        group,
      ),
    ).toBe(true);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(restored);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
  },
);

test('history stays cleared after a failed reload and a successful retry', async () => {
  const h = setup();
  await h.store.load();
  expect(h.store.insertStep(tap('history-before-failed-reload'))).toBe(true);
  const first = draftOf(h.store);
  expect(h.store.insertStep(tap('history-redo-before-failed-reload'))).toBe(true);
  expect(h.store.undo()).toBe(true);
  const revision = h.store.getSnapshot().historyRevision;
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  h.getCase.mockRejectedValueOnce(new Error('Fixture source unavailable'));
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'error',
    draft: null,
    canUndo: false,
    canRedo: false,
    historyRevision: revision + 1,
  });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'ready',
    draft: first,
    canUndo: false,
    canRedo: false,
    historyRevision: revision + 2,
  });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

test('history is discarded when the case is deselected rather than revived on reselection', async () => {
  const h = setup();
  await h.store.load();
  expect(h.store.insertStep(tap('history-selected'))).toBe(true);
  const first = draftOf(h.store);
  expect(h.store.insertStep(tap('history-deselected-redo'))).toBe(true);
  expect(h.store.undo()).toBe(true);
  const before = h.store.getSnapshot();
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  h.projects.set({ selectedCaseId: null });
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'idle',
    scope: null,
    draft: null,
    canUndo: false,
    canRedo: false,
    historyRevision: before.historyRevision + 1,
  });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  h.projects.set({ selectedCaseId: 'starter' });
  await h.store.load();
  expect(h.store.getSnapshot()).toMatchObject({ draft: first, canUndo: false, canRedo: false });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(false);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
});

test('history is not persisted or revived after dispose, and the disposed store cannot write or observe selections', async () => {
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  const h = setup({ storage: { getItem: (key) => window.localStorage.getItem(key), setItem } });
  await h.store.load();
  const group = {};
  const original = draftOf(h.store);
  expect(
    h.store.editDraft(
      { ...original, steps: [...original.steps, tap('history-dispose-edit')] },
      original,
      group,
    ),
  ).toBe(true);
  const first = draftOf(h.store);
  expect(h.store.insertStep(tap('history-dispose-redo'))).toBe(true);
  expect(h.store.undo()).toBe(true);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  expect(JSON.parse(saved ?? 'null')).toEqual({
    version: 2,
    scope,
    original,
    draft: first,
    savedAt: 1000,
  });
  expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: true });
  const reads = h.getCase.mock.calls.length;
  const listener = vi.fn();
  h.store.subscribe(listener);
  h.store.dispose();
  const closed = h.store.getSnapshot();
  expect(closed).toMatchObject({ status: 'idle', draft: null, canUndo: false, canRedo: false });
  expect(listener).toHaveBeenCalledTimes(1);
  setItem.mockClear();
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(false);
  expect(h.store.editDraft(first, first, group)).toBe(false);
  expect(h.store.insertStep(tap('history-disposed-insert'))).toBe(false);
  await h.store.load();
  h.projects.set({ selectedCaseId: 'other' });
  expect(h.store.getSnapshot()).toBe(closed);
  expect(listener).toHaveBeenCalledTimes(1);
  expect(h.getCase).toHaveBeenCalledTimes(reads);
  expect(setItem).not.toHaveBeenCalled();
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  h.projects.set({ selectedCaseId: 'starter' });
  const restored = setup({ projects: h.projects });
  await restored.store.load();
  expect(restored.store.getSnapshot()).toMatchObject({
    status: 'ready',
    draft: first,
    original,
    canUndo: false,
    canRedo: false,
  });
  expect(restored.store.undo()).toBe(false);
  expect(restored.store.redo()).toBe(false);
  const current = draftOf(restored.store);
  expect(
    restored.store.editDraft(
      { ...current, steps: [...current.steps, tap('history-recreated-edit')] },
      current,
      group,
    ),
  ).toBe(true);
  expect(restored.store.undo()).toBe(true);
  expect(draftOf(restored.store)).toEqual(first);
  expect(restored.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
});

test('history retains exactly the newest 100 entries shared by insertion and editing, with a bounded redo round trip', async () => {
  const setItem = vi.fn((key: string, value: string) => window.localStorage.setItem(key, value));
  const now = vi.fn(() => 1000);
  const h = setup({
    raw: { ...sourceCase(), steps: [tap('history-limit-base')] },
    storage: { getItem: (key) => window.localStorage.getItem(key), setItem },
    now,
  });
  await h.store.load();
  const original = draftOf(h.store);
  const drafts = [original];
  const revision = h.store.getSnapshot().historyRevision;
  for (let index = 1; index <= 102; index++) {
    const expected = draftOf(h.store);
    if (index % 2 === 1) {
      const step = tap(`history-limit-${index}`);
      expect(h.store.insertStep(step)).toBe(true);
      expect(draftOf(h.store)).toEqual({ ...expected, steps: [...expected.steps, step] });
    } else {
      const next = structuredClone(expected);
      next.steps[0].timeout_ms = 1000 + index;
      // Keep the document size fixed while measuring the shared history entry limit.
      next.steps.splice(1);
      expect(h.store.editDraft(next, expected)).toBe(true);
      expect(draftOf(h.store)).toEqual(next);
    }
    drafts.push(draftOf(h.store));
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: false });
  }
  const atLimit = h.store.getSnapshot();
  const writes = setItem.mock.calls.length;
  expect(h.store.editDraft(structuredClone(draftOf(h.store)), draftOf(h.store), {})).toBe(true);
  expect(
    h.store.editDraft({ ...draftOf(h.store), steps: [] } as unknown as Case, draftOf(h.store)),
  ).toBe(false);
  expect(h.store.getSnapshot().historyRevision).toBe(atLimit.historyRevision);
  expect(setItem).toHaveBeenCalledTimes(writes);
  for (let index = 101; index >= 2; index--) {
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(drafts[index]);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: index > 2, canRedo: true });
  }
  expect(h.store.getSnapshot().historyRevision).toBe(revision + 100);
  const oldestRetained = h.store.getSnapshot();
  const undoWrites = setItem.mock.calls.length;
  const undoTimes = now.mock.calls.length;
  expect(h.store.undo()).toBe(false);
  expect(h.store.getSnapshot()).toBe(oldestRetained);
  expect(setItem).toHaveBeenCalledTimes(undoWrites);
  expect(now).toHaveBeenCalledTimes(undoTimes);
  expect(draftOf(h.store)).toEqual(drafts[2]);
  expect(draftOf(h.store)).not.toEqual(original);
  for (let index = 3; index <= 102; index++) {
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(drafts[index]);
    expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: index < 102 });
  }
  const redone = h.store.getSnapshot();
  expect(redone.historyRevision).toBe(revision + 200);
  expect(h.store.redo()).toBe(false);
  expect(h.store.getSnapshot()).toBe(redone);
  expect(setItem).toHaveBeenCalledTimes(302);
  expect(now).toHaveBeenCalledTimes(302);
  expect(redone.original).toBe(original);
  expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
    version: 2,
    scope,
    original,
    draft: drafts[102],
    savedAt: 1000,
  });
});

test('history counts more than 100 consecutive grouped saves as one entry, not 100 keystrokes', async () => {
  const h = setup({ raw: { ...sourceCase(), steps: [tap('history-group-limit-base')] } });
  await h.store.load();
  const original = draftOf(h.store);
  const group = {};
  for (let index = 1; index <= 101; index++) {
    const expected = draftOf(h.store);
    const next = structuredClone(expected);
    next.steps[0].timeout_ms = 1000 + index;
    expect(h.store.editDraft(next, expected, group)).toBe(true);
  }
  const grouped = draftOf(h.store);
  expect(grouped.steps[0].timeout_ms).toBe(1101);
  expect(h.store.undo()).toBe(true);
  expect(draftOf(h.store)).toEqual(original);
  expect(h.store.getSnapshot()).toMatchObject({ canUndo: false, canRedo: true });
  expect(h.store.undo()).toBe(false);
  expect(h.store.redo()).toBe(true);
  expect(draftOf(h.store)).toEqual(grouped);
  expect(h.store.getSnapshot()).toMatchObject({ canUndo: true, canRedo: false });
  expect(h.store.redo()).toBe(false);
  expect(h.store.getSnapshot().original).toBe(original);
});

test('explicit binding edits persist v4 without relaxing the legacy structure-edit API', async () => {
  const h = setup();
  await h.store.load();
  const original = draftOf(h.store);
  const next = copyDraftCase({
    ...original,
    parameters: {
      ...original.parameters,
      flag: { type: 'boolean', required: false, default: false },
    },
    inputs: { ...original.inputs, flag: false },
  });
  expect(h.store.editDraft(next, original)).toBe(false);
  expect(h.store.editCaseBindings(next, original)).toBe(true);
  expect(h.store.getSnapshot().original).toBe(original);
  expect(draftOf(h.store)).toEqual(next);
  expect(JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null')).toEqual({
    version: 4,
    format: 'case-parameter-bindings',
    scope,
    original,
    draft: next,
    savedAt: 1000,
  });
  const reloaded = setup();
  await reloaded.store.load();
  expect(reloaded.store.getSnapshot()).toMatchObject({
    status: 'ready',
    persistence: 'saved',
    canInsert: true,
    canUndo: false,
  });
  expect(draftOf(reloaded.store)).toEqual(next);
});

test('binding, structure and authoring-text edits share v4 history while source text stays immutable', async () => {
  const h = setup();
  await h.store.load();
  const original = draftOf(h.store);
  const bindings = copyDraftCase({
    ...original,
    parameters: { ...original.parameters, query: { type: 'string', required: false, default: '' } },
    inputs: { ...original.inputs, query: '' },
  });
  expect(h.store.editCaseBindings(bindings, original)).toBe(true);
  const steps = copyDraftCase({ ...bindings, steps: [tap('v4-step')] });
  expect(h.store.editDraft(steps, draftOf(h.store))).toBe(true);
  const authoring = copyDraftCase({ ...steps, original_text: 'Draft authoring input' });
  expect(h.store.editCaseText(authoring, draftOf(h.store))).toBe(true);
  for (const expected of [steps, bindings, original]) {
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(expected);
    expect(
      JSON.parse(window.localStorage.getItem(caseDraftStorageKey(scope)) ?? 'null'),
    ).toMatchObject({ version: 4, format: 'case-parameter-bindings' });
  }
  for (const expected of [bindings, steps, authoring]) {
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(expected);
  }
  expect(h.store.getSnapshot().original).toBe(original);
});

test.each([1, 2, 3])(
  'never treats protected binding edits in a v%s record as v4',
  async (version) => {
    const original = copyDraftCase(sourceCase());
    const draft = copyDraftCase({
      ...original,
      parameters: { ...original.parameters, added: { type: 'string', required: true } },
    });
    const saved = JSON.stringify({
      version,
      ...(version === 3 ? { format: 'case-authoring-text' } : {}),
      scope,
      original,
      draft,
      savedAt: 1000,
    });
    window.localStorage.setItem(caseDraftStorageKey(scope), saved);
    const h = setup();
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  },
);

test.each(['missing-format', 'wrong-format', 'source', 'identity', 'profile', 'base'] as const)(
  'quarantines a damaged v4 %s record without deleting it',
  async (damage) => {
    const original = copyDraftCase(sourceCase());
    const record = {
      version: 4,
      format: 'case-parameter-bindings',
      scope,
      original,
      draft: original,
      savedAt: 1000,
    };
    const damaged =
      damage === 'missing-format'
        ? { ...record, format: undefined }
        : damage === 'wrong-format'
          ? { ...record, format: 'case-authoring-text' }
          : damage === 'base'
            ? { ...record, original: { ...original, goal: 'Changed source goal' } }
            : {
                ...record,
                draft: {
                  ...original,
                  ...(damage === 'source'
                    ? { source: null }
                    : damage === 'identity'
                      ? { case_id: 'other' }
                      : { profile_refs: ['other-profile'] }),
                },
              };
    const saved = JSON.stringify(damaged);
    window.localStorage.setItem(caseDraftStorageKey(scope), saved);
    const h = setup();
    await h.store.load();
    expect(h.store.getSnapshot()).toMatchObject({ status: 'error', draft: null, canInsert: false });
    expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  },
);

test.each(protectedEdits.filter(([field]) => field !== 'parameters' && field !== 'inputs'))(
  'binding entrypoint still rejects protected %s',
  async (_name, fields) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    expect(h.store.editCaseBindings({ ...original, ...fields } as Case, original)).toBe(false);
    expect(draftOf(h.store)).toBe(original);
    expect(window.localStorage.length).toBe(0);
  },
);

test.each(['constructor', 'toString', '__proto__'])(
  'binding history safely preserves a legal own-property name %s',
  async (name) => {
    const h = setup();
    await h.store.load();
    const original = draftOf(h.store);
    const next = copyDraftCase({
      ...original,
      parameters: {
        ...original.parameters,
        ...Object.fromEntries([[name, { type: 'boolean', required: false, default: false }]]),
      },
      inputs: { ...original.inputs, ...Object.fromEntries([[name, false]]) },
    });
    expect(h.store.editCaseBindings(next, original)).toBe(true);
    expect(Object.hasOwn(draftOf(h.store).parameters, name)).toBe(true);
    expect(h.store.undo()).toBe(true);
    expect(draftOf(h.store)).toEqual(original);
    expect(h.store.redo()).toBe(true);
    expect(draftOf(h.store)).toEqual(next);
  },
);

test('binding writes reject stale references and storage failures without overwriting the confirmed draft', async () => {
  let fail = false;
  const h = setup({
    storage: {
      getItem: (key) => window.localStorage.getItem(key),
      setItem: (key, value) => {
        if (fail) throw new Error('Synthetic disk full');
        window.localStorage.setItem(key, value);
      },
    },
  });
  await h.store.load();
  const original = draftOf(h.store);
  const first = copyDraftCase({ ...original, inputs: { ...original.inputs, quantity: 0 } });
  expect(h.store.editCaseBindings(first, original)).toBe(true);
  const confirmed = draftOf(h.store);
  const saved = window.localStorage.getItem(caseDraftStorageKey(scope));
  expect(h.store.editCaseBindings(original, original)).toBe(false);
  fail = true;
  expect(
    h.store.editCaseBindings(
      { ...confirmed, inputs: { ...confirmed.inputs, quantity: 1 } },
      confirmed,
    ),
  ).toBe(false);
  expect(draftOf(h.store)).toBe(confirmed);
  expect(window.localStorage.getItem(caseDraftStorageKey(scope))).toBe(saved);
  expect(h.store.getSnapshot()).toMatchObject({
    persistence: 'error',
    canInsert: false,
    canUndo: false,
  });
  expect(h.store.getSnapshot().original).toBe(original);
});
