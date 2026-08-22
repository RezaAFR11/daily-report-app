from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable
from zoneinfo import ZoneInfo

from flask import jsonify, request, send_file, session, url_for

from .aggregate import aggregate_monthly_records
from .ai_summary import AISummaryError, generate_ai_summary
from .importer import DEFAULT_LIMITS, PDFImportError, import_daily_report_pdf
from .overtime import parse_overtime_workbooks
from .photos import (
    DEFAULT_PHOTO_LIMITS,
    attach_canonical_photo_candidates,
    asset_filename,
    copy_photo_assets,
    extract_pdf_photo_candidates,
    is_asset_id,
    periodic_photo_limits,
    store_photo_candidates,
)
from .renderer import render_monthly_report
from .report_quality import build_report_preflight
from .storage import list_canonical_records
from .timesheet import TimesheetError, compile_timesheets
from .validation import (
    build_source_validation,
    resolve_duplicate_records,
    resolve_project_records,
)
from .workforce import (
    decide_overtime,
    decide_timesheet,
    ensure_workforce_state,
    has_pending_workforce_review,
    reset_workforce,
    set_overtime_preview,
    set_timesheet_preview,
)


_DRAFT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_UPLOAD_FILE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._() -]+")
_MAX_UPLOAD_FILES = 35
_MAX_ACTIVE_UPLOAD_SESSIONS = 10
_UPLOAD_SESSION_TTL_SECONDS = 24 * 60 * 60
_UPLOAD_OPERATION_LOCK_STALE_SECONDS = 15 * 60
_MAX_STAGED_REQUEST_BYTES = DEFAULT_LIMITS.max_bytes + (1024 * 1024)
_MAX_REVIEW_TEXT = 30_000
_REPORT_TYPES = {"monthly", "weekly"}
_MAX_PHOTO_REVIEW_BYTES = 128 * 1024
_STORED_PHOTO_WARNING_PREFIX = "Stored JSON photo:"
_MAX_WORKBOOK_FILES = 6
_MAX_WORKBOOK_FILE_BYTES = 16 * 1024 * 1024
_MAX_WORKBOOK_REQUEST_BYTES = 48 * 1024 * 1024
_AI_COOLDOWN_SECONDS = 20
_AI_DRAFT_LOCK_STALE_SECONDS = 5 * 60
_AI_DRAFT_LOCK_RETRY_SECONDS = 5
_MAKASSAR_TIMEZONE = ZoneInfo("Asia/Makassar")

# Sandbox/testing policy for issued Final periodic reports.
#
# This branch is used for report-development testing, so Final revisions may be
# hard-deleted and repeated Final issues do not require a revision reason by
# default.  Before using this code in production, set:
#
#     PROTECT_FINAL_PERIODIC_REPORTS=true
#
# That restores immutable Final reports and mandatory revision reasons.
_PROTECT_FINAL_PERIODIC_REPORTS = str(
    os.getenv("PROTECT_FINAL_PERIODIC_REPORTS", "false")
).strip().lower() in {"1", "true", "yes", "on"}
_ALLOW_FINAL_REPORT_DELETE = not _PROTECT_FINAL_PERIODIC_REPORTS
_REQUIRE_FINAL_REVISION_REASON = _PROTECT_FINAL_PERIODIC_REPORTS


def _report_type(value: Any) -> str:
    text = str(value or "monthly").strip().lower()
    if text not in _REPORT_TYPES:
        raise ValueError("Report type must be monthly or weekly.")
    return text


def _report_name(report_type: Any) -> str:
    return "Weekly" if _report_type(report_type) == "weekly" else "Monthly"


def _preflight_failure_message(preflight: Mapping[str, Any]) -> str:
    blockers = preflight.get("blockers") if isinstance(preflight, Mapping) else []
    messages: list[str] = []
    if isinstance(blockers, list):
        for row in blockers:
            if isinstance(row, Mapping):
                message = _clean_text(row.get("message"), 1_000)
            else:
                message = _clean_text(row, 1_000)
            if message and message not in messages:
                messages.append(message)
    if not messages:
        return "Report preflight failed."
    return "Report preflight failed:\n- " + "\n- ".join(messages)


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


def _makassar_issue_date(now: datetime | None = None) -> str:
    """Return the report issue date in the project's local timezone."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(_MAKASSAR_TIMEZONE).date().isoformat()


def _ai_draft_lock_path(data_dir: str | Path, username: str, draft_id: str) -> Path:
    if not _DRAFT_ID_RE.fullmatch(str(draft_id or "")):
        raise ValueError("Invalid report draft ID")
    return _monthly_user_dir(data_dir, username) / "drafts" / f".{draft_id}.ai.lock"


def _acquire_ai_draft_lock(
    data_dir: str | Path,
    username: str,
    draft_id: str,
) -> tuple[Path, str] | None:
    """Atomically acquire one paid-AI operation lock for a report draft."""

    lock_path = _ai_draft_lock_path(data_dir, username, draft_id)
    for attempt in range(2):
        token = uuid.uuid4().hex
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError:
            try:
                stale = lock_path.stat().st_mtime < (
                    time.time() - _AI_DRAFT_LOCK_STALE_SECONDS
                )
            except FileNotFoundError:
                continue
            except OSError:
                stale = False
            if attempt == 0 and stale:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
            return None
        payload = json.dumps(
            {
                "token": token,
                "draft_id": draft_id,
                "username": username,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        except Exception:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        return lock_path, token
    return None


def _release_ai_draft_lock(lock: tuple[Path, str] | None) -> None:
    """Release only the lock created by this request."""

    if lock is None:
        return
    lock_path, token = lock
    try:
        with lock_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or value.get("token") != token:
            return
        lock_path.unlink()
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return


def _ai_cooldown_remaining(draft: dict[str, Any], now: datetime | None = None) -> int:
    """Return whole retry seconds for a persisted per-draft AI cooldown."""

    control = draft.get("ai_request_control")
    control = control if isinstance(control, dict) else {}
    timestamp = str(control.get("last_started_at") or "")
    if not timestamp:
        previous = draft.get("ai_summary")
        previous = previous if isinstance(previous, dict) else {}
        timestamp = str(previous.get("requested_at") or "")
    if not timestamp:
        return 0
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        # Legacy drafts stored server-local naive timestamps. Compare them
        # with the current server-local clock to preserve their short cooldown.
        elapsed = (datetime.now() - parsed).total_seconds()
    else:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        elapsed = (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    remaining = _AI_COOLDOWN_SECONDS - elapsed
    if remaining <= 0:
        return 0
    return max(1, int(remaining) + (0 if remaining.is_integer() else 1))


def _ai_retry_response(message: str, *, code: str, status: int, retry_after: int):
    response = jsonify({
        "error": message,
        "code": code,
        "retryable": True,
        "retry_after_seconds": max(1, int(retry_after)),
    })
    response.status_code = status
    response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _draft_photo_dir(
    data_dir: str | Path,
    username: str,
    draft_id: str,
    *,
    create: bool = True,
) -> Path | None:
    if not _DRAFT_ID_RE.fullmatch(str(draft_id or "")):
        return None
    root = (_monthly_user_dir(data_dir, username) / "draft_assets").resolve(strict=False)
    directory = (root / draft_id).resolve(strict=False)
    if directory.parent != root:
        return None
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def _photo_asset_preflight_issues(
    data_dir: str | Path,
    username: str,
    draft_id: str,
    report: Mapping[str, Any],
) -> list[str]:
    """Return reviewed photo references whose draft-local JPEG asset is unavailable."""
    photos = report.get("photo_documentation")
    if not isinstance(photos, list) or not photos:
        return []

    directory = _draft_photo_dir(data_dir, username, draft_id, create=False)
    issues: list[str] = []
    for index, row in enumerate(photos, start=1):
        if not isinstance(row, Mapping):
            issues.append(f"Photo {index} has invalid metadata.")
            continue
        asset_id = str(row.get("asset_id") or "")
        if not is_asset_id(asset_id):
            issues.append(f"Photo {index} has an invalid asset reference.")
            continue
        path = directory / asset_filename(asset_id) if directory is not None else None
        if path is None or not path.is_file():
            caption = _clean_text(row.get("caption"), 120)
            label = f" ({caption})" if caption else ""
            issues.append(f"Photo {index}{label} asset is unavailable.")

    return issues


def _append_runtime_preflight_blockers(
    preflight: dict[str, Any],
    *,
    data_dir: str | Path,
    username: str,
    draft_id: str,
    report: Mapping[str, Any],
    for_final: bool,
) -> dict[str, Any]:
    """Add checks that need filesystem/runtime context to the pure preflight result."""
    if for_final:
        for message in _photo_asset_preflight_issues(data_dir, username, draft_id, report):
            preflight.setdefault("blockers", []).append({
                "code": "photo_asset_unavailable",
                "message": message,
            })
    preflight["ready"] = not bool(preflight.get("blockers"))
    return preflight


def _remove_draft_assets(data_dir: str | Path, username: str, draft_id: str) -> None:
    directory = _draft_photo_dir(data_dir, username, draft_id, create=False)
    if directory is not None and directory.is_dir():
        shutil.rmtree(directory)


def _is_generated_stored_photo_warning(value: Any) -> bool:
    text = str(value or "").strip()
    return text.startswith(_STORED_PHOTO_WARNING_PREFIX) or bool(
        re.fullmatch(
            r"\d+ (?:duplicate photo\(s\) across Daily Reports were removed"
            r"|photo\(s\) exceeded the \d+-photo or draft asset byte limit and were excluded)\.",
            text,
        )
    )


def _prune_draft_photo_assets(
    data_dir: str | Path,
    username: str,
    draft_id: str,
    references: Any,
) -> None:
    """Best-effort removal of assets no longer selected for one draft."""

    directory = _draft_photo_dir(data_dir, username, draft_id, create=False)
    if directory is None or not directory.is_dir():
        return
    allowed = {
        str(item.get("asset_id") or "")
        for item in (references if isinstance(references, list) else [])
        if isinstance(item, dict) and is_asset_id(item.get("asset_id"))
    }
    for path in directory.glob("*.jpg"):
        if is_asset_id(path.stem) and path.stem not in allowed:
            try:
                path.unlink()
            except OSError:
                continue


def _upload_sessions_dir(data_dir: str | Path, username: str) -> Path:
    directory = _monthly_user_dir(data_dir, username) / "upload_sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _upload_session_dir(
    data_dir: str | Path,
    username: str,
    upload_session_id: str,
) -> Path | None:
    upload_session_id = str(upload_session_id or "")
    if not _DRAFT_ID_RE.fullmatch(upload_session_id):
        return None
    root = _upload_sessions_dir(data_dir, username).resolve(strict=False)
    directory = (root / upload_session_id).resolve(strict=False)
    if directory.parent != root:
        return None
    return directory


def _remove_upload_session(data_dir: str | Path, username: str, upload_session_id: str) -> bool:
    directory = _upload_session_dir(data_dir, username, upload_session_id)
    if directory is None or not directory.exists():
        return False
    root = _upload_sessions_dir(data_dir, username).resolve(strict=False)
    if directory.resolve(strict=False).parent != root:
        return False
    if directory.is_dir():
        shutil.rmtree(directory)
    else:
        directory.unlink()
    return True


def _cleanup_upload_sessions(data_dir: str | Path, username: str) -> None:
    root = _upload_sessions_dir(data_dir, username)
    cutoff = time.time() - _UPLOAD_SESSION_TTL_SECONDS
    for directory in root.iterdir():
        if not directory.is_dir() or not _DRAFT_ID_RE.fullmatch(directory.name):
            continue
        try:
            if directory.stat().st_mtime >= cutoff:
                continue
            _remove_upload_session(data_dir, username, directory.name)
        except OSError:
            continue


def _load_upload_session(
    data_dir: str | Path,
    username: str,
    upload_session_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    directory = _upload_session_dir(data_dir, username, upload_session_id)
    if directory is None:
        return None
    try:
        if directory.is_dir() and directory.stat().st_mtime < time.time() - _UPLOAD_SESSION_TTL_SECONDS:
            _remove_upload_session(data_dir, username, upload_session_id)
            return None
    except OSError:
        return None
    manifest_path = directory / "session.json"
    if not manifest_path.is_file():
        return None
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or manifest.get("owner") != username:
            return None
        return directory, manifest
    except (OSError, ValueError, TypeError):
        return None


def _load_upload_item(directory: Path, file_id: str) -> dict[str, Any] | None:
    if not _UPLOAD_FILE_ID_RE.fullmatch(str(file_id or "")):
        return None
    path = directory / "items" / f"{file_id}.json"
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _public_upload_item(item: dict[str, Any]) -> dict[str, Any]:
    record = item.get("record") if isinstance(item.get("record"), dict) else {}
    source_identity = (
        record.get("source_identity")
        if isinstance(record.get("source_identity"), dict)
        else {}
    )
    return {
        "file_id": str(item.get("file_id") or ""),
        "filename": str(item.get("filename") or "report.pdf"),
        "status": str(item.get("status") or "skipped"),
        "included": bool(item.get("included")),
        "report_date": str(item.get("report_date") or ""),
        "size_bytes": int(item.get("size_bytes") or 0),
        "warnings": [str(value) for value in item.get("warnings", []) if str(value).strip()],
        "source_project_no": str(source_identity.get("project_no") or ""),
        "source_project_title": str(source_identity.get("project_title") or ""),
    }


def _acquire_upload_operation_lock(directory: Path) -> Path | None:
    lock_path = directory / ".operation.lock"
    for attempt in range(2):
        try:
            lock_path.mkdir()
            return lock_path
        except (FileExistsError, FileNotFoundError):
            if not directory.is_dir():
                return None
            try:
                stale = lock_path.stat().st_mtime < time.time() - _UPLOAD_OPERATION_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if attempt == 0 and stale:
                try:
                    lock_path.rmdir()
                    continue
                except OSError:
                    pass
            return None
    return None


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


def _save_draft(
    data_dir: str | Path,
    username: str,
    draft: dict[str, Any],
    *,
    draft_id: str | None = None,
) -> str:
    drafts_dir = _monthly_user_dir(data_dir, username) / "drafts"
    cutoff = datetime.now().timestamp() - (7 * 24 * 60 * 60)
    existing = sorted(drafts_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in existing[19:]:
        try:
            stale.unlink()
            _remove_draft_assets(data_dir, username, stale.stem)
        except OSError:
            pass
    for stale in existing[:19]:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
                _remove_draft_assets(data_dir, username, stale.stem)
        except OSError:
            pass
    draft_id = str(draft_id or uuid.uuid4().hex)
    if not _DRAFT_ID_RE.fullmatch(draft_id):
        raise ValueError("Invalid report draft ID")
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
        if day_count > 7:
            raise ValueError("A Weekly Report period cannot be longer than 7 days.")
        if mode != "wtd" and day_count != 7:
            raise ValueError("A full Weekly Report period must be exactly 7 consecutive days.")
    return start, end


def _rolling_week_period(records: list[dict[str, Any]]) -> tuple[datetime, datetime]:
    """Anchor an uploaded weekly batch to its earliest valid report date."""
    valid_dates: list[datetime] = []
    for record in records:
        try:
            valid_dates.append(datetime.strptime(_record_date(record), "%Y-%m-%d"))
        except (TypeError, ValueError):
            continue
    if not valid_dates:
        raise ValueError("No uploaded PDF has a valid report date for the Weekly Report.")
    start = min(valid_dates)
    return start, start + timedelta(days=6)


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


def _optional_number(value: Any) -> float | None:
    """Return a finite numeric value without turning missing data into zero."""

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text.replace("%", "").replace(",", ""))
    except ValueError:
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _optional_non_negative(value: Any, *, integer: bool = False) -> int | float | None:
    parsed = _optional_number(value)
    if parsed is None:
        return None
    parsed = max(0.0, parsed)
    return int(parsed) if integer else parsed


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


def _clean_activity_rows(value: Any, maximum_items: int = 250) -> list[dict[str, str]]:
    """Keep AI-condensed activity bullets structured by area for rendering."""

    rows = value if isinstance(value, list) else []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in rows[:maximum_items]:
        status = ""
        if isinstance(item, str):
            area = ""
            text = _clean_text(item, 2_000)
        elif isinstance(item, dict):
            area = _clean_text(item.get("area"), 200)
            text = _clean_text(
                item.get("text", item.get("activity", item.get("description", ""))),
                2_000,
            )
            status = _clean_text(item.get("status"), 100)
        else:
            continue
        if not text or text.casefold() == "not supplied":
            continue
        key = (area.casefold(), text.casefold())
        if key in seen:
            continue
        seen.add(key)
        row = {"area": area or "Site", "text": text}
        if status and status.casefold() not in text.casefold():
            row["status"] = status
        result.append(row)
    return result


_ACTIVITY_EQUIPMENT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<prefix>\d{1,3})\s*[- ]\s*"
    r"(?P<tag>[A-Za-z]{2,8})\s*[- ]\s*(?P<number>\d{2,6})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _activity_match_text(value: Any) -> str:
    text = _clean_text(value, 2_000).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _activity_equipment_ids(value: Any) -> set[str]:
    result: set[str] = set()
    text = _clean_text(value, 2_000)
    for match in _ACTIVITY_EQUIPMENT_ID_RE.finditer(text):
        result.add(
            f"{match.group('prefix')}-{match.group('tag').upper()}-{match.group('number')}"
        )
    return result


def _source_activity_status_rows(draft: dict[str, Any]) -> list[dict[str, str]]:
    """Return deterministic source activities carrying an explicit status."""

    rows = draft.get("activities") if isinstance(draft.get("activities"), list) else []
    result: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        description = _clean_text(row.get("description", row.get("text", "")), 2_000)
        status = _clean_text(row.get("status"), 100)
        if not description or not status:
            continue
        result.append({
            "area": _clean_text(row.get("area"), 200),
            "description": description,
            "status": status,
        })
    return result


def _enrich_activity_statuses(
    rows: Any,
    draft: dict[str, Any],
) -> list[dict[str, str]]:
    """Restore source-backed status when Claude omits it from a condensed bullet."""

    cleaned = _clean_activity_rows(rows)
    sources = _source_activity_status_rows(draft)
    if not cleaned or not sources:
        return cleaned

    for row in cleaned:
        if row.get("status"):
            continue

        text = row.get("text", "")
        area_key = _activity_match_text(row.get("area", ""))
        text_key = _activity_match_text(text)
        equipment_ids = _activity_equipment_ids(text)

        matches: list[dict[str, str]] = []
        for source in sources:
            source_area = _activity_match_text(source.get("area", ""))
            if area_key and source_area and area_key != source_area:
                continue

            source_text = _activity_match_text(source.get("description", ""))
            source_ids = _activity_equipment_ids(source.get("description", ""))
            text_match = (
                bool(source_text)
                and (
                    source_text == text_key
                    or source_text in text_key
                    or text_key in source_text
                )
            )
            id_match = bool(
                equipment_ids
                and source_ids
                and equipment_ids.intersection(source_ids)
            )
            if text_match or id_match:
                matches.append(source)

        statuses = {item["status"] for item in matches if item.get("status")}
        if len(statuses) == 1:
            status = next(iter(statuses))
            if status.casefold() not in text.casefold():
                row["status"] = status

    return cleaned

def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload", record.get("data", {}))
    return value if isinstance(value, dict) else {}


def _record_photo_areas(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return parsed Daily Report areas used to label extracted PDF photographs."""

    areas = _payload(record).get("areas")
    return [item for item in areas if isinstance(item, dict)] if isinstance(areas, list) else []


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


_FILENAME_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def _valid_iso_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return ""


def _date_from_filename(value: Any) -> str:
    match = _FILENAME_ISO_DATE_RE.search(str(value or ""))
    return _valid_iso_date_text(match.group(1)) if match else ""


def _imported_report_date(imported: dict[str, Any], filename: str = "") -> tuple[str, str]:
    """Resolve parser date across current/legacy envelope shapes.

    The importer historically exposed report date both at envelope level and in
    ``data``.  Keeping this boundary tolerant prevents a parser-shape change from
    appearing in Review Weekly/Monthly Draft as a false Missing Date.
    """

    containers = [
        ("imported.report_date", imported),
        ("imported.date", imported),
        ("imported.data.date", imported.get("data") if isinstance(imported.get("data"), dict) else {}),
        ("imported.payload.date", imported.get("payload") if isinstance(imported.get("payload"), dict) else {}),
    ]
    keys = ["report_date", "date", "date", "date"]
    for (method, container), key in zip(containers, keys):
        parsed = _valid_iso_date_text(container.get(key) if isinstance(container, dict) else "")
        if parsed:
            return parsed, method

    source = imported.get("source") if isinstance(imported.get("source"), dict) else {}
    parsed = _date_from_filename(filename or source.get("filename"))
    if parsed:
        return parsed, "filename_iso_fallback"
    return "", "missing"


