import copy
import unittest

from monthly_report.narrative_fallback import (
    apply_deterministic_narrative,
    build_deterministic_narrative,
)


def _weekly_report():
    return {
        "report_type": "weekly",
        "project_title": "Repair & Services Control Valve & ON OFF Valve",
        "project_no": "P01.0825.J075",
        "period": {"start": "2026-08-09", "end": "2026-08-15"},
        "coverage": {
            "expected_dates": [f"2026-08-{day:02d}" for day in range(9, 16)],
            "covered_dates": ["2026-08-09", "2026-08-10"],
            "missing_dates": [f"2026-08-{day:02d}" for day in range(11, 16)],
            "selected_record_count": 2,
        },
        "activities": [
            {
                "date": "2026-08-09",
                "area": "MA-81",
                "description": "Check valve accessories",
                "status": "Ongoing",
                "source_id": "day-309",
            },
            {
                "date": "2026-08-10",
                "area": "MA-81",
                "description": " check   valve accessories ",
                "status": "Completed",
                "source_id": "day-310",
            },
            {
                "date": "2026-08-10",
                "area": "MA-81",
                "description": "Install solenoid valve",
                "source_id": "day-310",
            },
        ],
        "tomorrow_activities": [],
        "weather": [
            {
                "date": "2026-08-09",
                "morning": "Sunny",
                "afternoon": "Cloudy",
                "wind": "Calm",
                "impact": "None",
            },
            {
                "date": "2026-08-10",
                "morning": "Sunny",
                "wind": "Calm",
                "impact": "No impact",
            },
        ],
        "constraint_reporting": {
            "none_reported_dates": ["2026-08-09", "2026-08-10"],
            "reported_dates": [],
            "not_supplied_dates": [],
        },
        "constraints": [],
        "manpower": {
            "daily": [{"date": "2026-08-09"}, {"date": "2026-08-10"}],
            "totals": {
                "peak_headcount": 16,
                "direct_man_hours": 140,
                "indirect_man_hours": 180,
                "total_man_hours": 320,
                "parsed_hours_count": 32,
                "hours_complete": True,
            },
        },
        "safety": {
            "total_manpower": 16,
            "total_man_hours": 320,
            "recordable_cases": None,
            "lost_workdays": None,
            "lost_time_injuries": None,
        },
        "engineering": {"summary": "Manual weekly input required."},
        "procurement": {"summary": "Not supplied"},
    }


