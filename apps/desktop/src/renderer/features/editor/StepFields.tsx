import type {
  Action,
  Case,
  Checkpoint,
  Locator,
  Parameter,
  ObservationSource,
  Point,
  Predicate,
  Step,
  Target,
  Value,
} from '@pulse/schema';
import { useId, useState, type ReactNode } from 'react';
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react';
import { Button, IconButton, Input, Select } from '../../shared/ui';
import { changeAction, changePredicate, createCheckpoint, defaultTarget } from './stepEditing';

export type FieldChange<T> = (value: T, save?: boolean, clearedIssue?: string) => void;
export type FieldIssueHandler = (key: string, issue: string | null) => void;

export function TextField({
  label,
  value,
  onChange,
  multiline = false,
  list,
}: {
  label: string;
  value: string;
  onChange: FieldChange<string>;
  multiline?: boolean;
  list?: string;
}) {
  return (
    <label className="case-editor-field" data-field-label={label}>
      <span>{label}</span>
      {multiline ? (
        <textarea
          className="ui-input case-editor-textarea"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          rows={3}
        />
      ) : (
        <Input
          size="sm"
          value={value}
          list={list}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </label>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  min = 1,
  optional = false,
  integer = true,
}: {
  label: string;
  value: number | undefined;
  onChange: FieldChange<number | undefined>;
  min?: number;
  optional?: boolean;
  integer?: boolean;
}) {
  return (
    <label className="case-editor-field" data-field-label={label}>
      <span>{label}</span>
      <Input
        size="sm"
        type="number"
        value={value !== undefined && Number.isFinite(value) ? value : ''}
        min={min}
        step={integer ? 1 : 'any'}
        placeholder={optional ? '使用默认值' : undefined}
        onChange={(event) =>
          onChange(
            event.target.value === ''
              ? optional
                ? undefined
                : Number.NaN
              : Number(event.target.value),
          )
        }
      />
    </label>
  );
}

export function ChoiceField<T extends string>({
  label,
  value,
  choices,
  onChange,
}: {
  label: string;
  value: T;
  choices: readonly T[];
  onChange: FieldChange<T>;
}) {
  return (
    <div className="case-editor-field" data-field-label={label}>
      <span>{label}</span>
      <Select
        size="sm"
        aria-label={label}
        value={value}
        items={choices.map((choice) => ({ value: choice, label: choice }))}
        onValueChange={(choice) => onChange(choice as T, true)}
      />
    </div>
  );
}

export function BooleanField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: boolean;
  onChange: FieldChange<boolean>;
}) {
  return (
    <label className="case-editor-checkbox" data-field-label={label}>
      <input
        type="checkbox"
        checked={value}
        onChange={(event) => onChange(event.target.checked, true)}
      />
      <span>{label}</span>
    </label>
  );
}

function PointFields({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Point;
  onChange: FieldChange<Point>;
}) {
  return (
    <fieldset className="case-editor-group" data-field-scope={label}>
      <legend>{label}</legend>
      <div className="case-editor-grid">
        {(['x', 'y', 'width', 'height'] as const).map((key) => (
          <NumberField
            key={key}
            label={key}
            min={key === 'x' || key === 'y' ? 0 : 1}
            value={value[key]}
            onChange={(next, save) => onChange({ ...value, [key]: next ?? Number.NaN }, save)}
          />
        ))}
      </div>
    </fieldset>
  );
}

