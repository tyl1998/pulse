import { execFile, spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { constants, readFileSync, writeSync } from 'node:fs';
import { access, mkdir, open, readFile, unlink } from 'node:fs/promises';
import http from 'node:http';
import { createRequire } from 'node:module';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const DESKTOP_HOST = '127.0.0.1';
export const DESKTOP_PORT = 5175;
const SELF = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(SELF), '..');
const PNPM_REMEDY = 'Run pnpm install from the repository root.';
const UV_REMEDY = 'Run uv sync from the repository root (including the default dev group).';
const PYTHON_IMPORTS = [
  'pulse_runtime.server',
  'uvicorn.protocols.http.h11_impl',
  'uvicorn.protocols.websockets.websockets_sansio_impl',
  'uvicorn.lifespan.on',
];

export function checkNodeVersion(version = process.versions.node) {
  const parts = /^(\d+)\.(\d+)\.(\d+)$/.exec(version);
  if (!parts || +parts[1] < 22 || (+parts[1] === 22 && +parts[2] < 12)) {
    throw new Error(
      `Pulse dev requires Node >=22.12.0; found ${version}. Select a supported Node.`,
    );
  }
}

/** Keep the defaults in sync with runtime/pulse_runtime/paths.py. Resolve before changing cwd. */
export function runtimeDirectory({
  platform = process.platform,
  env = process.env,
  home = os.homedir(),
  cwd = ROOT,
} = {}) {
  const p = platform === 'win32' ? path.win32 : path.posix;
  home = env.HOME || env.USERPROFILE || home;
  const directory =
    env.PULSE_RUNTIME_DIR ||
    (platform === 'darwin'
      ? p.join(home, 'Library', 'Application Support', 'Pulse', 'runtime')
      : platform === 'win32'
        ? p.join(env.LOCALAPPDATA || p.join(home, 'AppData', 'Local'), 'Pulse', 'runtime')
        : p.join(env.XDG_STATE_HOME || p.join(home, '.local', 'state'), 'pulse', 'runtime'));
  return p.resolve(cwd, directory);
}

function validPid(pid) {
  return Number.isInteger(pid) && pid > 0 && pid <= 0x7fffffff;
}

export function pidAlive(pid, kill = process.kill) {
  if (!validPid(pid)) return false;
  try {
    kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    if (error.code === 'EPERM') return true;
    throw new Error(`Cannot check Runtime PID ${pid} (${error.code ?? 'unknown error'}).`, {
      cause: error,
    });
  }
}

export function parseDiscovery(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return { pid: null, connection: null };
  }
  const pid = validPid(data?.pid) ? data.pid : null;
  const valid =
    pid !== null &&
    Number.isInteger(data.port) &&
    data.port > 0 &&
    data.port <= 65535 &&
    typeof data.token === 'string' &&
    /^[A-Za-z0-9_-]{16,256}$/.test(data.token) &&
    typeof data.version === 'string' &&
    data.version.length > 0;
  return {
    pid,
    connection: valid ? { pid, port: data.port, token: data.token, version: data.version } : null,
  };
}

export async function readDiscovery(directory, signal) {
  try {
    return parseDiscovery(
      await readFile(path.join(directory, 'runtime.json'), { encoding: 'utf8', signal }),
    );
  } catch (error) {
    signal?.throwIfAborted();
    if (error.code === 'ENOENT') return { pid: null, connection: null };
    throw new Error(
      `Cannot read Runtime discovery in ${directory} (${error.code ?? 'read error'}). Check directory permissions.`,
      { cause: error },
    );
  }
}

