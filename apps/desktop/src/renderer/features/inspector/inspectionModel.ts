import type { Locator, LocatorCandidate } from '@pulse/schema';
import type { FrameGeometry, PixelBounds, Point, Rotation } from '../../shared/lib/coords';
import type { PresentedFrame } from '../../shared/stream/workerProtocol';
import { asRecord } from '../devices/deviceModel';
import type { DisplayGeometry } from '../stream/screenControl';

export const INSPECTION_MAX_AGE_MS = 15_000;
export const MAX_INSPECTION_NODES = 4096;
export const PACKAGE_NAME = /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/;

export interface InspectionDevice {
  serial: string;
  sessionId: string;
}

export interface InspectionSource {
  source: string;
  requested: boolean;
  status: 'available' | 'unavailable' | 'failed' | 'unknown';
  errorCode: string | null;
  coordinateSpace: 'display_pixels' | 'screenshot_pixels';
  completeness: string;
  reasons: string[];
  startedAt: number | null;
  finishedAt: number | null;
  width: number | null;
  height: number | null;
}

export interface InspectedElement {
  id: string;
  source: 'native' | 'ocr';
  coordinateSpace: 'display_pixels' | 'screenshot_pixels';
  parentId: string | null;
  path: number[] | null;
  depth: number;
  hasChildren: boolean;
  label: string;
  searchText: string;
  packageName: string | null;
  className: string | null;
  resId: string | null;
  text: string | null;
  description: string | null;
  textRedacted: boolean | null;
  bounds: PixelBounds | null;
  states: Record<string, boolean | null>;
}

export interface InspectionSnapshot {
  requestId: string;
  revisionStart: number | null;
  revisionEnd: number | null;
  receivedAt: number;
  geometry: DisplayGeometry | null;
  sources: { tree: InspectionSource; ocr: InspectionSource };
  elements: InspectedElement[];
  byId: ReadonlyMap<string, InspectedElement>;
  expectedPackage: string;
  changedDuringCapture: boolean;
}

export interface InspectorCandidate {
  key: string;
  locator: LocatorCandidate;
  enabled: boolean;
  snapshotMatches: number;
  fragile: boolean;
}

const text = (value: unknown): string | null =>
  typeof value === 'string' && value.length <= 1024 && !/[\uD800-\uDFFF]/u.test(value)
    ? value
    : null;
const integer = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
const boolean = (value: unknown): boolean | null => (typeof value === 'boolean' ? value : null);
const nativeId = (path: number[]) => `native:${path.join('.') || 'root'}`;

function parseBounds(value: unknown): PixelBounds | null {
  const bounds = asRecord(value);
  if (!bounds) return null;
  const left = integer(bounds['left']);
  const top = integer(bounds['top']);
  const right = integer(bounds['right']);
  const bottom = integer(bounds['bottom']);
  return left !== null &&
    top !== null &&
    right !== null &&
    bottom !== null &&
    left <= right &&
    top <= bottom
    ? { left, top, right, bottom }
    : null;
}

function parseSource(value: unknown, key: 'tree' | 'ocr'): InspectionSource {
  const source = asRecord(value);
  const completeness = asRecord(source?.['completeness']);
  const time = asRecord(source?.['time']);
  const coordinates = asRecord(source?.['coordinates']);
  const status = source?.['status'];
  const reasons = completeness?.['reasons'];
  return {
    source: text(source?.['source']) ?? (key === 'tree' ? 'accessibility' : 'mlkit_chinese'),
    requested: source?.['requested'] === true,
    status:
      status === 'available' || status === 'unavailable' || status === 'failed'
        ? status
        : 'unknown',
    errorCode: text(asRecord(source?.['error'])?.['code']),
    coordinateSpace: key === 'tree' ? 'display_pixels' : 'screenshot_pixels',
    completeness: text(completeness?.['status']) ?? 'unknown',
    reasons: Array.isArray(reasons)
      ? reasons.slice(0, 64).flatMap((reason: unknown) => text(reason) ?? [])
      : [],
    startedAt: integer(time?.['started_at_uptime_ms']),
    finishedAt: integer(time?.['finished_at_uptime_ms']),
    width: integer(coordinates?.['width']),
    height: integer(coordinates?.['height']),
  };
}

