import { spawn, type SpawnOptions } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import { access, readFile } from 'node:fs/promises';
import { get as httpGet } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import type { RuntimeConnection, RuntimeHealth, RuntimeStatus } from '../shared/ipc';
import { runtimeCompatibility } from '../shared/runtime-version';

type Env = Record<string, string | undefined>;

/** Must match runtime/pulse_runtime/paths.py:runtime_dir. */
export function runtimeDir(
  platform: NodeJS.Platform = process.platform,
  env: Env = process.env,
  home: string = os.homedir(),
): string {
  if (env.PULSE_RUNTIME_DIR) return env.PULSE_RUNTIME_DIR;
  const p = platform === 'win32' ? path.win32 : path.posix;
  if (platform === 'darwin') {
    return p.join(home, 'Library', 'Application Support', 'Pulse', 'runtime');
  }
  if (platform === 'win32') {
    return p.join(env.LOCALAPPDATA ?? p.join(home, 'AppData', 'Local'), 'Pulse', 'runtime');
  }
  return p.join(env.XDG_STATE_HOME ?? p.join(home, '.local', 'state'), 'pulse', 'runtime');
}

/** Strictly validate runtime.json; anything unexpected is treated as "no runtime". */
export function parseConnection(raw: string): RuntimeConnection | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof data !== 'object' || data === null) return null;
  const { port, token, pid, version } = data as Record<string, unknown>;
  if (
    typeof port !== 'number' ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    typeof token !== 'string' ||
    !/^[A-Za-z0-9_-]{16,256}$/.test(token) ||
    typeof pid !== 'number' ||
    !Number.isInteger(pid) ||
    pid < 1 ||
    typeof version !== 'string'
  ) {
    return null;
  }
  return { port, token, pid, version };
}

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    // ESRCH: gone (stale file); EPERM: another user's process, not our user-level Runtime.
    return false;
  }
}

/** Raw discovery state. A malformed file still reports the PID it claimed for diagnostics. */
export interface RuntimeDiscovery {
  connection: RuntimeConnection | null;
  pid: number | null;
  detail: string | null;
}

export async function readRuntimeDiscovery(dir: string = runtimeDir()): Promise<RuntimeDiscovery> {
  let raw: string;
  try {
    raw = await readFile(path.join(dir, 'runtime.json'), 'utf8');
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code ?? 'read error';
    return {
      connection: null,
      pid: null,
      detail: code === 'ENOENT' ? 'runtime.json 不存在' : `runtime.json 无法读取（${code}）`,
    };
  }
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return { connection: null, pid: null, detail: 'runtime.json 不是有效 JSON' };
  }
  const claimed =
    typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {};
  const rawPid = claimed.pid;
  const pid =
    typeof rawPid === 'number' && Number.isSafeInteger(rawPid) && rawPid > 0 ? rawPid : null;
  const connection = parseConnection(raw);
  return {
    connection,
    pid,
    detail: connection === null ? 'runtime.json 内容不完整或格式异常' : null,
  };
}

/** Read the current Runtime connection. Never cached: the Runtime may restart at any time. */
export async function readRuntimeConnection(
  dir: string = runtimeDir(),
  isAlive: (pid: number) => boolean = pidAlive,
): Promise<RuntimeConnection | null> {
  const discovery = await readRuntimeDiscovery(dir);
  return discovery.connection && isAlive(discovery.connection.pid) ? discovery.connection : null;
}

const MAX_HEALTH_BYTES = 64 * 1024;

/**
 * Authenticated loopback health probe. Deliberately no proxy and no redirect following, and the
 * Bearer token never appears in logs or returned diagnostics.
 */
export function probeRuntimeHealth(
  connection: RuntimeConnection,
  timeoutMs = 2000,
): Promise<RuntimeHealth | null> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: RuntimeHealth | null): void => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const request = httpGet(
      {
        host: '127.0.0.1',
        port: connection.port,
        path: '/health',
        method: 'GET',
        headers: { Authorization: `Bearer ${connection.token}`, Accept: 'application/json' },
        agent: false,
        timeout: timeoutMs,
      },
      (response) => {
        if (response.statusCode !== 200) {
          response.resume();
          finish(null);
          return;
        }
        let body = '';
        let size = 0;
        response.setEncoding('utf8');
        response.on('data', (chunk: string) => {
          size += chunk.length;
          if (size > MAX_HEALTH_BYTES) {
            request.destroy();
            finish(null);
          } else {
            body += chunk;
          }
        });
        response.on('error', () => finish(null));
        response.on('end', () => {
          try {
            const data = JSON.parse(body) as Record<string, unknown>;
            if (
              data.status === 'ok' &&
              data.pid === connection.pid &&
              data.version === connection.version &&
              typeof data.uptime_s === 'number' &&
              Number.isFinite(data.uptime_s) &&
              data.uptime_s >= 0
            ) {
              finish({
                status: 'ok',
                version: connection.version,
                pid: connection.pid,
                uptime_s: data.uptime_s,
              });
              return;
            }
          } catch {
            // fall through to null
          }
          finish(null);
        });
      },
    );
    request.on('timeout', () => {
      request.destroy();
      finish(null);
    });
    request.on('error', () => finish(null));
  });
}

