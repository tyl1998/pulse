import type { Case, Checkpoint, Step } from '@pulse/schema';
import { useEffect, useEffectEvent, useId, useLayoutEffect, useRef, useState } from 'react';
import { Badge, Button, Select } from '../../shared/ui';
import { caseDraftStorageKey, type CaseDraftStore } from './caseDraftStore';
import { assertDraftCase, caseSchemaIssues, sameJson } from './caseDraftValidation';
import { useCaseProblemReporter, useCaseProblems, type LocalProblem } from './CaseProblems';
import type { CaseProblem } from './caseDiagnostics';
import {
  caseStepAtOffset,
  caseStepPositions,
  caseTextIssue,
  caseTextRange,
  parseCaseText,
  readThreeSectionText,
  writeThreeSectionText,
  type CaseTextIssue,
} from './caseText';
import type { MonacoCaseEditor } from './monacoCaseEditor';
import { findStep, stepLabel } from './stepEditing';

type SectionField = 'preconditions' | 'steps' | 'checks';

function useCaseTextDraft(
  document: Case,
  store: CaseDraftStore,
  field?: SectionField | 'original_text',
  active = true,
) {
  type TextBuffer = {
    text: string;
    base: Case;
    issue: CaseTextIssue | null;
  };
  const [buffer, setBuffer] = useState<TextBuffer | null>(null);
  const bufferRef = useRef<TextBuffer | null>(null);
  const updateBuffer = (next: TextBuffer | null) => {
    bufferRef.current = next;
    setBuffer(next);
  };
  useLayoutEffect(() => {
    const current = bufferRef.current;
    if (
      current?.issue === null &&
      current.base !== document &&
      store.getSnapshot().draft === document
    )
      bufferRef.current = null;
  }, [document, store]);
  const group = useRef<object>({});
  useEffect(() => {
    group.current = {};
  }, [active]);
  const [scopeKey] = useState(() => {
    const scope = store.getSnapshot().scope;
    return scope ? caseDraftStorageKey(scope) : null;
  });
  const select = (current: Case) => (field ? current[field] : current);
  // Accepted text follows external edits; rejected text remains local until explicitly restored.
  const acceptedElsewhere =
    buffer &&
    buffer.base !== document &&
    buffer.issue === null &&
    store.getSnapshot().draft === document;
  if (acceptedElsewhere) setBuffer(null);
  const retained = acceptedElsewhere ? null : buffer;
  const text =
    retained?.text ??
    (field === 'original_text'
      ? (document.original_text ?? '')
      : JSON.stringify(select(document), null, 2));
  const conflict = retained && !sameJson(select(retained.base), select(document));
  const issue = conflict
    ? caseTextIssue(
        text,
        new Error('此内容已被其他视图修改；未保存文本已保留，请恢复已保存文本后再编辑。'),
        field,
      )
    : (retained?.issue ?? null);
  const change = (text: string) => {
    // Monaco can emit another change before React commits the previous buffer update.
    const base = bufferRef.current?.base ?? document;
    try {
      const state = store.getSnapshot();
      if (!state.scope || caseDraftStorageKey(state.scope) !== scopeKey)
        throw new Error('选中用例已变化，未将旧文本写入新用例');
      const expected = state.draft;
      if (!state.canInsert || !expected) throw new Error(state.reason ?? '草稿暂不可编辑');
      if (!sameJson(select(base), select(expected)))
        throw new Error('此内容已被其他视图修改，请恢复已保存文本后再编辑');
      const parsed = field === 'original_text' ? text : parseCaseText(text);
      const next: unknown = field ? { ...expected, [field]: parsed } : parsed;
      assertDraftCase(next);
      const edit =
        field === undefined || field === 'original_text' ? store.editCaseText : store.editDraft;
      if (!edit(next, expected, group.current))
        throw new Error(store.getSnapshot().error ?? '草稿未确认保存');
      updateBuffer({ text, base: store.getSnapshot().draft ?? expected, issue: null });
    } catch (error) {
      updateBuffer({ text, base, issue: caseTextIssue(text, error, field) });
    }
  };
  const problems: LocalProblem[] = [];
  if (issue) {
    try {
      if (field !== 'original_text') {
        const parsed = parseCaseText(text);
        const candidate = field ? { ...document, [field]: parsed } : parsed;
        for (const found of caseSchemaIssues(candidate))
          problems.push({
            ...found,
            category: 'schema',
            severity: 'error',
            target: { view: field ? 'three' : 'json', ...caseTextRange(text, found.path, field) },
          });
      }
    } catch {
      // The retained parse error already carries the exact offending text range.
    }
    if (!problems.length || conflict)
      problems.push({
        category:
          /JSON|结构无效|重复(?:步骤|检查点) ID/.test(issue.message) && !conflict
            ? 'schema'
            : 'editing',
        severity: 'error',
        path: issue.path ?? (field ? `Case/${field}` : 'Case'),
        message: issue.message,
        target: { view: field ? 'three' : 'json', offset: issue.offset, length: issue.length },
      });
  }
  return {
    text,
    issue,
    problems,
    hasPendingText: () => !!bufferRef.current?.issue,
    change,
    blur: () => {
      group.current = {};
    },
    restore: () => {
      group.current = {};
      updateBuffer(null);
    },
  };
}