export function parseInspectionGeometry(
  value: unknown,
  device: InspectionDevice,
): DisplayGeometry | null {
  const response = asRecord(value);
  const geometry = asRecord(response?.['geometry']);
  if (
    response?.['serial'] !== device.serial ||
    response['session_id'] !== device.sessionId ||
    response['status'] !== 'available' ||
    response['source'] !== 'adb_input_viewport' ||
    response['atomic_with_observation'] !== false ||
    geometry?.['display_id'] !== 0 ||
    ![0, 90, 180, 270].includes(geometry['rotation'] as number)
  )
    return null;
  const width = integer(geometry['width']);
  const height = integer(geometry['height']);
  return width !== null &&
    height !== null &&
    width > 0 &&
    height > 0 &&
    width <= 8192 &&
    height <= 8192
    ? { display_id: 0, width, height, rotation: geometry['rotation'] as Rotation }
    : null;
}

export const sameInspectionGeometry = (a: DisplayGeometry | null, b: DisplayGeometry | null) =>
  a !== null &&
  b !== null &&
  a.display_id === b.display_id &&
  a.width === b.width &&
  a.height === b.height &&
  a.rotation === b.rotation;

/** Consume source-local metadata only. Screenshot bytes never enter the inspector store. */
export function parseInspection(
  value: unknown,
  device: InspectionDevice,
  requestId: string,
  receivedAt: number,
  geometry: DisplayGeometry | null,
): InspectionSnapshot {
  const observation = asRecord(value);
  const framing = asRecord(observation?.['framing']);
  if (
    observation?.['serial'] !== device.serial ||
    observation['session_id'] !== device.sessionId ||
    observation['request_id'] !== requestId ||
    framing?.['validated'] !== true ||
    framing['source_data_validated'] !== true
  )
    throw new Error('观察身份或数据校验未确认，未使用该元素快照。');
  const sources = asRecord(observation['sources']);
  const tree = parseSource(sources?.['tree'], 'tree');
  const ocr = parseSource(sources?.['ocr'], 'ocr');
  const elements: InspectedElement[] = [];
  const byId = new Map<string, InspectedElement>();
  if (tree.status === 'available') {
    const rawTree = asRecord(sources?.['tree']);
    const coordinates = asRecord(rawTree?.['coordinates']);
    const data = asRecord(rawTree?.['data']);
    const nodes = data?.['nodes'];
    if (
      coordinates?.['coordinate_space'] !== 'display_pixels' ||
      coordinates['display_id'] !== 0 ||
      data?.['coordinate_space'] !== 'display_pixels' ||
      data['display_id'] !== 0 ||
      !Array.isArray(nodes) ||
      nodes.length > MAX_INSPECTION_NODES
    ) {
      throw new Error('原生树坐标或节点结构无效，未使用该元素快照。');
    }
    for (const value of nodes) {
      const node = asRecord(value);
      const path = node?.['node_path'];
      const states = asRecord(node?.['states']);
      const bounds = parseBounds(node?.['bounds']);
      if (
        node?.['source'] !== 'accessibility' ||
        !Array.isArray(path) ||
        path.length > 32 ||
        path.some(
          (index: unknown) =>
            integer(index) === null || (index as number) < 0 || (index as number) > 511,
        ) ||
        !states ||
        !bounds
      ) {
        throw new Error('原生节点结构无效，未使用该元素快照。');
      }
      const indices = path as number[];
      const id = nativeId(indices);
      const parentId = indices.length ? nativeId(indices.slice(0, -1)) : null;
      if (byId.has(id) || (parentId !== null && !byId.has(parentId)))
        throw new Error('原生节点路径重复或缺少父节点。');
      const redacted = boolean(node['text_redacted']);
      const nodeText = redacted === false ? text(node['text']) : null;
      const description = redacted === false ? text(node['content_description']) : null;
      const resId = text(node['view_id']);
      const className = text(node['class_name']);
      const packageName = text(node['package_name']);
      const label =
        nodeText ||
        description ||
        resId?.split('/').at(-1) ||
        className?.split('.').at(-1) ||
        '未命名节点';
      const element: InspectedElement = {
        id,
        source: 'native',
        coordinateSpace: 'display_pixels',
        parentId,
        path: indices,
        depth: indices.length,
        hasChildren: false,
        label,
        searchText: [label, resId, className, packageName, nodeText, description, indices.join('.')]
          .join(' ')
          .toLocaleLowerCase(),
        packageName,
        className,
        resId,
        text: nodeText,
        description,
        textRedacted: redacted,
        bounds,
        states: Object.fromEntries(
          [
            'enabled',
            'visible',
            'checked',
            'checkable',
            'clickable',
            'editable',
            'focused',
            'selected',
          ].map((key) => [key, boolean(states[key])]),
        ),
      };
      elements.push(element);
      byId.set(id, element);
      if (parentId !== null) {
        const parent = byId.get(parentId);
        if (parent) parent.hasChildren = true;
      }
    }
    elements.sort((a, b) => {
      const ap = a.path ?? [];
      const bp = b.path ?? [];
      for (let index = 0; index < Math.min(ap.length, bp.length); index++) {
        const difference = (ap[index] ?? 0) - (bp[index] ?? 0);
        if (difference) return difference;
      }
      return ap.length - bp.length;
    });
  }
  if (ocr.status === 'available') {
    const raw = asRecord(sources?.['ocr']);
    const coordinates = asRecord(raw?.['coordinates']);
    const data = asRecord(raw?.['data']);
    const blocks = data?.['blocks'];
    if (
      coordinates?.['coordinate_space'] !== 'screenshot_pixels' ||
      coordinates['display_id'] !== 0 ||
      data?.['coordinate_space'] !== 'screenshot_pixels' ||
      data['display_id'] !== 0 ||
      !Array.isArray(blocks) ||
      blocks.length > 256
    )
      throw new Error('OCR 坐标或文本块结构无效。');
    for (const [index, value] of blocks.entries()) {
      const block = asRecord(value);
      const blockText = text(block?.['text']);
      if (blockText === null) throw new Error('OCR 文本块结构无效。');
      const element: InspectedElement = {
        id: `ocr:${index}`,
        source: 'ocr',
        coordinateSpace: 'screenshot_pixels',
        parentId: null,
        path: null,
        depth: 0,
        hasChildren: false,
        label: blockText || '空文本块',
        searchText: `ocr ${blockText}`.toLocaleLowerCase(),
        packageName: null,
        className: null,
        resId: null,
        text: blockText,
        description: null,
        textRedacted: null,
        bounds: parseBounds(block?.['bounds']),
        states: { enabled: null, visible: null, checked: null, clickable: null },
      };
      elements.push(element);
      byId.set(element.id, element);
    }
  }
  const root = byId.get(nativeId([]));
  const revisionStart = integer(observation['revision_start']);
  const revisionEnd = integer(observation['revision_end']);
  return {
    requestId,
    revisionStart,
    revisionEnd,
    receivedAt,
    geometry,
    sources: { tree, ocr },
    elements,
    byId,
    expectedPackage:
      root?.packageName && PACKAGE_NAME.test(root.packageName) ? root.packageName : '',
    changedDuringCapture:
      (revisionStart !== null && revisionEnd !== null && revisionStart !== revisionEnd) ||
      [...tree.reasons, ...ocr.reasons].includes('page_changed_during_capture'),
  };
}

