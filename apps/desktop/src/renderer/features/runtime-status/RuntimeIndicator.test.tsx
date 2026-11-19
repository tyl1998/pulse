import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import type { RuntimeState } from '../../shared/api/runtimeClient';
import { RuntimeIndicator } from './RuntimeIndicator';

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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test('without preload the top bar reports an unknown-time search, not a connection', () => {
  vi.stubGlobal('pulse', undefined);
  render(<RuntimeIndicator />);
  const indicator = screen.getByTestId('runtime-indicator');
  expect(indicator).toHaveAttribute('data-state', 'searching');
  expect(indicator).toHaveTextContent('查找 Runtime');
  expect(indicator).toHaveTextContent('来源：本地 Runtime 连接观察器');
  expect(indicator).toHaveTextContent('时间未知');
  expect(observer.start).not.toHaveBeenCalled();
});

test('connected keeps a heartbeat source and observation time without task semantics', () => {
  vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn() } });
  render(<RuntimeIndicator />);
  const beat = Date.now();
  act(() =>
    observer.emit?.({
      kind: 'connected',
      port: 1234,
      pid: 42,
      version: '0.0.0',
      lastBeat: beat,
      seq: 3,
    }),
  );
  const indicator = screen.getByTestId('runtime-indicator');
  expect(indicator).toHaveAttribute('data-state', 'connected');
  expect(indicator).toHaveTextContent('Runtime 已连接 v0.0.0');
  expect(indicator).toHaveTextContent('来源：Runtime 心跳（本机接收时间）');
  expect(indicator.querySelector('time')).toHaveAttribute('dateTime', new Date(beat).toISOString());
  expect(indicator).not.toHaveTextContent(/通过|运行中/);
});

test('an incompatible live Runtime is labelled from the main-process check, not "not running"', async () => {
  const getStatus = vi.fn().mockResolvedValue({
    kind: 'incompatible',
    connection: { port: 1234, token: 't', pid: 42, version: '0.9.0' },
    health: { status: 'ok', version: '0.9.0', pid: 42, uptime_s: 1 },
    required: '>=1.0.0',
    detail: 'x',
  });
  vi.stubGlobal('pulse', { runtime: { getConnection: vi.fn(), getStatus } });
  render(<RuntimeIndicator />);
  await act(async () => {
    observer.emit?.({ kind: 'not-running', retryInMs: 500 });
  });
  const indicator = screen.getByTestId('runtime-indicator');
  expect(indicator).toHaveAttribute('data-state', 'degraded');
  expect(indicator).toHaveTextContent('Runtime 版本不兼容');
  expect(indicator).toHaveTextContent('来源：主进程版本检查');
  expect(indicator.querySelector('.ui-badge')).toHaveAttribute('data-tone', 'warning');
});
