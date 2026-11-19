import { Menu, Tray, nativeImage } from 'electron';
import type { RuntimeStatus } from '../shared/ipc';
import { runtimeSummary } from './lifecycle';
import { TRAY_ICON_PNG_BASE64 } from './tray-icon';

export interface TrayDeps {
  /** Bring the existing window back (the renderer keeps running while hidden). */
  showWindow(): void;
  /** Quit the GUI. The Runtime is a separate user-level service and is never stopped here. */
  quit(): void;
  /** Read-only lifecycle status used for the menu label. */
  status(): Promise<RuntimeStatus>;
  platform?: NodeJS.Platform;
}

export interface InstalledTray {
  tray: Tray;
  dispose(): void;
}

const STATUS_REFRESH_MS = 10_000;

export function installTray({
  showWindow,
  quit,
  status,
  platform = process.platform,
}: TrayDeps): InstalledTray {
  const icon = nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_PNG_BASE64, 'base64'));
  const tray = new Tray(icon.resize({ width: 16, height: 16 }));
  tray.setToolTip('Pulse');

  let summary = 'Runtime 状态未知';
  const refresh = async (): Promise<void> => {
    try {
      summary = runtimeSummary(await status());
    } catch {
      summary = 'Runtime 状态未知';
    }
    rebuild();
  };
  const rebuild = (): void => {
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: '打开 Pulse', click: showWindow },
        { type: 'separator' },
        { label: summary, enabled: false },
        { type: 'separator' },
        { label: '退出 Pulse（不停止 Runtime）', click: quit },
      ]),
    );
  };

  rebuild();
  void refresh();
  const timer = setInterval(() => void refresh(), STATUS_REFRESH_MS);
  timer.unref?.();
  if (platform === 'win32') tray.on('click', showWindow);

  return {
    tray,
    dispose: () => {
      clearInterval(timer);
      tray.destroy();
    },
  };
}
