"""Low-level, resource-bounded OOXML access for legacy Daily Report workbooks."""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


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
SECTION_PATTERNS = {
    "weather": re.compile(r"^A[.]\s*WEATHER REPORT$", re.I),
    "activities": re.compile(r"^B[.]\s*ACTIVITY TODAY$", re.I),
    "manpower": re.compile(r"^D[.]\s*INDIRECT MANPOWER$", re.I),
    "constraints": re.compile(r"^F[.]\s*CONSTRAINTS?\s*/\s*PROBLEM", re.I),
    "remarks": re.compile(r"^G[.]\s*REMARKS$", re.I),
    "conclusion": re.compile(r"^H[.]\s*CONCLUSION$", re.I),
    "photos": re.compile(r"^(?:DOCUMENTATION PHOTO|PHOTO DOCUMENTATION)$", re.I),
}
FIELD_LABELS = {
    "Project No": "project_no",
    "Project Name": "project_title",
    "Customer": "customer",
    "Location / Area": "location",
    "Equipment": "equipment",
}


def warning(
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


def clean(value: Any, maximum: int = 4_000) -> str:
    text = " ".join(str(value or "").replace("\ufffd", " ").split())
    return text[:maximum]


def identity_text(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", clean(value).casefold()).split())


def safe_member_path(base: str, target: str) -> str:
    if not target:
        raise LegacyExcelError("Workbook relationship target is empty.")
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise LegacyExcelError("Workbook relationship escapes the XLSX package.")
    return resolved


def read_xml(
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


def validate_archive(
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


def shared_strings(
    archive: zipfile.ZipFile,
    limits: LegacyExcelLimits,
) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = read_xml(archive, "xl/sharedStrings.xml", limits)
    return [
        "".join(node.text or "" for node in item.iter(tag(MAIN_NS, "t")))
        for item in root.findall(tag(MAIN_NS, "si"))
    ]


def relationship_map(root: ET.Element) -> dict[str, str]:
    return {
        str(node.attrib.get("Id") or ""): str(node.attrib.get("Target") or "")
        for node in root.findall(tag(PACKAGE_REL_NS, "Relationship"))
        if node.attrib.get("Id") and node.attrib.get("Target")
    }


def workbook_sheets(
    archive: zipfile.ZipFile,
    limits: LegacyExcelLimits,
) -> list[dict[str, str]]:
    workbook = read_xml(archive, "xl/workbook.xml", limits)
    relationships = relationship_map(
        read_xml(archive, "xl/_rels/workbook.xml.rels", limits)
    )
    sheets_node = workbook.find(tag(MAIN_NS, "sheets"))
    if sheets_node is None:
        raise LegacyExcelError("Workbook does not contain any worksheets.")
    result: list[dict[str, str]] = []
    for node in sheets_node:
        relationship_id = node.attrib.get(tag(OFFICE_REL_NS, "id"), "")
        target = relationships.get(relationship_id, "")
        if not target:
            continue
        result.append({
            "name": str(node.attrib.get("name") or "").strip(),
            "path": safe_member_path("xl/workbook.xml", target),
            "state": str(node.attrib.get("state") or "visible"),
        })
    if not result:
        raise LegacyExcelError("Workbook does not contain readable worksheets.")
    if len(result) > limits.max_worksheets:
        raise LegacyExcelError("Workbook contains too many worksheets.")
    return result


def cell_value(cell: ET.Element, shared: Sequence[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return clean("".join(
            node.text or "" for node in cell.iter(tag(MAIN_NS, "t"))
        ))
    value = cell.find(tag(MAIN_NS, "v"))
    if value is None:
        return ""
    raw = str(value.text or "").strip()
    if cell_type == "s":
        try:
            return clean(shared[int(raw)])
        except (IndexError, TypeError, ValueError):
            return clean(raw)
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return clean(raw)


def worksheet_rows(
    root: ET.Element,
    shared: Sequence[str],
) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = defaultdict(dict)
    for cell in root.iter(tag(MAIN_NS, "c")):
        match = _CELL_REF_RE.fullmatch(str(cell.attrib.get("r") or ""))
        if not match:
            continue
        value = cell_value(cell, shared)
        if value:
            rows[int(match.group(2))][match.group(1)] = value
    return dict(rows)


def sheet_date(value: str) -> date | None:
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


def content_date(value: str) -> date | None:
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


def section_rows(rows: Mapping[int, Mapping[str, str]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for row_number, row in rows.items():
        for value in row.values():
            text = clean(value)
            for key, pattern in SECTION_PATTERNS.items():
                if key not in found and pattern.search(text):
                    found[key] = int(row_number)
    return found


def field_values(rows: Mapping[int, Mapping[str, str]]) -> dict[str, str]:
    result = {value: "" for value in FIELD_LABELS.values()}
    for row in rows.values():
        for column, value in row.items():
            field = FIELD_LABELS.get(clean(value))
            if not field:
                continue
            value_column = "C" if column == "B" else "I" if column == "H" else ""
            if value_column:
                result[field] = clean(row.get(value_column, "").lstrip(":"), 500)
    return result


def day_number(rows: Mapping[int, Mapping[str, str]]) -> str:
    for value in rows.get(10, {}).values():
        match = re.search(r"\bDays?\s*-?\s*(\d+)\b", value, re.I)
        if match:
            return match.group(1)
    return ""


def sheet_profile(
    name: str,
    path: str,
    rows: Mapping[int, Mapping[str, str]],
) -> dict[str, Any] | None:
    report_date = sheet_date(name)
    if report_date is None:
        return None
    reported_content_date = content_date(rows.get(10, {}).get("B", ""))
    sections = section_rows(rows)
    fields = field_values(rows)
    warnings: list[dict[str, str]] = []
    if reported_content_date and reported_content_date != report_date:
        warnings.append(warning(
            "sheet_content_date_mismatch",
            (
                f"Sheet {name} represents {report_date.isoformat()}, but the date inside "
                f"cell B10 is {reported_content_date.isoformat()}. The sheet name is used."
            ),
            field="date",
            sheet_name=name,
        ))
    for key in SECTION_PATTERNS:
        if key not in sections:
            warnings.append(warning(
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
        "content_date": reported_content_date.isoformat() if reported_content_date else "",
        "date_source": "sheet_name",
        "day_no": day_number(rows),
        "fields": fields,
        "sections": sections,
        "warnings": warnings,
    }


def dominant(values: Iterable[str]) -> str:
    counts = Counter(clean(value, 500) for value in values if clean(value, 500))
    return counts.most_common(1)[0][0] if counts else ""


def worksheet_relationships_path(sheet_path: str) -> str:
    return posixpath.join(
        posixpath.dirname(sheet_path),
        "_rels",
        posixpath.basename(sheet_path) + ".rels",
    )


def anchor_position(anchor: ET.Element) -> tuple[int, int]:
    start = anchor.find(tag(DRAWING_NS, "from"))
    if start is None:
        return 0, 0
    try:
        row = int(start.findtext(tag(DRAWING_NS, "row"), "-1")) + 1
        column = int(start.findtext(tag(DRAWING_NS, "col"), "-1")) + 1
    except ValueError:
        return 0, 0
    return row, column


def drawing_items(
    archive: zipfile.ZipFile,
    sheet_path: str,
    worksheet_root: ET.Element,
    limits: LegacyExcelLimits,
) -> list[dict[str, Any]]:
    drawing = worksheet_root.find(tag(MAIN_NS, "drawing"))
    if drawing is None:
        return []
    rels_path = worksheet_relationships_path(sheet_path)
    if rels_path not in archive.namelist():
        return []
    relationships = relationship_map(read_xml(archive, rels_path, limits))
    relationship_id = drawing.attrib.get(tag(OFFICE_REL_NS, "id"), "")
    target = relationships.get(relationship_id, "")
    if not target:
        return []
    drawing_path = safe_member_path(sheet_path, target)
    drawing_root = read_xml(archive, drawing_path, limits)
    drawing_rels_path = posixpath.join(
        posixpath.dirname(drawing_path),
        "_rels",
        posixpath.basename(drawing_path) + ".rels",
    )
    drawing_relationships = (
        relationship_map(read_xml(archive, drawing_rels_path, limits))
        if drawing_rels_path in archive.namelist()
        else {}
    )
    result: list[dict[str, Any]] = []
    for anchor in list(drawing_root):
        row, column = anchor_position(anchor)
        text = clean(" ".join(
            node.text or ""
            for node in anchor.iter(tag(DRAWING_MAIN_NS, "t"))
        ), 1_000)
        blip = anchor.find(f".//{tag(DRAWING_MAIN_NS, 'blip')}")
        media_path = ""
        if blip is not None:
            embedded = blip.attrib.get(tag(OFFICE_REL_NS, "embed"), "")
            media_target = drawing_relationships.get(embedded, "")
            if media_target:
                media_path = safe_member_path(drawing_path, media_target)
        result.append({"row": row, "column": column, "text": text, "media_path": media_path})
    return result


__all__ = [
    "DEFAULT_LIMITS",
    "FIELD_LABELS",
    "LegacyExcelError",
    "LegacyExcelLimits",
    "PARSER_VERSION",
    "SECTION_PATTERNS",
    "clean",
    "day_number",
    "dominant",
    "drawing_items",
    "field_values",
    "identity_text",
    "read_xml",
    "section_rows",
    "shared_strings",
    "validate_archive",
    "workbook_sheets",
    "worksheet_rows",
    "warning",
]