export function generateLocatorCandidates(
  element: InspectedElement,
  snapshot: InspectionSnapshot,
): InspectorCandidate[] {
  const candidates: InspectorCandidate[] = [];
  const add = (by: LocatorCandidate['by'], value: string | null, fragile = false) => {
    if (!value) return;
    const field = by === 'res_id' ? 'resId' : by === 'desc' ? 'description' : 'text';
    candidates.push({
      key: `${by}:${value}`,
      locator: { by, value },
      enabled: true,
      fragile,
      snapshotMatches:
        by === 'xpath'
          ? 1
          : snapshot.elements.filter(
              (node) => node.source === element.source && node[field] === value,
            ).length,
    });
  };
  if (element.source === 'ocr') add('ocr_text', element.text);
  else {
    add('res_id', element.resId);
    add('desc', element.description);
    add('text', element.text);
    // @index uses the native child index, not position among a possibly partial list of children.
    if (element.path && element.path.length < 32)
      add(
        'xpath',
        `/node${element.path.map((index) => `/node[@index='${index}']`).join('')}`,
        true,
      );
  }
  return candidates;
}

export function inspectorLocator(
  candidates: readonly InspectorCandidate[],
  visible: boolean,
): Locator | null {
  const enabled = candidates
    .filter((candidate) => candidate.enabled)
    .map((candidate) => candidate.locator);
  const first = enabled[0];
  return first
    ? { candidates: [first, ...enabled.slice(1)], expect: { unique: true, visible } }
    : null;
}

