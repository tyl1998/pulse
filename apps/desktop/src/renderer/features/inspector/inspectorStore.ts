import type { Locator } from '@pulse/schema';
import type { RuntimeConnection } from '../../../shared/ipc';
import { RuntimeApiError, type PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import { createRuntimeClient } from '../../shared/api/runtimeClient';
import { withTimeout } from '../../shared/stream/timeout';
import type { PresentedFrame } from '../../shared/stream/workerProtocol';
import {
  generateLocatorCandidates,
  INSPECTION_MAX_AGE_MS,
  inspectorLocator,
  PACKAGE_NAME,
  parseInspection,
  parseInspectionGeometry,
  sameInspectionGeometry,
  type InspectionDevice,
  type InspectionSnapshot,
  type InspectorCandidate,
} from './inspectionModel';

type LocateResult = PulseRuntimeApiSchemas.LocateResult;

export interface LocatorValidation {
  status: LocateResult['status'] | 'checking';
  matchCount: number | null;
  errorCode: string | null;
  observationId: string | null;
  attempts: {
    key: string;
    status: LocateResult['status'];
    matchCount: number;
    errorCode: string | null;
  }[];
}

export interface InspectorState {
  device: InspectionDevice | null;
  blockedReason: string | null;
  status: 'idle' | 'loading' | 'ready' | 'offline' | 'error';
  snapshot: InspectionSnapshot | null;
  selectedId: string | null;
  hoveredId: string | null;
  candidates: InspectorCandidate[];
  includeOcr: boolean;
  expectedPackage: string;
  visible: boolean;
  expired: boolean;
  expiredReason: string | null;
  validation: LocatorValidation | null;
  error: string | null;
}

export interface InspectorApi {
  observeAgent(serial: string, body: PulseRuntimeApiSchemas.AgentObserveBody): Promise<unknown>;
  getAgentGeometry(serial: string, query: { session_id: string }): Promise<unknown>;
  locate(serial: string, body: PulseRuntimeApiSchemas.LocateRequest): Promise<LocateResult>;
}

export interface InspectorDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  clientFor(connection: RuntimeConnection, signal: AbortSignal): InspectorApi;
  id(): string;
  now(): number;
  setTimer(callback: () => void, ms: number): unknown;
  clearTimer(timer: unknown): void;
}

export interface ElementInspectorStore {
  getSnapshot(): InspectorState;
  subscribe(listener: () => void): () => void;
  setDevice(device: InspectionDevice | null, blockedReason?: string | null): void;
  setFrameSpace(frame: PresentedFrame | null, connection: RuntimeConnection | null): void;
  refresh(): Promise<void>;
  isFresh(): boolean;
  currentLocator(): Locator | null;
  select(id: string | null): void;
  hover(id: string | null): void;
  moveCandidate(index: number, direction: -1 | 1): void;
  toggleCandidate(key: string): void;
  setVisible(visible: boolean): void;
  setExpectedPackage(packageName: string): void;
  setIncludeOcr(include: boolean): void;
  validate(): Promise<void>;
  invalidate(reason: string): void;
  stop(): void;
}

export const defaultInspectorDeps: InspectorDeps = {
  getConnection: () => window.pulse?.runtime.getConnection() ?? Promise.resolve(null),
  clientFor: (connection, signal) =>
    createRuntimeClient(connection, (input, init) => fetch(input, { ...init, signal })),
  id: () => crypto.randomUUID(),
  now: () => performance.now(),
  setTimer: (callback, ms) => setTimeout(callback, ms),
  clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
};

const sameRuntime = (a: RuntimeConnection | null, b: RuntimeConnection | null) =>
  a !== null &&
  b !== null &&
  a.pid === b.pid &&
  a.port === b.port &&
  a.token === b.token &&
  a.version === b.version;
const sameDevice = (a: InspectionDevice | null, b: InspectionDevice | null) =>
  a?.serial === b?.serial && a?.sessionId === b?.sessionId;
const initialState = (): InspectorState => ({
  device: null,
  blockedReason: null,
  status: 'idle',
  snapshot: null,
  selectedId: null,
  hoveredId: null,
  candidates: [],
  includeOcr: false,
  expectedPackage: '',
  visible: true,
  expired: true,
  expiredReason: null,
  validation: null,
  error: null,
});
const errorMessage = (error: unknown) =>
  error instanceof RuntimeApiError
    ? `Runtime 拒绝了只读请求（HTTP ${error.status}），请复查连接和目标应用。`
    : error instanceof Error
      ? error.message
      : '只读请求未确认，请刷新后重试。';

