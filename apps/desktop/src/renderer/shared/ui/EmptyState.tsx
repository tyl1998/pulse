import { useId, type ReactNode } from 'react';
import { Inbox } from 'lucide-react';

export interface EmptyStateProps {
  title: string;
  description: string;
  icon?: ReactNode;
  action?: ReactNode;
  /** Match the surrounding page outline: page-level 1/2, panel-level 3. */
  headingLevel?: 1 | 2 | 3 | 4;
}

export function EmptyState({
  title,
  description,
  icon = <Inbox />,
  action,
  headingLevel = 3,
}: EmptyStateProps) {
  const titleId = useId();
  const Heading = `h${headingLevel}` as 'h1' | 'h2' | 'h3' | 'h4';
  return (
    <section className="ui-empty-state" aria-labelledby={titleId}>
      <span aria-hidden="true">{icon}</span>
      <Heading id={titleId}>{title}</Heading>
      <p>{description}</p>
      {action}
    </section>
  );
}
