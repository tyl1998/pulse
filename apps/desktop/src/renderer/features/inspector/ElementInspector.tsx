import { Fragment, useState, useSyncExternalStore } from 'react';
import type { CheckStep, ActionStep } from '@pulse/schema';
import { RefreshCw, ScanSearch } from 'lucide-react';
import { Badge, Button, EmptyState } from '../../shared/ui';
import { useCaseDraft, type CaseDraftStore } from '../editor/caseDraftStore';
import { ElementTree } from './ElementTree';
import { LocatorCandidates } from './LocatorCandidates';
import type { InspectionSource, InspectedElement } from './inspectionModel';
import type { ElementInspectorStore } from './inspectorStore';
import './inspector.css';

const SOURCE_LABELS = {
  available: '可用',
  unavailable: '不可用',
  failed: '采集失败',
  unknown: '未知',
};
const bool = (value: boolean | null | undefined) =>
  value === true ? '是' : value === false ? '否' : '未知';

function SourceDetails({ label, source }: { label: string; source: InspectionSource }) {
  return (
    <div className="inspector-source-details">
      <strong>
        {label} / {source.source}
      </strong>
      <dl className="inspector-properties">
        <dt>来源状态</dt>
        <dd>
          {SOURCE_LABELS[source.status]}
          {source.errorCode ? ` / ${source.errorCode}` : ''}
        </dd>
        <dt>采集范围</dt>
        <dd>
          {source.requested ? '已请求' : '未请求'} / {source.completeness}
        </dd>
        <dt>坐标空间</dt>
        <dd>
          <code>{source.coordinateSpace}</code>
          {source.width && source.height ? ` / ${source.width}×${source.height}` : ''}
        </dd>
        <dt>采集时间</dt>
        <dd>
          {source.startedAt ?? '未知'} 至 {source.finishedAt ?? '未知'} ms（Android
          uptime，时钟未校准）
        </dd>
        <dt>完整性说明</dt>
        <dd>{source.reasons.length ? source.reasons.join(' / ') : '未知'}</dd>
      </dl>
    </div>
  );
}

function ElementProperties({ element }: { element: InspectedElement }) {
  const bounds = element.bounds;
  return (
    <section aria-label="元素属性" className="inspector-element-properties">
      <div className="inspector-section-heading">
        <h3>{element.label}</h3>
        <Badge tone={element.source === 'ocr' ? 'warning' : 'info'}>
          {element.source === 'ocr' ? 'OCR' : '原生树'}
        </Badge>
      </div>
      <dl className="inspector-properties">
        <dt>来源</dt>
        <dd>{element.source === 'native' ? 'accessibility / native' : 'OCR 文本块 / ocr'}</dd>
        <dt>路径</dt>
        <dd>
          <code>{element.path ? `[${element.path.join(', ')}]` : element.id}</code>
        </dd>
        <dt>坐标空间</dt>
        <dd>
          <code>{element.coordinateSpace}</code>
        </dd>
        <dt>边界</dt>
        <dd>
          {bounds
            ? `[${bounds.left}, ${bounds.top}] 至 [${bounds.right}, ${bounds.bottom}]`
            : '未知'}
        </dd>
        <dt>所属应用</dt>
        <dd>{element.packageName ?? '未知'}</dd>
        <dt>类名</dt>
        <dd>{element.className ?? '未知'}</dd>
        <dt>资源 ID</dt>
        <dd>{element.resId ?? '未知'}</dd>
        <dt>文本</dt>
        <dd>
          {element.source === 'native' && element.textRedacted !== false
            ? element.textRedacted
              ? '已脱敏，不展示'
              : '脱敏状态未知，不展示'
            : element.text === ''
              ? '空字符串'
              : (element.text ?? '未知')}
        </dd>
        <dt>描述</dt>
        <dd>{element.description ?? '未知'}</dd>
        {Object.entries({
          visible: '可见',
          enabled: '启用',
          checked: '选中（checked）',
          clickable: '可点击',
          editable: '可编辑',
          focused: '焦点',
        }).map(([key, label]) => (
          <Fragment key={key}>
            <dt>{label}</dt>
            <dd>{bool(element.states[key])}</dd>
          </Fragment>
        ))}
      </dl>
      <p className="inspector-note">
        {element.source === 'ocr'
          ? 'OCR 不证明原生 visible / enabled / checked。截图到显示坐标映射未确认，不在画面上高亮，也不能生成原生点击。'
          : '这些状态仅描述此快照；存在节点不等于可见、可操作或业务正确。'}
      </p>
    </section>
  );
}

