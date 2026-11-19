import { afterEach, expect, test, vi } from 'vitest';
import type { RuntimeConnection } from '../../../shared/ipc';
import { RuntimeApiError } from '../../shared/api/runtime.generated';
import { simulatedDevice, simulatedTargetApp } from './deviceFixtures';
import { createDeviceStore, DEVICE_REFRESH_MS } from './deviceStore';

const connection: RuntimeConnection = {
  port: 4311,
  token: 'simulated-runtime-token',
  pid: 9,
  version: '1.0.0',
};

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve: (value: T) => resolve(value) };
}

function harness(devices = [simulatedDevice()]) {
  const api = {
    listDevices: vi.fn(async (): Promise<unknown> => ({ devices })),
    getDevice: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    getAgentHealth: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    connectAgent: vi.fn(async (serial: string): Promise<unknown> => simulatedDevice(serial)),
    getTargetApp: vi.fn(async (serial: string): Promise<unknown> => simulatedTargetApp(serial)),
  };
  const signals: AbortSignal[] = [];
  const deps = {
    getConnection: vi.fn(async (): Promise<RuntimeConnection | null> => connection),
    clientFor: vi.fn((_connection: RuntimeConnection, signal: AbortSignal) => {
      signals.push(signal);
      return api;
    }),
    setTimer: vi.fn((callback: () => void, ms: number) => ({ callback, ms })),
    clearTimer: vi.fn(),
  };
  return { api, deps, signals, store: createDeviceStore(deps) };
}

afterEach(() => vi.useRealTimers());

test('browser preview explains the missing desktop bridge without requesting the Runtime', async () => {
  vi.stubGlobal('pulse', undefined);
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  try {
    const store = createDeviceStore();
    await store.refresh();
    expect(store.getSnapshot()).toMatchObject({ status: 'unavailable', devices: [] });
    expect(store.getSnapshot().error).toContain('浏览器预览');
    expect(store.getSnapshot().error).toContain('Electron 桌面桥接');
    expect(store.getSnapshot().error).toContain('pnpm dev');
    expect(store.getSnapshot().error).not.toContain('未运行或未连接');
    expect(fetch).not.toHaveBeenCalled();
  } finally {
    vi.unstubAllGlobals();
  }
});

test('a desktop bridge with no connection remains a Runtime availability error', async () => {
  vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(async () => null) } });
  try {
    const store = createDeviceStore();
    await store.refresh();
    expect(store.getSnapshot().status).toBe('unavailable');
    expect(store.getSnapshot().error).toContain('Runtime 未运行或未连接');
    expect(store.getSnapshot().error).not.toContain('浏览器预览');
  } finally {
    vi.unstubAllGlobals();
  }
});

test('discovery only reads snapshots and never implicitly selects, connects or checks a device', async () => {
  const h = harness();
  await h.store.refresh();
  expect(h.store.getSnapshot().status).toBe('ready');
  expect(h.store.getSnapshot().selectedSerial).toBeNull();
  expect(h.api.connectAgent).not.toHaveBeenCalled();
  expect(h.api.getAgentHealth).not.toHaveBeenCalled();
  expect(h.api.getTargetApp).not.toHaveBeenCalled();
  expect(h.api.getDevice).not.toHaveBeenCalled();
  expect(JSON.stringify(h.store.getSnapshot())).not.toContain(connection.token);
});

test('successful empty discovery differs from unavailable Runtime and HTTP-200 ADB failure', async () => {
  const h = harness([]);
  await h.store.refresh();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', devices: [], error: null });
  h.api.listDevices.mockResolvedValue({ devices: [], error: 'adb_unavailable' });
  await h.store.refresh();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', devices: [] });
  expect(h.store.getSnapshot().error).toContain('adb_unavailable');
  h.deps.getConnection.mockResolvedValue(null);
  await h.store.refresh();
  expect(h.store.getSnapshot().status).toBe('unavailable');
});

test('failed refresh preserves the last observation as historical, not a fresh ready state', async () => {
  const h = harness();
  await h.store.refresh();
  const previous = h.store.getSnapshot().devices[0];
  h.api.listDevices.mockRejectedValue(new Error('sensitive internal output'));
  await h.store.refresh();
  expect(h.store.getSnapshot().status).toBe('error');
  expect(h.store.getSnapshot().devices[0]).toEqual(previous);
  expect(h.store.getSnapshot().error).not.toContain('sensitive');
});

