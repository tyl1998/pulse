import { spawnSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';
import { format, resolveConfig } from 'prettier';

const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
const check = args.includes('--check');
for (const argument of args) {
  if (argument !== '--check') throw new Error(`Unknown generator argument: ${argument}`);
}

const generated = spawnSync(
  'uv',
  ['run', '--no-sync', 'python', '-m', 'pulse_runtime', 'openapi'],
  { cwd: root, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 60_000 },
);
if (generated.error || generated.status !== 0) {
  throw new Error('OpenAPI export failed. Install uv and run uv sync first.', {
    cause: generated.error ?? new Error(generated.stderr),
  });
}
const spec = JSON.parse(generated.stdout);
const style = (await resolveConfig(path.join(root, '.prettierrc.json'))) ?? {};

const componentPrefix = '#/components/schemas/';
const definitions = {};
for (const [name, schema] of Object.entries(spec.components?.schemas ?? {})) {
  definitions[name] = rewriteRefs(schema);
}

function rewriteRefs(value) {
  if (Array.isArray(value)) return value.map(rewriteRefs);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === '$ref' && typeof item === 'string' && item.startsWith(componentPrefix)) {
      result[key] = `#/definitions/${item.slice(componentPrefix.length)}`;
    } else {
      result[key] = rewriteRefs(item);
    }
  }
  return result;
}

function typeOf(schema) {
  if (!schema) return 'unknown';
  if (typeof schema.$ref === 'string') {
    if (schema.$ref.startsWith(componentPrefix)) {
      return `PulseRuntimeApiSchemas.${schema.$ref.slice(componentPrefix.length)}`;
    }
    if (schema.$ref.startsWith('#/definitions/')) {
      return `PulseRuntimeApiSchemas.${schema.$ref.slice('#/definitions/'.length)}`;
    }
    return 'unknown';
  }
  if (Array.isArray(schema.anyOf)) {
    return schema.anyOf.map((item) => typeOf(item)).join(' | ') || 'unknown';
  }
  if (Array.isArray(schema.oneOf)) {
    return schema.oneOf.map((item) => typeOf(item)).join(' | ') || 'unknown';
  }
  if (Array.isArray(schema.allOf)) {
    return schema.allOf.map((item) => typeOf(item)).join(' & ') || 'unknown';
  }
  if (Array.isArray(schema.enum)) {
    return schema.enum.map((item) => JSON.stringify(item)).join(' | ');
  }
  if (schema.const !== undefined) return JSON.stringify(schema.const);
  switch (schema.type) {
    case 'string':
      return 'string';
    case 'integer':
    case 'number':
      return 'number';
    case 'boolean':
      return 'boolean';
    case 'null':
      return 'null';
    case 'array':
      return `Array<${typeOf(schema.items)}>`;
    case 'object':
      if (schema.additionalProperties === true) return 'Record<string, unknown>';
      if (schema.additionalProperties && typeof schema.additionalProperties === 'object') {
        return `Record<string, ${typeOf(schema.additionalProperties)}>`;
      }
      return 'Record<string, unknown>';
    default:
      return 'unknown';
  }
}

function methodName(operationId, method, route) {
  const raw =
    operationId ??
    `${method}_${route
      .replace(/[{}]/g, '')
      .replace(/[^A-Za-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')}`;
  const parts = raw.split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (!parts.length) return 'request';
  const head = parts[0];
  const name = head.charAt(0).toLowerCase() + head.slice(1) + parts.slice(1).map(pascal).join('');
  return /^[0-9]/.test(name) ? `op${name}` : name;
}

function pascal(value) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function successType(operation) {
  for (const status of [200, 201, 202, 203, 204]) {
    const response = operation.responses?.[String(status)];
    if (!response) continue;
    if (status === 204) return 'void';
    for (const [mediaType, media] of Object.entries(response.content ?? {})) {
      if (media.schema) return typeOf(media.schema);
      if (mediaType.startsWith('text/')) return 'string';
    }
    return 'Record<string, unknown>';
  }
  return 'Record<string, unknown>';
}

