import type { PulseRuntimeApiSchemas } from '../../shared/api/runtime.generated';
import type { DisplayGeometry } from '../stream/screenControl';
import type { ProjectState } from '../projects/projectStore';
import controlFlowCase from '../../../../../../fixtures/golden-cases/v1-draft/cases/control-flow.json';
import {
  SCREEN_GEOMETRY,
  SCREEN_SERIAL,
  SCREEN_SESSION,
  SCREEN_TREE_REASONS,
  simulatedNativeObservation,
} from '../stream/screenFixtures';

/** Source-local synthetic evidence for stories/tests only. No Runtime, Agent or device calls. */
export function simulatedInspectorSource(
  key: 'tree' | 'screenshot' | 'ocr',
  requested: boolean,
): PulseRuntimeApiSchemas.LocateObservationSource {
  const bitmap = key !== 'tree';
  return {
    source:
      key === 'tree'
        ? 'accessibility'
        : key === 'ocr'
          ? 'mlkit_chinese'
          : 'accessibility_screenshot',
    requested,
    capability: 'available',
    status: requested ? 'available' : 'unavailable',
    error: requested ? null : { code: 'not_requested' },
    completeness: {
      status: requested ? 'partial' : 'unavailable',
      reasons: requested
        ? key === 'tree'
          ? [...SCREEN_TREE_REASONS]
          : ['ocr_text_only_no_native_states', 'rotation_unverified', 'non_atomic_snapshot']
        : ['not_requested'],
    },
    time: {
      clock: 'android_uptime_ms',
      calibrated: false,
      started_at_uptime_ms: requested ? 900 : null,
      finished_at_uptime_ms: requested ? 920 : null,
    },
    scope: {
      kind: bitmap ? 'default_display' : 'active_window',
      display_id: requested ? 0 : null,
      window_id: !bitmap && requested ? 7 : null,
      target_app_verified: null,
    },
    coordinates: {
      coordinate_space: bitmap ? 'screenshot_pixels' : 'display_pixels',
      requested_display_id: 0,
      display_id: requested ? 0 : null,
      width: bitmap && requested ? 720 : null,
      height: bitmap && requested ? 1600 : null,
      original_width: bitmap && requested ? 1080 : null,
      original_height: bitmap && requested ? 2400 : null,
      scale_x: bitmap && requested ? 2 / 3 : null,
      scale_y: bitmap && requested ? 2 / 3 : null,
      rotation: null,
      ...(bitmap
        ? { to_display: { status: 'unavailable' as const, error: { code: 'rotation_unverified' } } }
        : {}),
    },
  };
}

export function simulatedInspectorObservation(
  requestId: string,
  geometry: DisplayGeometry = SCREEN_GEOMETRY,
  { ocr = false, count = 0, duplicateText = false } = {},
) {
  const observation = simulatedNativeObservation(requestId, geometry);
  let nodes = observation.sources.tree.data.nodes.map((node, index) => ({
    ...node,
    class_name: index === 0 ? 'android.view.ViewGroup' : node.class_name,
    view_id: `com.pulse.target:id/${index === 0 ? 'form' : index === 1 ? 'order_number' : 'submit'}`,
    text: index === 0 ? null : index === 1 ? '订单号' : '提交订单',
    content_description: index === 0 ? '订单表单' : index === 1 ? '订单编号' : '确认提交',
  }));
  const root = nodes[0];
  const button = nodes[2];
  if (root && count > 0) {
    nodes = [root];
    for (let group = 0; group < 8 && nodes.length < count; group++) {
      nodes.push({
        ...root,
        node_path: [group],
        view_id: `com.pulse.target:id/group_${group}`,
        text: `group-${group}`,
      });
      for (let child = 0; child < 511 && nodes.length < count; child++) {
        nodes.push({
          ...root,
          node_path: [group, child],
          view_id: `com.pulse.target:id/node_${nodes.length}`,
          text: `node-${nodes.length}`,
        });
      }
    }
  } else if (button && duplicateText)
    nodes.push({
      ...button,
      node_path: [2],
      view_id: 'com.pulse.target:id/secondary_submit',
      content_description: '另一个提交',
      bounds: { left: 100, top: 1600, right: 980, bottom: 1900 },
    });
  return {
    ...observation,
    atomic_snapshot: false,
    sources: {
      tree: {
        ...simulatedInspectorSource('tree', true),
        data: { ...observation.sources.tree.data, nodes },
      },
      screenshot: { ...simulatedInspectorSource('screenshot', false), data: null },
      ocr: {
        ...simulatedInspectorSource('ocr', ocr),
        data: ocr
          ? {
              display_id: 0,
              coordinate_space: 'screenshot_pixels',
              width: 720,
              height: 1600,
              original_width: 1080,
              original_height: 2400,
              scale_x: 2 / 3,
              scale_y: 2 / 3,
              captured_at_uptime_ms: 910,
              protected_content: 'not_verified',
              blocks: [
                {
                  text: '总计 42.00',
                  bounds: { left: 40, top: 1100, right: 480, bottom: 1180 },
                  confidence: null,
                },
              ],
            }
          : null,
      },
    },
  };
}

