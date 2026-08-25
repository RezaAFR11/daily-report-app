"""Deterministic readiness/preflight checks for periodic reports."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import difflib
import math
import re


_ACTIVITY_WORKSTREAM_PREFIXES = (
    "Oil System & Flushing",
    "Mechanical Maintenance",
    "Instrumentation & Electrical",
    "Actuator & Pneumatic",
    "Valve Mechanical",
    "Testing & Commissioning",
    "Standby / Coordination",
    "Other Site Work",
)


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        return list(value)
    return [value]


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _has_approved_s_curve(report: Mapping[str, Any]) -> bool:
    value = report.get("s_curve")
    if not isinstance(value, Mapping):
        return False
    labels = _as_list(value.get("labels"))
    plan = _as_list(value.get("plan", value.get("planned")))
    actual = _as_list(value.get("actual"))
    if min(len(labels), len(plan), len(actual)) < 2:
        return False
    return _bool(value.get("approved"), False) and not _bool(value.get("illustrative"), False)


def _has_s_curve_data(report: Mapping[str, Any]) -> bool:
    """Return True only when an explicit S-Curve time series is actually supplied."""
    value = report.get("s_curve")
    if not isinstance(value, Mapping):
        return False
    labels = _as_list(value.get("labels"))
    plan = _as_list(value.get("plan", value.get("planned")))
    actual = _as_list(value.get("actual"))
    return min(len(labels), len(plan), len(actual)) >= 2


def _has_progress_rows(value: Any) -> bool:
    """Detect real progress rows without treating metadata-only mappings as data.

    ``aggregate.py`` always returns an ``overall_progress`` mapping, even when
    ``available`` is false and ``rows`` is empty.  Likewise ``web.py`` stores
    ``progress`` as ``{"rows": []}`` for reports without progress.  A plain
    ``bool(mapping)`` therefore produces false-positive S-Curve blockers.
    """
    if isinstance(value, Mapping):
        rows = value.get("rows")
        if rows is None:
            return False
    else:
        rows = value

    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray, Mapping)):
        return any(isinstance(row, Mapping) for row in rows)
    return False


def _s_curve_requested(report: Mapping[str, Any], *, for_final: bool) -> bool:
    """Mirror renderer intent while keeping Final reports safe.

    Explicit ``include_s_curve`` always wins.  An explicitly supplied S-Curve
    also counts as requested.  Progress rows alone may create an illustrative
    preview, but they must not make a Final report impossible to issue; when no
    approved time series was supplied, Final simply omits that appendix.
    """
    if "include_s_curve" in report:
        return _bool(report.get("include_s_curve"), False)
    if _has_s_curve_data(report):
        return True
    if for_final:
        return False
    return (
        _has_progress_rows(report.get("progress"))
        or _has_progress_rows(report.get("overall_progress"))
    )


def _pending_workforce(report: Mapping[str, Any]) -> bool:
    state = report.get("workforce_validation")
    if not isinstance(state, Mapping):
        return False
    for key in ("timesheet", "overtime"):
        item = state.get(key)
        if isinstance(item, Mapping) and str(item.get("status") or "").lower() == "preview":
            return True
    return False


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _workforce_reconciliation_issues(report: Mapping[str, Any]) -> list[str]:
    manpower = report.get("manpower") if isinstance(report.get("manpower"), Mapping) else {}
    totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    issues: list[str] = []

    def mismatch(left: Any, right: Any, total: Any, tolerance: float = 0.01) -> bool:
        a, b, c = _number(left), _number(right), _number(total)
        return a is not None and b is not None and c is not None and abs((a + b) - c) > tolerance

    if mismatch(totals.get("direct_person_days"), totals.get("indirect_person_days"), totals.get("total_person_days"), 0.001):
        issues.append("Manpower person-day totals do not reconcile: Direct + Indirect != Total.")
    if mismatch(totals.get("direct_man_hours"), totals.get("indirect_man_hours"), totals.get("total_man_hours")):
        issues.append("Man-hour totals do not reconcile: Direct + Indirect != Total.")

    daily = manpower.get("daily") if isinstance(manpower.get("daily"), Sequence) else []
    for row in daily:
        if not isinstance(row, Mapping):
            continue
        date_text = str(row.get("date") or "unknown date")
        if mismatch(row.get("direct_headcount"), row.get("indirect_headcount"), row.get("total_headcount"), 0.001):
            issues.append(f"{date_text}: Direct HC + Indirect HC != Total HC.")
        if mismatch(row.get("direct_man_hours"), row.get("indirect_man_hours"), row.get("total_man_hours")):
            issues.append(f"{date_text}: Direct MH + Indirect MH != Total MH.")
    return issues


_LEADING_ACTIVITY_MA_RE = re.compile(
    r"^\s*MA\s*[- ]?(?P<body>\d{1,3}(?:\s*(?:/|&|,|\band\b)\s*(?:MA\s*)?[- ]?\d{1,3}){0,5})\b",
    re.I,
)
_LEADING_ACTIVITY_MA_PAIR_RE = re.compile(
    r"^\s*MA\s+(?P<left>\d{1,2})\s*-\s*(?P<right>\d{1,2})(?=\s+[A-Za-z])",
    re.I,
)


def _ma_tokens(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text.lower().startswith("ma"):
        return set()
    return {str(int(token)) for token in re.findall(r"\d{1,3}", text)}


def _leading_ma_tokens(value: Any) -> set[str]:
    text = str(value or "").strip()
    pair = _LEADING_ACTIVITY_MA_PAIR_RE.match(text)
    if pair:
        return {str(int(pair.group("left"))), str(int(pair.group("right")))}
    match = _LEADING_ACTIVITY_MA_RE.match(text)
    if not match:
        return set()
    return {str(int(token)) for token in re.findall(r"\d{1,3}", match.group("body"))}


def _activity_area_ownership_issue(row: Mapping[str, Any]) -> bool:
    expected = _ma_tokens(row.get("area"))
    explicit = _leading_ma_tokens(row.get("text", row.get("description", "")))
    return bool(expected and explicit and not explicit.issubset(expected))


def _client_text_issues(report: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    summary = str(report.get("executive_summary") or "")
    if re.search(r"\bthis\s+(?:weekly|monthly|week-to-date|month-to-date)?\s*draft\b|\bthis\s+draft\b", summary, re.I):
        issues.append("Executive Summary still contains draft-oriented wording.")
    contamination = re.compile(
        r"(?:Confidential\s+Daily\s+Activity\s+Report|Daily\s+Activity\s+Report\s*\||PT\.?\s+GARUDA\s+PRIMA\s+AKSARA\s*\|)",
        re.I,
    )
    for row in _as_list(report.get("activities")):
        if isinstance(row, Mapping):
            text = str(row.get("description", row.get("text", "")) or "")
            if contamination.search(text):
                issues.append("Activity text contains repeated Daily Report header/footer boilerplate.")
                break
    for row in _as_list(report.get("photo_documentation")):
        if isinstance(row, Mapping) and contamination.search(str(row.get("caption") or "")):
            issues.append("Photo caption contains repeated Daily Report header/footer boilerplate.")
            break

    site = report.get("site") if isinstance(report.get("site"), Mapping) else {}
    current = (
        site.get("this_week_activities")
        or site.get("this_month_activities")
        or report.get("current_period_activities")
        or []
    )
    for row in _as_list(current):
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text", row.get("description", "")) or "").strip()
        if _activity_area_ownership_issue(row):
            issues.append("Activity narrative contains an explicit MA area that conflicts with its assigned reporting area.")
            return issues
        for label in _ACTIVITY_WORKSTREAM_PREFIXES:
            if text.casefold().startswith((label + ":").casefold()):
                issues.append("Activity narrative contains a duplicated/nested workstream label.")
                return issues
    return issues



def _photo_completeness_issues(report: Mapping[str, Any]) -> list[str]:
    """Detect any explicit evidence that photo documentation was truncated."""

    issues: list[str] = []
    patterns = (
        re.compile(r"only the first\s+\d+\s+(?:reviewable\s+)?photo(?:\(s\)|s)? were retained", re.I),
        re.compile(r"photo(?:\(s\)|s)? exceeded the\s+\d+-photo or draft asset byte limit", re.I),
        re.compile(r"some photos were excluded by the report draft asset limit", re.I),
        re.compile(r"draft photo count or byte limit was reached", re.I),
        re.compile(r"photo exceeded the overall .* photo count or byte limit", re.I),
    )
    for raw in _as_list(report.get("warnings")):
        text = str(raw or "").strip()
        if text and any(pattern.search(text) for pattern in patterns) and text not in issues:
            issues.append(text)
    return issues



def _photo_mapping_issues(report: Mapping[str, Any]) -> list[str]:
    """Flag uploaded-PDF photos that could not be tied back to a source area."""

    unmapped = 0
    for row in _as_list(report.get("photo_documentation")):
        if not isinstance(row, Mapping):
            continue
        source_type = str(row.get("source_type") or "").strip().lower()
        if source_type != "legacy_pdf_extraction":
            continue
        if not str(row.get("source_area") or "").strip():
            unmapped += 1
    if not unmapped:
        return []
    return [
        f"{unmapped} uploaded-PDF photo(s) could not be mapped to a Daily Report area; review Photo Documentation."
    ]


def _photo_date_coverage_issues(report: Mapping[str, Any]) -> list[str]:
    """Detect source dates with extractable photos that disappeared from the appendix."""

    coverage = report.get("photo_coverage")
    if not isinstance(coverage, Mapping):
        return []
    missing = [
        str(value).strip()
        for value in _as_list(coverage.get("missing_photo_dates"))
        if str(value).strip()
    ]
    if not missing:
        return []
    return [
        "Photo Documentation is missing retained photo references for source date(s): "
        + ", ".join(missing)
        + "."
    ]


def _deterministic_summary_issues(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Check the non-AI narrative baseline without making AI mandatory."""

    warnings: list[str] = []
    info: list[str] = []
    summary = report.get("deterministic_summary")
    if not isinstance(summary, Mapping):
        return warnings, info

    version = str(summary.get("version") or report.get("narrative_engine_version") or "").strip()
    current = _as_list(summary.get("current_activities"))
    raw_activities = _as_list(report.get("activities"))
    if raw_activities and not current:
        warnings.append("Deterministic summary contains no current-period activity groups although source activities are available.")

    seen: set[tuple[str, str]] = set()
    for row in current:
        if not isinstance(row, Mapping):
            continue
        area = str(row.get("area") or "").strip().casefold()
        workstream = str(row.get("workstream") or "").strip().casefold()
        if not area or not workstream:
            continue
        key = (area, workstream)
        if key in seen:
            warnings.append("Deterministic activity summary contains duplicate Area + Workstream groups.")
            break
        seen.add(key)
        if _activity_area_ownership_issue(row):
            warnings.append("Deterministic activity summary contains an explicit MA area that conflicts with its assigned reporting area.")
            break

    if version:
        info.append(
            f"Deterministic narrative baseline is available ({version}); AI enhancement is optional."
        )
    else:
        info.append("Deterministic narrative baseline is available; AI enhancement is optional.")
    return warnings, info


