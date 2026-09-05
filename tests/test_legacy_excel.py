import hashlib
import io
import tempfile
import unittest
import zipfile
from html import escape
from pathlib import Path

from PIL import Image

from monthly_report.aggregate import aggregate_monthly_records
from monthly_report.legacy_excel import (
    PARSER_VERSION,
    LegacyExcelError,
    analyze_legacy_daily_workbook,
    extract_legacy_daily_records,
)
from monthly_report.photos import DEFAULT_PHOTO_LIMITS
from monthly_report.validation import build_source_validation, resolve_project_records


PROJECT_NO = "001/KN-GPA/EPC-2F-P2/IV/2025"
PROJECT_TITLE = "RE-ACTIVATION TURBINES AND GENERATORS"
CANONICAL_PROJECT_TITLE = (
    "PROJECT REVAMPING PT KERTAS NUSANTARA - "
    "REACTIVATION FOR TURBINES AND GENERATORS"
)


def _cell(reference, value):
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    )


def _sheet_xml(*, content_date, drawing=False):
    rows = {
        10: {
            "B": f"Date: {content_date} Days - 7",
        },
        12: {
            "B": "Project No",
            "C": "PC-25-012-KN-GPA-171-DAR",
            "H": "Customer",
            "I": "PT. KERTAS NUSANTARA",
        },
        13: {
            "B": "Project Name",
            "C": PROJECT_TITLE,
            "H": "Location / Area",
            "I": "AREA MA77",
        },
        14: {
            "H": "Equipment",
            "I": "TURBINES & GENERATORS",
        },
        20: {"B": "A. WEATHER REPORT"},
        22: {"B": "Sunny", "F": "Cloud", "J": "Rain"},
        25: {"B": "B. ACTIVITY TODAY"},
        27: {"C": "TURBINE 1", "I": "TURBINE 1"},
        28: {
            "B": "1",
            "C": "Cleaning Gear",
            "H": "1",
            "I": "Assembly Coupling Gear",
        },
        35: {"B": "D. INDIRECT MANPOWER"},
        37: {"C": "TURBINE 1", "I": "TURBINE 1"},
        38: {
            "B": "1",
            "C": "Ali",
            "F": "1",
            "G": "07:00-17:00",
            "H": "1",
            "I": "Agus",
            "L": "1",
            "M": "07:00-17:00",
        },
        45: {"B": "F. CONSTRAINTS / PROBLEM"},
        47: {"B": "TURBINE 1", "C": "Waiting material"},
        50: {"B": "G. REMARKS"},
        51: {"B": "TURBINE 1"},
        52: {"B": "1", "C": "Work continues"},
        55: {"B": "H. CONCLUSION"},
        57: {
            "B": "1",
            "C": "Mechanical Work",
            "F": "0.50",
            "G": "0.40",
            "H": "0.35",
            "I": "0.10",
            "J": "0.08",
            "K": "0.50",
            "L": "0.43",
            "M": "-0.07",
        },
        58: {
            "D": "TOTAL PROGRESS",
            "G": "40%",
            "H": "35%",
            "I": "10%",
            "J": "8%",
            "K": "50%",
            "L": "43%",
            "M": "-7%",
        },
        65: {"B": "DOCUMENTATION PHOTO"},
    }
    row_xml = []
    for row_number, values in rows.items():
        cells = "".join(_cell(f"{column}{row_number}", value) for column, value in values.items())
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    drawing_xml = '<drawing r:id="rId1"/>' if drawing else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheetData>{"".join(row_xml)}</sheetData>{drawing_xml}</worksheet>'
    )


def _drawing_xml():
    def shape(row, column, text):
        return f"""
        <xdr:oneCellAnchor>
          <xdr:from><xdr:col>{column}</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
          <xdr:ext cx="100" cy="100"/>
          <xdr:sp><xdr:txBody><a:p><a:r><a:t>{escape(text)}</a:t></a:r></a:p></xdr:txBody></xdr:sp>
          <xdr:clientData/>
        </xdr:oneCellAnchor>
        """

    image = """
    <xdr:oneCellAnchor>
      <xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>69</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
      <xdr:ext cx="100" cy="100"/>
      <xdr:pic><xdr:blipFill><a:blip r:embed="rIdImg1"/></xdr:blipFill></xdr:pic>
      <xdr:clientData/>
    </xdr:oneCellAnchor>
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      {shape(65, 1, "TURBINE 1")}
      {image}
      {shape(70, 1, "Cleaning coupling")}
    </xdr:wsDr>"""


