import type { Case, Flow } from '@pulse/schema';
import {
  createContext,
  useContext,
  useEffect,
  useEffectEvent,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { AlertTriangle, CircleAlert, Info, Navigation } from 'lucide-react';
import { Badge, Select } from '../../shared/ui';
import { useCaseDraft, type CaseDraftStore } from './caseDraftStore';
import { sameJson } from './caseDraftValidation';
import {
  CASE_PROBLEM_CATEGORIES,
  caseFlowReferences,
  collectCaseProblems,
  type CaseProblem,
  type CaseProblemDetails,
  type CaseProblemProject,
} from './caseDiagnostics';
import './caseDraft.css';

interface FocusRequest {
  sequence: number;
  original: Case | null;
  problem: CaseProblem;
}

export type LocalProblem = CaseProblemDetails & { target?: Omit<CaseProblem['target'], 'owner'> };
type Reports = Record<string, { original: Case | null; problems: CaseProblem[] }>;

interface ProblemServices {
  report: (owner: string, original: Case | null, problems: CaseProblem[]) => void;
  register: (owner: string, locate: (problem: CaseProblem) => void) => () => void;
  locate: (problem: CaseProblem) => void;
}

interface ProblemContextValue {
  store: CaseDraftStore;
  problems: CaseProblem[];
  focus: FocusRequest | null;
  services: ProblemServices;
  navigate: (problem: CaseProblem) => void;
  available: boolean;
  flowPending: boolean;
  flowBlocked: boolean;
}

const ProblemContext = createContext<ProblemContextValue | null>(null);

export function CaseProblemsProvider({
  store,
  project,
  children,
}: {
  store: CaseDraftStore;
  project: CaseProblemProject | null;
  children: ReactNode;
}) {
  const { state } = useCaseDraft(store);
  const [reports, setReports] = useState<Reports>({});
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const sequence = useRef(0);
  const [services] = useState<ProblemServices>(() => {
    const locators = new Map<string, (problem: CaseProblem) => void>();
    return {
      report: (owner, original, problems) =>
        setReports((current) => {
          if (current[owner]?.original === original && sameJson(current[owner]?.problems, problems))
            return current;
          if (!problems.length) {
            if (!Object.hasOwn(current, owner)) return current;
            return Object.fromEntries(Object.entries(current).filter(([key]) => key !== owner));
          }
          return { ...current, [owner]: { original, problems } };
        }),
      register: (owner, locate) => {
        locators.set(owner, locate);
        return () => locators.delete(owner);
      },
      locate: (problem) => {
        if (problem.target.owner) locators.get(problem.target.owner)?.(problem);
      },
    };
  });
  const document = state.draft ?? state.original;
  const confirmedProject =
    state.scope &&
    project?.root === state.scope.root &&
    project.project_id === state.scope.projectId &&
    project.case_ids.includes(state.scope.caseId)
      ? project
      : null;
  const references = JSON.stringify(document ? caseFlowReferences(document.steps) : []);
  const [flowRead, setFlowRead] = useState<{
    original: Case | null;
    references: string;
    project: CaseProblemProject | null;
    flows: Record<string, Flow>;
    failed: { id: string; reason: 'read' | 'depth' }[];
  } | null>(null);
  useEffect(() => {
    if (state.status !== 'ready' || !state.original || !confirmedProject) return;
    const original = state.original;
    const controller = new AbortController();
    const flows: Record<string, Flow> = {};
    const failed: { id: string; reason: 'read' | 'depth' }[] = [];
    const visited = new Set<string>();
    const read = async (id: string, depth = 0): Promise<void> => {
      if (visited.has(id) || !confirmedProject.flow_ids.includes(id)) return;
      visited.add(id);
      if (depth >= 16) {
        failed.push({ id, reason: 'depth' });
        return;
      }
      try {
        const flow = await store.readFlow(id, controller.signal);
        if (controller.signal.aborted) return;
        Object.defineProperty(flows, id, { value: flow, enumerable: true, configurable: true });
        await Promise.all(caseFlowReferences(flow.steps).map((ref) => read(ref, depth + 1)));
      } catch {
        if (!controller.signal.aborted) failed.push({ id, reason: 'read' });
      }
    };
    const ids = JSON.parse(references) as string[];
    void Promise.all(ids.map((id) => read(id))).then(() => {
      if (!controller.signal.aborted && store.getSnapshot().original === original)
        setFlowRead({ original, references, project: confirmedProject, flows, failed });
    });
    return () => controller.abort();
  }, [store, state.original, state.status, confirmedProject, references]);
  const currentFlows =
    flowRead?.original === state.original &&
    flowRead?.references === references &&
    flowRead?.project === confirmedProject
      ? flowRead
      : null;
  const problems = document
    ? collectCaseProblems(document, confirmedProject, currentFlows?.flows)
    : [];
  if (document && currentFlows?.failed.length) {
    for (const { id, reason } of currentFlows.failed) {
      const caller = problems.find(
        (problem) => problem.category === 'reference' && problem.message.includes(id),
      );
      problems.push({
        id: `flow-read:${id}`,
        category: 'reference',
        severity: 'warning',
        path: caller?.path ?? 'Case/steps',
        message:
          reason === 'depth'
            ? `公共流程 ${id} 超过本地读取深度上限（16），相关实参、返回值、AI 依赖与禁止动作尚未验证。`
            : `公共流程 ${id} 读取未确认，相关实参、返回值与模型依赖尚未验证。`,
        source: '工程依赖',
        target: { view: 'json' },
      });
    }
  }
  for (const report of Object.values(reports))
    if (report.original === state.original) problems.push(...report.problems);
  const rank = { error: 0, warning: 1, info: 2 };
  problems.sort((left, right) => rank[left.severity] - rank[right.severity]);
  return (
    <ProblemContext.Provider
      value={{
        store,
        problems,
        services,
        available: !!document,
        flowPending:
          state.status === 'ready' &&
          !!document &&
          !!confirmedProject &&
          references !== '[]' &&
          !currentFlows,
        flowBlocked: state.status !== 'ready' && !!document && references !== '[]',
        focus: focus?.original === state.original ? focus : null,
        navigate: (problem) => {
          if (!problems.some((current) => current.id === problem.id)) return;
          setFocus({
            problem,
            original: store.getSnapshot().original,
            sequence: ++sequence.current,
          });
        },
      }}
    >
      {children}
    </ProblemContext.Provider>
  );
}

export function useCaseProblems(store?: CaseDraftStore) {
  const context = useContext(ProblemContext);
  return store && context?.store !== store ? null : context;
}

/** Rejected buffers stay local; only their diagnostics and focus handles are shared. */
export function useCaseProblemReporter(
  store: CaseDraftStore,
  problems: LocalProblem[],
  source: string,
  view: CaseProblem['target']['view'],
  locate: (problem: CaseProblem) => void,
) {
  const context = useCaseProblems(store);
  const services = context?.services;
  const owner = useId();
  const [original] = useState(() => store.getSnapshot().original);
  const key = JSON.stringify(problems);
  const currentProblems = useEffectEvent(() => problems);
  const focus = useEffectEvent((problem: CaseProblem) => locate(problem));
  useEffect(() => {
    if (!services) return;
    return services.register(owner, (problem) => focus(problem));
  }, [services, owner]);
  useEffect(() => {
    services?.report(
      owner,
      original,
      currentProblems().map((problem, index) => ({
        ...problem,
        id: `${owner}:${index}`,
        source: `未保存 · ${source}`,
        target: { view, ...problem.target, owner },
      })),
    );
  }, [services, owner, original, key, source, view]);
  useEffect(() => () => services?.report(owner, original, []), [services, owner, original]);
}

export function CaseProblemsPanel() {
  const context = useCaseProblems();
  const [category, setCategory] = useState('all');
  const problems = context?.problems ?? [];
  const visible =
    category === 'all' ? problems : problems.filter((problem) => problem.category === category);
  const errors = problems.filter((problem) => problem.severity === 'error').length;
  return (
    <section className="case-problems-panel" aria-label="用例问题">
      <header className="case-draft-header">
        <h3>当前用例问题</h3>
        <div className="case-editor-actions">
          <Badge tone={errors ? 'danger' : 'neutral'}>
            {errors} 个错误 / {problems.length - errors} 个提示
          </Badge>
          <Select
            size="sm"
            aria-label="问题分类"
            value={category}
            onValueChange={setCategory}
            items={[
              { value: 'all', label: '全部问题' },
              ...Object.entries(CASE_PROBLEM_CATEGORIES).map(([value, label]) => ({
                value,
                label,
              })),
            ]}
          />
        </div>
      </header>
      <p className="case-draft-note" role="status">
        {!context?.available
          ? '请选择并读取用例后查看问题。'
          : context.flowBlocked
            ? '草稿读取或恢复未确认，未核对公共流程；请先处理编辑区的读取错误。'
            : context.flowPending
              ? '正在只读核对公共流程定义，不执行步骤或请求模型。'
              : '已确认草稿与未保存编辑均纳入诊断；点击问题定位，不会丢弃未保存内容。'}
      </p>
      {visible.length ? (
        <ol className="case-problems-list" aria-label="问题列表">
          {visible.map((problem) => {
            const Icon =
              problem.severity === 'error'
                ? CircleAlert
                : problem.severity === 'warning'
                  ? AlertTriangle
                  : Info;
            return (
              <li key={problem.id}>
                <button
                  type="button"
                  className="case-problem-row"
                  data-severity={problem.severity}
                  aria-label={`定位 ${CASE_PROBLEM_CATEGORIES[problem.category]}：${problem.path} ${problem.message} ${problem.source}`}
                  onClick={() => context?.navigate(problem)}
                >
                  <Icon aria-hidden="true" />
                  <span className="case-problem-content">
                    <span className="case-problem-message">{problem.message}</span>
                    <span className="case-problem-location">
                      <code>{problem.path}</code>
                      <span>{problem.source}</span>
                    </span>
                  </span>
                  <Badge
                    tone={
                      problem.severity === 'error'
                        ? 'danger'
                        : problem.severity === 'warning'
                          ? 'warning'
                          : 'neutral'
                    }
                  >
                    {CASE_PROBLEM_CATEGORIES[problem.category]}
                  </Badge>
                  <Navigation aria-hidden="true" />
                </button>
              </li>
            );
          })}
        </ol>
      ) : context?.available ? (
        <p className="case-draft-note">
          {problems.length ? '此分类下没有问题。' : '未发现可确认的草稿问题。'}
        </p>
      ) : null}
      <p className="case-draft-note">
        环境定位器、数据键与运行能力尚未完整验证。本面板不替代全工程校验、运行预检或业务验收。
      </p>
    </section>
  );
}
