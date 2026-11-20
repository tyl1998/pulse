import type { Case } from '@pulse/schema';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import { simulatedInspectorProjectState } from '../inspector/inspectorFixtures';
import { CaseDraftPanel } from './CaseDraftPanel';
import {
  createCaseDraftStore,
  caseDraftStorageKey,
  type CaseDraftStore,
  type CaseDraftStoreDeps,
} from './caseDraftStore';
import { copyDraftCase } from './caseDraftValidation';
import { caseStepAtOffset, readThreeSectionText, writeThreeSectionText } from './caseText';
import type { createMonacoCaseEditor } from './monacoCaseEditor';

type MountOptions = Parameters<typeof createMonacoCaseEditor>[1];
const monaco = vi.hoisted(() => ({ create: vi.fn(), dispose: vi.fn() }));
vi.mock('./monacoCaseEditor', () => ({ createMonacoCaseEditor: monaco.create }));
const stores: CaseDraftStore[] = [];

beforeEach(() => {
  window.localStorage.clear();
  monaco.create.mockReset();
  monaco.dispose.mockReset();
  // Only the Monaco layout/worker boundary is replaced in jsdom; store and validation are real.
  monaco.create.mockImplementation((host: HTMLElement, options: MountOptions) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'monaco-editor';
    const input = document.createElement('textarea');
    input.setAttribute('aria-label', '用例 JSON 文本');
    input.value = options.text;
    input.readOnly = options.readOnly;
    input.onchange = () => {
      options.onChange(input.value);
      options.onCursorStep(caseStepAtOffset(input.value, input.selectionStart));
    };
    input.onblur = options.onBlur;
    input.onkeydown = (event) => {
      if (!(event.ctrlKey || event.metaKey) || !['z', 'y'].includes(event.key.toLowerCase()))
        return;
      event.preventDefault();
      if (event.shiftKey || event.key.toLowerCase() === 'y') options.onRedo();
      else options.onUndo();
    };
    wrapper.append(input);
    host.append(wrapper);
    return {
      setText: (text: string) => {
        input.value = text;
      },
      setReadOnly: (readOnly: boolean) => {
        input.readOnly = readOnly;
      },
      focusText: (start: number, end: number, backward: boolean) => {
        input.focus();
        input.setSelectionRange(start, end, backward ? 'backward' : 'forward');
      },
      setIssue: (issue: { offset: number } | null) => {
        input.setAttribute('aria-invalid', String(!!issue));
        input.dataset['markerOffset'] = String(issue?.offset ?? -1);
      },
      layout: () => undefined,
      dispose: () => {
        monaco.dispose();
        wrapper.remove();
      },
    };
  });
});

afterEach(() => {
  cleanup();
  stores.forEach((store) => store.dispose());
  stores.length = 0;
  vi.restoreAllMocks();
});

async function setup({
  original = copyDraftCase({
    ...controlFlowCase,
    case_id: 'starter',
    steps: [
      { type: 'action', id: 'launch', action: { kind: 'launch', app_id: 'com.pulse.target' } },
    ],
  }),
  storage = window.localStorage,
  initialView = 'steps',
  mount = true,
}: {
  original?: Case;
  storage?: CaseDraftStoreDeps['storage'];
  initialView?: 'three' | 'steps' | 'json';
  mount?: boolean;
} = {}) {
  let state = simulatedInspectorProjectState();
  if (!state.project) throw new Error('missing fixture project');
  state = {
    ...state,
    project: { ...state.project, case_ids: ['starter', 'other'], entry_cases: ['starter'] },
    selectedCaseId: 'starter',
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
    name: caseId,
  }));
  const store = createCaseDraftStore({
    projects,
    getClient: async () => ({
      getProject: async () => state.project,
      getCase,
      verifyConnection: async () => undefined,
    }),
    storage,
    now: () => 1000,
  });
  stores.push(store);
  await store.load();
  const view = mount ? render(<CaseDraftPanel store={store} initialView={initialView} />) : null;
  return { store, projects, getCase, original, view };
}

function current(store: CaseDraftStore): Case {
  const draft = store.getSnapshot().draft;
  if (!draft) throw new Error('missing fixture draft');
  return draft;
}

async function switchView(name: string) {
  await userEvent.click(screen.getByRole('tab', { name }));
  return screen.getByRole('tabpanel', { name });
}

