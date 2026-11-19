import type { Meta, StoryObj } from '@storybook/react-vite';
import { Info } from 'lucide-react';
import { Badge, type Tone } from './Badge';

const meta = {
  title: '组件/Badge',
  component: Badge,
  args: { children: '本地示例' },
} satisfies Meta<typeof Badge>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {};
export const Tones: Story = {
  render: () => (
    <div className="sb-stack">
      <div className="sb-row">
        {(
          ['neutral', 'success', 'danger', 'warning', 'accent', 'info', 'pending'] satisfies Tone[]
        ).map((tone) => (
          <Badge key={tone} tone={tone} icon={<Info />}>
            {tone} 语义示例
          </Badge>
        ))}
      </div>
      <div className="sb-row">
        <Badge size="sm">小标签</Badge>
        <Badge size="md">中标签</Badge>
      </div>
      <Badge>很长的本地标签也能在窄窗口中自然换行，不用颜色代替文字内容</Badge>
    </div>
  ),
};
