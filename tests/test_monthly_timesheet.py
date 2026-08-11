import html
import io
import json
import os
from pathlib import Path
import unittest
import zipfile

from monthly_report.timesheet import (
    FORMULA_VERSION,
    TimesheetError,
    compile_timesheets,
    normalize_attendance_status,
    parse_timesheet_xlsx,
)


REAL_TIMESHEET = Path(
    r"C:\Users\ROG\Downloads\EMPLOYEE TIMESHEET  05 AGUSTUS  2026 ELECTRICAL PT GARUDA PRIMA AKSARA.xlsx"
)


def _column_letters(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _inline_cell(reference, value):
    text = html.escape(str(value), quote=False)
    return f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet_xml(rows, merges=()):
    row_xml = []
    for row_number, values in sorted(rows.items()):
        cells = []
        for column, value in sorted(values.items()):
            if value is None:
                continue
            reference = f"{_column_letters(column)}{row_number}"
            cells.append(_inline_cell(reference, value))
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    merge_xml = ""
    if merges:
        merge_xml = (
            f'<mergeCells count="{len(merges)}">'
            + "".join(f'<mergeCell ref="{item}"/>' for item in merges)
            + "</mergeCells>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>{merge_xml}</worksheet>'
    )


def _workbook_bytes(sheet_specs):
    """Build a minimal, dependency-free .xlsx fixture."""

    content_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheet_specs) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{content_overrides}</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    sheet_nodes = []
    workbook_relationships = []
    for index, spec in enumerate(sheet_specs, start=1):
        state = f' state="{spec.get("state")}"' if spec.get("state") else ""
        sheet_nodes.append(
            f'<sheet name="{html.escape(spec.get("name", f"Sheet{index}"), quote=True)}" '
            f'sheetId="{index}" state="{spec.get("state")}" r:id="rId{index}"/>'
            if spec.get("state")
            else f'<sheet name="{html.escape(spec.get("name", f"Sheet{index}"), quote=True)}" '
            f'sheetId="{index}" r:id="rId{index}"/>'
        )
        workbook_relationships.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheet_nodes)}</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(workbook_relationships)}</Relationships>'
    )

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, spec in enumerate(sheet_specs, start=1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(spec["rows"], spec.get("merges", ())),
            )
    return stream.getvalue()


def _cross_year_fixture(*, alice_first="1", hidden_noise=False):
    # PERIOD has six dates. Date columns D:I intentionally cross year/month.
    rows = {
        1: {1: "GPA Attendance Timesheet"},
        2: {1: "PERIOD :", 4: "29/12/2026 - 03/01/2027"},
        4: {1: "NO.", 2: "FULL NAME", 3: "POSITION", 4: "DATE & SIGNATURE"},
        5: {4: "29", 5: "30", 6: "31", 7: "1", 8: "2", 9: "3"},
        6: {1: "INDIRECT MANPOWER"},
        7: {1: "1", 2: "Alice Example", 3: "Project Control", 4: alice_first, 5: "1", 7: "CUTI", 9: "0"},
        8: {1: "DIRECT MANPOWER"},
        9: {1: "1", 2: "Bob Example", 3: "Technician", 4: "1", 5: "S", 7: "1", 8: "1"},
        10: {1: "Prepared By"},
    }
    specs = [{"name": "Attendance", "rows": rows, "merges": ("G7:H7", "E9:F9")}]
    if hidden_noise:
        specs.append(
            {
                "name": "Hidden old data",
                "state": "hidden",
                "rows": {
                    1: {1: "PERIOD", 2: "01/01/2000 - 01/01/2000"},
                    2: {2: "FULL NAME", 3: "POSITION", 4: "DATE & SIGNATURE"},
                    3: {4: "1"},
                    4: {1: "DIRECT MANPOWER"},
                    5: {2: "Noise", 3: "Noise", 4: "1"},
                },
            }
        )
    return _workbook_bytes(specs)


class TimesheetStatusTests(unittest.TestCase):
    def test_only_literal_one_is_present(self):
        self.assertEqual(normalize_attendance_status("1"), "present")
        self.assertEqual(normalize_attendance_status(1), "present")
        self.assertEqual(normalize_attendance_status("✓"), "nonpresent")
        self.assertEqual(normalize_attendance_status("0"), "nonpresent")
        self.assertEqual(normalize_attendance_status(""), "missing")

    def test_expected_nonpresent_statuses(self):
        expected = {
            "C": "leave",
            "CUTI": "leave",
            "I": "permission",
            "S": "sick",
            "RESIGN": "resigned",
            "RISEGN": "resigned",
        }
        self.assertEqual(
            {raw: normalize_attendance_status(raw) for raw in expected},
            expected,
        )


