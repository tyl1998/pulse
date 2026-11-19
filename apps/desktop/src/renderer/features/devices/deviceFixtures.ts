/** Synthetic snapshots shared by isolated previews and tests; never connect to a device. */
export const DEVICE_PREVIEW_TIME = Date.UTC(2026, 10, 16, 3, 0, 0);

export function simulatedDevice(serial = 'SIM-ANDROID-A', overrides: Record<string, unknown> = {}) {
  return {
    serial,
    state: 'device',
    status: 'ready',
    ready: true,
    model: '模拟 Android',
    transport_id: '7',
    android_api: 34,
    android_user: 0,
    observed_at_ms: DEVICE_PREVIEW_TIME,
    agent: {
      package: 'com.pulse.agent',
      installed: true,
      version_name: '0.1.0',
      version_code: 1,
      debuggable: true,
      build_verified: true,
      protocol_version: 1,
    },
    connection: {
      session_id: '00000000-0000-4000-8000-000000000001',
      agent_lifecycle_id: '00000000-0000-4000-8000-000000000002',
      health_authenticated: true,
      loopback_verified: true,
      agent_pid: 123,
    },
    capabilities: {
      accessibility: 'available',
      screenshot: 'available',
      ocr: 'unavailable',
      actions: ['tap', 'swipe', 'set_text', 'node_click'],
      platform_operation_pending: false,
      recovery_required: false,
      minimum_android_api: 30,
      display_scope: 'default_only',
      ocr_engine: 'mlkit_chinese_bundled',
      ocr_languages: ['zh', 'en'],
    },
    diagnostic: {
      code: null,
      guidance: null,
      manual_required: false,
      last_invalidation: null,
      cleanup_pending: false,
      native_cleanup_guard: false,
      operation_id: null,
      awaiting_authorization: false,
      authorization_reason: null,
    },
    control: { recovery_required: false },
    ...overrides,
  };
}

export const simulatedTargetApp = (serial = 'SIM-ANDROID-A') => ({
  serial,
  package: 'com.pulse.target',
  status: 'installed',
  version_name: '1.0',
  version_code: 1,
  observed_at_ms: DEVICE_PREVIEW_TIME,
  diagnostic: { code: 'target_app_installed', guidance: null },
});
