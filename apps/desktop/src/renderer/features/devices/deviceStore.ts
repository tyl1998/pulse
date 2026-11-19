import type { RuntimeConnection } from '../../../shared/ipc';
import { RuntimeApiError } from '../../shared/api/runtime.generated';
import { createRuntimeClient } from '../../shared/api/runtimeClient';
import { withTimeout } from '../../shared/stream/timeout';
import {
  parseDeviceList,
  parseDeviceSnapshot,
  parseTargetAppCheck,
  type DeviceSnapshot,
  type TargetAppCheck,
} from './deviceModel';

export const DEVICE_REFRESH_MS = 5000;
const REQUEST_TIMEOUT_MS = 12_000;

export interface DeviceRuntimeClient {
  listDevices(): Promise<unknown>;
  getDevice(serial: string): Promise<unknown>;
  getAgentHealth(serial: string, query: { session_id: string }): Promise<unknown>;
  connectAgent(serial: string, body: { timeout_ms: number }): Promise<unknown>;
  getTargetApp(serial: string): Promise<unknown>;
}

export interface DeviceStoreDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  clientFor(connection: RuntimeConnection, signal: AbortSignal): DeviceRuntimeClient;
  setTimer(callback: () => void, ms: number): unknown;
  clearTimer(timer: unknown): void;
}

export interface DeviceState {
  status: 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';
  devices: DeviceSnapshot[];
  selectedSerial: string | null;
  refreshing: boolean;
  busy: 'inspect' | 'connect' | 'target' | null;
  error: string | null;
  actionError: string | null;
  targetCheck: TargetAppCheck | null;
}

export interface DeviceStore {
  getSnapshot(): DeviceState;
  subscribe(listener: () => void): () => void;
  start(): () => void;
  refresh(): Promise<void>;
  selectDevice(serial: string): void;
  inspectSelected(): Promise<void>;
  connectSelected(): Promise<void>;
  checkTargetApp(): Promise<void>;
}

class DevicePanelFault extends Error {
  constructor(
    message: string,
    readonly invalidatesRuntime = false,
  ) {
    super(message);
  }
}

function describeError(error: unknown): string {
  if (error instanceof DevicePanelFault) return error.message;
  if (error instanceof RuntimeApiError) {
    return `Runtime 请求失败（HTTP ${error.status}），请刷新设备状态后重试。`;
  }
  return '未能读取 Runtime 设备信息，请检查本机 Runtime 后重试。';
}

function sameRuntime(a: RuntimeConnection | null, b: RuntimeConnection): boolean {
  return a?.pid === b.pid && a.port === b.port && a.token === b.token && a.version === b.version;
}

const defaultDeps = (): DeviceStoreDeps => ({
  getConnection: async () => {
    const bridge = window.pulse?.runtime;
    if (!bridge)
      throw new DevicePanelFault(
        '当前为浏览器预览，未加载 Electron 桌面桥接，无法读取本机设备。设备操作请使用 Pulse 桌面窗口；完整开发环境可在 pulse 目录通过 pnpm dev 启动。此提示不代表 Runtime 未运行。',
        true,
      );
    return bridge.getConnection();
  },
  clientFor: (connection, signal) =>
    createRuntimeClient(connection, (input, init) => fetch(input, { ...init, signal })),
  setTimer: (callback, ms) => setTimeout(callback, ms),
  clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
});