class InspectorConnectionFault extends Error {}

/** A device/session-bound authoring snapshot. No control, action or replay endpoint is available. */
export function createElementInspectorStore(
  deps: InspectorDeps = defaultInspectorDeps,
): ElementInspectorStore {
  let state = initialState();
  const listeners = new Set<() => void>();
  let observationVersion = 0;
  let validationVersion = 0;
  let observationWork: Promise<void> | null = null;
  let observeController: AbortController | null = null;
  let validateController: AbortController | null = null;
  let expiryTimer: unknown = null;
  let observationConnection: RuntimeConnection | null = null;
  let frameConnection: RuntimeConnection | null = null;
  let frameKey: string | null = null;

  const update = (partial: Partial<InspectorState>) => {
    state = { ...state, ...partial };
    for (const listener of listeners) listener();
  };
  const clearExpiry = () => {
    if (expiryTimer !== null) deps.clearTimer(expiryTimer);
    expiryTimer = null;
  };
  const cancelValidation = () => {
    validationVersion += 1;
    validateController?.abort();
    validateController = null;
  };
  const cancel = () => {
    observationVersion += 1;
    observeController?.abort();
    observeController = null;
    observationWork = null;
    cancelValidation();
    clearExpiry();
  };
  const invalidate = (reason: string) => {
    cancel();
    update({
      expired: true,
      expiredReason: reason,
      hoveredId: null,
      validation: null,
      status: state.blockedReason ? 'offline' : state.snapshot ? 'ready' : 'idle',
    });
  };
  const isFresh = () => {
    if (!state.snapshot || state.blockedReason || state.expired || state.status !== 'ready')
      return false;
    const age = deps.now() - state.snapshot.receivedAt;
    // Background tabs can delay timers. Recheck freshness at the authoring/validation boundary.
    if (age < 0 || age >= INSPECTION_MAX_AGE_MS) {
      invalidate('元素快照已过期，请重新采集后生成步骤。');
      return false;
    }
    return true;
  };
  const currentLocator = () =>
    isFresh() ? inspectorLocator(state.candidates, state.visible) : null;
  const readConnection = async (signal: AbortSignal) => {
    try {
      const connection = await withTimeout(
        deps.getConnection(),
        5000,
        'Runtime 连接读取超时。',
        signal,
      );
      if (!connection)
        throw new InspectorConnectionFault('本机 Runtime 未连接，请先恢复 Runtime 连接。');
      return connection;
    } catch (error) {
      if (signal.aborted || error instanceof InspectorConnectionFault) throw error;
      throw new InspectorConnectionFault(
        'Runtime 连接未确认，旧快照已失效；请恢复连接后重新采集。',
      );
    }
  };
  const verifyConnection = async (connection: RuntimeConnection, signal: AbortSignal) => {
    if (!sameRuntime(connection, await readConnection(signal)))
      throw new InspectorConnectionFault('Runtime 已更换，旧请求结果未使用。');
  };

  const validate = async () => {
    cancelValidation();
    const device = state.device;
    const locator = currentLocator();
    if (!device || state.blockedReason || state.expired || state.status !== 'ready' || !locator) {
      update({ validation: null });
      return;
    }
    if (!PACKAGE_NAME.test(state.expectedPackage)) {
      update({
        validation: {
          status: 'indeterminate',
          matchCount: null,
          errorCode: 'target_app_unverified',
          observationId: null,
          attempts: [],
        },
      });
      return;
    }
    const version = validationVersion;
    const controller = new AbortController();
    validateController = controller;
    const current = () => version === validationVersion && !controller.signal.aborted;
    const candidates = state.candidates.filter((candidate) => candidate.enabled);
    const expectedPackage = state.expectedPackage;
    update({
      validation: {
        status: 'checking',
        matchCount: null,
        errorCode: null,
        observationId: null,
        attempts: [],
      },
      error: null,
    });
    try {
      const connection = await readConnection(controller.signal);
      if (!current()) return;
      if (!sameRuntime(connection, observationConnection))
        throw new InspectorConnectionFault('观察与当前 Runtime 不一致，请重新采集元素。');
      const requestId = deps.id();
      const result = await withTimeout(
        deps.clientFor(connection, controller.signal).locate(device.serial, {
          serial: device.serial,
          session_id: device.sessionId,
          request_id: requestId,
          expected_package: expectedPackage,
          target: { locator },
          display_id: 0,
          timeout_ms: 15_000,
        }),
        20_000,
        '定位验证超时，唯一性未确认。',
        controller.signal,
      );
      await verifyConnection(connection, controller.signal);
      if (!current()) return;
      if (
        result.contract !== 'pulse-runtime-locator-draft' ||
        result.serial !== device.serial ||
        result.session_id !== device.sessionId ||
        result.request_id !== requestId ||
        result.automatic_replay !== false ||
        result.business_success !== null ||
        !['resolved', 'ambiguous', 'not_found', 'indeterminate'].includes(result.status) ||
        !Number.isSafeInteger(result.match_count) ||
        result.match_count < 0 ||
        !Array.isArray(result.attempts) ||
        result.attempts.some(
          (attempt) =>
            !Number.isSafeInteger(attempt.index) ||
            candidates[attempt.index]?.locator.by !== attempt.by,
        )
      )
        throw new Error('定位验证回执不匹配，唯一性未确认。');
      const complete =
        result.status !== 'resolved' ||
        (result.query_complete === true && result.match_count === 1);
      update({
        validation: {
          status: complete ? result.status : 'indeterminate',
          matchCount: result.match_count,
          errorCode: complete ? (result.error?.code ?? null) : 'query_incomplete',
          observationId: result.observation_id,
          attempts: result.attempts.flatMap((attempt) => {
            const candidate = candidates[attempt.index];
            return candidate
              ? [
                  {
                    key: candidate.key,
                    status: attempt.status,
                    matchCount: attempt.match_count,
                    errorCode: attempt.error?.code ?? null,
                  },
                ]
              : [];
          }),
        },
      });
    } catch (error) {
      if (current()) {
        if (error instanceof InspectorConnectionFault) {
          invalidate(error.message);
          update({ error: error.message });
        } else
          update({
            error: errorMessage(error),
            validation: {
              status: 'indeterminate',
              matchCount: null,
              errorCode: 'request_unconfirmed',
              observationId: null,
              attempts: [],
            },
          });
      }
    } finally {
      controller.abort();
      if (validateController === controller) validateController = null;
    }
  };

  const refresh = (): Promise<void> => {
    if (!state.device || state.blockedReason) return Promise.resolve();
    if (observationWork) return observationWork;
    cancel();
    const device = state.device;
    const includeOcr = state.includeOcr;
    const version = observationVersion;
    const controller = new AbortController();
    observeController = controller;
    const current = () => version === observationVersion && !controller.signal.aborted;
    update({
      status: 'loading',
      expired: true,
      expiredReason: '正在重新采集，旧快照仅供查看。',
      selectedId: null,
      hoveredId: null,
      candidates: [],
      validation: null,
      error: null,
    });
    const work = (async () => {
      try {
        const connection = await readConnection(controller.signal);
        if (!current()) return;
        if (frameKey && !sameRuntime(frameConnection, connection))
          throw new Error('画面与当前 Runtime 不一致，请先重连画面。');
        const api = deps.clientFor(connection, controller.signal);
        const readGeometry = async () => {
          try {
            return parseInspectionGeometry(
              await withTimeout(
                api.getAgentGeometry(device.serial, { session_id: device.sessionId }),
                5000,
                '显示几何读取超时。',
                controller.signal,
              ),
              device,
            );
          } catch (error) {
            if (controller.signal.aborted) throw error;
            return null;
          }
        };
        const before = await readGeometry();
        const requestId = deps.id();
        const observation = await withTimeout(
          api.observeAgent(device.serial, {
            session_id: device.sessionId,
            request_id: requestId,
            tree: true,
            screenshot: false,
            ocr: includeOcr,
            display_id: 0,
            for_action: false,
            timeout_ms: 15_000,
          }),
          20_000,
          '元素采集超时，请刷新后重试。',
          controller.signal,
        );
        const receivedAt = deps.now();
        const after = await readGeometry();
        await verifyConnection(connection, controller.signal);
        if (!current()) return;
        const geometry = sameInspectionGeometry(before, after) ? after : null;
        const snapshot = parseInspection(observation, device, requestId, receivedAt, geometry);
        const changed = before !== null && after !== null && !geometry;
        const age = deps.now() - receivedAt;
        const expired =
          changed || snapshot.changedDuringCapture || age < 0 || age >= INSPECTION_MAX_AGE_MS;
        observationConnection = connection;
        update({
          status: 'ready',
          snapshot,
          expectedPackage: snapshot.expectedPackage,
          visible: true,
          expired,
          expiredReason: expired ? '采集期间页面、方向或时间有效性已变化，请重新采集。' : null,
        });
        if (!expired)
          expiryTimer = deps.setTimer(() => {
            expiryTimer = null;
            invalidate('元素快照已超过 15 秒，请重新采集后生成步骤。');
          }, INSPECTION_MAX_AGE_MS - age);
      } catch (error) {
        if (current())
          update({
            status: 'error',
            error: errorMessage(error),
            expired: true,
            expiredReason: '采集未确认，旧快照仅供查看。',
          });
      } finally {
        controller.abort();
        if (observeController === controller) {
          observeController = null;
          observationWork = null;
        }
      }
    })();
    observationWork = work;
    return work;
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setDevice: (device, blockedReason = null) => {
      if (sameDevice(device, state.device) && blockedReason === state.blockedReason) return;
      const changed = !sameDevice(device, state.device);
      cancel();
      if (changed) {
        observationConnection = null;
        frameConnection = null;
        frameKey = null;
        state = { ...initialState(), includeOcr: state.includeOcr };
      }
      update({
        device,
        blockedReason,
        status: blockedReason ? 'offline' : state.snapshot ? 'ready' : 'idle',
        expired: true,
        expiredReason: state.snapshot
          ? (blockedReason ?? '设备连接已恢复，请重新采集元素。')
          : null,
        hoveredId: null,
        validation: null,
      });
    },
    setFrameSpace: (frame, connection) => {
      const next = frame
        ? `${frame.sessionId}:${frame.configId}:${frame.width}:${frame.height}:${frame.captureOrientation}`
        : null;
      if (
        next === frameKey &&
        (connection === frameConnection || sameRuntime(connection, frameConnection))
      )
        return;
      frameKey = next;
      frameConnection = connection;
      if (state.snapshot || observationWork) invalidate('画面会话或配置已变化，请重新采集元素。');
    },
    refresh,
    isFresh,
    currentLocator,
    select: (id) => {
      if (state.status === 'loading') return;
      const element = id ? state.snapshot?.byId.get(id) : null;
      if (id && !element) return;
      cancelValidation();
      update({
        selectedId: id,
        candidates:
          element && state.snapshot ? generateLocatorCandidates(element, state.snapshot) : [],
        validation: null,
        error: null,
      });
      if (id) void validate();
    },
    hover: (id) => {
      if (id === state.hoveredId || (id && (state.expired || !state.snapshot?.byId.has(id))))
        return;
      update({ hoveredId: id });
    },
    moveCandidate: (index, direction) => {
      const candidates = [...state.candidates];
      const first = candidates[index];
      const second = candidates[index + direction];
      if (!first || !second) return;
      candidates[index] = second;
      candidates[index + direction] = first;
      update({ candidates });
      void validate();
    },
    toggleCandidate: (key) => {
      update({
        candidates: state.candidates.map((candidate) =>
          candidate.key === key ? { ...candidate, enabled: !candidate.enabled } : candidate,
        ),
      });
      void validate();
    },
    setVisible: (visible) => {
      if (visible === state.visible) return;
      update({ visible });
      void validate();
    },
    setExpectedPackage: (packageName) => {
      update({ expectedPackage: packageName.slice(0, 256) });
      cancelValidation();
      update({ validation: null });
    },
    setIncludeOcr: (include) => {
      if (include === state.includeOcr) return;
      update({ includeOcr: include });
      if (state.snapshot || observationWork) invalidate('采集来源已调整，请重新采集元素。');
    },
    validate,
    invalidate,
    stop: () => {
      cancel();
      update({
        status: state.blockedReason ? 'offline' : state.snapshot ? 'ready' : 'idle',
        expired: true,
        expiredReason: state.snapshot ? '检查会话已暂停，请重新采集元素。' : null,
        hoveredId: null,
        validation: null,
      });
    },
  };
}
