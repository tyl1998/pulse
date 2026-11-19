/* Generated from ipc.contract.json. Do not edit. Run pnpm ipc:generate. */
import type {
  AppInfo,
  ProjectCreateRequest,
  ProjectExportResult,
  ProjectWorkspace,
  RuntimeConnection,
  RuntimeStatus,
} from './ipc.types';

export const IPC_CHANNELS = {
  appInfo: 'pulse:app-info',
  runtimeConnection: 'pulse:runtime-connection',
  runtimeStatus: 'pulse:runtime-status',
  runtimeEnsure: 'pulse:runtime-ensure',
  projectPickDirectory: 'pulse:project-pick-directory',
  projectCreate: 'pulse:project-create',
  projectImport: 'pulse:project-import',
  projectExport: 'pulse:project-export',
} as const;

export type IpcChannel = (typeof IPC_CHANNELS)[keyof typeof IPC_CHANNELS];

export interface PulseBridge {
  app: {
    getInfo(): Promise<AppInfo>;
  };
  runtime: {
    getConnection(): Promise<RuntimeConnection | null>;
    getStatus(): Promise<RuntimeStatus>;
    ensureRunning(): Promise<RuntimeStatus>;
  };
  projects: {
    pickDirectory(title: string): Promise<string | null>;
    createProject(request: ProjectCreateRequest): Promise<ProjectWorkspace>;
    importProject(): Promise<ProjectWorkspace | null>;
    exportProject(root: string): Promise<ProjectExportResult | null>;
  };
}
