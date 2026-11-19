"""Tri-state predicate evaluation over fresh driver resolutions.

Every non-semantic operator from the plan (§7.3) is evaluated here with fixed rules:

* ``exists``/``page_is``/``visible``/``enabled``/``checked``/``text_equals``/``contains``/
  ``regex``/``count_eq``/``count_gt``/``count_lt`` return ``true``/``false``;
* ``number_cmp`` parses one number from the matched text with the declared unit and precision
  (half-up); an ambiguous value, foreign currency or different compact unit is ``unknown``;
* ``not_exists`` samples one declared page scope inside its own observation window and only
  passes when every complete sample is empty; any collection failure or a window that cannot
  finish inside the remaining budget is ``unknown`` (never a pass).

``unknown`` means "could not be verified" and is reported as ``blocked`` by the interpreter.
Only ``semantic`` stays out of scope (P2) and is reported as ``model_required`` here.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import InvalidOperation
from typing import Literal

from pulse_runtime.engine.assertions import as_decimal, compare, normalize_number
from pulse_runtime.engine.budget import Deadline
from pulse_runtime.engine.driver import CaseDriver, DriverFault, Element, Resolution
from pulse_runtime.engine.errors import EngineStop
from pulse_runtime.engine.regex import search as regex_search
from pulse_runtime.engine.values import Scope, TypedValue, resolve_value
from pulse_schema import models as m
from pulse_schema.validation import Project

Verdict = Literal["true", "false", "unknown"]
_SOURCE = {
    "native": "tree",
    "webview": "tree",
    "ocr": "ocr",
    "vision": "screenshot",
}
# Sources that carry text; vision returns bitmap bounds without OCR text in the driver.
_TEXT_SOURCE = frozenset({"tree", "ocr"})


@dataclass(frozen=True)
class ConditionResult:
    verdict: Verdict
    operator: str
    source: str
    code: str | None = None
    actual: dict[str, object] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return self.verdict != "unknown"


@dataclass(frozen=True)
class _Sample:
    verdict: Verdict
    code: str | None
    actual: dict[str, object]


def _single(resolution: Resolution) -> Element | None:
    return resolution.matches[0] if len(resolution.matches) == 1 else None


async def _resolve(
    driver: CaseDriver,
    target: m.Target,
    locators: Mapping[str, m.Locator],
    *,
    timeout_ms: int,
    deadline: Deadline,
    scope: m.Target | None = None,
) -> Resolution:
    read_deadline = Deadline.after(timeout_ms / 1000, deadline)
    try:
        if read_deadline.expired:
            raise TimeoutError
        async with asyncio.timeout(read_deadline.remaining()):
            if scope is None:
                result = await driver.resolve(target, locators, timeout_ms=timeout_ms)
            else:
                result = await driver.resolve(target, locators, timeout_ms=timeout_ms, scope=scope)
        if read_deadline.expired:
            raise TimeoutError
        return result
    except TimeoutError:
        return Resolution("indeterminate", "deadline_exceeded")
    except DriverFault as fault:
        if fault.status in ("canceled", "interrupted"):
            raise
        return Resolution("indeterminate", fault.code)


def _source_matches(resolution: Resolution, source: str) -> bool:
    expected = _SOURCE.get(source)
    return bool(resolution.matches) and all(item.source == expected for item in resolution.matches)


def _actual(resolution: Resolution) -> dict[str, object]:
    return {
        "status": resolution.status,
        "match_count": len(resolution.matches),
        "sources": sorted({item.source for item in resolution.matches}),
    }


def _absence_sample(resolution: Resolution, source: str) -> _Sample:
    actual = _actual(resolution)
    if resolution.matches and not _source_matches(resolution, source):
        return _Sample("unknown", "source_mismatch", actual)
    if resolution.matches:
        return _Sample("false", None, actual)
    if resolution.status == "not_found":
        return _Sample("true", None, actual)
    return _Sample("unknown", resolution.error_code or "source_unavailable", actual)


def _presence(resolution: Resolution, source: str) -> _Sample:
    actual = _actual(resolution)
    if resolution.status not in ("resolved", "not_found"):
        return _Sample("unknown", resolution.error_code or "source_unavailable", actual)
    if resolution.matches and not _source_matches(resolution, source):
        return _Sample("unknown", "source_mismatch", actual)
    if resolution.matches:
        return _Sample("true", None, actual)
    if resolution.status == "not_found":
        return _Sample("false", None, actual)
    return _Sample("unknown", resolution.error_code or "source_unavailable", actual)


def _expected(
    condition: m.Predicate,
    *,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> TypedValue:
    if not isinstance(
        condition, (m.TextCondition, m.CountCondition, m.NumberCondition, m.SemanticCondition)
    ):
        raise EngineStop("blocked", "condition_not_comparable")
    return resolve_value(condition.expected, project=project, scope=scope, secrets=secrets)


def _count_sample(
    condition: m.Predicate,
    resolution: Resolution,
    source: str,
    *,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> _Sample:
    operator = condition.operator
    actual = _actual(resolution)
    if resolution.matches and not _source_matches(resolution, source):
        return _Sample("unknown", "source_mismatch", actual)
    if resolution.status == "indeterminate" or not resolution.query_complete:
        return _Sample("unknown", resolution.error_code or "source_unavailable", actual)
    expected = _expected(condition, project=project, scope=scope, secrets=secrets)
    if expected.type != "integer" or type(expected.value) is not int:
        return _Sample("unknown", "expected_type", actual)
    count = len(resolution.matches)
    actual["count"] = count
    result = (
        count == expected.value
        if operator == "count_eq"
        else count > expected.value
        if operator == "count_gt"
        else count < expected.value
    )
    return _Sample("true" if result else "false", None, actual)


def _number_sample(
    condition: m.NumberCondition,
    resolution: Resolution,
    *,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> _Sample:
    actual = _actual(resolution)
    if resolution.status != "resolved" and resolution.status != "ambiguous":
        return _Sample("unknown", resolution.error_code or "target_unavailable", actual)
    if not _source_matches(resolution, condition.source):
        return _Sample("unknown", "source_mismatch", actual)
    element = _single(resolution)
    if element is None:
        return _Sample("unknown", "ambiguous_target", actual)
    if element.source not in _TEXT_SOURCE:
        # Vision returns bitmap bounds only; the interpreter has no OCR text for that target.
        return _Sample("unknown", "text_unverified", actual)
    if element.text is None:
        return _Sample("unknown", "text_unverified", actual)
    expected = _expected(condition, project=project, scope=scope, secrets=secrets)
    if expected.type not in ("number", "integer") or type(expected.value) not in (int, float):
        return _Sample("unknown", "expected_type", actual)
    if type(expected.value) is not int and type(expected.value) is not float:
        return _Sample("unknown", "expected_type", actual)
    amount = as_decimal(expected.value)
    if amount is None:
        return _Sample("unknown", "expected_type", actual)
    quantity = normalize_number(element.text, unit=condition.unit, precision=condition.precision)
    if quantity is None:
        actual["text"] = element.text
        actual["unit"] = condition.unit
        actual["precision"] = condition.precision
        return _Sample("unknown", "number_unverified", actual)
    actual["text"] = element.text
    actual.update(quantity.as_actual())
    actual["expected"] = expected.value
    actual["comparison"] = condition.comparison
    try:
        result = compare(quantity, amount, condition.comparison)
    except InvalidOperation:
        return _Sample("unknown", "number_unverified", actual)
    return _Sample("true" if result else "false", None, actual)


async def _evaluate_resolution(
    condition: m.Predicate,
    resolution: Resolution,
    *,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
    deadline: Deadline,
) -> _Sample:
    operator = condition.operator
    source = condition.source
    actual = _actual(resolution)
    if operator in ("exists", "page_is"):
        return _presence(resolution, source)
    if operator in ("count_eq", "count_gt", "count_lt"):
        return _count_sample(
            condition, resolution, source, project=project, scope=scope, secrets=secrets
        )
    if isinstance(condition, m.NumberCondition):
        return _number_sample(condition, resolution, project=project, scope=scope, secrets=secrets)
    if operator == "semantic":
        return _Sample("unknown", "model_required", actual)
    if resolution.status != "resolved" and resolution.status != "ambiguous":
        return _Sample(
            "unknown",
            resolution.error_code or "target_unavailable",
            actual,
        )
    if not _source_matches(resolution, source):
        return _Sample("unknown", "source_mismatch", actual)
    element = _single(resolution)
    if element is None:
        return _Sample("unknown", "ambiguous_target", actual)
    if isinstance(condition, m.StateCondition):
        wanted = condition.expected
        value = element.visible if operator == "visible" else element.states.get(operator)
        if value is None:
            return _Sample("unknown", "attribute_unverified", actual)
        actual["value"] = value
        return _Sample("true" if value == wanted else "false", None, actual)
    expected = _expected(condition, project=project, scope=scope, secrets=secrets)
    if expected.type != "string" or type(expected.value) is not str:
        return _Sample("unknown", "expected_type", actual)
    text = element.text
    if text is None:
        return _Sample("unknown", "text_unverified", actual)
    actual["text"] = text
    if operator == "text_equals":
        result = text == expected.value
    elif operator == "contains":
        result = expected.value in text
    elif operator == "regex":
        try:
            result = await regex_search(expected.value, text, timeout=deadline.remaining())
        except re.error:
            return _Sample("unknown", "regex_invalid", actual)
        except TimeoutError:
            return _Sample("unknown", "regex_timeout", actual)
        except (OSError, RuntimeError, OverflowError):
            return _Sample("unknown", "regex_unverified", actual)
    else:
        return _Sample("unknown", "operator_unavailable", actual)
    return _Sample("true" if result else "false", None, actual)


async def evaluate(
    condition: m.Predicate,
    *,
    driver: CaseDriver,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> ConditionResult:
    deadline = Deadline.after(condition.timeout_ms / 1000, deadline)
    if isinstance(condition, m.AbsenceCondition):
        return await _absence(
            condition,
            driver=driver,
            locators=locators,
            deadline=deadline,
            project=project,
            scope=scope,
            secrets=secrets,
        )
    if deadline.expired:
        raise EngineStop("blocked", "deadline_exceeded")
    timeout = min(condition.timeout_ms, deadline.limit_ms(condition.timeout_ms))
    if timeout <= 0:
        raise EngineStop("blocked", "deadline_exceeded")
    resolution = await _resolve(
        driver, condition.target, locators, timeout_ms=timeout, deadline=deadline
    )
    sample = await _evaluate_resolution(
        condition, resolution, project=project, scope=scope, secrets=secrets, deadline=deadline
    )
    return ConditionResult(
        sample.verdict, condition.operator, condition.source, sample.code, sample.actual
    )


async def _absence(
    condition: m.AbsenceCondition,
    *,
    driver: CaseDriver,
    locators: Mapping[str, m.Locator],
    deadline: Deadline,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> ConditionResult:
    if deadline.expired:
        raise EngineStop("blocked", "deadline_exceeded")
    window = condition.observation_window_ms
    if deadline.remaining() * 1000 < window:
        return ConditionResult(
            "unknown", condition.operator, condition.source, "observation_window_exceeded"
        )
    started = deadline.remaining()
    window_deadline = Deadline.after(window / 1000, deadline)
    samples: list[dict[str, object]] = []
    while True:
        if deadline.expired:
            raise EngineStop("blocked", "deadline_exceeded")
        if window_deadline.expired:
            return ConditionResult(
                "true" if len(samples) >= 2 else "unknown",
                condition.operator,
                condition.source,
                None if len(samples) >= 2 else "observation_samples_missing",
                {"samples": samples, "window_ms": window},
            )
        timeout = window_deadline.limit_ms(condition.timeout_ms)
        resolution = await _resolve(
            driver,
            condition.target,
            locators,
            timeout_ms=timeout,
            deadline=window_deadline,
            scope=condition.scope,
        )
        sample = _absence_sample(resolution, condition.source)
        samples.append(
            {
                **sample.actual,
                "at_ms": round((started - deadline.remaining()) * 1000, 3),
                "verdict": sample.verdict,
                "error": sample.code,
            }
        )
        if sample.verdict == "false":
            return ConditionResult(
                "false",
                condition.operator,
                condition.source,
                None,
                {"samples": samples, "window_ms": window},
            )
        if sample.verdict == "unknown":
            return ConditionResult(
                "unknown",
                condition.operator,
                condition.source,
                sample.code,
                {"samples": samples, "window_ms": window},
            )
        remaining = window_deadline.remaining()
        await asyncio.sleep(min(condition.poll_interval_ms / 1000, remaining))
