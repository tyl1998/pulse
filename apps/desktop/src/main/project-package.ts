import { isUtf8 } from 'node:buffer';
import { lstat, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { ProjectCreateRequest, ProjectExportResult, ProjectWorkspace } from '../shared/ipc';

/**
 * Portable project package used by FE-P2.4 import/export.
 *
 * Only manifest-declared assets are included; runs/sync/credentials and absolute local paths are
 * rejected instead of being silently copied. Binary resources are base64 encoded.
 */
export const PROJECT_PACKAGE_CONTRACT = 'pulse-project-package-draft';

const MAX_FILE_BYTES = 8 * 1024 * 1024;
const MAX_TOTAL_BYTES = 32 * 1024 * 1024;
const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_-]*$/;
const JSON_ASSET = /^(?:cases|flows|data|profiles)\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.json$/;
const RESOURCE_ASSET = /^resources\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+\.[A-Za-z0-9]+$/;
const SENSITIVE_KEY =
  /^(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|credentials)$/i;
const SECRET_REFERENCE_KEYS = new Set(['secret_ref', 'secret_refs']);
const PERSONAL_PATH_PATTERNS = [
  /file:\/\//i,
  /(?:^|[\s"'(:=,])\/(?:Users|home|root|private|var|tmp|Volumes|mnt)\//,
  /(?:^|[^A-Za-z0-9])[A-Za-z]:[\\/]/,
  /\\\\[^\\\s]+\\/,
];

export type ProjectPackageEncoding = 'utf8' | 'base64';

export interface ProjectPackageFile {
  encoding: ProjectPackageEncoding;
  content: string;
}

export interface ProjectPackage {
  contract: typeof PROJECT_PACKAGE_CONTRACT;
  project_id: string;
  name: string;
  revision: string;
  files: Record<string, ProjectPackageFile>;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function fail(message: string): never {
  throw new Error(message);
}

function parseJsonObject(text: string, where: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    fail(`${where} 不是合法 JSON`);
  }
  if (!isRecord(parsed)) fail(`${where} 必须是 JSON 对象`);
  return parsed;
}

function validateAssetPath(relative: string): void {
  if (path.isAbsolute(relative) || relative.includes('\\')) {
    fail(`工程包包含非相对路径 "${relative}"`);
  }
  if (relative === 'manifest.json') return;
  if (!JSON_ASSET.test(relative) && !RESOURCE_ASSET.test(relative)) {
    fail(`工程包包含未授权路径 "${relative}"`);
  }
}

function declaredAssetPaths(manifest: Record<string, unknown>): string[] {
  if (manifest['$schema'] !== 'urn:pulse:schema:1-draft:manifest') {
    fail('manifest.json 的 $schema 不是 1-draft 工程清单');
  }
  const projectId = manifest['project_id'];
  if (typeof projectId !== 'string' || !IDENTIFIER.test(projectId)) {
    fail('manifest.json 的 project_id 无效');
  }
  const name = manifest['name'];
  const revision = manifest['revision'];
  if (typeof name !== 'string' || !name.trim()) fail('manifest.json 缺少工程名称');
  if (typeof revision !== 'string' || !revision.trim()) fail('manifest.json 缺少修订号');

  const assets: string[] = [];
  const seen = new Set<string>();
  for (const key of ['cases', 'flows', 'data', 'profiles'] as const) {
    const collection = manifest[key];
    if (!isRecord(collection)) fail(`manifest.json 缺少 ${key} 清单`);
    for (const relative of Object.values(collection)) {
      if (typeof relative !== 'string') fail(`manifest.json/${key} 包含非字符串路径`);
      validateAssetPath(relative);
      if (seen.has(relative)) fail(`manifest.json 重复声明资产 "${relative}"`);
      seen.add(relative);
      assets.push(relative);
    }
  }
  const resources = manifest['resources'] ?? {};
  if (!isRecord(resources)) fail('manifest.json/resources 必须是对象');
  for (const relative of Object.values(resources)) {
    if (typeof relative !== 'string') fail('manifest.json/resources 包含非字符串路径');
    validateAssetPath(relative);
    if (seen.has(relative)) fail(`manifest.json 重复声明资产 "${relative}"`);
    seen.add(relative);
    assets.push(relative);
  }

  const entries = manifest['entry_cases'];
  if (!Array.isArray(entries) || entries.length === 0) {
    fail('manifest.json 缺少 entry_cases');
  }
  const cases = manifest['cases'];
  if (isRecord(cases)) {
    for (const entry of entries) {
      if (typeof entry !== 'string' || !(entry in cases)) {
        fail('manifest.json 的 entry_cases 引用了不存在的用例');
      }
    }
  }
  if (entries.some((entry) => typeof entry !== 'string')) {
    fail('manifest.json 的 entry_cases 必须由字符串组成');
  }
  return assets.sort();
}

function assertNoPersonalPaths(text: string, where: string): void {
  for (const pattern of PERSONAL_PATH_PATTERNS) {
    if (pattern.test(text)) {
      fail(`${where} 包含个人绝对路径，导出包只允许工程内相对路径`);
    }
  }
}

function isSecretReference(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const keys = Object.keys(value);
  if (keys.length !== 1) return false;
  const key = keys[0];
  if (key === 'secret_ref') return typeof value[key] === 'string';
  if (key === 'secret_refs') {
    const refs = value[key];
    return Array.isArray(refs) && refs.every((ref) => typeof ref === 'string');
  }
  return false;
}

function assertNoSecretValues(value: unknown, where: string): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoSecretValues(item, `${where}/${index}`));
    return;
  }
  if (!isRecord(value)) return;
  for (const [key, child] of Object.entries(value)) {
    if (!SECRET_REFERENCE_KEYS.has(key) && SENSITIVE_KEY.test(key) && !isSecretReference(child)) {
      fail(`${where} 包含疑似明文密钥字段 "${key}"，请改用 secret_ref 引用`);
    }
    assertNoSecretValues(child, `${where}/${key}`);
  }
}

