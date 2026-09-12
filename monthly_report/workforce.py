"""Review-first workforce overrides for periodic reports.

The Daily Report aggregation remains an immutable baseline.  Attendance and
overtime workbooks are kept as review metadata until a user explicitly applies
them; reset restores the original manpower and safety totals exactly.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping

from .workforce_matching import match_employee, reconcile_timesheet


WORKFORCE_VERSION = "workforce-validation/1"


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _compact_number(value: float) -> int | float:
    rounded = round(float(value), 4)
    return int(rounded) if rounded.is_integer() else rounded


def _baseline_effective(draft: Mapping[str, Any]) -> dict[str, Any]:
    manpower = draft.get("manpower") if isinstance(draft.get("manpower"), Mapping) else {}
    totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    return {
        "source": "daily_report",
        "peak_headcount": totals.get("peak_headcount", 0),
        "regular_man_hours": totals.get("total_man_hours", 0),
        "overtime_man_hours": None,
        "total_man_hours": totals.get("total_man_hours", 0),
        "overtime_applied": False,
        "note": "Overtime not supplied or not applied.",
    }


def ensure_workforce_state(draft: dict[str, Any]) -> dict[str, Any]:
    state = draft.get("workforce_validation")
    if isinstance(state, dict) and state.get("version") == WORKFORCE_VERSION:
        return state
    state = {
        "version": WORKFORCE_VERSION,
        "baseline": {
            "manpower": copy.deepcopy(draft.get("manpower", {})),
            "safety": copy.deepcopy(draft.get("safety", {})),
        },
        "timesheet": {"status": "not_reviewed"},
        "overtime": {"status": "not_reviewed"},
        "effective": _baseline_effective(draft),
    }
    draft["workforce_validation"] = state
    return state


def _restore_baseline(draft: dict[str, Any], state: Mapping[str, Any]) -> None:
    baseline = state.get("baseline") if isinstance(state.get("baseline"), Mapping) else {}
    draft["manpower"] = copy.deepcopy(baseline.get("manpower", {}))
    draft["safety"] = copy.deepcopy(baseline.get("safety", {}))


def reset_workforce(draft: dict[str, Any]) -> dict[str, Any]:
    state = ensure_workforce_state(draft)
    baseline = copy.deepcopy(state.get("baseline", {}))
    _restore_baseline(draft, state)
    draft["workforce_validation"] = {
        "version": WORKFORCE_VERSION,
        "baseline": baseline,
        "timesheet": {"status": "not_reviewed"},
        "overtime": {"status": "not_reviewed"},
        "effective": _baseline_effective(draft),
    }
    return draft["workforce_validation"]


def standardise_timesheet_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(preview))
    daily: list[dict[str, Any]] = []
    effective_daily: list[dict[str, Any]] = []
    supplied_dates: list[str] = []
    partial_dates: list[str] = []
    not_supplied_dates: list[str] = []
    for row in result.get("daily_totals", []):
        if not isinstance(row, Mapping):
            continue
        by_section = row.get("present_by_section") if isinstance(row.get("present_by_section"), Mapping) else {}
        direct = int(_number(by_section.get("direct")))
        indirect = int(_number(by_section.get("indirect")))
        total = int(_number(row.get("present_count", direct + indirect)))
        hours_by_section = row.get("hours_by_section") or {}
        status_counts = (
            copy.deepcopy(row.get("status_counts"))
            if isinstance(row.get("status_counts"), Mapping)
            else {}
        )
        observed = sum(int(_number(value)) for value in status_counts.values())
        missing = int(_number(status_counts.get("missing")))
        conflicts = int(_number(status_counts.get("conflict")))
        all_missing = observed > 0 and missing == observed
        report_date = str(row.get("date") or "")
        supplied = not all_missing
        complete = supplied and missing == 0 and conflicts == 0 and row.get("hours_complete", True)
        item = {
            "date": report_date,
            "direct_headcount": direct,
            "indirect_headcount": indirect,
            "total_headcount": total,
            "direct_man_hours": hours_by_section.get("direct", direct * 10) if supplied else None,
            "indirect_man_hours": hours_by_section.get("indirect", indirect * 10) if supplied else None,
            "total_man_hours": (
                _compact_number(_number(row.get("physical_manhours", total * 10)))
                if supplied
                else None
            ),
            "status_counts": status_counts,
            "supplied": supplied,
            "hours_complete": complete,
            "source": "timesheet_kn_v1",
        }
        daily.append(item)
        if supplied:
            effective_daily.append(copy.deepcopy(item))
            supplied_dates.append(report_date)
            if not complete:
                partial_dates.append(report_date)
        else:
            not_supplied_dates.append(report_date)
    source_totals = result.get("totals") if isinstance(result.get("totals"), Mapping) else {}
    by_section = source_totals.get("by_section") if isinstance(source_totals.get("by_section"), Mapping) else {}
    direct_totals = by_section.get("direct") if isinstance(by_section.get("direct"), Mapping) else {}
    indirect_totals = by_section.get("indirect") if isinstance(by_section.get("indirect"), Mapping) else {}
    unresolved = result.get("unresolved") if isinstance(result.get("unresolved"), list) else []
    warning_codes = {
        str(row.get("code") or "")
        for row in (result.get("warnings") if isinstance(result.get("warnings"), list) else [])
        if isinstance(row, Mapping)
    }
    blocking_warning_codes = {
        "unknown_nonpresent_marker",
        "incomplete_date_columns",
        "date_header_mismatch",
        "no_employee_rows",
        "period_not_found",
        "attendance_table_not_found",
    }
    result["daily"] = daily
    result["coverage"] = {
        "expected_dates": [row["date"] for row in daily],
        "supplied_dates": supplied_dates,
        "partial_dates": partial_dates,
        "not_supplied_dates": not_supplied_dates,
        "covered_days": len(supplied_dates),
        "expected_days": len(daily),
        "complete": not partial_dates and not not_supplied_dates,
    }
    result["requires_confirmation"] = (
        bool(unresolved)
        or not result["coverage"]["complete"]
        or bool(warning_codes & blocking_warning_codes)
    )
    result["manpower"] = {
        # A wholly blank date is unknown, not a zero-headcount workday.  It
        # remains visible in the review preview above but is deliberately not
        # written into the effective report totals.
        "daily": effective_daily,
        "roles": copy.deepcopy(result.get("roles", [])),
        "totals": {
            "direct_person_days": int(_number(direct_totals.get("present_person_days"))),
            "indirect_person_days": int(_number(indirect_totals.get("present_person_days"))),
            "total_person_days": int(_number(source_totals.get("present_person_days"))),
            "direct_man_hours": _compact_number(_number(direct_totals.get("physical_manhours"))),
            "indirect_man_hours": _compact_number(_number(indirect_totals.get("physical_manhours"))),
            "total_man_hours": _compact_number(_number(source_totals.get("physical_manhours"))),
            "peak_headcount": int(_number(source_totals.get("peak_present_count"))),
            "average_daily_headcount": round(sum(row["total_headcount"] for row in effective_daily) / len(effective_daily), 2) if effective_daily else None,
            "manpower_supplied_day_count": len(supplied_dates),
            "manpower_not_supplied_day_count": len(not_supplied_dates),
            "hours_complete": result["coverage"]["complete"],
            "partial_dates": copy.deepcopy(partial_dates),
            "not_supplied_dates": copy.deepcopy(not_supplied_dates),
            "source": "timesheet_kn_v1",
        },
    }
    return result


def set_timesheet_preview(
    draft: dict[str, Any],
    preview: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    state = ensure_workforce_state(draft)
    _restore_baseline(draft, state)
    prepared = standardise_timesheet_preview(reconcile_timesheet(preview, state["baseline"].get("manpower", {})))
    timesheet_only = standardise_timesheet_preview(reconcile_timesheet(
        preview, state["baseline"].get("manpower", {}), include_daily_fallback=False))
    state["timesheet"] = {
        "status": "preview",
        "preview": prepared,
        "preview_options": {"combined": prepared, "timesheet_only": timesheet_only},
        "reviewed_by": actor,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    state["overtime"] = {"status": "not_reviewed"}
    state["effective"] = _baseline_effective(draft)
    return state


def decide_timesheet(
    draft: dict[str, Any],
    decision: str,
    *,
    confirm_exceptions: bool = False,
    source_mode: str | None = None,
    actor: str,
) -> dict[str, Any]:
    state = ensure_workforce_state(draft)
    timesheet = state.get("timesheet") if isinstance(state.get("timesheet"), dict) else {}
    if decision not in {"apply", "keep"}:
        raise ValueError("Timesheet decision must be apply or keep.")
    mode = source_mode or ("daily_only" if decision == "keep" else "combined")
    if mode not in {"daily_only", "combined", "timesheet_only"}:
        raise ValueError("Choose Daily PDF only, Timesheet + Daily fallback, or Timesheet only.")
    if (decision == "keep") != (mode == "daily_only"):
        raise ValueError("The decision does not match the selected workforce source.")
    preview = timesheet.get("preview") if isinstance(timesheet.get("preview"), Mapping) else None
    if preview is None:
        raise ValueError("Analyze an attendance timesheet before saving a decision.")
    options = timesheet.get("preview_options") or {}
    if mode != "daily_only":
        if mode in options:
            preview = options[mode]
        elif mode == "timesheet_only" or timesheet.get("source_mode") == "timesheet_only":
            raise ValueError("Analyze the timesheet again to select this workforce source.")
    _restore_baseline(draft, state)
    state["overtime"] = {"status": "not_reviewed"}
    if decision == "keep":
        timesheet["source_mode"] = mode
        timesheet["status"] = "kept"
        timesheet["decided_by"] = actor
        timesheet["decided_at"] = datetime.now().isoformat(timespec="seconds")
        timesheet["confirmed_exceptions"] = False
        state["effective"] = _baseline_effective(draft)
        state["effective"]["source_mode"] = mode
        return state

    manpower = preview.get("manpower") if isinstance(preview.get("manpower"), Mapping) else {}
    totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    if not manpower.get("daily"):
        raise ValueError("The selected period has no confirmed attendance to apply.")
    if preview.get("requires_confirmation") and not confirm_exceptions:
        raise ValueError(
            "Confirm missing roles, attendance conflicts, and incomplete dates before applying the timesheet."
        )
    timesheet["status"] = "applied"
    timesheet["source_mode"] = mode
    timesheet["preview"] = copy.deepcopy(preview)
    timesheet["decided_by"] = actor
    timesheet["decided_at"] = datetime.now().isoformat(timespec="seconds")
    timesheet["confirmed_exceptions"] = bool(confirm_exceptions)
    draft["manpower"] = copy.deepcopy(manpower)
    safety = draft.get("safety") if isinstance(draft.get("safety"), dict) else {}
    safety["total_manpower"] = int(_number(totals.get("peak_headcount")))
    safety["total_man_hours"] = _compact_number(_number(totals.get("total_man_hours")))
    draft["safety"] = safety
    coverage = preview.get("coverage") if isinstance(preview.get("coverage"), Mapping) else {}
    regular_complete = bool(totals.get("hours_complete"))
    state["effective"] = {
        "source": "timesheet",
        "source_mode": mode,
        "peak_headcount": safety["total_manpower"],
        "regular_man_hours": safety["total_man_hours"],
        "regular_coverage_complete": regular_complete,
        "regular_partial_dates": copy.deepcopy(coverage.get("partial_dates", [])),
        "regular_not_supplied_dates": copy.deepcopy(coverage.get("not_supplied_dates", [])),
        "overtime_man_hours": None,
        "total_man_hours": safety["total_man_hours"],
        "overtime_applied": False,
        "total_hours_complete": False,
        "note": (
            "Regular attendance is partial; total excludes missing attendance and overtime."
            if not regular_complete
            else "OT not supplied or not applied; total currently excludes overtime."
        ),
    }
    return state


def prepare_overtime_review(
    overtime: Mapping[str, Any],
    timesheet_preview: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(overtime))
    period = result.get("period") if isinstance(result.get("period"), Mapping) else {}
    period_start = str(period.get("start") or "")
    period_end = str(period.get("end") or "")
    all_warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
    relevant_warnings = []
    for warning in all_warnings:
        warning_date = str(warning.get("date") or "") if isinstance(warning, Mapping) else ""
        if warning_date and period_start and warning_date < period_start:
            continue
        if warning_date and period_end and warning_date > period_end:
            continue
        relevant_warnings.append(warning)
    result["workbook_warnings"] = all_warnings
    result["warnings"] = relevant_warnings
    employee_index: dict[str, Mapping[str, Any]] = {}
    if isinstance(timesheet_preview, Mapping):
        employee_index = {
            str(row.get("employee_key") or ""): row
            for row in timesheet_preview.get("employees", [])
            if isinstance(row, Mapping) and row.get("employee_key")
        }
    records_by_person: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in result.get("records", []):
        if isinstance(record, Mapping):
            records_by_person[str(record.get("employee_key") or "")].append(record)

    people: list[dict[str, Any]] = []
    for employee in result.get("employees", []):
        if not isinstance(employee, Mapping):
            continue
        key = str(employee.get("employee_key") or "")
        match = employee_index.get(key)
        method = "exact" if match else "unmatched"
        if match is None:
            match, method = match_employee(employee.get("employee", ""), list(employee_index.values()))
        if match:
            for record in records_by_person.get(key, []):
                record["matched_employee_key"] = match.get("employee_key", key)
                record["match_method"] = method
        if method in {"name_variant", "spelling_variant", "ambiguous"}:
            relevant_warnings.append({"code": "overtime_name_" + method, "severity": "warning",
                "message": (f"Overtime name {employee.get('employee', key)} matched to {match['name']}."
                            if match else f"Overtime name {employee.get('employee', key)} has multiple possible matches; review its identity.")})
        category = str(match.get("section") or "") if match else ""
        statuses = {
            str(row.get("date") or ""): str(row.get("status") or "")
            for row in (match.get("statuses", []) if match else [])
            if isinstance(row, Mapping)
        }
        mismatch_dates = sorted({
            str(record.get("date") or "")
            for record in records_by_person.get(key, [])
            if statuses.get(str(record.get("date") or "")) != "present"
        })
        source_requires_review = any(
            bool(record.get("requires_review")) for record in records_by_person.get(key, [])
        )
        requires_confirmation = not match or bool(mismatch_dates) or source_requires_review
        if mismatch_dates:
            relevant_warnings.append({"code": "overtime_attendance_difference", "severity": "warning",
                "message": f"{employee.get('employee', key)} has overtime without confirmed attendance on {', '.join(mismatch_dates)}. Review the timesheet and overtime source."})
        people.append({
            "key": key,
            "name": employee.get("employee", key),
            "match_name": match.get("name", "") if match else "",
            "match_method": method,
            "category": category if category in {"direct", "indirect"} else "",
            "dates": copy.deepcopy(employee.get("dates", [])),
            "ot_hours": employee.get("confirmed_elapsed_hours", 0),
            "attendance_mismatch_dates": mismatch_dates,
            "requires_confirmation": requires_confirmation,
        })
    matched_intervals = {}
    for record in result.get("records", []):
        if not record.get("included_in_total") or not record.get("start") or not record.get("end"):
            continue
        canonical = record.get("matched_employee_key") or record.get("employee_key")
        identity = (canonical, record.get("date"), record.get("start"), record.get("end"))
        if identity in matched_intervals:
            record["included_in_total"] = False
            record["duplicate"] = True
            record["duplicate_of_record_id"] = matched_intervals[identity]
            relevant_warnings.append({"code": "matched_overtime_duplicate", "severity": "warning",
                "message": f"{record.get('date')}: duplicate overtime interval for matched worker {record.get('employee')} counted once."})
        else:
            matched_intervals[identity] = record.get("record_id")
    for person in people:
        person["ot_hours"] = _compact_number(sum(_number(r.get("duration_hours"))
            for r in records_by_person.get(person["key"], []) if r.get("included_in_total")))
    result["review_people"] = people
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    expected = len(coverage.get("not_supplied_dates", [])) + len(coverage.get("selected_populated_dates", []))
    coverage["covered_days"] = len(coverage.get("selected_populated_dates", []))
    coverage["expected_days"] = expected
    result["coverage"] = coverage
    result["requires_confirmation"] = (
        bool(result.get("requires_manual_review"))
        or bool(relevant_warnings)
        or bool(coverage.get("not_supplied_dates"))
        or any(row["requires_confirmation"] for row in people)
    )
    daily_rows = [row for row in result.get("daily", []) if isinstance(row, dict)]
    for row in daily_rows:
        if isinstance(row, dict):
            records = [r for r in result.get("records", []) if r.get("date") == row.get("date")]
            if records:
                row["confirmed_elapsed_hours"] = _compact_number(sum(_number(r.get("duration_hours")) for r in records if r.get("included_in_total")))
                row["employee_count"] = len({r.get("matched_employee_key") or r.get("employee_key") for r in records})
            row["participant_count"] = row.get("employee_count", 0)
            row["actual_ot_man_hours"] = row.get("confirmed_elapsed_hours", 0)
            row["supplied"] = True
    represented_dates = {str(row.get("date") or "") for row in daily_rows}
    for report_date in coverage.get("not_supplied_dates", []):
        report_date = str(report_date or "")
        if not report_date or report_date in represented_dates:
            continue
        daily_rows.append({
            "date": report_date,
            "participant_count": None,
            "actual_ot_man_hours": None,
            "employee_count": None,
            "confirmed_elapsed_hours": None,
            "supplied": False,
            "requires_review": True,
        })
    result["daily"] = sorted(daily_rows, key=lambda row: str(row.get("date") or ""))
    totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
    records = result.get("records", [])
    if records:
        totals["selected_confirmed_elapsed_hours"] = _compact_number(sum(_number(r.get("duration_hours")) for r in records if r.get("included_in_total")))
        totals["selected_employee_count"] = len({r.get("matched_employee_key") or r.get("employee_key") for r in records})
    totals["participant_count"] = totals.get("selected_employee_count", 0)
    totals["actual_ot_man_hours"] = totals.get("selected_confirmed_elapsed_hours", 0)
    result["totals"] = totals
    return result


def set_overtime_preview(
    draft: dict[str, Any],
    preview: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    state = ensure_workforce_state(draft)
    timesheet = state.get("timesheet") if isinstance(state.get("timesheet"), Mapping) else {}
    if timesheet.get("status") != "applied":
        raise ValueError("Apply the attendance timesheet before reviewing overtime.")
    prepared = prepare_overtime_review(preview, timesheet.get("preview"))
    state["overtime"] = {
        "status": "preview",
        "preview": prepared,
        "reviewed_by": actor,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }
    return state


def _resolution_map(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, list):
        return result
    for row in value:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("key") or "")
        category = str(row.get("category") or "").lower()
        if key and category in {"direct", "indirect", "exclude"}:
            result[key] = category
    return result


def _record_resolution_map(value: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list):
        return result
    for row in value:
        if not isinstance(row, Mapping):
            continue
        record_id = str(row.get("record_id") or "")
        decision = str(row.get("decision") or "").lower()
        if not record_id or decision not in {"include", "exclude"}:
            continue
        duration = row.get("duration_hours")
        result[record_id] = {
            "decision": decision,
            "duration_hours": duration,
        }
    return result


def _keep_regular_workforce(
    draft: dict[str, Any],
    state: dict[str, Any],
    overtime: dict[str, Any],
    regular_manpower: Mapping[str, Any],
    regular_hours: float,
    *,
    actor: str,
) -> dict[str, Any]:
    overtime["status"] = "kept"
    overtime["decided_by"] = actor
    overtime["decided_at"] = datetime.now().isoformat(timespec="seconds")
    draft["manpower"] = copy.deepcopy(regular_manpower)
    regular_totals = draft["manpower"].get("totals", {})
    safety = draft.get("safety") if isinstance(draft.get("safety"), dict) else {}
    safety["total_manpower"] = int(_number(regular_totals.get("peak_headcount")))
    safety["total_man_hours"] = _compact_number(regular_hours)
    draft["safety"] = safety
    state["effective"] = {
        "source": "timesheet",
        "peak_headcount": safety["total_manpower"],
        "regular_man_hours": _compact_number(regular_hours),
        "overtime_man_hours": None,
        "total_man_hours": _compact_number(regular_hours),
        "overtime_applied": False,
        "total_hours_complete": False,
        "note": "Reviewed overtime workbook was not applied.",
        "source_mode": (state.get("timesheet") or {}).get("source_mode", "combined"),
    }
    return state


def _overtime_categories(
    people: Mapping[str, Mapping[str, Any]],
    decisions: Mapping[str, str],
) -> dict[str, str]:
    categories: dict[str, str] = {}
    for key, row in people.items():
        category = decisions.get(key) or str(row.get("category") or "")
        if category not in {"direct", "indirect", "exclude"}:
            raise ValueError(
                "Choose Direct, Indirect, or Exclude for overtime employee "
                f"{row.get('name') or key}."
            )
        categories[key] = category
    return categories


def _accepted_overtime_records(
    preview: Mapping[str, Any],
    categories: Mapping[str, str],
    record_decisions: Mapping[str, Mapping[str, Any]],
    selected_populated_dates: list[str],
) -> tuple[dict[str, dict[str, float]], list[dict[str, Any]]]:
    daily_ot: dict[str, dict[str, float]] = defaultdict(
        lambda: {"direct": 0.0, "indirect": 0.0}
    )
    for report_date in selected_populated_dates:
        # A populated reviewed sheet is supplied even if every accepted row is
        # subsequently excluded by the reviewer.
        daily_ot[report_date]

    accepted_records: list[dict[str, Any]] = []
    accepted_intervals: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    seen_intervals: set[tuple[str, str, str, str]] = set()
    for record in preview.get("records", []):
        if not isinstance(record, Mapping):
            continue
        category = categories.get(str(record.get("employee_key") or ""), "")
        if category not in {"direct", "indirect"}:
            continue
        record_id = str(record.get("record_id") or "")
        record_decision = record_decisions.get(record_id)
        if record.get("requires_review") and record_decision is None:
            raise ValueError(
                "Choose Include or Exclude for reviewed overtime record "
                f"{record_id or '(unknown)'}."
            )
        include_record = bool(record.get("included_in_total"))
        if record_decision is not None:
            include_record = record_decision["decision"] == "include"
        if not include_record:
            continue
        raw_hours = (
            record_decision.get("duration_hours")
            if record_decision is not None
            and record_decision.get("duration_hours") not in (None, "")
            else record.get("duration_hours", record.get("suggested_duration_hours"))
        )
        hours = _number(raw_hours)
        if hours <= 0 or hours > 24:
            raise ValueError(
                "Enter an overtime duration greater than 0 and no more than 24 hours "
                f"for record {record_id or '(unknown)'}."
            )
        canonical_key = str(record.get("matched_employee_key") or record.get("employee_key") or "")
        date = str(record.get("date") or "")
        start, end = str(record.get("start") or ""), str(record.get("end") or "")
        if start and end:
            identity = (canonical_key, date, start, end)
            if identity in seen_intervals:
                continue
            seen_intervals.add(identity)
            start_minutes = sum(int(v) * factor for v, factor in zip(start.split(':'), (60, 1)))
            end_minutes = sum(int(v) * factor for v, factor in zip(end.split(':'), (60, 1)))
            if end_minutes <= start_minutes:
                end_minutes += 1440
            if any(start_minutes < b and a < end_minutes for a, b in accepted_intervals[(canonical_key, date)]):
                raise ValueError(f"Overlapping overtime for {record.get('employee', canonical_key)} on {date}. Exclude the overlapping record before applying OT.")
            accepted_intervals[(canonical_key, date)].append((start_minutes, end_minutes))
        daily_ot[date][category] += hours
        accepted = copy.deepcopy(dict(record))
        accepted["employee_key"] = canonical_key
        accepted["category"] = category
        accepted["duration_hours"] = _compact_number(hours)
        accepted["review_decision"] = (
            record_decision["decision"]
            if record_decision is not None
            else "source_confirmed"
        )
        accepted_records.append(accepted)

    if not accepted_records:
        raise ValueError(
            "No confirmed overtime record remains to apply. Resolve the intervals/mappings "
            "or Keep Without OT."
        )
    return daily_ot, accepted_records


def _apply_daily_overtime(
    manpower: dict[str, Any],
    daily_ot: Mapping[str, Mapping[str, float]],
    not_supplied_dates: list[str],
) -> None:
    daily_rows = {
        str(row.get("date") or ""): row
        for row in manpower.get("daily", [])
        if isinstance(row, dict)
    }
    for report_date, category_hours in daily_ot.items():
        row = daily_rows.get(report_date)
        if row is None:
            row = {
                "date": report_date,
                "direct_headcount": 0,
                "indirect_headcount": 0,
                "total_headcount": 0,
                "direct_man_hours": 0,
                "indirect_man_hours": 0,
                "total_man_hours": 0,
                "source": "timesheet_kn_v1",
            }
            manpower.setdefault("daily", []).append(row)
            daily_rows[report_date] = row
        regular_direct = _number(row.get("direct_man_hours"))
        regular_indirect = _number(row.get("indirect_man_hours"))
        row["regular_man_hours"] = _compact_number(regular_direct + regular_indirect)
        row["direct_overtime_man_hours"] = _compact_number(category_hours["direct"])
        row["indirect_overtime_man_hours"] = _compact_number(category_hours["indirect"])
        row["overtime_man_hours"] = _compact_number(
            category_hours["direct"] + category_hours["indirect"]
        )
        row["overtime_supplied"] = True
        row["direct_man_hours"] = _compact_number(regular_direct + category_hours["direct"])
        row["indirect_man_hours"] = _compact_number(
            regular_indirect + category_hours["indirect"]
        )
        row["total_man_hours"] = _compact_number(
            row["direct_man_hours"] + row["indirect_man_hours"]
        )
    for report_date in not_supplied_dates:
        row = daily_rows.get(report_date)
        if row is None:
            continue
        regular_direct = _number(row.get("direct_man_hours"))
        regular_indirect = _number(row.get("indirect_man_hours"))
        row["regular_man_hours"] = _compact_number(regular_direct + regular_indirect)
        row["direct_overtime_man_hours"] = None
        row["indirect_overtime_man_hours"] = None
        row["overtime_man_hours"] = None
        row["overtime_supplied"] = False
        row["hours_complete"] = False
    manpower["daily"] = sorted(
        manpower.get("daily", []),
        key=lambda row: row.get("date", ""),
    )


def _apply_overtime_totals(
    manpower: dict[str, Any],
    daily_ot: Mapping[str, Mapping[str, float]],
    *,
    regular_hours: float,
    selected_populated_dates: list[str],
    not_supplied_dates: list[str],
) -> tuple[dict[str, Any], float]:
    direct_ot = sum(values["direct"] for values in daily_ot.values())
    indirect_ot = sum(values["indirect"] for values in daily_ot.values())
    total_ot = direct_ot + indirect_ot
    totals = manpower.setdefault("totals", {})
    totals["regular_direct_man_hours"] = totals.get("direct_man_hours", 0)
    totals["regular_indirect_man_hours"] = totals.get("indirect_man_hours", 0)
    totals["regular_man_hours"] = _compact_number(regular_hours)
    totals["direct_overtime_man_hours"] = _compact_number(direct_ot)
    totals["indirect_overtime_man_hours"] = _compact_number(indirect_ot)
    totals["overtime_man_hours"] = _compact_number(total_ot)
    totals["direct_man_hours"] = _compact_number(
        _number(totals.get("direct_man_hours")) + direct_ot
    )
    totals["indirect_man_hours"] = _compact_number(
        _number(totals.get("indirect_man_hours")) + indirect_ot
    )
    totals["total_man_hours"] = _compact_number(regular_hours + total_ot)
    totals["overtime_coverage_complete"] = not not_supplied_dates
    totals["overtime_supplied_dates"] = copy.deepcopy(selected_populated_dates)
    totals["overtime_not_supplied_dates"] = copy.deepcopy(not_supplied_dates)
    totals["hours_complete"] = bool(totals.get("hours_complete")) and not not_supplied_dates
    return totals, total_ot


def _apply_role_overtime(
    manpower: dict[str, Any],
    timesheet: Mapping[str, Any],
    accepted_records: list[dict[str, Any]],
) -> None:
    timesheet_employees = {
        str(row.get("employee_key") or ""): row
        for row in timesheet.get("preview", {}).get("employees", [])
        if isinstance(row, Mapping) and row.get("employee_key")
    }
    role_rows = [row for row in manpower.get("roles", []) if isinstance(row, dict)]
    roles_by_key: dict[str, dict[str, Any]] = {}
    for row in role_rows:
        role_name = str(row.get("role") or "Unspecified").strip() or "Unspecified"
        role_key = " ".join(role_name.casefold().split())
        regular_role_hours = _number(row.get("physical_manhours", row.get("man_hours")))
        row["regular_man_hours"] = _compact_number(regular_role_hours)
        row["overtime_man_hours"] = 0
        row["total_man_hours"] = _compact_number(regular_role_hours)
        row["man_hours"] = row["total_man_hours"]
        roles_by_key[role_key] = row
    for record in accepted_records:
        employee = timesheet_employees.get(str(record.get("employee_key") or ""), {})
        role_name = (
            str(employee.get("role") or record.get("role") or "Unspecified").strip()
            or "Unspecified"
        )
        role_key = " ".join(role_name.casefold().split())
        row = roles_by_key.get(role_key)
        if row is None:
            row = {
                "role": role_name,
                "employee_count": 0,
                "present_person_days": 0,
                "physical_manhours": 0,
                "regular_man_hours": 0,
                "overtime_man_hours": 0,
                "total_man_hours": 0,
                "man_hours": 0,
            }
            role_rows.append(row)
            roles_by_key[role_key] = row
        overtime_hours = _number(record.get("duration_hours"))
        row["overtime_man_hours"] = _compact_number(
            _number(row.get("overtime_man_hours")) + overtime_hours
        )
        row["total_man_hours"] = _compact_number(
            _number(row.get("regular_man_hours"))
            + _number(row.get("overtime_man_hours"))
        )
        row["man_hours"] = row["total_man_hours"]
    manpower["roles"] = sorted(
        role_rows,
        key=lambda row: str(row.get("role") or "").casefold(),
    )


def decide_overtime(
    draft: dict[str, Any],
    decision: str,
    *,
    resolutions: Any = None,
    record_resolutions: Any = None,
    confirm_exceptions: bool = False,
    actor: str,
) -> dict[str, Any]:
    state = ensure_workforce_state(draft)
    timesheet = state.get("timesheet") if isinstance(state.get("timesheet"), Mapping) else {}
    overtime = state.get("overtime") if isinstance(state.get("overtime"), dict) else {}
    if timesheet.get("status") != "applied":
        raise ValueError("Apply the attendance timesheet before applying overtime.")
    if decision not in {"apply", "keep"}:
        raise ValueError("Overtime decision must be apply or keep.")
    preview = overtime.get("preview") if isinstance(overtime.get("preview"), Mapping) else None
    if preview is None:
        raise ValueError("Analyze an overtime workbook before saving a decision.")
    regular_manpower = timesheet.get("preview", {}).get("manpower", {})
    regular_totals = regular_manpower.get("totals", {}) if isinstance(regular_manpower, Mapping) else {}
    regular_hours = _number(regular_totals.get("total_man_hours"))
    if decision == "keep":
        return _keep_regular_workforce(
            draft,
            state,
            overtime,
            regular_manpower,
            regular_hours,
            actor=actor,
        )

    coverage = preview.get("coverage") if isinstance(preview.get("coverage"), Mapping) else {}
    selected_populated_dates = [
        str(value) for value in coverage.get("selected_populated_dates", []) if str(value)
    ]
    if not selected_populated_dates:
        raise ValueError(
            "No machine-readable overtime was supplied for the selected period. Keep Without OT instead."
        )
    people = {
        str(row.get("key") or ""): row
        for row in preview.get("review_people", [])
        if isinstance(row, Mapping)
    }
    decisions = _resolution_map(resolutions)
    record_decisions = _record_resolution_map(record_resolutions)
    if preview.get("requires_confirmation") and not confirm_exceptions:
        raise ValueError("Confirm overtime exceptions and attendance mismatches before applying OT.")
    categories = _overtime_categories(people, decisions)

    daily_ot, accepted_records = _accepted_overtime_records(
        preview,
        categories,
        record_decisions,
        selected_populated_dates,
    )
    overtime["status"] = "applied"
    overtime["decided_by"] = actor
    overtime["decided_at"] = datetime.now().isoformat(timespec="seconds")
    draft["manpower"] = copy.deepcopy(regular_manpower)
    regular_totals = draft["manpower"].get("totals", {})

    not_supplied_dates = [
        str(value) for value in coverage.get("not_supplied_dates", []) if str(value)
    ]
    _apply_daily_overtime(draft["manpower"], daily_ot, not_supplied_dates)
    totals, total_ot = _apply_overtime_totals(
        draft["manpower"],
        daily_ot,
        regular_hours=regular_hours,
        selected_populated_dates=selected_populated_dates,
        not_supplied_dates=not_supplied_dates,
    )

    _apply_role_overtime(draft["manpower"], timesheet, accepted_records)
    overtime["accepted_records"] = accepted_records
    overtime["resolutions"] = decisions
    overtime["record_resolutions"] = record_decisions
    overtime["confirmed_exceptions"] = bool(confirm_exceptions)

    safety = draft.get("safety") if isinstance(draft.get("safety"), dict) else {}
    safety["total_manpower"] = int(_number(totals.get("peak_headcount")))
    safety["total_man_hours"] = totals["total_man_hours"]
    draft["safety"] = safety
    state["effective"] = {
        "source": "timesheet",
        "peak_headcount": safety["total_manpower"],
        "regular_man_hours": _compact_number(regular_hours),
        "overtime_man_hours": _compact_number(total_ot),
        "total_man_hours": totals["total_man_hours"],
        "overtime_applied": True,
        "source_mode": timesheet.get("source_mode", "combined"),
        "overtime_coverage_complete": not not_supplied_dates,
        "overtime_supplied_dates": copy.deepcopy(selected_populated_dates),
        "overtime_not_supplied_dates": copy.deepcopy(not_supplied_dates),
        "total_hours_complete": bool(totals.get("hours_complete")),
        "note": (
            "Physical OT uses elapsed clock time without break or payroll multiplier."
            if not not_supplied_dates
            else "Physical OT is partial and excludes dates marked Not supplied."
        ),
    }
    return state


def has_pending_workforce_review(draft: Mapping[str, Any]) -> bool:
    state = draft.get("workforce_validation")
    if not isinstance(state, Mapping):
        return False
    return any(
        isinstance(state.get(key), Mapping) and state[key].get("status") == "preview"
        for key in ("timesheet", "overtime")
    )


__all__ = [
    "WORKFORCE_VERSION",
    "decide_overtime",
    "decide_timesheet",
    "ensure_workforce_state",
    "has_pending_workforce_review",
    "prepare_overtime_review",
    "reset_workforce",
    "set_overtime_preview",
    "set_timesheet_preview",
    "standardise_timesheet_preview",
]
