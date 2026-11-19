import type { ReactNode } from 'react';
import { Badge, EmptyState } from '../shared/ui';

export function PageHeader({
  title,
  description,
  phase,
  actions,
}: {
  title: string;
  description: string;
  phase?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="route-header">
      <div className="route-heading">
        <h1>{title}</h1>
        {phase ? <Badge tone="neutral">{phase}</Badge> : null}
      </div>
      <p>{description}</p>
      {actions ? <div className="route-actions">{actions}</div> : null}
    </header>
  );
}

export function PlaceholderPage({
  title,
  description,
  phase,
  emptyTitle,
  emptyDescription,
}: {
  title: string;
  description: string;
  phase: string;
  emptyTitle: string;
  emptyDescription: string;
}) {
  return (
    <div className="route-page">
      <PageHeader title={title} description={description} phase={phase} />
      <EmptyState headingLevel={2} title={emptyTitle} description={emptyDescription} />
    </div>
  );
}
