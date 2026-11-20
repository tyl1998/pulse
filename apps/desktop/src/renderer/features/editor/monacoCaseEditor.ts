import {
  editor,
  KeyCode,
  KeyMod,
  MarkerSeverity,
  Selection,
  Uri,
} from 'monaco-editor/editor/editor.api.js';
import 'monaco-editor/editor/browser/coreCommands.js';
import { jsonDefaults } from 'monaco-editor/languages/features/json/register.js';
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import JsonWorker from 'monaco-editor/languages/features/json/json.worker.js?worker';
import caseSchema from '@pulse/schema/json/case.schema.json';
import { caseStepAtOffset, type CaseTextIssue } from './caseText';

// Bundled workers stay on the renderer origin; no CDN, blob worker or unsafe-eval is needed.
self.MonacoEnvironment = {
  getWorker: (_moduleId, label) => (label === 'json' ? new JsonWorker() : new EditorWorker()),
};
jsonDefaults.setDiagnosticsOptions({
  validate: true,
  enableSchemaRequest: false,
  schemaValidation: 'error',
  comments: 'error',
  trailingCommas: 'error',
  schemas: [
    { uri: caseSchema.$id, fileMatch: ['inmemory://pulse/case-drafts/*'], schema: caseSchema },
  ],
});

export interface MonacoCaseEditor {
  setText: (text: string) => void;
  setReadOnly: (readOnly: boolean) => void;
  setIssue: (issue: CaseTextIssue | null) => void;
  focusText: (start: number, end: number, backward: boolean) => void;
  layout: () => void;
  dispose: () => void;
}

let nextModel = 0;

export function createMonacoCaseEditor(
  host: HTMLElement,
  options: {
    text: string;
    readOnly: boolean;
    onChange: (text: string) => void;
    onBlur: () => void;
    onCursorStep: (id: string | null) => void;
    onUndo: () => void;
    onRedo: () => void;
    hasPendingText: () => boolean;
  },
): MonacoCaseEditor {
  const model = editor.createModel(
    options.text,
    'json',
    Uri.parse(`inmemory://pulse/case-drafts/${++nextModel}/case.json`),
  );
  let instance: editor.IStandaloneCodeEditor;
  try {
    instance = editor.create(host, {
      model,
      ariaLabel: '用例 JSON 文本',
      automaticLayout: true,
      // Use the stable textarea input path for shared-history model replacement.
      editContext: false,
      readOnly: options.readOnly,
      minimap: { enabled: false },
      fontSize: 13,
      tabSize: 2,
      wordWrap: 'on',
      scrollBeyondLastLine: false,
      stickyScroll: { enabled: false },
      quickSuggestions: { strings: true, other: true, comments: false },
      renderValidationDecorations: 'on',
    });
  } catch (error) {
    model.dispose();
    throw error;
  }
  let replacing = false;
  const cursorStep = () => {
    const position = instance.getPosition();
    options.onCursorStep(
      position ? caseStepAtOffset(model.getValue(), model.getOffsetAt(position)) : null,
    );
  };
  const subscriptions = [
    model.onDidChangeContent(() => {
      if (!replacing) options.onChange(model.getValue());
      cursorStep();
    }),
    instance.onDidChangeCursorPosition(cursorStep),
    instance.onDidBlurEditorText(options.onBlur),
    instance.addAction({
      id: 'pulse.case-draft.undo',
      label: '撤销已保存草稿',
      precondition: 'editorTextFocus',
      keybindings: [KeyMod.CtrlCmd | KeyCode.KeyZ],
      run: () => {
        if (options.hasPendingText()) instance.trigger('pulse-unconfirmed', 'undo', null);
        else options.onUndo();
      },
    }),
    instance.addAction({
      id: 'pulse.case-draft.redo',
      label: '重做已保存草稿',
      precondition: 'editorTextFocus',
      keybindings: [KeyMod.CtrlCmd | KeyMod.Shift | KeyCode.KeyZ, KeyMod.CtrlCmd | KeyCode.KeyY],
      run: () => {
        if (options.hasPendingText()) instance.trigger('pulse-unconfirmed', 'redo', null);
        else options.onRedo();
      },
    }),
  ];
  return {
    setText: (text) => {
      if (model.getValue() === text) return;
      replacing = true;
      try {
        const selections = instance.getSelections();
        model.setValue(text);
        if (selections) instance.setSelections(selections);
      } finally {
        replacing = false;
      }
      cursorStep();
    },
    setReadOnly: (readOnly) => instance.updateOptions({ readOnly }),
    focusText: (start, end, backward) => {
      const anchor = model.getPositionAt(backward ? end : start);
      const cursor = model.getPositionAt(backward ? start : end);
      instance.setSelection(
        new Selection(anchor.lineNumber, anchor.column, cursor.lineNumber, cursor.column),
      );
      instance.focus();
    },
    setIssue: (issue) => {
      const start = model.getPositionAt(issue?.offset ?? 0);
      const end = model.getPositionAt((issue?.offset ?? 0) + (issue?.length ?? 1));
      editor.setModelMarkers(
        model,
        'pulse-case-draft',
        issue
          ? [
              {
                severity: MarkerSeverity.Error,
                message: issue.message,
                startLineNumber: start.lineNumber,
                startColumn: start.column,
                endLineNumber: end.lineNumber,
                endColumn: end.column,
              },
            ]
          : [],
      );
    },
    layout: () => instance.layout(),
    dispose: () => {
      subscriptions.forEach((subscription) => subscription.dispose());
      instance.dispose();
      model.dispose();
    },
  };
}
