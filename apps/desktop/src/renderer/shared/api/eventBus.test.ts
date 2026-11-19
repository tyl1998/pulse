import { describe, expect, test } from 'vitest';
import type { RuntimeConnection } from '../../../shared/ipc';
import {
  EventBus,
  type EventBusDeps,
  type EventStreamState,
  type RuntimeEventEnvelope,
} from './eventBus';
import { runtimeEventStreamUrl } from './runtime.generated';

class FakeSocket {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  sendJson(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent);
  }
  hello(body: Record<string, unknown>) {
    this.sendJson({
      v: 1,
      type: 'stream.hello',
      msg_id: `hello-${this.url}`,
      seq: 0,
      ts: 1,
      run_id: null,
      device_id: null,
      body,
    });
  }
  event(seq: number, overrides: Partial<RuntimeEventEnvelope> = {}) {
    this.sendJson({
      v: 1,
      type: 'run.state',
      msg_id: `msg-${seq}`,
      seq,
      ts: 1000,
      run_id: 'run-1',
      device_id: null,
      body: { state: 'running' },
      ...overrides,
    });
  }
  drop(code = 1006) {
    this.onclose?.({ code } as CloseEvent);
  }
  close() {
    this.closed = true;
  }
}

interface Timer {
  fn(): void;
  ms: number;
}

function harness(connections: (RuntimeConnection | null)[]) {
  const sockets: FakeSocket[] = [];
  const timers: Timer[] = [];
  let connectionIndex = 0;
  const deps: EventBusDeps = {
    getConnection: () =>
      Promise.resolve(connections[Math.min(connectionIndex++, connections.length - 1)] ?? null),
    openSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket as unknown as WebSocket;
    },
    setTimer: (fn, ms) => {
      const timer = { fn, ms };
      timers.push(timer);
      return timer;
    },
    clearTimer: (handle) => {
      const index = timers.indexOf(handle as Timer);
      if (index >= 0) timers.splice(index, 1);
    },
  };
  const bus = new EventBus(deps);
  const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
  const fireTimer = async () => {
    const timer = timers.shift();
    timer?.fn();
    await flush();
  };
  const socket = (index: number): FakeSocket => {
    const value = sockets[index];
    if (!value) throw new Error(`socket ${index} not opened`);
    return value;
  };
  return { bus, sockets, socket, timers, flush, fireTimer };
}

const connA: RuntimeConnection = { port: 1111, token: 'tok-a', pid: 10, version: '0.0.0' };
const connB: RuntimeConnection = { port: 2222, token: 'tok b', pid: 20, version: '0.0.0' };
const connAUrl = (since: number, runId?: string) =>
  runtimeEventStreamUrl({
    baseUrl: 'http://127.0.0.1:1111',
    token: 'tok-a',
    since,
    ...(runId ? { runId } : {}),
  });