function decodedSize(file: ProjectPackageFile, where: string): number {
  if (file.encoding === 'base64') {
    if (file.content.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(file.content)) {
      fail(`${where} 不是合法 base64`);
    }
    return Buffer.from(file.content, 'base64').byteLength;
  }
  if (file.encoding !== 'utf8') fail(`${where} 使用了未知编码`);
  return Buffer.byteLength(file.content, 'utf8');
}

function validatePackageFile(relative: string, value: unknown): ProjectPackageFile {
  validateAssetPath(relative);
  if (!isRecord(value)) fail(`工程包文件 "${relative}" 格式无效`);
  const encoding = value['encoding'];
  const content = value['content'];
  if (encoding !== 'utf8' && encoding !== 'base64') {
    fail(`工程包文件 "${relative}" 编码无效`);
  }
  if (typeof content !== 'string') fail(`工程包文件 "${relative}" 缺少内容`);
  const file: ProjectPackageFile = { encoding, content };
  const size = decodedSize(file, relative);
  if (size > MAX_FILE_BYTES) fail(`工程包文件 "${relative}" 超过单文件大小上限`);
  if (encoding === 'utf8') {
    assertNoPersonalPaths(content, relative);
    if (relative.endsWith('.json')) {
      assertNoSecretValues(parseJsonObject(content, relative), relative);
    }
  }
  if (relative === 'manifest.json' && encoding !== 'utf8') {
    fail('manifest.json 必须以 utf8 编码导出');
  }
  return file;
}

/** Validate an untrusted package before reading or writing any file. */
export function validateProjectPackage(value: unknown): ProjectPackage {
  if (!isRecord(value)) fail('工程包必须是 JSON 对象');
  if (value['contract'] !== PROJECT_PACKAGE_CONTRACT) fail('工程包版本不受支持');
  const projectId = value['project_id'];
  const name = value['name'];
  const revision = value['revision'];
  if (typeof projectId !== 'string' || !IDENTIFIER.test(projectId)) fail('工程包 project_id 无效');
  if (typeof name !== 'string' || !name.trim()) fail('工程包缺少工程名称');
  if (typeof revision !== 'string' || !revision.trim()) fail('工程包缺少修订号');
  if (!isRecord(value['files'])) fail('工程包缺少 files');

  const files: Record<string, ProjectPackageFile> = {};
  let total = 0;
  for (const [relative, fileValue] of Object.entries(value['files'])) {
    const file = validatePackageFile(relative, fileValue);
    total += decodedSize(file, relative);
    files[relative] = file;
  }
  if (total > MAX_TOTAL_BYTES) fail('工程包超过总大小上限');
  const manifestFile = files['manifest.json'];
  if (!manifestFile) fail('工程包缺少 manifest.json');
  const manifest = parseJsonObject(manifestFile.content, 'manifest.json');
  const assets = declaredAssetPaths(manifest);
  const expected = new Set(['manifest.json', ...assets]);
  for (const relative of Object.keys(files)) {
    if (!expected.has(relative)) fail(`工程包包含未声明文件 "${relative}"`);
  }
  for (const relative of expected) {
    if (!(relative in files)) fail(`工程包缺少已声明文件 "${relative}"`);
  }
  if (manifest['project_id'] !== projectId) fail('工程包 project_id 与 manifest.json 不一致');
  if (manifest['name'] !== name || manifest['revision'] !== revision) {
    fail('工程包名称或修订号与 manifest.json 不一致');
  }
  const ordered: Record<string, ProjectPackageFile> = {};
  for (const relative of ['manifest.json', ...assets]) {
    const file = files[relative];
    if (file) ordered[relative] = file;
  }
  return {
    contract: PROJECT_PACKAGE_CONTRACT,
    project_id: projectId,
    name,
    revision,
    files: ordered,
  };
}

