/**
 * IPC contract shared by main / preload / renderer.
 * Domain types live in `ipc.types.ts`; channels and the bridge shape are generated from
 * `ipc.contract.json` by `pnpm ipc:generate`.
 */
export * from './ipc.types';
export * from './ipc.generated';
