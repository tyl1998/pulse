import { useEffect, useId, useSyncExternalStore, type ReactNode } from 'react';
import { Activity, RefreshCw, ShieldCheck, Smartphone, Usb } from 'lucide-react';
import { Badge, Button, EmptyState, StatusPill } from '../../shared/ui';
import {
  describeBoolean,
  describeCapability,
  describeList,
  devicePresentation,
  integerFact,
  type DeviceSnapshot,
} from './deviceModel';
import { getDeviceStore, type DeviceStore } from './deviceStore';

interface DevicePanelProps {
  store?: DeviceStore;
  source?: string;
  now?: number;
}

const CAPABILITY_ROWS = [
  ['accessibility', '无障碍'],
  ['screenshot', '截图'],
  ['ocr', 'OCR'],
] as const;

function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="device-facts">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value ?? '未知'}</dd>
        </div>
      ))}
    </dl>
  );
}

function DeviceStatus({
  device,
  source,
  now,
}: {
  device: DeviceSnapshot;
  source: string;
  now?: number;
}) {
  const presentation = devicePresentation(device.status);
  return (
    <StatusPill
      value={`${presentation.label} · ready=${device.ready === null ? 'unknown' : String(device.ready)}`}
      tone={presentation.tone}
      icon={<Activity />}
      source={source}
      observedAt={device.observed_at_ms}
      {...(now !== undefined ? { now } : {})}
    />
  );
}

