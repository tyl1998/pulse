import type { Case, Flow, Step } from '@pulse/schema';
import { useEffect, useRef, useState } from 'react';
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  ArrowDown,
  ArrowUp,
  Braces,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  Clock3,
  GitBranch,
  GripVertical,
  ListFilter,
  MousePointer2,
  Pencil,
  Plus,
  Repeat2,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { Badge, Button, Dialog, IconButton, Select } from '../../shared/ui';
import type { CaseDraftStore } from './caseDraftStore';
import { sameJson } from './caseDraftValidation';
import { DraftForm } from './DraftForm';
import { StepFields } from './StepFields';
import {
  STEP_TYPES,
  createStep,
  editStepList,
  findStep,
  replaceStep,
  stepLabel,
  type StepListLocation,
} from './stepEditing';

const TYPE_ICONS = {
  action: MousePointer2,
  wait: Clock3,
  check: CheckCheck,
  extract: ListFilter,
  flow: Braces,
  if: GitBranch,
  loop: Repeat2,
  ai_step: Sparkles,
};

function ReadOnlyStep({
  step,
  store,
  chain,
}: {
  step: Step;
  store: CaseDraftStore;
  chain: string[];
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <li className="case-draft-step">
      <div className="case-draft-step-row">
        <code>{step.id}</code>
        <span>{stepLabel(step)}</span>
      </div>
      {step.type === 'if' ? (
        <>
          <p className="case-draft-block-label">then</p>
          <ReadOnlySteps steps={step.then} store={store} chain={chain} label={`${step.id} then`} />
          {step.else?.length ? (
            <>
              <p className="case-draft-block-label">else</p>
              <ReadOnlySteps
                steps={step.else}
                store={store}
                chain={chain}
                label={`${step.id} else`}
              />
            </>
          ) : null}
        </>
      ) : null}
      {step.type === 'loop' ? (
        <ReadOnlySteps steps={step.steps} store={store} chain={chain} label={`${step.id} loop`} />
      ) : null}
      {step.type === 'flow' ? (
        <>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
            {expanded ? '折叠流程' : '展开流程'} {step.id}
          </Button>
          {expanded ? <FlowExpansion flowId={step.flow_ref} store={store} chain={chain} /> : null}
        </>
      ) : null}
    </li>
  );
}

function ReadOnlySteps({
  steps,
  store,
  chain,
  label,
}: {
  steps: Step[];
  store: CaseDraftStore;
  chain: string[];
  label: string;
}) {
  return (
    <ol aria-label={label} className="case-draft-branch">
      {steps.map((step) => (
        <ReadOnlyStep key={step.id} step={step} store={store} chain={chain} />
      ))}
    </ol>
  );
}

function FlowRead({
  flowId,
  store,
  chain,
  retry,
}: {
  flowId: string;
  store: CaseDraftStore;
  chain: string[];
  retry: () => void;
}) {
  const [result, setResult] = useState<{ flow: Flow | null; error: string | null }>({
    flow: null,
    error: null,
  });
  useEffect(() => {
    const controller = new AbortController();
    void store.readFlow(flowId, controller.signal).then(
      (flow) => {
        if (!controller.signal.aborted) setResult({ flow, error: null });
      },
      (error: unknown) => {
        if (!controller.signal.aborted)
          setResult({ flow: null, error: error instanceof Error ? error.message : String(error) });
      },
    );
    return () => controller.abort();
  }, [flowId, store]);
  if (result.error)
    return (
      <div className="case-draft-flow">
        <p className="case-draft-error" role="alert">
          流程读取失败: {result.error}
        </p>
        <Button size="sm" variant="ghost" onClick={retry}>
          重新读取流程 {flowId}
        </Button>
      </div>
    );
  if (!result.flow)
    return (
      <p className="case-draft-note" aria-live="polite">
        正在读取流程 {flowId}...
      </p>
    );
  const flow = result.flow;
  return (
    <section className="case-draft-flow" aria-label={`公共流程 ${flowId}`}>
      <div className="case-draft-step-row">
        <strong>{flow.name}</strong>
        <Badge tone="neutral">公共流程 · 只读</Badge>
      </div>
      <p className="case-draft-note">只展开工程中的流程定义，不执行步骤，也不修改公共流程文件。</p>
      <p className="case-draft-note">
        参数:{' '}
        {Object.entries(flow.parameters)
          .map(([name, parameter]) => `${name}: ${parameter?.type ?? ''}`)
          .join(', ') || '无'}{' '}
        / 返回:{' '}
        {Object.entries(flow.returns)
          .map(([name, value]) => `${name}: ${value?.type ?? ''}`)
          .join(', ') || '无'}
      </p>
      <ReadOnlySteps
        steps={flow.steps}
        store={store}
        chain={[...chain, flowId]}
        label={`${flowId} 公共流程步骤`}
      />
    </section>
  );
}

