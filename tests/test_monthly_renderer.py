import io
import re
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from monthly_report import renderer
from monthly_report.renderer import render_monthly_report


def _page_count(pdf_bytes: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf_bytes))


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


class MonthlyReportRendererTests(unittest.TestCase):
    def test_minimal_report_returns_rewound_seven_page_pdf(self):
        result = render_monthly_report({})

        self.assertIsInstance(result, io.BytesIO)
        self.assertEqual(result.tell(), 0)
        self.assertTrue(result.getvalue().startswith(b"%PDF-"))
        # Cover, TOC, executive/safety, engineering, procurement, site,
        # appendices.  The blank eighth source-template page is not emitted.
        self.assertEqual(_page_count(result.getvalue()), 7)

    def test_review_schema_progress_adds_generated_s_curve_page(self):
        report = {
            "status": "mtd",
            "project_name": "Runtime Project",
            "project_no": "PROJECT-001",
            "reporting_period": "2026-07-01 to 2026-07-31",
            "issued_date": "2026-08-04",
            "progress": {
                "rows": [
                    {
                        "description": "Engineering",
                        "previous": 40,
                        "this_month": 5,
                        "to_date": 45,
                        "plan": 46,
                        "variance": -1,
                    },
                    {
                        "description": "Total Overall",
                        "previous": 29,
                        "this_month": 6,
                        "to_date": 35,
                        "plan": 36.5,
                        "variance": -1.5,
                        "is_total": True,
                    },
                ]
            },
            "safety": {
                "total_manpower": 120,
                "total_man_hours": 28640,
                "recordable_cases": 0,
                "lost_workdays": 0,
                "lost_time_injuries": 0,
            },
            "engineering": {"summary": "Engineering review is in progress."},
            "procurement": {"summary": "Procurement review is in progress."},
            "site": {
                "this_month_activities": ["Illustrative activity"],
                "next_month_activities": ["Illustrative plan"],
                "concerns": [
                    {"concern": "Illustrative concern", "corrective_action": "Review"}
                ],
            },
        }

        result = render_monthly_report(report)

        self.assertEqual(_page_count(result.getvalue()), 8)
        self.assertEqual(result.tell(), 0)

    def test_internal_line_breaks_and_safety_bullets_do_not_render_as_markup(self):
        result = render_monthly_report({
            "progress": {
                "rows": [{
                    "description": "Total Overall",
                    "previous": 10,
                    "this_month": 5,
                    "plan": 16,
                    "is_total": True,
                }]
            },
            "safety": {"total_manpower": 25},
            "procurement": {"rows": []},
        })

        text = _pdf_text(result.getvalue())
        self.assertNotIn("<br/>", text)
        self.assertNotIn("&#8226;", text)
        self.assertNotIn("&# 82", text)
        self.assertIn("To-date Plan", text)
        self.assertIn("Total Manpower", text)
        self.assertIn("Supplier/Vendor/", text)

    def test_s_curve_can_be_disabled(self):
        report = {
            "status": "final",
            "include_s_curve": False,
            "progress": {
                "rows": [
                    {
                        "description": "Total Overall",
                        "previous": 10,
                        "this_month": 5,
                        "plan": 16,
                    }
                ]
            },
        }

        result = render_monthly_report(report)

        self.assertEqual(_page_count(result.getvalue()), 7)

    def test_long_runtime_text_and_tables_split_without_layout_error(self):
        long_text = "Wrapped runtime text with XML chars & < > " * 350
        report = {
            "status": "draft",
            "project_name": "A very long runtime project title " * 12,
            "engineering": {"summary": long_text},
            "procurement": {
                "summary": "Procurement summary & review",
                "rows": [
                    {
                        "po_number": f"PO-{index:03d}",
                        "po_name": "Long equipment package description " * 5,
                        "supplier": "Vendor & Contractor",
                        "status": "Under review " * 4,
                    }
                    for index in range(45)
                ],
            },
            "equipment_delivery": {
                "rows": [
                    {
                        "equipment": "Equipment " * 10,
                        "supplier": "Supplier",
                        "status": "In transit",
                        "expected_delivery": "2026-08-15",
                        "actual_delivery": "",
                    }
                    for _ in range(20)
                ]
            },
            "shipments": [
                {
                    "shipment_no": f"S-{index:03d}",
                    "description": "Shipment description " * 5,
                    "po_number": f"PO-{index:03d}",
                    "expected_loading_port": "Port of Loading",
                    "arriving_port": "Port of Arrival",
                    "etd": "2026-08-10",
                    "actual_departure_date": "",
                }
                for index in range(35)
            ],
            "site": {
                "this_month_activities": [f"Activity {index}: {long_text[:300]}" for index in range(30)],
                "next_month_activities": [f"Plan {index}: {long_text[:220]}" for index in range(20)],
                "concerns": [
                    {"concern": long_text[:500], "corrective_action": long_text[:500]}
                    for _ in range(12)
                ],
            },
        }

        result = render_monthly_report(report)

        self.assertTrue(result.getvalue().startswith(b"%PDF-"))
        self.assertGreater(_page_count(result.getvalue()), 7)

    def test_invalid_logo_uses_vector_fallback(self):
        with patch(
            "monthly_report.renderer._draw_logo_fallback",
            wraps=renderer._draw_logo_fallback,
        ) as fallback:
            result = render_monthly_report({}, logo_path="missing-logo-file.png")

        self.assertTrue(result.getvalue().startswith(b"%PDF-"))
        self.assertGreater(fallback.call_count, 0)

    def test_progress_normalization_calculates_missing_fields(self):
        rows = renderer._normalise_progress({
            "rows": [
                {
                    "description": "Engineering",
                    "previous": "40,5%",
                    "this_month": "4.5%",
                    "plan": "46%",
                }
            ]
        })

        self.assertEqual(rows[0]["to_date"], 45.0)
        self.assertEqual(rows[0]["variance"], -1.0)

    def test_status_is_safe_and_explicit(self):
        self.assertEqual(renderer._normalise_status("month-to-date"), "MTD")
        self.assertEqual(renderer._normalise_status("approved"), "FINAL")
        self.assertEqual(renderer._normalise_status("unexpected"), "DRAFT")

    def test_non_mapping_report_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "report must be a mapping"):
            render_monthly_report([])


if __name__ == "__main__":
    unittest.main()
