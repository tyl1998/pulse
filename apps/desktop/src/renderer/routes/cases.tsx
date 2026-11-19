import { WorkbenchDock } from '../features/cases/WorkbenchDock';
import { PageHeader } from './PageScaffold';

export function CasesPage() {
  return (
    <div className="route-page route-page-fill">
      <PageHeader
        title="用例"
        description="查看用例草稿与设备画面。在检查模式中选中元素，验证定位器并插入步骤；检查与草稿创作不会执行设备动作。"
      />
      <WorkbenchDock />
    </div>
  );
}