def _progress_source_consistency_issues(report: Mapping[str, Any]) -> list[str]:
    """Ensure source-backed progress survives review/render normalisation verbatim."""

    overall = report.get("overall_progress")
    progress = report.get("progress")
    if not isinstance(overall, Mapping) or not overall.get("available"):
        return []
    if not isinstance(progress, Mapping):
        return []
    if str(progress.get("source_type") or "").strip() != "latest_daily_overall_progress_snapshot":
        return []

    source_rows = [row for row in _as_list(overall.get("rows")) if isinstance(row, Mapping)]
    progress_rows = [row for row in _as_list(progress.get("rows")) if isinstance(row, Mapping)]
    source_total = next(
        (
            row for row in reversed(source_rows)
            if row.get("is_total") or str(row.get("description") or "").strip().casefold() == "overall progress"
        ),
        None,
    )
    if not isinstance(source_total, Mapping):
        return []

    total_like = [
        row for row in progress_rows
        if row.get("is_total")
        or str(row.get("description") or "").strip().casefold() in {"overall progress", "total overall"}
    ]
    issues: list[str] = []
    if len(total_like) > 1:
        issues.append(
            "Source-backed progress contains more than one overall total row; keep only the authoritative Daily Report OVERALL PROGRESS row."
        )
    progress_total = next(
        (
            row for row in reversed(total_like)
            if str(row.get("description") or "").strip().casefold() == "overall progress"
        ),
        total_like[-1] if total_like else None,
    )
    if not isinstance(progress_total, Mapping):
        issues.append("The authoritative Daily Report OVERALL PROGRESS row is missing from the rendered progress model.")
        return issues

    comparisons = (
        ("Previous Actual", progress_total.get("previous"), source_total.get("cumulative_previous_actual")),
        ("This Period Actual", progress_total.get("this_month"), source_total.get("this_period_actual")),
        ("To-date Actual", progress_total.get("to_date"), source_total.get("cumulative_to_date_actual")),
        ("To-date Plan", progress_total.get("plan"), source_total.get("cumulative_to_date_plan")),
        ("Variance", progress_total.get("variance"), source_total.get("deviation")),
    )
    mismatches: list[str] = []
    for label, rendered, source in comparisons:
        rendered_number = _number(rendered)
        source_number = _number(source)
        if rendered_number is None or source_number is None:
            if rendered_number != source_number:
                mismatches.append(label)
            continue
        if abs(rendered_number - source_number) > 0.005:
            mismatches.append(label)
    if mismatches:
        issues.append(
            "Source-backed Overall Progress changed after review/normalisation for: "
            + ", ".join(mismatches)
            + ". Preserve the latest Daily Report snapshot verbatim instead of recalculating it."
        )
    return issues




