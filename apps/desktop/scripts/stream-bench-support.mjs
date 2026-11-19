import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { open, rename, rm } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

export const sleep = (ms, signal) => delay(ms, undefined, { signal });
export const message = (error) => (error instanceof Error ? error.message : String(error));
const abortError = (signal) => signal.reason ?? new Error('interrupted');

// Keep a referenced timer: a disconnected WebSocket must not leave an unsettled await at exit.
export function bounded(task, { timeoutMs = 30000, signal, label = 'operation' } = {}) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) {
    return Promise.reject(new Error(`invalid ${label} timeout: ${timeoutMs}`));
  }
  if (signal?.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      fn(value);
    };
    const cancel = (error) => {
      controller.abort(error);
      finish(reject, error);
    };
    const onAbort = () => cancel(abortError(signal));
    const timer = setTimeout(
      () => cancel(new Error(`${label} timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );
    signal?.addEventListener('abort', onAbort, { once: true });
    Promise.resolve()
      .then(() => {
        controller.signal.throwIfAborted();
        return task(controller.signal);
      })
      .then(
        (value) => finish(resolve, value),
        (error) => finish(reject, error),
      );
  });
}

export async function connect({
  port = process.env.PULSE_REMOTE_DEBUG_PORT ?? '9333',
  timeoutMs = 10000,
  commandTimeoutMs = 30000,
  targetId,
  signal,
  fetchImpl = globalThis.fetch,
  WebSocketImpl = globalThis.WebSocket,
} = {}) {
  const targets = await bounded(
    async (requestSignal) => {
      const response = await fetchImpl(`http://127.0.0.1:${port}/json/list`, {
        signal: requestSignal,
      });
      if (!response.ok) throw new Error(`CDP discovery HTTP ${response.status}`);
      return response.json();
    },
    { timeoutMs, signal, label: 'CDP discovery' },
  );
  const page = targets.find((t) =>
    targetId ? t.id === targetId : t.type === 'page' && !t.url?.startsWith('devtools'),
  );
  if (!page?.webSocketDebuggerUrl) throw new Error('no CDP page target');
  const ws = new WebSocketImpl(page.webSocketDebuggerUrl);
  const pending = new Map();
  let nextId = 0;
  let closed = null;
  let openResolve;
  let openReject;
  const opened = new Promise((resolve, reject) => {
    openResolve = resolve;
    openReject = reject;
  });
  // Abort may win before bounded() attaches to this promise; close still rejects it.
  opened.catch(() => undefined);
  const fail = (error) => {
    closed ??= error;
    openReject(closed);
    for (const waiter of pending.values()) waiter.reject(closed);
    pending.clear();
  };
  const close = (error = new Error('CDP connection closed')) => {
    fail(error);
    try {
      ws.close();
    } catch {
      // Closing a socket which failed during its handshake can itself throw.
    }
  };
  ws.onopen = () => openResolve();
  ws.onerror = () => close(new Error('CDP WebSocket error'));
  ws.onclose = (event) =>
    fail(new Error(`CDP WebSocket closed (${event.code ?? '?'} ${event.reason ?? ''})`));
  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(String(event.data));
      const waiter = pending.get(data.id);
      if (!waiter) return;
      pending.delete(data.id);
      if (data.error) waiter.reject(new Error(`CDP: ${data.error.message}`));
      else waiter.resolve(data.result);
    } catch (error) {
      close(new Error(`invalid CDP message: ${message(error)}`));
    }
  };
  try {
    await bounded(() => opened, { timeoutMs, signal, label: 'CDP WebSocket connect' });
  } catch (error) {
    close(error);
    throw error;
  }
  const call = (method, params = {}, options = {}) => {
    if (closed) return Promise.reject(closed);
    const id = ++nextId;
    return bounded(
      () =>
        new Promise((resolve, reject) => {
          if (closed) {
            reject(closed);
            return;
          }
          pending.set(id, { resolve, reject });
          try {
            ws.send(JSON.stringify({ id, method, params }));
          } catch (error) {
            reject(error);
          }
        }),
      { timeoutMs: commandTimeoutMs, ...options, label: `CDP ${method}` },
    ).finally(() => pending.delete(id));
  };
  return {
    port,
    targetId: page.id,
    get closed() {
      return Boolean(closed);
    },
    call,
    async evaluate(expression, options) {
      const result = await call(
        'Runtime.evaluate',
        { expression, awaitPromise: true, returnByValue: true },
        options,
      );
      if (result?.exceptionDetails) {
        throw new Error(
          result.exceptionDetails.exception?.description ??
            result.exceptionDetails.text ??
            'CDP eval failed',
        );
      }
      return result?.result?.value;
    },
    close,
  };
}

