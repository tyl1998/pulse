export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface PixelBounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export type Rotation = 0 | 90 | 180 | 270;

export interface FrameGeometry {
  frameWidth: number;
  frameHeight: number;
  displayWidth: number;
  displayHeight: number;
  /** Clockwise transform from the current display-pixel viewport to the encoded frame. */
  rotation: Rotation;
}

const positive = (value: number) => Number.isFinite(value) && value > 0;

/** The same centered object-fit: contain rectangle used by the screen canvas. */
export function containRect(viewport: Rect, width: number, height: number): Rect | null {
  if (
    ![viewport.left, viewport.top].every(Number.isFinite) ||
    ![viewport.width, viewport.height, width, height].every(positive)
  )
    return null;
  const scale = Math.min(viewport.width / width, viewport.height / height);
  return {
    left: viewport.left + (viewport.width - width * scale) / 2,
    top: viewport.top + (viewport.height - height * scale) / 2,
    width: width * scale,
    height: height * scale,
  };
}

function validGeometry(frame: FrameGeometry): boolean {
  return (
    [frame.frameWidth, frame.frameHeight, frame.displayWidth, frame.displayHeight].every(
      (value) => Number.isSafeInteger(value) && value > 0,
    ) && [0, 90, 180, 270].includes(frame.rotation)
  );
}

/** Reject letterboxing and off-screen releases instead of clamping them into a device action. */
export function clientToDisplay(point: Point, viewport: Rect, frame: FrameGeometry): Point | null {
  if (!validGeometry(frame) || ![point.x, point.y].every(Number.isFinite)) return null;
  const content = containRect(viewport, frame.frameWidth, frame.frameHeight);
  if (!content) return null;
  const u = (point.x - content.left) / content.width;
  const v = (point.y - content.top) / content.height;
  if (u < 0 || u >= 1 || v < 0 || v >= 1) return null;
  const [x, y] =
    frame.rotation === 90
      ? [v, 1 - u]
      : frame.rotation === 180
        ? [1 - u, 1 - v]
        : frame.rotation === 270
          ? [1 - v, u]
          : [u, v];
  const pixel = (unit: number, size: number) => {
    const value = unit * size;
    const nearest = Math.round(value);
    // Snap floating-point noise at exact pixel boundaries, not genuinely adjacent points.
    const precise = Math.abs(value - nearest) <= 8 * Number.EPSILON * size ? nearest : value;
    return Math.min(size - 1, Math.floor(precise));
  };
  return {
    x: pixel(x, frame.displayWidth),
    y: pixel(y, frame.displayHeight),
  };
}

/** Map a display pixel's center back to client coordinates, including rotation and letterboxing. */
export function displayToClient(point: Point, viewport: Rect, frame: FrameGeometry): Point | null {
  if (
    !validGeometry(frame) ||
    ![point.x, point.y].every(Number.isFinite) ||
    point.x < 0 ||
    point.y < 0 ||
    point.x >= frame.displayWidth ||
    point.y >= frame.displayHeight
  )
    return null;
  const content = containRect(viewport, frame.frameWidth, frame.frameHeight);
  if (!content) return null;
  const x = (Math.floor(point.x) + 0.5) / frame.displayWidth;
  const y = (Math.floor(point.y) + 0.5) / frame.displayHeight;
  const [u, v] =
    frame.rotation === 90
      ? [1 - y, x]
      : frame.rotation === 180
        ? [1 - x, 1 - y]
        : frame.rotation === 270
          ? [y, 1 - x]
          : [x, y];
  return { x: content.left + u * content.width, y: content.top + v * content.height };
}

/** Bounds use exclusive right/bottom edges. Clip off-screen nodes without inventing a hit target. */
export function displayBoundsToClient(
  bounds: PixelBounds,
  viewport: Rect,
  frame: FrameGeometry,
): Rect | null {
  if (
    !validGeometry(frame) ||
    ![bounds.left, bounds.top, bounds.right, bounds.bottom].every(Number.isSafeInteger)
  )
    return null;
  const left = Math.max(0, bounds.left);
  const top = Math.max(0, bounds.top);
  const right = Math.min(frame.displayWidth, bounds.right);
  const bottom = Math.min(frame.displayHeight, bounds.bottom);
  if (left >= right || top >= bottom) return null;
  const content = containRect(viewport, frame.frameWidth, frame.frameHeight);
  if (!content) return null;
  const x1 = left / frame.displayWidth;
  const x2 = right / frame.displayWidth;
  const y1 = top / frame.displayHeight;
  const y2 = bottom / frame.displayHeight;
  const [u1, u2, v1, v2]: [number, number, number, number] =
    frame.rotation === 90
      ? [1 - y2, 1 - y1, x1, x2]
      : frame.rotation === 180
        ? [1 - x2, 1 - x1, 1 - y2, 1 - y1]
        : frame.rotation === 270
          ? [y1, y2, 1 - x2, 1 - x1]
          : [x1, x2, y1, y2];
  return {
    left: content.left + u1 * content.width,
    top: content.top + v1 * content.height,
    width: (u2 - u1) * content.width,
    height: (v2 - v1) * content.height,
  };
}
