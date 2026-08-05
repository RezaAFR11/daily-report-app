"""Immutable, filesystem-backed canonical daily-report records.

Files are kept below ``DATA_DIR/users/<username>/reports/canonical`` so they
remain on the same persistent volume as the existing report archive.  JSON is
written through a same-directory temporary file and then atomically replaced.
Report identifiers are immutable: an existing record is never overwritten.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


CANONICAL_SCHEMA_VERSION = 1

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_PHOTO_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,255}$")
_DATA_URI_BASE64 = re.compile(
    r"^\s*data:[^,;]+(?:;[^,]*)*;base64,",
    re.IGNORECASE,
)
_PLAIN_BASE64 = re.compile(r"^[A-Za-z0-9+/\r\n]+={0,2}$")
_DROP_INLINE_KEYS = {"img_data", "image_data", "signature_data"}


def _safe_component(value: Any, label: str) -> str:
    component = str(value or "").strip()
    if not _SAFE_COMPONENT.fullmatch(component) or component in {".", ".."}:
        raise ValueError(f"Invalid {label}.")
    return component


def _resolved(path: os.PathLike[str] | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _safe_join(root: Path, *parts: str) -> Path:
    root = root.resolve(strict=False)
    target = root.joinpath(*parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Path escapes the configured data directory.") from exc
    return target


def _canonical_dir(data_dir: os.PathLike[str] | str, username: str) -> Path:
    username = _safe_component(username, "username")
    return _safe_join(_resolved(data_dir), "users", username, "reports", "canonical")


def _validate_report_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("Report date must use YYYY-MM-DD.") from exc


def _normalise_timestamp(value: str | datetime | None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("generated_at must be an ISO-8601 timestamp.") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json_write(path: Path, payload: Mapping[str, Any], *, immutable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        raise FileExistsError(f"Canonical record already exists: {path.name}")

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        if immutable:
            # Creating a hard link is atomic and, unlike os.replace(), cannot
            # overwrite a record created concurrently by another process.
            try:
                os.link(temp_path, path)
            except FileExistsError as exc:
                raise FileExistsError(
                    f"Canonical record already exists: {path.name}"
                ) from exc
            temp_path.unlink()
        else:
            os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _is_inline_base64(value: Any, *, key: str = "") -> bool:
    if not isinstance(value, str):
        return False
    if _DATA_URI_BASE64.match(value):
        return True
    # Signatures in legacy drafts can occasionally be stored without a data URI.
    compact = re.sub(r"\s+", "", value)
    return key == "sig" and len(compact) >= 128 and bool(_PLAIN_BASE64.fullmatch(compact))


def _safe_photo_filename(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        return ""
    if Path(name).name != name or name in {".", ".."} or not _SAFE_PHOTO_NAME.fullmatch(name):
        raise ValueError("Unsafe photo filename.")
    return name


def _source_from_base(base_dir: Path, filename: str) -> Path:
    source = _safe_join(base_dir, filename)
    if not source.is_file():
        raise FileNotFoundError(f"Referenced photo does not exist: {filename}")
    return source


def _copy_hashed_asset(source: Path, assets_dir: Path) -> dict[str, Any]:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(f"Photo source is not a file: {source}")

    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"

    assets_dir.mkdir(parents=True, exist_ok=True)
    temp_path = assets_dir / f".asset.{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as source_file, temp_path.open("xb") as temp_file:
            while True:
                chunk = source_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                temp_file.write(chunk)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        filename = f"{digest.hexdigest()}{suffix}"
        target = _safe_join(assets_dir, filename)
        if target.exists():
            temp_path.unlink()
        else:
            os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    return {
        "sha256": digest.hexdigest(),
        "size_bytes": size,
        "asset_path": f"assets/{filename}",
    }


class _Sanitizer:
    def __init__(
        self,
        canonical_dir: Path,
        *,
        photo_base_dir: os.PathLike[str] | str | None,
        photo_paths: Mapping[str, os.PathLike[str] | str] | None,
    ) -> None:
        self.canonical_dir = canonical_dir
        self.photo_base_dir = _resolved(photo_base_dir) if photo_base_dir is not None else None
        self.photo_paths = dict(photo_paths or {})
        self.assets: dict[tuple[str, str], dict[str, Any]] = {}

        for filename in self.photo_paths:
            _safe_photo_filename(filename)

    def _photo_source(self, filename: str) -> Path | None:
        if filename in self.photo_paths:
            source = _resolved(self.photo_paths[filename])
            if self.photo_base_dir is not None:
                try:
                    source.relative_to(self.photo_base_dir)
                except ValueError as exc:
                    raise ValueError("Photo source escapes photo_base_dir.") from exc
            if not source.is_file():
                raise FileNotFoundError(f"Referenced photo does not exist: {filename}")
            return source
        if self.photo_base_dir is not None:
            return _source_from_base(self.photo_base_dir, filename)
        return None

    def clean(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            cleaned: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key = str(raw_key)
                if key in _DROP_INLINE_KEYS or _is_inline_base64(raw_value, key=key):
                    continue
                # Filesystem paths are accepted only through trusted function args.
                if key == "photo_path":
                    continue
                cleaned[key] = self.clean(raw_value)

            if "photo_filename" in value:
                filename = _safe_photo_filename(value.get("photo_filename"))
                if filename:
                    cleaned["photo_filename"] = filename
                    source = self._photo_source(filename)
                    if source is not None:
                        asset = _copy_hashed_asset(source, self.canonical_dir / "assets")
                        asset["original_name"] = filename
                        cleaned["asset"] = asset
                        self.assets[(asset["sha256"], asset["asset_path"])] = copy.deepcopy(asset)
            return cleaned

        if isinstance(value, list):
            return [self.clean(item) for item in value if not _is_inline_base64(item)]
        if isinstance(value, tuple):
            return [self.clean(item) for item in value if not _is_inline_base64(item)]
        if _is_inline_base64(value):
            return None
        return copy.deepcopy(value)


def _record_metadata(record: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = record.get(key)
    if value not in (None, ""):
        return value
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return payload.get(key, default)
    return default


def _next_revision(
    data_dir: os.PathLike[str] | str,
    username: str,
    *,
    report_date: str,
    project_no: str,
    project_title: str,
) -> int:
    maximum = 0
    # Revisions are project/date revisions, not merely per-user revisions.
    for record in list_canonical_records(data_dir):
        if str(_record_metadata(record, "date")) != report_date:
            continue
        if str(_record_metadata(record, "project_no")).strip().casefold() != project_no.casefold():
            continue
        if str(_record_metadata(record, "project_title")).strip().casefold() != project_title.casefold():
            continue
        try:
            maximum = max(maximum, int(record.get("revision", 0)))
        except (TypeError, ValueError):
            pass
    return maximum + 1


def archive_final_daily_record(
    data_dir: os.PathLike[str] | str,
    username: str,
    payload: Mapping[str, Any],
    *,
    generated_at: str | datetime | None = None,
    revision: int | None = None,
    report_id: str | None = None,
    photo_base_dir: os.PathLike[str] | str | None = None,
    photo_paths: Mapping[str, os.PathLike[str] | str] | None = None,
) -> dict[str, Any]:
    """Archive an immutable canonical JSON record for a generated daily report.

    Inline base64 images/signatures are removed.  If ``photo_base_dir`` or a
    ``photo_paths`` mapping is supplied, referenced photos are copied once to a
    content-addressed asset directory and their hashes are recorded.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping.")

    username = _safe_component(username, "username")
    report_date = _validate_report_date(payload.get("date"))
    generated_at_text = _normalise_timestamp(generated_at)
    project_no = str(payload.get("project_no") or "").strip()
    project_title = str(payload.get("project_title") or "").strip()

    if revision is None:
        revision = _next_revision(
            data_dir,
            username,
            report_date=report_date,
            project_no=project_no,
            project_title=project_title,
        )
    if isinstance(revision, bool):
        raise ValueError("revision must be a positive integer.")
    try:
        revision = int(revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision must be a positive integer.") from exc
    if revision < 1:
        raise ValueError("revision must be a positive integer.")

    if report_id is None:
        report_id = f"{report_date}-r{revision}-{uuid.uuid4().hex}"
    report_id = _safe_component(report_id, "report_id")

    canonical_dir = _canonical_dir(data_dir, username)
    target = _safe_join(canonical_dir, f"{report_id}.json")
    if target.exists():
        raise FileExistsError(f"Canonical record already exists: {target.name}")
    sanitizer = _Sanitizer(
        canonical_dir,
        photo_base_dir=photo_base_dir,
        photo_paths=photo_paths,
    )
    clean_payload = sanitizer.clean(payload)

    record = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "record_type": "final_daily_report",
        "report_id": report_id,
        "username": username,
        "date": report_date,
        "project_no": project_no,
        "project_title": project_title,
        "generated_at": generated_at_text,
        "revision": revision,
        "payload": clean_payload,
        "assets": sorted(
            sanitizer.assets.values(),
            key=lambda item: (item["sha256"], item["asset_path"]),
        ),
    }

    # json.dumps also validates that the sanitized payload is JSON-compatible.
    serialized = json.dumps(record, ensure_ascii=False, allow_nan=False)
    if "base64," in serialized.casefold():
        raise ValueError("Canonical record still contains inline base64 data.")

    _atomic_json_write(target, record, immutable=True)
    return copy.deepcopy(record)