function focusTextArea(
  input: HTMLTextAreaElement | null,
  problem: CaseProblem,
  text: string,
  section?: string,
) {
  if (!input) return;
  const range =
    problem.target.offset === undefined
      ? caseTextRange(text, problem.path, section)
      : { offset: problem.target.offset, length: problem.target.length ?? 1 };
  input.focus({ preventScroll: true });
  input.setSelectionRange(range.offset, range.offset + range.length);
  input.scrollIntoView({ block: 'nearest' });
}

function needsAi(item: Step | Checkpoint): boolean {
  if ('type' in item) {
    if (item.type === 'ai_step') return true;
    if (item.type === 'check') return needsAi(item.checkpoint);
    if (item.type === 'if')
      return (
        item.condition.operator === 'semantic' || [...item.then, ...(item.else ?? [])].some(needsAi)
      );
    if (item.type === 'loop')
      return item.condition?.operator === 'semantic' || item.steps.some(needsAi);
    return 'condition' in item && item.condition.operator === 'semantic';
  }
  return item.condition.operator === 'semantic';
}

function SectionText({
  field,
  label,
  document,
  store,
  canEdit,
  active,
}: {
  field: SectionField;
  label: string;
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
  active: boolean;
}) {
  const input = useRef<HTMLTextAreaElement>(null);
  const draft = useCaseTextDraft(document, store, field, active);
  useCaseProblemReporter(store, draft.problems, `${label}文本`, 'three', (problem) =>
    focusTextArea(input.current, problem, draft.text, field),
  );
  const id = useId();
  const items: (Step | Checkpoint)[] = document[field];
  return (
    <section
      className="case-editor-section"
      aria-label={`${label}分区`}
      data-pending-text={!!draft.issue}
    >
      <div className="case-draft-header">
        <label htmlFor={id}>
          <strong>{label}</strong> <code>{field}</code>
        </label>
        <Badge tone={draft.issue ? 'warning' : 'neutral'}>
          {draft.issue ? '文本待确认' : `${items.length} 项已保存结构`}
        </Badge>
      </div>
      <textarea
        ref={input}
        id={id}
        aria-label={`${label}文本`}
        aria-describedby={`${id}-note`}
        aria-invalid={!!draft.issue}
        className="ui-input case-editor-textarea case-editor-section-text"
        value={draft.text}
        readOnly={!canEdit}
        spellCheck={false}
        rows={field === 'steps' ? 12 : 6}
        onChange={(event) => draft.change(event.target.value)}
        onBlur={draft.blur}
      />
      <p id={`${id}-note`} className="case-draft-note">
        编辑此分区的 JSON 数组；有效结构自动保存。自然语言不会被自动转换成动作或断言。
      </p>
      {draft.issue ? (
        <>
          <p className="case-draft-error" role="alert">
            {draft.issue.message}
          </p>
          <Button size="sm" variant="ghost" onClick={draft.restore}>
            恢复已保存文本
          </Button>
        </>
      ) : null}
      {active && items.length ? (
        <ul className="case-editor-section-summary" aria-label={`${label}已保存结构`}>
          {items.map((item) => (
            <li key={item.id}>
              <code>{item.id}</code>
              <span>
                {'type' in item
                  ? stepLabel(item)
                  : `${item.condition.operator} / ${item.condition.source} / ${item.required ? '必须通过' : '可选'}`}
              </span>
              <Badge tone={needsAi(item) ? 'accent' : 'neutral'}>
                {needsAi(item) ? '需 AI' : '结构已确认'}
              </Badge>
            </li>
          ))}
        </ul>
      ) : active ? (
        <p className="case-draft-note">
          {field === 'preconditions'
            ? '未定义前置检查，不会自动准备或清理数据。'
            : '预期尚未补齐，不等于业务通过。'}
        </p>
      ) : null}
    </section>
  );
}

