import { waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { RuntimeApiError, type PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import { SCREEN_FRAME, SCREEN_GEOMETRY } from '../stream/screenFixtures';
import {
  INSPECTOR_CONNECTION,
  INSPECTOR_DEVICE,
  simulatedInspectorObservation,
  simulatedLocateResult,
} from './inspectorFixtures';
import {
  createElementInspectorStore,
  type ElementInspectorStore,
  type InspectorApi,
  type InspectorDeps,
} from './inspectorStore';

const stores: ElementInspectorStore[] = [];
afterEach(() => {
  stores.splice(0).forEach((store) => store.stop());
  vi.useRealTimers();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function setup(status: PulseRuntimeApiSchemas.LocateResult['status'] = 'resolved') {
  let now = 1000;
  let connection = { ...INSPECTOR_CONNECTION };
  const timers = new Set<() => void>();
  const getConnection = vi.fn<InspectorDeps['getConnection']>(async () => connection);
  const api = {
    getAgentGeometry: vi.fn<InspectorApi['getAgentGeometry']>(async (serial, query) => ({
      serial,
      session_id: query.session_id,
      source: 'adb_input_viewport',
      status: 'available',
      atomic_with_observation: false,
      geometry: SCREEN_GEOMETRY,
    })),
    observeAgent: vi.fn<InspectorApi['observeAgent']>(async (serial, body) => ({
      ...simulatedInspectorObservation(body.request_id, SCREEN_GEOMETRY, {
        ocr: body.ocr,
        duplicateText: true,
      }),
      serial,
      session_id: body.session_id,
    })),
    locate: vi.fn<InspectorApi['locate']>(async (_serial, body) =>
      simulatedLocateResult(body, status),
    ),
  };
  const store = createElementInspectorStore({
    getConnection,
    clientFor: () => api,
    id: () => crypto.randomUUID(),
    now: () => now,
    setTimer: (callback) => {
      timers.add(callback);
      return callback;
    },
    clearTimer: (timer) => {
      timers.delete(timer as () => void);
    },
  });
  stores.push(store);
  store.setDevice(INSPECTOR_DEVICE);
  return {
    store,
    api,
    getConnection,
    timers,
    setNow: (value: number) => {
      now = value;
    },
    setConnection: () => {
      connection = { ...connection, port: 9000, token: 'new-synthetic-runtime' };
    },
  };
}

async function validated(store: ElementInspectorStore) {
  await waitFor(() => expect(store.getSnapshot().validation?.status).not.toBe('checking'));
}

test('requires an explicit ready device and exposes no action/control API', async () => {
  const h = setup();
  h.store.setDevice(null);
  await h.store.refresh();
  expect(h.api.observeAgent).not.toHaveBeenCalled();
  h.store.setDevice(INSPECTOR_DEVICE, 'Agent 未 ready');
  await h.store.refresh();
  expect(h.getConnection).not.toHaveBeenCalled();
  expect(h.store.getSnapshot().status).toBe('offline');
  expect(h.store.currentLocator()).toBeNull();
});

test('collects tree and independent pose, explicitly excluding action context and screenshot bytes', async () => {
  const h = setup();
  await h.store.refresh();
  expect(h.api.observeAgent).toHaveBeenCalledWith(
    INSPECTOR_DEVICE.serial,
    expect.objectContaining({
      session_id: INSPECTOR_DEVICE.sessionId,
      tree: true,
      ocr: false,
      screenshot: false,
      display_id: 0,
      for_action: false,
    }),
  );
  expect(h.api.getAgentGeometry).toHaveBeenCalledTimes(2);
  expect(h.store.getSnapshot()).toMatchObject({
    status: 'ready',
    expired: false,
    selectedId: null,
    expectedPackage: 'com.pulse.target',
  });
  expect(h.store.getSnapshot().snapshot?.geometry).toEqual(SCREEN_GEOMETRY);
  expect(h.timers.size).toBe(1);
});

test('selection automatically validates declared candidates, preserving untried rows', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  expect(h.api.locate).toHaveBeenCalledOnce();
  expect(h.api.locate.mock.calls[0]?.[1]).toMatchObject({
    serial: INSPECTOR_DEVICE.serial,
    session_id: INSPECTOR_DEVICE.sessionId,
    expected_package: 'com.pulse.target',
    target: {
      locator: {
        candidates: [{ by: 'res_id' }, { by: 'desc' }, { by: 'text' }, { by: 'xpath' }],
        expect: { unique: true, visible: true },
      },
    },
  });
  expect(h.store.getSnapshot().validation).toMatchObject({ status: 'resolved', matchCount: 1 });
  expect(h.store.getSnapshot().validation?.attempts).toHaveLength(1);
});

test('reorder, disable and visibility changes revalidate the exact declaration without fallback reordering', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  h.store.moveCandidate(0, 1);
  await validated(h.store);
  h.store.toggleCandidate('text:提交订单');
  await validated(h.store);
  h.store.setVisible(false);
  await validated(h.store);
  expect(h.api.locate.mock.calls.at(-1)?.[1].target).toMatchObject({
    locator: {
      candidates: [{ by: 'desc' }, { by: 'res_id' }, { by: 'xpath' }],
      expect: { unique: true, visible: false },
    },
  });
  expect(h.store.currentLocator()?.candidates.map((candidate) => candidate.by)).toEqual([
    'desc',
    'res_id',
    'xpath',
  ]);
});

test.each(['ambiguous', 'not_found', 'indeterminate'] as const)(
  'keeps %s distinct and never promotes it to resolved',
  async (status) => {
    const h = setup(status);
    await h.store.refresh();
    h.store.select('native:1');
    await validated(h.store);
    expect(h.store.getSnapshot().validation?.status).toBe(status);
    expect(h.api.locate).toHaveBeenCalledOnce();
    expect(h.store.getSnapshot().validation?.attempts).toHaveLength(1);
    expect(h.store.currentLocator()?.expect.unique).toBe(true);
  },
);

test('a resolved but incomplete query does not claim proven uniqueness', async () => {
  const h = setup();
  h.api.locate.mockImplementation(async (_serial, body) => ({
    ...simulatedLocateResult(body),
    query_complete: false,
  }));
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  expect(h.store.getSnapshot().validation).toMatchObject({
    status: 'indeterminate',
    errorCode: 'query_incomplete',
  });
});

test('rejects foreign receipts and does not display Runtime error bodies or credentials', async () => {
  const h = setup();
  h.api.locate.mockImplementationOnce(async (_serial, body) => ({
    ...simulatedLocateResult(body),
    serial: 'FOREIGN',
  }));
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  expect(h.store.getSnapshot().validation?.status).toBe('indeterminate');
  expect(h.store.getSnapshot().error).toMatch(/回执不匹配/);
  h.api.locate.mockRejectedValueOnce(new RuntimeApiError(409, { token: 'private-token' }));
  await h.store.validate();
  expect(h.store.getSnapshot().error).toMatch(/HTTP 409/);
  expect(h.store.getSnapshot().error).not.toContain('private-token');
});

test('expires snapshots and cancels hover/verification without deleting provenance', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  h.store.hover('native:0');
  for (const timer of [...h.timers]) timer();
  expect(h.store.getSnapshot()).toMatchObject({ expired: true, hoveredId: null, validation: null });
  expect(h.store.getSnapshot().snapshot?.elements.length).toBeGreaterThan(0);
  expect(h.store.currentLocator()).toBeNull();
  expect(h.store.isFresh()).toBe(false);
});

test('rechecks age even when a background tab delays expiry timers', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  h.setNow(16_000);
  expect(h.store.currentLocator()).toBeNull();
  expect(h.store.getSnapshot().expired).toBe(true);
  expect(h.timers.size).toBe(0);
});