def _jpeg_bytes():
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "#1f5f3f").save(output, format="JPEG", quality=90)
    return output.getvalue()


def _workbook(path, sheets, *, with_photo=False):
    workbook_rows = []
    relationship_rows = []
    for index, (name, content_date) in enumerate(sheets, 1):
        workbook_rows.append(
            f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        relationship_rows.append(
            f'<Relationship Id="rId{index}" Type="worksheet" Target="worksheets/sheet{index}.xml"/>'
        )

    workbook = f"""<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets>{''.join(workbook_rows)}</sheets>
    </workbook>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      {''.join(relationship_rows)}
    </Relationships>"""

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        for index, (_name, content_date) in enumerate(sheets, 1):
            drawing = with_photo and index == 1
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(content_date=content_date, drawing=drawing),
            )
            if drawing:
                archive.writestr(
                    "xl/worksheets/_rels/sheet1.xml.rels",
                    """<?xml version="1.0" encoding="UTF-8"?>
                    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="rId1" Type="drawing" Target="../drawings/drawing1.xml"/>
                    </Relationships>""",
                )
                archive.writestr("xl/drawings/drawing1.xml", _drawing_xml())
                archive.writestr(
                    "xl/drawings/_rels/drawing1.xml.rels",
                    """<?xml version="1.0" encoding="UTF-8"?>
                    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                      <Relationship Id="rIdImg1" Type="image" Target="../media/image1.jpeg"/>
                    </Relationships>""",
                )
                archive.writestr("xl/media/image1.jpeg", _jpeg_bytes())


