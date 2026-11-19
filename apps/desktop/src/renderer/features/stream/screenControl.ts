import type { RuntimeConnection } from '../../../shared/ipc';
import { RuntimeApiError, type PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import { baseUrl, createRuntimeClient } from '../../shared/api/runtimeClient';
import { clientToDisplay, type Point, type Rotation } from '../../shared/lib/coords';
import { withTimeout } from '../../shared/stream/timeout';
import type { PresentedFrame } from '../../shared/stream/workerProtocol';
import { asRecord } from '../devices/deviceModel';

const REQUEST_MS = 5000;
const OBSERVE_MS = 20_000;
const ACTION_MS = 35_000;
const REFRESH_MS = 2000;
const MAX_POSE_AGE_MS = 5000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PACKAGE = /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/;
const UNSAFE_TREE_REASONS = new Set([
  'node_limit',
  'depth_limit',
  'missing_child',
  'text_truncated',
  'text_redaction_unverified',
  'page_changed_during_capture',
  'capability_changed_during_capture',
  'capability_result_conflict',
  'capability_declaration_unverified',
]);

export interface DisplayGeometry {
  display_id: 0;
  width: number;
  height: number;
  rotation: Rotation;
}

export interface ScreenControlSnapshot {
  device_id: string;
  session_id: string | null;
  epoch: number;
  holder_source: 'manual' | 'auto' | 'recorder' | 'ai' | null;
  run_id: string | null;
  recovery_required: boolean;
}

export interface ScreenControlState {
  status: 'loading' | 'ready' | 'unavailable';
  control: ScreenControlSnapshot | null;
  geometry: DisplayGeometry | null;
  owned: boolean;
  busy: 'acquire' | 'release' | 'action' | null;
  error: string | null;
  notice: string | null;
}

export type ScreenKey = 'HOME' | 'BACK' | 'ENTER' | 'BACKSPACE' | 'MENU' | 'RECENTS';
const KEY_CODES: Record<ScreenKey, number> = {
  HOME: 3,
  BACK: 4,
  ENTER: 66,
  BACKSPACE: 67,
  MENU: 82,
  RECENTS: 187,
};

export interface ScreenActionBody {
  serial: string;
  session_id: string;
  request_id: string;
  operation_id: string;
  kind: 'tap' | 'swipe' | 'set_text' | 'key';
  target: {
    observation_id: string;
    source: 'tree';
    coordinate_space: 'display_pixels';
    display_id: 0;
    expected_package: string;
    node_path: number[];
  };
  params: Record<string, number | string>;
  source: 'manual';
  control_epoch: number;
  run_id: string;
  timeout_ms: number;
  agent_timeout_ms: number;
}

export interface ScreenControlApi {
  getControl(serial: string, query: { session_id: string }): Promise<unknown>;
  getAgentGeometry(serial: string, query: { session_id: string }): Promise<unknown>;
  acquireControl(serial: string, body: PulseRuntimeApiSchemas.ControlAcquireBody): Promise<unknown>;
  releaseControl(serial: string, body: PulseRuntimeApiSchemas.ControlReleaseBody): Promise<unknown>;
  observeAgent(serial: string, body: PulseRuntimeApiSchemas.AgentObserveBody): Promise<unknown>;
  performAction(serial: string, body: ScreenActionBody): Promise<unknown>;
}

export interface ScreenControlDeps {
  getConnection(): Promise<RuntimeConnection | null>;
  clientFor(
    connection: RuntimeConnection,
    signal: AbortSignal,
    keepalive?: boolean,
  ): ScreenControlApi;
  id(): string;
  now(): number;
  setTimer(callback: () => void, ms: number): unknown;
  clearTimer(timer: unknown): void;
}

export const defaultScreenControlDeps: ScreenControlDeps = {
  getConnection: () => window.pulse?.runtime.getConnection() ?? Promise.resolve(null),
  clientFor: (connection, signal, keepalive = false) => {
    const fetchBound: typeof fetch = (input, init) =>
      fetch(input, { ...init, signal, keepalive: keepalive || init?.keepalive === true });
    const client = createRuntimeClient(connection, fetchBound);
    return {
      getControl: (serial, query) => client.getControl(serial, query),
      getAgentGeometry: (serial, query) => client.getAgentGeometry(serial, query),
      acquireControl: (serial, body) => client.acquireControl(serial, body),
      releaseControl: (serial, body) => client.releaseControl(serial, body),
      observeAgent: (serial, body) => client.observeAgent(serial, body),
      // This existing draft endpoint manually validates JSON and has no OpenAPI request body.
      performAction: async (serial, body) => {
        const response = await fetchBound(
          `${baseUrl(connection)}/devices/${encodeURIComponent(serial)}/agent/action`,
          {
            method: 'POST',
            keepalive: true,
            headers: {
              Authorization: `Bearer ${connection.token}`,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
          },
        );
        const result: unknown = await response.json();
        if (!response.ok) throw new RuntimeApiError(response.status, result);
        return result;
      },
    };
  },
  id: () => crypto.randomUUID(),
  now: () => performance.now(),
  setTimer: (callback, ms) => setTimeout(callback, ms),
  clearTimer: (timer) => clearTimeout(timer as ReturnType<typeof setTimeout>),
};

class ScreenFault extends Error {}

const sameRuntime = (a: RuntimeConnection | null, b: RuntimeConnection | null) =>
  a !== null &&
  b !== null &&
  a.pid === b.pid &&
  a.port === b.port &&
  a.token === b.token &&
  a.version === b.version;

const sameGeometry = (a: DisplayGeometry | null, b: DisplayGeometry | null) =>
  a !== null &&
  b !== null &&
  a.width === b.width &&
  a.height === b.height &&
  a.rotation === b.rotation &&
  a.display_id === b.display_id;

export const viewRotation = (geometry: DisplayGeometry): Rotation =>
  ((360 - geometry.rotation) % 360) as Rotation;

function parseGeometry(value: unknown): DisplayGeometry | null {
  const raw = asRecord(value);
  if (!raw || raw['display_id'] !== 0 || ![0, 90, 180, 270].includes(raw['rotation'] as number))
    return null;
  if (
    ![raw['width'], raw['height']].every(
      (size) => typeof size === 'number' && Number.isSafeInteger(size) && size > 0 && size <= 8192,
    )
  )
    return null;
  return {
    display_id: 0,
    width: raw['width'] as number,
    height: raw['height'] as number,
    rotation: raw['rotation'] as Rotation,
  };
}

interface NativeNode {
  path: number[];
  package: string | null;
  bounds: { left: number; top: number; right: number; bottom: number };
  enabled: boolean;
  visible: boolean | null;
  editable: boolean;
  focused: boolean;
  redacted: boolean | null;
}

function nativeNodes(value: unknown, interactive: boolean): NativeNode[] {
  const observation = asRecord(value);
  const framing = asRecord(observation?.['framing']);
  const tree = asRecord(asRecord(observation?.['sources'])?.['tree']);
  const coordinates = asRecord(tree?.['coordinates']);
  const data = asRecord(tree?.['data']);
  const reasons = asRecord(tree?.['completeness'])?.['reasons'];
  if (
    observation?.['success'] !== true ||
    framing?.['validated'] !== true ||
    framing['source_data_validated'] !== true ||
    (interactive &&
      (!Number.isSafeInteger(observation['revision_start']) ||
        observation['revision_start'] !== observation['revision_end'])) ||
    tree?.['status'] !== 'available' ||
    coordinates?.['coordinate_space'] !== 'display_pixels' ||
    coordinates['display_id'] !== 0 ||
    data?.['coordinate_space'] !== 'display_pixels' ||
    data['display_id'] !== 0 ||
    !Array.isArray(reasons) ||
    reasons.some((reason) => typeof reason !== 'string') ||
    (interactive && reasons.some((reason) => UNSAFE_TREE_REASONS.has(reason as string))) ||
    !Array.isArray(data['nodes']) ||
    data['nodes'].length > 4096
  )
    throw new ScreenFault('原生树不可用或不完整，本次动作未发送。');
  const paths = new Set<string>();
  return data['nodes'].map((value: unknown) => {
    const node = asRecord(value);
    const path = node?.['node_path'];
    const bounds = asRecord(node?.['bounds']);
    const states = asRecord(node?.['states']);
    const pkg = node?.['package_name'];
    if (
      node?.['source'] !== 'accessibility' ||
      !Array.isArray(path) ||
      path.length > 32 ||
      path.some((index) => !Number.isSafeInteger(index) || index < 0 || index > 511) ||
      (pkg !== null && (typeof pkg !== 'string' || !PACKAGE.test(pkg))) ||
      !bounds ||
      !['left', 'top', 'right', 'bottom'].every((key) => Number.isSafeInteger(bounds[key])) ||
      (node['text_redacted'] !== null && typeof node['text_redacted'] !== 'boolean') ||
      !states
    )
      throw new ScreenFault('原生节点信息无效，本次动作未发送。');
    const key = path.join('/');
    if (paths.has(key)) throw new ScreenFault('原生节点路径重复，本次动作未发送。');
    paths.add(key);
    return {
      path: path as number[],
      package: pkg as string | null,
      bounds: {
        left: bounds['left'] as number,
        top: bounds['top'] as number,
        right: bounds['right'] as number,
        bottom: bounds['bottom'] as number,
      },
      enabled: states['enabled'] === true,
      visible: typeof states['visible'] === 'boolean' ? states['visible'] : null,
      editable: states['editable'] === true,
      focused: states['focused'] === true,
      redacted: node['text_redacted'] as boolean | null,
    };
  });
}

interface Lease {
  connection: RuntimeConnection;
  runId: string;
  epoch: number;
}

export interface ScreenGesture {
  frame: PresentedFrame;
  start: Point;
  end: Point;
  rotation: Rotation;
  swipe: boolean;
  durationMs: number;
}

type InputAction =
  | { kind: 'gesture'; gesture: ScreenGesture }
  | { kind: 'text'; text: string }
  | { kind: 'key'; key: ScreenKey };

/** One local manual-control run. Never adopts another window's run_id or uses takeover. */
export function createScreenControl(
  device: { serial: string; sessionId: string | null },
  deps: ScreenControlDeps = defaultScreenControlDeps,
) {
  let state: ScreenControlState = {
    status: 'loading',
    control: null,
    geometry: null,
    owned: false,
    busy: null,
    error: null,
    notice: null,
  };
  const listeners = new Set<() => void>();
  let running = false;
  let enabled = false;
  let generation = 0;
  let owner: RuntimeConnection | null = null;
  let lease: Lease | null = null;
  let pendingLease: Lease | null = null;
  const cleanups = new Set<string>();
  let frame: PresentedFrame | null = null;
  let frameConnection: RuntimeConnection | null = null;
  let poseAt = -Infinity;
  let timer: unknown = null;
  let refreshWork: Promise<void> | null = null;
  let refreshController: AbortController | null = null;
  let workController: AbortController | null = null;
  let dispatched = false;

  const update = (partial: Partial<ScreenControlState>) => {
    state = { ...state, ...partial };
    for (const listener of listeners) listener();
  };
  const clearTimer = () => {
    if (timer !== null) deps.clearTimer(timer);
    timer = null;
  };
  const sessionId = () => {
    if (!device.sessionId || !UUID.test(device.sessionId))
      throw new ScreenFault('尚无有效 Agent 会话，请在设备面板接入既有 Agent。');
    return device.sessionId;
  };
  const readConnection = async (signal: AbortSignal) => {
    const connection = await withTimeout(
      deps.getConnection(),
      REQUEST_MS,
      'Runtime connection timeout',
      signal,
    );
    if (!connection) throw new ScreenFault('本机 Runtime 未连接，操作已禁用。');
    return connection;
  };
  const verifyRuntime = async (connection: RuntimeConnection, signal: AbortSignal) => {
    if (!sameRuntime(connection, await readConnection(signal)))
      throw new ScreenFault('Runtime 连接已更换，旧请求已丢弃。');
  };
  const parseControl = (value: unknown): ScreenControlSnapshot => {
    const raw = asRecord(value);
    const control = asRecord(raw?.['control']);
    const source = control?.['holder_source'];
    if (
      raw?.['serial'] !== device.serial ||
      control?.['device_id'] !== device.serial ||
      control['session_id'] !== sessionId() ||
      !Number.isSafeInteger(control['epoch']) ||
      (control['epoch'] as number) < 0 ||
      (source !== null &&
        source !== 'manual' &&
        source !== 'auto' &&
        source !== 'recorder' &&
        source !== 'ai') ||
      (control['run_id'] !== null &&
        (typeof control['run_id'] !== 'string' || !UUID.test(control['run_id']))) ||
      typeof control['recovery_required'] !== 'boolean'
    )
      throw new ScreenFault('控制会话不可确认，操作已禁用。');
    return {
      device_id: device.serial,
      session_id: device.sessionId,
      epoch: control['epoch'] as number,
      holder_source: source,
      run_id: control['run_id'] as string | null,
      recovery_required: control['recovery_required'],
    };
  };
  const matchesLease = (control: ScreenControlSnapshot, held: Lease | null = lease) =>
    held !== null &&
    control.holder_source === 'manual' &&
    control.run_id === held.runId &&
    control.epoch === held.epoch &&
    control.session_id === device.sessionId;
  const acceptControl = (control: ScreenControlSnapshot) =>
    update({ control, owned: matchesLease(control) });
  const describeError = (error: unknown) => {
    if (error instanceof ScreenFault) return error.message;
    if (error instanceof RuntimeApiError)
      return `Runtime 请求被拒绝（HTTP ${error.status}），请复查设备与控制状态；不会自动重试动作。`;
    return '请求未确认，请复查设备与控制状态；不会自动重试动作。';
  };
  const currentFrame = (presented: PresentedFrame) =>
    frame !== null &&
    presented.sessionId === frame.sessionId &&
    presented.configId === frame.configId &&
    presented.width === frame.width &&
    presented.height === frame.height &&
    presented.captureOrientation === frame.captureOrientation;
  const frameFits = (presented: PresentedFrame, geometry: DisplayGeometry) => {
    const sideways = geometry.rotation === 90 || geometry.rotation === 270;
    const width = sideways ? geometry.height : geometry.width;
    const height = sideways ? geometry.width : geometry.height;
    if (
      ![presented.width, presented.height].every((size) => Number.isSafeInteger(size) && size > 0)
    )
      return false;
    const scale = Math.min(presented.width / width, presented.height / height);
    return (
      Math.abs(presented.width - width * scale) <= 8 &&
      Math.abs(presented.height - height * scale) <= 8
    );
  };
  const operationReason = (): string | null => {
    if (!running || state.status !== 'ready') return '控制状态未确认，操作已禁用。';
    if (state.control?.recovery_required) return 'Runtime 要求人工恢复；不会自动接管或解除阻断。';
    if (!state.owned)
      return state.control?.holder_source
        ? '控制权属于其他控制会话，当前窗口不能操作。'
        : '尚未取得控制权，当前窗口只能观看。';
    if (state.busy) return '正在等待本次请求回执，不会重复发送。';
    if (!frame) return '没有可操作的当前画面。';
    if (
      !frame.sessionId ||
      !Number.isSafeInteger(frame.configId) ||
      !Number.isSafeInteger(frame.seq)
    )
      return '当前帧身份未确认，操作已禁用。';
    if (frame.captureOrientation !== 'natural')
      return '采集未确认固定 natural 方向，坐标操作已禁用；请重连到支持该协议的 Runtime。';
    if (!sameRuntime(owner, frameConnection)) return '画面与控制权来自不同 Runtime，操作已禁用。';
    if (!state.geometry || deps.now() - poseAt > MAX_POSE_AGE_MS || deps.now() < poseAt)
      return '显示几何未确认或已过期，操作已禁用。';
    if (!frameFits(frame, state.geometry)) return '画面与显示尺寸不匹配，操作已禁用。';
    return null;
  };
  const schedule = () => {
    clearTimer();
    if (!running) return;
    timer = deps.setTimer(() => {
      timer = null;
      if (state.busy) schedule();
      else void refresh();
    }, REFRESH_MS);
  };
  const refresh = (): Promise<void> => {
    if (!running || state.busy) return Promise.resolve();
    if (refreshWork) return refreshWork;
    clearTimer();
    const revision = generation;
    const controller = new AbortController();
    refreshController = controller;
    const current = () => running && revision === generation && !controller.signal.aborted;
    const work = (async () => {
      try {
        const id = sessionId();
        const connection = await readConnection(controller.signal);
        const api = deps.clientFor(connection, controller.signal);
        const control = parseControl(
          await withTimeout(
            api.getControl(device.serial, { session_id: id }),
            REQUEST_MS,
            'Control read timeout',
            controller.signal,
          ),
        );
        const pose = asRecord(
          await withTimeout(
            api.getAgentGeometry(device.serial, { session_id: id }),
            REQUEST_MS,
            'Geometry read timeout',
            controller.signal,
          ),
        );
        if (!current()) return;
        await verifyRuntime(connection, controller.signal);
        if (!current()) return;
        if (
          pose?.['serial'] !== device.serial ||
          pose['session_id'] !== id ||
          pose['source'] !== 'adb_input_viewport' ||
          pose['atomic_with_observation'] !== false
        )
          throw new ScreenFault('显示几何来源不可确认，操作已禁用。');
        const geometry = pose['status'] === 'available' ? parseGeometry(pose['geometry']) : null;
        owner = connection;
        poseAt = geometry ? deps.now() : -Infinity;
        update({ status: 'ready', control, geometry, owned: matchesLease(control), error: null });
      } catch (error) {
        if (current())
          update({
            status: 'unavailable',
            geometry: null,
            owned: false,
            error: describeError(error),
          });
      } finally {
        controller.abort();
        if (refreshController === controller) {
          refreshController = null;
          refreshWork = null;
          schedule();
        }
      }
    })();
    refreshWork = work;
    return work;
  };

  // Cleanup remains bound to the issuing Runtime and to this exact run, never a fresh discovery URL.
  const cleanupOwn = async (held: Lease) => {
    const key = `${held.runId}:${held.epoch}`;
    if (cleanups.has(key)) return;
    cleanups.add(key);
    const controller = new AbortController();
    try {
      const api = deps.clientFor(held.connection, controller.signal, true);
      // Submit before yielding: a destroyed document cannot continue a GET-then-POST chain.
      // Runtime rechecks the exact tuple after native work settles, including null holders.
      await withTimeout(
        api.releaseControl(device.serial, {
          session_id: sessionId(),
          control_epoch: held.epoch,
          run_id: held.runId,
          wait_for_idle_ms: 60_000,
        }),
        65_000,
        'Cleanup release timeout',
        controller.signal,
      );
    } catch {
      /* A vanished session or recovery guard is not bypassed. */
    } finally {
      controller.abort();
      cleanups.delete(key);
    }
  };

  const changeControl = async (kind: 'acquire' | 'release') => {
    if (!running || state.busy || state.status !== 'ready') return;
    if (
      kind === 'acquire'
        ? state.control?.holder_source !== null || state.control.recovery_required
        : !state.owned || !lease
    )
      return;
    const revision = generation;
    const controller = new AbortController();
    workController = controller;
    const held = lease;
    let submitted: Lease | null = null;
    const current = () => running && revision === generation && !controller.signal.aborted;
    clearTimer();
    update({ busy: kind, error: null, notice: null });
    try {
      await refreshWork;
      const connection = await readConnection(controller.signal);
      if (!current()) return;
      if (!sameRuntime(owner, connection))
        throw new ScreenFault('Runtime 连接已更换，请刷新后再取得控制权。');
      const api = deps.clientFor(connection, controller.signal);
      const before = parseControl(
        await withTimeout(
          api.getControl(device.serial, { session_id: sessionId() }),
          REQUEST_MS,
          'Control read timeout',
          controller.signal,
        ),
      );
      if (!current()) return;
      if (before.recovery_required) throw new ScreenFault('Runtime 要求人工恢复，控制权未变更。');
      let response: unknown;
      if (kind === 'acquire') {
        if (before.holder_source !== null) {
          acceptControl(before);
          throw new ScreenFault('控制权已由其他会话持有，不会自动接管。');
        }
        const runId = deps.id();
        submitted = { connection, runId, epoch: before.epoch };
        pendingLease = submitted;
        const acquiring = api.acquireControl(device.serial, {
          session_id: sessionId(),
          source: 'manual',
          run_id: runId,
        });
        void acquiring.then(
          (result) => {
            if (current()) return;
            try {
              const control = parseControl(result);
              if (control.holder_source === 'manual' && control.run_id === runId)
                void cleanupOwn({ connection, runId, epoch: control.epoch });
            } catch {
              /* Invalid or foreign responses cannot authorize cleanup. */
            }
          },
          () => undefined,
        );
        response = await withTimeout(
          acquiring,
          REQUEST_MS,
          'Control acquire timeout',
          controller.signal,
        );
      } else {
        if (!held || !matchesLease(before, held)) {
          acceptControl(before);
          throw new ScreenFault('控制权已变化，未释放其他会话的控制权。');
        }
        response = await withTimeout(
          api.releaseControl(device.serial, {
            session_id: sessionId(),
            control_epoch: held.epoch,
            run_id: held.runId,
          }),
          REQUEST_MS,
          'Control release timeout',
          controller.signal,
        );
      }
      const control = parseControl(response);
      await verifyRuntime(connection, controller.signal);
      if (!current()) {
        if (submitted) void cleanupOwn({ ...submitted, epoch: control.epoch });
        return;
      }
      if (kind === 'acquire') {
        if (!submitted || control.holder_source !== 'manual' || control.run_id !== submitted.runId)
          throw new ScreenFault('控制权取得回执不匹配，操作已禁用。');
        lease = { ...submitted, epoch: control.epoch };
      } else {
        if (control.holder_source !== null || control.run_id !== null)
          throw new ScreenFault('控制权释放未确认，请刷新状态。');
        lease = null;
      }
      update({
        status: 'ready',
        control,
        owned: matchesLease(control),
        notice:
          kind === 'acquire'
            ? '已确认取得本窗口控制权；切换到操作模式后才发送动作。'
            : '已确认释放本窗口控制权。',
      });
    } catch (error) {
      if (current()) update({ status: 'unavailable', owned: false, error: describeError(error) });
      if (submitted) void cleanupOwn(submitted);
    } finally {
      controller.abort();
      if (pendingLease === submitted) pendingLease = null;
      if (workController === controller) {
        workController = null;
        update({ busy: null });
        schedule();
      }
    }
  };

  const perform = async (input: InputAction): Promise<boolean> => {
    if (!enabled || operationReason() !== null || !lease || !frame || !state.geometry) return false;
    const held = lease;
    const initialFrame = frame;
    const initialGeometry = state.geometry;
    const revision = generation;
    const controller = new AbortController();
    workController = controller;
    dispatched = false;
    const current = () => running && generation === revision && !controller.signal.aborted;
    const beforeDispatch = () => {
      if (!current() || !enabled || !currentFrame(initialFrame))
        throw new ScreenFault('画面或操作模式已变化，本次动作未发送。');
    };
    clearTimer();
    update({ busy: 'action', error: null, notice: null });
    try {
      await refreshWork;
      const connection = await readConnection(controller.signal);
      beforeDispatch();
      if (!sameRuntime(held.connection, connection) || !sameRuntime(frameConnection, connection))
        throw new ScreenFault('Runtime 已更换，本次动作未发送。');
      const api = deps.clientFor(connection, controller.signal);
      const control = parseControl(
        await withTimeout(
          api.getControl(device.serial, { session_id: sessionId() }),
          REQUEST_MS,
          'Control read timeout',
          controller.signal,
        ),
      );
      beforeDispatch();
      acceptControl(control);
      if (!matchesLease(control, held) || control.recovery_required)
        throw new ScreenFault('控制权或 epoch 已变化，本次动作未发送。');
      const observationId = deps.id();
      const observed = asRecord(
        await withTimeout(
          api.observeAgent(device.serial, {
            session_id: sessionId(),
            request_id: observationId,
            tree: true,
            screenshot: false,
            ocr: false,
            display_id: 0,
            timeout_ms: 15_000,
            for_action: true,
          }),
          OBSERVE_MS,
          'Native observation timeout',
          controller.signal,
        ),
      );
      beforeDispatch();
      if (observed?.['session_id'] !== sessionId() || observed['request_id'] !== observationId)
        throw new ScreenFault('原生观察会话不匹配，本次动作未发送。');
      const context = asRecord(observed['action_context']);
      const geometry =
        context?.['status'] === 'available' && context['source'] === 'adb_input_viewport'
          ? parseGeometry(context['geometry'])
          : null;
      if (!geometry) throw new ScreenFault('当前显示几何不可确认，本次动作未发送。');
      poseAt = deps.now();
      update({ geometry });
      const nodes = nativeNodes(observed, input.kind !== 'key');
      const root = nodes.find((node) => node.path.length === 0);
      if (!root?.package) throw new ScreenFault('原生根节点所属应用不可确认，本次动作未发送。');
      let target: NativeNode | undefined;
      let kind: ScreenActionBody['kind'];
      let params: ScreenActionBody['params'];
      if (input.kind === 'gesture') {
        const stroke = input.gesture;
        if (
          !sameGeometry(initialGeometry, geometry) ||
          !currentFrame(stroke.frame) ||
          stroke.rotation !== viewRotation(geometry) ||
          !frameFits(stroke.frame, geometry)
        )
          throw new ScreenFault('设备方向或尺寸已变化，本次手势未发送。');
        const mapping = {
          frameWidth: stroke.frame.width,
          frameHeight: stroke.frame.height,
          displayWidth: geometry.width,
          displayHeight: geometry.height,
          rotation: geometry.rotation,
        };
        const rect = { left: 0, top: 0, width: stroke.frame.width, height: stroke.frame.height };
        const start = clientToDisplay(
          { x: stroke.start.x + 0.5, y: stroke.start.y + 0.5 },
          rect,
          mapping,
        );
        const end = clientToDisplay(
          { x: stroke.end.x + 0.5, y: stroke.end.y + 0.5 },
          rect,
          mapping,
        );
        if (!start || !end) throw new ScreenFault('手势越出当前画面，本次动作未发送。');
        const contains = (node: NativeNode, point: Point) =>
          node.bounds.left <= point.x &&
          point.x < node.bounds.right &&
          node.bounds.top <= point.y &&
          point.y < node.bounds.bottom;
        const hit = (point: Point): NativeNode => {
          const hits = nodes
            .filter((node) => contains(node, point) && node.visible !== false)
            .sort((a, b) => b.path.length - a.path.length);
          if (hits.some((node) => node.visible === null))
            throw new ScreenFault('触点节点可见性未知，本次手势未发送。');
          const deepest = hits[0];
          if (!deepest) throw new ScreenFault('触点没有明确的原生节点，本次手势未发送。');
          if (
            hits.some((node) => node.path.some((index, offset) => index !== deepest.path[offset]))
          )
            throw new ScreenFault('触点命中重叠的原生节点，本次手势未发送。');
          // Disabled or unknown descendants still occlude their ancestors; never click through them.
          if (!deepest.enabled || deepest.redacted !== false || deepest.package !== root.package)
            throw new ScreenFault('触点原生节点不可操作，本次手势未发送。');
          return deepest;
        };
        const first = hit(start);
        const last = stroke.swipe ? hit(end) : first;
        const mismatch = first.path.findIndex((index, offset) => index !== last.path[offset]);
        const common = first.path.slice(0, mismatch === -1 ? first.path.length : mismatch);
        target = nodes.find(
          (node) =>
            node.path.length === common.length &&
            node.path.every((index, offset) => index === common[offset]),
        );
        if (
          !target ||
          !target.enabled ||
          target.visible !== true ||
          target.redacted !== false ||
          target.package !== root.package ||
          !contains(target, start) ||
          !contains(target, end)
        )
          throw new ScreenFault('没有包含整段手势的可操作原生节点，本次动作未发送。');
        if (
          nodes.some(
            (node) =>
              node.redacted !== false &&
              Math.min(start.x, end.x) < node.bounds.right &&
              Math.max(start.x, end.x) >= node.bounds.left &&
              Math.min(start.y, end.y) < node.bounds.bottom &&
              Math.max(start.y, end.y) >= node.bounds.top,
          )
        )
          throw new ScreenFault('手势涉及敏感字段，本次动作未发送。');
        kind = stroke.swipe ? 'swipe' : 'tap';
        params = stroke.swipe
          ? {
              x: start.x,
              y: start.y,
              end_x: end.x,
              end_y: end.y,
              duration_ms: Math.min(1000, Math.max(500, Math.round(stroke.durationMs))),
            }
          : { x: start.x, y: start.y, duration_ms: 80 };
      } else if (input.kind === 'text') {
        const focused = nodes.filter(
          (node) =>
            node.enabled &&
            node.visible &&
            node.editable &&
            node.focused &&
            node.redacted === false &&
            node.package === root.package,
        );
        if (focused.length !== 1)
          throw new ScreenFault('没有唯一的可编辑焦点字段，本次文本未发送。');
        target = focused[0];
        kind = 'set_text';
        params = { text: input.text };
      } else {
        target = root;
        kind = 'key';
        params = { code: KEY_CODES[input.key] };
      }
      if (!target || target.package === 'com.pulse.agent')
        throw new ScreenFault('没有安全的原生动作目标，本次动作未发送。');
      const body: ScreenActionBody = {
        serial: device.serial,
        session_id: sessionId(),
        request_id: deps.id(),
        operation_id: deps.id(),
        kind,
        target: {
          observation_id: observationId,
          source: 'tree',
          coordinate_space: 'display_pixels',
          display_id: 0,
          expected_package: root.package,
          node_path: target.path,
        },
        params,
        source: 'manual',
        control_epoch: held.epoch,
        run_id: held.runId,
        timeout_ms: 30_000,
        agent_timeout_ms: 5000,
      };
      await verifyRuntime(connection, controller.signal);
      beforeDispatch();
      dispatched = true;
      const action = api.performAction(device.serial, body);
      void action
        .finally(() => {
          if (!running || revision !== generation) void cleanupOwn(held);
        })
        .catch(() => undefined);
      const result = asRecord(
        await withTimeout(action, ACTION_MS, 'Device action timeout', controller.signal),
      );
      if (!current()) return false;
      await verifyRuntime(connection, controller.signal);
      if (!current()) return false;
      if (
        result?.['serial'] !== device.serial ||
        result['session_id'] !== sessionId() ||
        result['request_id'] !== body.request_id ||
        result['operation_id'] !== body.operation_id
      )
        throw new ScreenFault('动作回执不匹配，请复查设备；不会重复发送。');
      const receipt = asRecord(result['control']);
      if (
        result['status'] !== 'completed' ||
        result['action_completed'] !== true ||
        asRecord(result['framing'])?.['validated'] !== true ||
        asRecord(result['ledger'])?.['state'] !== 'verified' ||
        receipt?.['epoch'] !== held.epoch ||
        receipt['run_id'] !== held.runId ||
        receipt['source'] !== 'manual'
      ) {
        const status =
          result['status'] === 'uncertain'
            ? '结果不确定'
            : result['status'] === 'rejected'
              ? '被 Runtime 拒绝'
              : '未确认完成';
        throw new ScreenFault(`动作${status}，请复查设备；不会自动重试。`);
      }
      update({
        notice:
          asRecord(result['verification'])?.['status'] === 'available'
            ? '设备动作已确认；未评估业务结果。'
            : '设备动作已确认；后置观察不可用，未评估业务结果。',
      });
      return true;
    } catch (error) {
      if (current())
        update({
          error: describeError(error),
          ...(dispatched ? { status: 'unavailable', owned: false, geometry: null } : {}),
        });
      return false;
    } finally {
      controller.abort();
      if (workController === controller) {
        workController = null;
        dispatched = false;
        update({ busy: null });
        schedule();
      }
    }
  };

  const stop = () => {
    if (!running) return;
    running = false;
    enabled = false;
    generation += 1;
    clearTimer();
    refreshController?.abort();
    if (!dispatched) workController?.abort();
    refreshController = null;
    refreshWork = null;
    frame = null;
    frameConnection = null;
    if (lease) void cleanupOwn(lease);
    if (pendingLease) void cleanupOwn(pendingLease);
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    start: () => {
      running = true;
      generation += 1;
      void refresh();
      const revision = generation;
      return () => {
        if (generation === revision) stop();
      };
    },
    stop,
    refresh,
    acquire: () => changeControl('acquire'),
    release: () => changeControl('release'),
    operationReason,
    setEnabled: (value: boolean) => {
      enabled = value;
      if (!value && !dispatched && state.busy === 'action') workController?.abort();
    },
    setFrame: (value: PresentedFrame | null, connection: RuntimeConnection | null) => {
      const changed =
        value === null
          ? frame !== null
          : !currentFrame(value) || !sameRuntime(frameConnection, connection);
      frame = value;
      frameConnection = connection;
      if (changed) {
        if (!dispatched && state.busy === 'action') workController?.abort();
        update({});
      }
    },
    gesture: (gesture: ScreenGesture) => perform({ kind: 'gesture', gesture }),
    key: (key: ScreenKey) =>
      Object.hasOwn(KEY_CODES, key) ? perform({ kind: 'key', key }) : Promise.resolve(false),
    input: (text: string) => {
      if (text.length > 4096 || /[\uD800-\uDFFF]/u.test(text)) {
        update({ error: '文本超过 4096 个 UTF-16 单元或含无效字符，本次输入未发送。' });
        return Promise.resolve(false);
      }
      return perform({ kind: 'text', text });
    },
  };
}
