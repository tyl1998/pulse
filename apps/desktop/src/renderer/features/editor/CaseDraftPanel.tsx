import type { Case, Locator } from '@pulse/schema';
import { useLayoutEffect, useRef, useState } from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { Pencil, Redo2, Undo2 } from 'lucide-react';
import { Badge, Button } from '../../shared/ui';
import {
  caseDraftStorageKey,
  type CaseDraftState,
  type CaseDraftStore,
  useCaseDraft,
} from './caseDraftStore';
import { CaseJsonView, ThreeSectionView } from './CaseTextViews';
import { CaseParameterBindings } from './CaseParameterBindings';
import { useCaseProblems } from './CaseProblems';
import { caseStepAtOffset, caseTextRange } from './caseText';
import { DraftForm } from './DraftForm';
import { BindingFields, CheckpointsFields, LocatorFields } from './StepFields';
import { StepList } from './StepList';
import { ROOT_STEPS } from './stepEditing';
import './caseDraft.css';

function CaseCheckpoints({
  field,
  document,
  store,
  canEdit,
}: {
  field: 'preconditions' | 'checks';
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const label = field === 'preconditions' ? '预检查' : '验收检查';
  return (
    <section
      className="case-draft-checkpoints"
      aria-label={label}
      data-case-field={field}
      tabIndex={-1}
    >
      <div className="case-draft-header">
        <strong>
          {label} / {document[field].length} 项
        </strong>
        <Button
          size="sm"
          variant="ghost"
          disabled={!canEdit}
          aria-expanded={editing}
          onClick={() => setEditing(true)}
        >
          <Pencil aria-hidden="true" />
          编辑{label}
        </Button>
      </div>
      {editing ? (
        <DraftForm
          label={`编辑${label}`}
          document={document}
          store={store}
          select={(current) => current[field]}
          replace={(current, value) => ({ ...current, [field]: value })}
          disabled={!canEdit}
          onClose={() => setEditing(false)}
        >
          {(value, change) => (
            <CheckpointsFields value={value} document={document} onChange={change} />
          )}
        </DraftForm>
      ) : document[field].length ? (
        <ul className="case-draft-checkpoint-summary">
          {document[field].map((checkpoint) => (
            <li key={checkpoint.id}>
              <code>{checkpoint.id}</code>
              <span>
                {checkpoint.condition.operator} / {checkpoint.condition.source} /{' '}
                {checkpoint.required ? '必须通过' : '可选'}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function CaseLocators({
  document,
  store,
  canEdit,
}: {
  document: Case;
  store: CaseDraftStore;
  canEdit: boolean;
}) {
  const [editing, setEditing] = useState(false);
  return (
    <section
      className="case-draft-checkpoints"
      aria-label="用例定位器"
      data-case-field="locators"
      tabIndex={-1}
    >
      <div className="case-draft-header">
        <strong>用例定位器 / {Object.keys(document.locators).length} 项</strong>
        <Button
          size="sm"
          variant="ghost"
          disabled={!canEdit}
          aria-expanded={editing}
          onClick={() => setEditing(true)}
        >
          <Pencil aria-hidden="true" />
          编辑用例定位器
        </Button>
      </div>
      {editing ? (
        <DraftForm
          label="编辑用例定位器"
          document={document}
          store={store}
          select={(current) => current.locators}
          replace={(current, locators) => ({ ...current, locators })}
          disabled={!canEdit}
          onClose={() => setEditing(false)}
        >
          {(value, change, onIssue) => (
            <BindingFields<Locator>
              label="用例定位器"
              value={value}
              onChange={change}
              nameLabel="定位器名称"
              prefix="locator"
              onIssue={onIssue}
              create={() => ({
                candidates: [{ by: 'text', value: '目标文本' }],
                expect: { unique: true, visible: true },
              })}
              render={(locator, update) => <LocatorFields value={locator} onChange={update} />}
            />
          )}
        </DraftForm>
      ) : null}
    </section>
  );
}

type EditorView = 'three' | 'steps' | 'json';

function CaseDraftViews({
  document,
  original,
  state,
  store,
  initialView,
}: {
  document: Case;
  original: Case;
  state: CaseDraftState;
  store: CaseDraftStore;
  initialView: EditorView;
}) {
  const [view, setView] = useState<string>(initialView);
  const [visited, setVisited] = useState<string[]>([initialView]);
  if (!visited.includes(view)) setVisited([...visited, view]);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [savedProblemPath, setSavedProblemPath] = useState<string | null>(null);
  const problems = useCaseProblems(store);
  const problemFocus = problems?.focus;
  const [navigationSequence, setNavigationSequence] = useState<number | null>(null);
  if (problemFocus && navigationSequence !== problemFocus.sequence) {
    setNavigationSequence(problemFocus.sequence);
    if (selectedStepId !== null) setSelectedStepId(null);
    if (savedProblemPath !== null) setSavedProblemPath(null);
    if (view !== problemFocus.problem.target.view) setView(problemFocus.problem.target.view);
  }
  const focusedProblem = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (!problemFocus || focusedProblem.current === problemFocus.sequence) return;
    if (view !== problemFocus.problem.target.view) return;
    focusedProblem.current = problemFocus.sequence;
    problems?.services.locate(problemFocus.problem);
  }, [view, problemFocus, problems?.services]);
  const root = useRef<HTMLDivElement>(null);
  const textSelection = useRef<{
    node: HTMLTextAreaElement;
    start: number;
    end: number;
    direction: 'forward' | 'backward' | 'none';
  } | null>(null);
  useLayoutEffect(() => {
    const selection = textSelection.current;
    textSelection.current = null;
    if (selection?.node.isConnected && globalThis.document.activeElement === selection.node) {
      selection.node.setSelectionRange(selection.start, selection.end, selection.direction);
    }
  }, [state.historyRevision]);
  useLayoutEffect(() => {
    if (view !== 'steps' || !selectedStepId) return;
    const row = [...(root.current?.querySelectorAll<HTMLElement>('[data-step-id]') ?? [])].find(
      (node) => node.dataset['stepId'] === selectedStepId,
    );
    const focus =
      row ?? root.current?.querySelector<HTMLElement>('[role="tab"][data-state="active"]');
    focus?.focus({ preventScroll: true });
    row?.scrollIntoView({ block: 'nearest' });
  }, [view, selectedStepId, state.historyRevision]);
  useLayoutEffect(() => {
    if (view !== 'steps' || !savedProblemPath || selectedStepId) return;
    const field = savedProblemPath.split('/')[1];
    const section = [
      ...(root.current?.querySelectorAll<HTMLElement>('[data-case-field]') ?? []),
    ].find((node) => node.dataset['caseField'] === field);
    const focus =
      section ?? root.current?.querySelector<HTMLElement>('[role="tab"][data-state="active"]');
    focus?.focus({ preventScroll: true });
    focus?.scrollIntoView({ block: 'nearest' });
  }, [view, savedProblemPath, selectedStepId]);
  return (
    <div
      ref={root}
      className="case-draft-content"
      onKeyDown={(event) => {
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.nativeEvent.isComposing)
          return;
        const key = event.key.toLowerCase();
        if (key !== 'z' && key !== 'y') return;
        const target = event.target;
        const stepId =
          target instanceof HTMLElement
            ? target.closest<HTMLElement>('[data-step-id]')?.dataset['stepId']
            : undefined;
        if (target instanceof HTMLElement && target.closest('.case-editor-monaco')) return;
        if (target instanceof HTMLTextAreaElement && target.closest('[data-pending-text="true"]'))
          return;
        event.preventDefault();
        event.stopPropagation();
        if (target instanceof HTMLTextAreaElement && !target.closest('.case-editor-form')) {
          textSelection.current = {
            node: target,
            start: target.selectionStart,
            end: target.selectionEnd,
            direction: target.selectionDirection,
          };
        }
        if (
          (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) &&
          target.closest('.case-editor-form')
        )
          target.blur();
        const restored = key === 'y' || event.shiftKey ? store.redo() : store.undo();
        if (restored && stepId) setSelectedStepId(stepId);
      }}
    >
      <p className="case-draft-goal">{document.goal}</p>
      <div className="case-editor-actions" aria-label="草稿历史">
        <Button
          size="sm"
          variant="ghost"
          disabled={!state.canUndo}
          title="撤销已保存编辑（⌘Z / Ctrl+Z）"
          onClick={() => store.undo()}
        >
          <Undo2 aria-hidden="true" />
          撤销
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={!state.canRedo}
          title="重做已保存编辑（⌘⇧Z / Ctrl+Shift+Z / Ctrl+Y）"
          onClick={() => store.redo()}
        >
          <Redo2 aria-hidden="true" />
          重做
        </Button>
        <span className="case-draft-note">三视图共享已保存草稿的撤销历史。</span>
      </div>
      <TabsPrimitive.Root
        value={view}
        onValueChange={(next) => {
          setSavedProblemPath(null);
          setView(next);
        }}
        activationMode="manual"
        className="case-editor-views"
      >
        <TabsPrimitive.List aria-label="用例编辑视图" className="ui-tab-list">
          {(
            [
              ['three', '三段式'],
              ['steps', '步骤'],
              ['json', 'JSON'],
            ] as const
          ).map(([value, label]) => (
            <TabsPrimitive.Trigger key={value} value={value} className="ui-tab" data-size="sm">
              {label}
            </TabsPrimitive.Trigger>
          ))}
        </TabsPrimitive.List>
        <TabsPrimitive.Content
          forceMount
          value="three"
          className="ui-tab-panel"
          hidden={view !== 'three'}
        >
          {visited.includes('three') ? (
            <ThreeSectionView
              document={document}
              store={store}
              canEdit={state.canInsert}
              active={view === 'three'}
            />
          ) : null}
        </TabsPrimitive.Content>
        <TabsPrimitive.Content
          forceMount
          value="steps"
          className="ui-tab-panel"
          hidden={view !== 'steps'}
        >
          {visited.includes('steps') ? (
            <div className="case-draft-content" key={state.historyRevision}>
              <div className="case-draft-counts">
                <span>
                  原始 {original.steps.length} 步 / 新增{' '}
                  {
                    document.steps.filter(
                      (step) => !original.steps.some((item) => item.id === step.id),
                    ).length
                  }{' '}
                  步
                </span>
                <span>拖拽或上下移排序 / 块内独立编辑</span>
              </div>
              <CaseCheckpoints
                field="preconditions"
                document={document}
                store={store}
                canEdit={state.canInsert}
              />
              <StepList
                steps={document.steps}
                location={ROOT_STEPS}
                label="草稿步骤"
                document={document}
                original={original}
                store={store}
                canEdit={state.canInsert}
                selectedStepId={selectedStepId}
              />
              <CaseCheckpoints
                field="checks"
                document={document}
                store={store}
                canEdit={state.canInsert}
              />
              <CaseLocators document={document} store={store} canEdit={state.canInsert} />
              <div data-case-field="parameters" tabIndex={-1}>
                <div data-case-field="inputs" tabIndex={-1}>
                  <CaseParameterBindings
                    document={document}
                    store={store}
                    canEdit={state.canInsert}
                  />
                </div>
              </div>
            </div>
          ) : null}
        </TabsPrimitive.Content>
        <TabsPrimitive.Content
          forceMount
          value="json"
          className="ui-tab-panel"
          hidden={view !== 'json'}
        >
          {visited.includes('json') ? (
            <CaseJsonView
              document={document}
              store={store}
              canEdit={state.canInsert}
              active={view === 'json'}
              onJump={(id) => {
                setSelectedStepId(id);
                setSavedProblemPath(null);
                setView('steps');
              }}
              onMissingProblem={(problem) => {
                const text = JSON.stringify(document);
                const range = caseTextRange(text, problem.path);
                setSelectedStepId(caseStepAtOffset(text, range.offset));
                setSavedProblemPath(problem.path);
                setView('steps');
              }}
            />
          ) : null}
        </TabsPrimitive.Content>
      </TabsPrimitive.Root>
      {document.original_text !== original.original_text ? (
        <section className="case-editor-section" aria-label="草稿创作输入">
          <div className="case-draft-header">
            <strong>草稿创作输入</strong>
            <Badge tone="warning">待解析 / 需人工核对结构</Badge>
          </div>
          <pre className="case-editor-authoring-text">
            {document.original_text ?? '创作输入已清空，来源原文仍保留。'}
          </pre>
          <p className="case-draft-note">
            这是当前草稿的数据，不是可执行回退；不自动修改已有步骤或测试结果。
          </p>
        </section>
      ) : null}
      <details className="case-editor-source">
        <summary>来源与原文（只读）</summary>
        <p className="case-draft-note">
          来源: {original.source ?? '未记录'}。原文是创作资料，不是可执行回退。
        </p>
        <pre>{original.original_text ?? '未记录原始文本'}</pre>
      </details>
      <p className="case-draft-note">
        预检查 {document.preconditions.length} 项 / 验收检查 {document.checks.length}{' '}
        项。结构校验不等于全工程引用校验或运行预检。
      </p>
    </div>
  );
}

export function CaseDraftPanel({
  store: injectedStore,
  initialView = 'steps',
}: { store?: CaseDraftStore; initialView?: EditorView } = {}) {
  const { state, store } = useCaseDraft(injectedStore);
  const document = state.draft ?? state.original;
  return (
    <section
      className="case-draft-panel"
      aria-label="用例草稿"
      aria-busy={state.status === 'loading'}
    >
      <header className="case-draft-header">
        <div>
          <h3>{document?.name ?? '用例草稿'}</h3>
          {state.scope ? <code className="case-draft-id">{state.scope.caseId}</code> : null}
        </div>
        <Badge
          tone={
            state.persistence === 'saved'
              ? 'success'
              : state.persistence === 'error'
                ? 'danger'
                : 'neutral'
          }
        >
          {state.persistence === 'saved'
            ? '草稿已保存'
            : state.persistence === 'error'
              ? '草稿未确认保存'
              : '原始用例'}
        </Badge>
      </header>
      <p className="case-draft-note" role="status" aria-label="草稿保存状态">
        {state.persistence === 'saved'
          ? '已自动保存本地草稿。工程原始用例未修改，步骤不会执行。'
          : '编辑后自动保存本地草稿，不修改工程原始用例，也不执行步骤。'}
      </p>
      {state.error ? (
        <p className="case-draft-error" role="alert">
          {state.error}
        </p>
      ) : null}
      {!state.canInsert && state.reason !== state.error ? (
        <p className="case-draft-note">{state.reason}</p>
      ) : null}
      {state.scope && (state.status === 'error' || state.persistence === 'error') ? (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void store.load();
          }}
        >
          重新读取用例草稿
        </Button>
      ) : null}
      {document && state.original ? (
        <CaseDraftViews
          key={state.scope ? caseDraftStorageKey(state.scope) : 'original'}
          document={document}
          original={state.original}
          state={state}
          store={store}
          initialView={initialView}
        />
      ) : null}
    </section>
  );
}
