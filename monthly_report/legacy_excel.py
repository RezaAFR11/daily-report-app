"""Bounded parser for legacy GPA Daily Report Excel workbooks.

The legacy workbook stores one Daily Report per date-named worksheet. This
module reads the Open XML package directly so an 80+ MB workbook does not need
to be expanded into a large openpyxl object graph. The implementation stays in
one file, with separate internal sections for OOXML access, workbook analysis,
sheet extraction, and embedded photographs.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
import zipfile
from collections import Counter, OrderedDict, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .identity import project_title_match
from .photos import PhotoLimits, _normalise_image, store_photo_candidates


# ---------------------------------------------------------------------------
# Public limits and parser contract
# ---------------------------------------------------------------------------

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


@dataclass(frozen=True)
class LegacyExcelLimits:
    max_file_bytes: int = 128 * 1024 * 1024
    max_entries: int = 5_000
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_xml_member_bytes: int = 32 * 1024 * 1024
    max_worksheets: int = 750


DEFAULT_LIMITS = LegacyExcelLimits()
PARSER_VERSION = "legacy-daily-xlsx/1.0"


class LegacyExcelError(ValueError):
    """Raised when a workbook is unsafe or does not match the legacy format."""


_SHEET_DATE_RE = re.compile(r"^\s*(\d{1,2})[.]([01]?\d)[.](\d{2}|\d{4})\s*$")
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")
_NUMBER_RE = re.compile(r"^\d+(?:[.]0+)?$")
_INDONESIAN_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}
_SECTION_PATTERNS = {
    "weather": re.compile(r"^A[.]\s*WEATHER REPORT$", re.I),
    "activities": re.compile(r"^B[.]\s*ACTIVITY TODAY$", re.I),
    "manpower": re.compile(r"^D[.]\s*INDIRECT MANPOWER$", re.I),
    "constraints": re.compile(r"^F[.]\s*CONSTRAINTS?\s*/\s*PROBLEM", re.I),
    "remarks": re.compile(r"^G[.]\s*REMARKS$", re.I),
    "conclusion": re.compile(r"^H[.]\s*CONCLUSION$", re.I),
    "photos": re.compile(r"^(?:DOCUMENTATION PHOTO|PHOTO DOCUMENTATION)$", re.I),
}
_FIELD_LABELS = {
    "Project No": "project_no",
    "Project Name": "project_title",
    "Customer": "customer",
    "Location / Area": "location",
    "Equipment": "equipment",
}
_PROJECT_FIELDS = (
    "project_no",
    "project_title",
    "customer",
    "location",
    "equipment",
)
_VARIANT_FIELDS = ("project_title", "customer", "location", "equipment")


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _warning(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    field: str = "",
    sheet_name: str = "",
) -> dict[str, str]:
    result = {"code": code, "severity": severity, "message": message}
    if field:
        result["field"] = field
    if sheet_name:
        result["filename"] = sheet_name
    return result


def _clean(value: Any, maximum: int = 4_000) -> str:
    text = " ".join(str(value or "").replace("\ufffd", " ").split())
    return text[:maximum]


# ---------------------------------------------------------------------------
# Resource-bounded OOXML access
# ---------------------------------------------------------------------------

def _safe_member_path(base: str, target: str) -> str:
    if not target:
        raise LegacyExcelError("Workbook relationship target is empty.")
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise LegacyExcelError("Workbook relationship escapes the XLSX package.")
    return resolved


def _read_xml(
    archive: zipfile.ZipFile,
    member: str,
    limits: LegacyExcelLimits,
) -> ET.Element:
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise LegacyExcelError(f"Required workbook member is missing: {member}") from exc
    if info.file_size > limits.max_xml_member_bytes:
        raise LegacyExcelError(f"Workbook XML member is too large: {member}")
    try:
        return ET.fromstring(archive.read(info))
    except (ET.ParseError, OSError, RuntimeError) as exc:
        raise LegacyExcelError(f"Workbook XML is invalid: {member}") from exc


def _validate_archive(
    path: str | os.PathLike[str],
    limits: LegacyExcelLimits,
) -> tuple[Path, zipfile.ZipFile]:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise LegacyExcelError("Uploaded workbook is unavailable.") from exc
    if size <= 0:
        raise LegacyExcelError("Uploaded workbook is empty.")
    if size > limits.max_file_bytes:
        raise LegacyExcelError(
            f"Workbook exceeds the {limits.max_file_bytes // (1024 * 1024)} MB limit."
        )
    if not zipfile.is_zipfile(source):
        raise LegacyExcelError("File is not a valid .xlsx Open XML workbook.")
    try:
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise LegacyExcelError("File is not a readable .xlsx workbook.") from exc
    if len(infos) > limits.max_entries:
        archive.close()
        raise LegacyExcelError("Workbook contains too many package entries.")
    if sum(info.file_size for info in infos) > limits.max_uncompressed_bytes:
        archive.close()
        raise LegacyExcelError("Workbook expands beyond the safe processing limit.")
    return source, archive


def _shared_strings(
    archive: zipfile.ZipFile,
    limits: LegacyExcelLimits,
) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml", limits)
    return [
        "".join(node.text or "" for node in item.iter(_tag(_MAIN_NS, "t")))
        for item in root.findall(_tag(_MAIN_NS, "si"))
    ]


def _relationship_map(root: ET.Element) -> dict[str, str]:
    return {
        str(node.attrib.get("Id") or ""): str(node.attrib.get("Target") or "")
        for node in root.findall(_tag(_PACKAGE_REL_NS, "Relationship"))
        if node.attrib.get("Id") and node.attrib.get("Target")
    }


def _workbook_sheets(
    archive: zipfile.ZipFile,
    limits: LegacyExcelLimits,
) -> list[dict[str, str]]:
    workbook = _read_xml(archive, "xl/workbook.xml", limits)
    relationships = _relationship_map(
        _read_xml(archive, "xl/_rels/workbook.xml.rels", limits)
    )
    sheets_node = workbook.find(_tag(_MAIN_NS, "sheets"))
    if sheets_node is None:
        raise LegacyExcelError("Workbook does not contain any worksheets.")
    result: list[dict[str, str]] = []
    for node in sheets_node:
        relationship_id = node.attrib.get(_tag(_OFFICE_REL_NS, "id"), "")
        target = relationships.get(relationship_id, "")
        if not target:
            continue
        result.append({
            "name": str(node.attrib.get("name") or "").strip(),
            "path": _safe_member_path("xl/workbook.xml", target),
            "state": str(node.attrib.get("state") or "visible"),
        })
    if not result:
        raise LegacyExcelError("Workbook does not contain readable worksheets.")
    if len(result) > limits.max_worksheets:
        raise LegacyExcelError("Workbook contains too many worksheets.")
    return result


def _cell_value(cell: ET.Element, shared: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return _clean("".join(
            node.text or "" for node in cell.iter(_tag(_MAIN_NS, "t"))
        ))
    value = cell.find(_tag(_MAIN_NS, "v"))
    if value is None:
        return ""
    raw = str(value.text or "").strip()
    if cell_type == "s":
        try:
            return _clean(shared[int(raw)])
        except (IndexError, TypeError, ValueError):
            return _clean(raw)
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return _clean(raw)


def _worksheet_rows(
    root: ET.Element,
    shared: Sequence[str],
) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = defaultdict(dict)
    for cell in root.iter(_tag(_MAIN_NS, "c")):
        match = _CELL_REF_RE.fullmatch(str(cell.attrib.get("r") or ""))
        if not match:
            continue
        value = _cell_value(cell, shared)
        if value:
            rows[int(match.group(2))][match.group(1)] = value
    return dict(rows)


# ---------------------------------------------------------------------------
# Worksheet profiling and workbook analysis
# ---------------------------------------------------------------------------

def _sheet_date(value: str) -> date | None:
    match = _SHEET_DATE_RE.fullmatch(str(value or ""))
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _content_date(value: str) -> date | None:
    match = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", str(value or ""))
    if not match:
        return None
    month = _INDONESIAN_MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _section_rows(rows: Mapping[int, Mapping[str, str]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for row_number, row in rows.items():
        for value in row.values():
            text = _clean(value)
            for key, pattern in _SECTION_PATTERNS.items():
                if key not in found and pattern.search(text):
                    found[key] = int(row_number)
    return found


def _field_values(rows: Mapping[int, Mapping[str, str]]) -> dict[str, str]:
    result = {value: "" for value in _FIELD_LABELS.values()}
    for row in rows.values():
        for column, value in row.items():
            field = _FIELD_LABELS.get(_clean(value))
            if not field:
                continue
            value_column = "C" if column == "B" else "I" if column == "H" else ""
            if value_column:
                result[field] = _clean(row.get(value_column, "").lstrip(":"), 500)
    return result


def _day_number(rows: Mapping[int, Mapping[str, str]]) -> str:
    for value in rows.get(10, {}).values():
        match = re.search(r"\bDays?\s*-?\s*(\d+)\b", value, re.I)
        if match:
            return match.group(1)
    return ""


def _sheet_profile(
    name: str,
    path: str,
    rows: Mapping[int, Mapping[str, str]],
) -> dict[str, Any] | None:
    report_date = _sheet_date(name)
    if report_date is None:
        return None
    content_date = _content_date(rows.get(10, {}).get("B", ""))
    sections = _section_rows(rows)
    fields = _field_values(rows)
    warnings: list[dict[str, str]] = []
    if content_date and content_date != report_date:
        warnings.append(_warning(
            "sheet_content_date_mismatch",
            (
                f"Sheet {name} represents {report_date.isoformat()}, but the date inside "
                f"cell B10 is {content_date.isoformat()}. The sheet name is used."
            ),
            field="date",
            sheet_name=name,
        ))
    for key in _SECTION_PATTERNS:
        if key not in sections:
            warnings.append(_warning(
                f"missing_{key}_section",
                f"Sheet {name} does not contain the expected {key} section.",
                severity="error",
                field=key,
                sheet_name=name,
            ))
    return {
        "sheet_name": name,
        "sheet_path": path,
        "date": report_date.isoformat(),
        "content_date": content_date.isoformat() if content_date else "",
        "date_source": "sheet_name",
        "day_no": _day_number(rows),
        "fields": fields,
        "sections": sections,
        "warnings": warnings,
    }


def _dominant(values: Iterable[str]) -> str:
    counts = Counter(_clean(value, 500) for value in values if _clean(value, 500))
    return counts.most_common(1)[0][0] if counts else ""


def _collect_sheet_profiles(
    archive: zipfile.ZipFile,
    limits: LegacyExcelLimits,
) -> tuple[list[dict[str, Any]], list[str]]:
    shared = _shared_strings(archive, limits)
    profiles: list[dict[str, Any]] = []
    ignored: list[str] = []
    for sheet in _workbook_sheets(archive, limits):
        root = _read_xml(archive, sheet["path"], limits)
        profile = _sheet_profile(
            sheet["name"],
            sheet["path"],
            _worksheet_rows(root, shared),
        )
        if profile is None:
            ignored.append(sheet["name"])
        else:
            profiles.append(profile)
    return profiles, ignored


def _analysis_warnings(
    profiles: Sequence[Mapping[str, Any]],
    ignored: Sequence[str],
    duplicates: Sequence[str],
) -> list[dict[str, str]]:
    warnings = [
        dict(item)
        for profile in profiles
        for item in profile.get("warnings", [])
        if isinstance(item, Mapping)
    ]
    if duplicates:
        warnings.append(_warning(
            "duplicate_sheet_dates",
            f"More than one worksheet exists for: {', '.join(duplicates)}.",
            severity="error",
            field="date",
        ))
    if ignored:
        warnings.append(_warning(
            "ignored_non_daily_sheets",
            f"Ignored {len(ignored)} worksheet(s) without a DD.MM.YY date name.",
            severity="info",
            field="sheet_name",
        ))
    return warnings


def _project_manifest(
    profiles: Sequence[Mapping[str, Any]],
    warnings: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    fields = {
        key: _dominant(item["fields"].get(key, "") for item in profiles)
        for key in _PROJECT_FIELDS
    }
    field_variants: dict[str, list[str]] = {}
    for key in _VARIANT_FIELDS:
        variants = sorted({
            item["fields"].get(key, "")
            for item in profiles
            if item["fields"].get(key, "")
        })
        if len(variants) > 1:
            field_variants[key] = variants
            warnings.append(_warning(
                f"mixed_{key}",
                f"Workbook contains {len(variants)} different {key.replace('_', ' ')} values.",
                field=key,
            ))
    return fields, field_variants


def analyze_legacy_daily_workbook(
    source: str | os.PathLike[str],
    *,
    filename: str = "legacy-daily-report.xlsx",
    sha256: str = "",
    limits: LegacyExcelLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Return the selectable date manifest without decoding workbook images."""

    path, archive = _validate_archive(source, limits)
    try:
        profiles, ignored = _collect_sheet_profiles(archive, limits)
    finally:
        archive.close()

    if not profiles:
        raise LegacyExcelError(
            "No date-named Daily Report worksheets were found (expected DD.MM.YY)."
        )
    profiles.sort(key=lambda item: (item["date"], item["sheet_name"]))
    dates = [item["date"] for item in profiles]
    duplicates = sorted(
        date_text for date_text, count in Counter(dates).items() if count > 1
    )
    warnings = _analysis_warnings(profiles, ignored, duplicates)
    fields, field_variants = _project_manifest(profiles, warnings)

    return {
        "schema_version": "legacy-daily-xlsx-analysis/1",
        "parser_version": PARSER_VERSION,
        "filename": os.path.basename(str(filename or path.name)),
        "size_bytes": path.stat().st_size,
        "sha256": sha256,
        "daily_sheet_count": len(profiles),
        "ignored_sheet_count": len(ignored),
        "ignored_sheets": ignored,
        "available_dates": sorted(set(dates)),
        "date_from": min(dates),
        "date_to": max(dates),
        "duplicate_dates": duplicates,
        "project": fields,
        "field_variants": field_variants,
        "warning_count": len(warnings),
        "warnings": warnings,
        "sheets": profiles,
    }


