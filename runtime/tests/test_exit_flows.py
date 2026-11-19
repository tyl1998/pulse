"""P1 exit flows: the ten target-app cases of ``fixtures/golden-cases/p1-exit``.

The project is the one an exit run uses on a real phone. Here it runs against a scripted state
machine of the target app (``exit_fixture``) so the flows themselves — locator names, action
order, expected texts — are proven consistent before any device time is spent. Nothing here is
physical evidence.
"""

# ruff: noqa: RUF001 - the target app's strings use full-width CJK punctuation.

from __future__ import annotations

from pathlib import Path

import pytest

from pulse_runtime.engine.interpreter import run_case
from pulse_schema.validation import Project, load_project, preflight
from runtime.tests.exit_fixture import ExitDriver, ExitWorld

FIXTURE = Path(__file__).parents[2] / "fixtures/golden-cases/p1-exit"

EXPECTED: dict[str, tuple[str, str | None]] = {
    "home-list": ("passed", None),
    "cart-total": ("passed", None),
    "cart-checkout": ("passed", None),
    "form-submit": ("passed", None),
    "dialog-accept": ("passed", None),
    "delayed-load": ("passed", None),
    "not-exists-banner": ("passed", None),
    "home-back": ("passed", None),
    "home-repeat-visit": ("passed", None),
    "home-fault-banner": ("passed", None),
}


def project():
    return load_project(FIXTURE)


async def run(case_id: str, world: ExitWorld | None = None) -> tuple[object, ExitDriver]:
    driver = ExitDriver(world)
    result = await run_case(
        project=project(),
        driver=driver,
        case_id=case_id,
        profile_id="target-exit",
        inputs={"who": "Pulse"} if case_id == "form-submit" else {},
    )
    return result, driver


def test_project_declares_ten_entry_flows() -> None:
    loaded = project()
    assert len(loaded.cases) == 10
    assert set(loaded.cases) == set(EXPECTED)
    assert list(loaded.manifest.entry_cases) == list(EXPECTED)


@pytest.mark.parametrize("case_id", list(EXPECTED))
def test_every_flow_is_offline_ready(case_id: str) -> None:
    loaded = project()
    profile_id = loaded.cases[case_id].profile_refs[0]
    ready = preflight(
        loaded,
        case_id,
        profile_id,
        strategy="rule_only",
        inputs={"who": "Pulse"} if case_id == "form-submit" else {},
    )
    assert ready.offline_ready, [issue.code for issue in ready.issues]


@pytest.mark.parametrize(("case_id", "expected"), list(EXPECTED.items()))
async def test_flow_reaches_expected_terminal_state(
    case_id: str, expected: tuple[str, str | None]
) -> None:
    result, driver = await run(case_id)
    assert (result.status, result.reason) == expected, result.as_dict()
    assert all(item.status == "passed" for item in result.checks), result.as_dict()
    assert driver.finished == ["passed"]


async def test_cart_flow_increments_a_quantity_and_updates_the_total() -> None:
    world = ExitWorld()
    result, driver = await run("cart-total", world)
    assert result.status == "passed"
    assert world.cart_qty[1] == 2
    # Only the two taps are writes: open cart, then plus. Everything else is a read.
    assert driver.actions == ["tap", "tap"]


async def test_cart_flow_is_idempotent_when_the_quantity_is_already_two() -> None:
    world = ExitWorld(cart_qty={1: 2, 2: 2, 3: 1})
    result, driver = await run("cart-total", world)
    assert result.status == "passed"
    assert world.cart_qty[1] == 2
    assert driver.actions == ["tap"]  # open cart only; the if-branch found qty already 2


async def test_form_flow_fills_types_and_submits() -> None:
    world = ExitWorld()
    result, _ = await run("form-submit", world)
    assert result.status == "passed"
    assert world.name == "Pulse"
    assert world.large is True
    assert world.agree is True
    assert world.submitted == "提交成功：Pulse / 大杯 / 通知关"


async def test_form_flow_reports_a_wrong_result_as_failed() -> None:
    world = ExitWorld()
    definition = project().cases["form-submit"]
    edited = definition.model_copy(
        update={
            "checks": [
                definition.checks[0].model_copy(
                    update={
                        "condition": definition.checks[0].condition.model_copy(
                            update={"expected": "提交成功：Pulse / 小杯 / 通知开"}
                        )
                    }
                )
            ]
        }
    )
    original = project()
    driver = ExitDriver(world)
    result = await run_case(
        project=Project(
            manifest=original.manifest,
            cases={**original.cases, "form-submit": edited},
            flows=original.flows,
            data=original.data,
            profiles=original.profiles,
        ),
        driver=driver,
        case_id="form-submit",
        profile_id="target-exit",
        inputs={"who": "Pulse"},
    )
    assert result.status == "failed"
    assert result.reason == "check_failed"
    assert result.business_success is False


async def test_home_list_reports_the_declared_count() -> None:
    world = ExitWorld()
    result, _ = await run("home-list", world)
    assert result.status == "passed"
    assert world.page == "list"


async def test_back_key_returns_to_home() -> None:
    world = ExitWorld()
    result, driver = await run("home-back", world)
    assert result.status == "passed"
    assert world.page == "home"
    assert "key" in driver.actions


async def test_repeat_visit_returns_home_twice() -> None:
    world = ExitWorld()
    result, _ = await run("home-repeat-visit", world)
    assert result.status == "passed"
    assert world.page == "home"
    assert world.home_visits == 3


async def test_delayed_flow_waits_for_the_loaded_state() -> None:
    world = ExitWorld(page="home")
    result, _ = await run("delayed-load", world)
    assert result.status == "passed"
    assert world.delayed_loaded is True


async def test_not_exists_flow_only_passes_when_the_node_is_absent() -> None:
    result, _ = await run("not-exists-banner", ExitWorld())
    assert result.status == "passed"
    # On home the very same locator does exist, so the flow cannot pass from the wrong page.
    assert ExitDriver(ExitWorld()).node("open-dialog") is not None
