import { Profiler, StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import type {
  FromWorker,
  Metrics,
  PresentedFrame,
  ToWorker,
} from '../../shared/stream/workerProtocol';
import type { WorkerLike } from '../../shared/stream/streamClient';
import { displayToClient, type Rotation } from '../../shared/lib/coords';
import { createElementInspectorStore, type InspectorApi } from '../inspector/inspectorStore';
import {
  simulatedInspectorObservation,
  simulatedLocateResult,
} from '../inspector/inspectorFixtures';
import { simulatedDevice } from '../devices/deviceFixtures';
import { createDeviceStore } from '../devices/deviceStore';
import { DeviceScreen, type DeviceScreenDeps } from './DeviceScreen';
import type { ScreenActionBody, ScreenControlApi } from './screenControl';
import {
  SCREEN_FRAME,
  SCREEN_GEOMETRY,
  SCREEN_SERIAL,
  SCREEN_SESSION,
  simulatedActionReceipt,
  simulatedNativeObservation,
  simulatedScreenControl,
} from './screenFixtures';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

class ScreenWorker implements WorkerLike {
  onmessage: WorkerLike['onmessage'] = null;
  onerror: WorkerLike['onerror'] = null;
  onmessageerror: WorkerLike['onmessageerror'] = null;
  sent: ToWorker[] = [];
  connectionId = 0;
  terminated = false;
  postMessage(message: ToWorker) {
    this.sent.push(message);
    if (message.type === 'connect') this.connectionId = message.connectionId;
  }
  emit(message: FromWorker) {
    this.onmessage?.({
      data: { ...message, connectionId: this.connectionId },
    } as MessageEvent<unknown>);
  }
  terminate() {
    this.terminated = true;
  }
}

function fixture({ foreign = false, supported = true, strict = false, inspection = false } = {}) {
  const connection = { port: 4311, token: 'synthetic-ui-only', pid: 9, version: '1.0.0' };
  let control = simulatedScreenControl(foreign ? '99999999-0000-4000-8000-000000000001' : null);
  let geometry = { ...SCREEN_GEOMETRY };
  const workers: ScreenWorker[] = [];
  const canvases: HTMLCanvasElement[] = [];
  const polls = new Set<() => void>();
  const api = {
    getControl: vi.fn<ScreenControlApi['getControl']>(async (serial) => ({
      serial,
      control: { ...control, device_id: serial },
    })),
    getAgentGeometry: vi.fn<ScreenControlApi['getAgentGeometry']>(async (serial) => ({
      serial,
      session_id: SCREEN_SESSION,
      status: 'available',
      source: 'adb_input_viewport',
      geometry,
      atomic_with_observation: false,
    })),
    acquireControl: vi.fn<ScreenControlApi['acquireControl']>(async (serial, body) => {
      control = simulatedScreenControl(body.run_id ?? null);
      return { serial, control: { ...control, device_id: serial } };
    }),
    releaseControl: vi.fn<ScreenControlApi['releaseControl']>(async (serial) => {
      control = simulatedScreenControl();
      return { serial, control: { ...control, device_id: serial } };
    }),
    observeAgent: vi.fn<ScreenControlApi['observeAgent']>(async (serial, body) => ({
      ...simulatedNativeObservation(body.request_id, geometry),
      serial,
    })),
    performAction: vi.fn<ScreenControlApi['performAction']>(async (_serial, body) =>
      simulatedActionReceipt(body),
    ),
  };
  const store = createDeviceStore({
    getConnection: async () => connection,
    clientFor: () => ({
      listDevices: async () => ({ devices: [simulatedDevice(), simulatedDevice('SIM-ANDROID-B')] }),
      getDevice: async (serial) => simulatedDevice(serial),
      getAgentHealth: async (serial) => simulatedDevice(serial),
      connectAgent: async (serial) => simulatedDevice(serial),
      getTargetApp: async () => ({}),
    }),
    setTimer: () => null,
    clearTimer: () => undefined,
  });
  const deps: DeviceScreenDeps = {
    supported: () => supported,
    stream: {
      createWorker: () => {
        const worker = new ScreenWorker();
        workers.push(worker);
        return worker;
      },
      setTimer: (callback, ms) => setTimeout(callback, ms),
      clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
    },
    transferCanvas: (canvas) => {
      canvases.push(canvas);
      return {} as OffscreenCanvas;
    },
    control: {
      getConnection: async () => connection,
      clientFor: () => api,
      now: () => 1000,
      id: () => crypto.randomUUID(),
      setTimer: (callback) => {
        polls.add(callback);
        return callback;
      },
      clearTimer: (timer) => {
        polls.delete(timer as () => void);
      },
    },
  };
  const renders = vi.fn();
  const locate = vi.fn<InspectorApi['locate']>(async (_serial, body) =>
    simulatedLocateResult(body),
  );
  const inspector = inspection
    ? createElementInspectorStore({
        getConnection: async () => connection,
        clientFor: () => ({ ...api, locate }),
        now: () => 1000,
        id: () => crypto.randomUUID(),
        setTimer: () => null,
        clearTimer: () => undefined,
      })
    : undefined;
  const component = (
    <Profiler id="screen" onRender={renders}>
      <DeviceScreen store={store} deps={deps} {...(inspector ? { inspector } : {})} />
    </Profiler>
  );
  const view = render(strict ? <StrictMode>{component}</StrictMode> : component);
  const metrics: Metrics = {
    fps: 30,
    decodeMsMean: 1,
    decodeMsMax: 2,
    decodeQueue: 0,
    droppedWorker: 0,
    droppedUpstream: 0,
    droppedRelay: 0,
    e2eMsP50: NaN,
    netMsP50: 3,
    relayQueueMsP50: 0,
    networkExcludingRelayMsP50: 3,
    kbps: 4000,
    width: 720,
    height: 1600,
    codec: 'avc1.64001f',
    clockRttMs: null,
    framesTotal: 1,
  };
  const rect = { left: 100, top: 50, width: 400, height: 400 };
  const worker = () => {
    const current = workers.at(-1);
    if (!current) throw new Error('no worker');
    return current;
  };
  const select = async () => {
    await store.refresh();
    act(() => store.selectDevice(SCREEN_SERIAL));
    if (!supported) return;
    await waitFor(() =>
      expect(worker().sent.some((message) => message.type === 'connect')).toBe(true),
    );
    const host = screen.getByRole('group', { name: '设备画面交互区' });
    vi.spyOn(host, 'getBoundingClientRect').mockImplementation(
      () => new DOMRect(rect.left, rect.top, rect.width, rect.height),
    );
    act(() => {
      worker().emit({ type: 'status', status: { state: 'open' } });
      worker().emit({ type: 'frame', frame: SCREEN_FRAME });
      worker().emit({
        type: 'status',
        status: { state: 'streaming', sessionId: SCREEN_FRAME.sessionId },
      });
      worker().emit({ type: 'metrics', metrics });
    });
    await screen.findByText('本机画面已连接');
    for (const canvas of canvases) {
      vi.spyOn(canvas, 'getBoundingClientRect').mockImplementation(() => {
        const angle = Number(canvas.dataset['rotation']);
        const sideways = angle === 90 || angle === 270;
        const width = parseFloat(sideways ? canvas.style.height : canvas.style.width);
        const height = parseFloat(sideways ? canvas.style.width : canvas.style.height);
        return new DOMRect(
          rect.left + (rect.width - width) / 2,
          rect.top + (rect.height - height) / 2,
          width,
          height,
        );
      });
    }
  };
  return {
    api,
    inspector,
    locate,
    store,
    workers,
    canvases,
    worker,
    rect,
    select,
    renders,
    metrics,
    ...view,
    setGeometry: (value: typeof geometry) => {
      geometry = value;
    },
    setControl: (value: typeof control) => {
      control = value;
    },
    poll: async () => {
      await act(async () => {
        for (const callback of [...polls]) callback();
      });
    },
    operate: async () => {
      await select();
      await userEvent.click(await screen.findByRole('button', { name: '取得控制权' }));
      await waitFor(() => expect(screen.getByRole('button', { name: '操作' })).toBeEnabled());
      await userEvent.click(screen.getByRole('button', { name: '操作' }));
    },
    emitFrame: (frame: PresentedFrame) => act(() => worker().emit({ type: 'frame', frame })),
  };
}

function pointer(
  type: 'pointerdown' | 'pointerup' | 'pointercancel' | 'lostpointercapture',
  x = 300,
  y = 250,
  pointerId = 1,
) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperties(event, { pointerId: { value: pointerId }, isPrimary: { value: true } });
  fireEvent(screen.getByRole('group', { name: '设备画面交互区' }), event);
}

