import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { RuntimeStatus as RuntimeLifecycleStatus } from '../../../shared/ipc';
import type { RuntimeState } from '../../shared/api/runtimeClient';
import { RuntimeStatus } from './RuntimeStatus';

const observer = vi.hoisted(() => ({
  emit: null as ((state: RuntimeState) => void) | null,
  start: vi.fn(),
  stop: vi.fn(),
}));

vi.mock('../../shared/api/runtimeClient', () => ({
  defaultDeps: vi.fn(),
  RuntimeMonitor: class {
    constructor(_deps: unknown, emit: (state: RuntimeState) => void) {
      observer.emit = emit;
    }
    start = observer.start;
    stop = observer.stop;
  },
}));

const incompatibleStatus: RuntimeLifecycleStatus = {
  kind: 'incompatible',
  connection: { port: 1234, token: 't', pid: 42, version: '0.9.0' },
  health: { status: 'ok', version: '0.9.0', pid: 42, uptime_s: 1 },
  required: '>=1.0.0',
  detail: 'incompatible version',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.parse('2026-11-02T09:00:00.000Z'));
  observer.emit = null;
  observer.start.mockClear();
  observer.stop.mockClear();
  vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn() } });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test('without preload, searching has an honest unknown time and no monitor is started', () => {
  vi.stubGlobal('pulse', undefined);
  render(<RuntimeStatus />);
  expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-state', 'searching');
  expect(screen.getByText('时间未知')).toBeVisible();
  expect(observer.start).not.toHaveBeenCalled();
});

test('connected keeps the original details and heartbeat source, never task-running or passed semantics', () => {
  render(<RuntimeStatus />);
  act(() =>
    observer.emit?.({
      kind: 'connected',
      port: 1234,
      pid: 42,
      version: '0.0.0',
      lastBeat: Date.now(),
      seq: 3,
    }),
  );
  const status = screen.getByTestId('runtime-status');
  expect(status).toHaveAttribute('data-state', 'connected');
  expect(status).toHaveTextContent('已连接 · v0.0.0 · pid 42 · 127.0.0.1:1234 · 心跳 #3');
  expect(status).toHaveTextContent('来源：Runtime 心跳（本机接收时间）');
  expect(status.querySelector('.ui-badge')).toHaveAttribute('data-tone', 'info');
  expect(status).not.toHaveTextContent(/通过|运行中/);
  act(() => vi.advanceTimersByTime(31_000));
  expect(screen.getByText('可能过期')).toBeVisible();
});

test('explains an incompatible Runtime instead of calling it not running', async () => {
  const getStatus = vi.fn().mockResolvedValue({
    kind: 'incompatible',
    connection: { port: 1234, token: 't', pid: 42, version: '0.9.0' },
    health: { status: 'ok', version: '0.9.0', pid: 42, uptime_s: 1 },
    required: '>=1.0.0',
    detail: 'x',
  });
  vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(), getStatus } });
  render(<RuntimeStatus />);
  await act(async () => {
    observer.emit?.({ kind: 'not-running', retryInMs: 500 });
  });
  expect(screen.getByTestId('runtime-detail')).toHaveTextContent(
    'Runtime v0.9.0 与当前桌面不兼容，需要 >=1.0.0',
  );
});