export function DevicePanel({
  store = getDeviceStore(),
  source = '本机 Runtime 设备快照',
  now,
}: DevicePanelProps) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const descriptionId = useId();
  useEffect(() => store.start(), [store]);
  const selected = state.devices.find((device) => device.serial === state.selectedSerial);
  const current = state.status === 'ready';
  const disabled = !current || state.refreshing || state.busy !== null;
  const capabilities = selected?.capabilities;
  const connectReason = !selected
    ? null
    : selected.connection.session_id
      ? '已有 Runtime 会话，使用“复查健康”更新事实，不重复接入。'
      : selected.state !== 'device'
        ? 'ADB 未授权在线；接入入口不可用，先在设备端处理连接问题。'
        : selected.recovery_required === true || selected.diagnostic.cleanup_pending === true
          ? '存在恢复要求或待清理资源，面板不会绕过 Runtime 的阻断。'
          : null;
  const target = state.targetCheck;

  return (
    <section className="device-panel" aria-label="本机设备面板" data-testid="device-panel">
      <div className="device-toolbar">
        <div>
          <h2>本机 Android 设备</h2>
          <p>每 5 秒读取 Runtime 快照；仅查看本机设备，不加入公共设备池。</p>
        </div>
        <Button
          onClick={() => void store.refresh()}
          loading={state.refreshing}
          disabled={state.busy !== null}
        >
          <RefreshCw aria-hidden="true" />
          刷新设备列表
        </Button>
      </div>

      {state.status === 'loading' && state.devices.length === 0 ? (
        <p role="status">正在读取本机 Runtime 设备列表…</p>
      ) : null}
      {state.error ? (
        <p className="device-notice" role="alert">
          {state.error} {state.devices.length ? '保留最近快照，不代表当前仍然可用。' : ''}
        </p>
      ) : null}
      {state.actionError ? (
        <p className="device-notice" role="alert">
          {state.actionError}
        </p>
      ) : null}
      {current && state.devices.length === 0 ? (
        <EmptyState
          headingLevel={3}
          icon={<Usb />}
          title="未发现本机设备"
          description="连接已授权 USB 调试的 Android 设备后刷新。已通过 ADB 连接的网络设备也会列出；这里不执行配对或 ADB 连接命令。"
        />
      ) : null}
      {!current && state.status !== 'loading' && state.devices.length === 0 ? (
        <EmptyState
          headingLevel={3}
          title="设备状态不可确认"
          description="本机 Runtime 或设备发现不可用不等于设备列表为空。检查 Runtime 后重新刷新；不需要团队服务端。"
        />
      ) : null}

      {state.devices.length ? (
        <div className="device-workspace">
          <ul className="device-list" aria-label="Runtime 设备列表">
            {state.devices.map((device) => (
              <li key={device.serial}>
                <button
                  type="button"
                  className="device-list-button"
                  aria-label={`查看设备 ${device.model ?? 'Android'} ${device.serial}`}
                  aria-describedby={`${descriptionId}-${device.serial}-adb ${descriptionId}-${device.serial}-status`}
                  aria-pressed={device.serial === state.selectedSerial}
                  onClick={() => store.selectDevice(device.serial)}
                >
                  <span className="device-list-name">
                    <Smartphone aria-hidden="true" />
                    <strong>{device.model ?? '型号未知'}</strong>
                    <Badge id={`${descriptionId}-${device.serial}-adb`} tone="neutral">
                      ADB：{device.state}
                    </Badge>
                  </span>
                  <code>{device.serial}</code>
                  <span id={`${descriptionId}-${device.serial}-status`}>
                    <DeviceStatus
                      device={device}
                      source={source}
                      {...(now !== undefined ? { now } : {})}
                    />
                  </span>
                </button>
              </li>
            ))}
          </ul>

          {selected ? (
            <section className="device-detail" aria-label="选中设备详情">
              <header className="device-detail-header">
                <div>
                  <h3>{selected.model ?? 'Android 设备'}</h3>
                  <code>{selected.serial}</code>
                </div>
                <Button
                  size="sm"
                  onClick={() => void store.inspectSelected()}
                  loading={state.busy === 'inspect'}
                  disabled={disabled}
                >
                  <ShieldCheck aria-hidden="true" />
                  复查健康
                </Button>
              </header>
              {!current ? <p className="device-notice">以下为上次快照，当前健康未确认。</p> : null}
              <DeviceStatus
                device={selected}
                source={source}
                {...(now !== undefined ? { now } : {})}
              />

              <section className="device-section" aria-label="设备与 Agent 健康">
                <h4>设备与 Agent 健康</h4>
                <Facts
                  items={[
                    ['ADB 状态', selected.state],
                    [
                      'Android API / 用户',
                      `${selected.android_api ?? '未知'} / ${selected.android_user ?? '未知'}`,
                    ],
                    ['Transport ID', selected.transport_id],
                    ['Agent 包', selected.agent.package],
                    [
                      'Agent 安装字段',
                      `${describeBoolean(selected.agent.installed)}${selected.state !== 'device' ? '（设备不可检查，不代表已卸载）' : ''}`,
                    ],
                    [
                      'Agent 版本 / 版本号',
                      `${selected.agent.version_name ?? '未知'} / ${selected.agent.version_code ?? '未知'}`,
                    ],
                    [
                      'Debug / 精确构建验证',
                      `${describeBoolean(selected.agent.debuggable)} / ${describeBoolean(selected.agent.build_verified)}`,
                    ],
                    ['协议版本（支持策略）', selected.agent.protocol_version],
                    ['Health 已鉴权', describeBoolean(selected.connection.health_authenticated)],
                    ['Loopback 已验证', describeBoolean(selected.connection.loopback_verified)],
                    ['Runtime 会话', <code>{selected.connection.session_id ?? '未建立'}</code>],
                    [
                      'Agent 生命周期',
                      <code>{selected.connection.agent_lifecycle_id ?? '未知'}</code>,
                    ],
                    ['Agent PID', selected.connection.agent_pid],
                    ['控制门要求恢复', describeBoolean(selected.recovery_required)],
                  ]}
                />
                <p className="device-note">
                  协议支持策略、ADB
                  在线和鉴权健康是不同事实；它们不代表页面采集或用例已通过，也不代表已取得操作控制权。
                </p>
                <div className="device-connect">
                  <Button
                    size="sm"
                    onClick={() => void store.connectSelected()}
                    loading={state.busy === 'connect'}
                    disabled={disabled || connectReason !== null}
                  >
                    接入既有 Agent
                  </Button>
                  <p>
                    {connectReason ??
                      '验证既有服务并创建本 Runtime 的 forward / 会话，不安装、不授权、不改 pin、不重启 Agent。'}
                  </p>
                </div>
              </section>

              <section className="device-section" aria-label="Agent 能力声明">
                <h4>Agent 能力声明</h4>
                <div className="device-capabilities">
                  {CAPABILITY_ROWS.map(([key, label]) => (
                    <div key={key}>
                      <strong>{label}</strong>
                      <StatusPill
                        value={describeCapability(capabilities?.[key])}
                        source={`${source}中的 Agent health 声明`}
                        observedAt={selected.observed_at_ms}
                        tone={capabilities?.[key] === 'available' ? 'info' : 'neutral'}
                        {...(now !== undefined ? { now } : {})}
                      />
                    </div>
                  ))}
                </div>
                <Facts
                  items={[
                    ['Agent 动作声明', describeList(capabilities?.['actions'])],
                    [
                      'OCR 语言 / 引擎',
                      `${describeList(capabilities?.['ocr_languages'])} / ${typeof capabilities?.['ocr_engine'] === 'string' ? capabilities['ocr_engine'] : '未知'}`,
                    ],
                    [
                      '显示范围',
                      capabilities?.['display_scope'] === 'default_only' ? '仅默认显示器' : '未知',
                    ],
                    ['最低 Android API', integerFact(capabilities?.['minimum_android_api'])],
                    [
                      '原生操作 Pending',
                      describeBoolean(capabilities?.['platform_operation_pending']),
                    ],
                    ['Agent 要求恢复', describeBoolean(capabilities?.['recovery_required'])],
                  ]}
                />
                <p className="device-note">
                  未获取的能力保持“未知”。这里是能力声明与快照采样时间，不是本次树、截图或 OCR
                  结果；视频能力不从 Agent 截图状态推断。
                </p>
              </section>

              <section className="device-section" aria-label="Runtime 连接诊断">
                <h4>连接诊断</h4>
                <Facts
                  items={[
                    ['诊断码', selected.diagnostic.code ?? '未提供'],
                    ['要求人工处理', describeBoolean(selected.diagnostic.manual_required)],
                    ['上次失效原因', selected.diagnostic.last_invalidation ?? '未提供'],
                    [
                      '等待清理 / 原生清理保护',
                      `${describeBoolean(selected.diagnostic.cleanup_pending)} / ${describeBoolean(selected.diagnostic.native_cleanup_guard)}`,
                    ],
                    ['等待人工授权', describeBoolean(selected.diagnostic.awaiting_authorization)],
                    ['授权原因', selected.diagnostic.authorization_reason ?? '未提供'],
                    ['操作 ID', <code>{selected.diagnostic.operation_id ?? '未提供'}</code>],
                  ]}
                />
                {selected.diagnostic.guidance ? (
                  <p className="device-guidance">Runtime 指引：{selected.diagnostic.guidance}</p>
                ) : null}
              </section>

              <section className="device-section" aria-label="靶场安装检查">
                <div className="device-section-heading">
                  <h4>靶场安装检查</h4>
                  <Button
                    size="sm"
                    onClick={() => void store.checkTargetApp()}
                    loading={state.busy === 'target'}
                    disabled={disabled || selected.state !== 'device'}
                  >
                    检查靶场安装
                  </Button>
                </div>
                <p className="device-note">
                  仅检查固定应用 com.pulse.target
                  的安装信息，不启动应用、不执行用例、不安装或重置数据。
                </p>
                {target ? (
                  <>
                    <StatusPill
                      value={
                        {
                          installed: '靶场已安装',
                          missing: '靶场未安装',
                          unavailable: '靶场安装不可确认',
                          failed: '靶场安装检查失败',
                        }[target.status]
                      }
                      source="本机 Runtime 靶场安装检查"
                      observedAt={target.observed_at_ms}
                      tone={target.status === 'installed' ? 'info' : 'warning'}
                      {...(now !== undefined ? { now } : {})}
                    />
                    <Facts
                      items={[
                        [
                          '靶场版本 / 版本号',
                          `${target.version_name ?? '未知'} / ${target.version_code ?? '未知'}`,
                        ],
                        ['检查诊断码', target.diagnostic.code],
                      ]}
                    />
                    {target.diagnostic.guidance ? (
                      <p className="device-guidance">{target.diagnostic.guidance}</p>
                    ) : null}
                  </>
                ) : (
                  <p className="device-note">
                    尚未检查；Agent 健康和包存在都不证明靶场用例能通过。
                  </p>
                )}
              </section>
            </section>
          ) : (
            <EmptyState
              headingLevel={3}
              title="选择要查看的设备"
              description="设备详情与只读检查绑定明确的 serial，不自动选择或切换到另一台设备。"
            />
          )}
        </div>
      ) : null}
      <p className="device-note">
        当前 Runtime 只提供 Android 设备。连接类型、电量、温度和 iOS
        能力未提供，面板不推断这些信息。
      </p>
    </section>
  );
}
