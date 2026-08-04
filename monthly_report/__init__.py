"""Canonical daily-report storage and monthly aggregation helpers.

The package is deliberately independent from Flask so it can be exercised by
unit tests, CLI migration tools, and future background workers.
"""

from .aggregate import aggregate_monthly_records
from .storage import (
    CANONICAL_SCHEMA_VERSION,
    archive_final_daily_json,
    archive_final_daily_record,
    list_canonical_records,
    load_canonical_record,
)

__all__ = [
    "CANONICAL_SCHEMA_VERSION",
    "aggregate_monthly_records",
    "archive_final_daily_json",
    "archive_final_daily_record",
    "list_canonical_records",
    "load_canonical_record",
]
