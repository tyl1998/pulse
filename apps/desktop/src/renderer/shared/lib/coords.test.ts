import { describe, expect, test } from 'vitest';
import { clientToDisplay, containRect, displayToClient, type FrameGeometry } from './coords';

const portrait: FrameGeometry = {
  frameWidth: 540,
  frameHeight: 1200,
  displayWidth: 1080,
  displayHeight: 2400,
  rotation: 0,
};
const viewport = { left: 100, top: 50, width: 400, height: 400 };

describe('current-frame coordinate mapping', () => {
  test('fits a scaled portrait frame with horizontal letterboxing and client offsets', () => {
    expect(containRect(viewport, 540, 1200)).toEqual({
      left: 210,
      top: 50,
      width: 180,
      height: 400,
    });
    expect(clientToDisplay({ x: 300, y: 250 }, viewport, portrait)).toEqual({ x: 540, y: 1200 });
    expect(clientToDisplay({ x: 210, y: 50 }, viewport, portrait)).toEqual({ x: 0, y: 0 });
  });

  test('fits landscape frames with vertical letterboxing', () => {
    const frame = { ...portrait, frameWidth: 1200, frameHeight: 540, rotation: 90 as const };
    expect(containRect(viewport, frame.frameWidth, frame.frameHeight)).toEqual({
      left: 100,
      top: 160,
      width: 400,
      height: 180,
    });
    expect(clientToDisplay({ x: 200, y: 205 }, viewport, frame)).toEqual({ x: 270, y: 1800 });
  });

  test.each([
    [{ x: 209.99, y: 250 }],
    [{ x: 390, y: 250 }],
    [{ x: 300, y: 49.99 }],
    [{ x: 300, y: 450 }],
    [{ x: -1, y: 0 }],
    [{ x: NaN, y: 250 }],
    [{ x: Infinity, y: 250 }],
  ])('rejects black bars, exclusive edges and invalid client points: %j', (point) => {
    expect(clientToDisplay(point, viewport, portrait)).toBeNull();
  });

  test.each([
    [0, { x: 270, y: 600 }],
    [90, { x: 270, y: 1800 }],
    [180, { x: 810, y: 1800 }],
    [270, { x: 810, y: 600 }],
  ] as const)('inverts a clockwise %s degree frame transform', (rotation, expected) => {
    const frame = { ...portrait, rotation };
    expect(clientToDisplay({ x: 255, y: 150 }, viewport, frame)).toEqual(expected);
  });

  test.each([0, 90, 180, 270] as const)('pixel-center round trip at %s degrees', (rotation) => {
    const frame = {
      ...portrait,
      rotation,
      ...(rotation === 90 || rotation === 270 ? { frameWidth: 1200, frameHeight: 540 } : {}),
    };
    for (const point of [
      { x: 0, y: 0 },
      { x: 1079, y: 2399 },
      { x: 1079, y: 0 },
      { x: 0, y: 2399 },
      { x: 317, y: 819 },
    ]) {
      const client = displayToClient(point, viewport, frame);
      expect(client).not.toBeNull();
      expect(client && clientToDisplay(client, viewport, frame)).toEqual(point);
    }
  });

  test('current native-orientation frames are not rotated twice after a device rotation', () => {
    const landscape = {
      ...portrait,
      frameWidth: 1200,
      frameHeight: 540,
      displayWidth: 2400,
      displayHeight: 1080,
    };
    expect(clientToDisplay({ x: 200, y: 205 }, viewport, landscape)).toEqual({ x: 600, y: 270 });
  });

  test.each([0, -1, NaN, Infinity])('rejects invalid viewport/frame dimensions: %s', (value) => {
    expect(containRect({ ...viewport, width: value }, 540, 1200)).toBeNull();
    expect(containRect(viewport, 540, value)).toBeNull();
    expect(
      clientToDisplay({ x: 300, y: 250 }, viewport, { ...portrait, displayWidth: value }),
    ).toBeNull();
    expect(
      displayToClient({ x: 0, y: 0 }, viewport, { ...portrait, frameHeight: value }),
    ).toBeNull();
  });

  test('rejects unknown rotation, fractional pixel dimensions and off-device points', () => {
    expect(
      clientToDisplay({ x: 300, y: 250 }, viewport, { ...portrait, rotation: 45 as 0 }),
    ).toBeNull();
    expect(
      clientToDisplay({ x: 300, y: 250 }, viewport, { ...portrait, displayWidth: 1080.5 }),
    ).toBeNull();
    expect(containRect({ ...viewport, left: NaN }, 540, 1200)).toBeNull();
    expect(displayToClient({ x: 1080, y: 0 }, viewport, portrait)).toBeNull();
    expect(displayToClient({ x: -1, y: 0 }, viewport, portrait)).toBeNull();
  });
});
