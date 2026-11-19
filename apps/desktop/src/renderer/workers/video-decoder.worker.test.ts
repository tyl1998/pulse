import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { encodeEnvelope, PacketKind } from '../shared/stream/envelope';
import type { ToWorker, WorkerMessage } from '../shared/stream/workerProtocol';

const CONFIG = new Uint8Array([0, 0, 0, 1, 0x67, 0x64, 0, 0x1f]);
const sockets: FakeSocket[] = [];
const canvases: FakeCanvas[] = [];
const decoders: FakeDecoder[] = [];
let pixels = new Uint8ClampedArray(16).fill(10);

class FakeCanvas {
  context = {
    canvas: this,
    drawImage: vi.fn(),
    clearRect: vi.fn(),
    getImageData: vi.fn(() => ({ data: pixels })),
  };
  constructor(
    public width: number,
    public height: number,
  ) {
    canvases.push(this);
  }
  getContext(): typeof this.context | null {
    return this.context;
  }
}

class FakeSocket {
  binaryType = '';
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<ArrayBuffer | string>) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    sockets.push(this);
  }
  close() {
    this.closed = true;
  }
  text(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) } as MessageEvent<string>);
  }
  packet(kind: PacketKind, seq: number) {
    this.onmessage?.({
      data: encodeEnvelope({
        kind,
        seq,
        ptsUs: seq * 1000,
        rtRecvMs: 0,
        rtSendMs: 1,
        payload: kind === PacketKind.config ? CONFIG : new Uint8Array([0, 0, 1, 0x65]),
      }),
    } as MessageEvent<ArrayBuffer>);
  }
}

class FakeDecoder {
  state = 'configured';
  decodeQueueSize = 0;
  chunks: { timestamp: number }[] = [];
  constructor(readonly init: VideoDecoderInit) {
    decoders.push(this);
  }
  configure() {}
  decode(chunk: { timestamp: number }) {
    this.chunks.push(chunk);
  }
  reset() {}
  close() {
    this.state = 'closed';
  }
  emit(i: number) {
    const chunk = this.chunks[i];
    if (!chunk) throw new Error('no chunk');
    const frame = {
      timestamp: chunk.timestamp,
      displayWidth: 720,
      displayHeight: 1600,
      close: vi.fn(),
    };
    this.init.output(frame as unknown as VideoFrame);
    return frame;
  }
}

const scope: {
  onmessage: ((event: MessageEvent<ToWorker>) => void) | null;
  postMessage: (msg: WorkerMessage) => void;
} = { onmessage: null, postMessage: vi.fn() };
const send = (msg: ToWorker) => scope.onmessage?.({ data: msg } as MessageEvent<ToWorker>);
const currentSocket = () => {
  const socket = sockets.at(-1);
  if (!socket) throw new Error('no socket');
  return socket;
};

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(1000);
  vi.resetModules();
  sockets.length = 0;
  canvases.length = 0;
  decoders.length = 0;
  pixels = new Uint8ClampedArray(16).fill(10);
  scope.postMessage = vi.fn();
  scope.onmessage = null;
  vi.stubGlobal('self', scope);
  vi.stubGlobal('performance', { timeOrigin: 0, now: () => Date.now() });
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal('OffscreenCanvas', FakeCanvas);
  vi.stubGlobal('VideoDecoder', FakeDecoder);
  vi.stubGlobal(
    'EncodedVideoChunk',
    class {
      constructor(readonly init: EncodedVideoChunkInit) {}
      get timestamp() {
        return this.init.timestamp;
      }
    },
  );
  await import('./video-decoder.worker');
  send({ type: 'init', canvas: new FakeCanvas(1, 1) as unknown as OffscreenCanvas });
  send({ type: 'connect', connectionId: 1, url: 'ws://fake', relay: false });
  currentSocket().onopen?.();
});

