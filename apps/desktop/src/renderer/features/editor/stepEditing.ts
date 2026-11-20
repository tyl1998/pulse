import type { Action, Case, Checkpoint, Predicate, Step, Target } from '@pulse/schema';
import { sameJson } from './caseDraftValidation';

export interface StepListLocation {
  parentId: string | null;
  branch: 'steps' | 'then' | 'else';
}

export const ROOT_STEPS: StepListLocation = { parentId: null, branch: 'steps' };
export const STEP_TYPES: readonly Step['type'][] = [
  'action',
  'wait',
  'check',
  'extract',
  'flow',
  'if',
  'loop',
  'ai_step',
];

export function findStep(steps: readonly Step[], id: string): Step | undefined {
  for (const step of steps) {
    if (step.id === id) return step;
    const child =
      step.type === 'if'
        ? findStep([...step.then, ...(step.else ?? [])], id)
        : step.type === 'loop'
          ? findStep(step.steps, id)
          : undefined;
    if (child) return child;
  }
  return undefined;
}

export function editStepList(
  document: Case,
  location: StepListLocation,
  edit: (steps: Step[]) => Step[],
): Case {
  if (location.parentId === null) {
    if (location.branch !== 'steps') throw new Error('根步骤块无效');
    return { ...document, steps: edit(document.steps) as Case['steps'] };
  }
  let found = false;
  const walk = (steps: Step[]): Step[] =>
    steps.map((step) => {
      if (step.id === location.parentId) {
        found = true;
        if (step.type === 'if' && location.branch === 'then') {
          return { ...step, then: edit(step.then) as typeof step.then };
        }
        if (step.type === 'if' && location.branch === 'else') {
          return { ...step, else: edit(step.else ?? []) };
        }
        if (step.type === 'loop' && location.branch === 'steps') {
          return { ...step, steps: edit(step.steps) as typeof step.steps };
        }
        throw new Error('步骤块与控制流类型不匹配');
      }
      if (step.type === 'if') {
        return {
          ...step,
          then: walk(step.then) as typeof step.then,
          ...(step.else === undefined ? {} : { else: walk(step.else) }),
        };
      }
      if (step.type === 'loop') return { ...step, steps: walk(step.steps) as typeof step.steps };
      return step;
    });
  const steps = walk(document.steps);
  if (!found) throw new Error('步骤块已变化，请重新打开编辑');
  return { ...document, steps: steps as Case['steps'] };
}

export function replaceStep(document: Case, original: Step, next: Step): Case {
  if (!sameJson(findStep(document.steps, original.id), original)) {
    throw new Error('此步骤已被修改，请恢复已保存内容后再编辑');
  }
  const walk = (steps: Step[]): Step[] =>
    steps.map((step) => {
      if (step.id === original.id) return next;
      if (step.type === 'if') {
        return {
          ...step,
          then: walk(step.then) as typeof step.then,
          ...(step.else === undefined ? {} : { else: walk(step.else) }),
        };
      }
      if (step.type === 'loop') return { ...step, steps: walk(step.steps) as typeof step.steps };
      return step;
    });
  return { ...document, steps: walk(document.steps) as Case['steps'] };
}

export function targetLabel(target: Target): string {
  if ('locator_ref' in target) return target.locator_ref;
  const candidate = target.locator.candidates[0];
  return candidate.by === 'point'
    ? `point (${candidate.value.x}, ${candidate.value.y})`
    : `${candidate.by}: ${candidate.value}`;
}

export function stepLabel(step: Step): string {
  switch (step.type) {
    case 'action': {
      const action = step.action;
      if ('target' in action) return `${action.kind} / ${targetLabel(action.target)}`;
      if ('app_id' in action) return `${action.kind} / ${action.app_id}`;
      if (action.kind === 'key') return `key / ${action.key}`;
      return `swipe / ${action.duration_ms} ms`;
    }
    case 'check':
      return `check / ${step.checkpoint.condition.operator} / ${step.checkpoint.condition.source} / ${targetLabel(step.checkpoint.condition.target)}`;
    case 'wait':
    case 'if':
      return `${step.type} / ${step.condition.operator} / ${step.condition.source}`;
    case 'extract':
      return `extract / ${step.attribute} / ${step.source} / ${step.as}`;
    case 'flow':
      return `flow / ${step.flow_ref}`;
    case 'loop':
      return `loop / 最多 ${step.max_iterations} 次`;
    case 'ai_step':
      return `ai_step / ${step.goal}`;
  }
}

export function defaultTarget(document?: Case): Target {
  const ref = document && Object.keys(document.locators)[0];
  return ref
    ? { locator_ref: ref }
    : {
        locator: {
          candidates: [{ by: 'text', value: '目标文本' }],
          expect: { unique: true, visible: true },
        },
      };
}

