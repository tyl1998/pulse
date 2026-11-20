import type { editor } from 'monaco-editor/editor/editor.api.js';
import { beforeEach, expect, test, vi } from 'vitest';
import caseSchema from '@pulse/schema/json/case.schema.json';
import { createMonacoCaseEditor } from './monacoCaseEditor';

const sdk = vi.hoisted(() => ({
  create: vi.fn(),
  createModel: vi.fn(),
  markers: vi.fn(),
  configure: vi.fn(),
  editorWorker: vi.fn(),
  jsonWorker: vi.fn(),
}));
vi.mock('monaco-editor/editor/editor.api.js', () => ({
  editor: { create: sdk.create, createModel: sdk.createModel, setModelMarkers: sdk.markers },
  KeyMod: { CtrlCmd: 2048, Shift: 1024 },
  KeyCode: { KeyZ: 56, KeyY: 55 },
  MarkerSeverity: { Error: 8 },
  Uri: { parse: (value: string) => value },
  Selection: class {
    constructor(
      readonly selectionStartLineNumber: number,
      readonly selectionStartColumn: number,
      readonly positionLineNumber: number,
      readonly positionColumn: number,
    ) {}
  },
}));
vi.mock('monaco-editor/languages/features/json/register.js', () => ({
  jsonDefaults: { setDiagnosticsOptions: sdk.configure },
}));
vi.mock('monaco-editor/editor/browser/coreCommands.js', () => ({}));
vi.mock('monaco-editor/editor/editor.worker.js?worker', () => ({
  default: vi.fn(function MockEditorWorker() {
    sdk.editorWorker();
  }),
}));
vi.mock('monaco-editor/languages/features/json/json.worker.js?worker', () => ({
  default: vi.fn(function MockJsonWorker() {
    sdk.jsonWorker();
  }),
}));

beforeEach(() => {
  sdk.create.mockReset();
  sdk.createModel.mockReset();
  sdk.markers.mockClear();
  sdk.editorWorker.mockClear();
  sdk.jsonWorker.mockClear();
});

function setup() {
  const text = '{"steps":[{"id":"launch","type":"action"}]}';
  let value = text;
  let contentChanged: () => void = () => undefined;
  const subscriptions: { dispose: ReturnType<typeof vi.fn> }[] = [];
  const subscribe = () => {
    const subscription = { dispose: vi.fn() };
    subscriptions.push(subscription);
    return subscription;
  };
  const model = {
    getValue: () => value,
    setValue: vi.fn((next: string) => {
      value = next;
      contentChanged();
    }),
    getOffsetAt: ({ column }: { column: number }) => column - 1,
    getPositionAt: (offset: number) => ({ lineNumber: 1, column: offset + 1 }),
    onDidChangeContent: (listener: () => void) => {
      contentChanged = listener;
      return subscribe();
    },
    dispose: vi.fn(),
  };
  const actions: editor.IActionDescriptor[] = [];
  const selection = [{ startLineNumber: 1, startColumn: 3, endLineNumber: 1, endColumn: 8 }];
  const instance = {
    getPosition: () => ({ lineNumber: 1, column: value.indexOf('launch') + 1 }),
    getSelections: () => selection,
    setSelections: vi.fn(),
    setSelection: vi.fn(),
    onDidChangeCursorPosition: subscribe,
    onDidBlurEditorText: subscribe,
    addAction: (descriptor: editor.IActionDescriptor) => {
      actions.push(descriptor);
      return subscribe();
    },
    updateOptions: vi.fn(),
    trigger: vi.fn(),
    focus: vi.fn(),
    layout: vi.fn(),
    dispose: vi.fn(),
  };
  sdk.createModel.mockReturnValue(model);
  sdk.create.mockReturnValue(instance);
  const options = {
    text,
    readOnly: false,
    onChange: vi.fn(),
    onBlur: vi.fn(),
    onCursorStep: vi.fn(),
    onUndo: vi.fn(),
    onRedo: vi.fn(),
    hasPendingText: vi.fn(() => false),
  };
  const mount = () => createMonacoCaseEditor(document.createElement('div'), options);
  return { mount, model, instance, actions, subscriptions, selection, options };
}

test('uses the generated schema with local workers and no remote schema requests', async () => {
  vi.resetModules();
  await import('./monacoCaseEditor');
  expect(sdk.configure).toHaveBeenCalledWith({
    validate: true,
    enableSchemaRequest: false,
    schemaValidation: 'error',
    comments: 'error',
    trailingCommas: 'error',
    schemas: [
      { uri: caseSchema.$id, fileMatch: ['inmemory://pulse/case-drafts/*'], schema: caseSchema },
    ],
  });
  self.MonacoEnvironment?.getWorker?.('', 'json');
  self.MonacoEnvironment?.getWorker?.('', 'editor');
  expect(sdk.jsonWorker).toHaveBeenCalledTimes(1);
  expect(sdk.editorWorker).toHaveBeenCalledTimes(1);
});

