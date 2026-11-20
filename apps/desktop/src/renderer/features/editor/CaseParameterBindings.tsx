import type { Case, Parameter, Scalar, Value } from '@pulse/schema';
import { useState } from 'react';
import { Badge, Button } from '../../shared/ui';
import { DraftForm } from './DraftForm';
import { draftPath, isRecord } from './caseDraftValidation';
import type { CaseDraftStore } from './caseDraftStore';
import {
  BindingFields,
  BooleanField,
  ChoiceField,
  NumberField,
  TextField,
  ValueFields,
  type FieldChange,
  type ValueMode,
} from './StepFields';

type Bindings = Pick<Case, 'parameters' | 'inputs'>;
const INPUT_MODES: readonly ValueMode[] = ['text', 'number', 'boolean', 'data_ref', 'secret_ref'];

function matchesType(value: unknown, type: Parameter['type']): value is Scalar {
  if (type === 'integer') return typeof value === 'number' && Number.isSafeInteger(value);
  if (type === 'number') return typeof value === 'number' && Number.isFinite(value);
  return typeof value === type;
}

function defaultScalar(type: Parameter['type']): Scalar {
  return type === 'string' ? '' : type === 'boolean' ? false : 0;
}

function bindingIssue(bindings: Bindings): string | null {
  for (const [name, parameter] of Object.entries(bindings.parameters)) {
    if (!parameter) continue;
    const path = draftPath('Case/parameters', name);
    if (parameter.required && Object.hasOwn(parameter, 'default'))
      return `${path}/default: 必填参数不能带默认值。`;
    if (!parameter.required && !matchesType(parameter.default, parameter.type))
      return `${path}/default: 可选参数必须提供与声明类型一致的标量默认值。`;
  }
  for (const [name, input] of Object.entries(bindings.inputs)) {
    const path = draftPath('Case/inputs', name);
    if (isRecord(input) && ('param_ref' in input || 'var_ref' in input))
      return `${path}: 初始输入只允许标量、数据集或秘密引用。`;
    const parameter = Object.hasOwn(bindings.parameters, name)
      ? bindings.parameters[name]
      : undefined;
    if (!parameter) continue;
    if (isRecord(input)) {
      if ('secret_ref' in input && parameter.type !== 'string')
        return `${path}: 秘密引用只能绑定 string 参数。`;
    } else if (!matchesType(input, parameter.type))
      return `${path}: 初始输入与声明的参数类型不匹配。`;
  }
  return null;
}

function ParameterFields({
  value,
  onChange,
}: {
  value: Parameter;
  onChange: FieldChange<Parameter>;
}) {
  return (
    <div className="case-editor-fields">
      <ChoiceField
        label="参数类型"
        value={value.type}
        choices={['string', 'integer', 'number', 'boolean']}
        onChange={(type, save) => {
          const next = { ...value, type };
          if (next.required) delete next.default;
          else if (!matchesType(next.default, type)) next.default = defaultScalar(type);
          onChange(next, save);
        }}
      />
      <BooleanField
        label="必填参数"
        value={value.required}
        onChange={(required, save) => {
          const next = { ...value, required };
          if (required) delete next.default;
          else if (!matchesType(next.default, next.type)) next.default = defaultScalar(next.type);
          onChange(next, save);
        }}
      />
      {!value.required ? (
        value.type === 'string' ? (
          <TextField
            label="默认值"
            value={typeof value.default === 'string' ? value.default : ''}
            multiline
            onChange={(defaultValue, save) => onChange({ ...value, default: defaultValue }, save)}
          />
        ) : value.type === 'boolean' ? (
          <BooleanField
            label="默认值"
            value={value.default === true}
            onChange={(defaultValue, save) => onChange({ ...value, default: defaultValue }, save)}
          />
        ) : (
          <NumberField
            label="默认值"
            value={typeof value.default === 'number' ? value.default : undefined}
            min={-Number.MAX_VALUE}
            integer={value.type === 'integer'}
            onChange={(defaultValue, save) =>
              onChange({ ...value, default: defaultValue ?? Number.NaN }, save)
            }
          />
        )
      ) : (
        <p className="case-draft-note">必填参数不写默认值；初始输入或运行输入可以提供值。</p>
      )}
    </div>
  );
}

