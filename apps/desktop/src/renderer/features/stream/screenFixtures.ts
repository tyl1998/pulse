import type { PresentedFrame } from '../../shared/stream/workerProtocol';
import type { DisplayGeometry, ScreenActionBody } from './screenControl';

/** Synthetic UI evidence only; these helpers never call Runtime, ADB or Agent. */
export const SCREEN_SERIAL = 'SIM-ANDROID-A';
export const SCREEN_SESSION = '00000000-0000-4000-8000-000000000001';
export const SCREEN_GEOMETRY: DisplayGeometry = {
  display_id: 0,
  width: 1080,
  height: 2400,
  rotation: 0,
};
export const SCREEN_FRAME = {
  sessionId: '0123456789abcdef',
  configId: 1,
  seq: 1,
  width: 720,
  height: 1600,
  drawnAtMs: 1000,
  captureOrientation: 'natural',
} satisfies PresentedFrame;
export const SCREEN_TREE_REASONS = [
  'native_semantics_only_visual_coverage_unverified',
  'native_display_geometry_unknown',
  'non_atomic_snapshot',
  'rotation_unverified',
  'clock_uncalibrated',
];

export const simulatedScreenControl = (runId: string | null = null, epoch = 7) => ({
  device_id: SCREEN_SERIAL,
  session_id: SCREEN_SESSION,
  epoch,
  holder_source: runId ? 'manual' : null,
  run_id: runId,
  recovery_required: false,
  recovery_action_id: null,
});

export function simulatedNativeObservation(requestId: string, geometry = SCREEN_GEOMETRY) {
  const node = (
    path: number[],
    bounds: { left: number; top: number; right: number; bottom: number },
    editable = false,
  ) => ({
    node_path: path,
    source: 'accessibility',
    package_name: 'com.pulse.target',
    class_name: editable ? 'android.widget.EditText' : 'android.view.View',
    text: null,
    text_redacted: false,
    view_id: null,
    content_description: null,
    bounds,
    states: { enabled: true, visible: true, editable, focused: editable, clickable: true },
  });
  return {
    serial: SCREEN_SERIAL,
    session_id: SCREEN_SESSION,
    request_id: requestId,
    status: 'completed',
    success: true,
    error: null,
    revision_start: 42,
    revision_end: 42,
    framing: { validated: true, source_data_validated: true },
    action_context: {
      status: 'available',
      source: 'adb_input_viewport',
      geometry,
      atomic_with_observation: false,
    },
    sources: {
      tree: {
        status: 'available',
        completeness: { status: 'partial', reasons: [...SCREEN_TREE_REASONS] },
        coordinates: { display_id: 0, coordinate_space: 'display_pixels' },
        data: {
          window_id: 7,
          display_id: 0,
          coordinate_space: 'display_pixels',
          coverage: 'exposed_accessibility_nodes_only',
          nodes: [
            node([], { left: 0, top: 0, right: geometry.width, bottom: geometry.height }),
            node([0], { left: 100, top: 400, right: 980, bottom: 800 }, true),
            node([1], { left: 100, top: 1000, right: 980, bottom: 1400 }),
          ],
        },
      },
    },
  };
}

export const simulatedActionReceipt = (body: ScreenActionBody, status = 'completed') => ({
  contract: 'pulse-runtime-action-draft',
  serial: body.serial,
  session_id: body.session_id,
  request_id: body.request_id,
  operation_id: body.operation_id,
  kind: body.kind,
  status,
  action_completed: status === 'completed',
  automatic_replay: false,
  business_success: null,
  framing: { validated: true },
  ledger: { state: status === 'completed' ? 'verified' : status },
  control: { source: body.source, epoch: body.control_epoch, run_id: body.run_id },
  verification: { status: 'available', business_status: 'not_evaluated' },
});