export async function acquireLauncherLock(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const lockPath = path.join(directory, 'dev-launcher.lock');
  const contents = JSON.stringify({ pid: process.pid, nonce: randomUUID() });
  let handle;
  try {
    handle = await open(lockPath, 'wx', 0o600);
  } catch (error) {
    if (error.code !== 'EEXIST') {
      throw new Error(
        `Cannot create launcher lock ${lockPath} (${error.code}). Check permissions.`,
        { cause: error },
      );
    }
    // Never reap somebody else's lock on a guess, or race two stale-lock reclaimers.
    throw new Error(
      `Another or interrupted Pulse dev launcher owns ${lockPath}. Verify that launcher has exited before removing only this lock file; no Runtime or other service was stopped.`,
      { cause: error },
    );
  }
  try {
    await handle.writeFile(contents);
  } catch (error) {
    await handle.close();
    await unlink(lockPath);
    throw error;
  }
  await handle.close();
  return async () => {
    try {
      if ((await readFile(lockPath, 'utf8')) === contents) await unlink(lockPath);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  };
}

// node:http deliberately avoids proxies and redirects, including for authenticated health requests.
function loopbackRequest(
  port,
  pathname,
  { signal, timeoutMs = 1000, headers, maxBytes = 16384 } = {},
) {
  signal?.throwIfAborted();
  return new Promise((resolve) => {
    let response;
    let timer;
    let settled = false;
    const finish = (value = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      response?.destroy();
      request.destroy();
      resolve(value);
    };
    const abort = () => finish();
    const request = http.get(
      { hostname: DESKTOP_HOST, port, path: pathname, headers, agent: false },
      (res) => {
        response = res;
        let body = '';
        let bytes = 0;
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          bytes += Buffer.byteLength(chunk);
          if (bytes > maxBytes) finish();
          else body += chunk;
        });
        res.on('error', () => finish());
        res.on('aborted', () => finish());
        res.on('end', () => finish({ status: res.statusCode, body }));
      },
    );
    request.on('error', () => finish());
    timer = setTimeout(finish, timeoutMs);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) finish();
  });
}

export async function probeRuntime(connection, options) {
  const response = await loopbackRequest(connection.port, '/health', {
    ...options,
    headers: { Authorization: `Bearer ${connection.token}` },
  });
  if (response?.status !== 200) return false;
  try {
    const health = JSON.parse(response.body);
    return (
      health?.status === 'ok' &&
      health.pid === connection.pid &&
      health.version === connection.version
    );
  } catch {
    return false;
  }
}

export async function rendererReady(options) {
  const [html, client] = await Promise.all([
    loopbackRequest(DESKTOP_PORT, '/', options),
    loopbackRequest(DESKTOP_PORT, '/@vite/client', { ...options, maxBytes: 1024 * 1024 }),
  ]);
  return (
    html?.status === 200 &&
    html.body.includes('/src/renderer/main.tsx') &&
    client?.status === 200 &&
    client.body.includes('createHotContext')
  );
}

export async function checkDesktopPort(signal) {
  signal?.throwIfAborted();
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', (error) =>
      reject(
        new Error(
          error.code === 'EADDRINUSE'
            ? `Desktop TCP ${DESKTOP_PORT} is already in use. Stop its owner and retry pnpm dev; Pulse only uses http://${DESKTOP_HOST}:${DESKTOP_PORT} (no alternate port).`
            : `Cannot bind desktop ${DESKTOP_HOST}:${DESKTOP_PORT} (${error.code}). Check local networking permissions.`,
        ),
      ),
    );
    server.listen({ host: DESKTOP_HOST, port: DESKTOP_PORT, exclusive: true }, () =>
      server.close(resolve),
    );
  });
  signal?.throwIfAborted();
}