export function createDeviceStore(deps: DeviceStoreDeps = defaultDeps()): DeviceStore {
  let state: DeviceState = {
    status: 'idle',
    devices: [],
    selectedSerial: null,
    refreshing: false,
    busy: null,
    error: null,
    actionError: null,
    targetCheck: null,
  };
  const listeners = new Set<() => void>();
  let users = 0;
  let generation = 0;
  let selectionRevision = 0;
  let timer: unknown = null;
  let refreshWork: Promise<void> | null = null;
  let refreshController: AbortController | null = null;
  let actionController: AbortController | null = null;
  let owner: RuntimeConnection | null = null;

  const update = (partial: Partial<DeviceState>) => {
    state = { ...state, ...partial };
    for (const listener of listeners) listener();
  };
  const clearTimer = () => {
    if (timer !== null) deps.clearTimer(timer);
    timer = null;
  };
  const readConnection = async (signal: AbortSignal): Promise<RuntimeConnection> => {
    let connection: RuntimeConnection | null;
    try {
      connection = await withTimeout(
        deps.getConnection(),
        REQUEST_TIMEOUT_MS,
        'Runtime connection timeout',
        signal,
      );
    } catch (error) {
      if (error instanceof DevicePanelFault) throw error;
      throw new DevicePanelFault('无法确认本机 Runtime 连接，请刷新设备列表后重试。', true);
    }
    if (!connection)
      throw new DevicePanelFault('本机 Runtime 未运行或未连接，无法确认设备状态。', true);
    return connection;
  };
  const client = async (signal: AbortSignal) => {
    const connection = await readConnection(signal);
    return { connection, api: deps.clientFor(connection, signal) };
  };
  const verifyRuntime = async (connection: RuntimeConnection, signal: AbortSignal) => {
    if (!sameRuntime(connection, await readConnection(signal)))
      throw new DevicePanelFault('Runtime 连接已更换，旧响应已丢弃，请刷新设备列表。', true);
  };
  const schedule = () => {
    clearTimer();
    if (users === 0) return;
    timer = deps.setTimer(() => {
      timer = null;
      if (state.busy) schedule();
      else void refresh();
    }, DEVICE_REFRESH_MS);
  };
  const acceptDevices = (devices: DeviceSnapshot[]) => {
    const selected = devices.find((device) => device.serial === state.selectedSerial);
    const previous = state.devices.find((device) => device.serial === state.selectedSerial);
    const changedSession = selected?.connection.session_id !== previous?.connection.session_id;
    update({
      devices,
      selectedSerial: selected?.serial ?? null,
      ...(!selected || changedSession || selected.state !== 'device' ? { targetCheck: null } : {}),
    });
  };

  const refresh = (): Promise<void> => {
    if (refreshWork) return refreshWork;
    if (state.busy) return Promise.resolve();
    clearTimer();
    const currentGeneration = generation;
    const controller = new AbortController();
    refreshController = controller;
    update({ refreshing: true, status: state.devices.length ? state.status : 'loading' });
    const work = (async () => {
      try {
        const { connection, api } = await client(controller.signal);
        if (currentGeneration !== generation) return;
        if (!sameRuntime(owner, connection)) {
          owner = null;
          update({ status: 'loading', devices: [], targetCheck: null, error: null });
        }
        const result = parseDeviceList(
          await withTimeout(
            api.listDevices(),
            REQUEST_TIMEOUT_MS,
            'Device list timeout',
            controller.signal,
          ),
        );
        if (currentGeneration !== generation) return;
        await verifyRuntime(connection, controller.signal);
        if (currentGeneration !== generation) return;
        owner = connection;
        acceptDevices(result.devices);
        update({
          status: result.error ? 'error' : 'ready',
          error: result.error
            ? `Runtime 设备发现失败（${result.error}），当前列表不能确认为完整。`
            : null,
          ...(result.error ? { targetCheck: null } : {}),
        });
      } catch (error) {
        if (currentGeneration !== generation) return;
        if (error instanceof DevicePanelFault && error.invalidatesRuntime) owner = null;
        update({
          status: error instanceof DevicePanelFault ? 'unavailable' : 'error',
          error: describeError(error),
          targetCheck: null,
        });
      } finally {
        controller.abort();
        if (refreshController === controller) {
          refreshController = null;
          refreshWork = null;
          update({ refreshing: false });
          schedule();
        }
      }
    })();
    refreshWork = work;
    return work;
  };

  const perform = async (kind: NonNullable<DeviceState['busy']>) => {
    const selected = state.devices.find((device) => device.serial === state.selectedSerial);
    if (!selected || state.status !== 'ready' || state.busy || state.refreshing) return;
    if (kind === 'target' && selected.state !== 'device') return;
    if (
      kind === 'connect' &&
      (selected.state !== 'device' ||
        selected.connection.session_id !== null ||
        selected.recovery_required === true ||
        selected.diagnostic.cleanup_pending === true)
    )
      return;
    const serial = selected.serial;
    const currentGeneration = generation;
    const revision = selectionRevision;
    const controller = new AbortController();
    actionController = controller;
    clearTimer();
    update({ busy: kind, actionError: null, ...(kind === 'target' ? { targetCheck: null } : {}) });
    const current = () =>
      currentGeneration === generation &&
      revision === selectionRevision &&
      !controller.signal.aborted;
    try {
      const { connection, api } = await client(controller.signal);
      if (!current()) return;
      if (!sameRuntime(owner, connection)) {
        update({ status: 'unavailable', devices: [], targetCheck: null });
        throw new DevicePanelFault('Runtime 连接已更换，请刷新设备列表后再操作。', true);
      }
      const work =
        kind === 'target'
          ? api.getTargetApp(serial)
          : kind === 'connect'
            ? api.connectAgent(serial, { timeout_ms: 8000 })
            : selected.connection.session_id
              ? api.getAgentHealth(serial, { session_id: selected.connection.session_id })
              : api.getDevice(serial);
      const result = await withTimeout(
        work,
        REQUEST_TIMEOUT_MS,
        'Device request timeout',
        controller.signal,
      );
      if (!current()) return;
      await verifyRuntime(connection, controller.signal);
      if (!current()) return;
      if (kind === 'target') {
        update({ targetCheck: parseTargetAppCheck(result, serial) });
      } else {
        const snapshot = parseDeviceSnapshot(result);
        if (snapshot.serial !== serial)
          throw new DevicePanelFault('Runtime 返回了其他设备的快照，请重新刷新。');
        acceptDevices(
          state.devices.map((device) => (device.serial === serial ? snapshot : device)),
        );
        if (snapshot.operation_error) {
          update({
            actionError: 'Runtime 未执行接入操作，存在进行中的操作或待完成清理，请查看诊断。',
          });
        }
      }
    } catch (error) {
      if (current()) {
        const runtimeFault =
          (error instanceof DevicePanelFault && error.invalidatesRuntime) ||
          (error instanceof RuntimeApiError && error.status === 401);
        if (runtimeFault) owner = null;
        update({
          actionError: describeError(error),
          ...(kind !== 'target' || runtimeFault
            ? {
                status: 'error',
                error: '设备复查未完成，以下仅为上次收到的快照。',
                targetCheck: null,
              }
            : {}),
        });
      }
    } finally {
      controller.abort();
      if (actionController === controller) {
        actionController = null;
        update({ busy: null });
        schedule();
      }
    }
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    start: () => {
      users += 1;
      if (users === 1) void refresh();
      let released = false;
      return () => {
        if (released) return;
        released = true;
        users -= 1;
        if (users !== 0) return;
        generation += 1;
        clearTimer();
        refreshController?.abort();
        actionController?.abort();
        refreshController = null;
        actionController = null;
        refreshWork = null;
        owner = null;
        update({ status: 'idle', devices: [], refreshing: false, busy: null, targetCheck: null });
      };
    },
    refresh,
    selectDevice: (serial) => {
      if (
        serial === state.selectedSerial ||
        !state.devices.some((device) => device.serial === serial)
      )
        return;
      selectionRevision += 1;
      actionController?.abort();
      actionController = null;
      update({ selectedSerial: serial, busy: null, actionError: null, targetCheck: null });
      schedule();
    },
    inspectSelected: () => perform('inspect'),
    connectSelected: () => perform('connect'),
    checkTargetApp: () => perform('target'),
  };
}

let defaultStore: DeviceStore | null = null;

export function getDeviceStore(): DeviceStore {
  defaultStore ??= createDeviceStore();
  return defaultStore;
}

export function resetDeviceStore(): void {
  defaultStore = null;
}
