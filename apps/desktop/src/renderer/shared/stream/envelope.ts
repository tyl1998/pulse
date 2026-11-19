/**
 * Pulse video envelope (mirror of runtime/pulse_runtime/stream/envelope.py). Big-endian:
 * u8 version | u8 kind | u16 flags | u32 seq | i64 pts_us | f64 rt_recv_ms | f64 rt_send_ms | payload
 */
export const ENVELOPE_VERSION = 1;
export const ENVELOPE_HEADER_SIZE = 32;

export const PacketKind = { config: 1, key: 2, delta: 3 } as const;
export type PacketKind = (typeof PacketKind)[keyof typeof PacketKind];

export interface Envelope {
  kind: PacketKind;
  seq: number;
  ptsUs: number;
  rtRecvMs: number;
  rtSendMs: number;
  payload: Uint8Array;
}

export function decodeEnvelope(buf: ArrayBuffer): Envelope {
  if (buf.byteLength < ENVELOPE_HEADER_SIZE) throw new Error('envelope too short');
  const v = new DataView(buf);
  const version = v.getUint8(0);
  if (version !== ENVELOPE_VERSION) throw new Error(`unsupported envelope version ${version}`);
  const kind = v.getUint8(1);
  if (kind !== 1 && kind !== 2 && kind !== 3) throw new Error(`bad packet kind ${kind}`);
  return {
    kind,
    seq: v.getUint32(4),
    ptsUs: Number(v.getBigInt64(8)),
    rtRecvMs: v.getFloat64(16),
    rtSendMs: v.getFloat64(24),
    payload: new Uint8Array(buf, ENVELOPE_HEADER_SIZE),
  };
}

/** Test/bench helper: build an envelope the way the Runtime does. */
export function encodeEnvelope(e: Envelope): ArrayBuffer {
  const buf = new ArrayBuffer(ENVELOPE_HEADER_SIZE + e.payload.byteLength);
  const v = new DataView(buf);
  v.setUint8(0, ENVELOPE_VERSION);
  v.setUint8(1, e.kind);
  v.setUint16(2, 0);
  v.setUint32(4, e.seq >>> 0);
  v.setBigInt64(8, BigInt(e.ptsUs));
  v.setFloat64(16, e.rtRecvMs);
  v.setFloat64(24, e.rtSendMs);
  new Uint8Array(buf, ENVELOPE_HEADER_SIZE).set(e.payload);
  return buf;
}
