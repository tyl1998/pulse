import {
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';
import { ArrowLeft, Home, Maximize2, Minimize2, Pause, Play, RefreshCw } from 'lucide-react';
import type { RuntimeConnection } from '../../../shared/ipc';
import {
  clientToDisplay,
  containRect,
  displayBoundsToClient,
  type Point,
  type Rect,
  type Rotation,
} from '../../shared/lib/coords';
import {
  DEFAULT_STREAM_OPTIONS,
  defaultStreamDeps,
  runtimeStreamUrl,
  StreamView,
  type StreamViewDeps,
} from '../../shared/stream/streamClient';
import {
  interruptsStream,
  type Metrics,
  type PresentedFrame,
  type StreamStatus,
} from '../../shared/stream/workerProtocol';
import { Badge, Button, EmptyState, Input, Select } from '../../shared/ui';
import type { DeviceSnapshot } from '../devices/deviceModel';
import { getDeviceStore, type DeviceStore } from '../devices/deviceStore';
import {
  hitTestInspection,
  inspectionFrameGeometry,
  sameInspectionGeometry,
} from '../inspector/inspectionModel';
import {
  createElementInspectorStore,
  type ElementInspectorStore,
} from '../inspector/inspectorStore';
import '../inspector/inspector.css';
import {
  createScreenControl,
  defaultScreenControlDeps,
  viewRotation,
  type ScreenControlDeps,
  type ScreenKey,
} from './screenControl';

export interface DeviceScreenDeps {
  supported(): boolean;
  stream: StreamViewDeps;
  control: ScreenControlDeps;
  transferCanvas(canvas: HTMLCanvasElement): OffscreenCanvas;
}

export const defaultDeviceScreenDeps: DeviceScreenDeps = {
  supported: () =>
    typeof Worker !== 'undefined' &&
    typeof VideoDecoder !== 'undefined' &&
    typeof HTMLCanvasElement.prototype.transferControlToOffscreen === 'function',
  stream: defaultStreamDeps,
  control: defaultScreenControlDeps,
  transferCanvas: (canvas) => canvas.transferControlToOffscreen(),
};

type ScreenMode = 'watch' | 'operate' | 'inspect';

interface Gesture {
  pointerId: number;
  frame: PresentedFrame;
  rotation: Rotation;
  rect: Rect;
  start: Point;
  clientStart: Point;
  startedAt: number;
}

const sameFrameSpace = (a: PresentedFrame | null, b: PresentedFrame | null) =>
  a?.sessionId === b?.sessionId &&
  a?.configId === b?.configId &&
  a?.width === b?.width &&
  a?.height === b?.height &&
  a?.captureOrientation === b?.captureOrientation;

const sameRect = (a: Rect, b: Rect) =>
  (['left', 'top', 'width', 'height'] as const).every((key) => Math.abs(a[key] - b[key]) < 0.5);

function canvasRect(host: HTMLElement, frame: PresentedFrame, rotation: Rotation): Rect | null {
  const canvas = host.querySelector('canvas');
  if (
    !canvas ||
    canvas.dataset['rotation'] !== String(rotation) ||
    canvas.dataset['session'] !== frame.sessionId ||
    canvas.dataset['config'] !== String(frame.configId) ||
    canvas.dataset['width'] !== String(frame.width) ||
    canvas.dataset['height'] !== String(frame.height)
  )
    return null;
  return canvas.getBoundingClientRect();
}

function streamDescription(status: StreamStatus | null): string {
  if (!status) return '等待设备画面';
  switch (status.state) {
    case 'connecting':
      return '正在连接本机画面';
    case 'open':
      return '已连接，等待首帧';
    case 'bootstrapping':
      return '等待关键帧，操作已禁用';
    case 'streaming':
      return '本机画面已连接';
    case 'stopped':
      return '画面已暂停';
    case 'closed':
      return status.reason === 'capture_orientation_conflict'
        ? '其他订阅使用了不同方向配置，请结束该订阅后重试'
        : '画面已断开，正在重新连接';
    case 'upstream':
      return status.upstream === 'error'
        ? '本机采集不可用，等待重试'
        : `采集状态：${status.upstream}`;
    case 'relay':
      return '本机画面不使用 relay';
  }
}

const metric = (value: number | undefined, suffix: string) =>
  value !== undefined && Number.isFinite(value) ? `${value.toFixed(1)} ${suffix}` : '未知';

/** Product view of the existing Worker pipeline. Only scalar metadata enters React state. */
export function DeviceScreen({
  store = getDeviceStore(),
  deps = defaultDeviceScreenDeps,
  inspector: injectedInspector,
  onInspect,
}: {
  store?: DeviceStore;
  deps?: DeviceScreenDeps;
  inspector?: ElementInspectorStore;
  onInspect?: () => void;
}) {
  const [ownInspector] = useState(createElementInspectorStore);
  const inspector = injectedInspector ?? ownInspector;
  const devices = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  useEffect(() => store.start(), [store]);
  const screenRef = useRef<HTMLElement>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [fullscreenError, setFullscreenError] = useState<string | null>(null);
  useEffect(() => {
    const changed = () => setFullscreen(document.fullscreenElement === screenRef.current);
    document.addEventListener('fullscreenchange', changed);
    return () => document.removeEventListener('fullscreenchange', changed);
  }, []);
  const selected = devices.devices.find((device) => device.serial === devices.selectedSerial);
  const serial = selected?.serial ?? null;
  const sessionId = selected?.connection.session_id ?? '';
  const inspectionReason = !selected
    ? null
    : devices.status !== 'ready' || selected.state !== 'device'
      ? '设备状态不可确认，旧元素仅供查看；请恢复设备连接后重新采集。'
      : selected.ready !== true || !sessionId
        ? 'Agent 尚未 ready 或缺少会话，请先在设备面板复查健康。'
        : null;
  useEffect(() => {
    inspector.setDevice(serial ? { serial, sessionId } : null, inspectionReason);
  }, [inspector, serial, sessionId, inspectionReason]);
  useEffect(() => () => inspector.setDevice(null), [inspector]);
  return (
    <section
      ref={screenRef}
      className="device-screen"
      aria-label="本机设备画面"
      data-testid="device-screen"
    >
      <div className="device-screen-select">
        <Select
          size="sm"
          aria-label="画面设备"
          placeholder="选择本机设备"
          value={devices.selectedSerial ?? ''}
          items={devices.devices.map((device) => ({
            value: device.serial,
            label: `${device.model ?? 'Android'} · ${device.serial}`,
          }))}
          onValueChange={(serial) => store.selectDevice(serial)}
          disabled={devices.status !== 'ready' || devices.devices.length === 0}
        />
        <Button
          size="sm"
          variant="ghost"
          aria-label="刷新画面设备"
          loading={devices.refreshing}
          onClick={() => void store.refresh()}
        >
          <RefreshCw aria-hidden="true" />
        </Button>
        <Button
          size="sm"
          variant="secondary"
          aria-label={fullscreen ? '退出全屏画面' : '放大画面'}
          aria-pressed={fullscreen}
          disabled={document.fullscreenEnabled === false}
          onClick={() => {
            setFullscreenError(null);
            void (async () => {
              try {
                if (document.fullscreenElement === screenRef.current)
                  await document.exitFullscreen();
                else await screenRef.current?.requestFullscreen();
              } catch {
                setFullscreenError('无法进入或退出全屏；可拖动面板分隔线调整画面大小。');
              }
            })();
          }}
        >
          {fullscreen ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
          {fullscreen ? '退出全屏' : '放大'}
        </Button>
      </div>
      {fullscreenError ? (
        <p role="alert" className="device-notice">
          {fullscreenError}
        </p>
      ) : null}
      {selected ? (
        <SelectedScreen
          key={`${selected.serial}:${selected.connection.session_id ?? 'no-session'}`}
          device={selected}
          available={devices.status === 'ready' && selected.state === 'device'}
          deps={deps}
          inspector={inspector}
          onInspect={onInspect}
        />
      ) : (
        <EmptyState
          headingLevel={3}
          title={
            devices.status === 'unavailable' || devices.status === 'error'
              ? '画面设备不可确认'
              : '选择要观看的设备'
          }
          description={
            devices.error ?? '选择明确的本机设备后连接画面；不会自动接入 Agent 或取得操作控制权。'
          }
        />
      )}
    </section>
  );
}

function SelectedScreen({
  device,
  available,
  deps,
  inspector,
  onInspect,
}: {
  device: DeviceSnapshot;
  available: boolean;
  deps: DeviceScreenDeps;
  inspector: ElementInspectorStore;
  onInspect: (() => void) | undefined;
}) {
  const [controller] = useState(() =>
    createScreenControl(
      { serial: device.serial, sessionId: device.connection.session_id },
      deps.control,
    ),
  );
  const control = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );
  const inspection = useSyncExternalStore(
    inspector.subscribe,
    inspector.getSnapshot,
    inspector.getSnapshot,
  );
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasHostRef = useRef<HTMLDivElement>(null);
  const latest = useRef<PresentedFrame | null>(null);
  const gesture = useRef<Gesture | null>(null);
  const composing = useRef(false);
  const [frame, setFrame] = useState<PresentedFrame | null>(null);
  const [frameRect, setFrameRect] = useState<Rect | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [status, setStatus] = useState<StreamStatus | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [paused, setPaused] = useState(false);
  const [mode, setMode] = useState<ScreenMode>('watch');
  const [text, setText] = useState('');
  const [gestureNotice, setGestureNotice] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    controller.start();
    const hide = () => controller.stop();
    const show = (event: PageTransitionEvent) => {
      if (event.persisted) {
        controller.start();
        setRevision((value) => value + 1);
      }
    };
    window.addEventListener('pagehide', hide);
    window.addEventListener('pageshow', show);
    return () => {
      window.removeEventListener('pagehide', hide);
      window.removeEventListener('pageshow', show);
      controller.stop();
    };
  }, [controller]);
  const helpId = useId();
  const textId = useId();
  const supported = deps.supported();
  // Locked capture has a stable natural-pixel grid; the CSS view follows the sampled display pose.
  const rotation: Rotation =
    frame?.captureOrientation === 'natural' && control.geometry
      ? viewRotation(control.geometry)
      : 0;
  const baseReason = !available
    ? '设备状态不可确认，操作已禁用。'
    : device.ready !== true
      ? 'Agent 尚未 ready，请在设备面板复查健康。'
      : controller.operationReason();
  const operationDisabled = mode !== 'operate' || baseReason !== null;
  const inspectionMapping =
    mode === 'inspect' &&
    !inspection.expired &&
    !inspection.blockedReason &&
    inspection.status === 'ready' &&
    inspection.snapshot
      ? inspectionFrameGeometry(inspection.snapshot, frame, control.geometry, rotation)
      : null;
  useEffect(() => {
    if (
      !inspection.expired &&
      inspection.snapshot?.geometry &&
      control.geometry &&
      !sameInspectionGeometry(inspection.snapshot.geometry, control.geometry)
    )
      inspector.invalidate('设备方向或显示尺寸已变化，请重新采集元素。');
  }, [inspector, inspection.expired, inspection.snapshot, control.geometry]);

  useEffect(() => {
    controller.setEnabled(mode === 'operate' && available && device.ready === true);
    gesture.current = null;
    return () => controller.setEnabled(false);
  }, [controller, mode, available, device.ready, rotation, control.owned, control.control?.epoch]);

  useEffect(() => {
    const host = canvasHostRef.current;
    if (!host || !supported || !available || paused) return;
    let alive = true;
    let lookup = 0;
    let streamConnection: RuntimeConnection | null = null;
    let previousDrops = 0;
    const view = new StreamView(deps.stream, (message) => {
      if (!alive) return;
      if (message.type === 'frame') {
        if (!sameFrameSpace(latest.current, message.frame)) {
          gesture.current = null;
          setFrame(message.frame);
        }
        latest.current = message.frame;
        controller.setFrame(message.frame, streamConnection);
        inspector.setFrameSpace(message.frame, streamConnection);
      } else if (message.type === 'metrics') {
        const drops = message.metrics.droppedWorker + message.metrics.droppedUpstream;
        setDegraded(drops > previousDrops);
        previousDrops = drops;
        setMetrics(message.metrics);
      } else if (message.type === 'status') {
        setStatus(message.status);
        if (message.status.state === 'streaming') setStreamError(null);
        if (interruptsStream(message.status)) {
          gesture.current = null;
          latest.current = null;
          controller.setFrame(null, null);
          inspector.setFrameSpace(null, null);
          setFrame(null);
          setMetrics(null);
          setDegraded(false);
        }
      } else if (message.type === 'error') {
        gesture.current = null;
        latest.current = null;
        controller.setFrame(null, null);
        inspector.setFrameSpace(null, null);
        setStreamError('画面解码失败，操作已禁用；请重连画面。');
      }
    });
    try {
      view.attach(() => {
        const canvas = document.createElement('canvas');
        canvas.className = 'device-screen-canvas';
        canvas.setAttribute('aria-hidden', 'true');
        host.replaceChildren(canvas);
        return deps.transferCanvas(canvas);
      });
      view.start(async () => {
        const attempt = ++lookup;
        const connection = await deps.control.getConnection();
        if (!alive || lookup !== attempt) return null;
        streamConnection = connection;
        return connection
          ? runtimeStreamUrl(connection, device.serial, {
              ...DEFAULT_STREAM_OPTIONS,
              captureOrientation: 'natural',
            })
          : null;
      });
    } catch {
      queueMicrotask(() => {
        if (alive) setStreamError('无法创建 Worker 画面，请检查 WebCodecs 支持后重试。');
      });
    }
    const pagehide = () => {
      gesture.current = null;
      latest.current = null;
      controller.setFrame(null, null);
      inspector.setFrameSpace(null, null);
      view.stop();
    };
    window.addEventListener('pagehide', pagehide);
    return () => {
      alive = false;
      lookup += 1;
      window.removeEventListener('pagehide', pagehide);
      gesture.current = null;
      latest.current = null;
      controller.setFrame(null, null);
      inspector.setFrameSpace(null, null);
      view.dispose();
      host.replaceChildren();
    };
  }, [available, controller, deps, device.serial, inspector, paused, revision, supported]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !frame) return;
    const resize = () => {
      gesture.current = null;
      const canvas = host.querySelector('canvas');
      if (!canvas) return;
      const rect = host.getBoundingClientRect();
      const style = getComputedStyle(host);
      const width =
        rect.width -
        (parseFloat(style.borderLeftWidth) || 0) -
        (parseFloat(style.borderRightWidth) || 0);
      const height =
        rect.height -
        (parseFloat(style.borderTopWidth) || 0) -
        (parseFloat(style.borderBottomWidth) || 0);
      const sideways = rotation === 90 || rotation === 270;
      const content = containRect(
        { width, height, left: 0, top: 0 },
        sideways ? frame.height : frame.width,
        sideways ? frame.width : frame.height,
      );
      if (!content) return;
      setFrameRect((previous) => (previous && sameRect(previous, content) ? previous : content));
      canvas.style.width = `${sideways ? content.height : content.width}px`;
      canvas.style.height = `${sideways ? content.width : content.height}px`;
      canvas.style.transform = `translate(-50%, -50%) rotate(${rotation}deg)`;
      canvas.dataset['rotation'] = String(rotation);
      canvas.dataset['session'] = frame.sessionId ?? '';
      canvas.dataset['config'] = String(frame.configId);
      canvas.dataset['width'] = String(frame.width);
      canvas.dataset['height'] = String(frame.height);
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    return () => observer.disconnect();
  }, [frame, rotation, revision]);

  useEffect(() => {
    const cancel = () => {
      gesture.current = null;
    };
    window.addEventListener('blur', cancel);
    document.addEventListener('visibilitychange', cancel);
    document.addEventListener('fullscreenchange', cancel);
    return () => {
      window.removeEventListener('blur', cancel);
      document.removeEventListener('visibilitychange', cancel);
      document.removeEventListener('fullscreenchange', cancel);
    };
  }, []);

  const rawPoint = (client: Point, rect: Rect, presented: PresentedFrame, angle: Rotation) =>
    clientToDisplay(client, rect, {
      frameWidth: angle === 90 || angle === 270 ? presented.height : presented.width,
      frameHeight: angle === 90 || angle === 270 ? presented.width : presented.height,
      displayWidth: presented.width,
      displayHeight: presented.height,
      rotation: angle,
    });

  const inspectedAt = (event: PointerEvent<HTMLDivElement>) => {
    if (mode !== 'inspect' || !inspector.isFresh() || !latest.current) return null;
    const snapshot = inspector.getSnapshot().snapshot;
    if (!snapshot) return null;
    const mapping = inspectionFrameGeometry(snapshot, latest.current, control.geometry, rotation);
    const rect = canvasRect(event.currentTarget, latest.current, rotation);
    const point =
      mapping && rect && clientToDisplay({ x: event.clientX, y: event.clientY }, rect, mapping);
    return point ? hitTestInspection(snapshot, point) : null;
  };

  const pointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (mode === 'inspect') {
      if (event.button !== 0 || event.isPrimary === false) return;
      const element = inspectedAt(event);
      if (element) {
        event.preventDefault();
        event.currentTarget.focus();
        inspector.select(element.id);
        onInspect?.();
      }
      return;
    }
    if (gesture.current && event.pointerId !== gesture.current.pointerId) {
      gesture.current = null;
      return;
    }
    if (operationDisabled || event.button !== 0 || event.isPrimary === false || !latest.current)
      return;
    const rect = canvasRect(event.currentTarget, latest.current, rotation);
    if (!rect) return;
    const client = { x: event.clientX, y: event.clientY };
    const start = rawPoint(client, rect, latest.current, rotation);
    if (!start) return;
    event.preventDefault();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.focus();
      gesture.current = {
        pointerId: event.pointerId,
        frame: latest.current,
        rotation,
        rect,
        start,
        clientStart: client,
        startedAt: performance.now(),
      };
      setGestureNotice(null);
    } catch {
      gesture.current = null;
      setGestureNotice('无法捕获指针，本次手势未发送。');
    }
  };

  const pointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const stroke = gesture.current;
    gesture.current = null;
    if (!stroke || stroke.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    const rect = canvasRect(event.currentTarget, stroke.frame, stroke.rotation);
    const end =
      rect && rawPoint({ x: event.clientX, y: event.clientY }, rect, stroke.frame, stroke.rotation);
    if (
      operationDisabled ||
      !end ||
      rotation !== stroke.rotation ||
      !sameFrameSpace(stroke.frame, latest.current) ||
      !rect ||
      !sameRect(stroke.rect, rect)
    ) {
      setGestureNotice('画面方向、布局或控制状态已变化，本次手势未发送。');
      return;
    }
    const swipe =
      Math.hypot(event.clientX - stroke.clientStart.x, event.clientY - stroke.clientStart.y) >= 6;
    void controller.gesture({
      frame: stroke.frame,
      start: stroke.start,
      end: swipe ? end : stroke.start,
      rotation: stroke.rotation,
      swipe,
      durationMs: Math.min(1000, Math.max(500, Math.round(performance.now() - stroke.startedAt))),
    });
  };

  const sendKey = (key: ScreenKey) => {
    gesture.current = null;
    if (!operationDisabled) void controller.key(key);
  };
  const keyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && document.fullscreenElement) return;
    if (mode === 'inspect') {
      if (
        event.target !== event.currentTarget ||
        event.repeat ||
        event.nativeEvent.isComposing ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        !inspectionMapping ||
        !inspector.isFresh()
      )
        return;
      const elements =
        inspection.snapshot?.elements.filter((node) => node.source === 'native') ?? [];
      const index = elements.findIndex((node) => node.id === inspection.selectedId);
      const next =
        event.key === 'ArrowDown'
          ? Math.min(elements.length - 1, index + 1)
          : event.key === 'ArrowUp'
            ? Math.max(0, index - 1)
            : event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? elements.length - 1
                : null;
      if (event.key === 'Escape') {
        event.preventDefault();
        inspector.select(null);
      } else if (next !== null || event.key === 'Enter') {
        event.preventDefault();
        const element = elements[next ?? Math.max(0, index)];
        if (element) {
          inspector.select(element.id);
          onInspect?.();
        }
      }
      return;
    }
    if (
      event.target !== event.currentTarget ||
      operationDisabled ||
      event.repeat ||
      event.nativeEvent.isComposing ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey
    )
      return;
    const key =
      event.key === 'Escape'
        ? 'BACK'
        : event.key === 'Home'
          ? 'HOME'
          : event.key === 'Enter'
            ? 'ENTER'
            : event.key === 'Backspace'
              ? 'BACKSPACE'
              : null;
    if (key) {
      event.preventDefault();
      sendKey(key);
    }
  };

  return (
    <div className="device-screen-content">
      <div className="device-screen-toolbar">
        <div className="device-screen-modes" role="group" aria-label="画面模式">
          {(['watch', 'operate', 'inspect'] as const).map((value) => (
            <Button
              key={value}
              size="sm"
              variant={mode === value ? 'primary' : 'secondary'}
              aria-pressed={mode === value}
              disabled={value === 'operate' && baseReason !== null}
              onClick={() => {
                gesture.current = null;
                setMode(value);
                if (value === 'inspect') {
                  onInspect?.();
                  void inspector.refresh();
                } else {
                  inspector.hover(null);
                  if (value === 'operate')
                    inspector.invalidate('已切换到操作模式，设备动作可能使元素快照失效。');
                }
              }}
            >
              {{ watch: '观看', operate: '操作', inspect: '检查' }[value]}
            </Button>
          ))}
        </div>
        <Button
          size="sm"
          variant="ghost"
          aria-label={paused ? '继续画面' : '暂停画面'}
          disabled={!available || !supported}
          onClick={() => {
            gesture.current = null;
            controller.setFrame(null, null);
            inspector.setFrameSpace(null, null);
            latest.current = null;
            setFrame(null);
            setPaused(!paused);
          }}
        >
          {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label="重连画面"
          disabled={!available || !supported || paused}
          onClick={() => {
            gesture.current = null;
            controller.setFrame(null, null);
            inspector.setFrameSpace(null, null);
            latest.current = null;
            setRevision((value) => value + 1);
          }}
        >
          <RefreshCw aria-hidden="true" />
        </Button>
      </div>
      <div className="device-screen-control">
        <span>
          控制者：
          {control.control?.holder_source
            ? control.owned
              ? '此窗口（manual）'
              : `${control.control.holder_source} · 其他控制会话`
            : control.status === 'ready'
              ? '无'
              : '未知'}{' '}
          · epoch {control.control?.epoch ?? '未知'}
        </span>
        {control.owned ? (
          <Button
            size="sm"
            onClick={() => {
              setMode('watch');
              void controller.release();
            }}
            loading={control.busy === 'release'}
            disabled={control.busy !== null}
          >
            释放控制权
          </Button>
        ) : (
          <Button
            size="sm"
            onClick={() => void controller.acquire()}
            loading={control.busy === 'acquire'}
            disabled={
              !available ||
              device.ready !== true ||
              control.status !== 'ready' ||
              control.control?.holder_source !== null ||
              control.busy !== null ||
              control.control?.recovery_required === true
            }
          >
            取得控制权
          </Button>
        )}
      </div>
      <p id={helpId} className="device-screen-help">
        {mode === 'inspect'
          ? `检查模式不发送设备动作；${inspectionMapping ? '悬停高亮，单击选中。方向键浏览元素，Enter 选中；也可使用元素面板。' : inspection.status === 'loading' ? '正在采集元素，请等待只读观察回执。' : '快照、显示几何或帧配置未确认，画面选取已禁用；可在元素面板查看来源并刷新。'}`
          : (baseReason ??
            (mode === 'watch'
              ? '已取得控制权。点击上方「操作」后，即可在画面上点按或拖动。'
              : '单击画面以点按，按住拖动以滑动。先点选设备输入框，再通过下方输入栏发送文本。'))}
      </p>
      <div
        ref={hostRef}
        className="device-screen-viewport"
        role="group"
        aria-label="设备画面交互区"
        aria-describedby={helpId}
        aria-disabled={mode === 'inspect' ? !inspectionMapping : operationDisabled}
        tabIndex={0}
        data-mode={mode}
        data-rotation={rotation}
        onPointerDown={pointerDown}
        onPointerMove={(event) => {
          if (mode === 'inspect') inspector.hover(inspectedAt(event)?.id ?? null);
        }}
        onPointerLeave={() => inspector.hover(null)}
        onPointerUp={pointerUp}
        onPointerCancel={() => {
          gesture.current = null;
        }}
        onLostPointerCapture={() => {
          gesture.current = null;
        }}
        onBlur={() => {
          gesture.current = null;
          inspector.hover(null);
        }}
        onKeyDown={keyboard}
      >
        <div ref={canvasHostRef} className="device-screen-canvas-host" aria-hidden="true" />
        {inspectionMapping && frameRect && inspection.snapshot
          ? [
              { id: inspection.selectedId, hovered: false },
              {
                id: inspection.hoveredId !== inspection.selectedId ? inspection.hoveredId : null,
                hovered: true,
              },
            ].map(({ id, hovered }) => {
              const node = id ? inspection.snapshot?.byId.get(id) : null;
              const rect =
                node?.source === 'native' && node.bounds
                  ? displayBoundsToClient(node.bounds, frameRect, inspectionMapping)
                  : null;
              return rect ? (
                <div
                  key={`${hovered}:${id}`}
                  aria-hidden="true"
                  className="device-inspection-box"
                  data-hovered={hovered}
                  data-testid={hovered ? 'inspection-hover' : 'inspection-selection'}
                  style={rect}
                >
                  <span>{node?.label}</span>
                </div>
              ) : null;
            })
          : null}
      </div>
      <div className="device-screen-metrics">
        <span>
          {frame
            ? `${frame.width}×${frame.height} · ${control.geometry ? `${control.geometry.rotation}°` : '方向未知'}`
            : '分辨率未知'}
        </span>
        <span>{metric(metrics?.fps, 'fps')}</span>
        <span>
          延迟{' '}
          {metrics && Number.isFinite(metrics.e2eMsP50) ? metric(metrics.e2eMsP50, 'ms') : '未校准'}
        </span>
        {degraded ? <Badge tone="warning">画面降级：丢帧</Badge> : null}
      </div>
      <p className="device-screen-note" role="status">
        {!supported
          ? '当前环境不支持 Worker / WebCodecs，无法显示画面。'
          : !available
            ? '设备不可用，画面与操作已禁用。'
            : paused
              ? '画面已暂停；仅停止本窗口的订阅，不停止 Runtime。'
              : streamDescription(status)}
      </p>
      {!supported || mode !== 'operate' ? null : (
        <form
          className="device-screen-input"
          aria-label="设备键盘输入"
          onSubmit={(event) => {
            event.preventDefault();
            if (operationDisabled || composing.current) return;
            const submitted = text;
            void controller.input(submitted).then((confirmed) => {
              if (confirmed) setText((current) => (current === submitted ? '' : current));
            });
          }}
        >
          <label htmlFor={textId}>输入到当前焦点字段</label>
          <Input
            id={textId}
            size="sm"
            value={text}
            disabled={operationDisabled}
            onChange={(event) => setText(event.target.value)}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            aria-describedby={helpId}
          />
          <Button
            type="submit"
            size="sm"
            disabled={operationDisabled}
            loading={control.busy === 'action'}
          >
            输入文本
          </Button>
          <Button
            size="sm"
            aria-label="设备返回"
            disabled={operationDisabled}
            onClick={() => sendKey('BACK')}
          >
            <ArrowLeft aria-hidden="true" />
          </Button>
          <Button
            size="sm"
            aria-label="设备主屏幕"
            disabled={operationDisabled}
            onClick={() => sendKey('HOME')}
          >
            <Home aria-hidden="true" />
          </Button>
        </form>
      )}
      <details className="device-screen-details">
        <summary>操作说明与画面指标</summary>
        <p className="device-screen-note">
          画面聚焦时 Escape 返回、Home 回到桌面；全屏时 Escape 优先退出全屏。
          观看与检查模式不会发送设备动作；不支持长按、多指手势或滚轮操作。
        </p>
        <p className="device-screen-note">
          动作需要可确认的原生节点；树不可用、敏感字段或控制状态变化时不会发送。 本机直连的延迟为
          PTS 到 Worker 绘制提交的估算，不代表光学上屏。
        </p>
      </details>
      <div className="device-screen-feedback">
        {gestureNotice ? (
          <p className="device-screen-note" role="status">
            {gestureNotice}
          </p>
        ) : null}
        {control.notice ? (
          <p className="device-screen-note" role="status">
            {control.notice}
          </p>
        ) : null}
        {streamError || control.error ? (
          <p className="device-notice" role="alert">
            {streamError ?? control.error}
          </p>
        ) : null}
      </div>
    </div>
  );
}
