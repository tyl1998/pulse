import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { appendFile, lstat, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const generator = fileURLToPath(new URL('./generate.mjs', import.meta.url));
const expectedFiles = [
  ...['case', 'data', 'document', 'flow', 'manifest', 'profile'].map(
    (kind) => `packages/schema/json/${kind}.schema.json`,
  ),
  'packages/schema/src/generated.ts',
];

async function temporaryRoot(t) {
  const root = await mkdtemp(join(tmpdir(), 'pulse-schema-test-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

function runGenerator(outputRoot, args = [], hashSeed = '1') {
  const result = spawnSync(process.execPath, [generator, '--output-root', outputRoot, ...args], {
    cwd: outputRoot,
    env: {
      ...process.env,
      PYTHONHASHSEED: hashSeed,
      PYTHONDONTWRITEBYTECODE: '1',
      UV_OFFLINE: '1',
      UV_PYTHON_DOWNLOADS: 'never',
    },
    encoding: 'utf8',
    timeout: 45_000,
    maxBuffer: 8 * 1024 * 1024,
  });
  assert.ifError(result.error);
  assert.equal(result.signal, null, result.stderr);
  return result;
}

async function snapshot(root, relative = '') {
  const entries = [];
  for (const name of (await readdir(join(root, relative))).sort()) {
    const filename = relative ? `${relative}/${name}` : name;
    const destination = join(root, filename);
    const stats = await lstat(destination, { bigint: true });
    if (stats.isDirectory()) {
      entries.push({ path: `${filename}/` });
      entries.push(...(await snapshot(root, filename)));
    } else {
      assert.ok(stats.isFile(), `Unexpected non-file artifact: ${filename}`);
      // Metadata catches rewriting identical bytes as well as repairing drift.
      entries.push({
        path: filename,
        bytes: await readFile(destination),
        mtimeNs: stats.mtimeNs,
        ctimeNs: stats.ctimeNs,
        ino: stats.ino,
      });
    }
  }
  return entries;
}

test(
  'generation produces deterministic bytes across output roots and Python hash seeds',
  {
    timeout: 120_000,
  },
  async (t) => {
    const firstRoot = await temporaryRoot(t);
    const secondRoot = await temporaryRoot(t);
    const first = runGenerator(firstRoot, [], '1');
    const second = runGenerator(secondRoot, [], '8675309');
    assert.equal(first.status, 0, first.stderr);
    assert.equal(second.status, 0, second.stderr);

    const firstFiles = (await snapshot(firstRoot))
      .filter((entry) => 'bytes' in entry)
      .map(({ path, bytes }) => ({ path, bytes }));
    const secondFiles = (await snapshot(secondRoot))
      .filter((entry) => 'bytes' in entry)
      .map(({ path, bytes }) => ({ path, bytes }));
    assert.deepEqual(
      firstFiles.map(({ path }) => path),
      expectedFiles,
    );
    assert.deepEqual(secondFiles, firstFiles);
  },
);

test(
  '--check succeeds on unchanged artifacts without rewriting them',
  { timeout: 120_000 },
  async (t) => {
    const root = await temporaryRoot(t);
    const generated = runGenerator(root);
    assert.equal(generated.status, 0, generated.stderr);
    const before = await snapshot(root);

    const checked = runGenerator(root, ['--check'], '8675309');
    assert.equal(checked.status, 0, checked.stderr);
    assert.equal(checked.stderr, '');
    assert.equal(
      checked.stdout,
      `Schema 1-draft: ${expectedFiles.length} generated files match.\n`,
    );
    assert.deepEqual(await snapshot(root), before);
  },
);

test(
  'generation repairs generated drift and removes only stale schemas',
  { timeout: 120_000 },
  async (t) => {
    const root = await temporaryRoot(t);
    const generated = runGenerator(root);
    assert.equal(generated.status, 0, generated.stderr);
    const schemaRoot = join(root, 'packages/schema');
    const changed = join(schemaRoot, 'json/case.schema.json');
    const missing = join(schemaRoot, 'json/data.schema.json');
    const stale = join(schemaRoot, 'json/obsolete.schema.json');
    const ordinary = join(schemaRoot, 'json/keep.txt');
    const siblingSchema = join(schemaRoot, 'keep.schema.json');
    const outsideSchema = join(root, 'keep.schema.json');
    await appendFile(changed, '\n');
    await rm(missing);
    await writeFile(stale, '{}\n');
    await writeFile(ordinary, 'ordinary file\n');
    await writeFile(siblingSchema, 'sibling schema\n');
    await writeFile(outsideSchema, 'outside schema\n');

    const repaired = runGenerator(root, [], '8675309');
    assert.equal(repaired.status, 0, repaired.stderr);
    assert.equal(repaired.stderr, '');
    await assert.rejects(readFile(stale), { code: 'ENOENT' });
    assert.equal(await readFile(ordinary, 'utf8'), 'ordinary file\n');
    assert.equal(await readFile(siblingSchema, 'utf8'), 'sibling schema\n');
    assert.equal(await readFile(outsideSchema, 'utf8'), 'outside schema\n');

    const beforeCheck = await snapshot(root);
    const checked = runGenerator(root, ['--check']);
    assert.equal(checked.status, 0, checked.stderr);
    assert.deepEqual(await snapshot(root), beforeCheck);
  },
);

test(
  '--check reports an entirely missing output tree without creating it',
  { timeout: 60_000 },
  async (t) => {
    const root = await temporaryRoot(t);
    const checked = runGenerator(root, ['--check']);

    assert.equal(checked.status, 1, checked.stderr);
    assert.match(checked.stderr, /Schema generation drift:/);
    for (const filename of expectedFiles) {
      assert.ok(checked.stderr.includes(filename.replace('packages/schema/', '')), checked.stderr);
    }
    assert.deepEqual(await snapshot(root), []);
  },
);

for (const { name, relative, mutate, diagnostic = relative } of [
  {
    name: 'missing JSON',
    relative: 'json/case.schema.json',
    mutate: (file) => rm(file),
  },
  {
    name: 'changed JSON bytes',
    relative: 'json/manifest.schema.json',
    mutate: (file) => appendFile(file, '\n'),
  },
  {
    name: 'unexpected JSON',
    relative: 'json/obsolete.schema.json',
    mutate: (file) => writeFile(file, '{}\n'),
    diagnostic: 'json/obsolete.schema.json (unexpected)',
  },
  {
    name: 'missing TypeScript',
    relative: 'src/generated.ts',
    mutate: (file) => rm(file),
  },
  {
    name: 'changed TypeScript bytes',
    relative: 'src/generated.ts',
    mutate: (file) => appendFile(file, '// Intentional generator drift.\n'),
  },
]) {
  test(
    `--check rejects ${name} artifacts without repairing them`,
    { timeout: 120_000 },
    async (t) => {
      const root = await temporaryRoot(t);
      const generated = runGenerator(root);
      assert.equal(generated.status, 0, generated.stderr);
      await mutate(join(root, 'packages/schema', relative));
      const before = await snapshot(root);

      const checked = runGenerator(root, ['--check']);
      assert.equal(checked.status, 1, checked.stderr);
      assert.equal(
        checked.stderr,
        `Schema generation drift: ${diagnostic}. Run pnpm schema:generate.\n`,
      );
      assert.deepEqual(await snapshot(root), before);
    },
  );
}

test(
  'unknown generator arguments fail before writing artifacts',
  { timeout: 60_000 },
  async (t) => {
    const root = await temporaryRoot(t);
    const result = runGenerator(root, ['--unknown-test-option']);

    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /Unknown generator argument: --unknown-test-option/);
    assert.deepEqual(await snapshot(root), []);
  },
);
