"""Bounded, review-first importer for one Daily Report PDF at a time.

The importer deliberately separates PDF text extraction from deterministic
field parsing.  Critical values (date, day number, and project number) are
never fuzzy matched.  RapidFuzz is used only to suggest non-critical project
title and section-label matches, and those suggestions are kept in extraction
metadata instead of silently replacing source values.

Runtime dependencies are loaded lazily so importing this module does not make
the rest of the Flask application depend on the optional monthly-report
feature.  PDF extraction requires ``pypdf``.  Fuzzy suggestions require
``rapidfuzz``; deterministic extraction still works when RapidFuzz is absent.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping, Sequence


SCHEMA_VERSION = "daily-report-import/1"
PARSER_VERSION = "monthly-pdf-importer/1.1"


class PDFImportError(Exception):
    """Base exception for a rejected or failed PDF import."""


class PDFValidationError(PDFImportError):
    """Raised before/during extraction when a PDF violates importer limits."""


class PDFDependencyError(PDFImportError):
    """Raised when a dependency required for the requested operation is absent."""


class PDFExtractionError(PDFImportError):
    """Raised when pypdf cannot safely open or inspect the document."""


@dataclass(frozen=True)
class ImportLimits:
    """Resource bounds applied to every individual PDF import."""

    max_bytes: int = 50 * 1024 * 1024
    max_pages: int = 50
    max_text_chars: int = 500_000

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_pages", "max_text_chars"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")


DEFAULT_LIMITS = ImportLimits()


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NUMBERED_ITEM = re.compile(r"^\s*(?:[-*\u2022]\s*)?(\d{1,3})[.)]\s+(.+?)\s*$")
_AREA_MARKER = re.compile(r"^\s*[\u25a0\u25aa\u25cf\u25a1]\s*(.+?)\s*$")
_SECTION_HEADING = re.compile(
    r"^\s*(\d+(?:\.\d+)*)\s*[.)]?\s+(.+?)\s*$",
    re.IGNORECASE,
)

_SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "report_information": ("REPORT INFORMATION",),
    "weather": ("WEATHER REPORT", "WEATHER STATUS"),
    "indirect_manpower": ("INDIRECT MANPOWER",),
    "overall_progress": ("OVERALL PROGRESS",),
    "daily_activities": (
        "DAILY ACTIVITIES & MANPOWER BY AREA",
        "DAILY ACTIVITIES AND MANPOWER BY AREA",
        "DAILY ACTIVITIES BY AREA",
    ),
    "direct_manpower": ("DIRECT MANPOWER BY AREA",),
    "constraints": ("CONSTRAINTS & ISSUES", "CONSTRAINTS AND ISSUES"),
    "remarks": ("REMARKS",),
    "sign_off": ("SIGN-OFF", "SIGN OFF"),
    "photo_documentation": ("PHOTO DOCUMENTATION",),
    # These aliases allow the same bounded parser to retain known sections
    # from legacy/third-party reports without treating them as daily fields.
    "this_month_activities": ("THIS MONTH ACTIVITIES",),
    "planned_activities_next_month": ("PLANNED ACTIVITIES NEXT MONTH",),
    "area_of_concern": (
        "AREA OF CONCERN AND SUGGESTED CORRECTIVE ACTION",
    ),
}

_LABEL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "project_no": (
        re.compile(r"^\s*Project\s*(?:No\.?|Number)\s*:?[ \t]*(.*?)\s*$", re.I),
        re.compile(r"^\s*Vendor\s+Project\s+No\.?\s*:?[ \t]*(.*?)\s*$", re.I),
        re.compile(r"^\s*Contract\s+No\.?\s*:?[ \t]*(.*?)\s*$", re.I),
    ),
    "project_title": (
        re.compile(r"^\s*Project\s*(?:Name|Title)\s*:?[ \t]*(.*?)\s*$", re.I),
    ),
    "customer": (
        re.compile(r"^\s*(?:Customer|Client)\s*:?[ \t]*(.*?)\s*$", re.I),
    ),
    "location": (
        re.compile(r"^\s*(?:Location|Site)\s*:?[ \t]*(.*?)\s*$", re.I),
    ),
    "equipment": (
        re.compile(r"^\s*Equipment\s*:?[ \t]*(.*?)\s*$", re.I),
    ),
    "date": (
        re.compile(
            r"^\s*(?:Report\s+)?Date\s*:?[ \t]*(.*?)"
            r"(?=\s*\|\s*(?:Day\b|Working\s+Day\b|Project\b)|\s*$)",
            re.I,
        ),
    ),
}

_KNOWN_FIELD_LABEL = re.compile(
    r"^\s*(?:Project\s*(?:No\.?|Number|Name|Title)|Vendor\s+Project\s+No\.?|"
    r"Contract\s+No\.?|Customer|Client|Location|Site|Equipment|(?:Report\s+)?Date|"
    r"Working\s+Day|Day\s*(?:No\.?|Number))\b",
    re.IGNORECASE,
)

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
    "agu": 8,
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


def _warning(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    field: str | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if field is not None:
        item["field"] = field
    if page is not None:
        item["page"] = page
    return item


def _clean_filename(filename: str | None) -> str:
    if not filename:
        return "uploaded-report.pdf"
    # pathlib on POSIX does not treat a Windows backslash as a separator, so
    # normalize both forms before retaining only a display name.
    clean = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    clean = _CONTROL_CHARS.sub("", clean).strip()
    return (clean or "uploaded-report.pdf")[:255]


def _read_one_source(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    max_bytes: int,
) -> tuple[bytes, str | None]:
    if isinstance(source, (list, tuple, set, frozenset)):
        raise PDFValidationError("Import exactly one PDF at a time")

    inferred_filename: str | None = None
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise PDFValidationError("PDF source cannot be read") from exc
        if size > max_bytes:
            raise PDFValidationError(
                f"PDF exceeds the {max_bytes}-byte per-file limit"
            )
        try:
            # Re-check through a bounded read so a file that grows between the
            # stat and open calls cannot cause an unbounded memory allocation.
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise PDFValidationError("PDF source cannot be read") from exc
        inferred_filename = path.name
    else:
        stream = getattr(source, "stream", source)
        if not hasattr(stream, "read"):
            raise PDFValidationError("Import exactly one PDF byte stream at a time")
        inferred_filename = getattr(source, "filename", None)
        original_position: int | None = None
        try:
            if hasattr(stream, "tell"):
                original_position = stream.tell()
            if hasattr(stream, "seek"):
                stream.seek(0)
            data = stream.read(max_bytes + 1)
        except (OSError, ValueError, TypeError) as exc:
            raise PDFValidationError("PDF stream cannot be read") from exc
        finally:
            if original_position is not None and hasattr(stream, "seek"):
                try:
                    stream.seek(original_position)
                except (OSError, ValueError):
                    pass
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise PDFValidationError("PDF stream must return bytes")
        data = bytes(data)

    if not data:
        raise PDFValidationError("PDF is empty")
    if len(data) > max_bytes:
        raise PDFValidationError(f"PDF exceeds the {max_bytes}-byte per-file limit")
    if not data.startswith(b"%PDF-"):
        raise PDFValidationError("File does not have a valid PDF magic header")
    return data, inferred_filename


def _load_pdf_reader() -> Any:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise PDFDependencyError(
            "PDF import requires the optional 'pypdf' dependency"
        ) from exc
    return PdfReader


def _extract_pdf_pages(
    data: bytes,
    limits: ImportLimits,
) -> tuple[list[str], list[dict[str, Any]]]:
    reader_cls = _load_pdf_reader()
    try:
        reader = reader_cls(io.BytesIO(data), strict=False)
        if getattr(reader, "is_encrypted", False):
            raise PDFValidationError("Encrypted PDFs are not accepted")
        page_count = len(reader.pages)
    except PDFImportError:
        raise
    except Exception as exc:
        raise PDFExtractionError("PDF could not be opened safely") from exc

    if page_count < 1:
        raise PDFValidationError("PDF contains no pages")
    if page_count > limits.max_pages:
        raise PDFValidationError(
            f"PDF has {page_count} pages; the per-file limit is {limits.max_pages}"
        )

    pages: list[str] = []
    warnings: list[dict[str, Any]] = []
    total_chars = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            try:
                text = page.extract_text(extraction_mode="layout")
            except TypeError:
                # Compatibility with older pypdf versions while still using
                # layout mode when the installed version supports it.
                text = page.extract_text()
        except Exception:
            text = ""
            warnings.append(
                _warning(
                    "page_text_extraction_failed",
                    f"Text extraction failed on page {index}",
                    page=index,
                )
            )
        if not isinstance(text, str):
            text = ""
        text = _normalize_page_text(text)
        total_chars += len(text)
        if total_chars > limits.max_text_chars:
            raise PDFValidationError(
                "Extracted PDF text exceeds the configured character limit"
            )
        pages.append(text)

    if sum(len(re.sub(r"\s+", "", page)) for page in pages) < 20:
        warnings.append(
            _warning(
                "no_extractable_text",
                "PDF has too little searchable text; OCR or manual entry is required",
                severity="error",
            )
        )
    return pages, warnings


def _normalize_page_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\ufeff", "")
    text = _CONTROL_CHARS.sub("", text)
    # Preserve horizontal spacing because it is used to split the two activity
    # columns generated by ReportLab's table layout.
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _line_records(pages: Sequence[str]) -> list[tuple[int, int, str]]:
    return [
        (page_number, line_number, line)
        for page_number, page in enumerate(pages, start=1)
        for line_number, line in enumerate(page.splitlines(), start=1)
    ]


def _next_value_line(
    records: Sequence[tuple[int, int, str]],
    current_index: int,
) -> tuple[str, int] | None:
    page = records[current_index][0]
    for _, (next_page, _, line) in zip(range(2), records[current_index + 1 :]):
        value = _compact(line)
        if not value:
            continue
        if next_page != page or _KNOWN_FIELD_LABEL.match(value) or _SECTION_HEADING.match(value):
            return None
        return value, next_page
    return None


def _extract_labeled_value(
    pages: Sequence[str],
    field: str,
) -> tuple[str | None, dict[str, Any] | None]:
    patterns = _LABEL_PATTERNS[field]
    records = _line_records(pages)
    for index, (page, line_number, line) in enumerate(records):
        compact_line = _compact(line)
        for pattern in patterns:
            match = pattern.match(compact_line)
            if not match:
                continue
            value = _compact(match.group(1))
            value_page = page
            if not value:
                following = _next_value_line(records, index)
                if following:
                    value, value_page = following
            if value:
                return value, {
                    "method": "label_regex",
                    "page": value_page,
                    "line": line_number,
                    "raw": value,
                }
    return None, None


def _build_iso_date(year: int, month: int, day: int) -> str | None:
    if not 1900 <= year <= 2200:
        return None
    try:
        return date_type(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_date_value(value: str) -> tuple[str | None, str | None]:
    value = _compact(value).strip(".,")

    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if iso:
        parsed = _build_iso_date(*(int(part) for part in iso.groups()))
        return parsed, None if parsed else "invalid_date"

    numeric = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", value)
    if numeric:
        first, second, year = (int(part) for part in numeric.groups())
        if first <= 12 and second <= 12:
            return None, "ambiguous_numeric_date"
        parsed = _build_iso_date(year, second, first)
        return parsed, None if parsed else "invalid_date"

    day_first = re.fullmatch(
        r"(\d{1,2})\s*[- /]\s*([A-Za-z]+)\s*[-, /]\s*(\d{4})",
        value,
        re.IGNORECASE,
    )
    if day_first:
        day, month_name, year = day_first.groups()
        month = _MONTHS.get(month_name.casefold().rstrip("."))
        parsed = _build_iso_date(int(year), month, int(day)) if month else None
        return parsed, None if parsed else "invalid_date"

    month_first = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})",
        value,
        re.IGNORECASE,
    )
    if month_first:
        month_name, day, year = month_first.groups()
        month = _MONTHS.get(month_name.casefold().rstrip("."))
        parsed = _build_iso_date(int(year), month, int(day)) if month else None
        return parsed, None if parsed else "invalid_date"

    return None, "unrecognized_date_format"


def _extract_date(
    pages: Sequence[str],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    raw, provenance = _extract_labeled_value(pages, "date")
    if raw is not None:
        parsed, error = _parse_date_value(raw)
        if parsed:
            assert provenance is not None
            provenance = dict(provenance, normalized=parsed)
            return parsed, provenance, warnings
        warnings.append(
            _warning(
                error or "invalid_date",
                f"Labeled report date could not be accepted: {raw}",
                severity="error",
                field="date",
                page=provenance.get("page") if provenance else None,
            )
        )
        return None, provenance, warnings

    candidates: dict[str, tuple[int, str]] = {}
    for page_number, page in enumerate(pages, start=1):
        for match in re.finditer(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", page):
            parsed, _ = _parse_date_value(match.group(1))
            if parsed:
                candidates.setdefault(parsed, (page_number, match.group(1)))
    if len(candidates) == 1:
        parsed, (page, raw_value) = next(iter(candidates.items()))
        warnings.append(
            _warning(
                "date_inferred_without_label",
                "Date was inferred from the only ISO date in the PDF and must be reviewed",
                field="date",
                page=page,
            )
        )
        return parsed, {
            "method": "unique_iso_regex",
            "page": page,
            "raw": raw_value,
            "normalized": parsed,
        }, warnings
    if len(candidates) > 1:
        warnings.append(
            _warning(
                "ambiguous_date_candidates",
                "Multiple unlabeled dates were found; report date was not selected",
                severity="error",
                field="date",
            )
        )
    return None, None, warnings


def _extract_day_number(
    pages: Sequence[str],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    labeled_patterns = (
        re.compile(
            r"^\s*(?:Working\s+Day|Day\s*(?:No\.?|Number))\s*:?[ \t]*(?:Day\s*)?(\d{1,6})\s*$",
            re.IGNORECASE,
        ),
    )
    for page, line_number, line in _line_records(pages):
        compact_line = _compact(line)
        for pattern in labeled_patterns:
            match = pattern.match(compact_line)
            if match:
                return match.group(1), {
                    "method": "label_regex",
                    "page": page,
                    "line": line_number,
                    "raw": match.group(1),
                }, []

    candidates: dict[str, int] = {}
    for page_number, page in enumerate(pages, start=1):
        for match in re.finditer(r"\bDay\s+(\d{1,6})\b", page, re.IGNORECASE):
            candidates.setdefault(match.group(1), page_number)
    if len(candidates) == 1:
        value, page = next(iter(candidates.items()))
        return value, {
            "method": "unique_day_regex",
            "page": page,
            "raw": value,
        }, [
            _warning(
                "day_inferred_without_label",
                "Day number was inferred from the only 'Day N' value and must be reviewed",
                field="day_no",
                page=page,
            )
        ]
    warnings: list[dict[str, Any]] = []
    if len(candidates) > 1:
        warnings.append(
            _warning(
                "ambiguous_day_candidates",
                "Multiple day numbers were found; no day number was selected",
                severity="error",
                field="day_no",
            )
        )
    return None, None, warnings


def _normalize_section_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).upper().replace("&", " AND ")
    return re.sub(r"[^A-Z0-9]+", " ", value).strip()


def _exact_section_key(label: str) -> str | None:
    normalized = _normalize_section_label(label)
    for key, aliases in _SECTION_LABELS.items():
        if normalized in {_normalize_section_label(alias) for alias in aliases}:
            return key
    return None


def _load_rapidfuzz() -> tuple[Any, Any] | None:
    try:
        from rapidfuzz import fuzz, process
    except ImportError:  # pragma: no cover - environment dependent
        return None
    return process, fuzz


def _fuzzy_section_key(label: str) -> tuple[str, float, float] | None:
    dependency = _load_rapidfuzz()
    if dependency is None:
        return None
    process, fuzz = dependency
    choices: list[tuple[str, str]] = [
        (key, _normalize_section_label(alias))
        for key, aliases in _SECTION_LABELS.items()
        for alias in aliases
    ]
    normalized = _normalize_section_label(label)
    ranked = process.extract(
        normalized,
        [choice for _, choice in choices],
        scorer=fuzz.WRatio,
        limit=2,
    )
    if not ranked:
        return None
    best_text, best_score, best_index = ranked[0]
    del best_text
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = float(best_score) - float(second_score)
    if float(best_score) < 95.0 or margin < 8.0:
        return None
    return choices[int(best_index)][0], float(best_score), margin


def _collect_sections(
    pages: Sequence[str],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    sections: dict[str, list[str]] = {}
    matches: list[dict[str, Any]] = []
    active_key: str | None = None

    for page_number, page in enumerate(pages, start=1):
        for line in page.splitlines():
            compact_line = _compact(line)
            heading = _SECTION_HEADING.match(compact_line)
            if heading:
                number, label = heading.groups()
                key = _exact_section_key(label)
                method = "exact"
                score: float | None = None
                margin: float | None = None
                # Numbered activity items use the same ``1. text`` shape as a
                # section heading.  Only an all-uppercase, heading-sized label
                # is eligible for fuzzy section matching; activity content is
                # retained verbatim and never fuzzily reclassified.
                fuzzy_eligible = (
                    label == label.upper()
                    and 1 <= len(label.split()) <= 12
                    and len(label) <= 120
                )
                if key is None and fuzzy_eligible:
                    fuzzy = _fuzzy_section_key(label)
                    if fuzzy:
                        key, score, margin = fuzzy
                        method = "rapidfuzz_suggestion"
                if key is not None:
                    active_key = key
                    sections.setdefault(key, [])
                    match_record: dict[str, Any] = {
                        "key": key,
                        "number": number,
                        "label": label,
                        "page": page_number,
                        "method": method,
                    }
                    if score is not None:
                        match_record.update(score=round(score, 2), margin=round(margin or 0, 2))
                    matches.append(match_record)
                    continue
                # Unknown numbered lines may be activity-list items, so they
                # do not terminate a recognized section.
            if active_key is not None:
                sections[active_key].append(line.rstrip())

    return {
        key: "\n".join(lines).strip()
        for key, lines in sections.items()
        if any(line.strip() for line in lines)
    }, matches


def _activity_items_from_segment(segment: str) -> tuple[list[str], list[str]]:
    lines = segment.splitlines()
    today: list[str] = []
    tomorrow: list[str] = []

    # ReportLab emits both table headings on one layout-preserving line.  Use
    # their character offsets as deterministic column boundaries.
    for header_index, line in enumerate(lines):
        lowered = line.casefold()
        today_pos = lowered.find("activity today")
        tomorrow_pos = lowered.find("activity tomorrow")
        if today_pos < 0 or tomorrow_pos <= today_pos:
            continue
        for item_line in lines[header_index + 1 :]:
            if re.search(
                r"\b(?:Direct|Indirect)\s+Manpower\b|^\s*(?:Constraints|Remarks)\s*:",
                item_line,
                re.IGNORECASE,
            ):
                break
            left = item_line[today_pos:tomorrow_pos]
            right = item_line[tomorrow_pos:]
            left_match = _NUMBERED_ITEM.match(left)
            right_match = _NUMBERED_ITEM.match(right)
            if left_match:
                today.append(_compact(left_match.group(2)))
            elif _compact(left).strip("-\u2013\u2014") and today:
                today[-1] = _compact(f"{today[-1]} {_compact(left)}")
            if right_match:
                tomorrow.append(_compact(right_match.group(2)))
            elif _compact(right).strip("-\u2013\u2014") and tomorrow:
                tomorrow[-1] = _compact(f"{tomorrow[-1]} {_compact(right)}")
        if today or tomorrow:
            return today, tomorrow

    mode: str | None = None
    for line in lines:
        compact_line = _compact(line)
        if re.fullmatch(r"Activity\s+Today", compact_line, re.IGNORECASE):
            mode = "today"
            continue
        if re.fullmatch(r"Activity\s+Tomorrow", compact_line, re.IGNORECASE):
            mode = "tomorrow"
            continue
        if re.search(
            r"\b(?:Direct|Indirect)\s+Manpower\b|^\s*(?:Constraints|Remarks)\s*:",
            compact_line,
            re.IGNORECASE,
        ):
            mode = None
            continue
        match = _NUMBERED_ITEM.match(compact_line)
        if match and mode == "today":
            today.append(_compact(match.group(2)))
        elif match and mode == "tomorrow":
            tomorrow.append(_compact(match.group(2)))
        elif compact_line.strip("-\u2013\u2014") and mode == "today" and today:
            today[-1] = _compact(f"{today[-1]} {compact_line}")
        elif compact_line.strip("-\u2013\u2014") and mode == "tomorrow" and tomorrow:
            tomorrow[-1] = _compact(f"{tomorrow[-1]} {compact_line}")
    return today, tomorrow


_MANPOWER_ROW = re.compile(
    r"^\s*(?P<number>\d{1,4})\s*[.)]?\s+"
    r"(?P<body>.+?)\s{2,}"
    r"(?P<hours>\d{1,2}:\d{2}\s*[-\u2013\u2014]\s*\d{1,2}:\d{2})\s*$"
)


def _area_segments(section: str) -> list[tuple[str, list[str]]]:
    """Split an area-based section while retaining layout-preserving lines."""

    if not section.strip():
        return []
    segments: list[tuple[str, list[str]]] = []
    current_id: str | None = None
    current_lines: list[str] = []
    for line in section.splitlines():
        marker = _AREA_MARKER.match(line)
        if marker:
            if current_id is not None or current_lines:
                segments.append((current_id or "Imported PDF", current_lines))
            current_id = _compact(marker.group(1))[:120]
            current_lines = []
        else:
            current_lines.append(line)
    if current_id is not None or current_lines:
        segments.append((current_id or "Imported PDF", current_lines))
    return segments


def _manpower_header(line: str) -> dict[str, Any] | None:
    normalized = _normalize_section_label(line)
    if not re.search(r"\bNO\b", normalized) or not re.search(r"\bNAME\b", normalized):
        return None
    if not re.search(r"\b(?:WORKING\s+)?HOURS?\b", normalized):
        return None
    return {
        "has_task": bool(re.search(r"\bTASK(?:\s+TODAY)?\b", normalized)),
        "raw": _compact(line),
    }


def _manpower_kind(line: str) -> str | None:
    compact_line = _compact(line)
    if re.search(r"\bDirect\s+Manpower\b", compact_line, re.IGNORECASE):
        return "direct"
    if re.search(r"\bIndirect\s+Manpower\b", compact_line, re.IGNORECASE):
        return "indirect"
    return None


def _parse_manpower_row(
    line: str,
    *,
    has_task: bool,
) -> tuple[dict[str, str], str] | None:
    """Parse one layout-text manpower row into the application's row shape.

    PDF table extractors omit empty cells.  Consequently a row containing only
    ``number, name, hours`` is valid and deliberately retains an empty role.
    ``Task Today`` is optional in legacy layouts and is always represented in
    the canonical result so all supported PDF profiles produce the same shape.
    """

    match = _MANPOWER_ROW.match(line)
    if not match:
        return None
    cells = [
        _compact(cell)
        for cell in re.split(r"\s{2,}", match.group("body").strip())
        if _compact(cell)
    ]
    if not cells:
        return None

    name = cells[0]
    role = ""
    task = ""
    if has_task:
        if len(cells) >= 2:
            role = cells[1]
        if len(cells) >= 3:
            task = _compact(" ".join(cells[2:]))
    elif len(cells) >= 2:
        role = _compact(" ".join(cells[1:]))

    return {
        "name": name,
        "role": role,
        "task": task,
        "hours": _compact(match.group("hours")),
    }, match.group("number")


def _parse_manpower_lines(
    lines: Sequence[str],
    *,
    default_category: str | None,
    area_id: str | None,
    source_section: str,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    rows: dict[str, list[dict[str, str]]] = {"direct": [], "indirect": []}
    provenance_rows: list[dict[str, Any]] = []
    category = default_category
    header: dict[str, Any] | None = None
    table_count = 0
    malformed_count = 0

    for line_index, line in enumerate(lines, start=1):
        explicit_category = _manpower_kind(line)
        if explicit_category:
            category = explicit_category
            header = None
            continue

        detected_header = _manpower_header(line)
        if detected_header:
            header = detected_header
            table_count += 1
            continue

        # Page headers/footers may occur between a legacy table header and its
        # continuation rows.  They intentionally do not reset table state.
        compact_line = _compact(line)
        if re.match(r"^(?:Constraints|Remarks)\s*:", compact_line, re.IGNORECASE):
            header = None
            continue

        rowish = bool(
            re.match(r"^\s*\d{1,4}\s*[.)]?\s+", line)
            and re.search(
                r"\d{1,2}:\d{2}\s*[-\u2013\u2014]\s*\d{1,2}:\d{2}\s*$",
                line,
            )
        )
        if not rowish or category not in rows:
            continue

        # If a page begins with continuation rows but the PDF extractor did
        # not retain the preceding header, use the section's known category
        # and record that the schema had to be inferred.
        inferred_header = header is None
        parsed = _parse_manpower_row(
            line,
            has_task=bool(header and header.get("has_task")),
        )
        if not parsed:
            malformed_count += 1
            continue
        row, source_row_number = parsed
        rows[category].append(row)
        provenance_rows.append(
            {
                "category": category,
                "area_id": area_id,
                "source_section": source_section,
                "source_line": line_index,
                "source_row_number": source_row_number,
                "table_schema": "inferred" if inferred_header else "header",
                "task_column": bool(header and header.get("has_task")),
            }
        )

    warnings: list[dict[str, Any]] = []
    if malformed_count:
        label = f" for area '{area_id}'" if area_id else ""
        warnings.append(
            _warning(
                "manpower_rows_require_manual_review",
                f"{malformed_count} manpower row(s){label} could not be mapped safely",
                field="manpower",
            )
        )
    metadata = {
        "source_section": source_section,
        "area_id": area_id,
        "table_count": table_count,
        "rows_extracted": len(rows["direct"]) + len(rows["indirect"]),
        "direct_rows": len(rows["direct"]),
        "indirect_rows": len(rows["indirect"]),
        "malformed_rows": malformed_count,
        "complete": table_count > 0 and malformed_count == 0,
        "row_provenance": provenance_rows,
    }
    return rows, metadata, warnings


def _area_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKC", value).casefold())


def _empty_area(area_id: str) -> dict[str, Any]:
    return {
        "id": area_id,
        "activities_today": [],
        "activities_tomorrow": [],
        "manpower": [],
        "indirect_manpower": [],
        "constraints": "",
        "remarks": "",
        "photos": [],
    }


def _extract_daily_content(
    sections: Mapping[str, str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    """Normalize legacy-combined and current-split daily report layouts."""

    areas: list[dict[str, Any]] = []
    areas_by_key: dict[str, dict[str, Any]] = {}
    manpower_metadata: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def ensure_area(area_id: str) -> dict[str, Any]:
        key = _area_key(area_id)
        area = areas_by_key.get(key)
        if area is None:
            area = _empty_area(area_id)
            areas_by_key[key] = area
            areas.append(area)
        return area

    activity_section = sections.get("daily_activities", "")
    for area_id, lines in _area_segments(activity_section):
        today, tomorrow = _activity_items_from_segment("\n".join(lines))
        parsed_rows, metadata, row_warnings = _parse_manpower_lines(
            lines,
            default_category=None,
            area_id=area_id,
            source_section="daily_activities",
        )
        if today or tomorrow or metadata["rows_extracted"]:
            area = ensure_area(area_id)
            area["activities_today"].extend(today)
            area["activities_tomorrow"].extend(tomorrow)
            area["manpower"].extend(parsed_rows["direct"])
            area["indirect_manpower"].extend(parsed_rows["indirect"])
        manpower_metadata.append(metadata)
        provenance_rows.extend(metadata.pop("row_provenance"))
        warnings.extend(row_warnings)

    direct_section = sections.get("direct_manpower", "")
    for area_id, lines in _area_segments(direct_section):
        parsed_rows, metadata, row_warnings = _parse_manpower_lines(
            lines,
            default_category="direct",
            area_id=area_id,
            source_section="direct_manpower",
        )
        if metadata["rows_extracted"]:
            area = ensure_area(area_id)
            area["manpower"].extend(parsed_rows["direct"])
            area["indirect_manpower"].extend(parsed_rows["indirect"])
        manpower_metadata.append(metadata)
        provenance_rows.extend(metadata.pop("row_provenance"))
        warnings.extend(row_warnings)

    global_section = sections.get("indirect_manpower", "")
    global_rows, global_metadata, global_warnings = _parse_manpower_lines(
        global_section.splitlines(),
        default_category="indirect",
        area_id=None,
        source_section="indirect_manpower",
    )
    provenance_rows.extend(global_metadata.pop("row_provenance"))
    manpower_metadata.append(global_metadata)
    warnings.extend(global_warnings)

    direct_sources = [
        item
        for item in manpower_metadata
        if item["source_section"] in {"daily_activities", "direct_manpower"}
    ]
    direct_tables = sum(item["table_count"] for item in direct_sources)
    direct_malformed = sum(item["malformed_rows"] for item in direct_sources)
    direct_rows = sum(item["direct_rows"] for item in direct_sources)
    if direct_section and direct_tables == 0:
        warnings.append(
            _warning(
                "direct_manpower_table_unrecognized",
                "Direct manpower section was found but no supported table header was recognized",
                field="manpower",
            )
        )
    if global_section and global_metadata["table_count"] == 0:
        warnings.append(
            _warning(
                "indirect_manpower_table_unrecognized",
                "Indirect manpower section was found but no supported table header was recognized",
                field="indirect_manpower",
            )
        )

    profile = "split_sections" if direct_section else "combined_activities_manpower"
    extraction = {
        "profile": profile,
        "rows": provenance_rows,
        "tables": manpower_metadata,
        "completeness": {
            "global_indirect": {
                "section_found": bool(global_section),
                "table_found": global_metadata["table_count"] > 0,
                "rows_extracted": global_metadata["indirect_rows"],
                "malformed_rows": global_metadata["malformed_rows"],
                "complete": bool(global_section)
                and global_metadata["table_count"] > 0
                and global_metadata["malformed_rows"] == 0,
            },
            "direct_by_area": {
                "section_found": bool(activity_section or direct_section),
                "table_found": direct_tables > 0,
                "rows_extracted": direct_rows,
                "malformed_rows": direct_malformed,
                "complete": direct_tables > 0 and direct_malformed == 0,
            },
        },
    }
    return areas, global_rows["indirect"], extraction, warnings


def _extract_areas(activity_section: str) -> list[dict[str, Any]]:
    """Backward-compatible activity-only wrapper used by older callers."""

    areas, _, _, _ = _extract_daily_content({"daily_activities": activity_section})
    return areas


def _project_candidates(known_projects: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for project in known_projects:
        if not isinstance(project, Mapping):
            continue
        title = _compact(str(project.get("title") or project.get("project_title") or ""))
        project_no = _compact(str(project.get("project_no") or ""))
        if not title and not project_no:
            continue
        candidates.append(
            {
                "project_id": project.get("project_id", project.get("id")),
                "title": title,
                "project_no": project_no,
            }
        )
    return candidates


def _normalized_identifier(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _match_project(
    project_no: str,
    project_title: str,
    known_projects: Iterable[Mapping[str, Any]],
) -> tuple[Any, dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = _project_candidates(known_projects)
    warnings: list[dict[str, Any]] = []
    if not candidates:
        return None, None, warnings

    if project_no:
        normalized_no = _normalized_identifier(project_no)
        exact = [
            candidate
            for candidate in candidates
            if candidate["project_no"]
            and _normalized_identifier(candidate["project_no"]) == normalized_no
        ]
        if len(exact) == 1:
            candidate = exact[0]
            return candidate["project_id"], {
                "method": "exact_project_no",
                "score": 100.0,
                "accepted": True,
                "candidate": candidate,
            }, warnings
        if len(exact) > 1:
            warnings.append(
                _warning(
                    "ambiguous_exact_project_number",
                    "Project number maps to more than one master project",
                    severity="error",
                    field="project_no",
                )
            )
            return None, None, warnings

    if not project_title:
        return None, None, warnings
    dependency = _load_rapidfuzz()
    if dependency is None:
        warnings.append(
            _warning(
                "rapidfuzz_unavailable",
                "RapidFuzz is unavailable; no non-critical project title suggestion was made",
                field="project_title",
            )
        )
        return None, None, warnings

    process, fuzz = dependency
    title_candidates = [candidate for candidate in candidates if candidate["title"]]
    ranked = process.extract(
        project_title,
        [candidate["title"] for candidate in title_candidates],
        scorer=fuzz.WRatio,
        limit=2,
    )
    if not ranked:
        return None, None, warnings
    _, best_score, best_index = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = float(best_score) - float(second_score)
    candidate = title_candidates[int(best_index)]
    suggestion = {
        "method": "rapidfuzz_title_suggestion",
        "score": round(float(best_score), 2),
        "margin": round(margin, 2),
        "accepted": False,
        "candidate": candidate,
    }
    if float(best_score) >= 95.0 and margin >= 8.0:
        # This remains a suggestion: project_id and project_no are critical
        # identity fields and are never assigned through fuzzy matching.
        suggestion["high_confidence_suggestion"] = True
    else:
        suggestion["high_confidence_suggestion"] = False
    warnings.append(
        _warning(
            "project_title_fuzzy_suggestion",
            "A non-critical title suggestion is available but was not auto-applied",
            field="project_title",
        )
    )
    return None, suggestion, warnings


def _field_confidence(
    value: str,
    provenance: Mapping[str, Any] | None,
) -> float:
    if not value or provenance is None:
        return 0.0
    method = provenance.get("method")
    if method == "label_regex":
        return 1.0
    if method in {"unique_iso_regex", "unique_day_regex"}:
        return 0.8
    if method == "master_data_from_exact_project_no":
        return 1.0
    return 0.7


def _deduplicate_warnings(warnings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for warning in warnings:
        key = (
            warning.get("code"),
            warning.get("field"),
            warning.get("page"),
            warning.get("message"),
        )
        if key not in seen:
            seen.add(key)
            result.append(warning)
    return result


def parse_daily_report_pages(
    pages: Sequence[str],
    *,
    known_projects: Iterable[Mapping[str, Any]] = (),
    source_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse already-extracted page text into a reviewable daily envelope.

    ``pages`` must contain one string per PDF page.  The function performs no
    I/O and is useful for deterministic re-parsing and tests.  It intentionally
    does not accept a combined list of PDF byte payloads.
    """

    if isinstance(pages, (str, bytes, bytearray)) or not isinstance(pages, Sequence):
        raise PDFValidationError("pages must be a sequence of page text strings")
    normalized_pages: list[str] = []
    for page in pages:
        if not isinstance(page, str):
            raise PDFValidationError("every extracted page must be text")
        normalized_pages.append(_normalize_page_text(page))

    warnings: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, Any]] = {}

    report_date, date_provenance, date_warnings = _extract_date(normalized_pages)
    warnings.extend(date_warnings)
    if date_provenance:
        provenance["date"] = date_provenance

    day_no, day_provenance, day_warnings = _extract_day_number(normalized_pages)
    warnings.extend(day_warnings)
    if day_provenance:
        provenance["day_no"] = day_provenance

    extracted: dict[str, str] = {}
    for field in ("project_no", "project_title", "customer", "location", "equipment"):
        value, field_provenance = _extract_labeled_value(normalized_pages, field)
        extracted[field] = value or ""
        if field_provenance:
            provenance[field] = field_provenance

    project_id, project_match, project_warnings = _match_project(
        extracted["project_no"],
        extracted["project_title"],
        known_projects,
    )
    warnings.extend(project_warnings)

    if project_match and project_match.get("method") == "exact_project_no":
        candidate = project_match["candidate"]
        if not extracted["project_title"] and candidate.get("title"):
            extracted["project_title"] = candidate["title"]
            provenance["project_title"] = {
                "method": "master_data_from_exact_project_no",
                "raw": candidate["title"],
            }
        elif (
            extracted["project_title"]
            and candidate.get("title")
            and _normalized_identifier(extracted["project_title"])
            != _normalized_identifier(candidate["title"])
        ):
            warnings.append(
                _warning(
                    "project_title_master_mismatch",
                    "Extracted project title differs from the exact project-number master record",
                    field="project_title",
                )
            )

    sections, section_matches = _collect_sections(normalized_pages)
    areas, indirect_manpower, manpower_extraction, manpower_warnings = (
        _extract_daily_content(sections)
    )
    warnings.extend(manpower_warnings)
    if sections.get("daily_activities") and not areas:
        warnings.append(
            _warning(
                "activities_require_manual_review",
                "Daily activity section was found but its table could not be mapped safely",
                field="areas",
            )
        )

    critical_values = {
        "date": report_date or "",
        "day_no": day_no or "",
        "project_no": extracted["project_no"],
    }
    for field, value in critical_values.items():
        if not value:
            warnings.append(
                _warning(
                    f"missing_{field}",
                    f"Critical field '{field}' was not deterministically extracted",
                    severity="error",
                    field=field,
                )
            )

    field_scores = {
        "date": _field_confidence(report_date or "", provenance.get("date")),
        "day_no": _field_confidence(day_no or "", provenance.get("day_no")),
        "project_no": _field_confidence(extracted["project_no"], provenance.get("project_no")),
        "project_title": _field_confidence(
            extracted["project_title"], provenance.get("project_title")
        ),
    }
    weights = {"date": 2, "day_no": 2, "project_no": 2, "project_title": 1}
    overall = sum(field_scores[key] * weights[key] for key in weights) / sum(weights.values())
    critical_complete = all(critical_values.values())
    if not critical_complete:
        overall = min(overall, 0.69)

    warnings = _deduplicate_warnings(warnings)
    has_error = any(warning.get("severity") == "error" for warning in warnings)
    data = {
        "date": report_date or "",
        "day_no": day_no or "",
        "project_no": extracted["project_no"],
        "project_title": extracted["project_title"],
        "location": extracted["location"],
        "customer": extracted["customer"],
        "equipment": extracted["equipment"],
        "photo_documentation_title": "",
        "prepared_by": "",
        "checked_by": "",
        "approved_by": "",
        "global_remarks": _compact(sections.get("remarks", "")),
        "weather": {},
        "indirect_manpower": indirect_manpower,
        "show_overall_progress": False,
        "overall_progress": [],
        "areas": areas,
        "sign_offs": [],
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "report_id": None,
        "revision": 1,
        # "ready" means ready for human confirmation, never automatically
        # approved for monthly aggregation.
        "status": "ready" if critical_complete and not has_error else "needs_review",
        "review_required": True,
        "project_id": project_id,
        "report_date": report_date,
        "day_no": day_no,
        "data": data,
        "source": dict(source_metadata or {}),
        "confidence": {
            "overall": round(overall, 4),
            "critical_complete": critical_complete,
            "fields": {key: round(value, 4) for key, value in field_scores.items()},
        },
        "warnings": warnings,
        "extraction": {
            "field_provenance": provenance,
            "sections": sections,
            "section_matches": section_matches,
            "project_match": project_match,
            "manpower": manpower_extraction,
            "completeness": {
                "manpower": manpower_extraction["completeness"],
            },
        },
    }


