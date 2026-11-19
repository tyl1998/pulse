import { describe, expect, test } from 'vitest';
import { isAllowedNavigation, secureWebPreferences } from './security';

describe('secureWebPreferences', () => {
  test('enforces isolation and sandbox', () => {
    const prefs = secureWebPreferences('/p.js');
    expect(prefs.contextIsolation).toBe(true);
    expect(prefs.sandbox).toBe(true);
    expect(prefs.nodeIntegration).toBe(false);
    expect(prefs.webviewTag).toBe(false);
  });
});

describe('isAllowedNavigation', () => {
  const dev = 'http://localhost:5173';
  test('allows dev server origin', () => {
    expect(isAllowedNavigation('http://localhost:5173/x', dev)).toBe(true);
  });
  test('blocks foreign origins', () => {
    expect(isAllowedNavigation('https://evil.example', dev)).toBe(false);
    expect(isAllowedNavigation('https://evil.example', undefined)).toBe(false);
  });
  test('allows file in packaged mode only', () => {
    expect(isAllowedNavigation('file:///app/index.html', undefined)).toBe(true);
    expect(isAllowedNavigation('file:///app/index.html', dev)).toBe(false);
  });
  test('rejects garbage', () => {
    expect(isAllowedNavigation('not a url', undefined)).toBe(false);
  });
});