export function ElementInspector({
  store,
  draftStore,
}: {
  store: ElementInspectorStore;
  draftStore?: CaseDraftStore;
}) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const { state: draft, store: drafts } = useCaseDraft(draftStore);
  const snapshot = state.snapshot;
  const selected = state.selectedId ? snapshot?.byId.get(state.selectedId) : null;
  const [notice, setNotice] = useState<{
    scope: string;
    observationId: string;
    elementId: string;
    message: string;
  } | null>(null);
  const scopeKey = JSON.stringify(draft.scope);
  const canInsert =
    !!selected &&
    !state.expired &&
    !state.blockedReason &&
    state.status === 'ready' &&
    draft.canInsert &&
    state.candidates.some((candidate) => candidate.enabled);
  const insert = (kind: 'tap' | 'check') => {
    setNotice(null);
    const current = store.getSnapshot();
    const element = current.selectedId ? current.snapshot?.byId.get(current.selectedId) : null;
    const locator = store.currentLocator();
    if (!element || !locator || (kind === 'tap' && element.source !== 'native')) return;
    const id = `inspect-${kind}-${crypto.randomUUID()}`;
    const step: ActionStep | CheckStep =
      kind === 'tap'
        ? { id, type: 'action', action: { kind: 'tap', target: { locator } } }
        : {
            id,
            type: 'check',
            checkpoint: {
              id: `inspect-exists-${crypto.randomUUID()}`,
              required: true,
              condition: { operator: 'exists', source: element.source, target: { locator } },
            },
          };
    if (!drafts.insertStep(step)) return;
    setNotice({
      scope: JSON.stringify(drafts.getSnapshot().scope),
      observationId: current.snapshot?.requestId ?? '',
      elementId: element.id,
      message: `已将${kind === 'tap' ? '点击步骤' : 'exists 检查点'}插入 ${drafts.getSnapshot().draft?.name ?? '当前用例'} 的步骤末尾，草稿已保存；未执行步骤。`,
    });
  };

  return (
    <section
      className="element-inspector"
      aria-label="元素检查器"
      aria-busy={state.status === 'loading'}
    >
      <div className="inspector-toolbar">
        <span className="inspector-toolbar-title">
          <ScanSearch aria-hidden="true" />
          元素检查器
        </span>
        <Badge tone={state.expired && snapshot ? 'warning' : 'neutral'}>
          {state.blockedReason
            ? '离线 / 未确认'
            : state.status === 'loading'
              ? '正在采集'
              : snapshot
                ? state.expired
                  ? '快照已过期'
                  : '快照'
                : '未采集'}
        </Badge>
        <Button
          size="sm"
          variant="ghost"
          aria-label="刷新元素"
          disabled={!state.device || !!state.blockedReason}
          loading={state.status === 'loading'}
          onClick={() => void store.refresh()}
        >
          <RefreshCw aria-hidden="true" />
          刷新元素
        </Button>
      </div>
      <label className="inspector-checkbox">
        <input
          type="checkbox"
          checked={state.includeOcr}
          onChange={(event) => store.setIncludeOcr(event.target.checked)}
        />
        同时采集 OCR（不返回截图字节）
      </label>
      {state.blockedReason ? (
        <p className="inspector-note" role="status">
          {state.blockedReason}
        </p>
      ) : null}
      {state.expiredReason && snapshot ? (
        <p className="inspector-warning" role="status">
          {state.expiredReason}
        </p>
      ) : null}
      {state.error ? (
        <p className="inspector-error" role="alert">
          {state.error}
        </p>
      ) : null}
      {snapshot ? (
        <>
          <div className="inspector-source-status">
            <Badge tone={snapshot.sources.tree.status === 'available' ? 'info' : 'warning'}>
              原生树 {SOURCE_LABELS[snapshot.sources.tree.status]}
            </Badge>
            <Badge tone={snapshot.sources.ocr.status === 'available' ? 'info' : 'neutral'}>
              OCR{' '}
              {snapshot.sources.ocr.requested
                ? SOURCE_LABELS[snapshot.sources.ocr.status]
                : '未请求'}
            </Badge>
            <span>非原子快照</span>
          </div>
          <details className="inspector-provenance">
            <summary>来源与采集信息</summary>
            <p className="inspector-note">
              观察 {snapshot.requestId} / revision {snapshot.revisionStart ?? '未知'} 至{' '}
              {snapshot.revisionEnd ?? '未知'}。相同 revision 不证明页面未变化；显示几何为独立采样。
            </p>
            <SourceDetails label="原生树" source={snapshot.sources.tree} />
            <SourceDetails label="OCR" source={snapshot.sources.ocr} />
          </details>
          {snapshot.elements.length ? (
            <ElementTree
              key={snapshot.requestId}
              snapshot={snapshot}
              selectedId={state.selectedId}
              store={store}
            />
          ) : (
            <EmptyState
              headingLevel={3}
              title={
                snapshot.sources.tree.status === 'available'
                  ? '当前快照未暴露节点'
                  : '无可用元素来源'
              }
              description="请检查来源状态或明确请求 OCR；采集不可用不等于元素不存在。"
            />
          )}
          {selected ? (
            <>
              <ElementProperties element={selected} />
              <LocatorCandidates state={state} store={store} />
              <div className="inspector-insert" role="group" aria-label="插入元素步骤">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!canInsert || selected.source !== 'native'}
                  onClick={() => insert('tap')}
                >
                  插入步骤
                </Button>
                <Button size="sm" disabled={!canInsert} onClick={() => insert('check')}>
                  设为检查点
                </Button>
                <p className="inspector-note">
                  插入点击步骤，或声明该元素应存在的 exists 检查点；不将当前文本或状态复制为预期。
                </p>
                {!draft.canInsert ? <p className="inspector-note">{draft.reason}</p> : null}
                {draft.error ? (
                  <p className="inspector-error" role="alert">
                    {draft.error}
                  </p>
                ) : null}
                {state.validation?.status !== 'resolved' ? (
                  <p className="inspector-note">
                    当前定位尚未确认唯一；草稿保留 unique: true，执行时须重新定位，歧义不会被接受。
                  </p>
                ) : null}
                {draft.persistence === 'saved' &&
                draft.status === 'ready' &&
                notice?.scope === scopeKey &&
                notice.observationId === snapshot.requestId &&
                notice.elementId === selected.id ? (
                  <p className="inspector-success" role="status">
                    {notice.message}
                  </p>
                ) : null}
              </div>
            </>
          ) : (
            <p className="inspector-note">
              在元素树或画面检查模式中选择元素，查看属性并生成定位器。
            </p>
          )}
        </>
      ) : (
        <EmptyState
          headingLevel={3}
          title={state.device ? '尚未采集元素' : '请选择画面设备'}
          description="接入已 ready 的 Agent 后刷新元素，或在画面上切换到检查模式。检查与草稿创作不取得控制权，也不发送设备动作。"
        />
      )}
    </section>
  );
}