function changeSteps(store: CaseDraftStore, appId: string): Case['steps'] {
  return [
    {
      ...current(store).steps[0],
      type: 'action',
      id: 'launch',
      action: { kind: 'launch', app_id: appId },
    },
  ];
}

test('step forms, three-section text and JSON mutate one draft with shared cross-view undo and redo', async () => {
  const h = await setup();
  const original = current(h.store);
  expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const appId = screen.getByRole('textbox', { name: '应用 ID' });
  fireEvent.change(appId, { target: { value: 'com.pulse.steps' } });
  fireEvent.blur(appId);
  const first = current(h.store);
  await switchView('三段式');
  const execution = screen.getByRole('textbox', { name: '执行文本' });
  expect(JSON.parse((execution as HTMLTextAreaElement).value)).toEqual(first.steps);
  fireEvent.change(execution, {
    target: { value: JSON.stringify(changeSteps(h.store, 'com.pulse.three')) },
  });
  fireEvent.blur(execution);
  const second = current(h.store);
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const json = screen.getByRole('textbox', { name: '用例 JSON 文本' });
  expect(JSON.parse((json as HTMLTextAreaElement).value)).toEqual(second);
  const next = { ...second, checks: [{ ...second.checks[0], id: 'json-final' }] };
  fireEvent.change(json, { target: { value: JSON.stringify(next, null, 2) } });
  const third = current(h.store);
  expect(third).toEqual(next);
  expect(h.store.getSnapshot().original).toBe(original);
  await switchView('步骤');
  expect(screen.getByText('json-final')).toBeVisible();
  expect(screen.getByRole('textbox', { name: '应用 ID' })).toHaveValue('com.pulse.three');
  for (const expected of [second, first, original]) {
    await userEvent.click(screen.getByRole('button', { name: '撤销' }));
    expect(current(h.store)).toEqual(expected);
  }
  expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
  for (const expected of [first, second, third]) {
    await userEvent.click(screen.getByRole('button', { name: '重做' }));
    expect(current(h.store)).toEqual(expected);
  }
  expect(screen.getByRole('button', { name: '重做' })).toBeDisabled();
  await switchView('三段式');
  expect(
    JSON.parse((screen.getByRole('textbox', { name: '执行文本' }) as HTMLTextAreaElement).value),
  ).toEqual(third.steps);
  expect(h.getCase).toHaveBeenCalledTimes(1);
});

test('keeps rejected three-section text across view switches and merges only an unchanged section', async () => {
  const h = await setup({ initialView: 'three' });
  const before = current(h.store);
  const execution = screen.getByRole('textbox', { name: '执行文本' });
  fireEvent.change(execution, { target: { value: '搜索商品并加入购物车' } });
  expect(current(h.store)).toBe(before);
  expect(execution).toHaveAttribute('aria-invalid', 'true');
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const next = { ...before, checks: [{ ...before.checks[0], id: 'other-section' }] };
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: JSON.stringify(next) },
  });
  await switchView('三段式');
  const retained = screen.getByRole('textbox', { name: '执行文本' });
  expect(retained).toHaveValue('搜索商品并加入购物车');
  fireEvent.change(retained, {
    target: { value: JSON.stringify(changeSteps(h.store, 'com.pulse.merged')) },
  });
  expect(current(h.store).checks).toEqual(next.checks);
  expect(current(h.store).steps[0]).toHaveProperty('action.app_id', 'com.pulse.merged');
  expect(retained).toHaveAttribute('aria-invalid', 'false');
});

test('requires explicit restore when another view changes the same rejected section', async () => {
  const h = await setup({ initialView: 'three' });
  fireEvent.change(screen.getByRole('textbox', { name: '执行文本' }), { target: { value: '[' } });
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const next = { ...current(h.store), steps: changeSteps(h.store, 'com.pulse.newer') };
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: JSON.stringify(next) },
  });
  await switchView('三段式');
  expect(screen.getByRole('textbox', { name: '执行文本' })).toHaveValue('[');
  const section = screen.getByRole('region', { name: '执行分区' });
  expect(within(section).getByRole('alert')).toHaveTextContent('其他视图修改');
  fireEvent.change(screen.getByRole('textbox', { name: '执行文本' }), {
    target: { value: JSON.stringify(changeSteps(h.store, 'com.pulse.stale')) },
  });
  expect(current(h.store)).toEqual(next);
  await userEvent.click(within(section).getByRole('button', { name: '恢复已保存文本' }));
  expect(
    JSON.parse((screen.getByRole('textbox', { name: '执行文本' }) as HTMLTextAreaElement).value),
  ).toEqual(next.steps);
});