test('a source option change invalidates old evidence; OCR is explicit and never requests screenshot output', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.setIncludeOcr(true);
  expect(h.store.getSnapshot().expired).toBe(true);
  await h.store.refresh();
  expect(h.api.observeAgent.mock.calls.at(-1)?.[1]).toMatchObject({
    ocr: true,
    screenshot: false,
    for_action: false,
  });
  h.store.select('ocr:0');
  await validated(h.store);
  expect(h.store.currentLocator()?.candidates).toEqual([{ by: 'ocr_text', value: '总计 42.00' }]);
});

test('page and pose changes during collection cannot produce a fresh generation target', async () => {
  const h = setup();
  h.api.getAgentGeometry.mockImplementationOnce(async (serial, query) => ({
    serial,
    session_id: query.session_id,
    source: 'adb_input_viewport',
    status: 'available',
    atomic_with_observation: false,
    geometry: { ...SCREEN_GEOMETRY, rotation: 90 },
  }));
  await h.store.refresh();
  expect(h.store.getSnapshot().expired).toBe(true);
  expect(h.store.getSnapshot().snapshot?.geometry).toBeNull();
  h.api.observeAgent.mockImplementation(async (serial, body) => ({
    ...simulatedInspectorObservation(body.request_id),
    serial,
    revision_end: 43,
  }));
  await h.store.refresh();
  expect(h.store.getSnapshot().snapshot?.changedDuringCapture).toBe(true);
  expect(h.store.getSnapshot().expired).toBe(true);
});

