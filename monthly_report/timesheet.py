"""Parse KN/GPA attendance workbooks into a reviewable manpower preview.

This module intentionally uses only the Python standard library.  An ``.xlsx``
file is an OOXML ZIP archive, so the small reader below is enough for the
attendance matrix while avoiding a runtime dependency on a spreadsheet suite.

Public entry points
-------------------
``parse_timesheet_xlsx`` parses one workbook. ``compile_timesheets`` combines
one or more workbooks, removes byte-identical uploads by SHA-256, and reports
employee/date conflicts instead of guessing.  Both return JSON-safe mappings.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import hashlib
import io
import os
import posixpath
import re
import unicodedata
from xml.etree import ElementTree as ET
import zipfile


FORMULA_VERSION = "kn_attendance_v1_10h"
PHYSICAL_HOURS_PER_PRESENT_DAY = 10
MAX_XLSX_BYTES = 50 * 1024 * 1024
MAX_SHEETS = 100
MAX_CELLS_PER_SHEET = 500_000
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"m": _MAIN_NS, "r": _REL_NS}
_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_RANGE_RE = re.compile(r"^([A-Z]+[1-9][0-9]*):([A-Z]+[1-9][0-9]*)$")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "januari": 1,
    "feb": 2,
    "february": 2,
    "februari": 2,
    "mar": 3,
    "march": 3,
    "maret": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mei": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "agustus": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "okt": 10,
    "oct": 10,
    "october": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
    "des": 12,
    "desember": 12,
}


class TimesheetError(ValueError):
    """Raised when an upload is not a supported attendance workbook."""


def _warning(code: str, message: str, **details) -> dict:
    item = {"code": code, "message": message}
    if details:
        item["details"] = details
    return item


def _column_number(letters: str) -> int:
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def _cell_coordinates(reference: str) -> tuple[int, int]:
    match = _CELL_REF_RE.match(reference.upper())
    if not match:
        raise TimesheetError(f"Invalid OOXML cell reference: {reference}")
    return int(match.group(2)), _column_number(match.group(1))


def _clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()


def _semantic(value) -> str:
    text = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _employee_key(value) -> str:
    # Deliberately conservative: spelling and punctuation differences are not
    # fuzzy-merged.  Only Unicode, case, and whitespace are normalised.
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", _clean_text(value)).casefold(),
    ).strip()


def _role_key(value) -> str:
    return _employee_key(value)


def _parse_requested_date(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean_text(value)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise TimesheetError(f"{field} must use YYYY-MM-DD") from exc


def _dates_in_text(value: str) -> list[date]:
    text = _clean_text(value)
    found: list[tuple[int, date]] = []

    numeric = re.compile(r"(?<!\d)(\d{1,4})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,4})(?!\d)")
    for match in numeric.finditer(text):
        first, middle, last = match.groups()
        try:
            if len(first) == 4:
                parsed = date(int(first), int(middle), int(last))
            else:
                parsed = date(int(last), int(middle), int(first))
        except ValueError:
            continue
        found.append((match.start(), parsed))

    month_pattern = "|".join(sorted((re.escape(key) for key in _MONTHS), key=len, reverse=True))
    words = re.compile(
        rf"(?<!\d)(\d{{1,2}})\s+({month_pattern})\.?\s+(\d{{4}})(?!\d)",
        re.IGNORECASE,
    )
    for match in words.finditer(text):
        try:
            parsed = date(
                int(match.group(3)),
                _MONTHS[match.group(2).casefold().rstrip(".")],
                int(match.group(1)),
            )
        except (KeyError, ValueError):
            continue
        found.append((match.start(), parsed))

    found.sort(key=lambda item: item[0])
    return [parsed for _, parsed in found]


def normalize_attendance_status(value) -> str:
    """Return a strict canonical attendance status.

    Only the literal/numeric value ``1`` is present.  A blank cell is missing,
    not absent; every other non-present marker has zero physical manhours.
    """

    raw = _clean_text(value)
    if not raw:
        return "missing"
    upper = raw.upper()
    if upper == "1":
        return "present"
    if upper in {"C", "CUTI", "LEAVE", "L"}:
        return "leave"
    if upper in {"I", "IZIN", "IJIN", "PERMISSION"}:
        return "permission"
    if upper in {"S", "SAKIT", "SICK"}:
        return "sick"
    if upper in {"RESIGN", "RISEGN"}:
        return "resigned"
    return "nonpresent"


def _known_nonpresent(value: str) -> bool:
    return _clean_text(value).upper() in {
        "0",
        "X",
        "✗",
        "ABSENT",
        "A",
        "-",
        "N/A",
        "NA",
    }


def _read_source(source, filename: str | None = None) -> tuple[str, bytes]:
    if isinstance(source, tuple) and len(source) == 2:
        tuple_name, source = source
        filename = filename or str(tuple_name)
    elif isinstance(source, dict):
        filename = filename or source.get("filename") or source.get("name")
        source = source.get("data", source.get("content"))

    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        filename = filename or os.path.basename(path)
        try:
            with open(path, "rb") as handle:
                payload = handle.read(MAX_XLSX_BYTES + 1)
        except OSError as exc:
            raise TimesheetError(f"Unable to read timesheet: {exc}") from exc
    elif isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
    elif hasattr(source, "read"):
        filename = filename or os.path.basename(getattr(source, "name", ""))
        position = None
        try:
            position = source.tell()
        except (AttributeError, OSError):
            pass
        payload = source.read(MAX_XLSX_BYTES + 1)
        if isinstance(payload, str):
            raise TimesheetError("Timesheet stream must be opened in binary mode")
        payload = bytes(payload)
        if position is not None:
            try:
                source.seek(position)
            except (AttributeError, OSError):
                pass
    else:
        raise TimesheetError("Timesheet source must be a path, bytes, or binary stream")

    filename = _clean_text(filename)
    if not filename or not filename.casefold().endswith(".xlsx"):
        raise TimesheetError("Only .xlsx attendance workbooks are supported")
    if not payload:
        raise TimesheetError("The uploaded .xlsx file is empty")
    if len(payload) > MAX_XLSX_BYTES:
        raise TimesheetError("The uploaded .xlsx file exceeds 50 MB")
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise TimesheetError("The uploaded file is not a valid .xlsx workbook")
    return filename, payload


def _archive_path(base: str, target: str) -> str:
    if target.startswith("/"):
        result = target.lstrip("/")
    else:
        result = posixpath.normpath(posixpath.join(posixpath.dirname(base), target))
    if result.startswith("../") or result == "..":
        raise TimesheetError("Workbook relationship points outside the archive")
    return result


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise TimesheetError("The .xlsx archive contains too many files")
    total = 0
    for info in infos:
        path = posixpath.normpath(info.filename.replace("\\", "/"))
        if path.startswith("../") or path == ".." or path.startswith("/"):
            raise TimesheetError("The .xlsx archive contains an unsafe path")
        if info.flag_bits & 0x1:
            raise TimesheetError("Encrypted .xlsx members are not supported")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise TimesheetError("The .xlsx archive contains an oversized member")
        total += info.file_size
        if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise TimesheetError("The uncompressed .xlsx archive is too large")
        if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise TimesheetError("The .xlsx archive has an unsafe compression ratio")


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ET.ParseError as exc:
        raise TimesheetError("Invalid shared strings in .xlsx workbook") from exc
    values = []
    for item in root.findall("m:si", _NS):
        values.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))
    return values


def _workbook_sheets(archive: zipfile.ZipFile) -> list[dict]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise TimesheetError("Invalid .xlsx workbook structure") from exc

    relation_targets = {
        item.attrib.get("Id"): item.attrib.get("Target")
        for item in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
        if item.attrib.get("TargetMode") != "External"
    }
    sheets = []
    sheet_nodes = workbook.find("m:sheets", _NS)
    if sheet_nodes is None:
        raise TimesheetError("The workbook does not contain worksheets")
    for node in sheet_nodes:
        relationship_id = node.attrib.get(f"{{{_REL_NS}}}id")
        target = relation_targets.get(relationship_id)
        if not target:
            continue
        sheets.append(
            {
                "name": node.attrib.get("name", "Sheet"),
                "state": node.attrib.get("state", "visible"),
                "path": _archive_path("xl/workbook.xml", target),
            }
        )
    if len(sheets) > MAX_SHEETS:
        raise TimesheetError(f"Workbook contains more than {MAX_SHEETS} worksheets")
    return sheets


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t"))
    value_node = cell.find("m:v", _NS)
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError) as exc:
            raise TimesheetError("Workbook contains an invalid shared-string reference") from exc
    return value


class _SheetGrid:
    def __init__(self, name: str, cells: dict, merges: dict):
        self.name = name
        self.cells = cells
        self.merges = merges
        self.max_row = max((row for row, _ in cells), default=0)
        self.max_col = max((column for _, column in cells), default=0)

    def value(self, row: int, column: int) -> str:
        anchor = self.merges.get((row, column), (row, column))
        return self.cells.get(anchor, "")

    def row_values(self, row: int) -> list[tuple[int, str]]:
        return [
            (column, self.value(row, column))
            for column in range(1, self.max_col + 1)
            if self.value(row, column) != ""
        ]


def _read_sheet(archive: zipfile.ZipFile, descriptor: dict, shared: list[str]) -> _SheetGrid:
    try:
        root = ET.fromstring(archive.read(descriptor["path"]))
    except (KeyError, ET.ParseError) as exc:
        raise TimesheetError(f"Invalid worksheet: {descriptor['name']}") from exc

    cells: dict[tuple[int, int], str] = {}
    for cell in root.findall(".//m:sheetData/m:row/m:c", _NS):
        reference = cell.attrib.get("r")
        if not reference:
            continue
        coordinates = _cell_coordinates(reference)
        cells[coordinates] = _cell_text(cell, shared)
        if len(cells) > MAX_CELLS_PER_SHEET:
            raise TimesheetError(
                f"Worksheet {descriptor['name']} contains more than {MAX_CELLS_PER_SHEET} cells"
            )

    merges: dict[tuple[int, int], tuple[int, int]] = {}
    merge_root = root.find("m:mergeCells", _NS)
    if merge_root is not None:
        for merge in merge_root:
            reference = merge.attrib.get("ref", "")
            match = _RANGE_RE.match(reference.upper())
            if not match:
                continue
            start_row, start_col = _cell_coordinates(match.group(1))
            end_row, end_col = _cell_coordinates(match.group(2))
            if end_row - start_row > 1000 or end_col - start_col > 1000:
                continue
            for row in range(start_row, end_row + 1):
                for column in range(start_col, end_col + 1):
                    merges[(row, column)] = (start_row, start_col)
    return _SheetGrid(descriptor["name"], cells, merges)


def _sheet_period(grid: _SheetGrid) -> tuple[date, date] | None:
    for row in range(1, min(grid.max_row, 100) + 1):
        values = grid.row_values(row)
        for column, value in values:
            if "period" not in _semantic(value) and "periode" not in _semantic(value):
                continue
            context = " ".join(grid.value(row, item) for item in range(column, grid.max_col + 1))
            parsed = _dates_in_text(context)
            if len(parsed) >= 2:
                start, end = parsed[0], parsed[1]
                if start > end:
                    raise TimesheetError(f"Worksheet {grid.name} has an inverted PERIOD range")
                return start, end

    # Some templates put PERIOD and its value in separate merged rows.  The
    # fallback stays conservative and only accepts exactly two visible dates.
    all_text = " ".join(value for row in range(1, min(grid.max_row, 100) + 1) for _, value in grid.row_values(row))
    parsed = _dates_in_text(all_text)
    if len(parsed) == 2 and parsed[0] <= parsed[1]:
        return parsed[0], parsed[1]
    return None


def _find_table(grid: _SheetGrid, period: tuple[date, date]) -> dict | None:
    start, end = period
    expected_days = (end - start).days + 1
    for header_row in range(1, min(grid.max_row, 150) + 1):
        semantics = {column: _semantic(value) for column, value in grid.row_values(header_row)}
        name_columns = [
            column
            for column, value in semantics.items()
            if value in {"full name", "employee name", "nama lengkap", "nama", "name"}
        ]
        role_columns = [
            column
            for column, value in semantics.items()
            if value in {"position", "role position", "jabatan", "role"}
        ]
        if not name_columns or not role_columns:
            continue
        name_column, role_column = name_columns[0], role_columns[0]

        best = None
        for day_row in range(header_row, min(header_row + 4, grid.max_row) + 1):
            day_cells = []
            for column, value in grid.row_values(day_row):
                if column <= role_column:
                    continue
                cleaned = _clean_text(value)
                if re.fullmatch(r"\d{1,2}(?:\.0+)?", cleaned):
                    day_number = int(float(cleaned))
                    if 1 <= day_number <= 31:
                        day_cells.append((column, day_number))
            day_cells.sort()
            for index, (_, day_number) in enumerate(day_cells):
                if day_number != start.day:
                    continue
                candidate = day_cells[index : index + expected_days]
                matches = sum(
                    1
                    for offset, (_, shown_day) in enumerate(candidate)
                    if shown_day == (start + timedelta(days=offset)).day
                )
                score = (matches, len(candidate))
                if best is None or score > best[0]:
                    best = (score, day_row, candidate)
        if best:
            _, day_row, cells = best
            mappings = [
                {"column": column, "date": start + timedelta(days=index), "shown_day": shown}
                for index, (column, shown) in enumerate(cells)
            ]
            return {
                "header_row": header_row,
                "day_row": day_row,
                "name_column": name_column,
                "role_column": role_column,
                "dates": mappings,
                "expected_days": expected_days,
            }
    return None


def _parse_sheet(
    grid: _SheetGrid,
    *,
    source_id: str,
    requested_start: date | None,
    requested_end: date | None,
    cutoff: date | None,
) -> dict:
    warnings: list[dict] = []
    unresolved: list[dict] = []
    period = _sheet_period(grid)
    if not period:
        return {
            "status": "skipped",
            "reason": "period_not_found",
            "entries": [],
            "warnings": [
                _warning(
                    "period_not_found",
                    "Visible worksheet was skipped because its PERIOD range was not found.",
                    sheet=grid.name,
                )
            ],
            "unresolved": [],
        }
    table = _find_table(grid, period)
    if not table:
        return {
            "status": "skipped",
            "reason": "attendance_table_not_found",
            "period_start": period[0].isoformat(),
            "period_end": period[1].isoformat(),
            "entries": [],
            "warnings": [
                _warning(
                    "attendance_table_not_found",
                    "Visible worksheet was skipped because FULL NAME/POSITION/date columns were not found.",
                    sheet=grid.name,
                )
            ],
            "unresolved": [],
        }

    if len(table["dates"]) != table["expected_days"]:
        warnings.append(
            _warning(
                "incomplete_date_columns",
                "The date header does not cover every date in PERIOD.",
                sheet=grid.name,
                expected=table["expected_days"],
                found=len(table["dates"]),
            )
        )
    for mapping in table["dates"]:
        if mapping["shown_day"] != mapping["date"].day:
            warnings.append(
                _warning(
                    "date_header_mismatch",
                    "A displayed day number does not match its sequential PERIOD date.",
                    sheet=grid.name,
                    date=mapping["date"].isoformat(),
                    displayed_day=mapping["shown_day"],
                )
            )

    effective_start = max(filter(None, (period[0], requested_start)))
    effective_end = min(filter(None, (period[1], requested_end, cutoff)))
    selected_dates = [
        mapping
        for mapping in table["dates"]
        if effective_start <= mapping["date"] <= effective_end
    ] if effective_start <= effective_end else []

    current_section = "unclassified"
    entries = []
    employee_count = 0
    unknown_statuses: set[str] = set()
    start_row = max(table["header_row"], table["day_row"]) + 1
    for row in range(start_row, grid.max_row + 1):
        row_text = " ".join(value for _, value in grid.row_values(row))
        semantic_row = _semantic(row_text)
        if "indirect manpower" in semantic_row:
            current_section = "indirect"
            continue
        if "direct manpower" in semantic_row:
            current_section = "direct"
            continue
        if any(marker in semantic_row for marker in ("prepared by", "checked by", "approved by", "acknowledged by")):
            break

        name = _clean_text(grid.value(row, table["name_column"]))
        role = _clean_text(grid.value(row, table["role_column"]))
        if not name or _semantic(name) in {"full name", "employee name", "nama", "nama lengkap"}:
            continue
        if not selected_dates:
            continue

        employee_count += 1
        if current_section == "unclassified":
            item = {
                "type": "unclassified_section",
                "sheet": grid.name,
                "row": row,
                "employee": name,
            }
            unresolved.append(item)
            warnings.append(
                _warning(
                    "unclassified_section",
                    "Employee is outside a recognised DIRECT/INDIRECT MANPOWER section.",
                    **item,
                )
            )
        if not role:
            unresolved.append(
                {
                    "type": "missing_role",
                    "sheet": grid.name,
                    "row": row,
                    "employee": name,
                }
            )
            warnings.append(
                _warning(
                    "missing_role",
                    "Employee position is blank and requires review.",
                    sheet=grid.name,
                    row=row,
                    employee=name,
                )
            )

        for mapping in selected_dates:
            raw = _clean_text(grid.value(row, mapping["column"]))
            status = normalize_attendance_status(raw)
            if status == "nonpresent" and not _known_nonpresent(raw):
                unknown_statuses.add(raw)
            entries.append(
                {
                    "employee_key": _employee_key(name),
                    "employee": name,
                    "role": role,
                    "section": current_section,
                    "date": mapping["date"].isoformat(),
                    "raw_status": raw,
                    "status": status,
                    "source_id": source_id,
                    "sheet": grid.name,
                    "row": row,
                }
            )

    for raw in sorted(unknown_statuses):
        warnings.append(
            _warning(
                "unknown_nonpresent_marker",
                "An unknown marker was treated as non-present (zero physical manhours).",
                sheet=grid.name,
                raw_status=raw,
            )
        )
    if employee_count == 0 and selected_dates:
        warnings.append(
            _warning(
                "no_employee_rows",
                "No employee rows were found in the attendance table.",
                sheet=grid.name,
            )
        )
    return {
        "status": "parsed",
        "period_start": period[0].isoformat(),
        "period_end": period[1].isoformat(),
        "effective_start": effective_start.isoformat() if effective_start <= effective_end else None,
        "effective_end": effective_end.isoformat() if effective_start <= effective_end else None,
        "employee_count": employee_count,
        "entries": entries,
        "warnings": warnings,
        "unresolved": unresolved,
    }


def _parse_payload(
    filename: str,
    payload: bytes,
    *,
    start_date: date | None,
    end_date: date | None,
    cutoff_date: date | None,
) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    source_id = digest[:16]
    warnings: list[dict] = []
    unresolved: list[dict] = []
    entries = []
    sheet_manifest = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            _validate_archive(archive)
            shared = _shared_strings(archive)
            sheets = _workbook_sheets(archive)
            for descriptor in sheets:
                if descriptor["state"] != "visible":
                    sheet_manifest.append(
                        {
                            "name": descriptor["name"],
                            "state": descriptor["state"],
                            "status": "skipped",
                            "reason": "hidden_sheet",
                        }
                    )
                    continue
                grid = _read_sheet(archive, descriptor, shared)
                result = _parse_sheet(
                    grid,
                    source_id=source_id,
                    requested_start=start_date,
                    requested_end=end_date,
                    cutoff=cutoff_date,
                )
                entries.extend(result.pop("entries"))
                warnings.extend(result.pop("warnings"))
                unresolved.extend(result.pop("unresolved"))
                sheet_manifest.append({"name": descriptor["name"], "state": "visible", **result})
    except zipfile.BadZipFile as exc:
        raise TimesheetError("The uploaded file is not a readable .xlsx workbook") from exc

    if not any(sheet["status"] == "parsed" for sheet in sheet_manifest):
        raise TimesheetError("No visible attendance worksheet could be parsed")
    return {
        "manifest": {
            "source_id": source_id,
            "filename": filename,
            "sha256": digest,
            "size_bytes": len(payload),
            "status": "accepted",
            "duplicate_of": None,
            "sheets": sheet_manifest,
        },
        "entries": entries,
        "warnings": warnings,
        "unresolved": unresolved,
    }


def _empty_preview() -> dict:
    return {
        "formula_version": FORMULA_VERSION,
        "hours_per_present_day": PHYSICAL_HOURS_PER_PRESENT_DAY,
        "source_manifest": [],
        "period": {"start": None, "end": None, "cutoff": None},
        "daily_totals": [],
        "roles": [],
        "employees": [],
        "totals": {
            "employee_count": 0,
            "present_person_days": 0,
            "physical_manhours": 0,
            "peak_present_count": 0,
            "peak_date": None,
            "by_section": {},
            "status_counts": {},
        },
        "warnings": [],
        "unresolved": [],
    }


def _build_preview(parsed_sources: list[dict], *, cutoff_date: date | None) -> dict:
    preview = _empty_preview()
    manifests = [source["manifest"] for source in parsed_sources]
    preview["source_manifest"] = manifests
    warnings = [item for source in parsed_sources for item in source["warnings"]]
    unresolved = [item for source in parsed_sources for item in source["unresolved"]]
    entries = [item for source in parsed_sources for item in source["entries"]]

    observations: dict[tuple[str, str], list[dict]] = defaultdict(list)
    names: dict[str, list[str]] = defaultdict(list)
    roles: dict[str, list[str]] = defaultdict(list)
    sections: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        key = entry["employee_key"]
        if entry["employee"] not in names[key]:
            names[key].append(entry["employee"])
        if entry["role"] and entry["role"] not in roles[key]:
            roles[key].append(entry["role"])
        if entry["section"] not in sections[key]:
            sections[key].append(entry["section"])
        observations[(key, entry["date"])].append(entry)

    resolved: dict[tuple[str, str], dict] = {}
    for (employee_key, day), items in observations.items():
        statuses = sorted({item["status"] for item in items})
        if len(statuses) == 1:
            status = statuses[0]
        else:
            status = "conflict"
            detail = {
                "type": "employee_date_status_conflict",
                "employee": names[employee_key][0],
                "date": day,
                "statuses": statuses,
                "observations": [
                    {
                        "raw_status": item["raw_status"],
                        "status": item["status"],
                        "source_id": item["source_id"],
                        "sheet": item["sheet"],
                        "row": item["row"],
                    }
                    for item in items
                ],
            }
            unresolved.append(detail)
            warnings.append(
                _warning(
                    "employee_date_status_conflict",
                    "Conflicting attendance statuses require manual review; no manhours were counted.",
                    employee=names[employee_key][0],
                    date=day,
                    statuses=statuses,
                )
            )
        resolved[(employee_key, day)] = {
            "date": day,
            "status": status,
            "raw_statuses": sorted({item["raw_status"] for item in items}),
            "physical_manhours": PHYSICAL_HOURS_PER_PRESENT_DAY if status == "present" else 0,
            "sources": sorted({item["source_id"] for item in items}),
        }

    for employee_key, employee_roles in roles.items():
        normalised_roles = {_role_key(role) for role in employee_roles}
        if len(normalised_roles) > 1:
            detail = {
                "type": "employee_role_conflict",
                "employee": names[employee_key][0],
                "roles": employee_roles,
            }
            unresolved.append(detail)
            warnings.append(
                _warning(
                    "employee_role_conflict",
                    "Employee has different roles across uploaded files.",
                    employee=names[employee_key][0],
                    roles=employee_roles,
                )
            )
    for employee_key, employee_sections in sections.items():
        if len(employee_sections) > 1:
            detail = {
                "type": "employee_section_conflict",
                "employee": names[employee_key][0],
                "sections": employee_sections,
            }
            unresolved.append(detail)
            warnings.append(
                _warning(
                    "employee_section_conflict",
                    "Employee appears in different manpower sections.",
                    employee=names[employee_key][0],
                    sections=employee_sections,
                )
            )

    employee_rows = []
    all_dates: set[str] = set()
    for employee_key in sorted(names, key=lambda item: names[item][0].casefold()):
        statuses = [
            value
            for (key, _), value in resolved.items()
            if key == employee_key
        ]
        statuses.sort(key=lambda value: value["date"])
        all_dates.update(item["date"] for item in statuses)
        counts = Counter(item["status"] for item in statuses)
        employee_rows.append(
            {
                "employee_key": employee_key,
                "name": names[employee_key][0],
                "name_variants": names[employee_key],
                "role": roles[employee_key][0] if roles[employee_key] else "",
                "role_variants": roles[employee_key],
                "section": sections[employee_key][0] if sections[employee_key] else "unclassified",
                "section_variants": sections[employee_key],
                "statuses": statuses,
                "status_counts": dict(sorted(counts.items())),
                "present_days": counts.get("present", 0),
                "physical_manhours": counts.get("present", 0) * PHYSICAL_HOURS_PER_PRESENT_DAY,
            }
        )

    daily_rows = []
    role_totals: dict[str, dict] = {}
    section_totals: dict[str, dict] = {}
    grand_counts: Counter = Counter()
    for day in sorted(all_dates):
        day_counts: Counter = Counter()
        day_sections: dict[str, int] = defaultdict(int)
        for employee in employee_rows:
            status_item = next((item for item in employee["statuses"] if item["date"] == day), None)
            if not status_item:
                continue
            status = status_item["status"]
            day_counts[status] += 1
            grand_counts[status] += 1
            if status == "present":
                day_sections[employee["section"]] += 1

                role = employee["role"] or "Unspecified"
                role_key = _role_key(role) or "unspecified"
                role_item = role_totals.setdefault(
                    role_key,
                    {"role": role, "employees": set(), "present_person_days": 0},
                )
                role_item["employees"].add(employee["employee_key"])
                role_item["present_person_days"] += 1

                section_item = section_totals.setdefault(
                    employee["section"], {"employees": set(), "present_person_days": 0}
                )
                section_item["employees"].add(employee["employee_key"])
                section_item["present_person_days"] += 1
        present = day_counts.get("present", 0)
        daily_rows.append(
            {
                "date": day,
                "present_count": present,
                "physical_manhours": present * PHYSICAL_HOURS_PER_PRESENT_DAY,
                "status_counts": dict(sorted(day_counts.items())),
                "present_by_section": dict(sorted(day_sections.items())),
            }
        )

    preview["employees"] = employee_rows
    preview["daily_totals"] = daily_rows
    preview["roles"] = [
        {
            "role": value["role"],
            "employee_count": len(value["employees"]),
            "present_person_days": value["present_person_days"],
            "physical_manhours": value["present_person_days"] * PHYSICAL_HOURS_PER_PRESENT_DAY,
        }
        for _, value in sorted(role_totals.items())
    ]
    present_days = grand_counts.get("present", 0)
    peak = max(daily_rows, key=lambda item: (item["present_count"], item["date"]), default=None)
    preview["totals"] = {
        "employee_count": len(employee_rows),
        "present_person_days": present_days,
        "physical_manhours": present_days * PHYSICAL_HOURS_PER_PRESENT_DAY,
        "peak_present_count": peak["present_count"] if peak else 0,
        "peak_date": peak["date"] if peak else None,
        "by_section": {
            key: {
                "employee_count": len(value["employees"]),
                "present_person_days": value["present_person_days"],
                "physical_manhours": value["present_person_days"] * PHYSICAL_HOURS_PER_PRESENT_DAY,
            }
            for key, value in sorted(section_totals.items())
        },
        "status_counts": dict(sorted(grand_counts.items())),
    }
    preview["period"] = {
        "start": min(all_dates) if all_dates else None,
        "end": max(all_dates) if all_dates else None,
        "cutoff": cutoff_date.isoformat() if cutoff_date else None,
    }
    preview["warnings"] = warnings
    preview["unresolved"] = unresolved
    return preview


def compile_timesheets(
    sources,
    *,
    start_date=None,
    end_date=None,
    cutoff_date=None,
) -> dict:
    """Compile one or more attendance workbooks into a JSON-safe preview.

    ``sources`` may contain paths, ``(filename, bytes)`` pairs, mappings with
    ``filename``/``data``, or binary file objects.  Byte-identical workbooks
    are retained in the manifest as duplicates but are not counted twice.
    """

    requested_start = _parse_requested_date(start_date, "start_date")
    requested_end = _parse_requested_date(end_date, "end_date")
    cutoff = _parse_requested_date(cutoff_date, "cutoff_date")
    if requested_start and requested_end and requested_start > requested_end:
        raise TimesheetError("start_date must not be after end_date")

    if isinstance(sources, (str, os.PathLike, bytes, bytearray, memoryview, dict)) or hasattr(sources, "read"):
        sources = [sources]
    else:
        sources = list(sources)
    if not sources:
        raise TimesheetError("At least one .xlsx timesheet is required")

    parsed_sources = []
    seen_hashes: dict[str, str] = {}
    for source in sources:
        filename, payload = _read_source(source)
        digest = hashlib.sha256(payload).hexdigest()
        if digest in seen_hashes:
            parsed_sources.append(
                {
                    "manifest": {
                        "source_id": digest[:16],
                        "filename": filename,
                        "sha256": digest,
                        "size_bytes": len(payload),
                        "status": "duplicate",
                        "duplicate_of": seen_hashes[digest],
                        "sheets": [],
                    },
                    "entries": [],
                    "warnings": [
                        _warning(
                            "duplicate_file",
                            "Byte-identical timesheet was not counted twice.",
                            filename=filename,
                            duplicate_of=seen_hashes[digest],
                        )
                    ],
                    "unresolved": [],
                }
            )
            continue
        parsed = _parse_payload(
            filename,
            payload,
            start_date=requested_start,
            end_date=requested_end,
            cutoff_date=cutoff,
        )
        seen_hashes[digest] = parsed["manifest"]["source_id"]
        parsed_sources.append(parsed)
    return _build_preview(parsed_sources, cutoff_date=cutoff)


def parse_timesheet_xlsx(
    source,
    *,
    filename: str | None = None,
    start_date=None,
    end_date=None,
    cutoff_date=None,
) -> dict:
    """Parse a single ``.xlsx`` attendance workbook.

    The optional date bounds are inclusive. Blank/future cells remain
    ``missing`` and never become zero-hour absence records implicitly.
    """

    resolved_name, payload = _read_source(source, filename=filename)
    return compile_timesheets(
        [(resolved_name, payload)],
        start_date=start_date,
        end_date=end_date,
        cutoff_date=cutoff_date,
    )


# Readable aliases for callers that prefer singular/file-oriented names.
parse_timesheet_file = parse_timesheet_xlsx
compile_timesheet_files = compile_timesheets


__all__ = [
    "FORMULA_VERSION",
    "PHYSICAL_HOURS_PER_PRESENT_DAY",
    "TimesheetError",
    "compile_timesheet_files",
    "compile_timesheets",
    "normalize_attendance_status",
    "parse_timesheet_file",
    "parse_timesheet_xlsx",
]
