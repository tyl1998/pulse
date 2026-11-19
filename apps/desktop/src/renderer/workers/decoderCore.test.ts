import { describe, expect, test } from 'vitest';
import { encodeEnvelope, PacketKind } from '../shared/stream/envelope';
import type { FromWorker } from '../shared/stream/workerProtocol';
import {
  DecoderCore,
  MAX_DECODE_QUEUE,
  MAX_SAMPLES,
  meanAbsDiff,
  type ChunkInit,
  type DecoderLike,
  type FrameLike,
} from './decoderCore';

const CONFIG = new Uint8Array([0, 0, 0, 1, 0x67, 0x64, 0x00, 0x1f, 0, 0, 0, 1, 0x68, 0xee]);

class FakeDecoder implements DecoderLike {
  decodeQueueSize = 0;
  configs: string[] = [];
  chunks: ChunkInit[] = [];
  resets = 0;
  closed = false;
  constructor(
    readonly output: (f: FrameLike) => void,
    readonly error: (e: Error) => void,
  ) {}
  configure(c: { codec: string }) {
    this.configs.push(c.codec);
  }
  decode(chunk: ChunkInit) {
    this.chunks.push(chunk);
  }
  reset() {
    this.resets += 1;
  }
  close() {
    this.closed = true;
  }
  /** Emit the decoded frame for chunk i (WebCodecs output callback). */
  emit(i: number, closed: number[] = []) {
    const c = this.chunks[i];
    if (!c) throw new Error('no chunk');
    this.output({
      timestamp: c.timestamp,
      displayWidth: 720,
      displayHeight: 1600,
      close: () => closed.push(c.timestamp),
    });
  }
}

function harness(relay = false, monotonic?: () => number) {
  let t = 1000;
  const posted: FromWorker[] = [];
  const drawn: number[] = [];
  const clears: number[] = [];
  let pixels = new Uint8ClampedArray(16).fill(10);
  let dec: FakeDecoder | null = null;
  const core = new DecoderCore(
    {
      createDecoder: (out, err) => (dec = new FakeDecoder(out, err)),
      draw: (f) => drawn.push(f.timestamp),
      clear: () => clears.push(t),
      sampleRoi: () => pixels,
      now: () => (t += 1),
      ...(monotonic ? { monotonic } : {}),
      post: (m) => posted.push(m),
    },
    relay,
  );
  const send = (kind: PacketKind, seq: number, payload = new Uint8Array([0, 0, 1, 0x41])) =>
    core.handleBinary(
      encodeEnvelope({
        kind,
        seq,
        ptsUs: seq * 1000,
        rtRecvMs: 50,
        rtSendMs: 51,
        payload: kind === PacketKind.config ? CONFIG : payload,
      }),
      52,
    );
  return {
    core,
    posted,
    drawn,
    clears,
    send,
    dec: () => {
      if (!dec) throw new Error('no decoder');
      return dec;
    },
    setPixels: (v: number) => (pixels = new Uint8ClampedArray(16).fill(v)),
    setTime: (value: number) => {
      t = value;
    },
  };
}

const calibrate = (core: DecoderCore, extra: Record<string, unknown> = {}) =>
  core.handleText(
    JSON.stringify({
      type: 'clock',
      valid: true,
      offset_us: 0,
      rtt_us: 900,
      at_ms: 1000,
      max_age_ms: 15000,
      ...extra,
    }),
  );

