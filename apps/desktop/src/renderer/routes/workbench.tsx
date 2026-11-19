import { EmptyState } from '../shared/ui';
import { PageHeader } from './PageScaffold';

export function WorkbenchPage() {
  return (
    <div className="route-page">
      <PageHeader
        title="工作台"
        description="最近用例、运行中任务、我的占用与待处理项。应用壳只提供入口，不推断任务状态。"
        phase="P2 待接入"
      />
      <section className="page-grid" aria-label="工作台卡片">
        <EmptyState
          headingLevel={2}
          title="运行中任务"
          description="Runtime 任务接入后，这里显示执行端、设备与剩余预算；当前无数据来源。"
        />
        <EmptyState
          headingLevel={2}
          title="我的设备占用"
          description="公共设备池与占用回执属于 FE-P4.2；当前不展示虚构占用。"
        />
        <EmptyState
          headingLevel={2}
          title="待处理项"
          description="同步冲突、AI 授权与中断任务将在对应阶段接入；当前没有待处理数据。"
        />
      </section>
    </div>
  );
}
