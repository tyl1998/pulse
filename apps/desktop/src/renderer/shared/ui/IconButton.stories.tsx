import type { Meta, StoryObj } from '@storybook/react-vite';
import { RefreshCw, Trash2 } from 'lucide-react';
import { fn } from 'storybook/test';
import { IconButton } from './IconButton';

const meta = {
  title: '组件/IconButton',
  component: IconButton,
  args: { label: '刷新示例', icon: <RefreshCw />, onClick: fn() },
} satisfies Meta<typeof IconButton>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const States: Story = {
  render: () => (
    <div className="sb-stack">
      <div className="sb-row">
        <IconButton label="刷新示例（小）" icon={<RefreshCw />} size="sm" />
        <IconButton label="刷新示例（中）" icon={<RefreshCw />} variant="primary" />
        <IconButton label="刷新示例（大）" icon={<RefreshCw />} size="lg" />
        <IconButton label="删除示例" icon={<Trash2 />} variant="danger" />
        <IconButton label="正在刷新示例" icon={<RefreshCw />} loading />
        <IconButton label="刷新不可用" icon={<RefreshCw />} disabled />
      </div>
      <p className="sb-note">仅图标按钮必须传入 label；禁用原因不能只放在不可聚焦的 Tooltip 中。</p>
    </div>
  ),
};