test('polling is sequential, bounded to active subscribers and cleans up on final release', async () => {
  const h = harness();
  const release = h.store.start();
  const releaseSecond = h.store.start();
  await h.store.refresh();
  expect(h.api.listDevices).toHaveBeenCalledOnce();
  const timer = h.deps.setTimer.mock.results.at(-1)?.value;
  expect(timer?.ms).toBe(DEVICE_REFRESH_MS);
  timer?.callback();
  await h.store.refresh();
  expect(h.api.listDevices).toHaveBeenCalledTimes(2);
  release();
  expect(h.store.getSnapshot().status).toBe('ready');
  releaseSecond();
  releaseSecond();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'idle', devices: [], busy: null });
  expect(h.deps.clearTimer).toHaveBeenCalled();
  expect(h.signals.every((signal) => signal.aborted)).toBe(true);
});

test('a stopped or StrictMode-replaced discovery cannot overwrite the new mount', async () => {
  const h = harness();
  const late = deferred<unknown>();
  h.api.listDevices
    .mockReturnValueOnce(late.promise)
    .mockResolvedValueOnce({ devices: [simulatedDevice('SIM-NEW')] });
  const release = h.store.start();
  const old = h.store.refresh();
  await vi.waitFor(() => expect(h.api.listDevices).toHaveBeenCalledOnce());
  release();
  const releaseNew = h.store.start();
  await h.store.refresh();
  late.resolve({ devices: [simulatedDevice('SIM-OLD')] });
  await old;
  expect(h.store.getSnapshot().devices.map((device) => device.serial)).toEqual(['SIM-NEW']);
  releaseNew();
});

test('health uses the exact selected Runtime session and no request falls back to a default serial', async () => {
  const h = harness([simulatedDevice('SIM-A'), simulatedDevice('SIM-B')]);
  await h.store.refresh();
  await h.store.inspectSelected();
  expect(h.api.getAgentHealth).not.toHaveBeenCalled();
  h.store.selectDevice('SIM-B');
  await h.store.inspectSelected();
  expect(h.api.getAgentHealth).toHaveBeenCalledWith('SIM-B', {
    session_id: '00000000-0000-4000-8000-000000000001',
  });
  expect(h.api.getDevice).not.toHaveBeenCalled();
});

test('a late health result from a previous selection cannot pollute the newly selected device', async () => {
  const h = harness([simulatedDevice('SIM-A'), simulatedDevice('SIM-B')]);
  await h.store.refresh();
  h.store.selectDevice('SIM-A');
  const late = deferred<unknown>();
  h.api.getAgentHealth.mockReturnValueOnce(late.promise);
  const work = h.store.inspectSelected();
  await vi.waitFor(() => expect(h.api.getAgentHealth).toHaveBeenCalledOnce());
  h.store.selectDevice('SIM-B');
  late.resolve(simulatedDevice('SIM-A', { status: 'permission_revoked', ready: false }));
  await work;
  expect(h.store.getSnapshot().selectedSerial).toBe('SIM-B');
  expect(h.store.getSnapshot().devices.every((device) => device.status === 'ready')).toBe(true);
});

test('explicit connect does not optimistically claim readiness and sends only safe connect options', async () => {
  const h = harness([
    simulatedDevice('SIM', {
      status: 'agent_available',
      ready: false,
      connection: {},
      capabilities: null,
    }),
  ]);
  await h.store.refresh();
  h.store.selectDevice('SIM');
  const late = deferred<unknown>();
  h.api.connectAgent.mockReturnValueOnce(late.promise);
  const work = h.store.connectSelected();
  await vi.waitFor(() => expect(h.api.connectAgent).toHaveBeenCalledOnce());
  expect(h.api.connectAgent).toHaveBeenCalledWith('SIM', { timeout_ms: 8000 });
  expect(h.store.getSnapshot().devices[0]?.ready).toBe(false);
  expect(h.store.getSnapshot().busy).toBe('connect');
  late.resolve(simulatedDevice('SIM'));
  await work;
  expect(h.store.getSnapshot().devices[0]?.ready).toBe(true);
});