test.each(['syntax', 'duplicate', 'empty-steps', 'source', 'case-id'])(
  'rejects %s JSON without modifying the confirmed draft or original provenance',
  async (scenario) => {
    const h = await setup({ initialView: 'json' });
    await waitFor(() => expect(monaco.create).toHaveBeenCalled());
    const before = current(h.store);
    const text =
      scenario === 'syntax'
        ? '{'
        : scenario === 'duplicate'
          ? JSON.stringify(before).replace('"steps":', '"steps":[],"steps":')
          : JSON.stringify({
              ...before,
              ...(scenario === 'empty-steps'
                ? { steps: [] }
                : scenario === 'source'
                  ? { source: 'replaced origin' }
                  : { case_id: 'replaced-case-id' }),
            });
    fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
      target: { value: text },
    });
    expect(current(h.store)).toBe(before);
    expect(h.store.getSnapshot().original).toBe(before);
    expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveValue(text);
    expect(screen.getByRole('status', { name: 'JSON 校验状态' })).toHaveTextContent('尚未保存');
    expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: '恢复已保存文本' }));
    expect(
      JSON.parse(
        (screen.getByRole('textbox', { name: '用例 JSON 文本' }) as HTMLTextAreaElement).value,
      ),
    ).toEqual(before);
  },
);

test('natural-language fields share Case.original_text with JSON and preserve the immutable source across undo, structured edits and reload', async () => {
  const h = await setup({ initialView: 'three' });
  const original = current(h.store);
  const before = screen.getByRole('textbox', { name: '前置（自然语言）' });
  fireEvent.change(before, { target: { value: '已登录，数据在用例外准备' } });
  fireEvent.blur(before);
  const first = current(h.store);
  const execution = screen.getByRole('textbox', { name: '执行（自然语言）' });
  fireEvent.change(execution, { target: { value: '搜索商品并加入购物车' } });
  fireEvent.blur(execution);
  const second = current(h.store);
  expect(readThreeSectionText(second.original_text ?? '')).toEqual({
    preconditions: '已登录，数据在用例外准备',
    steps: '搜索商品并加入购物车',
    checks: '',
  });
  expect(second.steps).toEqual(original.steps);
  expect(second.preconditions).toEqual(original.preconditions);
  expect(second.checks).toEqual(original.checks);
  expect(h.store.getSnapshot().original).toBe(original);
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const json = screen.getByRole('textbox', { name: '用例 JSON 文本' });
  expect(JSON.parse((json as HTMLTextAreaElement).value).original_text).toBe(second.original_text);
  const text = writeThreeSectionText({
    preconditions: '已登录，数据在用例外准备',
    steps: '搜索商品并加入购物车',
    checks: '数量为 1，金额正确',
  });
  fireEvent.change(json, { target: { value: JSON.stringify({ ...second, original_text: text }) } });
  const third = current(h.store);
  const originalSource = original.original_text;
  expect(h.store.getSnapshot().original?.original_text).toBe(originalSource);
  await switchView('步骤');
  expect(screen.getByRole('region', { name: '草稿创作输入' })).toHaveTextContent(
    '数量为 1，金额正确',
  );
  const structural = { ...third, steps: changeSteps(h.store, 'com.pulse.authored') };
  act(() => {
    expect(h.store.editDraft(structural, third)).toBe(true);
  });
  const fourth = current(h.store);
  expect(fourth.original_text).toBe(text);
  await userEvent.click(screen.getByRole('button', { name: '撤销' }));
  expect(current(h.store)).toEqual(third);
  await userEvent.click(screen.getByRole('button', { name: '撤销' }));
  expect(current(h.store)).toEqual(second);
  await userEvent.click(screen.getByRole('button', { name: '撤销' }));
  expect(current(h.store)).toEqual(first);
  await userEvent.click(screen.getByRole('button', { name: '重做' }));
  expect(current(h.store)).toEqual(second);
  const scope = h.store.getSnapshot().scope;
  if (!scope) throw new Error('missing scope');
  const key = caseDraftStorageKey(scope);
  const record = JSON.parse(window.localStorage.getItem(key) ?? 'null');
  expect(record).toMatchObject({
    version: 3,
    format: 'case-authoring-text',
    original,
    draft: second,
  });
  await act(async () => {
    await h.store.load();
  });
  expect(current(h.store)).toEqual(second);
  expect(h.store.getSnapshot().original?.original_text).toBe(originalSource);
  await switchView('三段式');
  expect(screen.getByRole('textbox', { name: '前置（自然语言）' })).toHaveValue(
    '已登录，数据在用例外准备',
  );
  expect(screen.getByRole('textbox', { name: '执行（自然语言）' })).toHaveValue(
    '搜索商品并加入购物车',
  );
  expect(screen.getByRole('textbox', { name: '预期（自然语言）' })).toHaveValue('');
});