class DeterministicNarrativeTests(unittest.TestCase):
    def test_builds_professional_source_grounded_weekly_fallback(self):
        result = build_deterministic_narrative(_weekly_report())
        summary = result["executive_summary"]

        self.assertIn("9–15 August 2026", summary)
        self.assertIn("P01.0825.J075", summary)
        self.assertIn("9–10 August 2026 (2 of 7 calendar dates)", summary)
        self.assertIn("11–15 August 2026 were not supplied", summary)
        self.assertIn("peak workforce of 16 personnel", summary)
        self.assertIn("total of 320 man-hours", summary)
        self.assertIn("140 direct and 180 indirect", summary)
        self.assertIn("no reported impact on work", summary)
        self.assertIn("No constraints were reported", summary)
        self.assertIn("Next-period activities were not supplied", summary)
        self.assertIn("Safety incident metrics were not supplied", summary)
        self.assertNotIn("No safety incidents", summary)

        activities = result["site"]["current_period_activities"]
        self.assertEqual(len(activities), 2)
        self.assertEqual(activities[0]["status"], "Completed")
        self.assertEqual(activities[0]["source_ids"], ["day-309", "day-310"])
        self.assertEqual(
            activities[0]["dates"],
            ["2026-08-09", "2026-08-10"],
        )
        self.assertEqual(result["site"]["this_week_activities"], activities)
        self.assertIn("Engineering status: Not supplied", result["missing_data"])
        self.assertIn("Procurement status: Not supplied", result["missing_data"])
        self.assertIn("Next-period activities: Not supplied", result["missing_data"])
        self.assertIn("Safety incident metrics: Not supplied", result["missing_data"])

    def test_preserves_supplied_sections_constraints_and_lookahead(self):
        report = _weekly_report()
        report["engineering"] = {"summary": "IFC drawing review continued."}
        report["procurement"] = {"summary": "Valve delivery was recorded."}
        report["constraints"] = [
            {
                "date": "2026-08-10",
                "area": "MA-81",
                "text": "Waiting for permit",
                "source_id": "day-310",
            },
            {
                "date": "2026-08-10",
                "area": "MA-81",
                "text": " waiting  for permit ",
                "source_id": "day-310",
            },
        ]
        report["constraint_reporting"] = {
            "none_reported_dates": ["2026-08-09"],
            "reported_dates": ["2026-08-10"],
            "not_supplied_dates": [],
        }
        report["tomorrow_activities"] = [
            {
                "source_date": "2026-08-10",
                "area": "MA-81",
                "description": "Function test",
                "source_id": "day-310",
            }
        ]

        result = build_deterministic_narrative(report)

        self.assertEqual(result["engineering"]["summary"], "IFC drawing review continued.")
        self.assertEqual(result["procurement"]["summary"], "Valve delivery was recorded.")
        self.assertIn("MA-81: Waiting for permit", result["site"]["summary"])
        self.assertEqual(len(result["site"]["concerns"]), 1)
        self.assertEqual(len(result["site"]["next_period_activities"]), 1)
        self.assertNotIn("Next-period activities: Not supplied", result["missing_data"])

    def test_rebuild_keeps_generated_missing_sections_marked_not_supplied(self):
        first = apply_deterministic_narrative(_weekly_report(), overwrite=True)

        rebuilt = build_deterministic_narrative(first)

        self.assertIn("Engineering status: Not supplied", rebuilt["missing_data"])
        self.assertIn("Procurement status: Not supplied", rebuilt["missing_data"])

    def test_includes_source_grounded_site_remarks(self):
        report = _weekly_report()
        report["remarks"] = [
            {
                "date": "2026-08-10",
                "area": "Generator Unit 1",
                "text": "Standby pending coordination",
                "source_id": "day-310",
            }
        ]

        result = build_deterministic_narrative(report)

        self.assertIn("Generator Unit 1: Standby pending coordination", result["site"]["summary"])
        self.assertEqual(result["site"]["remarks"][0]["source_ids"], ["day-310"])

    def test_unknown_constraint_state_does_not_claim_none(self):
        report = _weekly_report()
        report["constraint_reporting"] = {
            "none_reported_dates": [],
            "reported_dates": [],
            "not_supplied_dates": ["2026-08-09", "2026-08-10"],
        }

        summary = build_deterministic_narrative(report)["executive_summary"]

        self.assertIn("Constraint information was not supplied", summary)
        self.assertNotIn("No constraints were reported", summary)

    def test_incomplete_man_hours_are_qualified(self):
        report = _weekly_report()
        report["manpower"]["totals"].update({
            "total_man_hours": 210,
            "parsed_hours_count": 21,
            "hours_complete": False,
        })

        summary = build_deterministic_narrative(report)["executive_summary"]

        self.assertIn("210 man-hours from the parsed personnel entries", summary)
        self.assertIn("Man-hour information was incomplete", summary)

    def test_reviewed_workforce_effective_totals_take_precedence(self):
        report = _weekly_report()
        report["workforce_validation"] = {
            "effective": {
                "source": "timesheet",
                "peak_headcount": 18,
                "regular_man_hours": 400,
                "overtime_man_hours": 25,
                "total_man_hours": 425,
                "overtime_applied": True,
                "regular_coverage_complete": True,
                "overtime_coverage_complete": True,
                "total_hours_complete": True,
            }
        }

        summary = build_deterministic_narrative(report)["executive_summary"]

        self.assertIn("peak workforce of 18 personnel", summary)
        self.assertIn("425 total man-hours (400 regular and 25 overtime)", summary)
        self.assertNotIn("320 man-hours", summary)

    def test_incomplete_reviewed_workforce_never_claims_complete_total(self):
        report = _weekly_report()
        report["workforce_validation"] = {
            "effective": {
                "source": "timesheet",
                "peak_headcount": 15,
                "regular_man_hours": 300,
                "overtime_man_hours": 20,
                "total_man_hours": 320,
                "overtime_applied": True,
                "regular_coverage_complete": False,
                "overtime_coverage_complete": False,
                "total_hours_complete": False,
                "regular_not_supplied_dates": ["2026-08-15"],
                "overtime_not_supplied_dates": ["2026-08-14", "2026-08-15"],
            }
        }

        summary = build_deterministic_narrative(report)["executive_summary"]

        self.assertIn("320 recorded man-hours (300 regular and 20 overtime)", summary)
        self.assertIn("Workforce coverage was incomplete", summary)
        self.assertNotIn("320 total man-hours", summary)

    def test_apply_preserves_existing_review_text_and_does_not_mutate_input(self):
        report = _weekly_report()
        report["executive_summary"] = "Reviewer-approved narrative."
        report["site"] = {"summary": "Reviewer-approved site narrative."}
        before = copy.deepcopy(report)

        result = apply_deterministic_narrative(report)

        self.assertEqual(report, before)
        self.assertEqual(result["executive_summary"], "Reviewer-approved narrative.")
        self.assertEqual(result["site"]["summary"], "Reviewer-approved site narrative.")
        self.assertIn("deterministic_narrative", result)

    def test_monthly_output_uses_monthly_label_and_legacy_aliases(self):
        report = _weekly_report()
        report["report_type"] = "monthly"
        report["period"] = {"start": "2026-08-01", "end": "2026-08-31"}

        result = build_deterministic_narrative(report)

        self.assertIn("This Monthly Progress Report covers 1–31 August 2026", result["executive_summary"])
        self.assertEqual(
            result["site"]["this_month_activities"],
            result["site"]["current_period_activities"],
        )
        self.assertNotIn("this_week_activities", result["site"])


if __name__ == "__main__":
    unittest.main()