test('unavailable geometry does not erase native properties or pretend to prove coordinate mapping', async () => {
  const h = setup();
  h.api.getAgentGeometry.mockRejectedValue(new Error('synthetic geometry unavailable'));
  await h.store.refresh();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'ready', expired: false });
  expect(h.store.getSnapshot().snapshot?.geometry).toBeNull();
  expect(h.store.getSnapshot().snapshot?.elements.length).toBeGreaterThan(0);
});

test('same-space frame sequence updates do not invalidate or publish a new snapshot; config changes do', async () => {
  const h = setup();
  h.store.setFrameSpace(SCREEN_FRAME, INSPECTOR_CONNECTION);
  await h.store.refresh();
  const snapshot = h.store.getSnapshot();
  const listener = vi.fn();
  const unsubscribe = h.store.subscribe(listener);
  h.store.setFrameSpace({ ...SCREEN_FRAME, seq: 22 }, INSPECTOR_CONNECTION);
  expect(h.store.getSnapshot()).toBe(snapshot);
  expect(listener).not.toHaveBeenCalled();
  h.store.setFrameSpace({ ...SCREEN_FRAME, configId: 2 }, INSPECTOR_CONNECTION);
  expect(h.store.getSnapshot().expired).toBe(true);
  unsubscribe();
});

test('device switch cancels a pending observation and rejects its late result', async () => {
  const h = setup();
  const entered = deferred<PulseRuntimeApiSchemas.AgentObserveBody>();
  const response = deferred<unknown>();
  h.api.observeAgent.mockImplementationOnce((_serial, body) => {
    entered.resolve(body);
    return response.promise;
  });
  const work = h.store.refresh();
  const body = await entered.promise;
  h.store.setDevice({ serial: 'SIM-ANDROID-B', sessionId: INSPECTOR_DEVICE.sessionId });
  response.resolve(simulatedInspectorObservation(body.request_id));
  await work;
  expect(h.store.getSnapshot()).toMatchObject({
    device: { serial: 'SIM-ANDROID-B' },
    snapshot: null,
    selectedId: null,
    status: 'idle',
  });
  await h.store.refresh();
  expect(h.store.getSnapshot().status).toBe('ready');
});

test('a Runtime swap while collecting is not accepted as current device evidence', async () => {
  const h = setup();
  h.api.observeAgent.mockImplementation(async (_serial, body) => {
    h.setConnection();
    return simulatedInspectorObservation(body.request_id);
  });
  await h.store.refresh();
  expect(h.store.getSnapshot()).toMatchObject({ status: 'error', snapshot: null, expired: true });
  expect(h.store.getSnapshot().error).toMatch(/Runtime 已更换/);
});

