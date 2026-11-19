import { useRef, useState } from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { RefreshCw } from 'lucide-react';
import {
  Badge,
  Button,
  Dialog,
  DialogClose,
  EmptyState,
  IconButton,
  Input,
  Select,
  Tabs,
  Tooltip,
} from './index';

afterEach(cleanup);

describe('Button / IconButton', () => {
  test('supports mouse and keyboard without submitting a surrounding form by default', async () => {
    const user = userEvent.setup();
    const click = vi.fn();
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <form onSubmit={submit}>
        <Button onClick={click}>保存</Button>
      </form>,
    );
    const button = screen.getByRole('button', { name: '保存' });
    await user.click(button);
    await user.keyboard('{Enter} ');
    expect(click).toHaveBeenCalledTimes(3);
    expect(submit).not.toHaveBeenCalled();
    expect(button).toHaveAttribute('type', 'button');
  });

  test('explicit submit retains native form behavior', async () => {
    const user = userEvent.setup();
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <form onSubmit={submit}>
        <Button type="submit">提交</Button>
      </form>,
    );
    await user.click(screen.getByRole('button', { name: '提交' }));
    expect(submit).toHaveBeenCalledOnce();
  });

  test.each(['disabled', 'loading'] as const)(
    '%s suppresses actions and preserves the accessible name',
    async (state) => {
      const user = userEvent.setup();
      const click = vi.fn();
      render(
        <Button {...{ [state]: true }} onClick={click} size="sm" variant="danger">
          删除
        </Button>,
      );
      const button = screen.getByRole('button', { name: '删除' });
      await user.click(button);
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute('data-size', 'sm');
      expect(button).toHaveAttribute('data-variant', 'danger');
      if (state === 'loading') expect(button).toHaveAttribute('aria-busy', 'true');
      expect(click).not.toHaveBeenCalled();
    },
  );

  test('IconButton has a required name, supports focus/ref and loading', async () => {
    const user = userEvent.setup();
    const click = vi.fn();
    const ref = { current: null as HTMLButtonElement | null };
    const { rerender } = render(
      <IconButton ref={ref} label="刷新状态" icon={<RefreshCw />} onClick={click} />,
    );
    await user.tab();
    expect(ref.current).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(click).toHaveBeenCalledOnce();
    rerender(<IconButton label="刷新状态" icon={<RefreshCw />} loading />);
    expect(screen.getByRole('button', { name: '刷新状态' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '刷新状态' })).toHaveAttribute('aria-busy', 'true');
  });
});

describe('Input', () => {
  test('controlled text, label, invalid description and ref are preserved', async () => {
    const user = userEvent.setup();
    function Form() {
      const [value, setValue] = useState('');
      const ref = useRef<HTMLInputElement>(null);
      return (
        <>
          <label htmlFor="name">名称</label>
          <Input
            ref={ref}
            id="name"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            invalid={!value}
            aria-describedby="error"
          />
          <p id="error">请填写名称</p>
          <output>{value}</output>
        </>
      );
    }
    render(<Form />);
    const input = screen.getByRole('textbox', { name: '名称' });
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAccessibleDescription('请填写名称');
    await user.type(input, 'demo');
    expect(input).toHaveValue('demo');
    expect(input).not.toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('demo');
  });

  test('uncontrolled, disabled and read-only inputs retain native behavior', async () => {
    const user = userEvent.setup();
    render(
      <>
        <Input aria-label="备注" defaultValue="a" />
        <Input aria-label="禁用" disabled defaultValue="locked" />
        <Input aria-label="只读" readOnly defaultValue="read" />
      </>,
    );
    await user.type(screen.getByRole('textbox', { name: '备注' }), 'b');
    await user.type(screen.getByRole('textbox', { name: '禁用' }), 'x');
    await user.type(screen.getByRole('textbox', { name: '只读' }), 'x');
    expect(screen.getByRole('textbox', { name: '备注' })).toHaveValue('ab');
    expect(screen.getByRole('textbox', { name: '禁用' })).toHaveValue('locked');
    expect(screen.getByRole('textbox', { name: '只读' })).toHaveValue('read');
  });
});

const options = [
  { value: 'a', label: 'Alpha' },
  { value: 'b', label: 'Beta', disabled: true },
  { value: 'c', label: 'Charlie' },
];
const tabs = options.map((item) => ({ ...item, content: `${item.label} 内容` }));

