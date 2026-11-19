import type { RuntimeConnection } from '../../../shared/ipc';
import { RuntimeApiClient } from './runtime.generated';

export interface RuntimeHealth {
  status: 'ok';
  version: string;
  pid: number;
  uptime_s: number;
}

export type RuntimeState =
  | { kind: 'searching' }
  | { kind: 'not-running'; retryInMs: number }
  | { kind: 'connected'; port: number; pid: number; version: string; lastBeat: number; seq: number }
  | { kind: 'disconnected'; reason: string; retryInMs: number };

export interface RuntimeMonitorDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  fetchHealth(conn: RuntimeConnection): Promise<RuntimeHealth>;
  openSocket(url: string): WebSocket;
  now(): number;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
}

export const baseUrl = (c: RuntimeConnection) => `http://127.0.0.1:${c.port}`;
export const heartbeatUrl = (c: RuntimeConnection) =>
  `ws://127.0.0.1:${c.port}/ws/heartbeat?token=${encodeURIComponent(c.token)}`;

/** Generated OpenAPI client bound to the loopback endpoint and bearer token from `runtime.json`. */
export const createRuntimeClient = (
  connection: RuntimeConnection,
  fetchImpl?: typeof fetch,
): RuntimeApiClient =>
  new RuntimeApiClient({
    baseUrl: baseUrl(connection),
    token: connection.token,
    ...(fetchImpl ? { fetch: fetchImpl } : {}),
  });

export async function fetchHealth(conn: RuntimeConnection): Promise<RuntimeHealth> {
  const res = await fetch(`${baseUrl(conn)}/health`, {
    headers: { Authorization: `Bearer ${conn.token}` },
  });
  if (!res.ok) throw new Error(`health HTTP ${res.status}`);
  return (await res.json()) as RuntimeHealth;
}

export const defaultDeps = (getConnection: RuntimeMonitorDeps['getConnection']) =>
  ({
    getConnection,
    fetchHealth,
    openSocket: (url) => new WebSocket(url),
    now: () => Date.now(),
    setTimer: (fn, ms) => setTimeout(fn, ms),
    clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }) satisfies RuntimeMonitorDeps;

const MIN_BACKOFF = 500;
const MAX_BACKOFF = 5000;

/**
 * Keeps a live heartbeat WS to the Runtime. On any failure it re-reads runtime.json via the
 * preload bridge (port/token change when the Runtime restarts) and reconnects with backoff.
 */
export class RuntimeMonitor {
  private socket: WebSocket | null = null;
  private timer: unknown = null;
  private backoff = MIN_BACKOFF;
  private stopped = false;

  constructor(
    private readonly deps: RuntimeMonitorDeps,
    private readonly onState: (s: RuntimeState) => void,
  ) {}

  start(): void {
    this.stopped = false;
    this.onState({ kind: 'searching' });
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    const s = this.socket;
    this.socket = null;
    s?.close();
  }

  private retry(state: (retryInMs: number) => RuntimeState): void {
    if (this.stopped) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF);
    this.onState(state(delay));
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.connect();
    }, delay);
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    const conn = await this.deps.getConnection().catch(() => null);
    if (this.stopped) return;
    if (!conn) {
      this.retry((retryInMs) => ({ kind: 'not-running', retryInMs }));
      return;
    }
    let health: RuntimeHealth;
    try {
      health = await this.deps.fetchHealth(conn);
    } catch (err) {
      this.retry((retryInMs) => ({ kind: 'disconnected', reason: String(err), retryInMs }));
      return;
    }
    if (this.stopped) return;

    const ws = this.deps.openSocket(heartbeatUrl(conn));
    this.socket = ws;
    ws.onmessage = (ev: MessageEvent) => {
      const msg = JSON.parse(String(ev.data)) as { type?: string; seq?: number };
      if (msg.type !== 'heartbeat') return;
      this.backoff = MIN_BACKOFF;
      this.onState({
        kind: 'connected',
        port: conn.port,
        pid: health.pid,
        version: health.version,
        lastBeat: this.deps.now(),
        seq: msg.seq ?? 0,
      });
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.socket !== ws) return;
      this.socket = null;
      this.retry((retryInMs) => ({
        kind: 'disconnected',
        reason: `ws closed (${ev.code})`,
        retryInMs,
      }));
    };
  }
}
