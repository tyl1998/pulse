import { useEffect, useState } from 'react';
import type { RuntimeStatus as RuntimeLifecycleStatus } from '../../../shared/ipc';
import { defaultDeps, RuntimeMonitor, type RuntimeState } from '../../shared/api/runtimeClient';

interface LifecycleDetail {
  forState: RuntimeState;
  status: RuntimeLifecycleStatus;
}

/**
 * Owns the single heartbeat monitor used by the shell and the legacy debug page.
 * Domain semantics stay outside shared UI: UI decides how to present, never whether a task passed.
 */
export function useRuntimeStatus() {
  const [state, setState] = useState<RuntimeState>({ kind: 'searching' });
  const [detail, setDetail] = useState<LifecycleDetail | null>(null);

  useEffect(() => {
    const bridge = window.pulse?.runtime;
    if (!bridge) return;
    const monitor = new RuntimeMonitor(
      defaultDeps(() => bridge.getConnection()),
      setState,
    );
    monitor.start();
    return () => monitor.stop();
  }, []);

  // The monitor only knows "no usable connection". Ask main why, so an unhealthy or
  // version-incompatible live Runtime is not mislabelled as "not running".
  // Bind details to the exact observation, including retries with the same kind.
  useEffect(() => {
    if (state.kind !== 'not-running' && state.kind !== 'disconnected') return;
    const getStatus = window.pulse?.runtime?.getStatus;
    if (!getStatus) return;
    let cancelled = false;
    void getStatus()
      .then((status) => {
        if (!cancelled) setDetail({ forState: state, status });
      })
      .catch(() => {
        if (!cancelled) setDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  const lifecycle = detail?.forState === state ? detail.status : null;
  return { state, lifecycle };
}
