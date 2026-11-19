"""P1.7 interpreter golden cases and a real manager/ledger integration.

The golden project runs against a scripted driver only. The final test uses the same
interpreter with DeviceConnectionManager's fake Android backend so run/step IDs, gate
ownership and normal cleanup are exercised without a phone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest

from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_runtime.engine.interpreter import run_case
from pulse_runtime.perception.locators import LocateBody
from pulse_schema import models as m
from pulse_schema.validation import Project, load_project
from runtime.tests.test_actions import action_rig as action_rig
from runtime.tests.test_device_connections import SERIAL_A

FIXTURE = Path(__file__).parents[2] / "fixtures/golden-cases/interpreter"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text("utf-8"))["cases"]


class FakeDriver:
    app_id = "com.pulse.target"

    def __init__(self, world: Mapping[str, object]) -> None:
        self.world = dict(world)
        self.actions: list[ActionPlan] = []
        self.finished: list[str] = []
        self.lease: RunLease | None = None

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        self.lease = RunLease(run_id, 3)
        return self.lease

    def _element(self, name: str) -> Element:
        checked = bool(self.world.get("confirm_checked"))
        return Element(
            "tree",
            text=self.world.get("text") if type(self.world.get("text")) is str else None,
            visible=True,
            states={
                "enabled": True,
                "visible": True,
                "checked": checked if name == "confirm" else None,
            },
            bounds={"left": 10, "top": 20, "right": 110, "bottom": 120},
        )

    def _target(self, name: str) -> Resolution:
        if name == "items":
            count = int(self.world.get("items", 0))
            matches = tuple(self._element("item") for _ in range(count))
            return Resolution(
                "resolved" if count else "not_found",
                None if count else "no_match",
                matches,
            )
        if name == "banner":
            if not bool(self.world.get("banner")):
                return Resolution("not_found", "no_match")
            return Resolution(
                "resolved",
                None,
                (Element("tree", "Example banner", True, {"enabled": True, "visible": True}),),
                {"node_path": [4]},
            )
        if name == "product":
            status = self.world.get("product_status", "resolved")
            if status != "resolved":
                return Resolution(str(status), "source_incomplete")  # type: ignore[arg-type]
            return Resolution("resolved", None, (self._element(name),), {"node_path": [1]})
        if status := self.world.get(f"{name}_status"):
            return Resolution(str(status), "source_unavailable")  # type: ignore[arg-type]
        return Resolution("resolved", None, (self._element(name),), {"node_path": [2]})

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        if scope is not None:
            scoped = self._target(
                scope.locator_ref if isinstance(scope, m.LocatorRef) else "inline"
            )
            if scoped.status != "resolved" or not scoped.query_complete or len(scoped.matches) != 1:
                return Resolution("indeterminate", "scope_unavailable")
        name = target.locator_ref if isinstance(target, m.LocatorRef) else "inline"
        return self._target(name)

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        assert lease == self.lease
        self.actions.append(plan)
        if plan.kind == "set_text":
            self.world["text"] = plan.params["text"]
        elif plan.kind == "long":
            self.world["items"] = int(self.world.get("items", 0)) + 1
        elif plan.kind == "key" and plan.params["code"] == 4:
            self.world["confirm_checked"] = True
        elif plan.kind == "tap" and plan.target_ref == "banner":
            self.world["banner"] = False
        return ActionResult("completed", None, str(uuid4()), str(uuid4()))

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        assert lease == self.lease
        ledger_state = (
            "interrupted"
            if state == "interrupted"
            else "canceled"
            if state == "canceled"
            else "finished"
        )
        self.finished.append(ledger_state)
        return {"status": "cleaned", "state": ledger_state}


@pytest.mark.parametrize("case_id", list(EXPECTED))
async def test_golden_case_reaches_expected_terminal_state(case_id: str) -> None:
    expected = EXPECTED[case_id]
    project = load_project(FIXTURE)
    driver = FakeDriver(expected.get("world", {}))
    result = await run_case(
        project=project,
        driver=driver,
        case_id=case_id,
        profile_id="fake-android",
        run_id=str(uuid4()),
    )
    assert result.status == expected["status"]
    assert result.reason == expected["reason"]
    assert driver.finished == ([] if case_id == "ai-blocked" else ["finished"])
    for step in result.steps:
        wanted = expected.get("step_status", {}).get(step.id)
        if wanted is not None:
            assert step.status == wanted
    for check in result.checks:
        wanted = expected.get("check_status", {}).get(check.id)
        if wanted is not None:
            assert check.status == wanted
    if "loop_entries" in expected:
        loop = next(step for step in result.steps if step.id == "bounded")
        assert loop.details["entries"] == expected["loop_entries"]


async def test_flow_arguments_bind_if_else_loop_and_actions() -> None:
    expected = EXPECTED["pass"]
    driver = FakeDriver(expected["world"])
    project = load_project(FIXTURE)
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
    )
    assert result.status == "passed" and result.reason is None
    branch = next(step for step in result.steps if step.id == "banner-branch")
    assert branch.details["branch"] == "else"
    loop = next(step for step in result.steps if step.id == "bounded")
    assert loop.details["entries"] == 2 and loop.details["limit_reached"] is False
    kinds = [(plan.kind, plan.target_ref, plan.params) for plan in driver.actions]
    assert ("set_text", "search", {"text": "Coffee"}) in kinds
    assert sum(1 for kind, _, _ in kinds if kind == "set_text") == 3
    assert sum(1 for kind, _, _ in kinds if kind == "long") == 2
    assert ("key", None, {"code": 4}) in kinds
    assert driver.finished == ["finished"]


def project() -> tuple[Project, m.Profile]:
    profile = m.Profile.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:profile",
            "profile_id": "fake",
            "platform": "android",
            "environment": "test",
            "app_id": "com.pulse.target",
            "capabilities": ["observe"],
            "locators": {
                "root": {
                    "candidates": [{"by": "res_id", "value": "com.pulse.target:id/submit"}],
                    "expect": {"unique": True, "visible": True},
                }
            },
        }
    )
    case = m.Case.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:case",
            "case_id": "run",
            "name": "Run with a real manager",
            "goal": "Action and check complete through P1.5/P1.4",
            "parameters": {},
            "profile_refs": ["fake"],
            "inputs": {},
            "locators": {},
            "preconditions": [],
            "steps": [
                {
                    "type": "action",
                    "id": "tap-root",
                    "action": {"kind": "tap", "target": {"locator_ref": "root"}},
                },
                {
                    "type": "check",
                    "id": "root-exists",
                    "checkpoint": {
                        "id": "root-check",
                        "required": True,
                        "condition": {
                            "operator": "exists",
                            "target": {"locator_ref": "root"},
                            "source": "native",
                        },
                    },
                },
            ],
            "checks": [],
        }
    )
    manifest = m.Manifest.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:manifest",
            "project_id": "test",
            "name": "Test",
            "revision": "1",
            "cases": {"run": "cases/run.json"},
            "flows": {},
            "data": {},
            "profiles": {"fake": "profiles/fake.json"},
            "entry_cases": ["run"],
            "default_strategy": "rule_only",
            "platforms": ["android"],
            "required_capabilities": [],
            "constraints": {"allowed_apps": ["com.pulse.target"], "forbidden_actions": []},
            "dependencies": [],
        }
    )
    return Project(manifest, {"run": case}, {}, {}, {"fake": profile}), profile


async def test_manager_integration_records_run_and_step_and_releases_control(action_rig) -> None:
    backend, manager, _, actions = action_rig
    backend.geometry = {"display_id": 0, "width": 20, "height": 10, "rotation": 0}
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    selected, profile = project()
    driver = AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile)
    result = await run_case(
        project=selected,
        driver=driver,
        case_id="run",
        profile_id="fake",
        run_id=str(uuid4()),
    )
    assert result.status == "passed", result.as_dict()
    assert len(actions.calls) == 1
    control = manager.gate.snapshot(SERIAL_A)
    assert control.holder_source is None and control.run_id is None
    records = manager.ledger.list_actions(SERIAL_A)
    assert records[0]["run_id"] == result.run_id
    assert records[0]["step_id"] == "tap-root"
    assert records[0]["state"] == "verified"


@pytest.mark.parametrize("nested", [False, True], ids=["case-budget", "parent-flow-budget"])
async def test_debug_pause_expires_and_releases_control(action_rig, nested: bool) -> None:
    _, manager, _, actions = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    selected, profile = project()
    selected.manifest.budgets.case_timeout_ms = 30_000 if nested else 500
    if nested:
        selected.flows["nested"] = m.Flow.model_validate(
            {
                "$schema": "urn:pulse:schema:1-draft:flow",
                "flow_id": "nested",
                "name": "Paused child step",
                "parameters": {},
                "locators": {},
                "steps": selected.cases["run"].steps,
                "returns": {},
            }
        )
        selected.manifest.flows["nested"] = "flows/nested.json"
        selected.cases["run"].steps = [
            m.FlowStep(type="flow", id="call", flow_ref="nested", timeout_ms=500)
        ]

    class PausedDebug:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def before_step(self, step_id: str, step_type: str) -> None:
            if step_id == "tap-root":
                self.entered.set()
                await asyncio.Event().wait()

    debug = PausedDebug()
    run_id = str(uuid4())
    task = asyncio.create_task(
        run_case(
            project=selected,
            driver=AndroidCaseDriver(
                manager, serial=SERIAL_A, session_id=session.id, profile=profile
            ),
            case_id="run",
            profile_id="fake",
            run_id=run_id,
            debug=debug,
        )
    )
    try:
        await asyncio.wait_for(debug.entered.wait(), 5)
        assert manager.gate.snapshot(SERIAL_A).run_id == run_id
        result = await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert (result.status, result.reason) == ("blocked", "deadline_exceeded")
    assert result.elapsed_ms < 2500
    assert actions.calls == []
    control = manager.gate.snapshot(SERIAL_A)
    assert control.holder_source is None and control.run_id is None


async def test_probe_text_is_not_part_of_the_public_locate_projection(action_rig) -> None:
    _, manager, _, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    _, profile = project()
    body = LocateBody(
        serial=SERIAL_A,
        session_id=session.id,
        request_id=str(uuid4()),
        expected_package=profile.app_id,
        target=m.LocatorRef(locator_ref="root"),
        locators=profile.locators,
        timeout_ms=1000,
    )
    probe = await manager.probe(SERIAL_A, body)
    assert isinstance(probe["matches"][0]["text"], str)
    locate = await manager.locate(SERIAL_A, body.model_copy(update={"request_id": str(uuid4())}))
    assert "text" not in locate["matches"][0]


class HangingProductDriver(FakeDriver):
    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        name = target.locator_ref if isinstance(target, m.LocatorRef) else "inline"
        if name == "product":
            assert "product" in locators
            await asyncio.sleep(5)
        return await super().resolve(target, locators, timeout_ms=timeout_ms, scope=scope)


async def test_case_deadline_bounds_a_hanging_resolution() -> None:
    def timeout_locator(value: str) -> m.Locator:
        return m.Locator.model_validate(
            {
                "candidates": [{"by": "res_id", "value": value}],
                "expect": {"unique": True, "visible": True},
            }
        )

    profile = m.Profile.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:profile",
            "profile_id": "fake",
            "platform": "android",
            "environment": "test",
            "app_id": "com.pulse.target",
            "capabilities": ["observe"],
            "locators": {"page": timeout_locator("com.pulse.target:id/page")},
        }
    )
    case = m.Case.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:case",
            "case_id": "timeout",
            "name": "Deadline",
            "goal": "Bound a hanging read",
            "parameters": {},
            "profile_refs": ["fake"],
            "inputs": {},
            "locators": {"product": timeout_locator("com.pulse.target:id/product")},
            "preconditions": [],
            "steps": [
                {
                    "type": "wait",
                    "id": "ready",
                    "condition": {
                        "operator": "exists",
                        "target": {"locator_ref": "page"},
                        "source": "native",
                    },
                }
            ],
            "checks": [
                {
                    "id": "product",
                    "required": True,
                    "condition": {
                        "operator": "exists",
                        "target": {"locator_ref": "product"},
                        "source": "native",
                    },
                }
            ],
        }
    )
    manifest = m.Manifest.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:manifest",
            "project_id": "timeout",
            "name": "Timeout",
            "revision": "1",
            "cases": {"timeout": "cases/timeout.json"},
            "flows": {},
            "data": {},
            "profiles": {"fake": "profiles/fake.json"},
            "entry_cases": ["timeout"],
            "default_strategy": "rule_only",
            "platforms": ["android"],
            "required_capabilities": [],
            "constraints": {"allowed_apps": ["com.pulse.target"], "forbidden_actions": []},
            "dependencies": [],
            "budgets": {"case_timeout_ms": 100},
        }
    )
    project = Project(manifest, {"timeout": case}, {}, {}, {"fake": profile})
    driver = HangingProductDriver(EXPECTED["business-fail"]["world"])
    result = await asyncio.wait_for(
        run_case(
            project=project,
            driver=driver,
            case_id="timeout",
            profile_id="fake",
            run_id=str(uuid4()),
        ),
        2,
    )
    assert result.status == "blocked" and result.reason == "check_blocked"
    assert result.elapsed_ms < 1000


class BlockingDriver(FakeDriver):
    def __init__(self, world: Mapping[str, object]) -> None:
        super().__init__(world)
        self.entered = asyncio.Event()

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_cancellation_closes_run_once() -> None:
    project = load_project(FIXTURE)
    driver = BlockingDriver(EXPECTED["business-fail"]["world"])
    task = asyncio.create_task(
        run_case(
            project=project,
            driver=driver,
            case_id="business-fail",
            profile_id="fake-android",
            run_id=str(uuid4()),
        )
    )
    await asyncio.wait_for(driver.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.finished == ["canceled"]


async def test_finish_run_preserves_interrupted_state(action_rig) -> None:
    _, manager, _, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    run_id = str(uuid4())
    acquired = await manager.acquire_control(
        SERIAL_A, session_id=session.id, source="auto", run_id=run_id
    )
    epoch = acquired["control"]["epoch"]
    await manager.finish_run(
        SERIAL_A,
        session_id=session.id,
        control_epoch=epoch,
        run_id=run_id,
        state="interrupted",
    )
    assert manager.ledger.run_state(run_id) == "interrupted"
    control = manager.gate.snapshot(SERIAL_A)
    assert control.holder_source is None and control.run_id is None


async def test_progress_events_expose_step_boundaries_without_values() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def progress(event: str, data: dict[str, object]) -> None:
        events.append((event, dict(data)))

    project = load_project(FIXTURE)
    driver = FakeDriver(EXPECTED["pass"]["world"])
    result = await run_case(
        project=project,
        driver=driver,
        case_id="pass",
        profile_id="fake-android",
        run_id=str(uuid4()),
        progress=progress,
    )
    assert result.status == "passed"
    assert events[0][0] == "run_start"
    assert events[-1][0] == "run_finish"
    starts = [data for event, data in events if event == "step_start"]
    finishes = [data for event, data in events if event == "step_finish"]
    assert any(item["id"] == "fill-flow" for item in starts)
    flow = next(item for item in finishes if item["id"] == "fill-flow")
    assert flow["status"] == "passed"
    assert "text" not in flow and "params" not in flow
    assert events[-1][1]["status"] == "passed"


async def test_manual_takeover_prevents_read_only_run_from_passing(action_rig) -> None:
    _, manager, observations, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    selected, profile = project()
    selected.cases["run"].steps = selected.cases["run"].steps[1:]

    class TakingDriver(AndroidCaseDriver):
        async def resolve(self, target, locators, *, timeout_ms, scope=None, primary=None):
            takeover = await manager.take_over_control(
                SERIAL_A, session_id=session.id, source="manual", run_id=None
            )
            assert takeover["stopped_confirmed"] is True
            return await super().resolve(target, locators, timeout_ms=timeout_ms, scope=scope)

    result = await run_case(
        project=selected,
        driver=TakingDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile),
        case_id="run",
        profile_id="fake",
    )
    assert result.status == "canceled"
    assert result.business_success is None
    assert manager.ledger.run_state(result.run_id) == "canceled"
    assert observations.calls == []
    assert manager.gate.snapshot(SERIAL_A).holder_source == "manual"


async def test_swipe_rejects_declared_screen_size_mismatch_before_dispatch(action_rig) -> None:
    backend, manager, _, actions = action_rig
    backend.geometry = {"display_id": 0, "width": 20, "height": 10, "rotation": 0}
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    selected, profile = project()
    from pydantic import TypeAdapter

    selected.cases["run"].steps = TypeAdapter(list[m.Step]).validate_python(
        [
            {
                "id": "swipe",
                "type": "action",
                "action": {
                    "kind": "swipe",
                    "start": {"x": 1, "y": 1, "width": 1080, "height": 2400},
                    "end": {"x": 2, "y": 2, "width": 1080, "height": 2400},
                    "duration_ms": 500,
                },
            }
        ]
    )
    result = await run_case(
        project=selected,
        driver=AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile),
        case_id="run",
        profile_id="fake",
    )
    assert result.status == "blocked"
    assert result.reason == "swipe_screen_mismatch"
    assert actions.calls == []


async def test_android_adapter_preserves_partial_count_evidence(action_rig) -> None:
    _, manager, observations, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    _, profile = project()
    profile.locators["root"].expect.unique = False
    original = observations.modify_result[SERIAL_A]

    def incomplete(value):
        original(value)
        value["sources"]["tree"]["completeness"]["reasons"].append("node_limit")

    observations.modify_result[SERIAL_A] = incomplete
    driver = AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile)
    result = await driver.resolve(
        m.LocatorRef(locator_ref="root"), profile.locators, timeout_ms=1000
    )
    assert result.status == "resolved"
    assert result.query_complete is False
    assert len(result.matches) == 1


async def test_takeover_between_finish_validation_and_ledger_close_cannot_pass(
    action_rig, monkeypatch
) -> None:
    _, manager, _, _ = action_rig
    await manager.connect(SERIAL_A)
    session = manager.slot(SERIAL_A).session
    assert session is not None
    selected, profile = project()
    selected.cases["run"].steps = selected.cases["run"].steps[1:]
    finish = manager.finish_run

    async def takeover_then_finish(serial, **fields):
        await manager.take_over_control(
            SERIAL_A, session_id=session.id, source="manual", run_id=None
        )
        return await finish(serial, **fields)

    monkeypatch.setattr(manager, "finish_run", takeover_then_finish)
    result = await run_case(
        project=selected,
        driver=AndroidCaseDriver(manager, serial=SERIAL_A, session_id=session.id, profile=profile),
        case_id="run",
        profile_id="fake",
    )
    assert result.status == "canceled"
    assert result.business_success is None
    assert manager.ledger.run_state(result.run_id) == "canceled"
    assert manager.gate.snapshot(SERIAL_A).holder_source == "manual"


async def test_fault_before_the_lease_is_never_reported_as_passed() -> None:
    """A device that fails ``begin`` must surface its terminal state, not the default pass."""
    from pulse_runtime.engine.driver import DriverFault

    class Failing:
        app_id = FakeDriver.app_id

        async def begin(self, run_id: str, *, source: str):
            raise DriverFault("interrupted", "stale_session")

        async def resolve(self, *args: object, **kwargs: object):
            raise AssertionError("unreachable")

        async def act(self, *args: object, **kwargs: object):
            raise AssertionError("unreachable")

        async def finish(self, *args: object, **kwargs: object):
            raise AssertionError("unreachable")

    result = await run_case(
        project=load_project(FIXTURE),
        driver=Failing(),  # type: ignore[arg-type]
        case_id="pass",
        profile_id="fake-android",
    )
    assert (result.status, result.reason) == ("interrupted", "stale_session")
    assert result.business_success is None
