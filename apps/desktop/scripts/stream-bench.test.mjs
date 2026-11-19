import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { executeBench, frameSummary, main, redactBenchOutput } from './stream-bench.mjs';
import {
  atomicWriteJson,
  compileMotion,
  connect,
  createProcessSampler,
  runCommand,
  startMotion,
} from './stream-bench-support.mjs';

const frame = (overrides = {}) => ({
  devMs: 100,
  rtRecvMs: 120,
  rtSendMs: 125,
  guiRecvMs: 140,
  decodeStartMs: 141,
  decodedMs: 145,
  drawnMs: 150,
  key: true,
  ...overrides,
});
const trial = () => ({ sentMs: 95, sample: frame() });
const scenario = (change = {}) => ({
  name: 'fake',
  start: { serial: 'NOT-A-DEVICE', mode: 'direct' },
  warmupMs: 0,
  frames: 0,
  cleanupTimeoutMs: 30,
  change: { n: 10, tap: { x: 1, y: 2 }, settleMs: 0, timeoutMs: 1, ...change },
});

function fakeCdp({
  batch = (n) => ({ trials: Array.from({ length: n }, trial), misses: 0 }),
  onState,
  onCollect,
  failStart,
} = {}) {
  const calls = [];
  const cdp = {
    calls,
    batches: [],
    closed: false,
    stopped: false,
    async evaluate(expression, options) {
      calls.push(expression);
      if (expression.startsWith('window.__pulseBench.start(')) {
        if (failStart) throw new Error('start failed');
        return;
      }
      if (expression === 'window.__pulseBench.stop()') {
        assert.equal(options.signal.aborted, false, 'cleanup must not reuse an aborted run signal');
        cdp.stopped = true;
        return;
      }
      if (expression === 'window.__pulseBench.collect()') return onCollect?.() ?? [frame()];
      if (expression === 'window.__pulseBench.metrics()') return { framesTotal: 1, fps: 60 };
      if (expression.startsWith('({metrics:')) {
        return (
          onState?.() ?? {
            metrics: { fps: 60, clockRttMs: 2 },
            status: { state: 'open' },
            events: [{ atMs: 1, status: { state: 'open' } }],
          }
        );
      }
      if (expression.startsWith('window.__pulseBench.changeBench(')) {
        const options = JSON.parse(expression.slice('window.__pulseBench.changeBench('.length, -1));
        cdp.batches.push(options.n);
        return batch(options.n, cdp.batches.length);
      }
      throw new Error(`unexpected fake expression: ${expression}`);
    },
    close() {
      cdp.closed = true;
    },
  };
  return cdp;
}

function harness(cdp, extra = {}) {
  const snapshots = [];
  let time = 0;
  const options = {
    connectCdp: async () => cdp,
    command: async () => {
      throw new Error('no real commands allowed in fake bench');
    },
    wait: async (ms, signal) => {
      signal?.throwIfAborted();
      time += ms;
    },
    now: () => time,
    wallNow: () => 1700000000000 + time,
    save: async (value) => {
      snapshots.push(structuredClone(value));
    },
    motionFactory: () => null,
    ...extra,
  };
  return { options, snapshots, now: () => time };
}

function socketHarness({ open = true } = {}) {
  const sockets = [];
  class Socket {
    constructor() {
      this.sent = [];
      sockets.push(this);
      if (open) queueMicrotask(() => this.onopen?.());
    }
    send(value) {
      this.sent.push(JSON.parse(value));
    }
    close() {
      this.onclose?.({ code: 1000, reason: 'fake close' });
    }
  }
  return {
    sockets,
    options: {
      WebSocketImpl: Socket,
      timeoutMs: 20,
      commandTimeoutMs: 20,
      fetchImpl: async () => ({
        ok: true,
        json: async () => [{ type: 'page', url: 'app://fake', webSocketDebuggerUrl: 'ws://fake' }],
      }),
    },
  };
}

function mockCli(t, respond) {
  const h = socketHarness();
  class Socket extends h.options.WebSocketImpl {
    send(value) {
      super.send(value);
      const request = this.sent.at(-1);
      Promise.resolve()
        .then(() => respond(request, this))
        .then((reply) => {
          if (reply) this.onmessage?.({ data: JSON.stringify({ id: request.id, ...reply }) });
        });
    }
  }
  t.mock.method(globalThis, 'fetch', h.options.fetchImpl);
  // Mock functions retain the native constructor prototype, which has private WebSocket state.
  const original = globalThis.WebSocket;
  globalThis.WebSocket = Socket;
  t.after(() => {
    globalThis.WebSocket = original;
  });
  return {
    sockets: h.sockets,
    log: t.mock.method(console, 'log', () => {}),
    error: t.mock.method(console, 'error', () => {}),
  };
}

test('output redaction preserves nested shapes and raw sample values without mutating inputs', () => {
  const keys = [
    'relayToken',
    'ACCESS_TOKEN',
    'refresh-token',
    '%74oken',
    'idToken',
    'clientSecret',
    'password',
    'passwd',
    'pwd',
    'Authorization',
    'Proxy-Authorization',
    'Cookie',
    'Set-Cookie',
    'X-Api-Key',
    'accessKeyId',
    'secret_key',
    'privateKey',
    'signature',
  ];
  const sample = frame({
    seq: 9007199254740991,
    session: 'session-17',
    devMs: -0,
    clockRttMs: NaN,
    clockAgeMs: Infinity,
    clockMaxAgeMs: null,
    clockFresh: false,
    optional: undefined,
  });
  const input = {
    samples: [sample, null],
    nested: [{ ...Object.fromEntries(keys.map((key) => [key, 'FAKE_SECRET'])) }],
    credentials: [{ user: 'FAKE_USER', value: ['FAKE_PASSWORD', null] }],
    tokenCount: 7,
    secretExpiresAt: 1700000000000,
    session: { id: 'session-17', seq: 0 },
  };
  const before = structuredClone(input);
  const output = redactBenchOutput(input);
  assert.deepEqual(input, before);
  assert.notEqual(output, input);
  assert.notEqual(output.samples[0], sample);
  assert.deepEqual(output.samples, input.samples);
  assert.deepEqual(output.session, input.session);
  assert.equal(output.tokenCount, 7);
  assert.equal(output.secretExpiresAt, 1700000000000);
  assert.deepEqual(output.nested, [Object.fromEntries(keys.map((key) => [key, '[REDACTED]']))]);
  assert.deepEqual(output.credentials, [{ user: '[REDACTED]', value: ['[REDACTED]', null] }]);
  assert.deepEqual(redactBenchOutput(output), output);
});

