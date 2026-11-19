import { useEffect, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import type { WorkerLike } from '../../shared/stream/streamClient';
import type { FromWorker, Metrics, ToWorker } from '../../shared/stream/workerProtocol';
import { simulatedDevice } from '../devices/deviceFixtures';
import { createDeviceStore } from '../devices/deviceStore';
import { CaseDraftPanel } from '../editor/CaseDraftPanel';
import { createCaseDraftStore } from '../editor/caseDraftStore';
import { ElementInspector } from '../inspector/ElementInspector';
import { createElementInspectorStore, type InspectorApi } from '../inspector/inspectorStore';
import {
  simulatedInspectorCase,
  simulatedInspectorObservation,
  simulatedInspectorProjectState,
  simulatedLocateResult,
} from '../inspector/inspectorFixtures';
import { DeviceScreen, type DeviceScreenDeps } from './DeviceScreen';
import type { DisplayGeometry, ScreenControlApi } from './screenControl';
import {
  SCREEN_FRAME,
  SCREEN_GEOMETRY,
  SCREEN_SERIAL,
  SCREEN_SESSION,
  simulatedActionReceipt,
  simulatedScreenControl,
} from './screenFixtures';

type Scenario =
  'ready' | 'foreign' | 'interrupted' | 'degraded' | 'unknown-profile' | 'unsupported';

/** Synthetic preview art only; the product decoder never draws video on this thread. */
function drawPreview(canvas: HTMLCanvasElement, rotation: DisplayGeometry['rotation']) {
  canvas.width = SCREEN_FRAME.width;
  canvas.height = SCREEN_FRAME.height;
  const context = canvas.getContext('2d');
  if (!context) return;
  const sideways = rotation === 90 || rotation === 270;
  const width = sideways ? canvas.height : canvas.width;
  const height = sideways ? canvas.width : canvas.height;
  const geometry = {
    ...SCREEN_GEOMETRY,
    rotation,
    width: sideways ? 2400 : 1080,
    height: sideways ? 1080 : 2400,
  };
  const nodes = simulatedInspectorObservation('synthetic-preview', geometry).sources.tree.data
    .nodes;
  const field = nodes[1]?.bounds;
  const button = nodes[2]?.bounds;
  context.save();
  if (rotation === 90) {
    context.translate(canvas.width, 0);
    context.rotate(Math.PI / 2);
  }
  if (rotation === 180) {
    context.translate(canvas.width, canvas.height);
    context.rotate(Math.PI);
  }
  if (rotation === 270) {
    context.translate(0, canvas.height);
    context.rotate(-Math.PI / 2);
  }
  context.fillStyle = '#f8fafc';
  context.fillRect(0, 0, width, height);
  context.fillStyle = '#2559a7';
  context.fillRect(0, 0, width, 150);
  context.fillStyle = '#ffffff';
  context.font = '32px sans-serif';
  context.fillText('Pulse Target', 36, 84);
  context.fillStyle = '#172033';
  context.font = '28px sans-serif';
  context.fillText('Synthetic form', 36, 230);
  for (const [bounds, fill, color, label] of [
    [field, '#d5dde8', '#172033', '订单号'],
    [button, '#2559a7', '#ffffff', '提交订单'],
  ] as const) {
    if (!bounds) continue;
    const scale = 2 / 3;
    context.fillStyle = fill;
    context.fillRect(
      bounds.left * scale,
      bounds.top * scale,
      (bounds.right - bounds.left) * scale,
      (bounds.bottom - bounds.top) * scale,
    );
    context.fillStyle = color;
    context.font = '24px sans-serif';
    context.fillText(
      label,
      bounds.left * scale + 20,
      ((bounds.top + bounds.bottom) * scale) / 2 + 8,
    );
  }
  if (!sideways) {
    context.fillStyle = '#526177';
    context.font = '20px monospace';
    context.fillText('PREVIEW / NO DEVICE', 36, height - 55);
  }
  context.restore();
}

class PreviewWorker implements WorkerLike {
  onmessage: WorkerLike['onmessage'] = null;
  onerror: WorkerLike['onerror'] = null;
  onmessageerror: WorkerLike['onmessageerror'] = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private connectionId = 0;
  private seq = 0;
  private stopped = true;
  constructor(private scenario: Scenario) {}
  private emit(message: FromWorker) {
    this.onmessage?.({
      data: { ...message, connectionId: this.connectionId },
    } as MessageEvent<unknown>);
  }
  postMessage(message: ToWorker) {
    if (message.type === 'disconnect') {
      this.stop();
      return;
    }
    if (message.type !== 'connect') return;
    this.stopped = false;
    this.connectionId = message.connectionId;
    const connectionId = this.connectionId;
    queueMicrotask(() => {
      if (this.stopped || this.connectionId !== connectionId) return;
      this.emit({ type: 'status', status: { state: 'open' } });
      if (this.scenario === 'interrupted') {
        this.emit({
          type: 'status',
          status: {
            state: 'upstream',
            upstream: 'error',
            message: 'synthetic_capture_unavailable',
          },
        });
        return;
      }
      this.emit({
        type: 'frame',
        frame: {
          ...SCREEN_FRAME,
          captureOrientation: this.scenario === 'unknown-profile' ? null : 'natural',
        },
      });
      this.emit({
        type: 'status',
        status: { state: 'streaming', sessionId: SCREEN_FRAME.sessionId },
      });
      this.metrics();
      this.timer = setInterval(() => this.metrics(), 1000);
    });
  }
  private metrics() {
    this.seq += 1;
    const metrics: Metrics = {
      fps: 30,
      decodeMsMean: 1,
      decodeMsMax: 2,
      decodeQueue: 0,
      droppedWorker: this.scenario === 'degraded' ? this.seq : 0,
      droppedUpstream: 0,
      droppedRelay: 0,
      e2eMsP50: NaN,
      netMsP50: 3,
      relayQueueMsP50: 0,
      networkExcludingRelayMsP50: 3,
      kbps: 4000,
      width: SCREEN_FRAME.width,
      height: SCREEN_FRAME.height,
      codec: 'avc1.64001f',
      clockRttMs: null,
      framesTotal: this.seq,
    };
    this.emit({ type: 'metrics', metrics });
  }
  private stop() {
    this.stopped = true;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
  terminate() {
    this.stop();
  }
}

function ScreenPreview({
  scenario = 'ready',
  rotation = 0,
  width = 440,
  inspect = false,
}: {
  scenario?: Scenario;
  rotation?: DisplayGeometry['rotation'];
  width?: number;
  inspect?: boolean;
}) {
  const [preview] = useState(() => {
    const connection = { port: 1, pid: 1, token: 'synthetic-preview-only', version: '1.0.0' };
    const sideways = rotation === 90 || rotation === 270;
    const geometry: DisplayGeometry = {
      ...SCREEN_GEOMETRY,
      rotation,
      width: sideways ? 2400 : 1080,
      height: sideways ? 1080 : 2400,
    };
    let control = simulatedScreenControl(
      scenario === 'foreign' ? '99999999-0000-4000-8000-000000000001' : null,
    );
    const api: ScreenControlApi & InspectorApi = {
      getControl: async () => ({ serial: SCREEN_SERIAL, control }),
      getAgentGeometry: async () => ({
        serial: SCREEN_SERIAL,
        session_id: SCREEN_SESSION,
        status: 'available',
        source: 'adb_input_viewport',
        geometry,
        atomic_with_observation: false,
      }),
      acquireControl: async (_serial, body) => {
        control = simulatedScreenControl(body.run_id ?? null);
        return { serial: SCREEN_SERIAL, control };
      },
      releaseControl: async () => {
        control = simulatedScreenControl();
        return { serial: SCREEN_SERIAL, control };
      },
      observeAgent: async (_serial, body) =>
        simulatedInspectorObservation(body.request_id, geometry, { ocr: body.ocr }),
      locate: async (_serial, body) => simulatedLocateResult(body),
      performAction: async (_serial, body) => simulatedActionReceipt(body),
    };
    const store = createDeviceStore({
      getConnection: async () => connection,
      clientFor: () => ({
        listDevices: async () => ({ devices: [simulatedDevice()] }),
        getDevice: async () => simulatedDevice(),
        getAgentHealth: async () => simulatedDevice(),
        connectAgent: async () => simulatedDevice(),
        getTargetApp: async () => ({}),
      }),
      setTimer: () => null,
      clearTimer: () => undefined,
    });
    const deps: DeviceScreenDeps = {
      supported: () => scenario !== 'unsupported',
      stream: {
        createWorker: () => new PreviewWorker(scenario),
        setTimer: (callback, ms) => setTimeout(callback, ms),
        clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
      },
      transferCanvas: (canvas) => {
        drawPreview(canvas, rotation);
        return {} as OffscreenCanvas;
      },
      control: {
        getConnection: async () => connection,
        clientFor: () => api,
        now: () => performance.now(),
        id: () => crypto.randomUUID(),
        setTimer: (callback, ms) => setTimeout(callback, ms),
        clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
      },
    };
    const inspector = createElementInspectorStore({
      getConnection: async () => connection,
      clientFor: () => api,
      id: () => crypto.randomUUID(),
      now: () => 1000,
      setTimer: () => null,
      clearTimer: () => undefined,
    });
    const project = simulatedInspectorProjectState();
    const records = new Map<string, string>();
    const draft = createCaseDraftStore({
      projects: { getSnapshot: () => project, subscribe: () => () => undefined },
      getClient: async () => ({
        getProject: async () => project.project,
        getCase: async () => simulatedInspectorCase(),
        verifyConnection: async () => undefined,
      }),
      storage: {
        getItem: (key) => records.get(key) ?? null,
        setItem: (key, value) => {
          records.set(key, value);
        },
      },
      now: () => 1000,
    });
    return { store, deps, inspector, draft };
  });
  useEffect(() => {
    void preview.store.refresh().then(() => preview.store.selectDevice(SCREEN_SERIAL));
    return () => {
      preview.inspector.stop();
      preview.draft.dispose();
    };
  }, [preview]);
  return (
    <section style={{ width: `min(${width}px, 100%)` }}>
      {inspect ? <h2 style={{ fontSize: 'var(--ui-text-lg)' }}>画面检查与一键插入</h2> : null}
      <div className={inspect ? 'inspector-preview-layout' : undefined}>
        <div
          style={{
            width: '100%',
            height: 740,
            maxHeight: 'calc(100vh - 32px)',
            minHeight: 540,
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
          }}
        >
          <p className="device-screen-note">
            模拟 Worker、画面和控制回执。所有指标均为合成数据，不请求 Runtime / ADB / Agent。
          </p>
          <DeviceScreen store={preview.store} deps={preview.deps} inspector={preview.inspector} />
        </div>
        {inspect ? (
          <div>
            <ElementInspector store={preview.inspector} draftStore={preview.draft} />
            <CaseDraftPanel store={preview.draft} />
          </div>
        ) : null}
      </div>
    </section>
  );
}

const meta = {
  title: '工作室/本机设备画面',
  component: DeviceScreen,
  render: () => <ScreenPreview />,
  parameters: {
    docs: {
      description: {
        component:
          'FE-P2.6 / P2.7：本机画面、显式控制会话与只读检查。全部使用合成 Worker 元数据、来源观察和内存草稿，不连接任何设备。检查可悬停、选中并插入草稿；不发送动作或取得控制权。',
      },
    },
  },
} satisfies Meta<typeof DeviceScreen>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Portrait: Story = {};
export const Landscape: Story = { render: () => <ScreenPreview rotation={90} width={760} /> };
export const ReversePortrait: Story = { render: () => <ScreenPreview rotation={180} /> };
export const NarrowPanel: Story = { render: () => <ScreenPreview width={280} /> };
export const OtherController: Story = { render: () => <ScreenPreview scenario="foreign" /> };
export const Interrupted: Story = { render: () => <ScreenPreview scenario="interrupted" /> };
export const Degraded: Story = { render: () => <ScreenPreview scenario="degraded" /> };
export const UnknownCaptureProfile: Story = {
  render: () => <ScreenPreview scenario="unknown-profile" />,
};
export const Unsupported: Story = { render: () => <ScreenPreview scenario="unsupported" /> };
export const InspectAndInsert: Story = { render: () => <ScreenPreview inspect width={980} /> };
export const LandscapeInspector: Story = {
  render: () => <ScreenPreview inspect rotation={90} width={980} />,
};
