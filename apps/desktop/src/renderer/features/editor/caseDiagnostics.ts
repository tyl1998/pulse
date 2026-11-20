import type { Case, Flow, Locator, Parameter, Predicate, Step, Target, Value } from '@pulse/schema';
import type { PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import { caseSchemaIssues, draftPath, isRecord } from './caseDraftValidation';

export const CASE_PROBLEM_CATEGORIES = {
  schema: 'Schema 错误',
  reference: '未解析引用',
  semantic: '绑定与检查',
  ai: 'AI 依赖',
  policy: '禁止动作冲突',
  editing: '未保存编辑',
} as const;

export interface CaseProblemDetails {
  category: keyof typeof CASE_PROBLEM_CATEGORIES;
  severity: 'error' | 'warning' | 'info';
  path: string;
  message: string;
}

export interface CaseProblem extends CaseProblemDetails {
  id: string;
  source: string;
  target: {
    view: 'steps' | 'three' | 'json';
    owner?: string;
    offset?: number;
    length?: number;
  };
}

export type CaseProblemProject = Pick<
  PulseRuntimeApiSchemas.ProjectDetail,
  | 'root'
  | 'project_id'
  | 'case_ids'
  | 'profile_ids'
  | 'flow_ids'
  | 'data_ids'
  | 'allowed_apps'
  | 'manifest'
>;

interface ValueInfo {
  type?: ScalarType;
  value?: string | number | boolean;
  secret?: boolean;
}

type ScalarType = Parameter['type'];

function scalarInfo(value: string | number | boolean): ValueInfo {
  return {
    type:
      typeof value === 'number'
        ? Number.isSafeInteger(value)
          ? 'integer'
          : 'number'
        : typeof value === 'boolean'
          ? 'boolean'
          : 'string',
    value,
  };
}

function compatible(actual: ScalarType | undefined, expected: ScalarType): boolean {
  return (
    actual === undefined || actual === expected || (actual === 'integer' && expected === 'number')
  );
}

export function caseFlowReferences(steps: Step[]): string[] {
  const refs = new Set<string>();
  const visit = (items: Step[]) => {
    for (const step of items) {
      if (step.type === 'flow') refs.add(step.flow_ref);
      if (step.type === 'if') {
        visit(step.then);
        visit(step.else ?? []);
      }
      if (step.type === 'loop') visit(step.steps);
    }
  };
  visit(steps);
  return [...refs].sort();
}

/** Local draft diagnostics, not a substitute for the Runtime's complete project preflight. */
export function collectCaseProblems(
  document: Case,
  project: CaseProblemProject | null,
  flows: Readonly<Record<string, Flow | undefined>> = {},
): CaseProblem[] {
  const problems: CaseProblem[] = [];
  const add = (
    category: CaseProblemDetails['category'],
    path: string,
    message: string,
    severity: CaseProblemDetails['severity'] = 'error',
  ) => {
    problems.push({
      id: `${category}:${path}:${problems.length}`,
      category,
      severity,
      path,
      message,
      source: '已确认草稿',
      target: { view: 'json' },
    });
  };
  const schema = caseSchemaIssues(document);
  for (const issue of schema) add('schema', issue.path, issue.message);
  if (schema.length) return problems;

  const constraints = isRecord(project?.manifest['constraints'])
    ? project.manifest['constraints']
    : null;
  const forbidden = Array.isArray(constraints?.['forbidden_actions'])
    ? constraints['forbidden_actions']
    : [];
  const secrets = Array.isArray(project?.manifest['secret_refs'])
    ? project.manifest['secret_refs']
    : null;
  const resources = isRecord(project?.manifest['resources']) ? project.manifest['resources'] : null;
  const budgets = isRecord(project?.manifest['budgets']) ? project.manifest['budgets'] : null;
  const caseBudget =
    typeof budgets?.['case_timeout_ms'] === 'number' ? budgets['case_timeout_ms'] : 600_000;
  const parameters = new Map<string, ValueInfo>();
  for (const [name, parameter] of Object.entries(document.parameters)) {
    if (!parameter) continue;
    const path = draftPath('Case/parameters', name);
    const hasDefault = Object.hasOwn(parameter, 'default');
    if (parameter.required === hasDefault || (hasDefault && parameter.default == null))
      add('semantic', path, '必填参数不能带默认值；可选参数必须提供同类型标量默认值。');
    if (
      parameter.default != null &&
      !compatible(scalarInfo(parameter.default).type, parameter.type)
    )
      add('semantic', `${path}/default`, '默认值与声明的参数类型不匹配。');
    parameters.set(name, {
      type: parameter.type,
      ...(parameter.default == null ? {} : { value: parameter.default }),
    });
  }

  const valueInfo = (
    value: Value,
    path: string,
    variables: Map<string, ValueInfo>,
  ): ValueInfo | undefined => {
    if (typeof value !== 'object') return scalarInfo(value);
    if ('param_ref' in value) {
      const info = parameters.get(value.param_ref);
      if (!info) add('reference', `${path}/param_ref`, `参数 ${value.param_ref} 未声明。`);
      return info;
    }
    if ('var_ref' in value) {
      const info = variables.get(value.var_ref);
      if (!info)
        add(
          'reference',
          `${path}/var_ref`,
          `变量 ${value.var_ref} 尚未定义，或不在当前词法作用域内。`,
        );
      return info;
    }
    if ('secret_ref' in value) {
      if (secrets && !secrets.includes(value.secret_ref))
        add('reference', `${path}/secret_ref`, `秘密引用 ${value.secret_ref} 不在工程声明中。`);
      return { type: 'string', secret: true };
    }
    if (project && !project.data_ids.includes(value.data_ref)) {
      add('reference', `${path}/data_ref`, `数据集 ${value.data_ref} 不在当前工程中。`);
      return undefined;
    }
    return {};
  };

  for (const [name, value] of Object.entries(document.inputs)) {
    if (value === undefined) continue;
    const path = draftPath('Case/inputs', name);
    const parameter = Object.hasOwn(document.parameters, name)
      ? document.parameters[name]
      : undefined;
    if (!Object.hasOwn(document.parameters, name))
      add('reference', path, `初始输入 ${name} 没有对应的参数声明。`);
    if (typeof value === 'object' && ('param_ref' in value || 'var_ref' in value)) {
      add('semantic', path, '用例初始输入只允许标量、数据或秘密引用，不能引用参数或运行变量。');
      continue;
    }
    const info = valueInfo(value, path, new Map());
    if (parameter && info) {
      if (!compatible(info.type, parameter.type))
        add('semantic', path, '初始输入与声明的参数类型不匹配。');
      parameters.set(name, { ...info, type: parameter.type });
    }
  }
  for (const [index, name] of document.profile_refs.entries()) {
    if (project && !project.profile_ids.includes(name))
      add('reference', `Case/profile_refs/${index}`, `环境 ${name} 不在当前工程中。`);
  }

  const profileMayResolve =
    !project || document.profile_refs.some((name) => project.profile_ids.includes(name));
  const locator = (item: Locator, path: string) => {
    if (item.candidates.every((candidate) => candidate.by === 'point'))
      add('semantic', path, '可移植定位器至少需要一个非坐标候选。');
    for (const [index, candidate] of item.candidates.entries()) {
      const where = `${path}/candidates/${index}`;
      if (candidate.scope && !Object.hasOwn(document.locators, candidate.scope))
        add(
          'reference',
          `${where}/scope`,
          profileMayResolve
            ? `作用域 ${candidate.scope} 未在用例中定义，需复查环境定位器。`
            : `作用域定位器 ${candidate.scope} 未定义。`,
          profileMayResolve ? 'warning' : 'error',
        );
      if (candidate.by === 'image' && resources && !Object.hasOwn(resources, candidate.value))
        add('reference', `${where}/value`, `图片资源 ${candidate.value} 未在工程中声明。`);
      if (
        candidate.by === 'point' &&
        (candidate.value.x >= candidate.value.width || candidate.value.y >= candidate.value.height)
      )
        add('semantic', `${where}/value`, '坐标超出了声明的屏幕尺寸。');
    }
  };
  const target = (item: Target, path: string) => {
    if ('locator' in item) locator(item.locator, `${path}/locator`);
    else if (!Object.hasOwn(document.locators, item.locator_ref))
      add(
        'reference',
        `${path}/locator_ref`,
        profileMayResolve
          ? `定位器 ${item.locator_ref} 未在用例中定义，需复查环境定位器。`
          : `定位器 ${item.locator_ref} 未定义。`,
        profileMayResolve ? 'warning' : 'error',
      );
  };
  for (const [name, item] of Object.entries(document.locators))
    if (item) locator(item, draftPath('Case/locators', name));

  const condition = (
    item: Predicate,
    path: string,
    variables: Map<string, ValueInfo>,
    budget: number,
  ) => {
    target(item.target, `${path}/target`);
    if (item.operator === 'not_exists') {
      target(item.scope, `${path}/scope`);
      if (item.observation_window_ms > Math.min(item.timeout_ms ?? 10_000, budget))
        add('semantic', `${path}/observation_window_ms`, '观察窗口超过条件或外层步骤的超时。');
      if ((item.poll_interval_ms ?? 500) > item.observation_window_ms)
        add('semantic', `${path}/poll_interval_ms`, '轮询间隔不能超过观察窗口。');
    }
    if ('expected' in item && typeof item.expected !== 'boolean') {
      const pathExpected = `${path}/expected`;
      const info = valueInfo(item.expected, pathExpected, variables);
      const expected: ScalarType = item.operator.startsWith('count_')
        ? 'integer'
        : item.operator === 'number_cmp'
          ? 'number'
          : 'string';
      if (info && !compatible(info.type, expected))
        add('semantic', pathExpected, '固定期望的引用类型与检查运算符不匹配。');
      if (info?.secret) add('semantic', pathExpected, '固定期望不能来自秘密值。');
      if (item.operator.startsWith('count_') && typeof info?.value === 'number' && info.value < 0)
        add('semantic', pathExpected, '期望数量不能为负数。');
      if (
        (item.operator === 'contains' && info?.value === '') ||
        ((item.operator === 'semantic' || item.operator === 'regex') &&
          typeof info?.value === 'string' &&
          !info.value.trim())
      )
        add('semantic', pathExpected, '此检查标准不能为空；不会根据实际观察补写期望。');
    }
    if (item.operator === 'number_cmp') {
      const currencies = ['CNY', 'RMB', '元', '¥', '￥', 'USD', '$', 'EUR', '€', 'GBP', '£'];
      const units = ['%', '折', '小时', 'h', '分钟', 'min', '秒', 's', '杯', '次', '件', '个'];
      if (!currencies.includes(item.unit.toUpperCase()) && !units.includes(item.unit))
        add('semantic', `${path}/unit`, '单位不在断言库支持的闭集中，运行前会阻断。');
      if (item.precision > 6) add('semantic', `${path}/precision`, '数值比较精度最多为 6。');
    }
    if (item.operator === 'semantic')
      add(
        'ai',
        path,
        'semantic 检查需要非定位恢复阶段的模型参与；本编辑器不会请求模型。',
        'warning',
      );
    if (
      (item.operator === 'enabled' || item.operator === 'checked') &&
      (item.source === 'ocr' || item.source === 'vision')
    )
      add(
        'semantic',
        `${path}/source`,
        'OCR / vision 单独不能证明原生控件状态，运行时需要对应证据。',
        'warning',
      );
  };

  const policy = (step: Step, path: string, prefix = '') => {
    if (step.type === 'action') {
      if (forbidden.includes(step.action.kind) || forbidden.includes(step.id))
        add(
          'policy',
          prefix ? path : `${path}/action`,
          `${prefix}动作 ${step.action.kind} 或步骤 ID 与工程禁止动作冲突。`,
        );
      if ('app_id' in step.action && project && !project.allowed_apps.includes(step.action.app_id))
        add(
          'policy',
          prefix ? path : `${path}/action/app_id`,
          `${prefix}动作应用不在工程允许范围中。`,
        );
    }
  };
  const flowHazards = (flow: Flow, caller: string, stack: string[]) => {
    if (stack.includes(flow.flow_id) || stack.length >= 16) {
      add('reference', caller, '公共流程存在循环引用或调用深度超过 16 层。');
      return;
    }
    const visit = (items: Step[]) => {
      for (const step of items) {
        policy(step, caller, `公共流程 ${flow.flow_id}：`);
        const checks =
          step.type === 'check' ? [step.checkpoint] : step.type === 'ai_step' ? step.checks : [];
        if (
          step.type === 'ai_step' ||
          checks.some((check) => check.condition.operator === 'semantic') ||
          ((step.type === 'wait' || step.type === 'if' || step.type === 'loop') &&
            step.condition?.operator === 'semantic')
        )
          add('ai', caller, `公共流程 ${flow.flow_id} 的步骤 ${step.id} 需要模型参与。`, 'warning');
        if (step.type === 'flow') {
          const nested = Object.hasOwn(flows, step.flow_ref) ? flows[step.flow_ref] : undefined;
          if (project && !project.flow_ids.includes(step.flow_ref))
            add(
              'reference',
              caller,
              `公共流程 ${flow.flow_id} 引用了工程中不存在的流程 ${step.flow_ref}。`,
            );
          else if (nested) flowHazards(nested, caller, [...stack, flow.flow_id]);
        }
        if (step.type === 'if') {
          visit(step.then);
          visit(step.else ?? []);
        }
        if (step.type === 'loop') visit(step.steps);
      }
    };
    visit(flow.steps);
  };
  const assign = (
    variables: Map<string, ValueInfo>,
    name: string,
    info: ValueInfo,
    path: string,
  ) => {
    if (variables.has(name))
      add('semantic', path, `变量 ${name} 在当前作用域内已定义，不能重复赋值。`);
    else variables.set(name, info);
  };
  const steps = (
    items: Step[],
    path: string,
    variables: Map<string, ValueInfo>,
    budget: number,
  ) => {
    for (const [index, step] of items.entries()) {
      const where = `${path}/${index}`;
      const remaining = Math.min(step.timeout_ms ?? 30_000, budget);
      policy(step, where);
      if (step.type === 'action') {
        if ('target' in step.action) target(step.action.target, `${where}/action/target`);
        if (step.action.kind === 'input') {
          const info = valueInfo(step.action.text, `${where}/action/text`, variables);
          if (info && !compatible(info.type, 'string'))
            add(
              'semantic',
              `${where}/action/text`,
              '输入动作需要 string 类型，不能隐式转换引用值。',
            );
        }
        if ('duration_ms' in step.action && step.action.duration_ms > remaining)
          add('semantic', `${where}/action/duration_ms`, '动作时长超过步骤或外层超时。');
      } else if (step.type === 'flow') {
        const flow = Object.hasOwn(flows, step.flow_ref) ? flows[step.flow_ref] : undefined;
        if (project && !project.flow_ids.includes(step.flow_ref))
          add('reference', `${where}/flow_ref`, `公共流程 ${step.flow_ref} 不在当前工程中。`);
        for (const [name, argument] of Object.entries(step.arguments ?? {})) {
          if (argument === undefined) continue;
          const argumentPath = draftPath(`${where}/arguments`, name);
          const info = valueInfo(argument, argumentPath, variables);
          if (flow && !Object.hasOwn(flow.parameters, name))
            add('reference', argumentPath, `公共流程 ${step.flow_ref} 没有参数 ${name}。`);
          const parameter =
            flow && Object.hasOwn(flow.parameters, name) ? flow.parameters[name] : undefined;
          if (parameter && info && !compatible(info.type, parameter.type))
            add('semantic', argumentPath, '流程实参与声明的类型不匹配。');
        }
        if (flow) {
          for (const [name, parameter] of Object.entries(flow.parameters))
            if (parameter?.required && !Object.hasOwn(step.arguments ?? {}, name))
              add(
                'reference',
                `${where}/arguments`,
                `公共流程 ${step.flow_ref} 缺少必填实参 ${name}。`,
              );
          flowHazards(flow, `${where}/flow_ref`, []);
        }
        for (const [name, variable] of Object.entries(step.bind ?? {})) {
          if (!variable) continue;
          const output = flow && Object.hasOwn(flow.returns, name) ? flow.returns[name] : undefined;
          if (flow && !Object.hasOwn(flow.returns, name))
            add(
              'reference',
              draftPath(`${where}/bind`, name),
              `公共流程 ${step.flow_ref} 没有返回值 ${name}。`,
            );
          else
            assign(
              variables,
              variable,
              output ? { type: output.type } : {},
              draftPath(`${where}/bind`, name),
            );
        }
      } else if (step.type === 'extract') {
        target(step.target, `${where}/target`);
        assign(
          variables,
          step.as,
          {
            type:
              step.attribute === 'count'
                ? 'integer'
                : step.attribute === 'text'
                  ? 'string'
                  : 'boolean',
          },
          `${where}/as`,
        );
      } else if (step.type === 'check')
        condition(step.checkpoint.condition, `${where}/checkpoint/condition`, variables, remaining);
      else if (step.type === 'ai_step') {
        add(
          'ai',
          where,
          'ai_step 需要非定位恢复阶段的模型参与；关闭 AI 或仅定位恢复策略无法满足。',
          'warning',
        );
        step.checks.forEach((check, i) =>
          condition(check.condition, `${where}/checks/${i}/condition`, variables, remaining),
        );
      } else {
        if (step.condition) condition(step.condition, `${where}/condition`, variables, remaining);
        if (step.type === 'if') {
          steps(step.then, `${where}/then`, new Map(variables), remaining);
          steps(step.else ?? [], `${where}/else`, new Map(variables), remaining);
        }
        if (step.type === 'loop')
          steps(step.steps, `${where}/steps`, new Map(variables), remaining);
      }
    }
  };
  const variables = new Map<string, ValueInfo>();
  document.preconditions.forEach((check, i) =>
    condition(check.condition, `Case/preconditions/${i}/condition`, variables, caseBudget),
  );
  steps(document.steps, 'Case/steps', variables, caseBudget);
  document.checks.forEach((check, i) =>
    condition(check.condition, `Case/checks/${i}/condition`, variables, caseBudget),
  );
  return problems;
}