export function CaseParameterBindings({
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
    <section className="case-draft-checkpoints" aria-label="参数绑定">
      <header className="case-draft-header">
        <h3>参数声明与初始输入</h3>
        <Badge>
          {Object.keys(document.parameters).length} 个参数 / {Object.keys(document.inputs).length}{' '}
          个输入
        </Badge>
        <Button size="sm" variant="ghost" disabled={!canEdit} onClick={() => setEditing(!editing)}>
          {editing ? '收起参数绑定' : '编辑参数绑定'}
        </Button>
      </header>
      {editing ? (
        <DraftForm<Bindings>
          label="编辑参数绑定"
          document={document}
          store={store}
          disabled={!canEdit}
          persist={store.editCaseBindings}
          select={(draft) => ({ parameters: draft.parameters, inputs: draft.inputs })}
          replace={(draft, bindings) => ({ ...draft, ...bindings })}
          beforeSave={(_previous, next) => bindingIssue(next)}
          onClose={() => setEditing(false)}
        >
          {(bindings, change, reportIssue) => {
            const local = { ...document, ...bindings };
            return (
              <>
                <BindingFields<Parameter>
                  label="参数声明"
                  nameLabel="参数名称"
                  prefix="parameter"
                  value={bindings.parameters}
                  create={() => ({ type: 'string', required: true })}
                  onIssue={reportIssue}
                  onChange={(parameters, save, clearedIssue) =>
                    change({ ...bindings, parameters }, save, clearedIssue)
                  }
                  render={(parameter, update) => (
                    <ParameterFields value={parameter} onChange={update} />
                  )}
                />
                <BindingFields<Value>
                  label="初始输入"
                  nameLabel="输入参数名称"
                  prefix="input"
                  value={bindings.inputs}
                  nameSuggestions={Object.keys(bindings.parameters)}
                  create={(name) => {
                    const parameter =
                      name && Object.hasOwn(bindings.parameters, name)
                        ? bindings.parameters[name]
                        : undefined;
                    return parameter ? defaultScalar(parameter.type) : '';
                  }}
                  onIssue={reportIssue}
                  onChange={(inputs, save, clearedIssue) =>
                    change({ ...bindings, inputs }, save, clearedIssue)
                  }
                  render={(input, update, name) => (
                    <ValueFields
                      label="初始输入值"
                      value={input}
                      document={local}
                      modes={INPUT_MODES}
                      expectedType={
                        Object.hasOwn(bindings.parameters, name)
                          ? bindings.parameters[name]?.type
                          : undefined
                      }
                      onChange={update}
                    />
                  )}
                />
                <p className="case-draft-note">
                  初始输入只允许标量、数据集或秘密引用，不引用尚未运行的变量或参数自身。未声明的输入会在问题面板提示；数据键、秘密提供者和运行覆盖值由预检确认，不会在编辑器请求凭证。
                </p>
              </>
            );
          }}
        </DraftForm>
      ) : (
        <ul className="case-draft-checkpoint-list">
          {Object.entries(document.parameters).map(([name, parameter]) =>
            parameter ? (
              <li key={name}>
                <code>{name}</code>
                <Badge>{parameter.type}</Badge>
                <span>
                  {parameter.required ? '必填' : '有默认值'} /{' '}
                  {Object.hasOwn(document.inputs, name) ? '已绑定初始输入' : '等待运行输入'}
                </span>
              </li>
            ) : null,
          )}
          {!Object.keys(document.parameters).length ? (
            <li className="case-draft-note">
              尚未声明参数。参数类型限定为 string / integer / number / boolean。
            </li>
          ) : null}
        </ul>
      )}
    </section>
  );
}