test('requires explicit device selection and does not acquire, install or operate automatically', async () => {
  const h = fixture();
  await h.store.refresh();
  expect(screen.getByRole('heading', { name: '选择要观看的设备' })).toBeVisible();
  expect(h.workers).toHaveLength(0);
  expect(h.api.getControl).not.toHaveBeenCalled();
  expect(h.api.acquireControl).not.toHaveBeenCalled();
  const select = screen.getByRole('combobox', { name: '画面设备' });
  await userEvent.click(select);
  await userEvent.click(screen.getByRole('option', { name: /SIM-ANDROID-A/ }));
  await waitFor(() => expect(h.workers.length).toBeGreaterThan(0));
  expect(h.api.acquireControl).not.toHaveBeenCalled();
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('uses the loopback Worker pipeline with explicit natural profile and displays honest metrics', async () => {
  const h = fixture();
  await h.select();
  const connect = h.worker().sent.find((message) => message.type === 'connect');
  expect(connect?.type === 'connect' && connect.url).toContain(
    'ws://127.0.0.1:4311/ws/stream/SIM-ANDROID-A?',
  );
  expect(
    connect?.type === 'connect' && new URL(connect.url).searchParams.get('capture_orientation'),
  ).toBe('natural');
  expect(connect?.type === 'connect' && connect.relay).toBe(false);
  expect(screen.getByText('720×1600 · 0°')).toBeVisible();
  expect(screen.getByText('30.0 fps')).toBeVisible();
  expect(screen.getByText(/延迟.*未校准/)).toBeVisible();
  expect(h.canvases.at(-1)?.style.width).toBe('180px');
  expect(h.canvases.at(-1)?.style.height).toBe('400px');
  act(() =>
    h
      .worker()
      .emit({ type: 'metrics', metrics: { ...h.metrics, e2eMsP50: 12.5, droppedWorker: 1 } }),
  );
  expect(screen.getByText(/延迟.*12.5 ms/)).toBeVisible();
  expect(screen.getByText('画面降级：丢帧')).toBeVisible();
});

test('watch and inspect modes do not send device gestures or keys', async () => {
  const h = fixture();
  await h.select();
  expect(screen.getByRole('button', { name: '操作' })).toBeDisabled();
  pointer('pointerdown');
  pointer('pointerup');
  fireEvent.keyDown(screen.getByRole('group', { name: '设备画面交互区' }), { key: 'Escape' });
  await userEvent.click(screen.getByRole('button', { name: '检查' }));
  pointer('pointerdown');
  pointer('pointerup');
  expect(screen.getByText(/检查模式不发送设备动作/)).toBeVisible();
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('acquiring control explains the next step and still requires explicit operation mode', async () => {
  const h = fixture();
  await h.select();
  await userEvent.click(screen.getByRole('button', { name: '取得控制权' }));
  expect(await screen.findByText(/点击上方「操作」后/)).toBeVisible();
  const host = screen.getByRole('group', { name: '设备画面交互区' });
  expect(host).toHaveAttribute('aria-disabled', 'true');
  expect(host).toHaveAttribute('data-mode', 'watch');
  pointer('pointerdown');
  pointer('pointerup');
  expect(h.api.performAction).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '操作' }));
  expect(host).toHaveAttribute('aria-disabled', 'false');
  expect(screen.getByText(/单击画面以点按，按住拖动以滑动/)).toBeVisible();
  expect(screen.getByRole('textbox', { name: '输入到当前焦点字段' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '设备返回' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '设备主屏幕' })).toBeEnabled();
});

test('fullscreen preserves the stream and control lease and cancels pending gestures', async () => {
  const h = fixture();
  await h.operate();
  const root = screen.getByTestId('device-screen');
  const worker = h.worker();
  const canvas = h.canvases.at(-1);
  let fullscreen: Element | null = null;
  const previousElement = Object.getOwnPropertyDescriptor(document, 'fullscreenElement');
  const previousExit = Object.getOwnPropertyDescriptor(document, 'exitFullscreen');
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    get: () => fullscreen,
  });
  Object.defineProperty(root, 'requestFullscreen', {
    configurable: true,
    value: vi.fn(async () => {
      fullscreen = root;
      fireEvent(document, new Event('fullscreenchange'));
    }),
  });
  Object.defineProperty(document, 'exitFullscreen', {
    configurable: true,
    value: vi.fn(async () => {
      fullscreen = null;
      fireEvent(document, new Event('fullscreenchange'));
    }),
  });
  try {
    pointer('pointerdown');
    await userEvent.click(screen.getByRole('button', { name: '放大画面' }));
    expect(screen.getByRole('button', { name: '退出全屏画面' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    pointer('pointerup');
    fireEvent.keyDown(screen.getByRole('group', { name: '设备画面交互区' }), { key: 'Escape' });
    expect(h.api.performAction).not.toHaveBeenCalled();
    expect(h.worker()).toBe(worker);
    expect(h.canvases.at(-1)).toBe(canvas);
    expect(worker.terminated).toBe(false);
    expect(h.api.acquireControl).toHaveBeenCalledOnce();
    expect(h.api.releaseControl).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '释放控制权' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: '退出全屏画面' }));
    expect(screen.getByRole('button', { name: '放大画面' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    expect(h.worker()).toBe(worker);
    expect(screen.getByRole('group', { name: '设备画面交互区' })).toHaveAttribute(
      'data-mode',
      'operate',
    );
  } finally {
    if (previousElement) Object.defineProperty(document, 'fullscreenElement', previousElement);
    else Reflect.deleteProperty(document, 'fullscreenElement');
    if (previousExit) Object.defineProperty(document, 'exitFullscreen', previousExit);
    else Reflect.deleteProperty(document, 'exitFullscreen');
  }
});

test('fullscreen rejection is reported without replacing the stream or acquiring control', async () => {
  const h = fixture();
  await h.select();
  const worker = h.worker();
  Object.defineProperty(screen.getByTestId('device-screen'), 'requestFullscreen', {
    configurable: true,
    value: vi.fn(async () => {
      throw new Error('fullscreen unavailable');
    }),
  });
  await userEvent.click(screen.getByRole('button', { name: '放大画面' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('无法进入或退出全屏');
  expect(h.worker()).toBe(worker);
  expect(worker.terminated).toBe(false);
  expect(h.api.acquireControl).not.toHaveBeenCalled();
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('another manual run remains another controller and explains disabled operations', async () => {
  const h = fixture({ foreign: true });
  await h.select();
  expect(screen.getByText(/控制者：manual · 其他控制会话.*epoch 7/)).toBeVisible();
  expect(screen.getByRole('button', { name: '取得控制权' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '操作' })).toBeDisabled();
  expect(screen.getByText(/当前窗口不能操作/)).toBeVisible();
  pointer('pointerdown');
  pointer('pointerup');
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('normal frame updates stay out of React state and a click binds the current native hit', async () => {
  const h = fixture();
  await h.operate();
  const renders = h.renders.mock.calls.length;
  act(() => {
    for (let seq = 2; seq <= 100; seq++)
      h.worker().emit({ type: 'frame', frame: { ...SCREEN_FRAME, seq, drawnAtMs: 1000 + seq } });
  });
  expect(h.renders.mock.calls.length).toBe(renders);
  pointer('pointerdown');
  pointer('pointerup');
  await waitFor(() => expect(h.api.performAction).toHaveBeenCalledTimes(1));
  expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
    kind: 'tap',
    target: { node_path: [1] },
    params: { x: 540, y: 1200 },
    source: 'manual',
    control_epoch: 7,
  });
});

test.each([
  'black-bar',
  'outside',
  'cancel',
  'lost-capture',
  'blur',
  'layout',
  'config',
  'interruption',
  'multi-touch',
] as const)('does not dispatch a %s gesture', async (reason) => {
  const h = fixture();
  await h.operate();
  pointer('pointerdown', reason === 'black-bar' ? 101 : 300, 250);
  if (reason === 'cancel') pointer('pointercancel');
  if (reason === 'lost-capture') pointer('lostpointercapture');
  if (reason === 'blur') fireEvent(window, new Event('blur'));
  if (reason === 'layout') h.rect.width = 410;
  if (reason === 'config') h.emitFrame({ ...SCREEN_FRAME, configId: 2 });
  if (reason === 'interruption')
    act(() =>
      h
        .worker()
        .emit({ type: 'status', status: { state: 'bootstrapping', sessionId: 'new-capture' } }),
    );
  if (reason === 'multi-touch') pointer('pointerdown', 305, 255, 2);
  pointer('pointerup', reason === 'outside' ? 401 : 300, 250);
  await act(async () => undefined);
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('rotation is CSS-only, uses fixed natural frame metadata and cancels the active stroke', async () => {
  const h = fixture();
  await h.operate();
  pointer('pointerdown');
  h.setGeometry({ display_id: 0, width: 2400, height: 1080, rotation: 90 });
  await h.poll();
  expect(screen.getByRole('group', { name: '设备画面交互区' })).toHaveAttribute(
    'data-rotation',
    '270',
  );
  expect(h.canvases.at(-1)?.style.transform).toContain('rotate(270deg)');
  expect(h.canvases.at(-1)?.style.width).toBe('180px');
  expect(h.canvases.at(-1)?.style.height).toBe('400px');
  pointer('pointerup');
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('content fitting excludes the border and border clicks never reach device pixels', async () => {
  const h = fixture();
  await h.operate();
  const host = screen.getByRole('group', { name: '设备画面交互区' });
  host.style.border = '1px solid black';
  h.emitFrame({ ...SCREEN_FRAME, configId: 2 });
  expect(h.canvases.at(-1)?.style.height).toBe('398px');
  pointer('pointerdown', 300, 50);
  pointer('pointerup', 300, 50);
  expect(h.api.performAction).not.toHaveBeenCalled();
  pointer('pointerdown');
  pointer('pointerup');
  await waitFor(() => expect(h.api.performAction).toHaveBeenCalledTimes(1));
  expect(h.api.performAction.mock.calls[0]?.[1].params).toMatchObject({ x: 540, y: 1200 });
});

test('does not dispatch while the canvas projection belongs to an old frame config', async () => {
  const h = fixture();
  await h.operate();
  const canvas = h.canvases.at(-1);
  if (!canvas) throw new Error('missing canvas');
  canvas.dataset['config'] = '0';
  pointer('pointerdown');
  pointer('pointerup');
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('focused screen keys use only the closed key map and preserve global shortcuts and IME', async () => {
  const h = fixture();
  await h.operate();
  const host = screen.getByRole('group', { name: '设备画面交互区' });
  fireEvent.keyDown(host, { key: 'Escape' });
  await waitFor(() => expect(h.api.performAction).toHaveBeenCalledTimes(1));
  expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
    kind: 'key',
    params: { code: 4 },
  });
  await screen.findByText('设备动作已确认；未评估业务结果。');
  fireEvent.keyDown(host, { key: 'Home', ctrlKey: true });
  fireEvent.keyDown(host, { key: 'Enter', repeat: true });
  fireEvent.keyDown(host, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(window, { key: 'Home' });
  fireEvent.keyDown(host, { key: 'a' });
  expect(h.api.performAction).toHaveBeenCalledTimes(1);
});

test('text stays as a local draft until the exact set_text receipt arrives', async () => {
  const h = fixture();
  await h.operate();
  let finish: (body: unknown) => void = () => undefined;
  let submitted: ScreenActionBody | null = null;
  h.api.performAction.mockImplementation((_serial, body) => {
    submitted = body;
    return new Promise((resolve) => {
      finish = resolve;
    });
  });
  const input = screen.getByRole('textbox', { name: '输入到当前焦点字段' });
  fireEvent.change(input, { target: { value: '中文 pasted text' } });
  fireEvent.submit(screen.getByRole('form', { name: '设备键盘输入' }));
  await waitFor(() => expect(h.api.performAction).toHaveBeenCalledTimes(1));
  expect(input).toHaveValue('中文 pasted text');
  expect(input).toBeDisabled();
  expect(submitted).toMatchObject({
    kind: 'set_text',
    target: { node_path: [0] },
    params: { text: '中文 pasted text' },
  });
  if (!submitted) throw new Error('missing body');
  await act(async () => finish(simulatedActionReceipt(submitted as ScreenActionBody)));
  expect(input).toHaveValue('');
});

test('an uncertain text result preserves the draft and does not retry it', async () => {
  const h = fixture();
  await h.operate();
  h.api.performAction.mockImplementation(async (_serial, body) =>
    simulatedActionReceipt(body, 'uncertain'),
  );
  const input = screen.getByRole('textbox', { name: '输入到当前焦点字段' });
  fireEvent.change(input, { target: { value: 'keep this draft' } });
  fireEvent.submit(screen.getByRole('form', { name: '设备键盘输入' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('结果不确定');
  expect(input).toHaveValue('keep this draft');
  expect(h.api.performAction).toHaveBeenCalledTimes(1);
});

test('IME composition does not submit provisional text', async () => {
  const h = fixture();
  await h.operate();
  const input = screen.getByRole('textbox', { name: '输入到当前焦点字段' });
  fireEvent.compositionStart(input);
  fireEvent.change(input, { target: { value: '中文输入中' } });
  fireEvent.submit(screen.getByRole('form', { name: '设备键盘输入' }));
  expect(h.api.performAction).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.submit(screen.getByRole('form', { name: '设备键盘输入' }));
  await waitFor(() => expect(h.api.performAction).toHaveBeenCalledTimes(1));
  expect(h.api.performAction.mock.calls[0]?.[1].params).toEqual({ text: '中文输入中' });
});

test('unsupported WebCodecs is explicit and creates no canvas or Worker', async () => {
  const h = fixture({ supported: false });
  await h.select();
  expect(screen.getByText(/当前环境不支持 Worker/)).toBeVisible();
  expect(h.workers).toHaveLength(0);
  expect(h.canvases).toHaveLength(0);
  expect(screen.getByRole('button', { name: '操作' })).toBeDisabled();
});

test('pagehide submits own keepalive cleanup even while the video subscription is paused', async () => {
  const h = fixture();
  await h.operate();
  const runId = h.api.acquireControl.mock.calls[0]?.[1].run_id;
  await userEvent.click(screen.getByRole('button', { name: '暂停画面' }));
  const reads = h.api.getControl.mock.calls.length;
  fireEvent(window, new Event('pagehide'));
  expect(h.api.releaseControl).toHaveBeenCalledWith(SCREEN_SERIAL, {
    session_id: SCREEN_SESSION,
    control_epoch: 7,
    run_id: runId,
    wait_for_idle_ms: 60_000,
  });
  expect(h.api.getControl).toHaveBeenCalledTimes(reads);
  expect(h.api.performAction).not.toHaveBeenCalled();
});

test('pause and unmount terminate only this view, use fresh canvases, and never disconnect the Agent', async () => {
  const h = fixture({ strict: true });
  await h.select();
  const first = h.worker();
  await userEvent.click(screen.getByRole('button', { name: '暂停画面' }));
  expect(first.terminated).toBe(true);
  await userEvent.click(screen.getByRole('button', { name: '继续画面' }));
  await waitFor(() => expect(h.worker()).not.toBe(first));
  expect(new Set(h.canvases).size).toBe(h.canvases.length);
  h.unmount();
  expect(h.workers.every((worker) => worker.terminated)).toBe(true);
  expect(h.api.performAction).not.toHaveBeenCalled();
  expect(h.api.acquireControl).not.toHaveBeenCalled();
});

test.each([0, 90, 180, 270] as Rotation[])(
  'inspect hover/click maps native bounds at rotation %s without acquiring or performing actions',
  async (rotation) => {
    const h = fixture({ inspection: true });
    const sideways = rotation === 90 || rotation === 270;
    const geometry = {
      ...SCREEN_GEOMETRY,
      rotation,
      width: sideways ? 2400 : 1080,
      height: sideways ? 1080 : 2400,
    };
    h.setGeometry(geometry);
    h.api.observeAgent.mockImplementation(async (serial, body) => ({
      ...simulatedInspectorObservation(body.request_id, geometry),
      serial,
    }));
    await h.select();
    await userEvent.click(screen.getByRole('button', { name: '检查' }));
    const inspector = h.inspector;
    if (!inspector) throw new Error('missing inspector');
    await waitFor(() => expect(inspector.getSnapshot().status).toBe('ready'));
    const node = inspector.getSnapshot().snapshot?.byId.get('native:1');
    if (!node?.bounds) throw new Error('missing native bounds');
    const point = displayToClient(
      {
        x: (Math.max(0, node.bounds.left) + Math.min(geometry.width, node.bounds.right)) / 2,
        y: (Math.max(0, node.bounds.top) + Math.min(geometry.height, node.bounds.bottom)) / 2,
      },
      h.rect,
      {
        frameWidth: sideways ? SCREEN_FRAME.height : SCREEN_FRAME.width,
        frameHeight: sideways ? SCREEN_FRAME.width : SCREEN_FRAME.height,
        displayWidth: geometry.width,
        displayHeight: geometry.height,
        rotation: 0,
      },
    );
    if (!point) throw new Error('missing client point');
    const host = screen.getByRole('group', { name: '设备画面交互区' });
    fireEvent.pointerMove(host, { clientX: point.x, clientY: point.y });
    expect(await screen.findByTestId('inspection-hover')).toHaveTextContent('提交订单');
    pointer('pointerdown', point.x, point.y);
    pointer('pointerup', point.x, point.y);
    expect(inspector.getSnapshot().selectedId).toBe('native:1');
    expect(screen.getByTestId('inspection-selection')).toHaveTextContent('提交订单');
    expect(h.api.acquireControl).not.toHaveBeenCalled();
    expect(h.api.performAction).not.toHaveBeenCalled();
    expect(h.api.observeAgent).toHaveBeenCalledWith(
      SCREEN_SERIAL,
      expect.objectContaining({ for_action: false, screenshot: false }),
    );
  },
);

test('inspect rejects black bars, and a config change removes stale highlights and disables hit selection', async () => {
  const h = fixture({ inspection: true });
  h.api.observeAgent.mockImplementation(async (serial, body) => ({
    ...simulatedInspectorObservation(body.request_id),
    serial,
  }));
  await h.select();
  await userEvent.click(screen.getByRole('button', { name: '检查' }));
  const inspector = h.inspector;
  if (!inspector) throw new Error('missing inspector');
  await waitFor(() => expect(inspector.getSnapshot().status).toBe('ready'));
  const host = screen.getByRole('group', { name: '设备画面交互区' });
  pointer('pointerdown', h.rect.left + 1, h.rect.top + h.rect.height / 2);
  expect(inspector.getSnapshot().selectedId).toBeNull();
  fireEvent.pointerMove(host, { clientX: 300, clientY: 250 });
  expect(await screen.findByTestId('inspection-hover')).toBeVisible();
  h.emitFrame({ ...SCREEN_FRAME, configId: 2 });
  expect(inspector.getSnapshot().expired).toBe(true);
  expect(screen.queryByTestId('inspection-hover')).not.toBeInTheDocument();
  pointer('pointerdown', 300, 250);
  expect(inspector.getSnapshot().selectedId).toBeNull();
  expect(h.api.performAction).not.toHaveBeenCalled();
});