export function hitTestInspection(
  snapshot: InspectionSnapshot,
  point: Point,
): InspectedElement | null {
  return (
    snapshot.elements
      .filter(
        (node) =>
          node.source === 'native' &&
          node.states['visible'] !== false &&
          node.bounds &&
          node.bounds.left <= point.x &&
          point.x < node.bounds.right &&
          node.bounds.top <= point.y &&
          point.y < node.bounds.bottom,
      )
      .sort(
        (a, b) =>
          b.depth - a.depth ||
          ((a.bounds?.right ?? 0) - (a.bounds?.left ?? 0)) *
            ((a.bounds?.bottom ?? 0) - (a.bounds?.top ?? 0)) -
            ((b.bounds?.right ?? 0) - (b.bounds?.left ?? 0)) *
              ((b.bounds?.bottom ?? 0) - (b.bounds?.top ?? 0)),
      )[0] ?? null
  );
}

export function inspectionFrameGeometry(
  snapshot: InspectionSnapshot,
  frame: PresentedFrame | null,
  current: DisplayGeometry | null,
  viewRotation: Rotation,
): FrameGeometry | null {
  const geometry = snapshot.geometry;
  if (
    !frame ||
    frame.captureOrientation !== 'natural' ||
    !sameInspectionGeometry(geometry, current) ||
    !geometry ||
    snapshot.changedDuringCapture
  )
    return null;
  const sideways = geometry.rotation === 90 || geometry.rotation === 270;
  const naturalWidth = sideways ? geometry.height : geometry.width;
  const naturalHeight = sideways ? geometry.width : geometry.height;
  const scale = Math.min(frame.width / naturalWidth, frame.height / naturalHeight);
  if (
    !Number.isFinite(scale) ||
    scale <= 0 ||
    Math.abs(frame.width - naturalWidth * scale) > 8 ||
    Math.abs(frame.height - naturalHeight * scale) > 8
  )
    return null;
  const viewSideways = viewRotation === 90 || viewRotation === 270;
  return {
    frameWidth: viewSideways ? frame.height : frame.width,
    frameHeight: viewSideways ? frame.width : frame.height,
    displayWidth: geometry.width,
    displayHeight: geometry.height,
    rotation: ((geometry.rotation + viewRotation) % 360) as Rotation,
  };
}

export function visibleInspectionElements(
  snapshot: InspectionSnapshot,
  search: string,
  collapsed: ReadonlySet<string>,
): InspectedElement[] {
  const query = search.trim().toLocaleLowerCase();
  let included: Set<string> | null = null;
  if (query) {
    included = new Set();
    for (const node of snapshot.elements) {
      if (!node.searchText.includes(query)) continue;
      let current: InspectedElement | undefined = node;
      while (current && !included.has(current.id)) {
        included.add(current.id);
        current = current.parentId ? snapshot.byId.get(current.parentId) : undefined;
      }
    }
  }
  const visible: InspectedElement[] = [];
  let hiddenDepth: number | null = null;
  for (const node of snapshot.elements) {
    if (included && !included.has(node.id)) continue;
    if (hiddenDepth !== null && node.depth > hiddenDepth) continue;
    hiddenDepth = !query && collapsed.has(node.id) ? node.depth : null;
    visible.push(node);
  }
  return visible;
}