class TimesheetParserTests(unittest.TestCase):
    def test_cross_year_period_sections_merged_statuses_and_filter(self):
        result = parse_timesheet_xlsx(
            _cross_year_fixture(hidden_noise=True),
            filename="attendance.xlsx",
            start_date="2026-12-30",
            cutoff_date="2027-01-02",
        )

        self.assertEqual(result["formula_version"], FORMULA_VERSION)
        self.assertEqual(result["period"]["start"], "2026-12-30")
        self.assertEqual(result["period"]["end"], "2027-01-02")
        self.assertEqual(result["totals"]["present_person_days"], 3)
        self.assertEqual(result["totals"]["physical_manhours"], 30)
        self.assertEqual(result["totals"]["peak_present_count"], 1)

        employees = {item["name"]: item for item in result["employees"]}
        self.assertEqual(employees["Alice Example"]["section"], "indirect")
        self.assertEqual(employees["Bob Example"]["section"], "direct")
        self.assertEqual(
            employees["Alice Example"]["status_counts"],
            {"leave": 2, "missing": 1, "present": 1},
        )
        self.assertEqual(
            employees["Bob Example"]["status_counts"],
            {"present": 2, "sick": 2},
        )
        self.assertEqual(result["source_manifest"][0]["sheets"][1]["reason"], "hidden_sheet")
        json.dumps(result)  # Contract: preview is safe to send directly as JSON.

    def test_duplicate_sha_is_manifested_and_not_counted_twice(self):
        payload = _cross_year_fixture()
        result = compile_timesheets(
            [("first.xlsx", payload), ("same-file.xlsx", payload)],
            start_date="2026-12-29",
            end_date="2026-12-29",
        )
        self.assertEqual(result["totals"]["present_person_days"], 2)
        self.assertEqual(
            [item["status"] for item in result["source_manifest"]],
            ["accepted", "duplicate"],
        )
        self.assertIn("duplicate_file", {item["code"] for item in result["warnings"]})

    def test_employee_date_conflict_requires_review_and_counts_zero(self):
        present = _cross_year_fixture(alice_first="1")
        sick = _cross_year_fixture(alice_first="S")
        result = compile_timesheets(
            [("present.xlsx", present), ("sick.xlsx", sick)],
            start_date="2026-12-29",
            end_date="2026-12-29",
        )
        # Bob agrees and remains one present person-day; Alice is unresolved.
        self.assertEqual(result["totals"]["present_person_days"], 1)
        self.assertEqual(result["totals"]["physical_manhours"], 10)
        alice = next(item for item in result["employees"] if item["name"] == "Alice Example")
        self.assertEqual(alice["statuses"][0]["status"], "conflict")
        self.assertIn(
            "employee_date_status_conflict",
            {item["type"] for item in result["unresolved"]},
        )

    def test_rejects_non_xlsx_upload(self):
        with self.assertRaisesRegex(TimesheetError, "Only .xlsx"):
            parse_timesheet_xlsx(b"not xlsx", filename="attendance.xls")

    def test_does_not_fuzzy_merge_similar_names(self):
        first = _cross_year_fixture()
        rows = {
            1: {1: "PERIOD", 2: "29/12/2026 - 03/01/2027"},
            2: {1: "NO", 2: "FULL NAME", 3: "POSITION", 4: "DATE & SIGNATURE"},
            3: {4: "29", 5: "30", 6: "31", 7: "1", 8: "2", 9: "3"},
            4: {1: "DIRECT MANPOWER"},
            5: {1: "1", 2: "Alicia Example", 3: "Technician", 4: "1"},
            6: {1: "Prepared By"},
        }
        second = _workbook_bytes([{"name": "New", "rows": rows}])
        result = compile_timesheets(
            [("one.xlsx", first), ("two.xlsx", second)],
            start_date="2026-12-29",
            end_date="2026-12-29",
        )
        names = {item["name"] for item in result["employees"]}
        self.assertIn("Alice Example", names)
        self.assertIn("Alicia Example", names)

    def test_rejects_unsafe_high_ratio_xlsx_archive(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", b"0" * 1_000_000)
        with self.assertRaisesRegex(TimesheetError, "unsafe compression ratio"):
            parse_timesheet_xlsx(output.getvalue(), filename="unsafe.xlsx")


@unittest.skipUnless(REAL_TIMESHEET.exists(), "real GPA attendance workbook is not available")
class RealTimesheetGoldenTests(unittest.TestCase):
    def test_july_30_through_august_5_golden_totals(self):
        result = parse_timesheet_xlsx(
            REAL_TIMESHEET,
            start_date="2026-07-30",
            cutoff_date="2026-08-05",
        )
        self.assertEqual(result["totals"]["present_person_days"], 686)
        self.assertEqual(result["totals"]["physical_manhours"], 6860)
        self.assertEqual(result["totals"]["peak_present_count"], 105)
        self.assertEqual(result["totals"]["peak_date"], "2026-08-05")
        self.assertEqual(
            [item["date"] for item in result["daily_totals"]],
            [
                "2026-07-30",
                "2026-07-31",
                "2026-08-01",
                "2026-08-02",
                "2026-08-03",
                "2026-08-04",
                "2026-08-05",
            ],
        )


if __name__ == "__main__":
    unittest.main()
