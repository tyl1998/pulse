import { useEffect, useState, type ReactNode } from 'react';
import { Group, Panel, Separator, useGroupRef, type Layout } from 'react-resizable-panels';
import { ChevronDown, ChevronUp, LayoutGrid, PanelBottom, RotateCcw } from 'lucide-react';
import { Badge, Button, Tabs, type TabItem } from '../../shared/ui';
import { CaseDraftPanel } from '../editor/CaseDraftPanel';
import { getCaseDraftStore } from '../editor/caseDraftStore';
import { CaseProblemsPanel, CaseProblemsProvider } from '../editor/CaseProblems';
import { ElementInspector } from '../inspector/ElementInspector';
import { createElementInspectorStore } from '../inspector/inspectorStore';
import { CaseTree } from '../projects/CaseTree';
import { useProjectStore } from '../projects/projectStore';
import { DeviceScreen } from '../stream/DeviceScreen';
import { isBottomTabId, PRESET_IDS, WORKBENCH_PRESETS, useWorkbenchLayout } from './layoutStore';

const BOTTOM_TABS: readonly TabItem[] = [
  {
    value: 'run',
    label: '运行控制',
    content: (
      <DockPlaceholder
        title="运行控制"
        description="运行对话框、预检与停止四态将在 FE-P2.12 接入。"
      />
    ),
  },
  {
    value: 'progress',
    label: '步骤进度',
    content: (
      <DockPlaceholder title="步骤进度" description="逐步耗时与定位来源将在 FE-P2.12 接入。" />
    ),
  },
  {
    value: 'events',
    label: '事件日志',
    content: (
      <DockPlaceholder
        title="事件日志"
        description="Runtime 事件流将由 FE-P2.2 的 EventBus 接入。"
      />
    ),
  },
  {
    value: 'variables',
    label: '变量',
    content: <DockPlaceholder title="变量" description="调试变量面板将在 FE-P2.13 接入。" />,
  },
  {
    value: 'ai',
    label: 'AI 聊天',
    content: (
      <DockPlaceholder title="AI 聊天" description="仅创作、不操作设备的聊天面板在 P6 启用。" />
    ),
  },
  {
    value: 'issues',
    label: '问题',
    content: <CaseProblemsPanel />,
  },
];

