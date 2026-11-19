import type { ComponentProps, ReactNode } from 'react';

export type Tone = 'neutral' | 'success' | 'danger' | 'warning' | 'accent' | 'info' | 'pending';

export interface BadgeProps extends ComponentProps<'span'> {
  tone?: Tone;
  size?: 'sm' | 'md';
  icon?: ReactNode;
}

export function Badge({
  tone = 'neutral',
  size = 'sm',
  icon,
  className = '',
  children,
  ...props
}: BadgeProps) {
  return (
    <span {...props} className={`ui-badge ${className}`} data-tone={tone} data-size={size}>
      {icon && (
        <span className="ui-badge-icon" aria-hidden="true">
          {icon}
        </span>
      )}
      <span>{children}</span>
    </span>
  );
}