# Explicit alias for callers that describe the archive by its on-disk format.
archive_final_daily_json = archive_final_daily_record


def load_canonical_record(
    data_dir: os.PathLike[str] | str,
    username: str,
    report_id: str,
) -> dict[str, Any]:
    username = _safe_component(username, "username")
    report_id = _safe_component(report_id, "report_id")
    path = _safe_join(_canonical_dir(data_dir, username), f"{report_id}.json")
    with path.open(encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict):
        raise ValueError("Canonical record root must be an object.")
    return record


def _normalised_filter(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split()).casefold()


def list_canonical_records(
    data_dir: os.PathLike[str] | str,
    *,
    username: str | None = None,
    project_no: str | None = None,
    project_title: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """List valid canonical records across one or all user directories."""

    if date_from is not None:
        date_from = _validate_report_date(date_from)
    if date_to is not None:
        date_to = _validate_report_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from cannot be after date_to.")

    wanted_no = _normalised_filter(project_no)
    wanted_title = _normalised_filter(project_title)
    users_root = _safe_join(_resolved(data_dir), "users")
    if username is not None:
        usernames: Iterable[str] = [_safe_component(username, "username")]
    elif users_root.is_dir():
        usernames = sorted(
            child.name
            for child in users_root.iterdir()
            if child.is_dir() and not child.is_symlink() and _SAFE_COMPONENT.fullmatch(child.name)
        )
    else:
        usernames = []

    records: list[dict[str, Any]] = []
    for current_username in usernames:
        canonical_dir = _canonical_dir(data_dir, current_username)
        if not canonical_dir.is_dir():
            continue
        for path in sorted(canonical_dir.glob("*.json")):
            try:
                path.resolve(strict=True).relative_to(canonical_dir.resolve(strict=True))
                with path.open(encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("record_type") != "final_daily_report":
                continue

            try:
                report_date = _validate_report_date(_record_metadata(record, "date"))
            except ValueError:
                continue
            if date_from and report_date < date_from:
                continue
            if date_to and report_date > date_to:
                continue
            if wanted_no is not None and _normalised_filter(
                str(_record_metadata(record, "project_no"))
            ) != wanted_no:
                continue
            if wanted_title is not None and _normalised_filter(
                str(_record_metadata(record, "project_title"))
            ) != wanted_title:
                continue
            records.append(record)

    def sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
        try:
            revision_value = int(record.get("revision", 0))
        except (TypeError, ValueError):
            revision_value = 0
        return (
            str(_record_metadata(record, "date")),
            _normalised_filter(str(_record_metadata(record, "project_no"))) or "",
            str(record.get("username", "")),
            revision_value,
            str(record.get("generated_at", "")),
            str(record.get("report_id", "")),
        )

    records.sort(key=sort_key)
    return records