def _lookahead_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("text", value.get("description", value.get("activity", "")))
    return " ".join(str(value or "").split()).strip()


def _lookahead_normalised(value: Any) -> str:
    text = _lookahead_text(value).casefold()
    # Area suffixes added by AI are presentation-only and must not make the same
    # source plan look distinct.
    text = re.sub(r"\(\s*ma\s*[- ]?\d{1,3}\s*\)\s*[.]?$", "", text, flags=re.I)
    text = re.sub(r"[^a-z0-9#]+", " ", text).strip()
    return text


def _lookahead_identifiers(value: Any) -> set[str]:
    text = _lookahead_normalised(value)
    return {
        re.sub(r"\s+", "", token)
        for token in re.findall(r"\b(?:unit|tg|ma)?\s*#?\s*\d+\b", text, flags=re.I)
    }


def _duplicate_lookahead_issues(value: Any) -> list[str]:
    rows = [_lookahead_text(row) for row in _as_list(value)]
    rows = [row for row in rows if row]
    for index, left in enumerate(rows):
        left_norm = _lookahead_normalised(left)
        if not left_norm:
            continue
        left_ids = _lookahead_identifiers(left)
        for right in rows[index + 1:]:
            right_norm = _lookahead_normalised(right)
            if not right_norm:
                continue
            right_ids = _lookahead_identifiers(right)
            if left_ids or right_ids:
                if left_ids != right_ids:
                    continue
            score = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
            if score >= 0.92:
                return [
                    "Planned activities contain near-duplicate look-ahead items; keep one source-backed item per planned activity."
                ]
    return []


