import assert from 'node:assert/strict';
import { EventEmitter, once } from 'node:events';
import {
  access,
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import test from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import {
  acquireLauncherLock,
  checkDesktopDependencies,
  checkDesktopPort,
  checkNodeVersion,
  DESKTOP_HOST,
  DESKTOP_PORT,
  main,
  ownedChildren,
  parseDiscovery,
  pidAlive,
  probeRuntime,
  quoteWindowsArgument,
  readDiscovery,
  rendererReady,
  runDev,
  runtimeDirectory,
} from './dev.mjs';

const SELF = fileURLToPath(new URL('./dev.mjs', import.meta.url));
const TOKEN = 'test_only_runtime_secret_123456789';
const info = (overrides = {}) => ({
  port: 43210,
  token: TOKEN,
  pid: 32101,
  version: '0.0.0',
  started_at: 123,
  ...overrides,
});

async function temporary(t) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'pulse-dev-test-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function fixture(file, text = '') {
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, text);
}

async function server(t, handler, port = 0) {
  const instance = http.createServer(handler);
  instance.listen(port, DESKTOP_HOST);
  await once(instance, 'listening');
  t.after(
    () =>
      new Promise((resolve) => {
        instance.closeAllConnections();
        instance.close(resolve);
      }),
  );
  return instance;
}

class FakeChild extends EventEmitter {
  constructor(pid, options) {
    super();
    this.pid = pid;
    this.options = options;
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.exitCode = null;
    this.signalCode = null;
  }
  control(message) {
    this.stdout.write(`${this.options.env.PULSE_DEV_MARKER}${JSON.stringify(message)}\n`);
  }
  exit(code = 0, signal = null) {
    this.exitCode = code;
    this.signalCode = signal;
    this.emit('exit', code, signal);
    this.stdout.end();
    this.stderr.end();
  }
  kill(signal) {
    this.exit(null, signal);
    return true;
  }
}

async function harness(t, settings = {}) {
  const root = await temporary(t);
  const directory = path.join(root, 'state');
  const discoveryPath = path.join(directory, 'runtime.json');
  const python = path.join(
    root,
    '.venv',
    process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
  );
  await fixture(python);
  await chmod(python, 0o755);
  if (settings.initial !== undefined) {
    await fixture(
      discoveryPath,
      typeof settings.initial === 'string' ? settings.initial : JSON.stringify(settings.initial),
    );
  }
  const calls = [];
  const groups = new Map();
  const live = new Set(settings.initial?.pid ? [settings.initial.pid] : []);
  const kills = [];
  const events = [];
  const logs = [];
  const signals = new EventEmitter();
  const h = {
    root,
    directory,
    discoveryPath,
    python,
    calls,
    groups,
    live,
    kills,
    events,
    logs,
    signals,
  };
  h.write = (connection) => fixture(discoveryPath, JSON.stringify(connection));
  h.options = {
    root,
    env: { PULSE_RUNTIME_DIR: 'state' },
    platform: process.platform,
    runtimeTimeoutMs: 120,
    desktopTimeoutMs: 120,
    dependencyTimeoutMs: 100,
    pollMs: 2,
    graceMs: 2,
    isAlive: (pid) => live.has(pid),
    log: (line) => logs.push(line),
    checkPort: async () => events.push('port'),
    desktopDependencies: async () => ({
      desktopDir: path.join(root, 'apps/desktop'),
      forgeEntry: path.join(root, 'fake-forge.mjs'),
      electronDist: path.join(root, 'electron-dist'),
    }),
    kill: (pid, signal) => {
      kills.push([pid, signal]);
      const child = groups.get(-pid);
      if (!child) throw Object.assign(new Error('no such process group'), { code: 'ESRCH' });
      if (signal !== 0) {
        groups.delete(-pid);
        live.delete(-pid);
        if (child.exitCode === null && child.signalCode === null) child.exit(null, signal);
      }
    },
    spawnImpl: (command, args, options) => {
      const child = new FakeChild(40000 + calls.length, options);
      groups.set(child.pid, child);
      live.add(child.pid);
      calls.push({ command, args, options, child });
      const kind = args[0] === '-c' ? 'check' : args.includes('serve') ? 'runtime' : 'desktop';
      h[kind] = child;
      events.push(kind);
      queueMicrotask(async () => {
        try {
          if (settings[kind]) await settings[kind](h, child);
          else if (kind === 'check') {
            groups.delete(child.pid);
            live.delete(child.pid);
            child.exit();
          } else if (kind === 'runtime') await h.write(info({ pid: child.pid }));
          else child.control({ type: 'electron-started', pid: 50000 });
        } catch (error) {
          child.emit('error', error);
        }
      });
      return child;
    },
    probe: async (connection) => {
      events.push(`health:${connection.pid}`);
      return true;
    },
    rendererProbe: async () => {
      events.push('renderer');
      return true;
    },
    onReady: () => {
      h.desktop.control({ type: 'electron-exit', code: 0 });
      h.desktop.exit();
    },
    ...settings.options,
  };
  // Fake Windows process mechanics are exercised separately; lifecycle tests use POSIX groups.
  h.options.platform = 'darwin';
  if (process.platform === 'win32') {
    const posixPython = path.join(root, '.venv/bin/python');
    await fixture(posixPython);
    await chmod(posixPython, 0o755);
  }
  return h;
}

