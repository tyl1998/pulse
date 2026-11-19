import type { RuntimeConnection } from '../../../shared/ipc';
import { baseUrl } from './runtimeClient';
import { runtimeEventStreamUrl, type PulseRuntimeApiSchemas } from './runtime.generated';

export type RuntimeEventEnvelope = PulseRuntimeApiSchemas.EventEnvelope;

export type EventStreamState =
  | { kind: 'connecting' }
  | { kind: 'not-running'; retryInMs: number }
  | {
      kind: 'connected';
      since: number;
      oldestSeq: number;
      latestSeq: number;
      replayed: number;
    }
  | { kind: 'disconnected'; reason: string; retryInMs: number };

export interface EventGap {
  /** First sequence number the subscriber can no longer obtain. */
  fromSeq: number;
  /** Last unavailable sequence number. */
  toSeq: number;
  oldestSeq: number;
  latestSeq: number;
}

export type EventTypeFilter = readonly string[] | ((type: string) => boolean);

export interface EventSubscriptionOptions {
  /**
   * Initial last sequence number. The first request uses it as the replay baseline (`since`),
   * so 0 replays the whole retained buffer. Reconnects use the subscription's last seen seq
   * within the same Runtime instance, or 0 for a new instance.
   */
  since?: number;
  runId?: string | null;
  types?: EventTypeFilter;
  onState?: (state: EventStreamState) => void;
  /** Called when the Runtime buffer no longer covers the requested replay window. */
  onGap?: (gap: EventGap) => void;
}

export type RuntimeEventListener = (event: RuntimeEventEnvelope) => void;

export interface EventSubscription {
  /** Last sequence number observed on this subscription (including filtered-out types). */
  readonly lastSeq: number;
  readonly state: EventStreamState;
  close(): void;
}

export interface EventBusDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  openSocket(url: string): WebSocket;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

export const defaultEventBusDeps = (
  getConnection: EventBusDeps['getConnection'],
): EventBusDeps => ({
  getConnection,
  openSocket: (url) => new WebSocket(url),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
});

const MIN_BACKOFF = 500;
const MAX_BACKOFF = 5000;
const MAX_DEDUPE_IDS = 4096;

interface HelloBody {
  oldest_seq?: unknown;
  latest_seq?: unknown;
  replayed?: unknown;
  gap?: unknown;
}

