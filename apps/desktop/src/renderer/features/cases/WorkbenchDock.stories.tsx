import type { Meta, StoryObj } from '@storybook/react-vite';
import { WorkbenchDock } from './WorkbenchDock';

const meta = {
  title: '接入回归/用例工作室停靠布局',
  component: WorkbenchDock,
  decorators: [
    (Story) => (
      <div style={{ height: 640, display: 'flex', flexDirection: 'column' }}>
        <Story />
      </div>
    ),
  ],
  parameters: {
    hasMain: false,
    docs: {
      description: {
        component:
          '三套布局预设、可调整面板与底部标签。设备画面需要显式选择本机设备；本预览没有 Runtime 连接。预设与尺寸写入本机 localStorage。',
      },
    },
  },
} satisfies Meta<typeof WorkbenchDock>;
export default meta;
type Story = StoryObj<typeof meta>;
export const EditPreset: Story = {};
