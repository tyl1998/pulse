import { Search, Unplug, Wifi, WifiOff } from 'lucide-react';
import type { RuntimeState } from '../../shared/api/runtimeClient';
import type { Tone } from '../../shared/ui';

export const runtimePresentation = {
  searching: { tone: 'neutral', Icon: Search },
  'not-running': { tone: 'neutral', Icon: Unplug },
  disconnected: { tone: 'warning', Icon: WifiOff },
  connected: { tone: 'info', Icon: Wifi },
} satisfies Record<RuntimeState['kind'], { tone: Tone; Icon: typeof Wifi }>;

export function describeRuntime(state: RuntimeState): string {
  switch (state.kind) {
    case 'searching':
      return '正在查找 Runtime…';
    case 'not-running':
      return `Runtime 未运行（${Math.round(state.retryInMs / 100) / 10}s 后重试）`;
    case 'disconnected':
      return `连接断开：${state.reason}（${Math.round(state.retryInMs / 100) / 10}s 后重连）`;
    case 'connected':
      return `已连接 · v${state.version} · pid ${state.pid} · 127.0.0.1:${state.port} · 心跳 #${state.seq}`;
  }
}

export function describeRuntimeShort(state: RuntimeState): string {
  switch (state.kind) {
    case 'searching':
      return '查找 Runtime';
    case 'not-running':
      return 'Runtime 未运行';
    case 'disconnected':
      return 'Runtime 断开';
    case 'connected':
      return `Runtime 已连接 v${state.version}`;
  }
}

export function runtimeSource(state: RuntimeState): string {
  return state.kind === 'connected' ? 'Runtime 心跳（本机接收时间）' : '本地 Runtime 连接观察器';
}

export function runtimeObservedAt(state: RuntimeState): number | null {
  return state.kind === 'connected' ? state.lastBeat : null;
}
