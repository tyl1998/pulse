const NAMES: Record<string, string> = { darwin: 'macOS', win32: 'Windows', linux: 'Linux' };

export function formatPlatform(platform: string): string {
  return NAMES[platform] ?? platform;
}
