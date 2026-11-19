import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import type { RuntimeConnection } from '../../../shared/ipc';
import type { StreamView } from '../../shared/stream/streamClient';
import {
  DEFAULT_CONFIG,
  LEASE_RENEW_MS,
  StreamSession,
  type SessionState,
  type StreamConfig,
  type StreamSessionDeps,
} from './streamSession';

const config: StreamConfig = {
  ...DEFAULT_CONFIG,
  serial: 'S/1',
  mode: 'relay',
  relayToken: 'fake-token',
};
const connection: RuntimeConnection = { port: 5000, pid: 1, token: 'runtime-a', version: '0' };

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function harness() {
  let nextId = 0;
  const view = { start: vi.fn<StreamView['start']>(), stop: vi.fn() };
  const states: SessionState[] = [];
  const respond: StreamSessionDeps['request'] = (_conn, path, init) =>
    Promise.resolve(
      path.endsWith('/keepalive')
        ? { renewed: true }
        : init.method === 'POST'
          ? { started: true, publisher_id: `p${++nextId}`, lease_ms: 20000 }
          : {},
    );
  const request = vi.fn<StreamSessionDeps['request']>(respond);
  const getConnection = vi.fn<StreamSessionDeps['getConnection']>().mockResolvedValue(connection);
  const session = new StreamSession(
    view,
    {
      request,
      getConnection,
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
    },
    (state) => states.push(state),
  );
  return { session, view, states, request, respond, getConnection };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('GUI-owned stream session', () => {
  test('relay acquires/renews every 5s, then deletes only its own lease with keepalive', async () => {
    const h = harness();
    await h.session.start(config);
    expect(h.request).toHaveBeenCalledWith(
      connection,
      '/debug/stream/S%2F1/relay',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(h.view.start.mock.calls[0]?.[1]).toBe(true);
    await vi.advanceTimersByTimeAsync(LEASE_RENEW_MS);
    expect(h.request).toHaveBeenCalledWith(connection, '/debug/stream/S%2F1/relay/keepalive', {
      method: 'POST',
      body: JSON.stringify({ publisher_id: 'p1' }),
    });
    const signal = h.session.current?.signal;
    await h.session.stop();
    expect(signal?.aborted).toBe(true);
    expect(h.request).toHaveBeenLastCalledWith(
      connection,
      '/debug/stream/S%2F1/relay?publisher_id=p1',
      { method: 'DELETE', keepalive: true },
    );
    expect(h.session.current).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(h.request.mock.calls.filter(([, path]) => path.endsWith('/keepalive'))).toHaveLength(1);
  });

  test('stop/new start serialize behind a late POST; stale completion only deletes its own id', async () => {
    const h = harness();
    const late = deferred<unknown>();
    h.request.mockImplementationOnce(() => late.promise);
    const oldStart = h.session.start(config);
    await vi.advanceTimersByTimeAsync(0);
    const stop = h.session.stop();
    const newStart = h.session.start({ ...config, serial: 'new-device', channel: 'new-channel' });
    await vi.advanceTimersByTimeAsync(0);
    expect(h.request).toHaveBeenCalledTimes(1);
    expect(h.view.start).not.toHaveBeenCalled();
    late.resolve({ started: true, publisher_id: 'old/id', lease_ms: 20000 });
    await Promise.all([oldStart, stop, newStart]);
    expect(h.request.mock.calls.map(([, path, init]) => [init.method, path])).toEqual([
      ['POST', '/debug/stream/S%2F1/relay'],
      ['DELETE', '/debug/stream/S%2F1/relay?publisher_id=old%2Fid'],
      ['POST', '/debug/stream/new-device/relay'],
    ]);
    expect(h.view.start).toHaveBeenCalledOnce();
    expect(h.session.current?.config.serial).toBe('new-device');
    expect(h.states.at(-1)?.running).toBe(true);
    await h.session.stop();
  });

  test('late stop cleanup cannot stop or reset a newer session', async () => {
    const h = harness();
    await h.session.start(config);
    const cleanup = deferred<unknown>();
    h.request.mockImplementationOnce(() => cleanup.promise);
    const stopped = h.session.stop();
    await vi.advanceTimersByTimeAsync(0);
    const started = h.session.start({ ...config, mode: 'direct', serial: 'fresh' });
    const stops = h.view.stop.mock.calls.length;
    cleanup.resolve({});
    await Promise.all([stopped, started]);
    expect(h.view.stop).toHaveBeenCalledTimes(stops);
    expect(h.session.current?.config.serial).toBe('fresh');
    expect(h.states.at(-1)?.running).toBe(true);
    await h.session.stop();
  });

  test('404 keepalive recreates the same immutable active config', async () => {
    const h = harness();
    const cfg = { ...config };
    await h.session.start(cfg);
    cfg.channel = 'edited-after-start';
    h.request.mockImplementationOnce(() => Promise.reject(new Error('HTTP 404')));
    await vi.advanceTimersByTimeAsync(LEASE_RENEW_MS);
    const posts = h.request.mock.calls.filter(
      ([, path, init]) => init.method === 'POST' && path.endsWith('/relay'),
    );
    expect(posts).toHaveLength(2);
    expect(posts[1]?.[2].body).toBe(posts[0]?.[2].body);
    expect(h.view.start).toHaveBeenCalledTimes(2);
    expect(h.states.at(-1)).toMatchObject({ running: true, error: null });
    await h.session.stop();
    expect(h.request.mock.calls.at(-1)?.[1]).toContain('publisher_id=p2');
  });

  test('Runtime restart uses new connection, while cleanup stays bound to the issuing Runtime', async () => {
    const h = harness();
    await h.session.start(config);
    const fresh = { ...connection, port: 6000, pid: 2, token: 'runtime-b' };
    h.getConnection.mockResolvedValue(fresh);
    await vi.advanceTimersByTimeAsync(LEASE_RENEW_MS);
    expect(h.request).toHaveBeenCalledWith(
      connection,
      '/debug/stream/S%2F1/relay?publisher_id=p1',
      expect.objectContaining({ method: 'DELETE' }),
    );
    expect(h.request).toHaveBeenCalledWith(
      fresh,
      '/debug/stream/S%2F1/relay',
      expect.objectContaining({ method: 'POST' }),
    );
    await h.session.stop();
    expect(h.request.mock.calls.at(-1)?.[0]).toEqual(fresh);
  });

  test('hung old renewal is bounded and cannot recreate after stop/new start', async () => {
    const h = harness();
    await h.session.start(config);
    h.request.mockImplementationOnce(() => new Promise(() => undefined));
    await vi.advanceTimersByTimeAsync(LEASE_RENEW_MS);
    const stopped = h.session.stop();
    const started = h.session.start({ ...config, serial: 'new' });
    await vi.advanceTimersByTimeAsync(5000);
    await Promise.all([stopped, started]);
    expect(
      h.request.mock.calls.filter(
        ([, path, init]) => init.method === 'POST' && path === '/debug/stream/S%2F1/relay',
      ),
    ).toHaveLength(1);
    expect(h.session.current?.config.serial).toBe('new');
    await h.session.stop();
  });

  test('unmount/dispose cleans a lease returned after disposal and never connects', async () => {
    const h = harness();
    const late = deferred<unknown>();
    h.request.mockImplementationOnce(() => late.promise);
    const started = h.session.start(config);
    await vi.advanceTimersByTimeAsync(0);
    const disposed = h.session.dispose();
    late.resolve({ started: true, publisher_id: 'late', lease_ms: 20000 });
    await Promise.all([started, disposed]);
    expect(h.view.start).not.toHaveBeenCalled();
    expect(h.request.mock.calls.at(-1)?.[1]).toContain('publisher_id=late');
    expect(vi.getTimerCount()).toBe(0);
    await expect(h.session.start(config)).rejects.toThrow('disposed');
  });

  test('a hung acquisition rejects instead of permanently holding the UI start/stop queue', async () => {
    const h = harness();
    h.getConnection.mockImplementationOnce(() => new Promise(() => undefined));
    const start = h.session.start(config);
    const rejected = expect(start).rejects.toThrow('connection timeout');
    await vi.advanceTimersByTimeAsync(5000);
    await rejected;
    expect(h.states.at(-1)).toMatchObject({ running: false });
    await h.session.start(config);
    expect(h.view.start).toHaveBeenCalledOnce();
    await h.session.stop();
  });

  test('direct stream resolves fresh Runtime URLs, stops locally, and never changes Runtime lifetime', async () => {
    const h = harness();
    await h.session.start({ ...config, mode: 'direct' });
    const resolve = h.view.start.mock.calls[0]?.[0];
    expect(await resolve?.()).toContain('127.0.0.1:5000');
    h.getConnection.mockResolvedValue({ ...connection, port: 6000 });
    expect(await resolve?.()).toContain('127.0.0.1:6000');
    await h.session.stop();
    expect(await resolve?.()).toBeNull();
    expect(h.request).not.toHaveBeenCalled();
  });
});
