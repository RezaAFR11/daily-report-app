import hashlib
import io
import unittest
from unittest.mock import patch

from monthly_report.importer import (
    DEFAULT_LIMITS,
    ImportLimits,
    PDFDependencyError,
    PDFExtractionError,
    PDFValidationError,
    import_daily_report_pdf,
    parse_daily_report_pages,
)


PDF_BYTES = b"%PDF-1.7\nsynthetic unit-test payload\n%%EOF\n"


class _FakePage:
    def __init__(self, text, *, old_api=False, error=False):
        self.text = text
        self.old_api = old_api
        self.error = error

    def extract_text(self, extraction_mode=None):
        if self.error:
            raise RuntimeError("synthetic extraction failure")
        if self.old_api and extraction_mode is not None:
            raise TypeError("old pypdf API")
        return self.text


def _reader_class(texts, *, encrypted=False, old_api=False, error_page=None):
    class _FakeReader:
        def __init__(self, stream, strict=False):
            self.stream = stream
            self.strict = strict
            self.is_encrypted = encrypted
            self.pages = [
                _FakePage(
                    text,
                    old_api=old_api,
                    error=index == error_page,
                )
                for index, text in enumerate(texts, start=1)
            ]

    return _FakeReader


def _warning_codes(result):
    return {warning["code"] for warning in result["warnings"]}


LAYOUT_REPORT = "\n".join(
    [
        "1. REPORT INFORMATION",
        "Project No.        PC-TEST-001",
        "Project Name       Turbine Reactivation Test",
        "Customer           PT Test Customer",
        "Location           Berau",
        "Equipment          Turbine Unit 2",
        "Date               2026-07-28",
        "Working Day        Day 403",
        "4. DAILY ACTIVITIES & MANPOWER BY AREA",
        "■ Turbine Unit 2",
        f"{'Activity Today':<40}Activity Tomorrow",
        f"{'1. Install simulator':<40}1. Run stroking test",
        f"{'2. Connect signal cable':<40}2. Verify permissive interlock",
        "5. CONSTRAINTS & ISSUES",
        "No constraints reported.",
        "6. REMARKS",
        "Sample remarks only.",
    ]
)


KNOWN_PROJECTS = [
    {
        "project_id": "project-test-001",
        "title": "Turbine Reactivation Test",
        "project_no": "PC-TEST-001",
    }
]


LEGACY_COMBINED_REPORT = "\n".join(
    [
        "1. REPORT INFORMATION",
        "Project No.        PC-LEGACY-001",
        "Project Name       Legacy Combined Project",
        "Date               2026-08-10",
        "Working Day        Day 10",
        "3. INDIRECT MANPOWER",
        f"{'No.':<8}{'Name':<38}{'Role / Position':<34}Working Hours",
        f"{'1':<8}{'Faiz Satria':<38}{'Project Control':<34}07:00 - 17:00",
        f"{'2':<8}{'Role Empty':<72}07:00 - 17:00",
        "4. DAILY ACTIVITIES & MANPOWER BY AREA",
        "\u25a0 Area Legacy",
        f"{'Activity Today':<44}Activity Tomorrow",
        f"{'1. Inspect panel':<44}1. Continue testing",
        "Direct Manpower — Area Legacy",
        f"{'No.':<8}{'Name':<25}{'Position':<25}{'Task Today':<30}Hours",
        f"{'1':<8}{'Worker One':<25}{'Technician':<25}{'Inspect panel':<30}07:00 - 17:00",
        f"{'2':<8}{'Worker Two':<25}{'Helper':<55}07:00 - 17:00",
        "5. CONSTRAINTS & ISSUES",
    ]
)


