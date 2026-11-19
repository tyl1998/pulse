import type { ReactElement } from 'react';
import * as TooltipPrimitive from '@radix-ui/react-tooltip';

export interface TooltipProps {
  children: ReactElement;
  content: string;
  side?: 'top' | 'right' | 'bottom' | 'left';
}

export function Tooltip({ children, content, side = 'top' }: TooltipProps) {
  return (
    <TooltipPrimitive.Provider delayDuration={350} skipDelayDuration={150}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            className="ui-tooltip"
            side={side}
            sideOffset={4}
            collisionPadding={16}
          >
            {content}
            <TooltipPrimitive.Arrow className="ui-tooltip-arrow" />
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}
