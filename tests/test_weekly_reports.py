import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from pypdf import PdfReader

from monthly_report.renderer import _normalise_progress, render_monthly_report
from monthly_report.web import (
    _parse_period,
    _prepare_draft,
    _rolling_week_period,
    get_monthly_reports_index,
    register_monthly_routes,
)


PROJECT_NO = "001/KN-GPA/EPC-2F-P2/IV/2025"
PROJECT_TITLE = "REACTIVATION FOR TURBINES AND GENERATORS"


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _canonical_record(report_date: str, report_id: str) -> dict:
    return {
        "record_type": "final_daily_report",
        "report_id": report_id,
        "revision": 1,
        "username": "reza",
        "date": report_date,
        "project_no": PROJECT_NO,
        "project_title": PROJECT_TITLE,
        "generated_at": f"{report_date}T17:30:00+08:00",
        "payload": {
            "date": report_date,
            "project_no": PROJECT_NO,
            "project_title": PROJECT_TITLE,
            "company_name": "PT. GARUDA PRIMA AKSARA",
            "customer": "PT. KERTAS NUSANTARA",
            "location": "Berau, East Kalimantan",
            "prepared_by": "Project Control",
            "show_overall_progress": True,
            "overall_progress": [
                {
                    "item_id": "ENG",
                    "description": "Engineering",
                    "weight_factor": 100,
                    "cumulative_previous_actual": 20,
                    "this_period_actual": 4,
                    "cumulative_to_date_actual": 24,
                    "cumulative_to_date_plan": 26,
                }
            ],
            "areas": [
                {
                    "id": "Turbine Unit 2",
                    "activities_today": [f"Weekly activity on {report_date}"],
                    "activities_tomorrow": ["Continue weekly activity"],
                    "constraints": "No constraint",
                    "manpower": [
                        {
                            "name": "Bambang",
                            "role": "Foreman",
                            "hours": "07:00 - 17:00",
                        }
                    ],
                }
            ],
        },
    }


class WeeklyPeriodTests(unittest.TestCase):
    def test_week_to_date_accepts_arbitrary_start_and_may_cross_month(self):
        start, end = _parse_period(
            "2026-08-26",
            "2026-09-01",
            report_type="weekly",
            report_mode="wtd",
        )

        self.assertEqual(start.strftime("%Y-%m-%d"), "2026-08-26")
        self.assertEqual(end.strftime("%Y-%m-%d"), "2026-09-01")

    def test_full_week_accepts_any_exact_seven_day_period_across_year(self):
        start, end = _parse_period(
            "2026-12-30",
            "2027-01-05",
            report_type="weekly",
            report_mode="final",
        )

        self.assertEqual((end - start).days, 6)

    def test_invalid_weekly_periods_are_rejected(self):
        invalid_periods = (
            ("2026-08-03", "2026-08-10", "wtd", "longer than 7 days"),
            ("2026-08-03", "2026-08-08", "draft", "exactly 7 consecutive days"),
            ("2026-08-03", "2026-08-06", "final", "exactly 7 consecutive days"),
        )

        for date_from, date_to, mode, message in invalid_periods:
            with self.subTest(date_from=date_from, date_to=date_to, mode=mode):
                with self.assertRaisesRegex(ValueError, message):
                    _parse_period(
                        date_from,
                        date_to,
                        report_type="weekly",
                        report_mode=mode,
                    )

    def test_uploaded_week_is_anchored_to_earliest_valid_record_not_list_order(self):
        start, end = _rolling_week_period([
            _canonical_record("2026-08-14", "later"),
            _canonical_record("2026-08-10", "earliest"),
            _canonical_record("2026-08-12", "middle"),
        ])

        self.assertEqual(start.strftime("%Y-%m-%d"), "2026-08-10")
        self.assertEqual(end.strftime("%Y-%m-%d"), "2026-08-16")

    def test_existing_monthly_same_month_rule_is_unchanged(self):
        with self.assertRaisesRegex(ValueError, "one calendar month"):
            _parse_period(
                "2026-08-31",
                "2026-09-01",
                report_type="monthly",
                report_mode="mtd",
            )


