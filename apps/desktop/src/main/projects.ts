import { app, BrowserWindow, dialog, ipcMain, type OpenDialogOptions } from 'electron';
import path from 'node:path';
import { IPC_CHANNELS } from '../shared/ipc';
import type { ProjectCreateRequest, ProjectExportResult, ProjectWorkspace } from '../shared/ipc';
import {
  collectProjectPackage,
  createProjectWorkspace,
  importProjectPackage,
  readProjectPackage,
  writeProjectPackage,
} from './project-package';

const MAX_TITLE = 120;

function focusedWindow(): BrowserWindow | null {
  return BrowserWindow.getFocusedWindow();
}

async function showDirectory(title: string): Promise<string | null> {
  const options: OpenDialogOptions = {
    title,
    properties: ['openDirectory', 'createDirectory'],
  };
  const parent = focusedWindow();
  const result = parent
    ? await dialog.showOpenDialog(parent, options)
    : await dialog.showOpenDialog(options);
  return result.canceled ? null : (result.filePaths[0] ?? null);
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error(`${label}不能为空`);
  return value;
}

function requireCreateRequest(value: unknown): ProjectCreateRequest {
  if (typeof value !== 'object' || value === null) throw new Error('工程创建参数无效');
  const record = value as Record<string, unknown>;
  return {
    parentDir: requireString(record['parentDir'], '工程所在目录'),
    projectId: requireString(record['projectId'], '工程标识'),
    name: requireString(record['name'], '工程名称'),
  };
}

async function exportWithDialog(root: string): Promise<ProjectExportResult | null> {
  const packageData = await collectProjectPackage(root);
  const suggested = `${packageData.project_id}.pulse-project.json`;
  const parent = focusedWindow();
  const options = {
    title: '导出 Pulse 工程包',
    defaultPath: path.join(app.getPath('documents'), suggested),
    filters: [{ name: 'Pulse 工程包', extensions: ['json'] }],
  };
  const result = parent
    ? await dialog.showSaveDialog(parent, options)
    : await dialog.showSaveDialog(options);
  if (result.canceled || !result.filePath) return null;
  return writeProjectPackage(result.filePath, packageData);
}

async function importWithDialogs(): Promise<ProjectWorkspace | null> {
  const parent = focusedWindow();
  const fileOptions: OpenDialogOptions = {
    title: '选择 Pulse 工程包',
    properties: ['openFile'],
    filters: [{ name: 'Pulse 工程包', extensions: ['json'] }],
  };
  const fileResult = parent
    ? await dialog.showOpenDialog(parent, fileOptions)
    : await dialog.showOpenDialog(fileOptions);
  const packagePath = fileResult.filePaths[0];
  if (fileResult.canceled || !packagePath) return null;
  const packageData = await readProjectPackage(packagePath);
  const destination = await showDirectory('选择工程导入目标文件夹');
  if (!destination) return null;
  return importProjectPackage(packageData, destination);
}

/** Register whitelisted project-management IPC. All dialogs and filesystem writes stay in main. */
export function registerProjectHandlers(): void {
  ipcMain.handle(IPC_CHANNELS.projectPickDirectory, async (_event, title: unknown) => {
    const label = requireString(title, '对话框标题');
    return showDirectory(label.slice(0, MAX_TITLE));
  });

  ipcMain.handle(IPC_CHANNELS.projectCreate, async (_event, request: unknown) => {
    return createProjectWorkspace(requireCreateRequest(request));
  });

  ipcMain.handle(IPC_CHANNELS.projectImport, async () => importWithDialogs());

  ipcMain.handle(IPC_CHANNELS.projectExport, async (_event, root: unknown) => {
    return exportWithDialog(requireString(root, '工程目录'));
  });
}
