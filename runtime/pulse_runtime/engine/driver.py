"""Case interpreter's device contract.

The interpreter owns control flow, variables and budgets; a driver performs one read or one
write on a concrete device. Nothing here is a second case schema or a public REST contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pulse_schema import models as m

ResolutionStatus = Literal["resolved", "not_found", "ambiguous", "indeterminate"]
ActionKind = Literal["tap", "long", "set_text", "swipe", "key", "launch", "stop", "clear"]
ActionStatus = Literal["completed", "failed", "rejected", "uncertain"]
RunStatus = Literal["passed", "failed", "blocked", "canceled", "interrupted"]
StopStatus = Literal["failed", "blocked", "interrupted", "canceled"]
ControlSource = Literal["auto", "manual", "recorder", "ai"]


@dataclass(frozen=True)
class Element:
    source: str
    text: str | None = None
    visible: bool | None = None
    states: dict[str, bool | None] = field(default_factory=dict)
    bounds: dict[str, int] | None = None


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    error_code: str | None = None
    matches: tuple[Element, ...] = ()
    action_target: dict[str, object] | None = None
    observation_id: str | None = None
    action_target_error: str | None = None
    query_complete: bool = True
    candidate_index: int | None = None
    candidate_by: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.matches)


@dataclass(frozen=True)
class RunLease:
    run_id: str
    control_epoch: int


@dataclass(frozen=True)
class ActionPlan:
    kind: ActionKind
    params: dict[str, object]
    target_ref: str | None
    target: Resolution | None
    step_id: str
    timeout_ms: int
    agent_timeout_ms: int = 5000
    screen_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    error_code: str | None = None
    action_id: str | None = None
    observation_id: str | None = None


@dataclass(frozen=True)
class ExecutionEnvironment:
    """Observed run environment for reports; unknown values stay ``None``, never guessed."""

    platform: str | None = None
    device_serial: str | None = None
    device_model: str | None = None
    android_api: str | None = None
    agent_pid: int | None = None
    runtime_version: str | None = None
    ledger_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "device": {
                "serial": self.device_serial,
                "model": self.device_model,
                "android_api": self.android_api,
            },
            "agent_pid": self.agent_pid,
            "runtime_version": self.runtime_version,
            "ledger_path": self.ledger_path,
        }


@runtime_checkable
class ReportSink(Protocol):
    """Byte attachments captured inside a run before the driver releases the device."""

    async def screenshot(self, run_id: str, step_id: str) -> dict[str, object]: ...


@runtime_checkable
class DebugHook(Protocol):
    """Optional step-boundary hook used by the local debugger transport.

    The interpreter only calls it before a step starts. A hook must not perform device reads or
    writes; pausing/stepping is transport state, not a second execution policy.
    """

    async def before_step(self, step_id: str, step_type: str) -> None: ...


class DriverFault(Exception):
    """A bounded driver failure that already carries its interpreter terminal class."""

    def __init__(self, status: StopStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class CaseDriver(Protocol):
    app_id: str

    async def begin(self, run_id: str, *, source: ControlSource) -> RunLease: ...

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        """Resolve one target. ``primary`` reorders the same declared candidates only.

        It is a speed hint from local experience: the returned set must still satisfy the
        locator's own ``expect`` and must not depend on the hint for correctness.
        """
        ...

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult: ...

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]: ...

    def environment(self) -> ExecutionEnvironment:
        """Environment metadata for the report; ``None`` fields mean "not observed"."""
        return ExecutionEnvironment()

    def report_sink(
        self,
        serial: str | None,
        session_id: str | None,
        run_id: str,
        evidence_root: Path | None,
    ) -> ReportSink | None:
        """Optional byte-attachment sink for one run; ``None`` disables attachments."""
        return None
