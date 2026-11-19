#!/usr/bin/env node
// P0.7 CDP driver (Node 22+). Does not start Electron or change device/ADB settings.
//   stream-bench.mjs eval <expression> | shot <out.png>
//   stream-bench.mjs run <config.json> <out.json> | soak <config.json> <out.json>
// run config: {scenarios:[{name,start,warmupMs?,frames?,maxFrameWaitMs?,prepare?,motion?,
//   change:{n,tap,roi,threshold,settleMs,timeoutMs,batchSize?:10,maxAttempts?:ceil(n*1.2),batchTimeoutMs?}}]}
// soak config: {start,seconds,intervalMs?:10000,prepare?,motion?,runtimePid,relayPid?,electronPid}
// Shared options: cdpTimeoutMs, connectTimeoutMs, commandTimeoutMs, cleanupTimeoutMs, targetId.
// PIDs may also come from PULSE_RUNTIME_PID / PULSE_RELAY_PID / PULSE_ELECTRON_PID.
// Legacy motion "while true; do input swipe ...; sleep ...; done" is scheduled as bounded
// individual commands. Also accepts {commands:["input swipe ..."],repeat:true}; no remote loops.
// run keeps the legacy scenario array; soak now writes {rows,windows,...completion metadata}.
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
  atomicWriteJson,
  bounded,
  compileMotion,
  connect,
  createProcessSampler,
  message,
  runCommand,
  sleep,
  startMotion,
} from './stream-bench-support.mjs';

const REDACTED = '[REDACTED]';
const credentialKey = (key) => {
  // Decode names only; URL values and diagnostic clocks must not be normalized.
  for (;;) {
    const decoded = key.replace(/%([\da-f]{2})/gi, (_, byte) =>
      String.fromCharCode(Number.parseInt(byte, 16)),
    );
    if (decoded === key) break;
    key = decoded;
  }
  return /(?:tokens?|secrets?|password|passwd|pwd|authorization|cookies?|apiKey|accessKey(?:Id)?|secretKey|privateKey|credentials?|signature)$|^(?:auth|authentication)$/i.test(
    key.replace(/[-_.\s]/g, ''),
  );
};

