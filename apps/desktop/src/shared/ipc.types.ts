/**
 * IPC value types shared by main / preload / renderer.
 * Channel names and the bridge shape are generated from `ipc.contract.json`; run
 * `pnpm ipc:generate` after editing the contract instead of editing generated files.
 */
export interface AppInfo {
  version: string;
  platform: string;
}

/** New project request; the main process creates the directory and starter documents. */
export interface ProjectCreateRequest {
  parentDir: string;
  projectId: string;
  name: string;
}

/** Materialized local project (created or imported), before the renderer opens it in Runtime. */
export interface ProjectWorkspace {
  root: string;
  projectId: string;
  fileCount: number;
  totalBytes: number;
}

/** Result of writing a validated export package; never includes the project root path. */
export interface ProjectExportResult {
  targetPath: string;
  fileCount: number;
  totalBytes: number;
}

/** Loopback endpoint + token read by the main process from the Runtime's runtime.json. */
export interface RuntimeConnection {
  port: number;
  token: string;
  pid: number;
  version: string;
}

/** Sanitized authenticated /health response (P2.1 discovery; no cookies or extra fields). */
export interface RuntimeHealth {
  status: 'ok';
  version: string;
  pid: number;
  uptime_s: number;
}

/**
 * What the main process knows about the local Runtime. The GUI never infers "running" from a
 * discovery file alone: a live PID must also answer authenticated /health with the same
 * PID/version, and that version must be compatible with this desktop build.
 */
export type RuntimeStatus =
  | { kind: 'running'; connection: RuntimeConnection; health: RuntimeHealth }
  | { kind: 'not-running'; launchable: boolean; detail: string }
  | { kind: 'unhealthy'; pid: number; detail: string }
  | {
      kind: 'incompatible';
      connection: RuntimeConnection;
      health: RuntimeHealth;
      required: string;
      detail: string;
    };