export function LocatorFields({
  value,
  onChange,
}: {
  value: Locator;
  onChange: FieldChange<Locator>;
}) {
  const candidates = value.candidates;
  const updateCandidate = (
    index: number,
    candidate: Locator['candidates'][number],
    save?: boolean,
  ) =>
    onChange(
      {
        ...value,
        candidates: candidates.map((item, position) =>
          position === index ? candidate : item,
        ) as Locator['candidates'],
      },
      save,
    );
  const move = (index: number, offset: number) => {
    const next = [...candidates];
    const [candidate] = next.splice(index, 1);
    if (!candidate) return;
    next.splice(index + offset, 0, candidate);
    onChange({ ...value, candidates: next as Locator['candidates'] }, true);
  };
  return (
    <div className="case-editor-fields">
      {candidates.map((candidate, index) => (
        <fieldset className="case-editor-group" key={index} data-candidate-index={index}>
          <legend>候选 {index + 1}</legend>
          <div className="case-editor-grid">
            <ChoiceField
              label="定位方式"
              value={candidate.by}
              choices={['res_id', 'text', 'desc', 'xpath', 'ocr_text', 'image', 'point']}
              onChange={(by, save) => {
                if (by === candidate.by) return;
                const scope = candidate.scope === undefined ? {} : { scope: candidate.scope };
                updateCandidate(
                  index,
                  by === 'point'
                    ? { by, value: { x: 0, y: 0, width: 1080, height: 2400 }, ...scope }
                    : {
                        by,
                        value: typeof candidate.value === 'string' ? candidate.value : '目标文本',
                        ...scope,
                      },
                  save,
                );
              }}
            />
            <TextField
              label="候选作用域（可选）"
              value={candidate.scope ?? ''}
              onChange={(scope, save) => {
                const next = { ...candidate };
                if (scope === '') delete next.scope;
                else next.scope = scope;
                updateCandidate(index, next, save);
              }}
            />
          </div>
          {candidate.by === 'point' ? (
            <PointFields
              label="坐标与基准尺寸"
              value={candidate.value}
              onChange={(point, save) =>
                updateCandidate(index, { ...candidate, value: point }, save)
              }
            />
          ) : (
            <TextField
              label="定位值"
              value={candidate.value}
              onChange={(text, save) => updateCandidate(index, { ...candidate, value: text }, save)}
            />
          )}
          <div className="case-editor-actions">
            <IconButton
              label={`上移候选 ${index + 1}`}
              icon={<ArrowUp />}
              size="sm"
              variant="ghost"
              disabled={index === 0}
              onClick={() => move(index, -1)}
            />
            <IconButton
              label={`下移候选 ${index + 1}`}
              icon={<ArrowDown />}
              size="sm"
              variant="ghost"
              disabled={index === candidates.length - 1}
              onClick={() => move(index, 1)}
            />
            <IconButton
              label={`删除候选 ${index + 1}`}
              icon={<Trash2 />}
              size="sm"
              variant="ghost"
              disabled={candidates.length === 1}
              onClick={() =>
                onChange(
                  {
                    ...value,
                    candidates: candidates.filter(
                      (_, position) => position !== index,
                    ) as Locator['candidates'],
                  },
                  true,
                )
              }
            />
          </div>
        </fieldset>
      ))}
      <Button
        size="sm"
        variant="ghost"
        onClick={() =>
          onChange(
            { ...value, candidates: [...candidates, { by: 'text', value: '目标文本' }] },
            true,
          )
        }
      >
        <Plus aria-hidden="true" />
        添加定位候选
      </Button>
      <div className="case-editor-actions">
        <BooleanField
          label="要求唯一"
          value={value.expect.unique}
          onChange={(unique, save) =>
            onChange({ ...value, expect: { ...value.expect, unique } }, save)
          }
        />
        <BooleanField
          label="要求可见"
          value={value.expect.visible}
          onChange={(visible, save) =>
            onChange({ ...value, expect: { ...value.expect, visible } }, save)
          }
        />
      </div>
      <p className="case-draft-note">候选按顺序定位。这里编辑期望，不代表已通过设备唯一性验证。</p>
    </div>
  );
}

export function TargetFields({
  label = '目标',
  value,
  document,
  onChange,
}: {
  label?: string;
  value: Target;
  document: Case;
  onChange: FieldChange<Target>;
}) {
  const listId = useId();
  return (
    <fieldset className="case-editor-group" data-field-scope={label}>
      <legend>{label}</legend>
      <ChoiceField
        label="目标类型"
        value={'locator_ref' in value ? 'locator_ref' : 'locator'}
        choices={['locator_ref', 'locator']}
        onChange={(type, save) => {
          if (type === 'locator_ref')
            onChange({ locator_ref: Object.keys(document.locators)[0] ?? 'target' }, save);
          else {
            const declared =
              'locator_ref' in value
                ? Object.hasOwn(document.locators, value.locator_ref)
                  ? document.locators[value.locator_ref]
                  : undefined
                : value.locator;
            const fallback = defaultTarget();
            onChange(
              {
                locator:
                  declared ??
                  ('locator' in fallback
                    ? fallback.locator
                    : {
                        candidates: [{ by: 'text', value: '目标文本' }],
                        expect: { unique: true, visible: true },
                      }),
              },
              save,
            );
          }
        }}
      />
      {'locator_ref' in value ? (
        <>
          <TextField
            label="定位器引用"
            value={value.locator_ref}
            list={listId}
            onChange={(locator_ref, save) => onChange({ locator_ref }, save)}
          />
          <datalist id={listId}>
            {Object.keys(document.locators).map((ref) => (
              <option key={ref} value={ref} />
            ))}
          </datalist>
          <p className="case-draft-note">
            引用的用例定位器可在下方编辑；配置中的定位器由工程提供。
          </p>
        </>
      ) : (
        <LocatorFields
          value={value.locator}
          onChange={(locator, save) => onChange({ locator }, save)}
        />
      )}
    </fieldset>
  );
}