export function changeAction(action: Action, kind: Action['kind']): Action {
  if (action.kind === kind) return action;
  const target = 'target' in action ? action.target : defaultTarget();
  switch (kind) {
    case 'tap':
      return { kind, target };
    case 'long':
      return { kind, target, duration_ms: 500 };
    case 'input':
      return { kind, target, text: '', mode: 'replace' };
    case 'key':
      return { kind, key: 'BACK' };
    case 'swipe':
      return {
        kind,
        start: { x: 0, y: 0, width: 1080, height: 2400 },
        end: { x: 0, y: 0, width: 1080, height: 2400 },
        duration_ms: 500,
      };
    case 'launch':
    case 'stop':
    case 'clear':
      return { kind, app_id: 'app_id' in action ? action.app_id : 'app.id' };
  }
}

export function changePredicate(condition: Predicate, operator: Predicate['operator']): Predicate {
  if (condition.operator === operator) return condition;
  const base = {
    target: condition.target,
    source: condition.source,
    ...(condition.timeout_ms === undefined ? {} : { timeout_ms: condition.timeout_ms }),
  };
  switch (operator) {
    case 'exists':
    case 'page_is':
      return { ...base, operator };
    case 'not_exists':
      return {
        ...base,
        operator,
        scope: condition.target,
        observation_window_ms: 1000,
        poll_interval_ms: 500,
      };
    case 'visible':
    case 'enabled':
    case 'checked':
      return {
        ...base,
        operator,
        expected:
          'expected' in condition && typeof condition.expected === 'boolean'
            ? condition.expected
            : true,
      };
    case 'text_equals':
    case 'contains':
    case 'regex':
    case 'semantic': {
      const expected =
        'expected' in condition &&
        (typeof condition.expected === 'string' || typeof condition.expected === 'object')
          ? condition.expected
          : '';
      return operator === 'semantic'
        ? { ...base, operator, expected, timeout_ms: condition.timeout_ms ?? 10000 }
        : { ...base, operator, expected };
    }
    case 'count_eq':
    case 'count_gt':
    case 'count_lt':
      return {
        ...base,
        operator,
        expected:
          'expected' in condition &&
          (typeof condition.expected === 'object' ||
            (typeof condition.expected === 'number' &&
              Number.isSafeInteger(condition.expected) &&
              condition.expected >= 0))
            ? condition.expected
            : 1,
      };
    case 'number_cmp':
      return {
        ...base,
        operator,
        expected:
          'expected' in condition &&
          (typeof condition.expected === 'number' || typeof condition.expected === 'object')
            ? condition.expected
            : 0,
        comparison: 'eq',
        unit: 'unit',
        precision: 2,
      };
  }
}

function documentIds(document: Case) {
  const steps = new Set<string>();
  const checks = new Set([...document.preconditions, ...document.checks].map((item) => item.id));
  const walk = (items: Step[]) => {
    for (const step of items) {
      steps.add(step.id);
      if (step.type === 'check') checks.add(step.checkpoint.id);
      if (step.type === 'ai_step') step.checks.forEach((item) => checks.add(item.id));
      if (step.type === 'if') {
        walk(step.then);
        walk(step.else ?? []);
      }
      if (step.type === 'loop') walk(step.steps);
    }
  };
  walk(document.steps);
  return { steps, checks };
}

function uniqueId(prefix: string, used: Set<string>): string {
  let index = 1;
  while (used.has(`${prefix}_${index}`)) index++;
  const id = `${prefix}_${index}`;
  used.add(id);
  return id;
}

export function createCheckpoint(document: Case, pending: readonly Checkpoint[] = []): Checkpoint {
  const { checks } = documentIds(document);
  pending.forEach((item) => checks.add(item.id));
  return {
    id: uniqueId('checkpoint', checks),
    required: true,
    condition: { operator: 'exists', source: 'native', target: defaultTarget(document) },
  };
}

export function createStep(document: Case, type: Step['type']): Step {
  const used = documentIds(document).steps;
  const id = uniqueId(type, used);
  const condition: Predicate = {
    operator: 'exists',
    source: 'native',
    target: defaultTarget(document),
  };
  const child = (): Step => ({
    type: 'action',
    id: uniqueId('action', used),
    action: { kind: 'tap', target: defaultTarget(document) },
  });
  switch (type) {
    case 'action':
      return { type, id, action: { kind: 'tap', target: defaultTarget(document) } };
    case 'wait':
      return { type, id, condition };
    case 'check':
      return { type, id, checkpoint: createCheckpoint(document) };
    case 'extract':
      return {
        type,
        id,
        target: defaultTarget(document),
        source: 'native',
        attribute: 'text',
        as: 'value',
      };
    case 'flow':
      return { type, id, flow_ref: 'flow', arguments: {}, bind: {} };
    case 'if':
      return { type, id, condition, then: [child()], else: [] };
    case 'loop':
      return { type, id, max_iterations: 1, steps: [child()] };
    case 'ai_step':
      return {
        type,
        id,
        goal: '描述需要完成的目标',
        timeout_ms: 30000,
        checks: [createCheckpoint(document)],
      };
  }
}
