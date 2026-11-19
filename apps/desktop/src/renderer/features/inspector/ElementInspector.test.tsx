import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import axe from 'axe-core';
import { CaseDraftPanel } from '../editor/CaseDraftPanel';
import {
  createCaseDraftStore,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
} from '../editor/caseDraftStore';
import { SCREEN_GEOMETRY } from '../stream/screenFixtures';
import { ElementInspector } from './ElementInspector';
import { ElementTree } from './ElementTree';
import {
  INSPECTOR_CONNECTION,
  INSPECTOR_DEVICE,
  simulatedInspectorCase,
  simulatedInspectorObservation,
  simulatedInspectorProjectState,
  simulatedLocateResult,
} from './inspectorFixtures';
import {
  createElementInspectorStore,
  type ElementInspectorStore,
  type InspectorApi,
} from './inspectorStore';

const stores: ElementInspectorStore[] = [];
const drafts: CaseDraftStore[] = [];
afterEach(() => {
  cleanup();
  stores.splice(0).forEach((store) => store.stop());
  drafts.splice(0).forEach((store) => store.dispose());
  window.localStorage.clear();
});

async function setup({
  storage = window.localStorage,
  noCase = false,
  ocrOnly = false,
  count = 0,
  status = 'resolved',
}: {
  storage?: CaseDraftStoreDeps['storage'];
  noCase?: boolean;
  ocrOnly?: boolean;
  count?: number;
  status?: Parameters<typeof simulatedLocateResult>[1];
} = {}) {
  let now = 1000;
  const api = {
    getAgentGeometry: vi.fn<InspectorApi['getAgentGeometry']>(async (serial, query) => ({
      serial,
      session_id: query.session_id,
      source: 'adb_input_viewport',
      status: 'available',
      atomic_with_observation: false,
      geometry: SCREEN_GEOMETRY,
    })),
    observeAgent: vi.fn<InspectorApi['observeAgent']>(async (_serial, body) => {
      const raw = simulatedInspectorObservation(body.request_id, SCREEN_GEOMETRY, {
        ocr: body.ocr,
        duplicateText: true,
        count,
      });
      if (ocrOnly) {
        raw.sources.tree.status = 'unavailable';
        raw.sources.tree.error = { code: 'accessibility_unavailable' };
      }
      return raw;
    }),
    locate: vi.fn<InspectorApi['locate']>(async (_serial, body) =>
      simulatedLocateResult(body, status),
    ),
  };
  const store = createElementInspectorStore({
    getConnection: async () => INSPECTOR_CONNECTION,
    clientFor: () => api,
    now: () => now,
    id: () => crypto.randomUUID(),
    setTimer: () => null,
    clearTimer: () => undefined,
  });
  stores.push(store);
  store.setDevice(INSPECTOR_DEVICE);
  if (ocrOnly) store.setIncludeOcr(true);
  const project = simulatedInspectorProjectState();
  if (noCase) project.selectedCaseId = null;
  const draft = createCaseDraftStore({
    projects: { getSnapshot: () => project, subscribe: () => () => undefined },
    storage,
    now: () => 1000,
    getClient: async () => ({
      getProject: async () => project.project,
      getCase: async () => simulatedInspectorCase(),
      verifyConnection: async () => undefined,
    }),
  });
  drafts.push(draft);
  await Promise.all([store.refresh(), draft.load()]);
  return {
    store,
    draft,
    api,
    setNow: (value: number) => {
      now = value;
    },
  };
}

function mount(h: Awaited<ReturnType<typeof setup>>) {
  return render(
    <div>
      <h2>检查与草稿</h2>
      <ElementInspector store={h.store} draftStore={h.draft} />
      <CaseDraftPanel store={h.draft} />
    </div>,
  );
}

async function selectButton(user: ReturnType<typeof userEvent.setup>) {
  screen.getByRole('tree', { name: '元素树' }).focus();
  await user.keyboard('{ArrowDown}{ArrowDown}{Enter}');
  await waitFor(() =>
    expect(screen.getByRole('region', { name: '元素属性' })).toHaveTextContent('确认提交'),
  );
}

