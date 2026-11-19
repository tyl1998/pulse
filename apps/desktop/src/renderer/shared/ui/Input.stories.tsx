import { useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { expect, userEvent, within } from 'storybook/test';
import { Input } from './Input';

const meta = {
  title: '组件/Input',
  component: Input,
  args: { 'aria-label': '示例名称', placeholder: '输入名称' },
} satisfies Meta<typeof Input>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Controlled: Story = {
  render: function ControlledInput(args) {
    const [value, setValue] = useState('');
    return (
      <div className="sb-stack">
        <label className="sb-field">
          示例名称
          <Input {...args} value={value} onChange={(event) => setValue(event.target.value)} />
        </label>
        <output>当前值：{value || '空'}</output>
      </div>
    );
  },
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    await userEvent.type(canvas.getByRole('textbox', { name: '示例名称' }), 'local-demo');
    await expect(canvas.getByRole('status')).toHaveTextContent('local-demo');
  },
};

export const States: Story = {
  render: () => (
    <div className="sb-grid">
      <label className="sb-field">
        正常
        <Input defaultValue="本地示例" />
      </label>
      <label className="sb-field">
        小尺寸
        <Input size="sm" placeholder="搜索示例" />
      </label>
      <label className="sb-field">
        大尺寸
        <Input size="lg" placeholder="输入名称" />
      </label>
      <label className="sb-field">
        只读
        <Input readOnly value="本地只读值" />
      </label>
      <label className="sb-field">
        禁用
        <Input disabled value="离线不可编辑" />
      </label>
      <label className="sb-field">
        无效输入
        <Input invalid defaultValue="" aria-describedby="input-error" />
        <span id="input-error">名称不能为空。</span>
      </label>
      <label className="sb-field">
        长文本
        <Input defaultValue="这是用于检查窄窗口的很长很长的本地示例名称，不包含任何真实设备信息" />
      </label>
    </div>
  ),
};