test.each([
  { state: 'offline', connection: {} },
  { control: { recovery_required: true }, connection: {} },
  { diagnostic: { cleanup_pending: true }, connection: {} },
  {},
])(
  'connect cannot bypass an offline, recovery, cleanup or existing-session guard: %j',
  async (overrides) => {
    const h = harness([simulatedDevice('SIM', overrides)]);
    await h.store.refresh();
    h.store.selectDevice('SIM');
    await h.store.connectSelected();
    expect(h.api.connectAgent).not.toHaveBeenCalled();
  },
);

test('HTTP-200 connect operation_error is not reported as a successful connect', async () => {
  const h = harness([
    simulatedDevice('SIM', { status: 'agent_available', ready: false, connection: {} }),
  ]);
  await h.store.refresh();
  h.store.selectDevice('SIM');
  h.api.connectAgent.mockResolvedValue(
    simulatedDevice('SIM', {
      operation_error: 'operation_in_progress',
      status: 'connecting',
      ready: false,
    }),
  );
  await h.store.connectSelected();
  expect(h.store.getSnapshot().devices[0]?.ready).toBe(false);
  expect(h.store.getSnapshot().actionError).toContain('未执行接入');
});

test('stale health errors invalidate current-health presentation without exposing error bodies', async () => {
  const h = harness();
  await h.store.refresh();
  h.store.selectDevice('SIM-ANDROID-A');
  h.api.getAgentHealth.mockRejectedValue(
    new RuntimeApiError(409, { error: 'stale_session', token: 'secret' }),
  );
  await h.store.inspectSelected();
  expect(h.store.getSnapshot().status).toBe('error');
  expect(h.store.getSnapshot().actionError).toContain('409');
  expect(h.store.getSnapshot().actionError).not.toContain('secret');
});

test('a changed Runtime port/token cannot receive an action bound to the old snapshot', async () => {
  const h = harness();
  await h.store.refresh();
  h.store.selectDevice('SIM-ANDROID-A');
  h.deps.getConnection.mockResolvedValue({
    ...connection,
    port: 4312,
    token: 'new-token',
    pid: 10,
  });
  await h.store.inspectSelected();
  expect(h.api.getAgentHealth).not.toHaveBeenCalled();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', devices: [], targetCheck: null });
  await h.store.refresh();
  expect(h.store.getSnapshot().status).toBe('ready');
});

test('an old list response is discarded when Runtime changes while discovery is in flight', async () => {
  const h = harness();
  const late = deferred<unknown>();
  h.api.listDevices.mockReturnValueOnce(late.promise);
  const work = h.store.refresh();
  await vi.waitFor(() => expect(h.api.listDevices).toHaveBeenCalledOnce());
  h.deps.getConnection.mockResolvedValue({ ...connection, token: 'new-token', pid: 10 });
  late.resolve({ devices: [simulatedDevice('OLD')] });
  await work;
  expect(h.store.getSnapshot()).toMatchObject({ status: 'unavailable', devices: [] });
  expect(h.deps.getConnection).toHaveBeenCalledTimes(2);
});

test('switching Runtime makes a pending replacement list loading, never a confirmed empty result', async () => {
  const h = harness();
  await h.store.refresh();
  const late = deferred<unknown>();
  h.api.listDevices.mockReturnValueOnce(late.promise);
  h.deps.getConnection.mockResolvedValue({ ...connection, token: 'new-token', pid: 10 });
  const work = h.store.refresh();
  await vi.waitFor(() => expect(h.api.listDevices).toHaveBeenCalledTimes(2));
  expect(h.store.getSnapshot()).toMatchObject({ status: 'loading', devices: [], refreshing: true });
  late.resolve({ devices: [] });
  await work;
  expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', devices: [], refreshing: false });
});

