import type { RuntimeConnection } from '../../../shared/ipc';
import { baseUrl } from '../../shared/api/runtimeClient';
import {
  relaySubscribeUrl,
  runtimeStreamUrl,
  type StreamOptions,
  type StreamView,
} from '../../shared/stream/streamClient';
import { withTimeout } from '../../shared/stream/timeout';

export interface StreamConfig extends StreamOptions {
  serial: string;
  mode: 'direct' | 'relay';
  relayUrl: string;
  channel: string;
  relayToken: string;
}

export const DEFAULT_CONFIG: StreamConfig = {
  serial: '',
  mode: 'direct',
  maxSize: 1600,
  bitRate: 4_000_000,
  maxFps: 60,
  relayUrl: 'ws://127.0.0.1:7788',
  channel: 'dev',
  relayToken: '',
};
export const LEASE_RENEW_MS = 5000;
const REQUEST_TIMEOUT_MS = 5000;

export interface StreamSessionDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  request(conn: RuntimeConnection, path: string, init: RequestInit): Promise<unknown>;
  setTimer(fn: () => void, ms: number): unknown;
  clearTimer(timer: unknown): void;
}

async function runtimeRequest(
  conn: RuntimeConnection,
  path: string,
  init: RequestInit,
): Promise<unknown> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (init.signal?.aborted) abort();
  else init.signal?.addEventListener('abort', abort, { once: true });
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${conn.token}`);
  headers.set('Content-Type', 'application/json');
  try {
    return await withTimeout(
      (async () => {
        const response = await fetch(`${baseUrl(conn)}${path}`, {
          ...init,
          headers,
          signal: controller.signal,
        });
        const body: unknown = await response.json();
        if (!response.ok)
          throw new Error(`${path}: HTTP ${response.status} ${JSON.stringify(body)}`);
        return body;
      })(),
      REQUEST_TIMEOUT_MS,
      'Runtime request timeout',
      controller.signal,
    );
  } finally {
    init.signal?.removeEventListener('abort', abort);
    controller.abort();
  }
}

export const defaultSessionDeps: StreamSessionDeps = {
  getConnection: () => window.pulse?.runtime.getConnection() ?? Promise.resolve(null),
  request: runtimeRequest,
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
};

export async function runtimeFetch(path: string, init: RequestInit = {}): Promise<unknown> {
  const conn = await withTimeout(
    defaultSessionDeps.getConnection(),
    REQUEST_TIMEOUT_MS,
    'Runtime connection timeout',
    init.signal ?? undefined,
  );
  if (!conn) throw new Error('runtime not running');
  if (init.signal?.aborted) throw new Error('stream stopped');
  return runtimeRequest(conn, path, init);
}

interface Lease {
  conn: RuntimeConnection;
  path: string;
  publisherId: string;
}

interface ActiveSession {
  config: StreamConfig;
  abort: AbortController;
  lease: Lease | null;
}

export interface SessionState {
  running: boolean;
  config: StreamConfig | null;
  error: string | null;
}

/** One GUI-owned short lease. Serializing mutations prevents a late POST replacing a new one. */
export class StreamSession {
  private active: ActiveSession | null = null;
  private operation: Promise<void> = Promise.resolve();
  private timer: unknown = null;
  private disposed = false;

  constructor(
    private readonly view: Pick<StreamView, 'start' | 'stop'>,
    private readonly deps: StreamSessionDeps,
    private readonly onState: (state: SessionState) => void,
  ) {}

  get current(): { config: StreamConfig; signal: AbortSignal } | null {
    return this.active ? { config: this.active.config, signal: this.active.abort.signal } : null;
  }

  start(config: StreamConfig): Promise<void> {
    if (this.disposed) return Promise.reject(new Error('stream panel disposed'));
    const previous = this.cancelActive();
    const active: ActiveSession = {
      config: { ...config },
      abort: new AbortController(),
      lease: null,
    };
    this.active = active;
    this.view.stop();
    this.state(null);
    return this.enqueue(async () => {
      await this.release(previous?.lease ?? null);
      if (this.active !== active) return;
      try {
        if (active.config.mode === 'relay') {
          active.lease = await this.acquire(active);
          if (this.active !== active) return;
          this.connectRelay(active);
          this.scheduleRenewal(active);
        } else {
          this.view.start(async () => {
            const conn = await this.connection();
            return this.active === active && conn
              ? runtimeStreamUrl(conn, active.config.serial, active.config)
              : null;
          });
        }
      } catch (error) {
        if (this.active === active) {
          this.cancelActive();
          this.view.stop();
          this.state(String(error));
          await this.release(active.lease);
        }
        throw error;
      }
    });
  }

  stop(): Promise<void> {
    const active = this.cancelActive();
    this.view.stop();
    this.state(null);
    return this.enqueue(() => this.release(active?.lease ?? null));
  }

  dispose(): Promise<void> {
    this.disposed = true;
    return this.stop();
  }

  private cancelActive(): ActiveSession | null {
    const active = this.active;
    this.active = null;
    active?.abort.abort();
    if (this.timer !== null) this.deps.clearTimer(this.timer);
    this.timer = null;
    return active;
  }

  private enqueue(operation: () => Promise<void>): Promise<void> {
    const next = this.operation.then(operation);
    this.operation = next.catch(() => undefined);
    return next;
  }

  private connection(): Promise<RuntimeConnection | null> {
    return withTimeout(
      Promise.resolve().then(() => this.deps.getConnection()),
      REQUEST_TIMEOUT_MS,
      'Runtime connection timeout',
    );
  }

  private request(conn: RuntimeConnection, path: string, init: RequestInit): Promise<unknown> {
    return withTimeout(
      Promise.resolve().then(() => this.deps.request(conn, path, init)),
      REQUEST_TIMEOUT_MS,
      'Runtime request timeout',
    );
  }

  private async acquire(active: ActiveSession): Promise<Lease | null> {
    const conn = await this.connection();
    if (this.active !== active) return null;
    if (!conn) throw new Error('runtime not running');
    const cfg = active.config;
    const path = `/debug/stream/${encodeURIComponent(cfg.serial)}/relay`;
    const body = (await this.request(conn, path, {
      method: 'POST',
      body: JSON.stringify({
        relay_url: cfg.relayUrl,
        channel: cfg.channel,
        token: cfg.relayToken,
        max_size: cfg.maxSize,
        bit_rate: cfg.bitRate,
        max_fps: cfg.maxFps,
      }),
    })) as { started?: unknown; publisher_id?: unknown; lease_ms?: unknown } | null;
    if (
      !body ||
      body.started !== true ||
      typeof body.publisher_id !== 'string' ||
      !body.publisher_id ||
      typeof body.lease_ms !== 'number' ||
      !Number.isFinite(body.lease_ms) ||
      body.lease_ms <= LEASE_RENEW_MS
    ) {
      throw new Error('invalid relay lease response');
    }
    const lease = { conn, path, publisherId: body.publisher_id };
    if (this.active !== active) {
      await this.release(lease);
      return null;
    }
    return lease;
  }

  private connectRelay(active: ActiveSession): void {
    const cfg = active.config;
    this.view.start(
      () =>
        Promise.resolve(
          this.active === active
            ? relaySubscribeUrl(cfg.relayUrl, cfg.channel, cfg.relayToken)
            : null,
        ),
      true,
    );
  }

  private scheduleRenewal(active: ActiveSession): void {
    if (this.active !== active) return;
    this.timer = this.deps.setTimer(() => {
      this.timer = null;
      void this.enqueue(() => this.renew(active));
    }, LEASE_RENEW_MS);
  }

  private async renew(active: ActiveSession): Promise<void> {
    if (this.active !== active) return;
    try {
      const lease = active.lease;
      const conn = await this.connection();
      if (this.active !== active) return;
      if (
        !lease ||
        !conn ||
        conn.pid !== lease.conn.pid ||
        conn.port !== lease.conn.port ||
        conn.token !== lease.conn.token
      ) {
        throw new Error('Runtime restarted or lease missing');
      }
      const result = (await this.request(conn, `${lease.path}/keepalive`, {
        method: 'POST',
        body: JSON.stringify({ publisher_id: lease.publisherId }),
      })) as { renewed?: unknown } | null;
      if (result?.renewed !== true) throw new Error('relay lease not renewed');
    } catch (error) {
      if (this.active !== active) return;
      this.view.stop();
      this.state(`relay lease: ${String(error)}`);
      const lease = active.lease;
      active.lease = null;
      await this.release(lease);
      if (this.active !== active) return;
      try {
        active.lease = await this.acquire(active);
        if (this.active !== active) return;
        this.connectRelay(active);
        this.state(null);
      } catch (retryError) {
        if (this.active === active) this.state(`relay reconnect: ${String(retryError)}`);
      }
    } finally {
      this.scheduleRenewal(active);
    }
  }

  private async release(lease: Lease | null): Promise<void> {
    if (!lease) return;
    // Never re-resolve a cleanup URL: this id belongs to the Runtime which issued the lease.
    await this.request(
      lease.conn,
      `${lease.path}?publisher_id=${encodeURIComponent(lease.publisherId)}`,
      {
        method: 'DELETE',
        keepalive: true,
      },
    ).catch(() => undefined); // A vanished Runtime or closed page is bounded by the server lease.
  }

  private state(error: string | null): void {
    this.onState({ running: this.active !== null, config: this.active?.config ?? null, error });
  }
}
