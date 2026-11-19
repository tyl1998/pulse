import type { ComponentProps } from 'react';
import { LoaderCircle } from 'lucide-react';

export type ControlSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends ComponentProps<'button'> {
  size?: ControlSize;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  loading?: boolean;
}

export function Button({
  size = 'md',
  variant = 'secondary',
  loading = false,
  disabled,
  type = 'button',
  className = '',
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className={`ui-button ${className}`}
      data-size={size}
      data-variant={variant}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
    >
      {loading && <LoaderCircle aria-hidden="true" className="ui-spinner" />}
      {children}
    </button>
  );
}
