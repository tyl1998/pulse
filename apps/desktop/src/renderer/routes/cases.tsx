import { WorkbenchDock } from '../features/cases/WorkbenchDock';
import { PageHeader } from './PageScaffold';

export function CasesPage() {
  return (
    <div className="route-page route-page-fill">
      <PageHeader
        title="用例"
        description="拖拽排序与内联编辑用例步骤，展开公共流程和条件循环块。在检查模式中验证定位器并插入步骤；草稿创作不会执行设备动作。"
      />
      <WorkbenchDock />
    </div>
  );
}
