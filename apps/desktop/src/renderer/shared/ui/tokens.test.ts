// @vitest-environment node
import { readFileSync } from 'node:fs';
import { expect, test } from 'vitest';

const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8');
const colors = new Map(
  Array.from(css.matchAll(/(--ui-[\w-]+): (#[\da-f]{6});/g), (match) => [match[1], match[2]]),
);

function luminance(token: string): number {
  const hex = colors.get(token);
  if (!hex) throw new Error(`Missing color token: ${token}`);
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return channel(1) * 0.2126 + channel(3) * 0.7152 + channel(5) * 0.0722;
}

function contrast(foreground: string, background: string) {
  const a = luminance(`--ui-${foreground}`);
  const b = luminance(`--ui-${background}`);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test.each(['success', 'danger', 'warning', 'neutral', 'accent', 'info', 'pending'])(
  '%s semantic text meets AA on its background',
  (tone) => {
    expect(contrast(`${tone}-fg`, `${tone}-bg`)).toBeGreaterThanOrEqual(4.5);
  },
);

test.each(['primary', 'primary-hover', 'primary-active', 'danger', 'danger-hover'])(
  '%s action text meets AA',
  (background) => {
    expect(contrast('on-primary', background)).toBeGreaterThanOrEqual(4.5);
  },
);

test.each(['surface', 'canvas', 'surface-hover'])(
  'body and helper text meet AA on %s',
  (background) => {
    expect(contrast('text', background)).toBeGreaterThanOrEqual(4.5);
    expect(contrast('text-muted', background)).toBeGreaterThanOrEqual(4.5);
  },
);

test('control boundaries and focus rings have at least 3:1 non-text contrast', () => {
  for (const background of ['surface', 'canvas']) {
    expect(contrast('border', background)).toBeGreaterThanOrEqual(3);
    expect(contrast('focus', background)).toBeGreaterThanOrEqual(3);
  }
});