test.each(['health', 'connect', 'target'] as const)(
  'in-flight %s responses cannot outlive their Runtime identity',
  async (kind) => {
    const h = harness([
      kind === 'connect'
        ? simulatedDevice('SIM', { status: 'agent_available', ready: false, connection: {} })
        : simulatedDevice('SIM'),
    ]);
    await h.store.refresh();
    h.store.selectDevice('SIM');
    const late = deferred<unknown>();
    const api =
      kind === 'health'
        ? h.api.getAgentHealth
        : kind === 'connect'
          ? h.api.connectAgent
          : h.api.getTargetApp;
    api.mockReturnValueOnce(late.promise);
    const work =
      kind === 'health'
        ? h.store.inspectSelected()
        : kind === 'connect'
          ? h.store.connectSelected()
          : h.store.checkTargetApp();
    await vi.waitFor(() => expect(api).toHaveBeenCalledOnce());
    h.deps.getConnection.mockResolvedValue({
      ...connection,
      port: 4312,
      token: 'new-token',
      pid: 10,
    });
    late.resolve(
      kind === 'target'
        ? simulatedTargetApp('SIM')
        : simulatedDevice('SIM', { observed_at_ms: 999 }),
    );
    await work;
    expect(h.store.getSnapshot().status).toBe('error');
    expect(h.store.getSnapshot().targetCheck).toBeNull();
    expect(h.store.getSnapshot().devices[0]?.observed_at_ms).not.toBe(999);
    expect(h.store.getSnapshot().actionError).toContain('旧响应已丢弃');
  },
);

test('Runtime disappearance during a target check invalidates health, not only the target result', async () => {
  const h = harness();
  await h.store.refresh();
  h.store.selectDevice('SIM-ANDROID-A');
  h.deps.getConnection.mockResolvedValue(null);
  await h.store.checkTargetApp();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', targetCheck: null });
  expect(h.api.getTargetApp).not.toHaveBeenCalled();
  expect(h.store.getSnapshot().actionError).toContain('Runtime 未运行或未连接');
});

test('target installation checks work independently of Agent ready and retire on session changes', async () => {
  const h = harness([
    simulatedDevice('SIM', {
      status: 'agent_missing',
      ready: false,
      connection: {},
      capabilities: null,
    }),
  ]);
  await h.store.refresh();
  h.store.selectDevice('SIM');
  await h.store.checkTargetApp();
  expect(h.api.getTargetApp).toHaveBeenCalledWith('SIM');
  expect(h.store.getSnapshot().targetCheck?.status).toBe('installed');
  expect(h.store.getSnapshot().devices[0]?.ready).toBe(false);
  expect(h.api.connectAgent).not.toHaveBeenCalled();
  h.api.listDevices.mockResolvedValue({ devices: [simulatedDevice('SIM')] });
  await h.store.refresh();
  expect(h.store.getSnapshot().targetCheck).toBeNull();
});

test('target results and missing devices never automatically retarget to another serial', async () => {
  const h = harness([simulatedDevice('SIM-A'), simulatedDevice('SIM-B')]);
  await h.store.refresh();
  h.store.selectDevice('SIM-A');
  const late = deferred<unknown>();
  h.api.getTargetApp.mockReturnValueOnce(late.promise);
  const work = h.store.checkTargetApp();
  await vi.waitFor(() => expect(h.api.getTargetApp).toHaveBeenCalledOnce());
  h.store.selectDevice('SIM-B');
  late.resolve(simulatedTargetApp('SIM-A'));
  await work;
  expect(h.store.getSnapshot().targetCheck).toBeNull();
  h.api.listDevices.mockResolvedValue({ devices: [simulatedDevice('SIM-A')] });
  await h.store.refresh();
  expect(h.store.getSnapshot().selectedSerial).toBeNull();
});

test('an unresponsive request times out, aborts its fetch and cannot later refresh health', async () => {
  vi.useFakeTimers();
  const h = harness();
  await h.store.refresh();
  h.store.selectDevice('SIM-ANDROID-A');
  const late = deferred<unknown>();
  h.api.getAgentHealth.mockReturnValueOnce(late.promise);
  const work = h.store.inspectSelected();
  await vi.waitFor(() => expect(h.api.getAgentHealth).toHaveBeenCalledOnce());
  await vi.advanceTimersByTimeAsync(12_000);
  await work;
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', busy: null });
  expect(h.signals.at(-1)?.aborted).toBe(true);
  late.resolve(simulatedDevice('SIM-ANDROID-A', { status: 'permission_revoked' }));
  await Promise.resolve();
  expect(h.store.getSnapshot().devices[0]?.status).toBe('ready');
});