test('output strings redact credentials and encoded URL tokens while retaining diagnostic fields', () => {
  const url =
    'WSS://relay.invalid/video?session=s%2F17&%54o%4BeN=A%26B%3Dsecret&seq=001&ToKeN=second#end';
  const safe =
    'WSS://relay.invalid/video?session=s%2F17&%54o%4BeN=[REDACTED]&seq=001&ToKeN=[REDACTED]#end';
  for (const [input, expected] of [
    [url, safe],
    [
      'ws://relay.invalid/?%2574oken=encoded&clockRttMs=2.000',
      'ws://relay.invalid/?%2574oken=[REDACTED]&clockRttMs=2.000',
    ],
    [
      'wss://user:pass@relay.invalid/?api_key=other&seq=0',
      'wss://[REDACTED]@relay.invalid/?api_key=[REDACTED]&seq=0',
    ],
    ['failed (bEaReR abc.DEF-_/+=); seq=7', 'failed (bEaReR [REDACTED]); seq=7'],
    ['Authorization: Basic dXNlcjpwYXNz; seq=7', 'Authorization: [REDACTED]; seq=7'],
    [
      'relayToken=plain; password="space and \\"quote"; seq=7',
      'relayToken=[REDACTED]; password="[REDACTED]"; seq=7',
    ],
    ['{"api_key":"json-secret","seq":7}', '{"api_key":"[REDACTED]","seq":7}'],
  ]) {
    assert.equal(redactBenchOutput(input), expected);
    assert.equal(redactBenchOutput(expected), expected);
  }
  for (const encode of [
    encodeURIComponent,
    (value) => encodeURIComponent(encodeURIComponent(value)),
  ]) {
    assert.equal(redactBenchOutput(encode(url)), encode(safe));
  }
  const harmless = 'ws://relay.invalid/?session=s%2f17&seq=0001&clockAgeMs=0.000&tokenCount=4';
  assert.equal(redactBenchOutput(harmless), harmless);
});

test('diagnostic strings redact full cookies, CLI credentials and nested JSON error text', () => {
  for (const input of [
    'Cookie: sid=COOKIE_SECRET; refresh=REFRESH_SECRET',
    '--token CLI_SECRET --host localhost',
    '-----BEGIN PRIVATE KEY-----\nPEM_SECRET\n-----END PRIVATE KEY-----',
    JSON.stringify({ message: 'password=INNER_SECRET' }),
    JSON.stringify({ message: JSON.stringify({ token: 'NESTED_SECRET' }) }),
    'exception {"credentials": {"value": "CONTAINER_SECRET"}}',
    'token=["LIST_SECRET"]',
    'credentials={\n"unlabelled": "MULTILINE_SECRET"\n}',
    'wss://user:prefix%2FUSERINFO_SECRET@relay.invalid/ws?token=prefix%26QUERY_SECRET',
  ]) {
    const safe = redactBenchOutput(input);
    assert.doesNotMatch(safe, /[A-Z]+_SECRET/);
    assert.equal(redactBenchOutput(safe), safe);
  }
});

test('initial, incremental and returned run results redact copies but send the original start options', async () => {
  const start = {
    ...scenario().start,
    mode: 'relay',
    relayToken: 'START_SECRET',
    relayUrl: 'wss://relay.invalid/?ToKeN=URL_SECRET&session=s17&seq=7',
    headers: { Authorization: 'Bearer HEADER_SECRET' },
  };
  const sample = Object.freeze(frame({ seq: 7, session: 's17', clockRttMs: 2.125, clockAgeMs: 0 }));
  const status = {
    state: 'open',
    seq: 7,
    session: 's17',
    reason: 'wss://relay.invalid/?token=STATE_SECRET&seq=7',
  };
  const cdp = fakeCdp({
    onCollect: () => [sample],
    onState: () => ({
      metrics: { fps: 60.25, clockRttMs: 2.125, token: 'METRIC_SECRET' },
      status,
      events: [{ atMs: 1.125, status }],
    }),
    batch: (n) => ({
      trials: Array.from({ length: n }, () => ({ sentMs: 95.25, sample })),
      misses: 0,
    }),
  });
  const config = { scenarios: [{ ...scenario({ n: 1 }), start }] };
  const before = structuredClone(config);
  const h = harness(cdp);
  const results = await executeBench('run', config, h.options);
  assert.deepEqual(config, before);
  const sent = cdp.calls.find((expression) => expression.startsWith('window.__pulseBench.start('));
  assert.deepEqual(JSON.parse(sent.slice('window.__pulseBench.start('.length, -1)), start);
  assert.equal(h.snapshots[0][0].phase, 'pending');
  assert.ok(h.snapshots.some(([result]) => result.phase === 'sampling' && result.frames.length));
  for (const snapshot of [...h.snapshots, results]) {
    assert.doesNotMatch(JSON.stringify(snapshot), /(?:START|URL|HEADER|STATE|METRIC)_SECRET/);
    assert.equal(snapshot[0].start.relayToken, '[REDACTED]');
    assert.equal(snapshot[0].start.serial, start.serial);
  }
  const [result] = results;
  assert.equal(result.complete, true);
  assert.deepEqual(result.frames, [sample, sample, sample]);
  assert.deepEqual(result.change.trials, [{ sentMs: 95.25, sample }]);
  assert.equal(result.lastStatus.seq, 7);
  assert.equal(result.lastStatus.session, 's17');
  assert.equal(result.events[0].atMs, 1.125);
  assert.equal(result.metrics[0].fps, 60.25);
});

