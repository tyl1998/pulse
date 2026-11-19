import type { Meta, StoryObj } from '@storybook/react-vite';
import { Search, Unplug } from 'lucide-react';
import { fn } from 'storybook/test';
import { Button } from './Button';
import { EmptyState } from './EmptyState';

const meta = {
  title: '组件/EmptyState',
  component: EmptyState,
  args: {
    title: '暂无本地示例',
    description: '添加示例后可查看组件效果。此处不创建业务任务。',
    action: <Button onClick={fn()}>添加示例</Button>,
  },
} satisfies Meta<typeof EmptyState>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const NoMatches: Story = {
  args: {
    title: '没有匹配的示例',
    description: '调整筛选条件后重试。',
    icon: <Search />,
    action: <Button onClick={fn()}>清除筛选</Button>,
  },
};
export const Offline: Story = {
  args: {
    title: '模拟来源离线',
    description: '尚未获取数据，不把离线视为列表为空或操作成功。',
    icon: <Unplug />,
    action: <Button disabled>刷新不可用</Button>,
  },
};
export const LongContent: Story = {
  args: {
    title: '这是一个用于窄窗口边界验证的很长的空状态标题',
    description: '仅使用本地模拟数据。很长的说明应保持可读、自然换行，并且不覆盖操作入口。'.repeat(
      4,
    ),
  },
};
