/**
 * Video decoder worker: owns the stream WebSocket, a WebCodecs VideoDecoder (Annex-B H.264) and
 * the OffscreenCanvas transferred from the main thread. Protocol: shared/stream/workerProtocol.ts.
 */
import type { FromWorker, Roi, ToWorker, WorkerMessage } from '../shared/stream/workerProtocol';
import { DecoderCore, type ChunkInit, type FrameLike } from './decoderCore';

interface WorkerScope {
  postMessage(msg: WorkerMessage): void;
  onmessage: ((ev: MessageEvent<ToWorker>) => void) | null;
}
const scope = self as unknown as WorkerScope;
// Realtime epoch is shared with the Runtime. performance.timeOrigin does not follow host clock
// slewing; use performance.now() only for local decode/draw durations, anchored per frame.
const now = () => Date.now();
const post = (connectionId: number, msg: FromWorker) => scope.postMessage({ ...msg, connectionId });

const ROI_SAMPLE = 32;
export const METADATA_TIMEOUT_MS = 5000;
let ctx: OffscreenCanvasRenderingContext2D | null = null;
let probe: OffscreenCanvasRenderingContext2D | null = null;
let socket: WebSocket | null = null;
let core: DecoderCore | null = null;
let ticker: ReturnType<typeof setInterval> | null = null;
let connectionId = 0;

function draw(frame: FrameLike): void {
  if (!ctx) return;
  const canvas = ctx.canvas;
  if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
    canvas.width = frame.displayWidth;
    canvas.height = frame.displayHeight;
  }
  ctx.drawImage(frame as unknown as VideoFrame, 0, 0);
}

function clear(): void {
  if (ctx) ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
}

function sampleRoi(roi: Roi): Uint8ClampedArray | null {
  if (!probe || !ctx) return null;
  const { width: w, height: h } = ctx.canvas;
  probe.drawImage(
    ctx.canvas,
    roi.x * w,
    roi.y * h,
    Math.max(1, roi.w * w),
    Math.max(1, roi.h * h),
    0,
    0,
    ROI_SAMPLE,
    ROI_SAMPLE,
  );
  return probe.getImageData(0, 0, ROI_SAMPLE, ROI_SAMPLE).data;
}

function newCore(id: number, relay: boolean): DecoderCore {
  return new DecoderCore(
    {
      createDecoder: (output, error) => {
        const dec = new VideoDecoder({
          output: (f) => output(f),
          error: (e) => error(e instanceof Error ? e : new Error(String(e))),
        });
        return {
          get decodeQueueSize() {
            return dec.decodeQueueSize;
          },
          configure: (c) =>
            dec.configure({ ...c, hardwareAcceleration: 'no-preference' } as VideoDecoderConfig),
          decode: (chunk: ChunkInit) =>
            dec.decode(
              new EncodedVideoChunk({
                type: chunk.type,
                timestamp: chunk.timestamp,
                data: chunk.data,
              }),
            ),
          reset: () => dec.reset(),
          close: () => {
            if (dec.state !== 'closed') dec.close();
          },
        };
      },
      draw,
      clear,
      sampleRoi,
      now,
      monotonic: () => performance.now(),
      post: (msg) => post(id, msg),
    },
    relay,
  );
}

function disconnect(): void {
  const s = socket;
  socket = null;
  if (s) {
    s.onopen = null;
    s.onmessage = null;
    s.onerror = null;
    s.onclose = null;
    s.close();
  }
  core?.close();
  core = null;
  if (ticker !== null) clearInterval(ticker);
  ticker = null;
}

function connect(url: string, id: number, relay: boolean): void {
  disconnect();
  connectionId = id;
  if (!ctx || !probe) throw new Error('worker canvas context unavailable');
  const c = newCore(id, relay);
  core = c;
  const ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  socket = ws;
  let lastMetadata = now();
  let lastRuntimeMetadata = lastMetadata;
  post(id, { type: 'status', status: { state: 'connecting' } });
  ws.onopen = () => {
    if (socket !== ws) return;
    post(id, { type: 'status', status: { state: 'open' } });
  };
  ws.onmessage = (ev: MessageEvent<ArrayBuffer | string>) => {
    if (socket !== ws) return;
    const t = now();
    try {
      if (typeof ev.data === 'string') {
        const wasWaiting = !c.expectsRuntimeMetadata;
        const source = c.handleText(ev.data);
        if (source) lastMetadata = t;
        if (source === 'runtime' || (wasWaiting && c.expectsRuntimeMetadata))
          lastRuntimeMetadata = t;
      } else c.handleBinary(ev.data, t);
    } catch (err) {
      post(id, { type: 'error', message: String(err) });
    }
  };
  ws.onclose = (ev) => {
    if (socket !== ws) return;
    disconnect();
    post(id, { type: 'status', status: { state: 'closed', code: ev.code, reason: ev.reason } });
  };
  ws.onerror = () => {
    if (socket !== ws) return;
    disconnect();
    post(id, { type: 'status', status: { state: 'closed', code: 0, reason: 'WebSocket error' } });
  };
  ticker = setInterval(() => {
    if (socket !== ws) return;
    const socketExpired = now() - lastMetadata >= METADATA_TIMEOUT_MS;
    const runtimeExpired =
      c.expectsRuntimeMetadata && now() - lastRuntimeMetadata >= METADATA_TIMEOUT_MS;
    if (socketExpired || runtimeExpired) {
      disconnect();
      post(id, {
        type: 'status',
        status: {
          state: 'closed',
          code: 0,
          reason: socketExpired
            ? 'metadata heartbeat timeout'
            : 'Runtime metadata heartbeat timeout',
        },
      });
      return;
    }
    c.tick();
  }, 500);
}

scope.onmessage = (ev: MessageEvent<ToWorker>) => {
  const msg = ev.data;
  switch (msg.type) {
    case 'init':
      ctx = msg.canvas.getContext('2d', { alpha: false, desynchronized: true });
      probe = new OffscreenCanvas(ROI_SAMPLE, ROI_SAMPLE).getContext('2d', {
        willReadFrequently: true,
      });
      break;
    case 'connect':
      try {
        connect(msg.url, msg.connectionId, msg.relay);
      } catch (err) {
        disconnect();
        post(msg.connectionId, {
          type: 'status',
          status: { state: 'closed', code: 0, reason: String(err) },
        });
      }
      break;
    case 'disconnect':
      disconnect();
      break;
    case 'arm':
      if (core) core.arm(msg.id, msg.roi, msg.threshold);
      else post(connectionId, { type: 'armed', id: msg.id, ready: false });
      break;
    case 'disarm':
      core?.disarm(msg.id);
      break;
    case 'collect':
      post(connectionId, { type: 'samples', id: msg.id, samples: core?.collect() ?? [] });
      break;
  }
};