export async function checkDesktopDependencies(
  root = ROOT,
  env = process.env,
  version = process.versions.node,
) {
  checkNodeVersion(version);
  const desktopDir = path.join(root, 'apps', 'desktop');
  const require = createRequire(path.join(desktopDir, 'package.json'));
  const resolve = (name) => {
    try {
      return require.resolve(name);
    } catch {
      throw new Error(`Missing desktop dependency ${name}. ${PNPM_REMEDY}`);
    }
  };
  const cliPackage = resolve('@electron-forge/cli/package.json');
  const cli = JSON.parse(await readFile(cliPackage, 'utf8'));
  const cliRequire = createRequire(cliPackage);
  let forgeEntry;
  let semver;
  try {
    forgeEntry = cliRequire.resolve('@electron-forge/core');
    semver = cliRequire('semver');
  } catch (error) {
    throw new Error(
      `Electron Forge dependencies are incomplete: ${error.code ?? 'module error'}. ${PNPM_REMEDY}`,
      { cause: error },
    );
  }
  if (!semver.satisfies(version, cli.engines.node)) {
    throw new Error(
      `Installed Electron Forge ${cli.version} requires Node ${cli.engines.node}; found ${version}. Select a supported Node (Pulse minimum is 22.12.0).`,
    );
  }
  const desktop = JSON.parse(await readFile(path.join(desktopDir, 'package.json'), 'utf8'));
  const dependencies = new Set([
    ...Object.keys(desktop.dependencies ?? {}),
    ...Object.keys(desktop.devDependencies ?? {}).filter((name) =>
      /^(?:@electron|@vitejs\/|@tailwindcss\/|vite$|tailwindcss$)/.test(name),
    ),
  ]);
  dependencies.delete('@electron-forge/cli');
  dependencies.delete('electron');
  for (const name of dependencies) resolve(name);
  const electronDir = path.dirname(resolve('electron/package.json'));
  let relativeExecutable;
  try {
    relativeExecutable = (await readFile(path.join(electronDir, 'path.txt'), 'utf8')).trim();
  } catch (error) {
    if (error.code !== 'ENOENT')
      throw new Error(`Cannot read installed Electron path (${error.code}). ${PNPM_REMEDY}`, {
        cause: error,
      });
  }
  // Importing electron/index.js can install Electron. Inspect the binary without executing it.
  const electronDist = path.resolve(
    root,
    env.ELECTRON_OVERRIDE_DIST_PATH || path.join(electronDir, 'dist'),
  );
  if (!relativeExecutable && !env.ELECTRON_OVERRIDE_DIST_PATH) {
    throw new Error(
      `Electron binary metadata is missing (electron/path.txt). ${PNPM_REMEDY} Ensure Electron's install script is allowed during installation.`,
    );
  }
  const electron = path.join(electronDist, relativeExecutable || 'electron');
  try {
    await access(electron, process.platform === 'win32' ? constants.F_OK : constants.X_OK);
  } catch {
    throw new Error(
      `Electron executable is missing or not executable: ${electron}. ${PNPM_REMEDY} Ensure Electron's install script is allowed during installation.`,
    );
  }
  return { desktopDir, forgeEntry, electronDist };
}

function failure(child, phase, remedy) {
  const result = child.result;
  const reason = result?.error
    ? `could not spawn (${result.error.code ?? result.error.name})`
    : result?.signal
      ? `exited on ${result.signal}`
      : `exited with code ${result?.code}`;
  const module =
    /(?:No module named |Cannot find (?:package|module) |Could not resolve )["']([^"'\n]+)["']/.exec(
      child.output,
    )?.[1];
  return new Error(
    `${child.label} ${reason} ${phase}.${module ? ` Missing module: ${module}.` : ''} ${remedy}`,
  );
}

export function quoteWindowsArgument(value) {
  return `"${value.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/, '$1$1')}"`;
}