test('Node minimum is 22.12.0, not merely Node 22', () => {
  for (const version of ['22.12.0', '22.12.1', '22.23.3', '23.0.0', '24.0.0'])
    checkNodeVersion(version);
  for (const version of ['20.99.0', '21.0.0', '22.11.99', '22.1.0', '22.12.0-rc.1', 'invalid']) {
    assert.throws(() => checkNodeVersion(version), /requires Node >=22\.12\.0/);
  }
});

test('Runtime directory defaults match Python on macOS, Windows, and Linux, including empty env', () => {
  const env = { HOME: '/home/qa' };
  assert.equal(
    runtimeDirectory({ platform: 'darwin', env }),
    '/home/qa/Library/Application Support/Pulse/runtime',
  );
  assert.equal(runtimeDirectory({ platform: 'linux', env }), '/home/qa/.local/state/pulse/runtime');
  assert.equal(
    runtimeDirectory({ platform: 'linux', env: { ...env, XDG_STATE_HOME: '/state' } }),
    '/state/pulse/runtime',
  );
  assert.equal(
    runtimeDirectory({ platform: 'linux', env: { ...env, XDG_STATE_HOME: '' } }),
    '/home/qa/.local/state/pulse/runtime',
  );
  assert.equal(
    runtimeDirectory({ platform: 'win32', env: { USERPROFILE: 'C:\\Users\\qa' }, cwd: 'C:\\repo' }),
    'C:\\Users\\qa\\AppData\\Local\\Pulse\\runtime',
  );
  assert.equal(
    runtimeDirectory({
      platform: 'win32',
      env: { LOCALAPPDATA: 'D:\\Local', HOME: 'C:\\Home' },
      cwd: 'C:\\repo',
    }),
    'D:\\Local\\Pulse\\runtime',
  );
  assert.equal(
    runtimeDirectory({
      platform: 'linux',
      env: { PULSE_RUNTIME_DIR: '../state' },
      cwd: '/repo/pulse',
    }),
    '/repo/state',
  );
  assert.equal(
    runtimeDirectory({
      platform: 'win32',
      env: { PULSE_RUNTIME_DIR: '..\\state' },
      cwd: 'D:\\repo\\pulse',
    }),
    'D:\\repo\\state',
  );
});

test('launcher lock excludes concurrent cold starts and removes only its own lock', async (t) => {
  const root = await temporary(t);
  const release = await acquireLauncherLock(root);
  const lock = path.join(root, 'dev-launcher.lock');
  const contents = await readFile(lock, 'utf8');
  await assert.rejects(acquireLauncherLock(root), /Another or interrupted Pulse dev launcher/);
  assert.equal(await readFile(lock, 'utf8'), contents);
  await release();
  await assert.rejects(readFile(lock), { code: 'ENOENT' });
  const releaseSecond = await acquireLauncherLock(root);
  await writeFile(lock, 'a replacement not owned by this invocation');
  await releaseSecond();
  assert.equal(await readFile(lock, 'utf8'), 'a replacement not owned by this invocation');
});

test('an existing launcher lock never starts or stops Runtime and is not auto-reaped', async (t) => {
  const h = await harness(t, { initial: info() });
  const lock = path.join(h.directory, 'dev-launcher.lock');
  await fixture(lock, 'stale or unverified owner');
  await assert.rejects(runDev(h.options), /Verify that launcher has exited/);
  assert.equal(h.calls.length, 0);
  assert.equal(h.kills.length, 0);
  assert.equal(await readFile(lock, 'utf8'), 'stale or unverified owner');
});

test('discovery strictly validates credentials and endpoints but retains an invalid record live PID', () => {
  assert.equal(parseDiscovery(JSON.stringify(info())).connection.token, TOKEN);
  for (const raw of ['{', 'null', '[]', '42']) assert.equal(parseDiscovery(raw).connection, null);
  for (const change of [
    { port: 0 },
    { port: 65536 },
    { port: '43210' },
    { port: 1.2 },
    { token: 'tiny' },
    { token: 'newline\n'.repeat(5) },
    { version: 1 },
    { version: '' },
    { pid: 0 },
    { pid: -1 },
    { pid: '32101' },
  ]) {
    assert.equal(parseDiscovery(JSON.stringify(info(change))).connection, null);
  }
  assert.equal(parseDiscovery(JSON.stringify(info({ token: 'bad' }))).pid, 32101);
  assert.equal(pidAlive(-1), false);
  assert.equal(
    pidAlive(1, () => {
      throw Object.assign(new Error(), { code: 'EPERM' });
    }),
    true,
  );
  assert.equal(
    pidAlive(1, () => {
      throw Object.assign(new Error(), { code: 'ESRCH' });
    }),
    false,
  );
  assert.throws(
    () =>
      pidAlive(1, () => {
        throw Object.assign(new Error(), { code: 'EINVAL' });
      }),
    /Cannot check Runtime PID/,
  );
});