export function ThreeSectionView(props: {
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
  active: boolean;
}) {
  return (
    <div className="case-editor-three-sections">
      <p className="case-draft-note">
        前置 / 执行 / 预期与步骤和 JSON
        视图共用一份草稿。自然语言是待解析创作资料；下方结构仍需人工确认。
      </p>
      <AuthoringText
        document={props.document}
        store={props.store}
        canEdit={props.canEdit}
        active={props.active}
      />
      <SectionText {...props} field="preconditions" label="前置" />
      <SectionText {...props} field="steps" label="执行" />
      <SectionText {...props} field="checks" label="预期" />
    </div>
  );
}

function AuthoringText({
  document,
  store,
  canEdit,
  active,
}: {
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
  active: boolean;
}) {
  const root = useRef<HTMLElement>(null);
  const lastInput = useRef<HTMLTextAreaElement | null>(null);
  const draft = useCaseTextDraft(document, store, 'original_text', active);
  useCaseProblemReporter(store, draft.problems, '三段式创作输入', 'three', () => {
    const input = lastInput.current ?? root.current?.querySelector('textarea');
    input?.focus({ preventScroll: true });
    input?.scrollIntoView({ block: 'nearest' });
  });
  const sections = readThreeSectionText(draft.text);
  const text = sections ?? { preconditions: '', steps: '', checks: '' };
  return (
    <section
      ref={root}
      className="case-editor-section"
      aria-label="三段式创作输入"
      data-pending-text={!!draft.issue}
    >
      <div className="case-draft-header">
        <strong>三段式创作输入</strong>
        <Badge tone="warning">自然语言待解析</Badge>
      </div>
      {!sections && draft.text ? (
        <p className="case-draft-note">
          现有草稿文本未拆分，已保留在 JSON 的 original_text
          中。编辑三段式会替换草稿创作输入，不修改来源原文。
        </p>
      ) : null}
      {(
        [
          ['preconditions', '前置'],
          ['steps', '执行'],
          ['checks', '预期'],
        ] as const
      ).map(([field, label]) => (
        <label key={field} className="case-editor-field">
          <span>{label}（自然语言）</span>
          <textarea
            className="ui-input case-editor-textarea"
            aria-label={`${label}（自然语言）`}
            aria-invalid={!!draft.issue}
            value={text[field]}
            readOnly={!canEdit}
            rows={3}
            onChange={(event) => {
              lastInput.current = event.currentTarget;
              draft.change(writeThreeSectionText({ ...text, [field]: event.target.value }));
            }}
            onBlur={draft.blur}
          />
        </label>
      ))}
      <p className="case-draft-note">
        输入只保存为草稿
        original_text，不自动生成准备动作、步骤或检查点，也不请求模型。结构化内容与自然语言需人工核对。
      </p>
      {draft.issue ? (
        <>
          <p className="case-draft-error" role="alert">
            {draft.issue.message}
          </p>
          <Button size="sm" variant="ghost" onClick={draft.restore}>
            恢复已保存创作输入
          </Button>
        </>
      ) : null}
    </section>
  );
}

