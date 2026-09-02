"""Pure aggregation of canonical daily records into a monthly data model."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .area_normalization import reporting_activity_area


_TIME_RANGE = re.compile(
    r"(?P<start_h>\d{1,2}):(?P<start_m>\d{2})\s*(?:-|\u2013|\u2014|\u2212)\s*"
    r"(?P<end_h>\d{1,2}):(?P<end_m>\d{2})"
)
_ROLE_FIELDS = ("role", "position", "role_position")
_HOURS_FIELDS = ("hours", "working_hours", "work_hours")
_EMPLOYEE_ID_FIELDS = (
    "employee_id",
    "employee_no",
    "personnel_id",
    "personnel_no",
    "nik",
    "badge_no",
)
_MANPOWER_STATUSES = {"reported", "none_reported", "not_supplied"}
_PROGRESS_NUMERIC_FIELDS = (
    "weight_factor",
    "cumulative_previous_plan",
    "cumulative_previous_actual",
    "this_period_plan",
    "this_period_actual",
    "cumulative_to_date_plan",
    "cumulative_to_date_actual",
    "deviation",
)
_PROGRESS_TOTAL_FIELDS = (
    "cumulative_previous_plan",
    "cumulative_previous_actual",
    "this_period_plan",
    "this_period_actual",
    "cumulative_to_date_plan",
    "cumulative_to_date_actual",
)

# Daily-report PDF text occasionally leaks repeated page headers/footers into the
# final activity line when a table continues across pages.  Keep the canonical
# source untouched, but strip only unmistakable document boilerplate from the
# period aggregate used for client-facing Weekly/Monthly reports.
_DOCUMENT_BOILERPLATE_RE = re.compile(
    r"\s+(?:PT\.?\s+GARUDA\s+PRIMA\s+AKSARA|T\.\s*Garuda\s+Prima\s+Aksara|"
    r"Daily\s+Activity\s+Report|aily\s+Activity\s+Report)\b.*$",
    re.IGNORECASE,
)


def _clean_period_text(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    cleaned = _DOCUMENT_BOILERPLATE_RE.sub("", text).strip()
    # A flattened footer can also begin directly after a truncated location/customer
    # field.  Remove those fragments only when they follow real activity prose.
    cleaned = re.sub(
        r"\s+(?:LOCATION|CUSTOMER|DATE)\s*:\s*[^|]{0,160}(?:\||$).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or text


def _iso_date(value: Any, label: str = "date") -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD.") from exc


def _normalise_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else record


def _metadata(record: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = record.get(key)
    if value not in (None, ""):
        return value
    return _payload(record).get(key, default)


def _revision(record: Mapping[str, Any]) -> int:
    try:
        value = int(record.get("revision", 0))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _timestamp_sort_value(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return float("-inf")


def _record_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _revision(record),
        _timestamp_sort_value(record.get("generated_at")),
        str(record.get("report_id", "")),
    )


def _date_sequence(start: str, end: str) -> list[str]:
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    result = []
    while current <= last:
        result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def _iter_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = _clean_text(value)
        if text:
            yield text
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_text_values(item)


def _activity_status_map(area: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = area.get("activity_statuses")
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        description = _clean_text(row.get("description"))
        status = _clean_text(row.get("status"))
        if description and status:
            result[_normalise_text(description)] = status
    return result


def _weather_row(payload: Mapping[str, Any], report_date: str) -> dict[str, str] | None:
    weather = payload.get("weather")
    if not isinstance(weather, Mapping):
        return None
    item = {"date": report_date}
    for key in ("morning", "afternoon", "evening", "wind", "temperature", "impact"):
        value = _clean_text(weather.get(key))
        if value:
            item[key] = value
    return item if len(item) > 1 else None


def _dedupe_entries(entries: list[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    result = []
    for entry in entries:
        key = tuple(_normalise_text(entry.get(field)) for field in keys)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _policy_number(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def _normalise_work_hours_policy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, permissive physical-hours policy.

    The legacy elapsed/no-break calculation is deliberately the default.  A
    project may opt into one fixed break deduction without changing explicit
    ``man_hours`` supplied by the Daily Report.  Unsupported values fall back to
    the legacy behavior instead of preventing compilation.
    """

    raw = value if isinstance(value, Mapping) else {}
    requested_mode = _normalise_text(raw.get("mode") or raw.get("calculation"))
    mode_aliases = {
        "elapsed": "elapsed_no_break",
        "elapsed no break": "elapsed_no_break",
        "elapsed_no_break": "elapsed_no_break",
        "elapsed less break": "elapsed_less_break",
        "elapsed_less_break": "elapsed_less_break",
        # Accept the early sandbox spelling without exposing two policies.
        "elapsed minus break": "elapsed_less_break",
        "elapsed_minus_break": "elapsed_less_break",
    }
    mode = mode_aliases.get(requested_mode, "elapsed_no_break")
    break_minutes = _policy_number(raw.get("break_minutes"), 0.0)
    if raw.get("break_hours") not in (None, ""):
        break_minutes = 60.0 * _policy_number(raw.get("break_hours"), 0.0)
    break_minutes = min(break_minutes, 240.0)
    threshold_minutes = _policy_number(
        raw.get("deduct_when_elapsed_gte_minutes"),
        -1.0,
    )
    if threshold_minutes < 0:
        threshold_hours = _policy_number(
            raw.get(
                "deduct_when_elapsed_gte",
                raw.get("minimum_shift_hours", raw.get("break_threshold_hours")),
            ),
            6.0,
        )
        threshold_minutes = threshold_hours * 60.0
    threshold_minutes = min(threshold_minutes, 24 * 60.0)
    allow_overnight_raw = raw.get("allow_overnight", True)
    allow_overnight = (
        allow_overnight_raw
        if isinstance(allow_overnight_raw, bool)
        else _normalise_text(allow_overnight_raw) not in {"0", "false", "no", "off"}
    )
    if mode != "elapsed_less_break":
        break_minutes = 0.0
        threshold_minutes = 0.0
    return {
        "mode": mode,
        "break_minutes": round(break_minutes, 2),
        "deduct_when_elapsed_gte_minutes": round(threshold_minutes, 2),
        "allow_overnight": bool(allow_overnight),
        "version": _clean_text(raw.get("version")) or "work-hours-policy/1",
        "configured": bool(value),
    }


