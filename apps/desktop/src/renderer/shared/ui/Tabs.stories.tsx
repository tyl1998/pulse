import { useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { Tabs } from './Tabs';

const items = [
  { value: 'summary', label: '概览', content: '仅显示模拟概览。' },
  { value: 'disabled', label: '不可用', content: '不可用内容', disabled: true },
  { value: 'details', label: '详细信息', content: '模拟详细信息，不代表任务执行结果。' },
  {
    value: 'long',
    label: '长文本边界与窄窗口检查',
    content: '长内容会在可用宽度内换行。'.repeat(15),
  },
];
const meta = {
  title: '组件/Tabs',
  component: Tabs,
  args: { label: '示例视图', items, defaultValue: 'summary' },
} satisfies Meta<typeof Tabs>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Automatic: Story = {};
export const ManualControlled: Story = {
  render: function ControlledTabs() {
    const [value, setValue] = useState('summary');
    return (
      <div className="sb-stack">
        <Tabs
          label="手动激活视图"
          items={items}
          value={value}
          onValueChange={setValue}
          activationMode="manual"
        />
        <output>当前视图：{value}</output>
        <p className="sb-note">方向键仅移动焦点，Enter/空格才切换内容。</p>
      </div>
    );
  },
};
export const Comfortable: Story = { globals: { density: 'comfortable' } };
