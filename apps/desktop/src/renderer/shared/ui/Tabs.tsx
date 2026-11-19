import type { ComponentProps, ReactNode } from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import type { ControlSize } from './Button';

export interface TabItem {
  value: string;
  label: string;
  content: ReactNode;
  disabled?: boolean;
}

export interface TabsProps extends Pick<
  ComponentProps<typeof TabsPrimitive.Root>,
  'value' | 'defaultValue' | 'onValueChange' | 'activationMode' | 'className'
> {
  label: string;
  items: readonly TabItem[];
  size?: ControlSize;
}

export function Tabs({ label, items, size = 'md', ...props }: TabsProps) {
  return (
    <TabsPrimitive.Root {...props}>
      <TabsPrimitive.List aria-label={label} className="ui-tab-list">
        {items.map((item) => (
          <TabsPrimitive.Trigger
            key={item.value}
            value={item.value}
            disabled={item.disabled ?? false}
            className="ui-tab"
            data-size={size}
          >
            {item.label}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      {items.map((item) => (
        <TabsPrimitive.Content key={item.value} value={item.value} className="ui-tab-panel">
          {item.content}
        </TabsPrimitive.Content>
      ))}
    </TabsPrimitive.Root>
  );
}
