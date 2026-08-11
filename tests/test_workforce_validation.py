import copy
import unittest

from monthly_report.workforce import (
    decide_overtime,
    decide_timesheet,
    has_pending_workforce_review,
    reset_workforce,
    set_overtime_preview,
    set_timesheet_preview,
)


def _draft():
    return {
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
        "safety": {
            "total_manpower": 2, "total_man_hours": 20,
            "recordable_cases": None, "lost_workdays": None, "lost_time_injuries": None,
        },
    }


def _timesheet_preview():
    return {
        "formula_version": "kn_attendance_v1_10h",
        "period": {"start": "2026-08-01", "end": "2026-08-01"},
        "daily_totals": [{
            "date": "2026-08-01", "present_count": 3, "physical_manhours": 30,
            "present_by_section": {"direct": 2, "indirect": 1}, "status_counts": {"present": 3},
        }],
        "totals": {
            "present_person_days": 3, "physical_manhours": 30,
            "peak_present_count": 3,
            "by_section": {
                "direct": {"present_person_days": 2, "physical_manhours": 20},
                "indirect": {"present_person_days": 1, "physical_manhours": 10},
            },
        },
        "roles": [{"role": "Technician", "present_person_days": 2, "physical_manhours": 20}],
        "employees": [{
            "employee_key": "bambang", "name": "Bambang", "section": "direct",
            "statuses": [{"date": "2026-08-01", "status": "present"}],
        }],
        "warnings": [], "unresolved": [],
    }


