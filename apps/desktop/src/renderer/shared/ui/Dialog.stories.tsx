import { useRef, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { expect, userEvent, waitFor, within } from 'storybook/test';
import { Button } from './Button';
import { Dialog, DialogClose } from './Dialog';
import { Input } from './Input';
import { Select } from './Select';

const meta = {
  title: '组件/Dialog',
  component: Dialog,
  args: {
    trigger: <Button>编辑示例</Button>,
    title: '编辑本地示例',
    description: '仅修改此预览中的模拟数据，不调用 Runtime。',
    children: (
      <label className="sb-field">
        名称
        <Input defaultValue="local-demo" />
      </label>
    ),
  },
} satisfies Meta<typeof Dialog>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  play: async ({ canvasElement }) => {
    const body = within(canvasElement.ownerDocument.body);
    const trigger = within(canvasElement).getByRole('button', { name: '编辑示例' });
    await userEvent.click(trigger);
    await expect(body.getByRole('dialog')).toHaveAccessibleName('编辑本地示例');
    await expect(body.getByRole('heading', { name: '编辑本地示例' })).toHaveFocus();
    await userEvent.tab();
    await expect(body.getByRole('textbox', { name: '名称' })).toHaveFocus();
    await userEvent.tab({ shift: true });
    await expect(body.getByRole('button', { name: '关闭对话框' })).toHaveFocus();
    await userEvent.tab();
    await expect(body.getByRole('textbox', { name: '名称' })).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(trigger).toHaveFocus());
  },
};

export const Controlled: Story = {
  render: function ControlledDialog(args) {
    const [open, setOpen] = useState(false);
    const [saved, setSaved] = useState(false);
    const cancelRef = useRef<HTMLButtonElement>(null);
    return (
      <div className="sb-stack">
        <Dialog {...args} open={open} onOpenChange={setOpen} initialFocusRef={cancelRef}>
          <div className="sb-stack">
            <label className="sb-field">
              名称
              <Input defaultValue="local-demo" />
            </label>
            <label className="sb-field">
              类型
              <Select
                items={[
                  { value: 'local', label: '本地模拟' },
                  { value: 'preview', label: '预览模拟' },
                ]}
                defaultValue="local"
              />
            </label>
            <div className="sb-row">
              <DialogClose asChild>
                <Button ref={cancelRef}>取消</Button>
              </DialogClose>
              <Button
                variant="primary"
                onClick={() => {
                  setSaved(true);
                  setOpen(false);
                }}
              >
                保存示例
              </Button>
            </div>
          </div>
        </Dialog>
        <output>{saved ? '模拟数据已保存' : '尚未修改模拟数据'}</output>
      </div>
    );
  },
};

export const LongContent: Story = {
  args: {
    title: '用于窄窗口验证的较长对话框标题，必须完整换行并保留关闭入口',
    children: (
      <div className="sb-stack">
        {Array.from({ length: 12 }, (_, index) => (
          <p key={index}>
            本地模拟说明 {index + 1}：此处不包含真实
            token、设备信息或历史实测证据。内容超出可视区时只在对话框内滚动。
          </p>
        ))}
        <DialogClose asChild>
          <Button>返回示例</Button>
        </DialogClose>
      </div>
    ),
  },
};
