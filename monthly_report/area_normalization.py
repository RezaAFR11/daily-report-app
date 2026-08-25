"""Conservative source/reporting-area normalization shared by compilers."""

from __future__ import annotations

import re
from typing import Any


_LEADING_MA_AREA_RE = re.compile(
    r"^\s*MA\s*[- ]?(?P<body>\d{1,3}(?:\s*(?:/|&|,|\band\b)\s*(?:MA\s*)?[- ]?\d{1,3}){0,5})\b",
    re.IGNORECASE,
)
_LEADING_MA_HYPHEN_PAIR_RE = re.compile(
    r"^\s*MA\s+(?P<left>\d{1,2})\s*-\s*(?P<right>\d{1,2})(?=\s+[A-Za-z])",
    re.IGNORECASE,
)


def _clean(value: Any, maximum: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def canonical_numeric_ma_area(numbers: list[str]) -> str:
    values: list[str] = []
    for value in numbers:
        try:
            number = str(int(value))
        except (TypeError, ValueError):
            continue
        if number not in values:
            values.append(number)
    if not values:
        return ""
    if len(values) == 1:
        return f"MA-{values[0]}"
    return "MA " + "/".join(values)


def canonical_source_area_label(value: Any) -> str:
    """Normalise harmless MA punctuation while retaining source ownership."""

    source = _clean(value, 255) or "General"
    if re.match(r"^MA\s+WPP\b", source, re.IGNORECASE):
        return re.sub(r"(?<=\d)\.(?=\d)", "/", source)
    match = re.fullmatch(
        r"MA\s*[- ]?(?P<body>\d{1,3}(?:\s*/\s*(?:\d{1,3}|Pioneer|Jetty)){0,6})",
        source,
        re.IGNORECASE,
    )
    if not match:
        return source
    parts = [part.strip() for part in match.group("body").split("/") if part.strip()]
    numbers = sorted({int(part) for part in parts if part.isdigit()})
    names: list[str] = []
    for part in parts:
        if part.isdigit():
            continue
        name = part.title()
        if name not in names:
            names.append(name)
    if len(numbers) == 1 and not names:
        return f"MA-{numbers[0]}"
    body = "/".join([*(str(number) for number in numbers), *names])
    return f"MA {body}" if body else source


def reporting_activity_area(source_area: Any, description: Any) -> dict[str, Any]:
    """Return source and client-facing area with an auditable mapping method.

    Only an explicit *leading* MA label may override a composite crew heading.
    Mentions inside routing prose (for example ``from MA 59 to MA 42``) never
    change ownership.  Non-MA legacy equipment labels remain unchanged.
    """

    raw_source = _clean(source_area, 255) or "General"
    source = canonical_source_area_label(raw_source)
    base = {
        "source_area": raw_source,
        "reporting_area": source,
        "method": "exact_alias" if source != raw_source else "source_fallback",
        "confidence": 1.0 if source != raw_source else 0.85,
        "review_required": False,
    }
    if not source.casefold().startswith("ma"):
        base["confidence"] = 1.0
        return base

    text = _clean(description)
    pair = _LEADING_MA_HYPHEN_PAIR_RE.match(text)
    if pair:
        explicit = canonical_numeric_ma_area([pair.group("left"), pair.group("right")])
        if explicit:
            return {
                **base,
                "reporting_area": explicit,
                "method": "leading_explicit",
                "confidence": 0.98,
            }
    match = _LEADING_MA_AREA_RE.match(text)
    if not match:
        return base
    explicit = canonical_numeric_ma_area(re.findall(r"\d{1,3}", match.group("body")))
    if not explicit:
        return base
    return {
        **base,
        "reporting_area": explicit,
        "method": "leading_explicit",
        "confidence": 0.98,
    }


def numeric_ma_tokens(value: Any) -> set[str]:
    text = _clean(value, 255)
    if not text.casefold().startswith("ma"):
        return set()
    return {str(int(token)) for token in re.findall(r"\d{1,3}", text)}


def leading_numeric_ma_tokens(value: Any) -> set[str]:
    text = _clean(value)
    pair = _LEADING_MA_HYPHEN_PAIR_RE.match(text)
    if pair:
        return {str(int(pair.group("left"))), str(int(pair.group("right")))}
    match = _LEADING_MA_AREA_RE.match(text)
    if not match:
        return set()
    return {str(int(token)) for token in re.findall(r"\d{1,3}", match.group("body"))}


def activity_area_conflicts(area: Any, text: Any) -> bool:
    expected = numeric_ma_tokens(area)
    explicit = leading_numeric_ma_tokens(text)
    return bool(expected and explicit and not explicit.issubset(expected))


__all__ = [
    "activity_area_conflicts",
    "canonical_numeric_ma_area",
    "canonical_source_area_label",
    "leading_numeric_ma_tokens",
    "numeric_ma_tokens",
    "reporting_activity_area",
]
