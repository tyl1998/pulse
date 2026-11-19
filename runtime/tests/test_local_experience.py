"""P1.11 local experience v0: bounded store, live verification and speed-only semantics.

No phone, Agent, ADB, Runtime HTTP endpoint or background process is contacted. The scripted
driver resolves the *effective* locator, so the tests separate two facts: what the store
remembers, and what the case still authorises (the declared candidate order).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from pulse_runtime.engine.android_driver import AndroidCaseDriver
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
)
from pulse_runtime.engine.interpreter import (
    ExperienceKey,
    _Context,
    _experience_hit,
    _page_anchor,
    run_case,
)
from pulse_runtime.experience.local import (
    CONTRACT,
    MAX_ENTRIES,
    ExperiencedCandidate,
    ExperienceRecord,
    LocalExperience,
    declared_candidate,
    experience_key,
    recalled_candidate,
    reordered_target,
)
from pulse_runtime.perception.locators import resolve_locator
from pulse_schema import models as m
from pulse_schema.validation import Project, load_project, validate_semantics
from runtime.tests.experience_fixture import (
    FIXTURE,
    ExperienceDriver,
    ExperienceWorld,
    expected,
    world_for,
)
from runtime.tests.test_locators import observation

KEY = ExperienceKey("experience-golden", "com.pulse.target", "exists:page", "search", "search")
SEARCH = "com.pulse.target:id/search"
TEXT = "text:Search"


def page_key(case_id: str, target: str) -> ExperienceKey:
    return ExperienceKey("experience-golden", "com.pulse.target", "exists:page", case_id, target)


def summarise(result: object) -> list[dict[str, object]]:
    """Verdict-relevant report fields; durations and IDs are expected to differ per run."""
    return [
        {
            "check": item.id,
            "status": item.status,
            "required": item.required,
            "code": item.code,
            "expected": item.expected,
            "actual": item.actual,
        }
        for item in result.checks  # type: ignore[attr-defined]
    ] + [
        {"step": item.id, "status": item.status, "code": item.code}
        for item in result.steps  # type: ignore[attr-defined]
    ]


async def run(
    case_id: str,
    *,
    world: ExperienceWorld | None = None,
    store: object | None = None,
) -> tuple[object, ExperienceDriver]:
    driver = ExperienceDriver(world or world_for(case_id))
    result = await run_case(
        project=load_project(FIXTURE),
        driver=driver,
        case_id=case_id,
        profile_id="fake-android",
        experience=store,  # type: ignore[arg-type]
    )
    return result, driver


def test_fixture_project_loads() -> None:
    project = load_project(FIXTURE)
    assert sorted(project.cases) == ["ambiguous", "broken-remembered", "drifted", "search"]
    assert project.profiles["fake-android"].app_id == "com.pulse.target"


def test_key_is_stable_and_scoped() -> None:
    base = {"project_id": "p", "app_id": "a", "page": "page", "intent": "case"}
    first = experience_key(**base, target="search")
    assert first.hash() == experience_key(**base, target="search").hash()
    assert len(first.hash()) == 24
    variants = [
        {**base, "target": "search"},
        {**base, "project_id": "q", "target": "search"},
        {**base, "app_id": "b", "target": "search"},
        {**base, "page": "other", "target": "search"},
        {**base, "intent": "other", "target": "search"},
        {**base, "target": "other"},
    ]
    assert len({experience_key(**item).hash() for item in variants}) == len(variants)


def test_page_anchor_requires_a_required_precondition() -> None:
    project = load_project(FIXTURE)
    assert _page_anchor(project.cases["search"]) == "exists:page"
    case = project.cases["search"].model_copy(update={"preconditions": []})
    assert _page_anchor(case) is None


def test_store_roundtrip_is_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "locator-experience.json"
    store = LocalExperience(path)
    assert store.lookup(KEY) is None
    store.remember(KEY, ExperiencedCandidate("res_id", SEARCH))
    store.save()
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "nested").stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = json.loads(path.read_text("utf-8"))
    assert document["contract"] == CONTRACT
    assert document["entries"][0]["candidate"] == {"by": "res_id", "value": SEARCH, "scope": None}
    reloaded = LocalExperience(path)
    assert reloaded.lookup(KEY) == ExperiencedCandidate("res_id", SEARCH)
    reloaded.remember(KEY, ExperiencedCandidate("res_id", SEARCH))
    assert reloaded.records[KEY.hash()].hits == 2


def test_store_drops_stale_entry(tmp_path: Path) -> None:
    path = tmp_path / "locator-experience.json"
    store = LocalExperience(path)
    store.remember(KEY, ExperiencedCandidate("text", "Search"))
    store.remember(KEY, None)
    assert store.lookup(KEY) is None
    store.save()
    assert json.loads(path.read_text("utf-8"))["entries"] == []


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "locator-experience.json"
    path.write_text("{not json", encoding="utf-8")
    store = LocalExperience(path)
    assert store.broken
    store.remember(KEY, ExperiencedCandidate("text", "Search"))
    store.save()
    assert path.read_text(encoding="utf-8") == "{not json"


def test_store_drops_only_malformed_entries(tmp_path: Path) -> None:
    good = ExperienceRecord(
        key=KEY.hash(),
        scope=KEY,
        candidate=ExperiencedCandidate("text", "Search"),
        hits=1,
        misses=0,
        created_at_ms=1,
        updated_at_ms=1,
    ).as_json()
    document = {"contract": CONTRACT, "version": 0, "entries": [good, {"key": "x"}]}
    path = tmp_path / "locator-experience.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert list(LocalExperience(path).records) == [KEY.hash()]


def test_store_rejects_unknown_candidate_kind() -> None:
    payload = ExperienceRecord(
        key=KEY.hash(),
        scope=KEY,
        candidate=ExperiencedCandidate("text", "Search"),
        hits=1,
        misses=0,
        created_at_ms=1,
        updated_at_ms=1,
    ).as_json()
    payload["candidate"] = {"by": "point", "value": "x", "scope": None}
    assert ExperienceRecord.from_json(payload) is None
    payload["key"] = "0" * 24
    assert ExperienceRecord.from_json(payload) is None


def test_store_evicts_oldest_entry(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    for index in range(MAX_ENTRIES):
        store.remember(
            ExperienceKey("p", "a", "page", "case", f"target-{index}"),
            ExperiencedCandidate("text", "Search"),
        )
    assert len(store.records) == MAX_ENTRIES
    extra = ExperienceKey("p", "a", "page", "case", "extra")
    store.remember(extra, ExperiencedCandidate("text", "Search"))
    assert len(store.records) == MAX_ENTRIES
    assert extra.hash() in store.records


def test_declared_and_recalled_candidates() -> None:
    locators = load_project(FIXTURE).profiles["fake-android"].locators
    target = m.LocatorRef(locator_ref="search")
    first = declared_candidate(target, locators)
    assert first is not None and first.by == "text"
    assert recalled_candidate(target, locators, ExperiencedCandidate("res_id", SEARCH)) is not None
    assert recalled_candidate(target, locators, ExperiencedCandidate("text", "Missing")) is None
    assert declared_candidate(m.LocatorRef(locator_ref="absent"), locators) is None


def test_reordered_target_keeps_the_candidate_set() -> None:
    locator = load_project(FIXTURE).profiles["fake-android"].locators["search"]
    primary = locator.candidates[1]
    target = reordered_target(m.InlineLocator(locator=locator), primary)
    assert isinstance(target, m.InlineLocator)
    assert target.locator.candidates[0] is primary
    assert [item.value for item in target.locator.candidates] == [
        locator.candidates[1].value,
        locator.candidates[0].value,
        locator.candidates[2].value,
    ]
    assert target.locator.expect == locator.expect
    with pytest.raises(ValueError):
        reordered_target(m.LocatorRef(locator_ref="search"), primary)


async def test_declared_order_is_never_overridden(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    store.remember(KEY, ExperiencedCandidate("res_id", SEARCH))
    result, driver = await run("search", store=store)
    assert result.status == "passed", result.as_dict()
    # v0 never reorders: no hint reaches the driver and the declared first candidate is tried.
    assert all(item is None for item in driver.primaries)
    assert driver.action_primaries[0] is None
    assert tuple(driver.action_queries[0][0].split(":", 1)) == ("text", "Search")


async def test_no_store_means_no_hint() -> None:
    result, driver = await run("search")
    assert result.status == "passed"
    assert result.reason is None
    assert all(item is None for item in driver.primaries)


async def test_a_stale_store_changes_latency_only(tmp_path: Path) -> None:
    path = tmp_path / "locator-experience.json"
    store = LocalExperience(path)
    store.remember(KEY, ExperiencedCandidate("desc", "Removed label"))
    store.save()
    cached, cached_driver = await run("search", store=LocalExperience(path))
    path.unlink()
    plain, plain_driver = await run("search", store=LocalExperience(path))
    assert cached.status == plain.status == "passed"
    assert cached.reason == plain.reason is None
    assert summarise(cached) == summarise(plain)
    assert cached_driver.world.attempts == plain_driver.world.attempts


async def test_fallback_is_remembered_only_after_a_live_confirmation(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    world = ExperienceWorld(candidates=["search"])
    result, driver = await run("search", world=world, store=store)
    assert result.status == "passed", result.as_dict()
    assert all(item is None for item in driver.primaries)
    attempts = [
        item for item in driver.world.attempts if item not in {"res_id:com.pulse.target:id/page"}
    ]
    assert attempts[0] == "text:Search"
    assert f"res_id:{SEARCH}" in attempts
    assert store.lookup(KEY) is None
    assert store.records == {}


async def test_ambiguous_check_stays_blocked_with_a_store(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    store.remember(page_key("ambiguous", "row"), ExperiencedCandidate("res_id", SEARCH))
    result, _ = await run("ambiguous", store=store)
    assert result.status == "blocked"
    assert result.reason == "action_bounds_unavailable"


async def test_second_run_verdict_matches_first_run(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    first, first_driver = await run("search", store=store)
    second, second_driver = await run("search", store=store)
    assert first.status == second.status == "passed"
    assert first_driver.world.attempts == second_driver.world.attempts


async def test_collection_failure_stays_blocked_with_a_store(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    key = page_key("broken-remembered", "optional-search")
    store.remember(key, ExperiencedCandidate("res_id", SEARCH))
    result, driver = await run("broken-remembered", store=store)
    assert result.status == "blocked"
    assert result.reason == "observation_unavailable"
    assert driver.world.attempts == []


async def test_expected_status_matches_golden_without_a_store() -> None:
    for case_id, entry in expected().items():
        result, _ = await run(case_id)
        assert result.status == entry["status"], (case_id, result.as_dict())
        assert result.reason == entry["reason"], (case_id, result.as_dict())


async def test_expected_status_matches_golden_with_a_store(tmp_path: Path) -> None:
    for case_id, entry in expected().items():
        store = LocalExperience(tmp_path / f"{case_id}.json")
        store.remember(page_key(case_id, "search"), ExperiencedCandidate("res_id", SEARCH))
        result, _ = await run(case_id, store=store)
        assert result.status == entry["status"], (case_id, result.as_dict())
        assert result.reason == entry["reason"], (case_id, result.as_dict())


def test_experience_hit_ignores_non_declared_candidates(tmp_path: Path) -> None:
    locators = load_project(FIXTURE).profiles["fake-android"].locators
    target = m.LocatorRef(locator_ref="search")

    case_key = page_key("search", "search")

    class Context:
        experience = LocalExperience(tmp_path / "locator-experience.json")
        page = "exists:page"

        def experience_key(self, target_ref: str | None) -> ExperienceKey:
            assert target_ref in (None, "search")
            return case_key

    context = Context()
    assert _experience_hit(target, locators, context) is None  # type: ignore[arg-type]
    context.experience.remember(case_key, ExperiencedCandidate("text", "Not Declared"))
    assert _experience_hit(target, locators, context) is None  # type: ignore[arg-type]
    # "text:Search" is the declared first candidate, so it is never a *hint*: the declared
    # order already tries it first and v0 does not override that order.
    context.experience.remember(case_key, ExperiencedCandidate("text", "Search"))
    assert _experience_hit(target, locators, context) is None  # type: ignore[arg-type]
    # A declared later candidate is also refused until an ordering policy proves it neutral.
    context.experience.remember(case_key, ExperiencedCandidate("res_id", SEARCH))
    assert _experience_hit(target, locators, context) is None  # type: ignore[arg-type]
    inline = m.InlineLocator(locator=locators["search"])
    assert _experience_hit(inline, locators, context) is None  # type: ignore[arg-type]


def test_hit_is_ignored_without_a_page_anchor(tmp_path: Path) -> None:
    project = load_project(FIXTURE)
    locators = project.profiles["fake-android"].locators

    class Context:
        experience = LocalExperience(tmp_path / "locator-experience.json")
        page = None

        def experience_key(self, target_ref: str | None) -> ExperienceKey:
            return KEY

    Context.experience.remember(KEY, ExperiencedCandidate("res_id", SEARCH))

    # A case without a required precondition has no page anchor and no cache bucket.
    assert _page_anchor(project.cases["search"].model_copy(update={"preconditions": []})) is None

    # Without a page anchor the interpreter computes no key, so the store cannot leak into it.
    def context(page: str | None) -> _Context:
        return _Context(
            project,
            project.cases["search"],
            project.profiles["fake-android"],
            ExperienceDriver(world_for("search")),
            RunLease("00000000-0000-0000-0000-000000000000", 1),
            Deadline(ends_at=1e12),
            None,
            {},
            False,
            experience=Context.experience,
            page=page,
        )

    assert context(None).experience_key("search") is None
    anchored = context("exists:page")
    assert anchored.experience_key("search") == KEY
    assert _experience_hit(m.LocatorRef(locator_ref="search"), locators, context(None)) is None


def test_resolution_fields_carry_candidate_index() -> None:
    resolution = Resolution(
        "resolved", None, (Element("tree", "x"),), candidate_index=1, candidate_by="text"
    )
    assert resolution.candidate_index == 1
    assert resolution.candidate_by == "text"


class ResolverManager:
    """The real P1.5 resolver over a synthetic observation; no device or Agent is touched."""

    async def probe(self, serial: str, body: object, *, scope: object = None) -> dict[str, object]:
        value, binding = observation(body)  # type: ignore[arg-type]
        return resolve_locator(body, value, binding=binding, scope_target=scope)  # type: ignore[arg-type]


class LiveResolverDriver(AndroidCaseDriver):
    """Exercises the real driver path, where a hint used to reorder the declared candidates."""

    def __init__(self, profile: m.Profile) -> None:
        super().__init__(
            ResolverManager(),  # type: ignore[arg-type]
            serial="PULSE_A",
            session_id="00000000-0000-4000-8000-000000000001",
            profile=profile,
        )
        self.dispatched: list[list[int]] = []

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        return RunLease(run_id, 0)

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        target = plan.target.action_target if plan.target is not None else None
        self.dispatched.append(list((target or {}).get("node_path") or []))
        return ActionResult("completed")


def ambiguous_project() -> Project:
    """First declared candidate matches two nodes; the fallback candidate is unique."""
    project = load_project(FIXTURE)
    profile = project.profiles["fake-android"]
    profile.locators["search"] = m.Locator(
        candidates=[
            m.LocatorCandidate(by="text", value="Duplicate"),
            m.LocatorCandidate(by="res_id", value="com.pulse.target:id/submit"),
        ],
        expect=m.LocatorExpect(unique=True, visible=True),
    )
    project.cases["search"].checks[0].condition.target = m.LocatorRef(locator_ref="page")
    validate_semantics(project)
    return project


async def test_cached_candidate_cannot_bypass_declared_ambiguity() -> None:
    project = ambiguous_project()
    plain = await run_case(
        project=project,
        driver=LiveResolverDriver(project.profiles["fake-android"]),
        case_id="search",
        profile_id="fake-android",
    )
    store = LocalExperience(Path(tempfile.mkdtemp()) / "locator-experience.json")
    store.remember(KEY, ExperiencedCandidate("res_id", "com.pulse.target:id/submit"))
    driver = LiveResolverDriver(project.profiles["fake-android"])
    cached = await run_case(
        project=project,
        driver=driver,
        case_id="search",
        profile_id="fake-android",
        experience=store,
    )
    assert (plain.status, plain.reason) == ("blocked", "ambiguous_target")
    assert (cached.status, cached.reason) == (plain.status, plain.reason)
    assert driver.dispatched == []


async def test_failed_resolution_never_updates_the_store(tmp_path: Path) -> None:
    path = tmp_path / "locator-experience.json"
    seeded = LocalExperience(path)
    seeded.remember(KEY, ExperiencedCandidate("res_id", SEARCH))
    seeded.save()
    before = seeded.records[KEY.hash()].hits
    store = LocalExperience(path)
    result, _ = await run("search", world=ExperienceWorld(candidates=[]), store=store)
    assert result.status == "failed" and result.reason == "target_not_found"
    assert store.records[KEY.hash()].hits == before
    assert store.changed is False
    assert LocalExperience(path).records[KEY.hash()].hits == before


async def test_a_successful_run_never_writes_the_store(tmp_path: Path) -> None:
    store = LocalExperience(tmp_path / "locator-experience.json")
    result, _ = await run("search", store=store)
    assert result.status == "passed"
    assert store.records == {} and store.changed is False