def _periodic_source_propagation_issues(report: Mapping[str, Any]) -> list[str]:
    """Catch source-backed period facts that disappeared before rendering."""

    issues: list[str] = []
    overall = report.get("overall_progress")
    if isinstance(overall, Mapping) and overall.get("available") and not _has_progress_rows(report.get("progress")):
        issues.append("Overall Progress is available from Daily Report source data but is not present in the rendered progress model.")

    report_type = str(report.get("report_type") or "").strip().lower()
    if report_type == "weekly":
        period = report.get("period") if isinstance(report.get("period"), Mapping) else {}
        period_end = str(period.get("end", period.get("date_to", "")) or "").strip()
        tomorrow = [row for row in _as_list(report.get("tomorrow_activities")) if isinstance(row, Mapping)]
        period_end_tomorrow = [row for row in tomorrow if str(row.get("source_date") or "").strip() == period_end]
        site = report.get("site") if isinstance(report.get("site"), Mapping) else {}
        lookahead = _as_list(site.get("next_week_activities", site.get("next_period_activities")))
        if period_end_tomorrow and not lookahead:
            issues.append("The period-end Daily Report contains Activity Tomorrow items, but Weekly Planned Activities Next Week is empty.")
        issues.extend(_duplicate_lookahead_issues(lookahead))

    remarks = [row for row in _as_list(report.get("remarks")) if isinstance(row, Mapping) and str(row.get("text") or "").strip()]
    if remarks:
        site = report.get("site") if isinstance(report.get("site"), Mapping) else {}
        findings = _as_list(site.get("key_findings"))
        if not findings:
            issues.append("Daily Report remarks/findings are available but are not surfaced separately from formal constraints.")
    return issues