def import_daily_report_pdf(
    source: bytes | bytearray | memoryview | str | os.PathLike[str] | BinaryIO,
    *,
    filename: str | None = None,
    known_projects: Iterable[Mapping[str, Any]] = (),
    limits: ImportLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Validate and import exactly one PDF into a canonical review envelope.

    For a Flask ``FileStorage`` object, pass either the object directly or
    ``upload.stream`` together with ``filename=upload.filename``.  The stream
    position is restored after reading when it is seekable.
    """

    if not isinstance(limits, ImportLimits):
        raise TypeError("limits must be an ImportLimits instance")
    data, inferred_filename = _read_one_source(source, max_bytes=limits.max_bytes)
    pages, extraction_warnings = _extract_pdf_pages(data, limits)

    source_metadata = {
        "method": "uploaded_pdf",
        "filename": _clean_filename(filename or inferred_filename),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "page_count": len(pages),
        "parser_version": PARSER_VERSION,
        "pdf_version": data[5:8].decode("ascii", errors="replace"),
    }
    if b"%%EOF" not in data[-4096:]:
        extraction_warnings.append(
            _warning(
                "missing_eof_marker",
                "PDF EOF marker was not found near the end of the file",
            )
        )

    result = parse_daily_report_pages(
        pages,
        known_projects=known_projects,
        source_metadata=source_metadata,
    )
    result["warnings"] = _deduplicate_warnings(
        [*extraction_warnings, *result["warnings"]]
    )
    if any(warning.get("severity") == "error" for warning in result["warnings"]):
        result["status"] = "needs_review"
    return result


__all__ = [
    "DEFAULT_LIMITS",
    "ImportLimits",
    "PARSER_VERSION",
    "PDFDependencyError",
    "PDFExtractionError",
    "PDFImportError",
    "PDFValidationError",
    "SCHEMA_VERSION",
    "import_daily_report_pdf",
    "parse_daily_report_pages",
]
