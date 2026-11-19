import { TriangleAlert } from 'lucide-react';
import { StatusPill } from '../../shared/ui';
import {
  describeRuntimeShort,
  runtimeObservedAt,
  runtimePresentation,
  runtimeSource,
} from './runtimePresentation';
import { useRuntimeStatus } from './useRuntimeStatus';

/** Compact top-bar Runtime state. Still carries source and observation time; never claims a task state. */
export function RuntimeIndicator() {
  const { state, lifecycle } = useRuntimeStatus();
  const { tone, Icon: StateIcon } = runtimePresentation[state.kind];
  const incompatible = lifecycle?.kind === 'incompatible';
  const unhealthy = lifecycle?.kind === 'unhealthy';
  const degraded = incompatible || unhealthy;

  return (
    <div
      className="app-connection"
      data-testid="runtime-indicator"
      data-state={degraded ? 'degraded' : state.kind}
    >
      <StatusPill
        className="app-connection-pill"
        value={
          incompatible
            ? 'Runtime 版本不兼容'
            : unhealthy
              ? 'Runtime 健康检查未通过'
              : describeRuntimeShort(state)
        }
        source={
          incompatible ? '主进程版本检查' : unhealthy ? '主进程健康检查' : runtimeSource(state)
        }
        observedAt={degraded ? null : runtimeObservedAt(state)}
        tone={degraded ? 'warning' : tone}
        icon={degraded ? <TriangleAlert /> : <StateIcon />}
      />
    </div>
  );
}
