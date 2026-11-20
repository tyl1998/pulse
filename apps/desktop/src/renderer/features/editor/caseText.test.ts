import { expect, test } from 'vitest';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import { copyDraftCase } from './caseDraftValidation';
import {
  caseStepAtOffset,
  caseStepPositions,
  caseTextIssue,
  caseTextRange,
  parseCaseText,
  readThreeSectionText,
  writeThreeSectionText,
} from './caseText';

test.each([
  {
    preconditions: '已登录，数据由用例外准备',
    steps: '搜索拿铁并加入购物车',
    checks: '商品数量为 1，金额正确',
  },
  {
    preconditions: '',
    steps: '逐行说明\n执行：这一行是引用，不是新段落\n预期：也属于步骤文本',
    checks: 'literal ${not-an-expression}',
  },
  { preconditions: '前置\n\n第二行', steps: '"quotes" \\ backslash\n\tand tabs', checks: '' },
])(
  'round-trips three natural-language sections as authoring data without parsing actions: %j',
  (sections) => {
    expect(readThreeSectionText(writeThreeSectionText(sections))).toEqual(sections);
  },
);

test.each([
  controlFlowCase.original_text,
  'plain imported authoring text',
  '{"前置":"first","前置":"second","执行":"step","预期":"result"}',
])('does not invent three sections from free-form or ambiguous original text: %s', (text) => {
  expect(readThreeSectionText(text)).toBeNull();
});

test('strict text parsing preserves the generated Case and does not mutate or normalize source data', () => {
  const text = JSON.stringify(controlFlowCase, null, 2);
  expect(parseCaseText(text)).toEqual(controlFlowCase);
  expect(copyDraftCase(parseCaseText(text))).toEqual(controlFlowCase);
  expect(parseCaseText('{"__proto__":{"polluted":true}}')).toEqual(
    JSON.parse('{"__proto__":{"polluted":true}}'),
  );
  expect(Object.prototype).not.toHaveProperty('polluted');
});

test.each([
  '',
  '{',
  '{"id": "first", "id": "second"}',
  '{"id": 1, "i\\u0064": 2}',
  '[{"id":1,"id":2}]',
  '{"steps": [],}',
  '{/* not JSON */ "steps": []}',
  '{"steps": []} {"steps": []}',
  '1e400',
  '{"value":1e400}',
  '前置：已登录。执行：搜索商品。预期：数量为 1。',
])('rejects ambiguous or unparsed text without treating it as executable JSON: %s', (text) => {
  expect(() => parseCaseText(text)).toThrow();
});

test('reports the duplicate key range, including escaped keys, rather than silently keeping the last value', () => {
  const text = '{"id": 1, "i\\u0064": 2}';
  try {
    parseCaseText(text);
    throw new Error('expected duplicate rejection');
  } catch (error) {
    const issue = caseTextIssue(text, error);
    expect(issue.message).toMatch(/重复 JSON 字段: id/);
    expect(text.slice(issue.offset, issue.offset + issue.length)).toBe('"i\\u0064"');
  }
});

test('localizes generated schema errors in a full document and a three-section array', () => {
  const value = {
    ...controlFlowCase,
    steps: [{ type: 'action', id: 'launch', action: { kind: 'launch', app_id: 42 } }],
  };
  const text = JSON.stringify(value, null, 2);
  try {
    copyDraftCase(parseCaseText(text));
    throw new Error('expected schema rejection');
  } catch (error) {
    const issue = caseTextIssue(text, error);
    expect(issue.message).toMatch(/Case\/steps\/0\/action\/app_id/);
    expect(text.slice(issue.offset, issue.offset + issue.length)).toBe('42');
    const section = JSON.stringify(value.steps);
    const sectionIssue = caseTextIssue(section, error, 'steps');
    expect(section.slice(sectionIssue.offset, sectionIssue.offset + sectionIssue.length)).toBe(
      '42',
    );
  }
});