function FlowExpansion({
  flowId,
  store,
  chain = [],
}: {
  flowId: string;
  store: CaseDraftStore;
  chain?: string[];
}) {
  const [revision, setRevision] = useState(0);
  if (chain.includes(flowId))
    return (
      <p className="case-draft-error" role="alert">
        流程循环引用: {[...chain, flowId].join(' → ')}；已停止展开。
      </p>
    );
  if (chain.length >= 16)
    return (
      <p className="case-draft-error" role="alert">
        流程展开超过 16 层，已停止展开。
      </p>
    );
  return (
    <FlowRead
      key={`${flowId}:${revision}`}
      flowId={flowId}
      store={store}
      chain={chain}
      retry={() => setRevision((value) => value + 1)}
    />
  );
}

interface StepListProps {
  steps: Step[];
  location: StepListLocation;
  label: string;
  document: Case;
  original: Case;
  store: CaseDraftStore;
  canEdit: boolean;
  selectedStepId?: string | null;
}

function SortableStep({
  step,
  index,
  count,
  list,
}: {
  step: Step;
  index: number;
  count: number;
  list: StepListProps;
}) {
  const [editing, setEditing] = useState(false);
  const [flowExpanded, setFlowExpanded] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const deleteBase = useRef<Case | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const {
    attributes,
    listeners,
    setNodeRef,
    setActivatorNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: step.id, disabled: !list.canEdit });
  const originalStep = findStep(list.original.steps, step.id);
  const added = !originalStep;
  const changed = originalStep && !sameJson(originalStep, step);
  const Icon = TYPE_ICONS[step.type];
  const canRemove = list.canEdit && (list.location.branch === 'else' || count > 1);
  const move = (offset: number) => {
    const next = editStepList(list.document, list.location, (steps) =>
      arrayMove(steps, index, index + offset),
    );
    list.store.editDraft(next, list.document);
  };
  return (
    <li
      ref={setNodeRef}
      className="case-draft-step"
      data-added={added}
      data-dragging={isDragging}
      data-step-id={step.id}
      data-selected={list.selectedStepId === step.id}
      tabIndex={-1}
      style={{ transform: CSS.Transform.toString(transform), transition }}
    >
      <div className="case-draft-step-row">
        <IconButton
          {...attributes}
          {...listeners}
          ref={setActivatorNodeRef}
          label={`拖动步骤 ${step.id}`}
          title="空格开始拖动，方向键排序，空格确认，Esc 取消"
          icon={<GripVertical />}
          size="sm"
          variant="ghost"
          className="case-draft-drag-handle"
          disabled={!list.canEdit}
        />
        <Icon aria-hidden="true" className="case-draft-type-icon" />
        <code>{step.id}</code>
        <Badge tone={added ? 'info' : changed ? 'warning' : 'neutral'}>
          {added ? '新增步骤' : changed ? '已修改' : '原始步骤'}
        </Badge>
        <span className="case-draft-step-description">{stepLabel(step)}</span>
        <div className="case-draft-step-tools">
          <IconButton
            label={`编辑步骤 ${step.id}`}
            aria-expanded={editing}
            icon={<Pencil />}
            size="sm"
            variant="ghost"
            disabled={!list.canEdit}
            onClick={() => setEditing(true)}
          />
          <IconButton
            label={`上移步骤 ${step.id}`}
            icon={<ArrowUp />}
            size="sm"
            variant="ghost"
            disabled={!list.canEdit || index === 0}
            onClick={() => move(-1)}
          />
          <IconButton
            label={`下移步骤 ${step.id}`}
            icon={<ArrowDown />}
            size="sm"
            variant="ghost"
            disabled={!list.canEdit || index === count - 1}
            onClick={() => move(1)}
          />
          <IconButton
            label={`删除步骤 ${step.id}`}
            title={canRemove ? '仅从本地草稿删除' : '必需步骤块至少保留一个步骤'}
            icon={<Trash2 />}
            size="sm"
            variant="ghost"
            disabled={!canRemove}
            onClick={() => {
              deleteBase.current = list.document;
              setDeleteError(null);
              setDeleting(true);
            }}
          />
        </div>
      </div>
      {editing ? (
        <DraftForm<Step>
          label={`编辑步骤 ${step.id}`}
          document={list.document}
          store={list.store}
          disabled={!list.canEdit}
          onClose={() => setEditing(false)}
          beforeSave={(previous, next) =>
            previous.id !== next.id &&
            globalThis.document.querySelector(
              `[data-step-id="${previous.id}"] .case-draft-branch-block .case-editor-form`,
            )
              ? '子步骤编辑器仍打开，请先保存或放弃并收起，再修改此步骤 ID。'
              : null
          }
          select={(document) => {
            const current = findStep(document.steps, step.id);
            if (!current) throw new Error('步骤已移除，未保存旧编辑');
            return current;
          }}
          replace={(document, next) => {
            const current = findStep(document.steps, step.id);
            if (!current) throw new Error('步骤已移除，未保存旧编辑');
            return replaceStep(document, current, next);
          }}
        >
          {(value, change, onIssue) => (
            <StepFields
              value={value}
              document={list.document}
              onChange={change}
              onIssue={onIssue}
            />
          )}
        </DraftForm>
      ) : null}
      {step.type === 'flow' ? (
        <>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={flowExpanded}
            onClick={() => setFlowExpanded(!flowExpanded)}
          >
            {flowExpanded ? (
              <ChevronDown aria-hidden="true" />
            ) : (
              <ChevronRight aria-hidden="true" />
            )}
            {flowExpanded ? '折叠流程' : '展开流程'} {step.id}
          </Button>
          {flowExpanded ? <FlowExpansion flowId={step.flow_ref} store={list.store} /> : null}
        </>
      ) : null}
      {step.type === 'if' ? (
        <>
          <p className="case-draft-block-label">then / 条件成立</p>
          <StepList
            {...list}
            steps={step.then}
            location={{ parentId: step.id, branch: 'then' }}
            label={`${step.id} then`}
          />
          <p className="case-draft-block-label">else / 条件不成立</p>
          <StepList
            {...list}
            steps={step.else ?? []}
            location={{ parentId: step.id, branch: 'else' }}
            label={`${step.id} else`}
          />
        </>
      ) : null}
      {step.type === 'loop' ? (
        <>
          <p className="case-draft-block-label">loop / 每轮独立作用域</p>
          <StepList
            {...list}
            steps={step.steps}
            location={{ parentId: step.id, branch: 'steps' }}
            label={`${step.id} loop`}
          />
        </>
      ) : null}
      <Dialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`删除步骤 ${step.id}`}
        description="仅修改本地草稿，不删除工程原始用例。控制流步骤的子步骤也会一起移除。"
      >
        {deleteError ? (
          <p className="case-draft-error" role="alert">
            {deleteError}
          </p>
        ) : null}
        <Button
          size="sm"
          variant="danger"
          disabled={!canRemove}
          onClick={() => {
            const base = deleteBase.current;
            if (!base) return;
            const next = editStepList(base, list.location, (steps) =>
              steps.filter((item) => item.id !== step.id),
            );
            if (list.store.editDraft(next, base)) setDeleting(false);
            else setDeleteError(list.store.getSnapshot().error ?? '步骤未删除');
          }}
        >
          确认从草稿删除
        </Button>
      </Dialog>
    </li>
  );
}