test('missing or malformed discovery is distinct from an unreadable discovery path', async (t) => {
  const directory = await temporary(t);
  assert.equal((await readDiscovery(directory)).connection, null);
  await fixture(path.join(directory, 'runtime.json'), '{invalid');
  assert.equal((await readDiscovery(directory)).connection, null);
  await rm(path.join(directory, 'runtime.json'));
  await mkdir(path.join(directory, 'runtime.json'));
  await assert.rejects(readDiscovery(directory), /Cannot read Runtime discovery.*permissions/);
});

test('health uses authenticated loopback GET and matches status, PID, and version exactly', async (t) => {
  let response = { status: 'ok', pid: process.pid, version: '0.0.0', uptime_s: 4 };
  let status = 200;
  let requests = 0;
  const mock = await server(t, (req, res) => {
    requests++;
    assert.equal(req.url, '/health');
    assert.equal(req.method, 'GET');
    assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
    assert.equal(req.socket.localAddress, DESKTOP_HOST);
    res.writeHead(status).end(JSON.stringify(response));
  });
  const connection = info({ port: mock.address().port, pid: process.pid });
  assert.equal(await probeRuntime(connection), true);
  for (const change of [
    { status: 'starting' },
    { pid: process.pid + 1 },
    { pid: String(process.pid) },
    { version: 'new' },
    { version: undefined },
  ]) {
    const previous = response;
    response = { ...response, ...change };
    assert.equal(await probeRuntime(connection), false);
    response = previous;
  }
  status = 401;
  assert.equal(await probeRuntime(connection), false);
  assert.equal(requests, 7);
});

test('health does not follow redirects or send a token to another endpoint', async (t) => {
  let forwarded = 0;
  const target = await server(t, (_, res) => {
    forwarded++;
    res.end('{}');
  });
  const mock = await server(t, (_, res) =>
    res
      .writeHead(302, { Location: `http://${DESKTOP_HOST}:${target.address().port}/health` })
      .end(),
  );
  assert.equal(await probeRuntime(info({ port: mock.address().port })), false);
  assert.equal(forwarded, 0);
});

test('health has absolute body/connect deadlines, size limits, and cancellation', async (t) => {
  let oversized = false;
  const mock = await server(t, (_, res) => {
    if (oversized) {
      res.end('x'.repeat(20000));
      return;
    }
    const timer = setInterval(() => res.write(' '), 5);
    res.on('close', () => clearInterval(timer));
  });
  const connection = info({ port: mock.address().port });
  const started = Date.now();
  assert.equal(await probeRuntime(connection, { timeoutMs: 30 }), false);
  assert.ok(Date.now() - started < 1000);
  oversized = true;
  assert.equal(await probeRuntime(connection), false);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(probeRuntime(connection, { signal: controller.signal }), /abort/i);
});

async function dependencyFixture(t) {
  const root = await temporary(t);
  const modules = path.join(root, 'node_modules');
  await fixture(
    path.join(root, 'apps/desktop/package.json'),
    JSON.stringify({
      dependencies: { react: '*' },
      devDependencies: { vite: '*', electron: '*', '@electron-forge/cli': '*' },
    }),
  );
  for (const name of ['react', 'vite', '@electron-forge/core']) {
    await fixture(
      path.join(modules, name, 'package.json'),
      JSON.stringify({ name, main: 'index.js' }),
    );
    await fixture(
      path.join(modules, name, 'index.js'),
      'throw new Error("dependency preflight must not execute modules");',
    );
  }
  await fixture(
    path.join(modules, '@electron-forge/cli/package.json'),
    JSON.stringify({
      name: '@electron-forge/cli',
      version: '8.0.1',
      engines: { node: '>=22.13.0' },
      bin: { 'electron-forge': 'dist/electron-forge.js' },
    }),
  );
  await fixture(
    path.join(modules, 'semver/index.js'),
    'exports.satisfies = version => { const [major, minor] = version.split(".").map(Number); return major > 22 || major === 22 && minor >= 13; };',
  );
  await fixture(
    path.join(modules, 'electron/package.json'),
    '{"name":"electron","main":"index.js"}',
  );
  await fixture(
    path.join(modules, 'electron/index.js'),
    'throw new Error("auto-download was invoked");',
  );
  const executable = process.platform === 'win32' ? 'electron.exe' : 'electron';
  await fixture(path.join(modules, 'electron/path.txt'), executable);
  await fixture(path.join(modules, 'electron/dist', executable));
  await chmod(path.join(modules, 'electron/dist', executable), 0o755);
  return { root, modules, executable };
}

