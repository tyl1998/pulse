"""Scripted device backing ``fixtures/golden-cases/assertions``.

The cart text is fixed per locator; a case can ask for an uncollectable page. This is a test
double for the interpreter, never a physical device result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import uuid4

from pulse_runtime.engine.driver import (
    ActionPlan,
    ActionResult,
    Element,
    Resolution,
    RunLease,
    RunStatus,
)
from pulse_schema import models as m

APP = "com.pulse.target"

_INITIAL_TEXT = {
    "title": "Total: ¥12.50",
    "price": "¥12.50",
    "subtotal": "¥0.295",
    "discount": "85%",
    "dollars": "$12.50",
    "confirm": None,
}
_INITIAL_STATES = {
    "confirm": {"enabled": True, "checked": False},
}
_INITIAL_COUNTS = {"items": 1}


@dataclass
class AssertionWorld:
    text: dict[str, str] = field(default_factory=lambda: dict(_INITIAL_TEXT))
    states: dict[str, dict[str, bool]] = field(
        default_factory=lambda: {name: dict(value) for name, value in _INITIAL_STATES.items()}
    )
    counts: dict[str, int] = field(default_factory=lambda: dict(_INITIAL_COUNTS))
    missing: set[str] = field(default_factory=set)
    uncollectable: bool = False

    banner: bool = False
    # Names whose first read succeeds and every later read fails (window samples after the first).
    flaky: set[str] = field(default_factory=set)

    def set(self, name: str, value: str) -> None:
        self.text[name] = value


class AssertionDriver:
    app_id = APP

    @staticmethod
    def _name(target: m.Target) -> str:
        return target.locator_ref if isinstance(target, m.LocatorRef) else "inline"

    def __init__(self, world: AssertionWorld | None = None) -> None:
        self.world = world or AssertionWorld()
        self.finished: list[RunStatus] = []
        self.lease: RunLease | None = None
        self.seen: dict[str, int] = {}

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
        if scope is not None:
            scoped = await self.resolve(scope, locators, timeout_ms=timeout_ms)
            if scoped.status != "resolved" or not scoped.query_complete or len(scoped.matches) != 1:
                return Resolution("indeterminate", "scope_unavailable")
        name = self._name(target)
        self.seen[name] = self.seen.get(name, 0) + 1
        if (name in self.world.flaky and self.seen[name] > 1) or self.world.uncollectable:
            return Resolution("indeterminate", "observation_unavailable")
        if name == "items":
            count = self.world.counts.get(name, 0)
            matches = tuple(Element("tree", visible=True) for _ in range(count))
            return Resolution("resolved" if count else "not_found", None, matches)
        if name == "banner":
            present = self.world.banner
            return (
                Resolution("resolved", None, (Element("tree", "Limited offer", True),))
                if present
                else Resolution("not_found", "no_match")
            )
        if name in self.world.missing:
            return Resolution("not_found", "no_match")
        states = self.world.states.get(name, {})
        return Resolution(
            "resolved",
            None,
            (
                Element(
                    "tree",
                    text=self.world.text.get(name),
                    visible=True,
                    states=dict(states),
                ),
            ),
            {"node_path": [1]},
        )

    async def act(self, plan: ActionPlan, lease: RunLease) -> ActionResult:
        return ActionResult("completed", None, str(uuid4()), str(uuid4()))

    async def finish(self, lease: RunLease, *, state: RunStatus) -> dict[str, object]:
        self.finished.append(state)
        return {"status": "cleaned"}
