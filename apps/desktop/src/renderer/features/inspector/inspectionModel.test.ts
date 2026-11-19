import { expect, test } from 'vitest';
import { clientToDisplay, displayBoundsToClient, type Rotation } from '../../shared/lib/coords';
import { SCREEN_FRAME, SCREEN_GEOMETRY } from '../stream/screenFixtures';
import { viewRotation } from '../stream/screenControl';
import { INSPECTOR_DEVICE, simulatedInspectorObservation } from './inspectorFixtures';
import {
  generateLocatorCandidates,
  hitTestInspection,
  inspectionFrameGeometry,
  inspectorLocator,
  MAX_INSPECTION_NODES,
  parseInspection,
  parseInspectionGeometry,
  visibleInspectionElements,
} from './inspectionModel';

const requestId = '00000000-0000-4000-8000-000000000010';
const snapshot = () =>
  parseInspection(
    simulatedInspectorObservation(requestId, SCREEN_GEOMETRY, { duplicateText: true, ocr: true }),
    INSPECTOR_DEVICE,
    requestId,
    1000,
    SCREEN_GEOMETRY,
  );

test('keeps source, time, partial coverage, redaction and unknown state evidence separate', () => {
  const result = snapshot();
  expect(result.sources.tree).toMatchObject({
    source: 'accessibility',
    status: 'available',
    coordinateSpace: 'display_pixels',
    completeness: 'partial',
    startedAt: 900,
    finishedAt: 920,
  });
  expect(result.sources.ocr.coordinateSpace).toBe('screenshot_pixels');
  expect(result.byId.get('ocr:0')?.states).toEqual({
    enabled: null,
    visible: null,
    checked: null,
    clickable: null,
  });
  expect(result.expectedPackage).toBe('com.pulse.target');
  expect(result.elements.map((node) => node.id)).toEqual([
    'native:root',
    'native:0',
    'native:1',
    'native:2',
    'ocr:0',
  ]);
  expect(result.receivedAt).toBe(1000);
  expect(result.sources.tree.reasons).toContain('non_atomic_snapshot');
});

test.each([true, null, 'unverified'] as const)(
  'does not expose native text or desc with redaction %s',
  (redacted) => {
    const raw = simulatedInspectorObservation(requestId);
    const node = raw.sources.tree.data.nodes[1];
    if (!node) throw new Error('missing fixture node');
    Object.assign(node, {
      text_redacted: redacted,
      text: 'secret-value',
      content_description: 'secret-description',
    });
    const result = parseInspection(raw, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY);
    const selected = result.byId.get('native:0');
    if (!selected) throw new Error('missing selected node');
    expect(selected.text).toBeNull();
    expect(selected.description).toBeNull();
    expect(selected.searchText).not.toContain('secret');
    expect(
      generateLocatorCandidates(selected, result).map((candidate) => candidate.locator.by),
    ).toEqual(['res_id', 'xpath']);
    expect(JSON.stringify(result.elements)).not.toContain('secret');
  },
);

test.each(['serial', 'session_id', 'request_id'] as const)(
  'rejects mismatched observation %s',
  (key) => {
    const raw = { ...simulatedInspectorObservation(requestId), [key]: 'foreign' };
    expect(() => parseInspection(raw, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY)).toThrow(
      /身份/,
    );
  },
);

test('rejects unvalidated framing, duplicate paths, absent parents and mixed coordinate spaces', () => {
  const unvalidated = simulatedInspectorObservation(requestId);
  unvalidated.framing.source_data_validated = false;
  expect(() =>
    parseInspection(unvalidated, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY),
  ).toThrow(/校验/);
  const duplicate = simulatedInspectorObservation(requestId);
  const first = duplicate.sources.tree.data.nodes[0];
  if (!first) throw new Error('missing root');
  duplicate.sources.tree.data.nodes.push(first);
  expect(() =>
    parseInspection(duplicate, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY),
  ).toThrow(/重复/);
  const missing = simulatedInspectorObservation(requestId);
  missing.sources.tree.data.nodes.splice(0, 1);
  expect(() =>
    parseInspection(missing, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY),
  ).toThrow(/父节点/);
  const wrongSpace = simulatedInspectorObservation(requestId);
  wrongSpace.sources.tree.coordinates.coordinate_space = 'screenshot_pixels';
  expect(() =>
    parseInspection(wrongSpace, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY),
  ).toThrow(/坐标/);
});

test('bounds input size and keeps failed collection distinct from successful empty evidence', () => {
  const raw = simulatedInspectorObservation(requestId, SCREEN_GEOMETRY, {
    count: MAX_INSPECTION_NODES,
  });
  const first = raw.sources.tree.data.nodes[0];
  if (!first) throw new Error('missing root');
  raw.sources.tree.data.nodes.push(first);
  expect(() => parseInspection(raw, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY)).toThrow(
    /结构/,
  );
  const failed = simulatedInspectorObservation(requestId);
  failed.sources.tree.status = 'unavailable';
  failed.sources.tree.error = { code: 'accessibility_unavailable' };
  const result = parseInspection(failed, INSPECTOR_DEVICE, requestId, 1000, null);
  expect(result.elements).toEqual([]);
  expect(result.sources.tree.status).toBe('unavailable');
  expect(result.sources.tree.errorCode).toBe('accessibility_unavailable');
});