export interface RuntimeLaunchCommand {
  command: string;
  args: string[];
  cwd?: string;
  description: string;
}

export interface LaunchCommandOptions {
  platform?: NodeJS.Platform;
  env?: Env;
  isPackaged?: boolean;
  resourcesPath?: string;
  appPath?: string;
  canExecute?: (file: string) => Promise<boolean>;
}

async function executable(file: string, platform: NodeJS.Platform): Promise<boolean> {
  const mode = platform === 'win32' ? fsConstants.F_OK : fsConstants.F_OK | fsConstants.X_OK;
  try {
    await access(file, mode);
    return true;
  } catch {
    return false;
  }
}

/**
 * Find a Runtime executable. Packaged builds use the bundled PyInstaller output; a dev checkout
 * falls back to the repo's .venv. ``PULSE_RUNTIME_BIN`` always wins.
 */
export async function resolveRuntimeLaunchCommand({
  platform = process.platform,
  env = process.env,
  isPackaged = false,
  resourcesPath,
  appPath,
  canExecute = (file: string) => executable(file, platform),
}: LaunchCommandOptions = {}): Promise<RuntimeLaunchCommand | null> {
  const explicit = env.PULSE_RUNTIME_BIN?.trim();
  if (explicit) {
    return (await canExecute(explicit))
      ? { command: explicit, args: ['serve'], description: 'PULSE_RUNTIME_BIN' }
      : null;
  }

  if (isPackaged && resourcesPath) {
    const exe = platform === 'win32' ? 'pulse-runtime.exe' : 'pulse-runtime';
    const candidates = [
      path.join(resourcesPath, 'runtime', exe),
      path.join(resourcesPath, 'runtime', 'pulse-runtime', exe),
    ];
    for (const command of candidates) {
      if (await canExecute(command)) {
        return { command, args: ['serve'], description: 'bundled Runtime' };
      }
    }
    return null;
  }

  if (isPackaged) return null;

  const p = platform === 'win32' ? path.win32 : path.posix;
  const root = appPath ? p.resolve(appPath, '..', '..') : process.cwd();
  const python =
    env.PULSE_RUNTIME_PYTHON?.trim() ||
    p.join(
      root,
      '.venv',
      platform === 'win32' ? 'Scripts' : 'bin',
      platform === 'win32' ? 'python.exe' : 'python',
    );
  if (!(await canExecute(python))) return null;
  return {
    command: python,
    args: ['-m', 'pulse_runtime', 'serve'],
    cwd: root,
    description: 'checkout Runtime',
  };
}

export interface RuntimeChild {
  pid?: number;
  once(
    event: 'exit',
    listener: (code: number | null, signal: NodeJS.Signals | null) => void,
  ): unknown;
  once(event: 'error', listener: (error: Error) => void): unknown;
  unref?(): void;
}

export type SpawnRuntime = (command: string, args: string[], options: SpawnOptions) => RuntimeChild;

export interface RuntimeManagerOptions {
  dir?: string;
  desktopVersion: string;
  isAlive?: (pid: number) => boolean;
  probe?: (connection: RuntimeConnection, timeoutMs: number) => Promise<RuntimeHealth | null>;
  spawn?: SpawnRuntime;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  pollMs?: number;
  startupTimeoutMs?: number;
  probeTimeoutMs?: number;
  allowLaunch?: boolean;
  launchCommand?: () => Promise<RuntimeLaunchCommand | null>;
  env?: Env;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const defaultSpawn: SpawnRuntime = (command, args, options) =>
  spawn(command, args, options) as RuntimeChild;

/**
 * Owns Runtime discovery/launch in the main process. It never stops a Runtime: the Runtime is a
 * user-level service that must survive GUI restarts (R05). Closing the GUI therefore only drops
 * the renderer's connection; reopening re-reads runtime.json and reconnects.
 */
export class RuntimeManager {
  private readonly dir: string;
  private readonly desktopVersion: string;
  private readonly isAlive: (pid: number) => boolean;
  private readonly probe: (
    connection: RuntimeConnection,
    timeoutMs: number,
  ) => Promise<RuntimeHealth | null>;
  private readonly spawnRuntime: SpawnRuntime;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  private readonly pollMs: number;
  private readonly startupTimeoutMs: number;
  private readonly probeTimeoutMs: number;
  private readonly allowLaunch: boolean;
  private readonly launchCommand: () => Promise<RuntimeLaunchCommand | null>;
  private readonly env: Env;
  private ensuring: Promise<RuntimeStatus> | null = null;

