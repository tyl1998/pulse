import type { Meta, StoryObj } from '@storybook/react-vite';
import { Info, RefreshCw } from 'lucide-react';
import { Button } from './Button';
import { IconButton } from './IconButton';
import { Tooltip } from './Tooltip';

const meta = {
  title: '组件/Tooltip',
  component: Tooltip,
  args: {
    content: '刷新只更新本地示例，不访问设备。',
    children: <IconButton label="刷新示例" icon={<RefreshCw />} />,
  },
} satisfies Meta<typeof Tooltip>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const LongText: Story = {
  args: {
    content:
      '这是较长的辅助说明：键盘聚焦或鼠标悬停都能读取，Escape 可关闭；指针可以移入说明区域，内容在窄窗口中换行。',
    children: <IconButton label="查看说明" icon={<Info />} />,
  },
};
export const DisabledReason: Story = {
  render: () => (
    <div className="sb-stack">
      <Button disabled aria-describedby="disabled-reason">
        刷新示例
      </Button>
      <p id="disabled-reason">离线时不可刷新；原因始终可见，不依赖禁用按钮的 Tooltip。</p>
    </div>
  ),
};