/** Only records created children. An exited POSIX leader can still own a live process group. */
export function ownedChildren({
  spawnImpl = spawn,
  execFileImpl = execFile,
  kill = process.kill,
  platform = process.platform,
  graceMs = 1500,
  signal,
  redact = (text) => text,
} = {}) {
  const children = new Set();
  function start(label, command, args, { cwd, env, output, onMessage } = {}) {
    signal?.throwIfAborted();
    const marker = `PULSE_DEV_${randomUUID()}:`;
    env = { ...env, PULSE_DEV_MARKER: marker };
    if (platform === 'win32') {
      env.PULSE_DEV_COMMAND = JSON.stringify(
        [command, ...args].map(quoteWindowsArgument).join(' '),
      );
      const script = readFileSync(new URL('./dev-windows.ps1', import.meta.url), 'utf8');
      command = path.win32.join(
        env.SystemRoot || 'C:\\Windows',
        'System32',
        'WindowsPowerShell',
        'v1.0',
        'powershell.exe',
      );
      args = [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand',
        Buffer.from(script, 'utf16le').toString('base64'),
      ];
    }
    let processChild;
    try {
      processChild = spawnImpl(command, args, {
        cwd,
        env,
        detached: platform !== 'win32',
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      throw new Error(
        `${label} could not spawn (${error.code ?? error.name}). ${label === 'Electron Forge' ? PNPM_REMEDY : UV_REMEDY}`,
        { cause: error },
      );
    }
    const child = {
      process: processChild,
      label,
      output: '',
      result: null,
      pid: platform === 'win32' ? null : processChild.pid,
      systemRoot: env.SystemRoot || 'C:\\Windows',
    };
    children.add(child);
    child.done = new Promise((resolve) => {
      const settle = (result) => {
        if (child.result) return;
        child.result = result;
        resolve(result);
      };
      processChild.once('error', (error) => settle({ error }));
      processChild.once('exit', (code, exitSignal) => settle({ code, signal: exitSignal }));
    });
    for (const stream of [processChild.stdout, processChild.stderr]) {
      let pending = '';
      const line = (text) => {
        if (text.startsWith(marker)) {
          try {
            const message = JSON.parse(text.slice(marker.length));
            if (message.type === 'owned-pid' && validPid(message.pid)) child.pid = message.pid;
            else onMessage?.(message);
          } catch {
            /* Malformed control output is not a readiness signal. */
          }
          return;
        }
        output?.(redact(text));
      };
      stream?.setEncoding('utf8');
      stream?.on('data', (chunk) => {
        child.output = (child.output + chunk).slice(-16384);
        pending += chunk;
        let newline;
        while ((newline = pending.indexOf('\n')) !== -1) {
          line(pending.slice(0, newline));
          pending = pending.slice(newline + 1);
        }
        if (pending.length > 65536) {
          line('[oversized child output omitted]');
          pending = '';
        }
      });
      stream?.on('end', () => {
        if (pending) line(pending);
      });
    }
    return child;
  }
  async function stop(child) {
    if (!children.has(child)) return;
    const pid = child.process.pid;
    if (pid) {
      if (platform === 'win32') {
        // The supervisor's kernel job kills descendants even when their immediate parent exited.
        // /T also covers PowerShell's compiler if startup was interrupted before job creation.
        if (!child.result)
          await new Promise((resolve, reject) => {
            execFileImpl(
              path.win32.join(child.systemRoot, 'System32', 'taskkill.exe'),
              ['/PID', String(pid), '/T', '/F'],
              { windowsHide: true, timeout: 2000 },
              (error) => {
                if (error && !child.result) reject(error);
                else resolve();
              },
            );
          });
      } else {
        const send = (signalName) => {
          try {
            kill(-pid, signalName);
            return true;
          } catch (error) {
            if (error.code === 'ESRCH') return false;
            throw error;
          }
        };
        if (send('SIGTERM')) {
          const deadline = Date.now() + graceMs;
          while (Date.now() < deadline && send(0)) await delay(Math.min(25, graceMs));
          if (send(0)) send('SIGKILL');
        }
      }
      let timer;
      try {
        await Promise.race([
          child.done,
          new Promise((resolve) => {
            timer = setTimeout(resolve, 500);
          }),
        ]);
      } finally {
        clearTimeout(timer);
      }
    }
    children.delete(child);
  }
  async function cleanup() {
    const results = await Promise.allSettled([...children].reverse().map(stop));
    const errors = results.filter((result) => result.status === 'rejected');
    if (errors.length)
      throw new Error(
        `Could not clean up ${errors.length} owned process tree(s). Check launcher child permissions.`,
      );
  }
  return { start, stop, cleanup };
}

async function abortable(promise, signal) {
  signal.throwIfAborted();
  let abort;
  const interrupted = new Promise((_, reject) => {
    abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, { once: true });
  });
  try {
    return await Promise.race([promise, interrupted]);
  } finally {
    signal.removeEventListener('abort', abort);
  }
}

export async function runDev({
  root = ROOT,
  env = process.env,
  signal: externalSignal,
  nodeVersion = process.versions.node,
  platform = process.platform,
  spawnImpl = spawn,
  kill = process.kill,
  isAlive = pidAlive,
  desktopDependencies = checkDesktopDependencies,
  checkPort = checkDesktopPort,
  probe = probeRuntime,
  rendererProbe = rendererReady,
  runtimeTimeoutMs = 20000,
  desktopTimeoutMs = 60000,
  dependencyTimeoutMs = 15000,
  pollMs = 100,
  graceMs = 1500,
  log = console.log,
  onReady = () => {},
} = {}) {
  checkNodeVersion(nodeVersion);
  root = path.resolve(root);
  const controller = new AbortController();
  const signal = externalSignal
    ? AbortSignal.any([externalSignal, controller.signal])
    : controller.signal;
  const directory = runtimeDirectory({ env, platform, cwd: root });
  env = {
    ...env,
    PULSE_RUNTIME_DIR: directory,
    PYTHONUNBUFFERED: '1',
    PYTHONDONTWRITEBYTECODE: '1',
  };
  const tokens = new Set();
  const redact = (text) => {
    for (const token of tokens) text = text.replaceAll(token, '[REDACTED]');
    return text.replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]');
  };
  const owned = ownedChildren({ spawnImpl, kill, platform, signal, graceMs, redact });
  const pause = () => delay(pollMs, undefined, { signal });
  let failureError;
  let exitCode;
  let releaseLock;
  try {
    signal.throwIfAborted();
    const dependencies = await desktopDependencies(root, env, nodeVersion);
    await checkPort(signal);
    releaseLock = await acquireLauncherLock(directory);
    signal.throwIfAborted();
    let runtime;
    let connection;
    let python;
    let runtimeOwned = false;
    let runtimeRetired = false;
    const observedPids = new Set();
    let deadline = Date.now() + runtimeTimeoutMs;
    while (!connection) {
      signal.throwIfAborted();
      const discovery = await readDiscovery(directory, signal);
      if (discovery.connection) tokens.add(discovery.connection.token);
      if (discovery.pid && isAlive(discovery.pid)) observedPids.add(discovery.pid);
      const candidate = discovery.connection;
      if (
        candidate &&
        isAlive(candidate.pid) &&
        (await probe(candidate, {
          signal,
          timeoutMs: Math.max(1, Math.min(1000, deadline - Date.now())),
        }))
      ) {
        signal.throwIfAborted();
        const current = await readDiscovery(directory, signal);
        if (
          JSON.stringify(current.connection) === JSON.stringify(candidate) &&
          !(runtime?.result && candidate.pid === runtime.pid) &&
          isAlive(candidate.pid)
        ) {
          connection = candidate;
          runtimeOwned = runtime?.pid === connection.pid;
          if (runtime && !runtimeOwned) {
            runtimeRetired = true;
            await owned.stop(runtime);
          }
          break;
        }
      }
      const alive = [...observedPids].filter((pid) => isAlive(pid));
      if (!runtime && alive.length === 0) {
        if (!python) {
          python = path.join(
            root,
            '.venv',
            platform === 'win32' ? 'Scripts' : 'bin',
            platform === 'win32' ? 'python.exe' : 'python',
          );
          try {
            await access(python, platform === 'win32' ? constants.F_OK : constants.X_OK);
          } catch {
            throw new Error(
              `Local .venv Python is missing or not executable: ${python}. ${UV_REMEDY}`,
            );
          }
          const check = owned.start(
            'Python dependency check',
            python,
            ['-c', PYTHON_IMPORTS.map((name) => `import ${name}`).join('; ')],
            { cwd: root, env },
          );
          const checkDeadline = new AbortController();
          const checkTimer = setTimeout(() => checkDeadline.abort(), dependencyTimeoutMs);
          const checkSignal = AbortSignal.any([signal, checkDeadline.signal]);
          try {
            await abortable(check.done, checkSignal);
          } catch {
            signal.throwIfAborted();
            throw new Error(
              `Python dependency check timed out after ${dependencyTimeoutMs}ms. ${UV_REMEDY}`,
            );
          } finally {
            clearTimeout(checkTimer);
            await owned.stop(check);
          }
          if (check.result.error || check.result.code !== 0)
            throw failure(check, 'before Runtime startup', UV_REMEDY);
          deadline = Date.now() + runtimeTimeoutMs;
          continue; // Recheck discovery after imports; another launcher may have won the race.
        }
        runtime = owned.start('Runtime', python, ['-m', 'pulse_runtime', 'serve'], {
          cwd: root,
          env,
        });
        runtime.done.then((result) => {
          if (!runtimeRetired && (runtimeOwned || result.error || result.code !== 0)) {
            controller.abort(failure(runtime, 'before desktop shutdown', UV_REMEDY));
          }
        });
      }
      if (Date.now() >= deadline) {
        if (!runtime && alive.length)
          throw new Error(
            `Existing Runtime PID ${alive.join(', ')} is alive but authenticated /health is not ready or does not match discovery after ${runtimeTimeoutMs}ms. No replacement was started. Check logs and runtime.json in ${directory}; verify the PID's owner and repair stale discovery before retrying.`,
          );
        throw new Error(
          `Runtime readiness timed out after ${runtimeTimeoutMs}ms waiting for authenticated /health matching discovery. ${runtime?.result?.code === 0 ? 'The local Python exited with code 0, but no healthy winning Runtime appeared. ' : ''}Check Runtime logs in ${directory}. ${UV_REMEDY}`,
        );
      }
      await pause();
    }
    signal.throwIfAborted();
    if (runtimeOwned && runtime.result)
      throw failure(runtime, 'before Electron startup', UV_REMEDY);
    log(`${runtimeOwned ? 'Started' : 'Reusing existing'} Runtime (PID ${connection.pid}).`);
    await checkPort(signal);
    let electronPid;
    let ready = false;
    let finishDesktop;
    const desktopDone = new Promise((resolve) => {
      finishDesktop = resolve;
    });
    const desktop = owned.start(
      'Electron Forge',
      process.execPath,
      [SELF, '--forge', dependencies.forgeEntry],
      {
        cwd: dependencies.desktopDir,
        env: {
          ...env,
          ELECTRON_OVERRIDE_DIST_PATH: dependencies.electronDist,
          ELECTRON_SKIP_BINARY_DOWNLOAD: '1',
        },
        output: (line) => log(redact(line)),
        onMessage: (message) => {
          if (message.type === 'electron-started' && validPid(message.pid))
            electronPid = message.pid;
          if (message.type === 'electron-exit') {
            if (ready && message.code === 0 && !message.signal) finishDesktop(0);
            else
              controller.abort(
                new Error(
                  `Electron ${message.signal ? `exited on ${message.signal}` : `exited with code ${message.code}`} ${ready ? 'during development' : 'before desktop readiness'}. Check the Forge output above. ${PNPM_REMEDY}`,
                ),
              );
          }
        },
      },
    );
    desktop.done.then((result) => {
      if (ready && result.code === 0 && !result.error) finishDesktop(0);
      else
        controller.abort(
          failure(desktop, ready ? 'during development' : 'before desktop readiness', PNPM_REMEDY),
        );
    });
    deadline = Date.now() + desktopTimeoutMs;
    while (!ready) {
      signal.throwIfAborted();
      ready =
        !!electronPid &&
        (await rendererProbe({
          signal,
          timeoutMs: Math.max(1, Math.min(1000, deadline - Date.now())),
        }));
      signal.throwIfAborted();
      if (ready) break;
      if (Date.now() >= deadline)
        throw new Error(
          `Desktop readiness timed out after ${desktopTimeoutMs}ms: ${electronPid ? 'Electron started, but the renderer is not serving' : 'Electron has not started; expected renderer at'} http://${DESKTOP_HOST}:${DESKTOP_PORT}. Check Forge output and port ownership. ${PNPM_REMEDY}`,
        );
      await pause();
    }
    log(
      `Electron started; renderer ready at http://${DESKTOP_HOST}:${DESKTOP_PORT}. Ctrl-C stops only this launcher's processes.`,
    );
    onReady();
    exitCode = await abortable(desktopDone, signal);
  } catch (error) {
    failureError = new Error(redact(signal.aborted ? signal.reason.message : error.message), {
      cause: error,
    });
  } finally {
    try {
      await owned.cleanup();
    } catch (error) {
      if (failureError) log(error.message);
      else failureError = error;
    }
    try {
      await releaseLock?.();
    } catch (error) {
      if (failureError) log(`Launcher lock cleanup failed: ${error.message}`);
      else failureError = error;
    }
  }
  if (failureError) throw failureError;
  return exitCode;
}

