import { expect, test } from 'vitest';
import {
  createWorkbenchLayoutStore,
  defaultWorkbenchLayout,
  layoutStorageKey,
  LAYOUT_STORAGE_PREFIX,
  parseWorkbenchLayout,
  PRESET_IDS,
  WORKBENCH_PRESETS,
} from './layoutStore';

class MemoryStorage implements Storage {
  private readonly data = new Map<string, string>();

  get length(): number {
    return this.data.size;
  }

  clear(): void {
    this.data.clear();
  }

  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.data.delete(key);
  }

  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
}

test('defaults cover every preset and each group adds up to 100', () => {
  const state = defaultWorkbenchLayout();
  expect(state.version).toBe(1);
  expect(state.activePreset).toBe('edit');
  expect(Object.keys(state.presets)).toEqual(PRESET_IDS);
  for (const id of PRESET_IDS) {
    const preset = state.presets[id];
    expect(Object.values(preset.horizontal).reduce((sum, value) => sum + value, 0)).toBe(100);
    expect(Object.values(preset.right).reduce((sum, value) => sum + value, 0)).toBe(100);
    expect(Object.values(preset.vertical).reduce((sum, value) => sum + value, 0)).toBe(100);
    expect(preset.bottomTab).toBe(WORKBENCH_PRESETS[id].bottomTab);
  }
});

test('shipped layouts give the device readable width and most of the workspace height', () => {
  for (const preset of Object.values(WORKBENCH_PRESETS)) {
    expect(preset.horizontal.right).toBeGreaterThanOrEqual(40);
    expect(preset.vertical.workspace).toBeGreaterThanOrEqual(78);
    expect(preset.right.device).toBeGreaterThanOrEqual(78);
  }
  expect(WORKBENCH_PRESETS.record.horizontal.right).toBeGreaterThan(
    WORKBENCH_PRESETS.edit.horizontal.right,
  );
  expect(WORKBENCH_PRESETS.edit.right.elements).toBe(0);
  expect(WORKBENCH_PRESETS.record.right.elements).toBe(0);
  expect(WORKBENCH_PRESETS.edit.vertical.bottom).toBeLessThanOrEqual(10);
  expect(WORKBENCH_PRESETS.record.vertical.bottom).toBeLessThanOrEqual(10);
});

test('layout keys are scoped by project and user without leaking path characters', () => {
  expect(layoutStorageKey()).toBe(`${LAYOUT_STORAGE_PREFIX}:local:local`);
  expect(layoutStorageKey({ projectId: 'p 1', userId: 'u/2' })).toBe(
    `${LAYOUT_STORAGE_PREFIX}:p%201:u%2F2`,
  );
});

test('preset, custom sizes and bottom tab persist across store instances', () => {
  const storage = new MemoryStorage();
  const key = layoutStorageKey({ projectId: 'demo', userId: 'qa' });
  const store = createWorkbenchLayoutStore(key, storage);

  store.setActivePreset('debug');
  store.updateGroup('horizontal', { outline: 30, editor: 40, right: 30 });
  store.setBottomTab('events');

  const restored = createWorkbenchLayoutStore(key, storage).getSnapshot();
  expect(restored.activePreset).toBe('debug');
  expect(restored.presets.debug.horizontal).toEqual({ outline: 30, editor: 40, right: 30 });
  expect(restored.presets.debug.bottomTab).toBe('events');
  expect(restored.presets.edit.horizontal).toEqual(WORKBENCH_PRESETS.edit.horizontal);
});

test('reset returns only the active preset to its shipped defaults', () => {
  const storage = new MemoryStorage();
  const store = createWorkbenchLayoutStore('pulse:test', storage);
  store.setActivePreset('record');
  store.updateGroup('vertical', { workspace: 50, bottom: 50 });
  store.setBottomTab('issues');
  store.resetActivePreset();

  const state = store.getSnapshot();
  expect(state.activePreset).toBe('record');
  expect(state.presets.record.vertical).toEqual(WORKBENCH_PRESETS.record.vertical);
  expect(state.presets.record.bottomTab).toBe(WORKBENCH_PRESETS.record.bottomTab);
});

test('corrupt, out-of-range and partial layouts fall back per group without throwing', () => {
  expect(parseWorkbenchLayout('{not json').activePreset).toBe('edit');
  expect(parseWorkbenchLayout(JSON.stringify({ version: 2 })).activePreset).toBe('edit');

  const parsed = parseWorkbenchLayout(
    JSON.stringify({
      version: 1,
      activePreset: 'nope',
      presets: {
        edit: {
          horizontal: { outline: 10, editor: 10, right: 10 },
          right: { device: 101, elements: -1 },
          vertical: { workspace: 70, bottom: 30 },
          bottomTab: 'not-a-tab',
        },
        debug: {
          horizontal: { outline: 25, editor: 50, right: 25 },
          vertical: { workspace: 60, bottom: 40 },
          bottomTab: 'variables',
        },
      },
    }),
  );

  expect(parsed.activePreset).toBe('edit');
  expect(parsed.presets.edit.horizontal).toEqual(WORKBENCH_PRESETS.edit.horizontal);
  expect(parsed.presets.edit.right).toEqual(WORKBENCH_PRESETS.edit.right);
  expect(parsed.presets.edit.vertical).toEqual({ workspace: 70, bottom: 30 });
  expect(parsed.presets.edit.bottomTab).toBe(WORKBENCH_PRESETS.edit.bottomTab);
  expect(parsed.presets.debug.horizontal).toEqual({ outline: 25, editor: 50, right: 25 });
  expect(parsed.presets.debug.right).toEqual(WORKBENCH_PRESETS.debug.right);
});

test('storage failures degrade to in-memory state instead of breaking the workspace', () => {
  const broken: Storage = {
    get length(): number {
      throw new Error('denied');
    },
    clear: () => undefined,
    getItem: () => null,
    key: () => null,
    removeItem: () => undefined,
    setItem: () => {
      throw new Error('quota');
    },
  };
  const store = createWorkbenchLayoutStore('pulse:test', broken);
  store.setActivePreset('record');
  expect(store.getSnapshot().activePreset).toBe('record');
});
