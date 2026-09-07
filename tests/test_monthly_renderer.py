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


def _outline_destinations(reader: PdfReader, nodes=None):
    result = []
    for node in reader.outline if nodes is None else nodes:
        if isinstance(node, list):
            result.extend(_outline_destinations(reader, node))
        else:
            result.append((node.title, reader.get_destination_page_number(node)))
    return result


class MonthlyReportRendererTests(unittest.TestCase):
    def test_minimal_report_hides_every_empty_chapter(self):
        result = render_monthly_report({})

        self.assertIsInstance(result, io.BytesIO)
        self.assertEqual(result.tell(), 0)
        self.assertTrue(result.getvalue().startswith(b"%PDF-"))
        # Only the cover and TOC remain when every report chapter is empty.
        self.assertEqual(_page_count(result.getvalue()), 2)
        text = _pdf_text(result.getvalue())
        self.assertNotIn("Executive Summary", text)
        self.assertNotIn("Engineering", text)
        self.assertNotIn("Procurement", text)
        self.assertNotIn("Appendices", text)

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

        self.assertEqual(_page_count(result.getvalue()), 7)
        self.assertEqual(result.tell(), 0)
        reader = PdfReader(io.BytesIO(result.getvalue()))
        appendix_page = reader.pages[-1].extract_text() or ""
        self.assertIn("6. Appendices", appendix_page)
        self.assertIn("Appendix 6.1 - Progress S-Curve", appendix_page)

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
        self.assertNotIn("Procurement Status", text)

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

        self.assertEqual(_page_count(result.getvalue()), 3)

    def test_nonempty_chapters_and_subchapters_are_renumbered_without_gaps(self):
        result = render_monthly_report({
            "engineering": {"summary": "Issued drawing review completed."},
            "equipment_delivery": {"rows": [{
                "equipment": "Governor valve",
                "supplier": "Vendor A",
                "status": "Delivered",
            }]},
            "site": {"next_month_activities": ["Continue installation"]},
            "appendices": [{
                "number": "6.8",
                "title": "QC Document",
                "status": "Attached",
                "content": ["Inspection record attached"],
            }],
        })

        text = _pdf_text(result.getvalue())
        self.assertIn("1. Engineering", text)
        self.assertIn("1.1 Status Engineering", text)
        self.assertIn("2. Procurement", text)
        self.assertIn("2.1 Equipment Delivery Status", text)
        self.assertNotIn("Procurement Status", text)
        self.assertNotIn("Shipment Status", text)
        self.assertIn("3. Site Services / Construction", text)
        self.assertIn("3.1 Planned Activities Next Month", text)
        self.assertNotIn("This Month Activities", text)
        self.assertNotIn("Project Schedule Status", text)
        self.assertIn("4. Appendices", text)
        self.assertIn("Appendix 4.1 - QC Document", text)
        self.assertNotIn("Executive Summary", text)
        self.assertNotIn("Safety Status", text)

    def test_empty_concern_is_hidden_and_findings_receive_the_next_number(self):
        base = {
            "report_type": "weekly",
            "progress": {"rows": [{
                "description": "Total Overall",
                "previous": 10,
                "this_week": 2,
                "to_date": 12,
                "plan": 13,
                "is_total": True,
            }]},
            "safety": {"total_manpower": 12},
            "site": {
                "schedule_status": "On track",
                "this_week_activities": ["Alignment"],
                "next_week_activities": ["Continue alignment"],
                "constraint_reporting": {
                    "not_supplied_dates": ["2026-01-01"],
                },
                "concerns": [{"concern": "No constraint", "corrective_action": ""}],
                "key_findings": [{
                    "date": "2026-01-01",
                    "area": "Generator 2",
                    "text": "Waiting for coordination",
                }],
            },
        }

        text = _pdf_text(render_monthly_report(base).getvalue())

        self.assertNotIn("Area of Concern and Suggested Corrective Action", text)
        self.assertNotIn("Concern and closeout information was not supplied", text)
        self.assertIn("3.4 Key Remarks / Findings", text)

        with_concern = dict(base)
        with_concern["site"] = dict(base["site"])
        with_concern["site"]["concerns"] = [{
            "concern": "Oil flushing coordination is pending",
            "corrective_action": "Coordinate flushing schedule",
        }]
        text = _pdf_text(render_monthly_report(with_concern).getvalue())
        self.assertIn("3.4 Area of Concern and Suggested Corrective Action", text)
        self.assertIn("3.5 Key Remarks / Findings", text)

    def test_outline_destinations_resolve_to_each_heading_page(self):
        report = {
            "report_type": "weekly",
            "progress": {"rows": [{
                "description": "Total Overall",
                "previous": 10,
                "this_week": 2,
                "to_date": 12,
                "plan": 13,
                "is_total": True,
            }]},
            "site": {
                "schedule_status": "On track",
                "this_week_activities": ["Alignment"],
                "next_week_activities": ["Continue alignment"],
                "key_findings": [{"text": "Waiting for coordination"}],
            },
        }

        reader = PdfReader(io.BytesIO(render_monthly_report(report).getvalue()))
        destinations = _outline_destinations(reader)

        self.assertGreater(len(destinations), 4)
        self.assertGreater(len({page for _, page in destinations}), 1)
        for title, page_index in destinations:
            with self.subTest(title=title):
                self.assertIn(title, reader.pages[page_index].extract_text() or "")

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

    def test_progress_normalization_does_not_invent_missing_source_fields(self):
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

        self.assertIsNone(rows[0]["to_date"])
        self.assertIsNone(rows[0]["variance"])

    def test_status_is_safe_and_explicit(self):
        self.assertEqual(renderer._normalise_status("month-to-date"), "MTD")
        self.assertEqual(renderer._normalise_status("approved"), "FINAL")
        self.assertEqual(renderer._normalise_status("unexpected"), "DRAFT")

    def test_non_mapping_report_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "report must be a mapping"):
            render_monthly_report([])


if __name__ == "__main__":
    unittest.main()