def _record_date(record: dict[str, Any]) -> str:
    candidates = (
        record.get("report_date"),
        record.get("date"),
        (_payload(record).get("date") if isinstance(_payload(record), dict) else ""),
        (record.get("data", {}).get("date") if isinstance(record.get("data"), dict) else ""),
    )
    for value in candidates:
        parsed = _valid_iso_date_text(value)
        if parsed:
            return parsed
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    return _date_from_filename(source.get("filename"))


# Client-facing deterministic fallback summarisation.  AI may refine this after
# Source Data Validation, but the baseline Weekly/Monthly PDF should already read
# like a period report rather than seven/thirty Daily Reports concatenated together.
_DETERMINISTIC_SUMMARY_VERSION = "periodic-deterministic-summary/3"

_PERIOD_ACTIVITY_TAG_RE = re.compile(
    r"\(\s*\d{1,3}\s*-\s*[A-Za-z]{2,}\s*-\s*[^)]*\)", re.IGNORECASE
)

# Weighted keyword rules keep the classification deterministic while allowing one
# mixed Daily activity to land in the workstream that best describes its primary
# work.  The rules are intentionally construction-domain terms already present in
# GPA Daily Reports; they are not free-form AI categories.
_PERIOD_ACTIVITY_FAMILY_RULES: tuple[
    tuple[str, tuple[tuple[str, float], ...]], ...
] = (
    (
        "Testing & Commissioning",
        (
            ("loop test", 5.0), ("calibrat", 5.0), ("commission", 5.0),
            ("function test", 3.0), ("leak test", 3.0), ("continuity", 3.0),
            ("dcs", 2.0),
        ),
    ),
    (
        "Actuator & Pneumatic",
        (
            ("actuator", 3.5), ("pneumatic", 3.5), ("silinder", 3.0),
            ("cylinder", 3.0), ("solenoid", 3.0), ("regulator", 2.5),
            ("tubing", 2.0), ("hose", 2.0), ("5-way", 2.5),
            ("5 way", 2.5), ("6-way", 2.5), ("6 way", 2.5),
            ("setting shaft", 2.0), ("setting shaf", 2.0),
        ),
    ),
    (
        "Instrumentation & Electrical",
        (
            ("selector switch", 4.0), ("proximity", 3.5),
            ("junction box", 3.5), ("flexible conduit", 3.5),
            ("connect cable", 3.0), ("termination", 3.0),
            ("cable", 2.0), ("rewir", 3.0),
        ),
    ),
    (
        "Valve Mechanical",
        (
            ("butterfly", 4.5), ("seat rubber", 4.5),
            ("install valve", 4.0), ("valve control", 3.5), ("position valve", 3.5),
            ("reassembl", 3.5), ("tighten", 1.5), ("bolt", 1.5),
            ("gasket", 2.5), ("mechanical actuator damper", 2.5),
        ),
    ),
)

_PERIOD_ACTIVITY_THEME_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "Testing & Commissioning": (
        ("DCS loop testing", ("loop test", "dcs")),
        ("local functional testing", ("function test", "local")),
        ("continuity checking", ("continuity",)),
        ("leak testing", ("leak test",)),
        ("calibration", ("calibrat",)),
    ),
    "Actuator & Pneumatic": (
        ("actuator and cylinder work", ("actuator", "cylinder", "silinder")),
        ("solenoid and multi-way valve work", ("solenoid", "5-way", "5 way", "6-way", "6 way")),
        ("regulator, tubing and hose work", ("regulator", "tubing", "hose", "air instrument")),
        ("pneumatic shaft setting", ("setting shaft", "setting shaf")),
    ),
    "Instrumentation & Electrical": (
        ("proximity installation", ("installation proximity", "install proximity")),
        ("proximity position adjustment", ("adjust proximity",)),
        ("selector-switch inspection and repair", ("check and repair selector", "check and repait selector")),
        ("selector-switch rewiring and replacement", ("rewiring selector", "replace new selector")),
        ("flexible conduit and cable connection", ("flexible conduit", "connect cable")),
        ("junction-box work", ("junction box",)),
        ("proximity bracket fabrication", ("bracket proximity",)),
        ("termination work", ("termination",)),
    ),
    "Valve Mechanical": (
        ("valve assembly and installation", ("reassembl", "install valve", "seat rubber")),
        ("valve troubleshooting", ("trouble shoot", "troubleshoot")),
        ("valve position work", ("position valve",)),
        ("bolt-related mechanical maintenance", ("bolt", "tighten", "gasket", "cleaning all mechanical")),
    ),
}

_WORKSTREAM_DISPLAY_ORDER = (
    "Instrumentation & Electrical",
    "Actuator & Pneumatic",
    "Valve Mechanical",
    "Testing & Commissioning",
    "Other Site Work",
)


def _workstream_rank(value: Any) -> int:
    text = _clean_text(value, 255)
    try:
        return _WORKSTREAM_DISPLAY_ORDER.index(text)
    except ValueError:
        return len(_WORKSTREAM_DISPLAY_ORDER)


def _area_sort_key(value: Any) -> tuple[str, int, str]:
    text = _clean_text(value, 255)
    match = re.match(r"^([A-Za-z]+)[- ]?(\d+)(.*)$", text)
    if match:
        return (match.group(1).casefold(), int(match.group(2)), match.group(3).casefold())
    return (text.casefold(), 10**9, "")