test('offline retains read-only provenance, while a session change removes old element identities', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.setDevice(INSPECTOR_DEVICE, '设备已断开');
  expect(h.store.getSnapshot()).toMatchObject({ status: 'offline', expired: true });
  expect(h.store.getSnapshot().snapshot).not.toBeNull();
  h.store.setDevice({ ...INSPECTOR_DEVICE, sessionId: '00000000-0000-4000-8000-000000000009' });
  expect(h.store.getSnapshot().snapshot).toBeNull();
});

test('changing selected element discards late uniqueness results for the old selection', async () => {
  const h = setup();
  const entered = deferred<PulseRuntimeApiSchemas.LocateRequest>();
  const response = deferred<PulseRuntimeApiSchemas.LocateResult>();
  h.api.locate.mockImplementationOnce((_serial, body) => {
    entered.resolve(body);
    return response.promise;
  });
  await h.store.refresh();
  h.store.select('native:0');
  const body = await entered.promise;
  h.store.select('native:1');
  await validated(h.store);
  const current = h.store.getSnapshot().validation;
  response.resolve(simulatedLocateResult(body, 'ambiguous'));
  await Promise.resolve();
  expect(h.store.getSnapshot().selectedId).toBe('native:1');
  expect(h.store.getSnapshot().validation).toBe(current);
});

test('requires an explicit valid target application instead of inferring it from OCR', async () => {
  const h = setup();
  await h.store.refresh();
  h.store.select('native:1');
  await validated(h.store);
  h.api.locate.mockClear();
  h.store.setExpectedPackage('not-a-package');
  await h.store.validate();
  expect(h.api.locate).not.toHaveBeenCalled();
  expect(h.store.getSnapshot().validation?.errorCode).toBe('target_app_unverified');
});

test('Runtime identity invalidation expires a snapshot even without a running screen stream', async () => {
  const h = setup();
  await h.store.refresh();
  h.setConnection();
  h.store.select('native:1');
  await waitFor(() => expect(h.store.getSnapshot().expired).toBe(true));
  expect(h.store.getSnapshot().error).toMatch(/Runtime 不一致/);
  expect(h.store.isFresh()).toBe(false);
  expect(h.store.currentLocator()).toBeNull();
  expect(h.api.locate).not.toHaveBeenCalled();
});

test('an unconfirmed Runtime connection expires provenance instead of leaving authoring enabled', async () => {
  const h = setup();
  await h.store.refresh();
  h.getConnection.mockResolvedValueOnce(null);
  h.store.select('native:1');
  await waitFor(() => expect(h.store.getSnapshot().expired).toBe(true));
  expect(h.store.currentLocator()).toBeNull();
  expect(h.store.getSnapshot().snapshot).not.toBeNull();
});

test('Runtime replacement after locate also expires the authoring snapshot', async () => {
  const h = setup();
  await h.store.refresh();
  h.api.locate.mockImplementation(async (_serial, body) => {
    h.setConnection();
    return simulatedLocateResult(body);
  });
  h.store.select('native:1');
  await waitFor(() => expect(h.store.getSnapshot().expired).toBe(true));
  expect(h.store.getSnapshot().error).toMatch(/Runtime 已更换/);
  expect(h.store.currentLocator()).toBeNull();
});

test('a never-settling observation is bounded and stopping cannot leave a phantom loading state', async () => {
  vi.useFakeTimers();
  const h = setup();
  const entered = deferred<boolean>();
  h.api.observeAgent.mockImplementation(() => {
    entered.resolve(true);
    return new Promise(() => undefined);
  });
  const work = h.store.refresh();
  await entered.promise;
  await vi.advanceTimersByTimeAsync(20_001);
  await work;
  expect(h.store.getSnapshot().error).toMatch(/采集超时/);
  expect(h.store.getSnapshot().status).toBe('error');
  h.store.stop();
  expect(h.store.getSnapshot().status).toBe('idle');
});
