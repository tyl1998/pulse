"""Runtime-owned Android discovery, explicit preparation, observation and bounded actions.

Agent wire v1 and the observation draft are independent of case and media/WS protocols.
Locators are read-only projections; no interpreter, control gate or automatic recovery.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hmac
import logging
import math
import time
from collections.abc import Callable
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from pulse_runtime.drivers.android.actions import (
    ADB_ACTIONS,
    DISPATCH_MAX_AGE,
    INTERACTIVE_KINDS,
    SOURCE_MAX_AGE,
    ActionBody,
    ObservationBinding,
    bind_observation,
    validate_source,
)
from pulse_runtime.drivers.android.agent_action import (
    UNCERTAIN_CODES,
    ActionExchange,
    AdbActionExchange,
)
from pulse_runtime.drivers.android.agent_observe import ObserveExchange
from pulse_runtime.drivers.android.agent_transport import (
    AccessibilitySettings,
    AdbdPeer,
    AgentHealth,
    AndroidBackend,
    ConnectionFault,
    DeviceRow,
    ForwardLease,
    InstalledAgent,
    canonical_uuid,
    complete_mutation,
    configured_apk,
    finish_cleanup,
    validate_serial,
)
from pulse_runtime.gate.control import ControlGate, GateError
from pulse_runtime.gate.ledger import ActionLedger, LedgerError
from pulse_runtime.perception.locators import LocateBody, observation_metadata, resolve_locator
from pulse_runtime.perception.observation import failed_sources, normalize_observation

if TYPE_CHECKING:
    from pulse_schema import models as m

log = logging.getLogger(__name__)
MANUAL_ACCESSIBILITY = (
    "On the device, open Settings > Accessibility > Installed services > Pulse Agent. "
    "If offered, first allow restricted settings in Settings > Apps > Pulse Agent. "
    "OEM or enterprise policy may require administrator approval. Runtime never clicks dialogs."
    " If already enabled but not connected, check pending/uncertain actions, then manually "
    "switch only Pulse Agent off and on."
)
MANUAL_RECOVERY = (
    "Check the actual device state and any uncertain action first. Manually recover the Agent "
    "process, then connect with manual_recovery_confirmed=true. Runtime never restarts it."
)
MANUAL_CONFIGURATION = (
    "Confirm there is no unresolved action, manually disable only Pulse Agent accessibility, "
    "then prepare it again. A running listener cannot reload its private adbd pin. "
    "Runtime does not force-stop, toggle an enabled service, or bypass Agent isolation."
)

FAULT_STATES = {
    "adb_unauthorized": "adb_unauthorized",
    "adb_offline": "offline",
    "device_disconnected": "disconnected",
    "unsupported_android_api": "unsupported_android_api",
    "unsupported_android_user": "unsupported_configuration",
    "agent_missing": "agent_missing",
    "version_incompatible": "agent_incompatible",
    "wire_incompatible": "agent_incompatible",
    "build_incompatible": "agent_incompatible",
    "build_signature_incompatible": "agent_incompatible",
    "agent_package_already_exists": "agent_incompatible",
    "release_configuration_unsupported": "unsupported_configuration",
    "run_as_unsupported": "unsupported_configuration",
    "accessibility_disabled": "accessibility_disabled",
    "permission_revoked": "permission_revoked",
    "accessibility_enable_failed": "manual_authorization_required",
    "accessibility_policy_blocked": "manual_authorization_required",
    "accessibility_setting_denied": "manual_authorization_required",
    "accessibility_settings_changed": "manual_authorization_required",
    "accessibility_not_connected": "manual_authorization_required",
    "peer_configuration_missing": "configuration_required",
    "peer_configuration_invalid": "configuration_required",
    "peer_configuration_stale": "configuration_required",
    "configuration_in_use": "configuration_in_use",
    "recovery_required": "recovery_required",
    "action_uncertain": "recovery_required",
    "manual_recovery_not_observed": "recovery_required",
    "timeout": "connection_timeout",
    "cancelled": "cancelled",
    "agent_unavailable": "agent_not_connected",
    "credential_unavailable": "agent_not_connected",
    "agent_not_connected": "agent_not_connected",
    "native_cleanup_pending": "cleanup_pending",
}
MANUAL_AUTH_FAULTS = {
    "accessibility_disabled",
    "permission_revoked",
    "accessibility_enable_failed",
    "accessibility_policy_blocked",
    "accessibility_setting_denied",
    "accessibility_settings_changed",
    "accessibility_not_connected",
    "agent_not_connected",
    "agent_unavailable",
    "credential_unavailable",
}
OBSERVATION_FAULTS = frozenset(FAULT_STATES) | {
    "stale_session",
    "generation_changed",
    "connection_changed",
    "agent_lifecycle_changed",
    "authentication_failed",
    "forward_replaced",
    "forward_unavailable",
    "agent_observation_rejected",
    "invalid_message",
    "invalid_observation",
    "invalid_source_data",
    "invalid_framing",
    "observation_protocol_invalid",
    "observation_output_limit",
    "response_too_large",
    "observation_truncated",
    "observation_timeout",
    "observation_failed",
    "platform_operation_pending",
    "runtime_stopped",
    "disconnected",
    "busy",
    "window_unavailable",
}
# Package-scoped actions whose point is to leave the foreground. Their post-observation has
# no controllable window when the device lands on a launcher that exposes no Accessibility
# tree, so an empty read there is the expected evidence rather than a failed observation.
DEPARTING_ACTIONS = frozenset({"key", "launch", "stop"})


def empty_window(observation: dict[str, object]) -> bool:
    """True only for a validated read that found no window and therefore no tree data."""
    sources = observation.get("sources")
    framing = observation.get("framing")
    if not isinstance(sources, dict) or not isinstance(framing, dict):
        return False
    tree = sources.get("tree")
    return (
        isinstance(tree, dict)
        and tree.get("status") == "unavailable"
        and tree.get("data") is None
        and framing.get("source_data_validated") is True
        and observation.get("status") == "completed"
    )


ACTION_FAULTS = OBSERVATION_FAULTS | {
    "action_failed",
    "action_timeout",
    "action_protocol_invalid",
    "action_output_limit",
    "action_terminal_missing",
    "action_request_reused",
    "action_operation_reused",
    "action_request_limit",
    "stale_control_epoch",
    "stale_control_session",
    "control_owned_by_other_source",
    "control_epoch_changed",
    "control_ledger_unavailable",
    "run_not_active",
    "manual_recovery_not_confirmed",
    "action_source_stale",
    "action_source_unavailable",
    "action_source_incomplete",
    "action_target_unavailable",
    "action_coordinates_out_of_range",
    "coordinate_space_mismatch",
    "coordinate_mapping_unavailable",
    "unsupported_display",
    "rotation_unverified",
    "display_geometry_unavailable",
    "display_geometry_changed",
    "page_changed",
    "sensitive_target",
    "protected_target",
    "target_changed",
    "target_unavailable",
    "dispatch_rejected",
    "gesture_cancelled",
    "platform_failed",
    "internal_error",
    "unsupported_action",
    "adb_action_rejected",
    "adb_command_failed",
    "adb_unavailable",
    "cleanup_required",
    "operation_in_progress",
    "observation_in_progress",
    "action_in_progress",
    "invalid_serial",
    "manager_closed",
    "stopped",
    "accessibility_unavailable",
    "accessibility_interrupted",
    "response_too_large",
    "credential_unavailable",
}


@dataclass(frozen=True)
class Generation:
    transport_id: str | None
    peer: AdbdPeer
    agent_pid: int
    token: str = field(repr=False)

    def matches(self, other: Generation) -> bool:
        return (
            self.transport_id == other.transport_id
            and self.peer == other.peer
            and self.agent_pid == other.agent_pid
            and hmac.compare_digest(self.token, other.token)
        )


@dataclass
class AgentSession:
    id: str
    generation: Generation = field(repr=False)
    forward: ForwardLease = field(repr=False)
    health: AgentHealth
    observation_ids: set[str] = field(default_factory=set, repr=False)
    latest_observation: ObservationBinding | None = field(default=None, repr=False)


@dataclass
class ActiveObservation:
    request_id: str
    session_id: str
    exchange: ObserveExchange = field(repr=False)
    task: asyncio.Task[object] = field(repr=False)
    reason: str | None = None
    phase: str = "checking"
    latest_capabilities: dict[str, object] | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def abort(self, reason: str, *, interrupt: bool = True) -> None:
        if self.reason is None:
            self.reason = reason
        if reason not in {"cancelled", "observation_timeout", "disconnected", "runtime_stopped"}:
            # Never address an old request through a replaced forward/credential generation.
            self.exchange.invalidate()
        if interrupt and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()


@dataclass
class ActiveAction:
    request_id: str
    operation_id: str
    session_id: str
    kind: str
    exchange: ActionExchange = field(repr=False)
    task: asyncio.Task[object] = field(repr=False)
    reason: str | None = None
    phase: str = "checking"
    settled: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def abort(self, reason: str, *, interrupt: bool = True) -> None:
        if self.reason is None:
            self.reason = reason
        if reason not in {"cancelled", "action_timeout", "disconnected", "runtime_stopped"}:
            self.exchange.invalidate()
        if interrupt and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()


@dataclass
class DeviceSlot:
    serial: str
    row: DeviceRow
    status: str = "disconnected"
    code: str | None = None
    api: int | None = None
    user: int | None = None
    installed: InstalledAgent | None = None
    observed_at_ms: int | None = None
    session: AgentSession | None = field(default=None, repr=False)
    desired: bool = False
    previously_ready: bool = False
    last_invalidation: str | None = None
    peer_owner: str | None = None
    peer_value: AdbdPeer | None = None
    accessibility_lease: tuple[AccessibilitySettings, AccessibilitySettings] | None = field(
        default=None, repr=False
    )
    pending_forwards: list[ForwardLease] = field(default_factory=list, repr=False)
    cleanup_pending: bool = False
    private_cleanup_pending: bool = False
    awaiting_authorization: bool = False
    authorization_reason: str | None = None
    quarantine: Generation | None = field(default=None, repr=False)
    quarantine_lifecycle: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    operation: asyncio.Task[object] | None = field(default=None, repr=False)
    operation_id: str | None = None
    observation: ActiveObservation | None = field(default=None, repr=False)
    native_cleanup_guard: bool = False
    action: ActiveAction | None = field(default=None, repr=False)
    action_requests: set[str] = field(default_factory=set, repr=False)
    action_operations: set[str] = field(default_factory=set, repr=False)


class DeviceConnectionManager:
    def __init__(
        self,
        backend: AndroidBackend | None = None,
        *,
        apk: Path | None = None,
        poll_interval: float = 2.0,
        readiness_wait: float = 3.0,
        observation_factory: Callable[[str], ObserveExchange] | None = None,
        action_factory: Callable[[str], ActionExchange] | None = None,
        adb_action_factory: Callable[[str, AndroidBackend], ActionExchange] | None = None,
        ledger: ActionLedger | None = None,
        ledger_path: Path | str | None = None,
        gate: ControlGate | None = None,
    ) -> None:
        self.backend = backend if backend is not None else AndroidBackend()
        self.apk = apk if apk is not None else configured_apk()
        self.poll_interval = poll_interval
        self.readiness_wait = readiness_wait
        self.observation_factory = observation_factory or ObserveExchange
        self.action_factory = action_factory or ActionExchange
        self.adb_action_factory = adb_action_factory or AdbActionExchange
        if ledger is None and gate is not None:
            ledger = gate.ledger
        self.ledger = ledger if ledger is not None else ActionLedger(ledger_path)
        self.gate = gate if gate is not None else ControlGate(self.ledger)
        self.slots: dict[str, DeviceSlot] = {}
        self.watcher: asyncio.Task[None] | None = None
        self.closing: asyncio.Task[None] | None = None
        self.closed = False

    def slot(self, serial: str) -> DeviceSlot:
        validate_serial(serial)
        if self.closed:
            raise ConnectionFault("manager_closed")
        if serial not in self.slots:
            self.slots[serial] = DeviceSlot(serial, DeviceRow(serial, "absent"))
        return self.slots[serial]

    def snapshot(self, slot: DeviceSlot) -> dict[str, object]:
        build = self.backend.build
        installed = slot.installed
        session = slot.session
        control = self.gate.snapshot(slot.serial)
        manual = (
            slot.status
            in {
                "adb_unauthorized",
                "unsupported_configuration",
                "agent_incompatible",
                "accessibility_disabled",
                "permission_revoked",
                "manual_authorization_required",
                "configuration_required",
                "configuration_in_use",
                "recovery_required",
            }
            or slot.code == "forward_creation_uncertain"
            or control.recovery_required
        )
        guidance = (
            MANUAL_RECOVERY
            if slot.quarantine is not None or control.recovery_required
            else MANUAL_ACCESSIBILITY
            if slot.status
            in {"accessibility_disabled", "permission_revoked", "manual_authorization_required"}
            else MANUAL_CONFIGURATION
            if slot.status in {"configuration_required", "configuration_in_use"}
            else "Authorize this computer in the device's USB debugging prompt."
            if slot.status == "adb_unauthorized"
            else "Use the exact supported signed debug Agent; non-debug run-as bootstrap "
            "is unsupported. "
            "Runtime will not uninstall or replace an incompatible package."
            if slot.status in {"agent_incompatible", "unsupported_configuration"}
            else "An ADB forward creation had no ownership proof. Inspect it manually; "
            "Runtime will "
            "not delete unknown or foreign forwards."
            if slot.code == "forward_creation_uncertain"
            else None
        )
        return {
            "serial": slot.serial,
            "state": slot.row.state,
            "transport_id": slot.row.transport_id,
            "model": slot.row.model,
            "status": slot.status,
            "ready": slot.status == "ready"
            and session is not None
            and not control.recovery_required,
            "android_api": slot.api,
            "android_user": slot.user,
            "observed_at_ms": slot.observed_at_ms,
            "agent": {
                "package": build.package,
                "installed": installed is not None,
                "version_name": installed.version_name if installed else None,
                "version_code": installed.version_code if installed else None,
                "debuggable": installed.debuggable if installed else None,
                "build_verified": installed is not None
                and hmac.compare_digest(installed.apk_sha256, build.apk_sha256),
                "signature_verified_by_exact_artifact": installed is not None
                and hmac.compare_digest(installed.apk_sha256, build.apk_sha256),
                "supported_signer_sha256": build.signer_sha256,
                "supported_apk_sha256": build.apk_sha256,
                "protocol_version": build.protocol_version,
                "configuration_method": "private_debug_run_as",
            },
            "connection": {
                "session_id": session.id if session else None,
                "agent_lifecycle_id": session.health.lifecycle_id if session else None,
                "health_authenticated": session is not None,
                "loopback_verified": session is not None,
                "adbd_uid": session.generation.peer.uid if session else None,
                "adbd_pid": session.generation.peer.pid if session else None,
                "agent_pid": session.generation.agent_pid if session else None,
            },
            "capabilities": copy.deepcopy(session.health.capabilities) if session else None,
            "diagnostic": {
                "code": slot.code,
                "manual_required": manual,
                "guidance": guidance,
                "last_invalidation": slot.last_invalidation,
                "cleanup_pending": slot.cleanup_pending,
                "native_cleanup_guard": slot.native_cleanup_guard,
                "operation_id": slot.operation_id,
                "awaiting_authorization": slot.awaiting_authorization,
                "authorization_reason": slot.authorization_reason,
                "observation": {
                    "request_id": slot.observation.request_id,
                    "session_id": slot.observation.session_id,
                    "phase": slot.observation.phase,
                    "request_sent": slot.observation.exchange.sent,
                    "header_received": slot.observation.exchange.wire_session_id is not None,
                    "terminal_received": slot.observation.exchange.terminal_received,
                    "eof_received": slot.observation.exchange.eof_received,
                }
                if slot.observation is not None
                else None,
                "action": {
                    "request_id": slot.action.request_id,
                    "operation_id": slot.action.operation_id,
                    "session_id": slot.action.session_id,
                    "kind": slot.action.kind,
                    "phase": slot.action.phase,
                    "request_sent": slot.action.exchange.sent,
                    "dispatched_received": slot.action.exchange.dispatched_received,
                    "terminal_received": slot.action.exchange.terminal_received,
                    "eof_received": slot.action.exchange.eof_received,
                }
                if slot.action is not None
                else None,
            },
            "control": control.as_dict(),
        }

    def set_fault(self, slot: DeviceSlot, code: str) -> None:
        slot.status = (
            "recovery_required" if slot.quarantine else FAULT_STATES.get(code, "connection_failed")
        )
        if slot.quarantine is None and slot.awaiting_authorization and code in MANUAL_AUTH_FAULTS:
            slot.status = "manual_authorization_required"
        slot.code = code
        slot.observed_at_ms = time.time_ns() // 1_000_000
        log.warning(
            "Android connection blocked",
            extra={
                "event": "device.connection_blocked",
                "serial": slot.serial,
                "error_category": code,
            },
        )

    async def current_row(self, serial: str) -> DeviceRow:
        return next(
            (row for row in await self.inventory() if row.serial == serial),
            DeviceRow(serial, "absent"),
        )

    async def inventory(self) -> list[DeviceRow]:
        try:
            return await self.backend.inventory()
        except ConnectionFault:
            raise
        except Exception:
            raise ConnectionFault("adb_unavailable") from None

    async def inspect(
        self, slot: DeviceSlot, row: DeviceRow, *, allow_missing: bool = False
    ) -> None:
        slot.row = row
        slot.observed_at_ms = time.time_ns() // 1_000_000
        if row.state != "device":
            slot.installed = None
            slot.api = slot.user = None
            raise ConnectionFault(
                "adb_unauthorized"
                if row.state == "unauthorized"
                else "adb_offline"
                if row.state == "offline"
                else "device_disconnected"
            )
        if (await self.backend.command(slot.serial, "get-state")).strip() != "device":
            raise ConnectionFault("device_disconnected")
        slot.api, slot.user = await self.backend.platform(slot.serial)
        if slot.api < self.backend.build.minimum_api:
            raise ConnectionFault("unsupported_android_api")
        if slot.user != 0:
            raise ConnectionFault("unsupported_android_user")
        slot.installed = await self.backend.installed(slot.serial)
        installed = slot.installed
        if installed is None:
            if allow_missing:
                return
            raise ConnectionFault("agent_missing")
        build = self.backend.build
        if (
            installed.version_name != build.version_name
            or installed.version_code != build.version_code
        ):
            raise ConnectionFault("version_incompatible")
        if not installed.debuggable:
            raise ConnectionFault("release_configuration_unsupported")
        if not hmac.compare_digest(installed.apk_sha256, build.apk_sha256):
            raise ConnectionFault("build_signature_incompatible")
        await self.backend.require_run_as(slot.serial)

    async def generation(self, slot: DeviceSlot) -> Generation:
        row = await self.current_row(slot.serial)
        # Both sides of the handshake verify the platform and exact installed build, not just
        # the PID/token. An update between initial inspection and first generation fails closed.
        await self.inspect(slot, row)
        permissions = await self.backend.permissions(slot.serial)
        if not permissions.active:
            raise ConnectionFault(
                "permission_revoked" if slot.previously_ready else "accessibility_disabled"
            )
        peer = await self.backend.adbd_peer(slot.serial)
        config = await self.backend.peer(slot.serial)
        if config is None:
            raise ConnectionFault("peer_configuration_missing")
        if config != peer.config:
            raise ConnectionFault("peer_configuration_stale")
        token = await self.backend.token(slot.serial)
        pid = await self.backend.agent_pid(slot.serial)
        if token is None or pid is None:
            raise ConnectionFault("agent_not_connected")
        return Generation(row.transport_id, peer, pid, token)

    async def invalidate(
        self, slot: DeviceSlot, reason: str, *, interrupt_observation: bool = True
    ) -> None:
        session, slot.session = slot.session, None
        if session is None:
            return
        session.latest_observation = None
        if slot.action is not None and slot.action.session_id == session.id:
            if slot.action.exchange.sent and not slot.action.settled:
                slot.quarantine = session.generation
                slot.quarantine_lifecycle = session.health.lifecycle_id
                slot.desired = False
                slot.native_cleanup_guard = True
            slot.action.abort(reason, interrupt=interrupt_observation)
        if slot.observation is not None and slot.observation.session_id == session.id:
            slot.observation.abort(reason, interrupt=interrupt_observation)
        slot.last_invalidation = reason
        slot.pending_forwards.append(session.forward)
        await self.clean_forwards(slot)
        log.info(
            "Android session invalidated",
            extra={
                "event": "device.session_invalidated",
                "serial": slot.serial,
                "session_id": session.id,
                "error_category": reason,
            },
        )

    async def clean_forwards(self, slot: DeviceSlot) -> None:
        pending = []
        for forward in slot.pending_forwards:
            try:
                await forward.close()
            except Exception:
                pending.append(forward)
        slot.pending_forwards = pending
        slot.cleanup_pending = bool(pending) or slot.private_cleanup_pending

    def accept_health(
        self,
        slot: DeviceSlot,
        generation: Generation,
        health: AgentHealth,
        *,
        manual_recovery_confirmed: bool,
    ) -> None:
        if health.capabilities["recovery_required"] is True:
            slot.quarantine = generation
            slot.quarantine_lifecycle = health.lifecycle_id
            slot.desired = False
            raise ConnectionFault("recovery_required")
        if health.capabilities["accessibility"] != "available":
            raise ConnectionFault(
                "permission_revoked" if slot.previously_ready else "accessibility_not_connected"
            )
        if slot.quarantine is not None:
            if (
                not manual_recovery_confirmed
                or slot.quarantine.agent_pid == generation.agent_pid
                or slot.quarantine_lifecycle == health.lifecycle_id
                or health.capabilities["platform_operation_pending"] is True
            ):
                raise ConnectionFault("manual_recovery_not_observed")
            slot.quarantine = None
            slot.quarantine_lifecycle = None

    async def checked_connection(
        self, slot: DeviceSlot, *, manual_recovery_confirmed: bool = False
    ) -> None:
        generation = await self.generation(slot)
        session = slot.session
        if session is not None and not session.generation.matches(generation):
            await self.invalidate(slot, "generation_changed")
            session = None
        if slot.quarantine is not None and not manual_recovery_confirmed:
            raise ConnectionFault("recovery_required")
        if session is not None:
            await session.forward.verify()
            health = await self.fetch_health(slot, session.forward, generation)
            if not generation.matches(await self.generation(slot)):
                raise ConnectionFault("connection_changed")
            self.accept_health(
                slot, generation, health, manual_recovery_confirmed=manual_recovery_confirmed
            )
            if health.lifecycle_id != session.health.lifecycle_id:
                await self.invalidate(slot, "agent_lifecycle_changed")
                session = None
            else:
                session.health = health
                if (
                    health.capabilities["platform_operation_pending"] is False
                    and health.capabilities["recovery_required"] is False
                ):
                    slot.native_cleanup_guard = False
        if session is None and slot.desired:
            forward = ForwardLease(self.backend, slot.serial)
            try:
                await forward.open()
                health = await self.fetch_health(slot, forward, generation)
                if not generation.matches(await self.generation(slot)):
                    raise ConnectionFault("connection_changed")
                self.accept_health(
                    slot, generation, health, manual_recovery_confirmed=manual_recovery_confirmed
                )
                slot.session = session = AgentSession(str(uuid4()), generation, forward, health)
                try:
                    self.gate.bind_session(slot.serial, session.id)
                except LedgerError:
                    raise ConnectionFault("control_ledger_unavailable") from None
            except BaseException:
                if forward.owned:
                    slot.pending_forwards.append(forward)
                await finish_cleanup(self.clean_forwards(slot))
                raise
            log.info(
                "Android Agent connected",
                extra={
                    "event": "device.connected",
                    "serial": slot.serial,
                    "session_id": session.id,
                },
            )
        if session is None:
            slot.status, slot.code = "agent_available", "connection_not_established"
        else:
            if slot.awaiting_authorization:
                slot.awaiting_authorization = False
                slot.authorization_reason = None
                slot.operation_id = None
                slot.private_cleanup_pending = False
                slot.cleanup_pending = bool(slot.pending_forwards)
            slot.status = (
                "agent_busy"
                if session.health.capabilities["platform_operation_pending"]
                else "ready"
            )
            slot.code = "platform_operation_pending" if slot.status == "agent_busy" else None
            slot.previously_ready |= slot.status == "ready"
        slot.observed_at_ms = time.time_ns() // 1_000_000

    async def fetch_health(
        self, slot: DeviceSlot, forward: ForwardLease, generation: Generation
    ) -> AgentHealth:
        try:
            health = await self.backend.health(forward, generation.token)
        except ConnectionFault as exc:
            if exc.code in {"recovery_required", "action_uncertain"}:
                slot.quarantine = generation
                slot.quarantine_lifecycle = (
                    slot.session.health.lifecycle_id if slot.session else None
                )
                slot.desired = False
                slot.awaiting_authorization = False
            raise
        if (
            health.capabilities["platform_operation_pending"] is True
            or health.capabilities["recovery_required"] is True
        ):
            slot.native_cleanup_guard = True
        if (
            slot.observation is not None
            and slot.session is not None
            and slot.observation.session_id == slot.session.id
        ):
            slot.observation.latest_capabilities = copy.deepcopy(health.capabilities)
        if health.capabilities["recovery_required"] is True:
            slot.quarantine = generation
            slot.quarantine_lifecycle = health.lifecycle_id
            slot.desired = False
            slot.awaiting_authorization = False
            raise ConnectionFault("recovery_required")
        return health

    async def refresh(self, slot: DeviceSlot, row: DeviceRow) -> None:
        try:
            await self.inspect(slot, row)
            await self.checked_connection(slot)
        except ConnectionFault as exc:
            await self.invalidate(slot, exc.code)
            self.set_fault(slot, exc.code)
        except Exception:
            await self.invalidate(slot, "connection_failed")
            self.set_fault(slot, "connection_failed")

    async def list_devices(self) -> dict[str, object]:
        try:
            rows = {row.serial: row for row in await self.inventory()}
        except ConnectionFault as exc:
            for slot in tuple(self.slots.values()):
                async with slot.lock:
                    await self.invalidate(slot, exc.code)
                    self.set_fault(slot, exc.code)
            log.warning(
                "Device discovery failed",
                extra={"event": "devices.failed", "error_category": "device"},
            )
            return {"devices": [self.snapshot(s) for s in self.slots.values()], "error": exc.code}

        async def update(serial: str) -> None:
            slot = self.slot(serial)
            if slot.lock.locked():
                return
            async with slot.lock:
                await self.refresh(slot, rows.get(serial, DeviceRow(serial, "absent")))

        await asyncio.gather(*(update(serial) for serial in rows.keys() | self.slots.keys()))
        return {"devices": [self.snapshot(self.slots[serial]) for serial in sorted(self.slots)]}

    async def status(self, serial: str) -> dict[str, object]:
        slot = self.slot(serial)
        if not slot.lock.locked() and slot.observation is None and slot.action is None:
            async with slot.lock:
                try:
                    row = await self.current_row(serial)
                    await self.refresh(slot, row)
                except ConnectionFault as exc:
                    await self.invalidate(slot, exc.code)
                    self.set_fault(slot, exc.code)
        return self.snapshot(slot)

    async def native_idle_for_restore(self, slot: DeviceSlot) -> bool:
        """Fresh read-only evidence, not a cancellation acknowledgement or an OS reset."""
        session = slot.session
        if (
            slot.observation is not None
            or slot.action is not None
            or session is None
            or slot.quarantine is not None
        ):
            return False
        try:
            async with asyncio.timeout(10):
                generation = await self.generation(slot)
                if not session.generation.matches(generation):
                    return False
                await session.forward.verify()
                health = await self.fetch_health(slot, session.forward, generation)
                if (
                    not generation.matches(await self.generation(slot))
                    or health.lifecycle_id != session.health.lifecycle_id
                ):
                    return False
                await session.forward.verify()
                session.health = health
                return (
                    health.capabilities["platform_operation_pending"] is False
                    and health.capabilities["recovery_required"] is False
                )
        except (TimeoutError, ConnectionFault):
            return False
        except Exception:
            return False

    async def restore(self, slot: DeviceSlot, *, keep_peer: bool = False) -> None:
        await self.clean_forwards(slot)
        if (
            (
                slot.accessibility_lease is not None
                or (slot.peer_owner is not None and not keep_peer)
            )
            and (slot.native_cleanup_guard or slot.quarantine is not None)
            and not await self.native_idle_for_restore(slot)
        ):
            # Keep the existing ownership journal/configuration; native resources can outlive EOF.
            slot.cleanup_pending = slot.private_cleanup_pending = True
            slot.status, slot.code = "cleanup_pending", "native_cleanup_pending"
            return
        failed = bool(slot.pending_forwards)
        private_failed = False
        if slot.accessibility_lease is not None:
            original, expected = slot.accessibility_lease
            try:
                restored = await self.backend.restore_permissions(slot.serial, original, expected)
            except Exception:
                restored = False
            if restored:
                slot.accessibility_lease = None
            else:
                failed = True
                private_failed = True
        if slot.peer_owner is not None and not keep_peer:
            try:
                released = await self.backend.release_peer(slot.serial, slot.peer_owner)
            except Exception:
                released = False
            if released:
                slot.peer_owner = None
                slot.peer_value = None
            else:
                failed = True
                private_failed = True
        slot.cleanup_pending = failed
        slot.private_cleanup_pending = private_failed
        if not private_failed and slot.accessibility_lease is None and slot.peer_owner is None:
            slot.native_cleanup_guard = False

    async def prepare_once(
        self,
        slot: DeviceSlot,
        *,
        install: bool,
        enable_accessibility: bool,
        manual_recovery_confirmed: bool,
        readiness_wait: float | None = None,
    ) -> None:
        if slot.quarantine is not None and not manual_recovery_confirmed:
            raise ConnectionFault("recovery_required")
        await self.inspect(slot, await self.current_row(slot.serial), allow_missing=install)
        if slot.installed is None:
            slot.status = "installing_agent"
            await complete_mutation(self.backend.install(slot.serial, self.apk))
            await self.inspect(slot, await self.current_row(slot.serial))
        peer = await self.backend.adbd_peer(slot.serial)
        token = await self.backend.token(slot.serial)
        config = await self.backend.peer(slot.serial)
        if token is not None:
            # Never rewrite an active listener's pin or toggle it to obtain a fresh token.
            if config != peer.config:
                # A first bootstrap writes the pin, the Agent reloads it only when it restarts,
                # and the previous session's release removed the file again. Restoring that
                # same pin lets the already-restarted Agent be adopted instead of deadlocking.
                if config is not None:
                    raise ConnectionFault("peer_configuration_stale")
                if slot.peer_owner is None:
                    slot.peer_owner = str(uuid4())
                    await complete_mutation(
                        self.backend.claim_peer(slot.serial, slot.peer_owner, peer)
                    )
                slot.peer_value = peer
                if await self.backend.adbd_peer(slot.serial) != peer:
                    raise ConnectionFault("connection_changed")
            slot.desired = True
            await self.checked_connection(slot, manual_recovery_confirmed=manual_recovery_confirmed)
            return
        if slot.quarantine is not None:
            pid = await self.backend.agent_pid(slot.serial)
            if pid == slot.quarantine.agent_pid:
                raise ConnectionFault("manual_recovery_not_observed")
        if slot.peer_owner is None:
            slot.peer_owner = str(uuid4())
            await complete_mutation(self.backend.claim_peer(slot.serial, slot.peer_owner, peer))
        elif slot.peer_value != peer:
            await complete_mutation(self.backend.update_peer(slot.serial, slot.peer_owner, peer))
        slot.peer_value = peer
        if await self.backend.adbd_peer(slot.serial) != peer:
            raise ConnectionFault("connection_changed")
        original = await self.backend.permissions(slot.serial)
        if enable_accessibility and not original.active:
            target = original.with_agent()
            if slot.accessibility_lease is None or slot.accessibility_lease[1] != original:
                slot.accessibility_lease = original, target
            if not await complete_mutation(
                self.backend.set_permissions(slot.serial, original, target)
            ):
                raise ConnectionFault("accessibility_enable_failed")
        slot.desired = True
        # A first bootstrap has to observe the Agent reload its private pin and mint a token,
        # which is slower than a ready Agent publishing one it already holds.
        wait = self.readiness_wait if readiness_wait is None else readiness_wait
        deadline = time.monotonic() + wait
        while True:
            try:
                await self.checked_connection(
                    slot, manual_recovery_confirmed=manual_recovery_confirmed
                )
                return
            except ConnectionFault as exc:
                if exc.code not in {
                    "agent_not_connected",
                    "credential_unavailable",
                    "agent_unavailable",
                }:
                    raise
                if time.monotonic() >= deadline:
                    raise ConnectionFault("accessibility_not_connected") from None
                # Startup may publish the token non-atomically. Only safe reads/handshakes repeat.
                await asyncio.sleep(0.1)

    async def operation(
        self,
        serial: str,
        *,
        preparing: bool,
        install: bool = False,
        enable_accessibility: bool = False,
        manual_recovery_confirmed: bool = False,
        timeout: float = 30,  # noqa: ASYNC109
        operation_id: str | None = None,
    ) -> dict[str, object]:
        slot = self.slot(serial)
        if slot.lock.locked() or slot.observation is not None or slot.action is not None:
            return {**self.snapshot(slot), "operation_error": "operation_in_progress"}
        if slot.private_cleanup_pending and not slot.awaiting_authorization:
            return {**self.snapshot(slot), "operation_error": "cleanup_required"}
        identifier = canonical_uuid(operation_id) if operation_id is not None else str(uuid4())
        async with slot.lock:
            task = asyncio.current_task()
            slot.operation = task
            slot.operation_id = identifier
            was_authorizing = slot.awaiting_authorization
            slot.status = "preparing" if preparing else "connecting"
            slot.code = None
            try:
                async with asyncio.timeout(timeout):
                    if preparing:
                        await self.prepare_once(
                            slot,
                            install=install,
                            enable_accessibility=enable_accessibility,
                            manual_recovery_confirmed=manual_recovery_confirmed,
                            readiness_wait=max(self.readiness_wait, min(timeout, 15.0)),
                        )
                    else:
                        await self.inspect(slot, await self.current_row(serial))
                        slot.desired = True
                        await self.checked_connection(
                            slot, manual_recovery_confirmed=manual_recovery_confirmed
                        )
            except asyncio.CancelledError:
                slot.desired = False
                authorizing = slot.awaiting_authorization
                slot.awaiting_authorization = False
                slot.authorization_reason = None
                await finish_cleanup(self.invalidate(slot, "cancelled"))
                if preparing or authorizing:
                    await finish_cleanup(self.restore(slot))
                self.set_fault(slot, "cancelled")
                raise
            except (ConnectionFault, TimeoutError) as exc:
                code = exc.code if isinstance(exc, ConnectionFault) else "timeout"
                await finish_cleanup(self.invalidate(slot, code))
                keep_bootstrap = (
                    slot.quarantine is None
                    and slot.peer_owner is not None
                    and code in MANUAL_AUTH_FAULTS
                )
                slot.awaiting_authorization = keep_bootstrap
                slot.desired = keep_bootstrap
                if keep_bootstrap:
                    slot.authorization_reason = code
                    await finish_cleanup(self.restore(slot, keep_peer=True))
                elif (preparing or was_authorizing) and slot.quarantine is None:
                    slot.authorization_reason = None
                    await finish_cleanup(self.restore(slot))
                self.set_fault(slot, code)
            except Exception:
                slot.desired = False
                slot.awaiting_authorization = False
                await finish_cleanup(self.invalidate(slot, "connection_failed"))
                if (preparing or was_authorizing) and slot.quarantine is None:
                    slot.authorization_reason = None
                    await finish_cleanup(self.restore(slot))
                self.set_fault(slot, "connection_failed")
            finally:
                slot.operation = None
                if not slot.awaiting_authorization:
                    slot.operation_id = None
        return self.snapshot(slot)

    async def prepare(
        self,
        serial: str,
        *,
        install: bool = False,
        enable_accessibility: bool = True,
        manual_recovery_confirmed: bool = False,
        timeout: float = 30,  # noqa: ASYNC109
        operation_id: str | None = None,
    ) -> dict[str, object]:
        return await self.operation(
            serial,
            preparing=True,
            install=install,
            enable_accessibility=enable_accessibility,
            manual_recovery_confirmed=manual_recovery_confirmed,
            timeout=timeout,
            operation_id=operation_id,
        )

    async def connect(
        self,
        serial: str,
        *,
        manual_recovery_confirmed: bool = False,
        timeout: float = 30,  # noqa: ASYNC109
        operation_id: str | None = None,
    ) -> dict[str, object]:
        return await self.operation(
            serial,
            preparing=False,
            manual_recovery_confirmed=manual_recovery_confirmed,
            timeout=timeout,
            operation_id=operation_id,
        )

    async def health(self, serial: str, session_id: str) -> dict[str, object]:
        canonical_uuid(session_id)
        slot = self.slot(serial)
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            try:
                await self.inspect(slot, await self.current_row(serial))
                await self.checked_connection(slot)
            except ConnectionFault as exc:
                await self.invalidate(slot, exc.code)
                self.set_fault(slot, exc.code)
            except Exception:
                await self.invalidate(slot, "connection_failed")
                self.set_fault(slot, "connection_failed")
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            return self.snapshot(slot)

    async def geometry(self, serial: str, session_id: str) -> dict[str, object]:
        """Read input pose without an observation, recovery or capture-side atomicity claim."""
        canonical_uuid(session_id)
        slot = self.slot(serial)
        try:
            async with asyncio.timeout(5):
                async with slot.lock:
                    session = slot.session
                    if session is None or session.id != session_id:
                        raise ConnectionFault("stale_session")
                    if slot.action is not None:
                        raise ConnectionFault("action_in_progress")
                    if slot.observation is not None:
                        raise ConnectionFault("observation_in_progress")
                    if slot.operation is not None:
                        raise ConnectionFault("operation_in_progress")
                    if slot.cleanup_pending:
                        raise ConnectionFault("cleanup_required")
                    await self.check_borrowed_session(slot, session, beginning=True)
                    geometry = await self.backend.action_geometry(serial)
                    if (
                        type(geometry.get("display_id")) is not int
                        or geometry.get("display_id") != 0
                        or type(geometry.get("rotation")) is not int
                        or geometry.get("rotation") not in {0, 90, 180, 270}
                        or any(
                            type(geometry.get(key)) is not int or not 1 <= geometry[key] <= 8192
                            for key in ("width", "height")
                        )
                    ):
                        raise ConnectionFault("display_geometry_unavailable")
                    if geometry != await self.backend.action_geometry(serial):
                        raise ConnectionFault("display_geometry_changed")
                    await self.check_borrowed_session(slot, session, beginning=False)
                    if slot.session is not session or self.closed:
                        raise ConnectionFault("stale_session")
                    return {
                        "serial": serial,
                        "session_id": session_id,
                        "status": "available",
                        "source": "adb_input_viewport",
                        "geometry": geometry,
                        "atomic_with_observation": False,
                    }
        except TimeoutError:
            raise ConnectionFault("display_geometry_unavailable") from None

    async def observe(
        self,
        serial: str,
        session_id: str,
        request_id: str,
        *,
        tree: bool = True,
        screenshot: bool = False,
        ocr: bool = False,
        display_id: int = 0,
        timeout: float = 15.0,  # noqa: ASYNC109
        for_action: bool = False,
        _locator: LocateBody | None = None,
        _include_values: bool = False,
        _scope: m.Target | None = None,
    ) -> dict[str, object]:
        """One fresh read-only collection on a borrowed P1.2 session, with no recovery/replay."""
        canonical_uuid(session_id)
        canonical_uuid(request_id)
        requested = {"tree": tree, "screenshot": screenshot, "ocr": ocr}
        if (
            any(type(value) is not bool for value in requested.values())
            or not any(requested.values())
            or type(display_id) is not int
            or not 0 <= display_id <= 255
            or isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 30
            or type(for_action) is not bool
        ):
            raise ConnectionFault("invalid_message")
        slot = self.slot(serial)
        if slot.action is not None and slot.action.task is not asyncio.current_task():
            raise ConnectionFault("action_in_progress")
        if slot.operation is not None:
            raise ConnectionFault("operation_in_progress")
        # A periodic read-only inventory may hold the slot briefly. Wait within a bounded
        # portion of this request's budget rather than turning that refresh into a 409.
        try:
            async with asyncio.timeout(min(5.0, timeout / 2)):
                await slot.lock.acquire()
        except TimeoutError:
            raise ConnectionFault("operation_in_progress") from None
        try:
            session = slot.session
            if session is None or session.id != session_id:
                raise ConnectionFault("stale_session")
            if slot.observation is not None:
                raise ConnectionFault("observation_in_progress")
            if slot.cleanup_pending:
                raise ConnectionFault("cleanup_required")
            if request_id in session.observation_ids:
                raise ConnectionFault("observation_request_reused")
            if len(session.observation_ids) >= 256:
                raise ConnectionFault("observation_request_limit")
            task = asyncio.current_task()
            if task is None:
                raise ConnectionFault("observation_failed")
            lease = ActiveObservation(
                request_id, session_id, self.observation_factory(request_id), task
            )
            session.observation_ids.add(request_id)
            session.latest_observation = None
            slot.observation = lease
        finally:
            slot.lock.release()

        started_epoch = time.time_ns() // 1_000_000
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        capabilities = copy.deepcopy(session.health.capabilities)
        wire_validated = source_validated = False
        normalized: dict[str, object] | None = None
        error: str | None = None
        collection_error: str | None = None
        agent_terminal_success: bool | None = None
        captured_monotonic = started
        geometry: dict[str, int] | None = None
        geometry_error: str | None = None
        located: dict[str, object] | None = None

        async def check_session(*, beginning: bool) -> None:
            if lease.reason is not None or slot.session is not session or self.closed:
                raise ConnectionFault(lease.reason or "stale_session")
            await self.check_borrowed_session(slot, session, beginning=beginning)
            if lease.reason is not None or slot.session is not session or self.closed:
                raise ConnectionFault(lease.reason or "stale_session")

        async def fault_cleanup(reason: str) -> None:
            # Protect acquiring the lock as well as invalidation from a second cancellation.
            async with slot.lock:
                if slot.session is session:
                    if reason in {"recovery_required", "action_uncertain"}:
                        slot.quarantine = session.generation
                        slot.quarantine_lifecycle = session.health.lifecycle_id
                        slot.desired = False
                    await self.invalidate(slot, reason, interrupt_observation=False)
                    self.set_fault(slot, reason)

        try:
            async with asyncio.timeout_at(deadline):
                async with slot.lock:
                    await check_session(beginning=True)
                    capabilities = copy.deepcopy(session.health.capabilities)
                if display_id != 0:
                    error = "unsupported_display"
                else:
                    if for_action:
                        try:
                            geometry = await self.backend.action_geometry(serial)
                        except ConnectionFault:
                            geometry_error = "display_geometry_unavailable"
                    remaining_ms = int((deadline - loop.time()) * 1000)
                    if remaining_ms < 100:
                        raise ConnectionFault("observation_timeout")
                    lease.phase = "collecting"
                    slot.native_cleanup_guard = True
                    reply = await lease.exchange.run(
                        session.forward,
                        session.generation.token,
                        {**requested, "screenshot": screenshot or ocr},
                        min(10_000, remaining_ms),
                    )
                    captured_monotonic = time.monotonic()
                    if for_action and geometry is not None:
                        try:
                            if geometry != await self.backend.action_geometry(serial):
                                geometry = None
                                geometry_error = "display_geometry_changed"
                        except ConnectionFault:
                            geometry = None
                            geometry_error = "display_geometry_unavailable"
                    wire_validated = True
                    agent_terminal_success = reply.success
                    if lease.reason is not None:
                        raise ConnectionFault(lease.reason)
                    if not reply.success and reply.result is None:
                        raise ConnectionFault(reply.error_code or "agent_observation_rejected")
                    if reply.result is None:
                        raise ConnectionFault("invalid_observation")
                    if not reply.success:
                        if reply.error_code != "window_unavailable":
                            raise ConnectionFault(reply.error_code or "agent_observation_rejected")
                        collection_error = "window_unavailable"
                    lease.phase = "validating"
                    async with slot.lock:
                        await check_session(beginning=False)
                    normalized = normalize_observation(
                        reply.result,
                        requested=requested,
                        capabilities=capabilities,
                        display_id=display_id,
                    )
                    reported = normalized.get("capabilities")
                    if isinstance(reported, dict) and reported.get("recovery_required") is True:
                        lease.latest_capabilities = copy.deepcopy(reported)
                        slot.native_cleanup_guard = True
                        raise ConnectionFault("recovery_required")
                    if loop.time() >= deadline:
                        raise ConnectionFault("observation_timeout")
                    if lease.reason is not None or slot.session is not session:
                        raise ConnectionFault(lease.reason or "stale_session")
                    source_validated = True
                    if _locator is not None:
                        lease.phase = "resolving"
                        projection: dict[str, object] = {
                            **normalized,
                            "serial": serial,
                            "session_id": session_id,
                            "request_id": request_id,
                            "agent_lifecycle_id": session.health.lifecycle_id,
                            "requested_display_id": display_id,
                            "status": "completed",
                            "framing": {
                                "validated": wire_validated,
                                "source_data_validated": source_validated,
                                "terminal_received": lease.exchange.terminal_received,
                                "eof_received": lease.exchange.eof_received,
                            },
                        }
                        binding = bind_observation(projection, captured_monotonic, geometry)
                        # Image matching runs off-loop, still inside this read's serial/session
                        # lease and deadline. A late pure computation cannot publish a target.
                        located = await asyncio.to_thread(
                            resolve_locator,
                            _locator,
                            projection,
                            binding=binding,
                            age_ms=(time.monotonic() - captured_monotonic) * 1000,
                            include_text=_include_values,
                            scope_target=_scope,
                        )
                        async with slot.lock:
                            await check_session(beginning=False)
                        if loop.time() >= deadline:
                            raise ConnectionFault("observation_timeout")

        except asyncio.CancelledError:
            error = lease.reason or "cancelled"
            lease.abort(error)
        except (TimeoutError, ConnectionFault) as exc:
            error = (
                exc.code
                if isinstance(exc, ConnectionFault) and exc.code in OBSERVATION_FAULTS
                else "observation_timeout"
                if isinstance(exc, TimeoutError)
                else "observation_failed"
            )
            if error == "timeout":
                error = "observation_timeout"
            lease.abort(error)
            if error not in {
                "platform_operation_pending",
                "busy",
                "observation_timeout",
                "cancelled",
            }:
                with contextlib.suppress(asyncio.CancelledError):
                    await finish_cleanup(fault_cleanup(error))
        except Exception:
            error = "observation_failed"
            lease.abort(error)
            with contextlib.suppress(asyncio.CancelledError):
                await finish_cleanup(fault_cleanup(error))
        finally:
            lease.phase = "cleanup"
            # No await here: cancellation cannot leave a completed request installed as active.
            if slot.observation is lease:
                slot.observation = None
            lease.done.set()

        error = lease.reason or error
        if error is None and slot.session is not session:
            error = "stale_session"
        if error is not None:
            final_capabilities = copy.deepcopy(lease.latest_capabilities) or {
                key: None for key in capabilities
            }
            if not source_validated:
                final_capabilities = {
                    key: True
                    if key in {"platform_operation_pending", "recovery_required"} and value is True
                    else None
                    for key, value in final_capabilities.items()
                }
            if error in {"recovery_required", "action_uncertain"}:
                final_capabilities["recovery_required"] = True
            normalized = {
                "sources": failed_sources(
                    requested,
                    capabilities,
                    error,
                    status="unavailable" if error == "unsupported_display" else "failed",
                    display_id=display_id,
                ),
                "completeness": {"status": "unavailable", "reasons": [error]},
                "revision_start": None,
                "revision_end": None,
                "atomic_snapshot": False,
                "capabilities": final_capabilities,
            }
        if normalized is None:
            raise ConnectionFault("observation_failed")
        sources = normalized["sources"]
        success = (
            error is None
            and agent_terminal_success is True
            and isinstance(sources, dict)
            and all(
                isinstance(sources.get(key), dict) and sources[key].get("status") == "available"
                for key, enabled in requested.items()
                if enabled
            )
        )
        log.info(
            "Android observation finished",
            extra={
                "event": "device.observation_finished",
                "serial": serial,
                "session_id": session_id,
                "error_category": error,
                "duration_ms": (time.monotonic() - started) * 1000,
            },
        )
        result: dict[str, object] = {
            **normalized,
            "contract": "pulse-runtime-observation-draft",
            "serial": serial,
            "session_id": session_id,
            "request_id": request_id,
            "agent_lifecycle_id": session.health.lifecycle_id,
            "agent_http_session_id": lease.exchange.wire_session_id,
            "requested": requested,
            "capabilities_at_request": copy.deepcopy(capabilities),
            "requested_display_id": display_id,
            "status": "failed" if error is not None else "completed",
            "success": success,
            "error": {"code": error or collection_error}
            if error is not None or collection_error is not None
            else None,
            "agent_terminal_success": agent_terminal_success,
            "framing": {
                "protocol_version": 1,
                "validated": wire_validated,
                "source_data_validated": source_validated,
                "terminal_received": lease.exchange.terminal_received,
                "eof_received": lease.exchange.eof_received,
            },
            "runtime_time": {
                "started_at_epoch_ms": started_epoch,
                "finished_at_epoch_ms": time.time_ns() // 1_000_000,
                "duration_ms": (time.monotonic() - started) * 1000,
            },
            "clock_mapping": {
                "status": "unavailable",
                "data": None,
                "reason": "agent_uptime_not_calibrated_to_host_epoch",
            },
            "cleanup": copy.deepcopy(lease.exchange.cleanup),
        }
        if for_action:
            result["action_context"] = {
                "status": "available" if geometry is not None and error is None else "unavailable",
                "source": "adb_input_viewport",
                "geometry": copy.deepcopy(geometry) if error is None else None,
                "error": {"code": geometry_error or error} if geometry_error or error else None,
                "atomic_with_observation": False,
            }
        if source_validated and error is None and slot.session is session:
            session.latest_observation = bind_observation(result, captured_monotonic, geometry)
        if _locator is not None:
            if error is not None or located is None:
                located = resolve_locator(
                    _locator, result, include_text=_include_values, scope_target=_scope
                )
            located["observation"] = observation_metadata(result)
            result["locator_result"] = located
        return result

    async def locate(
        self, serial: str, body: LocateBody, *, scope: m.Target | None = None
    ) -> dict[str, object]:
        """Collect and resolve once; retain only P1.4's existing bounded provenance binding."""
        if serial != body.serial:
            raise ConnectionFault("serial_mismatch")
        result = await self.observe(
            serial,
            body.session_id,
            body.request_id,
            **body.requested_sources(scope=scope),
            display_id=body.display_id,
            timeout=body.timeout_ms / 1000,
            for_action=True,
            _locator=body,
            _scope=scope,
        )
        return cast(dict[str, object], result["locator_result"])

    async def probe(
        self, serial: str, body: LocateBody, *, scope: m.Target | None = None
    ) -> dict[str, object]:
        """Runtime-internal read with matched text for the interpreter.

        HTTP callers cannot request this projection. It is one fresh observation and reuses
        the same P1.5 resolver/provenance checks as ``locate``; nothing is retained.
        """
        if serial != body.serial:
            raise ConnectionFault("serial_mismatch")
        result = await self.observe(
            serial,
            body.session_id,
            body.request_id,
            **body.requested_sources(scope=scope),
            display_id=body.display_id,
            timeout=body.timeout_ms / 1000,
            for_action=True,
            _locator=body,
            _include_values=True,
            _scope=scope,
        )
        return cast(dict[str, object], result["locator_result"])

    async def check_borrowed_session(
        self, slot: DeviceSlot, session: AgentSession, *, beginning: bool
    ) -> None:
        """The existing P1.3 strict lease check, shared by reads and writes; never reconnect."""
        if slot.session is not session or self.closed:
            raise ConnectionFault("stale_session")
        generation = await self.generation(slot)
        if not session.generation.matches(generation):
            raise ConnectionFault("generation_changed")
        await session.forward.verify()
        health = await self.fetch_health(slot, session.forward, generation)
        if not generation.matches(await self.generation(slot)):
            raise ConnectionFault("generation_changed")
        await session.forward.verify()
        self.accept_health(slot, generation, health, manual_recovery_confirmed=False)
        if health.lifecycle_id != session.health.lifecycle_id:
            raise ConnectionFault("agent_lifecycle_changed")
        if slot.session is not session or self.closed:
            raise ConnectionFault("stale_session")
        session.health = health
        slot.status = "agent_busy" if health.capabilities["platform_operation_pending"] else "ready"
        slot.code = "platform_operation_pending" if slot.status == "agent_busy" else None
        slot.observed_at_ms = time.time_ns() // 1_000_000
        if (
            health.capabilities["platform_operation_pending"] is False
            and health.capabilities["recovery_required"] is False
        ):
            slot.native_cleanup_guard = False
        if beginning and health.capabilities["platform_operation_pending"]:
            raise ConnectionFault("platform_operation_pending")

    async def action(self, serial: str, body: ActionBody) -> dict[str, object]:
        """Single submission, fresh pre/post reads, no business assertion or write replay."""
        started_epoch, started = time.time_ns() // 1_000_000, time.monotonic()
        backend_name = "runtime_adb" if body.kind in ADB_ACTIONS else "agent_v1"
        base: dict[str, object] = {
            "contract": "pulse-runtime-action-draft",
            "serial": serial,
            "session_id": body.session_id,
            "request_id": body.request_id,
            "operation_id": body.operation_id,
            "kind": body.kind,
            "backend": backend_name,
            "target_source": body.target.source,
            "target_observation_id": body.target.observation_id,
            "business_success": None,
            "automatic_replay": False,
            "control": {
                "source": body.source,
                "epoch": body.control_epoch,
                "run_id": body.run_id,
                "step_id": body.step_id,
                "ledger_state": None,
            },
        }

        def rejected(code: str) -> dict[str, object]:
            return {
                **base,
                "status": "rejected",
                "action_completed": False,
                "error": {"code": code},
                "receipts": [{"status": "rejected", "source": "runtime_precheck"}],
                "post_observation": None,
                "verification": {"status": "not_performed", "business_status": "not_evaluated"},
            }

        if serial != body.serial:
            return rejected("serial_mismatch")
        prepared = False
        slot = self.slot(serial)
        try:
            async with asyncio.timeout(min(5.0, body.timeout_ms / 2000)):
                await slot.lock.acquire()
        except TimeoutError:
            return rejected("operation_in_progress")
        try:
            session = slot.session
            if session is None or session.id != body.session_id:
                return rejected("stale_session")
            if slot.quarantine is not None:
                return rejected("recovery_required")
            if (
                slot.operation is not None
                or slot.action is not None
                or slot.observation is not None
            ):
                return rejected("action_in_progress")
            if slot.cleanup_pending:
                return rejected("cleanup_required")
            try:
                self.gate.authorize(
                    serial,
                    session_id=body.session_id,
                    source=body.source,
                    control_epoch=body.control_epoch,
                    run_id=body.run_id,
                )
            except GateError as exc:
                return rejected(exc.code)
            except LedgerError:
                return rejected("control_ledger_unavailable")
            if body.request_id in slot.action_requests:
                return rejected("action_request_reused")
            if body.operation_id in slot.action_operations:
                return rejected("action_operation_reused")
            if len(slot.action_requests) >= 256 or len(slot.action_operations) >= 256:
                return rejected("action_request_limit")
            try:
                if self.ledger.action(body.request_id) is not None:
                    return rejected("action_request_reused")
            except LedgerError:
                return rejected("control_ledger_unavailable")
            task = asyncio.current_task()
            if task is None:
                return rejected("action_failed")
            exchange = (
                self.adb_action_factory(body.request_id, self.backend)
                if body.kind in ADB_ACTIONS
                else self.action_factory(body.request_id)
            )
            lease = ActiveAction(
                body.request_id, body.operation_id, body.session_id, body.kind, exchange, task
            )
            original = session.latest_observation
            try:
                inserted = self.ledger.prepare(
                    action_id=body.request_id,
                    device_id=serial,
                    session_id=body.session_id,
                    run_id=body.run_id,
                    step_id=body.step_id,
                    control_epoch=body.control_epoch,
                    source=body.source,
                    kind=body.kind,
                    side_effect=True,
                    target_observation_id=body.target.observation_id,
                    expected_package=body.target.expected_package,
                    agent_lifecycle_id=session.health.lifecycle_id,
                    agent_pid=session.generation.agent_pid,
                )
            except LedgerError:
                return rejected("control_ledger_unavailable")
            if not inserted:
                return rejected("action_request_reused")
            prepared = True
            slot.action_requests.add(body.request_id)
            slot.action_operations.add(body.operation_id)
            slot.action = lease
        finally:
            slot.lock.release()

        completed = False
        status, error = "rejected", None
        failure_phase: str | None = None
        binding_age_ms: int | None = None
        # Keep the measured age even when the binding is rejected, so a staleness gate
        # rejection reports why it fired instead of an always-null field.
        measured_binding_ms: int | None = None
        reply_status: str | None = None
        completion_source: str | None = None
        before_id: str | None = None
        after: dict[str, object] | None = None
        verification_error: str | None = None
        receipt_validated = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + body.timeout_ms / 1000

        def active() -> None:
            if lease.reason is not None or lease.task.cancelling():
                raise ConnectionFault(lease.reason or "cancelled")
            if slot.session is not session or self.closed:
                raise ConnectionFault("stale_session")

        def quarantine() -> None:
            slot.quarantine = session.generation
            slot.quarantine_lifecycle = session.health.lifecycle_id
            slot.desired = False
            slot.awaiting_authorization = False
            slot.native_cleanup_guard = True

        async def fault_cleanup(code: str) -> None:
            async with asyncio.timeout(3):
                async with slot.lock:
                    if slot.session is session:
                        await self.invalidate(slot, code, interrupt_observation=False)
                        self.set_fault(slot, code)

        try:
            async with asyncio.timeout_at(deadline):
                active()
                async with slot.lock:
                    await self.check_borrowed_session(slot, session, beginning=True)
                active()
                if (
                    original is None
                    or original.request_id != body.target.observation_id
                    or (
                        original.session_id != session.id
                        or original.lifecycle_id != session.health.lifecycle_id
                        or time.monotonic() - original.captured_monotonic > SOURCE_MAX_AGE
                    )
                ):
                    if original is not None:
                        measured_binding_ms = int(
                            (time.monotonic() - original.captured_monotonic) * 1000
                        )
                    raise ConnectionFault("action_source_stale")
                validate_source(body, original)
                if body.wire_kind not in ADB_ACTIONS and body.wire_kind not in cast(
                    list[str], session.health.capabilities["actions"]
                ):
                    raise ConnectionFault("unsupported_action")
                lease.phase = "pre_observation"
                before_id = str(uuid4())
                remaining = min(30.0, deadline - loop.time())
                if remaining < 1:
                    raise ConnectionFault("action_timeout")
                before = await self.observe(
                    serial,
                    session.id,
                    before_id,
                    timeout=remaining,
                    for_action=body.kind in {"tap", "long", "swipe"}
                    or original.geometry is not None,
                )
                active()
                fresh = session.latest_observation
                if before["success"] is not True or fresh is None:
                    raise ConnectionFault("action_source_unavailable")
                if fresh.request_id != before_id:
                    # Never fall back to an older binding if this collection did not publish.
                    raise ConnectionFault("action_source_stale")
                if body.kind in INTERACTIVE_KINDS and original.fingerprint != fresh.fingerprint:
                    # A package-scoped launch/stop/clear never reads the page content, so a
                    # launcher that keeps changing during capture must not fail it as page_changed.
                    raise ConnectionFault("page_changed")
                if original.geometry != fresh.geometry:
                    raise ConnectionFault("display_geometry_changed")
                validate_source(body, fresh)
                lease.phase = "pre_dispatch"
                async with slot.lock:
                    await self.check_borrowed_session(slot, session, beginning=True)
                active()
                if body.kind in {
                    "tap",
                    "long",
                    "swipe",
                } and fresh.geometry != await self.backend.action_geometry(serial):
                    raise ConnectionFault("display_geometry_changed")
                active()
                if time.monotonic() - fresh.captured_monotonic > DISPATCH_MAX_AGE:
                    measured_binding_ms = int((time.monotonic() - fresh.captured_monotonic) * 1000)
                    raise ConnectionFault("action_source_stale")
                remaining_ms = min(body.agent_timeout_ms, int((deadline - loop.time()) * 1000))
                if remaining_ms < 100:
                    raise ConnectionFault("action_timeout")
                # Invalidate the old target even when the platform later rejects this submission.
                session.latest_observation = None
                slot.native_cleanup_guard = True
                lease.phase = "dispatching"
                try:
                    self.ledger.mark_dispatched(
                        body.request_id,
                        agent_lifecycle_id=session.health.lifecycle_id,
                        agent_pid=session.generation.agent_pid,
                    )
                except LedgerError:
                    raise ConnectionFault("control_ledger_unavailable") from None
                reply = await lease.exchange.run(
                    session.forward, session.generation.token, body.wire_params(), remaining_ms
                )
                reply_status, completion_source = reply.status, reply.completion_source
                active()
                lease.phase = "validating_receipt"
                async with slot.lock:
                    await self.check_borrowed_session(slot, session, beginning=False)
                active()
                receipt_validated = True
                status, error = reply.status, reply.error_code
                if status == "failed" and error in UNCERTAIN_CODES:
                    status = "uncertain"
                    quarantine()
                    try:
                        self.ledger.finish(body.request_id, "uncertain", error_code=error)
                    except LedgerError:
                        log.error(
                            "Action ledger could not record uncertainty",
                            extra={
                                "event": "gate.ledger_write_failed",
                                "serial": serial,
                                "error_category": "ledger",
                            },
                        )
                else:
                    lease.settled = True
                    if status in {"completed", "failed"}:
                        try:
                            self.ledger.mark_acknowledged(body.request_id, error_code=error)
                        except LedgerError:
                            raise ConnectionFault("control_ledger_unavailable") from None
                completed = status == "completed"
                if completed or status == "failed":
                    lease.phase = "post_observation"
                    remaining = min(30.0, deadline - loop.time())
                    if remaining < 1:
                        verification_error = "post_observation_timeout"
                    else:
                        after = await self.observe(
                            serial, session.id, str(uuid4()), timeout=remaining, for_action=True
                        )
                        active()
                        # A package-scoped key/launch/stop can legitimately leave no
                        # controllable window (the launcher exposes no tree to the Agent).
                        # That is a successful departure read, not a failed observation.
                        departure = body.kind in DEPARTING_ACTIONS and empty_window(after)
                        if after["success"] is not True and not departure:
                            verification_error = "post_observation_unavailable"
        except (asyncio.CancelledError, TimeoutError, ConnectionFault) as exc:
            failure_phase = lease.phase
            if original is not None:
                binding_age_ms = int((time.monotonic() - original.captured_monotonic) * 1000)
            if measured_binding_ms is not None:
                binding_age_ms = measured_binding_ms
            code = (
                lease.reason or "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "action_timeout"
                if isinstance(exc, TimeoutError)
                else exc.code
                if exc.code in ACTION_FAULTS
                else "action_failed"
            )
            lease.abort(code, interrupt=False)
            if completed:
                # Observation/lifecycle failure cannot rewrite a previously validated completion.
                verification_error = code
                after = None
            else:
                error = code
                status = "uncertain" if exchange.sent and not lease.settled else "rejected"
                if status == "uncertain":
                    quarantine()
            if code not in {
                "cancelled",
                "action_timeout",
                "control_epoch_changed",
                "page_changed",
                "action_source_stale",
                "action_source_unavailable",
                "action_source_incomplete",
                "action_target_unavailable",
                "action_coordinates_out_of_range",
                "coordinate_space_mismatch",
                "coordinate_mapping_unavailable",
                "rotation_unverified",
                "unsupported_display",
                "display_geometry_unavailable",
                "display_geometry_changed",
                "sensitive_target",
                "protected_target",
                "target_changed",
                "unsupported_action",
                "platform_operation_pending",
            }:
                try:
                    await finish_cleanup(fault_cleanup(code))
                except (TimeoutError, ConnectionFault):
                    slot.cleanup_pending = True
        except Exception:
            failure_phase = lease.phase
            error = "action_failed"
            lease.abort(error, interrupt=False)
            if completed:
                verification_error, after = error, None
                error = None
            else:
                status = "uncertain" if exchange.sent else "rejected"
                if exchange.sent:
                    quarantine()
            try:
                await finish_cleanup(fault_cleanup("action_failed"))
            except (TimeoutError, ConnectionFault):
                slot.cleanup_pending = True
        finally:
            if lease.reason is not None or status == "uncertain":
                session.latest_observation = None
            if slot.action is lease:
                slot.action = None
            lease.phase = "finished"
            lease.done.set()

        if status == "uncertain":
            self.set_fault(slot, "recovery_required")
        receipts: list[dict[str, object]] = []
        if exchange.dispatched_received:
            receipts.append(
                {
                    "status": "dispatched",
                    "source": backend_name,
                    "meaning": "one_submission_committed_not_business_success",
                }
            )
        receipts.append(
            {
                "status": status,
                "source": backend_name if receipt_validated else "runtime",
                "error": (
                    {"code": error, "phase": failure_phase, "binding_age_ms": binding_age_ms}
                    if error
                    else None
                ),
            }
        )
        final_state: Literal["verified", "failed", "uncertain"] = (
            "verified"
            if status == "completed"
            else "uncertain"
            if status == "uncertain"
            else "failed"
        )
        ledger_view: dict[str, object] | None = None
        if prepared:
            try:
                self.ledger.finish(
                    body.request_id,
                    final_state,
                    error_code=error or verification_error,
                    post_observation_available=after is not None,
                )
            except LedgerError:
                log.error(
                    "Action ledger could not record final state",
                    extra={
                        "event": "gate.ledger_write_failed",
                        "serial": serial,
                        "error_category": "ledger",
                    },
                )
            try:
                record = self.ledger.action(body.request_id)
            except LedgerError:
                record = None
            ledger_view = {
                "action_id": body.request_id,
                "state": record["state"] if record is not None else None,
                "run_id": body.run_id,
                "step_id": body.step_id,
                "control_epoch": body.control_epoch,
                "source": body.source,
            }
        log.info(
            "Android action finished",
            extra={
                "event": "device.action_finished",
                "serial": serial,
                "session_id": body.session_id,
                "error_category": error or verification_error,
                "failure_phase": failure_phase,
                "binding_age_ms": binding_age_ms,
                "duration_ms": (time.monotonic() - started) * 1000,
            },
        )
        return {
            **base,
            "status": status,
            "action_completed": completed,
            "agent_lifecycle_id": session.health.lifecycle_id,
            "agent_http_session_id": exchange.wire_session_id,
            "agent_terminal_status": reply_status if backend_name == "agent_v1" else None,
            "completion_source": completion_source,
            "receipts": receipts,
            "error": (
                {"code": error, "phase": failure_phase, "binding_age_ms": binding_age_ms}
                if error
                else None
            ),
            "lifecycle_status": lease.reason or "current",
            "recovery_required": slot.quarantine is not None,
            "pre_observation_id": before_id,
            "post_observation": after,
            "verification": {
                "status": "available"
                if after is not None
                and not verification_error
                and (
                    after["success"] is True
                    or (body.kind in DEPARTING_ACTIONS and empty_window(after))
                )
                else "unavailable",
                "business_status": "not_evaluated",
                "error": {"code": verification_error} if verification_error else None,
                "required": "evaluate_new_post_observation_not_action_completion",
            },
            "ledger": ledger_view,
            "control": {
                **cast(dict[str, object], base["control"]),
                "ledger_state": ledger_view["state"] if ledger_view is not None else None,
            },
            "framing": {
                "validated": receipt_validated,
                "request_sent": exchange.sent,
                "dispatched_received": exchange.dispatched_received,
                "terminal_received": exchange.terminal_received,
                "eof_received": exchange.eof_received,
            },
            "cleanup": copy.deepcopy(exchange.cleanup),
            "runtime_time": {
                "started_at_epoch_ms": started_epoch,
                "finished_at_epoch_ms": time.time_ns() // 1_000_000,
                "duration_ms": (time.monotonic() - started) * 1000,
            },
        }

    async def cancel_action(
        self,
        serial: str,
        session_id: str,
        request_id: str,
        operation_id: str,
        *,
        reason: str = "cancelled",
    ) -> dict[str, object]:
        for identifier in (session_id, request_id, operation_id):
            canonical_uuid(identifier)
        slot = self.slot(serial)
        lease = slot.action
        if (
            lease is None
            or lease.session_id != session_id
            or lease.request_id != request_id
            or lease.operation_id != operation_id
        ):
            return {"cancelled": False, "cleanup_complete": True, "rollback_guaranteed": False}
        lease.abort(reason)
        try:
            await finish_cleanup(asyncio.wait_for(lease.done.wait(), 3))
        except TimeoutError:
            return {"cancelled": True, "cleanup_complete": False, "rollback_guaranteed": False}
        return {"cancelled": True, "cleanup_complete": True, "rollback_guaranteed": False}

    async def cancel_observation(
        self, serial: str, session_id: str, request_id: str
    ) -> dict[str, object]:
        canonical_uuid(session_id)
        canonical_uuid(request_id)
        slot = self.slot(serial)
        lease = slot.observation
        if (
            slot.action is not None
            or lease is None
            or lease.session_id != session_id
            or lease.request_id != request_id
            or slot.session is None
            or slot.session.id != session_id
        ):
            return {"cancelled": False, "cleanup_complete": True}
        lease.abort("cancelled")
        try:
            await finish_cleanup(asyncio.wait_for(lease.done.wait(), 3))
        except TimeoutError:
            return {"cancelled": True, "cleanup_complete": False}
        return {"cancelled": True, "cleanup_complete": True}

    async def cancel(self, serial: str, operation_id: str) -> bool:
        canonical_uuid(operation_id)
        slot = self.slot(serial)
        task = slot.operation
        if slot.operation_id != operation_id:
            return False
        if task is None:
            async with slot.lock:
                if not slot.awaiting_authorization or slot.operation_id != operation_id:
                    return False
                slot.awaiting_authorization = False
                slot.authorization_reason = None
                slot.operation_id = None
                slot.desired = False
                await finish_cleanup(self.invalidate(slot, "cancelled"))
                await finish_cleanup(self.restore(slot))
                self.set_fault(slot, "cancelled")
            return True
        task.cancel()
        await finish_cleanup(asyncio.gather(task, return_exceptions=True))
        return True

    async def disconnect(self, serial: str, session_id: str) -> dict[str, object]:
        canonical_uuid(session_id)
        slot = self.slot(serial)
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            slot.desired = False
            action = slot.action
            if action is not None:
                action.abort("disconnected")
                with contextlib.suppress(TimeoutError):
                    await finish_cleanup(asyncio.wait_for(action.done.wait(), 3))
            observation = slot.observation
            if observation is not None:
                observation.abort("disconnected")
                with contextlib.suppress(TimeoutError):
                    await finish_cleanup(asyncio.wait_for(observation.done.wait(), 3))
            # The same borrowed forward must still exist for the native-idle check.
            await finish_cleanup(self.restore(slot))
            await finish_cleanup(self.invalidate(slot, "disconnected"))
            if not slot.private_cleanup_pending:
                slot.status, slot.code = "disconnected", None
        if observation is not None:
            with contextlib.suppress(TimeoutError):
                await finish_cleanup(asyncio.wait_for(observation.done.wait(), 3))
        return self.snapshot(slot)

    async def control(self, serial: str, session_id: str | None = None) -> dict[str, object]:
        slot = self.slot(serial)
        if session_id is not None:
            canonical_uuid(session_id)
            async with slot.lock:
                if slot.session is None or slot.session.id != session_id:
                    raise ConnectionFault("stale_session")
        try:
            state = self.gate.snapshot(serial)
            interrupted = self.ledger.interrupted_actions(serial)
        except LedgerError:
            raise ConnectionFault("control_ledger_unavailable") from None
        return {
            "serial": serial,
            "session_id": slot.session.id if slot.session else None,
            "control": state.as_dict(),
            "interrupted_actions": [
                {
                    "action_id": item["action_id"],
                    "state": item["state"],
                    "kind": item["kind"],
                    "source": item["source"],
                    "control_epoch": item["control_epoch"],
                    "run_id": item["run_id"],
                    "agent_pid": item["agent_pid"],
                    "error_code": item["error_code"],
                    "created_at_ms": item["created_at_ms"],
                }
                for item in interrupted
            ],
        }

    async def acquire_control(
        self, serial: str, *, session_id: str, source: str, run_id: str | None
    ) -> dict[str, object]:
        canonical_uuid(session_id)
        if run_id is not None:
            canonical_uuid(run_id)
        slot = self.slot(serial)
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            try:
                state = self.gate.acquire(
                    serial, session_id=session_id, source=source, run_id=run_id
                )
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
        return {"serial": serial, "control": state.as_dict()}

    async def take_over_control(
        self,
        serial: str,
        *,
        session_id: str,
        source: str,
        run_id: str | None,
        timeout: float = 30.0,  # noqa: ASYNC109
    ) -> dict[str, object]:
        canonical_uuid(session_id)
        if run_id is not None:
            canonical_uuid(run_id)
        slot = self.slot(serial)
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            try:
                _control, previous_run, previous_epoch = self.gate.take_over(
                    serial, session_id=session_id, source=source, run_id=run_id
                )
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
            action = slot.action
            observation = slot.observation
        in_flight = (
            {
                "request_id": action.request_id,
                "operation_id": action.operation_id,
                "phase": action.phase,
                "request_sent": action.exchange.sent,
                "dispatched_received": action.exchange.dispatched_received,
            }
            if action is not None
            else {
                "request_id": observation.request_id,
                "phase": observation.phase,
                "request_sent": observation.exchange.sent,
            }
            if observation is not None
            else None
        )
        state = "canceled"
        if observation is not None:
            observation.abort(
                "cancelled", interrupt=action is None or observation.task is not action.task
            )
        if action is not None:
            action.abort("control_epoch_changed")
        try:
            async with asyncio.timeout(timeout):
                if observation is not None:
                    await observation.done.wait()
                if action is not None:
                    await action.done.wait()
        except TimeoutError:
            state = "stopping"
        control = self.gate.snapshot(serial)
        recovery_required = slot.quarantine is not None or control.recovery_required
        if recovery_required or slot.session is None:
            state = "interrupted"
        elif slot.cleanup_pending or any(
            lease is not None
            and lease.exchange.sent
            and lease.exchange.cleanup.get("connection_closed") is not True
            for lease in (observation, action)
        ):
            state = "stopping"
        try:
            self.ledger.finish_takeover(previous_run, state)
            if previous_run is not None:
                state = self.ledger.run_state(previous_run) or state
        except LedgerError:
            raise ConnectionFault("control_ledger_unavailable") from None
        return {
            "serial": serial,
            "status": "stopped" if state == "canceled" else state,
            "control": control.as_dict(),
            "previous_epoch": previous_epoch,
            "previous_run_id": previous_run,
            "previous_run_state": state,
            "in_flight": in_flight,
            "recovery_required": recovery_required,
            "stopped_confirmed": state == "canceled",
        }

    async def release_control(
        self,
        serial: str,
        *,
        session_id: str,
        control_epoch: int,
        run_id: str | None,
        wait_for_idle_ms: int = 0,
    ) -> dict[str, object]:
        canonical_uuid(session_id)
        if run_id is not None:
            canonical_uuid(run_id)
        if type(wait_for_idle_ms) is not int or not 0 <= wait_for_idle_ms <= 60_000:
            raise ConnectionFault("invalid_message")
        slot = self.slot(serial)
        if wait_for_idle_ms:
            if run_id is None:
                raise ConnectionFault("run_not_active")
            try:
                async with asyncio.timeout(wait_for_idle_ms / 1000):
                    while True:
                        async with slot.lock:
                            if slot.session is None or slot.session.id != session_id:
                                raise ConnectionFault("stale_session")
                            current = self.gate.snapshot(serial)
                            if current.session_id != session_id:
                                raise ConnectionFault("stale_control_session")
                            if current.epoch != control_epoch:
                                raise ConnectionFault("stale_control_epoch")
                            if current.recovery_required:
                                raise ConnectionFault("recovery_required")
                            # Reserve a terminal nonce if the closing window's claim has not
                            # reached the gate yet. A late acquire cannot adopt this UUID.
                            if (
                                current.holder_source is None
                                and current.run_id is None
                                and self.ledger.run_state(run_id) is None
                            ):
                                self.ledger.start_run(run_id, serial, control_epoch)
                                self.ledger.set_run_state(run_id, "canceled")
                                return {"serial": serial, "control": current.as_dict()}
                            if current.holder_source != "manual" or current.run_id != run_id:
                                raise ConnectionFault("run_not_active")
                            action, observation, operation = (
                                slot.action,
                                slot.observation,
                                slot.operation,
                            )
                            if action is None and observation is None and operation is None:
                                if slot.cleanup_pending:
                                    raise ConnectionFault("cleanup_required")
                                released = self.gate.release(
                                    serial,
                                    session_id=session_id,
                                    control_epoch=control_epoch,
                                    run_id=run_id,
                                )
                                return {"serial": serial, "control": released.as_dict()}
                        # Wait on the existing lease, never cancel native work or spin on a flag.
                        if action is not None:
                            if action.done.is_set():
                                raise ConnectionFault("cleanup_required")
                            await action.done.wait()
                        elif observation is not None:
                            if observation.done.is_set():
                                raise ConnectionFault("cleanup_required")
                            await observation.done.wait()
                        elif operation is not None:
                            if operation.done():
                                raise ConnectionFault("cleanup_required")
                            await asyncio.wait({operation})
            except TimeoutError:
                raise ConnectionFault("control_release_timeout") from None
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            if slot.action is not None:
                raise ConnectionFault("action_in_progress")
            if slot.observation is not None:
                raise ConnectionFault("observation_in_progress")
            try:
                state = self.gate.release(
                    serial,
                    session_id=session_id,
                    control_epoch=control_epoch,
                    run_id=run_id,
                )
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
        return {"serial": serial, "control": state.as_dict()}

    async def finish_run(
        self,
        serial: str,
        *,
        session_id: str,
        control_epoch: int,
        run_id: str,
        state: str,
    ) -> dict[str, object]:
        """Close one interpreter run after its last read/write has settled."""
        canonical_uuid(session_id)
        canonical_uuid(run_id)
        slot = self.slot(serial)
        async with slot.lock:
            if slot.session is None or slot.session.id != session_id:
                raise ConnectionFault("stale_session")
            if slot.action is not None or slot.observation is not None:
                raise ConnectionFault("operation_in_progress")
            try:
                control = self.gate.close_run(
                    serial,
                    session_id=session_id,
                    control_epoch=control_epoch,
                    run_id=run_id,
                    state=state,
                )
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
            return {
                "serial": serial,
                "run_id": run_id,
                "state": state,
                "control": control.as_dict(),
            }

    async def acknowledge_recovery(
        self, serial: str, *, session_id: str, confirmed: bool
    ) -> dict[str, object]:
        canonical_uuid(session_id)
        if confirmed is not True:
            raise ConnectionFault("manual_recovery_not_confirmed")
        slot = self.slot(serial)
        async with slot.lock:
            session = slot.session
            if session is None or session.id != session_id:
                raise ConnectionFault("stale_session")
            current_pid = session.generation.agent_pid
            try:
                interrupted = self.ledger.interrupted_actions(serial)
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
            try:
                await self.check_borrowed_session(slot, session, beginning=True)
            except ConnectionFault as exc:
                await finish_cleanup(self.invalidate(slot, exc.code))
                self.set_fault(slot, exc.code)
                raise
            if slot.session is not session:
                raise ConnectionFault("stale_session")
            try:
                state = self.gate.acknowledge_recovery(serial, session_id=session_id)
            except GateError as exc:
                raise ConnectionFault(exc.code) from None
            except LedgerError:
                raise ConnectionFault("control_ledger_unavailable") from None
        return {
            "serial": serial,
            "acknowledged": True,
            "control": state.as_dict(),
            "interrupted_action_ids": [item["action_id"] for item in interrupted],
            "current_agent_pid": current_pid,
            "interrupted_agent_pids": sorted(
                {item["agent_pid"] for item in interrupted if type(item["agent_pid"]) is int}
            ),
        }

    def ledger_actions(self, serial: str, *, limit: int = 50) -> dict[str, object]:
        validate_serial(serial)
        try:
            actions = self.ledger.list_actions(serial, limit=limit)
        except LedgerError:
            raise ConnectionFault("control_ledger_unavailable") from None
        return {"serial": serial, "actions": actions}

    def start(self) -> None:
        if self.watcher is None and not self.closed:
            self.watcher = asyncio.create_task(self.watch(), context=Context())

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            if any(
                slot.desired or slot.session is not None or slot.cleanup_pending
                for slot in self.slots.values()
            ):
                await self.list_devices()
                for slot in tuple(self.slots.values()):
                    if slot.cleanup_pending and not slot.lock.locked():
                        async with slot.lock:
                            # Finish only previously requested cleanup, never preparation.
                            if slot.private_cleanup_pending:
                                await self.restore(slot, keep_peer=slot.awaiting_authorization)
                            else:
                                await self.clean_forwards(slot)

    async def close(self) -> None:
        async def cleanup() -> None:
            watcher, self.watcher = self.watcher, None
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            slots = tuple(self.slots.values())
            actions = [s.action for s in slots if s.action is not None]
            for action in actions:
                action.abort("runtime_stopped")
            for action in actions:
                with contextlib.suppress(TimeoutError):
                    await finish_cleanup(asyncio.wait_for(action.done.wait(), 3))
            observations = [s.observation for s in slots if s.observation is not None]
            for observation in observations:
                observation.abort("runtime_stopped")
            for observation in observations:
                with contextlib.suppress(TimeoutError):
                    await finish_cleanup(asyncio.wait_for(observation.done.wait(), 3))
            operations = [s.operation for s in slots if s.operation is not None]
            for task in operations:
                task.cancel()
            await asyncio.gather(*operations, return_exceptions=True)
            for slot in slots:
                async with slot.lock:
                    slot.desired = False
                    slot.awaiting_authorization = False
                    slot.authorization_reason = None
                    slot.operation_id = None
                    await self.restore(slot)
                    with contextlib.suppress(ConnectionFault):
                        await self.invalidate(slot, "runtime_stopped")
                    if not slot.private_cleanup_pending:
                        slot.status = (
                            "recovery_required" if slot.quarantine is not None else "disconnected"
                        )

        if self.closing is None:
            self.closed = True
            self.closing = asyncio.create_task(cleanup(), context=Context())
        # All callers join the same owned shutdown. Cancellation propagates only after cleanup.
        try:
            await complete_mutation(self.closing)
        finally:
            self.ledger.close()