def _period_activity_base(value: Any) -> str:
    text = _clean_text(value, 2_000)
    if not text:
        return ""
    text = text.lstrip("■▪●□•- ")
    # Keep issue/findings in deterministic constraints; the management activity
    # summary should describe the work itself rather than repeat inline Notes.
    text = re.split(r"\bNote\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    # Remove only parenthesised equipment-tag tails such as (81-EV-3833).
    # Technical prose and quantities outside those tag blocks are preserved.
    text = _PERIOD_ACTIVITY_TAG_RE.sub("", text)
    text = re.sub(r"\s+([,;:.])", r"\1", text)
    text = re.sub(r"\s*&\s*", " and ", text)
    return " ".join(text.split()).strip(" ,;:.-")


def _period_activity_family(value: Any) -> str:
    text = _clean_text(value, 2_000).casefold()
    if not text:
        return "Other Site Work"
    ranked: list[tuple[float, int, str]] = []
    for index, (label, rules) in enumerate(_PERIOD_ACTIVITY_FAMILY_RULES):
        score = sum(weight for needle, weight in rules if needle in text)
        if score > 0:
            ranked.append((score, -index, label))
    if not ranked:
        return "Other Site Work"
    ranked.sort(reverse=True)
    return ranked[0][2]


def _period_activity_themes(family: str, value: Any) -> list[str]:
    text = _clean_text(value, 2_000).casefold()
    result: list[str] = []
    for label, needles in _PERIOD_ACTIVITY_THEME_RULES.get(family, ()):
        # Theme labels are deterministic paraphrases of explicit source terms.
        # Requiring at least one family-specific keyword prevents unrelated work
        # from being pulled into a more attractive management label.
        if any(needle in text for needle in needles):
            if label not in result:
                result.append(label)
    return result


def _theme_rank(family: str, label: str) -> int:
    ordered = [item[0] for item in _PERIOD_ACTIVITY_THEME_RULES.get(family, ())]
    try:
        return ordered.index(label)
    except ValueError:
        return len(ordered)


def _english_join(values: list[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _short_period_label(start: str, end: str) -> str:
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return f"{start} to {end}" if start and end else (start or end)
    if start_date.date() == end_date.date():
        return start_date.strftime("%d %B %Y")
    if start_date.year == end_date.year and start_date.month == end_date.month:
        return f"{start_date.day:02d}-{end_date.day:02d} {end_date.strftime('%B %Y')}"
    if start_date.year == end_date.year:
        return f"{start_date.strftime('%d %B')}-{end_date.strftime('%d %B %Y')}"
    return f"{start_date.strftime('%d %B %Y')}-{end_date.strftime('%d %B %Y')}"


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _format_positive_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number) or number <= 0:
        return ""
    return _format_number(number)


def _summarise_period_activities(value: Any, *, max_phrases_per_group: int = 3) -> list[dict[str, Any]]:
    """Build a deterministic management summary by Area + Workstream.

    The full Daily activity rows always remain in ``draft['activities']``.  This
    function creates a compact client-facing layer with source/date provenance,
    theme-level de-duplication and equipment tags retained as metadata for audit
    and optional AI polishing.
    """

    rows = value if isinstance(value, list) else []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    group_order: list[tuple[str, str]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        description = _clean_text(raw.get("description", raw.get("text")), 2_000)
        if not description:
            continue
        area = _clean_text(raw.get("area"), 255) or "General"
        family = _period_activity_family(description)
        key = (area, family)
        if key not in groups:
            groups[key] = {
                "area": area,
                "family": family,
                "themes": [],
                "phrases": [],
                "dates": [],
                "source_report_ids": [],
                "statuses": [],
                "equipment_tags": [],
                "occurrence_count": 0,
            }
            group_order.append(key)
        group = groups[key]
        group["occurrence_count"] += 1
        base = _period_activity_base(description) or description
        base_key = _activity_match_text(base)
        if base_key and base_key not in {_activity_match_text(item) for item in group["phrases"]}:
            group["phrases"].append(base)
        for theme in _period_activity_themes(family, description):
            if theme not in group["themes"]:
                group["themes"].append(theme)
        for tag in sorted(_activity_equipment_ids(description)):
            if tag not in group["equipment_tags"]:
                group["equipment_tags"].append(tag)
        report_date = _clean_text(raw.get("date", raw.get("source_date")), 10)
        source_id = _clean_text(raw.get("source_report_id"), 200)
        status = _clean_text(raw.get("status"), 80)
        if report_date and report_date not in group["dates"]:
            group["dates"].append(report_date)
        if source_id and source_id not in group["source_report_ids"]:
            group["source_report_ids"].append(source_id)
        if status and status.casefold() not in {item.casefold() for item in group["statuses"]}:
            group["statuses"].append(status)

    result: list[dict[str, Any]] = []
    for key in group_order:
        group = groups[key]
        themes = sorted(group["themes"], key=lambda item: _theme_rank(group["family"], item))
        phrases = list(group["phrases"])
        if themes:
            detail = _english_join(themes[:5])
        else:
            selected = phrases[:max_phrases_per_group]
            detail = "; ".join(selected)
        if not detail:
            continue

        text = f"{group['family']}: {detail}."
        tags = group["equipment_tags"]
        # A short equipment list improves traceability for one-off work fronts;
        # broad MA-81 groups keep the tag list in metadata to avoid a wall of IDs.
        if 1 <= len(tags) <= 4 and len(group["themes"]) <= 2:
            text += f" Equipment: {', '.join(tags)}."
        statuses = group["statuses"]
        if len(statuses) == 1 and len(phrases) == 1:
            text += f" Status: {statuses[0]}."
        result.append({
            "area": group["area"],
            "workstream": group["family"],
            "text": text,
            "source_dates": group["dates"],
            "source_report_ids": group["source_report_ids"],
            "equipment_tags": tags,
            "representative_activities": phrases[:4],
            "themes": themes,
            "occurrence_count": int(group.get("occurrence_count") or len(phrases)),
            "summary_type": "deterministic_period_group_v3",
        })
    result.sort(key=lambda row: (_area_sort_key(row.get("area")), _workstream_rank(row.get("workstream"))))
    return result

def _field_evidence_terms(activities: Any) -> list[str]:
    text = " ".join(
        _clean_text(row.get("description", row.get("text")), 2_000)
        for row in activities if isinstance(row, dict)
    ).casefold() if isinstance(activities, list) else ""
    catalog = (
        ("calibration", "calibration"),
        ("loop testing", "loop test"),
        ("function testing", "function test"),
        ("continuity checking", "continuity"),
        ("regulators", "regulator"),
        ("tubing", "tubing"),
        ("solenoids", "solenoid"),
        ("selector switches", "selector switch"),
        ("junction boxes", "junction box"),
        ("hoses", "hose"),
        ("flexible conduit", "conduit"),
        ("proximity devices", "proximity"),
    )
    return [label for label, needle in catalog if needle in text]


def _deterministic_engineering_summary(activities: Any) -> str:
    terms = [term for term in _field_evidence_terms(activities) if term in {
        "calibration", "loop testing", "function testing", "continuity checking"
    }]
    if not terms:
        return "No separate engineering deliverable register was supplied in the available Daily Reports."
    return (
        "No separate engineering deliverable register was supplied. Daily Reports record field execution support "
        f"including {', '.join(terms)}; these observations do not establish engineering deliverable progress."
    )


def _deterministic_procurement_summary(activities: Any) -> str:
    materials = [term for term in _field_evidence_terms(activities) if term not in {
        "calibration", "loop testing", "function testing", "continuity checking"
    }]
    if not materials:
        return "No separate procurement, equipment-delivery, or shipment register was supplied in the available Daily Reports."
    return (
        "No separate PO/material register was supplied. Daily Reports record field use, installation, replacement, or repair "
        f"involving {', '.join(materials)}. PO status, outstanding quantities, delivery status, and shipment status cannot be determined from the Daily Reports."
    )


def _progress_summary_sentence(draft: Mapping[str, Any]) -> str:
    progress = draft.get("progress") if isinstance(draft.get("progress"), dict) else {}
    rows = progress.get("rows") if isinstance(progress.get("rows"), list) else []
    candidate = None
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if row.get("is_total") or "total" in _clean_text(row.get("description"), 200).casefold():
            candidate = row
            break
    if candidate is None and rows:
        candidate = rows[-1] if isinstance(rows[-1], dict) else None
    if not isinstance(candidate, dict):
        return ""
    try:
        actual = float(candidate.get("to_date"))
        plan = float(candidate.get("plan"))
    except (TypeError, ValueError):
        return ""
    variance = candidate.get("variance")
    try:
        variance_value = float(variance) if variance is not None else actual - plan
    except (TypeError, ValueError):
        variance_value = actual - plan
    return f"Overall progress is {actual:.2f}% actual versus {plan:.2f}% plan, a variance of {variance_value:+.2f}%."


def _period_source_provenance(draft: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    manifest = draft.get("source_manifest") if isinstance(draft.get("source_manifest"), list) else []
    source_ids: list[str] = []
    dates: list[str] = []
    for row in manifest:
        if not isinstance(row, Mapping):
            continue
        source_id = _clean_text(row.get("report_id"), 200)
        report_date = _clean_text(row.get("report_date", row.get("date")), 10)
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
        if _valid_iso_date_text(report_date) and report_date not in dates:
            dates.append(report_date)
    return source_ids, dates


def _is_no_constraint_text(value: Any) -> bool:
    text = _activity_match_text(value)
    return text in {
        "", "tidak ada", "none", "nil", "n a", "na", "no issue", "no issues",
        "no constraint", "no constraints", "no constraint reported",
        "no constraints reported", "not applicable",
    }


def _real_constraint_rows(rows: Any) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        text = _clean_text(row.get("text", row.get("concern", row.get("description"))), 2_000)
        if not text or _is_no_constraint_text(text):
            continue
        result.append(row)
    return result


def _constraint_tags(rows: Any) -> list[str]:
    tags: list[str] = []
    for row in _real_constraint_rows(rows):
        for tag in sorted(_activity_equipment_ids(row.get("text", row.get("concern", "")))):
            if tag not in tags:
                tags.append(tag)
    return tags


def _constraint_areas(rows: Any) -> list[str]:
    result: list[str] = []
    for row in _real_constraint_rows(rows):
        area = _clean_text(row.get("area"), 255)
        if area and area not in result:
            result.append(area)
    return result


def _constraint_followup_score(constraint: Mapping[str, Any], activity: Mapping[str, Any]) -> float:
    constraint_tags = _activity_equipment_ids(constraint.get("text", constraint.get("concern", "")))
    activity_tags = _activity_equipment_ids(activity.get("description", activity.get("text", "")))
    if not constraint_tags or not activity_tags or not constraint_tags.intersection(activity_tags):
        return -1.0

    constraint_body = _ACTIVITY_EQUIPMENT_ID_RE.sub(" ", _clean_text(constraint.get("text", ""), 2_000))
    activity_body = _ACTIVITY_EQUIPMENT_ID_RE.sub(
        " ", _clean_text(activity.get("description", activity.get("text", "")), 2_000)
    )
    constraint_words = set(_activity_match_text(constraint_body).split())
    activity_words = set(_activity_match_text(activity_body).split())
    generic = {
        "there", "this", "that", "with", "from", "after", "before", "area",
        "not", "no", "is", "are", "and", "the", "for", "to", "of",
    }
    overlap = (constraint_words - generic) & (activity_words - generic)
    action_text = _activity_match_text(activity.get("description", activity.get("text", "")))
    action_bonus = 0.0
    for needle in ("repair", "replace", "install", "reinstall", "function test", "leak test", "fix"):
        if needle in action_text:
            action_bonus += 1.0

    constraint_date = _valid_iso_date_text(constraint.get("date"))
    activity_date = _valid_iso_date_text(activity.get("date", activity.get("source_date")))
    if constraint_date and activity_date and activity_date < constraint_date:
        return -1.0
    same_day_bonus = 2.0 if constraint_date and activity_date == constraint_date else 0.0
    return 10.0 + (3.0 * len(overlap)) + min(action_bonus, 3.0) + same_day_bonus


def _deterministic_concerns(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return formal constraints with only source-backed related follow-up.

    A Daily activity is attached only when it shares an explicit equipment tag
    with the constraint and contains enough lexical/action evidence to be useful.
    This avoids AI-style causal inference while still making Section 5.4 more
    informative before Claude is used.
    """

    constraints = _real_constraint_rows(draft.get("constraints"))
    activities = draft.get("activities") if isinstance(draft.get("activities"), list) else []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in constraints:
        if not isinstance(raw, Mapping):
            continue
        concern_text = _clean_text(raw.get("text", raw.get("concern")), 2_000)
        if not concern_text:
            continue
        area = _clean_text(raw.get("area"), 255)
        identity = (area.casefold(), _activity_match_text(concern_text))
        if identity in seen:
            continue
        seen.add(identity)

        ranked: list[tuple[float, str, Mapping[str, Any]]] = []
        for activity in activities:
            if not isinstance(activity, Mapping):
                continue
            activity_area = _clean_text(activity.get("area"), 255)
            if area and activity_area and area.casefold() != activity_area.casefold():
                continue
            score = _constraint_followup_score(raw, activity)
            if score < 12.0:
                continue
            ranked.append((
                score,
                _valid_iso_date_text(activity.get("date", activity.get("source_date"))) or "9999-99-99",
                activity,
            ))
        ranked.sort(key=lambda item: (-item[0], item[1], _activity_match_text(item[2].get("description", ""))))

        followups: list[str] = []
        followup_sources: list[str] = []
        followup_dates: list[str] = []
        for _score, _date_key, activity in ranked:
            description = _clean_text(activity.get("description", activity.get("text")), 2_000)
            if not description:
                continue
            description_key = _activity_match_text(description)
            if description_key in {_activity_match_text(item) for item in followups}:
                continue
            followups.append(description)
            source_id = _clean_text(activity.get("source_report_id"), 200)
            source_date = _valid_iso_date_text(activity.get("date", activity.get("source_date")))
            if source_id and source_id not in followup_sources:
                followup_sources.append(source_id)
            if source_date and source_date not in followup_dates:
                followup_dates.append(source_date)
            if len(followups) >= 2:
                break

        corrective_action = ""
        if followups:
            corrective_action = "Related source-recorded follow-up: " + "; ".join(followups) + "."

        source_ids = []
        source_id = _clean_text(raw.get("source_report_id"), 200)
        if source_id:
            source_ids.append(source_id)
        for item in followup_sources:
            if item not in source_ids:
                source_ids.append(item)
        source_dates = []
        constraint_date = _valid_iso_date_text(raw.get("date", raw.get("source_date")))
        if constraint_date:
            source_dates.append(constraint_date)
        for item in followup_dates:
            if item not in source_dates:
                source_dates.append(item)

        display_concern = concern_text
        if area and not concern_text.casefold().startswith(area.casefold()):
            display_concern = f"{area} - {concern_text}"
        result.append({
            "area": area,
            "concern": display_concern,
            "corrective_action": corrective_action,
            "source_dates": source_dates,
            "source_report_ids": source_ids,
            "equipment_tags": sorted(_activity_equipment_ids(concern_text)),
            "summary_type": "deterministic_constraint_v2",
        })
    return result


def _deterministic_site_summary(draft: Mapping[str, Any], grouped_activities: list[dict[str, Any]]) -> str:
    areas: list[str] = []
    workstreams: list[str] = []
    for row in grouped_activities:
        if not isinstance(row, Mapping):
            continue
        area = _clean_text(row.get("area"), 255)
        workstream = _clean_text(row.get("workstream"), 255)
        if area and area not in areas:
            areas.append(area)
        if workstream and workstream != "Other Site Work" and workstream not in workstreams:
            workstreams.append(workstream)

    areas.sort(key=_area_sort_key)
    workstreams.sort(key=_workstream_rank)

    sentences: list[str] = []
    if areas:
        sentences.append(f"Site execution covered {_english_join(areas)} during the reporting period.")
    if workstreams:
        sentences.append(f"Principal workstreams were {_english_join(workstreams)}.")

    manpower = draft.get("manpower") if isinstance(draft.get("manpower"), Mapping) else {}
    totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    peak = _format_positive_number(totals.get("peak_headcount"))
    man_hours = _format_positive_number(totals.get("total_man_hours"))
    if peak and man_hours:
        sentences.append(f"Peak daily headcount was {peak} personnel and {man_hours} man-hours were recorded during the period.")
    elif peak:
        sentences.append(f"Peak daily headcount was {peak} personnel.")
    elif man_hours:
        sentences.append(f"Recorded man-hours for the period totaled {man_hours}.")

    constraints = _real_constraint_rows(draft.get("constraints"))
    if constraints:
        areas_with_constraints = _constraint_areas(constraints)
        tags = _constraint_tags(constraints)
        if tags:
            sentences.append(
                f"Formal constraints were recorded"
                + (f" in {_english_join(areas_with_constraints)}" if areas_with_constraints else "")
                + f" for {_english_join(tags)}."
            )
        else:
            sentences.append(
                "Formal constraints were recorded"
                + (f" in {_english_join(areas_with_constraints)}." if areas_with_constraints else ".")
            )
    return " ".join(sentences)


def _executive_area_highlights(grouped: Any) -> list[dict[str, Any]]:
    """Return compact, source-backed area highlights for management narrative.

    The deterministic compiler never invents an activity. It reuses the workstream
    themes already classified from Daily Report wording, and falls back to the
    workstream label only when no specific theme was detected.
    """

    rows = grouped if isinstance(grouped, list) else []
    by_area: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        area = _clean_text(row.get("area"), 255) or "General"
        item = by_area.setdefault(area, {
            "area": area,
            "themes": [],
            "workstreams": [],
            "themes_by_workstream": {},
            "occurrence_count": 0,
            "source_dates": [],
            "source_report_ids": [],
        })
        workstream = _clean_text(row.get("workstream"), 255)
        if workstream and workstream != "Other Site Work" and workstream not in item["workstreams"]:
            item["workstreams"].append(workstream)
        themes = row.get("themes") if isinstance(row.get("themes"), list) else []
        bucket = item["themes_by_workstream"].setdefault(workstream or "Other Site Work", [])
        for theme in themes:
            text = _clean_text(theme, 255)
            if text and text not in item["themes"]:
                item["themes"].append(text)
            if text and text not in bucket:
                bucket.append(text)
        try:
            item["occurrence_count"] += max(1, int(row.get("occurrence_count") or 1))
        except (TypeError, ValueError):
            item["occurrence_count"] += 1
        for key, target in (("source_dates", "source_dates"), ("source_report_ids", "source_report_ids")):
            values = row.get(key) if isinstance(row.get(key), list) else []
            for value in values:
                text = _clean_text(value, 255)
                if text and text not in item[target]:
                    item[target].append(text)

    result = list(by_area.values())
    for item in result:
        item["workstreams"].sort(key=_workstream_rank)
    result.sort(key=lambda item: _area_sort_key(item.get("area")))
    return result


def _area_highlight_detail(item: Mapping[str, Any], *, max_details: int) -> str:
    workstreams = [
        _clean_text(value, 255)
        for value in (item.get("workstreams") if isinstance(item.get("workstreams"), list) else [])
        if _clean_text(value, 255)
    ]
    raw_buckets = item.get("themes_by_workstream") if isinstance(item.get("themes_by_workstream"), Mapping) else {}
    buckets: list[list[str]] = []
    for workstream in workstreams:
        bucket = [
            _clean_text(value, 255)
            for value in (raw_buckets.get(workstream) if isinstance(raw_buckets.get(workstream), list) else [])
            if _clean_text(value, 255)
        ]
        if bucket:
            buckets.append(bucket)

    # Round-robin across workstreams so a detail-rich instrumentation group cannot
    # crowd actuator/testing work out of the Executive Summary.
    details: list[str] = []
    depth = 0
    while buckets and len(details) < max_details:
        added = False
        for bucket in buckets:
            if depth < len(bucket):
                value = bucket[depth]
                if value not in details:
                    details.append(value)
                    added = True
                if len(details) >= max_details:
                    break
        if not added:
            break
        depth += 1

    if not details:
        themes = [
            _clean_text(value, 255)
            for value in (item.get("themes") if isinstance(item.get("themes"), list) else [])
            if _clean_text(value, 255)
        ]
        details = themes[:max_details] or workstreams[:max_details]
    return _english_join(details)


def _missing_management_status_sentence(draft: Mapping[str, Any]) -> str:
    missing: list[str] = []
    if not _progress_summary_sentence(draft):
        missing.append("overall progress percentages")

    safety = draft.get("safety") if isinstance(draft.get("safety"), Mapping) else {}
    incident_keys = (
        "recordable_cases",
        "lost_workdays",
        "lost_time_injuries",
    )
    if safety and all(safety.get(key) in (None, "") for key in incident_keys):
        missing.append("safety incident metrics")
    if not missing:
        return ""
    if len(missing) == 1:
        return missing[0].capitalize() + " were not supplied."
    return f"{missing[0].capitalize()} and {missing[1]} were not supplied."


def _deterministic_executive_summary(draft: Mapping[str, Any], *, report_type: str) -> str:
    """Build a detailed management-facing baseline without requiring Claude.

    V3 keeps the stronger completeness/warning behaviour from Revision 3.1/3.2,
    while restoring the useful area-level detail that made the earlier executive
    summary easier for a project manager to understand.
    """

    period = draft.get("period") if isinstance(draft.get("period"), dict) else {}
    start = _clean_text(period.get("start", period.get("date_from")), 10)
    end = _clean_text(period.get("end", period.get("date_to")), 10)
    grouped = draft.get("activity_summary") if isinstance(draft.get("activity_summary"), list) else []
    if not grouped:
        activities = draft.get("activities") if isinstance(draft.get("activities"), list) else []
        grouped = _summarise_period_activities(activities)

    highlights = _executive_area_highlights(grouped)
    areas = [item["area"] for item in highlights if _clean_text(item.get("area"), 255)]
    workstreams: list[str] = []
    for row in grouped:
        if not isinstance(row, Mapping):
            continue
        workstream = _clean_text(row.get("workstream"), 255)
        if workstream and workstream != "Other Site Work" and workstream not in workstreams:
            workstreams.append(workstream)
    workstreams.sort(key=_workstream_rank)

    report_word = "week" if report_type == "weekly" else "month"
    sentences: list[str] = []
    if grouped:
        period_label = _short_period_label(start, end)
        opening = f"During the reporting {report_word}"
        if period_label:
            opening += f" ({period_label})"
        opening += ", field activities were carried out"
        if areas:
            opening += f" across {_english_join(areas)}"
        sentences.append(opening + ".")

        # Give the busiest recorded area enough detail to be useful, without
        # describing it as the project's most important area. Activity-line volume
        # is used only to choose presentation order, never as a progress metric.
        ranked = sorted(
            highlights,
            key=lambda item: (-int(item.get("occurrence_count") or 0), _area_sort_key(item.get("area"))),
        )
        if ranked:
            focus = ranked[0]
            focus_area = _clean_text(focus.get("area"), 255) or "General"
            focus_detail = _area_highlight_detail(focus, max_details=7)
            if focus_detail:
                sentences.append(f"In {focus_area}, recorded work included {focus_detail}.")

            # Remaining area highlights stay compact. Weekly reports normally have
            # few enough active areas to name all of them; monthly reports may have
            # many, so cap the detailed clauses while the opening still lists the
            # full supported area set.
            remainder = [item for item in ranked[1:] if item.get("area") != focus.get("area")]
            detailed_limit = 4 if report_type == "weekly" else 5
            clauses = []
            for item in remainder[:detailed_limit]:
                area = _clean_text(item.get("area"), 255) or "General"
                detail = _area_highlight_detail(item, max_details=3)
                if detail:
                    clauses.append(f"{area}: {detail}")
            if clauses:
                sentences.append("Other recorded work fronts included " + "; ".join(clauses) + ".")
        elif workstreams:
            sentences.append(f"Major work fronts included {_english_join(workstreams)}.")
    else:
        sentences.append(f"No current-period site activities were supplied for this reporting {report_word}.")

    # Workforce is a deterministic period fact and should remain visible even when
    # progress values are supplied. This avoids the earlier behaviour where adding
    # progress silently removed the useful headcount/man-hour sentence.
    manpower = draft.get("manpower") if isinstance(draft.get("manpower"), Mapping) else {}
    totals = manpower.get("totals") if isinstance(manpower.get("totals"), Mapping) else {}
    peak = _format_positive_number(totals.get("peak_headcount"))
    man_hours = _format_positive_number(totals.get("total_man_hours"))
    if peak and man_hours:
        sentences.append(f"Peak daily headcount was {peak} personnel with {man_hours} man-hours recorded during the period.")
    elif peak:
        sentences.append(f"Peak daily headcount was {peak} personnel.")
    elif man_hours:
        sentences.append(f"Recorded man-hours for the period totaled {man_hours}.")

    progress_sentence = _progress_summary_sentence(draft)
    if progress_sentence:
        sentences.append(progress_sentence)

    constraints = _real_constraint_rows(draft.get("constraints"))
    if constraints:
        tags = _constraint_tags(constraints)
        areas_with_constraints = _constraint_areas(constraints)
        detail = "Formal constraints were reported"
        if areas_with_constraints:
            detail += f" in {_english_join(areas_with_constraints)}"
        if tags:
            detail += f" for {_english_join(tags)}"
        sentences.append(detail + "; details and source-recorded follow-up are shown in Section 5.4.")

    missing_sentence = _missing_management_status_sentence(draft)
    if missing_sentence:
        sentences.append(missing_sentence)

    coverage = draft.get("coverage") if isinstance(draft.get("coverage"), dict) else {}
    missing = [str(item) for item in coverage.get("missing_dates", [])] if isinstance(coverage.get("missing_dates"), list) else []
    if missing:
        sentences.append("Daily Report coverage is partial; available and missing dates are listed in Source Coverage.")
    return " ".join(sentences)

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
    # The issue date is the date the periodic draft was compiled, not the
    # Daily Report cutoff date. It can still be reviewed before final issue.
    draft["issued_date"] = _makassar_issue_date()
    draft["company_name"] = context.get("company_name", "PT. GARUDA PRIMA AKSARA")
    draft["customer"] = context.get("customer", "PT. KERTAS NUSANTARA")
    draft["location"] = context.get("location", "")
    draft["equipment"] = context.get("equipment", "")
    draft["prepared_by"] = context.get("prepared_by", "")
    draft["checked_by"] = context.get("checked_by", "")
    draft["approved_by"] = context.get("approved_by", "")
    draft["revision_description"] = _periodic_revision_description(draft)

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
    draft["warnings"] = _compact_review_warnings(warnings)

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
        "peak_daily_headcount": manpower_totals.get("peak_headcount", 0),
        "headcount_metric": "peak_daily",
        "total_man_hours": manpower_totals.get("total_man_hours", 0),
        # Absence of an incident field in a Daily Report is not evidence of
        # zero incidents. Keep these values explicitly unsupplied until a
        # reviewer enters verified HSE data.
        "recordable_cases": None,
        "lost_workdays": None,
        "lost_time_injuries": None,
        "severity_rate": None,
        "average_day_away": None,
    })
    # Build a complete management-facing baseline before any AI call. Claude is
    # optional: the deterministic compiler owns classification, grouping, source
    # provenance, workforce numbers and constraint follow-up selection.
    activities_for_summary = draft.get("activities") if isinstance(draft.get("activities"), list) else []
    grouped_activities = _summarise_period_activities(activities_for_summary)
    draft["activity_summary"] = copy.deepcopy(grouped_activities)

    engineering_summary = _deterministic_engineering_summary(activities_for_summary)
    procurement_summary = _deterministic_procurement_summary(activities_for_summary)
    draft.setdefault("engineering", {
        "summary": engineering_summary,
        "source_meta": {"source_type": "derived_from_daily_reports", "scope": "field_evidence_only"},
    })
    draft.setdefault("procurement", {
        "summary": procurement_summary,
        "source_meta": {"source_type": "derived_from_daily_reports", "scope": "field_evidence_only"},
    })

    site = draft.get("site") if isinstance(draft.get("site"), dict) else {}
    if not site.get("this_month_activities"):
        site["this_month_activities"] = grouped_activities or copy.deepcopy(activities_for_summary)

    # Activity Tomorrow is deliberately NOT promoted to next-week/next-month.
    # Period look-ahead is shown only when the source explicitly supplies it.
    explicit_lookahead = (
        draft.get("planned_next_week", []) if kind == "weekly"
        else draft.get("planned_next_month", [])
    )
    cleaned_lookahead: list[Any] = []
    for row in explicit_lookahead if isinstance(explicit_lookahead, list) else []:
        if isinstance(row, dict):
            description = _clean_text(row.get("description", row.get("text")), 2_000)
            if description:
                cleaned_lookahead.append({
                    "area": _clean_text(row.get("area"), 255) or "General",
                    "description": description,
                    "source_date": _clean_text(row.get("source_date"), 10),
                    "source_report_id": _clean_text(row.get("source_report_id"), 200),
                    "source_path": _clean_text(row.get("source_path"), 500),
                })
        else:
            text_value = _clean_text(row, 2_000)
            if text_value:
                cleaned_lookahead.append(text_value)
    site["next_month_activities"] = cleaned_lookahead
    site["tomorrow_activities"] = copy.deepcopy(draft.get("tomorrow_activities", []))

    deterministic_concerns = _deterministic_concerns(draft)
    if not site.get("concerns"):
        site["concerns"] = deterministic_concerns or draft.get("concerns", draft.get("constraints", []))
    if isinstance(draft.get("constraint_reporting"), dict):
        site["constraint_reporting"] = copy.deepcopy(draft["constraint_reporting"])
    if isinstance(draft.get("weather"), list):
        site["weather"] = copy.deepcopy(draft["weather"])

    baseline_site_summary = _deterministic_site_summary(draft, grouped_activities)
    if not _clean_text(site.get("summary"), 4_000):
        site["summary"] = baseline_site_summary

    # Generic aliases let the renderer and review UI use period-neutral labels
    # while legacy monthly keys continue to support archived drafts.
    site["current_period_activities"] = site.get("this_month_activities", [])
    site["this_period_activities"] = site["current_period_activities"]
    site["next_period_activities"] = site.get("next_month_activities", [])
    if kind == "weekly":
        site["this_week_activities"] = site["current_period_activities"]
        site["next_week_activities"] = site["next_period_activities"]
    draft["site"] = site

    baseline_executive = _deterministic_executive_summary(draft, report_type=kind)
    if not draft.get("executive_summary"):
        draft["executive_summary"] = baseline_executive

    # Persist the deterministic narrative layer separately from raw source rows.
    # ai_summary.py can send this compact baseline to Claude instead of asking the
    # model to rediscover structure from hundreds of Daily activities.
    source_ids, source_dates = _period_source_provenance(draft)
    draft["deterministic_summary"] = {
        "version": _DETERMINISTIC_SUMMARY_VERSION,
        "source_type": "deterministic_compiler",
        "executive_summary": {
            "text": baseline_executive,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "site_summary": {
            "text": baseline_site_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "engineering_summary": {
            "text": engineering_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "procurement_summary": {
            "text": procurement_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "current_activities": copy.deepcopy(grouped_activities),
        "concerns": copy.deepcopy(deterministic_concerns),
        "lookahead": copy.deepcopy(cleaned_lookahead),
    }
    draft["narrative_mode"] = "deterministic"
    draft["narrative_engine_version"] = _DETERMINISTIC_SUMMARY_VERSION
    return draft


def _activity_summary_signature(value: Any) -> list[tuple[str, str, str]]:
    rows = _clean_activity_rows(value, maximum_items=500)
    return [
        (
            _activity_match_text(row.get("area")),
            _activity_match_text(row.get("text")),
            _activity_match_text(row.get("status")),
        )
        for row in rows
    ]


def _concern_summary_signature(value: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for raw in value if isinstance(value, list) else []:
        if isinstance(raw, Mapping):
            concern = _clean_text(raw.get("concern", raw.get("text", raw.get("description"))), 2_000)
            action = _clean_text(raw.get("corrective_action", raw.get("action", "")), 2_000)
        else:
            concern = _clean_text(raw, 2_000)
            action = ""
        if concern or action:
            result.append((_activity_match_text(concern), _activity_match_text(action)))
    return result


def _refresh_deterministic_summary(draft: dict[str, Any]) -> dict[str, Any]:
    """Refresh deterministic narrative after reviewed data changes.

    Timesheet decisions, progress edits and structured sources can legitimately
    change factual totals after the initial compile.  Rebuild the deterministic
    baseline, but update client-facing fields only while they still match the
    previous baseline.  Manual edits and accepted AI wording therefore survive.
    """

    if not isinstance(draft, dict):
        return draft
    kind = _draft_report_type(draft)
    previous = draft.get("deterministic_summary") if isinstance(draft.get("deterministic_summary"), Mapping) else {}

    old_exec = _clean_text(
        previous.get("executive_summary", {}).get("text")
        if isinstance(previous.get("executive_summary"), Mapping) else "",
        4_000,
    )
    old_site_summary = _clean_text(
        previous.get("site_summary", {}).get("text")
        if isinstance(previous.get("site_summary"), Mapping) else "",
        4_000,
    )
    old_engineering = _clean_text(
        previous.get("engineering_summary", {}).get("text")
        if isinstance(previous.get("engineering_summary"), Mapping) else "",
        4_000,
    )
    old_procurement = _clean_text(
        previous.get("procurement_summary", {}).get("text")
        if isinstance(previous.get("procurement_summary"), Mapping) else "",
        4_000,
    )
    old_activities = previous.get("current_activities") if isinstance(previous.get("current_activities"), list) else []
    old_concerns = previous.get("concerns") if isinstance(previous.get("concerns"), list) else []

    activities = draft.get("activities") if isinstance(draft.get("activities"), list) else []
    grouped = _summarise_period_activities(activities)
    draft["activity_summary"] = copy.deepcopy(grouped)
    engineering_summary = _deterministic_engineering_summary(activities)
    procurement_summary = _deterministic_procurement_summary(activities)
    concerns = _deterministic_concerns(draft)

    site = draft.get("site") if isinstance(draft.get("site"), dict) else {}
    new_site_summary = _deterministic_site_summary(draft, grouped)
    new_exec = _deterministic_executive_summary(draft, report_type=kind)

    current_exec = _clean_text(draft.get("executive_summary"), 4_000)
    if not current_exec or (old_exec and current_exec == old_exec):
        draft["executive_summary"] = new_exec

    current_site_summary = _clean_text(site.get("summary"), 4_000)
    if not current_site_summary or (old_site_summary and current_site_summary == old_site_summary):
        site["summary"] = new_site_summary

    current_activities = site.get(
        "current_period_activities",
        site.get("this_week_activities", site.get("this_month_activities", [])),
    )
    if (
        not _activity_summary_signature(current_activities)
        or _activity_summary_signature(current_activities) == _activity_summary_signature(old_activities)
    ):
        site["this_month_activities"] = copy.deepcopy(grouped)
        site["current_period_activities"] = copy.deepcopy(grouped)
        site["this_period_activities"] = copy.deepcopy(grouped)
        if kind == "weekly":
            site["this_week_activities"] = copy.deepcopy(grouped)

    current_concerns = site.get("concerns") if isinstance(site.get("concerns"), list) else []
    if (
        not _concern_summary_signature(current_concerns)
        or _concern_summary_signature(current_concerns) == _concern_summary_signature(old_concerns)
    ):
        site["concerns"] = copy.deepcopy(concerns)

    for section_key, old_text, new_text in (
        ("engineering", old_engineering, engineering_summary),
        ("procurement", old_procurement, procurement_summary),
    ):
        section = draft.get(section_key) if isinstance(draft.get(section_key), dict) else {}
        current_text = _clean_text(section.get("summary"), 4_000)
        source_meta = section.get("source_meta") if isinstance(section.get("source_meta"), Mapping) else {}
        manually_sourced = str(source_meta.get("source_type") or "") == "manual"
        if not manually_sourced and (not current_text or (old_text and current_text == old_text)):
            section["summary"] = new_text
            section.setdefault("source_meta", {
                "source_type": "derived_from_daily_reports",
                "scope": "field_evidence_only",
            })
        draft[section_key] = section

    draft["site"] = site
    source_ids, source_dates = _period_source_provenance(draft)
    lookahead = site.get("next_period_activities", site.get("next_month_activities", []))
    draft["deterministic_summary"] = {
        "version": _DETERMINISTIC_SUMMARY_VERSION,
        "source_type": "deterministic_compiler",
        "executive_summary": {
            "text": new_exec,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "site_summary": {
            "text": new_site_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "engineering_summary": {
            "text": engineering_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "procurement_summary": {
            "text": procurement_summary,
            "source_report_ids": source_ids,
            "source_dates": source_dates,
        },
        "current_activities": copy.deepcopy(grouped),
        "concerns": copy.deepcopy(concerns),
        "lookahead": copy.deepcopy(lookahead if isinstance(lookahead, list) else []),
    }
    draft["narrative_engine_version"] = _DETERMINISTIC_SUMMARY_VERSION
    if str(draft.get("narrative_mode") or "").strip() not in {"ai_enhanced"}:
        draft["narrative_mode"] = "deterministic"
    return draft


def _source_manifest(records: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    manifest = []
    seen: set[str] = set()
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        source_identity = (
            record.get("source_identity")
            if isinstance(record.get("source_identity"), dict)
            else {}
        )
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
            "source_project_no": source_identity.get("project_no", ""),
            "source_project_title": source_identity.get("project_title", ""),
            "source_document_no": source_identity.get("document_no", ""),
        }
        identity = str(row["report_id"] or row["sha256"] or f"{row['report_date']}:{row['filename']}")
        if identity in seen:
            continue
        seen.add(identity)
        manifest.append(row)
    return manifest



_DAILY_REPORT_DOCUMENT_NO_RE = re.compile(r"(?:^|[-_/])DAR(?:$|[-_/])", re.IGNORECASE)


def _looks_like_daily_report_document_no(value: Any) -> bool:
    return bool(_DAILY_REPORT_DOCUMENT_NO_RE.search(_clean_text(value, 250)))


def _project_title_alias_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value, 500)).casefold().replace("&", " and ")
    tokens = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split()).split()
    return " ".join("service" if token == "services" else token for token in tokens)


def _high_confidence_selected_title_match(
    imported: Mapping[str, Any],
    project_no: str,
    parsed_project_title: str,
    selected_project_title: str,
) -> bool:
    if (
        parsed_project_title
        and selected_project_title
        and _project_title_alias_key(parsed_project_title) == _project_title_alias_key(selected_project_title)
    ):
        return True
    extraction = imported.get("extraction") if isinstance(imported.get("extraction"), Mapping) else {}
    match = extraction.get("project_match") if isinstance(extraction.get("project_match"), Mapping) else {}
    candidate = match.get("candidate") if isinstance(match.get("candidate"), Mapping) else {}
    return bool(
        match.get("high_confidence_suggestion")
        and _clean_text(candidate.get("project_no"), 250).casefold() == _clean_text(project_no, 250).casefold()
    )


def _compact_review_warnings(values: Any) -> list[str]:
    """Collapse repetitive low-risk photo/import notices without hiding blockers."""

    rows = values if isinstance(values, list) else []
    result: list[str] = []
    seen: set[str] = set()
    counters = {
        "ignored_images": 0,
        "duplicate_photos": 0,
        "template_artwork": 0,
        "signature_artwork": 0,
    }
    patterns = (
        ("ignored_images", re.compile(r":\s*(\d+)\s+small, oversized, or unsupported image occurrence\(s\) were ignored\.$", re.I)),
        ("duplicate_photos", re.compile(r":\s*(\d+)\s+duplicate photo occurrence\(s\) were removed\.$", re.I)),
        ("template_artwork", re.compile(r":\s*(\d+)\s+recurring header/logo image occurrence\(s\) were excluded\.$", re.I)),
        ("signature_artwork", re.compile(r":\s*(\d+)\s+signature/line-art image occurrence\(s\) were excluded from Photo Documentation\.$", re.I)),
    )
    for raw in rows:
        text = _warning_text(raw)
        if not text:
            continue
        matched = False
        for key, pattern in patterns:
            match = pattern.search(text)
            if match:
                counters[key] += int(match.group(1))
                matched = True
                break
        if matched:
            continue
        if text not in seen:
            seen.add(text)
            result.append(text)

    summaries = (
        ("ignored_images", "Photo processing: {n} small/oversized/unsupported non-report image occurrence(s) were ignored."),
        ("duplicate_photos", "Photo processing: {n} exact duplicate photo occurrence(s) were removed."),
        ("template_artwork", "Photo processing: {n} recurring header/logo image occurrence(s) were excluded."),
        ("signature_artwork", "Photo processing: {n} signature/line-art image occurrence(s) were excluded."),
    )
    for key, template in summaries:
        if counters[key]:
            result.append(template.format(n=counters[key]))
    return result


def _periodic_revision_description(report: Mapping[str, Any]) -> str:
    """Create the client-facing DESCRIPTION text used in the blue revision table."""

    kind = str(report.get("report_type") or "monthly").strip().lower()
    period = report.get("period") if isinstance(report.get("period"), Mapping) else {}
    start_text = _clean_text(period.get("start"), 10)
    end_text = _clean_text(period.get("end"), 10)
    try:
        start = datetime.strptime(start_text, "%Y-%m-%d").date()
        end = datetime.strptime(end_text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return f"{_report_name(kind)} Progress Report"

    if kind == "monthly":
        if start.year == end.year and start.month == end.month:
            return f"{start.strftime('%B %Y')} Monthly Progress Report"
        return f"{start.strftime('%B %Y')} - {end.strftime('%B %Y')} Monthly Progress Report"

    if start.year == end.year and start.month == end.month:
        return f"{start.day:02d}-{end.day:02d} {start.strftime('%B %Y')} Weekly Progress Report"
    return f"{start.strftime('%d %B %Y')} - {end.strftime('%d %B %Y')} Weekly Progress Report"



def _normalised_daily_table_text(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _clean_text(value, 4_000).casefold()).split())


def _sanitize_current_split_uploaded_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Remove deterministic table-header artifacts from current Daily PDFs.

    ``importer.py`` intentionally remains backward-compatible with older Daily
    templates.  Some current split-layout PDFs flatten the Remarks table into
    text such as ``Area Remarks; MA-42 ...`` and may expose the table header as a
    synthetic ``Imported PDF`` area.  Weekly/Monthly compilation can safely
    remove only those exact structural artifacts here, without altering genuine
    free-text remarks or any legacy-layout payload.
    """

    if not isinstance(data, dict):
        return data
    if str(data.get("layout_profile") or "").strip().lower() != "current_split":
        return data

    raw_areas = data.get("areas")
    if not isinstance(raw_areas, list):
        return data

    cleaned_areas: list[dict[str, Any]] = []
    for area in raw_areas:
        if not isinstance(area, dict):
            continue
        area_id = _clean_text(area.get("id"), 255)
        remark_key = _normalised_daily_table_text(area.get("remarks"))
        structural_empty = not any(
            area.get(key)
            for key in (
                "activities_today",
                "activities_tomorrow",
                "manpower",
                "indirect_manpower",
                "constraints",
                "photos",
            )
        )
        if (
            area_id.casefold() == "imported pdf"
            and structural_empty
            and remark_key in {"area remarks", "area remark", "remarks", "remark"}
        ):
            continue
        cleaned_areas.append(area)
    data["areas"] = cleaned_areas

    raw_global = _clean_text(data.get("global_remarks"), 20_000)
    if raw_global:
        expected_area_rows: set[str] = set()
        known_area_prefixes: set[str] = set()
        for area in cleaned_areas:
            area_id = _clean_text(area.get("id"), 255)
            if not area_id:
                continue
            area_key = _normalised_daily_table_text(area_id)
            if area_key:
                known_area_prefixes.add(area_key)
            remark = _clean_text(area.get("remarks"), 4_000)
            expected_area_rows.add(
                _normalised_daily_table_text(f"{area_id} {remark if remark else '-'}")
            )

        keep: list[str] = []
        for part in (item.strip() for item in raw_global.split(";")):
            if not part:
                continue
            key = _normalised_daily_table_text(part)
            if key in {"area remarks", "area remark", "remarks", "remark"}:
                continue
            if key in expected_area_rows:
                continue
            # A flattened empty table row can lose the trailing dash. Remove it
            # only when the corresponding area already exists in structured data.
            if key in known_area_prefixes:
                continue
            keep.append(part)
        data["global_remarks"] = "; ".join(dict.fromkeys(keep))

    return data


def _record_from_uploaded_pdf(
    imported: dict[str, Any],
    *,
    filename: str,
    username: str,
    project_no: str,
    project_title: str,
    date_from: str,
    date_to: str,
    report_type: str = "monthly",
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    data = copy.deepcopy(imported.get("data")) if isinstance(imported.get("data"), dict) else {}
    data = _sanitize_current_split_uploaded_payload(data)
    parsed_project = _clean_text(data.get("project_no"), 250)
    parsed_project_title = _clean_text(data.get("project_title"), 500)
    daily_document_no = parsed_project if _looks_like_daily_report_document_no(parsed_project) else ""
    title_matches_selected = _high_confidence_selected_title_match(
        imported, project_no, parsed_project_title, project_title
    )
    if (
        parsed_project
        and not daily_document_no
        and parsed_project.casefold() != project_no.casefold()
    ):
        warnings.append(
            f"{filename}: project number {parsed_project} differs from selected project {project_no}; "
            "file included for project confirmation."
        )
    if not parsed_project:
        warnings.append(f"{filename}: project assigned from the selected project.")

    # Aggregation uses the selected project as its working identity. Keep the
    # parser's original values separately so the review step can ask whether
    # variants should be merged or kept apart without losing evidence.
    data["project_no"] = project_no
    if project_title:
        data["project_title"] = project_title

    report_date, date_method = _imported_report_date(imported, filename)
    if not report_date:
        warnings.append(f"{filename}: report date was not detected; file requires manual import and was skipped.")
        return None, warnings
    data["date"] = report_date
    if date_method == "filename_iso_fallback":
        warnings.append(
            f"{filename}: report date {report_date} was recovered from the filename; confirm it in Source Data Validation."
        )
    if _report_type(report_type) != "weekly" and not (date_from <= report_date <= date_to):
        warnings.append(f"{filename}: date {report_date} is outside the selected period; file skipped.")
        return None, warnings

    source = imported.get("source") if isinstance(imported.get("source"), dict) else {}
    report_id = f"pdf-{source.get('sha256') or uuid.uuid4().hex}"
    record = {
        "record_type": "final_daily_report",
        "report_id": report_id,
        "revision": int(imported.get("revision") or 1),
        "username": username,
        "date": report_date,
        "project_no": project_no,
        "project_title": project_title,
        "generated_at": "",
        "payload": data,
        "source": source,
        "confidence": imported.get("confidence", {}),
        "import_status": imported.get("status", "needs_review"),
        "review_required": True,
        "source_identity": {
            # ``*-DAR`` values are document-control numbers, not the Vendor
            # Project No. used by the periodic report. Preserve both identities.
            "project_no": project_no if daily_document_no and title_matches_selected else parsed_project,
            "project_title": project_title if daily_document_no and title_matches_selected else parsed_project_title,
            "reported_project_no": parsed_project,
            "reported_project_title": parsed_project_title,
            "document_no": daily_document_no,
        },
    }
    if imported.get("status") != "ready":
        warnings.append(f"{filename}: parser result needs manual review.")
    for warning in imported.get("warnings", []):
        if (
            isinstance(warning, Mapping)
            and str(warning.get("code") or "") == "project_title_fuzzy_suggestion"
            and daily_document_no
            and title_matches_selected
        ):
            # A high-confidence title match plus an explicit Daily Report
            # document number is expected and need not be repeated for every day.
            continue
        warnings.append(f"{filename}: {_warning_text(warning)}")
    return record, warnings


def _bound_record_photo_candidates(
    records: list[dict[str, Any]],
    *,
    limits=None,
) -> list[str]:
    """Apply report-specific count/byte bounds and exact hash deduplication."""

    limits = limits or DEFAULT_PHOTO_LIMITS
    seen: set[str] = set()
    total_bytes = 0
    retained = 0
    removed_duplicates = 0
    removed_for_limit = 0
    for record in records:
        raw = record.get("_photo_candidates")
        bounded: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "")
            try:
                size_bytes = max(0, int(item.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size_bytes = 0
            if not is_asset_id(asset_id) or size_bytes > limits.max_asset_bytes:
                continue
            if asset_id in seen:
                removed_duplicates += 1
                # Keep the reference on every source record so choosing a
                # later duplicate record during Source Validation does not
                # accidentally remove its photo. Output collection dedupes it.
                bounded.append(copy.deepcopy(item))
                continue
            if (
                retained >= limits.max_images_per_draft
                or total_bytes + size_bytes > limits.max_total_asset_bytes_per_draft
            ):
                removed_for_limit += 1
                continue
            seen.add(asset_id)
            total_bytes += size_bytes
            retained += 1
            bounded.append(copy.deepcopy(item))
        record["_photo_candidates"] = bounded

    warnings: list[str] = []
    if removed_duplicates:
        warnings.append(
            f"{removed_duplicates} duplicate photo(s) across Daily Reports were removed."
        )
    if removed_for_limit:
        warnings.append(
            f"{removed_for_limit} photo(s) exceeded the {limits.max_images_per_draft}-photo "
            "or draft asset byte limit and were excluded."
        )
    return warnings


def _photo_references_for_records(
    records: list[dict[str, Any]],
    *,
    previous: Any = None,
    limits=None,
) -> list[dict[str, Any]]:
    """Return references belonging only to the selected source records."""

    limits = limits or DEFAULT_PHOTO_LIMITS
    selected_ids = {str(record.get("report_id") or "") for record in records}
    prior_by_id: dict[str, dict[str, Any]] = {}
    prior_order: list[str] = []
    if isinstance(previous, list):
        for item in previous:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "")
            if is_asset_id(asset_id) and asset_id not in prior_by_id:
                prior_by_id[asset_id] = item
                prior_order.append(asset_id)

    available: dict[str, dict[str, Any]] = {}
    discovered_order: list[str] = []
    for record in records:
        report_id = str(record.get("report_id") or "")
        if report_id not in selected_ids:
            continue
        candidates = record.get("_photo_candidates")
        for item in candidates if isinstance(candidates, list) else []:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "")
            if not is_asset_id(asset_id) or asset_id in available:
                continue
            try:
                page_number = int(item.get("page") or 0)
            except (TypeError, ValueError):
                page_number = 0
            reference = {
                "schema_version": "periodic-photo/1",
                "asset_id": asset_id,
                "source_report_id": report_id,
                "source": _clean_text(item.get("source"), 255),
                "width": max(1, int(item.get("width") or 1)),
                "height": max(1, int(item.get("height") or 1)),
                "size_bytes": max(0, int(item.get("size_bytes") or 0)),
                "caption": _clean_text(item.get("caption"), 500),
            }
            if page_number > 0:
                reference["page"] = page_number
            for metadata_key, maximum_length in (
                ("source_date", 10),
                ("source_area", 255),
                ("activity_id", 100),
                ("activity_description", 500),
                ("activity_status", 80),
                ("source_type", 80),
                ("photo_match_method", 80),
                ("context_type", 40),
            ):
                metadata_value = _clean_text(item.get(metadata_key), maximum_length)
                if metadata_value:
                    reference[metadata_key] = metadata_value
            previous_item = prior_by_id.get(asset_id)
            if previous_item is not None:
                previous_caption = _clean_text(previous_item.get("caption"), 500)
                if previous_caption:
                    reference["caption"] = previous_caption
            available[asset_id] = reference
            discovered_order.append(asset_id)

    ordered_ids = [asset_id for asset_id in prior_order if asset_id in available]
    ordered_ids.extend(asset_id for asset_id in discovered_order if asset_id not in ordered_ids)
    result = [available[asset_id] for asset_id in ordered_ids]
    for index, item in enumerate(result):
        item["order"] = index
    return result[: limits.max_images_per_draft]


def _all_photo_references(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        candidates = record.get("_photo_candidates")
        if isinstance(candidates, list):
            result.extend(item for item in candidates if isinstance(item, dict))
    return result


def _provisional_project_records(
    records: list[dict[str, Any]],
    validation: dict[str, Any],
    *,
    project_no: str,
    project_title: str,
) -> list[dict[str, Any]]:
    """Provisionally merge uploaded project identities for the review preview.

    Source Data Validation remains unapplied/unconfirmed, so the reviewer must
    still decide Merge vs Keep separate before Final issue.  Including all
    uploaded identities in the provisional preview prevents a project-number
    mismatch from being misreported as a *missing date* when the Daily Report
    for that date was actually uploaded and parsed successfully.
    """

    groups = validation.get("project_groups") if isinstance(validation, dict) else []
    resolutions = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or not group.get("key"):
            continue
        resolutions.append({
            "group_key": group["key"],
            "decision": "merge",
        })
    try:
        included, _ = resolve_project_records(
            records,
            validation,
            project_no=project_no,
            project_title=project_title,
            resolutions=resolutions,
        )
    except ValueError as exc:
        if "At least one project group" in str(exc):
            return []
        raise
    return included



def _source_validation_payload(review: Any) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise ValueError("Invalid Source Data Validation.")
    project_no = _clean_text(review.get("project_no"), 250)
    project_title = _clean_text(review.get("project_title"), 500)
    notes = _clean_text(review.get("notes"), 4_000)
    resolutions = review.get("project_resolutions")
    if not isinstance(resolutions, list):
        resolutions = []
    duplicate_resolutions = review.get("duplicate_resolutions")
    if not isinstance(duplicate_resolutions, list):
        duplicate_resolutions = []
    return {
        "confirmed": bool(review.get("confirmed")),
        "project_no": project_no,
        "project_title": project_title,
        "notes": notes,
        "project_resolutions": resolutions,
        "duplicate_resolutions": duplicate_resolutions,
    }


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



def _manual_source_meta(review: Mapping[str, Any], section: str, actor: str) -> dict[str, Any]:
    refs = review.get("source_references") if isinstance(review.get("source_references"), dict) else {}
    reference = _clean_text(refs.get(section), 1_000) if isinstance(refs, dict) else ""
    return {
        "source_type": "manual",
        "entered_by": _clean_text(actor, 200),
        "entered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reference": reference,
    }


def _apply_review(draft: dict[str, Any], review: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
    value = copy.deepcopy(draft)
    kind = _report_type(value.get("report_type") or "monthly")
    mode = _normalise_report_mode(kind, review.get("report_mode") or value.get("report_mode"))
    period = value.get("period") if isinstance(value.get("period"), dict) else {}
    _parse_period(period.get("start"), period.get("end"), kind, mode)
    stored_validation = value.get("source_validation")
    if isinstance(stored_validation, dict):
        if not stored_validation.get("applied") or not stored_validation.get("confirmed"):
            raise ValueError("Apply Source Data Validation before Preview or Generate.")
        incoming_raw = review.get("source_validation")
        if incoming_raw is not None:
            incoming_validation = _source_validation_payload(incoming_raw)
            if not incoming_validation["confirmed"]:
                raise ValueError("Confirm Source Data Validation before Preview or Generate.")
            if (
                incoming_validation["project_no"] != str(value.get("project_no") or "")
                or incoming_validation["project_title"] != str(value.get("project_title") or "")
            ):
                raise ValueError("Project identity changed. Apply Source Data Validation again.")
            stored_decisions = {
                str(group.get("key") or ""): str(group.get("decision") or "")
                for group in stored_validation.get("project_groups", [])
                if isinstance(group, dict)
            }
            incoming_decisions = {
                str(row.get("group_key") or ""): str(row.get("decision") or "")
                for row in incoming_validation["project_resolutions"]
                if isinstance(row, dict)
            }
            if incoming_decisions != stored_decisions:
                raise ValueError("Project decisions changed. Apply Source Data Validation again.")
            stored_duplicates = {
                str(group.get("key") or ""): str(group.get("selected_record_id") or "")
                for group in stored_validation.get("duplicate_groups", [])
                if isinstance(group, dict)
            }
            incoming_duplicates = {
                str(row.get("group_key") or ""): str(row.get("selected_record_id") or "")
                for row in incoming_validation["duplicate_resolutions"]
                if isinstance(row, dict)
            }
            if incoming_duplicates != stored_duplicates:
                raise ValueError("Duplicate source choices changed. Apply Source Data Validation again.")
            stored_validation["notes"] = incoming_validation["notes"]
        value["source_validation"] = stored_validation
    if has_pending_workforce_review(value):
        raise ValueError("Apply or keep every timesheet/overtime preview before Preview or Generate.")
    ai_summary = value.get("ai_summary")
    if isinstance(ai_summary, dict) and ai_summary.get("status") == "suggested":
        raise ValueError("Accept or reject the pending AI narrative suggestions before Preview or Generate.")
    value["report_type"] = kind
    value["report_title"] = f"{_report_name(kind)} Progress Report"
    value["report_mode"] = mode
    value["status"] = value["report_mode"]
    value["executive_summary"] = _clean_text(review.get("executive_summary", value.get("executive_summary")))
    value["progress"] = _normalize_progress(review.get("progress", value.get("progress", {})))

    current_safety = value.get("safety") if isinstance(value.get("safety"), dict) else {}
    safety_review = review.get("safety") if isinstance(review.get("safety"), dict) else {}
    safety = {
        "total_manpower": _optional_non_negative(
            safety_review.get("total_manpower", current_safety.get("total_manpower")),
            integer=True,
        ),
        "total_man_hours": _optional_non_negative(
            safety_review.get("total_man_hours", current_safety.get("total_man_hours")),
        ),
        "recordable_cases": _optional_non_negative(
            safety_review.get("recordable_cases", current_safety.get("recordable_cases")),
            integer=True,
        ),
        "lost_workdays": _optional_non_negative(
            safety_review.get("lost_workdays", current_safety.get("lost_workdays")),
            integer=True,
        ),
        "lost_time_injuries": _optional_non_negative(
            safety_review.get("lost_time_injuries", current_safety.get("lost_time_injuries")),
            integer=True,
        ),
    }
    workforce = value.get("workforce_validation")
    effective = workforce.get("effective") if isinstance(workforce, dict) and isinstance(workforce.get("effective"), dict) else {}
    if effective.get("source") == "timesheet":
        # Reviewed workbook facts are deterministic and cannot be changed by
        # an editable text/number field or by an AI suggestion.  Apply these
        # values before calculating rates so the denominator stays coherent.
        safety["total_manpower"] = int(_number(effective.get("peak_headcount")))
        safety["total_man_hours"] = max(0.0, _number(effective.get("total_man_hours")))
    lost_days = safety["lost_workdays"]
    man_hours = safety["total_man_hours"]
    injuries = safety["lost_time_injuries"]
    safety["severity_rate"] = (
        round(float(lost_days) * 1_000_000 / float(man_hours), 2)
        if lost_days is not None and man_hours not in (None, 0)
        else None
    )
    safety["average_day_away"] = (
        round(float(lost_days) / float(injuries), 2)
        if lost_days is not None and injuries not in (None, 0)
        else None
    )
    # ``total_manpower`` is retained for review-API compatibility, but the
    # value derived from Daily/Timesheet data is specifically the peak daily HC.
    safety["peak_daily_headcount"] = safety.get("total_manpower")
    safety["headcount_metric"] = "peak_daily"
    safety_changed = any(
        key in safety_review and safety.get(key) != current_safety.get(key)
        for key in ("total_manpower", "total_man_hours", "recordable_cases", "lost_workdays", "lost_time_injuries")
    )
    if safety_changed:
        safety["source_meta"] = _manual_source_meta(review, "safety", actor)
    elif isinstance(current_safety.get("source_meta"), dict):
        safety["source_meta"] = copy.deepcopy(current_safety["source_meta"])
    value["safety"] = safety

    for key in ("engineering", "procurement"):
        current = value.get(key) if isinstance(value.get(key), dict) else {}
        incoming = review.get(key) if isinstance(review.get(key), dict) else {}
        old_summary = _clean_text(current.get("summary"))
        new_summary = _clean_text(incoming.get("summary", current.get("summary")))
        current["summary"] = new_summary
        if "summary" in incoming and new_summary != old_summary:
            current["source_meta"] = _manual_source_meta(review, key, actor)
        value[key] = current

    current_site = value.get("site") if isinstance(value.get("site"), dict) else {}
    incoming_site = review.get("site") if isinstance(review.get("site"), dict) else {}
    if "summary" in incoming_site:
        current_site["summary"] = _clean_text(incoming_site.get("summary"), 4_000)
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
    # Preserve Area labels for deterministic/AI activity groups through the
    # Preview/Generate review round-trip. Older plain-string rows remain valid
    # and are assigned to the generic Site area.
    current_site["this_month_activities"] = _clean_activity_rows(current_activities)
    current_site["next_month_activities"] = _clean_activity_rows(next_activities)
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


def _require_applied_source_validation(draft: dict[str, Any]) -> None:
    validation = draft.get("source_validation")
    if not isinstance(validation, dict) or not validation.get("applied") or not validation.get("confirmed"):
        raise ValueError("Apply Source Data Validation before reviewing workforce data or AI suggestions.")


def _workbook_uploads() -> list[tuple[str, bytes]]:
    if request.content_length is not None and request.content_length > _MAX_WORKBOOK_REQUEST_BYTES:
        raise ValueError("The workbook upload request exceeds 48 MB.")
    files = request.files.getlist("files")
    if not files:
        raise ValueError("Choose at least one .xlsx workbook.")
    if len(files) > _MAX_WORKBOOK_FILES:
        raise ValueError(f"Choose no more than {_MAX_WORKBOOK_FILES} workbooks at once.")
    result: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        filename = os.path.basename(str(upload.filename or ""))
        if not filename.lower().endswith(".xlsx"):
            raise ValueError(f"{filename or 'Upload'} is not an .xlsx workbook.")
        payload = upload.stream.read(_MAX_WORKBOOK_FILE_BYTES + 1)
        if len(payload) > _MAX_WORKBOOK_FILE_BYTES:
            raise ValueError(f"{filename} exceeds the 16 MB workbook limit.")
        if not payload:
            raise ValueError(f"{filename} is empty.")
        total += len(payload)
        if total > _MAX_WORKBOOK_REQUEST_BYTES:
            raise ValueError("The combined workbook upload exceeds 48 MB.")
        result.append((filename, payload))
    return result


def _ai_admin_only() -> bool:
    return str(os.environ.get("ANTHROPIC_AI_ADMIN_ONLY", "true")).strip().lower() not in {
        "0", "false", "no", "off",
    }


def _claim_text(value: Any) -> str:
    if isinstance(value, dict):
        return _clean_text(value.get("text"), 4_000)
    return _clean_text(value, 4_000)


def _usable_ai_text(value: Any) -> str:
    """Return AI text only when it contains a real narrative suggestion."""

    text = _claim_text(value)
    return "" if text.casefold() == "not supplied" else text


def _executive_ai_candidate(draft: Mapping[str, Any], value: Any) -> str:
    """Keep Claude from downgrading a richer deterministic executive baseline.

    Claude remains optional polish. If it drops supported areas, constraint tags,
    or deterministic workforce/missing-status facts, the review UI falls back to
    the Python baseline instead of presenting a shorter but less useful summary.
    """

    candidate = _usable_ai_text(value)
    baseline = _clean_text(draft.get("executive_summary"), 4_000)
    if not candidate:
        return baseline
    if not baseline:
        return candidate

    # A very short rewrite is usually a regression when the baseline deliberately
    # carries several area highlights. This is a presentation guard, not a content
    # score and does not block a reviewer from manually editing the final field.
    if len(baseline) >= 600 and len(candidate) < min(420, int(len(baseline) * 0.45)):
        return baseline

    baseline_folded = candidate.casefold()
    grouped = draft.get("activity_summary") if isinstance(draft.get("activity_summary"), list) else []
    supported_areas = []
    for row in grouped:
        if not isinstance(row, Mapping):
            continue
        area = _clean_text(row.get("area"), 80)
        if area and area not in supported_areas:
            supported_areas.append(area)
    # Weekly reports usually have a manageable area set; preserve all of it. For
    # a large monthly set, require broad coverage without forcing a wall of labels.
    required_areas = supported_areas if len(supported_areas) <= 8 else supported_areas[:6]
    if any(area.casefold() not in baseline_folded for area in required_areas):
        return baseline

    constraint_tags = _constraint_tags(draft.get("constraints"))
    if len(constraint_tags) <= 6 and any(tag.casefold() not in baseline_folded for tag in constraint_tags):
        return baseline

    deterministic = draft.get("deterministic_summary") if isinstance(draft.get("deterministic_summary"), Mapping) else {}
    deterministic_exec = _clean_text(
        deterministic.get("executive_summary", {}).get("text")
        if isinstance(deterministic.get("executive_summary"), Mapping) else baseline,
        4_000,
    ).casefold()
    for phrase in ("peak daily headcount", "man-hours", "overall progress percentages", "safety incident metrics"):
        if phrase in deterministic_exec and phrase not in baseline_folded:
            return baseline
    return candidate



def _clean_ai_references(value: Any) -> dict[str, list[str]]:
    row = value if isinstance(value, dict) else {}
    source_ids = []
    for item in row.get("source_ids", [])[:40] if isinstance(row.get("source_ids"), list) else []:
        text = _clean_text(item, 300)
        if text and text not in source_ids:
            source_ids.append(text)
    dates = []
    for item in row.get("dates", [])[:40] if isinstance(row.get("dates"), list) else []:
        text = _clean_text(item, 10)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) and text not in dates:
            dates.append(text)
    evidence_paths = []
    for item in row.get("evidence_paths", [])[:40] if isinstance(row.get("evidence_paths"), list) else []:
        text = _clean_text(item, 500)
        if text and text not in evidence_paths:
            evidence_paths.append(text)
    return {"source_ids": source_ids, "dates": dates, "evidence_paths": evidence_paths}


def _clean_ai_missing_data(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows[:75]:
        text = _clean_text(row, 500)
        if text and text not in result:
            result.append(text)
    return result


def _clean_ai_citation_evidence(value: Any) -> dict[str, Any]:
    evidence = value if isinstance(value, dict) else {}
    result = {
        key: _clean_ai_references(evidence.get(key))
        for key in (
            "executive_summary",
            "engineering_summary",
            "procurement_summary",
            "site_summary",
        )
    }
    for key in ("current_activities", "concern_actions", "lookahead", "claims"):
        rows = evidence.get(key) if isinstance(evidence.get(key), list) else []
        result[key] = [_clean_ai_references(row) for row in rows[:75]]
    return result



def _is_missing_action_text(value: Any) -> bool:
    text = _clean_text(value, 2_000).casefold().strip(" .;:-—")
    return text in {"", "not supplied"}


def _merge_concern_rows(existing: Any, accepted: Any) -> list[dict[str, str]]:
    """Merge AI-enriched concerns into deterministic source constraints by tag.

    This prevents the same 81-EV-xxxx issue from appearing once as a formal
    constraint and again as a polished AI row.  The source constraint remains the
    baseline; accepted AI wording/action may enrich the same tagged issue.
    """

    result: list[dict[str, str]] = []
    for raw in existing if isinstance(existing, list) else []:
        if isinstance(raw, Mapping):
            concern = _clean_text(raw.get("concern", raw.get("text", raw.get("description"))), 2_000)
            action = _clean_text(raw.get("corrective_action", raw.get("action")), 2_000)
        else:
            concern = _clean_text(raw, 2_000)
            action = ""
        if concern or action:
            result.append({"concern": concern, "corrective_action": action})

    for raw in accepted if isinstance(accepted, list) else []:
        if not isinstance(raw, Mapping):
            continue
        concern = _clean_text(raw.get("concern"), 2_000)
        action = _clean_text(raw.get("corrective_action"), 2_000)
        if not concern and not action:
            continue
        tags = _activity_equipment_ids(concern)
        match_index = None
        if tags:
            for index, current in enumerate(result):
                current_tags = _activity_equipment_ids(current.get("concern", ""))
                if current_tags.intersection(tags):
                    match_index = index
                    break
        if match_index is None:
            exact_key = (concern.casefold(), action.casefold())
            if any((row["concern"].casefold(), row["corrective_action"].casefold()) == exact_key for row in result):
                continue
            result.append({"concern": concern, "corrective_action": "" if _is_missing_action_text(action) else action})
            continue

        current = result[match_index]
        # Prefer the accepted client-facing concern when it is a richer wording
        # of the same equipment-tagged issue; never merge rows with different tags.
        if concern and len(concern) > len(current.get("concern", "")):
            current["concern"] = concern
        if not _is_missing_action_text(action):
            current["corrective_action"] = action
    return result


def _clean_ai_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("AI suggestion review must be an object.")
    concerns = []
    for row in value.get("concerns", [])[:100] if isinstance(value.get("concerns"), list) else []:
        if not isinstance(row, dict):
            continue
        concern = _clean_text(row.get("concern"), 2_000)
        action = _clean_text(row.get("corrective_action"), 2_000)
        if concern or action:
            concerns.append({"concern": concern, "corrective_action": action})
    lookahead = [
        _clean_text(row, 2_000)
        for row in (value.get("lookahead", [])[:250] if isinstance(value.get("lookahead"), list) else [])
        if _clean_text(row, 2_000)
    ]
    current_activities = _clean_activity_rows(value.get("current_activities"))
    return {
        "executive_summary": _clean_text(value.get("executive_summary"), 4_000),
        "engineering_summary": _clean_text(value.get("engineering_summary"), 4_000),
        "procurement_summary": _clean_text(value.get("procurement_summary"), 4_000),
        "site_summary": _clean_text(value.get("site_summary"), 4_000),
        "current_activities": current_activities,
        "concerns": concerns,
        "lookahead": lookahead,
    }


def _audit_source_manifest(value: Any) -> list[dict[str, Any]]:
    """Keep content hashes needed for an audit without retaining upload names."""

    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            key: copy.deepcopy(row.get(key))
            for key in ("source_id", "sha256", "size_bytes", "status")
            if row.get(key) not in (None, "")
        }
        if item:
            result.append(item)
    return result


def _workforce_issue_audit(value: Any) -> dict[str, Any] | None:
    """Remove employee-level workbook data from an issued report JSON.

    Full previews remain in the owner's editable draft.  The issued artifact
    needs only reproducible source hashes, deterministic totals, decisions,
    and reviewer metadata; names, per-day attendance statuses, raw overtime
    rows, and workbook filenames are not report content.
    """

    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {
        "version": value.get("version"),
        "privacy_compacted": True,
        "effective": copy.deepcopy(value.get("effective", {})),
    }

    timesheet = value.get("timesheet") if isinstance(value.get("timesheet"), dict) else {}
    timesheet_preview = (
        timesheet.get("preview") if isinstance(timesheet.get("preview"), dict) else {}
    )
    timesheet_audit = {
        key: copy.deepcopy(timesheet.get(key))
        for key in (
            "status", "reviewed_by", "reviewed_at", "decided_by", "decided_at",
            "confirmed_exceptions",
        )
        if timesheet.get(key) not in (None, "")
    }
    timesheet_audit.update({
        "formula_version": timesheet_preview.get("formula_version"),
        "hours_per_present_day": timesheet_preview.get("hours_per_present_day"),
        "period": copy.deepcopy(timesheet_preview.get("period", {})),
        "coverage": copy.deepcopy(timesheet_preview.get("coverage", {})),
        "totals": copy.deepcopy(timesheet_preview.get("totals", {})),
        "source_manifest": _audit_source_manifest(timesheet_preview.get("source_manifest")),
        "warning_count": len(timesheet_preview.get("warnings", []))
        if isinstance(timesheet_preview.get("warnings"), list) else 0,
        "unresolved_count": len(timesheet_preview.get("unresolved", []))
        if isinstance(timesheet_preview.get("unresolved"), list) else 0,
    })
    result["timesheet"] = timesheet_audit

    overtime = value.get("overtime") if isinstance(value.get("overtime"), dict) else {}
    overtime_preview = (
        overtime.get("preview") if isinstance(overtime.get("preview"), dict) else {}
    )
    overtime_manifest = (
        overtime_preview.get("manifest")
        if isinstance(overtime_preview.get("manifest"), dict)
        else {}
    )
    resolution_rows = []
    resolutions = overtime.get("resolutions") if isinstance(overtime.get("resolutions"), dict) else {}
    for employee_key, decision in sorted(resolutions.items()):
        resolution_rows.append({
            "employee_hash": hashlib.sha256(str(employee_key).encode("utf-8")).hexdigest(),
            "decision": str(decision),
        })
    accepted_ids = sorted({
        str(row.get("record_id"))
        for row in overtime.get("accepted_records", [])
        if isinstance(row, dict) and row.get("record_id")
    }) if isinstance(overtime.get("accepted_records"), list) else []
    record_resolution_rows = []
    record_resolutions = (
        overtime.get("record_resolutions")
        if isinstance(overtime.get("record_resolutions"), dict)
        else {}
    )
    for record_id, decision in sorted(record_resolutions.items()):
        if not isinstance(decision, dict):
            continue
        record_resolution_rows.append({
            "record_id": str(record_id),
            "decision": str(decision.get("decision") or ""),
            "duration_hours": decision.get("duration_hours"),
        })
    overtime_audit = {
        key: copy.deepcopy(overtime.get(key))
        for key in (
            "status", "reviewed_by", "reviewed_at", "decided_by", "decided_at",
            "confirmed_exceptions",
        )
        if overtime.get(key) not in (None, "")
    }
    overtime_audit.update({
        "formula_version": overtime_preview.get("formula_version"),
        "calculation_policy": copy.deepcopy(overtime_preview.get("calculation_policy", {})),
        "period": copy.deepcopy(overtime_preview.get("period", {})),
        "coverage": copy.deepcopy(overtime_preview.get("coverage", {})),
        "totals": copy.deepcopy(overtime_preview.get("totals", {})),
        "source_manifest": _audit_source_manifest(overtime_manifest.get("files")),
        "warning_count": len(overtime_preview.get("warnings", []))
        if isinstance(overtime_preview.get("warnings"), list) else 0,
        "conflict_count": len(overtime_preview.get("conflicts", []))
        if isinstance(overtime_preview.get("conflicts"), list) else 0,
        "resolutions": resolution_rows,
        "record_resolutions": record_resolution_rows,
        "accepted_record_ids": accepted_ids,
    })
    result["overtime"] = overtime_audit
    return result


def _ai_issue_audit(value: Any) -> dict[str, Any] | None:
    """Keep provider accounting metadata but not the raw suggestion envelope."""

    if not isinstance(value, dict):
        return None
    envelope = value.get("provider_envelope") if isinstance(value.get("provider_envelope"), dict) else {}
    result = {
        key: copy.deepcopy(value.get(key))
        for key in ("status", "requested_at", "requested_by", "decided_at", "decided_by")
        if value.get(key) not in (None, "")
    }
    result.update({
        key: copy.deepcopy(envelope.get(key))
        for key in (
            "version", "prompt", "prompt_version", "model", "input_hash",
            "generated_at", "usage", "request_id",
        )
        if envelope.get(key) not in (None, "")
    })
    suggestion = value.get("suggestion") if isinstance(value.get("suggestion"), dict) else {}
    evidence = suggestion.get("citation_evidence")
    if isinstance(evidence, dict):
        result["citation_evidence"] = _clean_ai_citation_evidence(evidence)
    missing_data = _clean_ai_missing_data(suggestion.get("missing_data"))
    if missing_data:
        result["missing_data"] = missing_data
    result["privacy_compacted"] = True
    return result


def _issued_report_copy(report: dict[str, Any]) -> dict[str, Any]:
    """Create the persistent report artifact without draft-only sensitive data."""

    value = copy.deepcopy(report)
    value.pop("_source_records", None)
    value.pop("ai_request_control", None)
    workforce = _workforce_issue_audit(value.get("workforce_validation"))
    if workforce is None:
        value.pop("workforce_validation", None)
    else:
        value["workforce_validation"] = workforce
    ai_audit = _ai_issue_audit(value.get("ai_summary"))
    if ai_audit is None:
        value.pop("ai_summary", None)
    else:
        value["ai_summary"] = ai_audit
    return value






def _revision_history_rows(
    reports_dir: Path,
    prior_entries: list[dict[str, Any]],
    current: Mapping[str, Any],
    revision: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in sorted(prior_entries, key=lambda row: int(row.get("revision", 0) or 0)):
        detail: dict[str, Any] = {}
        json_name = str(entry.get("json_filename") or "")
        if json_name and os.path.basename(json_name) == json_name:
            path = reports_dir / json_name
            try:
                with path.open(encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    detail = loaded
            except (OSError, ValueError, TypeError):
                detail = {}
        rows.append({
            "rev": f"R{int(entry.get('revision', 0) or 0)}",
            "description": _clean_text(
                detail.get("revision_description")
                or _periodic_revision_description(detail or current), 500
            ),
            "date": _clean_text(detail.get("issued_date") or entry.get("generated_at"), 20)[:10],
            "prepared": _clean_text(detail.get("prepared_by"), 200),
            "checked": _clean_text(detail.get("checked_by"), 200),
            "vendor_approved": _clean_text(detail.get("approved_by"), 200),
            "kn_approved": _clean_text(detail.get("kn_approved_by"), 200),
        })
    rows.append({
        "rev": f"R{revision}",
        "description": _clean_text(
            current.get("revision_description")
            or _periodic_revision_description(current), 500
        ),
        "date": _clean_text(current.get("issued_date"), 20)[:10],
        "prepared": _clean_text(current.get("prepared_by"), 200),
        "checked": _clean_text(current.get("checked_by"), 200),
        "vendor_approved": _clean_text(current.get("approved_by"), 200),
        "kn_approved": _clean_text(current.get("kn_approved_by"), 200),
    })
    return rows[-3:]


def _set_appendix_content(
    draft: dict[str, Any],
    source_number: str,
    title: str,
    content: Any,
    source_meta: Mapping[str, Any],
) -> None:
    rows = draft.get("appendices") if isinstance(draft.get("appendices"), list) else []
    rows = copy.deepcopy(rows)
    found = None
    for row in rows:
        if isinstance(row, dict) and str(row.get("source_number") or row.get("number") or "") == source_number:
            found = row
            break
    if found is None:
        found = {"source_number": source_number, "number": source_number, "title": title}
        rows.append(found)
    found.update({
        "title": title,
        "status": "Included",
        "content": copy.deepcopy(content),
        "source_meta": copy.deepcopy(dict(source_meta)),
    })
    draft["appendices"] = rows


def _apply_structured_source(
    draft: dict[str, Any],
    *,
    section: str,
    payload: Any,
    actor: str,
    reference: str,
) -> None:
    allowed = {
        "engineering", "procurement", "equipment_delivery", "shipments", "safety",
        "schedule", "document_deliverables", "qc", "s_curve", "manpower_equipment",
    }
    if section not in allowed:
        raise ValueError("Unsupported structured source section.")
    source_type = {
        "engineering": "engineering_input",
        "procurement": "procurement_input",
        "equipment_delivery": "delivery_input",
        "shipments": "shipment_input",
        "safety": "safety_input",
        "schedule": "schedule_input",
        "document_deliverables": "document_register_input",
        "qc": "qc_input",
        "s_curve": "approved_progress_timeseries",
        "manpower_equipment": "manpower_equipment_input",
    }[section]
    meta = {
        "source_type": source_type,
        "entered_by": _clean_text(actor, 200),
        "entered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reference": _clean_text(reference, 1_000),
    }
    draft.setdefault("structured_sources", {})[section] = {
        "payload": copy.deepcopy(payload),
        "source_meta": copy.deepcopy(meta),
    }
    if section in {"engineering", "procurement", "equipment_delivery", "safety"}:
        value = copy.deepcopy(payload) if isinstance(payload, dict) else {"summary": _clean_text(payload, 10_000)}
        value["source_meta"] = copy.deepcopy(meta)
        draft[section] = value
        if section == "safety":
            _set_appendix_content(draft, "6.7", "Safety Report", payload, meta)
    elif section == "shipments":
        draft["shipments"] = copy.deepcopy(payload)
        draft["shipments_source_meta"] = copy.deepcopy(meta)
    elif section == "schedule":
        site = draft.get("site") if isinstance(draft.get("site"), dict) else {}
        site["schedule_status"] = copy.deepcopy(payload)
        site["schedule_source_meta"] = copy.deepcopy(meta)
        draft["site"] = site
        _set_appendix_content(draft, "6.3", "Overall Schedule", payload, meta)
    elif section == "document_deliverables":
        _set_appendix_content(draft, "6.4", "Document Deliverable List / Drawing Status", payload, meta)
    elif section == "qc":
        _set_appendix_content(draft, "6.8", "QC Document", payload, meta)
    elif section == "manpower_equipment":
        _set_appendix_content(draft, "6.5", "Manning Manpower / Equipment Loading", payload, meta)
    elif section == "s_curve":
        if not isinstance(payload, dict):
            raise ValueError("S-Curve payload must be an object with labels/plan/actual.")
        curve = copy.deepcopy(payload)
        curve.setdefault("approved", False)
        curve["source_meta"] = copy.deepcopy(meta)
        draft["s_curve"] = curve
        draft["include_s_curve"] = True


def _render(
    draft: dict[str, Any],
    config: dict[str, Any],
    *,
    photo_base_dir: str | os.PathLike[str] | None = None,
):
    configured_logo = config.get("logo_gpa") if isinstance(config, dict) else None
    bundled_logo = Path(__file__).resolve().parent.parent / "static" / "pdf_assets" / "gpa_logo.png"
    configured_path = Path(str(configured_logo)) if configured_logo else None
    logo_path = (
        str(configured_path)
        if configured_path is not None and configured_path.is_file()
        else (str(bundled_logo) if bundled_logo.is_file() else None)
    )
    result = render_monthly_report(
        draft,
        logo_path=logo_path,
        photo_base_dir=photo_base_dir,
    )
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

    @app.get("/monthly/preflight/<draft_id>")
    def periodic_report_preflight(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        draft = _load_draft(data_dir, session["username"], draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        for_final = str(request.args.get("final") or "").strip().lower() in {"1", "true", "yes"}
        preflight = build_report_preflight(draft, for_final=for_final)
        preflight = _append_runtime_preflight_blockers(
            preflight,
            data_dir=data_dir,
            username=session["username"],
            draft_id=draft_id,
            report=draft,
            for_final=for_final,
        )
        return jsonify({"ok": True, "preflight": preflight})

    @app.post("/monthly/structured-source/<draft_id>")
    def update_periodic_structured_source(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid structured source payload."}), 400
        try:
            _require_applied_source_validation(draft)
            section = _clean_text(body.get("section"), 80).lower()
            _apply_structured_source(
                draft,
                section=section,
                payload=body.get("payload"),
                actor=username,
                reference=_clean_text(body.get("reference"), 1_000),
            )
            _refresh_deterministic_summary(draft)
            draft.pop("ai_summary", None)
            _update_draft(data_dir, username, draft)
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/monthly/compile/stored")
    def compile_monthly_stored():
        auth = require_login_json()
        if auth:
            return auth
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid report compile request."}), 400
        kind = "monthly"
        pending_draft_id = ""
        draft_saved = False
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

            source_validation = build_source_validation(
                records,
                selected_project_no=project_no,
                selected_project_title=project_title,
            )
            provisional_records = _provisional_project_records(
                records,
                source_validation,
                project_no=project_no,
                project_title=project_title,
            )
            aggregated = aggregate_monthly_records(
                provisional_records,
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
                record for record in provisional_records
                if not selected_ids or str(record.get("report_id") or "") in selected_ids
            ]

            # Hydrate only the records that aggregation actually selected.
            # Unrelated projects and superseded revisions must not consume the
            # draft's report-specific photo/byte budget.
            pending_draft_id = uuid.uuid4().hex
            draft_photo_dir = _draft_photo_dir(
                data_dir,
                session["username"],
                pending_draft_id,
            )
            if draft_photo_dir is None:
                raise ValueError("Invalid report draft photo directory")
            photo_limits = periodic_photo_limits(kind)
            photo_warnings = attach_canonical_photo_candidates(
                selected_records,
                data_dir,
                draft_photo_dir,
                limits=photo_limits,
            )
            photo_warnings.extend(_bound_record_photo_candidates(selected_records, limits=photo_limits))
            photo_warnings = _compact_review_warnings(photo_warnings)
            # Rebuild the same source groups with photo warnings included in
            # the confirmation form. Selection itself remains unchanged.
            source_validation = build_source_validation(
                records,
                selected_project_no=project_no,
                selected_project_title=project_title,
                issues=photo_warnings,
            )
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
                extra_warnings=photo_warnings,
                report_type=kind,
            )
            draft["source_validation"] = source_validation
            draft["_source_records"] = copy.deepcopy(records)
            draft["photo_documentation"] = _photo_references_for_records(
                selected_records, limits=photo_limits
            )
            draft_id = _save_draft(
                data_dir,
                session["username"],
                draft,
                draft_id=pending_draft_id,
            )
            draft_saved = True
            draft["draft_id"] = draft_id
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            if pending_draft_id and not draft_saved:
                _remove_draft_assets(data_dir, session["username"], pending_draft_id)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            if pending_draft_id and not draft_saved:
                _remove_draft_assets(data_dir, session["username"], pending_draft_id)
            report_name = _report_name(kind)
            app.logger.exception("Stored JSON %s compilation failed", kind)
            return jsonify({"error": f"{report_name} compilation failed: {exc}"}), 500

    @app.post("/monthly/upload-session/start")
    def start_monthly_upload_session():
        auth = require_login_json()
        if auth:
            return auth
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid upload session request."}), 400
        try:
            kind = _report_type(body.get("report_type") or "monthly")
            mode = _normalise_report_mode(kind, body.get("report_mode"))
            start, end = _parse_period(body.get("date_from"), body.get("date_to"), kind, mode)
            project_no = _clean_text(body.get("project_no"), 250)
            project_title = _clean_text(body.get("project_title"), 500)
            if not project_no:
                raise ValueError("Select a project first.")

            raw_files = body.get("files")
            if not isinstance(raw_files, list) or not raw_files:
                raise ValueError("Choose at least one Daily Report PDF.")
            if len(raw_files) > _MAX_UPLOAD_FILES:
                return jsonify({
                    "error": f"A maximum of {_MAX_UPLOAD_FILES} PDF files can be compiled at once."
                }), 413

            planned_files: dict[str, dict[str, Any]] = {}
            for raw in raw_files:
                if not isinstance(raw, dict):
                    raise ValueError("Invalid PDF upload list.")
                file_id = str(raw.get("file_id") or "")
                if not _UPLOAD_FILE_ID_RE.fullmatch(file_id) or file_id in planned_files:
                    raise ValueError("Each PDF must have a unique upload ID.")
                filename = _clean_text(raw.get("filename"), 255) or "report.pdf"
                try:
                    size_bytes = int(raw.get("size_bytes") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{filename}: invalid file size.") from exc
                if size_bytes < 0:
                    raise ValueError(f"{filename}: invalid file size.")
                if size_bytes > DEFAULT_LIMITS.max_bytes:
                    return jsonify({
                        "error": (
                            f"{filename} is larger than the {DEFAULT_LIMITS.max_bytes // (1024 * 1024)} MB "
                            "per-file limit."
                        )
                    }), 413
                planned_files[file_id] = {
                    "file_id": file_id,
                    "filename": filename,
                    "size_bytes": size_bytes,
                }

            username = session["username"]
            _cleanup_upload_sessions(data_dir, username)
            root = _upload_sessions_dir(data_dir, username)
            requested_session_id = str(body.get("upload_session_id") or "")
            if requested_session_id and not _DRAFT_ID_RE.fullmatch(requested_session_id):
                raise ValueError("Invalid upload session ID.")
            upload_session_id = requested_session_id or uuid.uuid4().hex
            directory = root / upload_session_id
            if directory.exists():
                loaded = _load_upload_session(data_dir, username, upload_session_id)
                if loaded is None:
                    return jsonify({
                        "error": "This upload session is still being prepared. Retry shortly."
                    }), 409
                _, existing = loaded
                comparable_keys = (
                    "report_type", "report_mode", "project_no", "project_title", "date_from", "date_to", "files"
                )
                candidate = {
                    "report_type": kind,
                    "report_mode": mode,
                    "project_no": project_no,
                    "project_title": project_title,
                    "date_from": start.strftime("%Y-%m-%d"),
                    "date_to": end.strftime("%Y-%m-%d"),
                    "files": planned_files,
                }
                if all(existing.get(key) == candidate.get(key) for key in comparable_keys):
                    return jsonify({
                        "ok": True,
                        "cached": True,
                        "upload_session_id": upload_session_id,
                        "file_count": len(planned_files),
                        "max_file_bytes": DEFAULT_LIMITS.max_bytes,
                    })
                return jsonify({
                    "error": "Upload session ID is already used for a different report setup."
                }), 409

            active_count = sum(
                1
                for path in root.iterdir()
                if path.is_dir()
                and _DRAFT_ID_RE.fullmatch(path.name)
                and not (path / "result.json").is_file()
            )
            if active_count >= _MAX_ACTIVE_UPLOAD_SESSIONS:
                return jsonify({
                    "error": "Too many unfinished upload sessions. Finish or retry the current report first."
                }), 429

            directory.mkdir()
            (directory / "items").mkdir()
            manifest = {
                "schema_version": "report-upload-session/1",
                "upload_session_id": upload_session_id,
                "owner": username,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "report_type": kind,
                "report_mode": mode,
                "project_no": project_no,
                "project_title": project_title,
                "date_from": start.strftime("%Y-%m-%d"),
                "date_to": end.strftime("%Y-%m-%d"),
                "files": planned_files,
            }
            _atomic_json(directory / "session.json", manifest)
            return jsonify({
                "ok": True,
                "cached": False,
                "upload_session_id": upload_session_id,
                "file_count": len(planned_files),
                "max_file_bytes": DEFAULT_LIMITS.max_bytes,
            })
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            app.logger.exception("Could not start PDF upload session")
            return jsonify({"error": "Could not start PDF upload session. Retry or check the server logs."}), 500

    @app.post("/monthly/upload-session/<upload_session_id>/file")
    def upload_monthly_session_file(upload_session_id: str):
        auth = require_login_json()
        if auth:
            return auth
        if request.content_length is not None and request.content_length > _MAX_STAGED_REQUEST_BYTES:
            return jsonify({
                "error": f"Upload exceeds the {DEFAULT_LIMITS.max_bytes // (1024 * 1024)} MB per-file limit."
            }), 413
        loaded = _load_upload_session(data_dir, session["username"], upload_session_id)
        if loaded is None:
            return jsonify({"error": "Upload session not found or expired."}), 404
        directory, manifest = loaded
        if (directory / "result.json").is_file():
            return jsonify({"error": "This upload session has already been compiled."}), 409

        # The UI sends the stable ID as a header so a retry can be rejected as
        # busy before Werkzeug parses another large multipart body.
        file_id = str(request.headers.get("X-Upload-File-ID") or "")
        if not file_id:
            file_id = str(request.form.get("file_id") or "")
        planned = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        if not _UPLOAD_FILE_ID_RE.fullmatch(file_id) or file_id not in planned:
            return jsonify({"error": "Unknown PDF upload ID."}), 400

        existing = _load_upload_item(directory, file_id)
        if existing is not None:
            return jsonify({"ok": True, "cached": True, "item": _public_upload_item(existing)})

        operation_lock = _acquire_upload_operation_lock(directory)
        if operation_lock is None:
            return jsonify({
                "error": "This upload session is busy processing another request. Retry this file shortly."
            }), 409
        try:
            loaded = _load_upload_session(data_dir, session["username"], upload_session_id)
            if loaded is None:
                return jsonify({"error": "Upload session not found or expired."}), 404
            directory, manifest = loaded
            if (directory / "result.json").is_file():
                return jsonify({"error": "This upload session has already been compiled."}), 409
            existing = _load_upload_item(directory, file_id)
            if existing is not None:
                return jsonify({"ok": True, "cached": True, "item": _public_upload_item(existing)})

            uploads = request.files.getlist("file")
            if len(uploads) != 1:
                return jsonify({"error": "Upload exactly one Daily Report PDF per request."}), 400
            upload = uploads[0]
            planned_file = planned[file_id] if isinstance(planned[file_id], dict) else {}
            filename = _clean_text(upload.filename, 255) or str(planned_file.get("filename") or "report.pdf")
            warnings: list[str] = []
            record: dict[str, Any] | None = None
            source_size = int(planned_file.get("size_bytes") or 0)

            if not filename.lower().endswith(".pdf"):
                warnings.append(f"{filename}: only PDF files can be uploaded; file excluded.")
            else:
                try:
                    config = config_provider()
                    known_projects = config.get("projects", []) if isinstance(config, dict) else []
                    imported = import_daily_report_pdf(
                        upload.stream,
                        filename=filename,
                        known_projects=known_projects,
                    )
                    record, warnings = _record_from_uploaded_pdf(
                        imported,
                        filename=filename,
                        username=session["username"],
                        project_no=str(manifest.get("project_no") or ""),
                        project_title=str(manifest.get("project_title") or ""),
                        date_from=str(manifest.get("date_from") or ""),
                        date_to=str(manifest.get("date_to") or ""),
                        report_type=str(manifest.get("report_type") or "monthly"),
                    )
                    if record is not None:
                        photo_limits = periodic_photo_limits(manifest.get("report_type"))
                        candidates, photo_warnings = extract_pdf_photo_candidates(
                            upload.stream,
                            filename=filename,
                            areas=_record_photo_areas(record),
                            limits=photo_limits,
                        )
                        report_date = _record_date(record)
                        for candidate in candidates:
                            candidate.setdefault("source_date", report_date)
                            candidate.setdefault("source_type", "legacy_pdf_extraction")
                        photo_references = store_photo_candidates(
                            candidates,
                            directory / "assets",
                            source_report_id=str(record.get("report_id") or ""),
                            maximum=photo_limits.max_images_per_pdf,
                            max_total_bytes=photo_limits.max_total_asset_bytes_per_draft,
                        )
                        record["_photo_candidates"] = photo_references
                        if len(photo_references) < len(candidates):
                            warnings.append(
                                f"{filename}: some photos were excluded by the report draft asset limit."
                            )
                        warnings.extend(photo_warnings)
                    source = imported.get("source") if isinstance(imported.get("source"), dict) else {}
                    source_size = int(source.get("size_bytes") or source_size)
                except PDFImportError as exc:
                    warnings.append(f"{filename}: {exc}; file excluded.")
                except Exception:
                    app.logger.exception("Staged PDF import failed for %s", filename)
                    return jsonify({
                        "error": f"{filename}: PDF processing failed. Retry this file or check the server logs."
                    }), 500

            item = {
                "file_id": file_id,
                "filename": filename,
                "status": "uploaded" if record is not None else "skipped",
                "included": record is not None,
                "report_date": _record_date(record) if record is not None else "",
                "size_bytes": source_size,
                "warnings": warnings,
                "record": record,
            }
            _atomic_json(directory / "items" / f"{file_id}.json", item)
            try:
                os.utime(directory, None)
            except OSError:
                pass
            return jsonify({"ok": True, "cached": False, "item": _public_upload_item(item)})
        finally:
            try:
                operation_lock.rmdir()
            except OSError:
                pass

    @app.post("/monthly/upload-session/<upload_session_id>/compile")
    def compile_monthly_upload_session(upload_session_id: str):
        auth = require_login_json()
        if auth:
            return auth
        loaded = _load_upload_session(data_dir, session["username"], upload_session_id)
        if loaded is None:
            return jsonify({"error": "Upload session not found or expired."}), 404
        directory, manifest = loaded

        result_path = directory / "result.json"
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                result = {}
            draft_id = str(result.get("draft_id") or "") if isinstance(result, dict) else ""
            draft = _load_draft(data_dir, session["username"], draft_id)
            if draft is None:
                return jsonify({"error": "The compiled upload draft is no longer available."}), 410
            return jsonify({"ok": True, "cached": True, "draft_id": draft_id, "draft": draft})

        operation_lock = _acquire_upload_operation_lock(directory)
        if operation_lock is None:
            return jsonify({"error": "This upload session is already being compiled. Please wait."}), 409

        try:
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    result = {}
                draft_id = str(result.get("draft_id") or "") if isinstance(result, dict) else ""
                draft = _load_draft(data_dir, session["username"], draft_id)
                if draft is None:
                    return jsonify({"error": "The compiled upload draft is no longer available."}), 410
                return jsonify({"ok": True, "cached": True, "draft_id": draft_id, "draft": draft})

            planned = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
            items: list[dict[str, Any]] = []
            missing: list[str] = []
            for file_id in planned:
                item = _load_upload_item(directory, file_id)
                if item is None:
                    missing.append(file_id)
                else:
                    items.append(item)
            if missing:
                return jsonify({
                    "error": f"{len(missing)} PDF file(s) are still waiting to upload or retry.",
                    "pending_file_ids": missing,
                }), 409

            kind = _report_type(manifest.get("report_type") or "monthly")
            mode = _normalise_report_mode(kind, manifest.get("report_mode"))
            start, end = _parse_period(
                manifest.get("date_from"),
                manifest.get("date_to"),
                kind,
                mode,
            )
            project_no = _clean_text(manifest.get("project_no"), 250)
            project_title = _clean_text(manifest.get("project_title"), 500)
            records: list[dict[str, Any]] = []
            seen_hashes: set[str] = set()
            warnings = [
                "Uploaded PDF data was normalized from a Daily Report template. "
                "Project identity, manpower, man-hours, warnings, and editable summaries must be validated before issue."
            ]
            for item in items:
                for warning in item.get("warnings", []):
                    warning_text = str(warning).strip()
                    if warning_text:
                        warnings.append(warning_text)
                record = item.get("record") if isinstance(item.get("record"), dict) else None
                if record is None:
                    continue
                source = record.get("source") if isinstance(record.get("source"), dict) else {}
                digest = str(source.get("sha256") or "")
                if digest and digest in seen_hashes:
                    warnings.append(f"{item.get('filename', 'report.pdf')}: duplicate PDF skipped.")
                    continue
                if digest:
                    seen_hashes.add(digest)
                records.append(record)

            if not records:
                error = (
                    "None of the uploaded PDFs could be included in this Weekly Report. "
                    "Check the project, period, text layer, and file warnings."
                    if kind == "weekly"
                    else "None of the uploaded PDFs could be included. Check the project, period, text layer, and file warnings."
                )
                return jsonify({"error": error, "warnings": warnings}), 400

            if kind == "weekly":
                # The first day is derived from report content, not browser
                # selection order or the provisional dates in the form.
                start, end = _rolling_week_period(records)
                end_text = end.strftime("%Y-%m-%d")
                in_window: list[dict[str, Any]] = []
                for record in records:
                    report_date = _record_date(record)
                    if report_date <= end_text:
                        in_window.append(record)
                    else:
                        source = record.get("source") if isinstance(record.get("source"), dict) else {}
                        warnings.append(
                            f"{source.get('filename') or 'report.pdf'}: date {report_date} is outside the "
                            f"rolling 7-day period ending {end_text}; file excluded."
                        )
                records = in_window

            photo_limits = periodic_photo_limits(kind)
            warnings.extend(_bound_record_photo_candidates(records, limits=photo_limits))
            warnings = _compact_review_warnings(warnings)

            source_validation = build_source_validation(
                records,
                selected_project_no=project_no,
                selected_project_title=project_title,
                issues=warnings,
            )
            provisional_records = _provisional_project_records(
                records,
                source_validation,
                project_no=project_no,
                project_title=project_title,
            )
            aggregated = aggregate_monthly_records(
                provisional_records,
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
                record for record in provisional_records
                if not selected_ids or str(record.get("report_id") or "") in selected_ids
            ]
            source_manifest = _source_manifest(selected_records, "uploaded_pdf")
            draft = _prepare_draft(
                aggregated,
                project_no=project_no,
                project_title=project_title,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                report_mode=mode,
                source_method="uploaded_pdf",
                source_manifest=source_manifest,
                report_context=_latest_report_context(selected_records),
                extra_warnings=warnings,
                report_type=kind,
            )
            draft["source_validation"] = source_validation
            draft["_source_records"] = copy.deepcopy(records)
            draft["photo_documentation"] = _photo_references_for_records(
                selected_records, limits=photo_limits
            )
            # Reusing the random upload-session ID closes the crash window
            # between saving the draft and writing the small result tombstone.
            draft_photo_dir = _draft_photo_dir(
                data_dir,
                session["username"],
                upload_session_id,
            )
            if draft_photo_dir is None:
                raise ValueError("Invalid report draft photo directory")
            copy_photo_assets(
                _all_photo_references(records),
                directory / "assets",
                draft_photo_dir,
            )
            draft_id = _save_draft(
                data_dir,
                session["username"],
                draft,
                draft_id=upload_session_id,
            )
            draft["draft_id"] = draft_id
            _atomic_json(result_path, {
                "status": "compiled",
                "draft_id": draft_id,
                "compiled_at": datetime.now().isoformat(timespec="seconds"),
            })
            try:
                shutil.rmtree(directory / "items")
            except OSError:
                pass
            return jsonify({"ok": True, "cached": False, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            kind = manifest.get("report_type") or "monthly"
            app.logger.exception("Staged uploaded PDF %s compilation failed", kind)
            return jsonify({
                "error": f"{_report_name(kind)} PDF compilation failed. Retry or check the server logs."
            }), 500
        finally:
            try:
                operation_lock.rmdir()
            except OSError:
                pass

    @app.delete("/monthly/upload-session/<upload_session_id>")
    def delete_monthly_upload_session(upload_session_id: str):
        auth = require_login_json()
        if auth:
            return auth
        directory = _upload_session_dir(data_dir, session["username"], upload_session_id)
        if directory is None or not directory.is_dir():
            return jsonify({"error": "Upload session not found or expired."}), 404
        operation_lock = _acquire_upload_operation_lock(directory)
        if operation_lock is None:
            return jsonify({"error": "Upload session is currently busy."}), 409
        try:
            _remove_upload_session(data_dir, session["username"], upload_session_id)
            return jsonify({"ok": True})
        finally:
            try:
                operation_lock.rmdir()
            except OSError:
                pass

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
        pending_draft_id = uuid.uuid4().hex
        draft_saved = False
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
                "Uploaded PDF data was normalized from a Daily Report template. "
                "Project identity, manpower, man-hours, warnings, and editable summaries must be validated before issue."
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
                record, imported_warnings = _record_from_uploaded_pdf(
                    imported,
                    filename=filename,
                    username=session["username"],
                    project_no=project_no,
                    project_title=project_title,
                    date_from=start.strftime("%Y-%m-%d"),
                    date_to=end.strftime("%Y-%m-%d"),
                    report_type=kind,
                )
                warnings.extend(imported_warnings)
                if record is not None:
                    photo_limits = periodic_photo_limits(kind)
                    candidates, photo_warnings = extract_pdf_photo_candidates(
                        upload.stream,
                        filename=filename,
                        areas=_record_photo_areas(record),
                        limits=photo_limits,
                    )
                    report_date = _record_date(record)
                    for candidate in candidates:
                        candidate.setdefault("source_date", report_date)
                        candidate.setdefault("source_type", "legacy_pdf_extraction")
                    pending_assets = _draft_photo_dir(
                        data_dir,
                        session["username"],
                        pending_draft_id,
                    )
                    if pending_assets is None:
                        raise ValueError("Invalid report draft photo directory")
                    photo_references = store_photo_candidates(
                        candidates,
                        pending_assets,
                        source_report_id=str(record.get("report_id") or ""),
                        maximum=photo_limits.max_images_per_pdf,
                        max_total_bytes=photo_limits.max_total_asset_bytes_per_draft,
                    )
                    record["_photo_candidates"] = photo_references
                    if len(photo_references) < len(candidates):
                        warnings.append(
                            f"{filename}: some photos were excluded by the report draft asset limit."
                        )
                    warnings.extend(photo_warnings)
                    records.append(record)

            if not records:
                _remove_draft_assets(data_dir, session["username"], pending_draft_id)
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
            if kind == "weekly":
                start, end = _rolling_week_period(records)
                end_text = end.strftime("%Y-%m-%d")
                in_window = []
                for record in records:
                    report_date = _record_date(record)
                    if report_date <= end_text:
                        in_window.append(record)
                    else:
                        source = record.get("source") if isinstance(record.get("source"), dict) else {}
                        warnings.append(
                            f"{source.get('filename') or 'report.pdf'}: date {report_date} is outside the "
                            f"rolling 7-day period ending {end_text}; file excluded."
                        )
                records = in_window
            photo_limits = periodic_photo_limits(kind)
            warnings.extend(_bound_record_photo_candidates(records, limits=photo_limits))
            warnings = _compact_review_warnings(warnings)
            source_validation = build_source_validation(
                records,
                selected_project_no=project_no,
                selected_project_title=project_title,
                issues=warnings,
            )
            provisional_records = _provisional_project_records(
                records,
                source_validation,
                project_no=project_no,
                project_title=project_title,
            )
            aggregated = aggregate_monthly_records(
                provisional_records,
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
                record for record in provisional_records
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
            draft["source_validation"] = source_validation
            draft["_source_records"] = copy.deepcopy(records)
            draft["photo_documentation"] = _photo_references_for_records(
                selected_records, limits=photo_limits
            )
            draft_id = _save_draft(
                data_dir,
                session["username"],
                draft,
                draft_id=pending_draft_id,
            )
            draft_saved = True
            draft["draft_id"] = draft_id
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            if not draft_saved:
                _remove_draft_assets(data_dir, session["username"], pending_draft_id)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            if not draft_saved:
                _remove_draft_assets(data_dir, session["username"], pending_draft_id)
            app.logger.exception("Uploaded PDF %s compilation failed", kind)
            prefix = "Weekly PDF" if kind == "weekly" else "PDF"
            return jsonify({"error": f"{prefix} compilation failed: {exc}"}), 500

    @app.post("/monthly/validate/<draft_id>")
    def validate_monthly_report_sources(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid Source Data Validation."}), 400
        try:
            review = _source_validation_payload(body.get("source_validation"))
            if not review["confirmed"]:
                raise ValueError("Confirm Source Data Validation before applying it.")
            validation = draft.get("source_validation")
            if not isinstance(validation, dict):
                raise ValueError("This draft has no source validation data. Compile it again.")
            raw_records = draft.get("_source_records")
            if not isinstance(raw_records, list):
                raise ValueError("The source records are unavailable. Compile the report again.")

            project_records, project_excluded_records = resolve_project_records(
                raw_records,
                validation,
                project_no=review["project_no"],
                project_title=review["project_title"],
                resolutions=review["project_resolutions"],
            )
            included_records, duplicate_excluded_records = resolve_duplicate_records(
                project_records,
                validation,
                resolutions=review["duplicate_resolutions"],
            )
            excluded_records = project_excluded_records + duplicate_excluded_records
            kind = _draft_report_type(draft)
            mode = _normalise_report_mode(kind, draft.get("report_mode"))
            period = draft.get("period") if isinstance(draft.get("period"), dict) else {}
            start, end = _parse_period(period.get("start"), period.get("end"), kind, mode)
            project_no = review["project_no"]
            project_title = review["project_title"]
            aggregated = aggregate_monthly_records(
                included_records,
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
                record for record in included_records
                if not selected_ids or str(record.get("report_id") or "") in selected_ids
            ]
            source_method = str(draft.get("source_method") or "stored_json")
            photo_limits = periodic_photo_limits(kind)
            photo_warnings: list[str] = []
            if source_method == "stored_json":
                draft_photo_dir = _draft_photo_dir(data_dir, username, draft_id)
                if draft_photo_dir is None:
                    raise ValueError("Invalid report draft photo directory")
                # A changed project/revision choice gets a fresh, bounded
                # hydration pass. Canonical files remain read-only.
                photo_limits = periodic_photo_limits(kind)
                photo_warnings = attach_canonical_photo_candidates(
                    selected_records,
                    data_dir,
                    draft_photo_dir,
                    limits=photo_limits,
                )
                photo_warnings.extend(_bound_record_photo_candidates(selected_records, limits=photo_limits))
                photo_warnings = _compact_review_warnings(photo_warnings)
            warnings = [
                str(value).strip()
                for value in draft.get("warnings", [])
                if str(value).strip()
                and not (
                    source_method == "stored_json"
                    and _is_generated_stored_photo_warning(value)
                )
                and not re.fullmatch(
                    r"(?:\d+ Daily Report source\(s\) were kept separate and excluded from this report"
                    r"|\d+ duplicate Daily Report source\(s\) were not selected for this report)\.",
                    str(value).strip(),
                )
            ]
            if project_excluded_records:
                warnings.append(
                    f"{len(project_excluded_records)} Daily Report source(s) were kept separate and excluded from this report."
                )
            if duplicate_excluded_records:
                warnings.append(
                    f"{len(duplicate_excluded_records)} duplicate Daily Report source(s) were not selected for this report."
                )
            warnings.extend(photo_warnings)
            refreshed = _prepare_draft(
                aggregated,
                project_no=project_no,
                project_title=project_title,
                date_from=start.strftime("%Y-%m-%d"),
                date_to=end.strftime("%Y-%m-%d"),
                report_mode=mode,
                source_method=source_method,
                source_manifest=_source_manifest(
                    selected_records,
                    source_method,
                ),
                report_context=_latest_report_context(selected_records),
                extra_warnings=warnings,
                report_type=kind,
            )
            decisions = {
                str(row.get("group_key") or ""): str(row.get("decision") or "")
                for row in review["project_resolutions"]
                if isinstance(row, dict)
            }
            applied_validation = copy.deepcopy(validation)
            if source_method == "stored_json":
                retained_issues = []
                for issue in applied_validation.get("issues", []):
                    if not isinstance(issue, dict):
                        continue
                    if not _is_generated_stored_photo_warning(issue.get("message")):
                        retained_issues.append(issue)
                retained_issues.extend(
                    {"severity": "warning", "message": message}
                    for message in photo_warnings
                )
                applied_validation["issues"] = retained_issues
            for group in applied_validation.get("project_groups", []):
                if isinstance(group, dict):
                    group["decision"] = decisions.get(str(group.get("key") or ""), "")
            duplicate_decisions = {
                str(row.get("group_key") or ""): str(row.get("selected_record_id") or "")
                for row in review["duplicate_resolutions"]
                if isinstance(row, dict)
            }
            for group in applied_validation.get("duplicate_groups", []):
                if isinstance(group, dict):
                    group["selected_record_id"] = duplicate_decisions.get(
                        str(group.get("key") or ""),
                        "",
                    )
            applied_validation.update({
                "applied": True,
                "confirmed": True,
                "confirmed_by": username,
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                "selected_project_no": project_no,
                "selected_project_title": project_title,
                "notes": review["notes"],
                "included_record_count": len(selected_records),
                "excluded_record_count": len(excluded_records),
                "project_excluded_record_count": len(project_excluded_records),
                "duplicate_excluded_record_count": len(duplicate_excluded_records),
            })
            refreshed["source_validation"] = applied_validation
            refreshed["_source_records"] = copy.deepcopy(raw_records)
            refreshed["photo_documentation"] = _photo_references_for_records(
                selected_records,
                previous=draft.get("photo_documentation"),
                limits=photo_limits,
            )
            refreshed["draft_id"] = draft_id
            refreshed["owner"] = username
            refreshed["created_at"] = draft.get(
                "created_at", datetime.now().isoformat(timespec="seconds")
            )
            _update_draft(data_dir, username, refreshed)
            if source_method == "stored_json":
                _prune_draft_photo_assets(
                    data_dir,
                    username,
                    draft_id,
                    refreshed.get("photo_documentation"),
                )
            return jsonify({"ok": True, "draft_id": draft_id, "draft": refreshed})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            app.logger.exception("Source validation failed for report draft %s", draft_id)
            return jsonify({"error": "Source validation failed. Retry or check the server logs."}), 500

    @app.post("/monthly/workforce/timesheet/<draft_id>/preview")
    def preview_monthly_timesheet(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        try:
            _require_applied_source_validation(draft)
            period = draft.get("period") if isinstance(draft.get("period"), dict) else {}
            preview = compile_timesheets(
                _workbook_uploads(),
                start_date=period.get("start"),
                end_date=period.get("end"),
                cutoff_date=request.form.get("cutoff_date") or None,
            )
            set_timesheet_preview(draft, preview, actor=username)
            draft.pop("ai_summary", None)
            _update_draft(data_dir, username, draft)
            if activity_logger:
                activity_logger(username, "periodic_timesheet_reviewed", f"draft={draft_id}")
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except (TimesheetError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            app.logger.exception("Timesheet preview failed for draft %s", draft_id)
            return jsonify({"error": "Timesheet could not be analyzed. Check the workbook format."}), 500

    @app.post("/monthly/workforce/timesheet/<draft_id>/decision")
    def decide_monthly_timesheet(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid timesheet decision."}), 400
        try:
            _require_applied_source_validation(draft)
            decide_timesheet(
                draft,
                str(body.get("decision") or ""),
                confirm_exceptions=bool(body.get("confirm_exceptions")),
                actor=username,
            )
            _refresh_deterministic_summary(draft)
            draft.pop("ai_summary", None)
            _update_draft(data_dir, username, draft)
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/monthly/workforce/overtime/<draft_id>/preview")
    def preview_monthly_overtime(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        try:
            _require_applied_source_validation(draft)
            period = draft.get("period") if isinstance(draft.get("period"), dict) else {}
            preview = parse_overtime_workbooks(
                _workbook_uploads(),
                period_start=period.get("start"),
                period_end=period.get("end"),
            )
            set_overtime_preview(draft, preview, actor=username)
            draft.pop("ai_summary", None)
            _update_draft(data_dir, username, draft)
            if activity_logger:
                activity_logger(username, "periodic_overtime_reviewed", f"draft={draft_id}")
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            app.logger.exception("Overtime preview failed for draft %s", draft_id)
            return jsonify({"error": "Overtime workbook could not be analyzed."}), 500

    @app.post("/monthly/workforce/overtime/<draft_id>/decision")
    def decide_monthly_overtime(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid overtime decision."}), 400
        try:
            _require_applied_source_validation(draft)
            decide_overtime(
                draft,
                str(body.get("decision") or ""),
                resolutions=body.get("resolutions"),
                record_resolutions=body.get("record_resolutions"),
                confirm_exceptions=bool(body.get("confirm_exceptions")),
                actor=username,
            )
            _refresh_deterministic_summary(draft)
            draft.pop("ai_summary", None)
            _update_draft(data_dir, username, draft)
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/monthly/workforce/reset/<draft_id>")
    def reset_monthly_workforce(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        reset_workforce(draft)
        _refresh_deterministic_summary(draft)
        draft.pop("ai_summary", None)
        _update_draft(data_dir, username, draft)
        return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})

    @app.post("/monthly/ai-summary/<draft_id>")
    def generate_monthly_ai_summary(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        if _ai_admin_only() and not bool(session.get("is_admin")):
            return jsonify({"error": "Only an administrator may use the paid AI summary service."}), 403
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        ai_lock: tuple[Path, str] | None = None
        try:
            _require_applied_source_validation(draft)
            if has_pending_workforce_review(draft):
                raise ValueError("Apply or keep every timesheet/overtime preview before generating AI suggestions.")
            ai_lock = _acquire_ai_draft_lock(data_dir, username, draft_id)
            if ai_lock is None:
                return _ai_retry_response(
                    "AI summary generation is already running for this report draft.",
                    code="ai_generation_in_progress",
                    status=409,
                    retry_after=_AI_DRAFT_LOCK_RETRY_SECONDS,
                )

            # Reload after taking the cross-process lock. Another request may
            # have updated validation or workforce decisions while this
            # request was waiting to acquire it.
            draft = _load_draft(data_dir, username, draft_id)
            if draft is None:
                return jsonify({"error": "Report draft not found."}), 404
            _require_applied_source_validation(draft)
            if has_pending_workforce_review(draft):
                raise ValueError("Apply or keep every timesheet/overtime preview before generating AI suggestions.")
            _refresh_deterministic_summary(draft)

            remaining = _ai_cooldown_remaining(draft)
            if remaining:
                return _ai_retry_response(
                    f"Wait {remaining} seconds before retrying AI for this report draft.",
                    code="ai_cooldown_active",
                    status=429,
                    retry_after=remaining,
                )

            # Persist the cooldown before the billable provider request. This
            # prevents rapid retries after timeouts/provider failures and also
            # closes the race between multiple Railway workers.
            started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            draft["ai_request_control"] = {
                "version": "periodic-ai-request-control/1",
                "last_started_at": started_at,
                "last_started_by": username,
                "attempt_id": uuid.uuid4().hex,
            }
            _update_draft(data_dir, username, draft)
            envelope = generate_ai_summary(draft)
            raw = envelope.get("suggestion") if isinstance(envelope.get("suggestion"), dict) else {}
            concerns = []
            concern_evidence = []
            concern_actions = (
                raw.get("concern_actions")
                if isinstance(raw.get("concern_actions"), list)
                else []
            )
            for row in concern_actions[:75]:
                if not isinstance(row, dict):
                    continue
                concern = _clean_text(row.get("concern"), 2_000)
                action = _clean_text(row.get("corrective_action"), 2_000)
                if concern or action:
                    references = _clean_ai_references(row)
                    concerns.append({
                        "concern": concern,
                        "corrective_action": action,
                        **references,
                    })
                    concern_evidence.append(references)
            lookahead = []
            lookahead_evidence = []
            for row in raw.get("lookahead", [])[:75] if isinstance(raw.get("lookahead"), list) else []:
                text = _claim_text(row)
                if text:
                    lookahead.append(text)
                    lookahead_evidence.append(_clean_ai_references(row))
            current_activities = []
            current_activity_evidence = []
            for row in raw.get("current_activities", [])[:75] if isinstance(raw.get("current_activities"), list) else []:
                if not isinstance(row, dict):
                    continue
                text = _claim_text(row)
                area = _clean_text(row.get("area"), 200)
                if text:
                    references = _clean_ai_references(row)
                    current_activities.append({
                        "area": area or "Site",
                        "text": text,
                        **references,
                    })
                    current_activity_evidence.append(references)

            # Preserve deterministic source status even when Claude omits it.
            current_activities = _enrich_activity_statuses(current_activities, draft)

            claim_evidence = [
                _clean_ai_references(row)
                for row in (raw.get("claims", [])[:75] if isinstance(raw.get("claims"), list) else [])
            ]
            citation_evidence = {
                key: _clean_ai_references(raw.get(key))
                for key in (
                    "executive_summary",
                    "engineering_summary",
                    "procurement_summary",
                    "site_summary",
                )
            }
            citation_evidence.update({
                "current_activities": current_activity_evidence,
                "concern_actions": concern_evidence,
                "lookahead": lookahead_evidence,
                "claims": claim_evidence,
            })
            current_engineering = draft.get("engineering") if isinstance(draft.get("engineering"), dict) else {}
            current_procurement = draft.get("procurement") if isinstance(draft.get("procurement"), dict) else {}
            current_site = draft.get("site") if isinstance(draft.get("site"), dict) else {}
            display = {
                # A missing AI section must never make the review look worse
                # than the deterministic draft that existed before AI.
                "executive_summary": _executive_ai_candidate(draft, raw.get("executive_summary")),
                "engineering_summary": _usable_ai_text(raw.get("engineering_summary"))
                or _clean_text(current_engineering.get("summary"), 4_000),
                "procurement_summary": _usable_ai_text(raw.get("procurement_summary"))
                or _clean_text(current_procurement.get("summary"), 4_000),
                "site_summary": _usable_ai_text(raw.get("site_summary"))
                or _clean_text(current_site.get("summary"), 4_000),
                "current_activities": current_activities,
                "concerns": concerns,
                "lookahead": lookahead,
                "citation_evidence": citation_evidence,
                "missing_data": _clean_ai_missing_data(raw.get("missing_data")),
            }
            draft["ai_summary"] = {
                "status": "suggested",
                "requested_at": started_at,
                "requested_by": username,
                "suggestion": display,
                "provider_envelope": envelope,
            }
            _update_draft(data_dir, username, draft)
            if activity_logger:
                activity_logger(username, "periodic_ai_suggestion_generated", f"draft={draft_id} model={envelope.get('model', '')}")
            return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except AISummaryError as exc:
            app.logger.warning("AI summary failed code=%s retryable=%s", exc.code, exc.retryable)
            if exc.code == "rate_limited":
                return _ai_retry_response(
                    str(exc),
                    code="rate_limited",
                    status=429,
                    retry_after=30,
                )
            status = 503 if exc.code in {"missing_api_key", "billing_required"} else 502
            return jsonify({"error": str(exc), "code": exc.code, "retryable": exc.retryable}), status
        except Exception:
            app.logger.exception("AI summary failed for draft %s", draft_id)
            return jsonify({"error": "AI summary failed unexpectedly. The report content is unchanged."}), 500
        finally:
            _release_ai_draft_lock(ai_lock)

    @app.post("/monthly/ai-summary/<draft_id>/decision")
    def decide_monthly_ai_summary(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        state = draft.get("ai_summary") if isinstance(draft.get("ai_summary"), dict) else None
        if state is None or state.get("status") != "suggested":
            return jsonify({"error": "Generate an AI suggestion before saving a decision."}), 400
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("decision") not in {"accept", "reject"}:
            return jsonify({"error": "AI decision must be accept or reject."}), 400
        decision = str(body["decision"])
        if decision == "accept":
            try:
                accepted = _clean_ai_review(body.get("suggestion"))
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            engineering = draft.get("engineering") if isinstance(draft.get("engineering"), dict) else {}
            procurement = draft.get("procurement") if isinstance(draft.get("procurement"), dict) else {}
            site = draft.get("site") if isinstance(draft.get("site"), dict) else {}

            # Accept AI only where it actually improved/provided narrative.
            # Empty/Not supplied values preserve the deterministic draft.
            if accepted["executive_summary"] and accepted["executive_summary"].casefold() != "not supplied":
                draft["executive_summary"] = accepted["executive_summary"]
            ai_meta = {
                "source_type": "ai_narrative",
                "accepted_by": username,
                "accepted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "provider_model": _clean_text(
                    state.get("provider_envelope", {}).get("model")
                    if isinstance(state.get("provider_envelope"), dict) else "",
                    200,
                ),
            }
            citation_evidence = (
                state.get("suggestion", {}).get("citation_evidence")
                if isinstance(state.get("suggestion"), dict) else {}
            )
            if accepted["engineering_summary"] and accepted["engineering_summary"].casefold() != "not supplied":
                engineering["summary"] = accepted["engineering_summary"]
                engineering["narrative_source_meta"] = {
                    **ai_meta,
                    "evidence": copy.deepcopy(citation_evidence.get("engineering_summary", {}))
                    if isinstance(citation_evidence, dict) else {},
                }
            if accepted["procurement_summary"] and accepted["procurement_summary"].casefold() != "not supplied":
                procurement["summary"] = accepted["procurement_summary"]
                procurement["narrative_source_meta"] = {
                    **ai_meta,
                    "evidence": copy.deepcopy(citation_evidence.get("procurement_summary", {}))
                    if isinstance(citation_evidence, dict) else {},
                }
            if accepted["site_summary"] and accepted["site_summary"].casefold() != "not supplied":
                site["summary"] = accepted["site_summary"]
                site["narrative_source_meta"] = {
                    **ai_meta,
                    "evidence": copy.deepcopy(citation_evidence.get("site_summary", {}))
                    if isinstance(citation_evidence, dict) else {},
                }

            # The original deterministic activities remain in draft["activities"].
            # Once explicitly accepted, the site section may use Claude's
            # source-grounded, de-duplicated bullets for the client-facing 5.2 section.
            if accepted["current_activities"]:
                ai_activities = _enrich_activity_statuses(
                    copy.deepcopy(accepted["current_activities"]),
                    draft,
                )
                site["this_month_activities"] = ai_activities
                site["current_period_activities"] = ai_activities
                site["this_period_activities"] = ai_activities
                if _draft_report_type(draft) == "weekly":
                    site["this_week_activities"] = ai_activities

            # AI suggestions may improve wording, but accepting them must not
            # erase deterministic constraints or look-ahead items already
            # extracted from the Daily Reports.
            existing_concerns = site.get("concerns") if isinstance(site.get("concerns"), list) else []
            merged_concerns = _merge_concern_rows(existing_concerns, accepted["concerns"])
            existing_lookahead = site.get("next_period_activities", site.get("next_month_activities", []))
            merged_lookahead = _list_text(existing_lookahead)
            seen_lookahead = {item.casefold() for item in merged_lookahead}
            for item in accepted["lookahead"]:
                if item.casefold() not in seen_lookahead:
                    merged_lookahead.append(item)
                    seen_lookahead.add(item.casefold())
            site["concerns"] = merged_concerns
            site["next_month_activities"] = merged_lookahead
            site["next_period_activities"] = merged_lookahead
            if _draft_report_type(draft) == "weekly":
                site["next_week_activities"] = merged_lookahead
            draft["engineering"] = engineering
            draft["procurement"] = procurement
            draft["site"] = site
            draft["narrative_mode"] = "ai_enhanced"
            state["accepted_values"] = accepted
        elif decision == "reject":
            draft["narrative_mode"] = "deterministic"
        state["status"] = "accepted" if decision == "accept" else "rejected"
        state["decided_by"] = username
        state["decided_at"] = datetime.now().isoformat(timespec="seconds")
        _update_draft(data_dir, username, draft)
        return jsonify({"ok": True, "draft_id": draft_id, "draft": draft})

    @app.get("/monthly/photos/<draft_id>/<asset_id>")
    def get_monthly_draft_photo(draft_id: str, asset_id: str):
        auth = require_login_json()
        if auth:
            return auth
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None or not is_asset_id(asset_id):
            return jsonify({"error": "Photo not found."}), 404
        photos = draft.get("photo_documentation")
        allowed = {
            str(item.get("asset_id") or "")
            for item in (photos if isinstance(photos, list) else []) if isinstance(item, dict)
        }
        if asset_id not in allowed:
            return jsonify({"error": "Photo not found."}), 404
        directory = _draft_photo_dir(data_dir, username, draft_id, create=False)
        path = directory / asset_filename(asset_id) if directory is not None else None
        if path is None or not path.is_file():
            return jsonify({"error": "Photo asset is unavailable."}), 404
        return send_file(
            path,
            mimetype="image/jpeg",
            as_attachment=False,
            conditional=True,
            max_age=3600,
        )

    @app.patch("/monthly/photos/<draft_id>")
    def update_monthly_draft_photos(draft_id: str):
        auth = require_login_json()
        if auth:
            return auth
        if request.content_length is not None and request.content_length > _MAX_PHOTO_REVIEW_BYTES:
            return jsonify({"error": "Photo review request is too large."}), 413
        username = session["username"]
        draft = _load_draft(data_dir, username, draft_id)
        if draft is None:
            return jsonify({"error": "Report draft not found."}), 404
        body = request.get_json(silent=True)
        photos = body.get("photos") if isinstance(body, dict) else None
        if not isinstance(photos, list):
            return jsonify({"error": "photos must be a list."}), 400
        photo_limits = periodic_photo_limits(_draft_report_type(draft))
        if len(photos) > photo_limits.max_images_per_draft:
            return jsonify({
                "error": f"A {_report_name(_draft_report_type(draft))} report may contain at most {photo_limits.max_images_per_draft} photos."
            }), 400

        current = draft.get("photo_documentation")
        current_by_id = {
            str(item.get("asset_id") or ""): item
            for item in (current if isinstance(current, list) else []) if isinstance(item, dict)
            and is_asset_id(item.get("asset_id"))
        }
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(photos):
            if not isinstance(item, dict):
                return jsonify({"error": "Each photo review item must be an object."}), 400
            asset_id = str(item.get("asset_id") or "")
            if asset_id in seen or asset_id not in current_by_id:
                return jsonify({"error": "Photo review contains an unknown or duplicate asset."}), 400
            seen.add(asset_id)
            reference = copy.deepcopy(current_by_id[asset_id])
            reference.pop("data", None)
            reference.pop("path", None)
            reference["caption"] = _clean_text(item.get("caption"), 500)
            reference["order"] = index
            cleaned.append(reference)

        draft["photo_documentation"] = cleaned
        draft["photo_review"] = {
            "confirmed": True,
            "confirmed_by": username,
            "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "count": len(cleaned),
        }
        _update_draft(data_dir, username, draft)
        return jsonify({"ok": True, "count": len(cleaned), "photos": cleaned, "photo_review": draft["photo_review"]})

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
            reviewed = _apply_review(draft, body, actor=session.get("username", ""))
            _refresh_deterministic_summary(reviewed)
            _update_draft(data_dir, session["username"], reviewed)
            buffer = _render(
                reviewed,
                config_provider(),
                photo_base_dir=_draft_photo_dir(
                    data_dir,
                    session["username"],
                    draft_id,
                    create=False,
                ),
            )
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
            reviewed = _apply_review(draft, body, actor=session.get("username", ""))
            _refresh_deterministic_summary(reviewed)
            is_final = reviewed.get("status") == "final"
            preflight = build_report_preflight(reviewed, for_final=is_final)
            preflight = _append_runtime_preflight_blockers(
                preflight,
                data_dir=data_dir,
                username=username,
                draft_id=draft_id,
                report=reviewed,
                for_final=is_final,
            )
            if preflight["blockers"]:
                return jsonify({
                    "error": _preflight_failure_message(preflight),
                    "preflight": preflight,
                }), 400
            if is_final and not bool(body.get("confirm_final")):
                return jsonify({
                    "error": f"Confirm that all warnings, missing dates, and {kind} values were reviewed before saving a Final report.",
                    "preflight": preflight,
                }), 400
            override_reason = _clean_text(
                body.get("final_review_reason")
                or body.get("notes")
                or (reviewed.get("source_validation", {}).get("notes")
                    if isinstance(reviewed.get("source_validation"), dict) else ""),
                2_000,
            )
            if (
                is_final and preflight.get("requires_override_reason")
                and not override_reason and bool(body.get("confirm_final"))
            ):
                # Backward-compatible with the existing Final-confirmation UI:
                # the system still persists an explicit reason and approver.
                override_reason = "Partial Daily Report coverage explicitly confirmed for Final issue."
            if is_final:
                reviewed["final_review"] = {
                    "confirmed": True,
                    "confirmed_by": username,
                    "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "override_reason": override_reason,
                    "preflight": preflight,
                }
            # Draft-local raw sources are needed only while applying project
            # decisions. Do not copy them into the issued report JSON.
            reviewed.pop("_source_records", None)
            index = get_monthly_reports_index(data_dir, username)
            same_period = [row for row in index if (
                (row.get("report_type") or "monthly") == kind
                and row.get("project_no") == reviewed.get("project_no")
                and row.get("period_start") == reviewed.get("period", {}).get("start")
                and row.get("period_end") == reviewed.get("period", {}).get("end")
            )]
            revision = max([int(row.get("revision", 0)) for row in same_period] or [0]) + 1
            prior_final = [row for row in same_period if str(row.get("status") or "").lower() == "final"]
            revision_reason = _clean_text(body.get("revision_reason"), 2_000)
            if (
                is_final
                and prior_final
                and not revision_reason
                and _REQUIRE_FINAL_REVISION_REASON
            ):
                return jsonify({
                    "error": "Revision reason is required when issuing a new Final revision for the same period."
                }), 400
            reviewed["revision_reason"] = revision_reason
            filename = _monthly_filename(reviewed, revision)
            reports_dir = _monthly_user_dir(data_dir, username) / "reports"
            reviewed["revision_rows"] = _revision_history_rows(
                reports_dir, same_period, reviewed, revision
            )
            pdf_path = reports_dir / filename
            json_filename = f"{Path(filename).stem}.json"
            json_path = reports_dir / json_filename
            buffer = _render(
                reviewed,
                config_provider(),
                photo_base_dir=_draft_photo_dir(
                    data_dir,
                    username,
                    draft_id,
                    create=False,
                ),
            )
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
            _atomic_json(json_path, _issued_report_copy(reviewed))
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
                "revision_reason": revision_reason,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "size_kb": round(len(pdf_bytes) / 1024, 1),
                "lifecycle_status": "active",
            }
            if is_final:
                for row in index:
                    if (
                        (row.get("report_type") or "monthly") == kind
                        and row.get("project_no") == entry["project_no"]
                        and row.get("period_start") == entry["period_start"]
                        and row.get("period_end") == entry["period_end"]
                        and str(row.get("status") or "").lower() == "final"
                        and str(row.get("lifecycle_status") or "active") == "active"
                    ):
                        row["lifecycle_status"] = "superseded"
                        row["superseded_by"] = reviewed["monthly_report_id"]
                        row["superseded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
        deleting_final = any(str(row.get("status") or "").lower() == "final" for row in matches)
        if deleting_final and not _ALLOW_FINAL_REPORT_DELETE:
            return jsonify({
                "error": (
                    "Final reports are protected and cannot be deleted while "
                    "PROTECT_FINAL_PERIODIC_REPORTS=true."
                )
            }), 409

        reports_dir = _monthly_user_dir(data_dir, username) / "reports"
        for match in matches:
            for name in (match.get("filename"), match.get("json_filename")):
                if not name or os.path.basename(str(name)) != str(name):
                    continue
                path = reports_dir / str(name)
                if path.is_file():
                    path.unlink()

        remaining_index = [row for row in index if row.get("filename") != basename]

        # When the active/latest Final is hard-deleted during testing, restore the
        # newest remaining Final for the same project/period to "active".  Without
        # this repair, an older revision can remain permanently marked superseded
        # even though the revision that superseded it no longer exists.
        if deleting_final:
            deleted = matches[0]
            kind = str(deleted.get("report_type") or "monthly")
            project_no = deleted.get("project_no")
            period_start = deleted.get("period_start")
            period_end = deleted.get("period_end")
            same_period_finals = [
                row for row in remaining_index
                if (
                    (row.get("report_type") or "monthly") == kind
                    and row.get("project_no") == project_no
                    and row.get("period_start") == period_start
                    and row.get("period_end") == period_end
                    and str(row.get("status") or "").lower() == "final"
                    and str(row.get("lifecycle_status") or "active").lower() != "void"
                )
            ]
            if same_period_finals and not any(
                str(row.get("lifecycle_status") or "active").lower() == "active"
                for row in same_period_finals
            ):
                latest_remaining = max(
                    same_period_finals,
                    key=lambda row: int(row.get("revision", 0) or 0),
                )
                latest_remaining["lifecycle_status"] = "active"
                latest_remaining.pop("superseded_by", None)
                latest_remaining.pop("superseded_at", None)

        _save_monthly_index(data_dir, username, remaining_index)
        if activity_logger:
            report_type = str(matches[0].get("report_type") or "monthly")
            detail = basename
            if deleting_final:
                detail = f"{basename} [FINAL hard-delete sandbox/testing]"
            activity_logger(username, f"{report_type}_report_deleted", detail)
        return jsonify({
            "ok": True,
            "deleted_final": deleting_final,
            "testing_override": bool(deleting_final and _ALLOW_FINAL_REPORT_DELETE),
            "final_protection_enabled": _PROTECT_FINAL_PERIODIC_REPORTS,
        })

    @app.post("/monthly/void")
    def void_monthly_report():
        auth = require_login_json()
        if auth:
            return auth
        body = request.get_json(silent=True) or {}
        filename = str(body.get("filename") or "")
        basename = os.path.basename(filename)
        reason = _clean_text(body.get("reason"), 2_000)
        if not basename or filename != basename:
            return jsonify({"error": "Invalid filename."}), 400
        if not reason:
            return jsonify({"error": "A void reason is required."}), 400
        username = session["username"]
        index = get_monthly_reports_index(data_dir, username)
        matches = [row for row in index if row.get("filename") == basename]
        if not matches:
            return jsonify({"error": "Report not found."}), 404
        if not all(str(row.get("status") or "").lower() == "final" for row in matches):
            return jsonify({"error": "Only Final reports use the void lifecycle action."}), 400
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for row in index:
            if row.get("filename") == basename:
                row["lifecycle_status"] = "void"
                row["void_reason"] = reason
                row["voided_by"] = username
                row["voided_at"] = now
        _save_monthly_index(data_dir, username, index)
        if activity_logger:
            report_type = str(matches[0].get("report_type") or "monthly")
            activity_logger(username, f"{report_type}_report_voided", f"{basename} reason={reason}")
        return jsonify({"ok": True, "filename": basename, "lifecycle_status": "void"})
