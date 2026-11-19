import {
  useRef,
  type ComponentProps,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { IconButton } from './IconButton';
import { inertOutside } from './inertOutside';

export interface DialogProps extends Pick<
  ComponentProps<typeof DialogPrimitive.Root>,
  'open' | 'defaultOpen' | 'onOpenChange'
> {
  /** Optional for fully controlled dialogs that are opened from a store or shortcut. */
  trigger?: ReactElement;
  title: string;
  description: string;
  children: ReactNode;
  initialFocusRef?: RefObject<HTMLElement | null>;
}

export const DialogClose = DialogPrimitive.Close;

export function Dialog({
  trigger,
  title,
  description,
  children,
  initialFocusRef,
  ...props
}: DialogProps) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  return (
    <DialogPrimitive.Root {...props}>
      {trigger ? <DialogPrimitive.Trigger asChild>{trigger}</DialogPrimitive.Trigger> : null}
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="ui-dialog-overlay" />
        <DialogPrimitive.Content
          ref={inertOutside}
          className="ui-dialog-content"
          onOpenAutoFocus={(event) => {
            const target = initialFocusRef?.current ?? titleRef.current;
            if (target) {
              event.preventDefault();
              target.focus();
            }
          }}
        >
          <DialogPrimitive.Title ref={titleRef} tabIndex={-1} className="ui-dialog-title">
            {title}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="ui-dialog-description">
            {description}
          </DialogPrimitive.Description>
          {children}
          <DialogPrimitive.Close asChild>
            <IconButton
              label="关闭对话框"
              icon={<X />}
              variant="ghost"
              className="ui-dialog-close"
            />
          </DialogPrimitive.Close>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