describe('DecoderCore (fake WebCodecs)', () => {
  test('decode duration uses local monotonic time rather than a changing wall epoch', () => {
    let mono = 10;
    const h = harness(false, () => mono);
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.setTime(1400);
    mono = 12.25;
    h.dec().emit(0);
    const sample = h.core.collect()[0];
    expect(sample).toBeDefined();
    expect(sample && sample.decodedMs - sample.decodeStartMs).toBe(2.25);
    expect(sample?.timingResolutionMs).toBe(1);
  });

  test('configures from SPS and prepends config to key frames', () => {
    const h = harness();
    h.send(PacketKind.delta, 0); // no config yet -> dropped
    h.send(PacketKind.config, 1);
    expect(h.dec().configs).toEqual(['avc1.64001f']);
    h.send(PacketKind.delta, 2); // awaiting key -> dropped
    h.send(PacketKind.key, 3);
    h.send(PacketKind.delta, 4);
    const [key, delta] = h.dec().chunks;
    expect(key?.type).toBe('key');
    expect(Array.from(key?.data.subarray(0, CONFIG.length) ?? [])).toEqual(Array.from(CONFIG));
    expect(delta?.type).toBe('delta');
    expect(h.core.tick().droppedWorker).toBe(2);
  });

  test('frames are drawn, closed and timed', () => {
    const h = harness();
    calibrate(h.core);
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    const closed: number[] = [];
    h.dec().emit(0, closed);
    expect(h.drawn).toEqual([0]);
    expect(closed).toEqual([0]);
    const [s] = h.core.collect();
    expect(s).toMatchObject({
      seq: 1,
      key: true,
      devMs: 1,
      clockRttMs: 0.9,
      relayQueueMs: 0,
      rtRecvMs: 50,
      guiRecvMs: 52,
    });
    expect(s && s.drawnMs > s.decodedMs && s.decodedMs > s.decodeStartMs).toBe(true);
    const m = h.core.tick();
    expect(m).toMatchObject({ width: 720, height: 1600, codec: 'avc1.64001f', clockRttMs: 0.9 });
    expect(h.posted.at(-1)?.type).toBe('metrics');
  });

  test('publishes actual canvas dimensions and frame identity immediately after each draw', () => {
    const h = harness();
    h.core.handleText(JSON.stringify({ type: 'session', session_id: 'capture-a' }));
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    const first = h.posted.find((message) => message.type === 'frame');
    expect(first).toMatchObject({
      type: 'frame',
      frame: { sessionId: 'capture-a', seq: 1, width: 720, height: 1600 },
    });
    expect(h.drawn).toEqual([0]);
    h.send(PacketKind.delta, 2);
    const chunk = h.dec().chunks[1];
    if (!chunk) throw new Error('missing chunk');
    h.dec().output({
      timestamp: chunk.timestamp,
      displayWidth: 1600,
      displayHeight: 720,
      close: () => undefined,
    });
    const frames = h.posted.filter((message) => message.type === 'frame');
    expect(frames).toHaveLength(2);
    expect(frames[1]?.frame).toMatchObject({ seq: 2, width: 1600, height: 720 });
    expect(frames[1]?.frame.configId).toBe(frames[0]?.frame.configId);
  });

  test('CONFIG restarts change presented-frame identity and late output publishes no metadata', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    const old = h.dec();
    h.send(PacketKind.config, 0);
    old.emit(0);
    expect(h.posted.filter((message) => message.type === 'frame')).toHaveLength(1);
    h.send(PacketKind.key, 2);
    h.dec().emit(0);
    const frames = h.posted.filter((message) => message.type === 'frame');
    expect(frames).toHaveLength(2);
    expect(frames[1]?.frame.configId).not.toBe(frames[0]?.frame.configId);
    expect(frames[1]?.frame.seq).toBe(2);
  });

  test.each(['natural', 'display', null] as const)(
    'only confirms a capture profile supplied by the source: %s',
    (captureOrientation) => {
      const h = harness();
      h.core.handleText(
        JSON.stringify({
          type: 'session',
          session_id: 'capture',
          capture_orientation: captureOrientation,
        }),
      );
      h.send(PacketKind.config, 0);
      h.send(PacketKind.key, 1);
      h.dec().emit(0);
      expect(h.posted.find((message) => message.type === 'frame')).toMatchObject({
        frame: { captureOrientation },
      });
    },
  );

  test('backlogged decoder drops deltas until the next key frame', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.dec().decodeQueueSize = MAX_DECODE_QUEUE + 1;
    h.send(PacketKind.delta, 2);
    h.dec().decodeQueueSize = 0;
    h.send(PacketKind.delta, 3); // still skipping: its reference was dropped
    h.send(PacketKind.key, 4);
    expect(h.dec().chunks.map((c) => c.type)).toEqual(['key', 'key']);
  });

  test('decoder error rebuilds from CONFIG but still waits for a KEY', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    const old = h.dec();
    old.error(new Error('decode failed'));
    expect(old.closed).toBe(true);
    h.send(PacketKind.delta, 2);
    expect(h.dec().chunks).toEqual([]);
    h.send(PacketKind.key, 3);
    h.dec().emit(0);
    expect(h.core.collect().map((s) => s.seq)).toEqual([3]);
    old.emit(0);
    expect(h.drawn).toHaveLength(1);
  });

  test('an invalid CONFIG cannot leave the previous decoder/config active', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    const old = h.dec();
    h.core.handleBinary(
      encodeEnvelope({
        kind: PacketKind.config,
        seq: 2,
        ptsUs: 0,
        rtRecvMs: 0,
        rtSendMs: 0,
        payload: new Uint8Array([0, 0, 1, 0x68]),
      }),
      0,
    );
    old.emit(0);
    h.send(PacketKind.key, 3);
    expect(old.closed).toBe(true);
    expect(h.drawn).toEqual([]);
    expect(h.core.tick()).toMatchObject({ codec: null, width: 0 });
  });

  test('identical SPS is still a continuity boundary; late frames/errors cannot revive it', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    const old = h.dec();
    h.send(PacketKind.config, 0);
    expect(old.closed).toBe(true);
    expect(h.dec()).not.toBe(old);
    const fresh = h.dec();
    old.error(new Error('late old decoder error'));
    expect(h.dec()).toBe(fresh);
    const closed: number[] = [];
    old.emit(0, closed);
    expect(closed).toEqual([0]);
    expect(h.drawn).toEqual([]);
    h.send(PacketKind.delta, 2);
    expect(fresh.chunks).toHaveLength(0);
    h.send(PacketKind.key, 3);
    fresh.emit(0);
    expect(h.core.collect().map((s) => s.seq)).toEqual([3]);
    h.core.handleBinary(
      encodeEnvelope({
        kind: PacketKind.config,
        seq: 2,
        ptsUs: 0,
        rtRecvMs: 0,
        rtSendMs: 0,
        payload: new Uint8Array([0, 0, 0, 1, 0x67, 0x42, 0x00, 0x28]),
      }),
      0,
    );
    expect(fresh.closed).toBe(true);
    expect(h.dec().configs).toEqual(['avc1.420028']);
  });

  test('change detection reports the first differing frame after arming', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    for (let i = 1; i <= 3; i++) h.send(i === 1 ? PacketKind.key : PacketKind.delta, i);
    const roi = { x: 0, y: 0, w: 1, h: 1 };
    h.core.arm(7, roi, 5);
    expect(h.posted.some((m) => m.type === 'armed')).toBe(false);
    h.dec().emit(0); // baseline
    expect(h.posted).toContainEqual({ type: 'armed', id: 7, ready: true });
    h.setPixels(12);
    h.dec().emit(1); // diff 2 < 5
    h.setPixels(200);
    h.dec().emit(2);
    const change = h.posted.find((m) => m.type === 'change');
    expect(change).toMatchObject({ type: 'change', id: 7, sample: { seq: 3 } });
    // re-arming the same ROI reuses the last frame as baseline (static screens send no frames)
    h.core.arm(8, roi, 5);
    h.send(PacketKind.delta, 4);
    h.dec().emit(3);
    expect(h.posted.filter((m) => m.type === 'change')).toHaveLength(1);
  });

  test('first arm on an already-static canvas gets a baseline without needing another frame', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    h.core.arm(1, { x: 0.2, y: 0.2, w: 0.5, h: 0.5 }, 5);
    expect(h.posted.at(-1)).toEqual({ type: 'armed', id: 1, ready: true });
    h.setPixels(200);
    h.send(PacketKind.delta, 2);
    h.dec().emit(1);
    expect(h.posted.at(-1)).toMatchObject({ type: 'change', id: 1, sample: { seq: 2 } });
  });

  test('session id changes clear frames/calibration; codec updates with the same id do not', () => {
    const h = harness();
    h.core.handleText(JSON.stringify({ type: 'session', session_id: 'a', codec: null }));
    h.send(PacketKind.config, 0);
    calibrate(h.core);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    const dec = h.dec();
    h.core.handleText(JSON.stringify({ type: 'session', session_id: 'a', codec: 'avc1.64001f' }));
    expect(dec.closed).toBe(false);
    expect(h.core.collect()).toHaveLength(1);
    h.core.handleText(JSON.stringify({ type: 'session', session_id: 'b' }));
    expect(dec.closed).toBe(true);
    expect(h.core.tick()).toMatchObject({ width: 0, clockRttMs: null });
    h.send(PacketKind.key, 0);
    expect(h.drawn).toHaveLength(1);
    h.send(PacketKind.config, 0);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    expect(h.core.collect()[0]).toMatchObject({ devMs: null, clockRttMs: null });
  });

  test.each([
    { type: 'clock', valid: false, error: 'offline', offset_us: 0 },
    { type: 'clock', error: 'legacy error' },
    { type: 'clock', valid: true, offset_us: 0, rtt_us: 1, at_ms: -20_000, max_age_ms: 15000 },
    { type: 'clock', valid: true, offset_us: 0, rtt_us: 1, at_ms: 2000, max_age_ms: 15000 },
    { type: 'clock', valid: true, offset_us: 0, rtt_us: -1, at_ms: 1000, max_age_ms: 15000 },
  ])('rejects invalid calibration and strips pending timings: %j', (invalid) => {
    const h = harness();
    h.send(PacketKind.config, 0);
    calibrate(h.core);
    h.send(PacketKind.key, 1);
    h.core.handleText(JSON.stringify(invalid));
    h.dec().emit(0);
    expect(h.core.collect()[0]).toMatchObject({ devMs: null, clockRttMs: null });
    expect(h.core.tick().clockRttMs).toBeNull();
    expect(h.core.tick().e2eMsP50).toBeNaN();
  });

  test('calibration expires without receiving another clock message and can recover', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    calibrate(h.core);
    h.send(PacketKind.key, 1);
    h.setTime(16_001);
    h.dec().emit(0);
    expect(h.core.collect()[0]?.devMs).toBeNull();
    expect(h.core.tick().clockRttMs).toBeNull();
    calibrate(h.core, { at_ms: 16_000 });
    h.send(PacketKind.delta, 2);
    h.dec().emit(1);
    expect(h.core.collect()[0]).toMatchObject({ devMs: 2, clockRttMs: 0.9 });
  });

  test('small host versus performance epoch rounding does not discard fresh calibration', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    calibrate(h.core, { at_ms: 1004 });
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    expect(h.core.collect()[0]).toMatchObject({ devMs: 1, clockRttMs: 0.9 });
  });

  test.each(['starting', 'error', 'stopped'])(
    '%s clears old output, ROI baseline and calibration',
    (state) => {
      const h = harness();
      h.send(PacketKind.config, 0);
      calibrate(h.core);
      h.send(PacketKind.key, 1);
      const old = h.dec();
      old.emit(0);
      h.core.handleText(JSON.stringify({ type: 'status', state }));
      expect(h.core.collect()).toEqual([]);
      expect(h.core.tick()).toMatchObject({ width: 0, height: 0, clockRttMs: null });
      h.core.arm(1, { x: 0, y: 0, w: 1, h: 1 }, 5);
      expect(h.posted.filter((m) => m.type === 'armed')).toEqual([]);
      expect(old.closed).toBe(true);
      expect(h.clears.length).toBeGreaterThan(1);
    },
  );

  test('relay sidecars match only an adjacent seq and never use absolute relay timestamps', () => {
    const h = harness(true);
    h.send(PacketKind.config, 0);
    const sidecar = (seq: number) =>
      h.core.handleText(
        JSON.stringify({
          type: 'relay-frame',
          seq,
          recv_ms: 9e12,
          send_ms: -9e12,
          queue_ms: 0.4,
          dropped: 9,
        }),
      );
    sidecar(1);
    h.send(PacketKind.key, 1);
    h.dec().emit(0);
    expect(h.core.collect()[0]?.relayQueueMs).toBe(0.4);
    expect(h.core.tick()).toMatchObject({
      droppedRelay: 9,
      droppedUpstream: 0,
      relayQueueMsP50: 0.4,
      networkExcludingRelayMsP50: 0.6,
    });
    sidecar(8);
    h.send(PacketKind.delta, 2); // mismatch consumes it
    h.send(PacketKind.delta, 8);
    sidecar(4);
    h.core.handleText(JSON.stringify({ type: 'stats', dropped: 3 }));
    h.send(PacketKind.delta, 4); // intervening metadata consumes it
    for (let i = 1; i <= 3; i++) h.dec().emit(i);
    expect(h.core.collect().map((s) => s.relayQueueMs)).toEqual([null, null, null]);
    expect(h.core.tick()).toMatchObject({ droppedRelay: 9, droppedUpstream: 3 });
    sidecar(5);
    expect(() => h.core.handleBinary(new ArrayBuffer(1), 0)).toThrow();
    h.send(PacketKind.delta, 5);
    h.dec().emit(4);
    expect(h.core.collect()[0]?.relayQueueMs).toBeNull();
  });

  test.each(['waiting', 'disconnected'])(
    'relay %s clears sidecars and recovers only after CONFIG + KEY',
    (state) => {
      const h = harness(true);
      h.send(PacketKind.config, 0);
      calibrate(h.core);
      h.send(PacketKind.key, 1);
      h.dec().emit(0);
      h.core.handleText(JSON.stringify({ type: 'relay-frame', seq: 1, queue_ms: 2, dropped: 3 }));
      h.core.handleText(JSON.stringify({ type: 'relay-status', state }));
      expect(h.core.collect()).toEqual([]);
      expect(h.core.tick()).toMatchObject({ clockRttMs: null, width: 0, droppedRelay: 0 });
      h.core.handleText(JSON.stringify({ type: 'relay-status', state: 'connected' }));
      expect(h.posted.at(-1)).toMatchObject({ type: 'status', status: { state: 'bootstrapping' } });
      h.send(PacketKind.key, 1);
      h.send(PacketKind.config, 0);
      h.send(PacketKind.delta, 1);
      expect(h.dec().chunks).toEqual([]);
      h.send(PacketKind.key, 1);
      h.dec().emit(0);
      expect(h.core.collect()[0]).toMatchObject({ relayQueueMs: null, devMs: null });
      expect(h.posted.at(-1)).toMatchObject({ type: 'status', status: { state: 'streaming' } });
    },
  );

  test('sample storage is a bounded tail, not an unbounded soak history', () => {
    const h = harness();
    h.send(PacketKind.config, 0);
    for (let i = 1; i <= MAX_SAMPLES + 3; i++) {
      h.send(i === 1 ? PacketKind.key : PacketKind.delta, i);
      h.dec().emit(i - 1);
    }
    const samples = h.core.collect();
    expect(samples).toHaveLength(MAX_SAMPLES);
    expect(samples[0]?.seq).toBe(4);
    expect(samples.at(-1)?.seq).toBe(MAX_SAMPLES + 3);
    expect(h.core.collect()).toEqual([]);
    expect(h.core.tick().fps).toBeGreaterThan(100); // A bounded timing tail must not cap the frame count.
  });

  test('forwards upstream status and stats', () => {
    const h = harness();
    h.core.handleText(
      JSON.stringify({ type: 'status', state: 'error', message: 'offline', retry_in_ms: 500 }),
    );
    h.core.handleText(JSON.stringify({ type: 'stats', dropped: 4 }));
    h.core.handleText('not json');
    expect(h.posted[0]).toEqual({
      type: 'status',
      status: { state: 'upstream', upstream: 'error', message: 'offline', retryInMs: 500 },
    });
    expect(h.core.tick().droppedUpstream).toBe(4);
  });

  test('meanAbsDiff ignores alpha', () => {
    const a = new Uint8ClampedArray([0, 0, 0, 255]);
    const b = new Uint8ClampedArray([30, 30, 30, 0]);
    expect(meanAbsDiff(a, b)).toBe(30);
  });
});