def _manual_source_issues(report: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    sections = ["engineering", "procurement", "equipment_delivery", "shipments", "safety"]
    for section in sections:
        value = report.get(section)
        if not isinstance(value, Mapping):
            continue
        meta = value.get("source_meta")
        if not isinstance(meta, Mapping) or str(meta.get("source_type") or "") != "manual":
            continue
        if not str(meta.get("entered_by") or "").strip():
            issues.append(f"{section}: manual data has no entered_by audit value")
        if not str(meta.get("reference") or "").strip():
            issues.append(f"{section}: manual data has no source/reference note")
    return issues


_CRITICAL_SOURCE_SEVERITIES = {"error", "critical", "blocker"}
_RESOLVED_PROJECT_DECISIONS = {"merge", "separate"}


def _identity_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _source_validation_critical_issues(
    report: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Return only source problems that can make a Final report factually ambiguous.

    A parser asking for ordinary human review is not, by itself, a critical
    problem.  Final issue is blocked only when the project identity is missing or
    inconsistent, an explicit project/duplicate decision is unresolved, or the
    source-validation payload carries a non-overridable error severity.
    """

    validation = report.get("source_validation")
    if not isinstance(validation, Mapping):
        return [{
            "code": "source_validation_missing",
            "message": (
                "Source validation metadata is unavailable; source identity and "
                "duplicate-date decisions cannot be verified for Final issue."
            ),
        }]

    issues: list[dict[str, str]] = []
    report_project_no = str(report.get("project_no") or report.get("vendor_project_no") or "").strip()
    report_project_title = str(report.get("project_title") or report.get("project_name") or "").strip()
    selected_project_no = str(validation.get("selected_project_no") or report_project_no).strip()
    selected_project_title = str(validation.get("selected_project_title") or report_project_title).strip()

    if not selected_project_no or not selected_project_title:
        issues.append({
            "code": "source_identity_missing",
            "message": "Report Project No. and Project Title must be known before Final issue.",
        })
    elif (
        report_project_no
        and report_project_title
        and (
            _identity_key(selected_project_no) != _identity_key(report_project_no)
            or _identity_key(selected_project_title) != _identity_key(report_project_title)
        )
    ):
        issues.append({
            "code": "source_identity_mismatch",
            "message": (
                "The report project identity differs from the identity stored in "
                "Source Data Validation. Re-apply the source decision before Final issue."
            ),
        })

    project_groups = validation.get("project_groups")
    for index, group in enumerate(project_groups if isinstance(project_groups, list) else []):
        if not isinstance(group, Mapping) or not _bool(group.get("requires_confirmation"), False):
            continue
        decision = str(group.get("decision") or "").strip().casefold()
        if decision in _RESOLVED_PROJECT_DECISIONS:
            continue
        label = (
            str(group.get("project_title") or group.get("project_no") or "").strip()
            or f"project group {index + 1}"
        )
        issues.append({
            "code": "source_identity_unresolved",
            "message": f"Source project identity decision is unresolved for {label}.",
        })

    duplicate_groups = validation.get("duplicate_groups")
    for group in duplicate_groups if isinstance(duplicate_groups, list) else []:
        if not isinstance(group, Mapping):
            continue
        candidates = [
            str(candidate.get("record_id") or "").strip()
            for candidate in _as_list(group.get("candidates"))
            if isinstance(candidate, Mapping) and str(candidate.get("record_id") or "").strip()
        ]
        if len(candidates) < 2 and not _bool(group.get("requires_confirmation"), False):
            continue
        selected = str(group.get("selected_record_id") or "").strip()
        if selected and selected in candidates:
            continue
        report_date = str(group.get("report_date") or "unknown date").strip()
        issues.append({
            "code": "source_duplicate_unresolved",
            "message": f"A valid Daily Report source has not been selected for duplicate date {report_date}.",
        })

    source_issues = validation.get("issues")
    for source_issue in source_issues if isinstance(source_issues, list) else []:
        if not isinstance(source_issue, Mapping):
            continue
        severity = str(source_issue.get("severity") or "warning").strip().casefold()
        if severity not in _CRITICAL_SOURCE_SEVERITIES:
            continue
        code = str(source_issue.get("code") or "source_validation_error").strip()
        message = str(source_issue.get("message") or code).strip()
        filename = str(source_issue.get("filename") or "").strip()
        if filename:
            message = f"{filename}: {message}"
        issues.append({"code": code or "source_validation_error", "message": message})

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue["code"], issue["message"])
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def refresh_preflight_readiness(
    preflight: dict[str, Any],
    *,
    additional_final_blockers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Recalculate readiness metadata after runtime checks mutate a preflight.

    Some checks, such as draft-local photo availability, can only run in the web
    layer. Centralising this recalculation prevents ``ready``, ``readiness`` and
    ``risk_tiers`` from contradicting one another after those checks are added.
    For Preview, ``additional_final_blockers`` describes risks that would block a
    future Final while the Preview itself remains available.
    """

    def normalise_rows(value: Any) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in value if isinstance(value, list) else []:
            if not isinstance(raw, Mapping):
                continue
            code = str(raw.get("code") or "").strip()
            message = str(raw.get("message") or code).strip()
            if not message:
                continue
            key = (code, message)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"code": code, "message": message})
        return rows

    blockers = normalise_rows(preflight.get("blockers"))
    warnings = normalise_rows(preflight.get("warnings"))
    info = normalise_rows(preflight.get("info"))
    runtime_final = normalise_rows(list(additional_final_blockers))
    for_final = bool(preflight.get("for_final"))

    if for_final:
        known = {(row["code"], row["message"]) for row in blockers}
        for row in runtime_final:
            key = (row["code"], row["message"])
            if key not in known:
                known.add(key)
                blockers.append(row)
        critical_count = len(blockers)
        critical_codes = [
            code
            for code in dict.fromkeys(row["code"] for row in blockers)
            if code
        ]
    else:
        risk_tiers = preflight.get("risk_tiers")
        risk_tiers = risk_tiers if isinstance(risk_tiers, Mapping) else {}
        critical = risk_tiers.get("critical")
        critical = critical if isinstance(critical, Mapping) else {}
        try:
            critical_count = max(0, int(critical.get("count") or 0))
        except (TypeError, ValueError):
            critical_count = 0
        critical_codes = [
            str(code).strip()
            for code in _as_list(critical.get("codes"))
            if str(code).strip()
        ]
        critical_count += len(runtime_final)
        critical_codes = list(dict.fromkeys(
            critical_codes
            + [row["code"] for row in runtime_final if row.get("code")]
        ))

    def issue_codes(rows: list[dict[str, str]]) -> list[str]:
        return [
            code
            for code in dict.fromkeys(row["code"] for row in rows)
            if code
        ]

    final_ready = critical_count == 0
    requested_ready = final_ready if for_final else True
    if not requested_ready:
        status = "blocked"
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready"

    preflight["blockers"] = blockers
    preflight["warnings"] = warnings
    preflight["info"] = info
    preflight["ready"] = requested_ready
    preflight["requires_override_reason"] = any(
        row["code"] == "partial_coverage" for row in warnings
    )
    preflight["readiness"] = {
        "requested_stage": "final" if for_final else "preview",
        "status": status,
        "preview_ready": True,
        "final_ready": final_ready,
        "final_blocker_count": critical_count,
        "warning_confirmation_required": bool(warnings),
    }
    preflight["risk_tiers"] = {
        "highest": (
            "critical" if critical_count
            else "warning" if warnings
            else "info" if info
            else "clear"
        ),
        "critical": {"count": critical_count, "codes": critical_codes},
        "warning": {"count": len(warnings), "codes": issue_codes(warnings)},
        "info": {"count": len(info), "codes": issue_codes(info)},
    }
    return preflight


