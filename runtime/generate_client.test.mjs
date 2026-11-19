import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

test('generated Runtime client and OpenAPI match the live API models', () => {
  const result = spawnSync(process.execPath, ['runtime/generate_client.mjs', '--check'], {
    cwd: root,
    encoding: 'utf8',
    timeout: 60_000,
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('locate generation retains a typed JSON body and result instead of unknown', async () => {
  const spec = JSON.parse(await readFile(path.join(root, 'runtime/openapi.json'), 'utf8'));
  const client = await readFile(
    path.join(root, 'apps/desktop/src/renderer/shared/api/runtime.generated.ts'),
    'utf8',
  );
  const operation = spec.paths['/devices/{serial}/agent/locate'].post;
  assert.equal(operation.operationId, 'locate');
  assert.equal(operation.requestBody.required, true);
  assert.deepEqual(operation.requestBody.content['application/json'].schema, {
    $ref: '#/components/schemas/LocateRequest',
  });
  assert.deepEqual(operation.responses['200'].content['application/json'].schema, {
    $ref: '#/components/schemas/LocateResult',
  });
  assert.match(
    client,
    /async locate\(\s*serial: string,\s*body: PulseRuntimeApiSchemas\.LocateRequest,?\s*\): Promise<PulseRuntimeApiSchemas\.LocateResult>/,
  );
  assert.match(
    client,
    /this\.request<PulseRuntimeApiSchemas\.LocateResult>\(\s*'POST',\s*`\/devices\/\$\{encodeURIComponent\(serial\)\}\/agent\/locate`,\s*undefined,\s*body,/,
  );
  assert.deepEqual(spec.components.schemas.LocateResult.properties.status.enum, [
    'resolved',
    'ambiguous',
    'not_found',
    'indeterminate',
  ]);
});
