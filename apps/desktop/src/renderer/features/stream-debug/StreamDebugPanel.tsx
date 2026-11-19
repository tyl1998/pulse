import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Input } from '../../shared/ui';
import { runChangeBench, type ChangeBenchOptions } from '../../shared/stream/bench';
import { defaultStreamDeps, StreamView, type StreamEvent } from '../../shared/stream/streamClient';
import {
  interruptsStream,
  type FromWorker,
  type Metrics,
  type StreamStatus,
} from '../../shared/stream/workerProtocol';
import {
  DEFAULT_CONFIG,
  defaultSessionDeps,
  runtimeFetch,
  StreamSession,
  type StreamConfig,
} from './streamSession';

/** P0.7 / FE-P0.3 spike panel: live view + pipeline metrics. Not a product UI. */

function describeStatus(s: StreamStatus | null): string {
  if (!s) return '未连接';
  switch (s.state) {
    case 'connecting':
      return '连接中…';
    case 'open':
      return 'WS 已连接，等待画面';
    case 'stopped':
      return '已停止';
    case 'bootstrapping':
      return '等待 CONFIG / KEY，恢复画面中';
    case 'streaming':
      return '画面解码中';
    case 'relay':
      return s.relay === 'connected'
        ? 'relay 已连接，等待画面'
        : `relay 断流（${s.relay}）${s.message ? ` · ${s.message}` : ''}`;
    case 'closed':
      return `已断开（${s.code} ${s.reason}），自动重连中`;
    case 'upstream':
      return `采集：${s.upstream}${s.message ? ` · ${s.message}` : ''}${
        s.retryInMs !== undefined ? ` · ${s.retryInMs}ms 后重试` : ''
      }`;
  }
}

const fmt = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined || !Number.isFinite(v) ? '–' : v.toFixed(digits);

interface BenchApi {
  start(cfg: Partial<StreamConfig>): Promise<void>;
  stop(): Promise<void>;
  metrics(): Metrics | null;
  status(): StreamStatus | null;
  events(): StreamEvent[];
  collect(): Promise<unknown>;
  changeBench(opts: ChangeBenchOptions & { tap: { x: number; y: number } }): Promise<unknown>;
}

declare global {
  interface Window {
    __pulseBench?: BenchApi;
  }
}