test('JSON can revise or clear only draft authoring text, and never changes its source Case or executable structure', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const original = current(h.store);
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: {
      value: JSON.stringify({
        ...original,
        original_text: 'Free-form draft authoring text without guessed steps',
      }),
    },
  });
  expect(current(h.store).original_text).toBe(
    'Free-form draft authoring text without guessed steps',
  );
  expect(current(h.store).steps).toEqual(original.steps);
  expect(h.store.getSnapshot().original?.original_text).toBe(original.original_text);
  await switchView('三段式');
  for (const name of ['前置（自然语言）', '执行（自然语言）', '预期（自然语言）'])
    expect(screen.getByRole('textbox', { name })).toHaveValue('');
  expect(screen.getByText(/现有草稿文本未拆分/)).toBeVisible();
  await switchView('JSON');
  const withoutText = { ...current(h.store) };
  delete withoutText.original_text;
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: JSON.stringify(withoutText) },
  });
  expect(current(h.store)).not.toHaveProperty('original_text');
  expect(h.store.getSnapshot().original).toEqual(original);
  await userEvent.click(screen.getByRole('button', { name: '撤销' }));
  expect(current(h.store).original_text).toBe(
    'Free-form draft authoring text without guessed steps',
  );
});

test('shared undo and redo keep the JSON editor focused and do not remount its model', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const original = current(h.store);
  const input = screen.getByRole('textbox', { name: '用例 JSON 文本' });
  input.focus();
  fireEvent.change(input, {
    target: {
      value: JSON.stringify({ ...original, steps: changeSteps(h.store, 'com.pulse.focused') }),
    },
  });
  const next = current(h.store);
  fireEvent.keyDown(input, { key: 'z', metaKey: true });
  expect(current(h.store)).toEqual(original);
  expect(input).toHaveFocus();
  fireEvent.keyDown(input, { key: 'Z', metaKey: true, shiftKey: true });
  expect(current(h.store)).toEqual(next);
  expect(input).toHaveFocus();
  expect(monaco.create).toHaveBeenCalledTimes(1);
  expect(monaco.dispose).not.toHaveBeenCalled();
});

test.each(['前置（自然语言）', '执行文本'])(
  'shared history keeps %s focused for consecutive keyboard undo and redo',
  async (name) => {
    const h = await setup({ initialView: 'three' });
    const original = current(h.store);
    const input = screen.getByRole('textbox', { name });
    input.focus();
    const value =
      name === '执行文本'
        ? JSON.stringify(changeSteps(h.store, 'com.pulse.keep-focus'))
        : '保留焦点的前置说明';
    fireEvent.change(input, { target: { value } });
    const next = current(h.store);
    fireEvent.keyDown(input, { key: 'z', metaKey: true });
    expect(current(h.store)).toEqual(original);
    expect(input).toHaveFocus();
    fireEvent.keyDown(input, { key: 'Z', metaKey: true, shiftKey: true });
    expect(current(h.store)).toEqual(next);
    expect(input).toHaveFocus();
  },
);