describe('EventBus', () => {
  test('opens an authenticated stream from since=0 and delivers ordered events', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const states: EventStreamState[] = [];
    const subscription = h.bus.subscribe({ onState: (s) => states.push(s) }, (e) => events.push(e));
    await h.flush();

    expect(h.socket(0).url).toBe(connAUrl(0));
    h.socket(0).hello({ oldest_seq: 1, latest_seq: 2, replayed: 2, gap: false });
    h.socket(0).event(1);
    h.socket(0).event(2);

    expect(events.map((e) => e.seq)).toEqual([1, 2]);
    expect(subscription.lastSeq).toBe(2);
    expect(subscription.state).toMatchObject({
      kind: 'connected',
      oldestSeq: 1,
      latestSeq: 2,
      replayed: 2,
    });
    expect(states.map((s) => s.kind)).toEqual(['connecting', 'connecting', 'connected']);
  });

  test('filters run id and types while keeping the stream position', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const subscription = h.bus.subscribe({ runId: 'run-1', types: ['run.state'] }, (e) =>
      events.push(e),
    );
    await h.flush();
    expect(h.socket(0).url).toBe(connAUrl(0, 'run-1'));

    h.socket(0).hello({ oldest_seq: 1, latest_seq: 3, replayed: 3, gap: false });
    h.socket(0).event(1);
    h.socket(0).event(2, { run_id: 'run-2' });
    h.socket(0).event(3, { type: 'device.note' });

    expect(events.map((e) => e.seq)).toEqual([1]);
    expect(subscription.lastSeq).toBe(3);
  });

  test('does not treat server-filtered run gaps as missing events', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const gaps: unknown[] = [];
    const subscription = h.bus.subscribe({ runId: 'run-1', onGap: (gap) => gaps.push(gap) }, (e) =>
      events.push(e),
    );
    await h.flush();
    h.socket(0).hello({ oldest_seq: 1, latest_seq: 9, replayed: 2, gap: true });
    h.socket(0).event(1);
    h.socket(0).event(7);

    expect(gaps).toEqual([]);
    expect(events.map((e) => e.seq)).toEqual([1, 7]);
    expect(subscription.lastSeq).toBe(7);
  });

  test('de-duplicates msg_id and ignores seq already applied', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const subscription = h.bus.subscribe({}, (e) => events.push(e));
    await h.flush();

    h.socket(0).hello({ oldest_seq: 1, latest_seq: 3, gap: false });
    h.socket(0).event(1, { msg_id: 'reused' });
    h.socket(0).event(1, { msg_id: 'reused' });
    h.socket(0).event(2, { msg_id: 'reused' });
    h.socket(0).event(3, { msg_id: 'fresh' });

    expect(events.map((e) => e.seq)).toEqual([1, 3]);
    expect(subscription.lastSeq).toBe(3);
  });

  test('reconnect asks for since=last seq and does not redeliver replayed events', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const states: EventStreamState[] = [];
    const subscription = h.bus.subscribe({ onState: (s) => states.push(s) }, (e) => events.push(e));
    await h.flush();
    h.socket(0).hello({ oldest_seq: 1, latest_seq: 2, gap: false });
    h.socket(0).event(1);
    h.socket(0).event(2);

    h.socket(0).drop();
    expect(subscription.state).toMatchObject({ kind: 'disconnected' });
    expect(h.timers[0]?.ms).toBeGreaterThan(0);
    await h.fireTimer();

    expect(h.socket(1).url).toBe(connAUrl(2));
    h.socket(1).hello({ oldest_seq: 1, latest_seq: 3, replayed: 2, gap: false });
    h.socket(1).event(1);
    h.socket(1).event(2);
    h.socket(1).event(3);

    expect(events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(subscription.lastSeq).toBe(3);
    expect(states.map((s) => s.kind)).toEqual([
      'connecting',
      'connecting',
      'connected',
      'disconnected',
      'connecting',
      'connected',
    ]);
  });

  test('reconnects to a restarted Runtime with the new port and token', async () => {
    const h = harness([connA, connB]);
    const subscription = h.bus.subscribe({ since: 0 }, () => undefined);
    await h.flush();
    h.socket(0).drop();
    await h.fireTimer();

    expect(h.socket(1).url).toBe(
      runtimeEventStreamUrl({ baseUrl: 'http://127.0.0.1:2222', token: 'tok b', since: 0 }),
    );
    expect(subscription.state.kind).toBe('connecting');
  });

  test.each([
    ['PID changes', { ...connA, pid: 20 }],
    ['token changes with a reused PID', { ...connA, token: 'new-token' }],
    ['PID and token change', connB],
  ] as const)('resets the replay cursor and msg_id cache when %s', async (_change, connection) => {
    const h = harness([connA, connection]);
    const events: RuntimeEventEnvelope[] = [];
    const subscription = h.bus.subscribe({ since: 99 }, (e) => events.push(e));
    await h.flush();
    expect(h.socket(0).url).toBe(connAUrl(99));
    h.socket(0).hello({ oldest_seq: 1, latest_seq: 100, gap: false });
    h.socket(0).event(100, { msg_id: 'reused' });

    h.socket(0).drop();
    await h.fireTimer();
    expect(h.socket(1).url).toBe(
      runtimeEventStreamUrl({
        baseUrl: `http://127.0.0.1:${connection.port}`,
        token: connection.token,
        since: 0,
      }),
    );
    expect(subscription.lastSeq).toBe(0);
    h.socket(1).hello({ oldest_seq: 1, latest_seq: 2, gap: false });
    h.socket(1).event(1, { msg_id: 'reused' });
    h.socket(1).event(2, { msg_id: 'reused' });

    expect(events.map((e) => e.seq)).toEqual([100, 1]);
    expect(subscription.lastSeq).toBe(2);
  });

  test.each([
    ['the same instance', { ...connA }, 100, [100, 102]],
    ['a new instance', connB, 0, [100, 1, 2]],
  ] as const)(
    'preserves the last instance identity while unavailable and restores %s',
    async (_instance, connection, since, expectedSequences) => {
      const h = harness([connA, null, connection]);
      const events: RuntimeEventEnvelope[] = [];
      const subscription = h.bus.subscribe({ since: 99 }, (e) => events.push(e));
      await h.flush();
      h.socket(0).hello({ oldest_seq: 1, latest_seq: 100, gap: false });
      h.socket(0).event(100, { msg_id: 'reused' });

      h.socket(0).drop();
      await h.fireTimer();
      expect(subscription.state.kind).toBe('not-running');
      expect(subscription.lastSeq).toBe(100);
      expect(h.sockets).toHaveLength(1);

      await h.fireTimer();
      expect(h.socket(1).url).toBe(
        runtimeEventStreamUrl({
          baseUrl: `http://127.0.0.1:${connection.port}`,
          token: connection.token,
          since,
        }),
      );
      h.socket(1).hello({ oldest_seq: 1, latest_seq: since + 2, gap: false });
      h.socket(1).event(1, { msg_id: 'reused' });
      h.socket(1).event(since + 1, { msg_id: 'reused' });
      h.socket(1).event(since + 2);

      expect(events.map((e) => e.seq)).toEqual(expectedSequences);
      expect(subscription.lastSeq).toBe(since + 2);
    },
  );

  test('keeps the caller replay cursor until the first available Runtime connection', async () => {
    const h = harness([null, null, connA]);
    const subscription = h.bus.subscribe({ since: 99 }, () => undefined);
    await h.flush();
    await h.fireTimer();
    expect(subscription.lastSeq).toBe(99);
    expect(h.sockets).toHaveLength(0);

    await h.fireTimer();
    expect(h.socket(0).url).toBe(connAUrl(99));
    expect(subscription.lastSeq).toBe(99);
  });

  test('reports an evicted replay window once and continues from oldest_seq', async () => {
    const h = harness([connA]);
    const events: RuntimeEventEnvelope[] = [];
    const gaps: unknown[] = [];
    const subscription = h.bus.subscribe({ since: 5, onGap: (gap) => gaps.push(gap) }, (e) =>
      events.push(e),
    );
    await h.flush();
    expect(h.socket(0).url).toBe(connAUrl(5));

    h.socket(0).hello({ oldest_seq: 10, latest_seq: 12, replayed: 3, gap: true });
    h.socket(0).event(10);
    h.socket(0).event(11);

    expect(gaps).toEqual([{ fromSeq: 6, toSeq: 9, oldestSeq: 10, latestSeq: 12 }]);
    expect(events.map((e) => e.seq)).toEqual([10, 11]);
    expect(subscription.lastSeq).toBe(11);
    expect(subscription.state).toMatchObject({ kind: 'connected', since: 9 });
  });

  test('retries while the Runtime is not running, then connects', async () => {
    const h = harness([null, connA]);
    const states: EventStreamState[] = [];
    const events: RuntimeEventEnvelope[] = [];
    h.bus.subscribe({ onState: (s) => states.push(s) }, (e) => events.push(e));
    await h.flush();
    expect(states.at(-1)).toMatchObject({ kind: 'not-running' });

    await h.fireTimer();
    h.socket(0).hello({ oldest_seq: 1, latest_seq: 1, gap: false });
    h.socket(0).event(1);
    expect(events).toHaveLength(1);
    expect(states.at(-1)?.kind).toBe('connected');
  });

  test('close stops reconnecting and closes the socket', async () => {
    const h = harness([connA]);
    const subscription = h.bus.subscribe({}, () => undefined);
    await h.flush();
    subscription.close();
    expect(h.socket(0).closed).toBe(true);

    h.socket(0).drop();
    expect(h.timers).toHaveLength(0);
    expect(subscription.state.kind).toBe('connecting');
  });

  test('subscriptions are independent and keep their own since', async () => {
    const h = harness([connA]);
    const first: RuntimeEventEnvelope[] = [];
    const second: RuntimeEventEnvelope[] = [];
    h.bus.subscribe({ since: 0 }, (e) => first.push(e));
    h.bus.subscribe({ since: 42 }, (e) => second.push(e));
    await h.flush();
    expect(h.sockets.map((s) => s.url)).toEqual([connAUrl(0), connAUrl(42)]);

    h.socket(0).hello({ oldest_seq: 1, latest_seq: 1, gap: false });
    h.socket(1).hello({ oldest_seq: 1, latest_seq: 43, gap: false });
    h.socket(0).event(1);
    h.socket(1).event(43);
    expect(first.map((e) => e.seq)).toEqual([1]);
    expect(second.map((e) => e.seq)).toEqual([43]);
  });

  test('rejects new subscriptions after bus close', () => {
    const h = harness([connA]);
    h.bus.close();
    expect(() => h.bus.subscribe({}, () => undefined)).toThrow('EventBus is closed');
  });
});
