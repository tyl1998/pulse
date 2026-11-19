import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { FromWorker, ToWorker } from '../../shared/stream/workerProtocol';
import { StreamDebugPanel } from './StreamDebugPanel';

const workers: PanelWorker[] = [];
class PanelWorker {
  onmessage: ((event: MessageEvent<unknown>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  onmessageerror: ((event: MessageEvent<unknown>) => void) | null = null;
  sent: ToWorker[] = [];
  connectionId = 0;
  autoArm = false;
  terminated = false;
  constructor() {
    workers.push(this);
  }
  postMessage(msg: ToWorker) {
    this.sent.push(msg);
    if (msg.type === 'connect') this.connectionId = msg.connectionId;
    if (msg.type === 'arm' && this.autoArm) this.emit({ type: 'armed', id: msg.id, ready: true });
  }
  emit(msg: FromWorker) {
    this.onmessage?.({
      data: { ...msg, connectionId: this.connectionId },
    } as MessageEvent<unknown>);
  }
  terminate() {
    this.terminated = true;
  }
}

const requests: { url: string; init: RequestInit }[] = [];
const response = (body: unknown) =>
  ({ ok: true, status: 200, json: () => Promise.resolve(body) }) as Response;
const fetchMock = vi.fn<typeof fetch>();
const originalTransfer = Object.getOwnPropertyDescriptor(
  HTMLCanvasElement.prototype,
  'transferControlToOffscreen',
);
const transfer = vi.fn(() => ({}) as OffscreenCanvas);
const bench = () => {
  const api = window.__pulseBench;
  if (!api) throw new Error('bench API missing');
  return api;
};
const flush = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });

