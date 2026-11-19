import { cleanup, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, test, vi } from 'vitest';
import { CommandPalette, filterCommands, type CommandItem } from './CommandPalette';

afterEach(cleanup);

function items(runFirst = vi.fn(), runSecond = vi.fn(), runDisabled = vi.fn()): CommandItem[] {
  return [
    {
      id: 'cases',
      group: '导航',
      label: '转到用例',
      hint: '工程与用例树',
      keywords: ['studio'],
      run: runFirst,
    },
    {
      id: 'devices',
      group: '导航',
      label: '转到设备',
      hint: '本机设备与设备池',
      run: runSecond,
    },
    {
      id: 'future',
      group: '导航',
      label: '转到未来功能',
      disabled: true,
      run: runDisabled,
    },
    {
      id: 'layout-record',
      group: '布局',
      label: '以录制布局打开用例工作室',
      hint: '设备画面与事件流优先',
      run: vi.fn(),
    },
  ];
}

test('filters by label, hint and keywords; every token must match', () => {
  const list = items();
  expect(filterCommands(list, '').map((item) => item.id)).toEqual(list.map((item) => item.id));
  expect(filterCommands(list, '设备 本机').map((item) => item.id)).toEqual(['devices']);
  expect(filterCommands(list, 'STUDIO').map((item) => item.id)).toEqual(['cases']);
  expect(filterCommands(list, '录制 事件').map((item) => item.id)).toEqual(['layout-record']);
  expect(filterCommands(list, '不存在')).toEqual([]);
});

test('opens focused, filters, skips disabled items and runs the active command', async () => {
  const runFirst = vi.fn();
  const runSecond = vi.fn();
  const runDisabled = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <CommandPalette
      open
      onOpenChange={onOpenChange}
      items={items(runFirst, runSecond, runDisabled)}
    />,
  );

  const input = screen.getByRole('combobox', { name: '搜索命令' });
  await waitFor(() => expect(input).toHaveFocus());
  const options = screen.getAllByRole('option');
  expect(options).toHaveLength(4);
  await waitFor(() => expect(options[0]).toHaveAttribute('aria-selected', 'true'));

  await userEvent.keyboard('{ArrowDown}');
  expect(screen.getByRole('option', { name: /转到设备/ })).toHaveAttribute('aria-selected', 'true');
  await userEvent.keyboard('{ArrowDown}');
  expect(screen.getByRole('option', { name: /录制布局/ })).toHaveAttribute('aria-selected', 'true');
  await userEvent.keyboard('{Enter}');
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(runSecond).not.toHaveBeenCalled();
  expect(runDisabled).not.toHaveBeenCalled();

  await userEvent.clear(input);
  await userEvent.type(input, '本机');
  expect(screen.getAllByRole('option')).toHaveLength(1);
  await userEvent.keyboard('{Enter}');
  expect(onOpenChange).toHaveBeenCalledWith(false);
  expect(runSecond).toHaveBeenCalledOnce();
  expect(runFirst).not.toHaveBeenCalled();
});

test('mouse click runs a command, Escape requests close and empty results are honest', async () => {
  const runFirst = vi.fn();
  const onOpenChange = vi.fn();
  render(<CommandPalette open onOpenChange={onOpenChange} items={items(runFirst)} />);

  await userEvent.click(screen.getByRole('option', { name: /转到用例/ }));
  expect(runFirst).toHaveBeenCalledOnce();
  expect(onOpenChange).toHaveBeenCalledWith(false);

  const input = screen.getByRole('combobox', { name: '搜索命令' });
  await userEvent.type(input, '没有这个命令');
  expect(screen.getByText('没有匹配的命令')).toBeVisible();
  expect(screen.queryAllByRole('option')).toHaveLength(0);
  await userEvent.keyboard('{Escape}');
  expect(onOpenChange).toHaveBeenLastCalledWith(false);
});

test.each([
  ['native composition', { isComposing: true }],
  ['legacy key code 229', { keyCode: 229 }],
] as const)(
  'IME %s leaves confirmation and candidate keys untouched',
  async (_mode, nativeEvent) => {
    const runFirst = vi.fn();
    const runSecond = vi.fn();
    const onOpenChange = vi.fn();
    render(<CommandPalette open onOpenChange={onOpenChange} items={items(runFirst, runSecond)} />);
    const input = screen.getByRole('combobox', { name: '搜索命令' });
    await waitFor(() => expect(input).toHaveFocus());
    await userEvent.keyboard('{ArrowDown}');
    const activeOption = input.getAttribute('aria-activedescendant');
    expect(screen.getByRole('option', { name: /转到设备/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    for (const key of ['Enter', 'ArrowDown', 'ArrowUp', 'Home', 'End']) {
      const event = createEvent.keyDown(input, { key, ...nativeEvent });
      fireEvent(input, event);
      expect(event.defaultPrevented).toBe(false);
      expect(input).toHaveAttribute('aria-activedescendant', activeOption);
      expect(runFirst).not.toHaveBeenCalled();
      expect(runSecond).not.toHaveBeenCalled();
      expect(onOpenChange).not.toHaveBeenCalled();
    }

    await userEvent.keyboard('{Enter}');
    expect(runSecond).toHaveBeenCalledOnce();
    expect(runFirst).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  },
);