test('a net-zero segment cannot resume an older segment with the same group token after undo and redo', async () => {
  const h = await setup();
  const group = {};
  const edit = (timeout: number) => {
    const expected = current(h.store);
    expect(
      h.store.editDraft(
        { ...expected, steps: [{ ...expected.steps[0], timeout_ms: timeout }] },
        expected,
        group,
      ),
    ).toBe(true);
  };
  act(() => {
    edit(1001);
    expect(h.store.undo()).toBe(true);
    expect(h.store.redo()).toBe(true);
    edit(2001);
    edit(1001);
    edit(3001);
    expect(h.store.undo()).toBe(true);
  });
  expect(current(h.store).steps[0].timeout_ms).toBe(1001);
});

test('shared textarea history restores a backward selection instead of moving the caret to the end', async () => {
  const original = copyDraftCase({
    ...controlFlowCase,
    case_id: 'starter',
    original_text: writeThreeSectionText({
      preconditions: 'Existing precondition text',
      steps: 'Existing execution text',
      checks: 'Existing expected text',
    }),
  });
  const h = await setup({ original, initialView: 'three' });
  const input = screen.getByRole('textbox', { name: '前置（自然语言）' }) as HTMLTextAreaElement;
  input.focus();
  fireEvent.change(input, { target: { value: 'Updated precondition text' } });
  input.setSelectionRange(2, 8, 'backward');
  const next = current(h.store);
  fireEvent.keyDown(input, { key: 'z', metaKey: true });
  expect(input).toHaveValue('Existing precondition text');
  expect([input.selectionStart, input.selectionEnd, input.selectionDirection]).toEqual([
    2,
    8,
    'backward',
  ]);
  fireEvent.keyDown(input, { key: 'Z', metaKey: true, shiftKey: true });
  expect(current(h.store)).toEqual(next);
  expect([input.selectionStart, input.selectionEnd, input.selectionDirection]).toEqual([
    2,
    8,
    'backward',
  ]);
});

test('lazy Monaco takeover keeps a focused fallback selection and does not steal focus from a tab', async () => {
  const h = await setup({ mount: false });
  const view = render(<CaseDraftPanel store={h.store} initialView="json" />);
  const fallback = screen.getByRole('textbox', { name: '用例 JSON 文本' }) as HTMLTextAreaElement;
  fallback.focus();
  fallback.setSelectionRange(3, 12, 'backward');
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const editor = screen.getByRole('textbox', { name: '用例 JSON 文本' }) as HTMLTextAreaElement;
  expect(editor).not.toBe(fallback);
  expect(editor).toHaveFocus();
  expect([editor.selectionStart, editor.selectionEnd, editor.selectionDirection]).toEqual([
    3,
    12,
    'backward',
  ]);
  view.unmount();
  render(<CaseDraftPanel store={h.store} initialView="json" />);
  const tab = screen.getByRole('tab', { name: 'JSON' });
  tab.focus();
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(2));
  expect(tab).toHaveFocus();
});

test('Monaco history routing observes the current pending buffer rather than its initial render', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const options = monaco.create.mock.calls[0]?.[1] as MountOptions;
  const before = current(h.store);
  expect(options.hasPendingText()).toBe(false);
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: '{' },
  });
  expect(options.hasPendingText()).toBe(true);
  expect(current(h.store)).toBe(before);
  await userEvent.click(screen.getByRole('button', { name: '恢复已保存文本' }));
  expect(options.hasPendingText()).toBe(false);
});

test('step form history returns focus to its row so keyboard undo and redo can continue', async () => {
  const h = await setup();
  const original = current(h.store);
  await userEvent.click(screen.getByRole('button', { name: '编辑步骤 launch' }));
  const input = screen.getByRole('textbox', { name: '应用 ID' });
  input.focus();
  fireEvent.change(input, { target: { value: 'com.pulse.first' } });
  fireEvent.blur(input);
  const first = current(h.store);
  input.focus();
  fireEvent.change(input, { target: { value: 'com.pulse.second' } });
  fireEvent.keyDown(input, { key: 'z', metaKey: true });
  expect(current(h.store)).toEqual(first);
  const row = screen.getByRole('button', { name: '编辑步骤 launch' }).closest('li');
  if (!row) throw new Error('missing step row');
  expect(row).toHaveFocus();
  fireEvent.keyDown(row, { key: 'z', metaKey: true });
  expect(current(h.store)).toEqual(original);
  fireEvent.keyDown(
    screen.getByRole('button', { name: '编辑步骤 launch' }).closest('li') as HTMLElement,
    { key: 'Z', metaKey: true, shiftKey: true },
  );
  expect(current(h.store)).toEqual(first);
});

