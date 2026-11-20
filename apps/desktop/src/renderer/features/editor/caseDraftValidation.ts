import type { Case, Checkpoint, Flow, Step } from '@pulse/schema';
import caseSchema from '@pulse/schema/json/case.schema.json';
import flowSchema from '@pulse/schema/json/flow.schema.json';

interface SchemaNode {
  $ref?: string;
  type?: string;
  const?: unknown;
  enum?: unknown[];
  anyOf?: SchemaNode[];
  oneOf?: SchemaNode[];
  discriminator?: { propertyName: string; mapping: Record<string, string> };
  properties?: Record<string, SchemaNode>;
  required?: string[];
  additionalProperties?: boolean | SchemaNode;
  propertyNames?: SchemaNode;
  items?: SchemaNode;
  minItems?: number;
  minLength?: number;
  pattern?: string;
  minimum?: number;
  exclusiveMinimum?: number;
  maximum?: number;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
  );
}

const supportedKeys = new Set(
  '$ref type const enum anyOf oneOf properties required additionalProperties propertyNames items minItems minLength pattern minimum exclusiveMinimum maximum $schema $id $comment $defs title default discriminator'.split(
    ' ',
  ),
);

export interface DraftSchemaIssue {
  path: string;
  message: string;
}

export const DRAFT_SCHEMA_ISSUE_LIMIT = 100;

export function draftPath(parent: string, key: string | number): string {
  return `${parent}/${String(key).replaceAll('~', '~0').replaceAll('/', '~1')}`;
}

// Interpret the generated schema rather than compile it: renderer CSP forbids unsafe-eval.
function structureIssues(
  value: unknown,
  schema: SchemaNode,
  path: string,
  definitions: Record<string, SchemaNode>,
  depth = 0,
): DraftSchemaIssue[] {
  const issue = (message: string): DraftSchemaIssue[] => [{ path, message }];
  if (depth > 128) return issue('结构嵌套过深');
  const unsupported = Object.keys(schema).find((key) => !supportedKeys.has(key));
  if (unsupported) return issue(`不支持的 schema 约束 ${unsupported}`);
  if (schema.$ref) {
    const target = schema.$ref.startsWith('#/$defs/')
      ? definitions[schema.$ref.slice(8)]
      : undefined;
    if (!target) return issue('不支持的 schema 引用');
    const found = structureIssues(value, target, path, definitions, depth);
    if (found.length) return found;
  }
  if ('const' in schema && value !== schema.const) return issue('固定值不匹配');
  if (schema.enum && !schema.enum.includes(value)) return issue('枚举值无效');
  if (schema.anyOf) {
    const alternatives = schema.anyOf.map((item) => ({
      schema: item.$ref ? definitions[item.$ref.replace('#/$defs/', '')] : item,
      issues: structureIssues(value, item, path, definitions, depth),
    }));
    if (!alternatives.some((alternative) => !alternative.issues.length)) {
      const matching = alternatives.filter(({ schema: candidate }) => {
        if (!candidate) return false;
        if (candidate.type === 'null') return value === null;
        if (candidate.type === 'array') return Array.isArray(value);
        if (candidate.type === 'number' || candidate.type === 'integer')
          return typeof value === 'number';
        if (candidate.type === 'string' || candidate.type === 'boolean')
          return typeof value === candidate.type;
        if (candidate.type === 'object')
          return (
            isRecord(value) &&
            Object.keys(candidate.properties ?? {}).some((key) => Object.hasOwn(value, key))
          );
        return !!candidate.oneOf && isRecord(value);
      });
      // A recognisable union variant retains all its leaf errors, rather than a parent-only error.
      return matching.length === 1
        ? (matching[0]?.issues ?? issue('不符合允许的结构'))
        : issue('不符合允许的结构');
    }
  }
  let discriminatedRef: string | undefined;
  if (schema.oneOf && schema.discriminator && isRecord(value)) {
    const type = value[schema.discriminator.propertyName];
    const ref = typeof type === 'string' ? schema.discriminator.mapping[type] : undefined;
    if (ref && schema.oneOf.some((item) => item.$ref === ref)) discriminatedRef = ref;
  }
  if (discriminatedRef) {
    // Generated discriminator branches are disjoint. Validate once, not twice per nested block.
    const found = structureIssues(value, { $ref: discriminatedRef }, path, definitions, depth);
    if (found.length) return found;
  } else if (
    schema.oneOf &&
    schema.oneOf.filter((item) => !structureIssues(value, item, path, definitions, depth).length)
      .length !== 1
  ) {
    return issue('类型或必填字段无效');
  }
  const issues: DraftSchemaIssue[] = [];
  if (schema.type === 'string') {
    if (typeof value !== 'string') return issue('需要字符串');
    if (schema.minLength !== undefined && [...value].length < schema.minLength) {
      return issue('字符串过短');
    }
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) return issue('字符串格式无效');
  } else if (schema.type === 'number' || schema.type === 'integer') {
    if (typeof value !== 'number' || !Number.isFinite(value)) return issue('需要有限数字');
    if (schema.type === 'integer' && !Number.isInteger(value)) return issue('需要整数');
    if (schema.minimum !== undefined && value < schema.minimum) return issue('数字过小');
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
      return issue('数字过小');
    }
    if (schema.maximum !== undefined && value > schema.maximum) return issue('数字过大');
  } else if (schema.type === 'boolean') {
    if (typeof value !== 'boolean') return issue('需要布尔值');
  } else if (schema.type === 'null') {
    if (value !== null) return issue('需要 null');
  } else if (schema.type === 'array') {
    if (!Array.isArray(value)) return issue('需要数组');
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      issues.push({ path, message: '数组不能为空' });
    }
    if (schema.items) {
      for (const [index, item] of value.entries()) {
        issues.push(
          ...structureIssues(item, schema.items, draftPath(path, index), definitions, depth + 1),
        );
        if (issues.length >= DRAFT_SCHEMA_ISSUE_LIMIT) break;
      }
    }
  } else if (schema.type === 'object') {
    if (!isRecord(value) || Object.getOwnPropertySymbols(value).length)
      return issue('需要 JSON 对象');
    for (const key of schema.required ?? []) {
      if (!Object.hasOwn(value, key))
        issues.push({ path: draftPath(path, key), message: '缺少必填字段' });
    }
    for (const [key, item] of Object.entries(value)) {
      if (schema.propertyNames) {
        issues.push(
          ...structureIssues(
            key,
            schema.propertyNames,
            draftPath(path, key),
            definitions,
            depth + 1,
          ),
        );
      }
      const property = Object.hasOwn(schema.properties ?? {}, key)
        ? schema.properties?.[key]
        : undefined;
      if (!property && schema.additionalProperties === false)
        issues.push({ path: draftPath(path, key), message: '不允许额外字段' });
      const child =
        property ??
        (typeof schema.additionalProperties === 'object' ? schema.additionalProperties : null);
      if (child) {
        issues.push(...structureIssues(item, child, draftPath(path, key), definitions, depth + 1));
      }
      if (issues.length >= DRAFT_SCHEMA_ISSUE_LIMIT) break;
    }
  } else if (schema.type !== undefined) return issue('不支持的 schema 类型');
  return issues.slice(0, DRAFT_SCHEMA_ISSUE_LIMIT);
}