def _overtime_preview():
    return {
        "period": {"start": "2026-08-01", "end": "2026-08-01"},
        "coverage": {"selected_populated_dates": ["2026-08-01"], "not_supplied_dates": []},
        "totals": {"selected_employee_count": 1, "selected_confirmed_elapsed_hours": 4},
        "daily": [{
            "date": "2026-08-01", "employee_count": 1, "confirmed_elapsed_hours": 4,
        }],
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


class WorkforceValidationTests(unittest.TestCase):
    def test_apply_timesheet_then_overtime_and_reset_is_reversible(self):
        draft = _draft()
        baseline = copy.deepcopy(draft)
        set_timesheet_preview(draft, _timesheet_preview(), actor="reza")
        self.assertTrue(has_pending_workforce_review(draft))

        decide_timesheet(draft, "apply", actor="reza")
        self.assertEqual(draft["safety"]["total_manpower"], 3)
        self.assertEqual(draft["safety"]["total_man_hours"], 30)
        self.assertEqual(draft["manpower"]["totals"]["total_person_days"], 3)

        set_overtime_preview(draft, _overtime_preview(), actor="reza")
        decide_overtime(
            draft, "apply", actor="reza", confirm_exceptions=False,
            resolutions=[{"key": "bambang", "category": "direct"}],
        )
        self.assertEqual(draft["safety"]["total_man_hours"], 34)
        self.assertEqual(draft["manpower"]["totals"]["regular_man_hours"], 30)
        self.assertEqual(draft["manpower"]["totals"]["overtime_man_hours"], 4)
        self.assertEqual(draft["manpower"]["daily"][0]["direct_headcount"], 2)

        reset_workforce(draft)
        self.assertEqual(draft["manpower"], baseline["manpower"])
        self.assertEqual(draft["safety"], baseline["safety"])

    def test_unmatched_overtime_requires_mapping_and_confirmation(self):
        draft = _draft()
        preview = _timesheet_preview()
        preview["employees"] = []
        set_timesheet_preview(draft, preview, actor="reza")
        decide_timesheet(draft, "apply", actor="reza")
        set_overtime_preview(draft, _overtime_preview(), actor="reza")

        with self.assertRaisesRegex(ValueError, "Confirm overtime exceptions"):
            decide_overtime(
                draft, "apply", actor="reza", confirm_exceptions=False,
                resolutions=[{"key": "bambang", "category": "direct"}],
            )
        with self.assertRaisesRegex(ValueError, "Choose Direct"):
            decide_overtime(
                draft, "apply", actor="reza", confirm_exceptions=True, resolutions=[],
            )

    def test_keep_timesheet_preserves_daily_report_baseline(self):
        draft = _draft()
        baseline = copy.deepcopy(draft["manpower"])
        set_timesheet_preview(draft, _timesheet_preview(), actor="reza")
        decide_timesheet(draft, "keep", actor="reza")
        self.assertEqual(draft["manpower"], baseline)
        self.assertEqual(draft["workforce_validation"]["effective"]["source"], "daily_report")

    def test_timesheet_all_missing_date_is_review_only_and_excluded_from_effective_rows(self):
        draft = _draft()
        preview = _timesheet_preview()
        preview["daily_totals"].append({
            "date": "2026-08-02", "present_count": 0, "physical_manhours": 0,
            "present_by_section": {}, "status_counts": {"missing": 3},
        })
        preview["totals"]["status_counts"] = {"present": 3, "missing": 3}

        state = set_timesheet_preview(draft, preview, actor="reza")
        reviewed = state["timesheet"]["preview"]

        self.assertEqual(reviewed["coverage"]["not_supplied_dates"], ["2026-08-02"])
        self.assertFalse(reviewed["manpower"]["totals"]["hours_complete"])
        self.assertEqual(
            [row["date"] for row in reviewed["manpower"]["daily"]],
            ["2026-08-01"],
        )
        missing_row = next(row for row in reviewed["daily"] if row["date"] == "2026-08-02")
        self.assertFalse(missing_row["supplied"])
        self.assertIsNone(missing_row["total_man_hours"])

        with self.assertRaisesRegex(ValueError, "Confirm missing roles"):
            decide_timesheet(draft, "apply", actor="reza")
        decide_timesheet(draft, "apply", actor="reza", confirm_exceptions=True)
        self.assertEqual(
            draft["workforce_validation"]["effective"]["regular_not_supplied_dates"],
            ["2026-08-02"],
        )

    def test_timesheet_unresolved_role_requires_explicit_confirmation(self):
        draft = _draft()
        preview = _timesheet_preview()
        preview["unresolved"] = [{"type": "missing_role", "employee": "Bambang"}]
        set_timesheet_preview(draft, preview, actor="reza")

        with self.assertRaisesRegex(ValueError, "Confirm missing roles"):
            decide_timesheet(draft, "apply", actor="reza")
        self.assertEqual(draft["workforce_validation"]["timesheet"]["status"], "preview")

        decide_timesheet(draft, "apply", actor="reza", confirm_exceptions=True)
        self.assertTrue(draft["workforce_validation"]["timesheet"]["confirmed_exceptions"])

    def test_overtime_with_no_machine_readable_records_cannot_be_applied_as_zero(self):
        draft = _draft()
        set_timesheet_preview(draft, _timesheet_preview(), actor="reza")
        decide_timesheet(draft, "apply", actor="reza")
        preview = {
            "period": {"start": "2026-08-01", "end": "2026-08-01"},
            "coverage": {
                "selected_populated_dates": [],
                "not_supplied_dates": ["2026-08-01"],
            },
            "totals": {"selected_employee_count": 0, "selected_confirmed_elapsed_hours": 0},
            "daily": [], "employees": [], "records": [], "warnings": [], "conflicts": [],
            "requires_manual_review": False,
        }
        set_overtime_preview(draft, preview, actor="reza")

        with self.assertRaisesRegex(ValueError, "No machine-readable overtime"):
            decide_overtime(
                draft, "apply", actor="reza", confirm_exceptions=True, resolutions=[],
            )
        self.assertEqual(draft["workforce_validation"]["overtime"]["status"], "preview")
        self.assertIsNone(draft["workforce_validation"]["effective"]["overtime_man_hours"])

    def test_overtime_with_every_record_excluded_must_use_keep_without_ot(self):
        draft = _draft()
        set_timesheet_preview(draft, _timesheet_preview(), actor="reza")
        decide_timesheet(draft, "apply", actor="reza")
        set_overtime_preview(draft, _overtime_preview(), actor="reza")

        with self.assertRaisesRegex(ValueError, "No confirmed overtime record"):
            decide_overtime(
                draft, "apply", actor="reza", confirm_exceptions=False,
                resolutions=[{"key": "bambang", "category": "exclude"}],
            )
        self.assertEqual(draft["workforce_validation"]["overtime"]["status"], "preview")
        self.assertEqual(draft["safety"]["total_man_hours"], 30)

    def test_partial_overtime_keeps_not_supplied_rows_and_role_totals(self):
        draft = _draft()
        timesheet = _timesheet_preview()
        timesheet["daily_totals"].append({
            "date": "2026-08-02", "present_count": 3, "physical_manhours": 30,
            "present_by_section": {"direct": 2, "indirect": 1},
            "status_counts": {"present": 3},
        })
        timesheet["totals"].update({"present_person_days": 6, "physical_manhours": 60})
        timesheet["totals"]["by_section"] = {
            "direct": {"present_person_days": 4, "physical_manhours": 40},
            "indirect": {"present_person_days": 2, "physical_manhours": 20},
        }
        timesheet["roles"] = [{
            "role": "Technician", "present_person_days": 6, "physical_manhours": 60,
        }]
        timesheet["employees"][0]["role"] = "Technician"
        timesheet["employees"][0]["statuses"].append({
            "date": "2026-08-02", "status": "present",
        })
        set_timesheet_preview(draft, timesheet, actor="reza")
        decide_timesheet(draft, "apply", actor="reza")

        overtime = _overtime_preview()
        overtime["period"]["end"] = "2026-08-02"
        overtime["coverage"]["not_supplied_dates"] = ["2026-08-02"]
        set_overtime_preview(draft, overtime, actor="reza")
        reviewed = draft["workforce_validation"]["overtime"]["preview"]
        missing = next(row for row in reviewed["daily"] if row["date"] == "2026-08-02")
        self.assertFalse(missing["supplied"])
        self.assertIsNone(missing["actual_ot_man_hours"])

        decide_overtime(
            draft, "apply", actor="reza", confirm_exceptions=True,
            resolutions=[{"key": "bambang", "category": "direct"}],
        )
        totals = draft["manpower"]["totals"]
        self.assertEqual(totals["regular_man_hours"], 60)
        self.assertEqual(totals["overtime_man_hours"], 4)
        self.assertEqual(totals["total_man_hours"], 64)
        self.assertFalse(totals["overtime_coverage_complete"])
        self.assertFalse(totals["hours_complete"])
        missing_day = next(row for row in draft["manpower"]["daily"] if row["date"] == "2026-08-02")
        self.assertFalse(missing_day["overtime_supplied"])
        self.assertIsNone(missing_day["overtime_man_hours"])
        technician = next(row for row in draft["manpower"]["roles"] if row["role"] == "Technician")
        self.assertEqual(technician["regular_man_hours"], 60)
        self.assertEqual(technician["overtime_man_hours"], 4)
        self.assertEqual(technician["total_man_hours"], 64)

    def test_reviewed_overtime_record_requires_explicit_decision(self):
        draft = _draft()
        set_timesheet_preview(draft, _timesheet_preview(), actor="reza")
        decide_timesheet(draft, "apply", actor="reza")
        overtime = _overtime_preview()
        overtime["records"][0].update({
            "requires_review": True,
            "notes": "sakit",
            "review_reasons": ["Annotated overtime requires review: sakit"],
        })
        overtime["requires_manual_review"] = True
        set_overtime_preview(draft, overtime, actor="reza")

        with self.assertRaisesRegex(ValueError, "Choose Include or Exclude"):
            decide_overtime(
                draft,
                "apply",
                actor="reza",
                confirm_exceptions=True,
                resolutions=[{"key": "bambang", "category": "direct"}],
            )
        decide_overtime(
            draft,
            "apply",
            actor="reza",
            confirm_exceptions=True,
            resolutions=[{"key": "bambang", "category": "direct"}],
            record_resolutions=[{
                "record_id": "ot-1", "decision": "include", "duration_hours": 2.5,
            }],
        )
        self.assertEqual(draft["manpower"]["totals"]["overtime_man_hours"], 2.5)
        self.assertEqual(
            draft["workforce_validation"]["overtime"]["accepted_records"][0]["duration_hours"],
            2.5,
        )


if __name__ == "__main__":
    unittest.main()
