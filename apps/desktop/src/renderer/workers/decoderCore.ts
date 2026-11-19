import { codecFromConfig, concatBytes } from '../shared/stream/annexb';
import { networkWithoutRelay } from '../shared/stream/bench';
import { decodeEnvelope, PacketKind } from '../shared/stream/envelope';
import { percentile } from '../shared/stream/stats';
import type { FrameSample, FromWorker, Metrics, Roi } from '../shared/stream/workerProtocol';

/** Structural subsets of WebCodecs types so the core is testable with fakes. */
export interface FrameLike {
  readonly timestamp: number;
  readonly displayWidth: number;
  readonly displayHeight: number;
  close(): void;
}

export interface ChunkInit {
  type: 'key' | 'delta';
  timestamp: number;
  data: Uint8Array;
}

export interface DecoderLike {
  readonly decodeQueueSize: number;
  configure(config: { codec: string; optimizeForLatency: boolean }): void;
  decode(chunk: ChunkInit): void;
  reset(): void;
  close(): void;
}

export interface CoreDeps {
  createDecoder(output: (frame: FrameLike) => void, error: (err: Error) => void): DecoderLike;
  draw(frame: FrameLike): void;
  clear(): void;
  /** Downsample the already-drawn worker canvas, never the VideoFrame or main-thread canvas. */
  sampleRoi(roi: Roi): Uint8ClampedArray | null;
  now(): number;
  monotonic?(): number;
  post(msg: FromWorker): void;
}

/** Above this many frames waiting in the decoder, drop deltas until the next key frame. */
export const MAX_DECODE_QUEUE = 6;
const MAX_PENDING = 512;
export const MAX_SAMPLES = 50_000;
const MAX_CLOCK_AGE_MS = 15_000;
// Chromium's performance epoch and the host wall clock have separate precision/rounding.
// Do not reject a just-received calibration for a sub-5ms epoch discrepancy; larger jumps fail.
const CLOCK_EPOCH_TOLERANCE_MS = 5;

interface Armed {
  id: number;
  roi: Roi;
  threshold: number;
  baseline: Uint8ClampedArray | null;
}

interface Pending {
  sample: Omit<FrameSample, 'decodedMs' | 'drawnMs'>;
  clockEpoch: number;
  clockExpiresAt: number;
  decodeStartMono: number;
}

interface Clock {
  offsetUs: number;
  rttMs: number;
  atMs: number;
  expiresAt: number;
}

const finite = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

export function meanAbsDiff(a: Uint8ClampedArray, b: Uint8ClampedArray): number {
  const n = Math.min(a.length, b.length);
  if (n === 0) return 0;
  let sum = 0;
  let count = 0;
  for (let i = 0; i < n; i += 4) {
    // RGB only; alpha is constant
    sum +=
      Math.abs((a[i] ?? 0) - (b[i] ?? 0)) +
      Math.abs((a[i + 1] ?? 0) - (b[i + 1] ?? 0)) +
      Math.abs((a[i + 2] ?? 0) - (b[i + 2] ?? 0));
    count += 3;
  }
  return sum / count;
}

export class DecoderCore {
  private decoder: DecoderLike | null = null;
  private config: Uint8Array | null = null;
  private codec: string | null = null;
  private awaitingKey = true;
  private closed = false;
  private generation = 0;
  private sessionId: string | null = null;
  private captureOrientation: 'display' | 'natural' | null = null;
  private counter = 0;
  private readonly pending = new Map<number, Pending>();
  private clock: Clock | null = null;
  private clockEpoch = 0;
  private relayConnected = false;
  private relayFrame: { seq: number; queueMs: number; dropped: number } | null = null;
  private armed: Armed | null = null;
  private hasFrame = false;
  private samples: FrameSample[] = [];
  private sampleCursor = 0;
  private window: FrameSample[] = [];
  private windowFrames = 0;
  private windowBytes = 0;
  private windowStart: number;
  private width = 0;
  private height = 0;
  private droppedWorker = 0;
  private droppedUpstream = 0;
  private droppedRelay = 0;
  private framesTotal = 0;

  constructor(
    private readonly deps: CoreDeps,
    private readonly relay = false,
  ) {
    this.windowStart = deps.now();
  }

  get expectsRuntimeMetadata(): boolean {
    return !this.relay || this.relayConnected;
  }