async function ensureDirectory(directory: string, label: string): Promise<string> {
  const resolved = path.resolve(directory);
  let info;
  try {
    info = await lstat(resolved);
  } catch {
    fail(`${label}不存在`);
  }
  if (!info.isDirectory()) fail(`${label}不是目录`);
  return resolved;
}

async function readAsset(root: string, relative: string): Promise<ProjectPackageFile> {
  const candidate = path.resolve(root, relative);
  const relativeCheck = path.relative(root, candidate);
  if (relativeCheck.startsWith('..') || path.isAbsolute(relativeCheck)) {
    fail(`资产路径越出工程目录 "${relative}"`);
  }
  let info;
  try {
    info = await lstat(candidate);
  } catch {
    fail(`已声明资产不存在 "${relative}"`);
  }
  if (!info.isFile()) fail(`已声明资产不是普通文件 "${relative}"`);
  if (info.size > MAX_FILE_BYTES) fail(`资产超过单文件大小上限 "${relative}"`);
  const bytes = await readFile(candidate);
  if (isUtf8(bytes)) {
    const text = bytes.toString('utf8');
    assertNoPersonalPaths(text, relative);
    if (relative.endsWith('.json')) {
      assertNoSecretValues(parseJsonObject(text, relative), relative);
    }
    return { encoding: 'utf8', content: text };
  }
  return { encoding: 'base64', content: bytes.toString('base64') };
}

/** Read manifest-declared assets only; never walk runs/, sync/ or arbitrary files. */
export async function collectProjectPackage(rootPath: string): Promise<ProjectPackage> {
  const root = await ensureDirectory(rootPath, '工程目录');
  const manifestFile = await readAsset(root, 'manifest.json');
  const manifest = parseJsonObject(manifestFile.content, 'manifest.json');
  const assets = declaredAssetPaths(manifest);
  const files: Record<string, ProjectPackageFile> = { 'manifest.json': manifestFile };
  let total = decodedSize(manifestFile, 'manifest.json');
  for (const relative of assets) {
    const file = await readAsset(root, relative);
    total += decodedSize(file, relative);
    if (total > MAX_TOTAL_BYTES) fail('工程超过导出包总大小上限');
    files[relative] = file;
  }
  return validateProjectPackage({
    contract: PROJECT_PACKAGE_CONTRACT,
    project_id: manifest['project_id'],
    name: manifest['name'],
    revision: manifest['revision'],
    files,
  });
}

export function normalizeProjectPackagePath(targetPath: string): string {
  const resolved = path.resolve(targetPath);
  if (/\.pulse-project\.json$/i.test(resolved)) return resolved;
  if (/\.json$/i.test(resolved)) return resolved.replace(/\.json$/i, '.pulse-project.json');
  return `${resolved}.pulse-project.json`;
}

