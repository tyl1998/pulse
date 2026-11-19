import type { Meta, StoryObj } from '@storybook/react-vite';
import { expect, fn, userEvent, within } from 'storybook/test';
import { Button } from './Button';

const meta = {
  title: '组件/Button',
  component: Button,
  args: { children: '保存示例', onClick: fn() },
} satisfies Meta<typeof Button>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: { variant: 'primary' },
  play: async ({ canvasElement, args }) => {
    await userEvent.click(within(canvasElement).getByRole('button', { name: '保存示例' }));
    await expect(args.onClick).toHaveBeenCalled();
  },
};

export const States: Story = {
  render: () => (
    <div className="sb-stack">
      <div className="sb-row">
        <Button variant="primary">主要操作</Button>
        <Button>次要操作</Button>
        <Button variant="ghost">轻量操作</Button>
        <Button variant="danger">删除示例</Button>
      </div>
      <div className="sb-row">
        <Button size="sm">小尺寸</Button>
        <Button size="md">中尺寸</Button>
        <Button size="lg">大尺寸</Button>
      </div>
      <div className="sb-row">
        <Button disabled>不可用</Button>
        <Button loading variant="primary">
          正在保存示例
        </Button>
        <Button loading>正在读取示例</Button>
      </div>
      <Button>较长操作名称也必须完整可读，不应造成窄窗口横向溢出</Button>
    </div>
  ),
};

export const Comfortable: Story = { ...States, globals: { density: 'comfortable' } };
