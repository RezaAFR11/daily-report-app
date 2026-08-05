from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from flask import jsonify, request, send_file, session, url_for

from .aggregate import aggregate_monthly_records
from .importer import PDFImportError, import_daily_report_pdf
from .renderer import render_monthly_report
from .storage import list_canonical_records


_DRAFT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._() -]+")
_MAX_UPLOAD_FILES = 35
_MAX_REVIEW_TEXT = 30_000
_REPORT_TYPES = {"monthly", "weekly"}


def _report_type(value: Any) -> str:
    text = str(value or "monthly").strip().lower()
    if text not in _REPORT_TYPES:
        raise ValueError("Report type must be monthly or weekly.")
    return text


def _report_name(report_type: Any) -> str:
    return "Weekly" if _report_type(report_type) == "weekly" else "Monthly"


def _normalise_report_mode(report_type: Any, value: Any) -> str:
    kind = _report_type(report_type)
    default = "wtd" if kind == "weekly" else "mtd"
    mode = str(value or default).strip().lower().replace("_", "-")
    aliases = {
        "month-to-date": "mtd",
        "month to date": "mtd",
        "week-to-date": "wtd",
        "week to date": "wtd",
    }
    mode = aliases.get(mode, mode)
    allowed = {"wtd", "draft", "final"} if kind == "weekly" else {"mtd", "draft", "final"}
    return mode if mode in allowed else default


def _atomic_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _monthly_user_dir(data_dir: str | Path, username: str) -> Path:
    username_text = str(username or "user")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", username_text):
        safe_username = username_text
    else:
        digest = hashlib.sha256(username_text.encode("utf-8")).hexdigest()[:24]
        safe_username = f"legacy-user-{digest}"
    directory = Path(data_dir) / "monthly_reports" / safe_username
    (directory / "reports").mkdir(parents=True, exist_ok=True)
    (directory / "drafts").mkdir(parents=True, exist_ok=True)
    return directory


def get_monthly_reports_index(data_dir: str | Path, username: str) -> list[dict[str, Any]]:
    index_path = _monthly_user_dir(data_dir, username) / "index.json"
    if not index_path.is_file():
        return []
    try:
        with index_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, list):
            return []
        # Reports created before weekly support did not store a type. They are
        # monthly reports and remain visible to old and new clients.
        for row in value:
            if isinstance(row, dict):
                row.setdefault("report_type", "monthly")
        return value
    except (OSError, ValueError, TypeError):
        return []


def _save_monthly_index(data_dir: str | Path, username: str, rows: list[dict[str, Any]]) -> None:
    _atomic_json(_monthly_user_dir(data_dir, username) / "index.json", rows)