export async function atomicWriteJson(path, value) {
  const data = `${JSON.stringify(value)}\n`;
  const temporary = join(dirname(path), `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`);
  let file;
  try {
    file = await open(temporary, 'wx', 0o600);
    await file.writeFile(data);
    await file.sync();
    await file.close();
    file = null;
    await rename(temporary, path);
  } finally {
    await file?.close().catch(() => undefined);
    await rm(temporary, { force: true }).catch(() => undefined);
  }
}

// Only our spawned process group is signalled. No global process-name matching/pkill.
export function runCommand(
  file,
  args,
  {
    signal,
    timeoutMs = 15000,
    killGraceMs = 300,
    spawnImpl = spawn,
    kill = process.kill.bind(process),
  } = {},
) {
  if (signal?.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const child = spawnImpl(file, args, {
      detached: process.platform !== 'win32',
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, LC_ALL: 'C' },
    });
    let stdout = '';
    let stderr = '';
    let failure;
    let finished = false;
    let escalation;
    const signalTree = (name) => {
      if (!child.pid) return;
      try {
        if (process.platform === 'win32') child.kill(name);
        else kill(-child.pid, name);
      } catch (error) {
        if (error.code !== 'ESRCH') failure ??= error;
      }
    };
    const finish = (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      clearTimeout(escalation);
      signal?.removeEventListener('abort', onAbort);
      if (error) reject(Object.assign(error, { stdout, stderr }));
      else resolve(stdout);
    };
    const terminate = (error) => {
      if (failure || finished) return;
      failure = error;
      signalTree('SIGTERM');
      escalation = setTimeout(() => {
        signalTree('SIGKILL');
        finish(failure);
      }, killGraceMs);
    };
    const onAbort = () => terminate(abortError(signal));
    const timer = setTimeout(
      () => terminate(new Error(`${file} timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );
    signal?.addEventListener('abort', onAbort, { once: true });
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
      if (stdout.length > 4 * 1024 * 1024) terminate(new Error(`${file}: stdout limit exceeded`));
    });
    child.stderr.on('data', (chunk) => {
      stderr += chunk;
      if (stderr.length > 1024 * 1024) terminate(new Error(`${file}: stderr limit exceeded`));
    });
    child.on('error', (error) => finish(error));
    child.on('close', (code, exitSignal) => {
      signalTree('SIGKILL'); // Also reap any child left in our group after its parent exits.
      finish(
        failure ??
          (code === 0
            ? null
            : Object.assign(new Error(`${file} exited ${code ?? exitSignal}: ${stderr.trim()}`), {
                exitCode: code,
              })),
      );
    });
  });
}

export function compileMotion(motion) {
  if (!motion) return null;
  let commands;
  let repeat;
  if (typeof motion === 'string') {
    const source = motion.trim();
    const loop = source.match(/^while\s+(?:true|:)\s*;\s*do\s+([\s\S]*?)\s*;\s*done\s*;?$/);
    repeat = Boolean(loop);
    commands = (loop ? loop[1] : source)
      .split(/[;\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
  } else {
    commands = motion.commands;
    repeat = motion.repeat ?? true;
  }
  if (!Array.isArray(commands) || commands.length === 0)
    throw new Error('motion requires bounded input commands');
  const steps = commands.map((command) => {
    const tokens = String(command).trim().split(/\s+/);
    if (tokens[0] === 'sleep' && tokens.length === 2) {
      const ms = Number(tokens[1]) * 1000;
      if (Number.isFinite(ms) && ms >= 0 && ms <= 10000) return { sleepMs: ms };
    }
    const args = tokens.slice(1);
    if (['touchscreen', 'mouse', 'stylus'].includes(args[0])) args.shift();
    const kind = args.shift();
    const numeric = args.every((x) => /^-?\d+$/.test(x) && Math.abs(Number(x)) <= 100000);
    if (
      tokens[0] === 'input' &&
      numeric &&
      ((kind === 'tap' && args.length === 2) ||
        (kind === 'swipe' &&
          (args.length === 4 ||
            (args.length === 5 && Number(args[4]) >= 0 && Number(args[4]) <= 10000))))
    )
      return { args: tokens };
    throw new Error(
      `unsafe/unbounded motion command: ${command}; use input tap/swipe and bounded sleep (legacy while true loops are supported)`,
    );
  });
  if (!steps.some((step) => step.args))
    throw new Error('motion requires at least one input command');
  return { steps, repeat };
}

export function startMotion(config, { signal, command = runCommand, wait = sleep } = {}) {
  const compiled = compileMotion(config.motion);
  if (!compiled) return null;
  const controller = new AbortController();
  const motionSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
  let failure;
  const done = (async () => {
    do {
      for (const step of compiled.steps) {
        motionSignal.throwIfAborted();
        if (step.args) {
          await command(
            process.env.PULSE_ADB ?? 'adb',
            ['-s', config.start.serial, 'shell', ...step.args],
            {
              signal: motionSignal,
              timeoutMs: config.commandTimeoutMs ?? 15000,
            },
          );
        } else await wait(step.sleepMs, motionSignal);
      }
      if (compiled.repeat) await wait(20, motionSignal);
    } while (compiled.repeat);
  })().catch((error) => {
    if (!motionSignal.aborted) failure = error;
  });
  return {
    check() {
      if (failure) throw failure;
    },
    async stop() {
      controller.abort(new Error('motion stopped'));
      await done;
      if (failure) throw failure;
    },
  };
}

const cpuSeconds = (value) => {
  const match = value.match(/^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$/);
  return match
    ? Number(match[1] ?? 0) * 86400 +
        Number(match[2] ?? 0) * 3600 +
        Number(match[3]) * 60 +
        Number(match[4])
    : null;
};

export function createProcessSampler(
  config,
  { command = runCommand, now = () => performance.now(), env = process.env } = {},
) {
  const roots = {};
  for (const group of ['runtime', 'relay', 'electron']) {
    const value = config[`${group}Pid`] ?? env[`PULSE_${group.toUpperCase()}_PID`];
    if (value === undefined || value === null || value === '') continue;
    const pid = Number(value);
    if (!Number.isSafeInteger(pid) || pid <= 1) throw new Error(`invalid ${group}Pid: ${value}`);
    roots[group] = pid;
  }
  const required = ['runtime', 'electron', ...(config.start?.mode === 'relay' ? ['relay'] : [])];
  let previous = new Map();
  return {
    roots,
    missing: required.filter((group) => !roots[group]),
    async sample(signal) {
      if (!Object.keys(roots).length) return { ps: [], missingRoots: required };
      const options = { signal, timeoutMs: config.processTimeoutMs ?? 5000 };
      // The global snapshot contains IDs only. Read command/RSS/CPU for our tree alone.
      const tree = (await command('ps', ['-axo', 'pid=,ppid='], options))
        .trim()
        .split('\n')
        .map((line) => line.trim().split(/\s+/).map(Number))
        .filter(([pid, ppid]) => pid > 1 && ppid >= 0);
      const live = new Set(tree.map(([pid]) => pid));
      const selected = new Map(
        Object.entries(roots)
          .filter(([, pid]) => live.has(pid))
          .map(([group, pid]) => [pid, group]),
      );
      let changed = true;
      while (changed) {
        changed = false;
        for (const [pid, ppid] of tree) {
          if (!selected.has(pid) && selected.has(ppid)) {
            selected.set(pid, selected.get(ppid));
            changed = true;
          }
        }
      }
      if (!selected.size) return { ps: [], missingRoots: Object.keys(roots) };
      let output;
      try {
        output = await command(
          'ps',
          ['-p', [...selected.keys()].join(','), '-o', 'pid=,ppid=,rss=,%cpu=,time=,args='],
          options,
        );
      } catch (error) {
        signal?.throwIfAborted();
        if (error.exitCode !== 1 || !error.stdout?.trim()) throw error;
        output = error.stdout; // A child may exit between the two snapshots.
      }
      const at = now();
      const current = new Map();
      const ps = [];
      for (const line of output.trim().split('\n')) {
        const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\S+)\s+(.*)$/);
        if (!match) continue;
        const [, pidText, parent, rss, cpu, time, args] = match;
        const pid = Number(pidText);
        if (!selected.has(pid)) continue;
        const total = cpuSeconds(time);
        const before = previous.get(pid);
        const cpuWindow =
          total !== null &&
          before &&
          before.total !== null &&
          total >= before.total &&
          at > before.at
            ? ((total - before.total) * 100000) / (at - before.at)
            : null;
        const group = selected.get(pid);
        const type = args.match(/(?:^|\s)--type=([^\s]+)/)?.[1];
        ps.push({
          pid,
          ppid: Number(parent),
          group,
          role: type ? `${group}/${type}` : group,
          rssMb: Number(rss) / 1024,
          cpu: Number(cpu),
          cpuWindow,
          cpuTimeSec: total,
          cmd: args.split(/\s+/)[0],
        });
        current.set(pid, { total, at });
      }
      previous = current;
      return {
        ps,
        missingRoots: Object.entries(roots)
          .filter(([, pid]) => !current.has(pid))
          .map(([group]) => group),
      };
    },
  };
}
