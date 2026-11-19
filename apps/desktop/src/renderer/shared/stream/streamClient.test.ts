import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import {
  COLLECT_TIMEOUT_MS,
  relaySubscribeUrl,
  runtimeStreamUrl,
  StreamView,
  WORKER_TIMEOUT_MS,
  type WorkerLike,
} from './streamClient';
import type { FrameSample, FromWorker, StreamStatus, ToWorker } from './workerProtocol';

class FakeWorker implements WorkerLike {
  sent: ToWorker[] = [];
  onmessage: ((ev: MessageEvent<unknown>) => void) | null = null;
  onerror: ((ev: ErrorEvent) => void) | null = null;
  onmessageerror: ((ev: MessageEvent<unknown>) => void) | null = null;
  terminated = false;
  connectionId = 0;
  postMessage(msg: ToWorker) {
    this.sent.push(msg);
    if (msg.type === 'connect') this.connectionId = msg.connectionId;
  }
  terminate() {
    this.terminated = true;
  }
  emit(msg: FromWorker, connectionId = this.connectionId) {
    this.onmessage?.({ data: { ...msg, connectionId } } as MessageEvent<unknown>);
  }
}

function harness() {
  const workers: FakeWorker[] = [];
  const events: FromWorker[] = [];
  const canvas = vi.fn(() => ({}) as OffscreenCanvas);
  const view = new StreamView(
    {
      createWorker: () => {
        const worker = new FakeWorker();
        workers.push(worker);
        return worker;
      },
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
    },
    (event) => events.push(event),
  );
  view.attach(canvas);
  const worker = () => {
    const last = workers.at(-1);
    if (!last) throw new Error('no worker');
    return last;
  };
  const ready = () => {
    worker().emit({ type: 'status', status: { state: 'open' } });
    worker().emit({ type: 'status', status: { state: 'streaming' } });
  };
  return {
    view,
    worker,
    workers,
    events,
    canvas,
    ready,
    start: async () => {
      view.start(() => Promise.resolve('ws://x'));
      await vi.advanceTimersByTimeAsync(0);
      ready();
    },
  };
}