test('desktop preflight resolves hoisted Forge and checks Electron without importing or installing it', async (t) => {
  const { root, modules } = await dependencyFixture(t);
  const result = await checkDesktopDependencies(root, {}, '22.23.3');
  assert.equal(result.desktopDir, path.join(root, 'apps/desktop'));
  assert.equal(result.forgeEntry, path.join(modules, '@electron-forge/core/index.js'));
  assert.equal(result.electronDist, path.join(modules, 'electron/dist'));
  await assert.rejects(
    checkDesktopDependencies(root, {}, '22.12.0'),
    /Installed Electron Forge.*requires Node/,
  );
});

test('missing JS dependencies, transitive Forge modules, and Electron binary have pnpm install remedies', async (t) => {
  for (const [missing, expected] of [
    ['@electron-forge/cli', /Missing desktop dependency.*cli.*pnpm install/],
    ['@electron-forge/core', /Forge dependencies are incomplete.*pnpm install/],
    ['vite', /Missing desktop dependency vite.*pnpm install/],
    ['electron/path.txt', /binary metadata is missing.*pnpm install/],
    ['electron/dist', /executable is missing.*pnpm install/],
  ]) {
    const { root, modules } = await dependencyFixture(t);
    await rm(path.join(modules, missing), { recursive: true });
    await assert.rejects(checkDesktopDependencies(root, {}, '22.23.3'), expected);
  }
});

test('relative Electron override is made absolute before the desktop changes cwd', async (t) => {
  const { root, executable } = await dependencyFixture(t);
  await fixture(path.join(root, 'custom-dist', executable));
  await chmod(path.join(root, 'custom-dist', executable), 0o755);
  const result = await checkDesktopDependencies(
    root,
    { ELECTRON_OVERRIDE_DIST_PATH: 'custom-dist' },
    '22.23.3',
  );
  assert.equal(result.electronDist, path.join(root, 'custom-dist'));
});

test('healthy existing Runtime needs no .venv and its discovery and service remain untouched', async (t) => {
  const mock = await server(t, (req, res) => {
    assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
    res.end(JSON.stringify({ status: 'ok', pid: process.pid, version: '0.0.0' }));
  });
  const connection = info({ pid: process.pid, port: mock.address().port });
  const h = await harness(t, {
    initial: connection,
    options: { isAlive: pidAlive, probe: probeRuntime },
  });
  await rm(path.join(h.root, '.venv'), { recursive: true });
  const original = await readFile(h.discoveryPath, 'utf8');
  const timestamp = (await stat(h.discoveryPath)).mtimeMs;
  assert.equal(await runDev(h.options), 0);
  assert.equal(h.calls.length, 1);
  assert.ok(h.calls[0].args.includes('--forge'));
  assert.equal(await readFile(h.discoveryPath, 'utf8'), original);
  assert.equal((await stat(h.discoveryPath)).mtimeMs, timestamp);
  assert.equal(await probeRuntime(connection), true);
  assert.ok(h.kills.every(([pid]) => pid === -h.desktop.pid));
  assert.doesNotMatch(h.logs.join('\n'), new RegExp(TOKEN));
});

test('absent Runtime launches local .venv only, waits for health, then starts Forge with consistent absolute paths', async (t) => {
  const h = await harness(t);
  let probes = 0;
  h.options.probe = async () => {
    h.events.push(++probes < 3 ? 'not-ready' : 'healthy');
    return probes >= 3;
  };
  assert.equal(await runDev(h.options), 0);
  assert.deepEqual(h.calls[0].args.slice(0, 1), ['-c']);
  assert.match(
    h.calls[0].args[1],
    /pulse_runtime\.server.*h11_impl.*websockets_sansio_impl.*lifespan\.on/,
  );
  assert.deepEqual(h.calls[1].args, ['-m', 'pulse_runtime', 'serve']);
  assert.equal(h.calls[1].command, h.python);
  assert.equal(h.calls[2].command, process.execPath);
  assert.equal(h.calls[2].args[0], SELF);
  assert.ok(h.events.indexOf('healthy') < h.events.indexOf('desktop'));
  for (const call of h.calls) {
    assert.equal(call.options.env.PULSE_RUNTIME_DIR, h.directory);
    assert.equal(call.options.detached, true);
    assert.equal(call.options.shell, undefined);
  }
  assert.equal(h.calls[1].options.cwd, h.root);
  assert.equal(h.calls[2].options.cwd, path.join(h.root, 'apps/desktop'));
  assert.equal(h.calls[2].options.env.ELECTRON_SKIP_BINARY_DOWNLOAD, '1');
  assert.ok(h.kills.some(([pid, signal]) => pid === -h.runtime.pid && signal === 'SIGTERM'));
});

