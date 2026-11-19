"""Fake-only P1.4 lifecycle/source/ADB/API regressions, not dual-physical acceptance."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android.actions import (
    DISPATCH_MAX_AGE,
    SOURCE_MAX_AGE,
    ActionBody,
)
from pulse_runtime.drivers.android.agent_action import (
    ActionExchange,
    ActionReply,
    AdbActionExchange,
)
from pulse_runtime.drivers.android.agent_transport import ConnectionFault, parse_action_geometry
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from runtime.tests.test_device_connections import (
    ACTIVE_SETTINGS,
    SERIAL_A,
    SERIAL_B,
    TOKEN_A,
    TOKEN_B,
    FakeAndroidBackend,
    Gate,
    session_id,
)
from runtime.tests.test_device_connections import (
    forbid_external_io as forbid_external_io,
)
from runtime.tests.test_observations import ExchangeFactory


class ActionBackend(FakeAndroidBackend):
    def __init__(self) -> None:
        super().__init__()
        self.geometry = {"display_id": 0, "width": 4, "height": 4, "rotation": 0}
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self.command_gate: Gate | None = None
        self.command_fault: str | None = None
        self.geometry_fault = False

    async def action_geometry(self, serial: str) -> dict[str, int]:
        await self.visit(serial, "action_geometry")
        if self.geometry_fault:
            raise ConnectionFault("display_geometry_unavailable")
        return dict(self.geometry)

    async def command(self, serial: str, *args: str, timeout: float = 5) -> str:  # noqa: ASYNC109
        if args[:2] not in {("shell", "am"), ("shell", "input"), ("shell", "pm")}:
            return await super().command(serial, *args, timeout=timeout)
        assert args[2] in {"start", "force-stop", "keyevent", "clear"}
        assert all(";" not in value and "\n" not in value and "$" not in value for value in args)
        self.commands.append((serial, args))
        if self.command_gate is not None:
            await self.command_gate.wait()
        if self.command_fault:
            raise ConnectionFault(self.command_fault)
        return "Status: ok\n" if args[2] == "start" else "Success" if args[2] == "clear" else ""


class ActionFactory:
    def __init__(self, backend: ActionBackend) -> None:
        self.backend = backend
        self.exchanges: list[FakeAction] = []
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.states = {SERIAL_A: False, SERIAL_B: False}
        self.mode = "completed"
        self.gate: Gate | None = None
        self.before_send = False
        self.swallow_cancel = False

    def __call__(self, request_id: str) -> FakeAction:
        exchange = FakeAction(request_id, self)
        self.exchanges.append(exchange)
        return exchange

    def source(self, serial: str, value: dict[str, object]) -> dict[str, object]:
        node = value["sources"]["tree"]["data"]["nodes"][0]
        node["states"].update(
            enabled=True,
            visible=True,
            clickable=True,
            editable=True,
            checkable=True,
            checked=self.states[serial],
        )
        return value


class FakeAction(ActionExchange):
    def __init__(self, request_id: str, factory: ActionFactory) -> None:
        super().__init__(request_id)
        self.factory = factory

    async def run(self, forward, token, params, timeout_ms) -> ActionReply:
        factory = self.factory
        self._started = True
        try:
            if factory.gate and factory.before_send:
                await factory.gate.wait()
            self.sent = True
            self.wire_session_id = str(uuid4())
            factory.calls.append((forward.serial, token, copy.deepcopy(params)))
            if factory.mode == "rejected":
                self.terminal_received = self.eof_received = True
                return ActionReply(self.wire_session_id, "rejected", "target_unavailable")
            self.dispatched_received = True
            if factory.gate and not factory.before_send:
                try:
                    await factory.gate.wait()
                except asyncio.CancelledError:
                    if not factory.swallow_cancel:
                        raise
                    await factory.gate.release.wait()
            if factory.mode in {
                "action_timeout",
                "action_protocol_invalid",
                "action_terminal_missing",
            }:
                raise ConnectionFault(factory.mode)
            if factory.mode == "raw_error":
                raise RuntimeError("private-platform-exception " + TOKEN_A)
            self.terminal_received = self.eof_received = True
            if factory.mode == "failed":
                return ActionReply(self.wire_session_id, "failed", "gesture_cancelled")
            if factory.mode == "uncertain":
                return ActionReply(self.wire_session_id, "failed", "action_uncertain")
            factory.states[forward.serial] = not factory.states[forward.serial]
            source = (
                "gesture_callback"
                if params["kind"] in {"tap", "swipe"}
                else "accessibility_action_return"
            )
            return ActionReply(self.wire_session_id, "completed", None, source)
        finally:
            self.cleanup["connection_closed"] = True


@pytest.fixture
async def action_rig() -> AsyncIterator[
    tuple[ActionBackend, DeviceConnectionManager, ExchangeFactory, ActionFactory]
]:
    backend = ActionBackend()
    actions = ActionFactory(backend)
    observations = ExchangeFactory(backend)
    for serial in (SERIAL_A, SERIAL_B):
        observations.modify_result[serial] = lambda value, serial=serial: actions.source(
            serial, value
        )
    manager = DeviceConnectionManager(
        backend, readiness_wait=0, observation_factory=observations, action_factory=actions
    )
    try:
        yield backend, manager, observations, actions
    finally:
        for gate in [
            actions.gate,
            backend.command_gate,
            *observations.gate.values(),
            *backend.gates.values(),
        ]:
            if gate:
                gate.release.set()
        await manager.close()


async def source(manager, serial=SERIAL_A) -> dict[str, object]:
    slot = manager.slot(serial)
    identifier = slot.session.id if slot.session else session_id(await manager.connect(serial))
    result = await manager.observe(serial, identifier, str(uuid4()), for_action=True)
    assert result["success"] is True
    return result


def body(view: dict[str, object], kind="node_click", **changes) -> ActionBody:
    params = {
        "tap": {"x": 1, "y": 1},
        "long": {"x": 1, "y": 1, "duration_ms": 600},
        "swipe": {"x": 1, "y": 1, "end_x": 2, "end_y": 2},
        "set_text": {"text": "private-input"},
        "node_click": {},
        "key": {"code": 4},
        "launch": {"package": "com.pulse.target", "activity": ".FormActivity"},
        "stop": {"package": "com.pulse.target"},
        "clear": {"package": "com.pulse.target", "confirmed": True},
    }
    value = {
        "serial": view["serial"],
        "session_id": view["session_id"],
        "request_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "kind": kind,
        "target": {
            "observation_id": view["request_id"],
            "source": "tree",
            "coordinate_space": "display_pixels",
            "display_id": 0,
            "expected_package": "com.pulse.target",
            "node_path": [],
        },
        "params": params[kind],
    }
    value.update(changes)
    return ActionBody.model_validate(value)


@pytest.mark.parametrize(
    "kind", ["tap", "long", "swipe", "set_text", "node_click", "key", "launch", "stop", "clear"]
)
async def test_all_actions_have_one_submission_new_post_source_and_no_business_success(
    action_rig, kind
) -> None:
    backend, manager, observations, actions = action_rig
    view = await source(manager)
    request = body(view, kind)
    result = await manager.action(SERIAL_A, request)
    assert result["status"] == "completed" and result["action_completed"] is True
    assert result["business_success"] is None and result["automatic_replay"] is False
    assert [item["status"] for item in result["receipts"]] == ["dispatched", "completed"]
    assert result["verification"]["business_status"] == "not_evaluated"
    assert result["verification"]["status"] == "available"
    after = result["post_observation"]
    assert after["request_id"] not in {view["request_id"], result["pre_observation_id"]}
    assert after["session_id"] == view["session_id"] and after["serial"] == SERIAL_A
    assert len(observations.calls) == 3
    if kind in {"key", "launch", "stop", "clear"}:
        assert len(backend.commands) == 1 and actions.calls == []
        assert (
            result["backend"] == "runtime_adb"
            and result["completion_source"] == "adb_command_return"
        )
    else:
        assert len(actions.calls) == 1 and backend.commands == []
        assert actions.calls[0][1] == TOKEN_A
        assert actions.calls[0][2]["kind"] == ("tap" if kind == "long" else kind)
        if kind == "long":
            assert actions.calls[0][2]["duration_ms"] == 600
        assert after["sources"]["tree"]["data"]["nodes"][0]["states"]["checked"] is True
    assert backend.mutations == []


@pytest.mark.parametrize("kind", ["tap", "long", "swipe", "set_text", "node_click"])
@pytest.mark.parametrize(
    "mode, expected",
    [
        ("rejected", "rejected"),
        ("failed", "failed"),
        ("uncertain", "uncertain"),
        ("action_timeout", "uncertain"),
        ("action_protocol_invalid", "uncertain"),
        ("action_terminal_missing", "uncertain"),
    ],
)
async def test_native_failures_are_not_replayed_or_promoted_by_observation(
    action_rig, kind, mode, expected
) -> None:
    _, manager, observations, actions = action_rig
    view = await source(manager)
    actions.mode = mode
    result = await manager.action(SERIAL_A, body(view, kind))
    assert result["status"] == expected and result["action_completed"] is False
    assert result["business_success"] is None and len(actions.calls) == 1
    assert result["recovery_required"] is (expected == "uncertain")
    if expected == "uncertain":
        assert len(observations.calls) == 2 and result["post_observation"] is None
        assert manager.slot(SERIAL_A).desired is False
        assert (await manager.connect(SERIAL_A))["ready"] is False
        assert len(actions.calls) == 1
    if expected == "failed":
        assert len(observations.calls) == 3


async def test_completed_with_nonmatching_post_observation_is_not_a_pass(action_rig) -> None:
    _, manager, _, _ = action_rig
    result = await manager.action(SERIAL_A, body(await source(manager)))
    observed = result["post_observation"]["sources"]["tree"]["data"]["nodes"][0]
    matched = observed["text"] == "not-the-expected-result"
    assert result["action_completed"] is True and matched is False
    assert result["business_success"] is None and "success" not in result


async def test_post_observation_failure_does_not_relabel_a_valid_action_completion(
    action_rig,
) -> None:
    _, manager, observations, actions = action_rig
    view = await source(manager)
    original = actions.source

    def fail_after(serial, value):
        if actions.states[serial]:
            value["sources"]["tree"].update(
                status="failed",
                data=None,
                error={"code": "window_unavailable", "message": "unavailable", "retryable": False},
                completeness={"status": "unavailable", "reasons": ["window_unavailable"]},
            )
        return original(serial, value) if not actions.states[serial] else value

    observations.modify_result[SERIAL_A] = lambda value: fail_after(SERIAL_A, value)
    result = await manager.action(SERIAL_A, body(view))
    assert result["status"] == "completed" and result["action_completed"] is True
    assert result["error"] is None and result["verification"]["status"] == "unavailable"
    assert result["business_success"] is None


@pytest.mark.parametrize("kind", ["key", "launch", "stop"])
async def test_departure_action_accepts_a_post_window_without_any_tree(action_rig, kind) -> None:
    """HOME/stop can land on a launcher the Agent cannot observe; that is the evidence."""
    backend, manager, observations, _ = action_rig
    view = await source(manager)
    inner = observations.modify_result[SERIAL_A]
    empty = False

    def empty_after(value):
        value = inner(value)
        if empty:
            value["sources"]["tree"].update(
                status="unavailable",
                data=None,
                error={"code": "window_unavailable", "message": "unavailable", "retryable": False},
                completeness={"status": "unavailable", "reasons": ["window_unavailable"]},
            )
        return value

    observations.modify_result[SERIAL_A] = empty_after
    backend.command_gate = Gate()
    pending = asyncio.create_task(manager.action(SERIAL_A, body(view, kind)))
    await backend.command_gate.entered.wait()
    # Only the post-observation may be empty: enable it once the action is dispatched.
    empty = True
    backend.command_gate.release.set()
    result = await asyncio.wait_for(pending, 5)
    assert result["status"] == "completed" and result["action_completed"] is True
    tree = result["post_observation"]["sources"]["tree"]
    assert tree["status"] == "unavailable" and tree["data"] is None
    assert result["verification"]["status"] == "available"
    assert result["verification"]["business_status"] == "not_evaluated"
    assert result["business_success"] is None


@pytest.mark.parametrize("kind", ["launch", "stop", "key"])
async def test_package_scoped_action_does_not_need_a_content_fingerprint(action_rig, kind) -> None:
    """A live launcher emits events during capture, so its tree has no stable fingerprint."""
    _, manager, observations, actions = action_rig

    def unsettled(value):
        actions.source(SERIAL_A, value)
        value["revision_start"] = 1
        value["revision_end"] = 2

    observations.modify_result[SERIAL_A] = unsettled
    view = await source(manager)
    assert manager.slot(SERIAL_A).session.latest_observation.fingerprint is None
    result = await manager.action(SERIAL_A, body(view, kind))
    assert result["status"] == "completed"
    assert result["action_completed"] is True


async def test_node_action_still_requires_the_fingerprint(action_rig) -> None:
    _, manager, observations, actions = action_rig

    def unsettled(value):
        actions.source(SERIAL_A, value)
        value["revision_start"] = 1
        value["revision_end"] = 2

    observations.modify_result[SERIAL_A] = unsettled
    view = await source(manager)
    result = await manager.action(SERIAL_A, body(view, "node_click"))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "action_source_incomplete"


@pytest.mark.parametrize("field", ["request_id", "operation_id"])
async def test_reused_ids_are_tombstoned_without_submission_even_after_reconnect(
    action_rig, field
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    first = body(view)
    assert (await manager.action(SERIAL_A, first))["status"] == "completed"
    await manager.disconnect(SERIAL_A, view["session_id"])
    next_view = await source(manager)
    duplicate = body(next_view, **{field: getattr(first, field)})
    assert (await manager.action(SERIAL_A, duplicate))["status"] == "rejected"
    assert len(actions.calls) == 1


@pytest.mark.parametrize(
    "case,code",
    [
        ("serial", "serial_mismatch"),
        ("session", "stale_session"),
        ("observation", "action_source_stale"),
        ("age", "action_source_stale"),
        ("other_runtime", "action_source_stale"),
        ("other_display", "unsupported_display"),
        ("space", "coordinate_space_mismatch"),
        ("rotation", "rotation_unverified"),
        ("outside", "action_coordinates_out_of_range"),
        ("node", "action_target_unavailable"),
        ("package", "target_changed"),
        ("sensitive", "sensitive_target"),
        ("enabled_unknown", "action_target_unavailable"),
        ("unchecked_clickable", "action_target_unavailable"),
    ],
)
async def test_invalid_provenance_fails_before_dispatch(action_rig, case, code) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    request = body(view, "tap" if case in {"rotation", "outside", "space"} else "node_click")
    slot = manager.slot(SERIAL_A)
    binding = slot.session.latest_observation
    if case == "serial":
        request.serial = SERIAL_B
    elif case == "session":
        request.session_id = str(uuid4())
    elif case == "observation":
        request.target.observation_id = str(uuid4())
    elif case == "age":
        slot.session.latest_observation = replace(binding, captured_monotonic=time.monotonic() - 31)
    elif case == "other_runtime":
        slot.session.latest_observation = replace(binding, session_id=str(uuid4()))
    elif case == "other_display":
        request.target.display_id = 1
    elif case == "space":
        request.target.coordinate_space = "screenshot_pixels"
    elif case == "rotation":
        slot.session.latest_observation = replace(binding, geometry=None)
    elif case == "outside":
        request.params["x"] = 4
    elif case == "node":
        request.target.node_path = [1]
    elif case == "package":
        request.target.expected_package = "com.other.target"
    elif case == "sensitive":
        binding.nodes[()]["text_redacted"] = True
    elif case == "enabled_unknown":
        binding.nodes[()]["states"]["enabled"] = None
    else:
        binding.nodes[()]["states"]["clickable"] = None
    result = await manager.action(SERIAL_A, request)
    assert result["status"] == "rejected" and result["error"]["code"] == code
    assert actions.calls == []


@pytest.mark.parametrize("source_name", ["screenshot", "ocr"])
async def test_ocr_and_screenshot_never_supply_native_states_or_an_invented_mapping(
    action_rig, source_name
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    view = await manager.observe(
        SERIAL_A, view["session_id"], str(uuid4()), screenshot=True, ocr=True, for_action=True
    )
    request = body(view, "tap")
    request.target.source = source_name
    request.target.coordinate_space = "screenshot_pixels"
    result = await manager.action(SERIAL_A, request)
    assert (
        result["status"] == "rejected"
        and result["error"]["code"] == "coordinate_mapping_unavailable"
    )
    assert actions.calls == []


@pytest.mark.parametrize(
    "change",
    [
        "token",
        "agent_process",
        "adbd",
        "transport",
        "permissions",
        "forward",
        "lifecycle",
        "pending",
        "recovery",
    ],
)
@pytest.mark.parametrize("kind", ["node_click", "key"])
async def test_preflight_changes_block_both_native_and_host_actions(
    action_rig, change, kind
) -> None:
    backend, manager, _, actions = action_rig
    view = await source(manager)
    snapshot = backend.snapshots[SERIAL_A]
    if change == "token":
        snapshot.credential = "c" * 64
    elif change == "agent_process":
        snapshot.agent_process += 1
    elif change == "adbd":
        snapshot.adbd = replace(snapshot.adbd, pid=999)
    elif change == "transport":
        snapshot.transport_id = "new"
    elif change == "permissions":
        snapshot.settings = replace(
            ACTIVE_SETTINGS, services="org.example.reader/.AccessibilityService"
        )
    elif change == "forward":
        backend.forwards = {row for row in backend.forwards if row[0] != SERIAL_A}
    elif change == "lifecycle":
        snapshot.lifecycle = str(uuid4())
    else:
        snapshot.capabilities[
            "platform_operation_pending" if change == "pending" else "recovery_required"
        ] = True
    result = await manager.action(SERIAL_A, body(view, kind))
    assert result["status"] == "rejected" and result["action_completed"] is False
    assert actions.calls == [] and backend.commands == []


@pytest.mark.parametrize("change", ["page", "rotation", "scale"])
async def test_pre_dispatch_reobservation_rejects_page_rotation_and_scale_change(
    action_rig, change
) -> None:
    backend, manager, _, actions = action_rig
    view = await source(manager)
    if change == "page":
        actions.states[SERIAL_A] = True
    elif change == "rotation":
        backend.geometry["rotation"] = 90
    else:
        backend.geometry["width"] = 3
    result = await manager.action(SERIAL_A, body(view, "tap"))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == (
        "page_changed" if change == "page" else "display_geometry_changed"
    )
    assert actions.calls == []


@pytest.mark.parametrize("kind", ["launch", "stop", "key"])
async def test_a_page_that_keeps_changing_does_not_fail_a_package_scoped_action(
    action_rig, kind
) -> None:
    """A live launcher mutates continuously; launch/stop/key read no node content."""
    _, manager, _, actions = action_rig
    view = await source(manager)
    actions.states[SERIAL_A] = True
    result = await manager.action(SERIAL_A, body(view, kind))
    assert result["status"] == "completed"
    assert result["action_completed"] is True


async def test_a_page_that_keeps_changing_still_fails_an_interactive_action(action_rig) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    actions.states[SERIAL_A] = True
    result = await manager.action(SERIAL_A, body(view, "tap"))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "page_changed"


@pytest.mark.parametrize("committed", [False, True])
async def test_exact_owned_cancel_cannot_cancel_sibling_serial_or_promise_rollback(
    action_rig, committed
) -> None:
    _, manager, _, actions = action_rig
    first, second = await asyncio.gather(source(manager, SERIAL_A), source(manager, SERIAL_B))
    request = body(first)
    actions.before_send = not committed
    actions.gate = Gate()
    pending = asyncio.create_task(manager.action(SERIAL_A, request))
    await actions.gate.entered.wait()
    wrong = await manager.cancel_action(
        SERIAL_B, second["session_id"], request.request_id, request.operation_id
    )
    assert wrong["cancelled"] is False and not pending.done()
    assert (
        await manager.cancel_action(SERIAL_A, first["session_id"], request.request_id, str(uuid4()))
    )["cancelled"] is False
    cancelled = await manager.cancel_action(
        SERIAL_A, first["session_id"], request.request_id, request.operation_id
    )
    result = await pending
    assert cancelled == {"cancelled": True, "cleanup_complete": True, "rollback_guaranteed": False}
    assert result["status"] == ("uncertain" if committed else "rejected")
    assert result["recovery_required"] is committed
    actions.gate = None
    actions.before_send = False
    sibling = await manager.action(SERIAL_B, body(second))
    assert sibling["status"] == "completed"
    assert all(
        token == (TOKEN_A if serial == SERIAL_A else TOKEN_B) for serial, token, _ in actions.calls
    )


async def test_late_terminal_after_generation_invalidation_cannot_enter_new_session_or_replay(
    action_rig,
) -> None:
    backend, manager, _, actions = action_rig
    view = await source(manager)
    actions.gate, actions.swallow_cancel = Gate(), True
    pending = asyncio.create_task(manager.action(SERIAL_A, body(view)))
    await actions.gate.entered.wait()
    backend.snapshots[SERIAL_A].credential = "c" * 64
    await manager.list_devices()
    actions.gate.release.set()
    result = await pending
    assert result["status"] == "uncertain" and result["action_completed"] is False
    assert result["post_observation"] is None and result["session_id"] == view["session_id"]
    assert manager.slot(SERIAL_A).session is None
    assert (await manager.connect(SERIAL_A))["ready"] is False and len(actions.calls) == 1


@pytest.mark.parametrize("kind", ["key", "launch", "stop", "clear"])
async def test_cancel_issued_adb_command_is_bounded_uncertain_and_never_reissued(
    action_rig, kind
) -> None:
    backend, manager, _, _ = action_rig
    view = await source(manager)
    request = body(view, kind)
    backend.command_gate = Gate()
    pending = asyncio.create_task(manager.action(SERIAL_A, request))
    await backend.command_gate.entered.wait()
    cancellation = asyncio.create_task(
        manager.cancel_action(
            SERIAL_A, view["session_id"], request.request_id, request.operation_id
        )
    )
    await asyncio.sleep(0)
    backend.command_gate.release.set()
    assert (await cancellation)["rollback_guaranteed"] is False
    result = await pending
    assert result["status"] == "uncertain" and len(backend.commands) == 1
    assert result["recovery_required"] is True and result["post_observation"] is None
    assert (await manager.connect(SERIAL_A))["ready"] is False


@pytest.mark.parametrize("kind", ["key", "launch", "stop", "clear"])
async def test_adb_timeout_cannot_be_retried_as_an_agent_action(action_rig, kind) -> None:
    backend, manager, _, actions = action_rig
    view = await source(manager)
    backend.command_fault = "adb_command_failed"
    result = await manager.action(SERIAL_A, body(view, kind))
    assert result["status"] == "uncertain" and result["recovery_required"] is True
    assert len(backend.commands) == 1 and actions.calls == []


async def test_action_lease_blocks_other_write_and_external_observe_or_cancel_operation(
    action_rig,
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    request = body(view)
    actions.gate = Gate()
    pending = asyncio.create_task(manager.action(SERIAL_A, request))
    await actions.gate.entered.wait()
    assert (await manager.action(SERIAL_A, body(view)))["status"] == "rejected"
    with pytest.raises(ConnectionFault, match="action_in_progress"):
        await manager.observe(SERIAL_A, view["session_id"], str(uuid4()))
    assert (await manager.cancel(SERIAL_A, request.operation_id)) is False
    actions.gate.release.set()
    assert (await pending)["status"] == "completed" and len(actions.calls) == 1


async def test_unknown_exception_is_closed_and_input_credentials_and_source_are_not_logged(
    action_rig, caplog
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    actions.mode = "raw_error"
    with caplog.at_level(logging.INFO):
        result = await manager.action(SERIAL_A, body(view, "set_text"))
    assert result["status"] == "uncertain" and result["error"]["code"] == "action_failed"
    text = caplog.text + json.dumps(result) + repr(manager.slot(SERIAL_A))
    assert all(
        secret not in text
        for secret in (TOKEN_A, "private-input", "private-platform-exception", "synthetic-tree-A")
    )
    assert "private-input" not in repr(body(view, "set_text"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", True),
        ("serial", False),
        ("session_id", str(uuid4()).upper()),
        ("request_id", "bad"),
        ("operation_id", 1),
        ("kind", "shell"),
        ("params", []),
        ("timeout_ms", True),
        ("timeout_ms", 999),
        ("timeout_ms", 90001),
        ("agent_timeout_ms", 10001),
    ],
)
def test_strict_action_top_level_inputs(field, value) -> None:
    view = {"serial": SERIAL_A, "session_id": str(uuid4()), "request_id": str(uuid4())}
    value_dict = body(view).model_dump()
    value_dict[field] = value
    with pytest.raises((ValidationError, ConnectionFault)):
        ActionBody.model_validate(value_dict)


@pytest.mark.parametrize(
    "kind,params",
    [
        ("tap", {"x": True, "y": 1}),
        ("tap", {"x": -1, "y": 1}),
        ("tap", {"x": 8192, "y": 1}),
        ("tap", {"x": 1.5, "y": 1}),
        ("long", {"x": 1, "y": 1, "duration_ms": 499}),
        ("swipe", {"x": 1, "y": 1, "end_x": 2, "end_y": 2, "duration_ms": 1001}),
        ("set_text", {"text": False}),
        ("set_text", {"text": "x" * 4097}),
        ("set_text", {"text": "\ud800"}),
        ("set_text", {"text": "a", "mode": "append"}),
        ("node_click", {"text": "a"}),
        ("key", {"code": True}),
        ("key", {"code": 26}),
        ("key", {"code": "4;reboot"}),
        ("launch", {"package": "com.pulse.target;reboot", "activity": ".FormActivity"}),
        ("launch", {"package": "com.pulse.target", "activity": ".FormActivity\nreboot"}),
        ("clear", {"package": "com.pulse.target"}),
        ("clear", {"package": "com.pulse.target", "confirmed": 1}),
        ("stop", {"package": "com.pulse.target", "command": "reboot"}),
    ],
)
def test_strict_action_params_reject_unknown_type_range_and_shell_injection(kind, params) -> None:
    view = {"serial": SERIAL_A, "session_id": str(uuid4()), "request_id": str(uuid4())}
    with pytest.raises(ValidationError):
        body(view, kind, params=params)


@pytest.mark.parametrize("kind", ["launch", "stop", "clear"])
async def test_protected_agent_target_is_never_stopped_cleared_or_launched(
    action_rig, kind
) -> None:
    backend, manager, _, _ = action_rig
    view = await source(manager)
    request = body(view, kind)
    request.params["package"] = "com.pulse.agent"
    result = await manager.action(SERIAL_A, request)
    assert result["status"] == "rejected" and backend.commands == []


def test_geometry_only_accepts_known_active_default_internal_viewports() -> None:
    native = (
        "Viewport INTERNAL: displayId=0, orientation=1, logicalFrame=[0, 0, 3, 4], isActive=[1]"
    )
    wrapped = (
        "DisplayViewport{type=INTERNAL, valid=true, isActive=true, displayId=0, "
        "orientation=1, logicalFrame=[0, 0, 3, 4]}"
    )
    assert parse_action_geometry(native + "\n" + wrapped) == {
        "display_id": 0,
        "width": 3,
        "height": 4,
        "rotation": 90,
    }


@pytest.mark.parametrize(
    "value",
    [
        "",
        "wm size 1080x2400",
        "Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 3, 4], isActive=[0]",
        "Viewport EXTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 3, 4], isActive=[1]",
        "Viewport INTERNAL: displayId=1, orientation=0, logicalFrame=[0, 0, 3, 4], isActive=[1]",
        "Viewport INTERNAL: displayId=0, orientation=9, logicalFrame=[0, 0, 3, 4], isActive=[1]",
        "Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[1, 0, 3, 4], isActive=[1]",
        "Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 8193, 4], isActive=[1]",
    ],
)
def test_unknown_geometry_or_sleep_is_not_guessed(value) -> None:
    with pytest.raises(ConnectionFault, match="display_geometry_unavailable"):
        parse_action_geometry(value)


async def test_action_api_uses_strict_json_and_owned_cancel_ids(action_rig) -> None:
    _, manager, _, _ = action_rig
    view = await source(manager)
    token = "runtime-token"
    app = create_app(token, device_manager=manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        response = await client.post(
            f"/devices/{SERIAL_A}/agent/action", json=body(view).model_dump()
        )
        assert response.status_code == 200 and response.json()["status"] == "completed"
        raw = json.dumps(body(view).model_dump())
        for value in (
            raw[:-1] + ',"kind":"node_click"}',
            raw.replace('"params": {}', '"params": {"x": NaN}'),
            raw.replace('"params": {}', '"params": {"text": Infinity}'),
        ):
            response = await client.post(
                f"/devices/{SERIAL_A}/agent/action",
                content=value,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 422 and response.json() == {
                "status": "rejected",
                "error": "invalid_request",
            }
        response = await client.post(
            f"/devices/{SERIAL_A}/agent/action",
            content=b"x" * 65537,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        response = await client.post(
            f"/devices/{SERIAL_A}/agent/action/cancel",
            params={
                "session_id": view["session_id"],
                "request_id": str(uuid4()),
                "operation_id": str(uuid4()),
            },
        )
        assert response.status_code == 200 and response.json()["cancelled"] is False
        response = await client.post(
            f"/devices/{SERIAL_A}/agent/action/cancel",
            params={
                "session_id": view["session_id"],
                "request_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "extra": "no",
            },
        )
        assert response.status_code == 400


async def test_direct_adb_adapter_rejects_unbounded_codes_and_package_injection(action_rig) -> None:
    backend, manager, _, _ = action_rig
    await source(manager)
    session = manager.slot(SERIAL_A).session
    for params in (
        {"kind": "key", "code": "4;reboot"},
        {"kind": "clear", "package": "com.pulse.agent", "confirmed": True},
        {"kind": "launch", "package": "com.pulse.target", "activity": ".FormActivity;reboot"},
    ):
        exchange = AdbActionExchange(str(uuid4()), backend)
        with pytest.raises(ConnectionFault):
            await exchange.run(session.forward, session.generation.token, params, 1000)
        assert exchange.sent is False
    assert backend.commands == []


@pytest.mark.parametrize("kind", ["key", "launch", "stop", "clear"])
async def test_package_scoped_actions_do_not_rely_on_node_states(action_rig, kind) -> None:
    _, manager, _, _ = action_rig
    view = await source(manager)
    binding = manager.slot(SERIAL_A).session.latest_observation
    binding.nodes[()]["states"].update(enabled=None, visible=None, clickable=None, editable=None)
    request = body(view, kind)
    if kind in {"stop", "clear"}:
        request.params["package"] = "com.pulse.target"
    result = await manager.action(SERIAL_A, request)
    assert result["status"] == "completed" and result["action_completed"] is True


def test_stale_inactive_viewport_does_not_mask_the_active_default_display() -> None:
    stale = (
        "Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 99, 99], isActive=[0]"
    )
    active = (
        "Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 3, 4], isActive=[1]"
    )
    assert parse_action_geometry(stale + "\n" + active) == {
        "display_id": 0,
        "width": 3,
        "height": 4,
        "rotation": 0,
    }


async def test_fingerprint_ignores_event_revision_but_detects_tree_content_change(
    action_rig,
) -> None:
    _, manager, observations, actions = action_rig
    view = await source(manager)
    slot = manager.slot(SERIAL_A)
    first = slot.session.latest_observation.fingerprint
    assert first is not None

    def revision_only(value):
        actions.source(SERIAL_A, value)
        value.update(revision_start=9, revision_end=9)

    observations.modify_result[SERIAL_A] = revision_only
    await manager.observe(SERIAL_A, view["session_id"], str(uuid4()), for_action=True)
    assert slot.session.latest_observation.fingerprint == first

    def change_node(value):
        actions.source(SERIAL_A, value)
        value["sources"]["tree"]["data"]["nodes"][0]["states"]["checked"] = True
        value["revision_start"] = value["revision_end"] = 11

    observations.modify_result[SERIAL_A] = change_node
    await manager.observe(SERIAL_A, view["session_id"], str(uuid4()), for_action=True)
    assert slot.session.latest_observation.fingerprint != first


async def test_observation_that_does_not_publish_its_binding_cannot_dispatch(
    action_rig, monkeypatch
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    real = manager.observe

    async def stale_observe(*args, **kwargs):
        result = await real(*args, **kwargs)
        session = manager.slot(SERIAL_A).session
        session.latest_observation = replace(session.latest_observation, request_id=str(uuid4()))
        return result

    monkeypatch.setattr(manager, "observe", stale_observe)
    result = await manager.action(SERIAL_A, body(view))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "action_source_stale"
    assert result["error"]["phase"] in {"checking", "pre_observation", "pre_dispatch"}
    assert actions.calls == []


async def test_staleness_rejection_reports_the_measured_binding_age(
    action_rig, monkeypatch, caplog
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    real = manager.observe

    async def aged_observe(*args, **kwargs):
        result = await real(*args, **kwargs)
        session = manager.slot(SERIAL_A).session
        session.latest_observation = replace(
            session.latest_observation, captured_monotonic=time.monotonic() - 31
        )
        return result

    monkeypatch.setattr(manager, "observe", aged_observe)
    with caplog.at_level(logging.INFO):
        result = await manager.action(SERIAL_A, body(view))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "action_source_stale"
    assert result["error"]["phase"] == "pre_dispatch"
    receipt_age = result["error"]["binding_age_ms"]
    assert type(receipt_age) is int and receipt_age >= 30_000
    finished = [record for record in caplog.records if record.event == "device.action_finished"]
    assert len(finished) == 1
    assert finished[0].binding_age_ms == receipt_age
    assert actions.calls == []


async def test_dispatch_window_covers_measured_device_collection_latency() -> None:
    """Guards the real-device regression: collection latency longer than the window.

    On the vivo V2364A every Accessibility tree took 6.5-11.5s while the dispatch window
    was 10s, so actions were rejected for age by construction. The window must stay above
    the observed capture latency, and must not be widened to the point of accepting a
    binding that is genuinely stale.
    """
    measured_worst_case_seconds = 11.5
    assert measured_worst_case_seconds < DISPATCH_MAX_AGE
    assert DISPATCH_MAX_AGE < SOURCE_MAX_AGE


async def test_tree_older_than_the_dispatch_window_is_still_rejected(
    action_rig, monkeypatch
) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    real = manager.observe

    async def stale_after_collection(*args, **kwargs):
        result = await real(*args, **kwargs)
        session = manager.slot(SERIAL_A).session
        session.latest_observation = replace(
            session.latest_observation,
            captured_monotonic=time.monotonic() - DISPATCH_MAX_AGE - 1,
        )
        return result

    monkeypatch.setattr(manager, "observe", stale_after_collection)
    result = await manager.action(SERIAL_A, body(view))
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "action_source_stale"
    assert actions.calls == []


async def test_control_gate_epoch_ledger_and_manual_takeover_api(action_rig) -> None:
    _, manager, _, actions = action_rig
    view = await source(manager)
    token = "runtime-token"
    run_a, run_b = str(uuid4()), str(uuid4())
    app = create_app(token, device_manager=manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        acquired = await client.post(
            f"/devices/{SERIAL_A}/agent/control/acquire",
            json={"session_id": view["session_id"], "source": "auto", "run_id": run_a},
        )
        assert acquired.status_code == 200
        assert acquired.json()["control"]["epoch"] == 0
        assert acquired.json()["control"]["holder_source"] == "auto"

        first = await client.post(
            f"/devices/{SERIAL_A}/agent/action",
            json=body(view, source="auto", control_epoch=0, run_id=run_a).model_dump(),
        )
        assert first.status_code == 200 and first.json()["status"] == "completed"
        assert first.json()["ledger"]["state"] == "verified"
        assert len(actions.calls) == 1

        takeover = await client.post(
            f"/devices/{SERIAL_A}/agent/control/takeover",
            json={
                "session_id": view["session_id"],
                "source": "manual",
                "run_id": run_b,
                "timeout_ms": 1000,
            },
        )
        assert takeover.status_code == 200
        assert takeover.json()["status"] == "stopped"
        assert takeover.json()["control"]["epoch"] == 1
        assert takeover.json()["previous_run_id"] == run_a
        assert takeover.json()["previous_run_state"] == "canceled"
        assert takeover.json()["stopped_confirmed"] is True

        stale = await client.post(
            f"/devices/{SERIAL_A}/agent/action",
            json=body(view, source="auto", control_epoch=0, run_id=run_a).model_dump(),
        )
        assert stale.status_code == 200
        assert stale.json()["status"] == "rejected"
        assert stale.json()["error"]["code"] == "stale_control_epoch"
        assert len(actions.calls) == 1

        manual_view = await source(manager)
        manual = await client.post(
            f"/devices/{SERIAL_A}/agent/action",
            json=body(manual_view, source="manual", control_epoch=1, run_id=run_b).model_dump(),
        )
        assert manual.status_code == 200 and manual.json()["status"] == "completed"
        assert manual.json()["ledger"]["state"] == "verified"

        status = await client.get(
            f"/devices/{SERIAL_A}/agent/control",
            params={"session_id": view["session_id"]},
        )
        assert status.status_code == 200
        assert status.json()["control"]["epoch"] == 1
        assert status.json()["control"]["holder_source"] == "manual"

        ledger = await client.get(f"/devices/{SERIAL_A}/agent/ledger")
        assert ledger.status_code == 200
        records = ledger.json()["actions"]
        assert len(records) == 2
        assert {record["state"] for record in records} == {"verified"}

        released = await client.post(
            f"/devices/{SERIAL_A}/agent/control/release",
            json={
                "session_id": view["session_id"],
                "control_epoch": 1,
                "run_id": run_b,
            },
        )
        assert released.status_code == 200
        assert released.json()["control"]["holder_source"] is None
