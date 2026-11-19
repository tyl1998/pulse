import { expect, test } from 'vitest';
import { displayBoundsToClient, type FrameGeometry, type Rotation } from './coords';

test.each([0, 90, 180, 270] as Rotation[])(
  'maps exclusive rectangle edges at rotation %s',
  (rotation) => {
    const sideways = rotation === 90 || rotation === 270;
    const frame: FrameGeometry = {
      frameWidth: sideways ? 800 : 400,
      frameHeight: sideways ? 400 : 800,
      displayWidth: 400,
      displayHeight: 800,
      rotation,
    };
    const viewport = {
      left: 100,
      top: 50,
      width: frame.frameWidth / 2,
      height: frame.frameHeight / 2,
    };
    expect(
      displayBoundsToClient({ left: 0, top: 0, right: 400, bottom: 800 }, viewport, frame),
    ).toEqual(viewport);
    const one = displayBoundsToClient(
      { left: 100, top: 100, right: 101, bottom: 101 },
      viewport,
      frame,
    );
    expect(one?.width).toBeCloseTo(0.5);
    expect(one?.height).toBeCloseTo(0.5);
  },
);

test('clips partly off-screen nodes, rejects degenerate rectangles and respects letterboxing', () => {
  const frame = {
    frameWidth: 400,
    frameHeight: 800,
    displayWidth: 400,
    displayHeight: 800,
    rotation: 0,
  } as const;
  const viewport = { left: 100, top: 50, width: 400, height: 400 };
  expect(
    displayBoundsToClient({ left: -40, top: -10, right: 100, bottom: 100 }, viewport, frame),
  ).toEqual({ left: 200, top: 50, width: 50, height: 50 });
  expect(
    displayBoundsToClient({ left: 800, top: 0, right: 900, bottom: 100 }, viewport, frame),
  ).toBeNull();
  expect(
    displayBoundsToClient({ left: 0, top: 0, right: 0, bottom: 100 }, viewport, frame),
  ).toBeNull();
  expect(
    displayBoundsToClient({ left: NaN, top: 0, right: 10, bottom: 100 }, viewport, frame),
  ).toBeNull();
  expect(
    displayBoundsToClient({ left: 0, top: 0, right: 10, bottom: 100 }, viewport, {
      ...frame,
      rotation: 45 as Rotation,
    }),
  ).toBeNull();
});
