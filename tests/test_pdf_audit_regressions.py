"""Small regressions for the historical PDF audit; no full report rendering."""
import unittest
from unittest.mock import patch

from monthly_report.importer import parse_daily_report_pages
from monthly_report.photos import _area_heading_photo_context, _attach_photo_contexts


class PDFAuditRegressionTests(unittest.TestCase):
    def test_wrapped_bold_heading_is_complete_for_all_four_cards(self):
        fragments = []
        boxes = []
        captions = ["Woodward Training", "Function test", "Draining oil", "Auto Test Hydraulic Turning Gear"]
        for index, caption in enumerate(captions):
            x = [53, 219, 385, 53][index]
            top = 665 if index < 3 else 455
            boxes.append((x, top - 147, x + 155, top))
            for text, y, font in [
                ("Cold Commissioning Activities - Day 12 -", top + 41, "/Helvetica-Bold"),
                ("Turbines & Generators", top - 49, "/Helvetica-Bold"),
                (caption, top + 9, "/Helvetica-Oblique"),
            ]:
                fragments.append({"text": text, "x": x, "y": y, "font": font})
        with patch("monthly_report.photos._page_text_fragments", return_value=fragments):
            contexts = [_area_heading_photo_context(None, box, [{"id": "Turbines & Generators"}]) for box in boxes]
        self.assertEqual([c["caption"] for c in contexts], captions)
        self.assertEqual({c["area"] for c in contexts}, {"Cold Commissioning Activities - Day 12 - Turbines & Generators"})
        self.assertFalse(any(c["review_required"] for c in contexts))

    def test_wrapped_caption_stays_with_its_card_despite_bad_line_coordinates(self):
        fragments = [
            {"text": text, "x": 385, "y": y}
            for text, y in [
                ("Turbine Unit 2", 649),
                ("LO Bypass line Oil flushing (Inlet line", 655),
                ("Bearing 2 & Outlet line Bearing 4 4). 10", 555),
                ("minutes flushing", 455),
                ("Turbine Unit 2", 467),
                ("LO Bypass line Oil flushing (Outlet line", 463),
                ("Bearing 2) 10 minutes flushing", 388),
            ]
        ]
        with patch("monthly_report.photos._page_text_fragments", return_value=fragments):
            first = _area_heading_photo_context(None, (385, 485, 530, 626), [{"id": "Turbine Unit 2"}])
            second = _area_heading_photo_context(None, (385, 304, 530, 445), [{"id": "Turbine Unit 2"}])
        self.assertEqual(first["area"], "Turbine Unit 2")
        self.assertEqual(first["caption"], "LO Bypass line Oil flushing (Inlet line Bearing 2 & Outlet line Bearing 4 4). 10 minutes flushing")
        self.assertEqual(second["caption"], "LO Bypass line Oil flushing (Outlet line Bearing 2) 10 minutes flushing")

    def test_unverified_area_requires_review_even_with_caption(self):
        candidate = {"page": 1, "_bbox": (50, 450, 200, 597)}
        context = {"area": "", "caption": "Long activity caption", "review_required": True}
        with patch("monthly_report.photos._area_heading_photo_context", return_value=context), \
             patch("monthly_report.photos._entry_position_matches", return_value=[]), \
             patch("monthly_report.photos._page_text", return_value=""):
            _attach_photo_contexts([candidate], [None], {1}, [])
        self.assertTrue(candidate["caption_review_required"])
        self.assertNotEqual(candidate["caption_match_confidence"], "high")

    def test_stacked_year_is_preserved_and_requires_review(self):
        page = "Project No. NO. 119/KN-GPA/EPC-2K-P2/VIII/20256\nProject Title: Electrical Construction\nDate: 2026-08-03\nDay: 119"
        result = parse_daily_report_pages([page])
        self.assertEqual(result["data"]["project_no"], "NO. 119/KN-GPA/EPC-2K-P2/VIII/20256")
        issue = next(w for w in result["warnings"] if w["code"] == "invalid_document_number_year")
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(result["status"], "needs_review")

    def test_valid_document_year_is_not_flagged(self):
        for year in ("2025", "2026"):
            result = parse_daily_report_pages([
                f"Project No. NO. 119/KN-GPA/EPC-2K-P2/VIII/{year}\nDate: 2026-08-03\nDay: 119"
            ])
            self.assertNotIn("invalid_document_number_year", {w["code"] for w in result["warnings"]})


if __name__ == "__main__":
    unittest.main()
