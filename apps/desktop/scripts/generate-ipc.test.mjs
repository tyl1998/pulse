import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
  contractPath,
  loadContract,
  renderFiles,
  renderIpcModule,
  validateContract,
} from './generate-ipc.mjs';

const script = fileURLToPath(new URL('./generate-ipc.mjs', import.meta.url));

test('contract validates and generated outputs match the repository', () => {
  const result = spawnSync(process.execPath, [script, '--check'], {
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /IPC contract matches/);
});

test('generation is deterministic and keeps channel order', async () => {
  const contract = await loadContract();
  const first = await renderFiles(contract);
  const second = await renderFiles(contract);
  assert.deepEqual([...first], [...second]);
  const ipc = first.get('apps/desktop/src/shared/ipc.generated.ts');
  assert.ok(ipc);
  assert.ok(ipc.indexOf("appInfo: 'pulse:app-info'") < ipc.indexOf('runtimeEnsure'));
  assert.match(ipc, /getConnection\(\): Promise<RuntimeConnection \| null>;/);
});

test('duplicate keys and channels are rejected', () => {
  const base = {
    version: 1,
    channels: [
      {
        key: 'a',
        channel: 'pulse:a',
        domain: 'app',
        method: 'a',
        params: [],
        result: 'AppInfo',
      },
    ],
  };
  assert.doesNotThrow(() => validateContract(base));
  assert.throws(
    () =>
      validateContract({
        ...base,
        channels: [base.channels[0], { ...base.channels[0], method: 'b' }],
      }),
    /key "a" is duplicated/,
  );
  assert.throws(
    () =>
      validateContract({
        ...base,
        channels: [base.channels[0], { ...base.channels[0], key: 'b' }],
      }),
    /channel "pulse:a" is duplicated/,
  );
});

test('unsafe type text and non-pulse channels are rejected', () => {
  const entry = {
    key: 'a',
    channel: 'pulse:a',
    domain: 'app',
    method: 'a',
    params: [],
    result: 'AppInfo',
  };
  assert.throws(() => validateContract({ channels: [{ ...entry, channel: 'app:a' }] }), /pulse:/);
  assert.throws(
    () =>
      validateContract({
        channels: [{ ...entry, result: 'AppInfo); process.exit(1' }],
      }),
    /safe TypeScript type/,
  );
  assert.throws(
    () =>
      validateContract({
        channels: [
          {
            ...entry,
            params: [
              { name: 'first', type: 'string', optional: true },
              { name: 'second', type: 'string' },
            ],
          },
        ],
      }),
    /optional parameters must come last/,
  );
});

test('parameter and optional parameter signatures are rendered', () => {
  const rendered = renderIpcModule({
    version: 1,
    channels: [
      {
        key: 'pickProject',
        channel: 'pulse:fs-pick-project',
        domain: 'fs',
        method: 'pickProject',
        params: [
          { name: 'title', type: 'string' },
          { name: 'start', type: 'string', optional: true },
        ],
        result: 'AppInfo | null',
      },
    ],
  });
  assert.match(
    rendered,
    /pickProject\(title: string, start\?: string\): Promise<AppInfo \| null>;/,
  );
  assert.match(rendered, /import type \{ AppInfo \} from '\.\/ipc\.types';/);
  assert.ok(contractPath.endsWith('ipc.contract.json'));
});
