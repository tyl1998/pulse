import { describe, expect, test } from 'vitest';
import { compareVersions, parseVersion, runtimeCompatibility } from './runtime-version';

const v = (s: string) => {
  const parsed = parseVersion(s);
  if (!parsed) throw new Error(`bad version ${s}`);
  return parsed;
};

describe('parseVersion / compareVersions', () => {
  test('accepts plain and prerelease versions', () => {
    expect(parseVersion('1.2.3')).toEqual({ major: 1, minor: 2, patch: 3 });
    expect(parseVersion(' 0.0.0 ')).toEqual({ major: 0, minor: 0, patch: 0 });
    expect(parseVersion('2.10.4-beta.1+build7')).toEqual({ major: 2, minor: 10, patch: 4 });
  });

  test.each(['', '1.2', 'v1.2.3', '1.2.3.4', '1.-2.3', 'dev'])('rejects %s', (raw) => {
    expect(parseVersion(raw)).toBeNull();
  });

  test('orders numerically, not lexically', () => {
    expect(compareVersions(v('1.10.0'), v('1.9.9'))).toBeGreaterThan(0);
    expect(compareVersions(v('1.2.3'), v('1.2.3'))).toBe(0);
    expect(compareVersions(v('0.9.9'), v('1.0.0'))).toBeLessThan(0);
  });
});

describe('runtimeCompatibility', () => {
  test('same version, newer minor/patch and newer compatible major are fine', () => {
    expect(runtimeCompatibility('0.0.0', '0.0.0').compatible).toBe(true);
    expect(runtimeCompatibility('0.1.0', '0.2.0').compatible).toBe(true);
    expect(runtimeCompatibility('1.2.3', '1.2.4').compatible).toBe(true);
    expect(runtimeCompatibility('1.2.3', '1.3.0').compatible).toBe(true);
  });

  test('older runtime or a different major is rejected', () => {
    expect(runtimeCompatibility('1.2.3', '1.2.2').compatible).toBe(false);
    expect(runtimeCompatibility('1.2.3', '1.1.9').compatible).toBe(false);
    expect(runtimeCompatibility('1.2.3', '0.9.9').compatible).toBe(false);
    expect(runtimeCompatibility('1.2.3', '2.0.0').compatible).toBe(false);
  });

  test('unparsable versions never silently count as compatible', () => {
    expect(runtimeCompatibility('1.2.3', 'dev').compatible).toBe(false);
    expect(runtimeCompatibility('dev', '1.2.3').compatible).toBe(false);
    expect(runtimeCompatibility('1.2.3', 'dev').required).toContain('1.2.3');
  });
});
