import type { RuntimeConnection } from '../../../shared/ipc';
import { withTimeout } from './timeout';
import type {
  FrameSample,
  FromWorker,
  Roi,
  StreamStatus,
  ToWorker,
  WorkerMessage,
} from './workerProtocol';
import { interruptsStream, isFromWorker } from './workerProtocol';

export interface StreamOptions {
  maxSize: number;
  bitRate: number;
  maxFps: number;
  captureOrientation?: 'display' | 'natural';
}

export const DEFAULT_STREAM_OPTIONS: StreamOptions = {
  maxSize: 1600,
  bitRate: 4_000_000,
  maxFps: 60,
};

export function runtimeStreamUrl(c: RuntimeConnection, serial: string, o: StreamOptions): string {
  const q = new URLSearchParams({
    token: c.token,
    max_size: String(o.maxSize),
    bit_rate: String(o.bitRate),
    max_fps: String(o.maxFps),
  });
  if (o.captureOrientation) q.set('capture_orientation', o.captureOrientation);
  return `ws://127.0.0.1:${c.port}/ws/stream/${encodeURIComponent(serial)}?${q.toString()}`;
}

export function relaySubscribeUrl(relayUrl: string, channel: string, token: string): string {
  const base = relayUrl.replace(/\/+$/, '');
  return `${base}/sub/${encodeURIComponent(channel)}?token=${encodeURIComponent(token)}`;
}

export interface WorkerLike {
  postMessage(msg: ToWorker, transfer?: Transferable[]): void;
  onmessage: ((ev: MessageEvent<unknown>) => void) | null;
  onerror: ((ev: ErrorEvent) => void) | null;
  onmessageerror: ((ev: MessageEvent<unknown>) => void) | null;
  terminate(): void;
}

export interface StreamViewDeps {
  createWorker(): WorkerLike;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

export const defaultStreamDeps: StreamViewDeps = {
  createWorker: () =>
    new Worker(new URL('../../workers/video-decoder.worker.ts', import.meta.url), {
      type: 'module',
    }) as unknown as WorkerLike,
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
};

const MIN_BACKOFF = 500;
const MAX_BACKOFF = 5000;
export const WORKER_TIMEOUT_MS = 7000;
export const COLLECT_TIMEOUT_MS = 5000;
const MAX_EVENTS = 256;

export interface StreamEvent {
  atMs: number;
  status: StreamStatus;
}

interface ChangeWaiter {
  id: number;
  ready(value: boolean): void;
  finish(sample: FrameSample | null): void;
}

/**
 * Main-thread handle of one live view. Only control messages cross the worker boundary; the
 * worker owns the socket, decoder and canvas. Reconnects with backoff, re-resolving the URL each
 * time (the Runtime may restart with a new port/token).
 */
export class StreamView {
  private worker: WorkerLike | null = null;
  private createCanvas: (() => OffscreenCanvas) | null = null;
  private resolveUrl: (() => Promise<string | null>) | null = null;
  private timer: unknown = null;
  private watchdog: unknown = null;
  private connectionId = 0;
  private relay = false;
  private available = false;
  private backoff = MIN_BACKOFF;
  private nextRequest = 1;
  private changeWaiter: ChangeWaiter | null = null;
  private readonly sampleWaiters = new Map<
    number,
    { resolve(s: FrameSample[]): void; reject(e: Error): void }
  >();
  private eventLog: StreamEvent[] = [];
  private lastStatus = '';

  constructor(
    private readonly deps: StreamViewDeps,
    private readonly onEvent: (msg: FromWorker) => void,
  ) {}

  /** A replacement canvas permits recovery from a crashed/hung worker without touching frames. */
  attach(createCanvas: () => OffscreenCanvas): void {
    this.createCanvas = createCanvas;
    this.replaceWorker();
  }

  start(resolveUrl: () => Promise<string | null>, relay = false): void {
    if (!this.worker) throw new Error('stream view not attached');
    this.stop();
    this.resolveUrl = resolveUrl;
    this.relay = relay;
    this.backoff = MIN_BACKOFF;
    void this.connect();
  }

  stop(): void {
    this.connectionId += 1;
    this.resolveUrl = null;
    this.available = false;
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    this.clearWatchdog();
    this.rejectWaiters('stream stopped');
    this.worker?.postMessage({ type: 'disconnect' });
    this.emit({ type: 'status', status: { state: 'stopped' } });
  }

  dispose(): void {
    this.stop();
    this.worker?.terminate();
    this.worker = null;
    this.createCanvas = null;
  }

  /** `ready` acknowledges a canvas baseline before any trigger may be sent. */
  waitForChange(
    roi: Roi,
    threshold: number,
    timeoutMs: number,
  ): {
    id: number;
    ready: Promise<boolean>;
    done: Promise<FrameSample | null>;
    cancel(): void;
  } {
    this.changeWaiter?.finish(null);
    const id = this.nextRequest++;
    if (!this.available || !this.worker) {
      return {
        id,
        ready: Promise.resolve(false),
        done: Promise.resolve(null),
        cancel: () => undefined,
      };
    }
    let readyResolve: (value: boolean) => void = () => undefined;
    const ready = new Promise<boolean>((resolve) => {
      readyResolve = resolve;
    });
    const done = new Promise<FrameSample | null>((resolve) => {
      const timer = this.deps.setTimer(
        () => this.changeWaiter?.id === id && this.changeWaiter.finish(null),
        timeoutMs,
      );
      this.changeWaiter = {
        id,
        ready: readyResolve,
        finish: (sample) => {
          this.deps.clearTimer(timer);
          if (this.changeWaiter?.id === id) this.changeWaiter = null;
          this.worker?.postMessage({ type: 'disarm', id });
          readyResolve(false);
          resolve(sample);
        },
      };
    });
    this.worker.postMessage({ type: 'arm', id, roi, threshold });
    return {
      id,
      ready,
      done,
      cancel: () => {
        if (this.changeWaiter?.id === id) this.changeWaiter.finish(null);
      },
    };
  }

