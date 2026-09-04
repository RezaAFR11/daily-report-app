"""Workbook-level manifest analysis for legacy Daily Report Excel sources."""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

from ._legacy_excel_ooxml import (
    DEFAULT_LIMITS,
    PARSER_VERSION,
    LegacyExcelError,
    LegacyExcelLimits,
    dominant,
    read_xml,
    shared_strings,
    sheet_profile,
    validate_archive,
    warning,
    workbook_sheets,
    worksheet_rows,
)


_PROJECT_FIELDS = (
    "project_no",
    "project_title",
    "customer",
    "location",
    "equipment",
)
_VARIANT_FIELDS = ("project_title", "customer", "location", "equipment")


def _collect_profiles(archive, limits: LegacyExcelLimits) -> tuple[list[dict[str, Any]], list[str]]:
    shared = shared_strings(archive, limits)
    profiles: list[dict[str, Any]] = []
    ignored: list[str] = []
    for sheet in workbook_sheets(archive, limits):
        root = read_xml(archive, sheet["path"], limits)
        profile = sheet_profile(
            sheet["name"],
            sheet["path"],
            worksheet_rows(root, shared),
        )
        if profile is None:
            ignored.append(sheet["name"])
        else:
            profiles.append(profile)
    return profiles, ignored


def _manifest_warnings(
    profiles: list[dict[str, Any]],
    ignored: list[str],
    duplicates: list[str],
) -> list[dict[str, str]]:
    warnings = [warning_row for item in profiles for warning_row in item["warnings"]]
    if duplicates:
        warnings.append(warning(
            "duplicate_sheet_dates",
            f"More than one worksheet exists for: {', '.join(duplicates)}.",
            severity="error",
            field="date",
        ))
    if ignored:
        warnings.append(warning(
            "ignored_non_daily_sheets",
            f"Ignored {len(ignored)} worksheet(s) without a DD.MM.YY date name.",
            severity="info",
            field="sheet_name",
        ))
    return warnings


def _project_manifest(
    profiles: list[dict[str, Any]],
    warnings: list[dict[str, str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    fields = {
        key: dominant(item["fields"].get(key, "") for item in profiles)
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
            warnings.append(warning(
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

    path, archive = validate_archive(source, limits)
    try:
        profiles, ignored = _collect_profiles(archive, limits)
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
    warnings = _manifest_warnings(profiles, ignored, duplicates)
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


__all__ = ["analyze_legacy_daily_workbook"]