  /** Heartbeat source, excluding frame sidecars; static streams still send Runtime stats. */
  handleText(raw: string): 'runtime' | 'relay' | false {
    if (this.closed) return false;
    this.relayFrame = null; // A sidecar belongs only to the immediately following binary message.
    let msg: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
      msg = parsed as Record<string, unknown>;
    } catch {
      return false;
    }
    switch (msg.type) {
      case 'hello': {
        const options = msg.options;
        const profile =
          typeof options === 'object' && options !== null
            ? (options as Record<string, unknown>).capture_orientation
            : null;
        this.captureOrientation = profile === 'display' || profile === 'natural' ? profile : null;
        return 'runtime';
      }
      case 'relay-heartbeat':
        if (this.relay && finite(msg.dropped) && msg.dropped >= 0) this.droppedRelay = msg.dropped;
        return 'relay';
      case 'session':
        if (typeof msg.session_id === 'string' && msg.session_id !== this.sessionId) {
          this.invalidate();
          this.sessionId = msg.session_id;
          this.bootstrapping();
        }
        this.captureOrientation =
          msg.capture_orientation === 'display' || msg.capture_orientation === 'natural'
            ? msg.capture_orientation
            : null;
        return 'runtime';
      case 'clock': {
        const now = this.deps.now();
        if (
          msg.valid === true &&
          !('error' in msg) &&
          finite(msg.offset_us) &&
          finite(msg.rtt_us) &&
          msg.rtt_us >= 0 &&
          finite(msg.at_ms) &&
          finite(msg.max_age_ms) &&
          msg.max_age_ms > 0 &&
          now + CLOCK_EPOCH_TOLERANCE_MS >= msg.at_ms &&
          now - msg.at_ms <= Math.min(msg.max_age_ms, MAX_CLOCK_AGE_MS)
        ) {
          this.clock = {
            offsetUs: msg.offset_us,
            rttMs: msg.rtt_us / 1000,
            atMs: msg.at_ms,
            expiresAt: msg.at_ms + Math.min(msg.max_age_ms, MAX_CLOCK_AGE_MS),
          };
        } else this.clearClock();
        return 'runtime';
      }
      case 'stats':
        if (finite(msg.dropped) && msg.dropped >= 0) this.droppedUpstream = msg.dropped;
        return 'runtime';
      case 'relay-frame':
        if (
          this.relay &&
          finite(msg.seq) &&
          Number.isSafeInteger(msg.seq) &&
          msg.seq >= 0 &&
          finite(msg.queue_ms) &&
          msg.queue_ms >= 0 &&
          finite(msg.dropped) &&
          msg.dropped >= 0
        ) {
          this.relayFrame = { seq: msg.seq, queueMs: msg.queue_ms, dropped: msg.dropped };
        }
        return false;
      case 'relay-status': {
        const state = msg.state;
        if (state !== 'waiting' && state !== 'connected' && state !== 'disconnected') return false;
        this.invalidate();
        this.relayConnected = state === 'connected';
        this.sessionId = null;
        this.captureOrientation = null;
        this.droppedRelay = 0;
        this.deps.post({
          type: 'status',
          status: {
            state: 'relay',
            relay: state,
            ...(typeof msg.message === 'string' ? { message: msg.message } : {}),
          },
        });
        if (state === 'connected') this.bootstrapping();
        return 'relay';
      }
      case 'status': {
        if (['starting', 'error', 'stopped'].includes(String(msg.state))) {
          this.invalidate();
          this.sessionId = null;
          this.captureOrientation = null;
        }
        const status: FromWorker = {
          type: 'status',
          status: { state: 'upstream', upstream: String(msg.state) },
        };
        if (status.status.state === 'upstream') {
          if (typeof msg.message === 'string') status.status.message = msg.message;
          if (typeof msg.retry_in_ms === 'number') status.status.retryInMs = msg.retry_in_ms;
        }
        this.deps.post(status);
        return 'runtime';
      }
      default:
        return false;
    }
  }

  handleBinary(buf: ArrayBuffer, guiRecvMs: number): void {
    if (this.closed) return;
    const sidecar = this.relayFrame;
    this.relayFrame = null;
    const env = decodeEnvelope(buf);
    const matched = sidecar?.seq === env.seq ? sidecar : null;
    if (matched) this.droppedRelay = matched.dropped;
    if (env.kind === PacketKind.config) {
      this.onConfig(env.payload);
      return;
    }
    const key = env.kind === PacketKind.key;
    const decoder = this.decoder;
    if (!decoder || !this.config) {
      this.droppedWorker += 1;
      return;
    }
    if (!key && (this.awaitingKey || decoder.decodeQueueSize > MAX_DECODE_QUEUE)) {
      this.awaitingKey = true;
      this.droppedWorker += 1;
      return;
    }
    this.awaitingKey = false;
    const ts = this.counter++;
    const start = this.deps.now();
    const clock = this.validClock(start);
    this.pending.set(ts, {
      sample: {
        timingResolutionMs: 1,
        seq: env.seq,
        ptsUs: env.ptsUs,
        key,
        bytes: env.payload.byteLength,
        devMs: clock ? (env.ptsUs + clock.offsetUs) / 1000 : null,
        clockRttMs: clock?.rttMs ?? null,
        relayQueueMs: this.relay ? (matched?.queueMs ?? null) : 0,
        rtRecvMs: env.rtRecvMs,
        rtSendMs: env.rtSendMs,
        guiRecvMs,
        decodeStartMs: start,
      },
      clockEpoch: this.clockEpoch,
      clockExpiresAt: clock?.expiresAt ?? 0,
      decodeStartMono: this.deps.monotonic?.() ?? start,
    });
    if (this.pending.size > MAX_PENDING) {
      const oldest = this.pending.keys().next().value;
      if (oldest !== undefined) this.pending.delete(oldest);
    }
    // In-band SPS/PPS must precede every IDR for Annex-B decoding (no `description`).
    const data = key ? concatBytes(this.config, env.payload) : env.payload;
    try {
      decoder.decode({ type: key ? 'key' : 'delta', timestamp: ts, data });
    } catch (err) {
      this.onDecoderError(err instanceof Error ? err : new Error(String(err)));
    }
  }

  /** Baseline = ROI of the last drawn frame (scrcpy sends no frames while the screen is static). */
  arm(id: number, roi: Roi, threshold: number): void {
    if (
      this.closed ||
      ![roi.x, roi.y, roi.w, roi.h, threshold].every(Number.isFinite) ||
      roi.x < 0 ||
      roi.y < 0 ||
      roi.w <= 0 ||
      roi.h <= 0 ||
      roi.x + roi.w > 1 ||
      roi.y + roi.h > 1 ||
      threshold < 0
    ) {
      this.deps.post({ type: 'armed', id, ready: false });
      return;
    }
    const baseline = this.hasFrame ? this.deps.sampleRoi(roi) : null;
    this.armed = { id, roi, threshold, baseline };
    if (baseline) this.deps.post({ type: 'armed', id, ready: true });
  }

  disarm(id: number): void {
    if (this.armed?.id === id) this.armed = null;
  }

  collect(): FrameSample[] {
    const out = this.samples
      .slice(this.sampleCursor)
      .concat(this.samples.slice(0, this.sampleCursor));
    this.samples = [];
    this.sampleCursor = 0;
    return out;
  }

  /** Emit metrics for the window since the previous tick. */
  tick(): Metrics {
    const now = this.deps.now();
    const clock = this.validClock(now);
    const secs = Math.max((now - this.windowStart) / 1000, 1e-3);
    const w = this.window;
    const decode = w.map((s) => s.decodedMs - s.decodeStartMs).sort((a, b) => a - b);
    const e2e = (clock ? w : [])
      .filter((s) => s.devMs !== null)
      .map((s) => s.drawnMs - (s.devMs ?? 0))
      .sort((a, b) => a - b);
    const net = w.map((s) => s.guiRecvMs - s.rtSendMs).sort((a, b) => a - b);
    const relay = w
      .flatMap((s) => (s.relayQueueMs === null ? [] : [s.relayQueueMs]))
      .sort((a, b) => a - b);
    const network = w
      .map(networkWithoutRelay)
      .filter(Number.isFinite)
      .sort((a, b) => a - b);
    const metrics: Metrics = {
      fps: this.windowFrames / secs,
      decodeMsMean: decode.length ? decode.reduce((a, b) => a + b, 0) / decode.length : NaN,
      decodeMsMax: decode.length ? (decode[decode.length - 1] ?? NaN) : NaN,
      decodeQueue: this.decoder?.decodeQueueSize ?? 0,
      droppedWorker: this.droppedWorker,
      droppedUpstream: this.droppedUpstream,
      droppedRelay: this.droppedRelay,
      e2eMsP50: percentile(e2e, 50),
      netMsP50: percentile(net, 50),
      relayQueueMsP50: percentile(relay, 50),
      networkExcludingRelayMsP50: percentile(network, 50),
      kbps: (this.windowBytes * 8) / 1000 / secs,
      width: this.width,
      height: this.height,
      codec: this.codec,
      clockRttMs: clock?.rttMs ?? null,
      framesTotal: this.framesTotal,
    };
    this.window = [];
    this.windowFrames = 0;
    this.windowBytes = 0;
    this.windowStart = now;
    this.deps.post({ type: 'metrics', metrics });
    return metrics;
  }

  close(): void {
    this.closed = true;
    this.invalidate();
  }

  private onConfig(payload: Uint8Array): void {
    // CONFIG is a continuity boundary even when a restarted encoder reuses identical SPS/PPS.
    this.resetFrames();
    this.bootstrapping();
    const codec = codecFromConfig(payload);
    if (!codec) {
      this.deps.post({ type: 'error', message: 'config packet without SPS' });
      return;
    }
    this.config = payload.slice();
    this.codec = codec;
    const generation = this.generation;
    try {
      this.decoder = this.deps.createDecoder(
        (f) => this.onFrame(f),
        (e) => {
          if (generation === this.generation) this.onDecoderError(e);
        },
      );
      this.decoder.configure({ codec, optimizeForLatency: true });
    } catch (error) {
      this.resetFrames();
      this.deps.post({ type: 'error', message: `decoder configure: ${String(error)}` });
    }
    this.awaitingKey = true;
  }

  private onDecoderError(err: Error): void {
    this.deps.post({ type: 'error', message: `decoder: ${err.message}` });
    const config = this.config;
    if (config) this.onConfig(config);
  }

  private onFrame(frame: FrameLike): void {
    const p = this.pending.get(frame.timestamp);
    this.pending.delete(frame.timestamp);
    try {
      if (!p || this.closed) return; // Never draw a late frame from the preceding CONFIG/session.
      const decodedMs =
        p.sample.decodeStartMs + (this.deps.monotonic?.() ?? this.deps.now()) - p.decodeStartMono;
      this.width = frame.displayWidth;
      this.height = frame.displayHeight;
      this.deps.draw(frame);
      const drawnMs =
        p.sample.decodeStartMs + (this.deps.monotonic?.() ?? this.deps.now()) - p.decodeStartMono;
      this.deps.post({
        type: 'frame',
        frame: {
          sessionId: this.sessionId,
          configId: this.generation,
          seq: p.sample.seq,
          width: frame.displayWidth,
          height: frame.displayHeight,
          drawnAtMs: drawnMs,
          captureOrientation: this.captureOrientation,
        },
      });
      const valid =
        this.validClock(drawnMs) !== null &&
        p.clockEpoch === this.clockEpoch &&
        drawnMs <= p.clockExpiresAt;
      const sample: FrameSample = {
        ...p.sample,
        devMs: valid ? p.sample.devMs : null,
        clockRttMs: valid ? p.sample.clockRttMs : null,
        decodedMs,
        drawnMs,
      };
      this.framesTotal += 1;
      this.windowFrames += 1;
      this.window.push(sample);
      if (this.window.length > MAX_PENDING) this.window.shift();
      this.windowBytes += sample.bytes;
      if (this.samples.length < MAX_SAMPLES) this.samples.push(sample);
      else {
        this.samples[this.sampleCursor] = sample;
        this.sampleCursor = (this.sampleCursor + 1) % MAX_SAMPLES;
      }
      if (!this.hasFrame) {
        this.hasFrame = true;
        this.deps.post({
          type: 'status',
          status: { state: 'streaming', ...(this.sessionId ? { sessionId: this.sessionId } : {}) },
        });
      }
      this.detect(sample);
    } finally {
      frame.close();
    }
  }

  private detect(sample: FrameSample): void {
    const armed = this.armed;
    if (!armed) return;
    const px = this.deps.sampleRoi(armed.roi);
    if (!px) return;
    if (!armed.baseline) {
      armed.baseline = px;
      this.deps.post({ type: 'armed', id: armed.id, ready: true });
      return;
    }
    const diff = meanAbsDiff(armed.baseline, px);
    if (diff > armed.threshold) {
      this.armed = null;
      this.deps.post({ type: 'change', id: armed.id, sample, diff });
    }
  }

  private bootstrapping(): void {
    this.deps.post({
      type: 'status',
      status: { state: 'bootstrapping', ...(this.sessionId ? { sessionId: this.sessionId } : {}) },
    });
  }

  private clearClock(): void {
    this.clock = null;
    this.clockEpoch += 1;
  }

  private validClock(now: number): Clock | null {
    if (
      this.clock &&
      (now + CLOCK_EPOCH_TOLERANCE_MS < this.clock.atMs || now > this.clock.expiresAt)
    )
      this.clearClock();
    return this.clock;
  }

  private invalidate(): void {
    this.clearClock();
    this.resetFrames();
  }

  private resetFrames(): void {
    this.generation += 1;
    this.decoder?.close();
    this.decoder = null;
    this.config = null;
    this.codec = null;
    this.pending.clear();
    this.relayFrame = null;
    this.awaitingKey = true;
    this.armed = null;
    this.hasFrame = false;
    this.width = 0;
    this.height = 0;
    this.samples = [];
    this.sampleCursor = 0;
    this.window = [];
    this.windowFrames = 0;
    this.windowBytes = 0;
    this.windowStart = this.deps.now();
    this.deps.clear();
  }
}
