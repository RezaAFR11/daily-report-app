import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from monthly_report.web import (
    _normalize_progress,
    _prepare_draft,
    register_monthly_routes,
)


PROJECT_NO = "001/KN-GPA/EPC-2F-P2/IV/2025"
PROJECT_TITLE = "REACTIVATION FOR TURBINES AND GENERATORS"


def _canonical_record(report_date, report_id, *, tomorrow, progress_actual):
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
                    "this_period_actual": progress_actual - 20,
                    "cumulative_to_date_actual": progress_actual,
                    "cumulative_to_date_plan": progress_actual + 2,
                }
            ],
            "indirect_manpower": [
                {"name": "Faiz", "role": "Project Control", "hours": "07:00 - 17:00"}
            ],
            "areas": [
                {
                    "id": "Turbine Unit 2",
                    "activities_today": [f"Activity on {report_date}"],
                    "activities_tomorrow": [tomorrow],
                    "constraints": "No constraint",
                    "manpower": [
                        {"name": "Bambang", "role": "Foreman", "hours": "07:00 - 17:00"}
                    ],
                }
            ],
        },
    }


class MonthlyWebUnitTests(unittest.TestCase):
    def test_normalize_progress_uses_weight_contribution_formula(self):
        result = _normalize_progress(
            {
                "rows": [
                    {
                        "description": "Engineering",
                        "weight": 25,
                        "previous": 40,
                        "this_month": 4,
                        "plan": 50,
                    },
                    {
                        "description": "Construction",
                        "weight": 50,
                        "previous": 20,
                        "this_month": 10,
                        "plan": 40,
                    },
                ]
            }
        )

        total = result["rows"][-1]
        self.assertTrue(total["is_total"])
        self.assertEqual(total["weight"], 75)
        # Contributions are sum(weight * value / 100), deliberately not
        # re-normalised by the incomplete 75% total weight.
        self.assertEqual(total["previous"], 20)
        self.assertEqual(total["this_month"], 6)
        self.assertEqual(total["to_date"], 26)
        self.assertEqual(total["plan"], 32.5)
        self.assertEqual(total["variance"], -6.5)

    def test_prepare_draft_maps_aggregate_fields_for_review(self):
        aggregate = {
            "coverage": {
                "expected_dates": ["2026-07-01", "2026-07-02", "2026-07-03"],
                "covered_dates": ["2026-07-01", "2026-07-03"],
                "missing_dates": ["2026-07-02"],
                "selected_record_count": 2,
                "duplicate_dates": ["2026-07-03"],
            },
            "overall_progress": {
                "rows": [
                    {
                        "description": "Engineering",
                        "weight_factor": 25,
                        "cumulative_previous_actual": 30,
                        "this_period_actual": 5,
                        "cumulative_to_date_actual": 35,
                        "cumulative_to_date_plan": 38,
                        "deviation": -3,
                    }
                ]
            },
            "activities": [
                {"date": "2026-07-01", "area": "Unit 2", "description": "Cable pulling"}
            ],
            "tomorrow_activities": [
                {"source_date": "2026-07-03", "area": "Unit 2", "description": "Loop test"}
            ],
            "constraints": [
                {"date": "2026-07-03", "area": "Unit 2", "text": "Waiting permit"}
            ],
            "manpower": {
                "totals": {"peak_headcount": 12, "total_man_hours": 224.5}
            },
        }
        manifest = [
            {"report_id": "day-1", "report_date": "2026-07-01"},
            {"report_id": "day-3", "report_date": "2026-07-03"},
        ]

        draft = _prepare_draft(
            aggregate,
            project_no=PROJECT_NO,
            project_title=PROJECT_TITLE,
            date_from="2026-07-01",
            date_to="2026-07-03",
            report_mode="mtd",
            source_method="stored_json",
            source_manifest=manifest,
        )

        self.assertEqual(draft["coverage"]["found_dates"], ["2026-07-01", "2026-07-03"])
        self.assertEqual(draft["coverage"]["missing_dates"], ["2026-07-02"])
        self.assertEqual(draft["coverage"]["included_count"], 2)
        self.assertEqual(draft["coverage"]["duplicate_count"], 1)
        self.assertEqual(
            draft["progress"]["rows"],
            [
                {
                    "description": "Engineering",
                    "weight": 25,
                    "previous": 30,
                    "this_month": 5,
                    "to_date": 35,
                    "plan": 38,
                    "variance": -3,
                }
            ],
        )
        self.assertEqual(draft["site"]["this_month_activities"], aggregate["activities"])
        self.assertEqual(draft["site"]["next_month_activities"], aggregate["tomorrow_activities"])
        self.assertEqual(draft["site"]["concerns"], aggregate["constraints"])
        self.assertEqual(draft["safety"]["total_manpower"], 12)
        self.assertEqual(draft["safety"]["total_man_hours"], 224.5)


class MonthlyWebRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="monthly-route-tests")
        register_monthly_routes(
            self.app,
            data_dir=str(self.data_dir),
            config_provider=lambda: {
                "projects": [{"project_no": PROJECT_NO, "project_title": PROJECT_TITLE}]
            },
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = "reza"
            flask_session["is_admin"] = False
        self.records = [
            _canonical_record(
                "2026-07-01",
                "day-1",
                tomorrow="Continue cable pulling",
                progress_actual=24,
            ),
            _canonical_record(
                "2026-07-02",
                "day-2",
                tomorrow="Start loop test",
                progress_actual=30,
            ),
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def _compile_stored(self, report_mode="mtd"):
        with patch("monthly_report.web.list_canonical_records", return_value=self.records) as loader:
            response = self.client.post(
                "/monthly/compile/stored",
                json={
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-02",
                    "report_mode": report_mode,
                },
            )
        loader.assert_called_once_with(
            str(self.data_dir),
            username="reza",
            project_no=PROJECT_NO,
            date_from="2026-07-01",
            date_to="2026-07-02",
        )
        return response

    def test_stored_compile_uses_records_and_persists_isolated_draft(self):
        response = self._compile_stored()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["draft"]["coverage"]["included_count"], 2)
        self.assertEqual(body["draft"]["coverage"]["missing_dates"], [])
        self.assertEqual(
            body["draft"]["site"]["next_month_activities"],
            [
                {
                    "area": "Turbine Unit 2",
                    "description": "Start loop test",
                    "source_date": "2026-07-02",
                }
            ],
        )
        draft_path = (
            self.data_dir
            / "monthly_reports"
            / "reza"
            / "drafts"
            / f"{body['draft_id']}.json"
        )
        self.assertTrue(draft_path.is_file())
        self.assertEqual(json.loads(draft_path.read_text(encoding="utf-8"))["owner"], "reza")

    def test_final_requires_confirmation_then_archives_and_downloads_pdf(self):
        compile_response = self._compile_stored(report_mode="final")
        self.assertEqual(compile_response.status_code, 200, compile_response.get_data(as_text=True))
        draft_id = compile_response.get_json()["draft_id"]

        rejected = self.client.post(
            f"/monthly/generate/{draft_id}",
            json={"report_mode": "final"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("Confirm", rejected.get_json()["error"])
        self.assertFalse(
            (self.data_dir / "monthly_reports" / "reza" / "index.json").exists()
        )

        preview = self.client.post(
            f"/monthly/preview/{draft_id}",
            json={"report_mode": "final"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.mimetype, "application/pdf")
        self.assertTrue(preview.data.startswith(b"%PDF-"))
        preview.close()

        generated = self.client.post(
            f"/monthly/generate/{draft_id}",
            json={"report_mode": "final", "confirm_final": True},
        )
        self.assertEqual(generated.status_code, 200, generated.get_data(as_text=True))
        generated_body = generated.get_json()
        self.assertTrue(generated_body["ok"])

        reports_dir = self.data_dir / "monthly_reports" / "reza" / "reports"
        pdf_path = reports_dir / generated_body["filename"]
        json_path = reports_dir / f"{pdf_path.stem}.json"
        self.assertTrue(pdf_path.is_file())
        self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(json_path.is_file())
        archived = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(archived["status"], "final")
        self.assertTrue(archived["final_review"]["confirmed"])

        download = self.client.get(generated_body["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.mimetype, "application/pdf")
        self.assertEqual(download.data, pdf_path.read_bytes())
        download.close()


if __name__ == "__main__":
    unittest.main()