class LegacyExcelCharacterizationTests(unittest.TestCase):
    def test_analysis_preserves_dates_duplicates_mismatch_and_project_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "daily.xlsx"
            _workbook(
                source,
                [
                    ("08.12.26", "8 Desember 2026"),
                    ("07.12.26", "7 Desember 2025"),
                    ("08.12.2026", "8 Desember 2026"),
                    ("SUMMARY", "8 Desember 2026"),
                ],
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            result = analyze_legacy_daily_workbook(
                source,
                filename="Daily Report Turbine.xlsx",
                sha256=digest,
            )

        self.assertEqual(result["parser_version"], PARSER_VERSION)
        self.assertEqual(result["available_dates"], ["2026-12-07", "2026-12-08"])
        self.assertEqual(result["date_from"], "2026-12-07")
        self.assertEqual(result["date_to"], "2026-12-08")
        self.assertEqual(result["duplicate_dates"], ["2026-12-08"])
        self.assertEqual(result["ignored_sheets"], ["SUMMARY"])
        self.assertEqual(result["project"]["project_title"], PROJECT_TITLE)
        self.assertEqual(result["project"]["equipment"], "TURBINES & GENERATORS")
        self.assertEqual(
            [item["date"] for item in result["sheets"]],
            ["2026-12-07", "2026-12-08", "2026-12-08"],
        )
        warning_codes = [item["code"] for item in result["warnings"]]
        self.assertIn("sheet_content_date_mismatch", warning_codes)
        self.assertIn("duplicate_sheet_dates", warning_codes)
        self.assertIn("ignored_non_daily_sheets", warning_codes)

    def test_selected_sheet_extracts_same_canonical_content_and_photo_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "daily.xlsx"
            asset_directory = root / "assets"
            _workbook(source, [("08.12.26", "8 Desember 2026")], with_photo=True)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            analysis = analyze_legacy_daily_workbook(
                source,
                filename="Daily Report Turbine.xlsx",
                sha256=digest,
            )

            records, warnings = extract_legacy_daily_records(
                source,
                analysis=analysis,
                selected_dates=["2026-12-08"],
                username="reza",
                project_no=PROJECT_NO,
                project_title=PROJECT_TITLE,
                asset_directory=asset_directory,
                photo_limits=DEFAULT_PHOTO_LIMITS,
            )

            self.assertEqual(warnings, [])
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["date"], "2026-12-08")
            self.assertEqual(record["report_id"], f"xlsx-{digest[:32]}-20261208")
            self.assertEqual(record["source"]["method"], "uploaded_excel")
            self.assertEqual(record["source"]["sheet_name"], "08.12.26")
            self.assertEqual(record["source_identity"]["document_no"], "PC-25-012-KN-GPA-171-DAR")
            self.assertFalse(record["review_required"])

            payload = record["payload"]
            self.assertEqual(payload["weather"], {
                "morning": "Sunny",
                "afternoon": "Cloud",
                "evening": "Rain",
            })
            self.assertEqual(payload["manpower_status"], "reported")
            self.assertEqual(payload["constraint_status"], "reported")
            self.assertEqual(payload["overall_progress"][-1]["cumulative_to_date_actual"], 43.0)
            turbine = next(item for item in payload["areas"] if item["id"] == "Turbine 1")
            self.assertEqual(turbine["activities_today"], ["Cleaning Gear"])
            self.assertEqual(turbine["activities_tomorrow"], ["Assembly Coupling Gear"])
            self.assertEqual(turbine["constraints"], ["Waiting material"])
            self.assertEqual(turbine["remarks"], ["Work continues"])
            self.assertEqual(turbine["manpower"][0]["name"], "Agus")
            self.assertEqual(turbine["indirect_manpower"][0]["name"], "Ali")

            self.assertEqual(len(record["_photo_candidates"]), 1)
            photo = record["_photo_candidates"][0]
            self.assertEqual(photo["source_area"], "Turbine 1")
            self.assertEqual(photo["caption"], "Cleaning coupling")
            self.assertEqual(photo["photo_match_method"], "worksheet_drawing_anchor")
            self.assertTrue((asset_directory / f'{photo["asset_id"]}.jpg').is_file())

    def test_selection_contract_rejects_empty_and_unknown_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "daily.xlsx"
            _workbook(source, [("08.12.26", "8 Desember 2026")])
            analysis = analyze_legacy_daily_workbook(source)
            arguments = {
                "analysis": analysis,
                "username": "reza",
                "project_no": PROJECT_NO,
                "project_title": PROJECT_TITLE,
                "asset_directory": root / "assets",
                "photo_limits": DEFAULT_PHOTO_LIMITS,
            }

            with self.assertRaisesRegex(LegacyExcelError, "Choose at least one"):
                extract_legacy_daily_records(source, selected_dates=[], **arguments)
            with self.assertRaisesRegex(LegacyExcelError, "not available"):
                extract_legacy_daily_records(
                    source,
                    selected_dates=["2026-12-09"],
                    **arguments,
                )

    def test_approved_project_alias_flows_directly_into_validation_and_aggregation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "daily.xlsx"
            _workbook(source, [("08.12.26", "8 Desember 2026")])
            analysis = analyze_legacy_daily_workbook(source)

            records, warnings = extract_legacy_daily_records(
                source,
                analysis=analysis,
                selected_dates=["2026-12-08"],
                username="reza",
                project_no=PROJECT_NO,
                project_title=CANONICAL_PROJECT_TITLE,
                asset_directory=root / "assets",
                photo_limits=DEFAULT_PHOTO_LIMITS,
                approved_title_aliases=[PROJECT_TITLE],
            )

        self.assertEqual(warnings, [])
        self.assertFalse(records[0]["review_required"])
        self.assertEqual(records[0]["source_identity"]["match_method"], "approved_alias")
        self.assertEqual(records[0]["source_identity"]["matched_title_alias"], PROJECT_TITLE)
        validation = build_source_validation(
            records,
            selected_project_no=PROJECT_NO,
            selected_project_title=CANONICAL_PROJECT_TITLE,
        )
        self.assertFalse(validation["required"])
        self.assertTrue(validation["project_groups"][0]["matches_selected"])
        included, excluded = resolve_project_records(
            records,
            validation,
            project_no=PROJECT_NO,
            project_title=CANONICAL_PROJECT_TITLE,
            resolutions=[],
        )
        self.assertEqual(excluded, [])
        aggregate = aggregate_monthly_records(
            included,
            date_from="2026-12-08",
            date_to="2026-12-08",
            project_no=PROJECT_NO,
            expected_dates=["2026-12-08"],
        )
        self.assertEqual(aggregate["coverage"]["covered_dates"], ["2026-12-08"])
        self.assertEqual(aggregate["coverage"]["missing_dates"], [])


if __name__ == "__main__":
    unittest.main()
