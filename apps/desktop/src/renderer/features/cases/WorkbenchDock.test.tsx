import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { WorkbenchDock } from './WorkbenchDock';
import {
  getWorkbenchLayoutStore,
  layoutStorageKey,
  resetWorkbenchLayoutStores,
  WORKBENCH_PRESETS,
} from './layoutStore';

beforeEach(() => {
  window.localStorage.clear();
  resetWorkbenchLayoutStores();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test('renders a docked workspace with resizable groups and the three presets', () => {
  render(<WorkbenchDock />);
  expect(screen.getByTestId('workbench-dock')).toHaveAttribute('data-preset', 'edit');
  expect(screen.getByTestId('studio-vertical')).toBeVisible();
  expect(screen.getByTestId('studio-horizontal')).toBeVisible();
  expect(screen.getByTestId('studio-right')).toBeVisible();
  expect(screen.getAllByRole('separator').map((item) => item.getAttribute('aria-label'))).toEqual([
    '调整用例大纲宽度',
    '调整右侧面板宽度',
    '调整设备画面与元素树高度',
    '调整底部面板高度',
  ]);
  expect(screen.getByRole('tab', { name: '问题' })).toHaveAttribute('data-state', 'active');
  expect(screen.getByTestId('device-screen')).toBeVisible();
});

test('narrow screens stack the workspace panels instead of squeezing three columns', () => {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query === '(max-width: 700px)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  render(<WorkbenchDock />);
  expect(screen.getByTestId('studio-horizontal')).toHaveStyle({ flexDirection: 'column' });
  expect(screen.getByRole('separator', { name: '调整用例大纲高度' })).toBeVisible();
  expect(screen.getByRole('separator', { name: '调整设备面板高度' })).toBeVisible();
  expect(screen.getByRole('button', { name: '放大画面' })).toBeVisible();
});

test('unused panels start folded and can be opened without rebuilding the device screen', async () => {
  render(<WorkbenchDock />);
  const device = screen.getByTestId('device-screen');
  const elements = screen.getByRole('button', { name: '元素面板' });
  expect(elements).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByTestId('elements')).toHaveAttribute('inert');
  await userEvent.click(elements);
  expect(elements).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByTestId('elements')).not.toHaveAttribute('inert');
  expect(screen.getByTestId('device-screen')).toBe(device);
  await userEvent.click(screen.getByRole('button', { name: '展开底部面板' }));
  expect(screen.getByRole('button', { name: '收起底部面板' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(screen.getByTestId('device-screen')).toBe(device);
  await userEvent.click(elements);
  expect(elements).toHaveAttribute('aria-pressed', 'false');
  await userEvent.click(screen.getByRole('button', { name: '收起底部面板' }));
  expect(screen.getByRole('button', { name: '展开底部面板' })).toBeVisible();
});

test('preset buttons update the active preset and persisted bottom tab', async () => {
  render(<WorkbenchDock />);
  const record = screen.getByRole('button', { name: '录制' });
  expect(record).toHaveAttribute('aria-pressed', 'false');

  await userEvent.click(record);
  expect(screen.getByTestId('workbench-dock')).toHaveAttribute('data-preset', 'record');
  expect(record).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('tab', { name: '事件日志' })).toHaveAttribute('data-state', 'active');

  const persisted = JSON.parse(window.localStorage.getItem(layoutStorageKey()) ?? '{}') as {
    activePreset?: string;
    presets?: Record<string, { bottomTab?: string }>;
  };
  expect(persisted.activePreset).toBe('record');
  expect(persisted.presets?.['record']?.bottomTab).toBe('events');
});

test('restores persisted preset, custom group sizes and clamp-safe tab choice on mount', () => {
  const key = layoutStorageKey();
  window.localStorage.setItem(
    key,
    JSON.stringify({
      version: 1,
      activePreset: 'debug',
      presets: {
        debug: {
          horizontal: { outline: 30, editor: 35, right: 35 },
          right: { device: 50, elements: 50 },
          vertical: { workspace: 55, bottom: 45 },
          bottomTab: 'events',
        },
      },
    }),
  );
  render(<WorkbenchDock />);
  expect(screen.getByTestId('workbench-dock')).toHaveAttribute('data-preset', 'debug');
  expect(screen.getByRole('button', { name: '调试' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('tab', { name: '事件日志' })).toHaveAttribute('data-state', 'active');
  expect(getWorkbenchLayoutStore().getSnapshot().presets.debug.horizontal).toEqual({
    outline: 30,
    editor: 35,
    right: 35,
  });
});

test('reset restores the active preset defaults without changing the preset', async () => {
  const store = getWorkbenchLayoutStore();
  store.setActivePreset('record');
  store.updateGroup('horizontal', { outline: 40, editor: 30, right: 30 });
  render(<WorkbenchDock />);
  expect(store.getSnapshot().presets.record.horizontal).toEqual({
    outline: 40,
    editor: 30,
    right: 30,
  });

  await userEvent.click(screen.getByRole('button', { name: '重置当前预设' }));
  expect(store.getSnapshot().activePreset).toBe('record');
  expect(store.getSnapshot().presets.record.horizontal).toEqual(
    WORKBENCH_PRESETS.record.horizontal,
  );
});

test('bottom panel tabs are controlled and persist separately from the preset', async () => {
  render(<WorkbenchDock />);
  // jsdom has no layout, so the resize library treats every pointerdown as a separator hit;
  // activate through the keyboard/focus contract instead of a synthetic pointer click.
  const tab = screen.getByRole('tab', { name: 'AI 聊天' });
  act(() => tab.focus());
  expect(getWorkbenchLayoutStore().getSnapshot().presets.edit.bottomTab).toBe('ai');
  expect(screen.getByRole('tab', { name: 'AI 聊天' })).toHaveAttribute('data-state', 'active');
  expect(screen.getByTestId('dock-placeholder-AI 聊天')).toBeTruthy();
  const persisted = JSON.parse(window.localStorage.getItem(layoutStorageKey()) ?? '{}') as {
    presets?: Record<string, { bottomTab?: string }>;
  };
  expect(persisted.presets?.['edit']?.bottomTab).toBe('ai');
});
