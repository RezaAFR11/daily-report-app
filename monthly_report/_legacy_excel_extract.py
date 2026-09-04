"""Canonical record and photo extraction for selected legacy workbook dates."""

from __future__ import annotations

import hashlib
import os
import re
import zipfile
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ._legacy_excel_ooxml import (
    DEFAULT_LIMITS,
    PARSER_VERSION,
    LegacyExcelError,
    LegacyExcelLimits,
    clean,
    day_number,
    drawing_items,
    field_values,
    identity_text,
    read_xml,
    section_rows,
    shared_strings,
    validate_archive,
    warning,
    worksheet_rows,
)
from .photos import PhotoLimits, _normalise_image, store_photo_candidates


_NUMBER_RE = re.compile(r"^\d+(?:[.]0+)?$")


def _is_number(value: Any) -> bool:
    return bool(_NUMBER_RE.fullmatch(str(value or "").strip()))


def _normalise_area(value: Any) -> str:
    text = clean(value, 255)
    folded = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    folded = re.sub(r"\bTURBIN\b", "TURBINE", folded)
    folded = re.sub(r"\bGENERR?ATOR\b", "GENERATOR", folded)
    return folded.title() if folded else "General"


def _looks_like_area(value: Any) -> bool:
    text = clean(value, 255)
    if not text or len(text) > 80:
        return False
    folded = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    return bool(
        re.search(
            r"\b(?:TURBIN(?:E)?|GENERR?ATOR|GENERATOR|INSULATION|ELECTRICAL|MECHANICAL)\b",
            folded,
        )
        or (folded == text and len(folded.split()) <= 6)
    )


def _ensure_area(
    areas: OrderedDict[str, dict[str, Any]],
    value: Any,
) -> dict[str, Any]:
    area_id = _normalise_area(value)
    if area_id not in areas:
        areas[area_id] = {
            "id": area_id,
            "activities_today": [],
            "activities_tomorrow": [],
            "manpower": [],
            "indirect_manpower": [],
            "constraints": [],
            "remarks": [],
        }
    return areas[area_id]


def _activities(
    rows: Mapping[int, Mapping[str, str]],
    start: int,
    end: int,
    areas: OrderedDict[str, dict[str, Any]],
) -> None:
    today_area = "General"
    tomorrow_area = "General"
    for row_number in range(start + 2, end):
        row = rows.get(row_number, {})
        if row.get("C") and not _is_number(row.get("B")) and _looks_like_area(row["C"]):
            today_area = tomorrow_area = _normalise_area(row["C"])
        if row.get("I") and not _is_number(row.get("H")) and _looks_like_area(row["I"]):
            tomorrow_area = _normalise_area(row["I"])
        if _is_number(row.get("B")) and clean(row.get("C")):
            _ensure_area(areas, today_area)["activities_today"].append(
                clean(row["C"], 2_000)
            )
        if _is_number(row.get("H")) and clean(row.get("I")):
            _ensure_area(areas, tomorrow_area)["activities_tomorrow"].append(
                clean(row["I"], 2_000)
            )


def _person(
    row: Mapping[str, str],
    *,
    side: str,
    area: str,
) -> dict[str, Any] | None:
    if side == "indirect":
        number, name, quantity, hours = (
            row.get("B"),
            row.get("C"),
            row.get("F"),
            row.get("G"),
        )
    else:
        number, name, quantity, hours = (
            row.get("H"),
            row.get("I"),
            row.get("L"),
            row.get("M"),
        )
    if not _is_number(number) or not clean(name):
        return None
    return {
        "name": clean(name, 255),
        "role": "",
        "task": "",
        "hours": clean(hours, 80),
        "quantity": clean(quantity, 40),
        "source_area": area,
    }


def _manpower(
    rows: Mapping[int, Mapping[str, str]],
    start: int,
    end: int,
    areas: OrderedDict[str, dict[str, Any]],
) -> None:
    indirect_area = "General"
    direct_area = "General"
    for row_number in range(start + 2, end):
        row = rows.get(row_number, {})
        if row.get("C") and not _is_number(row.get("B")) and _looks_like_area(row["C"]):
            indirect_area = direct_area = _normalise_area(row["C"])
        if row.get("I") and not _is_number(row.get("H")) and _looks_like_area(row["I"]):
            direct_area = _normalise_area(row["I"])
        indirect = _person(row, side="indirect", area=indirect_area)
        if indirect:
            _ensure_area(areas, indirect_area)["indirect_manpower"].append(indirect)
        direct = _person(row, side="direct", area=direct_area)
        if direct:
            _ensure_area(areas, direct_area)["manpower"].append(direct)