export type ValueMode =
  'text' | 'number' | 'boolean' | 'param_ref' | 'var_ref' | 'data_ref' | 'secret_ref';
const TEXT_MODES: readonly ValueMode[] = ['text', 'param_ref', 'var_ref', 'data_ref', 'secret_ref'];
const EXPECTED_TEXT_MODES: readonly ValueMode[] = ['text', 'param_ref', 'data_ref'];
const NUMBER_MODES: readonly ValueMode[] = ['number', 'param_ref', 'data_ref'];
const ALL_MODES: readonly ValueMode[] = [
  'text',
  'number',
  'boolean',
  'param_ref',
  'var_ref',
  'data_ref',
  'secret_ref',
];

function valueMode(value: Value): ValueMode {
  if (typeof value === 'string') return 'text';
  if (typeof value === 'number') return 'number';
  if (typeof value === 'boolean') return 'boolean';
  if ('param_ref' in value) return 'param_ref';
  if ('var_ref' in value) return 'var_ref';
  if ('secret_ref' in value) return 'secret_ref';
  return 'data_ref';
}

function defaultValue(mode: ValueMode): Value {
  switch (mode) {
    case 'text':
      return '';
    case 'number':
      return 0;
    case 'boolean':
      return false;
    case 'param_ref':
      return { param_ref: 'parameter' };
    case 'var_ref':
      return { var_ref: 'variable' };
    case 'data_ref':
      return { data_ref: 'data', key: 'value' };
    case 'secret_ref':
      return { secret_ref: 'secret' };
  }
}

export function ValueFields({
  label,
  value,
  onChange,
  modes = ALL_MODES,
  document,
  expectedType,
}: {
  label: string;
  value: Value;
  onChange: FieldChange<Value>;
  modes?: readonly ValueMode[];
  document?: Case;
  expectedType?: Parameter['type'] | undefined;
}) {
  const parameters = Object.entries(document?.parameters ?? {}).filter(
    ([, parameter]) =>
      parameter &&
      (!expectedType ||
        parameter.type === expectedType ||
        (expectedType === 'number' && parameter.type === 'integer')),
  );
  const declared =
    typeof value === 'object' &&
    'param_ref' in value &&
    document &&
    Object.hasOwn(document.parameters, value.param_ref)
      ? document.parameters[value.param_ref]
      : undefined;
  return (
    <fieldset
      className="case-editor-group"
      data-field-scope={label}
      data-value-mode={valueMode(value)}
    >
      <legend>{label}</legend>
      <ChoiceField
        label="值类型"
        value={valueMode(value)}
        choices={modes}
        onChange={(mode, save) => onChange(defaultValue(mode), save)}
      />
      {typeof value === 'string' ? (
        <TextField label="文本值" value={value} onChange={onChange} multiline />
      ) : null}
      {typeof value === 'number' ? (
        <NumberField
          label="数值"
          value={value}
          min={-Number.MAX_VALUE}
          integer={false}
          onChange={(number, save) => onChange(number ?? Number.NaN, save)}
        />
      ) : null}
      {typeof value === 'boolean' ? (
        <BooleanField label="布尔值" value={value} onChange={onChange} />
      ) : null}
      {typeof value === 'object' ? (
        'data_ref' in value ? (
          <div className="case-editor-grid">
            <TextField
              label="数据集引用"
              value={value.data_ref}
              onChange={(data_ref, save) => onChange({ ...value, data_ref }, save)}
            />
            <TextField
              label="数据键"
              value={value.key}
              onChange={(key, save) => onChange({ ...value, key }, save)}
            />
          </div>
        ) : 'param_ref' in value ? (
          <>
            <TextField
              label="参数引用"
              value={value.param_ref}
              onChange={(param_ref, save) => onChange({ param_ref }, save)}
            />
            {parameters.length ? (
              <Select
                size="sm"
                aria-label="已声明参数"
                value={parameters.some(([name]) => name === value.param_ref) ? value.param_ref : ''}
                placeholder="选择已声明参数"
                items={parameters.map(([name, parameter]) => ({
                  value: name,
                  label: `${name} (${parameter?.type})`,
                }))}
                onValueChange={(param_ref) => onChange({ param_ref }, true)}
              />
            ) : null}
            <p className="case-draft-note">
              {declared
                ? `声明类型：${declared.type}${declared.required ? '，必填' : '，有默认值'}`
                : '参数尚未声明，请在参数绑定中定义。'}
              {expectedType ? `；此处需要 ${expectedType}。` : ''}
            </p>
          </>
        ) : 'var_ref' in value ? (
          <TextField
            label="变量引用"
            value={value.var_ref}
            onChange={(var_ref, save) => onChange({ var_ref }, save)}
          />
        ) : (
          <TextField
            label="秘密引用"
            value={value.secret_ref}
            onChange={(secret_ref, save) => onChange({ secret_ref }, save)}
          />
        )
      ) : null}
      {expectedType && !(typeof value === 'object' && 'param_ref' in value) ? (
        <p className="case-draft-note">
          此处需要 {expectedType}；数据键和秘密提供者由工程预检核对。
        </p>
      ) : null}
    </fieldset>
  );
}

