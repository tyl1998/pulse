import { useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { Select } from './Select';

const items = [
  { value: 'local', label: '本地模拟环境' },
  { value: 'unavailable', label: '不可用模拟环境', disabled: true },
  { value: 'preview', label: '预览环境' },
  { value: 'long', label: '名称很长的模拟环境，用于检查弹层宽度、折行和窄窗口边界' },
];
const meta = {
  title: '组件/Select',
  component: Select,
  args: { 'aria-label': '示例环境', items },
} satisfies Meta<typeof Select>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Controlled: Story = {
  render: function ControlledSelect(args) {
    const [value, setValue] = useState('local');
    return (
      <div className="sb-stack">
        <label className="sb-field">
          示例环境
          <Select {...args} value={value} onValueChange={setValue} />
        </label>
        <output>当前选择：{value}</output>
        <p className="sb-note">方向键、Home/End、字符搜索和 Escape；跳过禁用选项。</p>
      </div>
    );
  },
};
export const States: Story = {
  render: () => (
    <div className="sb-grid">
      <label className="sb-field">
        尚未选择
        <Select items={items} placeholder="选择模拟环境" />
      </label>
      <label className="sb-field">
        小尺寸
        <Select items={items} defaultValue="local" size="sm" />
      </label>
      <label className="sb-field">
        大尺寸
        <Select items={items} defaultValue="preview" size="lg" />
      </label>
      <label className="sb-field">
        无效选择
        <Select items={items} invalid aria-describedby="select-error" />
        <span id="select-error">请选择一个可用的模拟环境。</span>
      </label>
      <label className="sb-field">
        禁用
        <Select items={items} defaultValue="local" disabled />
      </label>
      <label className="sb-field">
        离线
        <Select items={[]} disabled placeholder="离线，无可用选项" />
      </label>
      <label className="sb-field">
        加载中
        <Select items={[]} disabled placeholder="正在读取模拟选项" />
      </label>
      <label className="sb-field">
        长选项
        <Select items={items} defaultValue="long" />
      </label>
    </div>
  ),
};
