import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { codecFromConfig, concatBytes, nalType, splitAnnexB } from './annexb';
import { runChangeBench, segments } from './bench';
import { decodeEnvelope, encodeEnvelope, PacketKind } from './envelope';
import { percentile, summarize } from './stats';
import type { FrameSample } from './workerProtocol';

const SPS = [0x67, 0x64, 0x00, 0x20, 0xac];
const PPS = [0x68, 0xee, 0x3c];
const IDR = [0x65, 0x88, 0x84];

describe('annexb', () => {
  test('splits 3- and 4-byte start codes', () => {
    const data = new Uint8Array([0, 0, 0, 1, ...SPS, 0, 0, 1, ...PPS, 0, 0, 0, 1, ...IDR]);
    const nals = splitAnnexB(data);
    expect(nals.map((n) => Array.from(n))).toEqual([SPS, PPS, IDR]);
    expect(nals.map(nalType)).toEqual([7, 8, 5]);
    expect(splitAnnexB(new Uint8Array([1, 2, 3]))).toEqual([]);
  });

  test('codec string from SPS', () => {
    expect(codecFromConfig(new Uint8Array([0, 0, 0, 1, ...SPS, 0, 0, 0, 1, ...PPS]))).toBe(
      'avc1.640020',
    );
    expect(codecFromConfig(new Uint8Array([0, 0, 1, ...IDR]))).toBeNull();
  });

  test('concat', () => {
    expect(Array.from(concatBytes(new Uint8Array([1]), new Uint8Array([2, 3])))).toEqual([1, 2, 3]);
  });
});

describe('envelope', () => {
  test('round trips the Runtime wire format', () => {
    const buf = encodeEnvelope({
      kind: PacketKind.key,
      seq: 9,
      ptsUs: 1_297_761_150_366,
      rtRecvMs: 1_793_356_760_892.5,
      rtSendMs: 1_793_356_760_893.25,
      payload: new Uint8Array([1, 2, 3]),
    });
    expect(new Uint8Array(buf)[1]).toBe(2);
    const e = decodeEnvelope(buf);
    expect(e).toMatchObject({
      kind: 2,
      seq: 9,
      ptsUs: 1_297_761_150_366,
      rtSendMs: 1_793_356_760_893.25,
    });
    expect(Array.from(e.payload)).toEqual([1, 2, 3]);
  });

  test('rejects bad input', () => {
    expect(() => decodeEnvelope(new ArrayBuffer(4))).toThrow();
    const buf = new ArrayBuffer(32);
    new DataView(buf).setUint8(0, 9);
    expect(() => decodeEnvelope(buf)).toThrow(/version/);
  });
});

describe('stats', () => {
  test('percentiles', () => {
    const v = Array.from({ length: 101 }, (_, i) => i);
    expect(percentile(v, 50)).toBe(50);
    expect(percentile(v, 99)).toBe(99);
    expect(summarize([3, 1, 2, NaN])).toMatchObject({ n: 3, p50: 2, max: 3 });
    expect(Number.isNaN(summarize([]).p50)).toBe(true);
  });
});

const sample = (o: Partial<FrameSample> = {}): FrameSample => ({
  seq: 1,
  ptsUs: 1000,
  key: false,
  bytes: 10,
  devMs: 100,
  rtRecvMs: 120,
  rtSendMs: 121,
  guiRecvMs: 122,
  relayQueueMs: 0,
  clockRttMs: 0.9,
  decodeStartMs: 122,
  decodedMs: 125,
  drawnMs: 126,
  ...o,
});

describe('bench', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });
  test('segments add up to e2e', () => {
    const s = segments(sample());
    expect(s).toEqual({
      devToRuntime: 20,
      runtimeForward: 1,
      transport: 1,
      relayQueue: 0,
      networkExcludingRelay: 1,
      decode: 3,
      render: 1,
      e2e: 26,
    });
    expect(Number.isNaN(segments(sample({ devMs: null })).e2e)).toBe(true);
    expect(segments(sample({ relayQueueMs: 0.4 })).networkExcludingRelay).toBe(0.6);
    expect(segments(sample({ relayQueueMs: null })).networkExcludingRelay).toBeNaN();
  });

  test('change bench counts hits and misses', async () => {
    let i = 0;
    const watcher = {
      waitForChange: () => ({
        id: ++i,
        ready: Promise.resolve(true),
        done: Promise.resolve(i === 2 ? null : sample({ drawnMs: 500 })),
        cancel: () => undefined,
      }),
    };
    const r = await runChangeBench(
      watcher,
      () => Promise.resolve(400),
      { n: 3, roi: { x: 0, y: 0, w: 1, h: 1 }, threshold: 5, settleMs: 0, timeoutMs: 10 },
      () => Promise.resolve(),
    );
    expect(r.trials).toHaveLength(2);
    expect(r.misses).toBe(1);
    expect(r.trials[0]?.sentMs).toBe(400);
    expect(r).toMatchObject({ attempts: 3, aborted: false });
  });

  const opts = { n: 1, roi: { x: 0, y: 0, w: 1, h: 1 }, threshold: 5, settleMs: 0, timeoutMs: 10 };

  test('a missing baseline never dispatches the tap', async () => {
    const trigger = vi.fn(() => Promise.resolve(400));
    const cancel = vi.fn();
    const result = await runChangeBench(
      {
        waitForChange: () => ({
          id: 1,
          ready: Promise.resolve(false),
          done: Promise.resolve(null),
          cancel,
        }),
      },
      trigger,
      opts,
      () => Promise.resolve(),
    );
    expect(trigger).not.toHaveBeenCalled();
    expect(cancel).toHaveBeenCalledOnce();
    expect(result).toMatchObject({ trials: [], misses: 1, attempts: 0 });
  });

  test.each(['baseline', 'trigger', 'change'])(
    'a hung %s has a deadline even with a broken watcher',
    async (stage) => {
      const cancel = vi.fn();
      const trigger = vi.fn(() =>
        stage === 'trigger' ? new Promise<number>(() => undefined) : Promise.resolve(400),
      );
      const result = runChangeBench(
        {
          waitForChange: () => ({
            id: 1,
            ready:
              stage === 'baseline' ? new Promise<boolean>(() => undefined) : Promise.resolve(true),
            done: new Promise<FrameSample | null>(() => undefined),
            cancel,
          }),
        },
        trigger,
        opts,
        () => Promise.resolve(),
      );
      await vi.advanceTimersByTimeAsync(50);
      await expect(result).resolves.toMatchObject({ trials: [], misses: 1 });
      expect(cancel).toHaveBeenCalledOnce();
      expect(trigger).toHaveBeenCalledTimes(stage === 'baseline' ? 0 : 1);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  test('stop aborts the current wait and all remaining taps without inventing a miss', async () => {
    const controller = new AbortController();
    const trigger = vi.fn(() => new Promise<number>(() => undefined));
    const cancel = vi.fn();
    const result = runChangeBench(
      {
        waitForChange: () => ({
          id: 1,
          ready: Promise.resolve(true),
          done: new Promise<FrameSample | null>(() => undefined),
          cancel,
        }),
      },
      trigger,
      { ...opts, n: 300, signal: controller.signal },
      () => Promise.resolve(),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(trigger).toHaveBeenCalledOnce();
    controller.abort();
    await expect(result).resolves.toEqual({ trials: [], misses: 0, attempts: 1, aborted: true });
    expect(cancel).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });
});
