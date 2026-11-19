import type { Case, Checkpoint, Step } from '@pulse/schema';
import caseSchema from '@pulse/schema/json/case.schema.json';

interface SchemaNode {
  $ref?: string;
  type?: string;
  const?: unknown;
  enum?: unknown[];
  anyOf?: SchemaNode[];
  oneOf?: SchemaNode[];
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

const definitions: Record<string, SchemaNode> = caseSchema.$defs;
const supportedKeys = new Set(
  '$ref type const enum anyOf oneOf properties required additionalProperties propertyNames items minItems minLength pattern minimum exclusiveMinimum maximum $schema $id $comment $defs title default discriminator'.split(
    ' ',
  ),
);

// Interpret the generated schema rather than compile it: renderer CSP forbids unsafe-eval.
function structureIssue(
  value: unknown,
  schema: SchemaNode,
  path: string,
  depth = 0,
): string | null {
  if (depth > 128) return `${path}: 结构嵌套过深`;
  const unsupported = Object.keys(schema).find((key) => !supportedKeys.has(key));
  if (unsupported) return `${path}: 不支持的 schema 约束 ${unsupported}`;
  if (schema.$ref) {
    const target = schema.$ref.startsWith('#/$defs/')
      ? definitions[schema.$ref.slice(8)]
      : undefined;
    if (!target) return `${path}: 不支持的 schema 引用`;
    const issue = structureIssue(value, target, path, depth);
    if (issue) return issue;
  }
  if ('const' in schema && value !== schema.const) return `${path}: 固定值不匹配`;
  if (schema.enum && !schema.enum.includes(value)) return `${path}: 枚举值无效`;
  if (schema.anyOf && !schema.anyOf.some((item) => !structureIssue(value, item, path, depth))) {
    return `${path}: 不符合允许的结构`;
  }
  if (
    schema.oneOf &&
    schema.oneOf.filter((item) => !structureIssue(value, item, path, depth)).length !== 1
  ) {
    return `${path}: 类型或必填字段无效`;
  }
  if (schema.type === 'string') {
    if (typeof value !== 'string') return `${path}: 需要字符串`;
    if (schema.minLength !== undefined && [...value].length < schema.minLength) {
      return `${path}: 字符串过短`;
    }
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) return `${path}: 字符串格式无效`;
  } else if (schema.type === 'number' || schema.type === 'integer') {
    if (typeof value !== 'number' || !Number.isFinite(value)) return `${path}: 需要有限数字`;
    if (schema.type === 'integer' && !Number.isInteger(value)) return `${path}: 需要整数`;
    if (schema.minimum !== undefined && value < schema.minimum) return `${path}: 数字过小`;
    if (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) {
      return `${path}: 数字过小`;
    }
    if (schema.maximum !== undefined && value > schema.maximum) return `${path}: 数字过大`;
  } else if (schema.type === 'boolean') {
    if (typeof value !== 'boolean') return `${path}: 需要布尔值`;
  } else if (schema.type === 'null') {
    if (value !== null) return `${path}: 需要 null`;
  } else if (schema.type === 'array') {
    if (!Array.isArray(value)) return `${path}: 需要数组`;
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      return `${path}: 数组不能为空`;
    }
    if (schema.items) {
      for (const [index, item] of value.entries()) {
        const issue = structureIssue(item, schema.items, `${path}/${index}`, depth + 1);
        if (issue) return issue;
      }
    }
  } else if (schema.type === 'object') {
    if (!isRecord(value) || Object.getOwnPropertySymbols(value).length)
      return `${path}: 需要 JSON 对象`;
    for (const key of schema.required ?? []) {
      if (!Object.hasOwn(value, key)) return `${path}/${key}: 缺少必填字段`;
    }
    for (const [key, item] of Object.entries(value)) {
      if (schema.propertyNames) {
        const issue = structureIssue(key, schema.propertyNames, `${path}/${key}`, depth + 1);
        if (issue) return issue;
      }
      const property = Object.hasOwn(schema.properties ?? {}, key)
        ? schema.properties?.[key]
        : undefined;
      if (!property && schema.additionalProperties === false)
        return `${path}/${key}: 不允许额外字段`;
      const child =
        property ??
        (typeof schema.additionalProperties === 'object' ? schema.additionalProperties : null);
      if (child) {
        const issue = structureIssue(item, child, `${path}/${key}`, depth + 1);
        if (issue) return issue;
      }
    }
  } else if (schema.type !== undefined) return `${path}: 不支持的 schema 类型`;
  return null;
}

export function assertDraftCase(value: unknown): asserts value is Case {
  const issue = structureIssue(value, caseSchema, 'Case');
  if (issue) throw new Error(`用例结构无效: ${issue}`);
  const document = value as Case;
  const stepIds = new Set<string>();
  const checkpointIds = new Set<string>();
  const checkpoint = (item: Checkpoint) => {
    if (checkpointIds.has(item.id)) throw new Error(`重复检查点 ID: ${item.id}`);
    checkpointIds.add(item.id);
  };
  const steps = (items: Step[]) => {
    for (const item of items) {
      if (stepIds.has(item.id)) throw new Error(`重复步骤 ID: ${item.id}`);
      stepIds.add(item.id);
      if (item.type === 'check') checkpoint(item.checkpoint);
      if (item.type === 'ai_step') item.checks.forEach(checkpoint);
      if (item.type === 'if') {
        steps(item.then);
        steps(item.else ?? []);
      }
      if (item.type === 'loop') steps(item.steps);
    }
  };
  document.preconditions.forEach(checkpoint);
  steps(document.steps);
  document.checks.forEach(checkpoint);
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
  const copy = JSON.parse(JSON.stringify(value)) as Case;
  const freeze = (item: unknown) => {
    if (typeof item !== 'object' || item === null) return;
    Object.values(item).forEach(freeze);
    Object.freeze(item);
  };
  freeze(copy);
  return copy;
}
