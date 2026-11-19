import { spawnSync } from 'node:child_process';
import { mkdir, readFile, readdir, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';
import { format, resolveConfig } from 'prettier';

const root = fileURLToPath(new URL('../../', import.meta.url));
const args = process.argv.slice(2);
const check = args.includes('--check');
const outputIndex = args.indexOf('--output-root');
const outputRoot = outputIndex < 0 ? root : path.resolve(args[outputIndex + 1] ?? '');
const allowed = new Set(['--check', '--output-root']);
for (let i = 0; i < args.length; i += 1) {
  if (!allowed.has(args[i])) throw new Error(`Unknown generator argument: ${args[i]}`);
  if (args[i] === '--output-root') {
    if (!args[i + 1] || args[i + 1].startsWith('--')) {
      throw new Error('--output-root requires a directory');
    }
    i += 1;
  }
}

const result = spawnSync('uv', ['run', '--no-sync', 'python', '-m', 'pulse_schema.generate'], {
  cwd: root,
  encoding: 'utf8',
  maxBuffer: 8 * 1024 * 1024,
  timeout: 30_000,
});
if (result.error || result.status !== 0) {
  throw new Error('Pydantic export failed. Install uv and run uv sync first.', {
    cause: result.error ?? new Error(result.stderr),
  });
}

const exported = JSON.parse(result.stdout);
const style = (await resolveConfig(path.join(root, '.prettierrc.json'))) ?? {};
const files = new Map();
for (const [kind, schema] of Object.entries(exported.schemas)) {
  files.set(
    `json/${kind}.schema.json`,
    await format(JSON.stringify(schema), { ...style, parser: 'json' }),
  );
}
const types = await compile(exported.schemas.document, 'PulseDocument', {
  bannerComment: '/* Generated from Pydantic via JSON Schema. Do not edit. Offline draft only. */',
  unreachableDefinitions: true,
  strictIndexSignatures: true,
  style,
});
const constants = [
  `export const CONTRACT_VERSION = ${JSON.stringify(exported.version)} as const;`,
  `export const CONTRACT_STATUS = ${JSON.stringify(exported.status)} as const;`,
  `export const SCHEMA_IDS = ${JSON.stringify(
    Object.fromEntries(
      Object.entries(exported.schemas).map(([kind, schema]) => [kind, schema.$id]),
    ),
  )} as const;`,
].join('\n');
files.set(
  'src/generated.ts',
  await format(`${types}\n${constants}\n`, { ...style, parser: 'typescript' }),
);

const output = path.join(outputRoot, 'packages/schema');
const drift = [];
for (const [relative, content] of files) {
  const destination = path.join(output, relative);
  if (check) {
    let actual;
    try {
      actual = await readFile(destination, 'utf8');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    if (actual !== content) drift.push(relative);
  } else {
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, content, 'utf8');
  }
}
let entries = [];
try {
  entries = await readdir(path.join(output, 'json'));
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
}
for (const name of entries) {
  const relative = `json/${name}`;
  if (!name.endsWith('.schema.json') || files.has(relative)) continue;
  if (check) drift.push(`${relative} (unexpected)`);
  else await unlink(path.join(output, relative));
}
if (drift.length) {
  console.error(`Schema generation drift: ${drift.join(', ')}. Run pnpm schema:generate.`);
  process.exitCode = 1;
} else {
  console.log(
    `Schema ${exported.version}: ${files.size} generated files ${check ? 'match' : 'written'}.`,
  );
}
