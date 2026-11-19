import type { RuntimeStatus } from '../shared/ipc';

export interface ClosePolicy {
  /** Default true: closing the window hides it while the Runtime and tasks keep running. */
  closeToTray: boolean;
  /** A tray must exist, otherwise hiding the window would strand the process invisibly. */
  hasTray: boolean;
  /** Set by app 'before-quit': a real quit must be allowed to close windows. */
  quitting: boolean;
}

/** Decide whether a window 'close' event hides the window (preventDefault) or really closes it. */
export function shouldHideOnClose({ closeToTray, hasTray, quitting }: ClosePolicy): boolean {
  return closeToTray && hasTray && !quitting;
}

/**
 * 'window-all-closed' handling. macOS apps normally stay alive; with a tray the GUI also stays
 * alive on Windows/Linux so closing the last window does not kill the connection-based app.
 */
export function shouldQuitOnAllWindowsClosed({
  platform,
  closeToTray,
  hasTray,
}: Omit<ClosePolicy, 'quitting'> & { platform: NodeJS.Platform }): boolean {
  if (platform === 'darwin') return false;
  return !(closeToTray && hasTray);
}

/** One honest line for the tray menu; never claims tasks passed or stopped. */
export function runtimeSummary(status: RuntimeStatus): string {
  switch (status.kind) {
    case 'running':
      return `Runtime 已连接 · v${status.health.version} · pid ${status.health.pid}`;
    case 'not-running':
      return status.launchable ? 'Runtime 未运行（可自动启动）' : 'Runtime 未运行';
    case 'unhealthy':
      return `Runtime 异常 · pid ${status.pid} · 健康检查未通过`;
    case 'incompatible':
      return `Runtime 版本不兼容 · v${status.health.version}（需要 ${status.required}）`;
  }
}
