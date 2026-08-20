"""Deterministic readiness/preflight checks for periodic reports."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


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


def _pending_workforce(report: Mapping[str, Any]) -> bool:
    state = report.get("workforce_validation")
    if not isinstance(state, Mapping):
        return False
    for key in ("timesheet", "overtime"):
        item = state.get(key)
        if isinstance(item, Mapping) and str(item.get("status") or "").lower() == "preview":
            return True
    return False


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

    photos = [row for row in _as_list(report.get("photo_documentation")) if isinstance(row, Mapping)]
    photo_review = report.get("photo_review")
    if for_final and photos and not (isinstance(photo_review, Mapping) and photo_review.get("confirmed") is True):
        blockers.append({"code": "photo_review", "message": "Photo mapping/captions must be reviewed before Final issue."})

    coverage = report.get("coverage") if isinstance(report.get("coverage"), Mapping) else {}
    missing = [str(x) for x in _as_list(coverage.get("missing_dates")) if str(x).strip()]
    if missing:
        warnings.append({
            "code": "partial_coverage",
            "message": f"Daily Report coverage is partial; {len(missing)} date(s) are missing: {', '.join(missing)}.",
        })
    else:
        info.append({"code": "coverage_complete", "message": "Daily Report coverage is complete for the selected period."})

    include_s_curve = _bool(report.get("include_s_curve"), bool(report.get("progress") or report.get("overall_progress")))
    if for_final and include_s_curve and not _has_approved_s_curve(report):
        blockers.append({
            "code": "illustrative_s_curve",
            "message": "Final reports may not use an illustrative/generated S-Curve. Supply an approved time series or disable the S-Curve.",
        })

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