function DockPlaceholder({ title, description }: { title: string; description: string }) {
  return (
    <div className="studio-dock-placeholder" data-testid={`dock-placeholder-${title}`}>
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}

function DockPanel({
  title,
  phase,
  children,
}: {
  title: string;
  phase: string;
  children: ReactNode;
}) {
  return (
    <section className="studio-panel-surface" aria-label={title}>
      <header className="studio-panel-header">
        <h2>{title}</h2>
        <Badge tone="neutral">{phase}</Badge>
      </header>
      <div className="studio-panel-body" tabIndex={0}>
        {children}
      </div>
    </section>
  );
}

export function WorkbenchDock() {
  const [inspector] = useState(createElementInspectorStore);
  const [drafts] = useState(getCaseDraftStore);
  const { state: projectState } = useProjectStore();
  const { state, store } = useWorkbenchLayout(
    projectState.project ? { projectId: projectState.project.project_id } : {},
  );
  const [resetRevision, setResetRevision] = useState(0);
  const rightRef = useGroupRef();
  const verticalRef = useGroupRef();
  const [narrow, setNarrow] = useState(
    () => window.matchMedia?.('(max-width: 700px)').matches ?? false,
  );
  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 700px)');
    if (!media) return;
    const resize = () => setNarrow(media.matches);
    media.addEventListener('change', resize);
    return () => media.removeEventListener('change', resize);
  }, []);
  const preset = state.presets[state.activePreset];
  const presetMeta = WORKBENCH_PRESETS[state.activePreset];
  const elementsVisible = (preset.right['elements'] ?? 0) > 0;
  const bottomExpanded = (preset.vertical['bottom'] ?? 0) > 10;
  const openElements = () => {
    if (elementsVisible) return;
    const layout = { device: 60, elements: 40 };
    store.updateGroup('right', layout);
    rightRef.current?.setLayout(layout);
  };
  useEffect(() => () => inspector.stop(), [inspector]);

  const onGroupLayout = (
    group: 'horizontal' | 'right' | 'vertical',
    layout: Layout,
    requested: Layout | undefined,
  ) => {
    store.updateGroup(group, requested ?? layout);
  };

  return (
    <CaseProblemsProvider store={drafts} project={projectState.project}>
      <div className="studio-page" data-testid="workbench-dock" data-preset={state.activePreset}>
        <div className="studio-toolbar">
          <span className="studio-toolbar-label">
            <LayoutGrid aria-hidden="true" />
            布局预设
          </span>
          <div className="studio-presets" role="group" aria-label="布局预设">
            {PRESET_IDS.map((id) => (
              <Button
                key={id}
                size="sm"
                variant={state.activePreset === id ? 'primary' : 'secondary'}
                aria-pressed={state.activePreset === id}
                onClick={() => store.setActivePreset(id)}
              >
                {WORKBENCH_PRESETS[id].label}
              </Button>
            ))}
          </div>
          <span className="studio-preset-note">{presetMeta.description}</span>
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={elementsVisible}
            aria-controls="elements"
            onClick={() => {
              const layout = elementsVisible
                ? { device: 100, elements: 0 }
                : { device: 75, elements: 25 };
              store.updateGroup('right', layout);
              rightRef.current?.setLayout(layout);
            }}
          >
            {elementsVisible ? (
              <ChevronUp aria-hidden="true" />
            ) : (
              <ChevronDown aria-hidden="true" />
            )}
            元素面板
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={bottomExpanded}
            aria-controls="bottom"
            onClick={() => {
              const layout = bottomExpanded
                ? { workspace: 94, bottom: 6 }
                : { workspace: 72, bottom: 28 };
              store.updateGroup('vertical', layout);
              verticalRef.current?.setLayout(layout);
            }}
          >
            <PanelBottom aria-hidden="true" />
            {bottomExpanded ? '收起底部面板' : '展开底部面板'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              store.resetActivePreset();
              setResetRevision((value) => value + 1);
            }}
          >
            <RotateCcw aria-hidden="true" />
            重置当前预设
          </Button>
        </div>

        <div className="studio-dock">
          <Group
            key={`vertical-${state.activePreset}-${resetRevision}`}
            id="studio-vertical"
            orientation="vertical"
            className="studio-group"
            groupRef={verticalRef}
            defaultLayout={preset.vertical}
            onLayoutChanged={(layout, meta) =>
              onGroupLayout('vertical', layout, meta.requestedLayout)
            }
          >
            <Panel id="workspace" className="studio-panel" minSize="30%">
              <Group
                key={`horizontal-${state.activePreset}-${resetRevision}`}
                id="studio-horizontal"
                orientation={narrow ? 'vertical' : 'horizontal'}
                className="studio-group"
                defaultLayout={preset.horizontal}
                onLayoutChanged={(layout, meta) =>
                  onGroupLayout('horizontal', layout, meta.requestedLayout)
                }
              >
                <Panel id="outline" className="studio-panel" minSize={narrow ? '160px' : '14%'}>
                  <DockPanel title="用例 / 步骤大纲" phase="FE-P2.4">
                    <CaseTree />
                  </DockPanel>
                </Panel>
                <Separator
                  id="studio-h-separator-outline"
                  className={`studio-separator studio-separator-${narrow ? 'vertical' : 'horizontal'}`}
                  aria-label={narrow ? '调整用例大纲高度' : '调整用例大纲宽度'}
                />
                <Panel id="editor" className="studio-panel" minSize={narrow ? '160px' : '24%'}>
                  <DockPanel title="编辑区" phase="FE-P2.10">
                    <CaseDraftPanel store={drafts} />
                  </DockPanel>
                </Panel>
                <Separator
                  id="studio-h-separator-right"
                  className={`studio-separator studio-separator-${narrow ? 'vertical' : 'horizontal'}`}
                  aria-label={narrow ? '调整设备面板高度' : '调整右侧面板宽度'}
                />
                <Panel id="right" className="studio-panel" minSize={narrow ? '560px' : '28%'}>
                  <Group
                    key={`right-${state.activePreset}-${resetRevision}`}
                    id="studio-right"
                    orientation="vertical"
                    className="studio-group"
                    groupRef={rightRef}
                    defaultLayout={preset.right}
                    onLayoutChanged={(layout, meta) =>
                      onGroupLayout('right', layout, meta.requestedLayout)
                    }
                  >
                    <Panel id="device" className="studio-panel" minSize="40%">
                      <DockPanel title="设备画面" phase="本机直连">
                        <DeviceScreen inspector={inspector} onInspect={openElements} />
                      </DockPanel>
                    </Panel>
                    <Separator
                      id="studio-right-separator"
                      className="studio-separator studio-separator-vertical"
                      aria-label="调整设备画面与元素树高度"
                    />
                    <Panel
                      id="elements"
                      className="studio-panel"
                      minSize="0%"
                      inert={!elementsVisible}
                      aria-hidden={!elementsVisible}
                    >
                      <DockPanel title="元素树 / 属性" phase="只读检查">
                        <ElementInspector store={inspector} draftStore={drafts} />
                      </DockPanel>
                    </Panel>
                  </Group>
                </Panel>
              </Group>
            </Panel>
            <Separator
              id="studio-v-separator"
              className="studio-separator studio-separator-vertical"
              aria-label="调整底部面板高度"
            />
            <Panel id="bottom" className="studio-panel" minSize="40px">
              <section
                className="studio-panel-surface"
                aria-label="底部面板"
                data-collapsed={!bottomExpanded}
              >
                <Tabs
                  className="studio-bottom-tabs"
                  label="底部面板"
                  items={BOTTOM_TABS}
                  value={preset.bottomTab}
                  onValueChange={(value) => {
                    if (isBottomTabId(value)) {
                      store.setBottomTab(value);
                      if (!bottomExpanded) {
                        const layout = { workspace: 72, bottom: 28 };
                        store.updateGroup('vertical', layout);
                        verticalRef.current?.setLayout(layout);
                      }
                    }
                  }}
                />
              </section>
            </Panel>
          </Group>
        </div>
      </div>
    </CaseProblemsProvider>
  );
}