test('registers shared history only for editor text focus and releases every registration', () => {
  const h = setup();
  const handle = h.mount();
  expect(sdk.create).toHaveBeenCalledWith(
    expect.any(HTMLElement),
    expect.objectContaining({ editContext: false }),
  );
  expect(h.actions).toMatchObject([
    { id: 'pulse.case-draft.undo', precondition: 'editorTextFocus', keybindings: [2048 | 56] },
    {
      id: 'pulse.case-draft.redo',
      precondition: 'editorTextFocus',
      keybindings: [2048 | 1024 | 56, 2048 | 55],
    },
  ]);
  expect(h.instance.focus).not.toHaveBeenCalled();
  handle.dispose();
  expect(h.subscriptions).toHaveLength(5);
  h.subscriptions.forEach((subscription) => expect(subscription.dispose).toHaveBeenCalledTimes(1));
  expect(h.instance.dispose).toHaveBeenCalledTimes(1);
  expect(h.model.dispose).toHaveBeenCalledTimes(1);
});

test('pending text uses local model undo and redo instead of modifying confirmed history', async () => {
  const h = setup();
  const handle = h.mount();
  const undo = h.actions[0];
  const redo = h.actions[1];
  if (!undo || !redo) throw new Error('missing history actions');
  const instance = h.instance as unknown as editor.ICodeEditor;
  await undo.run(instance);
  await redo.run(instance);
  expect(h.options.onUndo).toHaveBeenCalledTimes(1);
  expect(h.options.onRedo).toHaveBeenCalledTimes(1);
  expect(h.instance.trigger).not.toHaveBeenCalled();
  h.options.hasPendingText.mockReturnValue(true);
  await undo.run(instance);
  await redo.run(instance);
  expect(h.instance.trigger).toHaveBeenCalledWith('pulse-unconfirmed', 'undo', null);
  expect(h.instance.trigger).toHaveBeenCalledWith('pulse-unconfirmed', 'redo', null);
  expect(h.options.onUndo).toHaveBeenCalledTimes(1);
  expect(h.options.onRedo).toHaveBeenCalledTimes(1);
  handle.dispose();
});

test('canonical replacements preserve selections, suppress saves and keep later user input live', () => {
  const h = setup();
  const handle = h.mount();
  const next = '{"steps":[{"id":"launch","type":"action","timeout_ms":1234}]}';
  handle.setText(next);
  expect(h.model.getValue()).toBe(next);
  expect(h.options.onChange).not.toHaveBeenCalled();
  expect(h.instance.setSelections).toHaveBeenCalledWith(h.selection);
  handle.setText(next);
  expect(h.model.setValue).toHaveBeenCalledTimes(1);
  h.model.setValue('{');
  expect(h.options.onChange).toHaveBeenCalledWith('{');
  handle.dispose();
});

test('focus handoff translates textarea offsets and preserves backward selection', () => {
  const h = setup();
  const handle = h.mount();
  handle.focusText(2, 5, true);
  expect(h.instance.setSelection).toHaveBeenCalledWith({
    selectionStartLineNumber: 1,
    selectionStartColumn: 6,
    positionLineNumber: 1,
    positionColumn: 3,
  });
  expect(h.instance.focus).toHaveBeenCalledTimes(1);
  handle.dispose();
});

test('renderer creation failure disposes its model without registering commands', () => {
  const h = setup();
  sdk.create.mockImplementationOnce(() => {
    throw new Error('synthetic renderer failure');
  });
  expect(h.mount).toThrow('synthetic renderer failure');
  expect(h.model.dispose).toHaveBeenCalledTimes(1);
  expect(h.actions).toHaveLength(0);
});

test('models have unique URIs and custom error markers never clear the worker owner', () => {
  const h = setup();
  const first = h.mount();
  const second = h.mount();
  expect(sdk.createModel.mock.calls[0]?.[2]).not.toBe(sdk.createModel.mock.calls[1]?.[2]);
  first.setIssue({ message: 'invalid field', offset: 4, length: 2 });
  expect(sdk.markers).toHaveBeenLastCalledWith(h.model, 'pulse-case-draft', [
    {
      severity: 8,
      message: 'invalid field',
      startLineNumber: 1,
      startColumn: 5,
      endLineNumber: 1,
      endColumn: 7,
    },
  ]);
  first.setIssue(null);
  expect(sdk.markers).toHaveBeenLastCalledWith(h.model, 'pulse-case-draft', []);
  first.dispose();
  second.dispose();
});