LEGACY_PAGE_BREAK_1 = "\n".join(
    [
        "1. REPORT INFORMATION",
        "Project No.        PC-LEGACY-002",
        "Project Name       Legacy Page Break Project",
        "Date               2026-08-11",
        "Working Day        Day 11",
        "3. INDIRECT MANPOWER",
        f"{'No.':<8}{'Name':<38}{'Role / Position':<34}Working Hours",
        f"{'1':<8}{'Indirect One':<38}{'Administration':<34}07:00 - 17:00",
        "5. DAILY ACTIVITIES & MANPOWER BY AREA",
        "\u25a0 Area Continued",
        f"{'Activity Today':<44}Activity Tomorrow",
        f"{'1. Start test':<44}1. Finish test",
        "Direct Manpower — Area Continued",
        f"{'No.':<8}{'Name':<25}{'Position':<25}{'Task Today':<30}Hours",
        "Daily Activity Report | footer from page one",
    ]
)


LEGACY_PAGE_BREAK_2 = "\n".join(
    [
        "Daily Activity Report | repeated page header",
        "Date: 2026-08-11 | Day: 11 | Project: PC-LEGACY-002",
        f"{'1':<8}{'Page Break Worker':<25}{'Woodward Engineer':<55}07:00 - 17:00",
        "Constraints: None",
        "6. CONSTRAINTS & ISSUES",
    ]
)


CURRENT_SPLIT_PAGE_1 = "\n".join(
    [
        "1. REPORT INFORMATION",
        "Project No.        PC-CURRENT-001",
        "Project Name       Current Split Project",
        "Date               2026-08-12",
        "Working Day        Day 12",
        "3. INDIRECT MANPOWER",
        f"{'No.':<8}{'Name':<38}{'Role / Position':<34}Working Hours",
        "4. DAILY ACTIVITIES BY AREA",
        "\u25a0 MA-42",
        f"{'Activity Today':<44}Activity Tomorrow",
        f"{'1. Install support':<44}1. Align support",
        "\u25a0 MA-39",
        f"{'Activity Today':<44}Activity Tomorrow",
        f"{'1. Pull cable':<44}1. Terminate cable",
        "5. DIRECT MANPOWER BY AREA",
        "\u25a0 MA-42",
        f"{'No.':<8}{'Name':<38}{'Role / Position':<34}Working Hours",
        f"{'1':<8}{'Worker MA42':<38}{'Foreman':<34}07:00 - 17:00",
    ]
)


CURRENT_SPLIT_PAGE_2 = "\n".join(
    [
        "Daily Activity Report | repeated page header",
        "\u25a0 MA-39",
        f"{'No.':<8}{'Name':<38}{'Role / Position':<34}Working Hours",
        f"{'1':<8}{'Worker MA39':<38}{'Technician':<34}07:00 - 17:00",
        "6. CONSTRAINTS & ISSUES",
    ]
)