test('multiple Monaco change events before a React commit use the latest accepted base and one shared history group', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const options = monaco.create.mock.calls[0]?.[1] as MountOptions;
  const original = current(h.store);
  act(() => {
    options.onChange(
      JSON.stringify({ ...original, steps: [{ ...original.steps[0], timeout_ms: 1234 }] }),
    );
    options.onChange(
      JSON.stringify({ ...original, steps: [{ ...original.steps[0], timeout_ms: 2345 }] }),
    );
  });
  expect(current(h.store).steps[0].timeout_ms).toBe(2345);
  expect(options.hasPendingText()).toBe(false);
  expect(screen.getByRole('status', { name: 'JSON 校验状态' })).toHaveTextContent('结构校验通过');
  act(() => {
    expect(h.store.undo()).toBe(true);
  });
  expect(current(h.store)).toEqual(original);
});

test('Monaco pending history routing updates synchronously within one batch of invalid and valid edits', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const options = monaco.create.mock.calls[0]?.[1] as MountOptions;
  const original = current(h.store);
  act(() => {
    options.onChange('{');
    expect(options.hasPendingText()).toBe(true);
    options.onChange(
      JSON.stringify({ ...original, steps: [{ ...original.steps[0], timeout_ms: 4321 }] }),
    );
    expect(options.hasPendingText()).toBe(false);
    options.onUndo();
  });
  expect(current(h.store)).toEqual(original);
});

test('passes exact generated-schema ranges to Monaco markers', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const before = current(h.store);
  const text = JSON.stringify({
    ...before,
    steps: [{ type: 'action', id: 'launch', action: { kind: 'launch', app_id: 42 } }],
  });
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: text },
  });
  expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveAttribute(
    'data-marker-offset',
    String(text.indexOf('42')),
  );
  expect(screen.getByRole('alert')).toHaveTextContent('Case/steps/0/action/app_id');
  expect(current(h.store)).toBe(before);
});

test('loads Monaco only on first JSON visit and retains its model and rejected buffer while hidden', async () => {
  await setup();
  expect(monaco.create).not.toHaveBeenCalled();
  await switchView('三段式');
  expect(monaco.create).not.toHaveBeenCalled();
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: '{' },
  });
  await switchView('步骤');
  await switchView('JSON');
  expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveValue('{');
  expect(monaco.create).toHaveBeenCalledTimes(1);
  expect(monaco.dispose).not.toHaveBeenCalled();
});

test('keeps text edits made during lazy loading and provides a usable fallback after loader failure', async () => {
  const h = await setup({ mount: false });
  monaco.create.mockImplementationOnce(() => {
    throw new Error('synthetic load failure');
  });
  render(<CaseDraftPanel store={h.store} initialView="json" />);
  const next = { ...current(h.store), steps: changeSteps(h.store, 'com.pulse.loading') };
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: JSON.stringify(next) },
  });
  await screen.findByText(/Monaco 加载失败/);
  expect(current(h.store)).toEqual(next);
  expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveValue(JSON.stringify(next));
  expect(screen.getByRole('status', { name: 'JSON 校验状态' })).toHaveTextContent('结构校验通过');
});

test('shows saving failure honestly and does not replace the original or confirmed draft', async () => {
  const h = await setup({
    initialView: 'json',
    storage: {
      getItem: () => null,
      setItem: () => {
        throw new Error('quota exceeded');
      },
    },
  });
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  const before = current(h.store);
  const text = JSON.stringify({ ...before, steps: changeSteps(h.store, 'com.pulse.unsaved') });
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: text },
  });
  expect(current(h.store)).toBe(before);
  expect(screen.getByText('草稿未确认保存')).toBeVisible();
  expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveValue(text);
  expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toHaveAttribute('readonly');
  expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: '重新读取用例草稿' }));
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).not.toHaveAttribute('readonly'),
  );
  expect(
    JSON.parse(
      (screen.getByRole('textbox', { name: '用例 JSON 文本' }) as HTMLTextAreaElement).value,
    ),
  ).toEqual(before);
});