export function simulatedLocateResult(
  body: PulseRuntimeApiSchemas.LocateRequest,
  status: PulseRuntimeApiSchemas.LocateResult['status'] = 'resolved',
): PulseRuntimeApiSchemas.LocateResult {
  if (!('locator' in body.target)) throw new Error('Synthetic inspector only uses inline locators');
  const candidate = body.target.locator.candidates[0];
  const ocr = candidate.by === 'ocr_text';
  const matchCount = status === 'resolved' ? 1 : status === 'ambiguous' ? 2 : 0;
  const error =
    status === 'ambiguous'
      ? { code: 'ambiguous_target' }
      : status === 'indeterminate'
        ? { code: 'source_incomplete' }
        : null;
  const states: PulseRuntimeApiSchemas.LocateStates = {
    visible: ocr ? null : true,
    enabled: ocr ? null : true,
    clickable: ocr ? null : true,
    checked: null,
    checkable: ocr ? null : false,
    editable: ocr ? null : false,
    focused: ocr ? null : false,
    selected: ocr ? null : false,
    long_clickable: null,
    password: null,
    scrollable: null,
    showing_hint_text: null,
  };
  const bounds = { left: 100, top: 1000, right: 980, bottom: 1400 };
  return {
    contract: 'pulse-runtime-locator-draft',
    serial: body.serial,
    session_id: body.session_id,
    request_id: body.request_id,
    observation_id: body.request_id,
    status,
    error,
    candidate_index: 0,
    candidate_by: candidate.by,
    match_count: matchCount,
    query_complete: status === 'resolved' ? true : null,
    matches: Array.from({ length: matchCount }, (_, index) => ({
      source: ocr ? 'ocr' : 'tree',
      coordinate_space: ocr ? 'screenshot_pixels' : 'display_pixels',
      display_id: 0,
      bounds,
      node_path: ocr ? null : [index + 1],
      block_index: ocr ? index : null,
      score: null,
      states,
      visible: true,
      visibility_evidence: ocr ? 'bitmap_bounds' : 'native_state',
    })),
    attempts: [{ index: 0, by: candidate.by, status, match_count: matchCount, error }],
    point: null,
    automatic_replay: false,
    business_success: null,
    action_target:
      status === 'resolved' && !ocr
        ? {
            source: 'tree',
            coordinate_space: 'display_pixels',
            display_id: 0,
            expected_package: body.expected_package,
            node_path: [1],
            observation_id: body.request_id,
          }
        : null,
    action_target_error: ocr ? { code: 'coordinate_mapping_unavailable' } : null,
    observation: {
      status: 'completed',
      error: null,
      atomic_snapshot: false,
      revision_start: 42,
      revision_end: 42,
      agent_lifecycle_id: '00000000-0000-4000-8000-000000000002',
      requested: { tree: true, ocr, screenshot: false },
      requested_display_id: 0,
      completeness: { status: 'partial', reasons: [...SCREEN_TREE_REASONS] },
      framing: {
        protocol_version: 1,
        eof_received: true,
        terminal_received: true,
        validated: true,
        source_data_validated: true,
      },
      runtime_time: { started_at_epoch_ms: 1000, finished_at_epoch_ms: 1010, duration_ms: 10 },
      action_context: {
        source: 'adb_input_viewport',
        status: 'available',
        geometry: SCREEN_GEOMETRY,
        atomic_with_observation: false,
        error: null,
      },
      sources: {
        tree: simulatedInspectorSource('tree', true),
        screenshot: simulatedInspectorSource('screenshot', false),
        ocr: simulatedInspectorSource('ocr', ocr),
      },
    },
  };
}

export const INSPECTOR_DEVICE = { serial: SCREEN_SERIAL, sessionId: SCREEN_SESSION };
export const INSPECTOR_CONNECTION = {
  port: 4311,
  pid: 9,
  token: 'synthetic-ui-only',
  version: '1.0.0',
};

export const simulatedInspectorCase = () => ({
  ...controlFlowCase,
  name: '订单表单检查用例',
  goal: '合成用例草稿，仅创作，不执行设备动作。',
});

export function simulatedInspectorProjectState(): ProjectState {
  return {
    version: 1,
    status: 'ready',
    scope: { root: '/synthetic/fe-p27', projectId: 'inspector-preview' },
    project: {
      root: '/synthetic/fe-p27',
      project_id: 'inspector-preview',
      name: '合成检查工程',
      revision: '1',
      allowed_apps: ['com.pulse.target'],
      case_ids: ['control-flow'],
      data_ids: [],
      default_strategy: 'rule_only',
      entry_cases: ['control-flow'],
      flow_ids: [],
      manifest: {},
      platforms: ['android'],
      profile_ids: ['android-fixture'],
      required_capabilities: [],
    },
    cases: {},
    selectedCaseId: 'control-flow',
    busy: null,
    dialogOpen: false,
    error: null,
    notice: null,
  };
}