export function CaseJsonView({
  document,
  store,
  canEdit,
  active,
  onJump,
  onMissingProblem,
}: {
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
  active: boolean;
  onJump: (id: string) => void;
  onMissingProblem?: (problem: CaseProblem) => void;
}) {
  const draft = useCaseTextDraft(document, store, undefined, active);
  const host = useRef<HTMLDivElement>(null);
  const instance = useRef<MonacoCaseEditor | null>(null);
  const fallback = useRef<HTMLTextAreaElement>(null);
  const handoff = useRef<{ start: number; end: number; backward: boolean } | null>(null);
  const [opened, setOpened] = useState(active);
  if (active && !opened) setOpened(true);
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [cursorStep, setCursorStep] = useState<string | null>(null);
  const problemContext = useCaseProblems(store);
  const navigation = problemContext?.focus;
  const focusedProblem = useRef<number | null>(null);
  useLayoutEffect(() => {
    instance.current?.setText(draft.text);
  }, [draft.text, ready]);
  const focusProblem = (problem: CaseProblem) => {
    const range =
      problem.target.offset === undefined
        ? caseTextRange(
            draft.text,
            problem.path,
            undefined,
            problem.target.owner ? undefined : document,
          )
        : { offset: problem.target.offset, length: problem.target.length ?? 1 };
    if ('found' in range && range.found === false) {
      onMissingProblem?.(problem);
      return;
    }
    if (instance.current) {
      instance.current.layout();
      instance.current.focusText(range.offset, range.offset + range.length, false);
      host.current?.scrollIntoView({ block: 'nearest' });
    } else
      focusTextArea(
        fallback.current,
        { ...problem, target: { ...problem.target, offset: range.offset, length: range.length } },
        draft.text,
      );
  };
  useCaseProblemReporter(store, draft.problems, 'JSON', 'json', (problem) => focusProblem(problem));
  const focusNavigation = useEffectEvent((problem: CaseProblem) => focusProblem(problem));
  useLayoutEffect(() => {
    if (
      !active ||
      !navigation ||
      navigation.problem.target.owner ||
      navigation.problem.target.view !== 'json' ||
      focusedProblem.current === navigation.sequence
    )
      return;
    focusedProblem.current = navigation.sequence;
    focusNavigation(navigation.problem);
  }, [active, ready, navigation]);
  const current = useEffectEvent(() => ({
    text: draft.text,
    issue: draft.issue,
    readOnly: !canEdit,
    pending: draft.hasPendingText(),
  }));
  const change = useEffectEvent((text: string) => draft.change(text));
  const blur = useEffectEvent(() => draft.blur());
  const undo = useEffectEvent(() => {
    store.undo();
  });
  const redo = useEffectEvent(() => {
    store.redo();
  });
  const captureFocus = useEffectEvent(() => {
    const input = fallback.current;
    if (active && input && globalThis.document.activeElement === input) {
      handoff.current = {
        start: input.selectionStart,
        end: input.selectionEnd,
        backward: input.selectionDirection === 'backward',
      };
    }
  });
  useEffect(() => {
    const node = host.current;
    if (!opened || !node) return;
    let cancelled = false;
    let mounted: MonacoCaseEditor | null = null;
    void import('./monacoCaseEditor')
      .then((module) => {
        if (cancelled) return;
        mounted = module.createMonacoCaseEditor(node, {
          ...current(),
          onChange: (text) => change(text),
          onBlur: () => blur(),
          onCursorStep: setCursorStep,
          onUndo: () => undo(),
          onRedo: () => redo(),
          hasPendingText: () => current().pending,
        });
        instance.current = mounted;
        mounted.setIssue(current().issue);
        captureFocus();
        setReady(true);
      })
      .catch((error: unknown) => {
        if (!cancelled)
          setLoadError(
            `Monaco 加载失败，已保留文本编辑: ${error instanceof Error ? error.message : String(error)}`,
          );
      });
    return () => {
      cancelled = true;
      mounted?.dispose();
      instance.current = null;
    };
  }, [opened]);
  useLayoutEffect(() => {
    const selection = handoff.current;
    if (!ready || !selection) return;
    handoff.current = null;
    if (active) instance.current?.focusText(selection.start, selection.end, selection.backward);
  }, [ready, active]);
  useEffect(() => {
    const editor = instance.current;
    if (!editor) return;
    editor.setIssue(draft.issue);
    editor.setReadOnly(!canEdit);
    if (active) editor.layout();
  }, [draft.issue, canEdit, ready, active]);
  const positions = caseStepPositions(JSON.stringify(document));
  const canJump = !!cursorStep && !!findStep(document.steps, cursorStep) && !draft.issue;
  return (
    <div className="case-editor-json" data-pending-text={!!draft.issue}>
      <p className="case-draft-note">
        使用生成的 Case Schema。可编辑 steps / locators / preconditions / checks / parameters /
        inputs 和 original_text（草稿创作输入）；身份、来源与来源原文保留不变。
      </p>
      {draft.issue && navigation && !navigation.problem.target.owner ? (
        <p className="case-draft-note">
          已确认草稿的问题按原步骤或检查点 ID
          定位；未保存文本的字段值尚未确认。若对应条目缺失或重复，会返回步骤视图，保留全部未保存文本。
        </p>
      ) : null}
      <div className="case-editor-actions">
        <Select
          size="sm"
          aria-label="JSON 对应步骤"
          placeholder="选择或将光标放到步骤"
          value={cursorStep ?? ''}
          items={positions.map((position) => ({ value: position.id, label: position.id }))}
          onValueChange={setCursorStep}
        />
        <Button
          size="sm"
          variant="ghost"
          disabled={!canJump}
          onClick={() => {
            if (cursorStep) onJump(cursorStep);
          }}
        >
          跳转到对应步骤
        </Button>
      </div>
      {!ready ? (
        <textarea
          ref={fallback}
          aria-label="用例 JSON 文本"
          aria-invalid={!!draft.issue}
          className="ui-input case-editor-textarea case-editor-json-fallback"
          value={draft.text}
          readOnly={!canEdit}
          spellCheck={false}
          rows={20}
          onChange={(event) => {
            draft.change(event.target.value);
            setCursorStep(caseStepAtOffset(event.target.value, event.target.selectionStart));
          }}
          onBlur={draft.blur}
          onClick={(event) =>
            setCursorStep(caseStepAtOffset(draft.text, event.currentTarget.selectionStart))
          }
          onKeyUp={(event) =>
            setCursorStep(caseStepAtOffset(draft.text, event.currentTarget.selectionStart))
          }
        />
      ) : null}
      <div ref={host} className="case-editor-monaco" hidden={!ready} />
      {loadError ? (
        <p className="case-draft-error" role="alert">
          {loadError}
        </p>
      ) : null}
      {!ready && !loadError && opened ? (
        <p className="case-draft-note" role="status">
          正在加载本地 Monaco 编辑器，文本可继续编辑。
        </p>
      ) : null}
      {draft.issue ? (
        <>
          <p className="case-draft-error" role="alert">
            {draft.issue.message}
          </p>
          <Button size="sm" variant="ghost" onClick={draft.restore}>
            恢复已保存文本
          </Button>
        </>
      ) : null}
      <p className="case-draft-note" role="status" aria-label="JSON 校验状态">
        {draft.issue
          ? '文本尚未保存，其他视图仍使用已确认草稿。'
          : '当前文本结构校验通过；不等于全工程引用校验、运行预检或业务通过。'}
      </p>
    </div>
  );
}