const SOURCES: readonly ObservationSource[] = ['native', 'webview', 'ocr', 'vision'];
const OPERATORS: readonly Predicate['operator'][] = [
  'exists',
  'not_exists',
  'visible',
  'enabled',
  'checked',
  'text_equals',
  'contains',
  'regex',
  'count_eq',
  'count_gt',
  'count_lt',
  'number_cmp',
  'page_is',
  'semantic',
];

export function PredicateFields({
  value,
  document,
  onChange,
}: {
  value: Predicate;
  document: Case;
  onChange: FieldChange<Predicate>;
}) {
  const expectedModes =
    value.operator === 'count_eq' ||
    value.operator === 'count_gt' ||
    value.operator === 'count_lt' ||
    value.operator === 'number_cmp'
      ? NUMBER_MODES
      : EXPECTED_TEXT_MODES;
  return (
    <div className="case-editor-fields">
      <div className="case-editor-grid">
        <ChoiceField
          label="检查运算符"
          value={value.operator}
          choices={OPERATORS}
          onChange={(operator, save) => onChange(changePredicate(value, operator), save)}
        />
        <ChoiceField
          label="观察来源"
          value={value.source}
          choices={SOURCES}
          onChange={(source, save) => onChange({ ...value, source }, save)}
        />
        <NumberField
          label="条件超时（ms）"
          value={value.timeout_ms}
          optional={value.operator !== 'semantic'}
          onChange={(timeout, save) => {
            const next = { ...value };
            if (timeout === undefined && next.operator !== 'semantic') delete next.timeout_ms;
            else next.timeout_ms = timeout ?? Number.NaN;
            onChange(next, save);
          }}
        />
      </div>
      <TargetFields
        value={value.target}
        document={document}
        onChange={(target, save) => onChange({ ...value, target }, save)}
      />
      {value.operator === 'not_exists' ? (
        <>
          <TargetFields
            label="消失检查作用域"
            value={value.scope}
            document={document}
            onChange={(scope, save) => onChange({ ...value, scope }, save)}
          />
          <div className="case-editor-grid">
            <NumberField
              label="观察窗口（ms）"
              value={value.observation_window_ms}
              onChange={(duration, save) =>
                onChange({ ...value, observation_window_ms: duration ?? Number.NaN }, save)
              }
            />
            <NumberField
              label="轮询间隔（ms）"
              value={value.poll_interval_ms}
              optional
              onChange={(interval, save) => {
                const next = { ...value };
                if (interval === undefined) delete next.poll_interval_ms;
                else next.poll_interval_ms = interval;
                onChange(next, save);
              }}
            />
          </div>
        </>
      ) : null}
      {'expected' in value ? (
        typeof value.expected === 'boolean' ? (
          <BooleanField
            label="期望为真"
            value={value.expected}
            onChange={(expected, save) => onChange({ ...value, expected } as Predicate, save)}
          />
        ) : (
          <ValueFields
            label="固定期望"
            value={value.expected}
            modes={expectedModes}
            document={document}
            expectedType={
              value.operator.startsWith('count_')
                ? 'integer'
                : value.operator === 'number_cmp'
                  ? 'number'
                  : 'string'
            }
            onChange={(expected, save) => onChange({ ...value, expected } as Predicate, save)}
          />
        )
      ) : null}
      {value.operator === 'number_cmp' ? (
        <div className="case-editor-grid">
          <ChoiceField
            label="数值比较"
            value={value.comparison}
            choices={['eq', 'ne', 'gt', 'gte', 'lt', 'lte']}
            onChange={(comparison, save) => onChange({ ...value, comparison }, save)}
          />
          <TextField
            label="单位"
            value={value.unit}
            onChange={(unit, save) => onChange({ ...value, unit }, save)}
          />
          <NumberField
            label="精度"
            min={0}
            value={value.precision}
            onChange={(precision, save) =>
              onChange({ ...value, precision: precision ?? Number.NaN }, save)
            }
          />
        </div>
      ) : null}
    </div>
  );
}

