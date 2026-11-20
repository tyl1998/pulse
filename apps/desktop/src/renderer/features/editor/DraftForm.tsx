import type { Case } from '@pulse/schema';
import { useRef, useState, type ReactNode } from 'react';
import { Button } from '../../shared/ui';
import { caseDraftStorageKey, type CaseDraftStore } from './caseDraftStore';
import { assertDraftCase, caseSchemaIssues, sameJson } from './caseDraftValidation';
import { useCaseProblemReporter } from './CaseProblems';
import type { CaseProblem, CaseProblemDetails } from './caseDiagnostics';
import { caseTextIssue } from './caseText';
import type { FieldChange, FieldIssueHandler } from './StepFields';

function focusFormProblem(
  form: HTMLFormElement,
  problem: CaseProblem,
  lastField: HTMLElement | null,
) {
  const parts = problem.path
    .split('/')
    .slice(1)
    .map((part) => part.replaceAll('~1', '/').replaceAll('~0', '~'));
  let region: HTMLElement = form;
  const scopes: Record<string, string> = {
    target: '目标',
    scope: '消失检查作用域',
    start: '滑动起点',
    end: '滑动终点',
    expected: '固定期望',
  };
  const bindingLabels: Record<string, string> = {
    parameters: '参数声明',
    inputs: '初始输入',
    locators: '用例定位器',
    arguments: '流程参数',
    bind: '流程返回绑定',
  };
  for (const [index, part] of parts.entries()) {
    if (Object.hasOwn(scopes, part)) {
      const group = [...region.querySelectorAll<HTMLElement>('[data-field-scope]')].find(
        (node) => node.dataset['fieldScope'] === scopes[part],
      );
      if (group) region = group;
    }
    if (
      part === 'parameters' ||
      part === 'inputs' ||
      part === 'locators' ||
      part === 'arguments' ||
      part === 'bind'
    ) {
      const group = [...region.querySelectorAll<HTMLElement>('[data-binding-label]')].find(
        (node) => node.dataset['bindingLabel'] === bindingLabels[part],
      );
      if (group) region = group;
      const binding = [...region.querySelectorAll<HTMLElement>('[data-binding-name]')].find(
        (node) => node.dataset['bindingName'] === parts[index + 1],
      );
      if (binding) region = binding;
    }
    if (part === 'checks' || part === 'preconditions') {
      const checkpoint = [...region.querySelectorAll<HTMLElement>('[data-checkpoint-index]')].find(
        (node) => node.dataset['checkpointIndex'] === parts[index + 1],
      );
      if (checkpoint) region = checkpoint;
    }
    if (part === 'candidates') {
      const candidate = [...region.querySelectorAll<HTMLElement>('[data-candidate-index]')].find(
        (node) => node.dataset['candidateIndex'] === parts[index + 1],
      );
      if (candidate) region = candidate;
    }
  }
  const scalarInput = parts[0] === 'inputs' && parts.length === 2;
  if (scalarInput)
    region = region.querySelector<HTMLElement>('[data-field-scope="初始输入值"]') ?? region;
  const labels: Record<string, string[]> = {
    id: [
      parts.includes('checkpoint') || parts.includes('checks') || parts.includes('preconditions')
        ? '检查点 ID'
        : '步骤 ID',
    ],
    timeout_ms: [parts.includes('condition') ? '条件超时（ms）' : '步骤超时（ms）'],
    operator: ['检查运算符'],
    source: ['观察来源', '提取来源'],
    observation_window_ms: ['观察窗口（ms）'],
    poll_interval_ms: ['轮询间隔（ms）'],
    precision: ['精度'],
    unit: ['单位'],
    comparison: ['数值比较'],
    expected: ['数值', '文本值', '期望为真'],
    param_ref: ['参数引用'],
    var_ref: ['变量引用'],
    data_ref: ['数据集引用'],
    key: ['数据键', '按键'],
    secret_ref: ['秘密引用'],
    locator_ref: ['定位器引用'],
    flow_ref: ['公共流程引用'],
    app_id: ['应用 ID'],
    duration_ms: ['动作时长（ms）'],
    max_iterations: ['最多迭代次数'],
    as: ['保存为变量'],
    goal: ['AI 步骤目标'],
    text: ['文本值'],
    value: ['定位值'],
    by: ['定位方式'],
    scope: ['候选作用域（可选）'],
    type: ['参数类型'],
    default: ['默认值', '数值', '文本值'],
    x: ['x'],
    y: ['y'],
    width: ['width'],
    height: ['height'],
  };
  const key = parts.at(-1) ?? '';
  const modes: Record<string, string> = {
    text: '文本值',
    number: '数值',
    boolean: '布尔值',
    param_ref: '参数引用',
    var_ref: '变量引用',
    secret_ref: '秘密引用',
  };
  const mode = region.dataset['valueMode'] ?? '';
  const wanted =
    scalarInput && Object.hasOwn(modes, mode)
      ? [modes[mode]]
      : Object.hasOwn(labels, key)
        ? (labels[key] ?? [])
        : [];
  const field = [...region.querySelectorAll<HTMLElement>('[data-field-label]')].find((node) =>
    wanted.includes(node.dataset['fieldLabel'] ?? ''),
  );
  const control =
    field?.querySelector<HTMLElement>('input, textarea, [role="combobox"], [role="checkbox"]') ??
    (lastField?.isConnected && region.contains(lastField) ? lastField : null) ??
    region.querySelector<HTMLElement>('input, textarea, [role="combobox"]') ??
    form;
  control.focus({ preventScroll: true });
  control.scrollIntoView({ block: 'nearest' });
}