test('generates ordered portable candidates and preserves explicit uniqueness/visibility', () => {
  const result = snapshot();
  const button = result.byId.get('native:1');
  if (!button) throw new Error('missing button');
  const candidates = generateLocatorCandidates(button, result);
  expect(candidates.map((candidate) => candidate.locator.by)).toEqual([
    'res_id',
    'desc',
    'text',
    'xpath',
  ]);
  expect(candidates.find((candidate) => candidate.locator.by === 'text')?.snapshotMatches).toBe(2);
  expect(candidates.at(-1)?.locator.value).toBe("/node/node[@index='1']");
  const selected = [candidates[2], candidates[0]].filter((candidate) => !!candidate);
  expect(inspectorLocator(selected, false)).toMatchObject({
    candidates: [{ by: 'text' }, { by: 'res_id' }],
    expect: { unique: true, visible: false },
  });
  expect(
    inspectorLocator(
      candidates.map((candidate) => ({ ...candidate, enabled: false })),
      true,
    ),
  ).toBeNull();
  const ocr = result.byId.get('ocr:0');
  if (!ocr) throw new Error('missing OCR');
  expect(generateLocatorCandidates(ocr, result).map((candidate) => candidate.locator)).toEqual([
    { by: 'ocr_text', value: '总计 42.00' },
  ]);
});

test('hit testing selects the deepest native node without promoting visibility or enabled state', () => {
  const result = snapshot();
  const input = result.byId.get('native:0');
  if (!input) throw new Error('missing input');
  input.states['visible'] = null;
  input.states['enabled'] = false;
  expect(hitTestInspection(result, { x: 400, y: 500 })?.id).toBe('native:0');
  expect(input.states['visible']).toBeNull();
  expect(hitTestInspection(result, { x: -1, y: 500 })).toBeNull();
  expect(hitTestInspection(result, { x: 400, y: 1200 })?.source).toBe('native');
});

test.each([0, 90, 180, 270] as Rotation[])(
  'maps native bounds through natural frames and the %s degree view',
  (rotation) => {
    const sideways = rotation === 90 || rotation === 270;
    const geometry = {
      ...SCREEN_GEOMETRY,
      rotation,
      width: sideways ? 2400 : 1080,
      height: sideways ? 1080 : 2400,
    };
    const result = parseInspection(
      simulatedInspectorObservation(requestId, geometry),
      INSPECTOR_DEVICE,
      requestId,
      1000,
      geometry,
    );
    const mapping = inspectionFrameGeometry(result, SCREEN_FRAME, geometry, viewRotation(geometry));
    expect(mapping).not.toBeNull();
    if (!mapping) throw new Error('no mapping');
    const rect = { left: 100, top: 50, width: 400, height: 400 };
    const bounds = { left: 100, top: 400, right: 900, bottom: 800 };
    const box = displayBoundsToClient(bounds, rect, mapping);
    expect(box).not.toBeNull();
    if (!box) throw new Error('no box');
    const center = clientToDisplay(
      { x: box.left + box.width / 2, y: box.top + box.height / 2 },
      rect,
      mapping,
    );
    expect(center).toEqual({ x: 500, y: 600 });
    expect(clientToDisplay({ x: 101, y: 51 }, rect, mapping)).toBeNull();
  },
);

test('rejects unverified pose, geometry mismatch, capture profiles and page changes', () => {
  const result = snapshot();
  expect(inspectionFrameGeometry(result, SCREEN_FRAME, null, 0)).toBeNull();
  expect(
    inspectionFrameGeometry(result, SCREEN_FRAME, { ...SCREEN_GEOMETRY, rotation: 90 }, 0),
  ).toBeNull();
  expect(
    inspectionFrameGeometry(
      result,
      { ...SCREEN_FRAME, captureOrientation: null },
      SCREEN_GEOMETRY,
      0,
    ),
  ).toBeNull();
  expect(
    inspectionFrameGeometry(result, { ...SCREEN_FRAME, width: 1500 }, SCREEN_GEOMETRY, 0),
  ).toBeNull();
  const changed = simulatedInspectorObservation(requestId);
  changed.revision_end = 43;
  const changing = parseInspection(changed, INSPECTOR_DEVICE, requestId, 1000, SCREEN_GEOMETRY);
  expect(changing.changedDuringCapture).toBe(true);
  expect(inspectionFrameGeometry(changing, SCREEN_FRAME, SCREEN_GEOMETRY, 0)).toBeNull();
  expect(
    parseInspectionGeometry(
      {
        serial: INSPECTOR_DEVICE.serial,
        session_id: INSPECTOR_DEVICE.sessionId,
        source: 'adb_input_viewport',
        status: 'available',
        atomic_with_observation: false,
        geometry: SCREEN_GEOMETRY,
      },
      INSPECTOR_DEVICE,
    ),
  ).toEqual(SCREEN_GEOMETRY);
  expect(parseInspectionGeometry({ geometry: SCREEN_GEOMETRY }, INSPECTOR_DEVICE)).toBeNull();
});

test('search preserves ancestors, overrides collapse and handles 3000 nodes within the model budget', () => {
  const started = performance.now();
  const result = parseInspection(
    simulatedInspectorObservation(requestId, SCREEN_GEOMETRY, { count: 3000 }),
    INSPECTOR_DEVICE,
    requestId,
    1000,
    SCREEN_GEOMETRY,
  );
  expect(visibleInspectionElements(result, '', new Set()).length).toBe(3000);
  expect(visibleInspectionElements(result, '', new Set(['native:root'])).length).toBe(1);
  const found = visibleInspectionElements(result, 'node-2999', new Set(['native:root']));
  expect(found).toHaveLength(3);
  expect(found.at(-1)?.label).toBe('node-2999');
  expect(performance.now() - started).toBeLessThan(100);
});