describe('Select', () => {
  test('keyboard selection skips disabled items, updates controlled value and restores trigger focus', async () => {
    const user = userEvent.setup();
    function Example() {
      const [value, setValue] = useState('a');
      return <Select aria-label="环境" value={value} onValueChange={setValue} items={options} />;
    }
    render(<Example />);
    const trigger = screen.getByRole('combobox', { name: '环境' });
    await user.tab();
    await user.keyboard('{ArrowDown}');
    expect(await screen.findByRole('listbox')).toBeVisible();
    await waitFor(() => expect(screen.getByRole('option', { name: 'Alpha' })).toHaveFocus());
    await user.keyboard('{ArrowDown}');
    await waitFor(() => expect(screen.getByRole('option', { name: 'Charlie' })).toHaveFocus());
    await user.keyboard('{Enter}');
    expect(trigger).toHaveTextContent('Charlie');
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  test('supports mouse selection and uncontrolled values', async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<Select aria-label="环境" defaultValue="a" onValueChange={change} items={options} />);
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'Charlie' }));
    expect(change).toHaveBeenCalledWith('c');
    expect(screen.getByRole('combobox')).toHaveTextContent('Charlie');
  });

  test('Home/End, typeahead and Escape operate without committing a canceled choice', async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<Select aria-label="环境" defaultValue="a" onValueChange={change} items={options} />);
    await user.tab();
    await user.keyboard(' ');
    await screen.findByRole('listbox');
    await user.keyboard('{End}');
    await waitFor(() => expect(screen.getByRole('option', { name: 'Charlie' })).toHaveFocus());
    await user.keyboard('{Home}');
    await waitFor(() => expect(screen.getByRole('option', { name: 'Alpha' })).toHaveFocus());
    await user.keyboard('c');
    await waitFor(() => expect(screen.getByRole('option', { name: 'Charlie' })).toHaveFocus());
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  test('label, placeholder, invalid and disabled states are accessible', async () => {
    const user = userEvent.setup();
    render(
      <>
        <label htmlFor="choice">选项</label>
        <Select
          id="choice"
          items={[]}
          placeholder="无可用选项"
          disabled
          invalid
          aria-describedby="reason"
        />
        <p id="reason">当前离线</p>
      </>,
    );
    const trigger = screen.getByRole('combobox', { name: '选项' });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent('无可用选项');
    expect(trigger).toHaveAttribute('aria-invalid', 'true');
    expect(trigger).toHaveAccessibleDescription('当前离线');
    await user.click(trigger);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  });

  test('controlled value only changes when the owner updates it', async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    const { rerender } = render(
      <Select aria-label="环境" value="a" onValueChange={change} items={options} />,
    );
    await user.click(screen.getByRole('combobox'));
    await user.click(await screen.findByRole('option', { name: 'Charlie' }));
    expect(change).toHaveBeenCalledWith('c');
    expect(screen.getByRole('combobox')).toHaveTextContent('Alpha');
    rerender(<Select aria-label="环境" value="c" onValueChange={change} items={options} />);
    expect(screen.getByRole('combobox')).toHaveTextContent('Charlie');
  });
});

describe('Tabs', () => {
  test('automatic activation, arrows, Home/End and disabled tabs follow the keyboard contract', async () => {
    const user = userEvent.setup();
    render(<Tabs label="内容视图" defaultValue="a" items={tabs} />);
    expect(screen.getByRole('tablist')).toHaveAccessibleName('内容视图');
    await user.tab();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Charlie' })).toHaveFocus());
    expect(screen.getByRole('tabpanel')).toHaveTextContent('Charlie 内容');
    expect(screen.getByRole('tab', { name: 'Beta' })).toBeDisabled();
    await user.keyboard('{Home}');
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveFocus());
    await user.keyboard('{End}');
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Charlie' })).toHaveFocus());
  });

  test('manual activation and controlled owner updates do not confuse focus with selection', async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    const { rerender } = render(
      <Tabs
        label="内容视图"
        value="a"
        onValueChange={change}
        activationMode="manual"
        items={tabs}
      />,
    );
    await user.tab();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Charlie' })).toHaveFocus());
    expect(screen.getByRole('tab', { name: 'Alpha' })).toHaveAttribute('aria-selected', 'true');
    expect(change).not.toHaveBeenCalled();
    await user.keyboard(' ');
    expect(change).toHaveBeenCalledWith('c');
    rerender(
      <Tabs
        label="内容视图"
        value="c"
        onValueChange={change}
        activationMode="manual"
        items={tabs}
      />,
    );
    expect(screen.getByRole('tabpanel')).toHaveAccessibleName('Charlie');
  });
});

