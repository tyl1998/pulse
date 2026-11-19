import { expect, test } from 'vitest';
import { DEVICE_PREVIEW_TIME, simulatedDevice, simulatedTargetApp } from './deviceFixtures';
import {
  describeBoolean,
  describeCapability,
  describeList,
  devicePresentation,
  parseDeviceList,
  parseDeviceSnapshot,
  parseTargetAppCheck,
} from './deviceModel';

test('ADB online, missing facts and unknown capabilities never imply ready', () => {
  const device = parseDeviceSnapshot({ serial: 'SIM', state: 'device', status: 'agent_available' });
  expect(device.ready).toBeNull();
  expect(device.connection.health_authenticated).toBeNull();
  expect(device.capabilities).toBeNull();
  expect(device.observed_at_ms).toBeNull();
  expect(describeBoolean(undefined)).toBe('未知');
  expect(describeBoolean('false')).toBe('未知');
  expect(describeCapability(null)).toBe('未知');
  expect(describeCapability('unavailable')).toBe('不可用');
  expect(describeList(null)).toBe('未知');
  expect(describeList([])).toBe('未声明支持项');
});

test('ready, status and capability availability remain independent Runtime facts', () => {
  const device = parseDeviceSnapshot(simulatedDevice('SIM', { ready: false }));
  expect(device.status).toBe('ready');
  expect(device.ready).toBe(false);
  expect(device.capabilities?.['ocr']).toBe('unavailable');
  expect(device.observed_at_ms).toBe(DEVICE_PREVIEW_TIME);
});

test('adapters strip unknown secret fields and copy the capability arrays', () => {
  const raw = simulatedDevice();
  const device = parseDeviceSnapshot({
    ...raw,
    token: 'secret-token',
    agent: { ...raw.agent, token: 'secret-token' },
    connection: { ...raw.connection, token: 'secret-token' },
    capabilities: { ...raw.capabilities, token: 'secret-token' },
  });
  expect(JSON.stringify(device)).not.toContain('secret-token');
  raw.capabilities.actions.push('unknown');
  expect(device.capabilities?.['actions']).not.toContain('unknown');
});

test.each(['new_backend_state', 'constructor', '__proto__'])(
  'unknown status %s remains visible',
  (status) => {
    expect(devicePresentation(status)).toEqual({ label: `未知状态（${status}）`, tone: 'neutral' });
  },
);

test('device list reports HTTP-200 discovery faults without inventing an empty success', () => {
  expect(parseDeviceList({ devices: [], error: 'adb_unavailable' })).toEqual({
    devices: [],
    error: 'adb_unavailable',
  });
  expect(() => parseDeviceList({ devices: [], error: false })).toThrow();
  expect(() => parseDeviceList({})).toThrow();
  expect(() => parseDeviceList({ devices: [simulatedDevice(), simulatedDevice()] })).toThrow(
    'duplicate_device_serial',
  );
  expect(() => parseDeviceSnapshot({ serial: 'SIM', state: 'device' })).toThrow();
});

test.each([NaN, Infinity, '123', -1])(
  'invalid timestamp %s stays unknown instead of becoming now',
  (observed_at_ms) => {
    expect(
      parseDeviceSnapshot(simulatedDevice('SIM', { observed_at_ms })).observed_at_ms,
    ).toBeNull();
  },
);

test('target checks are bound to a specific serial and fixed package, not device readiness', () => {
  expect(parseTargetAppCheck(simulatedTargetApp('SIM'), 'SIM').status).toBe('installed');
  expect(() => parseTargetAppCheck(simulatedTargetApp('OTHER'), 'SIM')).toThrow();
  expect(() =>
    parseTargetAppCheck({ ...simulatedTargetApp('SIM'), package: 'other.app' }, 'SIM'),
  ).toThrow();
  expect(() =>
    parseTargetAppCheck({ ...simulatedTargetApp('SIM'), status: 'passed' }, 'SIM'),
  ).toThrow();
});