// Output copies only: never redact the start options sent to the Runtime or raw samples in place.
export function redactBenchOutput(value, sensitive = false) {
  if (Array.isArray(value)) return value.map((item) => redactBenchOutput(item, sensitive));
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        redactBenchOutput(item, sensitive || credentialKey(key)),
      ]),
    );
  }
  if (value === null || value === undefined) return value;
  if (sensitive) return REDACTED;
  if (typeof value !== 'string') return value;
  // Free-form credential containers are opaque, even if malformed or multiline. Quantitative
  // samples are objects above, not diagnostic strings, and are never shortened by this fallback.
  for (const match of value.matchAll(/(["']?)([\w.%+-]+)\1\s*[:=]\s*(?=[{[])/g)) {
    const start = match.index + match[0].length;
    if (credentialKey(match[2]) && !value.startsWith(REDACTED, start)) {
      value = value.slice(0, start) + REDACTED;
      break;
    }
  }
  return value
    .replace(
      /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g,
      REDACTED,
    )
    .replace(/\b(?:https?|wss?)%(?:25)*3a%(?:25)*2f%(?:25)*2f[^\s"'<>]+/gi, (url) => {
      try {
        const decoded = decodeURIComponent(url);
        const safe = redactBenchOutput(decoded);
        return safe === decoded ? url : encodeURIComponent(safe);
      } catch {
        return REDACTED;
      }
    })
    .replace(/(\b(?:https?|wss?):\/\/)[^/\s"'<>?#@]+@/gi, `$1${REDACTED}@`)
    .replace(/([?&#])([^=&#\s"'<>]+)=([^&#\s"'<>]*)/g, (match, prefix, key) =>
      credentialKey(key) ? `${prefix}${key}=${REDACTED}` : match,
    )
    .replace(/(\b(?:set[-_]?)?cookie\s*[:=]\s*)[^\s"'][^\r\n]*/gi, `$1${REDACTED}`)
    .replace(
      /(--(?:token|password|api[-_]key|secret|client[-_]secret)(?:\s+|=))(?:"[^"]*"|'[^']*'|[^\s,;)[\]]+)/gi,
      `$1${REDACTED}`,
    )
    .replace(
      /(["']?)([\w.%+-]+)\1(\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?:Bearer|Basic)\s+(?:\[REDACTED\]|[^\s&,;#"'<>()[\]{}]+)|(?:\[REDACTED\]|[^\s&,;#"'<>()[\]{}])+)/gi,
      (match, quote, key, separator, raw) => {
        if (!credentialKey(key)) {
          if (raw.startsWith('"')) {
            try {
              const decoded = JSON.parse(raw);
              const safe = redactBenchOutput(decoded);
              if (safe !== decoded)
                return `${quote}${key}${quote}${separator}${JSON.stringify(safe)}`;
            } catch {
              // Not a JSON string; leave unrelated diagnostic text unchanged.
            }
          }
          const safe = redactBenchOutput(raw);
          if (safe !== raw) return `${quote}${key}${quote}${separator}${safe}`;
          return match;
        }
        const valueQuote = /^["']/.test(raw) ? raw[0] : '';
        return `${quote}${key}${quote}${separator}${valueQuote}${REDACTED}${valueQuote}`;
      },
    )
    .replace(/(\b(?:Bearer|Basic)\s+)[^\s"'`,;<>()[\]{}]+/gi, `$1${REDACTED}`);
}

const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const number = (value, name, minimum = 0, integer = false) => {
  if (!finite(value) || value < minimum || (integer && !Number.isSafeInteger(value))) {
    throw new Error(`invalid ${name}: ${value}`);
  }
  return value;
};
const positive = (value, name) => number(value, name, 1);
const successfulTrial = (trial) =>
  finite(trial?.sentMs) &&
  finite(trial?.sample?.drawnMs) &&
  finite(trial.sample.drawnMs - trial.sentMs) &&
  trial.sample.drawnMs >= trial.sentMs;

export function validDeviceTime(frame) {
  return (
    finite(frame?.devMs) &&
    frame.clockValid !== false &&
    frame.clockFresh !== false &&
    frame.clockStale !== true &&
    !frame.clockError &&
    (!Object.hasOwn(frame, 'clockRttMs') || (finite(frame.clockRttMs) && frame.clockRttMs >= 0)) &&
    (!Object.hasOwn(frame, 'clockAgeMs') ||
      (finite(frame.clockAgeMs) &&
        frame.clockAgeMs >= 0 &&
        frame.clockAgeMs <= (finite(frame.clockMaxAgeMs) ? frame.clockMaxAgeMs : 15000)))
  );
}

export function frameSummary(frames, windowMs) {
  const values = frames
    .filter((f) => validDeviceTime(f) && finite(f.drawnMs) && f.drawnMs >= f.devMs)
    .map((f) => f.drawnMs - f.devMs)
    .filter(finite)
    .sort((a, b) => a - b);
  const q = (p) => {
    if (!values.length) return null;
    const k = (values.length - 1) * p;
    const lo = Math.floor(k);
    return values[lo] + (values[Math.ceil(k)] - values[lo]) * (k - lo);
  };
  const drawn = frames.filter((f) => finite(f?.drawnMs)).length;
  return {
    frames: frames.length,
    drawn,
    windowMs,
    fps: windowMs > 0 ? (drawn * 1000) / windowMs : null,
    e2eN: values.length,
    e2eMisses: frames.length - values.length,
    e2eP50: q(0.5),
    e2eP95: q(0.95),
    e2eP99: q(0.99),
  };
}

function initialResult(config, kind) {
  return {
    schemaVersion: 2,
    kind,
    name: config.name ?? kind,
    start: config.start,
    phase: 'pending',
    complete: false,
    cleanupStopped: false,
    error: null,
    cleanupErrors: [],
    events: [],
    metrics: [],
    ...(kind === 'run'
      ? {
          frames: [],
          frameTarget: config.frames ?? 300,
          frameSuccesses: 0,
          frameMisses: 0,
          change: config.change
            ? {
                requested: config.change.n,
                trials: [],
                invalidTrials: [],
                successes: 0,
                attempts: 0,
                triggeredAttempts: 0,
                misses: 0,
                unconfirmed: 0,
                complete: false,
                aborted: false,
                error: null,
              }
            : null,
        }
      : { seconds: config.seconds, actualSeconds: 0, rows: [], windows: [], processErrors: [] }),
  };
}

function evaluate(cdp, expression, config, signal, timeoutMs = config.cdpTimeoutMs ?? 30000) {
  return bounded(
    (requestSignal) => cdp.evaluate(expression, { signal: requestSignal, timeoutMs }),
    {
      timeoutMs,
      signal,
      label: expression.includes('changeBench') ? 'changeBench batch' : 'bench CDP call',
    },
  );
}

async function state(cdp, config, result, context, signal = context.signal, timeoutMs) {
  const value = await evaluate(
    cdp,
    '({metrics:window.__pulseBench.metrics(),status:window.__pulseBench.status(),events:window.__pulseBench.events?.() ?? []})',
    config,
    signal,
    timeoutMs,
  );
  if (!value || !Array.isArray(value.events)) throw new Error('invalid bench state response');
  if (value.metrics) result.metrics.push(value.metrics);
  result.lastStatus = value.status;
  result.events.push(...value.events);
  return value;
}

function countFrames(result) {
  result.frameSuccesses = frameSummary(result.frames, 0).e2eN;
  result.frameMisses = result.frames.length - result.frameSuccesses;
}

async function collect(cdp, config, signal, timeoutMs) {
  const frames = await evaluate(cdp, 'window.__pulseBench.collect()', config, signal, timeoutMs);
  if (!Array.isArray(frames)) throw new Error('invalid collect response (expected frame array)');
  return frames;
}

async function captureRun(cdp, config, result, context) {
  result.frames.push(...(await collect(cdp, config, context.signal)));
  countFrames(result);
  await context.save(); // Preserve drained samples even if the following state call fails.
  await state(cdp, config, result, context);
  await context.save();
}

async function withStream(cdp, config, result, context, work) {
  let motion;
  let startAttempted = false;
  result.startedAt = new Date(context.wallNow()).toISOString();
  try {
    if (!config.start || typeof config.start !== 'object')
      throw new Error('config.start is required');
    compileMotion(config.motion); // Reject an unbounded command before touching the device.
    for (const key of [
      'cdpTimeoutMs',
      'connectTimeoutMs',
      'commandTimeoutMs',
      'cleanupTimeoutMs',
    ]) {
      if (config[key] !== undefined) positive(config[key], key);
    }
    result.phase = 'starting';
    await context.save();
    for (const command of config.prepare ?? []) {
      context.signal?.throwIfAborted();
      await context.command(
        process.env.PULSE_ADB ?? 'adb',
        ['-s', config.start.serial, 'shell', command],
        {
          signal: context.signal,
          timeoutMs: config.commandTimeoutMs ?? 15000,
        },
      );
    }
    startAttempted = true;
    await evaluate(
      cdp,
      `window.__pulseBench.start(${JSON.stringify(config.start)})`,
      config,
      context.signal,
    );
    motion = context.motionFactory(config, context);
    const end =
      context.now() + positive(config.firstFrameTimeoutMs ?? 20000, 'firstFrameTimeoutMs');
    for (;;) {
      context.signal?.throwIfAborted();
      motion?.check();
      const remaining = end - context.now();
      if (remaining <= 0) throw new Error('no frames before first-frame timeout');
      const metrics = await evaluate(
        cdp,
        'window.__pulseBench.metrics()',
        config,
        context.signal,
        Math.min(config.cdpTimeoutMs ?? 30000, remaining),
      );
      if (metrics?.framesTotal > 0 && metrics.fps > 0) break;
      await context.wait(Math.min(500, Math.max(0, end - context.now())), context.signal);
    }
    await context.wait(
      number(config.warmupMs ?? (result.kind === 'run' ? 3000 : 0), 'warmupMs'),
      context.signal,
    );
    await collect(cdp, config, context.signal); // Discard warm-up samples, not lifecycle events.
    await state(cdp, config, result, context);
    result.phase = 'sampling';
    await context.save();
    await work(() => {
      context.signal?.throwIfAborted();
      motion?.check();
    });
    motion?.check();
  } catch (error) {
    result.error = message(error);
    result.complete = false;
    if (result.change && !result.change.complete) result.change.error = result.error;
  } finally {
    if (context.signal?.aborted) {
      result.error ??= message(context.signal.reason ?? new Error('interrupted'));
      result.complete = false;
      if (result.change && !result.change.complete) result.change.error ??= result.error;
    }
    result.phase = 'stopping';
    const timeoutMs = config.cleanupTimeoutMs ?? 5000;
    const cleanup = async (label, fn) => {
      try {
        await bounded(fn, { timeoutMs, label });
      } catch (error) {
        result.cleanupErrors.push(`${label}: ${message(error)}`);
      }
    };
    // Cleanup has its own deadline, not the already-aborted run signal. On failure, stop
    // immediately rather than allowing an in-flight changeBench to tap during a final collect.
    const stopStream = () =>
      cleanup('stream stop', async (signal) => {
        let recovery;
        try {
          try {
            await evaluate(cdp, 'window.__pulseBench.stop()', config, signal, timeoutMs);
          } catch (error) {
            if (!cdp.closed || !cdp.targetId) throw error;
            // A lost CDP socket does not imply the renderer stopped. Retry cleanup once, on
            // exactly the original target, never an arbitrary replacement page.
            recovery = await context.connectCdp({
              port: cdp.port,
              targetId: cdp.targetId,
              signal,
              timeoutMs,
              commandTimeoutMs: timeoutMs,
            });
            result.cleanupReconnected = true;
            await evaluate(recovery, 'window.__pulseBench.stop()', config, signal, timeoutMs);
          }
          result.cleanupStopped = true;
        } finally {
          recovery?.close();
        }
      });
    const stopEarly = startAttempted && Boolean(result.error);
    if (stopEarly) await Promise.all([cleanup('motion stop', () => motion?.stop()), stopStream()]);
    else await cleanup('motion stop', () => motion?.stop());
    if (startAttempted) {
      await cleanup('final collect', async (signal) => {
        const frames = await collect(cdp, config, signal, timeoutMs);
        if (result.kind === 'run') {
          result.frames.push(...frames);
          countFrames(result);
        } else result.tailFrames = frames; // Outside timed soak windows, retained for diagnosis.
      });
      // stop is attempted even when start, collect, or motion cleanup failed.
      if (!stopEarly) await stopStream();
      await cleanup('final events', (signal) =>
        state(cdp, config, result, context, signal, timeoutMs),
      );
    }
    if (result.cleanupErrors.length) {
      result.error ??= result.cleanupErrors.join('; ');
      result.complete = false;
    }
    result.phase = 'finished';
    result.finishedAt = new Date(context.wallNow()).toISOString();
    await context.save();
  }
  if (result.error) throw new Error(redactBenchOutput(result.error));
}

export async function runScenario(cdp, config, result, context) {
  await withStream(cdp, config, result, context, async (check) => {
    const targetFrames = number(config.frames ?? 300, 'frames', 0, true);
    if (config.change) {
      const target = number(config.change.n, 'change.n', 1, true);
      const batchSize = number(config.change.batchSize ?? 10, 'change.batchSize', 1, true);
      const maxAttempts = number(
        config.change.maxAttempts ?? Math.ceil(target * 1.2),
        'change.maxAttempts',
        target,
        true,
      );
      const change = Object.assign(result.change, { requested: target, maxAttempts, batchSize });
      while (change.successes < target && change.attempts < maxAttempts) {
        check();
        const n = Math.min(batchSize, target - change.successes, maxAttempts - change.attempts);
        change.attempts += n;
        change.unconfirmed += n;
        const triggeredBefore = change.triggeredAttempts;
        change.triggeredAttempts = null;
        await context.save(); // Attempts are dispatched slots; unconfirmed are NOT asserted misses.
        const timeoutMs =
          config.change.batchTimeoutMs ??
          Math.max(
            config.cdpTimeoutMs ?? 30000,
            n * ((config.change.settleMs ?? 0) + (config.change.timeoutMs ?? 5000) + 1000) + 5000,
          );
        const batch = await evaluate(
          cdp,
          `window.__pulseBench.changeBench(${JSON.stringify({ ...config.change, n })})`,
          config,
          context.signal,
          timeoutMs,
        );
        if (
          !Array.isArray(batch?.trials) ||
          !Number.isSafeInteger(batch.misses) ||
          batch.misses < 0 ||
          batch.trials.length + batch.misses > n
        ) {
          throw new Error('invalid changeBench response');
        }
        const successes = batch.trials.filter(successfulTrial);
        const invalid = batch.trials.filter((trial) => !successfulTrial(trial));
        change.trials.push(...successes);
        change.invalidTrials.push(...invalid);
        change.successes += successes.length;
        change.misses += batch.misses + invalid.length;
        change.unconfirmed -= batch.trials.length + batch.misses;
        if (
          triggeredBefore !== null &&
          Number.isSafeInteger(batch.attempts) &&
          batch.attempts >= 0 &&
          batch.attempts <= n
        ) {
          change.triggeredAttempts = triggeredBefore + batch.attempts;
        }
        change.aborted = Boolean(batch.aborted);
        change.complete =
          change.successes >= target && change.unconfirmed === 0 && !batch.aborted && !batch.error;
        await context.save();
        if (batch.aborted)
          throw new Error('changeBench aborted; remaining dispatched slots are unconfirmed');
        if (batch.error) throw new Error(message(batch.error));
        await captureRun(cdp, config, result, context);
        if (change.unconfirmed) throw new Error('changeBench returned an incomplete batch');
      }
      if (!change.complete)
        throw new Error(
          `S1 target not met: ${change.successes}/${target} successes, ${change.attempts} attempts, ${change.misses} misses`,
        );
    }
    const end = context.now() + positive(config.maxFrameWaitMs ?? 120000, 'maxFrameWaitMs');
    for (;;) {
      check();
      await captureRun(cdp, config, result, context);
      if (result.frameSuccesses >= targetFrames) break;
      if (context.now() >= end)
        throw new Error(
          `S2 target not met: ${result.frameSuccesses}/${targetFrames} valid PTS-to-draw samples (${result.frameMisses} missing/invalid)`,
        );
      await context.wait(Math.min(1000, end - context.now()), context.signal);
    }
    result.complete = true;
  });
}

export async function runSoak(cdp, config, result, context) {
  await withStream(cdp, config, result, context, async (check) => {
    const durationMs = positive(config.seconds, 'seconds') * 1000;
    const intervalMs = Math.min(10000, positive(config.intervalMs ?? 10000, 'intervalMs'));
    const sampler = context.samplerFactory(config, context);
    result.processRoots = sampler.roots;
    result.processMissing = sampler.missing;
    const sampleProcesses = async () => {
      try {
        const value = await sampler.sample(context.signal);
        if (value.missingRoots.length)
          result.processErrors.push(`missing process roots: ${value.missingRoots.join(', ')}`);
        return value;
      } catch (error) {
        context.signal?.throwIfAborted();
        result.processErrors.push(message(error));
        return { ps: [], missingRoots: [], error: message(error) };
      }
    };
    result.processBaseline = await sampleProcesses();
    await collect(cdp, config, context.signal);
    const t0 = context.now();
    const end = t0 + durationMs;
    result.samplingStartedAt = new Date(context.wallNow()).toISOString();
    let last = t0;
    let windowStart = t0;
    let nextWindow = Math.min(end, t0 + 10000);
    let windowFrames = [];
    const finishWindow = () => {
      if (last <= windowStart) return;
      result.windows.push({
        fromSeconds: (windowStart - t0) / 1000,
        toSeconds: (last - t0) / 1000,
        targetWindowMs: 10000,
        ...frameSummary(windowFrames, last - windowStart),
      });
      windowFrames = [];
      windowStart = last;
      nextWindow = Math.min(end, t0 + (Math.floor((last - t0) / 10000) + 1) * 10000);
    };
    try {
      while (last < end) {
        check();
        const nextSample = Math.min(
          end,
          nextWindow,
          t0 + (Math.floor((context.now() - t0) / intervalMs) + 1) * intervalMs,
        );
        await context.wait(Math.max(0, nextSample - context.now()), context.signal);
        check();
        const frames = await collect(cdp, config, context.signal);
        const at = context.now();
        const row = { t: (at - t0) / 1000, ...frameSummary(frames, at - last), ps: [] };
        result.rows.push(row);
        windowFrames.push(...frames);
        last = at;
        result.actualSeconds = (last - t0) / 1000;
        if (last >= nextWindow) finishWindow();
        await context.save();
        const { metrics, status } = await state(cdp, config, result, context);
        Object.assign(
          row,
          {
            metricsFps: metrics?.fps,
            droppedWorker: metrics?.droppedWorker,
            droppedUpstream: metrics?.droppedUpstream,
            clockRttMs: metrics?.clockRttMs,
            upstream: status?.upstream,
          },
          await sampleProcesses(),
        );
        await context.save();
      }
      result.processComplete = sampler.missing.length === 0 && result.processErrors.length === 0;
      if (!result.processComplete)
        throw new Error(
          `soak duration reached but process coverage incomplete: ${[...sampler.missing, ...result.processErrors].join('; ')}`,
        );
      result.complete = true;
    } finally {
      finishWindow();
      await context.save();
    }
  });
}

export async function executeBench(
  kind,
  config,
  {
    save,
    signal,
    connectCdp = connect,
    command = runCommand,
    wait = sleep,
    now = () => performance.now(),
    wallNow = () => Date.now(),
    motionFactory = startMotion,
    samplerFactory = createProcessSampler,
  } = {},
) {
  if (!['run', 'soak'].includes(kind))
    throw new Error(redactBenchOutput(`invalid benchmark kind: ${kind}`));
  if (kind === 'run' && (!Array.isArray(config.scenarios) || config.scenarios.length === 0))
    throw new Error('run requires nonempty scenarios');
  const results =
    kind === 'run'
      ? config.scenarios.map((sc) => initialResult({ ...config, ...sc }, kind))
      : initialResult(config, kind);
  const context = {
    save: () => save(redactBenchOutput(results)),
    signal,
    command,
    wait,
    now,
    wallNow,
    motionFactory,
    samplerFactory,
    connectCdp,
  };
  let cdp;
  try {
    await context.save();
    cdp = await connectCdp({
      signal,
      timeoutMs: config.connectTimeoutMs ?? 10000,
      commandTimeoutMs: config.cdpTimeoutMs ?? 30000,
      targetId: config.targetId,
    });
    if (kind === 'run') {
      for (let i = 0; i < config.scenarios.length; i++) {
        await runScenario(cdp, { ...config, ...config.scenarios[i] }, results[i], context);
      }
    } else await runSoak(cdp, config, results, context);
  } catch (error) {
    for (const result of kind === 'run' ? results : [results]) {
      if (result.phase === 'pending') {
        result.error = `not started: ${message(error)}`;
        result.phase = 'failed';
        if (result.change) result.change.error = result.error;
      }
    }
    await context.save();
    // Do not attach the original cause: CDP errors may embed connection credentials.
    // eslint-disable-next-line preserve-caught-error
    throw new Error(redactBenchOutput(message(error)));
  } finally {
    cdp?.close();
  }
  return redactBenchOutput(results);
}

export async function main(args = process.argv.slice(2)) {
  const [command, a, b] = args;
  if (
    !['eval', 'shot', 'run', 'soak'].includes(command) ||
    !a ||
    (['run', 'soak'].includes(command) && !b)
  ) {
    console.error(
      'usage: stream-bench.mjs eval <expr> | shot <out.png> | run <config.json> <out.json> | soak <config.json> <out.json>',
    );
    return 2;
  }
  const controller = new AbortController();
  let exitCode = 0;
  const interrupt = (name) => {
    exitCode = name === 'SIGINT' ? 130 : 143;
    controller.abort(new Error(`interrupted (${name})`));
  };
  const onInt = () => interrupt('SIGINT');
  const onTerm = () => interrupt('SIGTERM');
  process.on('SIGINT', onInt);
  process.on('SIGTERM', onTerm);
  let cdp;
  let saved = false;
  try {
    if (command === 'run' || command === 'soak') {
      const config = JSON.parse(await readFile(a, 'utf8'));
      await executeBench(command, config, {
        signal: controller.signal,
        save: async (value) => {
          await atomicWriteJson(b, value);
          saved = true;
        },
      });
      console.log(redactBenchOutput(`${command} complete: ${b}`));
    } else {
      cdp = await connect({ signal: controller.signal });
      if (command === 'eval')
        console.log(
          JSON.stringify(
            redactBenchOutput(await cdp.evaluate(a, { signal: controller.signal })),
            null,
            1,
          ),
        );
      else {
        const result = await cdp.call(
          'Page.captureScreenshot',
          { format: 'png' },
          { signal: controller.signal },
        );
        await writeFile(a, Buffer.from(result.data, 'base64'));
      }
    }
  } catch (error) {
    if (b && !saved && (command === 'run' || command === 'soak')) {
      try {
        await atomicWriteJson(
          b,
          redactBenchOutput({
            schemaVersion: 2,
            kind: command,
            complete: false,
            error: message(error),
          }),
        );
      } catch (saveError) {
        console.error(redactBenchOutput(`cannot preserve failure result: ${message(saveError)}`));
      }
    }
    console.error(
      redactBenchOutput(`stream-bench: ${message(error)}${saved ? `; partial result: ${b}` : ''}`),
    );
    return exitCode || 1;
  } finally {
    cdp?.close();
    process.removeListener('SIGINT', onInt);
    process.removeListener('SIGTERM', onTerm);
  }
  return exitCode;
}

// Importing this module in node --test never connects to a browser/device.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      console.error(redactBenchOutput(`stream-bench: ${message(error)}`));
      process.exitCode = 1;
    },
  );
}
