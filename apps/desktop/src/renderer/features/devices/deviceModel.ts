import type { Tone } from '../../shared/ui';
import type { PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';

export interface DeviceSnapshot {
  serial: string;
  state: string;
  status: string;
  ready: boolean | null;
  model: string | null;
  transport_id: string | null;
  android_api: number | null;
  android_user: number | null;
  observed_at_ms: number | null;
  operation_error: string | null;
  agent: {
    package: string | null;
    installed: boolean | null;
    version_name: string | null;
    version_code: number | null;
    debuggable: boolean | null;
    build_verified: boolean | null;
    protocol_version: number | null;
  };
  connection: {
    session_id: string | null;
    agent_lifecycle_id: string | null;
    health_authenticated: boolean | null;
    loopback_verified: boolean | null;
    agent_pid: number | null;
  };
  capabilities: Record<string, unknown> | null;
  diagnostic: {
    code: string | null;
    manual_required: boolean | null;
    guidance: string | null;
    last_invalidation: string | null;
    cleanup_pending: boolean | null;
    native_cleanup_guard: boolean | null;
    operation_id: string | null;
    awaiting_authorization: boolean | null;
    authorization_reason: string | null;
  };
  recovery_required: boolean | null;
}

export type TargetAppCheck = PulseRuntimeApiSchemas.TargetAppResponse;

export const asRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const text = (value: unknown): string | null =>
  typeof value === 'string' && value.trim() ? value : null;

export const booleanFact = (value: unknown): boolean | null =>
  typeof value === 'boolean' ? value : null;

export const integerFact = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;

/** Device endpoints predate typed OpenAPI responses. Only retain known, non-secret facts. */
export function parseDeviceSnapshot(value: unknown): DeviceSnapshot {
  const raw = asRecord(value);
  const serial = text(raw?.['serial']);
  const state = text(raw?.['state']);
  const status = text(raw?.['status']);
  if (!raw || !serial || !state || !status) throw new Error('invalid_device_snapshot');
  const agent = asRecord(raw['agent']);
  const connection = asRecord(raw['connection']);
  const diagnostic = asRecord(raw['diagnostic']);
  return {
    serial,
    state,
    status,
    ready: booleanFact(raw['ready']),
    model: text(raw['model']),
    transport_id: text(raw['transport_id']),
    android_api: integerFact(raw['android_api']),
    android_user: integerFact(raw['android_user']),
    observed_at_ms: integerFact(raw['observed_at_ms']),
    operation_error: text(raw['operation_error']),
    agent: {
      package: text(agent?.['package']),
      installed: booleanFact(agent?.['installed']),
      version_name: text(agent?.['version_name']),
      version_code: integerFact(agent?.['version_code']),
      debuggable: booleanFact(agent?.['debuggable']),
      build_verified: booleanFact(agent?.['build_verified']),
      protocol_version: integerFact(agent?.['protocol_version']),
    },
    connection: {
      session_id: text(connection?.['session_id']),
      agent_lifecycle_id: text(connection?.['agent_lifecycle_id']),
      health_authenticated: booleanFact(connection?.['health_authenticated']),
      loopback_verified: booleanFact(connection?.['loopback_verified']),
      agent_pid: integerFact(connection?.['agent_pid']),
    },
    capabilities: parseCapabilities(raw['capabilities']),
    diagnostic: {
      code: text(diagnostic?.['code']),
      manual_required: booleanFact(diagnostic?.['manual_required']),
      guidance: text(diagnostic?.['guidance']),
      last_invalidation: text(diagnostic?.['last_invalidation']),
      cleanup_pending: booleanFact(diagnostic?.['cleanup_pending']),
      native_cleanup_guard: booleanFact(diagnostic?.['native_cleanup_guard']),
      operation_id: text(diagnostic?.['operation_id']),
      awaiting_authorization: booleanFact(diagnostic?.['awaiting_authorization']),
      authorization_reason: text(diagnostic?.['authorization_reason']),
    },
    recovery_required: booleanFact(asRecord(raw['control'])?.['recovery_required']),
  };
}

function parseCapabilities(value: unknown): Record<string, unknown> | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const strings = (input: unknown): string[] | null =>
    Array.isArray(input) && input.every((item) => typeof item === 'string') ? [...input] : null;
  return {
    accessibility: text(raw['accessibility']),
    screenshot: text(raw['screenshot']),
    ocr: text(raw['ocr']),
    actions: strings(raw['actions']),
    platform_operation_pending: booleanFact(raw['platform_operation_pending']),
    recovery_required: booleanFact(raw['recovery_required']),
    minimum_android_api: integerFact(raw['minimum_android_api']),
    display_scope: text(raw['display_scope']),
    ocr_engine: text(raw['ocr_engine']),
    ocr_languages: strings(raw['ocr_languages']),
  };
}

