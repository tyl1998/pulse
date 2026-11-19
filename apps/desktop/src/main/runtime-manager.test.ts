import { createServer, type Server } from 'node:http';
import { mkdirSync, writeFileSync } from 'node:fs';
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, test, vi } from 'vitest';
import type { RuntimeConnection, RuntimeHealth } from '../shared/ipc';
import {
  parseConnection,
  probeRuntimeHealth,
  readRuntimeConnection,
  resolveRuntimeLaunchCommand,
  RuntimeManager,
  type RuntimeChild,
  runtimeDir,
} from './runtime-manager';

const valid = { port: 4242, token: 'abcdefghijklmnop_-XYZ', pid: 99, version: '1.0.0' };
const health = (connection: RuntimeConnection): RuntimeHealth => ({
  status: 'ok',
  version: connection.version,
  pid: connection.pid,
  uptime_s: 1,
});

async function tempDir(): Promise<string> {
  return mkdtemp(path.join(os.tmpdir(), 'pulse-rt-'));
}

describe('runtimeDir (mirrors pulse_runtime.paths.runtime_dir)', () => {
  test('per platform', () => {
    expect(runtimeDir('darwin', {}, '/h')).toBe('/h/Library/Application Support/Pulse/runtime');
    expect(runtimeDir('linux', {}, '/h')).toBe('/h/.local/state/pulse/runtime');
    expect(runtimeDir('linux', { XDG_STATE_HOME: '/s' }, '/h')).toBe('/s/pulse/runtime');
    expect(runtimeDir('win32', { LOCALAPPDATA: 'C:\\L' }, 'C:\\U')).toBe('C:\\L\\Pulse\\runtime');
    expect(runtimeDir('darwin', { PULSE_RUNTIME_DIR: '/x' }, '/h')).toBe('/x');
  });
});

describe('parseConnection', () => {
  test('accepts valid file and drops extra fields', () => {
    expect(parseConnection(JSON.stringify({ ...valid, started_at: 1 }))).toEqual(valid);
  });
  test.each([
    'not json',
    'null',
    JSON.stringify({ ...valid, port: 0 }),
    JSON.stringify({ ...valid, port: '1' }),
    JSON.stringify({ ...valid, token: 'short' }),
    JSON.stringify({ ...valid, token: 'has spaces and is long enough' }),
    JSON.stringify({ ...valid, pid: undefined }),
    JSON.stringify({ ...valid, pid: 0 }),
  ])('rejects %s', (raw) => {
    expect(parseConnection(raw)).toBeNull();
  });
});

describe('readRuntimeConnection', () => {
  test('missing file, stale pid, live pid', async () => {
    const dir = await tempDir();
    expect(await readRuntimeConnection(dir, () => true)).toBeNull();
    await writeFile(path.join(dir, 'runtime.json'), JSON.stringify(valid));
    expect(await readRuntimeConnection(dir, () => false)).toBeNull();
    expect(await readRuntimeConnection(dir, () => true)).toEqual(valid);
  });
});

