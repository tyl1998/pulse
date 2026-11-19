import type { FrameSample, Roi } from './workerProtocol';
import { withTimeout } from './timeout';

export interface ChangeBenchOptions {
  n: number;
  roi: Roi;
  threshold: number;
  /** Wait between trials so the previous change (ripple / animation) settles. */
  settleMs: number;
  timeoutMs: number;
  /** Internal GUI-session cancellation; the public CDP options need not provide it. */
  signal?: AbortSignal;
}

export interface ChangeTrial {
  /** Host epoch ms when the Runtime started `adb shell input` (scheme 1 start). */
  sentMs: number;
  sample: FrameSample;
}

export interface ChangeBenchResult {
  trials: ChangeTrial[];
  misses: number;
  /** Actual trigger calls, excluding trials for which no canvas baseline was available. */
  attempts: number;
  aborted: boolean;
}

export interface ChangeWatcher {
  waitForChange(
    roi: Roi,
    threshold: number,
    timeoutMs: number,
  ): { id: number; ready: Promise<boolean>; done: Promise<FrameSample | null>; cancel(): void };
}

/**
 * Scheme 1: trigger a visible change on the device, detect the first changed frame on the
 * worker canvas. `drawnMs` remains a draw-submission estimate, not optical presentation.
 */
export async function runChangeBench(
  view: ChangeWatcher,
  trigger: () => Promise<number>,
  opts: ChangeBenchOptions,
  sleep: (ms: number) => Promise<void>,
  onProgress?: (done: number) => void,
): Promise<ChangeBenchResult> {
  if (
    !Number.isSafeInteger(opts.n) ||
    opts.n < 0 ||
    opts.n > 10_000 ||
    !Number.isFinite(opts.timeoutMs) ||
    opts.timeoutMs <= 0 ||
    !Number.isFinite(opts.settleMs) ||
    opts.settleMs < 0
  )
    throw new Error('invalid change bench limits');
  const trials: ChangeTrial[] = [];
  let misses = 0;
  let attempts = 0;
  for (let i = 0; i < opts.n; i++) {
    if (opts.signal?.aborted) break;
    let watch: ReturnType<ChangeWatcher['waitForChange']> | undefined;
    try {
      await withTimeout(
        sleep(opts.settleMs),
        opts.settleMs + opts.timeoutMs,
        'settle timeout',
        opts.signal,
      );
      if (opts.signal?.aborted) break;
      watch = view.waitForChange(opts.roi, opts.threshold, opts.timeoutMs);
      const ready = await withTimeout(watch.ready, opts.timeoutMs, 'baseline timeout', opts.signal);
      if (!ready) {
        misses += 1;
        continue;
      }
      if (opts.signal?.aborted) break;
      attempts += 1;
      const sentMs = await withTimeout(trigger(), opts.timeoutMs, 'trigger timeout', opts.signal);
      const sample = await withTimeout(watch.done, opts.timeoutMs, 'change timeout', opts.signal);
      if (sample && sample.drawnMs >= sentMs) trials.push({ sentMs, sample });
      else misses += 1;
    } catch {
      if (opts.signal?.aborted) break;
      misses += 1;
    } finally {
      watch?.cancel();
      onProgress?.(i + 1);
    }
  }
  return { trials, misses, attempts, aborted: opts.signal?.aborted ?? false };
}

export type Segment =
  | 'devToRuntime'
  | 'runtimeForward'
  | 'transport'
  | 'relayQueue'
  | 'networkExcludingRelay'
  | 'decode'
  | 'render'
  | 'e2e';

/** Subtract relay queue time without inventing negative sub-resolution network delays. */
export function networkWithoutRelay(s: FrameSample): number {
  if (s.relayQueueMs === null) return NaN;
  const value = s.guiRecvMs - s.rtSendMs - s.relayQueueMs;
  // A sub-ms relay queue can exceed a transport interval rounded to integer milliseconds.
  // Zero here means "below timestamp resolution", not literally zero network latency.
  return value >= -(s.timingResolutionMs ?? 0) ? Math.max(0, value) : NaN;
}

/** Split a frame sample into pipeline segments (ms). Device-based ones need the clock offset. */
export function segments(s: FrameSample): Record<Segment, number> {
  const dev = s.devMs ?? NaN;
  return {
    devToRuntime: s.rtRecvMs - dev,
    runtimeForward: s.rtSendMs - s.rtRecvMs,
    // Keep the original total for old result consumers. Runtime and GUI share a host clock.
    transport: s.guiRecvMs - s.rtSendMs,
    relayQueue: s.relayQueueMs ?? NaN,
    networkExcludingRelay: networkWithoutRelay(s),
    decode: s.decodedMs - s.decodeStartMs,
    render: s.drawnMs - s.decodedMs,
    e2e: s.drawnMs - dev,
  };
}