def build_report_preflight(report: Mapping[str, Any], *, for_final: bool = False) -> dict[str, Any]:
    """Return risk-tiered readiness without making Draft review unnecessarily strict.

    Preview/Draft output remains available with review warnings. Final issue is
    stricter: source ambiguity/errors, workforce arithmetic, authoritative progress
    consistency, client-text contamination, incomplete photo evidence/date coverage,
    and source-backed facts lost during periodic propagation are hard blockers.
    """

    final_blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    info: list[dict[str, str]] = []

    def add_final_blocker(code: str, message: str) -> None:
        row = {"code": code, "message": message}
        final_blockers.append(row)
        if not for_final:
            # A Draft remains previewable, but the future Final risk must stay
            # visible rather than disappearing merely because it is non-blocking.
            warnings.append(dict(row))

    validation = report.get("source_validation")
    source_critical = _source_validation_critical_issues(report)
    for issue in source_critical:
        add_final_blocker(issue["code"], issue["message"])
    if isinstance(validation, Mapping):
        if validation.get("applied") and validation.get("confirmed"):
            info.append({
                "code": "source_validation_confirmed",
                "message": "Source Data Validation is applied and confirmed.",
            })
        elif not source_critical:
            warnings.append({
                "code": "source_review_pending",
                "message": (
                    "Source review is not yet confirmed. Draft preview remains available; "
                    "resolve any material exceptions before Final issue."
                ),
            })
        for source_issue in (
            validation.get("issues")
            if isinstance(validation.get("issues"), list)
            else []
        ):
            if not isinstance(source_issue, Mapping):
                continue
            severity = str(source_issue.get("severity") or "warning").strip().casefold()
            if severity in _CRITICAL_SOURCE_SEVERITIES:
                continue
            message = str(source_issue.get("message") or source_issue.get("code") or "").strip()
            if not message:
                continue
            filename = str(source_issue.get("filename") or "").strip()
            if filename:
                message = f"{filename}: {message}"
            warnings.append({
                "code": str(source_issue.get("code") or "source_review_warning").strip()
                or "source_review_warning",
                "message": message,
            })

    if _pending_workforce(report):
        warnings.append({
            "code": "workforce_review_pending",
            "message": (
                "A timesheet/overtime preview is still pending. The current deterministic "
                "Daily Report workforce baseline remains available for review."
            ),
        })

    ai = report.get("ai_summary")
    if isinstance(ai, Mapping) and ai.get("status") == "suggested":
        warnings.append({
            "code": "ai_review_pending",
            "message": (
                "AI narrative suggestions are pending. They are not applied; the "
                "deterministic narrative remains authoritative."
            ),
        })

    # Photo review is optional. Automatic photo mapping/captions may be used
    # directly for Final issue; users can still review/edit photos when needed.

    coverage = report.get("coverage") if isinstance(report.get("coverage"), Mapping) else {}
    missing = [str(x) for x in _as_list(coverage.get("missing_dates")) if str(x).strip()]
    if missing:
        warnings.append({
            "code": "partial_coverage",
            "message": f"Daily Report coverage is partial; {len(missing)} date(s) are missing: {', '.join(missing)}.",
        })
    else:
        info.append({"code": "coverage_complete", "message": "Daily Report coverage is complete for the selected period."})

    include_s_curve = _s_curve_requested(report, for_final=for_final)
    final_include_s_curve = _s_curve_requested(report, for_final=True)
    progress_available = (
        _has_progress_rows(report.get("progress"))
        or _has_progress_rows(report.get("overall_progress"))
    )
    if final_include_s_curve and not _has_approved_s_curve(report):
        add_final_blocker(
            "illustrative_s_curve",
            "Final reports may not use an illustrative/generated S-Curve. Supply an approved time series or disable the S-Curve.",
        )
    elif for_final and progress_available and not include_s_curve:
        info.append({
            "code": "s_curve_omitted_final",
            "message": "Progress rows are available, but no approved S-Curve time series was supplied; the S-Curve appendix will be omitted from Final.",
        })

    for message in _workforce_reconciliation_issues(report):
        add_final_blocker("workforce_reconciliation", message)

    for message in _client_text_issues(report):
        if for_final:
            add_final_blocker("client_text_quality", message)
        else:
            warnings.append({"code": "client_text_quality", "message": message})

    for message in _photo_completeness_issues(report):
        rendered_message = "Photo Documentation may be incomplete: " + message
        if for_final:
            add_final_blocker("photo_documentation_incomplete", rendered_message)
        else:
            warnings.append({
                "code": "photo_documentation_incomplete",
                "message": rendered_message,
            })

    for message in _photo_mapping_issues(report):
        warnings.append({
            "code": "photo_area_mapping_review",
            "message": message,
        })

    photo_date_issues = _photo_date_coverage_issues(report)
    for message in photo_date_issues:
        if for_final:
            add_final_blocker("photo_date_coverage", message)
        else:
            warnings.append({
                "code": "photo_date_coverage",
                "message": message,
            })
    photo_coverage = report.get("photo_coverage")
    if isinstance(photo_coverage, Mapping) and not photo_date_issues:
        source_count = int(photo_coverage.get("source_date_count") or 0)
        retained_count = int(photo_coverage.get("retained_date_count") or 0)
        if source_count:
            info.append({
                "code": "photo_date_coverage_complete",
                "message": f"Photo Documentation retains extractable photo evidence for {retained_count}/{source_count} source date(s).",
            })

    for message in _periodic_source_propagation_issues(report):
        if for_final:
            add_final_blocker("periodic_source_propagation", message)
        else:
            warnings.append({"code": "periodic_source_propagation", "message": message})

    for message in _progress_source_consistency_issues(report):
        add_final_blocker("progress_source_consistency", message)

    deterministic_warnings, deterministic_info = _deterministic_summary_issues(report)
    for message in deterministic_warnings:
        warnings.append({"code": "deterministic_summary_quality", "message": message})
    for message in deterministic_info:
        info.append({"code": "deterministic_summary_ready", "message": message})

    for message in _manual_source_issues(report):
        warnings.append({"code": "manual_source_audit", "message": message})

    def unique_issues(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row.get("code") or ""), str(row.get("message") or ""))
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    final_blockers = unique_issues(final_blockers)
    warnings = unique_issues(warnings)
    info = unique_issues(info)
    blockers = final_blockers if for_final else []
    final_ready = not final_blockers
    requested_ready = final_ready if for_final else True
    if not requested_ready:
        readiness_status = "blocked"
    elif warnings:
        readiness_status = "ready_with_warnings"
    else:
        readiness_status = "ready"
    highest_risk = (
        "critical" if final_blockers
        else "warning" if warnings
        else "info" if info
        else "clear"
    )

    def issue_codes(rows: list[dict[str, str]]) -> list[str]:
        return list(dict.fromkeys(str(row.get("code") or "") for row in rows if row.get("code")))

    return {
        "ready": requested_ready,
        "for_final": bool(for_final),
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "requires_override_reason": any(row["code"] == "partial_coverage" for row in warnings),
        "readiness": {
            "requested_stage": "final" if for_final else "preview",
            "status": readiness_status,
            "preview_ready": True,
            "final_ready": final_ready,
            "final_blocker_count": len(final_blockers),
            "warning_confirmation_required": bool(warnings),
        },
        "risk_tiers": {
            "highest": highest_risk,
            "critical": {
                "count": len(final_blockers),
                "codes": issue_codes(final_blockers),
            },
            "warning": {
                "count": len(warnings),
                "codes": issue_codes(warnings),
            },
            "info": {
                "count": len(info),
                "codes": issue_codes(info),
            },
        },
    }
