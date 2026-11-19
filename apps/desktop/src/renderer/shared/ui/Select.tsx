import type { ComponentProps } from 'react';
import * as SelectPrimitive from '@radix-ui/react-select';
import { Check, ChevronDown } from 'lucide-react';
import type { ControlSize } from './Button';
import { inertOutside } from './inertOutside';

export interface SelectItem {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps extends Pick<
  ComponentProps<typeof SelectPrimitive.Root>,
  | 'value'
  | 'defaultValue'
  | 'onValueChange'
  | 'disabled'
  | 'name'
  | 'required'
  | 'open'
  | 'onOpenChange'
> {
  items: readonly SelectItem[];
  placeholder?: string;
  size?: ControlSize;
  invalid?: boolean;
  id?: string;
  className?: string;
  'aria-label'?: string;
  'aria-labelledby'?: string;
  'aria-describedby'?: string;
}

export function Select({
  items,
  placeholder = '请选择',
  size = 'md',
  invalid = false,
  id,
  className = '',
  'aria-label': ariaLabel,
  'aria-labelledby': ariaLabelledby,
  'aria-describedby': ariaDescribedby,
  ...props
}: SelectProps) {
  return (
    <SelectPrimitive.Root {...props}>
      <SelectPrimitive.Trigger
        id={id}
        className={`ui-select-trigger ${className}`}
        data-size={size}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        aria-describedby={ariaDescribedby}
        aria-invalid={invalid || undefined}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon asChild>
          <ChevronDown aria-hidden="true" />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          ref={inertOutside}
          className="ui-select-content"
          position="popper"
          sideOffset={4}
          collisionPadding={16}
        >
          <SelectPrimitive.Viewport className="ui-select-viewport">
            {items.map((item) => (
              <SelectPrimitive.Item
                key={item.value}
                value={item.value}
                disabled={item.disabled ?? false}
                className="ui-select-item"
              >
                <SelectPrimitive.ItemText>{item.label}</SelectPrimitive.ItemText>
                <SelectPrimitive.ItemIndicator className="ui-select-indicator">
                  <Check aria-hidden="true" />
                </SelectPrimitive.ItemIndicator>
              </SelectPrimitive.Item>
            ))}
          </SelectPrimitive.Viewport>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
}