test('keyboard-select, reorder, validate and one-click insert persist a real typed draft step', async () => {
  const h = await setup();
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  expect(h.store.getSnapshot().selectedId).toBe('native:1');
  await user.click(screen.getByRole('button', { name: '下移 res_id 候选' }));
  await user.click(screen.getByRole('checkbox', { name: '使用 text 候选' }));
  await waitFor(() => expect(h.store.getSnapshot().validation?.status).toBe('resolved'));
  await user.click(screen.getByRole('button', { name: '插入步骤' }));
  const inserted = h.draft.getSnapshot().draft?.steps.at(-1);
  expect(inserted).toMatchObject({
    type: 'action',
    action: {
      kind: 'tap',
      target: {
        locator: {
          candidates: [{ by: 'desc' }, { by: 'res_id' }, { by: 'xpath' }],
          expect: { unique: true, visible: true },
        },
      },
    },
  });
  expect(h.draft.getSnapshot().draft?.steps.length).toBe(simulatedInspectorCase().steps.length + 1);
  expect(h.draft.getSnapshot().persistence).toBe('saved');
  expect(screen.getByText(/已将点击步骤插入.*草稿已保存/)).toBeVisible();
  expect(
    within(screen.getByRole('list', { name: '草稿步骤' })).getByText('新增步骤'),
  ).toBeVisible();
  expect(h.api.observeAgent.mock.calls[0]?.[1]).toMatchObject({
    for_action: false,
    screenshot: false,
  });
});

test('setting a checkpoint explicitly declares exists/native without copying current values as expected', async () => {
  const h = await setup();
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  await user.click(screen.getByRole('button', { name: '设为检查点' }));
  const inserted = h.draft.getSnapshot().draft?.steps.at(-1);
  expect(inserted).toMatchObject({
    type: 'check',
    checkpoint: { required: true, condition: { operator: 'exists', source: 'native' } },
  });
  if (inserted?.type !== 'check') throw new Error('not a check step');
  expect(inserted.checkpoint.condition).not.toHaveProperty('expected');
  expect(inserted.checkpoint.id).toMatch(/^inspect-exists-/);
});

test('without a selected case there is no ephemeral insertion and the reason stays visible', async () => {
  const h = await setup({ noCase: true });
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  expect(screen.getByRole('button', { name: '插入步骤' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '设为检查点' })).toBeDisabled();
  expect(
    within(screen.getByRole('region', { name: '元素检查器' })).getByText(/请先在用例树中选择用例/),
  ).toBeVisible();
  expect(h.draft.getSnapshot().draft).toBeNull();
});

test('a late expiry timer cannot permit an insertion from an aged snapshot', async () => {
  const h = await setup();
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  const original = h.draft.getSnapshot().draft;
  h.setNow(16_000);
  await user.click(screen.getByRole('button', { name: '插入步骤' }));
  expect(h.draft.getSnapshot().draft).toBe(original);
  expect(screen.getByRole('button', { name: '插入步骤' })).toBeDisabled();
  expect(screen.queryByText(/已将点击步骤插入/)).not.toBeInTheDocument();
  expect(screen.getByText(/元素快照已过期/)).toBeVisible();
});

test('a failed second save removes the earlier success message and preserves the first draft', async () => {
  const records = new Map<string, string>();
  let saves = 0;
  const h = await setup({
    storage: {
      getItem: (key) => records.get(key) ?? null,
      setItem: (key, value) => {
        if (saves++ > 0) throw new Error('QuotaExceededError');
        records.set(key, value);
      },
    },
  });
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  await user.click(screen.getByRole('button', { name: '插入步骤' }));
  const first = h.draft.getSnapshot().draft;
  expect(screen.getByText(/已将点击步骤插入/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: '插入步骤' }));
  expect(h.draft.getSnapshot().draft).toBe(first);
  expect(h.draft.getSnapshot().persistence).toBe('error');
  expect(screen.queryByText(/已将点击步骤插入/)).not.toBeInTheDocument();
  expect(
    screen
      .getAllByRole('alert')
      .some((element) => element.textContent?.includes('QuotaExceededError')),
  ).toBe(true);
});