test('a live but unhealthy or invalid-discovery PID is waited for and never replaced or killed', async (t) => {
  for (const initial of [info(), info({ token: 'bad' })]) {
    const h = await harness(t, {
      initial,
      options: { runtimeTimeoutMs: 25, probe: async () => false },
    });
    const before = await readFile(h.discoveryPath, 'utf8');
    await assert.rejects(runDev(h.options), /Existing Runtime PID 32101 is alive.*No replacement/);
    assert.equal(h.calls.length, 0);
    assert.equal(h.kills.length, 0);
    assert.equal(await readFile(h.discoveryPath, 'utf8'), before);
  }
});

test('losing discovery does not permit replacing a previously observed live PID', async (t) => {
  const h = await harness(t, { initial: info(), options: { runtimeTimeoutMs: 25 } });
  h.options.probe = async () => {
    await rm(h.discoveryPath, { force: true });
    return false;
  };
  await assert.rejects(runDev(h.options), /Existing Runtime PID.*No replacement/);
  assert.equal(h.calls.length, 0);
});

test('an existing Runtime publishing discovery before ASGI readiness is reused after polling', async (t) => {
  let calls = 0;
  const h = await harness(t, { initial: info(), options: { probe: async () => ++calls > 2 } });
  assert.equal(await runDev(h.options), 0);
  assert.equal(h.calls.length, 1);
  assert.equal(calls, 3);
});

test('stale or invalid discovery can start Runtime without launcher deleting the file', async (t) => {
  for (const initial of ['not json', JSON.stringify(info({ pid: 54321 }))]) {
    const h = await harness(t, {
      initial,
      runtime: async (h, child) => {
        assert.equal(await readFile(h.discoveryPath, 'utf8'), initial);
        await h.write(info({ pid: child.pid }));
      },
    });
    assert.equal(await runDev(h.options), 0);
    assert.equal(h.calls.length, 3);
    assert.equal(JSON.parse(await readFile(h.discoveryPath, 'utf8')).pid, h.runtime.pid);
  }
});

test('Runtime appearing during Python imports is reused without a redundant serve invocation', async (t) => {
  const h = await harness(t, {
    check: async (h, child) => {
      h.live.add(32101);
      await h.write(info());
      h.groups.delete(child.pid);
      child.exit();
    },
  });
  assert.equal(await runDev(h.options), 0);
  assert.equal(h.calls.length, 2);
  assert.equal(h.runtime, undefined);
});

test('duplicate-start exit 0 waits for the healthy winner and does not delete its discovery', async (t) => {
  const h = await harness(t, {
    runtime: async (h, child) => {
      h.live.delete(child.pid);
      child.exit(0);
      await delay(8);
      h.live.add(32101);
      await h.write(info());
    },
  });
  assert.equal(await runDev(h.options), 0);
  assert.equal(JSON.parse(await readFile(h.discoveryPath, 'utf8')).pid, 32101);
  assert.ok(h.live.has(32101));
  assert.ok(h.logs.some((line) => line.includes('Reusing existing Runtime')));
});

test('a losing Runtime still exiting is retired without aborting a healthy winner', async (t) => {
  const h = await harness(t, {
    runtime: async (h) => {
      h.live.add(32101);
      await h.write(info());
    },
  });
  assert.equal(await runDev(h.options), 0);
  assert.equal(h.runtime.signalCode, 'SIGTERM');
  assert.ok(h.live.has(32101));
});

test('missing .venv or Python imports gives uv sync, never auto-installs or starts Electron', async (t) => {
  const missing = await harness(t);
  await rm(path.join(missing.root, '.venv'), { recursive: true });
  await assert.rejects(runDev(missing.options), /Local \.venv Python.*uv sync/);
  assert.equal(missing.calls.length, 0);
  const modules = await harness(t, {
    check: (h, child) => {
      child.stderr.write("ModuleNotFoundError: No module named 'fastapi'\n");
      child.exit(1);
    },
  });
  await assert.rejects(runDev(modules.options), /Missing module: fastapi.*uv sync/);
  assert.equal(modules.calls.length, 1);
});

test('Python dependency checks and Runtime health wait have finite deadlines', async (t) => {
  const dependency = await harness(t, { check: () => {}, options: { dependencyTimeoutMs: 20 } });
  await assert.rejects(runDev(dependency.options), /Python dependency check timed out.*uv sync/);
  assert.equal(dependency.calls.length, 1);
  const runtime = await harness(t, { options: { probe: async () => false, runtimeTimeoutMs: 25 } });
  await assert.rejects(runDev(runtime.options), /Runtime readiness timed out/);
  assert.equal(runtime.desktop, undefined);
  assert.equal(runtime.groups.size, 0);
});