export function StepList(props: StepListProps) {
  const [newType, setNewType] = useState<Step['type']>('action');
  const dragBase = useRef<Case | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    const document = dragBase.current;
    dragBase.current = null;
    if (!document || !over || active.id === over.id) return;
    const from = props.steps.findIndex((step) => step.id === active.id);
    const to = props.steps.findIndex((step) => step.id === over.id);
    if (from < 0 || to < 0) return;
    const next = editStepList(document, props.location, (steps) => arrayMove(steps, from, to));
    props.store.editDraft(next, document);
  };
  return (
    <div
      className={
        props.location.parentId === null ? 'case-draft-step-list' : 'case-draft-branch-block'
      }
    >
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={() => {
          dragBase.current = props.document;
        }}
        onDragCancel={() => {
          dragBase.current = null;
        }}
        onDragEnd={onDragEnd}
        accessibility={{
          screenReaderInstructions: {
            draggable:
              '按空格开始拖动步骤，用上下方向键在当前步骤块内排序，空格确认，Escape 取消。',
          },
          announcements: {
            onDragStart: ({ active }) => `开始拖动步骤 ${String(active.id)}`,
            onDragOver: ({ active, over }) =>
              over ? `步骤 ${String(active.id)} 移动到 ${String(over.id)}` : '仍在当前步骤块中',
            onDragEnd: ({ active, over }) =>
              over ? `步骤 ${String(active.id)} 排序结束` : '排序取消',
            onDragCancel: () => '排序取消，草稿未修改',
          },
        }}
      >
        <SortableContext
          items={props.steps.map((step) => step.id)}
          strategy={verticalListSortingStrategy}
        >
          <ol
            className={props.location.parentId === null ? 'case-draft-steps' : 'case-draft-branch'}
            aria-label={props.label}
          >
            {props.steps.map((step, index) => (
              <SortableStep
                key={step.id}
                step={step}
                index={index}
                count={props.steps.length}
                list={props}
              />
            ))}
          </ol>
        </SortableContext>
      </DndContext>
      {props.steps.length === 0 ? (
        <p className="case-draft-note">此分支为空，可添加步骤。</p>
      ) : null}
      <div className="case-draft-add-step">
        <Select
          size="sm"
          aria-label={`新增步骤类型 ${props.label}`}
          value={newType}
          disabled={!props.canEdit}
          items={STEP_TYPES.map((type) => ({ value: type, label: type }))}
          onValueChange={(type) => setNewType(type as Step['type'])}
        />
        <Button
          size="sm"
          variant="ghost"
          disabled={!props.canEdit}
          onClick={() => {
            const step = createStep(props.document, newType);
            props.store.editDraft(
              editStepList(props.document, props.location, (steps) => [...steps, step]),
              props.document,
            );
          }}
        >
          <Plus aria-hidden="true" />
          添加步骤到 {props.label}
        </Button>
      </div>
    </div>
  );
}
