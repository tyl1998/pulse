"""P1.5 collection/API/action provenance integration; all Android backends are fakes."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
from uuid import uuid4

import httpx2 as httpx
import pytest
from jsonschema import Draft202012Validator

from pulse_runtime.api.app import create_app
from pulse_runtime.api.contracts import LocateRequest, LocateResult
from pulse_runtime.drivers.android import devices
from pulse_runtime.drivers.android.agent_transport import ConnectionFault
from pulse_runtime.perception.locators import LocateBody
from pulse_schema import models as m
from runtime.tests.test_actions import action_rig as action_rig
from runtime.tests.test_actions import body as action_body
from runtime.tests.test_device_connections import SERIAL_A, SERIAL_B, TOKEN_A, Gate, session_id
from runtime.tests.test_device_connections import forbid_external_io as forbid_external_io
from runtime.tests.test_locators import PRIVATE, candidate, native_tree, request
from runtime.tests.test_observations import png

TOKEN = "locator-runtime-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def locate_request(manager, serial=SERIAL_A, **extra):
    slot = manager.slot(serial)
    identifier = slot.session.id if slot.session else session_id(await manager.connect(serial))
    return request(serial=serial, session_id=identifier, **extra)


def contract_validator(spec, name):
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
    )


async def test_locate_openapi_declares_the_existing_bounded_request_and_public_result(action_rig):
    backend, manager, observations, actions = action_rig
    app = create_app(TOKEN, device_manager=manager)
    spec = app.openapi()
    operation = spec["paths"]["/devices/{serial}/agent/locate"]["post"]
    schemas = spec["components"]["schemas"]
    assert operation["operationId"] == "locate"
    assert operation["requestBody"] == {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LocateRequest"}}},
    }
    for status, name in [
        (200, "LocateResult"),
        (400, "LocateHttpError"),
        (401, "LocateHttpError"),
        (409, "LocateHttpError"),
        (413, "LocateRejected"),
        (422, "LocateRejected"),
    ]:
        assert operation["responses"][str(status)]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{name}"
        }
    original = LocateBody.model_json_schema(ref_template="#/components/schemas/{model}")
    assert schemas["LocateRequest"]["properties"] == original["properties"]
    assert schemas["LocateRequest"]["required"] == original["required"]
    assert schemas["LocateRequest"]["additionalProperties"] is False
    assert set(schemas["LocateResult"]["required"]) == set(LocateResult.model_fields)
    assert schemas["LocateResult"]["properties"]["status"]["enum"] == [
        "resolved",
        "ambiguous",
        "not_found",
        "indeterminate",
    ]
    assert "text" not in schemas["LocateMatch"]["properties"]
    assert "data" not in schemas["LocateObservationSource"]["properties"]

    pending = [spec]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/components/schemas/")
                assert value["$ref"].removeprefix("#/components/schemas/") in schemas
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert app.openapi() is spec
    assert manager.slots == {} and backend.calls == []
    assert observations.calls == [] and actions.calls == []


@pytest.mark.parametrize(
    ("case", "status", "match_count"),
    [
        ("resolved", "resolved", 1),
        ("fallback", "resolved", 1),
        ("ambiguous", "ambiguous", 2),
        ("not_found", "not_found", 0),
        ("unavailable", "indeterminate", 0),
        ("unsupported_display", "indeterminate", 0),
        ("nonunique", "resolved", 2),
        ("incomplete_nonunique", "resolved", 2),
    ],
)
async def test_locate_http_result_round_trips_without_losing_evidence(
    action_rig, case, status, match_count
):
    backend, manager, observations, actions = action_rig
    body = await locate_request(manager)
    wire = body.model_dump()
    locator = wire["target"]["locator"]
    if case in {"fallback", "not_found"}:
        locator["candidates"] = [candidate("text", "Missing")]
        if case == "fallback":
            locator["candidates"].append(candidate())
    if case in {"nonunique", "incomplete_nonunique"}:
        locator["expect"]["unique"] = False
    if case == "unsupported_display":
        wire["display_id"] = 1
    original = observations.modify_result[SERIAL_A]

    def page(value):
        original(value)
        tree = value["sources"]["tree"]
        if case in {"ambiguous", "nonunique", "incomplete_nonunique"}:
            child = copy.deepcopy(tree["data"]["nodes"][0])
            child["node_path"] = [0]
            tree["data"]["nodes"].append(child)
        if case == "incomplete_nonunique":
            tree["completeness"]["reasons"].append("node_limit")
        if case == "unavailable":
            tree.update(
                status="unavailable",
                data=None,
                error={"code": "window_unavailable", "phase": "observe", "retryable": False},
            )

    observations.modify_result[SERIAL_A] = page
    request_model = LocateRequest.model_validate(wire)
    assert request_model.model_dump() == LocateBody.model_validate(wire).model_dump()
    app = create_app(TOKEN, device_manager=manager)
    spec = app.openapi()
    contract_validator(spec, "LocateRequest").validate(wire)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        response = await client.post(f"/devices/{SERIAL_A}/agent/locate", json=wire)
    assert response.status_code == 200
    payload = response.json()
    result = LocateResult.model_validate(payload)
    assert result.model_dump(mode="json", exclude_unset=True) == payload
    contract_validator(spec, "LocateResult").validate(payload)
    assert result.status == status and result.match_count == match_count
    assert result.business_success is None and result.automatic_replay is False
    assert all("text" not in match for match in payload["matches"])
    assert all("data" not in source for source in payload["observation"]["sources"].values())
    if case == "fallback":
        assert result.candidate_index == 1
        assert [attempt.status for attempt in result.attempts] == ["not_found", "resolved"]
    if status == "resolved":
        assert result.query_complete is (case != "incomplete_nonunique")
        assert result.matches[0].states.enabled is True
        assert result.matches[0].states.password is None
    else:
        assert result.query_complete is None and result.action_target is None
    assert len(observations.calls) == (0 if case == "unsupported_display" else 1)
    assert backend.commands == [] and actions.calls == []


@pytest.mark.parametrize("by", ["image", "ocr_text", "ocr_failure"])
async def test_locate_http_visual_contract_keeps_bitmap_and_dependency_evidence(action_rig, by):
    backend, manager, observations, actions = action_rig
    base = await locate_request(manager)
    body = request(
        candidate("image", "marker")
        if by == "image"
        else candidate("ocr_text", f"CONTROLLED_OCR_{SERIAL_A}"),
        serial=SERIAL_A,
        session_id=base.session_id,
        resources={"marker": {"png_base64": png()}} if by == "image" else {},
    )
    if by == "ocr_failure":

        def failed_capture(value):
            for source, code in [
                ("screenshot", "platform_failed"),
                ("ocr", "screenshot_unavailable"),
            ]:
                value["sources"][source].update(
                    status="failed",
                    data=None,
                    error={"code": code, "phase": "observe", "retryable": False},
                )

        observations.modify_result[SERIAL_A] = failed_capture
    app = create_app(TOKEN, device_manager=manager)
    spec = app.openapi()
    contract_validator(spec, "LocateRequest").validate(body.model_dump())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        response = await client.post(f"/devices/{SERIAL_A}/agent/locate", json=body.model_dump())
    assert response.status_code == 200
    payload = response.json()
    result = LocateResult.model_validate(payload)
    assert result.model_dump(mode="json", exclude_unset=True) == payload
    contract_validator(spec, "LocateResult").validate(payload)
    assert result.action_target is None
    if by == "ocr_failure":
        assert result.status == "indeterminate"
        assert result.observation.sources.ocr.error is not None
        assert result.observation.sources.ocr.error.cause_code == "platform_failed"
    else:
        assert result.status == "resolved"
        assert result.action_target_error is not None
        assert result.action_target_error.code == "coordinate_mapping_unavailable"
        assert result.matches[0].source == ("screenshot" if by == "image" else "ocr")
        assert result.matches[0].coordinate_space == "screenshot_pixels"
        assert all(state is None for state in result.matches[0].states.model_dump().values())
    if by != "image":
        dependency = result.observation.sources.screenshot.collection
        assert dependency is not None and dependency.purpose == "ocr_dependency"
    assert len(observations.calls) == 1 and backend.commands == [] and actions.calls == []


async def test_locate_is_one_read_and_its_target_uses_unchanged_action_pre_and_post_checks(
    action_rig,
):
    backend, manager, observations, actions = action_rig
    body = await locate_request(manager)
    result = await manager.locate(SERIAL_A, body)
    assert result["status"] == "resolved" and result["action_target"]["node_path"] == []
    assert len(observations.calls) == 1 and actions.calls == [] and backend.commands == []
    assert backend.mutations == [] and result["business_success"] is None
    stored = manager.slot(SERIAL_A).session.latest_observation
    assert stored.request_id == body.request_id
    assert all("data" not in source for source in stored.sources.values())
    assert "text" not in stored.nodes[()]
    view = {
        "serial": SERIAL_A,
        "session_id": body.session_id,
        "request_id": result["observation_id"],
    }
    action = action_body(view, target=result["action_target"])
    completed = await manager.action(SERIAL_A, action)
    assert completed["status"] == "completed" and len(actions.calls) == 1
    assert len(observations.calls) == 3 and completed["business_success"] is None
    assert completed["pre_observation_id"] != body.request_id
    assert completed["post_observation"]["request_id"] != body.request_id


async def test_private_scope_and_target_are_resolved_from_one_collection(action_rig):
    backend, manager, observations, _ = action_rig
    backend.geometry = {"display_id": 0, "width": 40, "height": 20, "rotation": 0}
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    nodes = native_tree()
    nodes[2]["text"] = "Other"

    def page(value):
        value["sources"]["tree"]["data"]["nodes"] = copy.deepcopy(nodes)

    observations.modify_result[SERIAL_A] = page
    body = request(
        candidate("text", "Duplicate"),
        serial=SERIAL_A,
        session_id=session.id,
        locators={
            "left": {
                "candidates": [candidate("res_id", "com.pulse.target:id/left")],
                "expect": {"unique": True, "visible": True},
            }
        },
    )
    result = await manager.probe(SERIAL_A, body, scope=m.LocatorRef(locator_ref="left"))
    assert result["status"] == "not_found"
    assert result["matches"] == []
    assert len(observations.calls) == 1


async def test_located_target_cannot_bypass_a_changed_page_before_dispatch(action_rig):
    _, manager, observations, actions = action_rig
    body = await locate_request(manager)
    located = await manager.locate(SERIAL_A, body)

    def changed(value):
        actions.source(SERIAL_A, value)
        value["sources"]["tree"]["data"]["nodes"][0]["text"] = "different page"

    observations.modify_result[SERIAL_A] = changed
    action = action_body(
        {"serial": SERIAL_A, "session_id": body.session_id, "request_id": body.request_id},
        target=located["action_target"],
    )
    result = await manager.action(SERIAL_A, action)
    assert result["status"] == "rejected" and result["error"]["code"] == "page_changed"
    assert actions.calls == []


async def test_duplicate_locator_is_ambiguous_through_the_authenticated_http_api(action_rig):
    _, manager, observations, actions = action_rig
    body = await locate_request(manager)
    inner = observations.modify_result[SERIAL_A]

    def duplicate(value):
        inner(value)
        nodes = value["sources"]["tree"]["data"]["nodes"]
        child = copy.deepcopy(nodes[0])
        child["node_path"] = [0]
        nodes.append(child)

    observations.modify_result[SERIAL_A] = duplicate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(TOKEN, device_manager=manager)),
        base_url="http://test",
        headers=AUTH,
    ) as client:
        response = await client.post(f"/devices/{SERIAL_A}/agent/locate", json=body.model_dump())
    result = response.json()
    assert response.status_code == 200 and result["status"] == "ambiguous"
    assert result["match_count"] == 2 and result["action_target"] is None
    assert actions.calls == []


async def test_visual_locator_is_a_bitmap_query_never_a_native_action_target(action_rig):
    _, manager, observations, actions = action_rig
    base = await locate_request(manager)
    body = request(
        candidate("image", "marker"),
        serial=SERIAL_A,
        session_id=base.session_id,
        resources={"marker": {"png_base64": png()}},
    )
    result = await manager.locate(SERIAL_A, body)
    assert result["status"] == "resolved" and result["matches"][0]["source"] == "screenshot"
    assert result["action_target"] is None
    assert result["action_target_error"]["code"] == "coordinate_mapping_unavailable"
    assert observations.calls[0][-1] == {"tree": True, "screenshot": True, "ocr": False}
    assert actions.calls == []


async def test_serial_sessions_requests_and_latest_binding_remain_isolated(action_rig):
    _, manager, observations, _ = action_rig
    first, second = await asyncio.gather(locate_request(manager), locate_request(manager, SERIAL_B))
    shared = str(uuid4())
    first.request_id = second.request_id = shared
    results = await asyncio.gather(
        manager.locate(SERIAL_A, first), manager.locate(SERIAL_B, second)
    )
    assert all(result["status"] == "resolved" for result in results)
    assert results[0]["serial"] == SERIAL_A and results[1]["serial"] == SERIAL_B
    assert manager.slot(SERIAL_A).session.latest_observation.session_id == first.session_id
    assert manager.slot(SERIAL_B).session.latest_observation.session_id == second.session_id
    with pytest.raises(ConnectionFault, match="serial_mismatch"):
        await manager.locate(SERIAL_B, first)
    with pytest.raises(ConnectionFault, match="observation_request_reused"):
        await manager.locate(SERIAL_A, first)
    assert len(observations.calls) == 2


async def test_stale_session_and_unavailable_source_never_become_not_found(action_rig):
    _, manager, observations, actions = action_rig
    body = await locate_request(manager)
    stale = body.model_copy(update={"session_id": str(uuid4())})
    with pytest.raises(ConnectionFault, match="stale_session"):
        await manager.locate(SERIAL_A, stale)

    def fail(value):
        value["sources"]["tree"].update(
            status="unavailable",
            data=None,
            error={"code": "window_unavailable", "phase": "observe", "retryable": False},
        )

    observations.modify_result[SERIAL_A] = fail
    result = await manager.locate(SERIAL_A, body)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["observation"]["sources"]["tree"]["status"] == "unavailable"
    assert actions.calls == []


async def test_owned_cancel_during_collection_does_not_cancel_another_serial(action_rig):
    _, manager, observations, actions = action_rig
    first, second = await asyncio.gather(locate_request(manager), locate_request(manager, SERIAL_B))
    gate = observations.gate[SERIAL_A] = Gate()
    task = asyncio.create_task(manager.locate(SERIAL_A, first))
    await asyncio.wait_for(gate.entered.wait(), 5)
    other = await manager.locate(SERIAL_B, second)
    assert other["status"] == "resolved"
    assert (await manager.cancel_observation(SERIAL_B, first.session_id, first.request_id))[
        "cancelled"
    ] is False
    assert (await manager.cancel_observation(SERIAL_A, first.session_id, str(uuid4())))[
        "cancelled"
    ] is False
    assert (await manager.cancel_observation(SERIAL_A, first.session_id, first.request_id))[
        "cancelled"
    ] is True
    result = await asyncio.wait_for(task, 5)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert manager.slot(SERIAL_A).session.latest_observation is None and actions.calls == []


def hold_resolver(monkeypatch):
    loop = asyncio.get_running_loop()
    entered, exited = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    original = devices.resolve_locator

    def held(*args, **kwargs):
        if args[1].get("status") != "completed":
            return original(*args, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5), "test resolver gate was not released"
            return original(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(exited.set)

    monkeypatch.setattr(devices, "resolve_locator", held)
    return entered, exited, release


async def test_pure_resolution_keeps_read_lease_and_cancelled_thread_cannot_publish(
    action_rig, monkeypatch
):
    _, manager, _, actions = action_rig
    body = await locate_request(manager)
    entered, exited, release = hold_resolver(monkeypatch)
    task = asyncio.create_task(manager.locate(SERIAL_A, body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert manager.slot(SERIAL_A).observation.phase == "resolving"
        with pytest.raises(ConnectionFault, match="observation_in_progress"):
            await manager.observe(SERIAL_A, body.session_id, str(uuid4()))
        blocked = await manager.action(
            SERIAL_A,
            action_body(
                {"serial": SERIAL_A, "session_id": body.session_id, "request_id": body.request_id}
            ),
        )
        assert blocked["status"] == "rejected" and actions.calls == []
        assert (await manager.cancel_observation(SERIAL_A, body.session_id, body.request_id))[
            "cancelled"
        ] is True
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    await asyncio.wait_for(exited.wait(), 5)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert manager.slot(SERIAL_A).observation is None
    assert manager.slot(SERIAL_A).session.latest_observation is None


async def test_lifecycle_change_during_resolution_invalidates_the_computed_target(
    action_rig, monkeypatch
):
    backend, manager, _, actions = action_rig
    body = await locate_request(manager)
    entered, exited, release = hold_resolver(monkeypatch)
    task = asyncio.create_task(manager.locate(SERIAL_A, body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        backend.snapshots[SERIAL_A].lifecycle = str(uuid4())
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    await asyncio.wait_for(exited.wait(), 5)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["observation"]["error"]["code"] == "agent_lifecycle_changed"
    assert manager.slot(SERIAL_A).session is None and actions.calls == []


async def test_resolution_timeout_does_not_publish_late_results(action_rig, monkeypatch):
    _, manager, _, _ = action_rig
    body = await locate_request(manager, timeout_ms=1000)
    entered, exited, release = hold_resolver(monkeypatch)
    task = asyncio.create_task(manager.locate(SERIAL_A, body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # No busy wait or timer loop: the request's real timeout is the event being tested.
        result = await asyncio.wait_for(task, 5)
    finally:
        release.set()
    await asyncio.wait_for(exited.wait(), 5)
    assert result["status"] == "indeterminate" and result["action_target"] is None
    assert result["observation"]["error"]["code"] == "observation_timeout"
    assert manager.slot(SERIAL_A).session.latest_observation is None


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "nested_duplicate",
        "nan",
        "infinity",
        "extra",
        "query",
        "type",
        "malformed",
        "utf8",
        "both_targets",
    ],
)
async def test_locator_http_input_is_strict_and_never_logs_raw_values(action_rig, caplog, case):
    _, manager, observations, _ = action_rig
    body = await locate_request(manager)
    value = body.model_dump()
    payload, path, content_type = (
        json.dumps(value),
        f"/devices/{SERIAL_A}/agent/locate",
        "application/json",
    )
    if case == "duplicate":
        payload = payload[:-1] + ', "timeout_ms": 1000}'
    elif case == "nested_duplicate":
        payload = payload.replace('"unique": true', '"unique": true, "unique": false')
    elif case in {"nan", "infinity"}:
        payload = payload.replace(
            '"image_threshold": 0.999',
            '"image_threshold": ' + ("NaN" if case == "nan" else "1e999"),
        )
    elif case == "extra":
        value["snapshot"] = PRIVATE
        payload = json.dumps(value)
    elif case == "query":
        path += "?unexpected=1"
    elif case == "type":
        content_type = "text/plain"
    elif case == "malformed":
        payload = "{invalid " + PRIVATE
    elif case == "utf8":
        payload = b"\xff"
    else:
        value["target"]["locator_ref"] = PRIVATE
        payload = json.dumps(value)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(TOKEN, device_manager=manager)),
        base_url="http://test",
        headers=AUTH,
    ) as client:
        response = await client.post(path, content=payload, headers={"Content-Type": content_type})
    assert response.status_code == 422 and response.json()["error"] == "invalid_request"
    assert observations.calls == []
    assert (
        PRIVATE not in response.text and PRIVATE not in caplog.text and TOKEN_A not in caplog.text
    )


async def test_locator_http_auth_size_and_cancel_query_boundaries(action_rig):
    _, manager, observations, _ = action_rig
    body = await locate_request(manager)
    app = create_app(TOKEN, device_manager=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(f"/devices/{SERIAL_A}/agent/locate", json=body.model_dump())
        assert denied.status_code == 401
        too_large = await client.post(
            f"/devices/{SERIAL_A}/agent/locate",
            content=b" " * 4_194_305,
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert too_large.status_code == 413
        cancel_path = (
            f"/devices/{SERIAL_A}/agent/locate/cancel"
            f"?session_id={body.session_id}&request_id={body.request_id}"
        )
        inactive = await client.post(cancel_path, headers=AUTH)
        duplicate = await client.post(cancel_path + f"&request_id={body.request_id}", headers=AUTH)
        assert inactive.json() == {"cancelled": False, "cleanup_complete": True}
        assert duplicate.status_code == 400
    assert observations.calls == []
