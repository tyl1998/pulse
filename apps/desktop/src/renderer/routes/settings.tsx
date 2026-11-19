import { PlaceholderPage } from './PageScaffold';

export function SettingsPage() {
  return (
    <PlaceholderPage
      title="设置"
      description="Runtime、服务端、模型只读列表、快捷键、外观与关于。当前壳层只持久化工作区布局。"
      phase="FE-P2.16"
      emptyTitle="设置页未接入"
      emptyDescription="设置页、快捷键与 i18n 基础将在 FE-P2.16 接入。布局按本机存储持久化，未写入 Runtime 配置。"
    />
  );
}
