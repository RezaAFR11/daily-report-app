"""Professional deterministic narrative for periodic report drafts.

This module deliberately does not call an AI provider.  It converts facts that
have already passed the Daily Report parser/aggregator boundary into concise
client-facing prose.  The resulting mapping can be applied to a weekly or
monthly draft when AI generation is unavailable or rejected.

Absence is never treated as a zero value.  In particular, an empty safety or
constraint field is not evidence that no incident/constraint occurred.
"""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any


_SPACE = re.compile(r"\s+")
_PLACEHOLDERS = {
    "",
    "not supplied",
    "manual input required",
    "manual input required.",
    "manual weekly input required",
    "manual weekly input required.",
    "manual monthly input required",
    "manual monthly input required.",
}
_GENERATED_NOT_SUPPLIED_PREFIXES = (
    "no engineering status data was supplied",
    "no procurement status data was supplied",
)
_NO_IMPACT = {
    "none",
    "no",
    "nil",
    "n/a",
    "na",
    "no impact",
    "no impact on work",
}


def _text(value: Any) -> str:
    return _SPACE.sub(" ", str(value or "")).strip()


def _normal(value: Any) -> str:
    return _text(value).casefold()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _meaningful_summary(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("summary", value.get("text", ""))
    result = _text(value)
    normal = result.casefold()
    if normal in _PLACEHOLDERS or normal.startswith(_GENERATED_NOT_SUPPLIED_PREFIXES):
        return ""
    return result


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(_text(value))
    except (TypeError, ValueError):
        return None


def _period(report: Mapping[str, Any]) -> tuple[date | None, date | None]:
    period = _mapping(report.get("period"))
    start = _iso(period.get("start", period.get("date_from")))
    end = _iso(period.get("end", period.get("date_to")))
    return start, end


def _date_span(start: date | None, end: date | None) -> str:
    if start is None and end is None:
        return "the selected reporting period"
    if end is None or start == end:
        value = start or end
        return f"{value.day} {value.strftime('%B %Y')}" if value else "the selected reporting period"
    if start is None:
        return f"up to {end.day} {end.strftime('%B %Y')}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%B %Y')}"
    if start.year == end.year:
        return f"{start.day} {start.strftime('%B')}–{end.day} {end.strftime('%B %Y')}"
    return f"{start.day} {start.strftime('%B %Y')}–{end.day} {end.strftime('%B %Y')}"


def _date_ranges(values: Sequence[Any]) -> str:
    parsed = sorted({_iso(value) for value in values} - {None})
    if not parsed:
        return ""
    ranges: list[tuple[date, date]] = []
    first = last = parsed[0]
    for value in parsed[1:]:
        if value == last + timedelta(days=1):
            last = value
            continue
        ranges.append((first, last))
        first = last = value
    ranges.append((first, last))
    return ", ".join(_date_span(first, last) for first, last in ranges)


def _human_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _source_refs(rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    source_ids: set[str] = set()
    dates: set[str] = set()
    for row in rows:
        source_id = _text(row.get("source_id"))
        source_date = _text(row.get("date", row.get("source_date")))
        if source_id:
            source_ids.add(source_id)
        if _iso(source_date):
            dates.add(source_date)
    return sorted(source_ids), sorted(dates)


def _activity_rows(report: Mapping[str, Any], *, future: bool = False) -> list[Mapping[str, Any]]:
    if future:
        value = report.get("tomorrow_activities", report.get("planned_activities"))
        if not _rows(value):
            site = _mapping(report.get("site"))
            value = site.get("next_period_activities", site.get("next_month_activities"))
    else:
        value = report.get("activities")
        if not _rows(value):
            site = _mapping(report.get("site"))
            value = site.get("current_period_activities", site.get("this_month_activities"))
    return _rows(value)


def _group_activities(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate activities while retaining latest explicit status and evidence."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    labels: dict[tuple[str, str], tuple[str, str]] = {}
    for row in rows:
        description = _text(
            row.get("description", row.get("text", row.get("activity", row.get("title"))))
        )
        if not description:
            continue
        area = _text(row.get("area", row.get("location"))) or "Site"
        key = (_normal(area), _normal(description))
        grouped[key].append(row)
        labels.setdefault(key, (area, description))

    result: list[dict[str, Any]] = []
    for key, source_rows in grouped.items():
        area, description = labels[key]
        ordered = sorted(
            enumerate(source_rows),
            key=lambda pair: (_text(pair[1].get("date", pair[1].get("source_date"))), pair[0]),
        )
        status = ""
        for _, row in ordered:
            candidate = _text(row.get("status"))
            if candidate:
                status = candidate
        source_ids, dates = _source_refs(source_rows)
        item: dict[str, Any] = {
            "area": area,
            "description": description,
            "text": description,
            "source_ids": source_ids,
            "dates": dates,
        }
        if status:
            item["status"] = status
        result.append(item)
    return result


def _activity_sentence(items: Sequence[Mapping[str, Any]], *, limit: int = 8) -> str:
    if not items:
        return "No site activity details were supplied in the available Daily Reports."
    phrases = []
    for item in items[:limit]:
        description = _text(item.get("description", item.get("text")))
        status = _text(item.get("status"))
        if status and _normal(status) not in _normal(description):
            description = f"{description} ({status})"
        phrases.append(f"{_text(item.get('area'))}: {description}")
    if len(items) > limit:
        phrases.append(f"{len(items) - limit} additional activity item(s) listed in the site section")
    return "Reported work focused on " + "; ".join(phrases) + "."


def _coverage_sentence(report: Mapping[str, Any]) -> str:
    coverage = _mapping(report.get("coverage"))
    covered = coverage.get("covered_dates", coverage.get("found_dates", []))
    missing = coverage.get("missing_dates", [])
    covered = list(covered) if isinstance(covered, Sequence) and not isinstance(covered, str) else []
    missing = list(missing) if isinstance(missing, Sequence) and not isinstance(missing, str) else []
    if not covered:
        covered = [
            _text(row.get("report_date", row.get("date")))
            for row in _rows(report.get("source_manifest"))
            if _iso(row.get("report_date", row.get("date")))
        ]
    expected = coverage.get("expected_dates", [])
    expected_count = len(expected) if isinstance(expected, Sequence) and not isinstance(expected, str) else 0
    included = coverage.get("included_count", coverage.get("selected_record_count", len(covered)))
    try:
        included_count = max(0, int(included))
    except (TypeError, ValueError):
        included_count = len(covered)

    if covered:
        sentence = f"Daily Reports were available for {_date_ranges(covered)}"
        if expected_count:
            sentence += f" ({included_count} of {expected_count} calendar dates)"
        sentence += "."
    elif included_count:
        sentence = f"{included_count} Daily Report(s) were included."
    else:
        sentence = "No Daily Reports were available for the selected reporting period."
    if missing:
        sentence += f" Reports for {_date_ranges(missing)} were not supplied."
    return sentence


def _manpower_sentence(report: Mapping[str, Any]) -> str:
    manpower = _mapping(report.get("manpower"))
    totals = _mapping(manpower.get("totals"))
    daily = _rows(manpower.get("daily"))
    workforce = _mapping(report.get("workforce_validation"))
    effective = _mapping(workforce.get("effective"))
    uses_reviewed_workforce = bool(effective) and any(
        key in effective
        for key in (
            "peak_headcount",
            "total_manpower",
            "regular_man_hours",
            "overtime_man_hours",
            "total_man_hours",
        )
    )
    peak = (
        effective.get("peak_headcount", effective.get("total_manpower"))
        if uses_reviewed_workforce
        else totals.get("peak_headcount")
    )
    hours = effective.get("total_man_hours") if uses_reviewed_workforce else totals.get("total_man_hours")
    parsed_count = totals.get("parsed_hours_count", 0)
    complete = totals.get("hours_complete")
    parts: list[str] = []
    try:
        if (daily or uses_reviewed_workforce) and float(peak) > 0:
            parts.append(f"a peak workforce of {_human_number(peak)} personnel")
    except (TypeError, ValueError):
        pass

    if uses_reviewed_workforce:
        regular = effective.get("regular_man_hours")
        overtime = effective.get("overtime_man_hours")
        overtime_applied = effective.get("overtime_applied") is True
        regular_complete = effective.get("regular_coverage_complete")
        overtime_complete = effective.get("overtime_coverage_complete")
        total_complete = effective.get("total_hours_complete")
        workforce_incomplete = (
            regular_complete is False
            or total_complete is False
            or (overtime_applied and overtime_complete is False)
        )
        try:
            hours_number = float(hours)
        except (TypeError, ValueError):
            hours_number = -1
        try:
            regular_number = float(regular)
        except (TypeError, ValueError):
            regular_number = -1
        try:
            overtime_number = float(overtime)
        except (TypeError, ValueError):
            overtime_number = -1

        if hours_number >= 0:
            if overtime_applied and regular_number >= 0 and overtime_number >= 0:
                qualifier = "recorded" if workforce_incomplete else "total"
                parts.append(
                    f"{_human_number(hours)} {qualifier} man-hours "
                    f"({_human_number(regular)} regular and {_human_number(overtime)} overtime)"
                )
            elif regular_number >= 0:
                parts.append(f"{_human_number(regular)} recorded regular man-hours")
            else:
                qualifier = "recorded" if workforce_incomplete else "total"
                parts.append(f"{_human_number(hours)} {qualifier} man-hours")

        if not parts:
            return "Manpower and man-hour information was not supplied in a usable form."
        sentence = "The reviewed workforce data recorded " + " and ".join(parts) + "."
        if workforce_incomplete:
            sentence += " Workforce coverage was incomplete for one or more dates."
        elif not overtime_applied:
            sentence += " Overtime was not supplied or was not applied."
        return sentence

    try:
        if int(parsed_count or 0) > 0 and float(hours) >= 0:
            direct_hours = totals.get("direct_man_hours")
            indirect_hours = totals.get("indirect_man_hours")
            split = ""
            try:
                direct_number = float(direct_hours)
                indirect_number = float(indirect_hours)
                if direct_number >= 0 and indirect_number >= 0:
                    split = (
                        f" ({_human_number(direct_hours)} direct and "
                        f"{_human_number(indirect_hours)} indirect)"
                    )
            except (TypeError, ValueError):
                pass
            if complete is False:
                parts.append(
                    f"{_human_number(hours)} man-hours from the parsed personnel entries{split}"
                )
            else:
                parts.append(f"a total of {_human_number(hours)} man-hours{split}")
    except (TypeError, ValueError):
        pass
    if not parts:
        return "Manpower and man-hour information was not supplied in a usable form."
    sentence = "The available Daily Reports recorded " + " and ".join(parts) + "."
    if complete is False:
        sentence += " Man-hour information was incomplete for one or more personnel entries."
    return sentence


def _weather_sentence(report: Mapping[str, Any]) -> str:
    rows = _rows(report.get("weather"))
    if not rows:
        rows = _rows(_mapping(report.get("site")).get("weather"))
    if not rows:
        return "Weather information was not supplied in the available Daily Reports."

    conditions: list[str] = []
    winds: list[str] = []
    temperatures: list[str] = []
    impacts: list[str] = []
    for row in rows:
        for key in ("morning", "afternoon", "evening"):
            value = _text(row.get(key))
            if value and _normal(value) not in {_normal(item) for item in conditions}:
                conditions.append(value)
        for key, target in (("wind", winds), ("temperature", temperatures), ("impact", impacts)):
            value = _text(row.get(key))
            if value and _normal(value) not in {_normal(item) for item in target}:
                target.append(value)

    pieces = []
    if conditions:
        pieces.append("conditions of " + ", ".join(conditions))
    if winds:
        pieces.append("wind reported as " + ", ".join(winds))
    if temperatures:
        pieces.append("temperatures of " + ", ".join(temperatures))
    sentence = "Weather observations for the available reporting days recorded "
    sentence += "; ".join(pieces) if pieces else "limited condition details"
    if impacts and all(_normal(value) in _NO_IMPACT for value in impacts):
        sentence += ", with no reported impact on work"
    elif impacts:
        sentence += "; reported work impact: " + ", ".join(impacts)
    return sentence + "."


def _constraint_result(report: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    constraints = _rows(report.get("constraints"))
    if not constraints:
        constraints = _rows(_mapping(report.get("site")).get("concerns"))

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in constraints:
        content = _text(row.get("text", row.get("concern", row.get("description"))))
        if not content:
            continue
        area = _text(row.get("area", row.get("location"))) or "Site"
        grouped[(_normal(area), _normal(content))].append(row)

    output = []
    for source_rows in grouped.values():
        first = source_rows[0]
        area = _text(first.get("area", first.get("location"))) or "Site"
        content = _text(first.get("text", first.get("concern", first.get("description"))))
        source_ids, dates = _source_refs(source_rows)
        output.append({
            "area": area,
            "concern": content,
            "corrective_action": "",
            "source_ids": source_ids,
            "dates": dates,
        })

    reporting = _mapping(report.get("constraint_reporting"))
    if not reporting:
        reporting = _mapping(_mapping(report.get("site")).get("constraint_reporting"))
    none_dates = reporting.get("none_reported_dates", [])
    reported_dates = reporting.get("reported_dates", [])
    missing_dates = reporting.get("not_supplied_dates", [])

    if output:
        phrases = [f"{item['area']}: {item['concern']}" for item in output[:6]]
        sentence = "Reported constraints included " + "; ".join(phrases) + "."
        if len(output) > 6:
            sentence += f" {len(output) - 6} additional constraint item(s) are listed in the site section."
        return sentence, output
    if reported_dates:
        return (
            f"Constraints were flagged for {_date_ranges(reported_dates)}, but details were not supplied.",
            output,
        )
    if none_dates and not missing_dates:
        return "No constraints were reported in the available Daily Reports.", output
    if none_dates and missing_dates:
        return (
            f"No constraints were reported for {_date_ranges(none_dates)}; constraint information "
            f"was not supplied for {_date_ranges(missing_dates)}.",
            output,
        )
    return "Constraint information was not supplied in the available Daily Reports.", output


def _remarks_result(report: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    remarks = _rows(report.get("remarks"))
    if not remarks:
        remarks = _rows(_mapping(report.get("site")).get("remarks"))

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in remarks:
        content = _text(row.get("text", row.get("remark", row.get("description"))))
        if not content:
            continue
        area = _text(row.get("area", row.get("location"))) or "Site"
        grouped[(_normal(area), _normal(content))].append(row)

    output: list[dict[str, Any]] = []
    for source_rows in grouped.values():
        first = source_rows[0]
        area = _text(first.get("area", first.get("location"))) or "Site"
        content = _text(first.get("text", first.get("remark", first.get("description"))))
        source_ids, dates = _source_refs(source_rows)
        output.append({
            "area": area,
            "text": content,
            "source_ids": source_ids,
            "dates": dates,
        })

    if not output:
        return "", output
    phrases = [f"{item['area']}: {item['text']}" for item in output[:6]]
    sentence = "Reported site remarks included " + "; ".join(phrases) + "."
    if len(output) > 6:
        sentence += f" {len(output) - 6} additional remark item(s) are listed in the site section."
    return sentence, output


def _existing_section_summary(report: Mapping[str, Any], key: str) -> str:
    return _meaningful_summary(report.get(key))


def build_deterministic_narrative(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build source-grounded narrative fields compatible with a report draft.

    The input may be either the aggregate returned by ``aggregate_monthly_records``
    or a prepared weekly/monthly draft.  The input is never mutated.
    """

    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")

    report_type = _normal(report.get("report_type"))
    if report_type not in {"weekly", "monthly"}:
        report_type = "weekly" if "weekly" in _normal(report.get("report_title")) else "monthly"
    label = "Weekly" if report_type == "weekly" else "Monthly"
    project_title = _text(report.get("project_title", report.get("project_name")))
    project_no = _text(report.get("project_no", report.get("vendor_project_no")))
    project = project_title or project_no or "the selected project"
    if project_title and project_no and _normal(project_no) not in _normal(project_title):
        project += f" (Project No. {project_no})"
    start, end = _period(report)

    current = _group_activities(_activity_rows(report))
    future = _group_activities(_activity_rows(report, future=True))
    coverage = _coverage_sentence(report)
    activity = _activity_sentence(current)
    manpower = _manpower_sentence(report)
    weather = _weather_sentence(report)
    constraints, concern_rows = _constraint_result(report)
    remarks, remark_rows = _remarks_result(report)

    engineering = _existing_section_summary(report, "engineering")
    procurement = _existing_section_summary(report, "procurement")
    missing_data: list[str] = []
    if not engineering:
        engineering = "No engineering status data was supplied in the available Daily Reports."
        missing_data.append("Engineering status: Not supplied")
    if not procurement:
        procurement = "No procurement status data was supplied in the available Daily Reports."
        missing_data.append("Procurement status: Not supplied")
    if not future:
        missing_data.append("Next-period activities: Not supplied")

    safety = _mapping(report.get("safety"))
    incident_fields = (
        "recordable_cases",
        "total_recordable_cases",
        "lost_workdays",
        "lost_time_injuries",
        "severity_rate",
        "average_day_away",
    )
    safety_supplied = any(safety.get(key) is not None and _text(safety.get(key)) != "" for key in incident_fields)
    if not safety_supplied:
        missing_data.append("Safety incident metrics: Not supplied")

    lookahead_sentence = (
        "Explicit next-period activities are listed in the look-ahead section."
        if future
        else "Next-period activities were not supplied in the available Daily Reports."
    )
    executive_parts = [
        f"This {label} Progress Report covers {_date_span(start, end)} for {project}.",
        coverage,
        activity,
        manpower,
        weather,
        constraints,
        remarks,
        lookahead_sentence,
    ]
    if not safety_supplied:
        executive_parts.append("Safety incident metrics were not supplied in the available Daily Reports.")
    executive_parts.extend((engineering, procurement))
    executive_summary = " ".join(part for part in executive_parts if part)

    site_summary = " ".join(
        part for part in (activity, weather, constraints, remarks, lookahead_sentence) if part
    )
    site: dict[str, Any] = {
        "summary": site_summary,
        "current_period_activities": copy.deepcopy(current),
        "this_period_activities": copy.deepcopy(current),
        "this_month_activities": copy.deepcopy(current),
        "next_period_activities": copy.deepcopy(future),
        "next_month_activities": copy.deepcopy(future),
        "concerns": concern_rows,
        "remarks": remark_rows,
    }
    if report_type == "weekly":
        site["this_week_activities"] = copy.deepcopy(current)
        site["next_week_activities"] = copy.deepcopy(future)

    return {
        "version": "periodic-narrative-fallback/1",
        "executive_summary": executive_summary,
        "engineering": {"summary": engineering},
        "procurement": {"summary": procurement},
        "site": site,
        "missing_data": missing_data,
    }


def apply_deterministic_narrative(
    report: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Return a copied draft with deterministic narrative fields applied.

    With ``overwrite=False`` existing meaningful reviewer/AI prose is preserved.
    Activity, look-ahead, and concern collections are never replaced merely by
    applying fallback text; the grouped versions remain available under
    ``deterministic_narrative`` for an explicit review decision.
    """

    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    result = copy.deepcopy(dict(report))
    narrative = build_deterministic_narrative(report)
    result["deterministic_narrative"] = copy.deepcopy(narrative)

    if overwrite or not _meaningful_summary(result.get("executive_summary")):
        result["executive_summary"] = narrative["executive_summary"]
    for section in ("engineering", "procurement"):
        current = dict(_mapping(result.get(section)))
        if overwrite or not _meaningful_summary(current):
            current["summary"] = narrative[section]["summary"]
        result[section] = current
    site = dict(_mapping(result.get("site")))
    if overwrite or not _meaningful_summary(site.get("summary")):
        site["summary"] = narrative["site"]["summary"]
    result["site"] = site
    return result


__all__ = [
    "apply_deterministic_narrative",
    "build_deterministic_narrative",
]