test('OCR stays in bitmap coordinates, has unknown native states, and generates only an explicit OCR checkpoint', async () => {
  const h = await setup({ ocrOnly: true });
  const user = userEvent.setup();
  mount(h);
  await user.click(screen.getByRole('treeitem', { name: '总计 42.00 OCR' }));
  const properties = screen.getByRole('region', { name: '元素属性' });
  expect(properties).toHaveTextContent('screenshot_pixels');
  expect(properties).toHaveTextContent('OCR 不证明原生');
  expect(within(properties).getByText('启用').nextElementSibling).toHaveTextContent('未知');
  expect(screen.getByRole('button', { name: '插入步骤' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '设为检查点' }));
  const inserted = h.draft.getSnapshot().draft?.steps.at(-1);
  expect(inserted).toMatchObject({
    type: 'check',
    checkpoint: {
      condition: {
        operator: 'exists',
        source: 'ocr',
        target: { locator: { candidates: [{ by: 'ocr_text', value: '总计 42.00' }] } },
      },
    },
  });
  expect(h.api.locate).not.toHaveBeenCalled();
});

test('ambiguous real-time validation is visible and insertion never weakens unique:true', async () => {
  const h = await setup({ status: 'ambiguous' });
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  await waitFor(() => expect(screen.getByText(/当前设备验证: 歧义/)).toBeVisible());
  expect(screen.getByText(/歧义不会静默取首个/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: '插入步骤' }));
  expect(h.draft.getSnapshot().draft?.steps.at(-1)).toMatchObject({
    action: { target: { locator: { expect: { unique: true } } } },
  });
  expect(h.api.locate).toHaveBeenCalledOnce();
});

test('a 3000-node tree is virtualized and its final row is keyboard reachable/searchable', async () => {
  const h = await setup({ count: 3000 });
  const user = userEvent.setup();
  const snapshot = h.store.getSnapshot().snapshot;
  if (!snapshot) throw new Error('missing snapshot');
  render(<ElementTree snapshot={snapshot} selectedId={null} store={h.store} />);
  const tree = screen.getByRole('tree', { name: '元素树' });
  expect(within(tree).getAllByRole('treeitem').length).toBeLessThan(24);
  fireEvent.keyDown(tree, { key: 'End' });
  const last = document.getElementById(tree.getAttribute('aria-activedescendant') ?? '');
  expect(last).toHaveTextContent('node-2999');
  await waitFor(() => expect(tree.scrollTop).toBeGreaterThan(90_000));
  fireEvent.keyDown(tree, { key: 'Enter' });
  expect(h.store.getSnapshot().selectedId).toBe(snapshot.elements.at(-1)?.id);
  await user.type(screen.getByRole('searchbox', { name: '搜索元素' }), 'node-2999');
  await waitFor(() => expect(within(tree).getAllByRole('treeitem')).toHaveLength(3));
  expect(within(tree).getByText('订单表单')).toBeVisible();
});

test('offline/expired state retains source details but disables authoring and verification', async () => {
  const h = await setup();
  const user = userEvent.setup();
  mount(h);
  await selectButton(user);
  act(() => h.store.setDevice(INSPECTOR_DEVICE, '合成设备已离线'));
  expect(screen.getByRole('button', { name: '刷新元素' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '验证定位器' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '插入步骤' })).toBeDisabled();
  expect(screen.getByRole('region', { name: '元素属性' })).toHaveTextContent('com.pulse.target');
});

test('keyboard names and tree/locator semantics pass non-layout accessibility checks', async () => {
  const h = await setup();
  const user = userEvent.setup();
  const { container } = mount(h);
  await selectButton(user);
  const canvas = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  try {
    const result = await axe.run(container, { rules: { 'color-contrast': { enabled: false } } });
    expect(result.violations).toEqual([]);
  } finally {
    canvas.mockRestore();
  }
});
