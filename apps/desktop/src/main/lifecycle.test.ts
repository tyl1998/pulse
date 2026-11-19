import { describe, expect, test } from 'vitest';
import { runtimeSummary, shouldHideOnClose, shouldQuitOnAllWindowsClosed } from './lifecycle';

describe('close policy (R05: closing the GUI never stops the Runtime)', () => {
  test('closing to tray hides only while a tray exists and no real quit is in progress', () => {
    expect(shouldHideOnClose({ closeToTray: true, hasTray: true, quitting: false })).toBe(true);
    expect(shouldHideOnClose({ closeToTray: true, hasTray: false, quitting: false })).toBe(false);
    expect(shouldHideOnClose({ closeToTray: true, hasTray: true, quitting: true })).toBe(false);
    expect(shouldHideOnClose({ closeToTray: false, hasTray: true, quitting: false })).toBe(false);
  });

  test('all-windows-closed keeps macOS alive and honours the tray setting elsewhere', () => {
    expect(
      shouldQuitOnAllWindowsClosed({ platform: 'darwin', closeToTray: false, hasTray: false }),
    ).toBe(false);
    expect(
      shouldQuitOnAllWindowsClosed({ platform: 'win32', closeToTray: true, hasTray: true }),
    ).toBe(false);
    expect(
      shouldQuitOnAllWindowsClosed({ platform: 'linux', closeToTray: true, hasTray: false }),
    ).toBe(true);
    expect(
      shouldQuitOnAllWindowsClosed({ platform: 'win32', closeToTray: false, hasTray: true }),
    ).toBe(true);
  });
});

describe('runtimeSummary', () => {
  test('describes lifecycle states without claiming task results', () => {
    expect(
      runtimeSummary({
        kind: 'running',
        connection: { port: 1, token: 't', pid: 2, version: '1.0.0' },
        health: { status: 'ok', version: '1.0.0', pid: 2, uptime_s: 1 },
      }),
    ).toBe('Runtime 已连接 · v1.0.0 · pid 2');
    expect(runtimeSummary({ kind: 'not-running', launchable: true, detail: 'x' })).toContain(
      '可自动启动',
    );
    expect(runtimeSummary({ kind: 'not-running', launchable: false, detail: 'x' })).toBe(
      'Runtime 未运行',
    );
    expect(runtimeSummary({ kind: 'unhealthy', pid: 7, detail: 'x' })).toContain('pid 7');
    expect(
      runtimeSummary({
        kind: 'incompatible',
        connection: { port: 1, token: 't', pid: 2, version: '0.9.0' },
        health: { status: 'ok', version: '0.9.0', pid: 2, uptime_s: 1 },
        required: '>=1.0.0',
        detail: 'x',
      }),
    ).toContain('不兼容');
  });
});
