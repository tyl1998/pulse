"""Fake-only FE-P2.6 pose and window-control cleanup; never access a physical Runtime."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx2 as httpx
import pytest

from pulse_runtime.api.app import create_app
from pulse_runtime.drivers.android import devices
from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from runtime.tests.test_actions import action_rig as action_rig
from runtime.tests.test_actions import body, source
from runtime.tests.test_device_connections import SERIAL_A, Gate, session_id
from runtime.tests.test_device_connections import forbid_external_io as forbid_external_io


async def claim(manager):
    identifier = session_id(await manager.connect(SERIAL_A))
    run_id = str(uuid4())
    result = await manager.acquire_control(
        SERIAL_A, session_id=identifier, source="manual", run_id=run_id
    )
    return identifier, run_id, result["control"]["epoch"]


def release_entered(monkeypatch, manager):
    entered = asyncio.Event()
    snapshot = manager.gate.snapshot

    def observed(serial):
        result = snapshot(serial)
        entered.set()
        return result

    monkeypatch.setattr(manager.gate, "snapshot", observed)
    return entered


async def test_pose_is_read_only_preserves_binding_and_observation_budget(action_rig):
    backend, manager, observations, _actions = action_rig
    view = await source(manager)
    slot = manager.slot(SERIAL_A)
    ids = set(slot.session.observation_ids)
    binding = slot.session.latest_observation
    mutations = list(backend.mutations)
    collections = len(observations.calls)
    for _ in range(3):
        pose = await manager.geometry(SERIAL_A, view["session_id"])
        assert pose == {
            "serial": SERIAL_A,
            "session_id": view["session_id"],
            "status": "available",
            "source": "adb_input_viewport",
            "geometry": backend.geometry,
            "atomic_with_observation": False,
        }
    assert slot.session.observation_ids == ids
    assert slot.session.latest_observation is binding
    assert backend.mutations == mutations
    assert len(observations.calls) == collections
    assert manager.gate.snapshot(SERIAL_A).holder_source is None


@pytest.mark.parametrize(
    "field,value",
    [("width", 0), ("height", 8193), ("rotation", 45), ("rotation", False), ("display_id", False)],
)
async def test_pose_rejects_unknown_or_invalid_geometry(action_rig, field, value):
    backend, manager, _observations, _actions = action_rig
    identifier = session_id(await manager.connect(SERIAL_A))
    backend.geometry[field] = value
    with pytest.raises(ConnectionFault, match="display_geometry_unavailable"):
        await manager.geometry(SERIAL_A, identifier)


async def test_pose_rejects_rotation_during_read_and_generation_changes(action_rig, monkeypatch):
    backend, manager, _observations, _actions = action_rig
    identifier = session_id(await manager.connect(SERIAL_A))
    original = backend.action_geometry
    reads = 0

    async def rotating(serial):
        nonlocal reads
        reads += 1
        result = await original(serial)
        if reads == 2:
            result["rotation"] = 180
        return result

    monkeypatch.setattr(backend, "action_geometry", rotating)
    with pytest.raises(ConnectionFault, match="display_geometry_changed"):
        await manager.geometry(SERIAL_A, identifier)

    async def restarted(serial):
        result = await original(serial)
        backend.snapshots[SERIAL_A].agent_process += 1
        return result

    monkeypatch.setattr(backend, "action_geometry", restarted)
    with pytest.raises(ConnectionFault):
        await manager.geometry(SERIAL_A, identifier)


async def test_pose_deadline_does_not_recover_or_replace_agent(action_rig, monkeypatch):
    backend, manager, _observations, _actions = action_rig
    identifier = session_id(await manager.connect(SERIAL_A))
    mutations = list(backend.mutations)
    gate = Gate()
    backend.gates[(SERIAL_A, "action_geometry")] = gate
    original = asyncio.timeout
    with monkeypatch.context() as patch:
        patch.setattr(devices.asyncio, "timeout", lambda delay: original(min(delay, 0.02)))
        with pytest.raises(ConnectionFault, match="display_geometry_unavailable"):
            await manager.geometry(SERIAL_A, identifier)
    assert backend.mutations == mutations
    assert manager.slot(SERIAL_A).session.id == identifier


async def test_cleanup_waits_for_observation_without_cancel_or_lock_spin(action_rig, monkeypatch):
    _backend, manager, observations, _actions = action_rig
    identifier, run_id, epoch = await claim(manager)
    gate = Gate()
    observations.gate[SERIAL_A] = gate
    observation = asyncio.create_task(
        manager.observe(SERIAL_A, identifier, str(uuid4()), for_action=True)
    )
    await asyncio.wait_for(gate.entered.wait(), 1)
    with pytest.raises(ConnectionFault, match="observation_in_progress"):
        await manager.release_control(
            SERIAL_A, session_id=identifier, control_epoch=epoch, run_id=run_id
        )
    entered = release_entered(monkeypatch, manager)
    release = asyncio.create_task(
        manager.release_control(
            SERIAL_A,
            session_id=identifier,
            control_epoch=epoch,
            run_id=run_id,
            wait_for_idle_ms=5000,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert not release.done()
    assert not gate.cancelled.is_set()
    gate.release.set()
    await asyncio.wait_for(observation, 1)
    result = await asyncio.wait_for(release, 1)
    assert result["control"]["holder_source"] is None
    assert manager.ledger.run_state(run_id) == "finished"


async def test_cleanup_waits_for_accepted_action_and_never_cancels_it(action_rig, monkeypatch):
    _backend, manager, _observations, actions = action_rig
    identifier, run_id, epoch = await claim(manager)
    view = await source(manager)
    gate = actions.gate = Gate()
    action = asyncio.create_task(
        manager.action(
            SERIAL_A, body(view, "tap", source="manual", run_id=run_id, control_epoch=epoch)
        )
    )
    await asyncio.wait_for(gate.entered.wait(), 1)
    with pytest.raises(ConnectionFault, match="action_in_progress"):
        await manager.geometry(SERIAL_A, identifier)
    entered = release_entered(monkeypatch, manager)
    release = asyncio.create_task(
        manager.release_control(
            SERIAL_A,
            session_id=identifier,
            control_epoch=epoch,
            run_id=run_id,
            wait_for_idle_ms=5000,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert not release.done()
    assert not gate.cancelled.is_set()
    gate.release.set()
    result = await asyncio.wait_for(action, 1)
    assert result["action_completed"] is True
    await asyncio.wait_for(release, 1)
    assert manager.ledger.run_state(run_id) == "finished"


async def test_foreign_epoch_or_anonymous_holder_cannot_be_released(action_rig, monkeypatch):
    _backend, manager, observations, _actions = action_rig
    identifier, run_id, epoch = await claim(manager)
    gate = observations.gate[SERIAL_A] = Gate()
    observation = asyncio.create_task(manager.observe(SERIAL_A, identifier, str(uuid4())))
    await asyncio.wait_for(gate.entered.wait(), 1)
    entered = release_entered(monkeypatch, manager)
    release = asyncio.create_task(
        manager.release_control(
            SERIAL_A,
            session_id=identifier,
            control_epoch=epoch,
            run_id=run_id,
            wait_for_idle_ms=5000,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    foreign = str(uuid4())
    current, _previous, _epoch = manager.gate.take_over(
        SERIAL_A, session_id=identifier, source="manual", run_id=foreign
    )
    gate.release.set()
    await asyncio.wait_for(observation, 1)
    with pytest.raises(ConnectionFault, match="stale_control_epoch"):
        await asyncio.wait_for(release, 1)
    assert manager.gate.snapshot(SERIAL_A).run_id == foreign
    await manager.release_control(
        SERIAL_A, session_id=identifier, control_epoch=current.epoch, run_id=foreign
    )
    await manager.acquire_control(SERIAL_A, session_id=identifier, source="manual", run_id=None)
    with pytest.raises(ConnectionFault, match="run_not_active"):
        await manager.release_control(
            SERIAL_A,
            session_id=identifier,
            control_epoch=current.epoch,
            run_id=run_id,
            wait_for_idle_ms=1000,
        )
    assert manager.gate.snapshot(SERIAL_A).holder_source == "manual"


async def test_cleanup_timeout_leaves_native_work_and_control_intact(action_rig):
    _backend, manager, observations, _actions = action_rig
    identifier, run_id, epoch = await claim(manager)
    gate = observations.gate[SERIAL_A] = Gate()
    observation = asyncio.create_task(manager.observe(SERIAL_A, identifier, str(uuid4())))
    await asyncio.wait_for(gate.entered.wait(), 1)
    with pytest.raises(ConnectionFault, match="control_release_timeout"):
        await manager.release_control(
            SERIAL_A, session_id=identifier, control_epoch=epoch, run_id=run_id, wait_for_idle_ms=20
        )
    assert not gate.cancelled.is_set()
    assert not observation.done()
    assert manager.gate.snapshot(SERIAL_A).run_id == run_id
    gate.release.set()
    await asyncio.wait_for(observation, 1)


async def test_closing_pending_claim_reserves_terminal_nonce_not_control(action_rig):
    _backend, manager, _observations, _actions = action_rig
    identifier = session_id(await manager.connect(SERIAL_A))
    run_id = str(uuid4())
    epoch = manager.gate.snapshot(SERIAL_A).epoch
    result = await manager.release_control(
        SERIAL_A, session_id=identifier, control_epoch=epoch, run_id=run_id, wait_for_idle_ms=1000
    )
    assert result["control"]["holder_source"] is None
    assert manager.ledger.run_state(run_id) == "canceled"
    with pytest.raises(ConnectionFault, match="run_not_active"):
        await manager.acquire_control(
            SERIAL_A, session_id=identifier, source="manual", run_id=run_id
        )


async def test_api_pose_and_cleanup_are_documented_strict_loopback_contracts(action_rig):
    _backend, manager, _observations, _actions = action_rig
    identifier, run_id, epoch = await claim(manager)
    app = create_app("test-token", device_manager=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://runtime",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        response = await client.get(
            f"/devices/{SERIAL_A}/agent/geometry", params={"session_id": identifier}
        )
        assert response.status_code == 200
        assert response.json()["atomic_with_observation"] is False
        body = {
            "session_id": identifier,
            "control_epoch": epoch,
            "run_id": run_id,
            "wait_for_idle_ms": True,
        }
        response = await client.post(f"/devices/{SERIAL_A}/agent/control/release", json=body)
        assert response.status_code == 422
        body["wait_for_idle_ms"] = 1000
        response = await client.post(f"/devices/{SERIAL_A}/agent/control/release", json=body)
        assert response.status_code == 200
        assert response.json()["control"]["holder_source"] is None
    spec = app.openapi()
    assert (
        spec["paths"]["/devices/{serial}/agent/geometry"]["get"]["operationId"]
        == "getAgentGeometry"
    )
    assert (
        spec["components"]["schemas"]["ControlReleaseBody"]["properties"]["wait_for_idle_ms"][
            "default"
        ]
        == 0
    )
