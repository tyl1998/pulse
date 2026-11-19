import { useMemo, useSyncExternalStore } from 'react';
import type { Layout } from 'react-resizable-panels';

export const BOTTOM_TAB_IDS = ['run', 'progress', 'events', 'variables', 'ai', 'issues'] as const;
export type BottomTabId = (typeof BOTTOM_TAB_IDS)[number];

export const WORKBENCH_PRESETS = {
  edit: {
    label: '编辑',
    description: '编辑与投屏并排，可放大设备画面',
    horizontal: { outline: 18, editor: 42, right: 40 },
    right: { device: 100, elements: 0 },
    vertical: { workspace: 94, bottom: 6 },
    bottomTab: 'issues',
  },
  record: {
    label: '录制',
    description: '设备画面与事件流优先',
    horizontal: { outline: 16, editor: 32, right: 52 },
    right: { device: 100, elements: 0 },
    vertical: { workspace: 94, bottom: 6 },
    bottomTab: 'events',
  },
  debug: {
    label: '调试',
    description: '变量与步骤进度优先',
    horizontal: { outline: 18, editor: 38, right: 44 },
    right: { device: 100, elements: 0 },
    vertical: { workspace: 84, bottom: 16 },
    bottomTab: 'variables',
  },
} as const satisfies Record<
  string,
  {
    label: string;
    description: string;
    horizontal: Layout;
    right: Layout;
    vertical: Layout;
    bottomTab: BottomTabId;
  }
>;

export type PresetId = keyof typeof WORKBENCH_PRESETS;
export const PRESET_IDS = Object.keys(WORKBENCH_PRESETS) as PresetId[];

export interface WorkbenchPresetLayout {
  horizontal: Layout;
  right: Layout;
  vertical: Layout;
  bottomTab: BottomTabId;
}

export interface WorkbenchLayoutState {
  version: 1;
  activePreset: PresetId;
  presets: Record<PresetId, WorkbenchPresetLayout>;
}

export interface LayoutScope {
  projectId?: string;
  userId?: string;
}

export const LAYOUT_STORAGE_PREFIX = 'pulse:workbench-layout:v1';

export function layoutStorageKey(scope: LayoutScope = {}): string {
  const project = encodeURIComponent(scope.projectId?.trim() || 'local');
  const user = encodeURIComponent(scope.userId?.trim() || 'local');
  return `${LAYOUT_STORAGE_PREFIX}:${project}:${user}`;
}

const cloneLayout = (layout: Layout): Layout => ({ ...layout });

export function defaultWorkbenchLayout(): WorkbenchLayoutState {
  const presets = {} as Record<PresetId, WorkbenchPresetLayout>;
  for (const id of PRESET_IDS) {
    const preset = WORKBENCH_PRESETS[id];
    presets[id] = {
      horizontal: cloneLayout(preset.horizontal),
      right: cloneLayout(preset.right),
      vertical: cloneLayout(preset.vertical),
      bottomTab: preset.bottomTab,
    };
  }
  return { version: 1, activePreset: 'edit', presets };
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function normalizeLayout(value: unknown, fallback: Layout): Layout {
  if (!isRecord(value)) return cloneLayout(fallback);
  const keys = Object.keys(fallback);
  const result: Layout = {};
  let total = 0;
  for (const key of keys) {
    const size = value[key];
    if (typeof size !== 'number' || !Number.isFinite(size) || size < 0 || size > 100) {
      return cloneLayout(fallback);
    }
    result[key] = size;
    total += size;
  }
  // Only sized keys count; a layout whose expected panels do not add up is corrupt.
  return Math.abs(total - 100) <= 1 ? result : cloneLayout(fallback);
}

export function isBottomTabId(value: unknown): value is BottomTabId {
  return typeof value === 'string' && (BOTTOM_TAB_IDS as readonly string[]).includes(value);
}

function isPresetId(value: unknown): value is PresetId {
  return typeof value === 'string' && (PRESET_IDS as readonly string[]).includes(value);
}

function normalizePreset(value: unknown, fallback: WorkbenchPresetLayout): WorkbenchPresetLayout {
  if (!isRecord(value)) {
    return {
      horizontal: cloneLayout(fallback.horizontal),
      right: cloneLayout(fallback.right),
      vertical: cloneLayout(fallback.vertical),
      bottomTab: fallback.bottomTab,
    };
  }
  return {
    horizontal: normalizeLayout(value['horizontal'], fallback.horizontal),
    right: normalizeLayout(value['right'], fallback.right),
    vertical: normalizeLayout(value['vertical'], fallback.vertical),
    bottomTab: isBottomTabId(value['bottomTab']) ? value['bottomTab'] : fallback.bottomTab,
  };
}

export function parseWorkbenchLayout(raw: string | null): WorkbenchLayoutState {
  const fallback = defaultWorkbenchLayout();
  if (!raw) return fallback;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return fallback;
  }
  if (!isRecord(parsed) || parsed['version'] !== 1) return fallback;
  const fallbackPresets = fallback.presets;
  const storedPresets = parsed['presets'];
  const presets = {} as Record<PresetId, WorkbenchPresetLayout>;
  for (const id of PRESET_IDS) {
    const stored = isRecord(storedPresets) ? storedPresets[id] : undefined;
    presets[id] = normalizePreset(stored, fallbackPresets[id]);
  }
  return {
    version: 1,
    activePreset: isPresetId(parsed['activePreset']) ? parsed['activePreset'] : 'edit',
    presets,
  };
}

function resolveStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

export interface WorkbenchLayoutStore {
  getSnapshot: () => WorkbenchLayoutState;
  subscribe: (listener: () => void) => () => void;
  setActivePreset: (id: PresetId) => void;
  setBottomTab: (id: BottomTabId) => void;
  updateGroup: (group: 'horizontal' | 'right' | 'vertical', layout: Layout) => void;
  resetActivePreset: () => void;
}

export function createWorkbenchLayoutStore(
  storageKey: string,
  storage: Storage | null = resolveStorage(),
): WorkbenchLayoutStore {
  let state = parseWorkbenchLayout(storage?.getItem(storageKey) ?? null);
  const listeners = new Set<() => void>();

  const persist = () => {
    try {
      storage?.setItem(storageKey, JSON.stringify(state));
    } catch {
      // Persistence is best-effort; an unavailable quota must not break the workspace.
    }
  };

  const update = (next: WorkbenchLayoutState) => {
    state = next;
    persist();
    for (const listener of listeners) listener();
  };

  const updatePreset = (id: PresetId, change: Partial<WorkbenchPresetLayout>) => {
    update({
      ...state,
      presets: { ...state.presets, [id]: { ...state.presets[id], ...change } },
    });
  };

  return {
    getSnapshot: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    setActivePreset: (id) => {
      if (state.activePreset === id) return;
      update({ ...state, activePreset: id });
    },
    setBottomTab: (id) => {
      const active = state.activePreset;
      if (state.presets[active].bottomTab === id) return;
      updatePreset(active, { bottomTab: id });
    },
    updateGroup: (group, layout) => {
      const active = state.activePreset;
      const current = state.presets[active][group];
      if (Object.keys(current).every((key) => current[key] === layout[key])) return;
      updatePreset(active, { [group]: { ...layout } });
    },
    resetActivePreset: () => {
      const active = state.activePreset;
      const defaults = defaultWorkbenchLayout().presets[active];
      updatePreset(active, defaults);
    },
  };
}

const stores = new Map<string, WorkbenchLayoutStore>();

export function getWorkbenchLayoutStore(storageKey = layoutStorageKey()): WorkbenchLayoutStore {
  const existing = stores.get(storageKey);
  if (existing) return existing;
  const created = createWorkbenchLayoutStore(storageKey);
  stores.set(storageKey, created);
  return created;
}

/** Tests only: drop singleton instances without deleting persisted data. */
export function resetWorkbenchLayoutStores(): void {
  stores.clear();
}

export function useWorkbenchLayout(scope: LayoutScope = {}) {
  const key = layoutStorageKey(scope);
  const store = useMemo(() => getWorkbenchLayoutStore(key), [key]);
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  return { state, store };
}
