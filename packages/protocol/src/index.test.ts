import { expect, test } from 'vitest';
import { PROTOCOL_VERSION } from './index';

test('exports protocol version', () => {
  expect(PROTOCOL_VERSION).toBe(0);
});
