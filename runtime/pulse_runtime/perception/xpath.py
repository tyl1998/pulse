"""A bounded native-tree XPath subset, with no XML parsing or expression execution."""

from __future__ import annotations

import re
from dataclasses import dataclass

ATTRIBUTES = {
    "resource-id": "view_id",
    "text": "text",
    "content-desc": "content_description",
    "class": "class_name",
    "package": "package_name",
    "index": "index",
    "visible-to-user": "visible",
    **{
        name: name
        for name in (
            "visible",
            "enabled",
            "checked",
            "checkable",
            "clickable",
            "editable",
            "focused",
            "selected",
            "showing_hint_text",
        )
    },
}
_TEST = re.compile(r"(?:node|\*|[A-Za-z_][A-Za-z0-9_.$]*)")
_EQUAL = re.compile(r"@([A-Za-z_-]+)\s*=\s*(['\"])(.*?)\2", re.DOTALL)
_FUNCTION = re.compile(
    r"(contains|starts-with)\(\s*@([A-Za-z_-]+)\s*,\s*(['\"])(.*?)\3\s*\)", re.DOTALL
)


@dataclass(frozen=True)
class Predicate:
    attribute: str
    operator: str
    value: str


@dataclass(frozen=True)
class Step:
    axis: str
    name: str
    predicates: tuple[tuple[Predicate, ...] | int, ...]


def parse_xpath(expression: str) -> tuple[Step, ...]:
    """Support child/descendant paths, class/node/* tests, literal predicates and positions.

    Predicates may use equality, contains, starts-with and conjunction. Other XPath syntax is
    rejected, rather than treated as a query that found nothing. // is rooted inside scope.
    """
    if not expression or len(expression) > 1024:
        raise ValueError("unsupported_xpath")
    if expression == ".":
        return (Step("self", "*", ()),)
    relative = expression.startswith("./")
    if expression.startswith("/hierarchy/"):
        expression = expression[len("/hierarchy") :]
    elif expression.startswith("./"):
        expression = expression[1:]
    steps: list[Step] = []
    cursor = 0
    while cursor < len(expression):
        axis = "child"
        if expression.startswith("//", cursor):
            axis, cursor = "descendant", cursor + 2
        elif expression[cursor] == "/":
            if cursor == 0 and not relative:
                axis = "root"
            cursor += 1
        elif steps:
            raise ValueError("unsupported_xpath")
        match = _TEST.match(expression, cursor)
        if match is None:
            raise ValueError("unsupported_xpath")
        name, cursor = match.group(), match.end()
        predicates: list[tuple[Predicate, ...] | int] = []
        while cursor < len(expression) and expression[cursor] == "[":
            start, cursor, quote = cursor + 1, cursor + 1, ""
            while cursor < len(expression):
                character = expression[cursor]
                if quote:
                    if character == quote:
                        quote = ""
                elif character in {"'", '"'}:
                    quote = character
                elif character == "]":
                    break
                elif character == "[":
                    raise ValueError("unsupported_xpath")
                cursor += 1
            if cursor == len(expression) or quote:
                raise ValueError("unsupported_xpath")
            text, cursor = expression[start:cursor].strip(), cursor + 1
            if re.fullmatch(r"[1-9][0-9]{0,2}", text):
                position = int(text)
                if position > 512:
                    raise ValueError("unsupported_xpath")
                predicates.append(position)
            else:
                terms: list[Predicate] = []
                while text:
                    if equality := _EQUAL.match(text):
                        attribute, value = equality.group(1, 3)
                        operator, end = "equals", equality.end()
                    elif function := _FUNCTION.match(text):
                        operator, attribute, value = function.group(1, 2, 4)
                        end = function.end()
                    else:
                        raise ValueError("unsupported_xpath")
                    if attribute not in ATTRIBUTES or len(terms) >= 16:
                        raise ValueError("unsupported_xpath")
                    terms.append(Predicate(ATTRIBUTES[attribute], operator, value))
                    text = text[end:].strip()
                    if text:
                        if not text.startswith("and "):
                            raise ValueError("unsupported_xpath")
                        text = text[4:].strip()
                        if not text:
                            raise ValueError("unsupported_xpath")
                if not terms:
                    raise ValueError("unsupported_xpath")
                predicates.append(tuple(terms))
            if len(predicates) > 16:
                raise ValueError("unsupported_xpath")
        steps.append(Step(axis, name, tuple(predicates)))
        if len(steps) > 32 or (cursor < len(expression) and expression[cursor] != "/"):
            raise ValueError("unsupported_xpath")
    return tuple(steps)
