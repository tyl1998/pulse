import { describe, expect, test } from 'vitest';
import type { RuntimeConnection } from '../../../shared/ipc';
import {
  createRuntimeClient,
  heartbeatUrl,
  RuntimeMonitor,
  type RuntimeMonitorDeps,
  type RuntimeState,
} from './runtimeClient';

class FakeSocket {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  beat(seq: number) {
    this.onmessage?.({ data: JSON.stringify({ type: 'heartbeat', seq }) } as MessageEvent);
  }
  drop(code = 1006) {
    this.onclose?.({ code } as CloseEvent);
  }
  close() {
    this.closed = true;
  }
}

function harness(connections: (RuntimeConnection | null)[]) {
  const sockets: FakeSocket[] = [];
  const timers: (() => void)[] = [];
  const states: RuntimeState[] = [];
  let i = 0;
  const deps: RuntimeMonitorDeps = {
    getConnection: () =>
      Promise.resolve(connections[Math.min(i++, connections.length - 1)] ?? null),
    fetchHealth: (c) =>
      Promise.resolve({ status: 'ok', version: '0.0.0', pid: c.pid, uptime_s: 1 }),
    openSocket: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s as unknown as WebSocket;
    },
    now: () => 1000,
    setTimer: (fn) => timers.push(fn),
    clearTimer: () => undefined,
  };
  const monitor = new RuntimeMonitor(deps, (s) => states.push(s));
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const fireTimer = async () => {
    timers.shift()?.();
    await flush();
  };
  const sock = (n: number): FakeSocket => {
    const s = sockets[n];
    if (!s) throw new Error(`socket ${n} not opened`);
    return s;
  };
  return { monitor, sock, states, flush, fireTimer, last: () => states[states.length - 1] };
}

const connA: RuntimeConnection = { port: 1111, token: 'tok-a', pid: 10, version: '0.0.0' };
const connB: RuntimeConnection = { port: 2222, token: 'tok b', pid: 20, version: '0.0.0' };

describe('RuntimeMonitor', () => {
  test('heartbeat URL carries the encoded token', () => {
    expect(heartbeatUrl(connB)).toBe('ws://127.0.0.1:2222/ws/heartbeat?token=tok%20b');
  });

  test('generated client is bound to loopback and the Runtime token', async () => {
    let seenUrl = '';
    let seenAuth = '';
    const client = createRuntimeClient(connB, async (input, init) => {
      seenUrl = String(input);
      seenAuth = new Headers(init?.headers).get('authorization') ?? '';
      return new Response(
        JSON.stringify({ status: 'ok', version: '0.0.0', pid: 20, uptime_s: 1 }),
        { headers: { 'content-type': 'application/json' } },
      );
    });
    await client.getHealth();
    expect(seenUrl).toBe('http://127.0.0.1:2222/health');
    expect(seenAuth).toBe('Bearer tok b');
  });

  test('retries while runtime is not running, then connects', async () => {
    const h = harness([null, connA]);
    h.monitor.start();
    await h.flush();
    expect(h.last()).toMatchObject({ kind: 'not-running' });
    await h.fireTimer();
    h.sock(0).beat(1);
    expect(h.last()).toMatchObject({ kind: 'connected', port: 1111, pid: 10, seq: 1 });
  });

  test('reconnects to a restarted runtime with a new port/token (R05)', async () => {
    const h = harness([connA, connB]);
    h.monitor.start();
    await h.flush();
    h.sock(0).beat(1);
    h.sock(0).drop();
    expect(h.last()).toMatchObject({ kind: 'disconnected' });
    await h.fireTimer();
    expect(h.sock(1).url).toBe(heartbeatUrl(connB));
    h.sock(1).beat(1);
    expect(h.last()).toMatchObject({ kind: 'connected', port: 2222, pid: 20 });
  });

  test('stop closes the socket and suppresses retries', async () => {
    const h = harness([connA]);
    h.monitor.start();
    await h.flush();
    h.monitor.stop();
    expect(h.sock(0).closed).toBe(true);
    const n = h.states.length;
    h.sock(0).drop();
    expect(h.states.length).toBe(n);
  });
});
