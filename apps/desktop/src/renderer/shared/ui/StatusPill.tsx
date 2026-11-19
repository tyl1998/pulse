import { useEffect, useState, type ReactNode } from 'react';
import { CircleHelp, ClockAlert } from 'lucide-react';
import { Badge, type Tone } from './Badge';

export interface StatusPillProps {
  /** Display text, not a domain enum. Domain adapters belong to features/entities. */
  value: string;
  source: string;
  /** Unix epoch milliseconds, or null when the observer has no timestamp. */
  observedAt: number | null;
  staleAfterMs?: number;
  /** Fixed clock for deterministic previews/tests. Omit for a live freshness check. */
  now?: number;
  tone?: Tone;
  icon?: ReactNode;
  className?: string;
}

export function StatusPill({
  value,
  source,
  observedAt,
  staleAfterMs = 30_000,
  now,
  tone = 'neutral',
  icon = <CircleHelp />,
  className = '',
}: StatusPillProps) {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (now !== undefined) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [now]);

  const date = observedAt === null ? null : new Date(observedAt);
  const timestamp = date && Number.isFinite(date.getTime()) ? date.toISOString() : null;
  const age = (now ?? clock) - (observedAt ?? 0);
  // The live clock samples once per second; do not flag a newly received observation as skew.
  const future = timestamp !== null && age < (now === undefined ? -1000 : 0);
  const stale = timestamp !== null && age > staleAfterMs;

  return (
    <span className={`ui-status-pill ${className}`} data-stale={stale}>
      <Badge tone={tone} icon={icon}>
        {value.trim() || '未知状态'}
      </Badge>
      <span className="ui-status-meta">
        <span>来源：{source.trim() || '未知来源'}</span>
        {timestamp ? <time dateTime={timestamp}>{timestamp}</time> : <span>时间未知</span>}
      </span>
      {stale && (
        <Badge tone="warning" icon={<ClockAlert />}>
          可能过期
        </Badge>
      )}
      {future && (
        <Badge tone="warning" icon={<ClockAlert />}>
          时间异常（晚于本机时钟）
        </Badge>
      )}
    </span>
  );
}