export function caseSchemaIssues(value: unknown): DraftSchemaIssue[] {
  const issues = structureIssues(value, caseSchema, 'Case', caseSchema.$defs);
  return issues.length ? issues : procedureIdIssues(value as Case, 'Case');
}

export function assertDraftCase(value: unknown): asserts value is Case {
  const issue = caseSchemaIssues(value)[0];
  if (issue) throw new Error(`用例结构无效: ${issue.path}: ${issue.message}`);
}

function assertDraftFlow(value: unknown): asserts value is Flow {
  const issues = structureIssues(value, flowSchema, 'Flow', flowSchema.$defs);
  const issue = (issues.length ? issues : procedureIdIssues(value as Flow, 'Flow'))[0];
  if (issue) throw new Error(`流程结构无效: ${issue.path}: ${issue.message}`);
}

function procedureIdIssues(document: Case | Flow, path: string): DraftSchemaIssue[] {
  const issues: DraftSchemaIssue[] = [];
  const stepIds = new Set<string>();
  const checkpointIds = new Set<string>();
  const checkpoint = (item: Checkpoint, where: string) => {
    if (checkpointIds.has(item.id))
      issues.push({ path: draftPath(where, 'id'), message: `重复检查点 ID: ${item.id}` });
    checkpointIds.add(item.id);
  };
  const steps = (items: Step[], where: string) => {
    for (const [index, item] of items.entries()) {
      const current = draftPath(where, index);
      if (stepIds.has(item.id))
        issues.push({ path: draftPath(current, 'id'), message: `重复步骤 ID: ${item.id}` });
      stepIds.add(item.id);
      if (item.type === 'check') checkpoint(item.checkpoint, draftPath(current, 'checkpoint'));
      if (item.type === 'ai_step')
        item.checks.forEach((check, i) => checkpoint(check, `${current}/checks/${i}`));
      if (item.type === 'if') {
        steps(item.then, draftPath(current, 'then'));
        steps(item.else ?? [], draftPath(current, 'else'));
      }
      if (item.type === 'loop') steps(item.steps, draftPath(current, 'steps'));
    }
  };
  if ('preconditions' in document)
    document.preconditions.forEach((check, i) => checkpoint(check, `${path}/preconditions/${i}`));
  steps(document.steps, draftPath(path, 'steps'));
  if ('checks' in document)
    document.checks.forEach((check, i) => checkpoint(check, `${path}/checks/${i}`));
  return issues.slice(0, DRAFT_SCHEMA_ISSUE_LIMIT);
}

export function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return (
      left.length === right.length && left.every((item, index) => sameJson(item, right[index]))
    );
  }
  if (isRecord(left) && isRecord(right)) {
    return (
      Object.keys(left).length === Object.keys(right).length &&
      Object.entries(left).every(
        ([key, item]) => Object.hasOwn(right, key) && sameJson(item, right[key]),
      )
    );
  }
  return false;
}

export function copyDraftCase(value: unknown): Case {
  assertDraftCase(value);
  const copy = frozenCopy(value);
  assertDraftCase(copy);
  return copy;
}

/** Generated document structure and procedure IDs only, not project-wide semantics. */
export function copyDraftFlow(value: unknown): Flow {
  assertDraftFlow(value);
  const copy = frozenCopy(value);
  assertDraftFlow(copy);
  return copy;
}

function frozenCopy<T extends Case | Flow>(value: T): T {
  const copy = JSON.parse(JSON.stringify(value)) as T;
  const freeze = (item: unknown) => {
    if (typeof item !== 'object' || item === null) return;
    Object.values(item).forEach(freeze);
    Object.freeze(item);
  };
  freeze(copy);
  return copy;
}
