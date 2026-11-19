import type { Meta, StoryObj } from '@storybook/react-vite';
import { Info, RefreshCw } from 'lucide-react';
import {
  Badge,
  Button,
  Dialog,
  EmptyState,
  IconButton,
  Input,
  Select,
  StatusPill,
  Tabs,
  Tooltip,
} from './index';

function Foundation() {
  return (
    <div className="sb-stack">
      <div className="sb-grid">
        <section className="sb-panel sb-stack">
          <h2>操作与输入</h2>
          <div className="sb-row">
            <Button variant="primary">保存示例</Button>
            <Button disabled>不可用</Button>
            <Button loading>读取中</Button>
            <Tooltip content="只刷新本地模拟数据">
              <IconButton label="刷新示例" icon={<RefreshCw />} />
            </Tooltip>
          </div>
          <label className="sb-field">
            名称
            <Input placeholder="本地示例名称" />
          </label>
          <label className="sb-field">
            来源
            <Select
              items={[
                { value: 'local', label: '本地模拟' },
                { value: 'offline', label: '离线模拟', disabled: true },
                { value: 'preview', label: '预览模拟' },
              ]}
              defaultValue="local"
            />
          </label>
          <Dialog
            trigger={<Button>打开对话框</Button>}
            title="组件交互预览"
            description="仅模拟数据。Tab 焦点约束在弹层内，Escape 关闭后回到触发按钮。"
          >
            <label className="sb-field">
              示例备注
              <Input />
            </label>
          </Dialog>
        </section>
        <section className="sb-panel sb-stack">
          <h2>信息与状态</h2>
          <Badge tone="info" icon={<Info />}>
            本地模拟
          </Badge>
          <StatusPill value="离线（模拟）" source="模拟观察器" observedAt={null} />
          <StatusPill
            value="已连接（旧模拟观察）"
            tone="info"
            source="模拟心跳"
            observedAt={Date.parse('2026-11-02T08:59:00.000Z')}
            now={Date.parse('2026-11-02T09:00:00.000Z')}
          />
          <Tabs
            label="示例内容"
            defaultValue="summary"
            items={[
              { value: 'summary', label: '概览', content: '连接状态不等于任务运行状态。' },
              {
                value: 'details',
                label: '明细',
                content: '全部示例均不依赖 preload、Runtime、relay 或设备。',
              },
            ]}
          />
        </section>
      </div>
      <EmptyState
        title="尚无业务执行结果"
        description="FE-P0.2 只提供前端基础组件。业务状态示例均为模拟数据，不代表执行引擎已经实现。"
      />
      <section className="sb-panel sb-stack">
        <h2>设计令牌</h2>
        <div className="sb-row">
          {[
            'primary',
            'text',
            'success-fg',
            'danger-fg',
            'warning-fg',
            'accent-fg',
            'pending-fg',
          ].map((token) => (
            <div key={token} className="sb-field">
              <span className="sb-swatch" style={{ background: `var(--ui-${token})` }} />
              <code className="sb-code">{token}</code>
            </div>
          ))}
        </div>
        <div className="sb-row">
          {['xs', 'sm', 'md', 'lg', 'xl'].map((size) => (
            <span key={size} style={{ fontSize: `var(--ui-text-${size})` }}>
              Aa 字号 {size}
            </span>
          ))}
        </div>
        <p className="sb-code">
          4px 间距栅格 · 4/6/8px 圆角 · 系统字体与等宽字体 · 120/180ms 动效 · 2px 焦点环
        </p>
        <p className="sb-note">
          工具栏切换紧凑/舒适密度；弹层与页面使用同一根密度令牌。支持减少动态效果偏好，不加载远程字体。
        </p>
      </section>
    </div>
  );
}

const meta = {
  id: 'foundation',
  title: '基础/设计令牌与组件',
  component: Foundation,
} satisfies Meta<typeof Foundation>;
export default meta;
type Story = StoryObj<typeof meta>;
export const Compact: Story = { globals: { density: 'compact' } };
export const Comfortable: Story = { globals: { density: 'comfortable' } };
