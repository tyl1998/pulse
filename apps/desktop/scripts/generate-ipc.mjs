import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { format, resolveConfig } from 'prettier';

const root = fileURLToPath(new URL('../../../', import.meta.url));
export const contractPath = path.join(root, 'apps/desktop/ipc.contract.json');
export const outputs = new Map([
  ['apps/desktop/src/shared/ipc.generated.ts', renderIpcModule],
  ['apps/desktop/src/preload/bridge.generated.ts', renderPreloadBridge],
]);

const IDENTIFIER = /^[A-Za-z_$][\w$]*$/;
const TYPE_TEXT = /^[A-Za-z_$][\w$.[\]<>|: ,"]*$/;
const TYPE_KEYWORDS = new Set([
  'null',
  'undefined',
  'true',
  'false',
  'string',
  'number',
  'boolean',
  'unknown',
  'any',
  'void',
  'never',
  'object',
  'Array',
  'Record',
  'Promise',
]);

function fail(message) {
  throw new Error(`Invalid IPC contract: ${message}`);
}

export function validateContract(contract) {
  if (!contract || typeof contract !== 'object') fail('contract must be an object');
  if (!Array.isArray(contract.channels) || contract.channels.length === 0) {
    fail('channels must be a non-empty array');
  }
  const keys = new Set();
  const channels = new Set();
  const domains = new Map();
  for (const [index, entry] of contract.channels.entries()) {
    const where = `channels[${index}]`;
    if (!entry || typeof entry !== 'object') fail(`${where} must be an object`);
    if (!IDENTIFIER.test(entry.key ?? '')) fail(`${where}.key is not an identifier`);
    if (!IDENTIFIER.test(entry.domain ?? '')) fail(`${where}.domain is not an identifier`);
    if (!IDENTIFIER.test(entry.method ?? '')) fail(`${where}.method is not an identifier`);
    if (typeof entry.channel !== 'string' || !entry.channel.startsWith('pulse:')) {
      fail(`${where}.channel must start with "pulse:"`);
    }
    if (keys.has(entry.key)) fail(`${where}.key "${entry.key}" is duplicated`);
    if (channels.has(entry.channel)) fail(`${where}.channel "${entry.channel}" is duplicated`);
    if (typeof entry.result !== 'string' || !TYPE_TEXT.test(entry.result)) {
      fail(`${where}.result is not a safe TypeScript type`);
    }
    if (!Array.isArray(entry.params)) fail(`${where}.params must be an array`);
    const methodNames = domains.get(entry.domain) ?? new Set();
    if (methodNames.has(entry.method)) {
      fail(`${where} duplicates ${entry.domain}.${entry.method}`);
    }
    for (const [paramIndex, param] of entry.params.entries()) {
      const paramWhere = `${where}.params[${paramIndex}]`;
      if (!param || typeof param !== 'object') fail(`${paramWhere} must be an object`);
      if (!IDENTIFIER.test(param.name ?? '')) fail(`${paramWhere}.name is not an identifier`);
      if (typeof param.type !== 'string' || !TYPE_TEXT.test(param.type)) {
        fail(`${paramWhere}.type is not a safe TypeScript type`);
      }
      if (param.optional !== undefined && typeof param.optional !== 'boolean') {
        fail(`${paramWhere}.optional must be a boolean`);
      }
      if (param.optional === true && paramIndex !== entry.params.length - 1) {
        fail(`${paramWhere} optional parameters must come last`);
      }
    }
    keys.add(entry.key);
    channels.add(entry.channel);
    methodNames.add(entry.method);
    domains.set(entry.domain, methodNames);
  }
}

export async function loadContract() {
  const contract = JSON.parse(await readFile(contractPath, 'utf8'));
  validateContract(contract);
  return contract;
}

function typeImports(contract) {
  const names = new Set(['PulseBridge']);
  for (const entry of contract.channels) {
    for (const source of [entry.result, ...entry.params.map((param) => param.type)]) {
      for (const name of source.match(/[A-Za-z_$][\w$]*/g) ?? []) {
        if (!TYPE_KEYWORDS.has(name)) names.add(name);
      }
    }
  }
  return [...names].sort();
}

function byDomain(contract) {
  const domains = new Map();
  for (const entry of contract.channels) {
    const methods = domains.get(entry.domain) ?? [];
    methods.push(entry);
    domains.set(entry.domain, methods);
  }
  return domains;
}

function paramSignature(entry) {
  return entry.params
    .map((param) => `${param.name}${param.optional ? '?' : ''}: ${param.type}`)
    .join(', ');
}

function callArguments(entry) {
  return entry.params.map((param) => `, ${param.name}`).join('');
}

export function renderIpcModule(contract) {
  const channelLines = contract.channels
    .map((entry) => `  ${entry.key}: ${JSON.stringify(entry.channel)},`)
    .join('\n');
  const imports = typeImports(contract)
    .filter((name) => name !== 'PulseBridge')
    .join(', ');
  const interfaceParts = [];
  for (const [domain, entries] of byDomain(contract)) {
    const methods = entries
      .map((entry) => `    ${entry.method}(${paramSignature(entry)}): Promise<${entry.result}>;`)
      .join('\n');
    interfaceParts.push(`  ${domain}: {\n${methods}\n  };`);
  }
  return `/* Generated from ipc.contract.json. Do not edit. Run pnpm ipc:generate. */
${imports ? `import type { ${imports} } from './ipc.types';\n` : ''}
export const IPC_CHANNELS = {
${channelLines}
} as const;

export type IpcChannel = (typeof IPC_CHANNELS)[keyof typeof IPC_CHANNELS];

export interface PulseBridge {
${interfaceParts.join('\n')}
}
`;
}

function renderPreloadBridge(contract) {
  const imports = typeImports(contract).join(', ');
  const body = [...byDomain(contract)]
    .map(([domain, entries]) => {
      const methods = entries
        .map((entry) => {
          const signature = paramSignature(entry);
          return `      ${entry.method}: (${signature}) =>
        invoke(IPC_CHANNELS.${entry.key}${callArguments(entry)}) as Promise<${entry.result}>,`;
        })
        .join('\n');
      return `    ${domain}: {\n${methods}\n    },`;
    })
    .join('\n');
  return `/* Generated from ipc.contract.json. Do not edit. Run pnpm ipc:generate. */
import { IPC_CHANNELS } from '../shared/ipc';
import type { ${imports} } from '../shared/ipc';

export type IpcInvoke = (channel: string, ...args: unknown[]) => Promise<unknown>;

export function createPulseBridge(invoke: IpcInvoke): PulseBridge {
  return {
${body}
  };
}
`;
}

export async function renderFiles(contract) {
  const style = (await resolveConfig(path.join(root, '.prettierrc.json'))) ?? {};
  const rendered = new Map();
  for (const [relative, render] of outputs) {
    rendered.set(
      relative,
      await format(render(contract), {
        ...style,
        parser: 'typescript',
        filepath: path.join(root, relative),
      }),
    );
  }
  return rendered;
}

async function main() {
  const args = process.argv.slice(2);
  const check = args.includes('--check');
  for (const argument of args) {
    if (argument !== '--check') throw new Error(`Unknown generator argument: ${argument}`);
  }
  const contract = await loadContract();
  const rendered = await renderFiles(contract);
  const drift = [];
  for (const [relative, content] of rendered) {
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
    console.error(`IPC contract drift: ${drift.join(', ')}. Run pnpm ipc:generate.`);
    process.exitCode = 1;
  } else {
    console.log(`IPC contract ${check ? 'matches' : 'written'}.`);
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  await main();
}
