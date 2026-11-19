import type { Meta, StoryObj } from '@storybook/react-vite';
import { FolderTree, LayoutPanelLeft, Smartphone } from 'lucide-react';
import { CommandPalette, type CommandItem } from './CommandPalette';

const items: CommandItem[] = [
  {
    id: 'nav-cases',
    group: '导航',
    label: '转到用例',
    hint: '本地工程、用例树与可停靠工作室',
    icon: <FolderTree />,
    run: () => undefined,
  },
  {
    id: 'nav-devices',
    group: '导航',
    label: '转到设备',
    hint: '本机设备、公共设备池与我的占用',
    icon: <Smartphone />,
    run: () => undefined,
  },
  {
    id: 'layout-record',
    group: '布局',
    label: '以录制布局打开用例工作室',
    hint: '设备画面与事件流优先',
    icon: <LayoutPanelLeft />,
    run: () => undefined,
  },
  {
    id: 'layout-debug',
    group: '布局',
    label: '以调试布局打开用例工作室',
    hint: '变量与步骤进度优先',
    icon: <LayoutPanelLeft />,
    run: () => undefined,
  },
];

const meta = {
  title: '接入回归/命令面板',
  component: CommandPalette,
  decorators: [
    (Story) => (
      <div style={{ minHeight: 420 }}>
        <Story />
      </div>
    ),
  ],
  parameters: {
    hasMain: false,
    docs: {
      description: {
        component:
          '命令面板静态展开态：分组命令、搜索过滤、方向键选择与回车执行。示例命令不导航，也不触发设备动作。',
      },
    },
  },
  args: {
    open: true,
    onOpenChange: () => undefined,
    items,
  },
} satisfies Meta<typeof CommandPalette>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Open: Story = {};
