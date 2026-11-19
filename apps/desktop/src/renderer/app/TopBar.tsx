import { Bot, CloudOff, FolderOpen, Search } from 'lucide-react';
import { useProjectStore } from '../features/projects/projectStore';
import { RuntimeIndicator } from '../features/runtime-status/RuntimeIndicator';
import { StatusPill } from '../shared/ui';

export function TopBar({ onOpenPalette }: { onOpenPalette: () => void }) {
  const { state: projectState, store: projectStore } = useProjectStore();
  const projectLabel =
    projectState.status === 'loading'
      ? '正在打开工程…'
      : (projectState.project?.name ?? '未打开工程');

  return (
    <header className="app-topbar">
      <div className="app-brand" aria-label="Pulse 本地客户端">
        <span className="app-brand-mark" aria-hidden="true">
          P
        </span>
        <span className="app-brand-name">Pulse</span>
      </div>

      <button
        type="button"
        className="app-project"
        data-state={projectState.project ? 'ready' : 'empty'}
        onClick={projectStore.openDialog}
        aria-haspopup="dialog"
        title={`工程管理：${projectLabel}`}
      >
        <FolderOpen aria-hidden="true" />
        <span>{projectLabel}</span>
      </button>

      <button
        type="button"
        className="app-command-trigger"
        onClick={onOpenPalette}
        aria-keyshortcuts="Meta+K Control+K"
      >
        <Search aria-hidden="true" />
        <span>搜索命令</span>
        <kbd className="ui-kbd" aria-hidden="true">
          ⌘K
        </kbd>
      </button>

      <div className="app-connections" role="group" aria-label="连接状态">
        <RuntimeIndicator />
        <div className="app-connection" data-testid="server-indicator">
          <StatusPill
            className="app-connection-pill"
            value="服务端未配置"
            source="本机配置（P3 未接入）"
            observedAt={null}
            tone="neutral"
            icon={<CloudOff />}
          />
        </div>
        <div className="app-connection" data-testid="model-indicator">
          <StatusPill
            className="app-connection-pill"
            value="模型未接入"
            source="本机配置（P6 未接入）"
            observedAt={null}
            tone="neutral"
            icon={<Bot />}
          />
        </div>
      </div>

      <div className="app-user">
        <span>本地用户</span>
        <span className="app-user-meta">未登录 · P3</span>
      </div>
    </header>
  );
}
