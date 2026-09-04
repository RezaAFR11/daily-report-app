"""Public façade for legacy GPA Daily Report Excel workbooks.

The implementation is split into bounded OOXML access, workbook analysis, and
selected-sheet extraction. Keeping this module as the stable import surface
preserves the existing web integration and external API.
"""

from ._legacy_excel_analysis import analyze_legacy_daily_workbook
from ._legacy_excel_extract import extract_legacy_daily_records
from ._legacy_excel_ooxml import (
    DEFAULT_LIMITS,
    PARSER_VERSION,
    LegacyExcelError,
    LegacyExcelLimits,
)


__all__ = [
    "DEFAULT_LIMITS",
    "LegacyExcelError",
    "LegacyExcelLimits",
    "PARSER_VERSION",
    "analyze_legacy_daily_workbook",
    "extract_legacy_daily_records",
]