export function StreamDebugPanel() {
  const webCodecsSupported = typeof Worker !== 'undefined' && typeof VideoDecoder !== 'undefined';
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<StreamView | null>(null);
  const sessionRef = useRef<StreamSession | null>(null);
  const [config, setConfig] = useState<StreamConfig>(DEFAULT_CONFIG);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<StreamStatus | null>(null);
  const [upstream, setUpstream] = useState<StreamStatus | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [devices, setDevices] = useState<string[]>([]);
  const latest = useRef<{ metrics: Metrics | null; status: StreamStatus | null }>({
    metrics: null,
    status: null,
  });

  useEffect(() => {
    let alive = true;
    void runtimeFetch('/devices')
      .then((d) => {
        if (!alive) return;
        const list = (d as { devices: { serial: string; state: string }[] }).devices
          .filter((x) => x.state === 'device')
          .map((x) => x.serial);
        setDevices(list);
        setConfig((c) => (c.serial || !list[0] ? c : { ...c, serial: list[0] }));
      })
      .catch(() => {
        if (alive) setDevices([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  // A canvas can be transferred to a worker only once, so every mount (incl. StrictMode's
  // double-mount) creates its own canvas + worker.
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (!webCodecsSupported) return;
    let alive = true;
    const onEvent = (msg: FromWorker) => {
      if (!alive) return;
      if (msg.type === 'metrics') {
        latest.current.metrics = msg.metrics;
        setMetrics(msg.metrics);
      } else if (msg.type === 'status') {
        latest.current.status = msg.status;
        if (msg.status.state === 'streaming') setError(null);
        if (interruptsStream(msg.status)) {
          latest.current.metrics = null;
          setMetrics(null);
        }
        if (msg.status.state === 'upstream') {
          setUpstream(msg.status);
        } else {
          setStatus(msg.status);
          if (interruptsStream(msg.status)) setUpstream(null);
        }
      } else if (msg.type === 'error') {
        setError(msg.message);
      }
    };
    const view = new StreamView(defaultStreamDeps, onEvent);
    view.attach(() => {
      const canvas = document.createElement('canvas');
      canvas.style.maxHeight = '80vh';
      canvas.style.maxWidth = '50vw';
      canvas.style.background = '#111';
      canvas.dataset.testid = 'stream-canvas';
      host.replaceChildren(canvas);
      return canvas.transferControlToOffscreen();
    });
    viewRef.current = view;
    const session = new StreamSession(view, defaultSessionDeps, (state) => {
      if (!alive) return;
      setRunning(state.running);
      setError(state.error);
      if (state.config) setConfig(state.config);
    });
    sessionRef.current = session;
    const pagehide = () => {
      void session.stop().catch(() => undefined);
    };
    window.addEventListener('pagehide', pagehide);
    return () => {
      alive = false;
      window.removeEventListener('pagehide', pagehide);
      void session.dispose().catch(() => undefined);
      view.dispose();
      viewRef.current = null;
      sessionRef.current = null;
      latest.current = { metrics: null, status: null };
      host.replaceChildren();
    };
  }, [webCodecsSupported]);

  const start = useCallback(async (cfg: StreamConfig) => {
    const session = sessionRef.current;
    if (!session) throw new Error('stream view unavailable');
    await session.start(cfg);
  }, []);

  const stop = useCallback(async () => {
    await sessionRef.current?.stop();
  }, []);

  // Dev-only automation hook for the latency bench (scripts/stream-bench.mjs drives it via CDP).
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    let current = DEFAULT_CONFIG;
    window.__pulseBench = {
      async start(partial) {
        current = { ...(sessionRef.current?.current?.config ?? current), ...partial };
        await start(current);
      },
      async stop() {
        await stop();
      },
      metrics: () => latest.current.metrics,
      status: () => latest.current.status,
      events: () => viewRef.current?.events() ?? [],
      collect: async () => {
        if (!viewRef.current) throw new Error('no view');
        return viewRef.current.collect();
      },
      async changeBench(opts) {
        const view = viewRef.current;
        const active = sessionRef.current?.current;
        if (!view || !active) throw new Error('no active stream');
        const path = `/debug/stream/${encodeURIComponent(active.config.serial)}/trigger`;
        return runChangeBench(
          view,
          async () => {
            if (active.signal.aborted) throw new Error('stream stopped');
            const r = (await runtimeFetch(path, {
              method: 'POST',
              body: JSON.stringify({ kind: 'tap', x: opts.tap.x, y: opts.tap.y }),
              signal: active.signal,
            })) as { sent_ms: number };
            if (!Number.isFinite(r.sent_ms)) throw new Error('invalid trigger timestamp');
            return r.sent_ms;
          },
          { ...opts, signal: active.signal },
          (ms) => new Promise((r) => setTimeout(r, ms)),
        );
      },
    };
    return () => {
      delete window.__pulseBench;
    };
  }, [start, stop]);

  const set = <K extends keyof StreamConfig>(k: K, v: StreamConfig[K]) =>
    setConfig((c) => ({ ...c, [k]: v }));

  return (
    <section aria-label="画面调试" style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
      <div style={{ minWidth: 300 }}>
        <h2>画面链路调试（P0.7）</h2>
        <fieldset disabled={running} style={{ border: 0, padding: 0, margin: 0 }}>
          <label>
            设备{' '}
            <select value={config.serial} onChange={(e) => set('serial', e.target.value)}>
              {devices.length === 0 && <option value="">（无设备）</option>}
              {devices.map((d) => (
                <option key={d}>{d}</option>
              ))}
            </select>
          </label>
          <br />
          <label>
            长边上限{' '}
            <select value={config.maxSize} onChange={(e) => set('maxSize', Number(e.target.value))}>
              <option value={2400}>2400（1080p）</option>
              <option value={1600}>1600（720p）</option>
              <option value={1280}>1280</option>
            </select>
          </label>
          <br />
          <label>
            码率{' '}
            <select value={config.bitRate} onChange={(e) => set('bitRate', Number(e.target.value))}>
              <option value={2_000_000}>2M</option>
              <option value={4_000_000}>4M</option>
              <option value={8_000_000}>8M</option>
            </select>
          </label>
          <br />
          <label>
            路径{' '}
            <select
              value={config.mode}
              onChange={(e) => set('mode', e.target.value as StreamConfig['mode'])}
            >
              <option value="direct">直连 Runtime（loopback）</option>
              <option value="relay">经 relay</option>
            </select>
          </label>
          {config.mode === 'relay' && (
            <>
              <br />
              <Input
                aria-label="relay 地址"
                value={config.relayUrl}
                onChange={(e) => set('relayUrl', e.target.value)}
              />
              <Input
                aria-label="relay token"
                placeholder="token"
                value={config.relayToken}
                onChange={(e) => set('relayToken', e.target.value)}
              />
            </>
          )}
        </fieldset>
        <br />
        {running ? (
          <Button onClick={() => void stop().catch(() => undefined)}>停止</Button>
        ) : (
          <Button
            variant="primary"
            disabled={!config.serial || !webCodecsSupported}
            onClick={() => void start(config).catch(() => undefined)}
          >
            开始
          </Button>
        )}
        <p data-testid="stream-status">
          {describeStatus(status)}
          {upstream ? ` · ${describeStatus(upstream)}` : ''}
        </p>
        {!webCodecsSupported && <p role="alert">当前环境不支持 Worker / WebCodecs，无法显示画面</p>}
        {error && <p role="alert">{error}</p>}
        <table>
          <tbody>
            <tr>
              <td>分辨率</td>
              <td>
                {metrics ? `${metrics.width}×${metrics.height}` : '–'} {metrics?.codec ?? ''}
              </td>
            </tr>
            <tr>
              <td>fps</td>
              <td>{fmt(metrics?.fps)}</td>
            </tr>
            <tr>
              <td>码率 kbps</td>
              <td>{fmt(metrics?.kbps, 0)}</td>
            </tr>
            <tr>
              <td>解码耗时 均值/最大 ms</td>
              <td>
                {fmt(metrics?.decodeMsMean)} / {fmt(metrics?.decodeMsMax)}
              </td>
            </tr>
            <tr>
              <td>解码队列</td>
              <td>{metrics?.decodeQueue ?? '–'}</td>
            </tr>
            <tr>
              <td>丢帧 worker/Runtime/relay</td>
              <td>
                {metrics?.droppedWorker ?? '–'} / {metrics?.droppedUpstream ?? '–'} /{' '}
                {metrics?.droppedRelay ?? '–'}
              </td>
            </tr>
            <tr>
              <td>PTS到draw提交估算 p50 ms</td>
              <td data-testid="e2e">{fmt(metrics?.e2eMsP50)}</td>
            </tr>
            <tr>
              <td>传输合计 p50 ms</td>
              <td>{fmt(metrics?.netMsP50)}</td>
            </tr>
            <tr>
              <td>relay 排队 p50 ms</td>
              <td>{fmt(metrics?.relayQueueMsP50)}</td>
            </tr>
            <tr>
              <td>网络（不含 relay 排队）p50 ms</td>
              <td>{fmt(metrics?.networkExcludingRelayMsP50)}</td>
            </tr>
            <tr>
              <td>时钟 RTT / 校准误差估算 ms</td>
              <td>
                {fmt(metrics?.clockRttMs, 2)} /{' '}
                {fmt(metrics?.clockRttMs == null ? null : metrics.clockRttMs / 2, 2)}
              </td>
            </tr>
          </tbody>
        </table>
        <p>
          仅测 Worker canvas 与 draw 提交，非光学上屏。minRTT/2 仅估算校准误差；校准失效不报 PTS
          指标。Runtime 与 GUI 同主机，relay 可异机。
        </p>
      </div>
      <div ref={hostRef} hidden={!running} />
    </section>
  );
}
