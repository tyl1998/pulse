"""P1.11 local positioning experience v0.

The store format and its lookup helpers exist, but v0 does not consume them in a run:

- an experience is a record of a past fact, not a pass verdict and not a device snapshot;
- a hit could only reorder declared candidates of the same locator. Reordering changes which
  candidate each attempt evaluates, and the P1.5 ambiguity rule applies per attempt, so a
  cached candidate can turn an ambiguous declared first candidate into a passing action;
- until an explicit ordering policy proves a reorder is verdict-neutral, the interpreter never
  offers a hint and therefore never rewrites the store. Declared order is the only order;
- deleting the store changes nothing, which keeps the DoD "cache affects latency only" true
  by construction rather than by an untested equivalence argument.

The store is Runtime-local. It is not the P7 shared experience service: no revisions,
promotion thresholds, sync, disabling/rollback UI or usage dashboards. It never stores page
text, screenshots, credentials or business data; only an existing declared candidate value.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pulse_runtime.paths import ensure_private_dir
from pulse_schema import models as m

CONTRACT = "pulse-runtime-local-experience-v0"
FILE_NAME = "locator-experience.json"
MAX_ENTRIES = 4096
MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_VALUE_CHARS = 1024
CANDIDATE_KINDS = frozenset({"res_id", "text", "desc", "xpath", "ocr_text", "image"})


@dataclass(frozen=True, order=True)
class ExperienceKey:
    """Matching scope: project + app + page + intent + target.

    Locators belong to the project/profile and the schema revision ``1-draft``; keying by
    project, app identity, page anchor, operation intent and target name keeps unrelated
    pages and unrelated targets in separate buckets. Wider scopes are not shared.
    """

    project: str
    app: str
    page: str
    intent: str
    target: str

    def hash(self) -> str:
        payload = json.dumps(
            [self.project, self.app, self.page, self.intent, self.target],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ExperiencedCandidate:
    by: str
    value: str
    scope: str | None = None

    def as_json(self) -> dict[str, object]:
        return {"by": self.by, "value": self.value, "scope": self.scope}


@dataclass(frozen=True)
class ExperienceRecord:
    key: str
    scope: ExperienceKey
    candidate: ExperiencedCandidate
    hits: int
    misses: int
    created_at_ms: int
    updated_at_ms: int
    contract: str = CONTRACT

    def as_json(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "key": self.key,
            "scope": {
                "project": self.scope.project,
                "app": self.scope.app,
                "page": self.scope.page,
                "intent": self.scope.intent,
                "target": self.scope.target,
            },
            "candidate": self.candidate.as_json(),
            "hits": self.hits,
            "misses": self.misses,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_json(cls, data: object) -> ExperienceRecord | None:
        """Parse one entry; any malformed field drops just that entry, never the store."""
        if not isinstance(data, dict) or data.get("contract") != CONTRACT:
            return None
        scope = data.get("scope")
        candidate = data.get("candidate")
        if not isinstance(scope, dict) or not isinstance(candidate, dict):
            return None
        try:
            parsed_scope = ExperienceKey(
                project=_text(scope["project"]),
                app=_text(scope["app"]),
                page=_text(scope["page"]),
                intent=_text(scope["intent"]),
                target=_text(scope["target"]),
            )
            by = candidate["by"]
            if by not in CANDIDATE_KINDS:
                return None
            raw_scope = candidate.get("scope")
            record = cls(
                key=_text(data["key"]),
                scope=parsed_scope,
                candidate=ExperiencedCandidate(
                    by=str(by),
                    value=_text(candidate["value"]),
                    scope=None if raw_scope is None else _text(raw_scope),
                ),
                hits=_count(data["hits"]),
                misses=_count(data["misses"]),
                created_at_ms=_count(data["created_at_ms"]),
                updated_at_ms=_count(data["updated_at_ms"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return record if record.key == parsed_scope.hash() else None


def _text(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_VALUE_CHARS:
        raise ValueError("invalid_experience_text")
    return value


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid_experience_count")
    return value


@runtime_checkable
class ExperienceStore(Protocol):
    """The interpreter's optional positioning-memory seam.

    v0 never calls :meth:`lookup`, so a store cannot change resolution. The protocol stays so a
    future explicit ordering policy can be injected without changing the interpreter again.
    """

    def lookup(self, key: ExperienceKey) -> ExperiencedCandidate | None: ...

    def remember(self, key: ExperienceKey, candidate: ExperiencedCandidate | None) -> None:
        """Keep a confirmed candidate, or drop the entry when it no longer resolves."""


class LocalExperience:
    """A bounded JSON store held in memory during a run and written atomically at the end."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, ExperienceRecord] = {}
        self.broken = False
        self.changed = False
        self.load()

    def load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self.broken = True
            return
        if len(raw) > MAX_STORE_BYTES:
            self.broken = True
            return
        try:
            document = json.loads(raw)
        except ValueError:
            self.broken = True
            return
        entries = document.get("entries") if isinstance(document, dict) else None
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            self.broken = True
            return
        for entry in entries:
            record = ExperienceRecord.from_json(entry)
            if record is not None:
                self.records[record.key] = record

    def lookup(self, key: ExperienceKey) -> ExperiencedCandidate | None:
        record = self.records.get(key.hash())
        return None if record is None else record.candidate

    def remember(self, key: ExperienceKey, candidate: ExperiencedCandidate | None) -> None:
        """Record the confirmed candidate, or drop the entry when it stopped resolving."""
        identifier = key.hash()
        now = time.time_ns() // 1_000_000
        previous = self.records.get(identifier)
        if candidate is None or candidate.by not in CANDIDATE_KINDS:
            if previous is not None:
                self.records.pop(identifier, None)
                self.changed = True
            return
        self.changed = True
        if previous is not None and previous.candidate == candidate:
            self.records[identifier] = ExperienceRecord(
                key=identifier,
                scope=key,
                candidate=candidate,
                hits=previous.hits + 1,
                misses=previous.misses,
                created_at_ms=previous.created_at_ms,
                updated_at_ms=now,
            )
            return
        if previous is None and len(self.records) >= MAX_ENTRIES:
            oldest = min(self.records.values(), key=lambda item: (item.updated_at_ms, item.key))
            self.records.pop(oldest.key, None)
        self.records[identifier] = ExperienceRecord(
            key=identifier,
            scope=key,
            candidate=candidate,
            hits=1,
            misses=0,
            created_at_ms=now,
            updated_at_ms=now,
        )

    def save(self) -> None:
        """Atomic private write. A broken or unwritable store is skipped, never raised."""
        if self.broken or not self.changed:
            return
        document = {
            "contract": CONTRACT,
            "version": 0,
            "entries": [self.records[key].as_json() for key in sorted(self.records)],
        }
        payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_STORE_BYTES:
            return
        temporary: Path | None = None
        try:
            ensure_private_dir(self.path.parent)
            handle, name = tempfile.mkstemp(dir=self.path.parent, prefix=".experience-")
            temporary = Path(name)
            if os.name != "nt":
                os.fchmod(handle, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def experience_key(
    *, project_id: str, app_id: str, page: str, intent: str, target: str
) -> ExperienceKey:
    return ExperienceKey(project=project_id, app=app_id, page=page, intent=intent, target=target)


def declared_candidate(
    target: m.Target, locators: Mapping[str, m.Locator]
) -> m.LocatorCandidate | None:
    """First declared candidate of a target; ref targets read the shared profile definition."""
    locator = (
        locators.get(target.locator_ref) if isinstance(target, m.LocatorRef) else target.locator
    )
    if locator is None or not locator.candidates:
        return None
    first = locator.candidates[0]
    return first if isinstance(first, m.LocatorCandidate) else None


def recalled_candidate(
    target: m.Target, locators: Mapping[str, m.Locator], experience: ExperiencedCandidate
) -> m.LocatorCandidate | None:
    """The remembered candidate only if it is still declared for this target.

    Nothing is invented from the store: by, value and scope must all match a declared entry.
    """
    locator = (
        locators.get(target.locator_ref) if isinstance(target, m.LocatorRef) else target.locator
    )
    if locator is None:
        return None
    for candidate in locator.candidates:
        if (
            isinstance(candidate, m.LocatorCandidate)
            and candidate.by == experience.by
            and candidate.value == experience.value
            and candidate.scope == experience.scope
        ):
            return candidate
    return None


def reordered_target(target: m.Target, primary: m.LocatorCandidate) -> m.Target:
    """Same target, same candidate set: a remembered candidate is tried first."""
    locator = target.locator if isinstance(target, m.InlineLocator) else None
    if locator is None:
        raise ValueError("inline_target_required")
    ordered: list[m.LocatorCandidate | m.PointCandidate] = [primary]
    ordered.extend(item for item in locator.candidates if item is not primary)
    return m.InlineLocator(locator=m.Locator(candidates=ordered, expect=locator.expect))