def _notes(
    rows: Mapping[int, Mapping[str, str]],
    start: int,
    end: int,
    areas: OrderedDict[str, dict[str, Any]],
    key: str,
) -> None:
    area = "General"
    for row_number in range(start + 1, end):
        row = rows.get(row_number, {})
        left = clean(row.get("B"), 255)
        description = clean(row.get("C"), 2_000)
        if left and not _is_number(left) and _looks_like_area(left):
            area = _normalise_area(left)
            if description:
                _ensure_area(areas, area)[key].append(description)
            continue
        if description and (_is_number(left) or not left):
            _ensure_area(areas, area)[key].append(description)


def _percent(value: Any) -> float | None:
    text = str(value or "").strip().replace("%", "").replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if abs(number) <= 1.5:
        number *= 100.0
    return round(number, 8)


def _progress(
    rows: Mapping[int, Mapping[str, str]],
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    total: dict[str, Any] | None = None
    for row_number in range(start + 1, end):
        row = rows.get(row_number, {})
        if _is_number(row.get("B")) and clean(row.get("C")):
            result.append({
                "item_id": str(int(float(row["B"]))),
                "description": clean(row["C"], 500),
                "duration": "",
                "weight_factor": _percent(row.get("F")),
                "start": "",
                "finish": "",
                "cumulative_previous_plan": _percent(row.get("G")),
                "cumulative_previous_actual": _percent(row.get("H")),
                "this_period_plan": _percent(row.get("I")),
                "this_period_actual": _percent(row.get("J")),
                "cumulative_to_date_plan": _percent(row.get("K")),
                "cumulative_to_date_actual": _percent(row.get("L")),
                "deviation": _percent(row.get("M")),
                "is_total": False,
                "source_period_label": "This Period",
            })
        if "total progress" in clean(row.get("D")).casefold():
            total = {
                "item_id": "TOTAL",
                "description": "OVERALL PROGRESS",
                "duration": "",
                "weight_factor": None,
                "start": "",
                "finish": "",
                "cumulative_previous_plan": _percent(row.get("G")),
                "cumulative_previous_actual": _percent(row.get("H")),
                "this_period_plan": _percent(row.get("I")),
                "this_period_actual": _percent(row.get("J")),
                "cumulative_to_date_plan": _percent(row.get("K")),
                "cumulative_to_date_actual": _percent(row.get("L")),
                "deviation": _percent(row.get("M")),
                "is_total": True,
                "source_period_label": "This Period",
            }
    if total:
        result.append(total)
    return result


def _weather(
    rows: Mapping[int, Mapping[str, str]],
    start: int,
) -> dict[str, str]:
    actual = rows.get(start + 2, {})

    def summary(columns: Sequence[str]) -> str:
        values: list[str] = []
        for column in columns:
            value = clean(actual.get(column), 80)
            if value and value not in values:
                values.append(value)
        return " / ".join(values)

    return {
        key: value
        for key, value in (
            ("morning", summary(("B", "C", "D", "E"))),
            ("afternoon", summary(("F", "G", "H", "I"))),
            ("evening", summary(("J", "K", "L", "M"))),
        )
        if value
    }


def _photo_context(
    image: Mapping[str, Any],
    shapes: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    image_row = int(image.get("row") or 0)
    image_column = int(image.get("column") or 0)
    prior_areas = [
        shape
        for shape in shapes
        if int(shape.get("row") or 0) <= image_row
        and _looks_like_area(shape.get("text"))
    ]
    source_area = _normalise_area(prior_areas[-1]["text"]) if prior_areas else "General"
    captions = [
        shape
        for shape in shapes
        if int(shape.get("row") or 0) >= image_row
        and int(shape.get("row") or 0) - image_row <= 100
        and not _looks_like_area(shape.get("text"))
        and clean(shape.get("text"))
        and not re.search(
            r"\b(?:prepared|checked|approved)\s+by\b",
            clean(shape.get("text")),
            re.I,
        )
    ]
    if not captions:
        return source_area, ""
    captions.sort(key=lambda shape: (
        int(shape.get("row") or 0) - image_row,
        abs(int(shape.get("column") or 0) - image_column),
    ))
    return source_area, clean(captions[0].get("text"), 500)


def _photos(
    archive: zipfile.ZipFile,
    *,
    sheet_path: str,
    worksheet_root,
    photo_start_row: int,
    report_date: str,
    report_id: str,
    source_name: str,
    destination: str | os.PathLike[str],
    photo_limits: PhotoLimits,
    limits: LegacyExcelLimits,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    items = drawing_items(archive, sheet_path, worksheet_root, limits)
    shapes = [item for item in items if item.get("text") and not item.get("media_path")]
    images = [
        item
        for item in items
        if item.get("media_path") and int(item.get("row") or 0) > photo_start_row
    ]
    candidates: list[dict[str, Any]] = []
    skipped = 0
    for image in images:
        media_path = str(image.get("media_path") or "")
        try:
            info = archive.getinfo(media_path)
        except KeyError:
            skipped += 1
            continue
        if info.file_size > photo_limits.max_embedded_image_bytes:
            skipped += 1
            continue
        normalised = _normalise_image(archive.read(info), photo_limits)
        if normalised is None:
            skipped += 1
            continue
        content, width, height = normalised
        source_area, caption = _photo_context(image, shapes)
        candidates.append({
            "asset_id": hashlib.sha256(content).hexdigest(),
            "content": content,
            "source": source_name,
            "width": width,
            "height": height,
            "caption": caption,
            "source_date": report_date,
            "source_area": source_area,
            "source_type": "legacy_excel_extraction",
            "photo_match_method": "worksheet_drawing_anchor",
        })
    references = store_photo_candidates(
        candidates,
        destination,
        source_report_id=report_id,
        maximum=photo_limits.max_images_per_pdf,
        max_total_bytes=photo_limits.max_total_asset_bytes_per_draft,
    )
    warnings: list[dict[str, str]] = []
    excluded = skipped + max(0, len(candidates) - len(references))
    if excluded:
        warnings.append(warning(
            "excel_photos_excluded",
            f"{source_name}: {excluded} image(s) were excluded by photo safety limits.",
            field="photo_documentation",
            sheet_name=source_name,
        ))
    return references, warnings


def _record(
    *,
    archive: zipfile.ZipFile,
    shared: Sequence[str],
    profile: Mapping[str, Any],
    workbook_filename: str,
    workbook_sha256: str,
    username: str,
    project_no: str,
    project_title: str,
    asset_directory: str | os.PathLike[str],
    photo_limits: PhotoLimits,
    limits: LegacyExcelLimits,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sheet_name = str(profile.get("sheet_name") or "")
    sheet_path = str(profile.get("sheet_path") or "")
    report_date = str(profile.get("date") or "")
    worksheet_root = read_xml(archive, sheet_path, limits)
    rows = worksheet_rows(worksheet_root, shared)
    sections = section_rows(rows)
    source_fields = field_values(rows)
    areas: OrderedDict[str, dict[str, Any]] = OrderedDict()
    warnings = [
        dict(item)
        for item in profile.get("warnings", [])
        if isinstance(item, Mapping)
    ]

    required = ("activities", "manpower", "constraints", "remarks", "conclusion", "photos")
    if all(key in sections for key in required):
        _activities(rows, sections["activities"], sections["manpower"], areas)
        _manpower(rows, sections["manpower"], sections["constraints"], areas)
        _notes(rows, sections["constraints"], sections["remarks"], areas, "constraints")
        _notes(rows, sections["remarks"], sections["conclusion"], areas, "remarks")
        progress = _progress(rows, sections["conclusion"], sections["photos"])
    else:
        progress = []

    workbook_key = workbook_sha256 or hashlib.sha256(
        workbook_filename.encode("utf-8")
    ).hexdigest()
    report_id = f"xlsx-{workbook_key[:32]}-{report_date.replace('-', '')}"
    photo_references: list[dict[str, Any]] = []
    if "photos" in sections:
        photo_references, photo_warnings = _photos(
            archive,
            sheet_path=sheet_path,
            worksheet_root=worksheet_root,
            photo_start_row=sections["photos"],
            report_date=report_date,
            report_id=report_id,
            source_name=f"{workbook_filename}#{sheet_name}",
            destination=asset_directory,
            photo_limits=photo_limits,
            limits=limits,
        )
        warnings.extend(photo_warnings)

    direct_count = sum(len(area["manpower"]) for area in areas.values())
    indirect_count = sum(len(area["indirect_manpower"]) for area in areas.values())
    constraints_count = sum(len(area["constraints"]) for area in areas.values())
    payload = {
        "date": report_date,
        "day_no": str(profile.get("day_no") or day_number(rows)),
        "layout_profile": "legacy_excel_turbine_generator",
        "project_no": project_no,
        "project_title": project_title,
        "location": source_fields.get("location", ""),
        "customer": source_fields.get("customer", ""),
        "equipment": source_fields.get("equipment", ""),
        "weather": _weather(rows, sections["weather"]) if "weather" in sections else {},
        "constraint_status": "reported" if constraints_count else "not_supplied",
        "indirect_manpower": [],
        "manpower_status": "reported" if direct_count or indirect_count else "not_supplied",
        "show_overall_progress": bool(progress),
        "overall_progress": progress,
        "areas": list(areas.values()),
        "global_constraints": "",
        "global_remarks": "",
        "sign_offs": [],
    }
    raw_document_no = source_fields.get("project_no", "")
    raw_title = source_fields.get("project_title", "")
    date_mismatch = bool(
        profile.get("content_date") and profile.get("content_date") != report_date
    )
    identity_matches = bool(
        raw_title
        and project_title
        and identity_text(raw_title) == identity_text(project_title)
    )
    record = {
        "record_type": "final_daily_report",
        "report_id": report_id,
        "revision": 1,
        "username": username,
        "date": report_date,
        "project_no": project_no,
        "project_title": project_title,
        "generated_at": "",
        "payload": payload,
        "source": {
            "method": "uploaded_excel",
            "filename": f"{workbook_filename}#{sheet_name}",
            "workbook_filename": workbook_filename,
            "sheet_name": sheet_name,
            "sha256": hashlib.sha256(
                f"{workbook_key}:{sheet_name}".encode("utf-8")
            ).hexdigest(),
            "workbook_sha256": workbook_sha256,
            "parser_version": PARSER_VERSION,
        },
        "confidence": {
            "overall": 0.95 if not date_mismatch else 0.82,
            "critical_complete": bool(report_date and raw_title),
            "fields": {"date": 1.0, "project_title": 1.0 if raw_title else 0.0},
        },
        "import_status": "ready",
        "review_required": date_mismatch
        or not identity_matches
        or any(
            str(item.get("severity") or "").casefold() == "error"
            for item in warnings
        ),
        "source_identity": {
            # Cell C12 is labelled "Project No" in the legacy template, but its
            # values are daily document numbers (and a few contain old typing
            # mistakes). Keep that value as document metadata so a malformed
            # document number cannot split one project into separate groups.
            "project_no": project_no if identity_matches else "",
            "project_title": raw_title,
            "reported_project_no": raw_document_no,
            "reported_project_title": raw_title,
            "document_no": raw_document_no,
            "canonical_project_no": project_no if identity_matches else "",
            "canonical_project_title": project_title if identity_matches else "",
            "match_method": "title_token_equivalent"
            if identity_matches
            else "confirmation_required",
            "review_state": "matched" if identity_matches else "confirmation_required",
        },
        "_photo_candidates": photo_references,
    }
    return record, warnings


def extract_legacy_daily_records(
    source: str | os.PathLike[str],
    *,
    analysis: Mapping[str, Any],
    selected_dates: Iterable[str],
    username: str,
    project_no: str,
    project_title: str,
    asset_directory: str | os.PathLike[str],
    photo_limits: PhotoLimits,
    limits: LegacyExcelLimits = DEFAULT_LIMITS,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract selected worksheet dates into canonical Daily Report records."""

    wanted = sorted({clean(value, 10) for value in selected_dates if clean(value, 10)})
    profiles = {
        str(item.get("date") or ""): item
        for item in analysis.get("sheets", [])
        if isinstance(item, Mapping) and item.get("date")
    }
    unknown = [value for value in wanted if value not in profiles]
    if unknown:
        raise LegacyExcelError(
            f"Selected date(s) are not available in the workbook: {', '.join(unknown)}"
        )
    if not wanted:
        raise LegacyExcelError("Choose at least one workbook date to compile.")

    _, archive = validate_archive(source, limits)
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    try:
        shared = shared_strings(archive, limits)
        for report_date in wanted:
            record, sheet_warnings = _record(
                archive=archive,
                shared=shared,
                profile=profiles[report_date],
                workbook_filename=str(
                    analysis.get("filename") or "legacy-daily-report.xlsx"
                ),
                workbook_sha256=str(analysis.get("sha256") or ""),
                username=username,
                project_no=project_no,
                project_title=project_title,
                asset_directory=asset_directory,
                photo_limits=photo_limits,
                limits=limits,
            )
            records.append(record)
            warnings.extend(sheet_warnings)
    finally:
        archive.close()
    return records, warnings


__all__ = ["extract_legacy_daily_records"]
