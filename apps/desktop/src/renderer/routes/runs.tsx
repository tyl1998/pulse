import { PlaceholderPage } from './PageScaffold';

export function RunsPage() {
  return (
    <PlaceholderPage
      title="运行"
      description="运行中任务、历史、用例集与计划。状态必须来自 Runtime 状态机并带来源与时间。"
      phase="FE-P2.12 / P9"
      emptyTitle="运行面板未接入"
      emptyDescription="运行对话框、预检、控制条与步骤进度将在 FE-P2.12 接入；用例集与计划属于 P9。"
    />
  );
}
