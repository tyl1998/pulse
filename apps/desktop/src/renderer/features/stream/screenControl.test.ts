import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import type { RuntimeConnection } from '../../../shared/ipc';
import {
  createScreenControl,
  defaultScreenControlDeps,
  viewRotation,
  type DisplayGeometry,
  type ScreenActionBody,
  type ScreenControlApi,
  type ScreenControlDeps,
} from './screenControl';
import {
  SCREEN_FRAME,
  SCREEN_GEOMETRY,
  SCREEN_SERIAL,
  SCREEN_SESSION,
  simulatedActionReceipt,
  simulatedNativeObservation,
  simulatedScreenControl,
} from './screenFixtures';

const connection: RuntimeConnection = {
  port: 4311,
  token: 'synthetic-test-only',
  pid: 9,
  version: '1.0.0',
};
const stops: (() => void)[] = [];

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function controlHarness() {
  let control = simulatedScreenControl();
  let geometry = { ...SCREEN_GEOMETRY };
  let runtime = connection;
  let now = 1000;
  let nextId = 0;
  const api = {
    getControl: vi.fn<ScreenControlApi['getControl']>(async () => ({
      serial: SCREEN_SERIAL,
      session_id: SCREEN_SESSION,
      control,
    })),
    getAgentGeometry: vi.fn<ScreenControlApi['getAgentGeometry']>(async () => ({
      serial: SCREEN_SERIAL,
      session_id: SCREEN_SESSION,
      status: 'available',
      source: 'adb_input_viewport',
      geometry,
      atomic_with_observation: false,
    })),
    acquireControl: vi.fn<ScreenControlApi['acquireControl']>(async (_serial, body) => {
      control = simulatedScreenControl(body.run_id ?? null, control.epoch);
      return { serial: SCREEN_SERIAL, control };
    }),
    releaseControl: vi.fn<ScreenControlApi['releaseControl']>(async () => {
      control = simulatedScreenControl(null, control.epoch);
      return { serial: SCREEN_SERIAL, control };
    }),
    observeAgent: vi.fn<ScreenControlApi['observeAgent']>(async (_serial, body) =>
      simulatedNativeObservation(body.request_id, geometry),
    ),
    performAction: vi.fn<ScreenControlApi['performAction']>(async (_serial, body) =>
      simulatedActionReceipt(body),
    ),
  };
  const clientFor = vi.fn<ScreenControlDeps['clientFor']>(() => api);
  const deps: ScreenControlDeps = {
    getConnection: async () => runtime,
    clientFor,
    id: () => `00000000-0000-4000-8000-${(++nextId).toString(16).padStart(12, '0')}`,
    now: () => now,
    setTimer: (callback, ms) => setTimeout(callback, ms),
    clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
  };
  const controller = createScreenControl(
    { serial: SCREEN_SERIAL, sessionId: SCREEN_SESSION },
    deps,
  );
  const stop = controller.start();
  stops.push(stop);
  return {
    api,
    deps,
    clientFor,
    controller,
    stop,
    setControl: (value: ReturnType<typeof simulatedScreenControl>) => {
      control = value;
    },
    setGeometry: (value: DisplayGeometry) => {
      geometry = value;
    },
    setRuntime: (value: RuntimeConnection) => {
      runtime = value;
    },
    setNow: (value: number) => {
      now = value;
    },
    ready: () => controller.refresh(),
    operate: async () => {
      await controller.refresh();
      await controller.acquire();
      controller.setFrame(SCREEN_FRAME, connection);
      controller.setEnabled(true);
    },
    tap: () =>
      controller.gesture({
        frame: SCREEN_FRAME,
        start: { x: 360, y: 800 },
        end: { x: 360, y: 800 },
        rotation: 0,
        swipe: false,
        durationMs: 80,
      }),
  };
}