def _save_draft(data_dir: str | Path, username: str, draft: dict[str, Any]) -> str:
    drafts_dir = _monthly_user_dir(data_dir, username) / "drafts"
    cutoff = datetime.now().timestamp() - (7 * 24 * 60 * 60)
    existing = sorted(drafts_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in existing[19:]:
        try:
            stale.unlink()
        except OSError:
            pass
    for stale in existing[:19]:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass
    draft_id = uuid.uuid4().hex
    value = copy.deepcopy(draft)
    value["draft_id"] = draft_id
    value["owner"] = username
    value.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    _atomic_json(drafts_dir / f"{draft_id}.json", value)
    return draft_id


def _load_draft(data_dir: str | Path, username: str, draft_id: str) -> dict[str, Any] | None:
    if not _DRAFT_ID_RE.fullmatch(str(draft_id or "")):
        return None
    path = _monthly_user_dir(data_dir, username) / "drafts" / f"{draft_id}.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("owner") != username:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _update_draft(data_dir: str | Path, username: str, draft: dict[str, Any]) -> None:
    draft_id = str(draft.get("draft_id") or "")
    if not _DRAFT_ID_RE.fullmatch(draft_id):
        raise ValueError("Invalid report draft ID")
    _atomic_json(_monthly_user_dir(data_dir, username) / "drafts" / f"{draft_id}.json", draft)


def _parse_period(
    date_from: str,
    date_to: str,
    report_type: str = "monthly",
    report_mode: str | None = None,
) -> tuple[datetime, datetime]:
    kind = _report_type(report_type)
    mode = _normalise_report_mode(kind, report_mode)
    try:
        start = datetime.strptime(str(date_from or ""), "%Y-%m-%d")
        end = datetime.strptime(str(date_to or ""), "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Use valid From and To dates.") from exc
    if start > end:
        raise ValueError("The From date cannot be after the To date.")
    if kind == "monthly" and (start.year, start.month) != (end.year, end.month):
        raise ValueError("A Monthly Report period must stay within one calendar month.")
    if kind == "weekly":
        day_count = (end - start).days + 1
        if start.weekday() != 0:
            raise ValueError("A Weekly Report period must start on Monday.")
        if day_count > 7:
            raise ValueError("A Weekly Report period cannot be longer than 7 days.")
        if mode != "wtd" and (day_count != 7 or end.weekday() != 6):
            raise ValueError("A full Weekly Report period must run from Monday through Sunday.")
    return start, end


def _expected_dates(start: datetime, end: datetime) -> list[str]:
    current = start
    result = []
    while current <= end:
        result.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return result


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value or "").strip().replace("%", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return default


def _clean_text(value: Any, maximum: int = _MAX_REVIEW_TEXT) -> str:
    return str(value or "").replace("\x00", "").strip()[:maximum]


def _list_text(value: Any, maximum_items: int = 500) -> list[str]:
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[str] = []
    for item in values[:maximum_items]:
        if isinstance(item, dict):
            item = item.get("text", item.get("activity", item.get("description", "")))
        text = _clean_text(item, 2_000)
        if text:
            result.append(text)
    return result


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload", record.get("data", {}))
    return value if isinstance(value, dict) else {}


def _warning_text(value: Any) -> str:
    if isinstance(value, dict):
        message = value.get("message") or value.get("code") or "PDF parsing warning"
        severity = str(value.get("severity") or "warning").upper()
        return f"{severity}: {message}"
    return _clean_text(value, 1_000)


def _latest_report_context(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    latest = max(
        records,
        key=lambda record: (
            _record_date(record),
            int(record.get("revision") or 0),
            str(record.get("generated_at") or ""),
        ),
    )
    payload = _payload(latest)
    return {
        "company_name": payload.get("company_name", "PT. GARUDA PRIMA AKSARA"),
        "customer": payload.get("customer", "PT. KERTAS NUSANTARA"),
        "location": payload.get("location", ""),
        "equipment": payload.get("equipment", ""),
        "prepared_by": payload.get("prepared_by", ""),
        "checked_by": payload.get("checked_by", ""),
        "approved_by": payload.get("approved_by", ""),
    }


def _record_date(record: dict[str, Any]) -> str:
    value = record.get("report_date", record.get("date", ""))
    if not value:
        value = _payload(record).get("date", "")
    return str(value or "")


def _prepare_draft(
    aggregated: dict[str, Any],
    *,
    project_no: str,
    project_title: str,
    date_from: str,
    date_to: str,
    report_mode: str,
    source_method: str,
    source_manifest: list[dict[str, Any]],
    report_context: dict[str, Any] | None = None,
    extra_warnings: list[str] | None = None,
    report_type: str = "monthly",
) -> dict[str, Any]:
    kind = _report_type(report_type)
    mode = _normalise_report_mode(kind, report_mode)
    # Validate again at the draft boundary so direct callers cannot create a
    # weekly draft with a malformed period.
    start, end = _parse_period(date_from, date_to, kind, mode)
    report_name = _report_name(kind)
    draft = copy.deepcopy(aggregated if isinstance(aggregated, dict) else {})
    draft["schema_version"] = "weekly-report/1" if kind == "weekly" else "monthly-report/1"
    draft["report_type"] = kind
    draft["report_title"] = f"{report_name} Progress Report"
    draft["project_no"] = project_no
    draft["project_title"] = project_title
    draft["period"] = {"start": date_from, "end": date_to, "timezone": "Asia/Makassar"}
    draft["report_mode"] = mode
    draft["status"] = draft["report_mode"]
    draft["source_method"] = source_method
    draft["source_manifest"] = source_manifest
    context = report_context if isinstance(report_context, dict) else {}
    draft["project_name"] = project_title
    draft["vendor_project_no"] = project_no
    draft["reporting_period"] = f"{date_from} to {date_to}"
    draft["issued_date"] = date_to
    draft["company_name"] = context.get("company_name", "PT. GARUDA PRIMA AKSARA")
    draft["customer"] = context.get("customer", "PT. KERTAS NUSANTARA")
    draft["location"] = context.get("location", "")
    draft["equipment"] = context.get("equipment", "")
    draft["prepared_by"] = context.get("prepared_by", "")
    draft["checked_by"] = context.get("checked_by", "")
    draft["approved_by"] = context.get("approved_by", "")

    coverage = draft.get("coverage") if isinstance(draft.get("coverage"), dict) else {}
    expected = _expected_dates(start, end)
    found_dates = sorted({
        str(item.get("report_date") or item.get("date") or "")
        for item in source_manifest if isinstance(item, dict)
    } - {""})
    coverage.setdefault("expected_dates", expected)
    covered_dates = coverage.get("covered_dates", found_dates)
    coverage.setdefault("found_dates", covered_dates)
    coverage.setdefault("missing_dates", [day for day in expected if day not in found_dates])
    coverage.setdefault("included_count", coverage.get("selected_record_count", len(source_manifest)))
    coverage.setdefault("duplicate_count", len(coverage.get("duplicate_dates", [])))
    draft["coverage"] = coverage

    warnings = draft.get("warnings") if isinstance(draft.get("warnings"), list) else []
    warnings = [_warning_text(item) for item in warnings if _warning_text(item)]
    for warning in extra_warnings or []:
        warning = _warning_text(warning)
        if warning and warning not in warnings:
            warnings.append(warning)
    draft["warnings"] = warnings

    if not draft.get("progress") and isinstance(draft.get("overall_progress"), dict):
        monthly_rows = []
        for row in draft["overall_progress"].get("rows", []):
            if not isinstance(row, dict):
                continue
            monthly_rows.append({
                "description": row.get("description", ""),
                "weight": row.get("weight_factor"),
                "previous": row.get("cumulative_previous_actual"),
                "this_month": row.get("this_period_actual"),
                "to_date": row.get("cumulative_to_date_actual"),
                "plan": row.get("cumulative_to_date_plan"),
                "variance": row.get("deviation"),
            })
        draft["progress"] = {"rows": monthly_rows}
    draft.setdefault("progress", {"rows": []})
    if isinstance(draft.get("progress"), list):
        draft["progress"] = {"rows": draft["progress"]}
    manpower_totals = (
        draft.get("manpower", {}).get("totals", {})
        if isinstance(draft.get("manpower"), dict)
        else {}
    )
    draft.setdefault("safety", {
        "total_manpower": manpower_totals.get("peak_headcount", 0),
        "total_man_hours": manpower_totals.get("total_man_hours", 0),
        "recordable_cases": 0,
        "lost_workdays": 0,
        "lost_time_injuries": 0,
    })
    draft.setdefault("engineering", {"summary": f"Manual {kind} input required."})
    draft.setdefault("procurement", {"summary": f"Manual {kind} input required."})

    site = draft.get("site") if isinstance(draft.get("site"), dict) else {}
    if not site.get("this_month_activities"):
        site["this_month_activities"] = draft.get("this_month_activities", draft.get("activities", []))
    if not site.get("next_month_activities"):
        site["next_month_activities"] = draft.get(
            "next_month_activities",
            draft.get("tomorrow_activities", draft.get("planned_activities", [])),
        )
    if not site.get("concerns"):
        site["concerns"] = draft.get("concerns", draft.get("constraints", []))
    # Generic aliases let the renderer and review UI use period-neutral labels
    # while legacy monthly keys continue to support archived drafts.
    site["current_period_activities"] = site.get("this_month_activities", [])
    site["this_period_activities"] = site["current_period_activities"]
    site["next_period_activities"] = site.get("next_month_activities", [])
    if kind == "weekly":
        site["this_week_activities"] = site["current_period_activities"]
        site["next_week_activities"] = site["next_period_activities"]
    draft["site"] = site
    if not draft.get("executive_summary"):
        included = coverage.get("included_count", len(source_manifest))
        missing = len(coverage.get("missing_dates", []))
        period_description = (
            ("week-to-date" if mode == "wtd" else "weekly")
            if kind == "weekly"
            else mode.upper()
        )
        draft["executive_summary"] = (
            f"This {period_description} draft compiles {included} Daily Report(s) for "
            f"{project_title or project_no} from {date_from} to {date_to}. "
            f"There are {missing} calendar date(s) without an included report. "
            "Progress, safety incidents, engineering, and procurement values must be reviewed before issue."
        )
    return draft


def _source_manifest(records: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    manifest = []
    seen: set[str] = set()
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        row = {
            "report_id": record.get("report_id"),
            "revision": record.get("revision", 1),
            "report_date": _record_date(record),
            "owner": record.get("username", record.get("owner", "")),
            "source_method": method,
            "filename": source.get("filename", record.get("pdf_filename", "")),
            "sha256": source.get("sha256", record.get("content_sha256", "")),
            "confidence": (record.get("confidence") or {}).get("overall") if isinstance(record.get("confidence"), dict) else None,
            "review_required": bool(record.get("review_required", False)),
        }
        identity = str(row["report_id"] or row["sha256"] or f"{row['report_date']}:{row['filename']}")
        if identity in seen:
            continue
        seen.add(identity)
        manifest.append(row)
    return manifest


def _normalize_progress(review: Any) -> dict[str, Any]:
    raw_rows = review.get("rows", []) if isinstance(review, dict) else []
    rows = []
    for raw in raw_rows[:100]:
        if not isinstance(raw, dict):
            continue
        description = _clean_text(raw.get("description"), 250)
        if not description or description.lower() in {"total", "total overall"}:
            continue
        previous = _number(raw.get("previous"))
        this_month = _number(raw.get(
            "this_month",
            raw.get("this_week", raw.get("this_period", raw.get("this_period_actual"))),
        ))
        to_date = previous + this_month
        plan = _number(raw.get("plan", raw.get("cumulative_to_date_plan")))
        rows.append({
            "description": description,
            "weight": max(0.0, _number(raw.get("weight", raw.get("weight_factor")))),
            "previous": round(previous, 4),
            "this_month": round(this_month, 4),
            "to_date": round(to_date, 4),
            "plan": round(plan, 4),
            "variance": round(to_date - plan, 4),
        })

    weight_total = sum(row["weight"] for row in rows)
    if rows and weight_total > 0:
        def weighted(key: str) -> float:
            return sum(row[key] * row["weight"] / 100.0 for row in rows)

        total_previous = weighted("previous")
        total_this = weighted("this_month")
        total_to_date = total_previous + total_this
        total_plan = weighted("plan")
        rows.append({
            "description": "Total Overall",
            "weight": round(weight_total, 4),
            "previous": round(total_previous, 4),
            "this_month": round(total_this, 4),
            "to_date": round(total_to_date, 4),
            "plan": round(total_plan, 4),
            "variance": round(total_to_date - total_plan, 4),
            "is_total": True,
        })
    return {"rows": rows}


def _apply_review(draft: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(draft)
    kind = _report_type(value.get("report_type") or "monthly")
    mode = _normalise_report_mode(kind, review.get("report_mode") or value.get("report_mode"))
    period = value.get("period") if isinstance(value.get("period"), dict) else {}
    _parse_period(period.get("start"), period.get("end"), kind, mode)
    value["report_type"] = kind
    value["report_title"] = f"{_report_name(kind)} Progress Report"
    value["report_mode"] = mode
    value["status"] = value["report_mode"]
    value["executive_summary"] = _clean_text(review.get("executive_summary", value.get("executive_summary")))
    value["progress"] = _normalize_progress(review.get("progress", value.get("progress", {})))

    current_safety = value.get("safety") if isinstance(value.get("safety"), dict) else {}
    safety_review = review.get("safety") if isinstance(review.get("safety"), dict) else {}
    safety = {
        "total_manpower": max(0, int(_number(safety_review.get("total_manpower", current_safety.get("total_manpower"))))),
        "total_man_hours": max(0.0, _number(safety_review.get("total_man_hours", current_safety.get("total_man_hours")))),
        "recordable_cases": max(0, int(_number(safety_review.get("recordable_cases", current_safety.get("recordable_cases"))))),
        "lost_workdays": max(0, int(_number(safety_review.get("lost_workdays", current_safety.get("lost_workdays"))))),
        "lost_time_injuries": max(0, int(_number(safety_review.get("lost_time_injuries", current_safety.get("lost_time_injuries"))))),
    }
    safety["severity_rate"] = round(
        safety["lost_workdays"] * 1_000_000 / safety["total_man_hours"], 2
    ) if safety["total_man_hours"] else 0
    safety["average_day_away"] = round(
        safety["lost_workdays"] / safety["lost_time_injuries"], 2
    ) if safety["lost_time_injuries"] else 0
    value["safety"] = safety

    for key in ("engineering", "procurement"):
        current = value.get(key) if isinstance(value.get(key), dict) else {}
        incoming = review.get(key) if isinstance(review.get(key), dict) else {}
        current["summary"] = _clean_text(incoming.get("summary", current.get("summary")))
        value[key] = current

    current_site = value.get("site") if isinstance(value.get("site"), dict) else {}
    incoming_site = review.get("site") if isinstance(review.get("site"), dict) else {}
    current_activities = incoming_site.get(
        "current_period_activities",
        incoming_site.get(
            "this_period_activities",
            incoming_site.get(
                "this_week_activities",
                incoming_site.get("this_month_activities", current_site.get(
                    "current_period_activities",
                    current_site.get("this_month_activities", []),
                )),
            ),
        ),
    )
    next_activities = incoming_site.get(
        "next_period_activities",
        incoming_site.get(
            "next_week_activities",
            incoming_site.get("next_month_activities", current_site.get(
                "next_period_activities",
                current_site.get("next_month_activities", []),
            )),
        ),
    )
    current_site["this_month_activities"] = _list_text(current_activities)
    current_site["next_month_activities"] = _list_text(next_activities)
    current_site["current_period_activities"] = current_site["this_month_activities"]
    current_site["this_period_activities"] = current_site["this_month_activities"]
    current_site["next_period_activities"] = current_site["next_month_activities"]
    if kind == "weekly":
        current_site["this_week_activities"] = current_site["this_month_activities"]
        current_site["next_week_activities"] = current_site["next_month_activities"]
    concerns = []
    for item in incoming_site.get("concerns", current_site.get("concerns", []))[:250]:
        if isinstance(item, str):
            concerns.append({"concern": _clean_text(item, 2_000), "corrective_action": ""})
        elif isinstance(item, dict):
            concern = _clean_text(item.get("concern", item.get("text", "")), 2_000)
            action = _clean_text(item.get("corrective_action", item.get("action", "")), 2_000)
            if concern or action:
                concerns.append({"concern": concern, "corrective_action": action})
    current_site["concerns"] = concerns
    value["site"] = current_site
    value["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return value


def _safe_filename_part(value: Any, fallback: str) -> str:
    text = _SAFE_FILENAME_RE.sub("-", str(value or fallback)).strip(" ._-")
    return text[:100] or fallback


def _monthly_filename(draft: dict[str, Any], revision: int) -> str:
    report_name = _report_name(draft.get("report_type") or "monthly")
    project = _safe_filename_part(draft.get("project_no"), "Project")
    period = draft.get("period") if isinstance(draft.get("period"), dict) else {}
    start = _safe_filename_part(period.get("start"), "Start")
    end = _safe_filename_part(period.get("end"), "End")
    mode = _safe_filename_part(str(draft.get("status", "draft")).upper(), "DRAFT")
    return f"{report_name} Progress Report - {project} - {start} to {end} ({mode}) - R{revision}.pdf"


def _draft_report_type(draft: dict[str, Any]) -> str:
    """Read new report types while treating every legacy draft as monthly."""
    return _report_type(draft.get("report_type") or "monthly")


def _render(draft: dict[str, Any], config: dict[str, Any]):
    configured_logo = config.get("logo_gpa") if isinstance(config, dict) else None
    bundled_logo = Path(__file__).resolve().parent.parent / "static" / "pdf_assets" / "gpa_logo.png"
    configured_path = Path(str(configured_logo)) if configured_logo else None
    logo_path = (
        str(configured_path)
        if configured_path is not None and configured_path.is_file()
        else (str(bundled_logo) if bundled_logo.is_file() else None)
    )
    result = render_monthly_report(draft, logo_path=logo_path)
    if hasattr(result, "seek") and hasattr(result, "getvalue"):
        result.seek(0)
        return result
    raise TypeError(f"{_report_name(draft.get('report_type') or 'monthly')} PDF renderer did not return a BytesIO object")


def register_monthly_routes(
    app,
    *,
    data_dir: str,
    config_provider: Callable[[], dict[str, Any]],
    activity_logger: Callable[[str, str, str], None] | None = None,
) -> None:
    """Register Weekly/Monthly Report endpoints on the existing Flask application."""

    def require_login_json():
        if "username" not in session:
            return jsonify({"error": "Login required."}), 401
        return None

    @app.post("/monthly/compile/stored")
    def compile_monthly_stored():
        auth = require_login_json()
        if auth:
            return auth
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid report compile request."}), 400
        kind = "monthly"
        try:
            kind = _report_type(body.get("report_type") or "monthly")
            mode = _normalise_report_mode(kind, body.get("report_mode"))
            start, end = _parse_period(body.get("date_from"), body.get("date_to"), kind, mode)
            project_no = _clean_text(body.get("project_no"), 250)
            project_title = _clean_text(body.get("project_title"), 500)
            if not project_no:
                raise ValueError("Select a project first.")
            include_all = bool(body.get("include_all_users")) and bool(session.get("is_admin"))
            username = None if include_all else session["username"]
            records = list_canonical_records(
                data_dir,
                username=username,
                project_no=project_no,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
            )
            if not records:
                error = (
                    "No final stored JSON was found for this project and weekly period. "
                    "Use Upload Daily Report PDF for older reports."
                    if kind == "weekly"
                    else "No final stored JSON was found for this project and period. Use Upload Daily Report PDF for older reports."
                )
                return jsonify({
                    "error": error
                }), 404
            aggregated = aggregate_monthly_records(
                records,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                project_no=project_no,
                expected_dates=_expected_dates(start, end),
            )
            selected_ids = {
                str(item.get("report_id") or "")
                for item in aggregated.get("source_records", [])
                if isinstance(item, dict)
            }
            selected_records = [
                record for record in records
                if not selected_ids or str(record.get("report_id") or "") in selected_ids
            ]
            manifest = _source_manifest(selected_records, "stored_json")
            draft = _prepare_draft(
                aggregated,
                project_no=project_no,
                project_title=project_title,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                report_mode=mode,
                source_method="stored_json",
                source_manifest=manifest,
                report_context=_latest_report_context(selected_records),
                report_type=kind,
            )
            draft_id = _save_draft(data_dir, session["username"], draft)
            draft["draft_id"] = draft_id
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            report_name = _report_name(kind)
            app.logger.exception("Stored JSON %s compilation failed", kind)
            return jsonify({"error": f"{report_name} compilation failed: {exc}"}), 500

    @app.post("/monthly/compile/upload")
    def compile_monthly_upload():
        auth = require_login_json()
        if auth:
            return auth
        uploads = request.files.getlist("files")
        if not uploads:
            return jsonify({"error": "Choose at least one Daily Report PDF."}), 400
        if len(uploads) > _MAX_UPLOAD_FILES:
            return jsonify({"error": f"A maximum of {_MAX_UPLOAD_FILES} PDF files can be compiled at once."}), 413
        kind = "monthly"
        try:
            kind = _report_type(request.form.get("report_type") or "monthly")
            mode = _normalise_report_mode(kind, request.form.get("report_mode"))
            start, end = _parse_period(
                request.form.get("date_from"),
                request.form.get("date_to"),
                kind,
                mode,
            )
            project_no = _clean_text(request.form.get("project_no"), 250)
            project_title = _clean_text(request.form.get("project_title"), 500)
            if not project_no:
                raise ValueError("Select a project first.")
            config = config_provider()
            known_projects = config.get("projects", []) if isinstance(config, dict) else []
            records = []
            warnings = [
                "Uploaded PDF mode extracts report identity and activity text. "
                "Manpower, progress, safety, engineering, and procurement values must be checked or entered manually."
            ]
            for upload in uploads:
                filename = str(upload.filename or "report.pdf")
                if not filename.lower().endswith(".pdf"):
                    warnings.append(f"Skipped non-PDF file: {filename}")
                    continue
                try:
                    imported = import_daily_report_pdf(
                        upload.stream,
                        filename=filename,
                        known_projects=known_projects,
                    )
                except PDFImportError as exc:
                    warnings.append(f"{filename}: {exc}")
                    continue
                data = imported.get("data") if isinstance(imported.get("data"), dict) else {}
                parsed_project = _clean_text(data.get("project_no"), 250)
                if parsed_project and parsed_project.casefold() != project_no.casefold():
                    warnings.append(
                        f"{filename}: project number {parsed_project} does not match selected project {project_no}; file skipped."
                    )
                    continue
                if not parsed_project:
                    data["project_no"] = project_no
                    data["project_title"] = data.get("project_title") or project_title
                    imported.setdefault("warnings", []).append("Project assigned from the user's selected project.")
                data["project_no"] = project_no
                if project_title:
                    data["project_title"] = project_title
                report_date = str(data.get("date") or imported.get("report_date") or "")
                if not report_date:
                    warnings.append(f"{filename}: report date was not detected; file requires manual import and was skipped.")
                    continue
                if not (start.strftime("%Y-%m-%d") <= report_date <= end.strftime("%Y-%m-%d")):
                    warnings.append(f"{filename}: date {report_date} is outside the selected period; file skipped.")
                    continue
                source = imported.get("source") if isinstance(imported.get("source"), dict) else {}
                report_id = f"pdf-{source.get('sha256') or uuid.uuid4().hex}"
                records.append({
                    "record_type": "final_daily_report",
                    "report_id": report_id,
                    "revision": int(imported.get("revision") or 1),
                    "username": session["username"],
                    "date": report_date,
                    "project_no": project_no,
                    "project_title": project_title,
                    "generated_at": "",
                    "payload": data,
                    "source": source,
                    "confidence": imported.get("confidence", {}),
                    "import_status": imported.get("status", "needs_review"),
                    "review_required": True,
                })
                if imported.get("status") != "ready":
                    warnings.append(f"{filename}: parser result needs manual review.")
                for warning in imported.get("warnings", []):
                    warnings.append(f"{filename}: {_warning_text(warning)}")

            if not records:
                error = (
                    "None of the uploaded PDFs could be included in this Weekly Report. "
                    "Check the project, period, text layer, and file warnings."
                    if kind == "weekly"
                    else "None of the uploaded PDFs could be included. Check the project, period, text layer, and file warnings."
                )
                return jsonify({
                    "error": error,
                    "warnings": warnings,
                }), 400
            aggregated = aggregate_monthly_records(
                records,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                project_no=project_no,
                expected_dates=_expected_dates(start, end),
            )
            selected_ids = {
                str(item.get("report_id") or "")
                for item in aggregated.get("source_records", [])
                if isinstance(item, dict)
            }
            selected_records = [
                record for record in records
                if not selected_ids or str(record.get("report_id") or "") in selected_ids
            ]
            manifest = _source_manifest(selected_records, "uploaded_pdf")
            draft = _prepare_draft(
                aggregated,
                project_no=project_no,
                project_title=project_title,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                report_mode=mode,
                source_method="uploaded_pdf",
                source_manifest=manifest,
                report_context=_latest_report_context(selected_records),
                extra_warnings=warnings,
                report_type=kind,
            )
            draft_id = _save_draft(data_dir, session["username"], draft)
            draft["draft_id"] = draft_id
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Uploaded PDF %s compilation failed", kind)
            prefix = "Weekly PDF" if kind == "weekly" else "PDF"
            return jsonify({"error": f"{prefix} compilation failed: {exc}"}), 500

    @app.post("/monthly/preview/<draft_id>")
    def preview_monthly_report(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        draft = _load_draft(data_dir, session["username"], draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        kind = _draft_report_type(draft)
        report_name = _report_name(kind)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid review data."}), 400
        try:
            reviewed = _apply_review(draft, body)
            _update_draft(data_dir, session["username"], reviewed)
            buffer = _render(reviewed, config_provider())
            return send_file(
                buffer,
                mimetype="application/pdf",
                as_attachment=False,
                download_name=f"{report_name} Progress Report Preview.pdf",
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("%s preview failed", report_name)
            return jsonify({"error": f"{report_name} preview failed: {exc}"}), 500

    @app.post("/monthly/generate/<draft_id>")
    def generate_monthly_report_route(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        kind = _draft_report_type(draft)
        report_name = _report_name(kind)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid review data."}), 400
        try:
            reviewed = _apply_review(draft, body)
            if reviewed.get("status") == "final" and not bool(body.get("confirm_final")):
                return jsonify({
                    "error": f"Confirm that all warnings, missing dates, and {kind} values were reviewed before saving a Final report."
                }), 400
            if reviewed.get("status") == "final":
                reviewed["final_review"] = {
                    "confirmed": True,
                    "confirmed_by": username,
                    "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                }
            index = get_monthly_reports_index(data_dir, username)
            same_period = [row for row in index if (
                (row.get("report_type") or "monthly") == kind
                and row.get("project_no") == reviewed.get("project_no")
                and row.get("period_start") == reviewed.get("period", {}).get("start")
                and row.get("period_end") == reviewed.get("period", {}).get("end")
            )]
            revision = max([int(row.get("revision", 0)) for row in same_period] or [0]) + 1
            filename = _monthly_filename(reviewed, revision)
            reports_dir = _monthly_user_dir(data_dir, username) / "reports"
            pdf_path = reports_dir / filename
            json_filename = f"{Path(filename).stem}.json"
            json_path = reports_dir / json_filename
            buffer = _render(reviewed, config_provider())
            pdf_bytes = buffer.getvalue()
            temporary = pdf_path.with_name(f"{pdf_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("wb") as handle:
                    handle.write(pdf_bytes)
                os.replace(temporary, pdf_path)
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass

            reviewed["monthly_report_id"] = uuid.uuid4().hex
            reviewed["report_id"] = reviewed["monthly_report_id"]
            if kind == "weekly":
                reviewed["weekly_report_id"] = reviewed["monthly_report_id"]
            reviewed["revision"] = revision
            reviewed["generated_at"] = datetime.now().isoformat(timespec="seconds")
            reviewed["filename"] = filename
            _atomic_json(json_path, reviewed)
            entry = {
                "monthly_report_id": reviewed["monthly_report_id"],
                "report_id": reviewed["monthly_report_id"],
                "report_type": kind,
                "filename": filename,
                "json_filename": json_filename,
                "project_no": reviewed.get("project_no", ""),
                "project_title": reviewed.get("project_title", ""),
                "period_start": reviewed.get("period", {}).get("start", ""),
                "period_end": reviewed.get("period", {}).get("end", ""),
                "status": reviewed.get("status", "draft"),
                "source_method": reviewed.get("source_method", ""),
                "revision": revision,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "size_kb": round(len(pdf_bytes) / 1024, 1),
            }
            index.insert(0, entry)
            _save_monthly_index(data_dir, username, index)
            _update_draft(data_dir, username, reviewed)
            if activity_logger:
                detail = (
                    f"project={entry['project_no']} period={entry['period_start']}..{entry['period_end']} revision={revision}"
                )
                if kind == "weekly":
                    detail = f"type=weekly {detail}"
                activity_logger(
                    username,
                    f"{kind}_report_generated",
                    detail,
                )
            return jsonify({
                "ok": True,
                "filename": filename,
                "download_url": url_for("download_monthly_report", filename=filename),
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("%s report generation failed", report_name)
            return jsonify({"error": f"{report_name} report generation failed: {exc}"}), 500

    @app.get("/monthly/download/<path:filename>")
    def download_monthly_report(filename: str):
        if "username" not in session:
            return "Login required", 401
        username = session["username"]
        basename = os.path.basename(filename)
        if filename != basename:
            return "Invalid filename", 400
        index = get_monthly_reports_index(data_dir, username)
        if not any(row.get("filename") == basename for row in index):
            return "Not found", 404
        path = _monthly_user_dir(data_dir, username) / "reports" / basename
        if not path.is_file():
            return "Not found", 404
        return send_file(path, as_attachment=True, download_name=basename, mimetype="application/pdf")

    @app.post("/monthly/delete")
    def delete_monthly_report():
        auth = require_login_json()
        if auth:
            return auth
        body = request.get_json(silent=True) or {}
        filename = str(body.get("filename") or "")
        basename = os.path.basename(filename)
        if not basename or filename != basename:
            return jsonify({"error": "Invalid filename."}), 400
        username = session["username"]
        index = get_monthly_reports_index(data_dir, username)
        matches = [row for row in index if row.get("filename") == basename]
        if not matches:
            return jsonify({"error": "Report not found."}), 404
        reports_dir = _monthly_user_dir(data_dir, username) / "reports"
        for match in matches:
            for name in (match.get("filename"), match.get("json_filename")):
                if not name or os.path.basename(str(name)) != str(name):
                    continue
                path = reports_dir / str(name)
                if path.is_file():
                    path.unlink()
        _save_monthly_index(data_dir, username, [row for row in index if row.get("filename") != basename])
        if activity_logger:
            report_type = str(matches[0].get("report_type") or "monthly")
            activity_logger(username, f"{report_type}_report_deleted", basename)
        return jsonify({"ok": True})
