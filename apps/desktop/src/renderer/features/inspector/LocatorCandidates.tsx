import { ArrowDown, ArrowUp } from 'lucide-react';
import { Badge, Button, Input } from '../../shared/ui';
import { inspectorLocator, PACKAGE_NAME } from './inspectionModel';
import type { ElementInspectorStore, InspectorState, LocatorValidation } from './inspectorStore';

const STATUS_LABELS: Record<LocatorValidation['status'], string> = {
  checking: '正在验证',
  resolved: '唯一命中',
  ambiguous: '歧义',
  not_found: '未找到',
  indeterminate: '无法确认',
};

export function LocatorCandidates({
  state,
  store,
}: {
  state: InspectorState;
  store: ElementInspectorStore;
}) {
  const locator = inspectorLocator(state.candidates, state.visible);
  const editable = state.status === 'ready' && !state.expired && !state.blockedReason;
  const validPackage = PACKAGE_NAME.test(state.expectedPackage);
  const validation = state.validation;
  return (
    <section className="locator-candidates" aria-label="候选定位器">
      <div className="inspector-section-heading">
        <h3>候选定位器</h3>
        <Badge
          tone={
            validation?.status === 'resolved'
              ? 'success'
              : validation?.status === 'ambiguous'
                ? 'warning'
                : 'neutral'
          }
        >
          {validation ? STATUS_LABELS[validation.status] : '未验证'}
        </Badge>
      </div>
      <p className="inspector-note">
        按声明顺序定位；歧义不会静默取首个或跳到下一个候选。快照计数不代表当前设备唯一性。
      </p>
      <ol className="locator-candidate-list">
        {state.candidates.map((candidate, index) => {
          const attempt = validation?.attempts.find((item) => item.key === candidate.key);
          return (
            <li key={candidate.key} className="locator-candidate" data-enabled={candidate.enabled}>
              <div className="locator-candidate-row">
                <label className="inspector-checkbox">
                  <input
                    type="checkbox"
                    checked={candidate.enabled}
                    disabled={!editable}
                    aria-label={`使用 ${candidate.locator.by} 候选`}
                    onChange={() => store.toggleCandidate(candidate.key)}
                  />
                  <code>{candidate.locator.by}</code>
                </label>
                {candidate.fragile ? <Badge tone="warning">结构路径</Badge> : null}
                <span className="locator-candidate-sort">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`上移 ${candidate.locator.by} 候选`}
                    disabled={!editable || index === 0}
                    onClick={() => store.moveCandidate(index, -1)}
                  >
                    <ArrowUp aria-hidden="true" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`下移 ${candidate.locator.by} 候选`}
                    disabled={!editable || index === state.candidates.length - 1}
                    onClick={() => store.moveCandidate(index, 1)}
                  >
                    <ArrowDown aria-hidden="true" />
                  </Button>
                </span>
              </div>
              <code className="locator-candidate-value">{candidate.locator.value}</code>
              <span className="inspector-note">
                快照相同值 {candidate.snapshotMatches} 个 /{' '}
                {candidate.enabled
                  ? attempt
                    ? `${attempt.status === 'resolved' ? '命中' : STATUS_LABELS[attempt.status]} ${attempt.matchCount} 个`
                    : '按序尚未验证'
                  : '未启用'}
              </span>
              {attempt?.errorCode ? (
                <code className="inspector-note">{attempt.errorCode}</code>
              ) : null}
            </li>
          );
        })}
      </ol>
      {!locator ? <p className="inspector-note">至少启用一个非坐标候选后才能生成定位器。</p> : null}
      <label className="inspector-package">
        目标应用包名
        <Input
          size="sm"
          value={state.expectedPackage}
          disabled={!editable}
          invalid={!!state.expectedPackage && !validPackage}
          aria-label="验证目标应用包名"
          placeholder="com.example.app"
          onChange={(event) => store.setExpectedPackage(event.target.value)}
          onBlur={() => {
            if (PACKAGE_NAME.test(store.getSnapshot().expectedPackage)) void store.validate();
          }}
        />
      </label>
      {!validPackage ? (
        <p className="inspector-note">
          所属应用未确认，请明确填写目标应用包名后验证。不会从 OCR 文本推断应用。
        </p>
      ) : null}
      <div className="locator-expectations">
        <span>要求唯一命中: true</span>
        <label className="inspector-checkbox">
          <input
            type="checkbox"
            checked={state.visible}
            disabled={!editable}
            onChange={(event) => store.setVisible(event.target.checked)}
          />
          要求可见
        </label>
        <Button
          size="sm"
          disabled={!editable || !locator || !validPackage}
          loading={validation?.status === 'checking'}
          onClick={() => void store.validate()}
        >
          验证定位器
        </Button>
      </div>
      {validation && validation.status !== 'checking' ? (
        <p className="inspector-note" role="status">
          当前设备验证: {STATUS_LABELS[validation.status]}
          {validation.matchCount !== null ? ` / ${validation.matchCount} 个` : ''}
          {validation.errorCode ? ` / ${validation.errorCode}` : ''}。
          {validation.observationId ? `验证观察 ${validation.observationId}。` : ''}
          定位成功不代表动作完成或业务通过。
        </p>
      ) : null}
      {locator ? (
        <details className="inspector-json">
          <summary>定位器 JSON</summary>
          <pre>{JSON.stringify(locator, null, 2)}</pre>
        </details>
      ) : null}
    </section>
  );
}
