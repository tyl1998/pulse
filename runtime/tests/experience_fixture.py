"""P1.11 local experience golden cases over a scripted driver.

The driver models the parts of P1.5 that this feature touches: a locator is a *list* of
candidates resolved in declared order, ``expect.unique/visible`` is enforced on the current
snapshot, and the resolver reports which declared candidate produced the single match. It
counts candidate attempts so "hit" and "fallback" are observable, and it never invents a
match from the store.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
)
from pulse_schema import models as m
from pulse_schema.validation import Project, load_project

FIXTURE = Path(__file__).parents[2] / "fixtures/golden-cases/experience"

RES_ID = "com.pulse.target:id/"


class ExperienceWorld:
    """A tiny native tree: page anchor, one search field, optional rows, optional breakage."""

    def __init__(
        self,
        *,
        candidates: Sequence[str] = ("search", "search-text"),
        rows: int = 0,
        broken: bool = False,
    ) -> None:
        self.candidates = list(candidates)
        self.rows = rows
        self.broken = broken
        self.attempts: list[str] = []

    def clone(self) -> ExperienceWorld:
        """Same page content for a second run, without the previous run's attempt log."""
        return ExperienceWorld(candidates=self.candidates, rows=self.rows, broken=self.broken)

    def node(self, by: str, value: str) -> list[Element]:
        text = "Pulse"
        if by == "res_id" and value == f"{RES_ID}page":
            return [Element("tree", "Home", True, {"enabled": True, "visible": True})]
        if by == "res_id" and value == f"{RES_ID}search":
            if "search" not in self.candidates:
                return []
            return [Element("tree", text, True, {"enabled": True, "visible": True}, _bounds(1))]
        if by == "text" and value == "Search":
            if "search-text" not in self.candidates:
                return []
            return [Element("tree", text, True, {"enabled": True, "visible": True}, _bounds(1))]
        if by == "res_id" and value == f"{RES_ID}row":
            return [
                Element("tree", text, True, {"enabled": True, "visible": True}, _bounds(n))
                for n in range(1, self.rows + 1)
            ]
        return []


def _bounds(n: int) -> dict[str, int]:
    top = n * 100
    return {"left": 0, "top": top, "right": 200, "bottom": top + 80}


class ExperienceDriver:
    """Resolves the interpreter's *effective* target, exactly like P1.5 does.

    The interpreter only reorders the target it hands to the driver, so the primary hint is
    observable as candidate order; an unresolved hint is therefore not reported separately.
    """

    app_id = "com.pulse.target"

    def __init__(self, world: ExperienceWorld) -> None:
        self.world = world
        self.lease: RunLease | None = None
        self.queries: list[list[str]] = []
        self.primaries: list[m.LocatorCandidate | None] = []
        self.action_queries: list[list[str]] = []
        self.action_primaries: list[m.LocatorCandidate | None] = []

    async def begin(self, run_id: str, *, source: str) -> RunLease:
        self.lease = RunLease(run_id, 1)
        return self.lease

    async def resolve(
        self,
        target: m.Target,
        locators: Mapping[str, m.Locator],
        *,
        timeout_ms: int,
        scope: m.Target | None = None,
        primary: m.LocatorCandidate | None = None,
    ) -> Resolution:
        locator = (
            locators[target.locator_ref] if isinstance(target, m.LocatorRef) else target.locator
        )
        self.primaries.append(primary)
        self.queries.append([f"{item.by}:{item.value}" for item in locator.candidates])
        if self.world.broken:
            return Resolution("indeterminate", "observation_unavailable", query_complete=False)
        for index, candidate in enumerate(locator.candidates):
            if not isinstance(candidate, m.LocatorCandidate):
                continue
            self.world.attempts.append(f"{candidate.by}:{candidate.value}")
            found = self.world.node(candidate.by, candidate.value)
            if not found:
                continue
            # P1.5 rejects an ambiguous candidate declaration instead of silently falling
            # through to a later candidate; the live ``expect`` is checked for every hit.
            if locator.expect.unique and len(found) != 1:
                return Resolution(
                    "ambiguous",
                    "ambiguous_match",
                    tuple(found),
                    candidate_index=index,
                    candidate_by=candidate.by,
                )
            return Resolution(
                "resolved",
                None,
                tuple(found),
                _action_target(found[0]),
                candidate_index=index,
                candidate_by=candidate.by,
            )
        return Resolution("not_found", "no_match")

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        assert self.lease == lease
        if plan.kind == "set_text":
            self.action_queries.append(list(self.queries[-1]))
            self.action_primaries.append(self.primaries[-1])
        return ActionResult("completed", None, "action-1", "observation-1")

    async def finish(self, lease: RunLease, *, state: str) -> dict[str, object]:
        return {"status": "finished", "run_status": state, "run_id": lease.run_id}


def _action_target(element: Element) -> dict[str, object]:
    return {
        "observation_id": "observation-1",
        "source": "tree",
        "coordinate_space": "display_pixels",
        "display_id": 0,
        "expected_package": "com.pulse.target",
        "node_path": [0, 1],
    }


def project() -> Project:
    return load_project(FIXTURE)


def expected() -> dict[str, object]:
    entries = json.loads((FIXTURE / "expected.json").read_text("utf-8"))["cases"]
    assert isinstance(entries, dict)
    return entries


def world_for(name: str) -> ExperienceWorld:
    settings = expected()[name]["world"]
    return ExperienceWorld(
        candidates=tuple(settings.get("candidates", ("search",))),
        rows=int(settings.get("rows", 0)),
        broken=bool(settings.get("broken", False)),
    )