test('Runtime early exit and spawn errors are reported before Electron', async (t) => {
  for (const runtime of [
    (h, child) => {
      child.stderr.write("ModuleNotFoundError: No module named 'websockets'\n");
      child.exit(1);
    },
    (h, child) => child.emit('error', Object.assign(new Error('spawn failed'), { code: 'ENOENT' })),
  ]) {
    const h = await harness(t, { runtime });
    await assert.rejects(
      runDev(h.options),
      /Runtime (exited with code 1|could not spawn \(ENOENT\)).*uv sync/,
    );
    assert.equal(h.desktop, undefined);
    assert.equal(h.groups.size, 0);
  }
});

test('Runtime exit 0 without a healthy winner is bounded and is not accepted as readiness', async (t) => {
  const h = await harness(t, {
    runtime: (h, child) => {
      h.live.delete(child.pid);
      child.exit();
    },
    options: { runtimeTimeoutMs: 25 },
  });
  await assert.rejects(
    runDev(h.options),
    /exited with code 0, but no healthy winning Runtime appeared/,
  );
  assert.equal(h.desktop, undefined);
});

test('fixed desktop config retains loopback, 5175, and strictPort', async () => {
  assert.equal(DESKTOP_PORT, 5175);
  assert.equal(DESKTOP_HOST, '127.0.0.1');
  const config = await readFile(
    new URL('../apps/desktop/vite.renderer.config.mts', import.meta.url),
    'utf8',
  );
  assert.match(config, /host:\s*'127\.0\.0\.1'/);
  assert.match(config, /port:\s*5175\b/);
  assert.match(config, /strictPort:\s*true/);
  assert.doesNotMatch(config, /5173/);
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(manifest.scripts.dev, 'node scripts/dev.mjs');
  assert.equal(manifest.scripts['desktop:dev'], 'pnpm --filter @pulse/desktop start');
  assert.equal(manifest.scripts['runtime:dev'], 'uv run pulse-runtime serve');
});

test('port conflict fails before any child starts and leaves its owner listening', async (t) => {
  let mock;
  try {
    mock = await server(t, (_, res) => res.end('unrelated service'), DESKTOP_PORT);
  } catch (error) {
    if (error.code === 'EADDRINUSE') {
      t.skip('TCP 5175 is already owned outside the test');
      return;
    }
    throw error;
  }
  const h = await harness(t, { options: { checkPort: checkDesktopPort } });
  await assert.rejects(runDev(h.options), /TCP 5175 is already in use.*no alternate port/);
  assert.equal(h.calls.length, 0);
  assert.equal(mock.listening, true);
});

test('renderer readiness requires the actual Vite client and Pulse HTML on 5175', async (t) => {
  let valid = false;
  try {
    await server(
      t,
      (req, res) =>
        res.end(
          valid
            ? req.url === '/@vite/client'
              ? 'export function createHotContext() {}'
              : '<script src="/src/renderer/main.tsx"></script>'
            : 'other app',
        ),
      DESKTOP_PORT,
    );
  } catch (error) {
    if (error.code === 'EADDRINUSE') {
      t.skip('TCP 5175 is already owned outside the test');
      return;
    }
    throw error;
  }
  assert.equal(await rendererReady(), false);
  valid = true;
  assert.equal(await rendererReady(), true);
});

test('port is checked again after Runtime readiness; failure cleans only the new Runtime', async (t) => {
  let ports = 0;
  const h = await harness(t, {
    options: {
      checkPort: async () => {
        if (++ports === 2) throw new Error('TCP 5175 acquired during startup');
      },
    },
  });
  await assert.rejects(runDev(h.options), /TCP 5175 acquired/);
  assert.equal(h.desktop, undefined);
  assert.equal(h.groups.size, 0);
});

test('desktop timeout requires both Electron startup and renderer HTTP readiness', async (t) => {
  for (const rendererMissing of [false, true]) {
    const h = await harness(t, {
      desktop: (h, child) => {
        if (rendererMissing) child.control({ type: 'electron-started', pid: 50000 });
      },
      options: { desktopTimeoutMs: 25, rendererProbe: async () => !rendererMissing },
    });
    await assert.rejects(runDev(h.options), /Desktop readiness timed out.*127\.0\.0\.1:5175/);
    assert.equal(h.groups.size, 0);
  }
});

test('desktop early clean exit, failure exit, missing modules, and spawn failures are failures', async (t) => {
  for (const desktop of [
    (h, child) => child.exit(0),
    (h, child) => {
      child.stderr.write("Error: Cannot find package 'vite' imported from forge\n");
      child.exit(1);
    },
    (h, child) => child.emit('error', Object.assign(new Error(), { code: 'EACCES' })),
  ]) {
    const h = await harness(t, { desktop });
    await assert.rejects(
      runDev(h.options),
      /Electron Forge.*before desktop readiness.*pnpm install/,
    );
    assert.equal(h.groups.size, 0);
  }
});

