import type { Step, Target } from '@pulse/schema';
import { Badge, Button } from '../../shared/ui';
import { type CaseDraftStore, useCaseDraft } from './caseDraftStore';
import './caseDraft.css';

function targetLabel(target: Target): string {
  if ('locator_ref' in target) return target.locator_ref;
  const candidate = target.locator.candidates[0];
  return candidate.by === 'point'
    ? `point (${candidate.value.x}, ${candidate.value.y})`
    : `${candidate.by}: ${candidate.value}`;
}

function stepLabel(step: Step): string {
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

function DraftStep({ step, added }: { step: Step; added: boolean }) {
  return (
    <li className="case-draft-step" data-added={added}>
      <div className="case-draft-step-row">
        <code>{step.id}</code>
        <Badge tone={added ? 'info' : 'neutral'}>{added ? '新增步骤' : '原始步骤'}</Badge>
        <span className="case-draft-step-description">{stepLabel(step)}</span>
      </div>
      {step.type === 'if' ? (
        <>
          <ol aria-label={`${step.id} then`} className="case-draft-branch">
            {step.then.map((child) => (
              <DraftStep key={child.id} step={child} added={added} />
            ))}
          </ol>
          {step.else?.length ? (
            <ol aria-label={`${step.id} else`} className="case-draft-branch">
              {step.else.map((child) => (
                <DraftStep key={child.id} step={child} added={added} />
              ))}
            </ol>
          ) : null}
        </>
      ) : null}
      {step.type === 'loop' ? (
        <ol aria-label={`${step.id} loop`} className="case-draft-branch">
          {step.steps.map((child) => (
            <DraftStep key={child.id} step={child} added={added} />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

export function CaseDraftPanel({ store: injectedStore }: { store?: CaseDraftStore } = {}) {
  const { state, store } = useCaseDraft(injectedStore);
  const document = state.draft ?? state.original;
  const originalCount = state.original?.steps.length ?? 0;
  return (
    <section
      className="case-draft-panel"
      aria-label="用例草稿"
      aria-busy={state.status === 'loading'}
    >
      <header className="case-draft-header">
        <div>
          <h3>{document?.name ?? '用例草稿'}</h3>
          {state.scope ? <code className="case-draft-id">{state.scope.caseId}</code> : null}
        </div>
        <Badge
          tone={
            state.persistence === 'saved'
              ? 'success'
              : state.persistence === 'error'
                ? 'danger'
                : 'neutral'
          }
        >
          {state.persistence === 'saved'
            ? '草稿已保存'
            : state.persistence === 'error'
              ? '草稿未确认保存'
              : '原始用例'}
        </Badge>
      </header>
      <p className="case-draft-note" role="status">
        {state.persistence === 'saved'
          ? '已自动保存本地草稿。工程原始用例未修改，步骤不会执行。'
          : '插入后自动保存本地草稿，不修改工程原始用例，也不执行步骤。'}
      </p>
      {state.error ? (
        <p className="case-draft-error" role="alert">
          {state.error}
        </p>
      ) : null}
      {!state.canInsert && state.reason !== state.error ? (
        <p className="case-draft-note">{state.reason}</p>
      ) : null}
      {state.scope && (state.status === 'error' || state.persistence === 'error') ? (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void store.load();
          }}
        >
          重新读取用例草稿
        </Button>
      ) : null}
      {document ? (
        <>
          <p className="case-draft-goal">{document.goal}</p>
          <div className="case-draft-counts">
            <span>
              原始 {originalCount} 步 / 新增{' '}
              {state.draft ? state.draft.steps.length - originalCount : 0} 步
            </span>
            <span>插入位置: 步骤末尾</span>
          </div>
          <ol className="case-draft-steps" aria-label="草稿步骤">
            {document.steps.map((step, index) => (
              <DraftStep key={step.id} step={step} added={index >= originalCount} />
            ))}
          </ol>
          <p className="case-draft-note">
            预检查 {document.preconditions.length} 项 / 验收检查 {document.checks.length}{' '}
            项，原始字段完整保留。
          </p>
        </>
      ) : null}
    </section>
  );
}
