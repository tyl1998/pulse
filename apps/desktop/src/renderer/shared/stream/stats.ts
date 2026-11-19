/** Percentile with linear interpolation (p in [0, 100]). */
export function percentile(sorted: readonly number[], p: number): number {
  if (sorted.length === 0) return NaN;
  const rank = (p / 100) * (sorted.length - 1);
  const lo = Math.floor(rank);
  const hi = Math.ceil(rank);
  const a = sorted[lo] ?? NaN;
  const b = sorted[hi] ?? NaN;
  return a + (b - a) * (rank - lo);
}

export interface Summary {
  n: number;
  p50: number;
  p95: number;
  p99: number;
  mean: number;
  max: number;
}

export function summarize(values: readonly number[]): Summary {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  const sum = v.reduce((s, x) => s + x, 0);
  return {
    n: v.length,
    p50: percentile(v, 50),
    p95: percentile(v, 95),
    p99: percentile(v, 99),
    mean: v.length ? sum / v.length : NaN,
    max: v.length ? (v[v.length - 1] ?? NaN) : NaN,
  };
}
