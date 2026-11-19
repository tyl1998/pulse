"""Deterministic normalization for numeric and amount checkpoints.

Only exact program rules live here: one ASCII/Latin numeric parse, one fixed set of widely
used unit aliases that map to the declared unit, and one decimal rounding step. Nothing is
guessed from context: unknown units, multiple numbers, mixed signs, non-finite values and
ambiguous formats are refused so the caller reports ``unknown`` instead of a verdict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

# Half-width forms only. Accent/currency glyphs are attached to the number, not merged.
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_CURRENCY = {
    "¥": "CNY",
    "￥": "CNY",
    "元": "CNY",
    "RMB": "CNY",
    "CNY": "CNY",
    "$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
}
_NUMBERED = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CODE_UNITS = "|".join(sorted((code for code in _CURRENCY), key=len, reverse=True))
_TRAILING = re.compile(rf"^({_NUMBERED})({_CODE_UNITS}|%|折|小时|分钟|秒|次|件|个|杯)$")
_BARE_UNITS = frozenset({"h", "min", "s"})
# Longest first, so "5min" is not read as "5min" -> "5mi" + "n"-like tails.
_BARE_COMPACT = ("min", "h", "s")
_UNIT_TAIL = frozenset({"%", "折", "小时", "分钟", "秒", "次", "件", "个", "杯"})
_LEADING_PERCENT = re.compile(rf"^%({_NUMBERED})$")
# A declared unit accepts exactly this closed set of written forms (suffix, prefix or bare).
_UNIT_ALIASES = {
    "%": frozenset({"%"}),
    "折": frozenset({"折"}),
    "小时": frozenset({"小时", "h"}),
    "h": frozenset({"小时", "h"}),
    "分钟": frozenset({"分钟", "min"}),
    "min": frozenset({"分钟", "min"}),
    "秒": frozenset({"秒", "s"}),
    "s": frozenset({"秒", "s"}),
    "次": frozenset({"次"}),
    "件": frozenset({"件"}),
    "个": frozenset({"个"}),
    "杯": frozenset({"杯"}),
    "CNY": frozenset({"CNY", "元"}),
    "USD": frozenset({"USD"}),
    "EUR": frozenset({"EUR"}),
    "GBP": frozenset({"GBP"}),
}
# A bare number carries no unit; only these compact time units need their suffix written.
_SUFFIX_REQUIRED = frozenset({"h", "min", "s"})
# More decimal places than this are not a display format we can verify exactly.
MAX_PRECISION = 6
_PRIMARY = frozenset({"元", "杯", "h", "min", "s", "CNY", "USD", "EUR", "GBP"})
_COMPARE = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
}
# NFKC already folds full-width digits and ASCII signs; U+FF65 (half-width CJK dot) does not.
_DOT = "\uff65"
_HALF_WIDTH = str.maketrans({_DOT: "."})


@dataclass(frozen=True)
class Quantity:
    """One exact decimal with its resolved unit and the rounding that produced it."""

    value: Decimal
    unit: str
    precision: int
    raw: str

    def as_actual(self) -> dict[str, object]:
        return {
            "value": _json_number(self.value),
            "unit": self.unit,
            "precision": self.precision,
            "raw": self.raw,
        }


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value() and abs(value) <= 2**53:
        return int(value)
    return float(value)


def _round(value: Decimal, precision: int) -> Decimal:
    if precision > MAX_PRECISION:
        raise ValueError("precision")
    with localcontext() as context:
        context.prec = 60
        return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)


def supported_unit(unit: str) -> bool:
    """Only the closed alias set is a program rule; any other unit needs a new decision."""
    return _declared(unit) is not None


def _declared(unit: str) -> str | None:
    upper = unit.upper()
    if upper in _CURRENCY:
        return _CURRENCY[upper]
    return unit if unit in _PRIMARY or unit in _UNIT_ALIASES else None


def _written_symbol(text: str) -> str:
    """Map one written glyph/code to a currency. Suffixes leave bare "%"/"折" as themselves."""
    upper = text.upper()
    return _CURRENCY.get(upper, upper)


def _split(compact: str) -> tuple[str | None, str | None, bool]:
    """Split one compact cell into (number, written unit, used-compact-suffix)."""
    if match := _NUMBER.fullmatch(compact):
        return match.group(0), None, False
    if match := _TRAILING.fullmatch(compact):
        return match.group(1), _written_symbol(match.group(2)), False
    if match := _LEADING_PERCENT.fullmatch(compact):
        return match.group(1), "%", False
    if len(compact) < 2:
        return None, None, False
    outer = compact[0] if compact[0] in "+-" else ""
    rest = compact[len(outer) :]
    tail = rest[-1:] if rest else ""
    if rest[:1] in _CURRENCY and _NUMBER.fullmatch(rest[1:]):
        return outer + rest[1:], _written_symbol(rest[0]), False
    if tail in _UNIT_TAIL and _NUMBER.fullmatch(rest[:-1]):
        return outer + rest[:-1], _written_symbol(tail), False
    if tail in _CURRENCY and _NUMBER.fullmatch(rest[:-1]):
        return outer + rest[:-1], _written_symbol(tail), False
    for suffix in _BARE_COMPACT:
        head = rest[: -len(suffix)]
        if rest.endswith(suffix) and head and _NUMBER.fullmatch(head):
            return outer + head, suffix, True
    return None, None, False


def normalize_number(text: str, *, unit: str, precision: int) -> Quantity | None:
    """Parse one observed cell into the declared unit.

    ``None`` means the cell is not one unambiguous number in the declared unit. Multiple
    numbers, unknown glyphs, mixed signs, ``NaN``/``Infinity`` and a different currency all
    stay unverified rather than being coerced.
    """
    declared = _declared(unit)
    if declared is None or not 0 <= precision <= MAX_PRECISION:
        return None
    compact = unicodedata.normalize("NFKC", text).translate(_HALF_WIDTH)
    compact = "".join(compact.split())
    if not compact:
        return None
    body, symbol, _ = _split(compact)
    if body is None:
        return None
    allowed = _UNIT_ALIASES[declared]
    if symbol is None:
        # A bare "5" does not establish "5 分钟"; compact units must be written.
        if declared in _SUFFIX_REQUIRED and not compact.endswith(tuple(_BARE_UNITS)):
            return None
    elif symbol not in allowed:
        # Never convert between units; a different or foreign glyph stays unverified.
        return None
    try:
        value = Decimal(body.replace(",", ""))
        if not value.is_finite():
            return None
        rounded = _round(value, precision)
    except InvalidOperation:
        return None
    return Quantity(rounded, declared, precision, text)


def compare(quantity: Quantity, expected: Decimal, comparison: str) -> bool:
    if comparison not in _COMPARE:
        raise ValueError(comparison)
    with localcontext() as context:
        context.prec = 60
        return bool(_COMPARE[comparison](quantity.value, _round(expected, quantity.precision)))


def as_decimal(value: int | float) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None
