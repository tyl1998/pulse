"""P2.2 local Runtime API: projects, runs, events, recording and step-boundary debug."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from pulse_runtime.api.app import create_app
from pulse_runtime.api.events import EventBus
from pulse_runtime.api.projects import ProjectRegistry, RegisteredProject
from pulse_runtime.api.recording import RecordingManager
from pulse_runtime.api.runs import RunManager
from pulse_runtime.drivers.android.actions import ActionBody
from pulse_runtime.drivers.android.devices import DeviceConnectionManager
from runtime.tests.test_interpreter_golden import (
    EXPECTED,
    FIXTURE,
    BlockingDriver,
    FakeDriver,
)
from runtime.tests.test_interpreter_golden import (
    project as simple_project,
)

TOKEN = "p22-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION_ID = "12345678-1234-4234-8234-123456789abc"
SERIAL = "FAKE_SERIAL"


class DriverBox:
    def __init__(self) -> None:
        self.next: Any = None

    def make(self) -> Any:
        driver = self.next if self.next is not None else FakeDriver(EXPECTED["pass"]["world"])
        self.next = None
        return driver


ApiRig = tuple[ProjectRegistry, EventBus, RunManager, RecordingManager, DriverBox, AsyncClient]


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[ApiRig]:
    registry = ProjectRegistry()
    registry.open(str(FIXTURE))
    events = EventBus()
    drivers = DriverBox()

    async def resolver(_serial: str, _auto: bool, _timeout: float) -> str:
        return SESSION_ID

    runs = RunManager(
        registry,
        events,
        driver_factory=lambda _serial, _session, _profile: drivers.make(),
        session_resolver=resolver,
        evidence_root=tmp_path / "evidence",
    )
    recordings = RecordingManager(events)
    manager = DeviceConnectionManager()
    app = create_app(
        TOKEN,
        device_manager=manager,
        project_registry=registry,
        event_bus=events,
        recording_manager=recordings,
        run_manager=runs,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://pulse.test"
        ) as client:
            yield registry, events, runs, recordings, drivers, client
    finally:
        await runs.close()
        events.close()
        await manager.close()


async def _wait_event(
    subscriber: Any, event_type: str, run_id: str, timeout_s: float = 5
) -> dict[str, Any]:
    while True:
        event = await asyncio.wait_for(subscriber.next(), timeout_s)
        assert event is not None
        if event.type == event_type and event.run_id == run_id:
            return event.model_dump(mode="json")


async def _start(api, body: dict[str, Any]) -> tuple[RunManager, Any, dict[str, Any]]:
    _, events, runs, _, _, client = api
    subscriber = events.subscribe()
    try:
        response = await client.post("/runs", json=body, headers=AUTH)
        assert response.status_code == 202, response.text
        summary = response.json()
        return runs, subscriber, summary
    except BaseException:
        events.unsubscribe(subscriber)
        raise


def _run_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "project_id": "interpreter-golden",
        "case_id": "pass",
        "profile_id": "fake-android",
        "device": SERIAL,
        "include_screenshot": False,
        "auto_connect": True,
    }
    body.update(overrides)
    return body


async def test_project_open_read_and_preflight(api) -> None:
    _, _, _, _, _, client = api
    response = await client.post("/projects", json={"path": str(FIXTURE)}, headers=AUTH)
    assert response.status_code == 201
    project = response.json()
    assert project["project_id"] == "interpreter-golden"
    assert project["case_ids"] == [
        "ai-blocked",
        "business-fail",
        "evidence-blocked",
        "loop-limit",
        "pass",
        "wait-timeout",
    ]
    assert project["manifest"]["project_id"] == "interpreter-golden"

    assert (await client.get("/projects", headers=AUTH)).json()["projects"][0]["project_id"] == (
        "interpreter-golden"
    )
    case = await client.get("/projects/interpreter-golden/cases/pass", headers=AUTH)
    assert case.status_code == 200 and case.json()["case_id"] == "pass"
    preflight = await client.post(
        "/projects/interpreter-golden/preflight",
        json={"case_id": "pass", "profile_id": "fake-android"},
        headers=AUTH,
    )
    assert preflight.status_code == 200
    assert preflight.json()["offline_ready"] is True


async def test_project_open_rejects_unreadable_invalid_and_conflicting_roots(
    api, tmp_path: Path
) -> None:
    _, _, _, _, _, client = api
    missing = await client.post("/projects", json={"path": str(tmp_path / "missing")}, headers=AUTH)
    assert missing.status_code == 404 and missing.json()["error"] == "project_unreadable"

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{", encoding="utf-8")
    invalid = await client.post("/projects", json={"path": str(bad)}, headers=AUTH)
    assert invalid.status_code == 422 and invalid.json()["error"] == "project_invalid"
    assert invalid.json()["issues"]

    copy = tmp_path / "copy"
    shutil.copytree(FIXTURE, copy)
    conflict = await client.post("/projects", json={"path": str(copy)}, headers=AUTH)
    assert conflict.status_code == 409 and conflict.json()["error"] == "project_id_conflict"


async def test_run_emits_ordered_events_and_writes_evidence(api) -> None:
    runs, subscriber, summary = await _start(api, _run_body())
    try:
        debug = await _wait_event(subscriber, "run.finished", summary["run_id"])
        final = await asyncio.wait_for(runs.wait(summary["run_id"]), 10)
    finally:
        api[1].unsubscribe(subscriber)

    assert final.state == "passed"
    assert debug["body"]["status"] == "passed"
    assert final.evidence.status == "written"
    assert final.business_success is True

    page = (await api[5].get("/runs", headers=AUTH)).json()
    assert page["runs"][0]["run_id"] == summary["run_id"]

    detail = (await api[5].get(f"/runs/{summary['run_id']}", headers=AUTH)).json()
    assert detail["result"]["status"] == "passed"
    assert detail["result"]["steps"]
    report = await api[5].get(f"/runs/{summary['run_id']}/report", headers=AUTH)
    assert report.status_code == 200 and report.json()["run"]["status"] == "passed"
    html = await api[5].get(f"/runs/{summary['run_id']}/report/html", headers=AUTH)
    assert html.status_code == 200 and "text/html" in html.headers["content-type"]

    assert page["runs"][0]["state"] == "passed"

    backfill = (
        await api[5].get(f"/events?since=0&run_id={summary['run_id']}", headers=AUTH)
    ).json()
    seqs = [event["seq"] for event in backfill["events"]]
    assert seqs == sorted(seqs)
    assert any(event["type"] == "run.step_start" for event in backfill["events"])
    assert any(event["type"] == "run.finished" for event in backfill["events"])


async def test_debug_breakpoint_pauses_at_step_boundary_and_resumes(api) -> None:
    runs, subscriber, summary = await _start(api, _run_body(breakpoints=["type-search"]))
    try:
        paused = await _wait_event(subscriber, "debug.paused", summary["run_id"])
        assert paused["body"]["step"]["id"] == "type-search"
        snapshot = (await api[5].get(f"/runs/{summary['run_id']}/debug", headers=AUTH)).json()
        assert snapshot["paused"] is True
        assert snapshot["state"] == "paused"
        assert snapshot["current"]["id"] == "type-search"

        set_points = await api[5].put(
            f"/runs/{summary['run_id']}/debug/breakpoints",
            json={"breakpoints": ["swipe"]},
            headers=AUTH,
        )
        assert set_points.json()["breakpoints"] == ["swipe"]

        resume = await api[5].post(f"/runs/{summary['run_id']}/debug/resume", headers=AUTH)
        assert resume.status_code == 200
        await _wait_event(subscriber, "debug.resumed", summary["run_id"])
        await _wait_event(subscriber, "debug.paused", summary["run_id"])
        second = (await api[5].get(f"/runs/{summary['run_id']}/debug", headers=AUTH)).json()
        assert second["current"]["id"] == "swipe"
        await api[5].post(f"/runs/{summary['run_id']}/debug/resume", headers=AUTH)
        final = await asyncio.wait_for(runs.wait(summary["run_id"]), 10)
    finally:
        api[1].unsubscribe(subscriber)

    assert final.state == "passed"


async def test_stop_cancels_an_active_run(api) -> None:
    _, events, runs, _, drivers, client = api
    blocking = BlockingDriver(EXPECTED["business-fail"]["world"])
    drivers.next = blocking
    subscriber = events.subscribe()
    try:
        response = await client.post(
            "/runs",
            json=_run_body(case_id="business-fail", include_screenshot=False),
            headers=AUTH,
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        await asyncio.wait_for(blocking.entered.wait(), 5)
        stopped = await client.post(f"/runs/{run_id}/stop", headers=AUTH)
        assert stopped.status_code == 200
        final = await asyncio.wait_for(runs.wait(run_id), 10)
        assert final.state == "canceled"
        assert blocking.finished == ["canceled"]
        event = await _wait_event(subscriber, "run.finished", run_id)
        assert event["body"]["status"] == "canceled"
        inactive = await client.post(f"/runs/{run_id}/debug/pause", headers=AUTH)
        assert inactive.status_code == 409
    finally:
        events.unsubscribe(subscriber)


async def test_stop_keeps_partial_results_and_json_html_reports(api) -> None:
    runs, subscriber, summary = await _start(api, _run_body(breakpoints=["swipe"]))
    run_id = summary["run_id"]
    client = api[5]
    try:
        await _wait_event(subscriber, "debug.paused", run_id)
        stopped = await client.post(f"/runs/{run_id}/stop", headers=AUTH)
        assert stopped.status_code == 200
        final = await asyncio.wait_for(runs.wait(run_id), 5)
        assert final.state == "canceled"
        assert final.evidence.status == "written" and final.evidence.directory is not None
        assert final.business_success is None

        detail = (await client.get(f"/runs/{run_id}", headers=AUTH)).json()
        assert detail["result"]["status"] == "canceled"
        assert detail["result"]["reason"] == "cancelled"
        assert detail["result"]["steps"]
        assert detail["result"]["cleanup"]["state"] == "canceled"

        report = await client.get(f"/runs/{run_id}/report", headers=AUTH)
        assert report.status_code == 200
        assert report.json()["run"]["status"] == "canceled"
        assert report.json()["steps"] == detail["result"]["steps"]
        html = await client.get(f"/runs/{run_id}/report/html", headers=AUTH)
        assert html.status_code == 200 and "text/html" in html.headers["content-type"]
        assert "canceled" in html.text

        repeated = await client.post(f"/runs/{run_id}/stop", headers=AUTH)
        assert repeated.status_code == 200 and repeated.json() == stopped.json()
        finished = [
            event for event in api[1].page(run_id=run_id).events if event.type == "run.finished"
        ]
        assert len(finished) == 1
    finally:
        api[1].unsubscribe(subscriber)


async def test_stop_before_a_lease_is_acquired_keeps_a_canceled_report(api) -> None:
    class WaitingBeginDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__(EXPECTED["pass"]["world"])
            self.entered = asyncio.Event()

        async def begin(self, run_id: str, *, source: str):
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    driver = WaitingBeginDriver()
    api[4].next = driver
    runs, subscriber, summary = await _start(api, _run_body())
    run_id = summary["run_id"]
    try:
        await asyncio.wait_for(driver.entered.wait(), 5)
        stopped = await api[5].post(f"/runs/{run_id}/stop", headers=AUTH)
        assert stopped.status_code == 200
        final = await asyncio.wait_for(runs.wait(run_id), 5)
        assert (final.state, final.reason) == ("canceled", "cancelled")
        assert final.evidence.status == "written"
        assert driver.finished == []
        report = await api[5].get(f"/runs/{run_id}/report", headers=AUTH)
        assert report.status_code == 200
        assert report.json()["run"]["status"] == "canceled"
        assert report.json()["run"]["business_success"] is None
    finally:
        api[1].unsubscribe(subscriber)


async def test_debug_pause_expires_without_a_resume_request(api) -> None:
    selected, _profile = simple_project()
    selected.manifest.budgets.case_timeout_ms = 250
    api[0]._projects["test"] = RegisteredProject(FIXTURE, selected)
    runs, subscriber, summary = await _start(
        api,
        _run_body(project_id="test", case_id="run", profile_id="fake", breakpoints=["tap-root"]),
    )
    run_id = summary["run_id"]
    try:
        await _wait_event(subscriber, "debug.paused", run_id)
        final = await asyncio.wait_for(runs.wait(run_id), 5)
        assert (final.state, final.reason) == ("blocked", "deadline_exceeded")
        assert final.evidence.status == "written"
        debug = (await api[5].get(f"/runs/{run_id}/debug", headers=AUTH)).json()
        assert debug["paused"] is False and debug["state"] == "blocked"
    finally:
        api[1].unsubscribe(subscriber)


async def test_run_refuses_preflight_and_unknown_selection(api) -> None:
    _, _, _, _, _, client = api
    unknown = await client.post("/runs", json=_run_body(case_id="missing"), headers=AUTH)
    assert unknown.status_code == 404 and unknown.json()["error"] == "case_not_found"

    bad_strategy = await client.post("/runs", json=_run_body(strategy="ai_led"), headers=AUTH)
    assert bad_strategy.status_code == 422
    assert bad_strategy.json()["error"] == "preflight_failed"
    assert any(item["code"] == "model_unavailable" for item in bad_strategy.json()["issues"])


async def test_recording_records_only_confirmed_recorder_actions(api) -> None:
    _, _, _, recordings, _, client = api
    response = await client.post(
        "/recordings", json={"serial": SERIAL, "session_id": SESSION_ID}, headers=AUTH
    )
    assert response.status_code == 201
    recording_id = response.json()["recording_id"]

    body = ActionBody(
        serial=SERIAL,
        session_id=SESSION_ID,
        request_id=str(uuid4()),
        operation_id=str(uuid4()),
        kind="tap",
        target={
            "observation_id": str(uuid4()),
            "source": "tree",
            "coordinate_space": "display_pixels",
            "display_id": 0,
            "expected_package": "com.pulse.target",
            "node_path": [1],
        },
        params={"x": 10, "y": 20},
        source="recorder",
    )
    completed = {
        "status": "completed",
        "action_completed": True,
        "post_observation": {"request_id": str(uuid4())},
    }
    assert recordings.capture_action(SERIAL, body, completed) is not None
    assert recordings.capture_action(SERIAL, body, {**completed, "status": "rejected"}) is None
    assert (
        recordings.capture_action(SERIAL, body.model_copy(update={"source": "auto"}), completed)
        is None
    )

    paused = await client.post(f"/recordings/{recording_id}/pause", headers=AUTH)
    assert paused.json()["state"] == "paused"
    resumed = await client.post(f"/recordings/{recording_id}/resume", headers=AUTH)
    assert resumed.json()["state"] == "recording"
    stopped = await client.post(f"/recordings/{recording_id}/stop", headers=AUTH)
    detail = stopped.json()
    assert detail["state"] == "stopped"
    assert detail["event_count"] == 1
    assert detail["draft"]["normalized"] is False
    assert detail["draft"]["events"][0]["action_id"] == body.request_id
    listed = (await client.get("/recordings", headers=AUTH)).json()
    assert listed["recordings"][0]["recording_id"] == recording_id


async def test_all_p22_http_routes_require_a_bearer_token(api) -> None:
    _, _, _, _, _, client = api
    routes = [
        ("GET", "/projects"),
        ("POST", "/projects"),
        ("GET", "/projects/interpreter-golden"),
        ("DELETE", "/projects/interpreter-golden"),
        ("GET", "/projects/interpreter-golden/cases"),
        ("GET", "/projects/interpreter-golden/cases/pass"),
        ("GET", "/projects/interpreter-golden/profiles/fake-android"),
        ("GET", "/projects/interpreter-golden/flows/fill-search"),
        ("GET", "/projects/interpreter-golden/data/cart"),
        ("POST", "/projects/interpreter-golden/preflight"),
        ("POST", "/runs"),
        ("GET", "/runs"),
        ("GET", "/runs/x"),
        ("GET", "/runs/x/result"),
        ("POST", "/runs/x/stop"),
        ("GET", "/runs/x/events"),
        ("GET", "/runs/x/report"),
        ("GET", "/runs/x/report/html"),
        ("GET", "/runs/x/debug"),
        ("PUT", "/runs/x/debug/breakpoints"),
        ("POST", "/runs/x/debug/pause"),
        ("POST", "/runs/x/debug/resume"),
        ("POST", "/runs/x/debug/step"),
        ("GET", "/recordings"),
        ("POST", "/recordings"),
        ("GET", "/recordings/x"),
        ("POST", "/recordings/x/pause"),
        ("POST", "/recordings/x/resume"),
        ("POST", "/recordings/x/stop"),
        ("GET", "/events"),
    ]
    for method, route in routes:
        response = await client.request(method, route)
        assert response.status_code == 401, (method, route, response.text)
        assert response.json() == {"error": "unauthorized"}


def test_event_bus_marks_an_evicted_backlog_as_a_gap() -> None:
    events = EventBus(max_events=2)
    for index in range(3):
        events.publish("test.event", {"n": index}, run_id=str(uuid4()))
    page = events.page(since=0)
    assert page.gap is True
    assert [event.seq for event in page.events] == [2, 3]
    assert events.page(since=2).gap is False


def test_event_stream_replays_from_sequence_and_rejects_bad_tokens() -> None:
    events = EventBus()
    app = create_app(TOKEN, event_bus=events)
    run_id = str(uuid4())
    events.publish("test.one", {"n": 1}, run_id=run_id)
    events.publish("test.two", {"n": 2}, run_id=run_id)
    with TestClient(app) as client:
        backfill = client.get(f"/events?since=1&run_id={run_id}", headers=AUTH)
        assert backfill.status_code == 200
        assert [item["seq"] for item in backfill.json()["events"]] == [2]

        with client.websocket_connect(f"/ws/events?token={TOKEN}&since=1&run_id={run_id}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "stream.hello"
            assert hello["body"]["replayed"] == 1
            event = ws.receive_json()
            assert event["seq"] == 2 and event["type"] == "test.two"

        with (
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect("/ws/events?token=wrong&since=0") as ws,
        ):
            ws.receive_json()
        assert caught.value.code == 4401

        with (
            pytest.raises(WebSocketDisconnect) as invalid,
            client.websocket_connect(f"/ws/events?token={TOKEN}&since=-1") as ws,
        ):
            ws.receive_json()
        assert invalid.value.code == 4400

        with client.websocket_connect(f"/ws/events?token={TOKEN}&since=0") as ws:
            ws.receive_json()
            ws.receive_json()
            ws.receive_json()


def test_openapi_document_is_authenticated_and_contains_p22_paths() -> None:
    app = create_app(TOKEN)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 401
        response = client.get("/openapi.json", headers=AUTH)
        assert response.status_code == 200
        spec = response.json()
    assert "/projects" in spec["paths"]
    assert "/runs/{run_id}/debug/step" in spec["paths"]
    assert "/ws/events" not in spec["paths"]
    assert spec["paths"]["/runs"]["post"]["operationId"] == "startRun"
    assert "RunSummary" in spec["components"]["schemas"]
    assert "EventEnvelope" in spec["components"]["schemas"]
