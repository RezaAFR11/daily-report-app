"""Pure aggregation of canonical daily records into a monthly data model."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any


_TIME_RANGE = re.compile(
    r"(?P<start_h>\d{1,2}):(?P<start_m>\d{2})\s*(?:-|\u2013|\u2014|\u2212)\s*"
    r"(?P<end_h>\d{1,2}):(?P<end_m>\d{2})"
)
_ROLE_FIELDS = ("role", "position", "role_position")
_HOURS_FIELDS = ("hours", "working_hours", "work_hours")
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


def _hours_value(value: Any) -> float | None:
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
            minutes += 24 * 60
        return minutes / 60.0

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


def _person_hours(row: Mapping[str, Any]) -> tuple[float | None, str, str, int]:
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
        parsed = _hours_value(value)
        if parsed is not None:
            return parsed, "parsed", field, 1

    if explicit_present or supplied_aliases:
        source = "man_hours" if explicit_present else supplied_aliases[0]
        return None, "invalid", source, 0
    return None, "missing", "", 0


def _person_key(row: Mapping[str, Any], category: str, index: int) -> tuple[str, str]:
    name = _normalise_text(row.get("name"))
    if name:
        return ("name", name)
    # Anonymous rows cannot be proven to be the same person, so retain each one.
    return ("anonymous", f"{category}:{index}")


def _merge_person(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
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


def _dedupe_people(rows: list[Mapping[str, Any]], category: str) -> dict[tuple[str, str], dict[str, Any]]:
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        relevant_fields = ("name", "man_hours", *_ROLE_FIELDS, *_HOURS_FIELDS)
        if not any(_has_value(row.get(key)) for key in relevant_fields):
            continue
        key = _person_key(row, category, index)
        hours, hours_state, hours_source, hours_priority = _person_hours(row)
        incoming = {
            "name": _clean_text(row.get("name")),
            "role": _person_role(row),
            "hours": hours,
            "hours_state": hours_state,
            "hours_source": hours_source,
            "hours_priority": hours_priority,
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


def _daily_manpower(payload: Mapping[str, Any], report_date: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    direct_rows: list[Mapping[str, Any]] = []
    indirect_rows: list[Mapping[str, Any]] = []
    global_indirect = payload.get("indirect_manpower")
    if isinstance(global_indirect, list):
        indirect_rows.extend(row for row in global_indirect if isinstance(row, Mapping))

    areas = payload.get("areas")
    if isinstance(areas, list):
        for area in areas:
            if not isinstance(area, Mapping):
                continue
            direct = area.get("manpower")
            if isinstance(direct, list):
                direct_rows.extend(row for row in direct if isinstance(row, Mapping))
            indirect = area.get("indirect_manpower")
            if isinstance(indirect, list):
                indirect_rows.extend(row for row in indirect if isinstance(row, Mapping))

    direct = _dedupe_people(direct_rows, "direct")
    indirect = _dedupe_people(indirect_rows, "indirect")
    combined = dict(direct)
    for key, person in indirect.items():
        if key not in combined:
            combined[key] = dict(person)
            continue
        _merge_person(combined[key], person)

    def hours_total(people: Mapping[Any, Mapping[str, Any]]) -> float:
        return round(sum(float(person["hours"]) for person in people.values() if person["hours"] is not None), 2)

    direct_completeness = _hours_completeness(direct)
    indirect_completeness = _hours_completeness(indirect)
    total_completeness = _hours_completeness(combined)
    day = {
        "date": report_date,
        "direct_headcount": len(direct),
        "indirect_headcount": len(indirect),
        "total_headcount": len(combined),
        "direct_man_hours": hours_total(direct),
        "indirect_man_hours": hours_total(indirect),
        "total_man_hours": hours_total(combined),
        "parsed_hours_count": total_completeness["parsed_hours_count"],
        "zero_hours_count": total_completeness["zero_hours_count"],
        "missing_hours_count": total_completeness["missing_hours_count"],
        "invalid_hours_count": total_completeness["invalid_hours_count"],
        "unparsed_hours_count": total_completeness["unparsed_hours_count"],
        "hours_complete": total_completeness["hours_complete"],
        "hours_completeness": {
            "direct": direct_completeness,
            "indirect": indirect_completeness,
            "total": total_completeness,
        },
    }

    role_rows = []
    for person in combined.values():
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
        previous_plan = _progress_number(earliest.get("cumulative_previous_plan"))
        previous_actual = _progress_number(earliest.get("cumulative_previous_actual"))
        cumulative_plan = values["cumulative_to_date_plan"]
        cumulative_actual = values["cumulative_to_date_actual"]

        if previous_plan is not None and cumulative_plan is not None:
            period_plan = cumulative_plan - previous_plan
        else:
            period_plan = values["this_period_plan"]
        if previous_actual is not None and cumulative_actual is not None:
            period_actual = cumulative_actual - previous_actual
        else:
            period_actual = values["this_period_actual"]

        deviation = (
            cumulative_actual - cumulative_plan
            if cumulative_actual is not None and cumulative_plan is not None
            else values["deviation"]
        )
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
            "first_source_date": earliest_date,
            "last_source_date": latest_date,
        }
        result_rows.append(row_result)

    totals: dict[str, float | None] = {}
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

    return {
        "available": bool(result_rows),
        "rows": result_rows,
        "totals": totals,
        "source_dates": sorted(source_dates),
        "method": "latest cumulative minus earliest previous; cumulative values are never summed",
    }


def aggregate_monthly_records(
    records: Iterable[Mapping[str, Any]],
    *,
    date_from: str,
    date_to: str,
    project_no: str | None = None,
    project_title: str | None = None,
    expected_dates: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Aggregate canonical records for one project and inclusive date range.

    When several revisions exist for a date, the highest revision wins; its
    generated timestamp and report ID are deterministic tie-breakers.
    """

    date_from = _iso_date(date_from, "date_from")
    date_to = _iso_date(date_to, "date_to")
    if date_from > date_to:
        raise ValueError("date_from cannot be after date_to.")

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
            raise ValueError("A project_no or project_title filter is required for mixed-project records.")

    selected: list[tuple[str, Mapping[str, Any]]] = []
    duplicate_dates = []
    superseded_records = []
    for report_date in sorted(groups):
        candidates = groups[report_date]
        chosen = max(candidates, key=_record_sort_key)
        selected.append((report_date, chosen))
        if len(candidates) > 1:
            duplicate_dates.append(report_date)
            superseded_records.extend(
                str(candidate.get("report_id", ""))
                for candidate in candidates
                if candidate is not chosen
            )

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

    activities = []
    constraints = []
    remarks = []
    weather_daily = []
    constraint_daily = []
    daily_manpower = []
    role_rows = []
    planned_next_week = []
    planned_next_month = []
    for report_date, record in selected:
        payload = _payload(record)
        source_report_id = str(record.get("report_id") or "")
        weather_item = _weather_row(payload, report_date)
        if weather_item is not None:
            weather_item["source_report_id"] = source_report_id
            weather_item["source_path"] = "$.weather"
            weather_daily.append(weather_item)
        constraint_state = _clean_text(payload.get("constraint_status"))
        if constraint_state not in {"none_reported", "reported", "not_supplied"}:
            constraint_state = "not_supplied"
        constraint_daily.append({"date": report_date, "status": constraint_state})
        areas = payload.get("areas")
        if not isinstance(areas, list):
            areas = []
        for area_index, area in enumerate(areas):
            if not isinstance(area, Mapping):
                continue
            area_name = _clean_text(area.get("id")) or "Unspecified"
            status_map = _activity_status_map(area)
            for activity_index, description in enumerate(_iter_text_values(area.get("activities_today"))):
                item = {
                    "date": report_date,
                    "area": area_name,
                    "description": description,
                    "source_report_id": source_report_id,
                    "source_path": f"$.areas[{area_index}].activities_today[{activity_index}]",
                }
                status = status_map.get(_normalise_text(description))
                if status:
                    item["status"] = status
                activities.append(item)
            for constraint_index, text in enumerate(_iter_text_values(area.get("constraints"))):
                constraints.append({
                    "date": report_date, "area": area_name, "text": text,
                    "source_report_id": source_report_id,
                    "source_path": f"$.areas[{area_index}].constraints[{constraint_index}]",
                })
            for remark_index, text in enumerate(_iter_text_values(area.get("remarks"))):
                remarks.append({
                    "date": report_date, "area": area_name, "text": text,
                    "source_report_id": source_report_id,
                    "source_path": f"$.areas[{area_index}].remarks[{remark_index}]",
                })

            # Explicit period look-ahead is intentionally separate from Activity Tomorrow.
            # A Daily Report tomorrow item is not automatically a next-week/month commitment.
            for key, target in (("planned_next_week", planned_next_week), ("next_week_activities", planned_next_week),
                                ("planned_next_month", planned_next_month), ("next_month_activities", planned_next_month)):
                for plan_index, description in enumerate(_iter_text_values(area.get(key))):
                    target.append({
                        "source_date": report_date, "area": area_name, "description": description,
                        "source_report_id": source_report_id,
                        "source_path": f"$.areas[{area_index}].{key}[{plan_index}]",
                    })
        day, day_roles = _daily_manpower(payload, report_date)
        daily_manpower.append(day)
        role_rows.extend(day_roles)

    activities = _dedupe_entries(activities, "date", "area", "description")
    constraints = _dedupe_entries(constraints, "date", "area", "text")
    remarks = _dedupe_entries(remarks, "date", "area", "text")

    activities_by_area: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item in activities:
        row = {"date": item["date"], "description": item["description"]}
        if item.get("status"):
            row["status"] = item["status"]
        activities_by_area[item["area"]].append(row)

    # Top-level explicit period look-ahead fields are also accepted when present.
    for report_date, record in selected:
        payload = _payload(record)
        source_report_id = str(record.get("report_id") or "")
        for key, target in (("planned_next_week", planned_next_week), ("next_week_activities", planned_next_week),
                            ("planned_next_month", planned_next_month), ("next_month_activities", planned_next_month)):
            for plan_index, description in enumerate(_iter_text_values(payload.get(key))):
                target.append({
                    "source_date": report_date, "area": "", "description": description,
                    "source_report_id": source_report_id,
                    "source_path": f"$.{key}[{plan_index}]",
                })
    planned_next_week = _dedupe_entries(planned_next_week, "source_date", "area", "description")
    planned_next_month = _dedupe_entries(planned_next_month, "source_date", "area", "description")

    tomorrow_activities = []
    last_report_date = covered[-1] if covered else None
    if selected:
        last_date, last_record = selected[-1]
        areas = _payload(last_record).get("areas")
        if isinstance(areas, list):
            for area in areas:
                if not isinstance(area, Mapping):
                    continue
                area_name = _clean_text(area.get("id")) or "Unspecified"
                for description in _iter_text_values(area.get("activities_tomorrow")):
                    tomorrow_activities.append({
                        "source_date": last_date, "area": area_name, "description": description,
                        "source_report_id": str(last_record.get("report_id") or ""),
                        "source_path": f"$.areas[{areas.index(area)}].activities_tomorrow",
                    })
    tomorrow_activities = _dedupe_entries(
        tomorrow_activities, "source_date", "area", "description"
    )

    constraint_reporting = {
        "daily": constraint_daily,
        "none_reported_dates": [row["date"] for row in constraint_daily if row["status"] == "none_reported"],
        "reported_dates": [row["date"] for row in constraint_daily if row["status"] == "reported"],
        "not_supplied_dates": [row["date"] for row in constraint_daily if row["status"] == "not_supplied"],
    }

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
        else:
            summary["parsed_hours_count"] += 1
            if row["man_hours"] == 0:
                summary["zero_hours_count"] += 1
            summary["man_hours"] += float(row["man_hours"])
    roles = sorted(role_summary.values(), key=lambda row: _normalise_text(row["role"]))
    for row in roles:
        row["man_hours"] = round(row["man_hours"], 2)

    manpower_totals = {
        "direct_person_days": sum(day["direct_headcount"] for day in daily_manpower),
        "indirect_person_days": sum(day["indirect_headcount"] for day in daily_manpower),
        "total_person_days": sum(day["total_headcount"] for day in daily_manpower),
        "direct_man_hours": round(sum(day["direct_man_hours"] for day in daily_manpower), 2),
        "indirect_man_hours": round(sum(day["indirect_man_hours"] for day in daily_manpower), 2),
        "total_man_hours": round(sum(day["total_man_hours"] for day in daily_manpower), 2),
        "peak_headcount": max((day["total_headcount"] for day in daily_manpower), default=0),
        "parsed_hours_count": sum(day["parsed_hours_count"] for day in daily_manpower),
        "zero_hours_count": sum(day["zero_hours_count"] for day in daily_manpower),
        "missing_hours_count": sum(day["missing_hours_count"] for day in daily_manpower),
        "invalid_hours_count": sum(day["invalid_hours_count"] for day in daily_manpower),
        "unparsed_hours_count": sum(day["unparsed_hours_count"] for day in daily_manpower),
        "hours_complete": all(day["hours_complete"] for day in daily_manpower),
    }

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
        "tomorrow_activities": tomorrow_activities,
        "planned_next_week": planned_next_week,
        "planned_next_month": planned_next_month,
        "constraints": constraints,
        "remarks": remarks,
        "weather": weather_daily,
        "constraint_reporting": constraint_reporting,
        "manpower": {
            "daily": daily_manpower,
            "totals": manpower_totals,
            "roles": roles,
            "hours_method": (
                "explicit man_hours takes precedence; otherwise use elapsed shift range without "
                "break deduction; duplicate assignments use the authoritative or longest shift"
            ),
        },
        "overall_progress": _aggregate_progress(selected),
    }