describe('probeRuntimeHealth', () => {
  let server: Server | null = null;
  afterEach(async () => {
    if (server) await new Promise<void>((resolve) => server?.close(() => resolve()));
    server = null;
  });

  const listen = async (
    handler: (
      req: import('node:http').IncomingMessage,
      res: import('node:http').ServerResponse,
    ) => void,
  ): Promise<number> => {
    server = createServer(handler);
    await new Promise<void>((resolve) => server?.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    if (typeof address !== 'object' || address === null) throw new Error('no port');
    return address.port;
  };

  test('requires 200 + Bearer + matching status/pid/version and returns sanitized fields', async () => {
    let auth = '';
    const port = await listen((req, res) => {
      auth = String(req.headers.authorization);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', version: '1.0.0', pid: 99, uptime_s: 2, extra: 'x' }));
    });
    const connection = { ...valid, port };
    expect(await probeRuntimeHealth(connection, 1000)).toEqual({
      status: 'ok',
      version: '1.0.0',
      pid: 99,
      uptime_s: 2,
    });
    expect(auth).toBe(`Bearer ${valid.token}`);
  });

  test.each([
    [200, { status: 'ok', version: '1.0.0', pid: 100, uptime_s: 2 }],
    [200, { status: 'degraded', version: '1.0.0', pid: 99, uptime_s: 2 }],
    [200, { status: 'ok', version: '0.9.0', pid: 99, uptime_s: 2 }],
    [401, { status: 'ok', version: '1.0.0', pid: 99, uptime_s: 2 }],
  ])('rejects HTTP %i with %j', async (code, body) => {
    const port = await listen((_req, res) => {
      res.writeHead(code, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(body));
    });
    expect(await probeRuntimeHealth({ ...valid, port }, 1000)).toBeNull();
  });

  test('rejects malformed JSON and an unreachable port', async () => {
    const port = await listen((_req, res) => {
      res.writeHead(200);
      res.end('not json');
    });
    expect(await probeRuntimeHealth({ ...valid, port }, 1000)).toBeNull();
    expect(await probeRuntimeHealth({ ...valid, port: 1 }, 100)).toBeNull();
  });
});

describe('resolveRuntimeLaunchCommand', () => {
  test('PULSE_RUNTIME_BIN wins only when executable', async () => {
    expect(
      await resolveRuntimeLaunchCommand({
        env: { PULSE_RUNTIME_BIN: '/opt/pulse-runtime' },
        canExecute: async () => true,
      }),
    ).toMatchObject({ command: '/opt/pulse-runtime', args: ['serve'] });
    expect(
      await resolveRuntimeLaunchCommand({
        env: { PULSE_RUNTIME_BIN: '/opt/pulse-runtime' },
        canExecute: async () => false,
      }),
    ).toBeNull();
  });

  test('packaged builds look for bundled onedir/standalone runtime', async () => {
    const roots = new Set<string>();
    const command = await resolveRuntimeLaunchCommand({
      platform: 'darwin',
      isPackaged: true,
      resourcesPath: '/app/Resources',
      canExecute: async (file) => {
        roots.add(file);
        return file === '/app/Resources/runtime/pulse-runtime/pulse-runtime';
      },
    });
    expect(command).toEqual({
      command: '/app/Resources/runtime/pulse-runtime/pulse-runtime',
      args: ['serve'],
      description: 'bundled Runtime',
    });
    expect(roots.size).toBe(2);
  });

  test('dev checkout falls back to the repo .venv python', async () => {
    expect(
      await resolveRuntimeLaunchCommand({
        platform: 'linux',
        appPath: '/repo/apps/desktop',
        env: {},
        canExecute: async (file) => file === '/repo/.venv/bin/python',
      }),
    ).toEqual({
      command: '/repo/.venv/bin/python',
      args: ['-m', 'pulse_runtime', 'serve'],
      cwd: '/repo',
      description: 'checkout Runtime',
    });
  });

  test('PULSE_RUNTIME_PYTHON overrides the venv path', async () => {
    expect(
      await resolveRuntimeLaunchCommand({
        platform: 'win32',
        appPath: 'C:\\repo\\apps\\desktop',
        env: { PULSE_RUNTIME_PYTHON: 'C:\\py\\python.exe' },
        canExecute: async () => true,
      }),
    ).toMatchObject({ command: 'C:\\py\\python.exe', cwd: 'C:\\repo' });
  });

  test('missing executable means not launchable', async () => {
    expect(
      await resolveRuntimeLaunchCommand({
        platform: 'linux',
        appPath: '/repo/apps/desktop',
        env: {},
        canExecute: async () => false,
      }),
    ).toBeNull();
  });
});

interface Harness {
  manager: RuntimeManager;
  clock: { value: number };
  spawns: { command: string; args: string[]; options: Record<string, unknown> }[];
}

function harness(
  directory: string,
  overrides: Partial<ConstructorParameters<typeof RuntimeManager>[0]> = {},
): Harness {
  const clock = { value: 0 };
  const spawns: Harness['spawns'] = [];
  const manager = new RuntimeManager({
    dir: directory,
    desktopVersion: '1.0.0',
    isAlive: () => true,
    probe: async (connection) => health(connection),
    allowLaunch: false,
    pollMs: 250,
    startupTimeoutMs: 2000,
    probeTimeoutMs: 100,
    now: () => clock.value,
    sleep: async (ms) => {
      clock.value += ms;
    },
    ...overrides,
  });
  return { manager, clock, spawns };
}

describe('RuntimeManager.status', () => {
  test('missing discovery is not-running', async () => {
    const { manager } = harness(await tempDir());
    expect(await manager.status()).toMatchObject({ kind: 'not-running', launchable: false });
  });

  test('stale PID is not-running, not unhealthy', async () => {
    const dir = await tempDir();
    await writeFile(path.join(dir, 'runtime.json'), JSON.stringify(valid));
    const { manager } = harness(dir, { isAlive: () => false });
    expect(await manager.status()).toMatchObject({ kind: 'not-running' });
    expect(await manager.getConnection()).toBeNull();
  });

  test('live PID with failing authenticated health is unhealthy and never replaced', async () => {
    const dir = await tempDir();
    await writeFile(path.join(dir, 'runtime.json'), JSON.stringify(valid));
    const { manager, spawns } = harness(dir, { probe: async () => null, allowLaunch: true });
    expect(await manager.status()).toMatchObject({ kind: 'unhealthy', pid: valid.pid });
    expect(await manager.ensureRunning()).toMatchObject({ kind: 'unhealthy' });
    expect(spawns).toHaveLength(0);
  });

  test('different major or older runtime is incompatible, not connected', async () => {
    const dir = await tempDir();
    await writeFile(path.join(dir, 'runtime.json'), JSON.stringify({ ...valid, version: '0.9.0' }));
    const { manager } = harness(dir, {
      probe: async () => ({ status: 'ok', version: '0.9.0', pid: 99, uptime_s: 1 }),
    });
    const status = await manager.status();
    expect(status).toMatchObject({
      kind: 'incompatible',
      required: expect.stringContaining('1.0.0'),
    });
    expect(await manager.getConnection()).toBeNull();
  });

  test('healthy compatible runtime is running', async () => {
    const dir = await tempDir();
    await writeFile(path.join(dir, 'runtime.json'), JSON.stringify(valid));
    const { manager } = harness(dir);
    expect(await manager.status()).toMatchObject({ kind: 'running' });
    expect(await manager.getConnection()).toEqual(valid);
  });
});

describe('RuntimeManager.ensureRunning', () => {
  const fakeChild = (): RuntimeChild => ({
    pid: 4321,
    once: () => undefined,
    unref: () => undefined,
  });

  test.each(['dir', 'env'] as const)(
    'normalizes a relative %s directory before spawning in a different cwd',
    async (setting) => {
      const root = await tempDir();
      const mainCwd = path.join(root, 'main');
      const childCwd = path.join(root, 'child');
      const relativeDir = path.join('runtime', path.basename(root));
      const directory = path.resolve(mainCwd, relativeDir);
      await mkdir(directory, { recursive: true });
      await mkdir(childCwd, { recursive: true });
      const cwd = vi.spyOn(process, 'cwd').mockReturnValue(mainCwd);
      const spawns: Harness['spawns'] = [];
      let clock = 0;
      try {
        const manager = new RuntimeManager({
          ...(setting === 'dir' ? { dir: relativeDir } : {}),
          env: { PULSE_RUNTIME_DIR: relativeDir },
          desktopVersion: '1.0.0',
          isAlive: () => true,
          probe: async (connection) => health(connection),
          allowLaunch: true,
          startupTimeoutMs: 1000,
          now: () => clock,
          sleep: async (ms) => {
            clock += ms;
          },
          launchCommand: async () => ({
            command: '/runtime',
            args: ['serve'],
            cwd: childCwd,
            description: 'different-cwd Runtime',
          }),
          spawn: (command, args, options) => {
            spawns.push({ command, args, options: options as Record<string, unknown> });
            const runtimeDirectory = path.resolve(
              String(options.cwd),
              options.env?.PULSE_RUNTIME_DIR ?? '',
            );
            mkdirSync(runtimeDirectory, { recursive: true });
            writeFileSync(path.join(runtimeDirectory, 'runtime.json'), JSON.stringify(valid));
            return fakeChild();
          },
        });
        expect(await manager.ensureRunning()).toMatchObject({ kind: 'running' });
        expect(spawns).toHaveLength(1);
        expect(spawns[0]?.options).toMatchObject({
          cwd: childCwd,
          env: { PULSE_RUNTIME_DIR: directory },
        });
      } finally {
        cwd.mockRestore();
      }
    },
  );

  test('launches detached once, waits for health, and never kills the Runtime', async () => {
    const dir = await tempDir();
    const spawns: Harness['spawns'] = [];
    const child = fakeChild();
    const { manager } = harness(dir, {
      allowLaunch: true,
      launchCommand: async () => ({
        command: '/runtime',
        args: ['serve'],
        description: 'test Runtime',
      }),
      spawn: (command, args, options) => {
        spawns.push({ command, args, options: options as Record<string, unknown> });
        writeFileSync(path.join(dir, 'runtime.json'), JSON.stringify(valid));
        return child;
      },
    });

    expect(await manager.ensureRunning()).toMatchObject({ kind: 'running' });
    expect(spawns).toHaveLength(1);
    expect(spawns[0]).toMatchObject({ command: '/runtime', args: ['serve'] });
    expect(spawns[0]?.options).toMatchObject({
      detached: true,
      stdio: 'ignore',
      env: expect.objectContaining({ PULSE_RUNTIME_DIR: dir }),
    });
    expect(await manager.ensureRunning()).toMatchObject({ kind: 'running' });
    expect(spawns).toHaveLength(1);
  });

  test('serializes concurrent ensure calls into one launch', async () => {
    const dir = await tempDir();
    let launches = 0;
    const { manager } = harness(dir, {
      allowLaunch: true,
      launchCommand: async () => ({ command: '/runtime', args: ['serve'], description: 't' }),
      spawn: () => {
        launches += 1;
        writeFileSync(path.join(dir, 'runtime.json'), JSON.stringify(valid));
        return fakeChild();
      },
    });
    const [a, b] = await Promise.all([manager.ensureRunning(), manager.ensureRunning()]);
    expect(a).toMatchObject({ kind: 'running' });
    expect(b).toMatchObject({ kind: 'running' });
    expect(launches).toBe(1);
  });

  test('does not launch when auto-launch is disabled or no executable exists', async () => {
    const dir = await tempDir();
    let launches = 0;
    const { manager } = harness(dir, {
      allowLaunch: false,
      launchCommand: async () => ({ command: '/runtime', args: [], description: 't' }),
      spawn: () => {
        launches += 1;
        return fakeChild();
      },
    });
    expect(await manager.ensureRunning()).toMatchObject({ kind: 'not-running', launchable: false });
    const other = harness(dir, {
      allowLaunch: true,
      launchCommand: async () => null,
      spawn: () => {
        launches += 1;
        return fakeChild();
      },
    });
    expect(await other.manager.ensureRunning()).toMatchObject({
      kind: 'not-running',
      launchable: false,
    });
    expect(launches).toBe(0);
  });

  test('reports a child that exits early', async () => {
    const dir = await tempDir();
    let exit: ((code: number | null, signal: NodeJS.Signals | null) => void) | null = null;
    const { manager } = harness(dir, {
      allowLaunch: true,
      startupTimeoutMs: 5000,
      now: () => Date.now(),
      sleep: () => new Promise((resolve) => setTimeout(resolve, 0)),
      launchCommand: async () => ({ command: '/runtime', args: [], description: 't' }),
      spawn: () => {
        const child = {
          pid: 1,
          once: (event: string, listener: (...args: never[]) => void) => {
            if (event === 'exit') exit = listener as typeof exit;
            return child;
          },
          unref: () => undefined,
        };
        return child as unknown as RuntimeChild;
      },
    });
    const pending = manager.ensureRunning();
    while (exit === null) await new Promise((resolve) => setTimeout(resolve, 0));
    (exit as (code: number | null, signal: NodeJS.Signals | null) => void)(3, null);
    expect(await pending).toMatchObject({
      kind: 'not-running',
      detail: expect.stringContaining('退出码 3'),
    });
  });

  test('times out without claiming success', async () => {
    const dir = await tempDir();
    const { manager, clock } = harness(dir, {
      allowLaunch: true,
      startupTimeoutMs: 1000,
      launchCommand: async () => ({ command: '/runtime', args: [], description: 't' }),
      spawn: () => fakeChild(),
    });
    expect(await manager.ensureRunning()).toMatchObject({
      kind: 'not-running',
      detail: expect.stringContaining('超时'),
    });
    expect(clock.value).toBeGreaterThanOrEqual(1000);
  });
});