beforeEach(() => {
  vi.useFakeTimers();
  workers.length = 0;
  requests.length = 0;
  transfer.mockClear();
  Object.defineProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen', {
    configurable: true,
    value: transfer,
  });
  vi.stubGlobal('Worker', PanelWorker);
  vi.stubGlobal('VideoDecoder', vi.fn());
  vi.stubGlobal('pulse', {
    app: { getInfo: () => Promise.resolve({ version: '0', platform: 'test' }) },
    runtime: {
      getConnection: () => Promise.resolve({ port: 5000, pid: 1, token: 'test', version: '0' }),
    },
  });
  fetchMock.mockReset().mockImplementation((input, init = {}) => {
    const url = String(input);
    requests.push({ url, init });
    if (url.endsWith('/devices'))
      return Promise.resolve(response({ devices: [{ serial: 'fake', state: 'device' }] }));
    if (url.endsWith('/keepalive')) return Promise.resolve(response({ renewed: true }));
    return Promise.resolve(
      response(
        init.method === 'POST' ? { started: true, publisher_id: 'owned', lease_ms: 20000 } : {},
      ),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(async () => {
  cleanup();
  await vi.advanceTimersByTimeAsync(0);
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalTransfer)
    Object.defineProperty(
      HTMLCanvasElement.prototype,
      'transferControlToOffscreen',
      originalTransfer,
    );
  else Reflect.deleteProperty(HTMLCanvasElement.prototype, 'transferControlToOffscreen');
});

test('running controls are disabled, stop clears status/metrics and deletes only the owned lease', async () => {
  const getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext');
  render(<StreamDebugPanel />);
  await flush();
  await act(async () => {
    await bench().start({ serial: 'fake', mode: 'relay' });
  });
  expect(screen.getByLabelText('设备').matches(':disabled')).toBe(true);
  expect(screen.getByLabelText('relay 地址').matches(':disabled')).toBe(true);
  expect(screen.getByText('PTS到draw提交估算 p50 ms')).toBeTruthy();
  await act(async () => {
    await bench().stop();
  });
  expect(screen.getByLabelText('设备').matches(':disabled')).toBe(false);
  expect(bench().metrics()).toBeNull();
  expect(bench().status()).toEqual({ state: 'stopped' });
  expect(requests.filter((r) => r.init.method === 'DELETE')).toMatchObject([
    {
      url: 'http://127.0.0.1:5000/debug/stream/fake/relay?publisher_id=owned',
      init: { keepalive: true },
    },
  ]);
  expect(getContext).not.toHaveBeenCalled();
  expect(requests.every((r) => !r.url.includes('shutdown'))).toBe(true);
});

test('pagehide releases the relay lease and cancels renewals without killing Runtime', async () => {
  const panel = render(<StreamDebugPanel />);
  await flush();
  await act(async () => {
    await bench().start({ serial: 'fake', mode: 'relay' });
  });
  await act(async () => {
    window.dispatchEvent(new Event('pagehide'));
    await vi.advanceTimersByTimeAsync(0);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  expect(requests.filter((r) => r.init.method === 'DELETE')).toHaveLength(1);
  expect(requests.filter((r) => r.url.endsWith('/keepalive'))).toHaveLength(0);
  panel.unmount();
  await flush();
  expect(workers.every((w) => w.terminated)).toBe(true);
  expect(window.__pulseBench).toBeUndefined();
  expect(screen.queryByTestId('stream-canvas')).toBeNull();
});

test('relay interruptions are visible and decoder recovery clears a stale error alert', async () => {
  render(<StreamDebugPanel />);
  await flush();
  await act(async () => {
    await bench().start({ serial: 'fake', mode: 'relay' });
  });
  await flush();
  const worker = workers.at(-1);
  if (!worker) throw new Error('worker missing');
  act(() => {
    worker.emit({ type: 'error', message: 'decoder failed' });
    worker.emit({ type: 'status', status: { state: 'relay', relay: 'disconnected' } });
  });
  expect(screen.getByRole('alert').textContent).toBe('decoder failed');
  expect(screen.getByTestId('stream-status').textContent).toContain('relay 断流');
  act(() => {
    worker.emit({ type: 'status', status: { state: 'bootstrapping' } });
    worker.emit({ type: 'status', status: { state: 'streaming' } });
  });
  expect(screen.queryByRole('alert')).toBeNull();
  expect(bench().status()).toEqual({ state: 'streaming' });
});

test('a lease POST completing after unmount is cleaned without connecting the worker', async () => {
  let resolvePost: (response: Response) => void = () => undefined;
  const pending = new Promise<Response>((resolve) => {
    resolvePost = resolve;
  });
  const panel = render(<StreamDebugPanel />);
  await flush();
  fetchMock.mockImplementationOnce((input, init = {}) => {
    requests.push({ url: String(input), init });
    return pending;
  });
  const api = bench();
  let start = Promise.resolve();
  await act(async () => {
    start = api.start({ serial: 'late', mode: 'relay' });
    await vi.advanceTimersByTimeAsync(0);
  });
  panel.unmount();
  resolvePost(response({ started: true, publisher_id: 'late-id', lease_ms: 20000 }));
  await start;
  await flush();
  expect(
    requests.some(
      (r) => r.url.endsWith('/relay?publisher_id=late-id') && r.init.method === 'DELETE',
    ),
  ).toBe(true);
  expect(workers.flatMap((w) => w.sent).some((msg) => msg.type === 'connect')).toBe(false);
});

test('StrictMode owns a fresh canvas per mount and unmount releases the active lease', async () => {
  const panel = render(
    <StrictMode>
      <StreamDebugPanel />
    </StrictMode>,
  );
  await flush();
  expect(transfer).toHaveBeenCalledTimes(2);
  expect(new Set(transfer.mock.contexts).size).toBe(2);
  expect(workers[0]?.terminated).toBe(true);
  await act(async () => {
    await bench().start({ serial: 'fake', mode: 'relay' });
  });
  panel.unmount();
  await flush();
  expect(requests.filter((r) => r.init.method === 'DELETE')).toHaveLength(1);
  expect(workers.every((w) => w.terminated)).toBe(true);
});

test('public stop aborts an in-flight changeBench and prevents every remaining tap', async () => {
  render(<StreamDebugPanel />);
  await flush();
  await act(async () => {
    await bench().start({ serial: 'active', mode: 'direct' });
  });
  await flush();
  const worker = workers.at(-1);
  if (!worker) throw new Error('worker missing');
  worker.autoArm = true;
  act(() => {
    worker.emit({ type: 'status', status: { state: 'streaming' } });
  });
  fetchMock.mockImplementationOnce((input, init = {}) => {
    requests.push({ url: String(input), init });
    return new Promise(() => undefined);
  });
  const result = bench().changeBench({
    n: 300,
    roi: { x: 0, y: 0, w: 1, h: 1 },
    threshold: 5,
    settleMs: 0,
    timeoutMs: 5000,
    tap: { x: 1, y: 1 },
  });
  await flush();
  const triggers = requests.filter((r) => r.url.endsWith('/trigger'));
  expect(triggers).toHaveLength(1);
  expect(triggers[0]?.url).toContain('/active/trigger');
  await act(async () => {
    await bench().stop();
  });
  await expect(result).resolves.toMatchObject({
    trials: [],
    attempts: 1,
    misses: 0,
    aborted: true,
  });
  expect(triggers[0]?.init.signal?.aborted).toBe(true);
  await flush();
  expect(requests.filter((r) => r.url.endsWith('/trigger'))).toHaveLength(1);
});

test('shared controls preserve editing, native selects, start/stop actions and fieldset disabling', async () => {
  render(<StreamDebugPanel />);
  await flush();
  fireEvent.change(screen.getByLabelText('路径'), { target: { value: 'relay' } });
  fireEvent.change(screen.getByLabelText('长边上限'), { target: { value: '1280' } });
  fireEvent.change(screen.getByLabelText('relay 地址'), {
    target: { value: 'ws://127.0.0.1:9999' },
  });
  fireEvent.change(screen.getByLabelText('relay token'), { target: { value: 'mock-only' } });
  expect(screen.getByLabelText('长边上限')).toHaveValue('1280');
  expect(screen.getByLabelText('relay 地址')).toHaveValue('ws://127.0.0.1:9999');
  expect(screen.getByLabelText('relay token')).toHaveValue('mock-only');
  expect(screen.getByLabelText('设备').tagName).toBe('SELECT');
  expect(screen.getByLabelText('relay 地址')).toHaveClass('ui-input');
  fireEvent.click(screen.getByRole('button', { name: '开始' }));
  await flush();
  expect(screen.getByLabelText('relay 地址').matches(':disabled')).toBe(true);
  expect(screen.getByRole('button', { name: '停止' })).toHaveClass('ui-button');
  fireEvent.click(screen.getByRole('button', { name: '停止' }));
  await flush();
  expect(screen.getByLabelText('设备').matches(':disabled')).toBe(false);
  expect(screen.getByRole('button', { name: '开始' })).toBeEnabled();
  expect(requests.filter((request) => request.init.method === 'DELETE')).toHaveLength(1);
});

test('component styles do not replace the owned canvas or alter its sizing contract', async () => {
  render(<StreamDebugPanel />);
  await flush();
  const canvas = screen.getByTestId('stream-canvas');
  expect(canvas.style.maxHeight).toBe('80vh');
  expect(canvas.style.maxWidth).toBe('50vw');
  expect(canvas.style.transform).toBe('');
  expect(canvas.className).toBe('');
  expect(transfer).toHaveBeenCalledOnce();
});

test('unsupported environments still disable start and explain the missing video capability', async () => {
  vi.stubGlobal('VideoDecoder', undefined);
  render(<StreamDebugPanel />);
  await flush();
  expect(screen.getByRole('button', { name: '开始' })).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('当前环境不支持 Worker / WebCodecs');
  expect(screen.queryByTestId('stream-canvas')).not.toBeInTheDocument();
  expect(workers).toHaveLength(0);
});