beforeEach(() => vi.useFakeTimers());
afterEach(async () => {
  for (const stop of stops.splice(0)) stop();
  await vi.advanceTimersByTimeAsync(0);
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('local screen control', () => {
  test('polls pose without consuming native observations and never acquires automatically', async () => {
    const h = controlHarness();
    await h.ready();
    expect(h.controller.getSnapshot()).toMatchObject({
      status: 'ready',
      owned: false,
      geometry: SCREEN_GEOMETRY,
    });
    expect(h.api.getAgentGeometry).toHaveBeenCalledWith(SCREEN_SERIAL, {
      session_id: SCREEN_SESSION,
    });
    expect(h.api.observeAgent).not.toHaveBeenCalled();
    expect(h.api.acquireControl).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(6000);
    expect(h.api.getAgentGeometry).toHaveBeenCalledTimes(4);
    expect(h.api.observeAgent).not.toHaveBeenCalled();
    expect(h.controller.operationReason()).toContain('尚未取得控制权');
  });

  test('does not adopt a different manual window run and does not take over', async () => {
    const h = controlHarness();
    h.setControl(simulatedScreenControl('99999999-0000-4000-8000-000000000001'));
    await h.ready();
    await h.controller.acquire();
    h.controller.setFrame(SCREEN_FRAME, connection);
    h.controller.setEnabled(true);
    expect(h.controller.operationReason()).toContain('其他控制会话');
    expect(await h.tap()).toBe(false);
    expect(h.api.acquireControl).not.toHaveBeenCalled();
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('ownership changes only after an exact acquisition receipt', async () => {
    const h = controlHarness();
    await h.ready();
    const done = deferred<unknown>();
    const entered = deferred<PulseAcquire>();
    type PulseAcquire = Parameters<ScreenControlApi['acquireControl']>[1];
    h.api.acquireControl.mockImplementation((_serial, body) => {
      entered.resolve(body);
      return done.promise;
    });
    const work = h.controller.acquire();
    const body = await entered.promise;
    expect(h.controller.getSnapshot()).toMatchObject({ busy: 'acquire', owned: false });
    const acquired = simulatedScreenControl(body.run_id ?? null);
    h.setControl(acquired);
    done.resolve({ serial: SCREEN_SERIAL, control: acquired });
    await work;
    expect(h.controller.getSnapshot()).toMatchObject({
      busy: null,
      owned: true,
      control: { run_id: body.run_id, epoch: 7 },
    });
  });

  test('normal release confirms its exact run and the next acquisition uses a new UUID', async () => {
    const h = controlHarness();
    await h.operate();
    const firstId = h.controller.getSnapshot().control?.run_id;
    await h.controller.release();
    expect(h.api.releaseControl).toHaveBeenCalledWith(SCREEN_SERIAL, {
      session_id: SCREEN_SESSION,
      control_epoch: 7,
      run_id: firstId,
    });
    expect(h.controller.getSnapshot().owned).toBe(false);
    await h.controller.acquire();
    expect(h.controller.getSnapshot().control?.run_id).not.toBe(firstId);
  });

  test('a tap binds the fresh native hit stack, current session, own run and epoch', async () => {
    const h = controlHarness();
    await h.operate();
    expect(await h.tap()).toBe(true);
    const body = h.api.performAction.mock.calls[0]?.[1];
    expect(body).toMatchObject({
      serial: SCREEN_SERIAL,
      session_id: SCREEN_SESSION,
      kind: 'tap',
      params: { x: 540, y: 1200, duration_ms: 80 },
      source: 'manual',
      run_id: h.controller.getSnapshot().control?.run_id,
      control_epoch: 7,
      target: {
        source: 'tree',
        coordinate_space: 'display_pixels',
        display_id: 0,
        expected_package: 'com.pulse.target',
        node_path: [1],
      },
    });
    expect(body?.target.observation_id).toBe(h.api.observeAgent.mock.calls[0]?.[1].request_id);
    expect(
      new Set([body?.request_id, body?.operation_id, body?.target.observation_id, body?.run_id])
        .size,
    ).toBe(4);
    expect(h.controller.getSnapshot().notice).toContain('未评估业务结果');
  });

  test.each([0, 90, 180, 270] as const)(
    'maps locked natural pixels through a %s degree input viewport',
    async (rotation) => {
      const h = controlHarness();
      const sideways = rotation === 90 || rotation === 270;
      const geometry: DisplayGeometry = {
        display_id: 0,
        width: sideways ? 2400 : 1080,
        height: sideways ? 1080 : 2400,
        rotation,
      };
      h.setGeometry(geometry);
      await h.operate();
      expect(
        await h.controller.gesture({
          frame: SCREEN_FRAME,
          start: { x: 180, y: 400 },
          end: { x: 180, y: 400 },
          rotation: viewRotation(geometry),
          swipe: false,
          durationMs: 80,
        }),
      ).toBe(true);
      const mapped = h.api.performAction.mock.calls[0]?.[1].params;
      expect(mapped).toMatchObject(
        rotation === 0
          ? { x: 270, y: 600 }
          : rotation === 90
            ? { x: 600, y: 809 }
            : rotation === 180
              ? { x: 809, y: 1799 }
              : { x: 1799, y: 270 },
      );
    },
  );

  test('a swipe stays in a common native node and respects the 500..1000ms device budget', async () => {
    const h = controlHarness();
    await h.operate();
    expect(
      await h.controller.gesture({
        frame: SCREEN_FRAME,
        start: { x: 300, y: 750 },
        end: { x: 400, y: 850 },
        rotation: 0,
        swipe: true,
        durationMs: 1700,
      }),
    ).toBe(true);
    expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
      kind: 'swipe',
      params: { x: 450, y: 1125, end_x: 600, end_y: 1275, duration_ms: 1000 },
      target: { node_path: [1] },
    });
  });

  test('rotation or config changes before dispatch cancel a gesture rather than guessing', async () => {
    const h = controlHarness();
    await h.operate();
    h.setGeometry({ display_id: 0, width: 2400, height: 1080, rotation: 90 });
    expect(await h.tap()).toBe(false);
    expect(h.controller.getSnapshot().error).toContain('方向或尺寸已变化');
    expect(h.api.performAction).not.toHaveBeenCalled();
    h.controller.setFrame({ ...SCREEN_FRAME, configId: 2 }, connection);
    expect(await h.tap()).toBe(false);
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('overlapping siblings remain ambiguous even when one has smaller bounds', async () => {
    const h = controlHarness();
    await h.operate();
    h.api.observeAgent.mockImplementation(async (_serial, body) => {
      const observed = simulatedNativeObservation(body.request_id);
      const node = observed.sources.tree.data.nodes[2];
      if (!node) throw new Error('missing node');
      observed.sources.tree.data.nodes.push({
        ...node,
        node_path: [2],
        bounds: { left: 400, top: 1100, right: 700, bottom: 1300 },
      });
      return observed;
    });
    expect(await h.tap()).toBe(false);
    expect(h.controller.getSnapshot().error).toContain('重叠的原生节点');
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('tree unavailable cannot be replaced by otherwise available geometry', async () => {
    const h = controlHarness();
    await h.operate();
    h.api.observeAgent.mockImplementation(async (_serial, body) => ({
      ...simulatedNativeObservation(body.request_id),
      success: false,
      sources: { tree: { status: 'unavailable', data: null } },
    }));
    expect(await h.tap()).toBe(false);
    expect(h.controller.getSnapshot().error).toContain('原生树不可用');
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test.each(['node_limit', 'page_changed_during_capture', 'text_redaction_unverified'])(
    'blocks genuinely unsafe completeness reason %s but preserves normal advisory reasons',
    async (reason) => {
      const h = controlHarness();
      await h.operate();
      h.api.observeAgent.mockImplementation(async (_serial, body) => {
        const observed = simulatedNativeObservation(body.request_id);
        observed.sources.tree.completeness.reasons.push(reason);
        return observed;
      });
      expect(await h.tap()).toBe(false);
      expect(h.api.performAction).not.toHaveBeenCalled();
    },
  );

  test.each(['disabled-sibling', 'disabled-child', 'unknown-enabled', 'unknown-visible'] as const)(
    'never clicks through a %s native hit',
    async (condition) => {
      const h = controlHarness();
      await h.operate();
      h.api.observeAgent.mockImplementation(async (_serial, body) => {
        const observed = simulatedNativeObservation(body.request_id);
        const button = observed.sources.tree.data.nodes[2];
        if (!button) throw new Error('missing button');
        const blocked = {
          ...button,
          node_path: condition === 'disabled-child' ? button.node_path : [2],
          states: {
            ...button.states,
            enabled: condition === 'unknown-enabled' ? null : false,
            visible: condition === 'unknown-visible' ? null : true,
          },
        };
        const nodes =
          condition === 'disabled-child'
            ? [...observed.sources.tree.data.nodes.slice(0, 2), blocked]
            : [...observed.sources.tree.data.nodes, blocked];
        return {
          ...observed,
          sources: {
            tree: { ...observed.sources.tree, data: { ...observed.sources.tree.data, nodes } },
          },
        };
      });
      expect(await h.tap()).toBe(false);
      expect(h.api.performAction).not.toHaveBeenCalled();
    },
  );

  test('global closed keys do not invent an editable-node fingerprint requirement', async () => {
    const h = controlHarness();
    await h.operate();
    h.api.observeAgent.mockImplementation(async (_serial, body) => {
      const observed = simulatedNativeObservation(body.request_id);
      observed.revision_end += 1;
      return observed;
    });
    expect(await h.controller.key('BACK')).toBe(true);
    expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
      kind: 'key',
      params: { code: 4 },
    });
  });

  test('text replacement uses the unique focused editable native node and preserves Chinese, paste and supplementary characters', async () => {
    const h = controlHarness();
    await h.operate();
    expect(await h.controller.input('中文 paste \n\u{20bb7}')).toBe(true);
    expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
      kind: 'set_text',
      target: { node_path: [0] },
      params: { text: '中文 paste \n\u{20bb7}' },
    });
    expect(await h.controller.input('')).toBe(true);
    expect(h.api.performAction.mock.calls[1]?.[1].params).toEqual({ text: '' });
  });

  test.each(['unfocused', 'disabled', 'sensitive', 'multiple'] as const)(
    'does not write to a %s text target',
    async (condition) => {
      const h = controlHarness();
      await h.operate();
      h.api.observeAgent.mockImplementation(async (_serial, body) => {
        const observed = simulatedNativeObservation(body.request_id);
        const field = observed.sources.tree.data.nodes[1];
        if (!field) throw new Error('missing field');
        if (condition === 'unfocused') field.states.focused = false;
        if (condition === 'disabled') field.states.enabled = false;
        if (condition === 'sensitive') field.text_redacted = true;
        if (condition === 'multiple')
          observed.sources.tree.data.nodes.push({ ...field, node_path: [3] });
        return observed;
      });
      expect(await h.controller.input('must not write')).toBe(false);
      expect(h.api.performAction).not.toHaveBeenCalled();
    },
  );

  test('oversized or unpaired-surrogate text is rejected without a device request', async () => {
    const h = controlHarness();
    await h.operate();
    expect(await h.controller.input('a'.repeat(4097))).toBe(false);
    expect(await h.controller.input('\ud800')).toBe(false);
    expect(h.api.observeAgent).not.toHaveBeenCalled();
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test.each([
    ['BACK', 4],
    ['HOME', 3],
    ['ENTER', 66],
    ['BACKSPACE', 67],
  ] as const)('maps %s only to the Runtime closed key set', async (key, code) => {
    const h = controlHarness();
    await h.operate();
    expect(await h.controller.key(key)).toBe(true);
    expect(h.api.performAction.mock.calls[0]?.[1]).toMatchObject({
      kind: 'key',
      params: { code },
      target: { node_path: [] },
      source: 'manual',
    });
    expect(await h.controller.key('ANY' as 'BACK')).toBe(false);
    expect(h.api.performAction).toHaveBeenCalledTimes(1);
  });

  test.each(['rejected', 'failed', 'uncertain'])(
    'HTTP-200 %s is not a confirmed action and is never replayed',
    async (status) => {
      const h = controlHarness();
      await h.operate();
      h.api.performAction.mockImplementation(async (_serial, body) =>
        simulatedActionReceipt(body, status),
      );
      expect(await h.controller.input('pending text')).toBe(false);
      expect(h.controller.getSnapshot()).toMatchObject({
        owned: false,
        notice: null,
        status: 'unavailable',
      });
      expect(h.controller.getSnapshot().error).toContain('不会');
      await vi.advanceTimersByTimeAsync(6000);
      expect(h.api.performAction).toHaveBeenCalledTimes(1);
    },
  );

  test('one outstanding action rejects repeated keys and still awaits its receipt after leaving operation mode', async () => {
    const h = controlHarness();
    await h.operate();
    const entered = deferred<ScreenActionBody>();
    const done = deferred<unknown>();
    h.api.performAction.mockImplementation((_serial, body) => {
      entered.resolve(body);
      return done.promise;
    });
    const action = h.controller.key('BACK');
    const body = await entered.promise;
    expect(await h.controller.key('BACK')).toBe(false);
    h.controller.setEnabled(false);
    const signal = h.clientFor.mock.calls.at(-1)?.[1];
    expect(signal?.aborted).toBe(false);
    done.resolve(simulatedActionReceipt(body));
    expect(await action).toBe(true);
    expect(h.api.performAction).toHaveBeenCalledTimes(1);
  });

  test('a fresh foreign epoch or Runtime restart blocks an old action before observation', async () => {
    const h = controlHarness();
    await h.operate();
    h.setControl(simulatedScreenControl(h.controller.getSnapshot().control?.run_id, 8));
    expect(await h.tap()).toBe(false);
    expect(h.controller.getSnapshot().owned).toBe(false);
    expect(h.api.observeAgent).not.toHaveBeenCalled();
    h.setRuntime({ ...connection, port: 4312, pid: 10, token: 'new-synthetic-runtime' });
    expect(await h.tap()).toBe(false);
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('unknown capture profile, frame size, pose age and stream Runtime fail closed', async () => {
    const h = controlHarness();
    await h.operate();
    h.controller.setFrame({ ...SCREEN_FRAME, captureOrientation: null }, connection);
    expect(h.controller.operationReason()).toContain('采集未确认');
    h.controller.setFrame({ ...SCREEN_FRAME, width: 1600, height: 720 }, connection);
    expect(h.controller.operationReason()).toContain('尺寸不匹配');
    h.controller.setFrame(SCREEN_FRAME, { ...connection, pid: 99 });
    expect(h.controller.operationReason()).toContain('不同 Runtime');
    h.controller.setFrame(SCREEN_FRAME, connection);
    h.setNow(6001);
    expect(h.controller.operationReason()).toContain('过期');
    expect(await h.tap()).toBe(false);
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('a late acquisition after unmount cleans only the old issuing Runtime run', async () => {
    const h = controlHarness();
    await h.ready();
    const entered = deferred<Parameters<ScreenControlApi['acquireControl']>[1]>();
    const done = deferred<unknown>();
    h.api.acquireControl.mockImplementation((_serial, body) => {
      entered.resolve(body);
      return done.promise;
    });
    const work = h.controller.acquire();
    const body = await entered.promise;
    h.stop();
    h.setRuntime({ ...connection, pid: 10, port: 4312 });
    const acquired = simulatedScreenControl(body.run_id ?? null);
    h.setControl(acquired);
    done.resolve({ serial: SCREEN_SERIAL, control: acquired });
    await work;
    await vi.advanceTimersByTimeAsync(0);
    expect(h.api.releaseControl).toHaveBeenCalledWith(SCREEN_SERIAL, {
      session_id: SCREEN_SESSION,
      control_epoch: 7,
      run_id: body.run_id,
      wait_for_idle_ms: 60_000,
    });
    expect(
      h.clientFor.mock.calls
        .filter((call) => call[2] === true)
        .every((call) => call[0] === connection),
    ).toBe(true);
    expect(h.controller.getSnapshot().owned).toBe(false);
  });

  test('a hung pose read has a deadline and retains no operation permission', async () => {
    const h = controlHarness();
    h.api.getAgentGeometry.mockImplementation(() => new Promise(() => undefined));
    const work = h.ready();
    await vi.advanceTimersByTimeAsync(5000);
    await work;
    expect(h.controller.getSnapshot()).toMatchObject({
      status: 'unavailable',
      owned: false,
      geometry: null,
    });
    expect(h.api.performAction).not.toHaveBeenCalled();
  });

  test('the action adapter carries only the loopback bearer-bound strict JSON body', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () =>
        new Response(JSON.stringify({ status: 'rejected' }), {
          headers: { 'content-type': 'application/json' },
        }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const abort = new AbortController();
    const api = defaultScreenControlDeps.clientFor(connection, abort.signal);
    const body = { serial: 'S/1', params: { text: 'payload' } } as unknown as ScreenActionBody;
    await api.performAction('S/1', body);
    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:4311/devices/S%2F1/agent/action',
      expect.objectContaining({
        method: 'POST',
        keepalive: true,
        headers: {
          Authorization: 'Bearer synthetic-test-only',
          'Content-Type': 'application/json',
        },
        signal: abort.signal,
        body: JSON.stringify(body),
      }),
    );
  });
});
