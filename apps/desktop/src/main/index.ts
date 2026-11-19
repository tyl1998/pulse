import { app, BrowserWindow, ipcMain, session, type Tray } from 'electron';
import path from 'node:path';
import { IPC_CHANNELS, type AppInfo } from '../shared/ipc';
import { shouldHideOnClose, shouldQuitOnAllWindowsClosed } from './lifecycle';
import { registerProjectHandlers } from './projects';
import { resolveRuntimeLaunchCommand, RuntimeManager } from './runtime-manager';
import { isAllowedNavigation, secureWebPreferences } from './security';
import { installTray } from './tray';

const devServerUrl: string | undefined = MAIN_WINDOW_VITE_DEV_SERVER_URL || undefined;

// Default: closing the window hides it; ``PULSE_CLOSE_TO_TRAY=0`` makes close mean quit.
const closeToTray = process.env.PULSE_CLOSE_TO_TRAY !== '0';

app.enableSandbox();

// Dev-only: lets scripts/stream-bench.mjs drive the P0.7 latency bench over CDP.
if (devServerUrl && process.env.PULSE_REMOTE_DEBUG_PORT) {
  app.commandLine.appendSwitch('remote-debugging-port', process.env.PULSE_REMOTE_DEBUG_PORT);
}

const runtimeManager = new RuntimeManager({
  desktopVersion: app.getVersion(),
  launchCommand: () =>
    resolveRuntimeLaunchCommand({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      appPath: app.getAppPath(),
    }),
});

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let trayDispose: (() => void) | null = null;
let quitting = false;

function showMainWindow(): void {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: secureWebPreferences(path.join(__dirname, 'preload.js')),
  });
  mainWindow = win;
  win.once('ready-to-show', () => win.show());
  win.on('close', (event) => {
    if (shouldHideOnClose({ closeToTray, hasTray: tray !== null, quitting })) {
      event.preventDefault();
      win.hide();
    }
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });

  if (devServerUrl) {
    void win.loadURL(devServerUrl);
  } else {
    void win.loadFile(path.join(__dirname, `../renderer/${MAIN_WINDOW_VITE_NAME}/index.html`));
  }
  return win;
}

app.on('web-contents-created', (_event, contents) => {
  contents.on('will-navigate', (event, url) => {
    if (!isAllowedNavigation(url, devServerUrl)) event.preventDefault();
  });
  contents.on('will-attach-webview', (event) => event.preventDefault());
  // External links will go through a shell.openExternal whitelist (later phase).
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
});

void app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) =>
    callback(false),
  );
  session.defaultSession.setPermissionCheckHandler(() => false);

  ipcMain.handle(IPC_CHANNELS.appInfo, (): AppInfo => ({
    version: app.getVersion(),
    platform: process.platform,
  }));

  // Only a healthy, version-compatible Runtime reaches the renderer. The renderer still probes
  // /health itself; main re-checks because the discovery file may be stale or incompatible.
  ipcMain.handle(IPC_CHANNELS.runtimeConnection, () => runtimeManager.getConnection());
  ipcMain.handle(IPC_CHANNELS.runtimeStatus, () => runtimeManager.status());
  ipcMain.handle(IPC_CHANNELS.runtimeEnsure, () => runtimeManager.ensureRunning());
  registerProjectHandlers();

  try {
    const installed = installTray({
      showWindow: showMainWindow,
      quit: () => {
        quitting = true;
        app.quit();
      },
      status: () => runtimeManager.status(),
    });
    tray = installed.tray;
    trayDispose = installed.dispose;
  } catch (error) {
    // Without a tray the close-to-tray policy degrades to a normal close; never trap a
    // windowless process on a desktop that cannot show the icon.
    console.warn(
      `Pulse tray unavailable: ${error instanceof Error ? error.message : String(error)}`,
    );
  }

  createWindow();
  app.on('activate', showMainWindow);

  // Do not block the window: the renderer's monitor retries until the Runtime is ready.
  void runtimeManager.ensureRunning().then((status) => {
    if (status.kind !== 'running') {
      console.warn(`Pulse Runtime is not ready: ${status.detail}`);
    }
  });
});

app.on('before-quit', () => {
  quitting = true;
});

// Closing the GUI never stops the Runtime (R05): the Runtime is an independent user-level
// service (launchd / systemd --user / scheduled task); the GUI only reads runtime.json.
app.on('window-all-closed', () => {
  if (
    shouldQuitOnAllWindowsClosed({
      platform: process.platform,
      closeToTray,
      hasTray: tray !== null,
    })
  ) {
    app.quit();
  }
});

app.on('will-quit', () => {
  trayDispose?.();
  trayDispose = null;
  tray = null;
});