const methods = [];
const usedNames = new Set();
for (const [route, pathItem] of Object.entries(spec.paths ?? {})) {
  for (const method of ['get', 'post', 'put', 'delete', 'patch']) {
    const operation = pathItem?.[method];
    if (!operation) continue;
    let name = methodName(operation.operationId, method, route);
    if (usedNames.has(name)) name = `${name}${pascal(method)}`;
    usedNames.add(name);
    const parameters = [...(pathItem.parameters ?? []), ...(operation.parameters ?? [])];
    const pathParams = parameters.filter((item) => item.in === 'path');
    const queryParams = parameters.filter((item) => item.in === 'query');
    const signature = [];
    for (const parameter of pathParams) {
      signature.push(`${parameter.name}: ${typeOf(parameter.schema)}`);
    }
    const bodySchema = operation.requestBody?.content?.['application/json']?.schema;
    if (bodySchema) signature.push(`body: ${typeOf(bodySchema)}`);
    if (queryParams.length) {
      const fields = queryParams
        .map((item) => `${item.name}${item.required ? '' : '?'}: ${typeOf(item.schema)}`)
        .join('; ');
      signature.push(`query?: { ${fields} }`);
    }
    const pathLiteral =
      '`' + route.replace(/\{([^}]+)\}/g, (_, name) => `\${encodeURIComponent(${name})}`) + '`';
    const callArgs = [JSON.stringify(method.toUpperCase()), pathLiteral];
    if (queryParams.length) callArgs.push('query');
    else if (bodySchema) callArgs.push('undefined');
    if (bodySchema) callArgs.push('body');
    const resultType = successType(operation);
    methods.push(
      `  async ${name}(${signature.join(', ')}): Promise<${resultType}> {
    return this.request<${resultType}>(${callArgs.join(', ')});
  }`,
    );
  }
}

const types = await compile(
  {
    $schema: 'http://json-schema.org/draft-07/schema#',
    title: 'PulseRuntimeApiSchemas',
    type: 'object',
    properties: {},
    definitions,
  },
  'PulseRuntimeApiSchemas',
  {
    bannerComment: '/* Generated from FastAPI OpenAPI. Do not edit. */',
    unreachableDefinitions: true,
    strictIndexSignatures: true,
    style,
  },
);
const typesBody = types.replace(/^\/\*[\s\S]*?\*\/\s*/, '').trim();

const client = `/* Generated from FastAPI OpenAPI. Do not edit. Run pnpm runtime:client. */
/* eslint-disable */

export namespace PulseRuntimeApiSchemas {
${typesBody}
}

export interface RuntimeApiClientOptions {
  baseUrl: string;
  token: string;
  fetch?: typeof fetch;
}

export class RuntimeApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(\`Runtime API HTTP \${status}\`);
    this.name = 'RuntimeApiError';
    this.status = status;
    this.detail = detail;
  }
}

export class RuntimeApiClient {
  constructor(private readonly options: RuntimeApiClientOptions) {}

  private async request<T>(
    method: string,
    path: string,
    query?: Record<string, unknown>,
    body?: unknown,
  ): Promise<T> {
    const url = new URL(path, this.options.baseUrl);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
      }
    }
    const headers: Record<string, string> = { Authorization: \`Bearer \${this.options.token}\` };
    const init: RequestInit = { method, headers };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const doFetch = this.options.fetch ?? fetch;
    const response = await doFetch(url, init);
    if (!response.ok) {
      const text = await response.text();
      let detail: unknown = text;
      try {
        detail = JSON.parse(text);
      } catch {
        // Preserve plain-text and malformed JSON errors without re-reading the response body.
      }
      throw new RuntimeApiError(response.status, detail);
    }
    if (response.status === 204) return undefined as T;
    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.includes('application/json')) return (await response.text()) as T;
    return (await response.json()) as T;
  }

${methods.join('\n\n')}
}

export interface RuntimeEventStreamOptions {
  baseUrl: string;
  token: string;
  since?: number;
  runId?: string;
}

export function runtimeEventStreamUrl(options: RuntimeEventStreamOptions): string {
  const url = new URL('/ws/events', options.baseUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('token', options.token);
  if (options.since !== undefined) url.searchParams.set('since', String(options.since));
  if (options.runId !== undefined) url.searchParams.set('run_id', options.runId);
  return url.toString();
}
`;

const specText = await format(JSON.stringify(spec), { ...style, parser: 'json' });
const clientText = await format(client, { ...style, parser: 'typescript' });
const outputs = new Map([
  ['runtime/openapi.json', specText],
  ['apps/desktop/src/renderer/shared/api/runtime.generated.ts', clientText],
]);

const drift = [];
for (const [relative, content] of outputs) {
  const destination = path.join(root, relative);
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
if (drift.length) {
  console.error(`Runtime API client drift: ${drift.join(', ')}. Run pnpm runtime:client.`);
  process.exitCode = 1;
} else {
  console.log(`Runtime API client ${check ? 'matches' : 'written'}.`);
}
