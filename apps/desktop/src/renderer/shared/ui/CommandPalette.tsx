import {
  useCallback,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { CornerDownLeft, Search } from 'lucide-react';
import { inertOutside } from './inertOutside';

export interface CommandItem {
  id: string;
  label: string;
  group?: string;
  hint?: string;
  keywords?: readonly string[];
  icon?: ReactNode;
  disabled?: boolean;
  run: () => void;
}

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  items: readonly CommandItem[];
  placeholder?: string;
  emptyText?: string;
}

/** Pure filter used by the palette and tests; every whitespace-separated token must match. */
export function filterCommands(items: readonly CommandItem[], query: string): CommandItem[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return [...items];
  return items.filter((item) => {
    const haystack = [item.label, item.group ?? '', item.hint ?? '', ...(item.keywords ?? [])]
      .join(' ')
      .toLowerCase();
    return tokens.every((token) => haystack.includes(token));
  });
}

function firstEnabledIndex(items: readonly CommandItem[], from = 0): number {
  for (let index = from; index < items.length; index += 1) {
    if (!items[index]?.disabled) return index;
  }
  for (let index = 0; index < from; index += 1) {
    if (!items[index]?.disabled) return index;
  }
  return -1;
}

function stepEnabledIndex(items: readonly CommandItem[], current: number, step: 1 | -1): number {
  if (items.length === 0) return -1;
  let index = current;
  for (let moved = 0; moved < items.length; moved += 1) {
    index = (index + step + items.length) % items.length;
    if (!items[index]?.disabled) return index;
  }
  return -1;
}

export function CommandPalette({
  open,
  onOpenChange,
  items,
  placeholder = '搜索命令…',
  emptyText = '没有匹配的命令',
}: CommandPaletteProps) {
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const contentRef = useCallback((node: HTMLDivElement | null) => {
    if (node) {
      // Capture the opener before native inert can blur it.
      const focused = node.ownerDocument.activeElement;
      returnFocusRef.current = focused instanceof HTMLElement ? focused : null;
    }
    return inertOutside(node);
  }, []);
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState<string | null>(null);
  const filtered = useMemo(() => filterCommands(items, query), [items, query]);
  const activeIndex = useMemo(() => {
    const stored = activeId ? filtered.findIndex((item) => item.id === activeId) : -1;
    const current = filtered[stored];
    if (current && !current.disabled) return stored;
    return firstEnabledIndex(filtered);
  }, [activeId, filtered]);

  const optionId = (index: number) => `${listId}-option-${index}`;
  const active = filtered[activeIndex];
  const activeOptionId = activeIndex >= 0 ? optionId(activeIndex) : undefined;

  const reset = () => {
    setQuery('');
    setActiveId(null);
  };

  const changeOpen = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const runItem = (item: CommandItem | undefined) => {
    if (!item || item.disabled) return;
    changeOpen(false);
    item.run();
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        setActiveId(filtered[stepEnabledIndex(filtered, activeIndex, 1)]?.id ?? null);
        break;
      case 'ArrowUp':
        event.preventDefault();
        setActiveId(filtered[stepEnabledIndex(filtered, activeIndex, -1)]?.id ?? null);
        break;
      case 'Home':
        event.preventDefault();
        setActiveId(filtered[firstEnabledIndex(filtered)]?.id ?? null);
        break;
      case 'End':
        event.preventDefault();
        setActiveId(filtered[stepEnabledIndex(filtered, 0, -1)]?.id ?? null);
        break;
      case 'Enter':
        event.preventDefault();
        runItem(active);
        break;
      default:
        break;
    }
  };

  let optionIndex = -1;
  const groups = new Map<string, { item: CommandItem; index: number }[]>();
  for (const item of filtered) {
    optionIndex += 1;
    const group = item.group ?? '';
    const entries = groups.get(group) ?? [];
    entries.push({ item, index: optionIndex });
    groups.set(group, entries);
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={changeOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="ui-dialog-overlay" />
        <DialogPrimitive.Content
          ref={contentRef}
          className="ui-command-content"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            reset();
            inputRef.current?.focus();
            inputRef.current?.select();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const target = returnFocusRef.current;
            // Leave focus with a command's next dialog or other explicit focus target.
            if (
              target?.isConnected &&
              !target.closest('[inert]') &&
              target.ownerDocument.activeElement === target.ownerDocument.body
            ) {
              target.focus({ preventScroll: true });
            }
          }}
        >
          <DialogPrimitive.Title className="ui-visually-hidden">命令面板</DialogPrimitive.Title>
          <DialogPrimitive.Description className="ui-visually-hidden">
            输入关键词筛选命令，方向键选择，回车执行。
          </DialogPrimitive.Description>
          <div className="ui-command-search">
            <Search aria-hidden="true" />
            <input
              ref={inputRef}
              className="ui-command-input"
              aria-label="搜索命令"
              role="combobox"
              aria-autocomplete="list"
              aria-controls={listId}
              aria-expanded="true"
              aria-activedescendant={active ? activeOptionId : undefined}
              autoComplete="off"
              spellCheck={false}
              placeholder={placeholder}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={onKeyDown}
            />
            <kbd className="ui-kbd">Esc</kbd>
          </div>
          <div id={listId} className="ui-command-list" role="listbox" aria-label="可用命令">
            {filtered.length === 0 ? (
              <p className="ui-command-empty">{emptyText}</p>
            ) : (
              [...groups.entries()].map(([group, entries]) => (
                <div key={group} className="ui-command-group" role="group" aria-label={group}>
                  {group ? (
                    <div className="ui-command-group-label" aria-hidden="true">
                      {group}
                    </div>
                  ) : null}
                  {entries.map(({ item, index }) => (
                    <div
                      key={item.id}
                      id={optionId(index)}
                      role="option"
                      aria-selected={index === activeIndex}
                      aria-disabled={item.disabled || undefined}
                      className="ui-command-option"
                      data-active={index === activeIndex || undefined}
                      data-disabled={item.disabled || undefined}
                      onMouseMove={() => {
                        if (!item.disabled) setActiveId(item.id);
                      }}
                      onClick={() => runItem(item)}
                    >
                      {item.icon ? (
                        <span className="ui-command-icon" aria-hidden="true">
                          {item.icon}
                        </span>
                      ) : null}
                      <span className="ui-command-copy">
                        <span className="ui-command-label">{item.label}</span>
                        {item.hint ? <span className="ui-command-hint">{item.hint}</span> : null}
                      </span>
                      {index === activeIndex && !item.disabled ? (
                        <CornerDownLeft
                          className="ui-command-enter"
                          aria-hidden="true"
                          data-testid={`command-enter-${item.id}`}
                        />
                      ) : null}
                    </div>
                  ))}
                </div>
              ))
            )}
          </div>
          <p className="ui-command-footer">↑↓ 选择 · ↵ 执行 · Esc 关闭</p>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
