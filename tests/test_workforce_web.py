import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from monthly_report.ai_summary import AISummaryError
from monthly_report.web import (
    _acquire_ai_draft_lock,
    _ai_draft_lock_path,
    _apply_review,
    _issued_report_copy,
    _load_draft,
    _makassar_issue_date,
    _release_ai_draft_lock,
    _save_draft,
    register_monthly_routes,
)


def _overtime_workbook_bytes():
    """Minimal real OOXML workbook for exercising the multipart adapter."""

    workbook = b'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="1 AUG" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    relationships = b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>'''
    rows = [
        ["ABSENSI MANPOWER PT. GARUDA PRIMA AKSARA OVERTIME"],
        ["Date: 1 August 2026 (MA-42)"],
        ["No", "Nama", "Posisi", "Overtime"],
        ["1", "Bambang", "Technician", "17.00-21.00"],
    ]
    row_xml = []
    for row_index, values in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(values):
            column = chr(ord("A") + column_index)
            cells.append(
                f'<c r="{column}{row_index}" t="inlineStr"><is><t>{value}</t></is></c>'
            )
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>'
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _draft():
    return {
        "owner": "reza",
        "report_type": "weekly",
        "report_mode": "draft",
        "period": {"start": "2026-08-01", "end": "2026-08-07"},
        "project_no": "P-001",
        "project_title": "Electrical Project",
        "source_manifest": [{"report_id": "day-1", "report_date": "2026-08-01"}],
        "source_validation": {"applied": True, "confirmed": True},
        "manpower": {
            "daily": [{
                "date": "2026-08-01", "direct_headcount": 1, "indirect_headcount": 1,
                "total_headcount": 2, "direct_man_hours": 10, "indirect_man_hours": 10,
                "total_man_hours": 20,
            }],
            "totals": {
                "direct_person_days": 1, "indirect_person_days": 1, "total_person_days": 2,
                "direct_man_hours": 10, "indirect_man_hours": 10, "total_man_hours": 20,
                "peak_headcount": 2,
            },
        },
        "safety": {"total_manpower": 2, "total_man_hours": 20},
        "site": {"this_week_activities": ["Cable installation"]},
    }


def _timesheet():
    return {
        "formula_version": "kn_attendance_v1_10h",
        "period": {"start": "2026-08-01", "end": "2026-08-07"},
        "daily_totals": [{
            "date": "2026-08-01", "present_count": 3, "physical_manhours": 30,
            "present_by_section": {"direct": 2, "indirect": 1},
        }],
        "totals": {
            "present_person_days": 3, "physical_manhours": 30, "peak_present_count": 3,
            "by_section": {
                "direct": {"present_person_days": 2, "physical_manhours": 20},
                "indirect": {"present_person_days": 1, "physical_manhours": 10},
            },
        },
        "roles": [],
        "employees": [{
            "employee_key": "bambang", "name": "Bambang", "section": "direct",
            "statuses": [{"date": "2026-08-01", "status": "present"}],
        }],
        "warnings": [], "unresolved": [],
    }


def _overtime():
    return {
        "period": {"start": "2026-08-01", "end": "2026-08-07"},
        "coverage": {"selected_populated_dates": ["2026-08-01"], "not_supplied_dates": ["2026-08-02"]},
        "totals": {"selected_employee_count": 1, "selected_confirmed_elapsed_hours": 4},
        "daily": [{"date": "2026-08-01", "employee_count": 1, "confirmed_elapsed_hours": 4}],
        "employees": [{
            "employee": "Bambang", "employee_key": "bambang", "dates": ["2026-08-01"],
            "confirmed_elapsed_hours": 4,
        }],
        "records": [{
            "record_id": "ot-1", "employee": "Bambang", "employee_key": "bambang",
            "date": "2026-08-01", "duration_hours": 4, "included_in_total": True,
            "requires_review": False,
        }],
        "warnings": [], "conflicts": [], "requires_manual_review": False,
    }


def _ai_envelope():
    return {
        "version": "periodic-ai-suggestion/2",
        "model": "claude-test",
        "input_hash": "abc",
        "suggestion": {
            "executive_summary": {
                "text": "Weekly work focused on cable installation.",
                "source_ids": ["day-1"],
                "dates": ["2026-08-01"],
            },
            "engineering_summary": {"text": "Not supplied", "source_ids": [], "dates": []},
            "procurement_summary": {"text": "Not supplied", "source_ids": [], "dates": []},
            "site_summary": {
                "text": "Cable installation continued.",
                "source_ids": ["day-1"],
                "dates": ["2026-08-01"],
            },
            "concern_actions": [{
                "concern": "Cable testing remains incomplete.",
                "corrective_action": "Complete testing during the next work period.",
                "source_ids": ["day-1"],
                "dates": ["2026-08-01"],
            }],
            "lookahead": [{
                "text": "Continue cable testing.",
                "source_ids": ["day-1"],
                "dates": ["2026-08-01"],
            }],
            "claims": [{
                "text": "Cable installation continued.",
                "source_ids": ["day-1"],
                "dates": ["2026-08-01"],
            }],
            "missing_data": ["Procurement: Not supplied"],
        },
    }


class WorkforceWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="workforce-web-tests")
        register_monthly_routes(
            self.app,
            data_dir=str(self.data_dir),
            config_provider=lambda: {},
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["username"] = "reza"
            session["is_admin"] = True
        self.draft_id = _save_draft(str(self.data_dir), "reza", _draft())

    def tearDown(self):
        self.temp.cleanup()

    def test_timesheet_overtime_apply_and_reset_routes(self):
        with patch("monthly_report.web.compile_timesheets", return_value=_timesheet()):
            response = self.client.post(
                f"/monthly/workforce/timesheet/{self.draft_id}/preview",
                data={"files": (io.BytesIO(b"xlsx"), "attendance.xlsx")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = self.client.post(
            f"/monthly/workforce/timesheet/{self.draft_id}/decision", json={"decision": "apply"}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["draft"]["safety"]["total_man_hours"], 30)

        with patch("monthly_report.web.parse_overtime_workbooks", return_value=_overtime()):
            response = self.client.post(
                f"/monthly/workforce/overtime/{self.draft_id}/preview",
                data={"files": (io.BytesIO(b"xlsx"), "overtime.xlsx")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = self.client.post(
            f"/monthly/workforce/overtime/{self.draft_id}/decision",
            json={
                "decision": "apply", "confirm_exceptions": True,
                "resolutions": [{"key": "bambang", "category": "direct"}],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["draft"]["safety"]["total_man_hours"], 34)

        response = self.client.post(f"/monthly/workforce/reset/{self.draft_id}")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["draft"]["safety"]["total_man_hours"], 20)

    def test_real_overtime_parser_accepts_multipart_named_bytes(self):
        with patch("monthly_report.web.compile_timesheets", return_value=_timesheet()):
            response = self.client.post(
                f"/monthly/workforce/timesheet/{self.draft_id}/preview",
                data={"files": (io.BytesIO(b"xlsx"), "attendance.xlsx")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = self.client.post(
            f"/monthly/workforce/timesheet/{self.draft_id}/decision",
            json={"decision": "apply"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())

        response = self.client.post(
            f"/monthly/workforce/overtime/{self.draft_id}/preview",
            data={
                "files": (
                    io.BytesIO(_overtime_workbook_bytes()),
                    "named-overtime.xlsx",
                )
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        preview = response.get_json()["draft"]["workforce_validation"]["overtime"]["preview"]
        self.assertEqual(preview["manifest"]["files"][0]["filename"], "named-overtime.xlsx")
        self.assertEqual(preview["totals"]["selected_record_count"], 1)
        self.assertEqual(preview["totals"]["selected_confirmed_elapsed_hours"], 4)

    def test_ai_suggestion_is_review_only_until_accepted(self):
        envelope = _ai_envelope()
        before = copy.deepcopy(_load_draft(str(self.data_dir), "reza", self.draft_id))
        with patch("monthly_report.web.generate_ai_summary", return_value=envelope) as generate:
            response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
        self.assertEqual(response.status_code, 200, response.get_json())
        suggested = response.get_json()["draft"]
        self.assertEqual(suggested.get("executive_summary"), before.get("executive_summary"))
        grounded_input = generate.call_args.args[0]
        self.assertEqual(
            grounded_input["executive_summary"],
            grounded_input["deterministic_summary"]["executive_summary"]["text"],
        )
        self.assertEqual(suggested["ai_summary"]["status"], "suggested")
        suggestion = suggested["ai_summary"]["suggestion"]
        self.assertEqual(
            suggestion["concerns"][0]["corrective_action"],
            "Complete testing during the next work period.",
        )
        self.assertEqual(suggestion["concerns"][0]["source_ids"], ["day-1"])
        self.assertEqual(
            suggestion["citation_evidence"]["lookahead"][0]["dates"],
            ["2026-08-01"],
        )
        self.assertEqual(suggestion["missing_data"], ["Procurement: Not supplied"])

        response = self.client.post(
            f"/monthly/ai-summary/{self.draft_id}/decision",
            json={"decision": "accept", "suggestion": {
                "executive_summary": "Edited reviewed summary.",
                "engineering_summary": "Not supplied",
                "procurement_summary": "Not supplied",
                "site_summary": "Cable installation continued.",
                "concerns": [], "lookahead": [],
            }},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        accepted = response.get_json()["draft"]
        self.assertEqual(accepted["executive_summary"], "Edited reviewed summary.")
        self.assertEqual(accepted["safety"]["total_man_hours"], 20)
        self.assertEqual(accepted["ai_summary"]["status"], "accepted")
        self.assertEqual(
            accepted["ai_summary"]["suggestion"]["citation_evidence"]["executive_summary"]["source_ids"],
            ["day-1"],
        )

    def test_ai_inflight_lock_returns_clear_409(self):
        lock = _acquire_ai_draft_lock(str(self.data_dir), "reza", self.draft_id)
        self.assertIsNotNone(lock)
        try:
            with patch("monthly_report.web.generate_ai_summary") as generate:
                response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
            self.assertEqual(response.status_code, 409, response.get_json())
            self.assertEqual(response.get_json()["code"], "ai_generation_in_progress")
            self.assertTrue(response.get_json()["retryable"])
            self.assertEqual(response.headers["Retry-After"], "5")
            generate.assert_not_called()
        finally:
            _release_ai_draft_lock(lock)

    def test_stale_ai_lock_is_recovered(self):
        lock = _acquire_ai_draft_lock(str(self.data_dir), "reza", self.draft_id)
        self.assertIsNotNone(lock)
        lock_path, _token = lock
        old = datetime.now().timestamp() - 601
        os.utime(lock_path, (old, old))

        with patch("monthly_report.web.generate_ai_summary", return_value=_ai_envelope()):
            response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(lock_path.exists())

    def test_ai_failure_persists_cooldown_before_provider_call(self):
        observed = {}

        def fail_after_check(_draft):
            persisted = _load_draft(str(self.data_dir), "reza", self.draft_id)
            observed.update(persisted.get("ai_request_control", {}))
            raise AISummaryError(
                "Claude is temporarily unavailable.",
                code="provider_error",
                retryable=True,
            )

        with patch("monthly_report.web.generate_ai_summary", side_effect=fail_after_check):
            response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
        self.assertEqual(response.status_code, 502, response.get_json())
        self.assertEqual(observed.get("version"), "periodic-ai-request-control/1")
        self.assertEqual(observed.get("last_started_by"), "reza")
        self.assertTrue(observed.get("last_started_at", "").endswith("Z"))
        self.assertFalse(_ai_draft_lock_path(str(self.data_dir), "reza", self.draft_id).exists())

        with patch("monthly_report.web.generate_ai_summary") as generate:
            retry = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
        self.assertEqual(retry.status_code, 429, retry.get_json())
        self.assertEqual(retry.get_json()["code"], "ai_cooldown_active")
        self.assertGreaterEqual(int(retry.headers["Retry-After"]), 1)
        generate.assert_not_called()

    def test_provider_rate_limit_returns_clear_429(self):
        with patch(
            "monthly_report.web.generate_ai_summary",
            side_effect=AISummaryError(
                "Claude rate limit was reached.",
                code="rate_limited",
                retryable=True,
            ),
        ):
            response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
        self.assertEqual(response.status_code, 429, response.get_json())
        self.assertEqual(response.get_json()["code"], "rate_limited")
        self.assertEqual(response.get_json()["retry_after_seconds"], 30)
        self.assertEqual(response.headers["Retry-After"], "30")
        self.assertFalse(_ai_draft_lock_path(str(self.data_dir), "reza", self.draft_id).exists())

    def test_issue_date_uses_asia_makassar_calendar_day(self):
        utc_near_midnight = datetime(2026, 8, 10, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(_makassar_issue_date(utc_near_midnight), "2026-08-11")

    def test_workbook_requires_applied_source_validation(self):
        draft = _draft()
        draft["source_validation"]["applied"] = False
        draft_id = _save_draft(str(self.data_dir), "reza", draft)
        response = self.client.post(
            f"/monthly/workforce/timesheet/{draft_id}/preview",
            data={"files": (io.BytesIO(b"xlsx"), "attendance.xlsx")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Source Data Validation", response.get_json()["error"])

    def test_ai_can_be_restricted_to_admin(self):
        with self.client.session_transaction() as session:
            session["is_admin"] = False
        with patch.dict(os.environ, {"ANTHROPIC_AI_ADMIN_ONLY": "true"}):
            response = self.client.post(f"/monthly/ai-summary/{self.draft_id}")
        self.assertEqual(response.status_code, 403)

    def test_issued_report_json_keeps_audit_facts_without_employee_rows(self):
        report = _draft()
        report["_source_records"] = [{"secret": "raw daily source"}]
        report["ai_request_control"] = {"attempt_id": "draft-only-request"}
        report["workforce_validation"] = {
            "version": "workforce-validation/1",
            "effective": {"source": "timesheet", "total_man_hours": 34},
            "baseline": {"private": "draft-only"},
            "timesheet": {
                "status": "applied",
                "decided_by": "reza",
                "preview": {
                    "formula_version": "kn_attendance_v1_10h",
                    "hours_per_present_day": 10,
                    "period": {"start": "2026-08-01", "end": "2026-08-07"},
                    "totals": {"physical_manhours": 30},
                    "source_manifest": [{
                        "filename": "employee-list.xlsx",
                        "sha256": "a" * 64,
                        "size_bytes": 123,
                    }],
                    "employees": [{
                        "name": "Bambang Private",
                        "statuses": [{"date": "2026-08-01", "status": "present"}],
                    }],
                },
            },
            "overtime": {
                "status": "applied",
                "confirmed_exceptions": True,
                "resolutions": {"bambang private": "direct"},
                "accepted_records": [{
                    "record_id": "ot-1",
                    "employee": "Bambang Private",
                    "overtime_raw": "17.00-21.00",
                }],
                "preview": {
                    "formula_version": "kn_overtime_elapsed_v1",
                    "totals": {"selected_confirmed_elapsed_hours": 4},
                    "manifest": {"files": [{
                        "filename": "named-overtime.xlsx",
                        "sha256": "b" * 64,
                        "size_bytes": 456,
                    }]},
                    "records": [{"employee": "Bambang Private"}],
                },
            },
        }
        report["ai_summary"] = {
            "status": "accepted",
            "requested_by": "reza",
            "suggestion": {
                "executive_summary": "raw provider wording",
                "citation_evidence": {
                    "executive_summary": {"source_ids": ["day-1"], "dates": ["2026-08-01"]},
                    "engineering_summary": {"source_ids": [], "dates": []},
                    "procurement_summary": {"source_ids": [], "dates": []},
                    "site_summary": {"source_ids": ["day-1"], "dates": ["2026-08-01"]},
                    "concern_actions": [{"source_ids": ["day-1"], "dates": ["2026-08-01"]}],
                    "lookahead": [{"source_ids": ["day-1"], "dates": ["2026-08-01"]}],
                    "claims": [{"source_ids": ["day-1"], "dates": ["2026-08-01"]}],
                },
                "missing_data": ["Procurement: Not supplied"],
            },
            "provider_envelope": {
                "model": "claude-test",
                "input_hash": "c" * 64,
                "usage": {"input_tokens": 12},
                "suggestion": {"claims": ["raw provider response"]},
            },
        }

        issued = _issued_report_copy(report)
        encoded = json.dumps(issued, ensure_ascii=False)

        self.assertIn("a" * 64, encoded)
        self.assertIn("b" * 64, encoded)
        self.assertIn("c" * 64, encoded)
        self.assertIn('"decision": "direct"', encoded)
        self.assertIn('"source_ids": ["day-1"]', encoded)
        self.assertIn("Procurement: Not supplied", encoded)
        self.assertNotIn("Bambang Private", encoded)
        self.assertNotIn("employee-list.xlsx", encoded)
        self.assertNotIn("named-overtime.xlsx", encoded)
        self.assertNotIn("raw provider", encoded)
        self.assertNotIn("draft-only-request", encoded)
        self.assertNotIn("_source_records", issued)
        self.assertNotIn("ai_request_control", issued)
        self.assertIn("Bambang Private", json.dumps(report))

    def test_timesheet_man_hours_are_used_for_severity_rate(self):
        draft = _draft()
        draft["workforce_validation"] = {
            "version": "workforce-validation/1",
            "timesheet": {"status": "applied"},
            "overtime": {"status": "not_reviewed"},
            "effective": {
                "source": "timesheet",
                "peak_headcount": 10,
                "total_man_hours": 1000,
            },
        }
        reviewed = _apply_review(draft, {
            "report_mode": "draft",
            "safety": {
                "total_manpower": 1,
                "total_man_hours": 100,
                "lost_workdays": 1,
                "lost_time_injuries": 1,
            },
        })
        self.assertEqual(reviewed["safety"]["total_manpower"], 10)
        self.assertEqual(reviewed["safety"]["total_man_hours"], 1000)
        self.assertEqual(reviewed["safety"]["severity_rate"], 1000.0)


if __name__ == "__main__":
    unittest.main()
