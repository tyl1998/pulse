/**
 * Messages between the main thread and workers/video-decoder.worker.ts.
 *
 * The worker owns the stream WebSocket, the VideoDecoder and the OffscreenCanvas, so the main
 * thread never touches video bytes or frames (desktop-frontend.md §9).
 */

export interface Roi {
  /** Normalized [0, 1] rectangle in frame coordinates. */
  x: number;
  y: number;
  w: number;
  h: number;
}

export type ToWorker =
  | { type: 'init'; canvas: OffscreenCanvas }
  | { type: 'connect'; url: string; connectionId: number; relay: boolean }
  | { type: 'disconnect' }
  /** Sample the worker canvas now, or acknowledge after the first baseline becomes available. */
  | { type: 'arm'; id: number; roi: Roi; threshold: number }
  | { type: 'disarm'; id: number }
  | { type: 'collect'; id: number };

/** Per-frame timing (host epoch ms unless noted). Device-based fields need a clock offset. */
export interface FrameSample {
  /** Cross-process epoch fields use Date.now() / realtime milliseconds, not performance epoch. */
  timingResolutionMs?: number;
  seq: number;
  ptsUs: number;
  key: boolean;
  bytes: number;
  /** Device frame time mapped to host clock (pts + offset), or null without clock sync. */
  devMs: number | null;
  rtRecvMs: number;
  rtSendMs: number;
  guiRecvMs: number;
  /** Relay's monotonic queue duration; 0 for direct, null for a missing/mismatched sidecar. */
  relayQueueMs: number | null;
  /** Calibration RTT, not an optical presentation accuracy bound. */
  clockRttMs: number | null;
  decodeStartMs: number;
  decodedMs: number;
  /** Worker drawImage submission completed; not compositor or optical presentation. */
  drawnMs: number;
}

/** Scalar metadata for the canvas's last draw, not a VideoFrame or encoded packet. */
export interface PresentedFrame {
  sessionId: string | null;
  /** Changes at every CONFIG boundary, including an identical encoder restart. */
  configId: number;
  seq: number;
  width: number;
  height: number;
  drawnAtMs: number;
  /** Confirmed by the capture source; missing metadata never implies natural orientation. */
  captureOrientation: 'display' | 'natural' | null;
}

export interface Metrics {
  /** Rendered frames per second over the last window. */
  fps: number;
  /** Mean / max decode time (submit -> output callback) over the window, ms. */
  decodeMsMean: number;
  decodeMsMax: number;
  decodeQueue: number;
  /** Independent counters: worker drops, Runtime-reported drops, and relay-reported drops. */
  droppedWorker: number;
  droppedUpstream: number;
  droppedRelay: number;
  /** PTS -> draw submission estimate; NaN without a currently valid calibration. */
  e2eMsP50: number;
  /** Median guiRecv - rtSend (WS / relay transport) over the window. */
  netMsP50: number;
  relayQueueMsP50: number;
  /** guiRecv - rtSend - relayQueue, assuming Runtime and GUI share a host clock. */
  networkExcludingRelayMsP50: number;
  kbps: number;
  width: number;
  height: number;
  codec: string | null;
  clockRttMs: number | null;
  framesTotal: number;
}

export type StreamStatus =
  | { state: 'connecting' }
  | { state: 'open' }
  | { state: 'stopped' }
  | { state: 'bootstrapping'; sessionId?: string }
  | { state: 'streaming'; sessionId?: string }
  | { state: 'relay'; relay: 'waiting' | 'connected' | 'disconnected'; message?: string }
  | { state: 'closed'; code: number; reason: string }
  /** Upstream (Runtime) capture status, forwarded verbatim from {type:"status"} messages. */
  | { state: 'upstream'; upstream: string; message?: string; retryInMs?: number };

export type FromWorker =
  | { type: 'status'; status: StreamStatus }
  | { type: 'frame'; frame: PresentedFrame }
  | { type: 'metrics'; metrics: Metrics }
  | { type: 'armed'; id: number; ready: boolean }
  | { type: 'change'; id: number; sample: FrameSample; diff: number }
  | { type: 'samples'; id: number; samples: FrameSample[] }
  | { type: 'error'; message: string };

/** Every wire reply belongs to exactly one socket attempt, including delayed decoder output. */
export type WorkerMessage = FromWorker & { connectionId: number };

export function isFromWorker(data: unknown): data is WorkerMessage {
  if (typeof data !== 'object' || data === null) return false;
  if (!Number.isSafeInteger((data as { connectionId?: unknown }).connectionId)) return false;
  const t = (data as { type?: unknown }).type;
  return (
    t === 'status' ||
    t === 'frame' ||
    t === 'metrics' ||
    t === 'armed' ||
    t === 'change' ||
    t === 'samples' ||
    t === 'error'
  );
}

export function interruptsStream(status: StreamStatus): boolean {
  return (
    status.state === 'connecting' ||
    status.state === 'closed' ||
    status.state === 'stopped' ||
    status.state === 'bootstrapping' ||
    status.state === 'relay' ||
    (status.state === 'upstream' && ['starting', 'error', 'stopped'].includes(status.upstream))
  );
}