def _hours_value(
    value: Any,
    work_hours_policy: Mapping[str, Any] | None = None,
) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None

    text = str(value).strip()
    if not text:
        return None
    match = _TIME_RANGE.search(text)
    if match:
        start_h, start_m, end_h, end_m = (
            int(match.group("start_h")),
            int(match.group("start_m")),
            int(match.group("end_h")),
            int(match.group("end_m")),
        )
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            return None
        minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
        if minutes < 0:
            policy = _normalise_work_hours_policy(work_hours_policy)
            if not policy["allow_overnight"]:
                return None
            minutes += 24 * 60
        elapsed = minutes / 60.0
        policy = _normalise_work_hours_policy(work_hours_policy)
        if (
            policy["mode"] == "elapsed_less_break"
            and minutes >= float(policy["deduct_when_elapsed_gte_minutes"])
        ):
            elapsed = max(0.0, elapsed - float(policy["break_minutes"]) / 60.0)
        return elapsed

    number_text = text.replace("%", "").replace(" ", "")
    if "," in number_text and "." not in number_text:
        number_text = number_text.replace(",", ".")
    try:
        number = float(number_text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    return not isinstance(value, str) or bool(value.strip())


def _numeric_man_hours(value: Any) -> float | None:
    """Return an explicit man-hour value without treating it as a shift range."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None

    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _person_role(row: Mapping[str, Any]) -> str:
    for field in _ROLE_FIELDS:
        value = _clean_text(row.get(field))
        if value:
            return value
    return ""


def _person_hours(
    row: Mapping[str, Any],
    work_hours_policy: Mapping[str, Any] | None = None,
) -> tuple[float | None, str, str, int]:
    """Normalise legacy hour fields and retain their completeness state.

    A valid explicit ``man_hours`` value is authoritative, including zero. If
    it is absent or invalid, known shift aliases are tried in order. Priority
    allows deduplication to retain explicit man-hours over a derived range.
    """

    explicit_present = _has_value(row.get("man_hours"))
    if explicit_present:
        explicit = _numeric_man_hours(row.get("man_hours"))
        if explicit is not None:
            return explicit, "parsed", "man_hours", 2

    supplied_aliases: list[str] = []
    for field in _HOURS_FIELDS:
        value = row.get(field)
        if not _has_value(value):
            continue
        supplied_aliases.append(field)
        parsed = _hours_value(value, work_hours_policy)
        if parsed is not None:
            return parsed, "parsed", field, 1

    if explicit_present or supplied_aliases:
        source = "man_hours" if explicit_present else supplied_aliases[0]
        return None, "invalid", source, 0
    return None, "missing", "", 0


def _normalise_employee_id(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return " ".join(text.split())


def _person_employee_id(row: Mapping[str, Any]) -> str:
    for field in _EMPLOYEE_ID_FIELDS:
        employee_id = _normalise_employee_id(row.get(field))
        if employee_id:
            return employee_id
    return ""


def _known_employee_ids_by_name(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _normalise_text(row.get("name"))
        employee_id = _person_employee_id(row)
        if name and employee_id:
            result[name].add(employee_id)
    return result


def _person_key(
    row: Mapping[str, Any],
    category: str,
    index: int,
    known_ids_by_name: Mapping[str, set[str]] | None = None,
) -> tuple[str, str]:
    employee_id = _person_employee_id(row)
    if employee_id:
        return ("employee_id", employee_id)
    name = _normalise_text(row.get("name"))
    if name:
        # A name-only occurrence may join one unambiguous, exactly matching ID
        # occurrence.  Multiple IDs with the same name remain distinct and the
        # name-only row is retained for review; no fuzzy identity inference is
        # ever performed.
        known_ids = set((known_ids_by_name or {}).get(name, set()))
        if len(known_ids) == 1:
            return ("employee_id", next(iter(known_ids)))
        return ("name", name)
    # Anonymous rows cannot be proven to be the same person, so retain each one.
    return ("anonymous", f"{category}:{index}")


def _merge_person(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    existing["occurrence_count"] = int(existing.get("occurrence_count") or 1) + int(
        incoming.get("occurrence_count") or 1
    )
    for list_field in ("source_names", "source_areas", "role_variants", "hours_variants"):
        current = existing.setdefault(list_field, [])
        for value in incoming.get(list_field, []):
            if value not in current:
                current.append(value)

    if not existing["role"] and incoming.get("role"):
        existing["role"] = incoming["role"]

    incoming_hours = incoming.get("hours")
    existing_hours = existing.get("hours")
    incoming_priority = int(incoming.get("hours_priority", 0))
    existing_priority = int(existing.get("hours_priority", 0))
    should_replace = False
    if incoming_hours is not None:
        if existing_hours is None or incoming_priority > existing_priority:
            should_replace = True
        elif incoming_priority == existing_priority and float(incoming_hours) > float(existing_hours):
            should_replace = True
    elif existing_hours is None:
        state_rank = {"missing": 0, "invalid": 1, "parsed": 2}
        should_replace = state_rank.get(str(incoming.get("hours_state")), 0) > state_rank.get(
            str(existing.get("hours_state")), 0
        )

    if should_replace:
        existing["hours"] = incoming_hours
        existing["hours_state"] = incoming.get("hours_state", "missing")
        existing["hours_source"] = incoming.get("hours_source", "")
        existing["hours_priority"] = incoming_priority


def _dedupe_people(
    rows: list[Mapping[str, Any]],
    category: str,
    *,
    known_ids_by_name: Mapping[str, set[str]] | None = None,
    work_hours_policy: Mapping[str, Any] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        relevant_fields = (
            "name",
            "man_hours",
            *_EMPLOYEE_ID_FIELDS,
            *_ROLE_FIELDS,
            *_HOURS_FIELDS,
        )
        if not any(_has_value(row.get(key)) for key in relevant_fields):
            continue
        key = _person_key(row, category, index, known_ids_by_name)
        hours, hours_state, hours_source, hours_priority = _person_hours(
            row, work_hours_policy
        )
        name = _clean_text(row.get("name"))
        role = _person_role(row)
        source_area = _clean_text(row.get("_source_area"))
        incoming = {
            "employee_id": key[1] if key[0] == "employee_id" else "",
            "identity_method": key[0],
            "name": name,
            "role": role,
            "hours": hours,
            "hours_state": hours_state,
            "hours_source": hours_source,
            "hours_priority": hours_priority,
            "occurrence_count": 1,
            "source_names": [name] if name else [],
            "source_areas": [source_area] if source_area else [],
            "role_variants": [role] if role else [],
            "hours_variants": [hours] if hours is not None else [],
        }
        existing = people.get(key)
        if existing is None:
            people[key] = incoming
            continue
        _merge_person(existing, incoming)
    return people


def _hours_completeness(people: Mapping[Any, Mapping[str, Any]]) -> dict[str, Any]:
    parsed = sum(person.get("hours") is not None for person in people.values())
    zero = sum(person.get("hours") == 0 for person in people.values())
    missing = sum(person.get("hours_state") == "missing" for person in people.values())
    invalid = sum(person.get("hours_state") == "invalid" for person in people.values())
    unresolved = missing + invalid
    return {
        "person_count": len(people),
        "parsed_hours_count": parsed,
        "zero_hours_count": zero,
        "missing_hours_count": missing,
        "invalid_hours_count": invalid,
        # Retain the combined unresolved count expected by existing consumers.
        "unparsed_hours_count": unresolved,
        "hours_complete": unresolved == 0,
    }


def _daily_manpower(
    payload: Mapping[str, Any],
    report_date: str,
    *,
    work_hours_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    direct_rows: list[Mapping[str, Any]] = []
    indirect_rows: list[Mapping[str, Any]] = []

    def sourced(row: Mapping[str, Any], area: str) -> dict[str, Any]:
        result = dict(row)
        result["_source_area"] = _clean_text(
            row.get("source_area") or row.get("_source_area") or area
        )
        return result

    global_indirect = payload.get("indirect_manpower")
    if isinstance(global_indirect, list):
        indirect_rows.extend(
            sourced(row, "General")
            for row in global_indirect
            if isinstance(row, Mapping)
        )

    areas = payload.get("areas")
    if isinstance(areas, list):
        for area in areas:
            if not isinstance(area, Mapping):
                continue
            area_name = _clean_text(area.get("id")) or "General"
            direct = area.get("manpower")
            if isinstance(direct, list):
                direct_rows.extend(
                    sourced(row, area_name) for row in direct if isinstance(row, Mapping)
                )
            indirect = area.get("indirect_manpower")
            if isinstance(indirect, list):
                indirect_rows.extend(
                    sourced(row, area_name)
                    for row in indirect
                    if isinstance(row, Mapping)
                )

    known_ids_by_name = _known_employee_ids_by_name([*direct_rows, *indirect_rows])
    direct = _dedupe_people(
        direct_rows,
        "direct",
        known_ids_by_name=known_ids_by_name,
        work_hours_policy=work_hours_policy,
    )
    indirect = _dedupe_people(
        indirect_rows,
        "indirect",
        known_ids_by_name=known_ids_by_name,
        work_hours_policy=work_hours_policy,
    )

    # The same employee can appear in the global Indirect table and again in an
    # area Direct table on the same day.  Treat that as one physical person.
    # Area/direct assignment takes precedence for category reporting, while the
    # most authoritative/longest supplied shift is retained.  This guarantees:
    #     Direct HC + Indirect HC == Total HC
    # and the same reconciliation for person-days/man-hours.
    overlap_keys = sorted(set(direct).intersection(indirect), key=str)
    cross_category_duplicates: list[dict[str, str]] = []
    for key in overlap_keys:
        indirect_person = indirect[key]
        _merge_person(direct[key], indirect_person)
        person_name = _clean_text(direct[key].get("name")) or _clean_text(indirect_person.get("name"))
        cross_category_duplicates.append({
            "name": person_name or "Unnamed person",
            "employee_id": _clean_text(direct[key].get("employee_id")),
            "kept_as": "direct",
        })
        indirect.pop(key, None)

    combined = dict(direct)
    for key, person in indirect.items():
        combined[key] = dict(person)

    def hours_total(people: Mapping[Any, Mapping[str, Any]]) -> float:
        return round(sum(float(person["hours"]) for person in people.values() if person["hours"] is not None), 2)

    direct_completeness = _hours_completeness(direct)
    indirect_completeness = _hours_completeness(indirect)
    total_completeness = _hours_completeness(combined)
    explicit_status = _normalise_text(payload.get("manpower_status"))
    status = explicit_status if explicit_status in _MANPOWER_STATUSES else ""
    identity_review: list[dict[str, Any]] = []
    if combined and status in {"none_reported", "not_supplied"}:
        identity_review.append({
            "severity": "warning",
            "code": "manpower_status_conflict",
            "message": "Manpower rows were supplied although the source status did not report manpower; rows were retained.",
        })
        status = "reported"
    elif not status:
        status = "reported" if combined else "not_supplied"

    for name_key, employee_ids in sorted(known_ids_by_name.items()):
        if len(employee_ids) > 1:
            identity_review.append({
                "severity": "warning",
                "code": "same_name_distinct_employee_ids",
                "name": next(
                    (
                        _clean_text(row.get("name"))
                        for row in [*direct_rows, *indirect_rows]
                        if _normalise_text(row.get("name")) == name_key
                    ),
                    name_key,
                ),
                "employee_ids": sorted(employee_ids),
                "message": "The same exact name is associated with multiple employee IDs; identities were kept separate.",
            })

    for person in combined.values():
        names = list(person.get("source_names") or [])
        roles = list(person.get("role_variants") or [])
        areas_for_person = list(person.get("source_areas") or [])
        hours_variants = list(person.get("hours_variants") or [])
        if person.get("employee_id") and len({_normalise_text(name) for name in names}) > 1:
            identity_review.append({
                "severity": "warning",
                "code": "employee_id_name_variation",
                "employee_id": person["employee_id"],
                "names": names,
                "message": "One employee ID has multiple exact source-name variants; the ID was authoritative.",
            })
        if len({_normalise_text(role) for role in roles}) > 1:
            identity_review.append({
                "severity": "warning",
                "code": "employee_role_variation",
                "name": person.get("name") or "Unnamed person",
                "roles": roles,
                "message": "One exact employee identity has multiple source roles; the first supplied role was retained.",
            })
        if len(hours_variants) > 1:
            identity_review.append({
                "severity": "info",
                "code": "employee_hours_variation",
                "name": person.get("name") or "Unnamed person",
                "hours": hours_variants,
                "message": "One exact employee identity has multiple source hour values; the authoritative or longest value was retained.",
            })
        if len(areas_for_person) > 1 and person.get("identity_method") == "name":
            identity_review.append({
                "severity": "info",
                "code": "exact_name_multiple_areas",
                "name": person.get("name") or "Unnamed person",
                "source_areas": areas_for_person,
                "message": "An exact normalized name occurred in multiple areas and was counted once for the day.",
            })

    supplied = status != "not_supplied"
    day = {
        "date": report_date,
        "manpower_status": status,
        "supplied": supplied,
        "direct_headcount": len(direct) if supplied else None,
        "indirect_headcount": len(indirect) if supplied else None,
        "total_headcount": len(combined) if supplied else None,
        "direct_man_hours": hours_total(direct) if supplied else None,
        "indirect_man_hours": hours_total(indirect) if supplied else None,
        "total_man_hours": hours_total(combined) if supplied else None,
        "parsed_hours_count": total_completeness["parsed_hours_count"],
        "zero_hours_count": total_completeness["zero_hours_count"],
        "missing_hours_count": total_completeness["missing_hours_count"],
        "invalid_hours_count": total_completeness["invalid_hours_count"],
        "unparsed_hours_count": total_completeness["unparsed_hours_count"],
        "headcount_complete": supplied,
        "hours_complete": supplied and total_completeness["hours_complete"],
        "hours_completeness": {
            "direct": direct_completeness,
            "indirect": indirect_completeness,
            "total": total_completeness,
        },
        "cross_category_duplicate_count": len(cross_category_duplicates),
        "cross_category_duplicates": cross_category_duplicates,
        "identity_review_required": any(
            row.get("severity") == "warning" for row in identity_review
        ),
        "identity_review": identity_review,
    }

    role_rows = []
    for person in (combined.values() if supplied else []):
        role_rows.append(
            {
                "date": report_date,
                "role": person["role"] or "Unspecified",
                "man_hours": person["hours"],
                "hours_state": person["hours_state"],
            }
        )
    return day, role_rows


def _progress_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace("%", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _progress_key(row: Mapping[str, Any]) -> str:
    for field in ("item_id", "wbs_id", "id"):
        value = _normalise_text(row.get(field))
        if value:
            return f"id:{value}"
    description = _normalise_text(row.get("description"))
    return f"description:{description}" if description else ""


def _aggregate_progress(selected: list[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    histories: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    key_order: list[str] = []
    for report_date, record in selected:
        payload = _payload(record)
        progress_visibility = payload.get("show_overall_progress")
        if progress_visibility is False or str(progress_visibility).strip().casefold() in {
            "false", "0", "off", "no",
        }:
            continue
        rows = payload.get("overall_progress")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = _progress_key(row)
            if not key:
                continue
            if key not in histories:
                key_order.append(key)
            histories[key].append((report_date, row))

    result_rows = []
    source_dates: set[str] = set()
    for key in key_order:
        history = histories[key]
        history.sort(key=lambda item: item[0])
        earliest_date, earliest = history[0]
        latest_date, latest = history[-1]
        source_dates.update(report_date for report_date, _ in history)

        values = {field: _progress_number(latest.get(field)) for field in _PROGRESS_NUMERIC_FIELDS}
        # Preserve the latest Daily Report snapshot exactly.  The source table's
        # ``This Period`` and ``Deviation`` values are evidence, not values for the
        # periodic compiler to silently recompute/reconcile.  First/last dates are
        # retained only as provenance.
        previous_plan = values["cumulative_previous_plan"]
        previous_actual = values["cumulative_previous_actual"]
        cumulative_plan = values["cumulative_to_date_plan"]
        cumulative_actual = values["cumulative_to_date_actual"]
        period_plan = values["this_period_plan"]
        period_actual = values["this_period_actual"]
        deviation = values["deviation"]
        row_result = {
            "key": key,
            "description": _clean_text(latest.get("description")),
            "duration": _clean_text(latest.get("duration")),
            "start": _clean_text(latest.get("start")),
            "finish": _clean_text(latest.get("finish")),
            "weight_factor": values["weight_factor"],
            "cumulative_previous_plan": previous_plan,
            "cumulative_previous_actual": previous_actual,
            "this_period_plan": period_plan,
            "this_period_actual": period_actual,
            "cumulative_to_date_plan": cumulative_plan,
            "cumulative_to_date_actual": cumulative_actual,
            "deviation": deviation,
            "is_total": bool(latest.get("is_total")),
            "source_period_label": _clean_text(latest.get("source_period_label")) or "This Period",
            "first_source_date": earliest_date,
            "last_source_date": latest_date,
        }
        result_rows.append(row_result)

    explicit_total = next((row for row in reversed(result_rows) if row.get("is_total")), None)
    totals: dict[str, float | None] = {}
    if explicit_total is not None:
        for field in _PROGRESS_TOTAL_FIELDS:
            totals[field] = explicit_total.get(field)
        totals["deviation"] = explicit_total.get("deviation")
        totals["weight_total"] = None
        totals_method = "latest explicit Overall Progress total row"
    else:
        for field in _PROGRESS_TOTAL_FIELDS:
            weighted = [
                row["weight_factor"] * row[field] / 100.0
                for row in result_rows
                if row["weight_factor"] is not None and row[field] is not None
            ]
            totals[field] = round(sum(weighted), 6) if weighted else None
        plan = totals.get("cumulative_to_date_plan")
        actual = totals.get("cumulative_to_date_actual")
        totals["deviation"] = round(actual - plan, 6) if plan is not None and actual is not None else None
        weights = [row["weight_factor"] for row in result_rows if row["weight_factor"] is not None]
        totals["weight_total"] = round(sum(weights), 6) if weights else None
        totals_method = "weighted fallback from latest detail rows"

    return {
        "available": bool(result_rows),
        "rows": result_rows,
        "totals": totals,
        "source_dates": sorted(source_dates),
        "latest_snapshot_date": max(source_dates) if source_dates else None,
        "source_period_label": "This Period",
        "method": f"latest Daily Report snapshot preserved; totals: {totals_method}",
    }


def _iter_constraint_facts(value: Any) -> Iterable[dict[str, str]]:
    """Yield source constraint facts without inferring action or closeout data."""

    if isinstance(value, str):
        text = _clean_text(value)
        if text:
            yield {"text": text}
        return
    if isinstance(value, Mapping):
        text = _clean_text(
            value.get("text")
            or value.get("constraint")
            or value.get("concern")
            or value.get("description")
        )
        if not text:
            return
        yield {
            "text": text,
            "status": _clean_text(value.get("status")),
            "action": _clean_text(
                value.get("corrective_action") or value.get("action")
            ),
            "pic": _clean_text(value.get("pic") or value.get("owner")),
            "target_date": _clean_text(value.get("target_date")),
            "closed_date": _clean_text(value.get("closed_date")),
        }
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_constraint_facts(item)


def _is_no_constraint_text(value: Any) -> bool:
    return _normalise_text(value) in {
        "",
        "-",
        "n/a",
        "na",
        "nil",
        "none",
        "not applicable",
        "no issue",
        "no issues",
        "no constraint",
        "no constraints",
        "no constraint reported",
        "no constraints reported",
        "tidak ada",
    }


def _constraint_register(constraints: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Consolidate only exact normalized constraint text and reporting area.

    Similar wording is intentionally kept separate.  A neutral ``reported``
    state is used when the source has no explicit status; the compiler never
    invents an open/closed state, owner, target date, or corrective action.
    """

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for raw in constraints:
        if not isinstance(raw, Mapping):
            continue
        text = _clean_text(raw.get("text"))
        if _is_no_constraint_text(text):
            continue
        reporting_area = _clean_text(raw.get("reporting_area") or raw.get("area")) or "General"
        source_area = _clean_text(raw.get("source_area") or raw.get("area")) or "General"
        key = (_normalise_text(reporting_area), _normalise_text(text))
        if key not in grouped:
            digest = hashlib.sha256(f"{key[0]}\0{key[1]}".encode("utf-8")).hexdigest()[:20]
            grouped[key] = {
                "constraint_id": f"constraint-{digest}",
                "id": f"constraint-{digest}",
                "area": reporting_area,
                "source_area": source_area,
                "source_areas": [],
                "reporting_area": reporting_area,
                "description": text,
                "text": text,
                "first_reported_date": "",
                "last_reported_date": "",
                "reported_dates": [],
                "occurrence_count": 0,
                "status": "reported",
                "corrective_action": "",
                "action": "",
                "pic": "",
                "target_date": "",
                "closed_date": "",
                "source_report_ids": [],
                "source_paths": [],
                "matching_method": "exact_normalized_area_and_text",
            }
            order.append(key)
        item = grouped[key]
        item["occurrence_count"] += 1
        if source_area not in item["source_areas"]:
            item["source_areas"].append(source_area)
        report_date = _clean_text(raw.get("date"))
        if report_date and report_date not in item["reported_dates"]:
            item["reported_dates"].append(report_date)
        source_report_id = _clean_text(raw.get("source_report_id"))
        if source_report_id and source_report_id not in item["source_report_ids"]:
            item["source_report_ids"].append(source_report_id)
        source_path = _clean_text(raw.get("source_path"))
        if source_path and source_path not in item["source_paths"]:
            item["source_paths"].append(source_path)

        # Only explicitly supplied management fields may replace the neutral
        # defaults.  Because source rows are date-ordered, the latest supplied
        # value is retained while all occurrence provenance remains available.
        explicit_status = _clean_text(raw.get("status"))
        action = _clean_text(raw.get("action") or raw.get("corrective_action"))
        if explicit_status:
            item["status"] = explicit_status
        if action:
            item["corrective_action"] = action
            item["action"] = action
        for field in ("pic", "target_date", "closed_date"):
            value = _clean_text(raw.get(field))
            if value:
                item[field] = value

    result = [grouped[key] for key in order]
    for item in result:
        item["reported_dates"].sort()
        item["first_reported_date"] = (
            item["reported_dates"][0] if item["reported_dates"] else ""
        )
        item["last_reported_date"] = (
            item["reported_dates"][-1] if item["reported_dates"] else ""
        )
    return result


@dataclass
class _PeriodFacts:
    """Mutable collection used only while folding selected Daily Reports."""

    activities: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    remarks: list[dict[str, Any]] = field(default_factory=list)
    weather_daily: list[dict[str, Any]] = field(default_factory=list)
    constraint_daily: list[dict[str, Any]] = field(default_factory=list)
    daily_manpower: list[dict[str, Any]] = field(default_factory=list)
    role_rows: list[dict[str, Any]] = field(default_factory=list)
    planned_next_week: list[dict[str, Any]] = field(default_factory=list)
    planned_next_month: list[dict[str, Any]] = field(default_factory=list)


def _select_period_records(
    records: Iterable[Mapping[str, Any]],
    *,
    date_from: str,
    date_to: str,
    project_no: str | None,
    project_title: str | None,
) -> tuple[list[tuple[str, Mapping[str, Any]]], int, list[str], list[str]]:
    """Filter records and select the latest deterministic revision per date."""
    wanted_no = _normalise_text(project_no) if project_no is not None else None
    wanted_title = _normalise_text(project_title) if project_title is not None else None
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    ignored_invalid = 0
    seen_projects: set[tuple[str, str]] = set()

    for record in records:
        if not isinstance(record, Mapping):
            ignored_invalid += 1
            continue
        try:
            report_date = _iso_date(_metadata(record, "date"))
        except ValueError:
            ignored_invalid += 1
            continue
        if report_date < date_from or report_date > date_to:
            continue

        record_no = _normalise_text(_metadata(record, "project_no"))
        record_title = _normalise_text(_metadata(record, "project_title"))
        if wanted_no is not None and record_no != wanted_no:
            continue
        if wanted_title is not None and record_title != wanted_title:
            continue
        seen_projects.add((record_no, record_title))
        groups[report_date].append(record)

    if wanted_no is None and wanted_title is None:
        nonempty_projects = {pair for pair in seen_projects if any(pair)}
        if len(nonempty_projects) > 1:
            raise ValueError(
                "A project_no or project_title filter is required for mixed-project records."
            )

    selected: list[tuple[str, Mapping[str, Any]]] = []
    duplicate_dates: list[str] = []
    superseded_records: list[str] = []
    for report_date in sorted(groups):
        candidates = groups[report_date]
        chosen = max(candidates, key=_record_sort_key)
        selected.append((report_date, chosen))
        if len(candidates) <= 1:
            continue
        duplicate_dates.append(report_date)
        superseded_records.extend(
            str(candidate.get("report_id", ""))
            for candidate in candidates
            if candidate is not chosen
        )

    return selected, ignored_invalid, duplicate_dates, superseded_records


def _period_coverage(
    selected: list[tuple[str, Mapping[str, Any]]],
    *,
    date_from: str,
    date_to: str,
    expected_dates: Iterable[str] | None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Resolve expected, covered, missing, and unexpected period dates."""
    if expected_dates is None:
        expected = _date_sequence(date_from, date_to)
    else:
        expected = sorted({_iso_date(value, "expected date") for value in expected_dates})
        outside = [value for value in expected if value < date_from or value > date_to]
        if outside:
            raise ValueError("expected_dates must stay inside date_from/date_to.")

    covered = [report_date for report_date, _ in selected]
    expected_set = set(expected)
    covered_set = set(covered)
    missing = [value for value in expected if value not in covered_set]
    extra = [value for value in covered if value not in expected_set]
    return expected, covered, missing, extra


def _append_area_activities(
    facts: _PeriodFacts,
    *,
    area: Mapping[str, Any],
    area_index: int,
    area_name: str,
    report_date: str,
    source_report_id: str,
) -> None:
    status_map = _activity_status_map(area)
    descriptions = _iter_text_values(area.get("activities_today"))
    for activity_index, description in enumerate(descriptions):
        description = _clean_period_text(description)
        if not description:
            continue
        area_meta = reporting_activity_area(area_name, description)
        item = {
            "date": report_date,
            "area": area_name,
            "source_area": area_meta["source_area"],
            "reporting_area": area_meta["reporting_area"],
            "area_mapping_method": area_meta["method"],
            "area_mapping_confidence": area_meta["confidence"],
            "area_review_required": bool(area_meta["review_required"]),
            "description": description,
            "source_report_id": source_report_id,
            "source_path": f"$.areas[{area_index}].activities_today[{activity_index}]",
        }
        status = status_map.get(_normalise_text(description))
        if status:
            item["status"] = status
        facts.activities.append(item)


def _append_area_constraints(
    facts: _PeriodFacts,
    *,
    area: Mapping[str, Any],
    area_index: int,
    area_name: str,
    area_meta: Mapping[str, Any],
    report_date: str,
    source_report_id: str,
) -> None:
    for constraint_index, fact in enumerate(_iter_constraint_facts(area.get("constraints"))):
        text = _clean_period_text(fact.get("text"))
        if not text:
            continue
        constraint = {
            "date": report_date,
            "area": area_name,
            "source_area": area_meta["source_area"],
            "reporting_area": area_meta["reporting_area"],
            "area_mapping_method": area_meta["method"],
            "text": text,
            "source_report_id": source_report_id,
            "source_path": f"$.areas[{area_index}].constraints[{constraint_index}]",
        }
        for key in ("status", "action", "pic", "target_date", "closed_date"):
            if fact.get(key):
                constraint[key] = fact[key]
        facts.constraints.append(constraint)


def _append_area_remarks(
    facts: _PeriodFacts,
    *,
    area: Mapping[str, Any],
    area_index: int,
    area_name: str,
    area_meta: Mapping[str, Any],
    report_date: str,
    source_report_id: str,
) -> None:
    for remark_index, text in enumerate(_iter_text_values(area.get("remarks"))):
        text = _clean_period_text(text)
        if not text:
            continue
        facts.remarks.append({
            "date": report_date,
            "area": area_name,
            "source_area": area_meta["source_area"],
            "reporting_area": area_meta["reporting_area"],
            "area_mapping_method": area_meta["method"],
            "text": text,
            "source_report_id": source_report_id,
            "source_path": f"$.areas[{area_index}].remarks[{remark_index}]",
        })


def _append_area_lookahead(
    facts: _PeriodFacts,
    *,
    area: Mapping[str, Any],
    area_index: int,
    area_name: str,
    report_date: str,
    source_report_id: str,
) -> None:
    # Activity Tomorrow remains separate because it is not automatically a
    # next-week or next-month commitment.
    targets = (
        ("planned_next_week", facts.planned_next_week),
        ("next_week_activities", facts.planned_next_week),
        ("planned_next_month", facts.planned_next_month),
        ("next_month_activities", facts.planned_next_month),
    )
    for key, target in targets:
        for plan_index, description in enumerate(_iter_text_values(area.get(key))):
            area_meta = reporting_activity_area(area_name, description)
            target.append({
                "source_date": report_date,
                "area": area_name,
                "source_area": area_meta["source_area"],
                "reporting_area": area_meta["reporting_area"],
                "area_mapping_method": area_meta["method"],
                "description": description,
                "source_report_id": source_report_id,
                "source_path": f"$.areas[{area_index}].{key}[{plan_index}]",
            })


def _append_area_facts(
    facts: _PeriodFacts,
    areas: Any,
    *,
    report_date: str,
    source_report_id: str,
) -> None:
    if not isinstance(areas, list):
        return
    for area_index, area in enumerate(areas):
        if not isinstance(area, Mapping):
            continue
        area_name = _clean_text(area.get("id")) or "Unspecified"
        area_meta = reporting_activity_area(area_name, "")
        _append_area_activities(
            facts,
            area=area,
            area_index=area_index,
            area_name=area_name,
            report_date=report_date,
            source_report_id=source_report_id,
        )
        _append_area_constraints(
            facts,
            area=area,
            area_index=area_index,
            area_name=area_name,
            area_meta=area_meta,
            report_date=report_date,
            source_report_id=source_report_id,
        )
        _append_area_remarks(
            facts,
            area=area,
            area_index=area_index,
            area_name=area_name,
            area_meta=area_meta,
            report_date=report_date,
            source_report_id=source_report_id,
        )
        _append_area_lookahead(
            facts,
            area=area,
            area_index=area_index,
            area_name=area_name,
            report_date=report_date,
            source_report_id=source_report_id,
        )


def _append_global_constraints(
    facts: _PeriodFacts,
    payload: Mapping[str, Any],
    *,
    report_date: str,
    source_report_id: str,
) -> None:
    for field_name in ("global_constraints", "constraints"):
        for constraint_index, fact in enumerate(
            _iter_constraint_facts(payload.get(field_name))
        ):
            text = _clean_period_text(fact.get("text"))
            if not text:
                continue
            constraint = {
                "date": report_date,
                "area": "General",
                "source_area": "General",
                "reporting_area": "General",
                "area_mapping_method": "source_fallback",
                "text": text,
                "source_report_id": source_report_id,
                "source_path": f"$.{field_name}[{constraint_index}]",
            }
            for key in ("status", "action", "pic", "target_date", "closed_date"):
                if fact.get(key):
                    constraint[key] = fact[key]
            facts.constraints.append(constraint)


def _append_global_remarks(
    facts: _PeriodFacts,
    payload: Mapping[str, Any],
    *,
    report_date: str,
    source_report_id: str,
) -> None:
    for field_name in ("global_remarks", "remarks"):
        for remark_index, text in enumerate(_iter_text_values(payload.get(field_name))):
            text = _clean_period_text(text)
            if not text:
                continue
            facts.remarks.append({
                "date": report_date,
                "area": "General",
                "source_area": "General",
                "reporting_area": "General",
                "area_mapping_method": "source_fallback",
                "text": text,
                "source_report_id": source_report_id,
                "source_path": f"$.{field_name}[{remark_index}]",
            })


def _daily_constraint_status(
    payload: Mapping[str, Any],
    day_constraints: list[dict[str, Any]],
) -> str:
    status = _normalise_text(payload.get("constraint_status"))
    real_constraints_supplied = any(
        not _is_no_constraint_text(row.get("text")) for row in day_constraints
    )
    explicit_none_reported = any(
        _clean_text(row.get("text")) and _is_no_constraint_text(row.get("text"))
        for row in day_constraints
    )
    if real_constraints_supplied:
        return "reported"
    if status in {"none_reported", "reported", "not_supplied"}:
        return status
    return "none_reported" if explicit_none_reported else "not_supplied"


def _collect_period_facts(
    selected: list[tuple[str, Mapping[str, Any]]],
    *,
    work_hours_policy: Mapping[str, Any],
) -> _PeriodFacts:
    """Fold selected Daily Reports into period-level fact collections."""
    facts = _PeriodFacts()
    for report_date, record in selected:
        payload = _payload(record)
        source_report_id = str(record.get("report_id") or "")
        weather_item = _weather_row(payload, report_date)
        if weather_item is not None:
            weather_item["source_report_id"] = source_report_id
            weather_item["source_path"] = "$.weather"
            facts.weather_daily.append(weather_item)

        constraint_count_before = len(facts.constraints)
        _append_area_facts(
            facts,
            payload.get("areas"),
            report_date=report_date,
            source_report_id=source_report_id,
        )
        _append_global_constraints(
            facts,
            payload,
            report_date=report_date,
            source_report_id=source_report_id,
        )
        _append_global_remarks(
            facts,
            payload,
            report_date=report_date,
            source_report_id=source_report_id,
        )
        day_constraints = [
            row
            for row in facts.constraints[constraint_count_before:]
            if isinstance(row, Mapping)
        ]
        facts.constraint_daily.append({
            "date": report_date,
            "status": _daily_constraint_status(payload, day_constraints),
        })

        day, day_roles = _daily_manpower(
            payload,
            report_date,
            work_hours_policy=work_hours_policy,
        )
        facts.daily_manpower.append(day)
        facts.role_rows.extend(day_roles)
    return facts


def _activity_indexes(
    activities: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    """Index activity rows by source area and normalized reporting area."""
    by_source_area: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_reporting_area: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in activities:
        row = {"date": item["date"], "description": item["description"]}
        if item.get("status"):
            row["status"] = item["status"]
        by_source_area[item["area"]].append(row)
        by_reporting_area[
            _clean_text(item.get("reporting_area")) or item["area"]
        ].append(dict(row))
    return by_source_area, by_reporting_area


def _append_top_level_lookahead(
    selected: list[tuple[str, Mapping[str, Any]]],
    planned_next_week: list[dict[str, Any]],
    planned_next_month: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add explicit report-level look-ahead rows to area-level rows."""
    targets = (
        ("planned_next_week", planned_next_week),
        ("next_week_activities", planned_next_week),
        ("planned_next_month", planned_next_month),
        ("next_month_activities", planned_next_month),
    )
    for report_date, record in selected:
        payload = _payload(record)
        source_report_id = str(record.get("report_id") or "")
        for key, target in targets:
            for plan_index, description in enumerate(_iter_text_values(payload.get(key))):
                target.append({
                    "source_date": report_date,
                    "area": "",
                    "source_area": "",
                    "reporting_area": "",
                    "area_mapping_method": "source_fallback",
                    "description": description,
                    "source_report_id": source_report_id,
                    "source_path": f"$.{key}[{plan_index}]",
                })
    return (
        _dedupe_entries(planned_next_week, "source_date", "area", "description"),
        _dedupe_entries(planned_next_month, "source_date", "area", "description"),
    )


def _latest_tomorrow_activities(
    selected: list[tuple[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Return tomorrow activities only from the latest selected Daily Report."""
    tomorrow_activities: list[dict[str, Any]] = []
    if not selected:
        return tomorrow_activities

    last_date, last_record = selected[-1]
    areas = _payload(last_record).get("areas")
    if not isinstance(areas, list):
        return tomorrow_activities
    for area in areas:
        if not isinstance(area, Mapping):
            continue
        area_name = _clean_text(area.get("id")) or "Unspecified"
        for description in _iter_text_values(area.get("activities_tomorrow")):
            area_meta = reporting_activity_area(area_name, description)
            tomorrow_activities.append({
                "source_date": last_date,
                "area": area_name,
                "source_area": area_meta["source_area"],
                "reporting_area": area_meta["reporting_area"],
                "area_mapping_method": area_meta["method"],
                "description": description,
                "source_report_id": str(last_record.get("report_id") or ""),
                "source_path": f"$.areas[{areas.index(area)}].activities_tomorrow",
            })
    return _dedupe_entries(
        tomorrow_activities,
        "source_date",
        "area",
        "description",
    )


def _role_totals(role_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize person-days and parsed hours by normalized role."""
    role_summary: dict[str, dict[str, Any]] = {}
    for row in role_rows:
        role = row["role"]
        key = _normalise_text(role)
        summary = role_summary.setdefault(
            key,
            {
                "role": role,
                "person_days": 0,
                "man_hours": 0.0,
                "parsed_hours_count": 0,
                "zero_hours_count": 0,
                "missing_hours_count": 0,
                "invalid_hours_count": 0,
                "unparsed_hours_count": 0,
                "hours_complete": True,
            },
        )
        summary["person_days"] += 1
        if row["man_hours"] is None:
            summary["unparsed_hours_count"] += 1
            if row.get("hours_state") == "invalid":
                summary["invalid_hours_count"] += 1
            else:
                summary["missing_hours_count"] += 1
            summary["hours_complete"] = False
            continue
        summary["parsed_hours_count"] += 1
        if row["man_hours"] == 0:
            summary["zero_hours_count"] += 1
        summary["man_hours"] += float(row["man_hours"])

    roles = sorted(role_summary.values(), key=lambda row: _normalise_text(row["role"]))
    for row in roles:
        row["man_hours"] = round(row["man_hours"], 2)
    return roles


def _supplied_manpower_sum(
    supplied_days: list[dict[str, Any]],
    field: str,
) -> float | None:
    if not supplied_days:
        return None
    return round(
        sum(float(day[field]) for day in supplied_days if day.get(field) is not None),
        2,
    )


def _supplied_manpower_average(
    supplied_days: list[dict[str, Any]],
    field: str,
) -> float | None:
    total = _supplied_manpower_sum(supplied_days, field)
    if total is None or not supplied_days:
        return None
    return round(total / len(supplied_days), 2)


def _manpower_summary(
    daily_manpower: list[dict[str, Any]],
    *,
    expected: list[str],
    covered: list[str],
    missing: list[str],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Build manpower totals, coverage, and identity-review metadata."""
    supplied_days = [day for day in daily_manpower if day.get("supplied")]
    supplied_dates = [day["date"] for day in supplied_days]
    not_supplied_dates = [day["date"] for day in daily_manpower if not day.get("supplied")]
    headcount_complete = bool(expected) and not missing and len(supplied_days) == len(daily_manpower)
    hours_complete = (
        headcount_complete
        and bool(supplied_days)
        and all(day["hours_complete"] for day in supplied_days)
    )
    peak_values = [
        int(day["total_headcount"])
        for day in supplied_days
        if day.get("total_headcount") is not None
    ]
    identity_review = [
        {"date": day["date"], **row}
        for day in daily_manpower
        for row in day.get("identity_review", [])
        if isinstance(row, Mapping)
    ]
    totals = {
        "direct_person_days": _supplied_manpower_sum(supplied_days, "direct_headcount"),
        "indirect_person_days": _supplied_manpower_sum(supplied_days, "indirect_headcount"),
        "total_person_days": _supplied_manpower_sum(supplied_days, "total_headcount"),
        "direct_man_hours": _supplied_manpower_sum(supplied_days, "direct_man_hours"),
        "indirect_man_hours": _supplied_manpower_sum(supplied_days, "indirect_man_hours"),
        "total_man_hours": _supplied_manpower_sum(supplied_days, "total_man_hours"),
        "known_direct_man_hours": _supplied_manpower_sum(supplied_days, "direct_man_hours"),
        "known_indirect_man_hours": _supplied_manpower_sum(supplied_days, "indirect_man_hours"),
        "known_total_man_hours": _supplied_manpower_sum(supplied_days, "total_man_hours"),
        "peak_headcount": max(peak_values) if peak_values else None,
        "average_daily_direct_headcount": _supplied_manpower_average(
            supplied_days, "direct_headcount"
        ),
        "average_daily_indirect_headcount": _supplied_manpower_average(
            supplied_days, "indirect_headcount"
        ),
        "average_daily_headcount": _supplied_manpower_average(
            supplied_days, "total_headcount"
        ),
        "average_headcount_denominator": "manpower_supplied_days",
        "manpower_supplied_day_count": len(supplied_days),
        "manpower_not_supplied_day_count": len(not_supplied_dates),
        "covered_daily_report_count": len(daily_manpower),
        "expected_day_count": len(expected),
        "headcount_complete": headcount_complete,
        "parsed_hours_count": sum(day["parsed_hours_count"] for day in daily_manpower),
        "zero_hours_count": sum(day["zero_hours_count"] for day in daily_manpower),
        "missing_hours_count": sum(day["missing_hours_count"] for day in daily_manpower),
        "invalid_hours_count": sum(day["invalid_hours_count"] for day in daily_manpower),
        "unparsed_hours_count": sum(day["unparsed_hours_count"] for day in daily_manpower),
        "hours_complete": hours_complete,
        "man_hours_partial": not hours_complete,
        "cross_category_duplicate_count": sum(
            int(day.get("cross_category_duplicate_count") or 0) for day in daily_manpower
        ),
        "identity_review_required": any(
            row.get("severity") == "warning" for row in identity_review
        ),
    }
    coverage = {
        "expected_dates": list(expected),
        "daily_report_covered_dates": list(covered),
        "missing_daily_report_dates": list(missing),
        "supplied_dates": supplied_dates,
        "reported_dates": [
            day["date"] for day in daily_manpower if day.get("manpower_status") == "reported"
        ],
        "none_reported_dates": [
            day["date"]
            for day in daily_manpower
            if day.get("manpower_status") == "none_reported"
        ],
        "not_supplied_dates": not_supplied_dates,
        "supplied_day_count": len(supplied_days),
        "headcount_complete": headcount_complete,
        "hours_complete": hours_complete,
    }
    return totals, coverage, identity_review


def _work_hours_method(policy: Mapping[str, Any]) -> str:
    if policy["mode"] == "elapsed_less_break":
        return (
            "explicit man_hours takes precedence; otherwise use elapsed shift range minus "
            f"{policy['break_minutes']:g} break minute(s) when elapsed time is at least "
            f"{policy['deduct_when_elapsed_gte_minutes']:g} minute(s); exact employee IDs take identity "
            "precedence, followed only by exact normalized names; no fuzzy identity merge is used"
        )
    return (
        "explicit man_hours takes precedence; otherwise use elapsed shift range without "
        "break deduction; exact employee IDs take identity precedence, followed only by exact "
        "normalized names; same-day direct/area assignment takes category precedence and the "
        "authoritative or longest supplied shift is retained; no fuzzy identity merge is used"
    )


def aggregate_monthly_records(
    records: Iterable[Mapping[str, Any]],
    *,
    date_from: str,
    date_to: str,
    project_no: str | None = None,
    project_title: str | None = None,
    expected_dates: Iterable[str] | None = None,
    work_hours_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate canonical records for one project and inclusive date range.

    When several revisions exist for a date, the highest revision wins; its
    generated timestamp and report ID are deterministic tie-breakers.
    """

    date_from = _iso_date(date_from, "date_from")
    date_to = _iso_date(date_to, "date_to")
    if date_from > date_to:
        raise ValueError("date_from cannot be after date_to.")
    effective_hours_policy = _normalise_work_hours_policy(work_hours_policy)
    (
        selected,
        ignored_invalid,
        duplicate_dates,
        superseded_records,
    ) = _select_period_records(
        records,
        date_from=date_from,
        date_to=date_to,
        project_no=project_no,
        project_title=project_title,
    )
    expected, covered, missing, extra = _period_coverage(
        selected,
        date_from=date_from,
        date_to=date_to,
        expected_dates=expected_dates,
    )

    facts = _collect_period_facts(
        selected,
        work_hours_policy=effective_hours_policy,
    )
    constraint_register_source = list(facts.constraints)
    activities = _dedupe_entries(facts.activities, "date", "area", "description")
    constraints = [
        row
        for row in _dedupe_entries(facts.constraints, "date", "area", "text")
        if not _is_no_constraint_text(row.get("text"))
    ]
    remarks = _dedupe_entries(facts.remarks, "date", "area", "text")

    activities_by_area, activities_by_reporting_area = _activity_indexes(activities)
    planned_next_week, planned_next_month = _append_top_level_lookahead(
        selected,
        facts.planned_next_week,
        facts.planned_next_month,
    )
    tomorrow_activities = _latest_tomorrow_activities(selected)
    last_report_date = covered[-1] if covered else None

    constraint_reporting = {
        "daily": facts.constraint_daily,
        "none_reported_dates": [
            row["date"]
            for row in facts.constraint_daily
            if row["status"] == "none_reported"
        ],
        "reported_dates": [
            row["date"] for row in facts.constraint_daily if row["status"] == "reported"
        ],
        "not_supplied_dates": [
            row["date"]
            for row in facts.constraint_daily
            if row["status"] == "not_supplied"
        ],
    }

    roles = _role_totals(facts.role_rows)
    manpower_totals, manpower_coverage, manpower_identity_review = _manpower_summary(
        facts.daily_manpower,
        expected=expected,
        covered=covered,
        missing=missing,
    )

    if selected:
        latest_payload = _payload(selected[-1][1])
        output_project_no = _clean_text(project_no) or _clean_text(latest_payload.get("project_no"))
        output_project_title = _clean_text(project_title) or _clean_text(
            latest_payload.get("project_title")
        )
    else:
        output_project_no = _clean_text(project_no)
        output_project_title = _clean_text(project_title)

    source_records = [
        {
            "date": report_date,
            "report_id": str(record.get("report_id", "")),
            "username": str(record.get("username", "")),
            "revision": _revision(record),
            "generated_at": str(record.get("generated_at", "")),
        }
        for report_date, record in selected
    ]
    constraints_register = _constraint_register(constraint_register_source)
    hours_method = _work_hours_method(effective_hours_policy)

    return {
        "schema_version": 1,
        "record_type": "monthly_report_aggregate",
        "project_no": output_project_no,
        "project_title": output_project_title,
        "period": {"date_from": date_from, "date_to": date_to},
        "coverage": {
            "expected_dates": expected,
            "covered_dates": covered,
            "missing_dates": missing,
            "extra_dates": extra,
            "duplicate_dates": duplicate_dates,
            "is_partial": bool(missing),
            "selected_record_count": len(selected),
            "ignored_invalid_record_count": ignored_invalid,
            "superseded_report_ids": sorted(value for value in superseded_records if value),
            "last_report_date": last_report_date,
        },
        "source_records": source_records,
        "activities": activities,
        "activities_by_area": [
            {"area": area, "activities": values}
            for area, values in sorted(activities_by_area.items(), key=lambda item: _normalise_text(item[0]))
        ],
        "activities_by_reporting_area": [
            {"reporting_area": area, "area": area, "activities": values}
            for area, values in sorted(
                activities_by_reporting_area.items(),
                key=lambda item: _normalise_text(item[0]),
            )
        ],
        "tomorrow_activities": tomorrow_activities,
        "planned_next_week": planned_next_week,
        "planned_next_month": planned_next_month,
        "constraints": constraints,
        "constraint_register": constraints_register,
        "remarks": remarks,
        "weather": facts.weather_daily,
        "constraint_reporting": constraint_reporting,
        "manpower": {
            "daily": facts.daily_manpower,
            "totals": manpower_totals,
            "roles": roles,
            "coverage": manpower_coverage,
            "identity_review": manpower_identity_review,
            "work_hours_policy": effective_hours_policy,
            "hours_method": hours_method,
        },
        "overall_progress": _aggregate_progress(selected),
    }
