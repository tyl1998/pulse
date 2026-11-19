/**
 * Desktop <-> Runtime version compatibility.
 *
 * The GUI and the Runtime are separate user-level processes and may be upgraded
 * independently. The GUI talks to the Runtime's private loopback API, so it only accepts a
 * Runtime whose major version matches and whose minor/patch is at least the GUI's own:
 * newer compatible runtimes are fine, older or foreign-major ones are not.
 */
export interface ParsedVersion {
  major: number;
  minor: number;
  patch: number;
}

export interface RuntimeCompatibility {
  compatible: boolean;
  /** Human-readable requirement used in status/diagnostics (no paths or secrets). */
  required: string;
}

const VERSION = /^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

export function parseVersion(raw: string): ParsedVersion | null {
  const match = VERSION.exec(raw.trim());
  if (!match) return null;
  const [, major, minor, patch] = match;
  const parsed = { major: Number(major), minor: Number(minor), patch: Number(patch) };
  return Object.values(parsed).every(Number.isSafeInteger) ? parsed : null;
}

/** Standard lexicographic ordering: negative when a < b, 0 when equal, positive when a > b. */
export function compareVersions(a: ParsedVersion, b: ParsedVersion): number {
  return a.major - b.major || a.minor - b.minor || a.patch - b.patch;
}

export function runtimeCompatibility(
  desktopVersion: string,
  runtimeVersion: string,
): RuntimeCompatibility {
  const desktop = parseVersion(desktopVersion);
  const runtime = parseVersion(runtimeVersion);
  const required = desktop
    ? `>=${desktop.major}.${desktop.minor}.${desktop.patch} 且主版本为 ${desktop.major}`
    : `有效的桌面版本（当前为 ${desktopVersion}）`;
  if (!desktop || !runtime) return { compatible: false, required };
  return {
    compatible: runtime.major === desktop.major && compareVersions(runtime, desktop) >= 0,
    required,
  };
}
