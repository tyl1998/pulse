import { useEffect, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import operatorsCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/operators.json';
import aiCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/ai.json';
import fillSearch from '../../../../../../fixtures/golden-cases/v1-draft/flows/fill-search.json';
import goldenManifest from '../../../../../../fixtures/golden-cases/v1-draft/manifest.json';
import { simulatedInspectorProjectState } from '../inspector/inspectorFixtures';
import { CaseDraftPanel } from './CaseDraftPanel';
import { createCaseDraftStore } from './caseDraftStore';
import { CaseProblemsPanel, CaseProblemsProvider } from './CaseProblems';

type Scenario =
  'ready' | 'operators' | 'ai' | 'issues' | 'save-failed' | 'read-only' | 'offline' | 'flow-error';

function EditorPreview({
  scenario = 'ready',
  width = 760,
  initialView = 'steps',
}: {
  scenario?: Scenario;
  width?: number;
  initialView?: 'three' | 'steps' | 'json';
}) {
  const [{ store, project }] = useState(() => {
    const fixture =
      scenario === 'operators'
        ? operatorsCase
        : scenario === 'ai'
          ? aiCase
          : scenario === 'issues'
            ? {
                ...controlFlowCase,
                steps: [
                  {
                    type: 'action',
                    id: 'clear-conflict',
                    action: { kind: 'clear', app_id: 'com.pulse.target' },
                  },
                  {
                    type: 'action',
                    id: 'unresolved-input',
                    action: {
                      kind: 'input',
                      mode: 'replace',
                      text: { param_ref: 'undeclared' },
                      target: { locator_ref: 'search' },
                    },
                  },
                  ...aiCase.steps,
                ],
              }
            : controlFlowCase;
    const state = simulatedInspectorProjectState();
    if (!state.project) throw new Error('Missing synthetic project');
    const root = '/synthetic/fe-p28';
    const projectId = 'editor-preview';
    state.scope = { root, projectId };
    state.project = {
      ...state.project,
      root,
      project_id: projectId,
      name: '合成步骤编辑工程',
      case_ids: [fixture.case_id],
      entry_cases: [fixture.case_id],
      flow_ids: ['fill-search'],
      data_ids: ['cart'],
      profile_ids: ['android-fixture'],
      manifest: { ...goldenManifest, project_id: projectId },
    };
    state.selectedCaseId = fixture.case_id;
    const records = new Map<string, string>();
    const store = createCaseDraftStore({
      projects: { getSnapshot: () => state, subscribe: () => () => undefined },
      getClient: async () => ({
        getProject: async () => state.project,
        getCase: async () => {
          if (scenario === 'offline')
            throw new Error('合成 Runtime 离线，未从未验证的缓存恢复用例。');
          return fixture;
        },
        getFlow: async () => {
          if (scenario === 'flow-error') throw new Error('合成流程读取失败；用例仍可编辑。');
          return fillSearch;
        },
        verifyConnection: async () => undefined,
      }),
      storage:
        scenario === 'read-only'
          ? null
          : {
              getItem: (key) => records.get(key) ?? null,
              setItem: (key, value) => {
                if (scenario === 'save-failed') throw new Error('合成草稿存储已满，未确认保存。');
                records.set(key, value);
              },
            },
      now: () => 1000,
    });
    return { store, project: state.project };
  });
  useEffect(() => () => store.dispose(), [store]);
  return (
    <div style={{ width: `min(${width}px, 100%)` }}>
      <p className="case-draft-note">
        黄金用例与内存草稿预览。不连接 Runtime / ADB / Agent，不写本机工程，不请求模型。
      </p>
      <CaseProblemsProvider store={store} project={project}>
        <div
          style={{
            height: 760,
            marginTop: 12,
            display: 'grid',
            gridTemplateRows: 'minmax(0, 1fr) minmax(100px, 32%)',
          }}
        >
          <CaseDraftPanel store={store} initialView={initialView} />
          <div style={{ overflow: 'auto' }}>
            <CaseProblemsPanel />
          </div>
        </div>
      </CaseProblemsProvider>
    </div>
  );
}

const meta = {
  title: '工作室/步骤编辑器',
  component: EditorPreview,
  render: () => <EditorPreview />,
  parameters: {
    docs: {
      description: {
        component:
          'FE-P2.8 / FE-P2.9 / FE-P2.10：八类步骤、14 个检查运算符、参数声明与初始输入、三段式与离线 Monaco、三视图共享草稿与撤销历史。问题面板覆盖未保存 Schema 错误、引用、AI 依赖与禁止动作，并可点击定位。来源原文保留，不请求模型或执行设备动作。全部使用黄金资产和合成读取接口。',
      },
    },
  },
} satisfies Meta<typeof EditorPreview>;

export default meta;
type Story = StoryObj<typeof meta>;
export const GoldenControlFlow: Story = {};
export const Operators: Story = { render: () => <EditorPreview scenario="operators" /> };
export const AiStep: Story = { render: () => <EditorPreview scenario="ai" /> };
export const Problems: Story = { render: () => <EditorPreview scenario="issues" /> };
export const NarrowProblems: Story = {
  render: () => <EditorPreview scenario="issues" width={390} />,
};
export const NarrowPanel: Story = { render: () => <EditorPreview width={280} /> };
export const SaveFailure: Story = { render: () => <EditorPreview scenario="save-failed" /> };
export const ReadOnly: Story = { render: () => <EditorPreview scenario="read-only" /> };
export const RuntimeOffline: Story = { render: () => <EditorPreview scenario="offline" /> };
export const FlowFailure: Story = { render: () => <EditorPreview scenario="flow-error" /> };
export const ThreeSections: Story = { render: () => <EditorPreview initialView="three" /> };
export const JsonSchema: Story = { render: () => <EditorPreview initialView="json" /> };
export const NarrowJson: Story = { render: () => <EditorPreview initialView="json" width={280} /> };
export const JsonReadOnly: Story = {
  render: () => <EditorPreview initialView="json" scenario="read-only" />,
};
