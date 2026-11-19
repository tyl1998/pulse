import { useEffect, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { CaseDraftPanel } from '../editor/CaseDraftPanel';
import { createCaseDraftStore } from '../editor/caseDraftStore';
import { SCREEN_GEOMETRY } from '../stream/screenFixtures';
import { ElementInspector } from './ElementInspector';
import { createElementInspectorStore, type InspectorApi } from './inspectorStore';
import {
  INSPECTOR_CONNECTION,
  INSPECTOR_DEVICE,
  simulatedInspectorCase,
  simulatedInspectorObservation,
  simulatedInspectorProjectState,
  simulatedLocateResult,
} from './inspectorFixtures';

type Scenario =
  | 'ready'
  | 'ambiguous'
  | 'unavailable'
  | 'ocr'
  | 'expired'
  | 'offline'
  | 'large'
  | 'loading'
  | 'error';

function InspectorPreview({
  scenario = 'ready',
  width = 940,
}: {
  scenario?: Scenario;
  width?: number;
}) {
  const [preview] = useState(() => {
    const project = simulatedInspectorProjectState();
    const records = new Map<string, string>();
    const draft = createCaseDraftStore({
      projects: { getSnapshot: () => project, subscribe: () => () => undefined },
      storage: {
        getItem: (key) => records.get(key) ?? null,
        setItem: (key, value) => {
          records.set(key, value);
        },
      },
      now: () => 1000,
      getClient: async () => ({
        getProject: async () => project.project,
        getCase: async () => simulatedInspectorCase(),
        verifyConnection: async () => undefined,
      }),
    });
    const api: InspectorApi = {
      getAgentGeometry: async () => ({
        serial: INSPECTOR_DEVICE.serial,
        session_id: INSPECTOR_DEVICE.sessionId,
        source: 'adb_input_viewport',
        status: 'available',
        atomic_with_observation: false,
        geometry: SCREEN_GEOMETRY,
      }),
      observeAgent: async (_serial, body) => {
        if (scenario === 'loading') return new Promise(() => undefined);
        if (scenario === 'error') throw new Error('合成观察请求失败；不是 Runtime 或设备错误。');
        const result = simulatedInspectorObservation(body.request_id, SCREEN_GEOMETRY, {
          ocr: body.ocr,
          count: scenario === 'large' ? 3000 : 0,
          duplicateText: true,
        });
        if (scenario === 'unavailable' || scenario === 'ocr') {
          result.sources.tree.status = 'unavailable';
          result.sources.tree.error = { code: 'accessibility_unavailable' };
        }
        return result;
      },
      locate: async (_serial, body) =>
        simulatedLocateResult(body, scenario === 'ambiguous' ? 'ambiguous' : 'resolved'),
    };
    const inspector = createElementInspectorStore({
      getConnection: async () => INSPECTOR_CONNECTION,
      clientFor: () => api,
      id: () => crypto.randomUUID(),
      now: () => 1000,
      setTimer: () => null,
      clearTimer: () => undefined,
    });
    return { inspector, draft };
  });
  useEffect(() => {
    let alive = true;
    preview.inspector.setDevice(INSPECTOR_DEVICE);
    if (scenario === 'ocr') preview.inspector.setIncludeOcr(true);
    void preview.inspector.refresh().then(() => {
      if (!alive) return;
      if (scenario !== 'large') preview.inspector.select(scenario === 'ocr' ? 'ocr:0' : 'native:1');
      if (scenario === 'expired')
        preview.inspector.invalidate('合成快照已过期，重新采集后才能生成步骤。');
      if (scenario === 'offline')
        preview.inspector.setDevice(INSPECTOR_DEVICE, '合成设备离线；旧快照仅供查看。');
    });
    return () => {
      alive = false;
      preview.inspector.stop();
      preview.draft.dispose();
    };
  }, [preview, scenario]);
  return (
    <section style={{ width: `min(${width}px, 100%)` }}>
      <h2 style={{ fontSize: 'var(--ui-text-lg)' }}>只读检查与用例草稿</h2>
      <p className="inspector-note">
        合成来源、定位回执和内存草稿；不请求 Runtime / ADB / Agent。预览时钟固定，草稿不写本机工程。
      </p>
      <div className="inspector-preview-layout">
        <ElementInspector store={preview.inspector} draftStore={preview.draft} />
        <CaseDraftPanel store={preview.draft} />
      </div>
    </section>
  );
}

const meta = {
  title: '工作室/元素检查器',
  component: InspectorPreview,
  render: () => <InspectorPreview />,
  parameters: {
    docs: {
      description: {
        component:
          'FE-P2.7：元素树虚拟化、来源/未知状态、候选排序、实时唯一性验证与真实 Case 草稿插入。全部使用合成数据，不连接设备或执行动作。',
      },
    },
  },
} satisfies Meta<typeof InspectorPreview>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Ready: Story = {};
export const Ambiguous: Story = { render: () => <InspectorPreview scenario="ambiguous" /> };
export const Unavailable: Story = { render: () => <InspectorPreview scenario="unavailable" /> };
export const OcrOnly: Story = { render: () => <InspectorPreview scenario="ocr" /> };
export const Expired: Story = { render: () => <InspectorPreview scenario="expired" /> };
export const Offline: Story = { render: () => <InspectorPreview scenario="offline" /> };
export const LargeTree: Story = { render: () => <InspectorPreview scenario="large" /> };
export const NarrowPanel: Story = { render: () => <InspectorPreview width={280} /> };
export const Loading: Story = { render: () => <InspectorPreview scenario="loading" /> };
export const RequestError: Story = { render: () => <InspectorPreview scenario="error" /> };
