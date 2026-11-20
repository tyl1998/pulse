import {
  getNodeValue,
  parseTree,
  printParseErrorCode,
  type Node,
  type ParseError,
} from 'jsonc-parser';
import { isRecord, sameJson } from './caseDraftValidation';

export type ThreeSectionText = Record<'preconditions' | 'steps' | 'checks', string>;

/** A projection of original_text, not an executable model or a natural-language parser. */
export function readThreeSectionText(text: string): ThreeSectionText | null {
  try {
    const value = parseCaseText(text);
    if (
      isRecord(value) &&
      Object.keys(value).length === 3 &&
      typeof value['前置'] === 'string' &&
      typeof value['执行'] === 'string' &&
      typeof value['预期'] === 'string'
    ) {
      return { preconditions: value['前置'], steps: value['执行'], checks: value['预期'] };
    }
  } catch {
    // Existing free-form authoring text remains visible and is never guessed into sections.
  }
  const match = /^前置[：:]([\s\S]*?)\n执行[：:]([\s\S]*?)\n预期[：:]([\s\S]*)$/.exec(text);
  if (!match || (text.match(/^(?:前置|执行|预期)[：:]/gm)?.length ?? 0) !== 3) return null;
  return { preconditions: match[1] ?? '', steps: match[2] ?? '', checks: match[3] ?? '' };
}

export function writeThreeSectionText(text: ThreeSectionText): string {
  const plain = `前置：${text.preconditions}\n执行：${text.steps}\n预期：${text.checks}`;
  if (sameJson(readThreeSectionText(plain), text)) return plain;
  // Quoted headings inside a paragraph must round-trip too, rather than changing its boundaries.
  return JSON.stringify({ 前置: text.preconditions, 执行: text.steps, 预期: text.checks }, null, 2);
}

export interface CaseTextIssue {
  message: string;
  offset: number;
  length: number;
  path?: string;
}

class JsonTextError extends Error {
  constructor(
    message: string,
    readonly offset: number,
    readonly length: number,
  ) {
    super(message);
  }
}

/** Strict JSON only: comments, trailing commas and duplicate keys must not be silently saved. */
export function parseCaseText(text: string): unknown {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors, { disallowComments: true, allowTrailingComma: false });
  const first = errors[0];
  if (first || !root) {
    throw new JsonTextError(
      `JSON 语法无效: ${first ? printParseErrorCode(first.error) : '需要 JSON 内容'}`,
      first?.offset ?? 0,
      first?.length ?? 1,
    );
  }
  const visit = (node: Node, depth = 0) => {
    if (depth > 128) throw new JsonTextError('JSON 结构嵌套过深', node.offset, node.length);
    if (node.type === 'number' && !Number.isFinite(node.value)) {
      throw new JsonTextError(
        'JSON 数字超出可保存范围，未将它静默转换为 null',
        node.offset,
        node.length,
      );
    }
    if (node.type === 'object') {
      const keys = new Set<string>();
      for (const property of node.children ?? []) {
        const key = property.children?.[0];
        if (!key || typeof key.value !== 'string') continue;
        if (keys.has(key.value))
          throw new JsonTextError(`重复 JSON 字段: ${key.value}`, key.offset, key.length);
        keys.add(key.value);
      }
    }
    for (const child of node.children ?? []) visit(child, depth + 1);
  };
  visit(root);
  return JSON.parse(text) as unknown;
}

export function caseTextIssue(text: string, error: unknown, section?: string): CaseTextIssue {
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof JsonTextError) {
    return { message, offset: error.offset, length: error.length };
  }
  const path = /Case((?:\/[^:]+)*):/.exec(message)?.[1];
  return {
    message,
    ...caseTextRange(text, `Case${path ?? ''}`, section),
    ...(path === undefined ? {} : { path: `Case${path}` }),
  };
}

/** Missing fields select the nearest existing parent; pointer escapes are not literal key text. */
export function caseTextRange(
  text: string,
  path: string,
  section?: string,
  reference?: unknown,
): Pick<CaseTextIssue, 'offset' | 'length'> & { found?: boolean } {
  const segments = path
    .split('/')
    .slice(1)
    .map((part) => part.replaceAll('~1', '/').replaceAll('~0', '~'));
  if (section && segments[0] === section) segments.shift();
  let node = parseTree(text);
  let expected: unknown = reference;
  let found = !!node;
  for (const part of segments) {
    let next: Node | undefined;
    if (node?.type === 'object') {
      next = propertyValue(node, part);
      expected = isRecord(expected) && Object.hasOwn(expected, part) ? expected[part] : undefined;
    } else if (node?.type === 'array' && /^\d+$/.test(part)) {
      const item: unknown = Array.isArray(expected) ? expected[Number(part)] : undefined;
      if (
        reference !== undefined &&
        Array.isArray(expected) &&
        !(isRecord(item) && typeof item['id'] === 'string') &&
        sameJson(getNodeValue(node), expected)
      ) {
        next = node.children?.[Number(part)];
      } else if (reference !== undefined && Array.isArray(expected)) {
        // Confirmed diagnostics follow stable IDs, not the indices of a rejected text buffer.
        const matches = (node.children ?? []).filter((child) =>
          isRecord(item) && typeof item['id'] === 'string'
            ? child.type === 'object' && propertyValue(child, 'id')?.value === item['id']
            : sameJson(getNodeValue(child), item),
        );
        next = matches.length === 1 ? matches[0] : undefined;
      } else next = node.children?.[Number(part)];
      expected = item;
    }
    if (!next) {
      found = false;
      break;
    }
    node = next;
  }
  return {
    offset: node?.offset ?? 0,
    length: node?.length ?? 1,
    ...(reference === undefined ? {} : { found }),
  };
}

export interface CaseStepPosition {
  id: string;
  offset: number;
  length: number;
}

function propertyValue(node: Node, name: string): Node | undefined {
  return node.children?.find((child) => child.children?.[0]?.value === name)?.children?.[1];
}

export function caseStepPositions(text: string): CaseStepPosition[] {
  const root = parseTree(text);
  const positions: CaseStepPosition[] = [];
  const visit = (array: Node | undefined, depth = 0) => {
    if (array?.type !== 'array' || depth > 128) return;
    for (const node of array.children ?? []) {
      if (node.type !== 'object') continue;
      const id = propertyValue(node, 'id')?.value;
      if (typeof id === 'string') positions.push({ id, offset: node.offset, length: node.length });
      const type = propertyValue(node, 'type')?.value;
      if (type === 'if') {
        visit(propertyValue(node, 'then'), depth + 1);
        visit(propertyValue(node, 'else'), depth + 1);
      } else if (type === 'loop') visit(propertyValue(node, 'steps'), depth + 1);
    }
  };
  if (root?.type === 'object') visit(propertyValue(root, 'steps'));
  return positions;
}

export function caseStepAtOffset(text: string, offset: number): string | null {
  // Descendants follow their parent; the last containing range is the actual cursor step.
  return (
    caseStepPositions(text).findLast(
      (position) => offset >= position.offset && offset < position.offset + position.length,
    )?.id ?? null
  );
}