function isEnvelope(message: unknown): message is RuntimeEventEnvelope {
  if (!message || typeof message !== 'object') return false;
  const value = message as Record<string, unknown>;
  return (
    typeof value.type === 'string' &&
    typeof value.msg_id === 'string' &&
    typeof value.seq === 'number' &&
    Number.isFinite(value.seq) &&
    value.seq >= 0 &&
    typeof value.body === 'object' &&
    value.body !== null
  );
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

class Subscription implements EventSubscription {
  private socket: WebSocket | null = null;
  private timer: unknown = null;
  private backoff = MIN_BACKOFF;
  private closed = false;
  private seenIds = new Set<string>();
  private seenOrder: string[] = [];
  private lastSequence: number;
  private runtimeIdentity: Pick<RuntimeConnection, 'pid' | 'token'> | null = null;
  private readonly contiguousSeq: boolean;
  private currentState: EventStreamState = { kind: 'connecting' };

  constructor(
    private readonly deps: EventBusDeps,
    private readonly options: EventSubscriptionOptions,
    private readonly listener: RuntimeEventListener,
  ) {
    this.lastSequence = options.since ?? 0;
    // A server-side run_id filter drops other runs' sequence numbers, so seq gaps are expected
    // and the unfiltered buffer gap flag cannot be interpreted for this subscription.
    this.contiguousSeq = typeof options.runId !== 'string';
    this.currentState = { kind: 'connecting' };
  }

  get lastSeq(): number {
    return this.lastSequence;
  }

  get state(): EventStreamState {
    return this.currentState;
  }

  start(): void {
    this.closed = false;
    this.setState({ kind: 'connecting' });
    void this.connect();
  }

  close(): void {
    this.closed = true;
    if (this.timer !== null) {
      this.deps.clearTimer(this.timer);
      this.timer = null;
    }
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }

  private setState(state: EventStreamState): void {
    this.currentState = state;
    this.notify(() => this.options.onState?.(state));
  }

  private notify(fn: () => void): void {
    try {
      fn();
    } catch {
      // A subscriber callback must not kill the transport or other subscribers.
    }
  }

  private retry(state: (retryInMs: number) => EventStreamState): void {
    if (this.closed) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF);
    this.setState(state(delay));
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      if (!this.closed) void this.connect();
    }, delay);
  }

  private async connect(): Promise<void> {
    if (this.closed) return;
    this.setState({ kind: 'connecting' });
    const connection = await this.deps.getConnection().catch(() => null);
    if (this.closed) return;
    if (!connection) {
      this.retry((retryInMs) => ({ kind: 'not-running', retryInMs }));
      return;
    }
    const instanceChanged =
      this.runtimeIdentity !== null &&
      (this.runtimeIdentity.pid !== connection.pid ||
        this.runtimeIdentity.token !== connection.token);
    const since = instanceChanged ? 0 : this.lastSequence;
    const streamOptions =
      typeof this.options.runId === 'string'
        ? {
            baseUrl: baseUrl(connection),
            token: connection.token,
            since,
            runId: this.options.runId,
          }
        : { baseUrl: baseUrl(connection), token: connection.token, since };
    let socket: WebSocket;
    try {
      socket = this.deps.openSocket(runtimeEventStreamUrl(streamOptions));
    } catch (error) {
      this.retry((retryInMs) => ({
        kind: 'disconnected',
        reason: String(error),
        retryInMs,
      }));
      return;
    }
    if (instanceChanged) {
      this.lastSequence = 0;
      this.seenIds.clear();
      this.seenOrder.length = 0;
    }
    this.runtimeIdentity = { pid: connection.pid, token: connection.token };
    this.socket = socket;
    socket.onmessage = (event: MessageEvent) => {
      if (this.socket !== socket) return;
      this.receive(String(event.data));
    };
    socket.onerror = () => {
      // Browsers normally follow an error with close; closing explicitly keeps the retry path
      // deterministic for embedded WebSocket implementations that do not.
      if (this.socket === socket) socket.close();
    };
    socket.onclose = (event: CloseEvent) => {
      if (this.socket !== socket) return;
      this.socket = null;
      this.retry((retryInMs) => ({
        kind: 'disconnected',
        reason: `ws closed (${event.code})`,
        retryInMs,
      }));
    };
  }

  private receive(raw: string): void {
    let message: unknown;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (!message || typeof message !== 'object') return;
    if ((message as { type?: unknown }).type === 'stream.hello') {
      this.onHello((message as { body?: HelloBody }).body);
      return;
    }
    if (!isEnvelope(message)) return;
    if (message.seq <= this.lastSequence) return;
    this.backoff = MIN_BACKOFF;
    if (this.seenIds.has(message.msg_id)) {
      this.lastSequence = message.seq;
      return;
    }

    if (this.contiguousSeq && this.lastSequence > 0 && message.seq > this.lastSequence + 1) {
      this.notify(() =>
        this.options.onGap?.({
          fromSeq: this.lastSequence + 1,
          toSeq: message.seq - 1,
          oldestSeq: this.lastSequence + 1,
          latestSeq: message.seq - 1,
        }),
      );
    }

    this.lastSequence = message.seq;
    this.remember(message.msg_id);
    if (this.options.runId != null && message.run_id !== this.options.runId) return;
    if (!this.matchesType(message.type)) return;
    this.notify(() => this.listener(message));
  }

  private onHello(body: HelloBody | undefined): void {
    this.backoff = MIN_BACKOFF;
    const oldestSeq = numberOr(body?.oldest_seq, 0);
    const latestSeq = numberOr(body?.latest_seq, this.lastSequence);
    const replayed = numberOr(body?.replayed, 0);
    if (this.contiguousSeq && body?.gap === true && oldestSeq > this.lastSequence + 1) {
      const gap: EventGap = {
        fromSeq: this.lastSequence + 1,
        toSeq: oldestSeq - 1,
        oldestSeq,
        latestSeq,
      };
      this.notify(() => this.options.onGap?.(gap));
      // The retained buffer starts at oldestSeq; skipping to oldestSeq - 1 keeps replay ordered
      // while the gap callback lets the consumer repair state from a snapshot instead.
      this.lastSequence = oldestSeq - 1;
    }
    this.setState({
      kind: 'connected',
      since: this.lastSequence,
      oldestSeq,
      latestSeq,
      replayed,
    });
  }

  private matchesType(type: string): boolean {
    const types = this.options.types;
    if (types === undefined) return true;
    if (typeof types === 'function') return types(type);
    return types.includes(type);
  }

  private remember(msgId: string): void {
    if (this.seenIds.has(msgId)) return;
    this.seenIds.add(msgId);
    this.seenOrder.push(msgId);
    while (this.seenOrder.length > MAX_DEDUPE_IDS) {
      const oldest = this.seenOrder.shift();
      if (oldest !== undefined) this.seenIds.delete(oldest);
    }
  }
}

/**
 * Fan-out over the Runtime event stream. Each subscription keeps its own last `seq`, reconnects
 * with `since=<lastSeq>`, and de-duplicates `msg_id`; one broken subscription cannot stall the
 * others.
 */
export class EventBus {
  private readonly subscriptions = new Set<Subscription>();
  private stopped = false;

  constructor(private readonly deps: EventBusDeps) {}

  subscribe(options: EventSubscriptionOptions, listener: RuntimeEventListener): EventSubscription {
    if (this.stopped) throw new Error('EventBus is closed');
    const subscription = new Subscription(this.deps, options, listener);
    this.subscriptions.add(subscription);
    subscription.start();
    return subscription;
  }

  close(): void {
    this.stopped = true;
    for (const subscription of this.subscriptions) subscription.close();
    this.subscriptions.clear();
  }
}