  constructor(options: RuntimeManagerOptions) {
    this.env = options.env ?? process.env;
    this.dir = path.resolve(options.dir ?? runtimeDir(process.platform, this.env));
    this.desktopVersion = options.desktopVersion;
    this.isAlive = options.isAlive ?? pidAlive;
    this.probe = options.probe ?? probeRuntimeHealth;
    this.spawnRuntime = options.spawn ?? defaultSpawn;
    this.sleep = options.sleep ?? sleep;
    this.now = options.now ?? Date.now;
    this.pollMs = options.pollMs ?? 250;
    this.startupTimeoutMs = options.startupTimeoutMs ?? 20_000;
    this.probeTimeoutMs = options.probeTimeoutMs ?? 2000;
    this.allowLaunch = options.allowLaunch ?? this.env.PULSE_RUNTIME_AUTOLAUNCH !== '0';
    this.launchCommand = options.launchCommand ?? (async () => null);
  }

  /** Connection for the renderer: only a healthy, version-compatible Runtime is usable. */
  async getConnection(): Promise<RuntimeConnection | null> {
    const status = await this.status();
    return status.kind === 'running' ? status.connection : null;
  }

  /**
   * Read-only lifecycle status. A live PID with failing health or an incompatible version is
   * reported honestly and never replaced by a second Runtime.
   */
  async status(): Promise<RuntimeStatus> {
    const discovery = await readRuntimeDiscovery(this.dir);
    if (!discovery.connection) {
      return this.notRunning(
        discovery.pid !== null
          ? `runtime.json 指向进程 ${discovery.pid}，但内容不可用：${discovery.detail ?? '格式异常'}`
          : (discovery.detail ?? 'runtime.json 不可用'),
      );
    }
    const connection = discovery.connection;
    if (!this.isAlive(connection.pid)) {
      return this.notRunning(`runtime.json 指向的进程 ${connection.pid} 已退出（发现文件已过期）`);
    }
    const health = await this.probe(connection, this.probeTimeoutMs);
    if (!health) {
      return {
        kind: 'unhealthy',
        pid: connection.pid,
        detail: `进程 ${connection.pid} 存活，但鉴权 /health 未通过或 PID/版本与发现文件不一致`,
      };
    }
    const compatibility = runtimeCompatibility(this.desktopVersion, health.version);
    if (!compatibility.compatible) {
      return {
        kind: 'incompatible',
        connection,
        health,
        required: compatibility.required,
        detail: `Runtime v${health.version} 与桌面 v${this.desktopVersion} 不兼容（需要 ${compatibility.required}）`,
      };
    }
    return { kind: 'running', connection, health };
  }

  /**
   * Start a detached Runtime when none is running, then wait for authenticated discovery.
   * Serialized so repeated renderer calls never fan out into multiple launches; the Runtime's own
   * duplicate-instance guard remains the final arbiter if another GUI races us.
   */
  async ensureRunning(): Promise<RuntimeStatus> {
    if (this.ensuring) return this.ensuring;
    this.ensuring = this.doEnsureRunning().finally(() => {
      this.ensuring = null;
    });
    return this.ensuring;
  }

  private async doEnsureRunning(): Promise<RuntimeStatus> {
    const initial = await this.status();
    if (initial.kind !== 'not-running') return initial;
    if (!this.allowLaunch || !initial.launchable) return initial;

    const command = await this.launchCommand();
    if (!command) return initial;

    let child: RuntimeChild;
    try {
      child = this.spawnRuntime(command.command, command.args, {
        cwd: command.cwd,
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
        env: { ...this.env, PULSE_RUNTIME_DIR: this.dir },
      });
    } catch (error) {
      return {
        kind: 'not-running',
        launchable: true,
        detail: `无法启动 Runtime（${command.description}）：${errorMessage(error)}`,
      };
    }

    // A holder avoids TypeScript treating a variable assigned only inside callbacks as never.
    const exit: {
      value: { code: number | null; signal: NodeJS.Signals | null; error?: Error } | null;
    } = { value: null };
    child.once('exit', (code, signal) => {
      exit.value = { code, signal };
    });
    child.once('error', (error) => {
      exit.value = { code: null, signal: null, error };
    });
    child.unref?.();

    const deadline = this.now() + this.startupTimeoutMs;
    while (this.now() < deadline) {
      await this.sleep(this.pollMs);
      const current = await this.status();
      if (current.kind === 'running' || current.kind === 'incompatible') return current;
      const ended = exit.value;
      if (ended && (ended.error || ended.code !== 0)) {
        const cause = ended.error
          ? errorMessage(ended.error)
          : ended.code === null
            ? `被信号 ${ended.signal ?? 'unknown'} 终止`
            : `退出码 ${ended.code}`;
        return {
          kind: 'not-running',
          launchable: true,
          detail: `Runtime 启动进程已退出（${cause}）`,
        };
      }
    }
    const last = await this.status();
    return last.kind === 'running'
      ? last
      : {
          kind: 'not-running',
          launchable: true,
          detail: `等待 Runtime 就绪超时（${this.startupTimeoutMs}ms）`,
        };
  }

  private async notRunning(detail: string): Promise<RuntimeStatus> {
    const command = this.allowLaunch ? await this.launchCommand() : null;
    return { kind: 'not-running', launchable: command !== null, detail };
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