export function CheckpointFields({
  value,
  document,
  onChange,
}: {
  value: Checkpoint;
  document: Case;
  onChange: FieldChange<Checkpoint>;
}) {
  return (
    <div className="case-editor-fields">
      <TextField
        label="检查点 ID"
        value={value.id}
        onChange={(id, save) => onChange({ ...value, id }, save)}
      />
      <BooleanField
        label="必须通过"
        value={value.required}
        onChange={(required, save) => onChange({ ...value, required }, save)}
      />
      <PredicateFields
        value={value.condition}
        document={document}
        onChange={(condition, save) => onChange({ ...value, condition }, save)}
      />
    </div>
  );
}

export function CheckpointsFields({
  value,
  document,
  onChange,
  required = false,
}: {
  value: Checkpoint[];
  document: Case;
  onChange: FieldChange<Checkpoint[]>;
  required?: boolean;
}) {
  return (
    <div className="case-editor-fields">
      {value.map((checkpoint, index) => (
        <fieldset
          key={index}
          className="case-editor-group"
          data-checkpoint-index={index}
          data-checkpoint-id={checkpoint.id}
        >
          <legend>检查点 {index + 1}</legend>
          <CheckpointFields
            value={checkpoint}
            document={document}
            onChange={(next, save) =>
              onChange(
                value.map((item, position) => (position === index ? next : item)),
                save,
              )
            }
          />
          <Button
            size="sm"
            variant="ghost"
            disabled={required && value.length === 1}
            onClick={() =>
              onChange(
                value.filter((_, position) => position !== index),
                true,
              )
            }
          >
            <Trash2 aria-hidden="true" />
            删除检查点 {index + 1}
          </Button>
        </fieldset>
      ))}
      <Button
        size="sm"
        variant="ghost"
        onClick={() => onChange([...value, createCheckpoint(document, value)], true)}
      >
        <Plus aria-hidden="true" />
        添加检查点
      </Button>
    </div>
  );
}

