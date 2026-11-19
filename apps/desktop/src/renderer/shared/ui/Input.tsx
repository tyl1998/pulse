import type { ComponentProps } from 'react';
import type { ControlSize } from './Button';

export interface InputProps extends Omit<ComponentProps<'input'>, 'size'> {
  size?: ControlSize;
  invalid?: boolean;
}

export function Input({ size = 'md', invalid = false, className = '', ...props }: InputProps) {
  return (
    <input
      {...props}
      className={`ui-input ${className}`}
      data-size={size}
      aria-invalid={invalid || props['aria-invalid']}
    />
  );
}
