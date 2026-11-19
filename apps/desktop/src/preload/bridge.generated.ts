/* Generated from ipc.contract.json. Do not edit. Run pnpm ipc:generate. */
import { IPC_CHANNELS } from '../shared/ipc';
import type {
  AppInfo,
  ProjectCreateRequest,
  ProjectExportResult,
  ProjectWorkspace,
  PulseBridge,
  RuntimeConnection,
  RuntimeStatus,
} from '../shared/ipc';

export type IpcInvoke = (channel: string, ...args: unknown[]) => Promise<unknown>;

export function createPulseBridge(invoke: IpcInvoke): PulseBridge {
  return {
    app: {
      getInfo: () => invoke(IPC_CHANNELS.appInfo) as Promise<AppInfo>,
    },
    runtime: {
      getConnection: () =>
        invoke(IPC_CHANNELS.runtimeConnection) as Promise<RuntimeConnection | null>,
      getStatus: () => invoke(IPC_CHANNELS.runtimeStatus) as Promise<RuntimeStatus>,
      ensureRunning: () => invoke(IPC_CHANNELS.runtimeEnsure) as Promise<RuntimeStatus>,
    },
    projects: {
      pickDirectory: (title: string) =>
        invoke(IPC_CHANNELS.projectPickDirectory, title) as Promise<string | null>,
      createProject: (request: ProjectCreateRequest) =>
        invoke(IPC_CHANNELS.projectCreate, request) as Promise<ProjectWorkspace>,
      importProject: () => invoke(IPC_CHANNELS.projectImport) as Promise<ProjectWorkspace | null>,
      exportProject: (root: string) =>
        invoke(IPC_CHANNELS.projectExport, root) as Promise<ProjectExportResult | null>,
    },
  };
}
