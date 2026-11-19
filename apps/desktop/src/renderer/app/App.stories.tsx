import type { Meta, StoryObj } from '@storybook/react-vite';
import { App } from './App';

const meta = {
  id: 'integration-app',
  title: '接入回归/应用壳',
  component: App,
  beforeEach: () => {
    if (window.pulse)
      throw new Error('This regression story requires an isolated browser without preload.');
  },
  decorators: [
    (Story) => (
      <div style={{ height: 'min(80vh, 760px)', minHeight: 560 }}>
        <Story />
      </div>
    ),
  ],
  parameters: {
    hasMain: true,
    docs: {
      description: {
        component:
          '真实应用壳：路由、主导航、顶栏连接状态、命令面板与可停靠工作室。无 preload，因此 Runtime 查找不会启动，也不请求设备或服务端。',
      },
    },
  },
} satisfies Meta<typeof App>;
export default meta;
type Story = StoryObj<typeof meta>;
export const WithoutPreload: Story = {};