export async function writeProjectPackage(
  targetPath: string,
  packageValue: ProjectPackage,
): Promise<ProjectExportResult> {
  const target = normalizeProjectPackagePath(targetPath);
  const validated = validateProjectPackage(packageValue);
  const serialized = `${JSON.stringify(validated, null, 2)}\n`;
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.tmp`;
  try {
    await writeFile(temporary, serialized, 'utf8');
    await rename(temporary, target);
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
  const totalBytes = Buffer.byteLength(serialized, 'utf8');
  return { targetPath: target, fileCount: Object.keys(validated.files).length, totalBytes };
}

export async function readProjectPackage(packagePath: string): Promise<ProjectPackage> {
  const resolved = path.resolve(packagePath);
  const info = await lstat(resolved);
  if (!info.isFile()) fail('工程包路径不是文件');
  if (info.size > MAX_TOTAL_BYTES + 1024 * 1024) fail('工程包超过总大小上限');
  return validateProjectPackage(parseJsonObject(await readFile(resolved, 'utf8'), '工程包'));
}

async function writeExclusiveFiles(root: string, files: Record<string, ProjectPackageFile>) {
  let totalBytes = 0;
  for (const relative of Object.keys(files).sort()) {
    validateAssetPath(relative);
    const file = files[relative];
    if (!file) continue;
    const destination = path.resolve(root, relative);
    await mkdir(path.dirname(destination), { recursive: true });
    const bytes =
      file.encoding === 'utf8'
        ? Buffer.from(file.content, 'utf8')
        : Buffer.from(file.content, 'base64');
    totalBytes += bytes.byteLength;
    await writeFile(destination, bytes, { flag: 'wx' });
  }
  return { fileCount: Object.keys(files).length, totalBytes };
}

/** Materialize a validated package into parentDir/<project_id>; never overwrites a directory. */
export async function importProjectPackage(
  packageValue: unknown,
  parentDir: string,
): Promise<ProjectWorkspace> {
  const packageData = validateProjectPackage(packageValue);
  const parent = await ensureDirectory(parentDir, '导入目标目录');
  const root = path.join(parent, packageData.project_id);
  try {
    await mkdir(root, { recursive: false });
  } catch {
    fail(`导入目标目录已存在：${root}`);
  }
  try {
    const written = await writeExclusiveFiles(root, packageData.files);
    return { root, projectId: packageData.project_id, ...written };
  } catch (error) {
    await rm(root, { recursive: true, force: true });
    throw error;
  }
}

function starterManifest(projectId: string, name: string): Record<string, unknown> {
  return {
    $schema: 'urn:pulse:schema:1-draft:manifest',
    project_id: projectId,
    name,
    revision: '1',
    cases: { starter: 'cases/starter.json' },
    flows: {},
    data: {},
    profiles: { 'android-local': 'profiles/android-local.json' },
    entry_cases: ['starter'],
    default_strategy: 'rule_only',
    platforms: ['android'],
    required_capabilities: [],
    constraints: {
      allowed_apps: ['com.pulse.target'],
      forbidden_actions: [],
    },
    dependencies: [],
    resources: {},
    secret_refs: [],
  };
}

function starterCase(): Record<string, unknown> {
  return {
    $schema: 'urn:pulse:schema:1-draft:case',
    case_id: 'starter',
    name: '启动靶场应用',
    goal: '确认目标应用可以从桌面工程启动',
    profile_refs: ['android-local'],
    parameters: {},
    locators: {},
    steps: [
      {
        id: 'launch',
        type: 'action',
        timeout_ms: 30_000,
        action: { kind: 'launch', app_id: 'com.pulse.target' },
      },
    ],
    inputs: {},
    preconditions: [],
    checks: [],
  };
}

function starterProfile(): Record<string, unknown> {
  return {
    $schema: 'urn:pulse:schema:1-draft:profile',
    profile_id: 'android-local',
    platform: 'android',
    environment: 'local',
    app_id: 'com.pulse.target',
    capabilities: [],
    locators: {},
  };
}

/** Create a schema-valid starter project with one launch case and one Android profile. */
export async function createProjectWorkspace(
  request: ProjectCreateRequest,
): Promise<ProjectWorkspace> {
  if (!IDENTIFIER.test(request.projectId)) {
    fail('工程标识必须以字母或下划线开头，且只包含字母、数字、下划线和连字符');
  }
  const name = request.name.trim();
  if (!name) fail('工程名称不能为空');
  const parent = await ensureDirectory(request.parentDir, '工程所在目录');
  const root = path.join(parent, request.projectId);
  try {
    await mkdir(root, { recursive: false });
  } catch {
    fail(`工程目录已存在：${root}`);
  }
  const files: Record<string, ProjectPackageFile> = {
    'manifest.json': {
      encoding: 'utf8',
      content: `${JSON.stringify(starterManifest(request.projectId, name), null, 2)}\n`,
    },
    'cases/starter.json': {
      encoding: 'utf8',
      content: `${JSON.stringify(starterCase(), null, 2)}\n`,
    },
    'profiles/android-local.json': {
      encoding: 'utf8',
      content: `${JSON.stringify(starterProfile(), null, 2)}\n`,
    },
  };
  try {
    const written = await writeExclusiveFiles(root, files);
    return { root, projectId: request.projectId, ...written };
  } catch (error) {
    await rm(root, { recursive: true, force: true });
    throw error;
  }
}