test.each(['not-running', 'disconnected'] as const)(
  'refreshes lifecycle details for each new %s state and hides stale details while pending',
  async (kind) => {
    const pending = deferred<RuntimeLifecycleStatus>();
    const getStatus = vi
      .fn()
      .mockResolvedValueOnce(incompatibleStatus)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ kind: 'not-running', launchable: true, detail: 'process exited' });
    vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(), getStatus } });
    render(<RuntimeStatus />);
    const state: RuntimeState =
      kind === 'not-running'
        ? { kind, retryInMs: 500 }
        : { kind, reason: 'offline', retryInMs: 500 };
    await act(async () => observer.emit?.(state));
    expect(getStatus).toHaveBeenCalledOnce();
    expect(screen.getByTestId('runtime-detail')).toHaveTextContent('Runtime v0.9.0');

    act(() => observer.emit?.({ ...state }));
    expect(getStatus).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId('runtime-detail')).not.toBeInTheDocument();
    await act(async () => {
      pending.resolve({ kind: 'unhealthy', pid: 43, detail: 'health failed' });
    });
    expect(screen.getByTestId('runtime-detail')).toHaveTextContent('Runtime 进程 43 存活');

    await act(async () => observer.emit?.({ ...state }));
    expect(getStatus).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-state', kind);
    expect(screen.queryByTestId('runtime-detail')).not.toBeInTheDocument();
  },
);

test.each(['resolve', 'reject'] as const)(
  'a late older status query cannot replace or clear the latest details when it %ss',
  async (outcome) => {
    const older = deferred<RuntimeLifecycleStatus>();
    const newer = deferred<RuntimeLifecycleStatus>();
    const getStatus = vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(), getStatus } });
    render(<RuntimeStatus />);
    act(() => observer.emit?.({ kind: 'not-running', retryInMs: 500 }));
    act(() => observer.emit?.({ kind: 'not-running', retryInMs: 500 }));
    expect(getStatus).toHaveBeenCalledTimes(2);

    await act(async () => {
      newer.resolve({ kind: 'unhealthy', pid: 43, detail: 'health failed' });
    });
    expect(screen.getByTestId('runtime-detail')).toHaveTextContent('Runtime 进程 43 存活');
    await act(async () => {
      if (outcome === 'resolve') older.resolve(incompatibleStatus);
      else older.reject(new Error('old status check failed'));
    });
    expect(screen.getByTestId('runtime-detail')).toHaveTextContent('Runtime 进程 43 存活');
    expect(screen.getByTestId('runtime-detail')).not.toHaveTextContent('Runtime v0.9.0');
  },
);

test.each(['resolve', 'reject'] as const)(
  'stops monitoring safely with a pending status query that %ss after unmount',
  async (outcome) => {
    const pending = deferred<RuntimeLifecycleStatus>();
    const getStatus = vi.fn().mockReturnValue(pending.promise);
    vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(), getStatus } });
    const { unmount } = render(<RuntimeStatus />);
    act(() => observer.emit?.({ kind: 'not-running', retryInMs: 500 }));
    expect(getStatus).toHaveBeenCalledOnce();
    unmount();
    expect(observer.stop).toHaveBeenCalledOnce();

    await act(async () => {
      observer.emit?.({ kind: 'not-running', retryInMs: 1000 });
      if (outcome === 'resolve') pending.resolve(incompatibleStatus);
      else pending.reject(new Error('status check failed after unmount'));
    });
    expect(getStatus).toHaveBeenCalledOnce();
    expect(screen.queryByTestId('runtime-status')).not.toBeInTheDocument();
  },
);

test('retry reasons and original state selectors survive the UI adapter; unmount stops the observer', () => {
  const { unmount } = render(<RuntimeStatus />);
  expect(observer.start).toHaveBeenCalledOnce();
  act(() => observer.emit?.({ kind: 'not-running', retryInMs: 500 }));
  expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-state', 'not-running');
  expect(screen.getByTestId('runtime-status')).toHaveTextContent('Runtime 未运行（0.5s 后重试）');
  act(() => observer.emit?.({ kind: 'disconnected', reason: '模拟连接关闭', retryInMs: 1000 }));
  expect(screen.getByTestId('runtime-status')).toHaveAttribute('data-state', 'disconnected');
  expect(screen.getByTestId('runtime-status')).toHaveTextContent(
    '连接断开：模拟连接关闭（1s 后重连）',
  );
  expect(screen.getByText('时间未知')).toBeVisible();
  unmount();
  expect(observer.stop).toHaveBeenCalledOnce();
});