# ---------------------------------------------------------------------------
# Daily sheet content extraction
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return bool(_NUMBER_RE.fullmatch(str(value or "").strip()))


def _normalise_area(value: Any) -> str:
    text = _clean(value, 255)
    folded = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    folded = re.sub(r"\bTURBIN\b", "TURBINE", folded)
    folded = re.sub(r"\bGENERR?ATOR\b", "GENERATOR", folded)
    return folded.title() if folded else "General"


def _looks_like_area(value: Any) -> bool:
    text = _clean(value, 255)
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
        if _is_number(row.get("B")) and _clean(row.get("C")):
            _ensure_area(areas, today_area)["activities_today"].append(
                _clean(row["C"], 2_000)
            )
        if _is_number(row.get("H")) and _clean(row.get("I")):
            _ensure_area(areas, tomorrow_area)["activities_tomorrow"].append(
                _clean(row["I"], 2_000)
            )


def _person(
    row: Mapping[str, str],
    *,
    side: str,
    area: str,
) -> dict[str, Any] | None:
    if side == "indirect":
        number, name, quantity, hours = (
            row.get("B"), row.get("C"), row.get("F"), row.get("G")
        )
    else:
        number, name, quantity, hours = (
            row.get("H"), row.get("I"), row.get("L"), row.get("M")
        )
    if not _is_number(number) or not _clean(name):
        return None
    return {
        "name": _clean(name, 255),
        "role": "",
        "task": "",
        "hours": _clean(hours, 80),
        "quantity": _clean(quantity, 40),
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
        left = _clean(row.get("B"), 255)
        description = _clean(row.get("C"), 2_000)
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
        if _is_number(row.get("B")) and _clean(row.get("C")):
            result.append({
                "item_id": str(int(float(row["B"]))),
                "description": _clean(row["C"], 500),
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
        if "total progress" in _clean(row.get("D")).casefold():
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
            value = _clean(actual.get(column), 80)
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


# ---------------------------------------------------------------------------
# Embedded drawing and photograph extraction
# ---------------------------------------------------------------------------

def _worksheet_relationships_path(sheet_path: str) -> str:
    return posixpath.join(
        posixpath.dirname(sheet_path),
        "_rels",
        posixpath.basename(sheet_path) + ".rels",
    )


def _anchor_position(anchor: ET.Element) -> tuple[int, int]:
    start = anchor.find(_tag(_DRAWING_NS, "from"))
    if start is None:
        return 0, 0
    try:
        row = int(start.findtext(_tag(_DRAWING_NS, "row"), "-1")) + 1
        column = int(start.findtext(_tag(_DRAWING_NS, "col"), "-1")) + 1
    except ValueError:
        return 0, 0
    return row, column


def _drawing_items(
    archive: zipfile.ZipFile,
    sheet_path: str,
    worksheet_root: ET.Element,
    limits: LegacyExcelLimits,
) -> list[dict[str, Any]]:
    drawing = worksheet_root.find(_tag(_MAIN_NS, "drawing"))
    if drawing is None:
        return []
    rels_path = _worksheet_relationships_path(sheet_path)
    if rels_path not in archive.namelist():
        return []
    relationships = _relationship_map(_read_xml(archive, rels_path, limits))
    relationship_id = drawing.attrib.get(_tag(_OFFICE_REL_NS, "id"), "")
    target = relationships.get(relationship_id, "")
    if not target:
        return []
    drawing_path = _safe_member_path(sheet_path, target)
    drawing_root = _read_xml(archive, drawing_path, limits)
    drawing_rels_path = posixpath.join(
        posixpath.dirname(drawing_path),
        "_rels",
        posixpath.basename(drawing_path) + ".rels",
    )
    drawing_relationships = (
        _relationship_map(_read_xml(archive, drawing_rels_path, limits))
        if drawing_rels_path in archive.namelist()
        else {}
    )
    result: list[dict[str, Any]] = []
    for anchor in list(drawing_root):
        row, column = _anchor_position(anchor)
        text = _clean(" ".join(
            node.text or ""
            for node in anchor.iter(_tag(_DRAWING_MAIN_NS, "t"))
        ), 1_000)
        blip = anchor.find(f".//{_tag(_DRAWING_MAIN_NS, 'blip')}")
        media_path = ""
        if blip is not None:
            embedded = blip.attrib.get(_tag(_OFFICE_REL_NS, "embed"), "")
            media_target = drawing_relationships.get(embedded, "")
            if media_target:
                media_path = _safe_member_path(drawing_path, media_target)
        result.append({
            "row": row,
            "column": column,
            "text": text,
            "media_path": media_path,
        })
    return result


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
        and _clean(shape.get("text"))
        and not re.search(
            r"\b(?:prepared|checked|approved)\s+by\b",
            _clean(shape.get("text")),
            re.I,
        )
    ]
    if not captions:
        return source_area, ""
    captions.sort(key=lambda shape: (
        int(shape.get("row") or 0) - image_row,
        abs(int(shape.get("column") or 0) - image_column),
    ))
    return source_area, _clean(captions[0].get("text"), 500)


def _photos(
    archive: zipfile.ZipFile,
    *,
    sheet_path: str,
    worksheet_root: ET.Element,
    photo_start_row: int,
    report_date: str,
    report_id: str,
    source_name: str,
    destination: str | os.PathLike[str],
    photo_limits: PhotoLimits,
    limits: LegacyExcelLimits,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    drawing_items = _drawing_items(archive, sheet_path, worksheet_root, limits)
    shapes = [
        item for item in drawing_items
        if item.get("text") and not item.get("media_path")
    ]
    images = [
        item for item in drawing_items
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
        warnings.append(_warning(
            "excel_photos_excluded",
            f"{source_name}: {excluded} image(s) were excluded by photo safety limits.",
            field="photo_documentation",
            sheet_name=source_name,
        ))
    return references, warnings


# ---------------------------------------------------------------------------
# Canonical record assembly and public extraction API
# ---------------------------------------------------------------------------

def _extract_sheet_content(
    rows: Mapping[int, Mapping[str, str]],
    sections: Mapping[str, int],
) -> tuple[OrderedDict[str, dict[str, Any]], list[dict[str, Any]]]:
    areas: OrderedDict[str, dict[str, Any]] = OrderedDict()
    required = ("activities", "manpower", "constraints", "remarks", "conclusion", "photos")
    if not all(key in sections for key in required):
        return areas, []
    _activities(rows, sections["activities"], sections["manpower"], areas)
    _manpower(rows, sections["manpower"], sections["constraints"], areas)
    _notes(rows, sections["constraints"], sections["remarks"], areas, "constraints")
    _notes(rows, sections["remarks"], sections["conclusion"], areas, "remarks")
    return areas, _progress(rows, sections["conclusion"], sections["photos"])


def _daily_payload(
    *,
    profile: Mapping[str, Any],
    rows: Mapping[int, Mapping[str, str]],
    sections: Mapping[str, int],
    source_fields: Mapping[str, str],
    areas: OrderedDict[str, dict[str, Any]],
    progress: list[dict[str, Any]],
    project_no: str,
    project_title: str,
) -> dict[str, Any]:
    direct_count = sum(len(area["manpower"]) for area in areas.values())
    indirect_count = sum(len(area["indirect_manpower"]) for area in areas.values())
    constraints_count = sum(len(area["constraints"]) for area in areas.values())
    return {
        "date": str(profile.get("date") or ""),
        "day_no": str(profile.get("day_no") or _day_number(rows)),
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


def _source_identity(
    *,
    raw_document_no: str,
    raw_title: str,
    project_no: str,
    project_title: str,
    title_match: Mapping[str, Any],
) -> dict[str, str]:
    # Cell C12 is labelled "Project No" in the legacy template, but its values
    # are daily document numbers. Keep it as document metadata so typing errors
    # cannot split one project into separate groups.
    identity_matches = bool(title_match.get("matched"))
    result = {
        "project_no": project_no if identity_matches else "",
        "project_title": raw_title,
        "reported_project_no": raw_document_no,
        "reported_project_title": raw_title,
        "document_no": raw_document_no,
        "canonical_project_no": project_no if identity_matches else "",
        "canonical_project_title": project_title if identity_matches else "",
        "match_method": _clean(title_match.get("method"), 100)
        if identity_matches else "confirmation_required",
        "review_state": "matched" if identity_matches else "confirmation_required",
    }
    matched_alias = _clean(title_match.get("alias"), 500)
    if identity_matches and matched_alias:
        result["matched_title_alias"] = matched_alias
    return result


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
    approved_title_aliases: Sequence[str],
    asset_directory: str | os.PathLike[str],
    photo_limits: PhotoLimits,
    limits: LegacyExcelLimits,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sheet_name = str(profile.get("sheet_name") or "")
    sheet_path = str(profile.get("sheet_path") or "")
    report_date = str(profile.get("date") or "")
    worksheet_root = _read_xml(archive, sheet_path, limits)
    rows = _worksheet_rows(worksheet_root, shared)
    sections = _section_rows(rows)
    source_fields = _field_values(rows)
    areas, progress = _extract_sheet_content(rows, sections)
    warnings = [
        dict(item)
        for item in profile.get("warnings", [])
        if isinstance(item, Mapping)
    ]

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

    payload = _daily_payload(
        profile=profile,
        rows=rows,
        sections=sections,
        source_fields=source_fields,
        areas=areas,
        progress=progress,
        project_no=project_no,
        project_title=project_title,
    )
    raw_document_no = source_fields.get("project_no", "")
    raw_title = source_fields.get("project_title", "")
    date_mismatch = bool(
        profile.get("content_date") and profile.get("content_date") != report_date
    )
    title_match = project_title_match(
        raw_title,
        project_title,
        approved_aliases=approved_title_aliases,
    )
    identity_matches = bool(title_match.get("matched"))
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
        "source_identity": _source_identity(
            raw_document_no=raw_document_no,
            raw_title=raw_title,
            project_no=project_no,
            project_title=project_title,
            title_match=title_match,
        ),
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
    approved_title_aliases: Iterable[str] = (),
    limits: LegacyExcelLimits = DEFAULT_LIMITS,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Extract selected worksheet dates into canonical Daily Report records."""

    wanted = sorted({_clean(value, 10) for value in selected_dates if _clean(value, 10)})
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
    aliases = tuple(
        alias
        for value in approved_title_aliases
        if (alias := _clean(value, 500))
    )

    _, archive = _validate_archive(source, limits)
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    try:
        shared = _shared_strings(archive, limits)
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
                approved_title_aliases=aliases,
                asset_directory=asset_directory,
                photo_limits=photo_limits,
                limits=limits,
            )
            records.append(record)
            warnings.extend(sheet_warnings)
    finally:
        archive.close()
    return records, warnings


__all__ = [
    "DEFAULT_LIMITS",
    "LegacyExcelError",
    "LegacyExcelLimits",
    "PARSER_VERSION",
    "analyze_legacy_daily_workbook",
    "extract_legacy_daily_records",
]