afterEach(() => {
  send({ type: 'disconnect' });
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test('static canvas is not a disconnect while Runtime stats metadata keeps arriving', async () => {
  for (let i = 0; i < 20; i++) {
    currentSocket().text({ type: 'stats', dropped: 0 });
    await vi.advanceTimersByTimeAsync(1000);
  }
  expect(currentSocket().closed).toBe(false);
  expect(scope.postMessage).toHaveBeenCalledWith(
    expect.objectContaining({ type: 'metrics', connectionId: 1 }),
  );
});

test('missing metadata times out even if binary packets keep arriving, then a new socket recovers', async () => {
  const old = currentSocket();
  old.packet(PacketKind.config, 0);
  for (let i = 1; i <= 5; i++) {
    old.packet(PacketKind.key, i);
    await vi.advanceTimersByTimeAsync(1000);
  }
  expect(old.closed).toBe(true);
  expect(scope.postMessage).toHaveBeenCalledWith({
    type: 'status',
    connectionId: 1,
    status: { state: 'closed', code: 0, reason: 'metadata heartbeat timeout' },
  });
  send({ type: 'connect', connectionId: 2, url: 'ws://recovered', relay: false });
  currentSocket().text({ type: 'stats', dropped: 0 });
  currentSocket().packet(PacketKind.config, 0);
  currentSocket().packet(PacketKind.key, 1);
  decoders.at(-1)?.emit(0);
  expect(scope.postMessage).toHaveBeenCalledWith({
    type: 'status',
    connectionId: 2,
    status: { state: 'streaming' },
  });
});

test('relay heartbeat keeps an idle subscriber alive, but cannot mask a stalled connected Runtime', async () => {
  send({ type: 'connect', connectionId: 2, url: 'ws://relay', relay: true });
  const socket = currentSocket();
  socket.text({ type: 'relay-status', state: 'waiting' });
  for (let i = 0; i < 10; i++) {
    socket.text({ type: 'relay-heartbeat', dropped: 0 });
    await vi.advanceTimersByTimeAsync(1000);
  }
  expect(socket.closed).toBe(false);
  socket.text({ type: 'relay-status', state: 'connected' });
  for (let i = 0; i < 5; i++) {
    socket.text({ type: 'relay-heartbeat', dropped: 0 });
    await vi.advanceTimersByTimeAsync(1000);
  }
  expect(socket.closed).toBe(true);
  expect(scope.postMessage).toHaveBeenCalledWith({
    type: 'status',
    connectionId: 2,
    status: { state: 'closed', code: 0, reason: 'Runtime metadata heartbeat timeout' },
  });
});

test('stop ignores even already-queued old socket callbacks', () => {
  const socket = currentSocket();
  const open = socket.onopen;
  const message = socket.onmessage;
  send({ type: 'disconnect' });
  vi.mocked(scope.postMessage).mockClear();
  open?.();
  message?.({
    data: JSON.stringify({ type: 'status', state: 'streaming' }),
  } as MessageEvent<string>);
  expect(scope.postMessage).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

test('a missing canvas context reports failure instead of inventing draw metrics', () => {
  send({ type: 'disconnect' });
  const canvas = new FakeCanvas(1, 1);
  vi.spyOn(canvas, 'getContext').mockReturnValue(null);
  send({ type: 'init', canvas: canvas as unknown as OffscreenCanvas });
  send({ type: 'connect', connectionId: 2, url: 'ws://fake', relay: false });
  expect(scope.postMessage).toHaveBeenCalledWith({
    type: 'status',
    connectionId: 2,
    status: { state: 'closed', code: 0, reason: 'Error: worker canvas context unavailable' },
  });
  expect(sockets).toHaveLength(1);
});

test('ROI baseline and change read the worker canvas, not a VideoFrame or the main thread', () => {
  const socket = currentSocket();
  socket.packet(PacketKind.config, 0);
  socket.packet(PacketKind.key, 1);
  const first = decoders.at(-1)?.emit(0);
  send({ type: 'arm', id: 7, roi: { x: 0, y: 0, w: 1, h: 1 }, threshold: 5 });
  expect(scope.postMessage).toHaveBeenCalledWith({
    type: 'armed',
    connectionId: 1,
    id: 7,
    ready: true,
  });
  const canvas = canvases[0];
  const probe = canvases[1];
  expect(probe?.context.drawImage).toHaveBeenCalledWith(canvas, 0, 0, 720, 1600, 0, 0, 32, 32);
  expect(first?.close).toHaveBeenCalledOnce();
  pixels = new Uint8ClampedArray(16).fill(200);
  socket.packet(PacketKind.delta, 2);
  decoders.at(-1)?.emit(1);
  expect(scope.postMessage).toHaveBeenCalledWith(
    expect.objectContaining({ type: 'change', connectionId: 1, id: 7 }),
  );
  send({ type: 'disconnect' });
  expect(canvas?.context.clearRect).toHaveBeenCalledWith(0, 0, 720, 1600);
});
