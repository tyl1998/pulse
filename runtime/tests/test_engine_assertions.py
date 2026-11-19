"""P1.8 assertion library: normalization units and every non-semantic operator.

The interpreter is driven by a scripted driver whose world is mutated between checkpoints, so
each checkpoint is an independent positive or negative sample. Collection failures must turn
into ``blocked``, never into ``passed`` for absence checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from pulse_runtime.engine.assertions import (
    MAX_PRECISION,
    as_decimal,
    compare,
    normalize_number,
    supported_unit,
)
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.conditions import evaluate
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_runtime.engine.interpreter import number_issues, run_case
from pulse_runtime.engine.values import Scope
from pulse_schema import models as m
from pulse_schema.validation import Project

APP = "com.pulse.target"
_PREDICATE = TypeAdapter(m.Predicate)


def locator(**expect: bool) -> m.Locator:
    body: dict[str, object] = {
        "candidates": [{"by": "res_id", "value": "com.pulse.target:id/root"}],
        "expect": {"unique": True, "visible": True} | expect,
    }
    return m.Locator.model_validate(body)


def condition(**body: object) -> m.Predicate:
    payload: dict[str, object] = {
        "target": {"locator_ref": "node"},
        "source": "native",
        **body,
    }
    return _PREDICATE.validate_python(payload)


EMPTY: Project


class World:
    """Scripted device state: callable entries produce a fresh value per read."""

    def __init__(self, **state: object) -> None:
        self.state = dict(state)

    def set(self, **state: object) -> None:
        self.state.update(state)

    def get(self, key: str, default: object = None) -> object:
        if key.endswith("_status") and key not in self.state:
            return None
        value = self.state.get(key, default)
        return value() if callable(value) and not key.endswith("_status") else value


class ScopedDriver:
    app_id = APP

    def __init__(self, world: World, *, reads: int = 1) -> None:
        self.world = world
        self.reads = reads
        self.leases: list[RunLease] = []
        self.finished: list[RunStatus] = []

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        lease = RunLease(run_id, 0)
        self.leases.append(lease)
        return lease

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        self.reads += 1
        if scope is not None:
            name = scope.locator_ref if isinstance(scope, m.LocatorRef) else "inline"
            scoped = self._resolve(name)
            if scoped.status != "resolved" or not scoped.query_complete or len(scoped.matches) != 1:
                return Resolution("indeterminate", "scope_unavailable")
        name = target.locator_ref if isinstance(target, m.LocatorRef) else "inline"
        return self._resolve(name)

    def _resolve(self, name: str) -> Resolution:
        status = self.world.get(f"{name}_status")
        if isinstance(status, str):
            return Resolution(
                str(status),  # type: ignore[arg-type]
                str(self.world.get(f"{name}_code", "source_unavailable")),
            )
        if name == "items":
            count = int(str(self.world.get("items", 0)))
            matches = tuple(Element("tree", visible=True) for _ in range(count))
            return Resolution("resolved" if count else "not_found", None, matches)
        if name == "banner":
            if not self.world.get("banner"):
                return Resolution("not_found", "no_match")
            return Resolution(
                "resolved",
                None,
                (Element(str(self.world.get("banner_source", "tree")), "Banner", True),),
            )
        if name == "target":
            if bool(self.world.get("target")):
                source = str(self.world.get("banner_source", "tree"))
                return Resolution("resolved", None, (Element(source, "Target", True),))
            return Resolution("not_found", "no_match")
        if name == "two":
            return Resolution(
                "ambiguous", "multiple_matches", (Element("tree", "a"), Element("tree", "b"))
            )
        text = self.world.get("text")
        source = str(self.world.get("source", "tree"))
        return Resolution(
            "resolved",
            None,
            (
                Element(
                    source,
                    text=text if type(text) is str else None,
                    visible=self.world.get("visible", True),  # type: ignore[arg-type]
                    states={
                        "enabled": self.world.get("enabled"),  # type: ignore[dict-item]
                        "checked": self.world.get("checked"),  # type: ignore[dict-item]
                    },
                ),
            ),
        )

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        return ActionResult("completed", None, str(uuid4()), str(uuid4()))

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        self.finished.append(state)
        return {"status": "cleaned"}


async def verdict(
    predicate: m.Predicate, world: World, *, budget: float = 5.0
) -> tuple[str, str | None, dict[str, object]]:
    result = await evaluate(
        predicate,
        driver=ScopedDriver(world),
        locators={"node": locator(unique=False)},
        deadline=Deadline.after(budget),
        project=EMPTY,
        scope=Scope(),
        secrets=None,
    )
    return result.verdict, result.code, result.actual


@pytest.fixture(scope="module", autouse=True)
def _project() -> Project:
    global EMPTY
    EMPTY = Project(
        m.Manifest.model_validate(
            {
                "$schema": "urn:pulse:schema:1-draft:manifest",
                "project_id": "p18",
                "name": "P1.8",
                "revision": "1",
                "cases": {"p18": "cases/p18.json"},
                "flows": {},
                "data": {},
                "profiles": {},
                "entry_cases": ["p18"],
                "default_strategy": "rule_only",
                "platforms": ["android"],
                "required_capabilities": [],
                "constraints": {"allowed_apps": [APP], "forbidden_actions": []},
                "dependencies": [],
            }
        ),
        {},
        {},
        {},
        {},
    )
    return EMPTY


class TestNormalization:
    @pytest.mark.parametrize(
        ("text", "unit", "precision", "value"),
        [
            ("12.50", "CNY", 2, "12.50"),
            ("¥12.5", "CNY", 2, "12.50"),
            ("￥ 12.5", "CNY", 2, "12.50"),
            ("-¥3.456", "CNY", 2, "-3.46"),
            ("¥-3.456", "CNY", 2, "-3.46"),
            ("1,234.5元", "CNY", 2, "1234.50"),
            ("12.5RMB", "CNY", 2, "12.50"),
            ("2.5人民币", "CNY", 2, None),
            ("12.5 USD", "CNY", 2, None),
            ("12.5%", "CNY", 2, None),
            ("85", "%", 0, "85"),
            ("85%", "%", 0, "85"),
            ("%85", "%", 0, "85"),
            ("8.5折", "折", 1, "8.5"),
            ("5分钟", "分钟", 0, "5"),
            ("5min", "分钟", 0, "5"),
            ("5", "分钟", 0, "5"),
            ("1.5h", "h", 1, "1.5"),
            ("５分钟", "分钟", 0, "5"),
            (" 12.5\u00a0", "CNY", 2, "12.50"),
            ("12.5", "元", 2, "12.50"),
            ("Coffee", "CNY", 2, None),
            ("1.2.3", "CNY", 2, None),
            ("12.5 13.5", "CNY", 2, None),
            ("", "CNY", 2, None),
            ("12.5", "parsec", 2, None),
            ("12.5", "CNY", MAX_PRECISION + 1, None),
        ],
    )
    def test_normalize_number(
        self, text: str, unit: str, precision: int, value: str | None
    ) -> None:
        quantity = normalize_number(text, unit=unit, precision=precision)
        assert (None if quantity is None else str(quantity.value)) == value

    def test_half_up_rounding_and_expected_rounding(self) -> None:
        quantity = normalize_number("12.345", unit="CNY", precision=2)
        assert quantity is not None and quantity.value == Decimal("12.35")
        assert compare(quantity, Decimal("12.35"), "eq")
        assert compare(quantity, Decimal("12.3451"), "eq")  # expected rounds too
        assert not compare(quantity, Decimal("12.34"), "eq")
        assert compare(quantity, Decimal("12.35"), "gte")
        assert compare(quantity, Decimal("12.4"), "lt")
        assert compare(quantity, Decimal("12.35"), "lte")
        assert compare(quantity, Decimal("12.36"), "ne")
        assert compare(quantity, Decimal("12.35"), "ne") is False

    @pytest.mark.parametrize("unit", ["CNY", "元", "RMB", "cny", "%", "折", "分钟", "min", "h"])
    def test_supported_units(self, unit: str) -> None:
        assert supported_unit(unit)

    @pytest.mark.parametrize("unit", ["parsec", "", "美元"])
    def test_unsupported_units(self, unit: str) -> None:
        assert not supported_unit(unit)

    def test_as_decimal_rejects_booleans(self) -> None:
        assert as_decimal(True) is None


class TestOperators:
    async def test_exists(self) -> None:
        assert (await verdict(condition(operator="exists"), World()))[0] == "true"
        assert (await verdict(condition(operator="exists"), World(text=None)))[0] == "true"
        world = World(node_status="not_found")
        assert (await verdict(condition(operator="exists"), world))[0] == "false"

    async def test_exists_collection_failure(self) -> None:
        world = World(node_status="indeterminate", node_code="observation_unavailable")
        got = await verdict(condition(operator="exists"), world)
        assert got[0] == "unknown" and got[1] == "observation_unavailable"

    async def test_page_is(self) -> None:
        assert (await verdict(condition(operator="page_is"), World()))[0] == "true"
        world = World(node_status="not_found")
        assert (await verdict(condition(operator="page_is"), world))[0] == "false"

    async def test_visible(self) -> None:
        assert (await verdict(condition(operator="visible", expected=True), World()))[0] == "true"
        world = World(visible=False)
        assert (await verdict(condition(operator="visible", expected=True), world))[0] == "false"
        assert (await verdict(condition(operator="visible", expected=False), world))[0] == "true"

    async def test_visible_unverified(self) -> None:
        world = World(visible=None)
        assert (await verdict(condition(operator="visible", expected=True), world))[0] == "unknown"

    async def test_enabled(self) -> None:
        assert (await verdict(condition(operator="enabled", expected=True), World(enabled=True)))[
            0
        ] == "true"
        assert (await verdict(condition(operator="enabled", expected=True), World(enabled=False)))[
            0
        ] == "false"
        got = await verdict(condition(operator="enabled", expected=True), World(enabled=None))
        assert got[0] == "unknown" and got[1] == "attribute_unverified"

    async def test_checked(self) -> None:
        assert (await verdict(condition(operator="checked", expected=True), World(checked=True)))[
            0
        ] == "true"
        assert (await verdict(condition(operator="checked", expected=True), World(checked=False)))[
            0
        ] == "false"
        assert (await verdict(condition(operator="checked", expected=False), World(checked=False)))[
            0
        ] == "true"

    async def test_state_operators_need_one_element(self) -> None:
        world = World(visible=True)
        got = await verdict(
            condition(operator="visible", expected=True, target={"locator_ref": "two"}), world
        )
        assert got[0] == "unknown" and got[1] == "ambiguous_target"

    async def test_text_equals(self) -> None:
        assert (
            await verdict(
                condition(operator="text_equals", expected="Coffee"), World(text="Coffee")
            )
        )[0] == "true"
        assert (
            await verdict(condition(operator="text_equals", expected="Coffee"), World(text="Tea"))
        )[0] == "false"

    async def test_text_equals_empty_actual(self) -> None:
        assert (await verdict(condition(operator="text_equals", expected=""), World(text="")))[
            0
        ] == "true"
        got = await verdict(condition(operator="text_equals", expected=""), World(text=None))
        assert got[0] == "unknown" and got[1] == "text_unverified"

    async def test_contains(self) -> None:
        assert (
            await verdict(condition(operator="contains", expected="off"), World(text="Coffee"))
        )[0] == "true"
        assert (
            await verdict(condition(operator="contains", expected="Tea"), World(text="Coffee"))
        )[0] == "false"

    async def test_regex(self) -> None:
        assert (await verdict(condition(operator="regex", expected="^Cof"), World(text="Coffee")))[
            0
        ] == "true"
        assert (
            await verdict(condition(operator="regex", expected="^Coffee$"), World(text="Coffee "))
        )[0] == "false"
        got = await verdict(condition(operator="regex", expected="("), World(text="Coffee"))
        assert got[0] == "unknown" and got[1] == "regex_invalid"

    @pytest.mark.parametrize(
        ("operator", "expected", "count", "want"),
        [
            ("count_eq", 1, 1, "true"),
            ("count_eq", 1, 2, "false"),
            ("count_gt", 1, 2, "true"),
            ("count_gt", 1, 1, "false"),
            ("count_lt", 2, 1, "true"),
            ("count_lt", 2, 2, "false"),
        ],
    )
    async def test_count(self, operator: str, expected: int, count: int, want: str) -> None:
        world = World(items=count)
        got = await verdict(
            condition(operator=operator, expected=expected, target={"locator_ref": "items"}), world
        )
        assert got[0] == want

    async def test_count_collection_failure(self) -> None:
        world = World(items_status="indeterminate")
        got = await verdict(
            condition(operator="count_eq", expected=0, target={"locator_ref": "items"}), world
        )
        assert got[0] == "unknown"

    @pytest.mark.parametrize(
        ("comparison", "text", "expected", "want"),
        [
            ("eq", "¥12.50", 12.5, "true"),
            ("eq", "¥13.50", 12.5, "false"),
            ("ne", "¥13.50", 12.5, "true"),
            ("gt", "¥13.50", 12.5, "true"),
            ("gt", "¥12.50", 12.5, "false"),
            ("gte", "¥12.50", 12.5, "true"),
            ("lt", "1,234.5元", 1234.6, "true"),
            ("lt", "¥12.50", 12.5, "false"),
            ("lte", "¥12.50", 12.5, "true"),
            ("eq", "¥12.495", 12.5, "true"),
            ("eq", "￥12.5", 12.5, "true"),
        ],
    )
    async def test_number_cmp(self, comparison: str, text: str, expected: float, want: str) -> None:
        world = World(text=text)
        got = await verdict(
            condition(
                operator="number_cmp",
                comparison=comparison,
                expected=expected,
                unit="CNY",
                precision=2,
            ),
            world,
        )
        assert got[0] == want, got
        assert got[2]["unit"] == "CNY" and got[2]["precision"] == 2
        assert got[2]["raw"] == text

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("Coffee", "number_unverified"),
            ("12.5 USD", "number_unverified"),
            ("12.5%", "number_unverified"),
            ("1.2.3", "number_unverified"),
            (None, "text_unverified"),
        ],
    )
    async def test_number_cmp_unverified(self, text: object, code: str) -> None:
        world = World(text=text)
        got = await verdict(
            condition(
                operator="number_cmp",
                comparison="eq",
                expected=12.5,
                unit="CNY",
                precision=2,
            ),
            world,
        )
        assert got[0] == "unknown" and got[1] == code

    async def test_number_cmp_vision_source_has_no_text(self) -> None:
        world = World(text="¥12.50", source="screenshot")
        got = await verdict(
            condition(
                operator="number_cmp",
                source="vision",
                comparison="eq",
                expected=12.5,
                unit="CNY",
                precision=2,
            ),
            world,
        )
        assert got[0] == "unknown" and got[1] == "text_unverified"

    async def test_number_cmp_collection_failure(self) -> None:
        world = World(node_status="indeterminate")
        got = await verdict(
            condition(
                operator="number_cmp",
                comparison="eq",
                expected=12.5,
                unit="CNY",
                precision=2,
            ),
            world,
        )
        assert got[0] == "unknown"

    async def test_semantic_stays_out_of_scope(self) -> None:
        got = await verdict(
            condition(operator="semantic", source="vision", expected="cart ok", timeout_ms=1000),
            World(),
        )
        assert got[0] == "unknown" and got[1] == "model_required"


def absence(target: str = "target", **body: object) -> m.Predicate:
    return condition(
        target={"locator_ref": target},
        operator="not_exists",
        scope={"locator_ref": "node"},
        observation_window_ms=300,
        poll_interval_ms=100,
        **body,
    )


class TestNotExists:
    async def test_absent_for_whole_window(self) -> None:
        got = await verdict(absence(), World(target_status="not_found"))
        assert got[0] == "true"
        samples = got[2]["samples"]
        assert isinstance(samples, list) and len(samples) >= 3
        assert all(item["verdict"] == "true" for item in samples)

    async def test_present_at_any_sample(self) -> None:
        got = await verdict(absence(), World(target=lambda: world_seen()))
        assert got[0] == "false"

    async def test_collection_failure_is_not_a_pass(self) -> None:
        got = await verdict(absence(), World(node_status="indeterminate"))
        assert got[0] == "unknown" and got[1] == "scope_unavailable"
        world = World(target_status="indeterminate", target_code="observation_unavailable")
        got = await verdict(absence(), world)
        assert got[0] == "unknown" and got[1] == "observation_unavailable"
        got = await verdict(absence(target="banner"), World(banner_status="indeterminate"))
        assert got[0] == "unknown" and got[1] == "source_unavailable"

    async def test_source_mismatch_is_unknown(self) -> None:
        got = await verdict(absence(), World(target=True, banner_source="screenshot"))
        assert got[0] == "unknown" and got[1] == "source_mismatch"

    async def test_window_must_fit_the_deadline(self) -> None:
        got = await verdict(absence(), World(), budget=0.1)
        assert got[0] == "unknown" and got[1] == "observation_window_exceeded"


_seen = 0


def world_seen() -> bool:
    global _seen
    _seen += 1
    return _seen >= 3


READY_STEP: dict[str, object] = {
    "type": "wait",
    "id": "ready",
    "condition": {"operator": "exists", "target": {"locator_ref": "node"}, "source": "native"},
}


def project_with(
    checks: list[dict[str, object]], *, steps: list[dict[str, object]] | None = None
) -> Project:
    case = m.Case.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:case",
            "case_id": "p18",
            "name": "P1.8",
            "goal": "Assertions",
            "parameters": {},
            "profile_refs": ["fake"],
            "inputs": {},
            "locators": {},
            "preconditions": [],
            "steps": [READY_STEP, *(steps or [])],
            "checks": checks,
        }
    )
    profile = m.Profile.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:profile",
            "profile_id": "fake",
            "platform": "android",
            "environment": "test",
            "app_id": APP,
            "capabilities": ["observe"],
            "locators": {
                "items": {
                    "candidates": [{"by": "res_id", "value": "com.pulse.target:id/item"}],
                    "expect": {"unique": False, "visible": False},
                },
                "banner": {
                    "candidates": [{"by": "text", "value": "Banner"}],
                    "expect": {"unique": False, "visible": True},
                },
                "absent": {
                    "candidates": [{"by": "res_id", "value": "com.pulse.target:id/absent"}],
                    "expect": {"unique": True, "visible": True},
                },
                "node": {
                    "candidates": [{"by": "res_id", "value": "com.pulse.target:id/root"}],
                    "expect": {"unique": False, "visible": True},
                },
            },
        }
    )
    manifest = m.Manifest.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:manifest",
            "project_id": "p18",
            "name": "P1.8",
            "revision": "1",
            "cases": {"p18": "cases/p18.json"},
            "flows": {},
            "data": {},
            "profiles": {"fake": "profiles/fake.json"},
            "entry_cases": ["p18"],
            "default_strategy": "rule_only",
            "platforms": ["android"],
            "required_capabilities": [],
            "constraints": {"allowed_apps": [APP], "forbidden_actions": []},
            "dependencies": [],
            "budgets": {"case_timeout_ms": 2000},
        }
    )
    return Project(manifest, {"p18": case}, {}, {}, {"fake": profile})


def check_body(operator: str, **body: object) -> dict[str, object]:
    return {
        "id": f"{operator}-check",
        "required": True,
        "condition": {
            "target": {"locator_ref": "node"},
            "source": "native",
            "operator": operator,
            **body,
        },
    }


class TestInterpreterIntegration:
    async def test_each_operator_reaches_its_terminal_status(self) -> None:
        positives = [
            check_body("exists"),
            check_body("visible", expected=True),
            check_body("enabled", expected=True),
            check_body("checked", expected=False),
            check_body("text_equals", expected="¥12.50"),
            check_body("contains", expected="12.5"),
            check_body("regex", expected="^¥12"),
            check_body("count_eq", expected=1, target={"locator_ref": "items"}),
            check_body("count_gt", expected=0, target={"locator_ref": "items"}),
            check_body("count_lt", expected=2, target={"locator_ref": "items"}),
            check_body("number_cmp", comparison="eq", expected=12.5, unit="CNY", precision=2),
            check_body("page_is"),
            check_body(
                "not_exists",
                target={"locator_ref": "banner"},
                scope={"locator_ref": "node"},
                observation_window_ms=200,
                poll_interval_ms=100,
            ),
        ]
        world = World(text="¥12.50", enabled=True, checked=False, items=1, banner=False)
        driver = ScopedDriver(world)
        result = await run_case(
            project=project_with(positives),
            driver=driver,
            case_id="p18",
            profile_id="fake",
            run_id=str(uuid4()),
        )
        assert result.status == "passed", result.as_dict()
        assert len(result.checks) == len(positives)
        assert all(item.status == "passed" for item in result.checks)
        assert driver.finished == ["passed"]

        negatives = [
            check_body("exists", target={"locator_ref": "absent"}),
            check_body("visible", expected=False),
            check_body("enabled", expected=False),
            check_body("checked", expected=True),
            check_body("text_equals", expected="Coffee"),
            check_body("contains", expected="Coffee"),
            check_body("regex", expected="^Coffee$"),
            check_body("count_eq", expected=3, target={"locator_ref": "items"}),
            check_body("count_gt", expected=5, target={"locator_ref": "items"}),
            check_body("count_lt", expected=0, target={"locator_ref": "items"}),
            check_body("number_cmp", comparison="eq", expected=13.5, unit="CNY", precision=2),
            check_body("page_is", target={"locator_ref": "absent"}),
            check_body(
                "not_exists",
                target={"locator_ref": "banner"},
                scope={"locator_ref": "node"},
                observation_window_ms=200,
                poll_interval_ms=100,
            ),
        ]
        run = await run_case(
            project=project_with(negatives),
            driver=ScopedDriver(
                World(
                    text="¥12.50",
                    enabled=True,
                    checked=False,
                    items=1,
                    banner=True,
                    absent_status="not_found",
                )
            ),
            case_id="p18",
            profile_id="fake",
            run_id=str(uuid4()),
        )
        assert run.status == "failed" and run.reason == "check_failed"
        assert [item.status for item in run.checks] == ["failed"] * len(negatives)

    async def test_collection_failure_blocks_instead_of_failing(self) -> None:
        number = check_body(
            "number_cmp",
            comparison="eq",
            expected=12.5,
            unit="CNY",
            precision=2,
            target={"locator_ref": "items"},
        )
        # Read order: ready, items, first absence sample (scope + target), then one page read
        # per later sample; degradation starts at the second sample.
        absence = check_body(
            "not_exists",
            target={"locator_ref": "banner"},
            scope={"locator_ref": "node"},
            observation_window_ms=200,
            poll_interval_ms=100,
        )
        # The page is healthy for READY_STEP; each later check then meets a page that can no
        # longer be collected and must block instead of failing.
        world = World()
        driver = ScopedDriver(world)
        original = driver._resolve
        reads = 0
        degrade = False

        def flaky(name: str) -> Resolution:
            nonlocal reads, degrade
            reads += 1
            if degrade:
                return Resolution("indeterminate", "source_unavailable")
            if reads == 4:
                degrade = True
            return original(name)

        driver._resolve = flaky  # type: ignore[method-assign]
        run = await run_case(
            project=project_with([number, absence]),
            driver=driver,
            case_id="p18",
            profile_id="fake",
            run_id=str(uuid4()),
        )
        assert run.status == "blocked" and run.reason == "check_blocked"
        assert [item.status for item in run.checks] == ["blocked", "blocked"]
        assert run.business_success is None

    async def test_unsupported_unit_is_refused_before_any_read(self) -> None:
        driver = ScopedDriver(World(), reads=0)
        run = await run_case(
            project=project_with(
                [check_body("number_cmp", comparison="eq", expected=1, unit="parsec", precision=0)]
            ),
            driver=driver,
            case_id="p18",
            profile_id="fake",
            run_id=str(uuid4()),
        )
        assert run.status == "blocked" and run.reason == "number_unit_unsupported"
        assert run.preflight_issues[0]["code"] == "number_unit_unsupported"
        assert driver.reads == 0  # refused before the device is even leased
        assert driver.leases == []

    @pytest.mark.parametrize("unit", ["杯", "元", "CNY", "%"])
    def test_display_units_are_accepted(self, unit: str) -> None:
        project = project_with(
            [check_body("number_cmp", comparison="gte", expected=1, unit=unit, precision=0)]
        )
        assert number_issues(project.cases["p18"]) == []

    def test_unverifiable_currency_declaration_is_refused(self) -> None:
        project = project_with(
            [check_body("number_cmp", comparison="gte", expected=1, unit="美元", precision=0)]
        )
        assert [item["code"] for item in number_issues(project.cases["p18"])] == [
            "number_unit_unsupported"
        ]

    def test_number_issues_scan_nested_steps(self) -> None:
        nested = {
            "type": "if",
            "id": "branch",
            "condition": {
                "operator": "exists",
                "target": {"locator_ref": "node"},
                "source": "native",
            },
            "then": [
                {
                    "type": "wait",
                    "id": "wait",
                    "condition": {
                        "operator": "number_cmp",
                        "target": {"locator_ref": "node"},
                        "source": "native",
                        "comparison": "gt",
                        "expected": 1,
                        "unit": "parsec",
                        "precision": 2,
                    },
                }
            ],
        }
        project = project_with([], steps=[nested])
        issues = number_issues(project.cases["p18"])
        assert [item["code"] for item in issues] == ["number_unit_unsupported"]
        precise = dict(nested)
        precise["then"] = [
            {
                "type": "wait",
                "id": "wait",
                "condition": {
                    "operator": "number_cmp",
                    "target": {"locator_ref": "node"},
                    "source": "native",
                    "comparison": "gt",
                    "expected": 1,
                    "unit": "CNY",
                    "precision": MAX_PRECISION + 1,
                },
            }
        ]
        issues = number_issues(project_with([], steps=[precise]).cases["p18"])
        assert [item["code"] for item in issues] == ["number_precision_unsupported"]


@pytest.mark.parametrize("nested", [False, True])
async def test_observed_variable_cannot_replace_same_named_parameter(nested: bool) -> None:
    extracted = {
        "type": "extract",
        "id": "extract-wanted",
        "target": {"locator_ref": "node"},
        "source": "native",
        "attribute": "text",
        "as": "wanted",
    }
    checked = {
        "type": "check",
        "id": "verify-wanted",
        "checkpoint": check_body("text_equals", expected={"param_ref": "wanted"}),
    }
    steps = [extracted, checked]
    if nested:
        steps = [
            {
                "type": "if",
                "id": "branch",
                "condition": READY_STEP["condition"],
                "then": steps,
            }
        ]
    selected = project_with([], steps=steps)
    case = selected.cases["p18"]
    case.parameters["wanted"] = m.Parameter(type="string", required=True)
    case.inputs["wanted"] = "EXPECTED"
    result = await run_case(
        project=selected,
        driver=ScopedDriver(World(text="WRONG")),
        case_id="p18",
        profile_id="fake",
    )
    assert result.status == "failed"
    assert result.business_success is False


@pytest.mark.parametrize("operator", ["exists", "page_is"])
@pytest.mark.parametrize("status", ["ambiguous", "indeterminate"])
async def test_presence_does_not_accept_unverified_matches(operator: str, status: str) -> None:
    driver = ScopedDriver(World())
    driver._resolve = lambda name: Resolution(  # type: ignore[method-assign]
        status,
        "source_incomplete",
        (Element("tree"), Element("tree")),  # type: ignore[arg-type]
    )
    result = await evaluate(
        condition(operator=operator),
        driver=driver,
        locators={},
        deadline=Deadline.after(1),
        project=EMPTY,
        scope=Scope(),
        secrets=None,
    )
    assert result.verdict == "unknown"


@pytest.mark.parametrize("operator", ["count_eq", "count_gt", "count_lt"])
async def test_partial_collection_cannot_establish_exact_count(operator: str) -> None:
    driver = ScopedDriver(World())
    driver._resolve = lambda name: Resolution(  # type: ignore[method-assign]
        "resolved", None, (Element("tree"),), query_complete=False
    )
    result = await evaluate(
        condition(operator=operator, expected=1),
        driver=driver,
        locators={},
        deadline=Deadline.after(1),
        project=EMPTY,
        scope=Scope(),
        secrets=None,
    )
    assert result.verdict == "unknown"


async def test_wait_unknown_is_blocked_not_business_failure() -> None:
    result = await run_case(
        project=project_with([]),
        driver=ScopedDriver(World(node_status="indeterminate")),
        case_id="p18",
        profile_id="fake",
    )
    assert result.status == "blocked"
    assert result.business_success is None


@pytest.mark.parametrize("field,value", [("unit", "parsec"), ("precision", 7)])
async def test_numeric_preflight_scans_called_flows_before_actions(
    field: str, value: object
) -> None:
    selected = project_with([], steps=[{"type": "flow", "id": "call", "flow_ref": "numeric"}])
    numeric = check_body("number_cmp", comparison="eq", expected=1, unit="CNY", precision=2)
    numeric["condition"][field] = value
    selected.flows["numeric"] = m.Flow.model_validate(
        {
            "$schema": "urn:pulse:schema:1-draft:flow",
            "flow_id": "numeric",
            "name": "Numeric flow",
            "parameters": {},
            "locators": {},
            "steps": [
                {"type": "action", "id": "write", "action": {"kind": "key", "key": "BACK"}},
                {"type": "check", "id": "compare", "checkpoint": numeric},
            ],
            "returns": {},
        }
    )
    selected.manifest.flows["numeric"] = "flows/numeric.json"
    driver = ScopedDriver(World(text="1"), reads=0)
    result = await run_case(project=selected, driver=driver, case_id="p18", profile_id="fake")
    assert result.status == "blocked"
    assert result.reason == (
        "number_unit_unsupported" if field == "unit" else "number_precision_unsupported"
    )
    assert driver.reads == 0
    assert driver.leases == []


@pytest.mark.parametrize("text,expected", [("9" * 100, 1), ("1", 1e100)])
async def test_numeric_overflow_returns_blocked(text: str, expected: int | float) -> None:
    driver = ScopedDriver(World(text=text))
    result = await run_case(
        project=project_with(
            [check_body("number_cmp", comparison="eq", expected=expected, unit="CNY", precision=2)]
        ),
        driver=driver,
        case_id="p18",
        profile_id="fake",
    )
    assert result.status == "blocked"
    assert result.checks[0].code == "number_unverified"
    assert driver.finished == ["blocked"]


@pytest.mark.parametrize("kind", ["condition", "extract", "absence"])
async def test_each_read_obeys_its_own_deadline(kind: str) -> None:
    entered = asyncio.Event()

    class HangingDriver(ScopedDriver):
        async def resolve(self, target, locators, *, timeout_ms, scope=None, primary=None):
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    driver = HangingDriver(World())
    if kind == "extract":
        selected = project_with([])
        selected.cases["p18"].steps = TypeAdapter(list[m.Step]).validate_python(
            [
                {
                    "type": "extract",
                    "id": "read",
                    "timeout_ms": 30,
                    "target": {"locator_ref": "node"},
                    "source": "native",
                    "attribute": "text",
                    "as": "text",
                }
            ]
        )
        result = await asyncio.wait_for(
            run_case(project=selected, driver=driver, case_id="p18", profile_id="fake"), 1
        )
        assert result.status == "blocked"
    else:
        predicate = (
            condition(operator="exists", timeout_ms=30)
            if kind == "condition"
            else condition(
                operator="not_exists",
                scope={"locator_ref": "node"},
                observation_window_ms=30,
                poll_interval_ms=10,
            )
        )
        result = await asyncio.wait_for(
            evaluate(
                predicate,
                driver=driver,
                locators={},
                deadline=Deadline.after(5),
                project=EMPTY,
                scope=Scope(),
                secrets=None,
            ),
            1,
        )
        assert result.verdict == "unknown"
    assert entered.is_set()
