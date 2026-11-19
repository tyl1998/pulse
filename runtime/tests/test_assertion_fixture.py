"""P1.8 golden fixture: positive and negative sample for every non-semantic operator.

Each case in ``fixtures/golden-cases/assertions`` is run through the real interpreter with the
scripted ``AssertionDriver``. The expectations below are hand-authored from the plan (§7.3),
not copied from the observed output.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from pulse_runtime.engine.interpreter import run_case
from pulse_schema.validation import load_project
from runtime.tests.assertion_fixture import AssertionDriver, AssertionWorld

FIXTURE = Path(__file__).parents[2] / "fixtures/golden-cases/assertions"
NON_SEMANTIC = (
    "exists",
    "not_exists",
    "visible",
    "enabled",
    "checked",
    "text_equals",
    "contains",
    "regex",
    "count_eq",
    "count_gt",
    "count_lt",
    "number_cmp",
    "page_is",
)


async def run(case_id: str, world: AssertionWorld | None = None):
    driver = AssertionDriver(world)
    result = await run_case(
        project=load_project(FIXTURE),
        driver=driver,
        case_id=case_id,
        profile_id="fake-android",
        run_id=str(uuid4()),
    )
    return result, driver


async def test_every_operator_passes_on_matching_observation() -> None:
    result, driver = await run("all-pass")
    assert result.status == "passed", result.as_dict()
    assert [check.id for check in result.checks] == list(NON_SEMANTIC)
    assert all(check.status == "passed" for check in result.checks)
    samples = next(check for check in result.checks if check.id == "not_exists").actual["samples"]
    assert isinstance(samples, list) and len(samples) >= 3
    number = next(check for check in result.checks if check.id == "number_cmp").actual
    assert number["unit"] == "CNY" and number["precision"] == 2 and number["value"] == 12.5
    assert driver.finished == ["passed"]


async def test_every_operator_fails_on_contradicting_observation() -> None:
    result, _ = await run("all-fail")
    assert result.status == "failed" and result.reason == "check_failed"
    assert [check.id for check in result.checks] == list(NON_SEMANTIC)
    assert all(check.status == "failed" for check in result.checks)
    assert result.business_success is False


async def test_collection_failure_blocks_every_operator_that_is_evaluated() -> None:
    # The ready wait is re-checked once and both reads succeed; the page then becomes
    # uncollectable, so every
    # remaining expectations must be blocked rather than passed or failed.
    world = AssertionWorld()
    driver = AssertionDriver(world)
    original = driver.resolve
    reads = 0

    async def flaky(target, locators, *, timeout_ms, **kwargs):
        nonlocal reads
        reads += 1
        if reads > 2:
            world.uncollectable = True
        return await original(target, locators, timeout_ms=timeout_ms, **kwargs)

    driver.resolve = flaky  # type: ignore[method-assign]
    result = await run_case(
        project=load_project(FIXTURE),
        driver=driver,
        case_id="collection-failure",
        profile_id="fake-android",
        run_id=str(uuid4()),
    )
    assert result.status == "blocked" and result.reason == "check_blocked"
    status = {check.id: check.status for check in result.checks}
    assert status == {
        "exists": "blocked",
        "count_eq": "blocked",
        "number_cmp": "blocked",
        "not_exists": "blocked",
        "text_equals": "blocked",
        "visible": "blocked",
    }
    assert result.business_success is None


async def test_not_exists_fails_as_soon_as_the_target_appears() -> None:
    world = AssertionWorld()
    world.banner = True
    result, _ = await run("all-pass", world)
    not_exists = next(check for check in result.checks if check.id == "not_exists")
    assert not_exists.status == "failed"
    samples = not_exists.actual["samples"]
    assert isinstance(samples, list) and samples[-1]["verdict"] == "false"
    assert result.status == "failed" and result.reason == "check_failed"


async def test_amount_normalization_is_exact() -> None:
    result, _ = await run("amount-normalization")
    by_id = {check.id: check for check in result.checks}
    assert by_id["cny-symbol"].status == "passed"
    assert by_id["half-up"].status == "passed", by_id["half-up"].actual
    assert by_id["half-up"].actual["raw"] == "¥0.295"
    assert by_id["percent"].status == "passed"
    assert by_id["gt"].status == "passed"
    assert by_id["foreign-currency"].status == "blocked"
    assert by_id["foreign-currency"].code == "number_unverified"
    assert result.status == "passed"


@pytest.mark.parametrize("text", ["¥0.295", "0.295元", "0.295RMB"])
async def test_half_up_ignore_of_decimal_binary_noise(text: str) -> None:
    world = AssertionWorld()
    world.set("subtotal", text)
    result, _ = await run("amount-normalization", world)
    half_up = next(check for check in result.checks if check.id == "half-up")
    assert half_up.status == "passed", half_up.actual
    assert half_up.actual["value"] == 0.3
