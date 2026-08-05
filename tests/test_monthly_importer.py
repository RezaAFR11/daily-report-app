import hashlib
import io
import unittest
from unittest.mock import patch

from monthly_report.importer import (
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

    def test_invalid_magic_header_is_rejected_before_pdf_reader(self):
        with patch("monthly_report.importer._load_pdf_reader") as loader:
            with self.assertRaisesRegex(PDFValidationError, "magic"):
                import_daily_report_pdf(b"not a pdf")
        loader.assert_not_called()

    def test_file_size_limit_is_enforced_per_pdf(self):
        limits = ImportLimits(max_bytes=10, max_pages=2, max_text_chars=100)
        with self.assertRaisesRegex(PDFValidationError, "per-file limit"):
            import_daily_report_pdf(PDF_BYTES, limits=limits)

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