test('Electron exit notification overrides a Forge parent that reports exit 0', async (t) => {
  const h = await harness(t, {
    options: {
      onReady: () => {
        h.desktop.control({ type: 'electron-exit', code: 7 });
        h.desktop.exit(0);
      },
    },
  });
  await assert.rejects(runDev(h.options), /Electron exited with code 7 during development/);
  assert.equal(h.groups.size, 0);
});

test('owned Runtime exit after readiness stops the desktop without restarting Runtime', async (t) => {
  const h = await harness(t, { options: { onReady: () => h.runtime.exit(2) } });
  await assert.rejects(runDev(h.options), /Runtime exited with code 2/);
  assert.equal(h.calls.filter((call) => call.args.includes('serve')).length, 1);
  assert.equal(h.groups.size, 0);
});

test('known Runtime tokens are never printed, including split child-output chunks', async (t) => {
  const h = await harness(t, {
    initial: info(),
    desktop: (h, child) => {
      child.stdout.write(`prefix ${TOKEN.slice(0, 12)}`);
      child.stdout.write(`${TOKEN.slice(12)} suffix\nBearer other-credential\n`);
      child.control({ type: 'electron-started', pid: 50000 });
    },
  });
  assert.equal(await runDev(h.options), 0);
  assert.doesNotMatch(h.logs.join('\n'), /test_only_runtime_secret|other-credential/);
  assert.match(h.logs.join('\n'), /prefix \[REDACTED\] suffix/);
});

test('SIGINT, SIGTERM and SIGHUP during import, Runtime startup, and desktop life clean owned children', async (t) => {
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    for (const phase of ['check', 'runtime', 'desktop']) {
      const settings =
        phase === 'desktop' ? { initial: info() } : { [phase]: (h) => h.signals.emit(signal) };
      const h = await harness(t, settings);
      if (phase === 'desktop') h.options.onReady = () => h.signals.emit(signal);
      const errors = [];
      const code = await main({
        ...h.options,
        processSignals: h.signals,
        error: (message) => errors.push(message),
      });
      assert.equal(code, { SIGINT: 130, SIGTERM: 143, SIGHUP: 129 }[signal]);
      assert.deepEqual(errors, []);
      assert.equal(h.groups.size, 0);
      await assert.rejects(access(path.join(h.directory, 'dev-launcher.lock')), { code: 'ENOENT' });
      assert.equal(h.signals.listenerCount(signal), 0);
      assert.ok(h.kills.every(([pid]) => h.calls.some((call) => pid === -call.child.pid)));
      if (phase === 'desktop') assert.ok(h.live.has(32101));
    }
  }
});

test('already interrupted startup and synchronous spawn failures cannot create late children', async (t) => {
  const controller = new AbortController();
  controller.abort(new Error('cancelled before startup'));
  const h = await harness(t, { options: { signal: controller.signal } });
  await assert.rejects(runDev(h.options), /cancelled before startup/);
  assert.equal(h.calls.length, 0);
  const failure = await harness(t, {
    options: {
      spawnImpl: () => {
        throw Object.assign(new Error('spawn denied'), { code: 'EACCES' });
      },
    },
  });
  await assert.rejects(runDev(failure.options), /could not spawn \(EACCES\).*uv sync/);
});

test('Windows command quoting preserves spaces, quotes, and trailing backslashes', () => {
  assert.equal(
    quoteWindowsArgument('C:\\Program Files\\node.exe'),
    '"C:\\Program Files\\node.exe"',
  );
  assert.equal(quoteWindowsArgument('with"quote'), '"with\\"quote"');
  assert.equal(quoteWindowsArgument('trailing\\'), '"trailing\\\\"');
  assert.equal(quoteWindowsArgument(''), '""');
});

test('Windows supervision uses a private job, suspended launch, and only its owned tree for cleanup', async () => {
  let child;
  let invocation;
  const killed = [];
  const manager = ownedChildren({
    platform: 'win32',
    spawnImpl: (command, args, options) => {
      invocation = { command, args, options };
      child = new FakeChild(42000, options);
      return child;
    },
    execFileImpl: (command, args, options, callback) => {
      killed.push({ command, args, options });
      child.kill('SIGKILL');
      callback(null);
    },
  });
  const owned = manager.start(
    'Windows Runtime',
    'C:\\repo path\\python.exe',
    ['-m', 'pulse_runtime', 'serve'],
    { cwd: 'C:\\repo path', env: { SystemRoot: 'C:\\Windows' } },
  );
  assert.equal(
    invocation.command,
    'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe',
  );
  assert.equal(invocation.options.shell, undefined);
  assert.equal(invocation.options.detached, false);
  const script = Buffer.from(invocation.args.at(-1), 'base64').toString('utf16le');
  assert.match(script, /JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE/);
  assert.match(script, /AssignProcessToJobObject/);
  assert.ok(script.indexOf('if (!AssignProcessToJobObject') < script.indexOf('if (ResumeThread'));
  assert.match(script, /CloseHandle\(job\)/);
  assert.equal(
    JSON.parse(invocation.options.env.PULSE_DEV_COMMAND),
    '"C:\\repo path\\python.exe" "-m" "pulse_runtime" "serve"',
  );
  child.control({ type: 'owned-pid', pid: 42001 });
  assert.equal(owned.pid, 42001);
  await manager.cleanup();
  assert.equal(child.signalCode, 'SIGKILL');
  assert.deepEqual(killed, [
    {
      command: 'C:\\Windows\\System32\\taskkill.exe',
      args: ['/PID', '42000', '/T', '/F'],
      options: { windowsHide: true, timeout: 2000 },
    },
  ]);
});