class MonthlyPDFImporterTests(unittest.TestCase):
    def import_with_pages(self, pages, **kwargs):
        reader = _reader_class(pages)
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            return import_daily_report_pdf(PDF_BYTES, **kwargs)

    def test_import_builds_canonical_review_envelope(self):
        result = self.import_with_pages(
            [LAYOUT_REPORT],
            filename=r"C:\unsafe\folder\daily.pdf",
            known_projects=KNOWN_PROJECTS,
        )

        self.assertEqual(result["schema_version"], "daily-report-import/1")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["review_required"])
        self.assertEqual(result["project_id"], "project-test-001")
        self.assertEqual(result["report_date"], "2026-07-28")
        self.assertEqual(result["day_no"], "403")

        data = result["data"]
        self.assertEqual(data["date"], "2026-07-28")
        self.assertEqual(data["project_no"], "PC-TEST-001")
        self.assertEqual(data["project_title"], "Turbine Reactivation Test")
        self.assertEqual(data["customer"], "PT Test Customer")
        self.assertEqual(data["location"], "Berau")
        self.assertEqual(data["global_remarks"], "Sample remarks only.")
        self.assertEqual(
            data["areas"],
            [
                {
                    "id": "Turbine Unit 2",
                    "activities_today": [
                        "Install simulator",
                        "Connect signal cable",
                    ],
                    "activities_tomorrow": [
                        "Run stroking test",
                        "Verify permissive interlock",
                    ],
                    "manpower": [],
                    "indirect_manpower": [],
                    "constraints": "",
                    "remarks": "",
                    "activity_statuses": [],
                    "photos": [],
                }
            ],
        )

        source = result["source"]
        self.assertEqual(source["filename"], "daily.pdf")
        self.assertEqual(source["sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        self.assertEqual(source["size_bytes"], len(PDF_BYTES))
        self.assertEqual(source["page_count"], 1)
        self.assertEqual(source["pdf_version"], "1.7")
        self.assertTrue(result["confidence"]["critical_complete"])
        self.assertEqual(result["confidence"]["fields"]["date"], 1.0)

    def test_legacy_combined_profile_normalizes_global_and_area_manpower(self):
        result = parse_daily_report_pages([LEGACY_COMBINED_REPORT])

        self.assertEqual(
            result["data"]["indirect_manpower"],
            [
                {
                    "name": "Faiz Satria",
                    "role": "Project Control",
                    "task": "",
                    "hours": "07:00 - 17:00",
                },
                {
                    "name": "Role Empty",
                    "role": "",
                    "task": "",
                    "hours": "07:00 - 17:00",
                },
            ],
        )
        area = result["data"]["areas"][0]
        self.assertEqual(area["id"], "Area Legacy")
        self.assertEqual(area["activities_today"], ["Inspect panel"])
        self.assertEqual(area["activities_tomorrow"], ["Continue testing"])
        self.assertEqual(
            area["manpower"],
            [
                {
                    "name": "Worker One",
                    "role": "Technician",
                    "task": "Inspect panel",
                    "hours": "07:00 - 17:00",
                },
                {
                    "name": "Worker Two",
                    "role": "Helper",
                    "task": "",
                    "hours": "07:00 - 17:00",
                },
            ],
        )
        extraction = result["extraction"]["manpower"]
        self.assertEqual(extraction["profile"], "combined_activities_manpower")
        self.assertTrue(extraction["completeness"]["global_indirect"]["complete"])
        self.assertTrue(extraction["completeness"]["direct_by_area"]["complete"])
        self.assertTrue(
            any(row["source_section"] == "daily_activities" for row in extraction["rows"])
        )

    def test_legacy_combined_table_state_survives_a_page_break(self):
        result = parse_daily_report_pages(
            [LEGACY_PAGE_BREAK_1, LEGACY_PAGE_BREAK_2]
        )

        area = result["data"]["areas"][0]
        self.assertEqual(
            area["manpower"],
            [
                {
                    "name": "Page Break Worker",
                    "role": "Woodward Engineer",
                    "task": "",
                    "hours": "07:00 - 17:00",
                }
            ],
        )
        provenance = result["extraction"]["manpower"]["rows"]
        direct = next(row for row in provenance if row["category"] == "direct")
        self.assertEqual(direct["table_schema"], "header")
        self.assertTrue(direct["task_column"])
        self.assertNotIn("manpower_rows_require_manual_review", _warning_codes(result))

    def test_current_split_profile_merges_activity_and_manpower_areas(self):
        result = parse_daily_report_pages(
            [CURRENT_SPLIT_PAGE_1, CURRENT_SPLIT_PAGE_2]
        )

        self.assertEqual(result["extraction"]["manpower"]["profile"], "split_sections")
        self.assertEqual(result["data"]["indirect_manpower"], [])
        self.assertEqual(
            [
                (
                    area["id"],
                    area["activities_today"],
                    [row["name"] for row in area["manpower"]],
                )
                for area in result["data"]["areas"]
            ],
            [
                ("MA-42", ["Install support"], ["Worker MA42"]),
                ("MA-39", ["Pull cable"], ["Worker MA39"]),
            ],
        )
        completeness = result["extraction"]["completeness"]["manpower"]
        self.assertTrue(completeness["global_indirect"]["complete"])
        self.assertEqual(completeness["global_indirect"]["rows_extracted"], 0)
        self.assertEqual(completeness["direct_by_area"]["rows_extracted"], 2)

    def test_repeated_exact_project_title_is_removed_as_page_chrome(self):
        result = parse_daily_report_pages([
            CURRENT_SPLIT_PAGE_1,
            "Current Split Project\n" + CURRENT_SPLIT_PAGE_2,
        ])

        self.assertEqual(
            [(area["id"], area["activities_today"]) for area in result["data"]["areas"]],
            [("MA-42", ["Install support"]), ("MA-39", ["Pull cable"])],
        )
        ignored = result["extraction"]["normalization"]["ignored_document_chrome"]
        self.assertTrue(any(
            row["raw"] == "Current Split Project"
            and row["reason"] == "exact_document_context"
            for row in ignored
        ))
        self.assertNotIn("Imported PDF", {area["id"] for area in result["data"]["areas"]})

    def test_invalid_magic_header_is_rejected_before_pdf_reader(self):
        with patch("monthly_report.importer._load_pdf_reader") as loader:
            with self.assertRaisesRegex(PDFValidationError, "magic"):
                import_daily_report_pdf(b"not a pdf")
        loader.assert_not_called()

    def test_file_size_limit_is_enforced_per_pdf(self):
        limits = ImportLimits(max_bytes=10, max_pages=2, max_text_chars=100)
        with self.assertRaisesRegex(PDFValidationError, "per-file limit"):
            import_daily_report_pdf(PDF_BYTES, limits=limits)

    def test_default_file_size_limit_is_50_mib_and_boundary_is_inclusive(self):
        self.assertEqual(DEFAULT_LIMITS.max_bytes, 50 * 1024 * 1024)
        limits = ImportLimits(
            max_bytes=len(PDF_BYTES),
            max_pages=2,
            max_text_chars=10_000,
        )
        reader = _reader_class([LAYOUT_REPORT])
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            result = import_daily_report_pdf(PDF_BYTES, limits=limits)
        self.assertEqual(result["source"]["size_bytes"], len(PDF_BYTES))

    def test_page_count_limit_is_enforced(self):
        limits = ImportLimits(max_bytes=1024, max_pages=2, max_text_chars=1000)
        reader = _reader_class(["one", "two", "three"])
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            with self.assertRaisesRegex(PDFValidationError, "3 pages"):
                import_daily_report_pdf(PDF_BYTES, limits=limits)

    def test_extracted_text_limit_is_enforced(self):
        limits = ImportLimits(max_bytes=1024, max_pages=2, max_text_chars=10)
        reader = _reader_class(["x" * 11])
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            with self.assertRaisesRegex(PDFValidationError, "character limit"):
                import_daily_report_pdf(PDF_BYTES, limits=limits)

    def test_encrypted_pdf_is_rejected(self):
        reader = _reader_class([LAYOUT_REPORT], encrypted=True)
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            with self.assertRaisesRegex(PDFValidationError, "Encrypted"):
                import_daily_report_pdf(PDF_BYTES)

    def test_reader_errors_are_wrapped_without_leaking_parser_details(self):
        class BrokenReader:
            def __init__(self, stream, strict=False):
                raise RuntimeError("internal parser detail")

        with patch("monthly_report.importer._load_pdf_reader", return_value=BrokenReader):
            with self.assertRaisesRegex(PDFExtractionError, "opened safely") as raised:
                import_daily_report_pdf(PDF_BYTES)
        self.assertNotIn("internal parser detail", str(raised.exception))

    def test_missing_pypdf_reports_a_feature_dependency_error(self):
        with patch(
            "monthly_report.importer._load_pdf_reader",
            side_effect=PDFDependencyError("pypdf required"),
        ):
            with self.assertRaisesRegex(PDFDependencyError, "pypdf"):
                import_daily_report_pdf(PDF_BYTES)

    def test_import_accepts_only_one_pdf_and_restores_stream_position(self):
        with self.assertRaisesRegex(PDFValidationError, "exactly one"):
            import_daily_report_pdf([PDF_BYTES, PDF_BYTES])

        stream = io.BytesIO(PDF_BYTES)
        stream.seek(7)
        reader = _reader_class([LAYOUT_REPORT])
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            result = import_daily_report_pdf(stream, filename="stream.pdf")
        self.assertEqual(stream.tell(), 7)
        self.assertEqual(result["source"]["filename"], "stream.pdf")

    def test_older_pypdf_extract_text_api_falls_back_safely(self):
        reader = _reader_class([LAYOUT_REPORT], old_api=True)
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            result = import_daily_report_pdf(PDF_BYTES)
        self.assertEqual(result["report_date"], "2026-07-28")

    def test_page_extraction_failure_becomes_warning_and_review_status(self):
        reader = _reader_class([LAYOUT_REPORT, "ignored"], error_page=2)
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            result = import_daily_report_pdf(PDF_BYTES)
        self.assertIn("page_text_extraction_failed", _warning_codes(result))
        # Critical fields on page one remain deterministic, but a failed page
        # still requires human review through the explicit review flag.
        self.assertTrue(result["review_required"])

    def test_ambiguous_numeric_date_is_not_auto_selected(self):
        page = "\n".join(
            [
                "Project No. PC-TEST-001",
                "Project Name Turbine Reactivation Test",
                "Date 04/08/2026",
                "Working Day Day 403",
            ]
        )
        result = parse_daily_report_pages(page.split("\f"))

        self.assertIsNone(result["report_date"])
        self.assertEqual(result["data"]["date"], "")
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("ambiguous_numeric_date", _warning_codes(result))
        self.assertIn("missing_date", _warning_codes(result))

    def test_human_month_name_date_is_parsed_without_fuzzy_matching(self):
        page = "\n".join(
            [
                "Project No. PC-TEST-001",
                "Project Name Turbine Reactivation Test",
                "Date 4 Agustus 2026",
                "Working Day Day 403",
            ]
        )
        result = parse_daily_report_pages([page])
        self.assertEqual(result["report_date"], "2026-08-04")
        self.assertEqual(
            result["extraction"]["field_provenance"]["date"]["method"],
            "label_regex",
        )

    def test_date_is_parsed_from_combined_daily_header_line(self):
        page = "\n".join(
            [
                "Project No. PC-TEST-001",
                "Project Name Turbine Reactivation Test",
                "Date: 2026-06-19 | Day: 2 | Project: PC-TEST-001 DAY 2",
            ]
        )

        result = parse_daily_report_pages([page], known_projects=KNOWN_PROJECTS)

        self.assertEqual(result["report_date"], "2026-06-19")
        self.assertEqual(result["data"]["date"], "2026-06-19")
        self.assertNotIn("unrecognized_date_format", _warning_codes(result))

    def test_fuzzy_title_is_suggestion_only_and_never_sets_critical_identity(self):
        page = "\n".join(
            [
                # Deliberately misspelled critical labels. They must not be
                # recovered through RapidFuzz. The unique ISO date may still
                # be retained by the explicit regex fallback for review.
                "Project Mo. PC-WRONG-001",
                "Dote 2026-08-04",
                "Project Name Turbine Reactivaton Test",
                "Working Day Day 404",
            ]
        )

        class FakeProcess:
            @staticmethod
            def extract(query, choices, scorer=None, limit=2):
                return [
                    (choices[0], 98.0, 0),
                    (choices[1], 80.0, 1),
                ][:limit]

        class FakeFuzz:
            WRatio = object()

        projects = [
            KNOWN_PROJECTS[0],
            {
                "project_id": "other-project",
                "title": "Other Project",
                "project_no": "OTHER-001",
            },
        ]
        with patch(
            "monthly_report.importer._load_rapidfuzz",
            return_value=(FakeProcess, FakeFuzz),
        ):
            result = parse_daily_report_pages([page], known_projects=projects)

        self.assertEqual(result["data"]["project_no"], "")
        self.assertIsNone(result["project_id"])
        self.assertEqual(result["report_date"], "2026-08-04")
        self.assertEqual(
            result["extraction"]["field_provenance"]["date"]["method"],
            "unique_iso_regex",
        )
        match = result["extraction"]["project_match"]
        self.assertEqual(match["method"], "rapidfuzz_title_suggestion")
        self.assertTrue(match["high_confidence_suggestion"])
        self.assertFalse(match["accepted"])
        self.assertIn("missing_project_no", _warning_codes(result))
        self.assertIn("date_inferred_without_label", _warning_codes(result))

    def test_structural_title_subset_is_suggestion_only(self):
        page = "\n".join(
            [
                "Project No. PC-TEST-404-DAR",
                "Project Name Turbine Generator Reactivation Scope",
                "Date 2026-08-04",
                "Working Day Day 404",
            ]
        )
        projects = [{
            "project_id": "master-project",
            "title": "Turbine Generator Reactivation Scope for Kertas Nusantara",
            "project_no": "MASTER-001",
        }]

        result = parse_daily_report_pages([page], known_projects=projects)

        self.assertIsNone(result["project_id"])
        match = result["extraction"]["project_match"]
        self.assertEqual(match["method"], "ordered_title_subset")
        self.assertTrue(match["suggestion_only"])
        self.assertFalse(match["accepted"])
        self.assertFalse(match["high_confidence_suggestion"])
        self.assertIn("project_title_subset_suggestion", _warning_codes(result))

    def test_exact_project_number_accepts_configured_alias_with_provenance(self):
        page = "\n".join(
            [
                "Project No. MASTER-ALIAS-001",
                "Project Name Arbitrary Legacy Commissioning Name",
                "Date 2026-08-04",
                "Working Day Day 404",
            ]
        )
        projects = [{
            "project_id": "master-project",
            "title": "Canonical Turbine Reactivation Contract",
            "project_no": "MASTER-ALIAS-001",
            "title_aliases": ["Arbitrary Legacy Commissioning Name"],
        }]

        result = parse_daily_report_pages([page], known_projects=projects)

        self.assertEqual(result["project_id"], "master-project")
        self.assertNotIn("project_title_master_mismatch", _warning_codes(result))
        match = result["extraction"]["project_match"]
        self.assertEqual(match["method"], "exact_project_no")
        self.assertTrue(match["accepted"])
        self.assertEqual(match["title_match"]["method"], "approved_alias")
        self.assertEqual(
            match["title_match"]["alias"],
            "Arbitrary Legacy Commissioning Name",
        )

    def test_exact_project_number_can_fill_noncritical_missing_title(self):
        page = "\n".join(
            [
                "Project No. PC-TEST-001",
                "Date 2026-07-28",
                "Working Day Day 403",
            ]
        )
        result = parse_daily_report_pages([page], known_projects=KNOWN_PROJECTS)

        self.assertEqual(result["project_id"], "project-test-001")
        self.assertEqual(result["data"]["project_title"], "Turbine Reactivation Test")
        self.assertEqual(
            result["extraction"]["field_provenance"]["project_title"]["method"],
            "master_data_from_exact_project_no",
        )

    def test_blank_or_scanned_pdf_is_returned_for_manual_review(self):
        result = self.import_with_pages(["   "])
        self.assertEqual(result["status"], "needs_review")
        self.assertFalse(result["confidence"]["critical_complete"])
        self.assertIn("no_extractable_text", _warning_codes(result))
        self.assertIn("missing_date", _warning_codes(result))

    def test_missing_eof_marker_is_a_warning_not_an_unbounded_retry(self):
        truncated_marker_pdf = b"%PDF-1.7\nsynthetic payload"
        reader = _reader_class([LAYOUT_REPORT])
        with patch("monthly_report.importer._load_pdf_reader", return_value=reader):
            result = import_daily_report_pdf(truncated_marker_pdf)
        self.assertIn("missing_eof_marker", _warning_codes(result))

    def test_limits_reject_non_positive_values(self):
        for kwargs in (
            {"max_bytes": 0},
            {"max_pages": 0},
            {"max_text_chars": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ImportLimits(**kwargs)


if __name__ == "__main__":
    unittest.main()
