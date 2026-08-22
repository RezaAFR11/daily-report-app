"""Deterministic readiness/preflight checks for periodic reports."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import math
import re


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

    if version:
        info.append(
            f"Deterministic narrative baseline is available ({version}); AI enhancement is optional."
        )
    else:
        info.append("Deterministic narrative baseline is available; AI enhancement is optional.")
    return warnings, info

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


def build_report_preflight(report: Mapping[str, Any], *, for_final: bool = False) -> dict[str, Any]:
    """Return hard blockers, overridable warnings and informational checks."""
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    info: list[dict[str, str]] = []

    validation = report.get("source_validation")
    if not isinstance(validation, Mapping) or not validation.get("applied") or not validation.get("confirmed"):
        blockers.append({"code": "source_validation", "message": "Source Data Validation is not applied and confirmed."})

    if _pending_workforce(report):
        blockers.append({"code": "workforce_review", "message": "A timesheet/overtime preview is still pending."})

    ai = report.get("ai_summary")
    if isinstance(ai, Mapping) and ai.get("status") == "suggested":
        blockers.append({"code": "ai_review", "message": "AI narrative suggestions are still pending acceptance/rejection."})

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
    progress_available = (
        _has_progress_rows(report.get("progress"))
        or _has_progress_rows(report.get("overall_progress"))
    )
    if for_final and include_s_curve and not _has_approved_s_curve(report):
        blockers.append({
            "code": "illustrative_s_curve",
            "message": "Final reports may not use an illustrative/generated S-Curve. Supply an approved time series or disable the S-Curve.",
        })
    elif for_final and progress_available and not include_s_curve:
        info.append({
            "code": "s_curve_omitted_final",
            "message": "Progress rows are available, but no approved S-Curve time series was supplied; the S-Curve appendix will be omitted from Final.",
        })

    for message in _workforce_reconciliation_issues(report):
        target = blockers if for_final else warnings
        target.append({"code": "workforce_reconciliation", "message": message})

    for message in _client_text_issues(report):
        target = blockers if for_final else warnings
        target.append({"code": "client_text_quality", "message": message})

    for message in _photo_completeness_issues(report):
        target = blockers if for_final else warnings
        target.append({
            "code": "photo_documentation_incomplete",
            "message": "Photo Documentation may be incomplete: " + message,
        })

    for message in _photo_mapping_issues(report):
        warnings.append({
            "code": "photo_area_mapping_review",
            "message": message,
        })

    deterministic_warnings, deterministic_info = _deterministic_summary_issues(report)
    for message in deterministic_warnings:
        warnings.append({"code": "deterministic_summary_quality", "message": message})
    for message in deterministic_info:
        info.append({"code": "deterministic_summary_ready", "message": message})

    for message in _manual_source_issues(report):
        warnings.append({"code": "manual_source_audit", "message": message})

    return {
        "ready": not blockers,
        "for_final": bool(for_final),
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "requires_override_reason": any(row["code"] == "partial_coverage" for row in warnings),
    }