class WeeklyDraftTests(unittest.TestCase):
    def test_prepare_weekly_draft_adds_period_neutral_and_weekly_aliases(self):
        activities = [
            {"date": "2026-08-03", "area": "Unit 2", "description": "Loop test"}
        ]
        plans = [
            {
                "source_date": "2026-08-04",
                "area": "Unit 2",
                "description": "Continue loop test",
            }
        ]
        draft = _prepare_draft(
            {
                "activities": activities,
                "tomorrow_activities": plans,
                "coverage": {
                    "expected_dates": ["2026-08-03", "2026-08-04"],
                    "covered_dates": ["2026-08-03", "2026-08-04"],
                    "missing_dates": [],
                    "selected_record_count": 2,
                },
            },
            project_no=PROJECT_NO,
            project_title=PROJECT_TITLE,
            date_from="2026-08-03",
            date_to="2026-08-04",
            report_mode="wtd",
            source_method="stored_json",
            source_manifest=[
                {"report_id": "day-1", "report_date": "2026-08-03"},
                {"report_id": "day-2", "report_date": "2026-08-04"},
            ],
            report_type="weekly",
        )

        self.assertEqual(draft["schema_version"], "weekly-report/1")
        self.assertEqual(draft["report_type"], "weekly")
        self.assertEqual(draft["report_title"], "Weekly Progress Report")
        self.assertEqual(draft["report_mode"], "wtd")
        self.assertEqual(draft["status"], "wtd")
        current = draft["site"]["current_period_activities"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["area"], "Unit 2")
        self.assertEqual(current[0]["workstream"], "Testing & Commissioning")
        self.assertEqual(current[0]["source_dates"], ["2026-08-03"])
        self.assertTrue(current[0]["stable_id"].startswith("activity-"))
        self.assertEqual(draft["site"]["this_week_activities"], current)
        lookahead = draft["site"]["next_period_activities"]
        self.assertEqual(len(lookahead), 1)
        self.assertEqual(lookahead[0]["area"], "Unit 2")
        self.assertEqual(lookahead[0]["description"], "Continue loop test")
        self.assertEqual(lookahead[0]["source_date"], "2026-08-04")
        self.assertEqual(lookahead[0]["source_type"], "period_end_activity_tomorrow")
        self.assertEqual(draft["site"]["next_week_activities"], lookahead)
        self.assertIn("reporting week (03-04 August 2026)", draft["executive_summary"])
        self.assertIn("Unit 2", draft["executive_summary"])


class WeeklyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="weekly-route-tests")
        register_monthly_routes(
            self.app,
            data_dir=str(self.data_dir),
            config_provider=lambda: {
                "projects": [
                    {"project_no": PROJECT_NO, "project_title": PROJECT_TITLE}
                ]
            },
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = "reza"
            flask_session["is_admin"] = False
        self.records = [
            _canonical_record("2026-08-03", "weekly-day-1"),
            _canonical_record("2026-08-04", "weekly-day-2"),
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def _compile_weekly(self):
        with patch(
            "monthly_report.web.list_canonical_records",
            return_value=self.records,
        ) as loader:
            response = self.client.post(
                "/monthly/compile/stored",
                json={
                    "report_type": "weekly",
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-08-03",
                    "date_to": "2026-08-04",
                    "report_mode": "wtd",
                },
            )
        loader.assert_called_once_with(
            str(self.data_dir),
            username="reza",
            date_from="2026-08-03",
            date_to="2026-08-04",
        )
        return response

    def _apply_source_validation(self, compiled_body):
        validation = compiled_body["draft"]["source_validation"]
        groups = validation["project_groups"]
        response = self.client.post(
            f"/monthly/validate/{compiled_body['draft_id']}",
            json={
                "source_validation": {
                    "confirmed": True,
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "project_resolutions": [
                        {"group_key": group["key"], "decision": "merge"}
                        for group in groups
                    ],
                    "duplicate_resolutions": [
                        {
                            "group_key": group["key"],
                            "selected_record_id": group["candidates"][0]["record_id"],
                        }
                        for group in validation.get("duplicate_groups", [])
                    ],
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def test_stored_weekly_compile_returns_and_persists_typed_draft(self):
        response = self._compile_weekly()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        draft = body["draft"]
        self.assertEqual(draft["report_type"], "weekly")
        self.assertEqual(draft["report_mode"], "wtd")
        self.assertEqual(draft["coverage"]["missing_dates"], [])
        self.assertEqual(
            draft["site"]["this_week_activities"],
            draft["site"]["current_period_activities"],
        )

        saved_path = (
            self.data_dir
            / "monthly_reports"
            / "reza"
            / "drafts"
            / f"{body['draft_id']}.json"
        )
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["report_type"], "weekly")
        self.assertEqual(saved["schema_version"], "weekly-report/1")

    def test_incomplete_full_week_is_rejected_before_loading_records(self):
        with patch("monthly_report.web.list_canonical_records") as loader:
            response = self.client.post(
                "/monthly/compile/stored",
                json={
                    "report_type": "weekly",
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-08-03",
                    "date_to": "2026-08-06",
                    "report_mode": "final",
                },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("exactly 7 consecutive days", response.get_json()["error"])
        loader.assert_not_called()

    def test_changing_partial_draft_to_full_week_returns_validation_error(self):
        compiled = self._compile_weekly()
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        draft_id = compiled.get_json()["draft_id"]

        preview = self.client.post(
            f"/monthly/preview/{draft_id}",
            json={"report_mode": "final", "confirm_final": True},
        )

        self.assertEqual(preview.status_code, 400, preview.get_data(as_text=True))
        self.assertIn("exactly 7 consecutive days", preview.get_json()["error"])

        generated = self.client.post(
            f"/monthly/generate/{draft_id}",
            json={"report_mode": "final", "confirm_final": True},
        )
        self.assertEqual(generated.status_code, 400, generated.get_data(as_text=True))
        self.assertIn("exactly 7 consecutive days", generated.get_json()["error"])

    def test_weekly_generation_uses_weekly_filename_and_separate_revision_series(self):
        compiled = self._compile_weekly()
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        validated = self._apply_source_validation(compiled.get_json())
        draft_id = validated["draft_id"]

        report_dir = self.data_dir / "monthly_reports" / "reza"
        legacy_monthly_row = {
            "filename": "Monthly Progress Report legacy R9.pdf",
            "project_no": PROJECT_NO,
            "period_start": "2026-08-03",
            "period_end": "2026-08-04",
            "revision": 9,
        }
        (report_dir / "index.json").write_text(
            json.dumps([legacy_monthly_row]),
            encoding="utf-8",
        )

        with patch(
            "monthly_report.web._render",
            return_value=io.BytesIO(b"%PDF-weekly-test"),
        ):
            generated = self.client.post(
                f"/monthly/generate/{draft_id}",
                json={"report_mode": "wtd"},
            )

        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        filename = generated.get_json()["filename"]
        self.assertTrue(filename.startswith("Weekly Progress Report -"))
        self.assertTrue(filename.endswith("(WTD) - R1.pdf"))

        index = get_monthly_reports_index(self.data_dir, "reza")
        self.assertEqual(index[0]["report_type"], "weekly")
        self.assertEqual(index[0]["revision"], 1)
        self.assertEqual(index[1]["report_type"], "monthly")


class WeeklyRendererTests(unittest.TestCase):
    def test_weekly_native_progress_alias_is_preserved(self):
        rows = _normalise_progress([
            {"description": "Engineering", "this_week": 4},
            {"description": "Construction", "this_week_actual": 2.5},
        ])

        self.assertEqual(rows[0]["this_month"], 4)
        self.assertEqual(rows[1]["this_month"], 2.5)

    def test_weekly_report_uses_dynamic_week_labels(self):
        result = render_monthly_report({
            "report_type": "weekly",
            "status": "wtd",
            "project_name": PROJECT_TITLE,
            "project_no": PROJECT_NO,
            "reporting_period": "2026-08-03 to 2026-08-04",
            "progress": {
                "rows": [
                    {
                        "description": "Engineering",
                        "previous": 20,
                        "this_week": 4,
                        "to_date": 24,
                        "plan": 26,
                    }
                ]
            },
            "site": {
                "this_week_activities": ["Weekly loop test"],
                "next_week_activities": ["Weekly commissioning plan"],
            },
        })

        text = _pdf_text(result.getvalue())
        self.assertIn("Weekly Progress Report", text)
        self.assertIn("WTD", text)
        self.assertIn("This Week", text)
        self.assertIn("5.2 This Week Activities", text)
        self.assertIn("5.3 Planned Activities Next Week", text)
        self.assertIn("Weekly loop test", text)
        self.assertIn("Weekly commissioning plan", text)
        self.assertNotIn("Monthly Progress Report", text)
        self.assertNotIn("This Month Activities", text)
        self.assertNotIn("Planned Activities Next Month", text)

    def test_legacy_report_without_type_still_renders_as_monthly(self):
        result = render_monthly_report({
            "status": "mtd",
            "project_name": "Legacy Monthly Project",
            "site": {
                "this_month_activities": ["Legacy monthly activity"],
                "next_month_activities": ["Legacy monthly plan"],
            },
        })

        text = _pdf_text(result.getvalue())
        self.assertIn("Monthly Progress Report", text)
        self.assertIn("MTD", text)
        self.assertIn("5.2 This Month Activities", text)
        self.assertIn("5.3 Planned Activities Next Month", text)
        self.assertIn("Legacy monthly activity", text)
        self.assertNotIn("Weekly Progress Report", text)

    def test_legacy_history_entry_without_type_defaults_to_monthly(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary) / "monthly_reports" / "reza"
            report_dir.mkdir(parents=True)
            (report_dir / "index.json").write_text(
                json.dumps([{"filename": "Legacy Monthly.pdf", "revision": 3}]),
                encoding="utf-8",
            )

            rows = get_monthly_reports_index(temporary, "reza")

        self.assertEqual(rows[0]["report_type"], "monthly")
        self.assertEqual(rows[0]["revision"], 3)


if __name__ == "__main__":
    unittest.main()
