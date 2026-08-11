import io
import json
import os
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from monthly_report.overtime import (
    FORMULA_VERSION,
    parse_overtime_workbook,
    parse_overtime_workbooks,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_name(index):
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _workbook_bytes(sheets):
    """Build the minimal OOXML package needed by the parser."""

    ET.register_namespace("", MAIN_NS)
    ET.register_namespace("r", REL_NS)
    workbook = ET.Element(f"{{{MAIN_NS}}}workbook")
    sheet_list = ET.SubElement(workbook, f"{{{MAIN_NS}}}sheets")
    relationships = ET.Element(f"{{{PKG_REL_NS}}}Relationships")
    worksheet_xml = []
    for sheet_index, sheet in enumerate(sheets, start=1):
        attributes = {
            "name": sheet["name"],
            "sheetId": str(sheet_index),
            f"{{{REL_NS}}}id": f"rId{sheet_index}",
        }
        if sheet.get("state"):
            attributes["state"] = sheet["state"]
        ET.SubElement(sheet_list, f"{{{MAIN_NS}}}sheet", attributes)
        ET.SubElement(
            relationships,
            f"{{{PKG_REL_NS}}}Relationship",
            {
                "Id": f"rId{sheet_index}",
                "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                "Target": f"worksheets/sheet{sheet_index}.xml",
            },
        )
        worksheet = ET.Element(f"{{{MAIN_NS}}}worksheet")
        sheet_data = ET.SubElement(worksheet, f"{{{MAIN_NS}}}sheetData")
        for row_index, values in enumerate(sheet["rows"], start=1):
            row = ET.SubElement(sheet_data, f"{{{MAIN_NS}}}row", {"r": str(row_index)})
            for column_index, value in enumerate(values):
                if value in (None, ""):
                    continue
                reference = f"{_column_name(column_index)}{row_index}"
                cell = ET.SubElement(
                    row,
                    f"{{{MAIN_NS}}}c",
                    {"r": reference, "t": "inlineStr"},
                )
                inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
                text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
                text.text = str(value)
        worksheet_xml.append(ET.tostring(worksheet, encoding="utf-8", xml_declaration=True))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            ET.tostring(workbook, encoding="utf-8", xml_declaration=True),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            ET.tostring(relationships, encoding="utf-8", xml_declaration=True),
        )
        for index, value in enumerate(worksheet_xml, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", value)
    return output.getvalue()


def _daily_sheet(name, date_text, records=(), context=(), state="visible"):
    rows = [
        ["ABSENSI MANPOWER PT. GARUDA PRIMA AKSARA OVERTIME"],
        [],
        [date_text],
    ]
    rows.extend([[value] for value in context])
    rows.append(["No", "Nama", "Posisi", "Overtime"])
    rows.extend(
        [[index, employee, role, interval] for index, (employee, role, interval) in enumerate(records, 1)]
    )
    return {"name": name, "state": state, "rows": rows}


class OvertimeParserTests(unittest.TestCase):
    def test_accepts_named_bytes_from_multipart_upload_adapter(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "Uploaded",
                    "Date: 10 Agustus 2026 (MA-42)",
                    [("Ari", "Technician", "17.00-21.00")],
                )
            ]
        )

        result = parse_overtime_workbooks(
            [("named-overtime.xlsx", workbook)],
            period_start="2026-08-10",
            period_end="2026-08-10",
        )

        self.assertEqual(result["manifest"]["files"][0]["filename"], "named-overtime.xlsx")
        self.assertEqual(result["totals"]["selected_confirmed_elapsed_hours"], 4)

    def test_parses_localised_dates_context_and_aggregates_same_date_sheets(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "Area A",
                    "Date: 10 Agustus 2026 (MA-42)",
                    [("Ari", "Technician", "17.00 - 21.00")],
                    ["Description Job: Cable termination", "Total Progress: 70%"],
                ),
                _daily_sheet(
                    "Area B",
                    "Date: 10 August 2026 (MA-41)",
                    [("Budi", "Helper", "17:00–22:00")],
                    ["Description Job: Transformer moving"],
                ),
            ]
        )

        result = parse_overtime_workbook(workbook)

        self.assertEqual(result["formula_version"], FORMULA_VERSION)
        self.assertEqual(result["totals"]["populated_sheet_count"], 2)
        self.assertEqual(result["totals"]["raw_record_count"], 2)
        self.assertEqual(result["totals"]["confirmed_elapsed_hours"], 9)
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["daily"][0]["areas"], ["MA-41", "MA-42"])
        self.assertEqual(result["daily"][0]["employee_count"], 2)
        sheet = result["manifest"]["files"][0]["sheets"][0]
        self.assertEqual(sheet["job_description"], ["Cable termination"])
        self.assertEqual(sheet["progress"], ["Total Progress: 70%"])
        json.dumps(result)

    def test_period_filter_blank_template_and_annotation_are_review_first(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "18 Jun",
                    "Date: 18 Juni 2026 (Area 42)",
                    [("Ari", "Technician", "17.00-19.00 (sakit)")],
                ),
                _daily_sheet("19 Jun", "Date: 19 June 2026", []),
                _daily_sheet(
                    "Outside",
                    "Date: 22 Juni 2026",
                    [("Budi", "Helper", "17.00-21.00")],
                ),
            ]
        )

        result = parse_overtime_workbook(
            workbook,
            period_start="2026-06-18",
            period_end="2026-06-20",
        )

        self.assertEqual(result["totals"]["blank_template_sheet_count"], 1)
        self.assertEqual(result["totals"]["selected_record_count"], 1)
        self.assertEqual(result["totals"]["selected_confirmed_elapsed_hours"], 2)
        self.assertTrue(result["records"][0]["requires_review"])
        self.assertEqual(result["records"][0]["notes"], "(sakit)")
        self.assertEqual(
            result["coverage"]["not_supplied_dates"],
            ["2026-06-19", "2026-06-20"],
        )
        self.assertIn("2026-06-19", result["coverage"]["blank_template_dates"])
        self.assertFalse(result["coverage"]["blank_templates_count_as_coverage"])

    def test_deduplicates_files_and_exact_records_but_sums_disjoint_intervals(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "First",
                    "Date: 29 April 2026",
                    [
                        ("Hardi", "Technician", "17.00-18.00"),
                        ("Hardi", "Technician", "19.00-20.00"),
                    ],
                ),
                _daily_sheet(
                    "Duplicate",
                    "Date: 29 April 2026",
                    [("Hardi", "Technician", "17.00-18.00")],
                ),
            ]
        )

        result = parse_overtime_workbooks([workbook, workbook])

        self.assertEqual(result["manifest"]["file_count"], 2)
        self.assertEqual(result["manifest"]["duplicate_file_count"], 1)
        self.assertEqual(result["totals"]["raw_record_count"], 3)
        self.assertEqual(result["totals"]["unique_record_count"], 2)
        self.assertEqual(result["totals"]["confirmed_elapsed_hours"], 2)
        self.assertEqual(result["totals"]["conflict_count"], 1)
        self.assertEqual(result["conflicts"][0]["type"], "exact_duplicate")
        codes = {warning["code"] for warning in result["warnings"]}
        self.assertIn("DUPLICATE_OVERTIME_FILE", codes)
        self.assertIn("EXACT_OVERTIME_DUPLICATE", codes)

    def test_overlapping_and_cross_midnight_intervals_are_not_silently_totalled(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "Overlap",
                    "Date: 5 July 2026",
                    [
                        ("Ari", "Technician", "17.00-20.00"),
                        ("Ari", "Technician", "19.00-21.00"),
                        ("Budi", "Helper", "22.00-02.00"),
                    ],
                )
            ]
        )

        result = parse_overtime_workbook(workbook)

        self.assertEqual(result["totals"]["unique_elapsed_hours"], 5)
        self.assertEqual(result["totals"]["confirmed_elapsed_hours"], 0)
        self.assertEqual(result["totals"]["conflict_count"], 1)
        cross_midnight = next(row for row in result["records"] if row["employee"] == "Budi")
        self.assertTrue(cross_midnight["cross_midnight"])
        self.assertIsNone(cross_midnight["duration_hours"])
        self.assertEqual(cross_midnight["suggested_duration_hours"], 4)
        self.assertFalse(cross_midnight["included_in_total"])

    def test_hidden_sheets_are_not_parsed(self):
        workbook = _workbook_bytes(
            [
                _daily_sheet(
                    "Visible",
                    "Date: 1 July 2026",
                    [("Ari", "Technician", "17.00-18.00")],
                ),
                _daily_sheet(
                    "Hidden",
                    "Date: 2 July 2026",
                    [("Budi", "Helper", "17.00-22.00")],
                    state="hidden",
                ),
            ]
        )

        result = parse_overtime_workbook(workbook)

        self.assertEqual(result["totals"]["workbook_sheet_count"], 2)
        self.assertEqual(result["totals"]["visible_sheet_count"], 1)
        self.assertEqual(result["totals"]["raw_elapsed_hours"], 1)

    def test_rejects_unsafe_high_ratio_xlsx_archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", b"0" * 1_000_000)
        with self.assertRaisesRegex(ValueError, "unsafe compression ratio"):
            parse_overtime_workbook(output.getvalue())


class ActualOvertimeWorkbookGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        default = Path.home() / "Downloads" / "ABSEN MANPOWER OVERTIME ELECTRICAL 2026.xlsx"
        cls.workbook = Path(os.environ.get("GPA_OVERTIME_GOLDEN_XLSX", default))

    def setUp(self):
        if not self.workbook.is_file():
            self.skipTest(f"Golden overtime workbook not available: {self.workbook}")

    def test_actual_workbook_shape_and_elapsed_hours(self):
        result = parse_overtime_workbook(self.workbook)

        self.assertEqual(result["totals"]["workbook_sheet_count"], 48)
        self.assertEqual(result["totals"]["visible_sheet_count"], 48)
        self.assertEqual(result["totals"]["populated_sheet_count"], 41)
        self.assertEqual(result["totals"]["blank_template_sheet_count"], 7)
        self.assertEqual(result["totals"]["raw_record_count"], 735)
        self.assertEqual(result["totals"]["raw_elapsed_hours"], 2696)
        self.assertEqual(result["totals"]["unique_record_count"], 734)
        self.assertEqual(result["totals"]["unique_elapsed_hours"], 2691)
        self.assertTrue(
            any(conflict["type"] == "exact_duplicate" for conflict in result["conflicts"])
        )

    def test_actual_workbook_15_to_21_june_period(self):
        result = parse_overtime_workbook(
            self.workbook,
            period_start="2026-06-15",
            period_end="2026-06-21",
        )

        self.assertEqual(result["totals"]["selected_record_count"], 24)
        self.assertEqual(result["totals"]["selected_employee_count"], 24)
        self.assertEqual(result["totals"]["selected_confirmed_elapsed_hours"], 96)
        self.assertEqual([row["date"] for row in result["daily"]], ["2026-06-18"])
        self.assertEqual(
            result["coverage"]["not_supplied_dates"],
            [
                "2026-06-15",
                "2026-06-16",
                "2026-06-17",
                "2026-06-19",
                "2026-06-20",
                "2026-06-21",
            ],
        )
        self.assertEqual(result["totals"]["conflict_count"], 0)
        self.assertFalse(result["requires_manual_review"])


if __name__ == "__main__":
    unittest.main()