export function parseDeviceList(value: unknown): {
  devices: DeviceSnapshot[];
  error: string | null;
} {
  const raw = asRecord(value);
  if (!raw || !Array.isArray(raw['devices'])) throw new Error('invalid_device_list');
  const devices = raw['devices'].map(parseDeviceSnapshot);
  if (new Set(devices.map((device) => device.serial)).size !== devices.length)
    throw new Error('duplicate_device_serial');
  if (raw['error'] !== undefined && raw['error'] !== null && !text(raw['error']))
    throw new Error('invalid_device_list_error');
  return { devices, error: text(raw['error']) };
}

export function parseTargetAppCheck(value: unknown, serial: string): TargetAppCheck {
  const raw = asRecord(value);
  const status = raw?.['status'];
  if (
    !raw ||
    raw['serial'] !== serial ||
    raw['package'] !== 'com.pulse.target' ||
    (status !== 'installed' &&
      status !== 'missing' &&
      status !== 'unavailable' &&
      status !== 'failed')
  )
    throw new Error('invalid_target_app_check');
  const diagnostic = asRecord(raw['diagnostic']);
  return {
    serial,
    package: 'com.pulse.target',
    status,
    version_name: text(raw['version_name']),
    version_code: integerFact(raw['version_code']),
    observed_at_ms: integerFact(raw['observed_at_ms']),
    diagnostic: {
      code: text(diagnostic?.['code']) ?? 'unknown',
      guidance: text(diagnostic?.['guidance']),
    },
  };
}

const DEVICE_STATUSES: Record<string, { label: string; tone: Tone }> = {
  ready: { label: 'Agent 状态 ready', tone: 'info' },
  offline: { label: 'ADB 离线', tone: 'warning' },
  disconnected: { label: '设备已断开', tone: 'neutral' },
  adb_unauthorized: { label: 'USB 调试未授权', tone: 'warning' },
  unsupported_android_api: { label: 'Android API 不受支持', tone: 'warning' },
  unsupported_configuration: { label: '配置不受支持', tone: 'warning' },
  agent_missing: { label: 'Agent 未安装', tone: 'warning' },
  agent_incompatible: { label: 'Agent 不兼容', tone: 'warning' },
  agent_not_connected: { label: 'Agent 未连接', tone: 'neutral' },
  agent_available: { label: 'Agent 可接入（尚无会话）', tone: 'neutral' },
  accessibility_disabled: { label: '无障碍未开启', tone: 'warning' },
  permission_revoked: { label: '无障碍权限已撤销', tone: 'warning' },
  manual_authorization_required: { label: '等待人工授权', tone: 'pending' },
  configuration_required: { label: '需要配置', tone: 'warning' },
  configuration_in_use: { label: '配置由其他连接占用', tone: 'warning' },
  agent_busy: { label: 'Agent 原生操作忙碌', tone: 'pending' },
  recovery_required: { label: '需要人工恢复', tone: 'warning' },
  cleanup_pending: { label: '等待清理回执', tone: 'warning' },
  connecting: { label: 'Runtime 正在连接', tone: 'pending' },
  preparing: { label: 'Runtime 正在准备', tone: 'pending' },
  installing_agent: { label: 'Runtime 正在安装 Agent', tone: 'pending' },
  connection_failed: { label: '连接失败', tone: 'warning' },
  connection_timeout: { label: '连接超时', tone: 'warning' },
  cancelled: { label: '连接操作已取消', tone: 'neutral' },
};

export const devicePresentation = (status: string): { label: string; tone: Tone } =>
  (Object.hasOwn(DEVICE_STATUSES, status) ? DEVICE_STATUSES[status] : undefined) ?? {
    label: `未知状态（${status}）`,
    tone: 'neutral',
  };

export const describeBoolean = (value: unknown): string =>
  booleanFact(value) === true ? '是' : booleanFact(value) === false ? '否' : '未知';

export const describeCapability = (value: unknown): string =>
  value === 'available' ? '可用' : value === 'unavailable' ? '不可用' : '未知';

export function describeList(value: unknown): string {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) return '未知';
  return value.length ? value.join('、') : '未声明支持项';
}
