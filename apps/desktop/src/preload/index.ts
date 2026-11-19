import { contextBridge, ipcRenderer } from 'electron';
import { createPulseBridge } from './bridge.generated';

// Whitelisted bridge only: never expose ipcRenderer or Node APIs directly. The bridge shape
// and channel names are generated from ipc.contract.json and checked by pnpm ipc:check.
const bridge = createPulseBridge((channel, ...args) => ipcRenderer.invoke(channel, ...args));

contextBridge.exposeInMainWorld('pulse', bridge);