const sample: FrameSample = {
  seq: 1,
  ptsUs: 1,
  key: false,
  bytes: 1,
  devMs: 1,
  clockRttMs: 0.1,
  relayQueueMs: 0,
  rtRecvMs: 1,
  rtSendMs: 1,
  guiRecvMs: 1,
  decodeStartMs: 1,
  decodedMs: 1,
  drawnMs: 2,
};
const roi = { x: 0.1, y: 0.1, w: 0.2, h: 0.2 };
const requestId = (worker: FakeWorker) => {
  const msg = worker.sent.at(-1);
  if (msg?.type !== 'collect') throw new Error('no collect');
  return msg.id;
};

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('StreamView', () => {
  test('urls', () => {
    const c = { port: 5000, token: 'a b', pid: 1, version: '0' };
    expect(runtimeStreamUrl(c, 'S/1', { maxSize: 720, bitRate: 4e6, maxFps: 30 })).toBe(
      'ws://127.0.0.1:5000/ws/stream/S%2F1?token=a+b&max_size=720&bit_rate=4000000&max_fps=30',
    );
    expect(relaySubscribeUrl('ws://h:7788/', 'dev 1', 't&')).toBe(
      'ws://h:7788/sub/dev%201?token=t%26',
    );
  });

  test('transfers canvas on init and reconnects with a fresh URL/id after close', async () => {
    const h = harness();
    expect(h.worker().sent[0]?.type).toBe('init');
    let n = 0;
    h.view.start(() => Promise.resolve(`ws://x/${++n}`), true);
    await vi.advanceTimersByTimeAsync(0);
    const firstId = h.worker().connectionId;
    expect(h.worker().sent.at(-1)).toMatchObject({ type: 'connect', url: 'ws://x/1', relay: true });
    h.worker().emit({ type: 'status', status: { state: 'closed', code: 1006, reason: '' } });
    await vi.advanceTimersByTimeAsync(500);
    expect(h.worker().sent.at(-1)).toMatchObject({ type: 'connect', url: 'ws://x/2' });
    expect(h.worker().connectionId).not.toBe(firstId);
    h.worker().emit({ type: 'status', status: { state: 'closed', code: 1006, reason: '' } });
    await vi.advanceTimersByTimeAsync(999);
    expect(n).toBe(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(n).toBe(3);
    h.view.dispose();
  });

  test('stop/dispose prevent reconnect and ignore late worker replies', async () => {
    const h = harness();
    await h.start();
    h.view.stop();
    const n = h.events.length;
    h.worker().emit({ type: 'status', status: { state: 'open' } });
    h.worker().emit({ type: 'status', status: { state: 'closed', code: 1006, reason: '' } });
    await vi.advanceTimersByTimeAsync(20_000);
    expect(h.worker().sent.at(-1)).toEqual({ type: 'disconnect' });
    expect(h.events).toHaveLength(n);
    h.view.dispose();
    expect(h.worker().terminated).toBe(true);
    await expect(h.view.collect()).rejects.toThrow('not connected');
  });

  test('presented-frame metadata is isolated to the current socket attempt', async () => {
    const h = harness();
    await h.start();
    const firstId = h.worker().connectionId;
    const frame = {
      sessionId: 'capture-a',
      configId: 1,
      seq: 7,
      width: 720,
      height: 1600,
      drawnAtMs: 1000,
      captureOrientation: 'natural' as const,
    };
    h.worker().emit({ type: 'frame', frame });
    expect(h.events.at(-1)).toMatchObject({ type: 'frame', frame });
    h.worker().emit({ type: 'status', status: { state: 'closed', code: 1006, reason: 'lost' } });
    await vi.advanceTimersByTimeAsync(500);
    const count = h.events.filter((event) => event.type === 'frame').length;
    h.worker().emit({ type: 'frame', frame }, firstId);
    expect(h.events.filter((event) => event.type === 'frame')).toHaveLength(count);
    h.worker().emit({ type: 'frame', frame: { ...frame, sessionId: 'capture-b' } });
    expect(h.events.at(-1)).toMatchObject({ type: 'frame', frame: { sessionId: 'capture-b' } });
    h.view.stop();
    const stopped = h.events.length;
    h.worker().emit({ type: 'frame', frame });
    expect(h.events).toHaveLength(stopped);
    h.view.dispose();
  });

  test('natural orientation is an explicit direct-stream request, not a new default', () => {
    const c = { port: 5000, token: 'fake-token', pid: 1, version: '0' };
    const url = runtimeStreamUrl(c, 'S', {
      maxSize: 1600,
      bitRate: 4e6,
      maxFps: 60,
      captureOrientation: 'natural',
    });
    expect(new URL(url).searchParams.get('capture_orientation')).toBe('natural');
    expect(
      new URL(
        runtimeStreamUrl(c, 'S', { maxSize: 1600, bitRate: 4e6, maxFps: 60 }),
      ).searchParams.has('capture_orientation'),
    ).toBe(false);
  });

  test('late URL resolution cannot connect after stop or a newer start with the same resolver', async () => {
    const h = harness();
    const resolves: ((url: string) => void)[] = [];
    const resolver = () => new Promise<string>((resolve) => resolves.push(resolve));
    h.view.start(resolver);
    await vi.advanceTimersByTimeAsync(0);
    h.view.stop();
    h.view.start(resolver);
    await vi.advanceTimersByTimeAsync(0);
    resolves[0]?.('ws://old');
    await vi.advanceTimersByTimeAsync(0);
    expect(h.worker().sent.filter((msg) => msg.type === 'connect')).toEqual([]);
    resolves[1]?.('ws://new');
    await vi.advanceTimersByTimeAsync(0);
    expect(h.worker().sent.at(-1)).toMatchObject({ type: 'connect', url: 'ws://new' });
    h.view.dispose();
  });

  test('a hung Runtime URL lookup has a deadline and retries', async () => {
    const h = harness();
    const resolver = vi
      .fn<() => Promise<string | null>>()
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockResolvedValue('ws://recovered');
    h.view.start(resolver);
    await vi.advanceTimersByTimeAsync(5500);
    expect(h.worker().sent.at(-1)).toMatchObject({ type: 'connect', url: 'ws://recovered' });
    h.view.dispose();
  });

  test('change waits for baseline ack; collect correlates replies by request id', async () => {
    const h = harness();
    await h.start();
    const { id, ready, done } = h.view.waitForChange(roi, 8, 1000);
    expect(h.worker().sent.at(-1)).toEqual({ type: 'arm', id, roi, threshold: 8 });
    h.worker().emit({ type: 'armed', id, ready: true });
    await expect(ready).resolves.toBe(true);
    h.worker().emit({ type: 'change', id, sample, diff: 50 });
    await expect(done).resolves.toEqual(sample);
    const collect = h.view.collect();
    h.worker().emit({ type: 'samples', id: requestId(h.worker()), samples: [sample] });
    await expect(collect).resolves.toEqual([sample]);
    h.view.dispose();
  });

  test('baseline/change timeout disarms and resolves both promises', async () => {
    const h = harness();
    await h.start();
    const { id, ready, done } = h.view.waitForChange(roi, 8, 10);
    await vi.advanceTimersByTimeAsync(10);
    await expect(ready).resolves.toBe(false);
    await expect(done).resolves.toBeNull();
    expect(h.worker().sent.at(-1)).toEqual({ type: 'disarm', id });
    h.view.dispose();
  });

  test('collect timeout rejects, and its late response cannot settle a later collection', async () => {
    const h = harness();
    await h.start();
    const first = h.view.collect();
    const oldId = requestId(h.worker());
    const rejected = expect(first).rejects.toThrow('collect timeout');
    await vi.advanceTimersByTimeAsync(COLLECT_TIMEOUT_MS);
    await rejected;
    const second = h.view.collect();
    const id = requestId(h.worker());
    h.worker().emit({ type: 'samples', id: oldId, samples: [sample] });
    h.worker().emit({ type: 'samples', id, samples: [] });
    await expect(second).resolves.toEqual([]);
    h.view.dispose();
  });

  test.each<StreamStatus>([
    { state: 'closed', code: 1006, reason: 'lost' },
    { state: 'upstream', upstream: 'error' },
    { state: 'bootstrapping', sessionId: 'new' },
    { state: 'relay', relay: 'disconnected' },
  ])('interruption rejects collections and cancels armed changes: %j', async (status) => {
    const h = harness();
    await h.start();
    const wait = h.view.waitForChange(roi, 8, 10_000);
    const collect = h.view.collect();
    const rejected = expect(collect).rejects.toThrow('interrupted');
    h.worker().emit({ type: 'status', status });
    await rejected;
    await expect(wait.ready).resolves.toBe(false);
    await expect(wait.done).resolves.toBeNull();
    h.view.dispose();
  });

  test('stop settles all outstanding requests immediately', async () => {
    const h = harness();
    await h.start();
    const wait = h.view.waitForChange(roi, 8, 10_000);
    const collect = h.view.collect();
    const rejected = expect(collect).rejects.toThrow('stopped');
    h.view.stop();
    await rejected;
    await expect(wait.done).resolves.toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  test('silent/crashed worker is replaced with a fresh canvas and reconnects', async () => {
    const h = harness();
    await h.start();
    const old = h.worker();
    await vi.advanceTimersByTimeAsync(WORKER_TIMEOUT_MS);
    expect(old.terminated).toBe(true);
    expect(h.canvas).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(500);
    h.ready();
    old.emit({ type: 'status', status: { state: 'closed', code: 1, reason: 'late' } });
    const collect = h.view.collect();
    h.worker().emit({ type: 'samples', id: requestId(h.worker()), samples: [] });
    await expect(collect).resolves.toEqual([]);
    h.worker().onerror?.({ message: 'crashed' } as ErrorEvent);
    expect(h.workers).toHaveLength(3);
    h.view.dispose();
  });

  test('events drain only new state transitions and remain bounded', async () => {
    const h = harness();
    await h.start();
    expect(h.view.events().length).toBeGreaterThan(0);
    expect(h.view.events()).toEqual([]);
    for (let i = 0; i < 300; i++)
      h.worker().emit({ type: 'status', status: { state: 'upstream', upstream: String(i) } });
    const events = h.view.events();
    expect(events).toHaveLength(256);
    expect(events.at(-1)?.status).toEqual({ state: 'upstream', upstream: '299' });
    h.view.dispose();
  });
});
