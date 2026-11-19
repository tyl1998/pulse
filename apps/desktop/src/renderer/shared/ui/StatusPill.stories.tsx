import type { Meta, StoryObj } from '@storybook/react-vite';
import {
  Ban,
  CircleCheck,
  CircleHelp,
  CirclePause,
  CircleX,
  Clock,
  LoaderCircle,
  TriangleAlert,
  Unplug,
} from 'lucide-react';
import type { Tone } from './Badge';
import { StatusPill } from './StatusPill';

const now = Date.parse('2026-11-02T09:00:00.000Z');
const meta = {
  title: '组件/StatusPill',
  component: StatusPill,
  args: { value: '未知（模拟）', source: '模拟观察器', observedAt: null, now },
} satisfies Meta<typeof StatusPill>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Unknown: Story = {};
export const SimulatedBusinessStates: Story = {
  render: () => {
    const examples = [
      { value: 'passed · 通过（模拟）', tone: 'success', Icon: CircleCheck },
      { value: 'failed · 失败（模拟）', tone: 'danger', Icon: CircleX },
      { value: 'blocked · 阻断（模拟）', tone: 'warning', Icon: TriangleAlert },
      { value: 'canceled · 已取消（模拟）', tone: 'neutral', Icon: Ban },
      { value: 'interrupted · 中断（模拟）', tone: 'accent', Icon: CirclePause },
      { value: 'running · 运行中（模拟）', tone: 'info', Icon: LoaderCircle },
      { value: 'waiting · 等待（模拟）', tone: 'pending', Icon: Clock },
    ] satisfies { value: string; tone: Tone; Icon: typeof CircleCheck }[];
    return (
      <div className="sb-stack">
        <p>以下均为展示适配样例。尚未实现业务执行引擎，不是实际运行结果。</p>
        {examples.map(({ value, tone, Icon }) => (
          <StatusPill
            key={value}
            value={value}
            tone={tone}
            icon={<Icon />}
            source="模拟任务事件（无执行引擎）"
            observedAt={now - 1000}
            now={now}
          />
        ))}
      </div>
    );
  },
};

export const Freshness: Story = {
  render: () => (
    <div className="sb-stack">
      <StatusPill
        value="已连接（模拟）"
        tone="info"
        source="模拟 Runtime 心跳，不是运行中任务"
        observedAt={now}
        now={now}
      />
      <StatusPill
        value="已连接（最后一次模拟观察）"
        tone="info"
        source="模拟心跳"
        observedAt={now - 60_000}
        now={now}
      />
      <StatusPill
        value="离线（模拟）"
        icon={<Unplug />}
        source="模拟连接观察器"
        observedAt={null}
        now={now}
      />
      <StatusPill
        value="未知（模拟）"
        icon={<CircleHelp />}
        source="模拟数据缺失"
        observedAt={NaN}
        now={now}
      />
      <StatusPill value="未知（模拟）" source="模拟异常时钟" observedAt={now + 10_000} now={now} />
      <StatusPill
        value="很长的观察状态标签，用于窄窗口展示（模拟）"
        source="名称很长的模拟观察器，时间和来源不得被截掉"
        observedAt={now - 60_000}
        now={now}
      />
    </div>
  ),
};