test('partial run files and thrown failures redact state, close reasons and cleanup errors', async () => {
  const directory = await mkdtemp(
    join(process.env.PULSE_BENCH_TEST_TMP ?? tmpdir(), 'pulse-redact-'),
  );
  const path = join(directory, 'new-partial.json');
  try {
    const cdp = fakeCdp({
      batch: () => {
        throw new Error(
          'CDP WebSocket closed (1006 wss://relay.invalid/?TOKEN=CLOSE_SECRET&seq=7)',
        );
      },
      onState: () => ({
        metrics: { fps: 60 },
        status: { error: 'Bearer STATE_SECRET', session: 's17' },
        events: [{ reason: 'password=EVENT_SECRET', seq: 7 }],
      }),
    });
    const snapshots = [];
    const h = harness(cdp, {
      save: async (value) => {
        await atomicWriteJson(path, value);
        snapshots.push(await readFile(path, 'utf8'));
      },
      motionFactory: () => ({
        check() {},
        stop() {
          throw new Error('cleanup apiKey=CLEANUP_SECRET');
        },
      }),
    });
    await assert.rejects(
      executeBench('run', { scenarios: [scenario({ n: 1 }), scenario()] }, h.options),
      (error) => {
        assert.match(error.message, /WebSocket closed/);
        assert.doesNotMatch(error.message, /CLOSE_SECRET/);
        return true;
      },
    );
    for (const text of snapshots) assert.doesNotMatch(text, /(?:CLOSE|STATE|EVENT|CLEANUP)_SECRET/);
    const [result, pending] = JSON.parse(snapshots.at(-1));
    assert.equal(result.complete, false);
    assert.equal(result.cleanupStopped, true);
    assert.match(result.cleanupErrors[0], /\[REDACTED\]/);
    assert.equal(result.lastStatus.session, 's17');
    assert.equal(result.events[0].seq, 7);
    assert.equal(pending.phase, 'failed');
    assert.match(pending.error, /\[REDACTED\]/);
    assert.deepEqual(await readdir(directory), ['new-partial.json']);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('soak snapshots redact process and state failures without changing timed windows or samples', async () => {
  const sample = frame({ seq: 17, session: 's17', clockRttMs: 2.125 });
  const h = harness(
    fakeCdp({
      onCollect: () => [sample],
      onState: () => ({
        metrics: { fps: 60, clockRttMs: 2.125 },
        status: { upstream: 'ws://relay.invalid/?token=UPSTREAM_SECRET&seq=17' },
        events: [],
      }),
    }),
    {
      samplerFactory: () => ({
        roots: { runtime: 100, electron: 200 },
        missing: [],
        sample: async () => {
          throw new Error('process probe Bearer PROCESS_SECRET');
        },
      }),
    },
  );
  await assert.rejects(
    executeBench(
      'soak',
      {
        start: { ...scenario().start, relayToken: 'SOAK_SECRET' },
        seconds: 2,
        intervalMs: 1000,
      },
      h.options,
    ),
    /process coverage incomplete/,
  );
  for (const snapshot of h.snapshots) {
    assert.doesNotMatch(JSON.stringify(snapshot), /(?:SOAK|UPSTREAM|PROCESS)_SECRET/);
  }
  const result = h.snapshots.at(-1);
  assert.equal(result.actualSeconds, 2);
  assert.equal(result.windows[0].windowMs, 2000);
  assert.equal(result.windows[0].e2eP99, 50);
  assert.equal(result.rows[0].clockRttMs, 2.125);
  assert.match(result.rows[0].error, /\[REDACTED\]/);
  assert.match(result.processBaseline.error, /\[REDACTED\]/);
  assert.ok(result.processErrors.every((error) => error.includes('[REDACTED]')));
  assert.deepEqual(result.tailFrames, [sample]);
});

test('CLI eval redacts arbitrary nested JSON stdout without changing the evaluated expression', async (t) => {
  const value = {
    seq: 17,
    session: 's17',
    samples: [frame()],
    nested: [{ token: 'EVAL_SECRET' }],
    reason: 'Bearer REASON_SECRET',
  };
  const h = mockCli(t, () => ({ result: { result: { value } } }));
  const expression = '({token:"INPUT_SECRET"})';
  assert.equal(await main(['eval', expression]), 0);
  assert.equal(h.sockets[0].sent[0].params.expression, expression);
  assert.equal(h.error.mock.calls.length, 0);
  const text = h.log.mock.calls[0].arguments[0];
  assert.doesNotMatch(text, /(?:EVAL|REASON)_SECRET/);
  assert.deepEqual(JSON.parse(text), {
    ...value,
    nested: [{ token: '[REDACTED]' }],
    reason: 'Bearer [REDACTED]',
  });
});

test('CLI run writes only a new sanitized result and sanitizes its success path on stdout', async (t) => {
  const directory = await mkdtemp(
    join(process.env.PULSE_BENCH_TEST_TMP ?? tmpdir(), 'pulse-redact-'),
  );
  try {
    const configPath = join(directory, 'config.json');
    const outputPath = join(directory, 'token=PATH_SECRET.json');
    const config = {
      scenarios: [
        { ...scenario({ n: 1 }), start: { ...scenario().start, relayToken: 'CLI_SECRET' } },
      ],
    };
    await atomicWriteJson(configPath, config);
    const before = await readFile(configPath, 'utf8');
    const cdp = fakeCdp();
    const h = mockCli(t, async (request) => ({
      result: {
        result: {
          value: await cdp.evaluate(request.params.expression, {
            signal: new AbortController().signal,
          }),
        },
      },
    }));
    assert.equal(await main(['run', configPath, outputPath]), 0);
    assert.equal(await readFile(configPath, 'utf8'), before);
    const text = await readFile(outputPath, 'utf8');
    assert.doesNotMatch(text, /CLI_SECRET/);
    assert.equal(JSON.parse(text)[0].complete, true);
    assert.match(cdp.calls[0], /CLI_SECRET/);
    assert.equal(h.error.mock.calls.length, 0);
    assert.match(h.log.mock.calls[0].arguments[0], /run complete/);
    assert.doesNotMatch(h.log.mock.calls[0].arguments[0], /PATH_SECRET/);
    assert.deepEqual((await readdir(directory)).sort(), ['config.json', 'token=PATH_SECRET.json']);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('CLI errors redact CDP exceptions and peer close reasons on stderr', async (t) => {
  let close = false;
  const reason = 'wss://relay.invalid/?%74oken=CLI_CLOSE_SECRET&seq=17 Bearer CLI_ERROR_SECRET';
  const h = mockCli(t, (_, socket) => {
    if (close) socket.onclose({ code: 1006, reason });
    else return { result: { exceptionDetails: { text: reason } } };
  });
  assert.equal(await main(['eval', 'fake']), 1);
  close = true;
  assert.equal(await main(['eval', 'fake']), 1);
  assert.equal(h.log.mock.calls.length, 0);
  assert.equal(h.error.mock.calls.length, 2);
  for (const call of h.error.mock.calls) {
    assert.doesNotMatch(call.arguments[0], /CLI_(?:CLOSE|ERROR)_SECRET/);
    assert.match(call.arguments[0], /seq=17/);
    assert.match(call.arguments[0], /\[REDACTED\]/);
  }
});

test('CLI early failure files and failure-to-save diagnostics both redact credential text', async (t) => {
  const directory = await mkdtemp(
    join(process.env.PULSE_BENCH_TEST_TMP ?? tmpdir(), 'pulse-redact-'),
  );
  const error = t.mock.method(console, 'error', () => {});
  const fetch = t.mock.method(globalThis, 'fetch', () => {
    throw new Error('unexpected network access');
  });
  try {
    const configPath = join(directory, 'token=READ_SECRET.json');
    const outputPath = join(directory, 'new-failure.json');
    assert.equal(await main(['run', configPath, outputPath]), 1);
    const text = await readFile(outputPath, 'utf8');
    assert.doesNotMatch(text, /READ_SECRET/);
    assert.match(JSON.parse(text).error, /ENOENT.*\[REDACTED\]/);
    assert.equal(
      await main(['run', configPath, join(directory, 'missing', 'token=SAVE_SECRET.json')]),
      1,
    );
    assert.equal(fetch.mock.calls.length, 0);
    assert.equal(error.mock.calls.length, 3);
    for (const call of error.mock.calls)
      assert.doesNotMatch(call.arguments[0], /(?:READ|SAVE)_SECRET/);
    assert.match(error.mock.calls[1].arguments[0], /cannot preserve failure result/);
    assert.deepEqual(await readdir(directory), ['new-failure.json']);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('CLI last-resort rejection handler redacts errors before printing', () => {
  const script = `
    process.argv = [process.execPath, ${JSON.stringify(fileURLToPath(new URL('./stream-bench.mjs', import.meta.url)))}, 'eval', 'fake'];
    process.on = () => { throw new Error('Bearer TOP_SECRET wss://relay.invalid/?token=TOP_URL_SECRET&seq=17'); };
    await import(${JSON.stringify(new URL('./stream-bench.mjs', import.meta.url).href)});
  `;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    encoding: 'utf8',
    timeout: 5000,
  });
  assert.equal(result.status, 1);
  assert.equal(result.stdout, '');
  assert.doesNotMatch(result.stderr, /TOP_(?:URL_)?SECRET/);
  assert.match(result.stderr, /stream-bench:.*\[REDACTED\].*seq=17/);
});

test(
  'CDP rejects pending and subsequent calls on peer close and local close',
  { timeout: 2000 },
  async () => {
    for (const local of [false, true]) {
      const h = socketHarness();
      const cdp = await connect(h.options);
      const pending = [cdp.evaluate('pending'), cdp.call('Page.captureScreenshot')];
      const settled = Promise.allSettled(pending);
      await Promise.resolve();
      if (local) cdp.close();
      else h.sockets[0].onclose({ code: 1006, reason: 'lost target' });
      assert.ok(
        (await settled).every(
          (value) => value.status === 'rejected' && /closed/.test(value.reason.message),
        ),
      );
      await assert.rejects(cdp.evaluate('late'), /closed/);
    }
    const h = socketHarness();
    const cdp = await connect(h.options);
    const neverSent = cdp.evaluate('closed before dispatch');
    cdp.close();
    await assert.rejects(neverSent, /closed/);
    assert.equal(h.sockets[0].sent.length, 0);
  },
);

test(
  'CDP discovery, handshake and silent requests have bounded timeouts',
  { timeout: 2000 },
  async () => {
    await assert.rejects(
      connect({ timeoutMs: 10, fetchImpl: () => new Promise(() => {}) }),
      /discovery timed out/,
    );
    const unopened = socketHarness({ open: false });
    await assert.rejects(connect(unopened.options), /connect timed out/);
    const controller = new AbortController();
    class AbortingSocket extends unopened.options.WebSocketImpl {
      constructor(url) {
        super(url);
        controller.abort(new Error('cancelled handshake'));
      }
    }
    await assert.rejects(
      connect({ ...unopened.options, WebSocketImpl: AbortingSocket, signal: controller.signal }),
      /cancelled handshake/,
    );
    const h = socketHarness();
    const cdp = await connect(h.options);
    await assert.rejects(cdp.evaluate('never replies'), /Runtime.evaluate timed out/);
    cdp.close();
  },
);

test('CDP eval exceptions reject, and aborting a call still allows cleanup on the connection', async () => {
  const h = socketHarness();
  const cdp = await connect(h.options);
  const controller = new AbortController();
  const pending = cdp.evaluate('long task', { signal: controller.signal });
  controller.abort(new Error('interrupted'));
  await assert.rejects(pending, /interrupted/);
  assert.equal(h.sockets[0].sent.length, 0, 'an aborted operation must not be dispatched later');
  const cleanup = cdp.evaluate('cleanup');
  await Promise.resolve();
  const id = h.sockets[0].sent.at(-1).id;
  h.sockets[0].onmessage({
    data: JSON.stringify({ id, result: { exceptionDetails: { text: 'bad eval' } } }),
  });
  await assert.rejects(cleanup, /bad eval/);
  cdp.close();
});

test('300 successful S1 samples use small batches and finite replacement attempts', async () => {
  const cdp = fakeCdp({
    batch: (n, index) => ({
      trials: Array.from({ length: n - (index === 1 ? 1 : 0) }, trial),
      misses: index === 1 ? 1 : 0,
      attempts: n,
    }),
  });
  const h = harness(cdp);
  const [result] = await executeBench('run', { scenarios: [scenario({ n: 300 })] }, h.options);
  assert.equal(result.change.successes, 300);
  assert.equal(result.change.attempts, 301);
  assert.equal(result.change.triggeredAttempts, 301);
  assert.equal(result.change.misses, 1);
  assert.equal(result.change.unconfirmed, 0);
  assert.equal(result.complete, true);
  assert.ok(cdp.batches.every((n) => n <= 10));
  assert.ok(h.snapshots.some(([sc]) => sc.change?.successes === 9 && !sc.complete));
  assert.equal(cdp.stopped && cdp.closed, true);
});

test('all misses stop at maxAttempts rather than retrying forever', async () => {
  const cdp = fakeCdp({ batch: (n) => ({ trials: [], misses: n }) });
  const h = harness(cdp);
  await assert.rejects(
    executeBench('run', { scenarios: [scenario({ n: 10, maxAttempts: 12 })] }, h.options),
    /S1 target not met/,
  );
  const result = h.snapshots.at(-1)[0];
  assert.deepEqual(cdp.batches, [10, 2]);
  assert.equal(result.change.attempts, 12);
  assert.equal(result.change.misses, 12);
  assert.equal(result.change.unconfirmed, 0);
  assert.equal(result.complete, false);
  assert.equal(cdp.stopped, true);
});

test('invalid S1 trials count as misses and may only use the remaining retry budget', async () => {
  const cdp = fakeCdp({
    batch: (n, index) => ({
      trials: index === 1 ? [trial(), { sentMs: null, sample: frame() }] : [trial()],
      misses: 0,
    }),
  });
  const h = harness(cdp);
  const [result] = await executeBench(
    'run',
    { scenarios: [scenario({ n: 2, maxAttempts: 3 })] },
    h.options,
  );
  assert.equal(result.change.successes, 2);
  assert.equal(result.change.attempts, 3);
  assert.equal(result.change.misses, 1);
  assert.equal(result.change.invalidTrials.length, 1);
});

test('an aborted renderer batch preserves actual trigger counts without retrying the old session', async () => {
  for (const n of [1, 10]) {
    const cdp = fakeCdp({
      batch: () => ({ trials: [trial()], misses: 0, attempts: 1, aborted: true }),
    });
    const h = harness(cdp);
    await assert.rejects(
      executeBench('run', { scenarios: [scenario({ n })] }, h.options),
      /changeBench aborted/,
    );
    const result = h.snapshots.at(-1)[0];
    assert.equal(cdp.batches.length, 1);
    assert.equal(result.change.attempts, n);
    assert.equal(result.change.triggeredAttempts, 1);
    assert.equal(result.change.successes, 1);
    assert.equal(result.change.unconfirmed, n - 1);
    assert.equal(result.change.complete, false);
    assert.equal(cdp.stopped && cdp.closed, true);
  }
});

test('disconnect preserves earlier batches atomically, including unknown attempts and finally cleanup', async () => {
  const directory = await mkdtemp(
    join(process.env.PULSE_BENCH_TEST_TMP ?? tmpdir(), 'pulse-bench-'),
  );
  const path = join(directory, 'partial.json');
  try {
    let stops = 0;
    const cdp = fakeCdp({
      batch: (n, index) => {
        if (index === 2) throw new Error('CDP WebSocket closed (1006)');
        return { trials: Array.from({ length: n }, trial), misses: 0 };
      },
    });
    const h = harness(cdp, {
      save: (value) => atomicWriteJson(path, value),
      motionFactory: () => ({
        check() {},
        async stop() {
          stops += 1;
        },
      }),
    });
    await assert.rejects(
      executeBench('run', { scenarios: [scenario({ n: 25 })] }, h.options),
      /WebSocket closed/,
    );
    const [result] = JSON.parse(await readFile(path, 'utf8'));
    assert.equal(result.change.trials.length, 10);
    assert.equal(result.change.attempts, 20);
    assert.equal(result.change.misses, 0);
    assert.equal(result.change.unconfirmed, 10);
    assert.equal(result.change.triggeredAttempts, null);
    assert.equal(result.complete, false);
    assert.match(result.error, /WebSocket closed/);
    assert.equal(stops, 1);
    assert.equal(cdp.stopped && cdp.closed, true);
    const cyclic = {};
    cyclic.self = cyclic;
    await assert.rejects(atomicWriteJson(path, cyclic), /circular/i);
    assert.equal(JSON.parse(await readFile(path, 'utf8'))[0].change.trials.length, 10);
    assert.deepEqual(await readdir(directory), ['partial.json']);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test(
  'hung batches and partially failed start both invoke stop and preserve failures',
  { timeout: 2000 },
  async () => {
    for (const cdp of [
      fakeCdp({ batch: () => new Promise(() => {}) }),
      fakeCdp({ failStart: true }),
    ]) {
      const h = harness(cdp);
      await assert.rejects(
        executeBench('run', { scenarios: [scenario({ batchTimeoutMs: 10 })] }, h.options),
        /timed out|start failed/,
      );
      assert.equal(cdp.stopped && cdp.closed, true);
      assert.equal(h.snapshots.at(-1)[0].complete, false);
      assert.ok(h.snapshots.at(-1)[0].error);
    }
  },
);

test('a lost CDP socket retries stop only on the original target and closes the cleanup connection', async () => {
  const recovery = fakeCdp();
  const cdp = fakeCdp({
    batch: (n, index) => {
      if (index === 2) {
        cdp.closed = true;
        throw new Error('CDP disconnected');
      }
      return { trials: Array.from({ length: n }, trial), misses: 0 };
    },
  });
  cdp.targetId = 'owned-target';
  cdp.port = '9444';
  const evaluate = cdp.evaluate.bind(cdp);
  cdp.evaluate = (expression, options) => {
    if (cdp.closed) return Promise.reject(new Error('CDP closed'));
    return evaluate(expression, options);
  };
  let connections = 0;
  const h = harness(cdp, {
    connectCdp: async (options) => {
      if (++connections === 1) return cdp;
      assert.equal(options.targetId, 'owned-target');
      assert.equal(options.port, '9444');
      return recovery;
    },
  });
  await assert.rejects(
    executeBench('run', { scenarios: [scenario({ n: 25 })] }, h.options),
    /disconnected/,
  );
  assert.equal(connections, 2);
  assert.equal(recovery.stopped && recovery.closed, true);
  assert.equal(h.snapshots.at(-1)[0].cleanupStopped, true);
  assert.equal(h.snapshots.at(-1)[0].change.successes, 10);
  assert.equal(h.snapshots.at(-1)[0].complete, false);
});

test('connection failures are saved before any scenario starts', async () => {
  const h = harness(null, {
    connectCdp: async () => {
      throw new Error('no page target');
    },
  });
  await assert.rejects(
    executeBench('run', { scenarios: [scenario()] }, h.options),
    /no page target/,
  );
  assert.equal(h.snapshots.at(-1)[0].phase, 'failed');
  assert.match(h.snapshots.at(-1)[0].error, /not started/);
  assert.equal(h.snapshots.at(-1)[0].change.attempts, 0);
  assert.equal(h.snapshots.at(-1)[0].change.requested, 10);
});

test(
  'a hung stop is bounded and turns an otherwise complete run into a saved failure',
  { timeout: 2000 },
  async () => {
    const cdp = fakeCdp();
    const evaluate = cdp.evaluate.bind(cdp);
    cdp.evaluate = async (expression, options) => {
      const value = await evaluate(expression, options);
      if (expression === 'window.__pulseBench.stop()') return new Promise(() => {});
      return value;
    };
    const h = harness(cdp);
    await assert.rejects(
      executeBench(
        'run',
        { scenarios: [{ ...scenario({ n: 1 }), cleanupTimeoutMs: 10 }] },
        h.options,
      ),
      /stream stop.*timed out/,
    );
    assert.equal(h.snapshots.at(-1)[0].complete, false);
    assert.equal(h.snapshots.at(-1)[0].change.successes, 1);
    assert.equal(cdp.closed, true);
  },
);

test('S2 target counts valid clock-based samples, not just collected frame objects', async () => {
  const cdp = fakeCdp({ onCollect: () => [frame({ devMs: null }), frame({ clockRttMs: null })] });
  const h = harness(cdp);
  const sc = { ...scenario(), change: undefined, frames: 2, maxFrameWaitMs: 5 };
  await assert.rejects(executeBench('run', { scenarios: [sc] }, h.options), /S2 target not met/);
  assert.equal(h.snapshots.at(-1)[0].frameSuccesses, 0);
  assert.ok(h.snapshots.at(-1)[0].frameMisses > 0);
});

const fakeSampler = () => ({
  roots: { runtime: 100, electron: 200 },
  missing: [],
  sample: async () => ({
    ps: [{ pid: 100, group: 'runtime', role: 'runtime', cpu: 10, cpuWindow: 5, rssMb: 10 }],
    missingRoots: [],
  }),
});

test('soak actually advances the requested duration and keeps 10s windows plus a tail', async () => {
  const cdp = fakeCdp();
  const h = harness(cdp, { samplerFactory: fakeSampler });
  const result = await executeBench(
    'soak',
    { start: scenario().start, seconds: 25, intervalMs: 4000 },
    h.options,
  );
  assert.equal(result.actualSeconds, 25);
  assert.deepEqual(
    result.windows.map((w) => w.windowMs),
    [10000, 10000, 5000],
  );
  assert.equal(result.complete, true);
  assert.equal(result.rows[0].fps, 0.25);
  assert.equal(result.rows[0].metricsFps, 60);
  assert.ok(result.windows.every((w) => w.e2eN === w.frames && w.e2eP99 === 50));
  assert.ok(h.snapshots.some((value) => value.windows.length === 1 && !value.complete));
  assert.equal(cdp.stopped && cdp.closed, true);
});

test('soak preserves a failed partial window and stops even if its state call fails', async () => {
  let h;
  const cdp = fakeCdp({
    onState: () => {
      if (h.now() >= 12000) throw new Error('renderer disappeared');
    },
  });
  h = harness(cdp, { samplerFactory: fakeSampler });
  await assert.rejects(
    executeBench('soak', { start: scenario().start, seconds: 25, intervalMs: 4000 }, h.options),
    /renderer disappeared/,
  );
  const result = h.snapshots.at(-1);
  assert.equal(result.rows.length, 4);
  assert.deepEqual(
    result.windows.map((w) => w.windowMs),
    [10000, 2000],
  );
  assert.equal(result.complete, false);
  assert.equal(cdp.stopped && cdp.closed, true);
});

test('interrupt cleanup uses a fresh signal and still flushes the failure result', async () => {
  const controller = new AbortController();
  const cdp = fakeCdp();
  let motionStopped = false;
  const h = harness(cdp, {
    signal: controller.signal,
    samplerFactory: fakeSampler,
    motionFactory: () => ({
      check() {},
      async stop() {
        motionStopped = true;
      },
    }),
    wait: async (ms) => {
      if (ms > 0) {
        controller.abort(new Error('SIGINT test'));
        throw controller.signal.reason;
      }
    },
  });
  await assert.rejects(
    executeBench('soak', { start: scenario().start, seconds: 20 }, h.options),
    /SIGINT test/,
  );
  assert.equal(cdp.stopped && cdp.closed && motionStopped, true);
  assert.ok(
    cdp.calls.lastIndexOf('window.__pulseBench.stop()') <
      cdp.calls.lastIndexOf('window.__pulseBench.collect()'),
  );
  assert.equal(h.snapshots.at(-1).complete, false);
  assert.deepEqual(h.snapshots.at(-1).cleanupErrors, []);
});

test('missing process coverage cannot masquerade as a complete soak but does not shorten it', async () => {
  const h = harness(fakeCdp(), {
    samplerFactory: () => ({
      roots: {},
      missing: ['electron'],
      sample: async () => ({ ps: [], missingRoots: [] }),
    }),
  });
  await assert.rejects(
    executeBench('soak', { start: scenario().start, seconds: 11 }, h.options),
    /process coverage incomplete/,
  );
  assert.equal(h.snapshots.at(-1).actualSeconds, 11);
});

test('motion translates the old swipe loop into bounded commands and rejects arbitrary remote loops', async () => {
  const motion =
    'while true; do input swipe 540 1900 540 500 250; input swipe 540 500 540 1900 250; done';
  assert.equal(compileMotion(motion).steps.length, 2);
  assert.equal(compileMotion(motion).repeat, true);
  assert.throws(() => compileMotion('while true; do arbitrary-command; done'), /unsafe/);
  assert.throws(() => compileMotion('input swipe 0 0 1 1 999999'), /unsafe/);
  const calls = [];
  const controller = new AbortController();
  const runner = startMotion(
    { start: scenario().start, motion },
    {
      signal: controller.signal,
      command: async (file, args) => {
        calls.push([file, args]);
        if (calls.length === 2) controller.abort();
      },
      wait: async (ms, signal) => signal.throwIfAborted(),
    },
  );
  await new Promise((resolve) => setImmediate(resolve));
  await runner.stop();
  assert.equal(calls.length, 2);
  assert.ok(calls.every(([, args]) => args[3] === 'input' && !args.join(' ').includes('while')));
});

test(
  'command timeout and cancellation only signal the spawned process group',
  { timeout: 2000 },
  async () => {
    for (const abort of [false, true]) {
      const child = new EventEmitter();
      child.pid = 43210;
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      const kills = [];
      const controller = new AbortController();
      const running = runCommand('fake-adb', [], {
        timeoutMs: 10,
        killGraceMs: 5,
        signal: controller.signal,
        spawnImpl: () => child,
        kill: (pid, signal) => kills.push([pid, signal]),
      });
      if (abort) controller.abort(new Error('cancelled command'));
      await assert.rejects(running, /timed out|cancelled command/);
      assert.deepEqual(kills, [
        [-43210, 'SIGTERM'],
        [-43210, 'SIGKILL'],
      ]);
    }
  },
);

test('process sampling reads identities globally but details only for specified trees, including renderer/GPU', async () => {
  let time = 0;
  const calls = [];
  const command = async (file, args) => {
    calls.push([file, args]);
    if (args[0] === '-axo')
      return '100 200\n101 100\n200 1\n201 200\n202 200\n300 1\n400 1\n401 400\n';
    assert.equal(args[0], '-p');
    assert.equal(args[1].split(',').includes('400'), false);
    const cpu = time ? '00:02' : '00:01';
    return `100 200 2048 10.0 ${cpu} /pulse-runtime --token SECRET\n101 100 1024 1.0 00:00 /child\n200 1 4096 2.0 00:00 /Electron\n201 200 8192 30.0 ${cpu} /Electron --type=renderer\n202 200 4096 8.0 00:00 /Electron --type=gpu-process\n300 1 1024 1.0 00:00 /python relay\n`;
  };
  const sampler = createProcessSampler(
    { runtimePid: 100, relayPid: 300, start: { mode: 'relay' } },
    { command, now: () => time, env: { PULSE_ELECTRON_PID: '200' } },
  );
  await sampler.sample();
  time = 10000;
  const result = await sampler.sample();
  assert.ok(
    calls.filter(([, args]) => args[0] === '-axo').every(([, args]) => args[1] === 'pid=,ppid='),
  );
  assert.equal(result.ps.find((p) => p.pid === 201).role, 'electron/renderer');
  assert.equal(result.ps.find((p) => p.pid === 202).role, 'electron/gpu-process');
  assert.equal(result.ps.find((p) => p.pid === 100).cpuWindow, 10);
  assert.equal(result.ps.find((p) => p.pid === 101).group, 'runtime');
  assert.equal(JSON.stringify(result).includes('SECRET'), false);
  assert.deepEqual(result.missingRoots, []);
});

test('frame summary excludes null, stale and nonfinite clock values without discarding raw frames', () => {
  const result = frameSummary(
    [
      frame(),
      frame({ clockRttMs: null }),
      frame({ clockStale: true }),
      frame({ drawnMs: NaN }),
      frame({ clockAgeMs: 15001 }),
      frame({ devMs: -1e308, drawnMs: 1e308 }),
      null,
    ],
    10000,
  );
  assert.equal(result.frames, 7);
  assert.equal(result.e2eN, 1);
  assert.equal(result.e2eMisses, 6);
  assert.equal(result.e2eP99, 50);
});

function renderReport(value) {
  const script =
    'import importlib.util, json, sys; spec = importlib.util.spec_from_file_location("p07_report", sys.argv[1]); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print("\\n".join(module.reports(json.loads(sys.stdin.read()))))';
  const args = [
    '-B',
    '-c',
    script,
    fileURLToPath(new URL('../../../runtime/packaging/p07_report.py', import.meta.url)),
  ];
  const python = process.env.PULSE_TEST_PYTHON;
  const result = spawnSync(
    python ?? 'uv',
    python ? args : ['run', '--no-sync', 'python', ...args],
    {
      input: JSON.stringify(value).replaceAll('"__NaN__"', 'NaN'),
      encoding: 'utf8',
      timeout: 5000,
    },
  );
  assert.equal(result.status, 0, result.stderr || String(result.error));
  return result.stdout;
}

test('reports redact legacy credential errors without trimming quantitative tables or changing inputs', () => {
  const input = {
    kind: 'soak',
    complete: false,
    seconds: 2000,
    actualSeconds: 2000,
    error: 'wss://relay.invalid/ws?token=prefix%26REPORT_SECRET',
    processErrors: ['credentials={"value":"CONTAINER_SECRET"}'],
    windows: Array.from({ length: 200 }, (_, i) => ({
      fromSeconds: i * 10,
      toSeconds: (i + 1) * 10,
      frames: 10,
      e2eN: 10,
      e2eMisses: 0,
      fps: 1,
      e2eP50: 50,
      e2eP95: 50,
      e2eP99: 50,
    })),
  };
  const before = structuredClone(input);
  const output = renderReport(input);
  assert.deepEqual(input, before);
  assert.doesNotMatch(output, /(?:REPORT|CONTAINER)_SECRET|\[TRUNCATED\]/);
  assert.match(output, /\[REDACTED\]/);
  assert.match(output, /1990\.0\.\.2000\.0/);
  assert.ok(output.length > 8192);
});

test('report preserves legacy samples, independently counts stages, and never invents relay/encoding splits', () => {
  const output = renderReport([
    {
      name: 'mixed',
      frames: [
        frame(),
        frame({ devMs: null, relayQueueMs: 4, clockRttMs: null }),
        frame({ relayQueueMs: 999, clockRttMs: 3 }),
        frame({ clockStale: true, clockRttMs: 3, relayQueueMs: '__NaN__' }),
        frame({ devMs: 0, relayQueueMs: 0, clockRttMs: 3 }),
        null,
      ],
      metrics: [null, { fps: '__NaN__', kbps: null, clockRttMs: null }],
      change: {
        trials: [
          trial(),
          { sentMs: 0, sample: frame({ devMs: 0 }) },
          { sentMs: null, sample: frame() },
        ],
        misses: 2,
      },
    },
  ]);
  assert.match(output, /Runtime forward \| 5 \| 1/);
  assert.match(output, /S2 PTS->draw submit \| 3 \| 3/);
  assert.match(output, /relay queue \(monotonic\) \| 3 \| 3/);
  assert.match(output, /network estimate\) \| 2 \| 4 \| 13\.0/);
  assert.match(output, /S1 success=2.*attempts=5.*misses=2/);
  assert.match(output, /capture\/encode \+ adb\/USB/);
  assert.match(output, /clock freshness/);
  assert.match(output, /complete=unknown/);
});

test('report handles old soak arrays and new failure metadata with missing/NaN fields', () => {
  const legacy = renderReport([
    {
      t: 10,
      frames: 30,
      fps: null,
      e2eP50: '__NaN__',
      e2eP95: 55,
      ps: [{ pid: 100, rssMb: null, cpu: 1 }],
    },
  ]);
  assert.match(legacy, /0\.0\.\.10\.0 \| 30 \| - \| - \| - \| - \| 55\.0 \| -/);
  const current = renderReport({
    kind: 'soak',
    complete: false,
    error: 'interrupted',
    seconds: 20,
    actualSeconds: 10,
    windows: [
      {
        fromSeconds: 0,
        toSeconds: 10,
        frames: 2,
        e2eN: 1,
        e2eMisses: 1,
        fps: 0.2,
        e2eP50: 50,
        e2eP95: 50,
        e2eP99: 50,
      },
    ],
    rows: [{ ps: [{ role: 'electron/renderer', rssMb: 20, cpu: null, cpuWindow: 5 }] }],
  });
  assert.match(current, /complete=false.*error=interrupted/);
  assert.match(current, /S2 success=1.*misses=1/);
  assert.match(current, /electron\/renderer \| 20\.0 \| 20\.0 \| 20\.0 \| 1 \| 5\.0/);
});

test('bad CLI arguments fail without connecting or executing work', async (t) => {
  t.mock.method(console, 'error', () => {});
  assert.equal(await main(['unknown']), 2);
  assert.equal(await main(['run', 'config.json']), 2);
});