  collect(timeoutMs = COLLECT_TIMEOUT_MS): Promise<FrameSample[]> {
    if (!this.available || !this.worker)
      return Promise.reject(new Error('stream not connected/ready'));
    const id = this.nextRequest++;
    return new Promise((resolve, reject) => {
      const timer = this.deps.setTimer(() => {
        this.sampleWaiters.delete(id);
        reject(new Error('collect timeout'));
      }, timeoutMs);
      this.sampleWaiters.set(id, {
        resolve: (samples) => {
          this.deps.clearTimer(timer);
          resolve(samples);
        },
        reject: (error) => {
          this.deps.clearTimer(timer);
          reject(error);
        },
      });
      this.worker?.postMessage({ type: 'collect', id });
    });
  }

  /** Drain only new state transitions, bounded independently of frame samples. */
  events(): StreamEvent[] {
    const events = this.eventLog;
    this.eventLog = [];
    return events;
  }

  private handle(msg: WorkerMessage): void {
    if (!this.resolveUrl || msg.connectionId !== this.connectionId) return;
    this.clearWatchdog();
    const connectionId = this.connectionId;
    this.watchdog = this.deps.setTimer(() => {
      if (this.connectionId === connectionId) this.failConnection('worker heartbeat timeout', true);
    }, WORKER_TIMEOUT_MS);
    if (msg.type === 'armed') {
      if (this.changeWaiter?.id === msg.id) {
        this.changeWaiter.ready(msg.ready);
        if (!msg.ready) this.changeWaiter.finish(null);
      }
    } else if (msg.type === 'change') {
      if (this.changeWaiter?.id === msg.id) this.changeWaiter.finish(msg.sample);
    } else if (msg.type === 'samples') {
      const waiter = this.sampleWaiters.get(msg.id);
      this.sampleWaiters.delete(msg.id);
      waiter?.resolve(msg.samples);
    } else if (msg.type === 'status') {
      if (msg.status.state === 'open') this.backoff = MIN_BACKOFF;
      if (msg.status.state === 'streaming') this.available = true;
      if (interruptsStream(msg.status)) {
        this.available = false;
        this.rejectWaiters('stream interrupted');
      }
      if (msg.status.state === 'closed') {
        this.clearWatchdog();
        this.connectionId += 1;
        this.scheduleReconnect();
      }
    }
    this.emit(msg);
  }

  private scheduleReconnect(): void {
    if (!this.resolveUrl || this.timer !== null) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF);
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.connect();
    }, delay);
  }

  private async connect(): Promise<void> {
    const resolve = this.resolveUrl;
    if (!resolve) return;
    const connectionId = ++this.connectionId;
    this.emit({ type: 'status', status: { state: 'connecting' } });
    const url = await withTimeout(
      Promise.resolve().then(resolve),
      5000,
      'runtime connection timeout',
    ).catch(() => null);
    if (this.connectionId !== connectionId || !this.resolveUrl) return;
    if (!url) {
      this.emit({
        type: 'status',
        status: { state: 'closed', code: 0, reason: 'runtime not running' },
      });
      this.scheduleReconnect();
      return;
    }
    this.watchdog = this.deps.setTimer(() => {
      if (this.connectionId === connectionId) this.failConnection('worker heartbeat timeout', true);
    }, WORKER_TIMEOUT_MS);
    this.worker?.postMessage({ type: 'connect', url, connectionId, relay: this.relay });
  }

  private replaceWorker(): void {
    this.worker?.terminate();
    const canvas = this.createCanvas?.();
    if (!canvas) throw new Error('stream canvas unavailable');
    const worker = this.deps.createWorker();
    this.worker = worker;
    worker.onmessage = (ev) => {
      if (this.worker === worker && isFromWorker(ev.data)) this.handle(ev.data);
    };
    worker.onerror = (ev) => {
      if (this.worker === worker) this.failConnection(`worker error: ${ev.message}`, true);
    };
    worker.onmessageerror = () => {
      if (this.worker === worker) this.failConnection('worker message error', true);
    };
    worker.postMessage({ type: 'init', canvas }, [canvas]);
  }

  private failConnection(reason: string, replaceWorker: boolean): void {
    if (!this.resolveUrl) return;
    this.connectionId += 1;
    this.available = false;
    this.clearWatchdog();
    this.rejectWaiters(reason);
    if (replaceWorker) this.replaceWorker();
    this.emit({ type: 'status', status: { state: 'closed', code: 0, reason } });
    this.scheduleReconnect();
  }

  private clearWatchdog(): void {
    if (this.watchdog !== null) this.deps.clearTimer(this.watchdog);
    this.watchdog = null;
  }

  private rejectWaiters(message: string): void {
    this.changeWaiter?.finish(null);
    for (const waiter of this.sampleWaiters.values()) waiter.reject(new Error(message));
    this.sampleWaiters.clear();
  }

  private emit(msg: FromWorker): void {
    if (msg.type === 'status') {
      const key = JSON.stringify(msg.status);
      if (key !== this.lastStatus) {
        this.lastStatus = key;
        this.eventLog.push({ atMs: Date.now(), status: msg.status });
        if (this.eventLog.length > MAX_EVENTS) this.eventLog.shift();
      }
    }
    this.onEvent(msg);
  }
}