// Run Forge in an owned Node process, not a package-manager shell. Its API provides the
// actual Electron child; CLI text is silent in noninteractive Forge 8 and is not a ready signal.
export async function runForge(entry) {
  const send = (message) =>
    writeSync(1, `\n${process.env.PULSE_DEV_MARKER}${JSON.stringify(message)}\n`);
  const { api } = await import(pathToFileURL(entry).href);
  const child = await api.start({ dir: process.cwd(), interactive: false });
  const watch = (electron) => {
    const exited = (code, signal) => {
      if (!electron.restarted) send({ type: 'electron-exit', code, signal });
    };
    // Vite's postStart listener can call process.exit(0), even for an Electron failure.
    electron.prependListener('exit', exited);
    electron.once('error', (error) => {
      console.error(error);
      process.exit(1);
    });
    electron.once('restarted', watch);
    if (electron.exitCode !== null || electron.signalCode !== null)
      exited(electron.exitCode, electron.signalCode);
    else if (electron.pid) send({ type: 'electron-started', pid: electron.pid });
  };
  watch(child);
}

export async function main(options = {}) {
  const controller = new AbortController();
  const processSignals = options.processSignals ?? process;
  let interrupted;
  const listeners = ['SIGINT', 'SIGTERM', 'SIGHUP'].map((name) => {
    const listener = () => {
      interrupted ??= name;
      controller.abort(new Error(`Interrupted by ${name}.`));
    };
    processSignals.on(name, listener);
    return [name, listener];
  });
  try {
    return await runDev({ ...options, signal: controller.signal });
  } catch (error) {
    if (!interrupted) (options.error ?? console.error)(`pulse dev: ${error.message}`);
    return interrupted === 'SIGINT'
      ? 130
      : interrupted === 'SIGTERM'
        ? 143
        : interrupted === 'SIGHUP'
          ? 129
          : 1;
  } finally {
    for (const [name, listener] of listeners) processSignals.removeListener(name, listener);
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  if (process.argv[2] === '--forge') {
    const fail = (error) => {
      console.error(error);
      process.exit(1);
    };
    process.on('uncaughtException', fail);
    process.on('unhandledRejection', fail);
    runForge(process.argv[3]).catch(fail);
  } else {
    main().then((code) => {
      process.exitCode = code;
    });
  }
}