test('clears rejected buffers and disposes Monaco on scope changes without accepting late callbacks', async () => {
  const h = await setup({ initialView: 'json' });
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(1));
  const oldOptions = monaco.create.mock.calls[0]?.[1] as MountOptions;
  const old = current(h.store);
  fireEvent.change(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    target: { value: '{' },
  });
  await act(async () => {
    h.projects.select('other');
    await h.store.load();
  });
  expect(monaco.dispose).toHaveBeenCalledTimes(1);
  expect(current(h.store).case_id).toBe('other');
  act(() =>
    oldOptions.onChange(JSON.stringify({ ...old, steps: changeSteps(h.store, 'com.pulse.late') })),
  );
  expect(current(h.store).steps[0]).toHaveProperty('action.app_id', 'com.pulse.target');
  expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
  await switchView('JSON');
  await waitFor(() => expect(monaco.create).toHaveBeenCalledTimes(2));
  expect(
    JSON.parse(
      (screen.getByRole('textbox', { name: '用例 JSON 文本' }) as HTMLTextAreaElement).value,
    ),
  ).toEqual(current(h.store));
});

test('uses the shared history for both renderer and Monaco keyboard shortcuts without double undo', async () => {
  const h = await setup({ initialView: 'three' });
  const original = current(h.store);
  const input = screen.getByRole('textbox', { name: '执行文本' });
  fireEvent.change(input, {
    target: { value: JSON.stringify(changeSteps(h.store, 'com.pulse.first')) },
  });
  fireEvent.blur(input);
  const first = current(h.store);
  fireEvent.change(input, {
    target: { value: JSON.stringify(changeSteps(h.store, 'com.pulse.second')) },
  });
  fireEvent.blur(input);
  const second = current(h.store);
  fireEvent.keyDown(screen.getByRole('tab', { name: '三段式' }), { key: 'z', metaKey: true });
  expect(current(h.store)).toEqual(first);
  fireEvent.keyDown(screen.getByRole('tab', { name: '三段式' }), {
    key: 'Z',
    ctrlKey: true,
    shiftKey: true,
  });
  expect(current(h.store)).toEqual(second);
  await switchView('JSON');
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toBeVisible(),
  );
  fireEvent.keyDown(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    key: 'z',
    ctrlKey: true,
  });
  expect(current(h.store)).toEqual(first);
  await waitFor(() =>
    expect(screen.getByRole('textbox', { name: '用例 JSON 文本' })).toBeVisible(),
  );
  fireEvent.keyDown(screen.getByRole('textbox', { name: '用例 JSON 文本' }), {
    key: 'z',
    metaKey: true,
  });
  expect(current(h.store)).toEqual(original);
});

test('jumps from a nested JSON step to the matching highlighted keyboard-focusable step row', async () => {
  await setup({
    original: copyDraftCase({ ...controlFlowCase, case_id: 'starter' }),
    initialView: 'json',
  });
  await waitFor(() => expect(monaco.create).toHaveBeenCalled());
  expect(screen.getByRole('button', { name: '跳转到对应步骤' })).toBeDisabled();
  await userEvent.click(screen.getByRole('combobox', { name: 'JSON 对应步骤' }));
  await userEvent.click(screen.getByRole('option', { name: 'tap-banner' }));
  await userEvent.click(screen.getByRole('button', { name: '跳转到对应步骤' }));
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveAttribute('aria-selected', 'true');
  const row = screen.getByRole('button', { name: '编辑步骤 tap-banner' }).closest('li');
  expect(row).toHaveAttribute('data-selected', 'true');
  expect(row).toHaveFocus();
});

test.each(['three', 'json'] as const)(
  'provides read-only, keyboard-reachable %s content with no save or execution claims',
  async (initialView) => {
    const h = await setup({ initialView, storage: null });
    if (initialView === 'json') await waitFor(() => expect(monaco.create).toHaveBeenCalled());
    const texts = screen.getAllByRole('textbox');
    texts.forEach((input) => expect(input).toHaveAttribute('readonly'));
    expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /运行|执行|提交/ })).not.toBeInTheDocument();
    expect(h.store.getSnapshot().persistence).toBe('error');
    expect(h.view).not.toBeNull();
    const results = await axe.run(h.view?.container as HTMLElement, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  },
);
