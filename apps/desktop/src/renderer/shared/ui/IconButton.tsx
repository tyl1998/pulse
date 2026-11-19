import type { ReactNode } from 'react';
import { Button, type ButtonProps } from './Button';

export interface IconButtonProps extends Omit<
  ButtonProps,
  'children' | 'aria-label' | 'aria-labelledby'
> {
  label: string;
  icon: ReactNode;
}

export function IconButton({ label, icon, className = '', loading, ...props }: IconButtonProps) {
  return (
    <Button
      {...props}
      loading={loading ?? false}
      aria-label={label}
      className={`ui-icon-button ${className}`}
    >
      {!loading && (
        <span className="ui-badge-icon" aria-hidden="true">
          {icon}
        </span>
      )}
    </Button>
  );
}
