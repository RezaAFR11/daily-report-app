"""Review-first parser for GPA overtime attendance workbooks.

The overtime workbook used by the project is a collection of daily worksheets,
not an employee-by-day matrix.  This module deliberately uses only the Python
standard library to read ``.xlsx`` files, so importing the monthly-report
package does not require a spreadsheet runtime.

Only elapsed (physical) overtime is calculated.  The parser never subtracts a
break and never applies weekend, payroll, or KN wage multipliers.  Ambiguous
records remain visible in the preview and are marked for manual review instead
of being silently guessed.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import unicodedata
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET


FORMULA_VERSION = "kn_overtime_elapsed_v1"
SCHEMA_VERSION = "overtime-preview/1"
MAX_XLSX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_SHEETS = 366
MAX_ROWS_PER_SHEET = 20_000

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"m": _MAIN_NS, "r": _DOC_REL_NS, "pr": _PKG_REL_NS}

_MONTHS = {
    "january": 1,
    "jan": 1,
    "januari": 1,
    "february": 2,
    "feb": 2,
    "februari": 2,
    "march": 3,
    "mar": 3,
    "maret": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "mei": 5,
    "june": 6,
    "jun": 6,
    "juni": 6,
    "july": 7,
    "jul": 7,
    "juli": 7,
    "august": 8,
    "aug": 8,
    "agustus": 8,
    "agu": 8,
    "september": 9,
    "sep": 9,
    "september": 9,
    "october": 10,
    "oct": 10,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "desember": 12,
    "december": 12,
    "dec": 12,
    "des": 12,
}

_DATE_TEXT_RE = re.compile(
    r"\bdate\s*:\s*(?P<day>\d{1,2})\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})(?P<trailing>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ISO_DATE_RE = re.compile(r"\bdate\s*:\s*(?P<iso>\d{4}-\d{1,2}-\d{1,2})(?P<trailing>.*)$", re.I | re.S)
_TIME_RANGE_RE = re.compile(
    r"(?<!\d)(?P<start_hour>\d{1,2})(?:[.:](?P<start_minute>\d{2}))?\s*"
    r"(?:-|\u2013|\u2014|to|s\s*/?\s*d)\s*"
    r"(?P<end_hour>\d{1,2})(?:[.:](?P<end_minute>\d{2}))?(?!\d)",
    re.IGNORECASE,
)

OvertimeSource = (
    str
    | os.PathLike[str]
    | bytes
    | bytearray
    | BinaryIO
    | tuple[str, Any]
    | Mapping[str, Any]
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _number(value: float | int) -> int | float:
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


def _warning(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    filename: str = "",
    sheet: str = "",
    report_date: str = "",
    row: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if filename:
        result["filename"] = filename
    if sheet:
        result["sheet"] = sheet
    if report_date:
        result["date"] = report_date
    if row is not None:
        result["row"] = row
    return result


def _coerce_date(value: str | date | datetime | None, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} must use ISO format YYYY-MM-DD.") from exc


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    if not letters:
        return -1
    result = 0
    for char in letters.group(0).upper():
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


def _read_source(source: OvertimeSource) -> tuple[bytes, str]:
    filename = ""
    if isinstance(source, tuple) and len(source) == 2:
        tuple_name, source = source
        filename = Path(str(tuple_name or "overtime.xlsx")).name
    elif isinstance(source, Mapping):
        filename = Path(str(source.get("filename") or source.get("name") or "overtime.xlsx")).name
        source = source.get("data", source.get("content"))
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_XLSX_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"Unable to read overtime workbook: {exc}") from exc
        if len(data) > MAX_XLSX_BYTES:
            raise ValueError("The overtime workbook exceeds 50 MB.")
        return data, path.name
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if len(data) > MAX_XLSX_BYTES:
            raise ValueError("The overtime workbook exceeds 50 MB.")
        return data, filename or "overtime.xlsx"
    if not hasattr(source, "read"):
        raise TypeError("Overtime source must be a path, bytes, or a binary file object.")
    original_position = None
    try:
        original_position = source.tell()
    except (AttributeError, OSError):
        pass
    data = source.read(MAX_XLSX_BYTES + 1)
    if original_position is not None:
        try:
            source.seek(original_position)
        except (AttributeError, OSError):
            pass
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("Overtime file object must be opened in binary mode.")
    data = bytes(data)
    if len(data) > MAX_XLSX_BYTES:
        raise ValueError("The overtime workbook exceeds 50 MB.")
    stream_name = Path(str(getattr(source, "name", "overtime.xlsx"))).name
    return data, filename or stream_name


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("The .xlsx archive contains too many files.")
    total = 0
    for info in infos:
        path = posixpath.normpath(info.filename.replace("\\", "/"))
        if path.startswith("../") or path == ".." or path.startswith("/"):
            raise ValueError("The .xlsx archive contains an unsafe path.")
        if info.flag_bits & 0x1:
            raise ValueError("Encrypted .xlsx members are not supported.")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("The .xlsx archive contains an oversized member.")
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("The uncompressed .xlsx archive is too large.")
        if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise ValueError("The .xlsx archive has an unsafe compression ratio.")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall("m:si", _NS)
    ]


def _workbook_sheets(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        node.attrib["Id"]: node.attrib["Target"]
        for node in relationships.findall("pr:Relationship", _NS)
    }
    sheets: list[dict[str, str]] = []
    for node in workbook.findall("m:sheets/m:sheet", _NS):
        target = targets.get(node.attrib.get(f"{{{_DOC_REL_NS}}}id", ""), "")
        if target.startswith("/"):
            target = target.lstrip("/")
        else:
            target = posixpath.normpath(posixpath.join("xl", target))
        if target.startswith("../") or target == ".." or target.startswith("/"):
            raise ValueError("Workbook relationship points outside the archive.")
        sheets.append(
            {
                "name": node.attrib.get("name", ""),
                "state": node.attrib.get("state", "visible"),
                "target": target,
            }
        )
    if len(sheets) > MAX_SHEETS:
        raise ValueError(f"Overtime workbook contains more than {MAX_SHEETS} worksheets.")
    return sheets


def _worksheet_rows(
    archive: zipfile.ZipFile,
    target: str,
    shared_strings: list[str],
) -> list[tuple[int, dict[int, str]]]:
    root = ET.fromstring(archive.read(target))
    rows: list[tuple[int, dict[int, str]]] = []
    for row_node in root.findall(".//m:sheetData/m:row", _NS):
        if len(rows) >= MAX_ROWS_PER_SHEET:
            raise ValueError("Overtime worksheet contains too many rows.")
        row_number = int(row_node.attrib.get("r", len(rows) + 1))
        values: dict[int, str] = {}
        for cell in row_node.findall("m:c", _NS):
            column = _column_index(cell.attrib.get("r", ""))
            if column < 0:
                continue
            cell_type = cell.attrib.get("t", "")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.findall(".//m:t", _NS)
                )
            else:
                value_node = cell.find("m:v", _NS)
                if value_node is None:
                    continue
                raw_value = value_node.text or ""
                if cell_type == "s":
                    try:
                        value = shared_strings[int(raw_value)]
                    except (ValueError, IndexError):
                        value = raw_value
                elif cell_type == "b":
                    value = "TRUE" if raw_value == "1" else "FALSE"
                else:
                    value = raw_value
            if _clean(value):
                values[column] = str(value)
        rows.append((row_number, values))
    return rows


def _parse_date_cell(value: str) -> tuple[date | None, str]:
    cleaned = _clean(value)
    iso_match = _ISO_DATE_RE.search(cleaned)
    if iso_match:
        try:
            parsed = date.fromisoformat(iso_match.group("iso"))
        except ValueError:
            return None, ""
        return parsed, _clean(iso_match.group("trailing")).strip("() ")

    match = _DATE_TEXT_RE.search(cleaned)
    if not match:
        return None, ""
    month_name = _normalise(match.group("month"))
    month = _MONTHS.get(month_name)
    if not month:
        return None, ""
    try:
        parsed = date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None, ""
    return parsed, _clean(match.group("trailing")).strip("() ")


def _find_sheet_date(rows: list[tuple[int, dict[int, str]]]) -> tuple[date | None, str, int | None]:
    for row_number, cells in rows:
        for value in cells.values():
            if not re.search(r"\bdate\s*:", value, re.IGNORECASE):
                continue
            parsed, area = _parse_date_cell(value)
            if parsed:
                return parsed, area, row_number
    return None, "", None


def _header_kind(value: str) -> str:
    normalised = _normalise(value)
    if normalised in {"no", "no.", "number", "nomor"}:
        return "number"
    if normalised in {"nama", "name", "employee", "employee name"}:
        return "name"
    if normalised in {"posisi", "position", "role", "jabatan", "role position"}:
        return "role"
    if normalised in {"overtime", "overtime hours", "lembur", "jam lembur"}:
        return "overtime"
    return ""


def _find_header(rows: list[tuple[int, dict[int, str]]]) -> tuple[int | None, dict[str, int]]:
    for row_number, cells in rows:
        mapping: dict[str, int] = {}
        for column, value in cells.items():
            kind = _header_kind(value)
            if kind:
                mapping[kind] = column
        if {"number", "name", "role", "overtime"}.issubset(mapping):
            return row_number, mapping
    return None, {}


def _context_lines(
    rows: list[tuple[int, dict[int, str]]],
    date_row: int | None,
    header_row: int | None,
) -> tuple[list[str], list[str]]:
    if date_row is None or header_row is None:
        return [], []
    description: list[str] = []
    progress: list[str] = []
    for row_number, cells in rows:
        if not (date_row < row_number < header_row):
            continue
        for value in cells.values():
            text = str(value or "").replace("\r", "\n")
            for raw_line in text.split("\n"):
                line = _clean(raw_line)
                if not line:
                    continue
                line = re.sub(r"^description\s+job\s*:\s*", "", line, flags=re.I)
                if not line:
                    continue
                if re.search(r"\b(?:total\s+)?progress\b", line, re.I):
                    progress.append(line)
                else:
                    description.append(line)
    return description, progress


def _parse_time_range(value: str) -> dict[str, Any]:
    raw = _clean(value)
    match = _TIME_RANGE_RE.search(raw)
    if not match:
        return {
            "valid": False,
            "raw": raw,
            "start": "",
            "end": "",
            "notes": raw,
            "duration_hours": None,
            "suggested_duration_hours": None,
            "cross_midnight": False,
            "error": "No supported overtime interval was found.",
        }
    start_hour = int(match.group("start_hour"))
    end_hour = int(match.group("end_hour"))
    start_minute = int(match.group("start_minute") or 0)
    end_minute = int(match.group("end_minute") or 0)
    if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
        return {
            "valid": False,
            "raw": raw,
            "start": "",
            "end": "",
            "notes": raw,
            "duration_hours": None,
            "suggested_duration_hours": None,
            "cross_midnight": False,
            "error": "Overtime interval contains an invalid clock time.",
        }
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    prefix = raw[: match.start()].strip(" -\u2013\u2014;,:|")
    suffix = raw[match.end() :].strip(" -\u2013\u2014;,:|")
    notes = _clean(" ".join(part for part in (prefix, suffix) if part))
    start = f"{start_hour:02d}:{start_minute:02d}"
    end = f"{end_hour:02d}:{end_minute:02d}"
    if end_total < start_total:
        suggested = (24 * 60 - start_total + end_total) / 60
        return {
            "valid": True,
            "raw": raw,
            "start": start,
            "end": end,
            "notes": notes,
            "duration_hours": None,
            "suggested_duration_hours": _number(suggested),
            "cross_midnight": True,
            "error": "Cross-midnight interval requires manual confirmation.",
        }
    if end_total == start_total:
        return {
            "valid": False,
            "raw": raw,
            "start": start,
            "end": end,
            "notes": notes,
            "duration_hours": None,
            "suggested_duration_hours": None,
            "cross_midnight": False,
            "error": "Overtime interval has the same start and end time.",
        }
    return {
        "valid": True,
        "raw": raw,
        "start": start,
        "end": end,
        "notes": notes,
        "duration_hours": _number((end_total - start_total) / 60),
        "suggested_duration_hours": None,
        "cross_midnight": False,
        "error": "",
    }


def _record_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        record.get("date"),
        record.get("employee_key"),
        _normalise(record.get("role")),
        record.get("start"),
        record.get("end"),
        _normalise(record.get("notes")),
    )


def _parse_single_workbook(data: bytes, filename: str, sha256: str) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    sheet_summaries: list[dict[str, Any]] = []
    try:
        archive_context = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{filename} is not a valid .xlsx file.") from exc

    with archive_context as archive:
        _validate_archive(archive)
        required = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
        if not required.issubset(archive.namelist()):
            raise ValueError(f"{filename} is not a supported .xlsx workbook.")
        shared_strings = _shared_strings(archive)
        sheets = _workbook_sheets(archive)
        for sheet_index, sheet in enumerate(sheets, start=1):
            name = sheet["name"]
            state = sheet["state"]
            summary: dict[str, Any] = {
                "name": name,
                "index": sheet_index,
                "state": state,
                "status": "hidden" if state != "visible" else "unread",
                "date": "",
                "area": "",
                "job_description": [],
                "progress": [],
                "row_count": 0,
                "raw_elapsed_hours": 0,
            }
            if state != "visible":
                sheet_summaries.append(summary)
                continue
            try:
                rows = _worksheet_rows(archive, sheet["target"], shared_strings)
            except (KeyError, ET.ParseError) as exc:
                summary["status"] = "invalid"
                warnings.append(
                    _warning(
                        "SHEET_XML_INVALID",
                        f"Worksheet could not be read: {exc}",
                        filename=filename,
                        sheet=name,
                    )
                )
                sheet_summaries.append(summary)
                continue

            report_date, area, date_row = _find_sheet_date(rows)
            header_row, columns = _find_header(rows)
            summary["date"] = report_date.isoformat() if report_date else ""
            summary["area"] = area
            description, progress = _context_lines(rows, date_row, header_row)
            summary["job_description"] = description
            summary["progress"] = progress

            if report_date is None:
                summary["status"] = "invalid"
                warnings.append(
                    _warning(
                        "SHEET_DATE_NOT_FOUND",
                        "No supported Date: value was found in the worksheet.",
                        filename=filename,
                        sheet=name,
                    )
                )
                sheet_summaries.append(summary)
                continue
            report_date_iso = report_date.isoformat()
            if header_row is None:
                summary["status"] = "invalid"
                warnings.append(
                    _warning(
                        "OVERTIME_HEADER_NOT_FOUND",
                        "No No/Nama/Posisi/Overtime header row was found.",
                        filename=filename,
                        sheet=name,
                        report_date=report_date_iso,
                    )
                )
                sheet_summaries.append(summary)
                continue

            sheet_records: list[dict[str, Any]] = []
            for row_number, cells in rows:
                if row_number <= header_row:
                    continue
                number_raw = _clean(cells.get(columns["number"], ""))
                name_raw = _clean(cells.get(columns["name"], ""))
                role_raw = _clean(cells.get(columns["role"], ""))
                overtime_raw = _clean(cells.get(columns["overtime"], ""))
                if not any((number_raw, name_raw, role_raw, overtime_raw)):
                    continue
                # Pre-numbered blank template rows are not attendance records.
                if not name_raw and not overtime_raw:
                    continue
                if not name_raw or not overtime_raw:
                    warnings.append(
                        _warning(
                            "INCOMPLETE_OVERTIME_ROW",
                            "Overtime row is missing an employee name or overtime interval.",
                            filename=filename,
                            sheet=name,
                            report_date=report_date_iso,
                            row=row_number,
                        )
                    )
                    continue

                parsed_range = _parse_time_range(overtime_raw)
                record_id_source = (
                    f"{sha256}\0{name}\0{row_number}\0{report_date_iso}\0"
                    f"{name_raw}\0{overtime_raw}"
                ).encode("utf-8")
                record = {
                    "record_id": hashlib.sha256(record_id_source).hexdigest()[:24],
                    "file_sha256": sha256,
                    "filename": filename,
                    "sheet": name,
                    "sheet_index": sheet_index,
                    "row": row_number,
                    "date": report_date_iso,
                    "area": area,
                    "job_description": description,
                    "progress": progress,
                    "number": number_raw,
                    "employee": name_raw,
                    "employee_key": _normalise(name_raw),
                    "role": role_raw,
                    "overtime_raw": overtime_raw,
                    "start": parsed_range["start"],
                    "end": parsed_range["end"],
                    "notes": parsed_range["notes"],
                    "duration_hours": parsed_range["duration_hours"],
                    "suggested_duration_hours": parsed_range["suggested_duration_hours"],
                    "cross_midnight": parsed_range["cross_midnight"],
                    "requires_review": bool(
                        parsed_range["error"] or parsed_range["notes"] or not role_raw
                    ),
                    "review_reasons": [],
                    "duplicate": False,
                    "duplicate_of_record_id": "",
                    "overlap_conflict": False,
                    "included_in_total": parsed_range["duration_hours"] is not None,
                }
                if parsed_range["error"]:
                    record["review_reasons"].append(parsed_range["error"])
                    warnings.append(
                        _warning(
                            "OVERTIME_INTERVAL_REQUIRES_REVIEW"
                            if parsed_range["valid"]
                            else "OVERTIME_INTERVAL_INVALID",
                            parsed_range["error"],
                            filename=filename,
                            sheet=name,
                            report_date=report_date_iso,
                            row=row_number,
                        )
                    )
                if parsed_range["notes"]:
                    reason = f"Annotated overtime requires review: {parsed_range['notes']}"
                    record["review_reasons"].append(reason)
                    warnings.append(
                        _warning(
                            "ANNOTATED_OVERTIME",
                            reason,
                            filename=filename,
                            sheet=name,
                            report_date=report_date_iso,
                            row=row_number,
                        )
                    )
                if not role_raw:
                    record["review_reasons"].append("Employee position/role is blank.")
                    warnings.append(
                        _warning(
                            "EMPLOYEE_ROLE_MISSING",
                            "Employee position/role is blank.",
                            filename=filename,
                            sheet=name,
                            report_date=report_date_iso,
                            row=row_number,
                        )
                    )
                sheet_records.append(record)

            summary["row_count"] = len(sheet_records)
            summary["raw_elapsed_hours"] = _number(
                sum(float(row["duration_hours"] or 0) for row in sheet_records)
            )
            if sheet_records:
                summary["status"] = "populated"
                records.extend(sheet_records)
            else:
                summary["status"] = "blank_template"
                warnings.append(
                    _warning(
                        "BLANK_OVERTIME_TEMPLATE",
                        "Worksheet has an overtime table but no machine-readable attendance rows; "
                        "it does not count as date coverage.",
                        filename=filename,
                        sheet=name,
                        report_date=report_date_iso,
                    )
                )
            sheet_summaries.append(summary)

    visible = [sheet for sheet in sheet_summaries if sheet["state"] == "visible"]
    populated = [sheet for sheet in visible if sheet["status"] == "populated"]
    blank_templates = [sheet for sheet in visible if sheet["status"] == "blank_template"]
    return {
        "filename": filename,
        "sha256": sha256,
        "size_bytes": len(data),
        "duplicate_file": False,
        "sheet_count": len(sheet_summaries),
        "visible_sheet_count": len(visible),
        "populated_sheet_count": len(populated),
        "blank_template_sheet_count": len(blank_templates),
        "record_count": len(records),
        "raw_elapsed_hours": _number(
            sum(float(record["duration_hours"] or 0) for record in records)
        ),
        "sheets": sheet_summaries,
        "records": records,
        "warnings": warnings,
    }


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _deduplicate_and_validate(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unique: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = _record_identity(record)
        original = seen.get(key)
        if original is None:
            seen[key] = record
            unique.append(record)
            continue
        record["duplicate"] = True
        record["duplicate_of_record_id"] = original["record_id"]
        record["included_in_total"] = False
        record["requires_review"] = True
        record["review_reasons"].append("Exact employee/date/overtime record is duplicated.")
        conflict = {
            "type": "exact_duplicate",
            "date": record["date"],
            "employee": record["employee"],
            "employee_key": record["employee_key"],
            "record_ids": [original["record_id"], record["record_id"]],
            "message": "Exact employee/date/overtime record was counted only once.",
            "requires_manual_review": True,
        }
        conflicts.append(conflict)
        warnings.append(
            _warning(
                "EXACT_OVERTIME_DUPLICATE",
                conflict["message"],
                filename=record["filename"],
                sheet=record["sheet"],
                report_date=record["date"],
                row=record["row"],
            )
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in unique:
        if record["start"] and record["end"] and not record["cross_midnight"]:
            grouped[(record["date"], record["employee_key"])].append(record)
    for (report_date, employee_key), group in grouped.items():
        group.sort(key=lambda row: (_minutes(row["start"]), _minutes(row["end"])))
        for left_index, left in enumerate(group):
            left_start, left_end = _minutes(left["start"]), _minutes(left["end"])
            for right in group[left_index + 1 :]:
                right_start, right_end = _minutes(right["start"]), _minutes(right["end"])
                if right_start >= left_end:
                    break
                if max(left_start, right_start) >= min(left_end, right_end):
                    continue
                for record in (left, right):
                    record["overlap_conflict"] = True
                    record["requires_review"] = True
                    record["included_in_total"] = False
                    reason = "Overlapping overtime intervals require manual resolution."
                    if reason not in record["review_reasons"]:
                        record["review_reasons"].append(reason)
                conflict = {
                    "type": "overlapping_intervals",
                    "date": report_date,
                    "employee": left["employee"],
                    "employee_key": employee_key,
                    "record_ids": [left["record_id"], right["record_id"]],
                    "intervals": [
                        f"{left['start']}-{left['end']}",
                        f"{right['start']}-{right['end']}",
                    ],
                    "message": "Overlapping overtime intervals were excluded from the confirmed total.",
                    "requires_manual_review": True,
                }
                conflicts.append(conflict)
                warnings.append(
                    _warning(
                        "OVERTIME_INTERVAL_OVERLAP",
                        conflict["message"],
                        filename=right["filename"],
                        sheet=right["sheet"],
                        report_date=report_date,
                        row=right["row"],
                    )
                )
    return unique, warnings, conflicts


def _date_sequence(start: date, end: date) -> list[str]:
    result = []
    cursor = start
    while cursor <= end:
        result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _aggregate_daily(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["date"]].append(record)
    daily: list[dict[str, Any]] = []
    for report_date, rows in sorted(grouped.items()):
        included = [row for row in rows if row["included_in_total"]]
        daily.append(
            {
                "date": report_date,
                "sheets": sorted({row["sheet"] for row in rows}),
                "areas": sorted({row["area"] for row in rows if row["area"]}),
                "record_count": len(rows),
                "employee_count": len({row["employee_key"] for row in rows}),
                "confirmed_employee_count": len(
                    {row["employee_key"] for row in included}
                ),
                "raw_elapsed_hours": _number(
                    sum(float(row["duration_hours"] or 0) for row in rows)
                ),
                "confirmed_elapsed_hours": _number(
                    sum(float(row["duration_hours"] or 0) for row in included)
                ),
                "requires_review": any(row["requires_review"] for row in rows),
            }
        )
    return daily


def _aggregate_employees(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["employee_key"]].append(record)
    employees: list[dict[str, Any]] = []
    for employee_key, rows in grouped.items():
        included = [row for row in rows if row["included_in_total"]]
        employees.append(
            {
                "employee": rows[0]["employee"],
                "employee_key": employee_key,
                "roles": sorted({row["role"] for row in rows if row["role"]}),
                "dates": sorted({row["date"] for row in rows}),
                "record_count": len(rows),
                "raw_elapsed_hours": _number(
                    sum(float(row["duration_hours"] or 0) for row in rows)
                ),
                "confirmed_elapsed_hours": _number(
                    sum(float(row["duration_hours"] or 0) for row in included)
                ),
                "requires_review": any(row["requires_review"] for row in rows),
            }
        )
    employees.sort(key=lambda row: row["employee_key"])
    return employees


def parse_overtime_workbooks(
    sources: Iterable[OvertimeSource],
    *,
    period_start: str | date | datetime | None = None,
    period_end: str | date | datetime | None = None,
) -> dict[str, Any]:
    """Parse and combine one or more daily-sheet overtime workbooks.

    ``period_start`` and ``period_end`` are inclusive.  Workbook/file totals
    remain available in ``totals`` while the detailed ``records``, ``daily``,
    and ``employees`` arrays contain only the selected reporting period.
    """

    start = _coerce_date(period_start, "period_start")
    end = _coerce_date(period_end, "period_end")
    if start and end and start > end:
        raise ValueError("period_start cannot be after period_end.")

    manifest_files: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    for source in sources:
        data, filename = _read_source(source)
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 in seen_hashes:
            duplicate_manifest = {
                "filename": filename,
                "sha256": sha256,
                "size_bytes": len(data),
                "duplicate_file": True,
                "duplicate_of": seen_hashes[sha256],
                "sheet_count": 0,
                "visible_sheet_count": 0,
                "populated_sheet_count": 0,
                "blank_template_sheet_count": 0,
                "record_count": 0,
                "raw_elapsed_hours": 0,
                "sheets": [],
            }
            manifest_files.append(duplicate_manifest)
            warnings.append(
                _warning(
                    "DUPLICATE_OVERTIME_FILE",
                    f"File has the same SHA-256 as {seen_hashes[sha256]} and was ignored.",
                    filename=filename,
                )
            )
            continue
        seen_hashes[sha256] = filename
        parsed = _parse_single_workbook(data, filename, sha256)
        raw_records.extend(parsed.pop("records"))
        warnings.extend(parsed.pop("warnings"))
        manifest_files.append(parsed)

    unique_records, validation_warnings, conflicts = _deduplicate_and_validate(raw_records)
    warnings.extend(validation_warnings)
    selected_records = []
    for record in unique_records:
        report_date = date.fromisoformat(record["date"])
        if start and report_date < start:
            continue
        if end and report_date > end:
            continue
        selected_records.append(record)
    selected_raw_records = []
    for record in raw_records:
        report_date = date.fromisoformat(record["date"])
        if start and report_date < start:
            continue
        if end and report_date > end:
            continue
        selected_raw_records.append(record)
    selected_conflicts = []
    for conflict in conflicts:
        conflict_date = str(conflict.get("date") or "")
        if not conflict_date:
            continue
        parsed_conflict_date = date.fromisoformat(conflict_date)
        if start and parsed_conflict_date < start:
            continue
        if end and parsed_conflict_date > end:
            continue
        selected_conflicts.append(conflict)

    all_populated_dates = sorted({record["date"] for record in unique_records})
    selected_populated_dates = sorted({record["date"] for record in selected_records})
    blank_template_dates = sorted(
        {
            sheet["date"]
            for file_row in manifest_files
            if not file_row.get("duplicate_file")
            for sheet in file_row.get("sheets", [])
            if sheet.get("status") == "blank_template" and sheet.get("date")
        }
    )
    selected_blank_template_dates = [
        value
        for value in blank_template_dates
        if (not start or date.fromisoformat(value) >= start)
        and (not end or date.fromisoformat(value) <= end)
    ]
    not_supplied_dates: list[str] = []
    if start and end:
        populated = set(selected_populated_dates)
        not_supplied_dates = [value for value in _date_sequence(start, end) if value not in populated]

    raw_hours = sum(float(record["duration_hours"] or 0) for record in raw_records)
    unique_hours = sum(float(record["duration_hours"] or 0) for record in unique_records)
    confirmed_hours = sum(
        float(record["duration_hours"] or 0)
        for record in unique_records
        if record["included_in_total"]
    )
    selected_unique_hours = sum(
        float(record["duration_hours"] or 0) for record in selected_records
    )
    selected_raw_hours = sum(
        float(record["duration_hours"] or 0) for record in selected_raw_records
    )
    selected_confirmed_hours = sum(
        float(record["duration_hours"] or 0)
        for record in selected_records
        if record["included_in_total"]
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "calculation_policy": {
            "basis": "elapsed physical overtime (end minus start)",
            "break_deduction": False,
            "weekend_multiplier": False,
            "payroll_multiplier": False,
            "cross_midnight_requires_review": True,
        },
        "period": {
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
        },
        "manifest": {
            "files": manifest_files,
            "file_count": len(manifest_files),
            "accepted_file_count": sum(
                1 for row in manifest_files if not row.get("duplicate_file")
            ),
            "duplicate_file_count": sum(
                1 for row in manifest_files if row.get("duplicate_file")
            ),
        },
        "coverage": {
            "all_populated_dates": all_populated_dates,
            "selected_populated_dates": selected_populated_dates,
            "blank_template_dates": blank_template_dates,
            "selected_blank_template_dates": selected_blank_template_dates,
            "not_supplied_dates": not_supplied_dates,
            "blank_templates_count_as_coverage": False,
        },
        "totals": {
            "workbook_sheet_count": sum(
                int(row.get("sheet_count", 0))
                for row in manifest_files
                if not row.get("duplicate_file")
            ),
            "visible_sheet_count": sum(
                int(row.get("visible_sheet_count", 0))
                for row in manifest_files
                if not row.get("duplicate_file")
            ),
            "populated_sheet_count": sum(
                int(row.get("populated_sheet_count", 0))
                for row in manifest_files
                if not row.get("duplicate_file")
            ),
            "blank_template_sheet_count": sum(
                int(row.get("blank_template_sheet_count", 0))
                for row in manifest_files
                if not row.get("duplicate_file")
            ),
            "raw_record_count": len(raw_records),
            "raw_elapsed_hours": _number(raw_hours),
            "unique_record_count": len(unique_records),
            "unique_elapsed_hours": _number(unique_hours),
            "confirmed_elapsed_hours": _number(confirmed_hours),
            "selected_record_count": len(selected_records),
            "selected_raw_record_count": len(selected_raw_records),
            "selected_employee_count": len(
                {record["employee_key"] for record in selected_records}
            ),
            "selected_raw_elapsed_hours": _number(selected_raw_hours),
            "selected_unique_elapsed_hours": _number(selected_unique_hours),
            "selected_confirmed_elapsed_hours": _number(selected_confirmed_hours),
            "workbook_conflict_count": len(conflicts),
            "conflict_count": len(selected_conflicts),
        },
        "records": selected_records,
        "daily": _aggregate_daily(selected_records),
        "employees": _aggregate_employees(selected_records),
        "warnings": warnings,
        "conflicts": selected_conflicts,
        "requires_manual_review": bool(selected_conflicts)
        or any(record["requires_review"] for record in selected_records),
    }


def parse_overtime_workbook(
    source: OvertimeSource,
    *,
    period_start: str | date | datetime | None = None,
    period_end: str | date | datetime | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for parsing one overtime workbook."""

    return parse_overtime_workbooks(
        [source],
        period_start=period_start,
        period_end=period_end,
    )


__all__ = [
    "FORMULA_VERSION",
    "SCHEMA_VERSION",
    "parse_overtime_workbook",
    "parse_overtime_workbooks",
]
