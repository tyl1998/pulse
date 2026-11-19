import { StatusPill } from '../../shared/ui';
import {
  describeRuntime,
  runtimeObservedAt,
  runtimePresentation,
  runtimeSource,
} from './runtimePresentation';
import { useRuntimeStatus } from './useRuntimeStatus';

export function RuntimeStatus() {
  const { state, lifecycle } = useRuntimeStatus();
  const { tone, Icon } = runtimePresentation[state.kind];

  return (
    <section aria-label="Runtime 状态">
      <p data-testid="runtime-status" data-state={state.kind}>
        <StatusPill
          value={describeRuntime(state)}
          source={runtimeSource(state)}
          observedAt={runtimeObservedAt(state)}
          tone={tone}
          icon={<Icon />}
        />
      </p>
      {lifecycle?.kind === 'incompatible' ? (
        <p data-testid="runtime-detail">
          Runtime v{lifecycle.health.version} 与当前桌面不兼容，需要 {lifecycle.required}
        </p>
      ) : null}
      {lifecycle?.kind === 'unhealthy' ? (
        <p data-testid="runtime-detail">Runtime 进程 {lifecycle.pid} 存活，但鉴权健康检查未通过</p>
      ) : null}
    </section>
  );
}
