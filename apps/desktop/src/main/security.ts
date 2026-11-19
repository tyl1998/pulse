import type { WebPreferences } from 'electron';

/** Hardened defaults for every BrowserWindow (desktop-frontend.md §3). */
export function secureWebPreferences(preload: string): WebPreferences {
  return {
    preload,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInWorker: false,
    nodeIntegrationInSubFrames: false,
    webSecurity: true,
    allowRunningInsecureContent: false,
    webviewTag: false,
    spellcheck: false,
  };
}

/** Only the app's own origin (dev server or packaged file) may be navigated to. */
export function isAllowedNavigation(target: string, devServerUrl: string | undefined): boolean {
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return false;
  }
  if (devServerUrl) {
    return url.origin === new URL(devServerUrl).origin;
  }
  return url.protocol === 'file:';
}