test('an exited Windows supervisor has already closed its job and is never targeted again', async () => {
  let child;
  const manager = ownedChildren({
    platform: 'win32',
    spawnImpl: (_, __, options) => {
      child = new FakeChild(42000, options);
      return child;
    },
    execFileImpl: () => assert.fail('must not target a PID after its supervisor exited'),
  });
  manager.start('Windows Runtime', 'C:\\repo\\python.exe', ['-m', 'pulse_runtime', 'serve'], {
    env: {},
  });
  child.exit(1);
  await manager.cleanup();
  await manager.cleanup();
  assert.equal(child.signalCode, null);
});

test('cleanup permission failures are reported rather than turning desktop exit into success', async (t) => {
  const h = await harness(t, { initial: info() });
  h.options.kill = () => {
    throw Object.assign(new Error('permission denied'), { code: 'EPERM' });
  };
  await assert.rejects(runDev(h.options), /Could not clean up 1 owned process tree/);
  assert.ok(h.live.has(32101));
});

test(
  'Forge runner reports actual Electron PID and exit before Vite can mask the exit code',
  { timeout: 5000 },
  async (t) => {
    const directory = await temporary(t);
    const entry = path.join(directory, 'forge.mjs');
    await fixture(
      entry,
      `import { EventEmitter } from 'node:events';
export const api = { async start() {
  const child = new EventEmitter();
  Object.assign(child, { pid: 12345, exitCode: null, signalCode: null });
  child.on('exit', () => process.exit(0));
  setTimeout(() => { child.exitCode = 7; child.emit('exit', 7, null); }, 25);
  return child;
} };`,
    );
    const manager = ownedChildren({ graceMs: 20 });
    t.after(() => manager.cleanup());
    const messages = [];
    const child = manager.start('fake Forge', process.execPath, [SELF, '--forge', entry], {
      env: process.env,
      cwd: directory,
      onMessage: (message) => messages.push(message),
    });
    const result = await child.done;
    assert.equal(result.code, 0, child.output);
    assert.deepEqual(messages, [
      { type: 'electron-started', pid: 12345 },
      { type: 'electron-exit', code: 7, signal: null },
    ]);
  },
);

async function connects(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: DESKTOP_HOST, port });
    const finish = (value) => {
      socket.destroy();
      resolve(value);
    };
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.setTimeout(100, () => finish(false));
  });
}

test(
  'real POSIX cleanup kills a stubborn descendant after its parent exits and leaves an unrelated service alive',
  { skip: process.platform === 'win32', timeout: 10000 },
  async (t) => {
    const directory = await temporary(t);
    const manager = ownedChildren({ graceMs: 50 });
    const unrelated = ownedChildren({ graceMs: 50 });
    t.after(async () => {
      await manager.cleanup();
      await unrelated.cleanup();
    });
    const service = `const net = require('node:net');
process.on('SIGTERM', () => {});
net.createServer(s => s.end()).listen(0, '127.0.0.1', function () {
  process.stdout.write(JSON.stringify({ pid: process.pid, port: this.address().port }) + '\\n');
});`;
    const parent = `const { spawn } = require('node:child_process');
const child = spawn(process.execPath, ['-e', ${JSON.stringify(service)}], { stdio: ['ignore', 'pipe', 'inherit'] });
child.stdout.once('data', data => process.stdout.write(data, () => process.exit(0)));`;
    const root = manager.start('controlled parent', process.execPath, ['-e', parent], {
      cwd: directory,
      env: process.env,
    });
    const other = unrelated.start(
      'unrelated controlled service',
      process.execPath,
      ['-e', service],
      { cwd: directory, env: process.env },
    );
    const readAddress = async (child) => JSON.parse((await once(child.process.stdout, 'data'))[0]);
    const [descendant, independent] = await Promise.all([readAddress(root), readAddress(other)]);
    await root.done;
    assert.equal(root.result.code, 0);
    assert.equal(await connects(descendant.port), true);
    assert.equal(await connects(independent.port), true);
    await manager.cleanup();
    const deadline = Date.now() + 2000;
    while ((await connects(descendant.port)) && Date.now() < deadline) await delay(10);
    assert.equal(await connects(descendant.port), false);
    assert.equal(await connects(independent.port), true);
    assert.equal(pidAlive(independent.pid), true);
  },
);
