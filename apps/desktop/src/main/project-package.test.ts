import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, expect, test } from 'vitest';
import {
  collectProjectPackage,
  createProjectWorkspace,
  importProjectPackage,
  normalizeProjectPackagePath,
  readProjectPackage,
  validateProjectPackage,
  writeProjectPackage,
  type ProjectPackage,
  type ProjectPackageFile,
} from './project-package';

const temporary: string[] = [];

async function tempDir(): Promise<string> {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'pulse-project-package-'));
  temporary.push(directory);
  return directory;
}

afterEach(async () => {
  await Promise.all(
    temporary.splice(0).map((directory) => rm(directory, { recursive: true, force: true })),
  );
});

function clone(packageValue: ProjectPackage): ProjectPackage {
  return JSON.parse(JSON.stringify(packageValue)) as ProjectPackage;
}

function utf8(content: string): ProjectPackageFile {
  return { encoding: 'utf8', content };
}

test('creates a schema-shaped starter project and exports only declared assets', async () => {
  const parent = await tempDir();
  const workspace = await createProjectWorkspace({
    parentDir: parent,
    projectId: 'demo_project',
    name: '演示工程',
  });
  expect(workspace.fileCount).toBe(3);
  expect(workspace.totalBytes).toBeGreaterThan(0);

  const packageValue = await collectProjectPackage(workspace.root);
  expect(Object.keys(packageValue.files)).toEqual([
    'manifest.json',
    'cases/starter.json',
    'profiles/android-local.json',
  ]);
  expect(packageValue.project_id).toBe('demo_project');
  expect(JSON.stringify(packageValue)).not.toContain(parent);
  expect(validateProjectPackage(packageValue)).toEqual(packageValue);
});

test('writes, reads and imports a package without overwriting the source', async () => {
  const parent = await tempDir();
  const workspace = await createProjectWorkspace({
    parentDir: parent,
    projectId: 'roundtrip',
    name: 'Round trip',
  });
  const packageValue = await collectProjectPackage(workspace.root);
  const target = path.join(parent, 'export', 'roundtrip.json');
  const written = await writeProjectPackage(target, packageValue);
  expect(written.targetPath).toBe(normalizeProjectPackagePath(target));
  expect(written.targetPath.endsWith('.pulse-project.json')).toBe(true);

  const restored = await readProjectPackage(written.targetPath);
  const importedParent = await tempDir();
  const imported = await importProjectPackage(restored, importedParent);
  expect(imported.root).toBe(path.join(importedParent, 'roundtrip'));
  for (const relative of ['manifest.json', 'cases/starter.json', 'profiles/android-local.json']) {
    const [original, copy] = await Promise.all([
      readFile(path.join(workspace.root, relative), 'utf8'),
      readFile(path.join(imported.root, relative), 'utf8'),
    ]);
    expect(copy).toBe(original);
  }

  await expect(importProjectPackage(restored, importedParent)).rejects.toThrow(/已存在/);
});

test('rejects personal absolute paths and plaintext secret fields', async () => {
  const parent = await tempDir();
  const workspace = await createProjectWorkspace({
    parentDir: parent,
    projectId: 'unsafe',
    name: 'Unsafe',
  });
  const packageValue = await collectProjectPackage(workspace.root);

  const withPath = clone(packageValue);
  const caseFile = withPath.files['cases/starter.json'];
  if (!caseFile) throw new Error('starter case missing');
  caseFile.content = JSON.stringify({
    ...(JSON.parse(caseFile.content) as Record<string, unknown>),
    source: '/Users/qa/private/project',
  });
  expect(() => validateProjectPackage(withPath)).toThrow(/个人绝对路径/);

  const withSecret = clone(packageValue);
  const secretCase = withSecret.files['cases/starter.json'];
  if (!secretCase) throw new Error('starter case missing');
  secretCase.content = JSON.stringify({
    ...(JSON.parse(secretCase.content) as Record<string, unknown>),
    inputs: { password: 'hunter2' },
  });
  expect(() => validateProjectPackage(withSecret)).toThrow(/明文密钥/);

  const withSecretReference = clone(packageValue);
  const referenceCase = withSecretReference.files['cases/starter.json'];
  if (!referenceCase) throw new Error('starter case missing');
  referenceCase.content = JSON.stringify({
    ...(JSON.parse(referenceCase.content) as Record<string, unknown>),
    inputs: { password: { secret_ref: 'qa_password' } },
  });
  expect(() => validateProjectPackage(withSecretReference)).not.toThrow();
});

test('rejects undeclared files, traversal paths and metadata mismatch', async () => {
  const parent = await tempDir();
  const workspace = await createProjectWorkspace({
    parentDir: parent,
    projectId: 'strict',
    name: 'Strict',
  });
  const packageValue = await collectProjectPackage(workspace.root);

  const extra = clone(packageValue);
  extra.files['runs/report.json'] = utf8('{}');
  expect(() => validateProjectPackage(extra)).toThrow(/未授权路径/);

  const traversal = clone(packageValue);
  traversal.files['../escape.json'] = utf8('{}');
  expect(() => validateProjectPackage(traversal)).toThrow(/非相对路径|未授权路径/);

  const missing = clone(packageValue);
  delete missing.files['profiles/android-local.json'];
  expect(() => validateProjectPackage(missing)).toThrow(/缺少已声明文件/);

  const mismatch = clone(packageValue);
  mismatch.name = 'Different name';
  expect(() => validateProjectPackage(mismatch)).toThrow(/名称或修订号/);
});

test('rejects invalid identifiers and existing target directories', async () => {
  const parent = await tempDir();
  await expect(
    createProjectWorkspace({ parentDir: parent, projectId: '../escape', name: 'bad' }),
  ).rejects.toThrow(/工程标识/);
  await createProjectWorkspace({ parentDir: parent, projectId: 'taken', name: 'Taken' });
  await expect(
    createProjectWorkspace({ parentDir: parent, projectId: 'taken', name: 'Again' }),
  ).rejects.toThrow(/已存在/);
});
