import type {
  ActionStep,
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
import type { ProjectState } from '../projects/projectStore';
import {
  caseDraftStorageKey,
  createCaseDraftStore,
  type CaseDraftRuntimeClient,
  type CaseDraftScope,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
} from './caseDraftStore';
import { assertDraftCase } from './caseDraftValidation';

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
      flow_ids: [],
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
}: {
  raw?: unknown;
  projects?: ReturnType<typeof projectSource>;
  storage?: CaseDraftStoreDeps['storage'];
} = {}) {
  const getCase = vi.fn<CaseDraftRuntimeClient['getCase']>().mockResolvedValue(raw);
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
  }));
  const store = createCaseDraftStore({ projects, getClient, storage, now: () => 1000 });
  stores.push(store);
  return {
    store,
    projects,
    getCase,
    getClient,
    getProject,
    verifyConnection,
    getCaseEntered: (caseId = 'starter') => entered(caseId).promise,
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
  ['version', (record) => JSON.stringify({ ...record, version: 2 })],
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