export function BindingFields<T>({
  label,
  value,
  onChange,
  create,
  render,
  nameLabel = '绑定名称',
  prefix = 'binding',
  onIssue,
  nameSuggestions = [],
}: {
  label: string;
  value: Record<string, T | undefined>;
  onChange: FieldChange<Record<string, T | undefined>>;
  create: (name?: string) => T;
  render: (value: T, onChange: FieldChange<T>, name: string) => ReactNode;
  nameLabel?: string;
  prefix?: string;
  onIssue?: FieldIssueHandler | undefined;
  nameSuggestions?: readonly string[];
}) {
  const groupId = useId();
  const [names, setNames] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const report = (next: Record<string, string>) => {
    setErrors(next);
    onIssue?.(groupId, Object.values(next)[0] ?? null);
  };
  return (
    <fieldset className="case-editor-group" data-binding-label={label}>
      <legend>{label}</legend>
      {Object.entries(value).map(([key, item], index) => (
        <div key={index} className="case-editor-binding" data-binding-name={key}>
          <TextField
            label={nameLabel}
            value={Object.hasOwn(names, key) ? (names[key] ?? key) : key}
            onChange={(name, save) => {
              const nextErrors = Object.fromEntries(
                Object.entries(errors).filter(([entry]) => entry !== key),
              );
              if (name !== key && Object.hasOwn(value, name)) {
                setNames({ ...names, [key]: name });
                report({ ...nextErrors, [key]: '绑定名称重复，未覆盖已有绑定。' });
                return;
              }
              const nextNames = Object.fromEntries(
                Object.entries(names).filter(([entry]) => entry !== key),
              );
              setNames({ ...nextNames, [name]: name });
              report(nextErrors);
              onChange(
                Object.fromEntries(
                  Object.entries(value).map(([entry, content]) => [
                    entry === key ? name : entry,
                    content,
                  ]),
                ),
                save,
              );
            }}
          />
          {render(
            item ?? create(key),
            (next, save) => onChange({ ...value, [key]: next }, save),
            key,
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              const nextNames = Object.fromEntries(
                Object.entries(names).filter(([entry]) => entry !== key),
              );
              setNames(nextNames);
              const nextErrors = Object.fromEntries(
                Object.entries(errors).filter(([entry]) => entry !== key),
              );
              report(nextErrors);
              const next = Object.fromEntries(
                Object.entries(value).filter(([entry]) => entry !== key),
              );
              if (Object.hasOwn(errors, key) && Object.keys(nextErrors).length === 0)
                onChange(next, true, groupId);
              else onChange(next, true);
            }}
          >
            <Trash2 aria-hidden="true" />
            删除绑定 {key}
          </Button>
        </div>
      ))}
      {Object.values(errors).length ? (
        <p className="case-draft-error" role="alert">
          {Object.values(errors)[0]}
        </p>
      ) : null}
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          let index = 1;
          while (Object.hasOwn(value, `${prefix}_${index}`)) index++;
          const name =
            nameSuggestions.find((name) => !Object.hasOwn(value, name)) ?? `${prefix}_${index}`;
          onChange({ ...value, [name]: create(name) }, true);
        }}
      >
        <Plus aria-hidden="true" />
        添加{label}
      </Button>
    </fieldset>
  );
}

function ActionFields({
  value,
  document,
  onChange,
}: {
  value: Action;
  document: Case;
  onChange: FieldChange<Action>;
}) {
  return (
    <div className="case-editor-fields">
      <ChoiceField
        label="动作类型"
        value={value.kind}
        choices={['tap', 'long', 'input', 'swipe', 'key', 'launch', 'stop', 'clear']}
        onChange={(kind, save) => onChange(changeAction(value, kind), save)}
      />
      {'target' in value ? (
        <TargetFields
          value={value.target}
          document={document}
          onChange={(target, save) => onChange({ ...value, target }, save)}
        />
      ) : null}
      {'duration_ms' in value ? (
        <NumberField
          label="动作时长（ms）"
          value={value.duration_ms}
          onChange={(duration, save) =>
            onChange({ ...value, duration_ms: duration ?? Number.NaN }, save)
          }
        />
      ) : null}
      {value.kind === 'input' ? (
        <>
          <ChoiceField
            label="输入模式"
            value={value.mode}
            choices={['replace', 'append']}
            onChange={(mode, save) => onChange({ ...value, mode }, save)}
          />
          <ValueFields
            label="输入内容"
            value={value.text}
            modes={TEXT_MODES}
            document={document}
            expectedType="string"
            onChange={(text, save) => onChange({ ...value, text } as Action, save)}
          />
        </>
      ) : null}
      {value.kind === 'swipe' ? (
        <>
          <PointFields
            label="滑动起点"
            value={value.start}
            onChange={(start, save) => onChange({ ...value, start }, save)}
          />
          <PointFields
            label="滑动终点"
            value={value.end}
            onChange={(end, save) => onChange({ ...value, end }, save)}
          />
        </>
      ) : null}
      {value.kind === 'key' ? (
        <TextField
          label="按键"
          value={value.key}
          onChange={(key, save) => onChange({ ...value, key }, save)}
        />
      ) : null}
      {'app_id' in value ? (
        <TextField
          label="应用 ID"
          value={value.app_id}
          onChange={(app_id, save) => onChange({ ...value, app_id }, save)}
        />
      ) : null}
    </div>
  );
}