export function DraftForm<T>({
  label,
  document,
  store,
  select,
  replace,
  children,
  onClose,
  disabled,
  beforeSave,
  persist = store.editDraft,
}: {
  label: string;
  document: Case;
  store: CaseDraftStore;
  select: (document: Case) => T;
  replace: (document: Case, value: T) => Case;
  children: (value: T, change: FieldChange<T>, reportIssue: FieldIssueHandler) => ReactNode;
  onClose: () => void;
  disabled: boolean;
  beforeSave?: (previous: T, next: T) => string | null;
  persist?: CaseDraftStore['editDraft'];
}) {
  const [value, setValue] = useState(() => select(document));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(value);
  const [fieldIssues, setFieldIssues] = useState<Record<string, string>>({});
  const [resetRevision, setResetRevision] = useState(0);
  const root = useRef<HTMLFormElement>(null);
  const lastField = useRef<HTMLElement | null>(null);
  const dirty = !sameJson(value, saved) || Object.keys(fieldIssues).length > 0;
  const selected = select(document);
  if (!dirty && !sameJson(selected, saved)) {
    setValue(selected);
    setSaved(selected);
    setError(null);
    setResetRevision(resetRevision + 1);
  }
  const [scopeKey] = useState(() => {
    const scope = store.getSnapshot().scope;
    return scope ? caseDraftStorageKey(scope) : null;
  });
  const problems: CaseProblemDetails[] = [];
  if (dirty) {
    try {
      for (const issue of caseSchemaIssues(replace(document, value)))
        problems.push({ category: 'schema', severity: 'error', ...issue });
    } catch (failure) {
      problems.push({
        category: 'editing',
        severity: 'error',
        path: 'Case',
        message: failure instanceof Error ? failure.message : String(failure),
      });
    }
  }
  for (const message of Object.values(fieldIssues))
    problems.push({ category: 'editing', severity: 'error', path: 'Case', message });
  if (error && !problems.some((problem) => error.includes(problem.message)))
    problems.push({
      category: 'editing',
      severity: 'error',
      path: caseTextIssue('', new Error(error)).path ?? 'Case',
      message: error,
    });
  useCaseProblemReporter(store, problems, label, 'steps', (problem) => {
    if (root.current) focusFormProblem(root.current, problem, lastField.current);
  });
  const save = (next = value, clearedIssue?: string): boolean => {
    try {
      const fieldIssue = Object.entries(fieldIssues).find(([key]) => key !== clearedIssue)?.[1];
      if (fieldIssue) throw new Error(fieldIssue);
      const state = store.getSnapshot();
      if (!state.scope || caseDraftStorageKey(state.scope) !== scopeKey) {
        throw new Error('选中用例已变化，未将旧编辑写入新用例');
      }
      if (!state.canInsert || !state.draft) throw new Error(state.reason ?? '草稿暂不可编辑');
      if (!sameJson(select(state.draft), saved)) {
        throw new Error('此内容已被修改，请恢复已保存内容后再编辑');
      }
      const blocked = beforeSave?.(saved, next);
      if (blocked) throw new Error(blocked);
      const edited = replace(state.draft, next);
      assertDraftCase(edited);
      if (!persist(edited, state.draft)) {
        throw new Error(store.getSnapshot().error ?? '草稿未确认保存');
      }
      setSaved(next);
      setError(null);
      return true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      return false;
    }
  };
  const change: FieldChange<T> = (next, immediate = false, clearedIssue) => {
    setValue(next);
    if (immediate) save(next, clearedIssue);
  };
  return (
    <form
      ref={root}
      className="case-editor-form"
      aria-label={label}
      tabIndex={-1}
      noValidate
      onChangeCapture={(event) => {
        if (event.target instanceof HTMLElement) lastField.current = event.target;
      }}
      onBlur={(event) => {
        if (
          event.relatedTarget instanceof HTMLElement &&
          event.relatedTarget.dataset['discard'] === 'true'
        )
          return;
        if (
          event.target instanceof HTMLInputElement ||
          event.target instanceof HTMLTextAreaElement
        ) {
          lastField.current = event.target;
          save();
        }
      }}
      onSubmit={(event) => {
        event.preventDefault();
        if (save()) onClose();
      }}
    >
      <fieldset className="case-editor-form-body" disabled={disabled}>
        <legend className="case-editor-form-title">{label}</legend>
        <p className="case-draft-note">
          字段离开焦点后自动保存，下拉选择与开关立即保存。无效编辑不会覆盖已保存草稿。
        </p>
        <div className="case-editor-fields" key={resetRevision}>
          {children(value, change, (key, issue) =>
            setFieldIssues((current) => {
              return issue
                ? { ...current, [key]: issue }
                : Object.fromEntries(Object.entries(current).filter(([entry]) => entry !== key));
            }),
          )}
        </div>
      </fieldset>
      {error ? (
        <p className="case-draft-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="case-editor-actions">
        <Button size="sm" type="submit" variant="primary" disabled={disabled}>
          保存并收起
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled}
          data-discard="true"
          onClick={() => {
            const current = store.getSnapshot().draft;
            if (!current) return;
            const restored = select(current);
            setValue(restored);
            setSaved(restored);
            setError(null);
            setFieldIssues({});
            setResetRevision((revision) => revision + 1);
          }}
        >
          恢复已保存
        </Button>
        <Button size="sm" variant="ghost" data-discard="true" onClick={onClose}>
          放弃未保存编辑并收起
        </Button>
        <span className="case-draft-note" aria-live="polite">
          {dirty ? '有未保存编辑' : '当前编辑已保存'}
        </span>
      </div>
    </form>
  );
}