test('maps all nested if/else/loop steps, but never mistakes checkpoint IDs or source text for steps', () => {
  const text = JSON.stringify(controlFlowCase, null, 2);
  const positions = caseStepPositions(text);
  expect(positions.map((position) => position.id)).toEqual([
    'launch',
    'search-product',
    'use-return',
    'banner-branch',
    'tap-banner',
    'wait-page',
    'bounded-observation',
    'iteration-label',
    'long-product',
    'swipe',
    'back',
    'confirm-check',
  ]);
  for (const position of positions) {
    expect(caseStepAtOffset(text, position.offset)).toBe(position.id);
    expect(
      JSON.parse(text.slice(position.offset, position.offset + position.length)),
    ).toHaveProperty('id', position.id);
  }
  expect(caseStepAtOffset(text, text.indexOf('"page-ready"'))).toBeNull();
  expect(caseStepAtOffset(text, text.indexOf('"product-name"'))).toBeNull();
  expect(caseStepAtOffset(text, text.indexOf('"selection-checked"'))).toBe('confirm-check');
  expect(caseStepAtOffset(text, text.indexOf('"original_text"'))).toBeNull();
  expect(caseStepAtOffset(text, -1)).toBeNull();
  expect(caseStepAtOffset(text, text.length)).toBeNull();
});

test('retains best-effort cursor navigation for incomplete JSON without accepting the document', () => {
  const text = '{"steps":[{"id":"pending","type":"action","action":';
  expect(() => parseCaseText(text)).toThrow();
  expect(caseStepPositions(text).map((position) => position.id)).toEqual(['pending']);
  expect(caseStepAtOffset(text, text.indexOf('"action"'))).toBe('pending');
});

test('selects escaped pointer keys and the nearest parent for a missing required field', () => {
  const text = '{"extra/key~":42,"steps":[{"id":"step","action":{"kind":"launch"}}]}';
  const escaped = caseTextRange(text, 'Case/extra~1key~0');
  expect(text.slice(escaped.offset, escaped.offset + escaped.length)).toBe('42');
  const missing = caseTextRange(text, 'Case/steps/0/action/app_id');
  expect(text.slice(missing.offset, missing.offset + missing.length)).toBe('{"kind":"launch"}');
  const section = '[{"id":"step","timeout_ms":0}]';
  const range = caseTextRange(section, 'Case/steps/0/timeout_ms', 'steps');
  expect(section.slice(range.offset, range.offset + range.length)).toBe('0');
});

test('unchanged non-ID arrays retain positional ranges for legal duplicate candidates', () => {
  const candidate = { by: 'point', value: { x: 100, y: 0, width: 100, height: 100 } };
  const reference = {
    locators: {
      duplicate: { candidates: [candidate, candidate], expect: { unique: true, visible: true } },
    },
  };
  const text = JSON.stringify(reference);
  const path = 'Case/locators/duplicate/candidates/1/value';
  expect(caseTextRange(text, path, undefined, reference)).toEqual({
    ...caseTextRange(text, path),
    found: true,
  });
});

test('confirmed issue ranges follow step and checkpoint IDs in a reordered rejected buffer', () => {
  const reference = {
    steps: [{ id: 'step', action: { text: { param_ref: 'query' } } }],
    checks: [{ id: 'check', condition: { expected: { param_ref: 'expected' } } }],
  };
  const text = JSON.stringify({
    steps: [{ id: 'new', action: { text: { param_ref: 'other' } } }, ...reference.steps],
    checks: [{ id: 'new-check', condition: { expected: 'Other' } }, ...reference.checks],
  });
  const step = caseTextRange(text, 'Case/steps/0/action/text/param_ref', undefined, reference);
  expect(step.found).toBe(true);
  expect(text.slice(step.offset, step.offset + step.length)).toBe('"query"');
  const check = caseTextRange(
    text,
    'Case/checks/0/condition/expected/param_ref',
    undefined,
    reference,
  );
  expect(check.found).toBe(true);
  expect(text.slice(check.offset, check.offset + check.length)).toBe('"expected"');
  expect(
    caseTextRange('{"steps":[]}', 'Case/steps/0/action/text', undefined, reference).found,
  ).toBe(false);
  const duplicated = JSON.stringify({ steps: [...reference.steps, ...reference.steps] });
  expect(caseTextRange(duplicated, 'Case/steps/0/action/text', undefined, reference).found).toBe(
    false,
  );
});