describe('Dialog', () => {
  test('enters focus, traps both Tab directions, closes on Escape and restores trigger focus', async () => {
    const user = userEvent.setup();
    render(
      <>
        <Button>外部操作</Button>
        <Dialog trigger={<Button>打开对话框</Button>} title="编辑名称" description="仅修改本地示例">
          <Input aria-label="名称" />
          <DialogClose asChild>
            <Button>取消</Button>
          </DialogClose>
        </Dialog>
      </>,
    );
    const trigger = screen.getByRole('button', { name: '打开对话框' });
    await user.click(trigger);
    const dialog = await screen.findByRole('dialog', { name: '编辑名称' });
    expect(dialog).toHaveAccessibleDescription('仅修改本地示例');
    const input = screen.getByRole('textbox', { name: '名称' });
    await waitFor(() => expect(screen.getByRole('heading', { name: '编辑名称' })).toHaveFocus());
    await user.tab();
    await waitFor(() => expect(input).toHaveFocus());
    await user.tab({ shift: true });
    expect(screen.getByRole('button', { name: '关闭对话框' })).toHaveFocus();
    await user.tab();
    expect(input).toHaveFocus();
    expect(screen.queryByRole('button', { name: '外部操作' })).not.toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  test('controlled state, explicit initial focus and close action work together', async () => {
    const user = userEvent.setup();
    function Example() {
      const [open, setOpen] = useState(false);
      const ref = useRef<HTMLButtonElement>(null);
      return (
        <Dialog
          open={open}
          onOpenChange={setOpen}
          initialFocusRef={ref}
          trigger={<Button>预览</Button>}
          title="确认修改"
          description="模拟数据，不执行任务"
        >
          <Input aria-label="备注" />
          <DialogClose asChild>
            <Button ref={ref}>返回</Button>
          </DialogClose>
        </Dialog>
      );
    }
    render(<Example />);
    await user.click(screen.getByRole('button', { name: '预览' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '返回' })).toHaveFocus());
    await user.keyboard('{Enter}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: '预览' })).toHaveFocus();
  });

  test('nested Select restores the outer Dialog without unblocking the background', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <>
        <Button>外部操作</Button>
        <Dialog trigger={<Button>预览</Button>} title="选择示例" description="本地模拟">
          <Select aria-label="环境" items={options} defaultValue="a" />
        </Dialog>
      </>,
    );
    await user.click(screen.getByRole('button', { name: '预览' }));
    const dialog = screen.getByRole('dialog');
    expect(container).toHaveAttribute('inert');
    await user.click(screen.getByRole('combobox', { name: '环境' }));
    expect(await screen.findByRole('listbox')).toBeVisible();
    expect(dialog).toHaveAttribute('inert');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(dialog).not.toHaveAttribute('inert'));
    expect(container).toHaveAttribute('inert');
    expect(screen.getByRole('combobox', { name: '环境' })).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(container).not.toHaveAttribute('inert'));
    expect(screen.getByRole('button', { name: '预览' })).toHaveFocus();
  });
});

describe('Tooltip / Badge / EmptyState', () => {
  test('Tooltip describes the focused trigger and Escape dismisses it', async () => {
    const user = userEvent.setup();
    render(
      <Tooltip content="重新读取示例状态">
        <IconButton label="刷新" icon={<RefreshCw />} />
      </Tooltip>,
    );
    await user.tab();
    expect(await screen.findByRole('tooltip')).toHaveTextContent('重新读取示例状态');
    expect(screen.getByRole('button', { name: '刷新' })).toHaveAccessibleDescription(
      '重新读取示例状态',
    );
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新' })).toHaveFocus();
  });

  test('Tooltip opens on hover and is removed after leaving', async () => {
    const user = userEvent.setup();
    render(
      <Tooltip content="辅助说明">
        <Button>帮助</Button>
      </Tooltip>,
    );
    const trigger = screen.getByRole('button');
    await user.hover(trigger);
    expect(await screen.findByRole('tooltip')).toHaveTextContent('辅助说明');
    await user.unhover(trigger);
    await user.pointer({ target: document.body, coords: { clientX: 1000, clientY: 1000 } });
    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument());
  });

  test('EmptyState heading level follows the surrounding page outline', () => {
    render(<EmptyState headingLevel={2} title="页面级空状态" description="没有数据。" />);
    expect(screen.getByRole('heading', { level: 2, name: '页面级空状态' })).toBeVisible();
  });

  test('Badge keeps text as well as icon, and EmptyState exposes its action', async () => {
    const user = userEvent.setup();
    const click = vi.fn();
    render(
      <>
        <Badge tone="info" size="md" icon={<RefreshCw />}>
          仅模拟数据
        </Badge>
        <EmptyState
          title="暂无示例"
          description="添加一个本地示例后查看组件效果。"
          action={<Button onClick={click}>添加示例</Button>}
        />
      </>,
    );
    expect(screen.getByText('仅模拟数据').closest('.ui-badge')).toHaveAttribute(
      'data-tone',
      'info',
    );
    expect(screen.getByRole('region', { name: '暂无示例' })).toHaveTextContent(
      '添加一个本地示例后查看组件效果。',
    );
    await user.click(screen.getByRole('button', { name: '添加示例' }));
    expect(click).toHaveBeenCalledOnce();
  });
});
