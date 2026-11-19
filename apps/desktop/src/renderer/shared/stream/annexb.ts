/** Annex-B H.264 helpers (mirror of split_annexb in scrcpy_video.py). */

/** Split an Annex-B stream into NAL units (start codes removed, views into `data`). */
export function splitAnnexB(data: Uint8Array): Uint8Array[] {
  const nals: Uint8Array[] = [];
  const n = data.length;
  let start = -1;
  let i = 0;
  while (i + 2 < n) {
    if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 1) {
      if (start >= 0) {
        const end = i > 0 && data[i - 1] === 0 ? i - 1 : i;
        if (end > start) nals.push(data.subarray(start, end));
      }
      i += 3;
      start = i;
    } else {
      i += 1;
    }
  }
  if (start >= 0 && start < n) nals.push(data.subarray(start));
  return nals;
}

export const nalType = (nal: Uint8Array): number => (nal[0] ?? 0) & 0x1f;

export const NAL_IDR = 5;
export const NAL_SPS = 7;
export const NAL_PPS = 8;

/** RFC 6381 codec string (avc1.PPCCLL) from the SPS in a config packet. */
export function codecFromConfig(config: Uint8Array): string | null {
  for (const nal of splitAnnexB(config)) {
    if (nalType(nal) === NAL_SPS && nal.length >= 4) {
      const hex = (b: number | undefined) => (b ?? 0).toString(16).padStart(2, '0');
      return `avc1.${hex(nal[1])}${hex(nal[2])}${hex(nal[3])}`;
    }
  }
  return null;
}

export function concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}