export function StepFields({
  value,
  document,
  onChange,
  onIssue,
}: {
  value: Step;
  document: Case;
  onChange: FieldChange<Step>;
  onIssue?: FieldIssueHandler | undefined;
}) {
  return (
    <div className="case-editor-fields">
      <div className="case-editor-grid">
        <TextField
          label="步骤 ID"
          value={value.id}
          onChange={(id, save) => onChange({ ...value, id }, save)}
        />
        <NumberField
          label="步骤超时（ms）"
          value={value.timeout_ms}
          optional={value.type !== 'ai_step'}
          onChange={(timeout, save) => {
            const next = { ...value };
            if (timeout === undefined && next.type !== 'ai_step') delete next.timeout_ms;
            else next.timeout_ms = timeout ?? Number.NaN;
            onChange(next, save);
          }}
        />
      </div>
      {value.type === 'action' ? (
        <ActionFields
          value={value.action}
          document={document}
          onChange={(action, save) => onChange({ ...value, action }, save)}
        />
      ) : null}
      {value.type === 'wait' || value.type === 'if' ? (
        <PredicateFields
          value={value.condition}
          document={document}
          onChange={(condition, save) => onChange({ ...value, condition }, save)}
        />
      ) : null}
      {value.type === 'check' ? (
        <CheckpointFields
          value={value.checkpoint}
          document={document}
          onChange={(checkpoint, save) => onChange({ ...value, checkpoint }, save)}
        />
      ) : null}
      {value.type === 'extract' ? (
        <>
          <TargetFields
            value={value.target}
            document={document}
            onChange={(target, save) => onChange({ ...value, target }, save)}
          />
          <div className="case-editor-grid">
            <ChoiceField
              label="提取来源"
              value={value.source}
              choices={SOURCES}
              onChange={(source, save) => onChange({ ...value, source }, save)}
            />
            <ChoiceField
              label="提取属性"
              value={value.attribute}
              choices={['text', 'count', 'enabled', 'checked']}
              onChange={(attribute, save) => onChange({ ...value, attribute }, save)}
            />
            <TextField
              label="保存为变量"
              value={value.as}
              onChange={(name, save) => onChange({ ...value, as: name }, save)}
            />
          </div>
        </>
      ) : null}
      {value.type === 'flow' ? (
        <>
          <TextField
            label="公共流程引用"
            value={value.flow_ref}
            onChange={(flow_ref, save) => onChange({ ...value, flow_ref }, save)}
          />
          <BindingFields<Value>
            label="流程参数"
            value={value.arguments ?? {}}
            create={() => ''}
            onIssue={onIssue}
            onChange={(args, save, clearedIssue) =>
              clearedIssue
                ? onChange({ ...value, arguments: args }, save, clearedIssue)
                : onChange({ ...value, arguments: args }, save)
            }
            render={(item, change) => (
              <ValueFields label="参数值" value={item} document={document} onChange={change} />
            )}
          />
          <BindingFields<string>
            label="流程返回绑定"
            value={value.bind ?? {}}
            create={() => 'variable'}
            onIssue={onIssue}
            onChange={(bind, save, clearedIssue) =>
              clearedIssue
                ? onChange({ ...value, bind }, save, clearedIssue)
                : onChange({ ...value, bind }, save)
            }
            render={(item, change) => (
              <TextField label="调用端变量" value={item} onChange={change} />
            )}
          />
        </>
      ) : null}
      {value.type === 'loop' ? (
        <>
          <NumberField
            label="最多迭代次数"
            value={value.max_iterations}
            onChange={(max, save) =>
              onChange({ ...value, max_iterations: max ?? Number.NaN }, save)
            }
          />
          <BooleanField
            label="使用循环条件"
            value={value.condition != null}
            onChange={(enabled, save) => {
              const next = { ...value };
              if (enabled)
                next.condition = {
                  operator: 'exists',
                  source: 'native',
                  target: defaultTarget(document),
                };
              else delete next.condition;
              onChange(next, save);
            }}
          />
          {value.condition ? (
            <PredicateFields
              value={value.condition}
              document={document}
              onChange={(condition, save) => onChange({ ...value, condition }, save)}
            />
          ) : null}
        </>
      ) : null}
      {value.type === 'ai_step' ? (
        <>
          <TextField
            label="AI 步骤目标"
            value={value.goal}
            multiline
            onChange={(goal, save) => onChange({ ...value, goal }, save)}
          />
          <CheckpointsFields
            value={value.checks}
            document={document}
            required
            onChange={(checks, save) =>
              onChange({ ...value, checks: checks as typeof value.checks }, save)
            }
          />
          <p className="case-draft-note">只编辑目标与固定检查点，不请求模型或执行步骤。</p>
        </>
      ) : null}
    </div>
  );
}
