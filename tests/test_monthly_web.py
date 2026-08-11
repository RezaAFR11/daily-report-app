import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from PIL import Image
from pypdf import PdfReader

from monthly_report.importer import DEFAULT_LIMITS, PDFImportError
from monthly_report.web import (
    _normalize_progress,
    _prepare_draft,
    register_monthly_routes,
)


PROJECT_NO = "001/KN-GPA/EPC-2F-P2/IV/2025"
PROJECT_TITLE = "REACTIVATION FOR TURBINES AND GENERATORS"


def _jpeg_bytes(colour="#2563eb"):
    output = io.BytesIO()
    Image.new("RGB", (640, 480), colour).save(output, format="JPEG", quality=88)
    return output.getvalue()


def _canonical_record(report_date, report_id, *, tomorrow, progress_actual):
    return {
        "record_type": "final_daily_report",
        "report_id": report_id,
        "revision": 1,
        "username": "reza",
        "_canonical_owner": "reza",
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

    def _compile_stored(self, report_mode="mtd", report_type="monthly"):
        with patch("monthly_report.web.list_canonical_records", return_value=self.records) as loader:
            response = self.client.post(
                "/monthly/compile/stored",
                json={
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-02",
                    "report_mode": report_mode,
                    "report_type": report_type,
                },
            )
        loader.assert_called_once_with(
            str(self.data_dir),
            username="reza",
            date_from="2026-07-01",
            date_to="2026-07-02",
        )
        return response

    def _attach_canonical_photo(
        self,
        record,
        *,
        caption="Stored valve inspection",
        colour="#2563eb",
    ):
        raw = _jpeg_bytes(colour)
        digest = hashlib.sha256(raw).hexdigest()
        relative_path = f"assets/{digest}.jpg"
        target = (
            self.data_dir
            / "users"
            / record["username"]
            / "reports"
            / "canonical"
            / relative_path
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        asset = {
            "sha256": digest,
            "size_bytes": len(raw),
            "asset_path": relative_path,
            "original_name": "stored-photo.jpg",
        }
        record["payload"]["areas"][0]["photos"] = [{
            "photo_filename": "stored-photo.jpg",
            "desc": caption,
            "asset": asset,
        }]
        record["assets"] = [asset]
        return raw

    def _start_staged_upload(
        self,
        file_ids=None,
        *,
        report_type="monthly",
        report_mode="mtd",
        date_from="2026-07-01",
        date_to="2026-07-02",
        size_bytes=1024,
    ):
        file_ids = file_ids or ["a" * 32]
        response = self.client.post(
            "/monthly/upload-session/start",
            json={
                "project_no": PROJECT_NO,
                "project_title": PROJECT_TITLE,
                "date_from": date_from,
                "date_to": date_to,
                "report_type": report_type,
                "report_mode": report_mode,
                "files": [
                    {
                        "file_id": file_id,
                        "filename": f"daily-{index + 1}.pdf",
                        "size_bytes": size_bytes,
                    }
                    for index, file_id in enumerate(file_ids)
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["upload_session_id"]

    def _imported_pdf(self, report_date, digest):
        payload = json.loads(json.dumps(self.records[0]["payload"]))
        payload["date"] = report_date
        return {
            "revision": 1,
            "status": "ready",
            "report_date": report_date,
            "data": payload,
            "source": {
                "filename": f"{report_date}.pdf",
                "sha256": digest,
                "size_bytes": 1024,
                "page_count": 5,
            },
            "confidence": {"overall": 0.95},
            "warnings": [],
        }

    def _upload_staged_file(self, upload_session_id, file_id, filename="daily.pdf"):
        return self.client.post(
            f"/monthly/upload-session/{upload_session_id}/file",
            headers={"X-Upload-File-ID": file_id},
            data={
                "file_id": file_id,
                "file": (io.BytesIO(b"%PDF-1.4 staged test"), filename),
            },
            content_type="multipart/form-data",
        )

    def _apply_source_validation(self, compiled_body, *, decisions=None):
        draft = compiled_body["draft"]
        groups = draft["source_validation"]["project_groups"]
        resolutions = decisions or [
            {"group_key": group["key"], "decision": "merge"}
            for group in groups
        ]
        duplicate_resolutions = [
            {
                "group_key": group["key"],
                "selected_record_id": group["candidates"][0]["record_id"],
            }
            for group in draft["source_validation"].get("duplicate_groups", [])
        ]
        response = self.client.post(
            f"/monthly/validate/{compiled_body['draft_id']}",
            json={
                "source_validation": {
                    "confirmed": True,
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "notes": "Reviewed in route test",
                    "project_resolutions": resolutions,
                    "duplicate_resolutions": duplicate_resolutions,
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

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

    def test_stored_json_photo_is_reviewable_and_rendered_in_appendix_66(self):
        self._attach_canonical_photo(self.records[0])

        response = self._compile_stored()

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        photos = body["draft"]["photo_documentation"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["caption"], "Stored valve inspection")
        self.assertIn("2026-07-01", photos[0]["source"])
        self.assertIn("Turbine Unit 2", photos[0]["source"])
        self.assertNotIn("page", photos[0])

        asset_id = photos[0]["asset_id"]
        draft_asset = (
            self.data_dir
            / "monthly_reports"
            / "reza"
            / "draft_assets"
            / body["draft_id"]
            / f"{asset_id}.jpg"
        )
        self.assertTrue(draft_asset.is_file())
        fetched = self.client.get(f"/monthly/photos/{body['draft_id']}/{asset_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.mimetype, "image/jpeg")
        fetched.close()

        validated = self._apply_source_validation(body)
        self.assertEqual(
            validated["draft"]["photo_documentation"][0]["caption"],
            "Stored valve inspection",
        )
        preview = self.client.post(
            f"/monthly/preview/{body['draft_id']}",
            json={"report_mode": "mtd"},
        )
        self.assertEqual(preview.status_code, 200)
        reader = PdfReader(io.BytesIO(preview.data))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Appendix 6.6 - Photographs Activity", text)
        self.assertIn("Stored valve inspection", text)
        preview.close()

    def test_stored_json_photo_is_included_for_weekly_report(self):
        self._attach_canonical_photo(self.records[0], caption="Weekly stored photo")

        response = self._compile_stored(report_mode="wtd", report_type="weekly")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        draft = response.get_json()["draft"]
        self.assertEqual(draft["report_type"], "weekly")
        self.assertEqual(len(draft["photo_documentation"]), 1)
        self.assertEqual(draft["photo_documentation"][0]["caption"], "Weekly stored photo")

    def test_project_ambiguity_requires_validation_and_keep_separate_reaggregates(self):
        variant = json.loads(json.dumps(self.records[1]))
        variant["project_no"] = "LEGACY-PC-DAR"
        variant["project_title"] = "Legacy Project Name"
        variant["payload"]["project_no"] = "LEGACY-PC-DAR"
        variant["payload"]["project_title"] = "Legacy Project Name"
        with patch(
            "monthly_report.web.list_canonical_records",
            return_value=[self.records[0], variant],
        ):
            compiled = self.client.post(
                "/monthly/compile/stored",
                json={
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-02",
                    "report_mode": "mtd",
                },
            )
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        body = compiled.get_json()
        self.assertTrue(body["draft"]["source_validation"]["required"])
        self.assertEqual(body["draft"]["coverage"]["included_count"], 1)

        blocked = self.client.post(
            f"/monthly/preview/{body['draft_id']}",
            json={"report_mode": "mtd"},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("Source Data Validation", blocked.get_json()["error"])

        decisions = []
        for group in body["draft"]["source_validation"]["project_groups"]:
            decisions.append({
                "group_key": group["key"],
                "decision": "separate" if group["project_no"] == "LEGACY-PC-DAR" else "merge",
            })
        validated = self._apply_source_validation(body, decisions=decisions)
        self.assertEqual(validated["draft"]["coverage"]["included_count"], 1)
        self.assertEqual(
            validated["draft"]["source_validation"]["excluded_record_count"],
            1,
        )
        self.assertTrue(validated["draft"]["source_validation"]["applied"])

        merged = self._apply_source_validation(validated)
        self.assertEqual(merged["draft"]["coverage"]["included_count"], 2)
        self.assertFalse(any(
            "kept separate and excluded" in warning
            for warning in merged["draft"].get("warnings", [])
        ))

    def test_same_date_duplicate_is_selected_explicitly_before_generation(self):
        first = json.loads(json.dumps(self.records[0]))
        second = json.loads(json.dumps(self.records[1]))
        second["date"] = first["date"]
        second["payload"]["date"] = first["date"]
        second["report_id"] = "report-explicit-second"
        self._attach_canonical_photo(
            first,
            caption="Photo from explicitly selected first source",
            colour="#15803d",
        )
        self._attach_canonical_photo(
            second,
            caption="Photo from provisional second source",
            colour="#dc2626",
        )
        with patch(
            "monthly_report.web.list_canonical_records",
            return_value=[first, second],
        ):
            compiled = self.client.post(
                "/monthly/compile/stored",
                json={
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-01",
                    "report_mode": "mtd",
                },
            )
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        body = compiled.get_json()
        duplicate = body["draft"]["source_validation"]["duplicate_groups"][0]
        chosen_id = duplicate["candidates"][0]["record_id"]
        validation = body["draft"]["source_validation"]
        response = self.client.post(
            f"/monthly/validate/{body['draft_id']}",
            json={
                "source_validation": {
                    "confirmed": True,
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "project_resolutions": [
                        {"group_key": group["key"], "decision": "merge"}
                        for group in validation["project_groups"]
                    ],
                    "duplicate_resolutions": [{
                        "group_key": duplicate["key"],
                        "selected_record_id": chosen_id,
                    }],
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        validated = response.get_json()["draft"]
        self.assertEqual(validated["coverage"]["included_count"], 1)
        self.assertEqual(validated["source_manifest"][0]["report_id"], chosen_id)
        self.assertEqual(
            validated["source_validation"]["duplicate_excluded_record_count"],
            1,
        )
        self.assertEqual(len(validated["photo_documentation"]), 1)
        self.assertEqual(
            validated["photo_documentation"][0]["caption"],
            "Photo from explicitly selected first source",
        )
        draft_assets = list(
            (
                self.data_dir
                / "monthly_reports"
                / "reza"
                / "draft_assets"
                / body["draft_id"]
            ).glob("*.jpg")
        )
        self.assertEqual(len(draft_assets), 1)

    def test_stored_photo_hydration_only_scans_provisionally_selected_records(self):
        unrelated = json.loads(json.dumps(self.records[0]))
        unrelated["report_id"] = "unrelated-project"
        unrelated["project_no"] = "OTHER-001"
        unrelated["project_title"] = "ANOTHER PROJECT"
        unrelated["payload"]["project_no"] = "OTHER-001"
        unrelated["payload"]["project_title"] = "ANOTHER PROJECT"

        with (
            patch(
                "monthly_report.web.list_canonical_records",
                return_value=[unrelated, self.records[0]],
            ),
            patch(
                "monthly_report.web.attach_canonical_photo_candidates",
                return_value=[],
            ) as hydrate,
        ):
            response = self.client.post(
                "/monthly/compile/stored",
                json={
                    "project_no": PROJECT_NO,
                    "project_title": PROJECT_TITLE,
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-01",
                    "report_mode": "mtd",
                },
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        hydrated_records = hydrate.call_args.args[0]
        self.assertEqual(
            [record["report_id"] for record in hydrated_records],
            [self.records[0]["report_id"]],
        )

    def test_final_requires_confirmation_then_archives_and_downloads_pdf(self):
        compile_response = self._compile_stored(report_mode="final")
        self.assertEqual(compile_response.status_code, 200, compile_response.get_data(as_text=True))
        validated = self._apply_source_validation(compile_response.get_json())
        draft_id = validated["draft_id"]

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

    def test_staged_upload_processes_files_separately_and_finalize_is_idempotent(self):
        file_ids = ["a" * 32, "b" * 32]
        upload_session_id = self._start_staged_upload(file_ids)
        imported = [
            self._imported_pdf("2026-07-01", "1" * 64),
            self._imported_pdf("2026-07-02", "2" * 64),
        ]

        with patch("monthly_report.web.import_daily_report_pdf", side_effect=imported) as importer:
            first = self._upload_staged_file(upload_session_id, file_ids[0], "day-1.pdf")
            second = self._upload_staged_file(upload_session_id, file_ids[1], "day-2.pdf")

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertTrue(first.get_json()["item"]["included"])
        self.assertTrue(second.get_json()["item"]["included"])
        self.assertEqual(importer.call_count, 2)

        compiled = self.client.post(f"/monthly/upload-session/{upload_session_id}/compile")
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        first_body = compiled.get_json()
        self.assertEqual(first_body["draft"]["coverage"]["included_count"], 2)

        compiled_again = self.client.post(f"/monthly/upload-session/{upload_session_id}/compile")
        self.assertEqual(compiled_again.status_code, 200, compiled_again.get_data(as_text=True))
        second_body = compiled_again.get_json()
        self.assertTrue(second_body["cached"])
        self.assertEqual(second_body["draft_id"], first_body["draft_id"])

        drafts = list(
            (self.data_dir / "monthly_reports" / "reza" / "drafts").glob("*.json")
        )
        self.assertEqual(len(drafts), 1)
        upload_dir = (
            self.data_dir
            / "monthly_reports"
            / "reza"
            / "upload_sessions"
            / upload_session_id
        )
        self.assertTrue((upload_dir / "result.json").is_file())
        self.assertFalse((upload_dir / "items").exists())
        self.assertEqual(list(upload_dir.rglob("*.pdf")), [])

    def test_staged_weekly_compile_anchors_to_earliest_valid_pdf_date(self):
        file_ids = ["a" * 32, "b" * 32, "c" * 32]
        upload_session_id = self._start_staged_upload(
            file_ids,
            report_type="weekly",
            report_mode="wtd",
            date_from="2026-08-03",
            date_to="2026-08-09",
        )
        later = self._imported_pdf("2026-08-14", "a" * 64)
        earliest = self._imported_pdf("2026-08-10", "b" * 64)
        earliest["data"]["project_no"] = "LEGACY-PROJECT-NO"
        earliest["data"]["project_title"] = "Legacy Project Name"
        outside = self._imported_pdf("2026-08-18", "c" * 64)

        with patch(
            "monthly_report.web.import_daily_report_pdf",
            side_effect=[later, earliest, outside],
        ):
            for index, file_id in enumerate(file_ids):
                response = self._upload_staged_file(
                    upload_session_id,
                    file_id,
                    f"weekly-{index + 1}.pdf",
                )
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        compiled = self.client.post(f"/monthly/upload-session/{upload_session_id}/compile")

        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        draft = compiled.get_json()["draft"]
        self.assertEqual(draft["period"]["start"], "2026-08-10")
        self.assertEqual(draft["period"]["end"], "2026-08-16")
        self.assertEqual(draft["coverage"]["included_count"], 1)
        self.assertTrue(any("2026-08-18" in warning for warning in draft["warnings"]))
        validated = self._apply_source_validation(compiled.get_json())
        self.assertEqual(validated["draft"]["coverage"]["included_count"], 2)
        source_projects = {
            row.get("source_project_no") for row in validated["draft"]["source_manifest"]
        }
        self.assertIn("LEGACY-PROJECT-NO", source_projects)

    def test_staged_upload_metadata_accepts_exact_file_limit_and_rejects_one_byte_over(self):
        exact = self.client.post(
            "/monthly/upload-session/start",
            json={
                "project_no": PROJECT_NO,
                "project_title": PROJECT_TITLE,
                "date_from": "2026-07-01",
                "date_to": "2026-07-02",
                "report_type": "monthly",
                "report_mode": "mtd",
                "files": [{
                    "file_id": "d" * 32,
                    "filename": "exact-limit.pdf",
                    "size_bytes": DEFAULT_LIMITS.max_bytes,
                }],
            },
        )
        self.assertEqual(exact.status_code, 200, exact.get_data(as_text=True))

        over = self.client.post(
            "/monthly/upload-session/start",
            json={
                "project_no": PROJECT_NO,
                "project_title": PROJECT_TITLE,
                "date_from": "2026-07-01",
                "date_to": "2026-07-02",
                "report_type": "monthly",
                "report_mode": "mtd",
                "files": [{
                    "file_id": "e" * 32,
                    "filename": "over-limit.pdf",
                    "size_bytes": DEFAULT_LIMITS.max_bytes + 1,
                }],
            },
        )
        self.assertEqual(over.status_code, 413)
        self.assertIn("per-file limit", over.get_json()["error"])

    def test_staged_upload_retry_returns_cached_item_without_reparsing(self):
        file_id = "c" * 32
        upload_session_id = self._start_staged_upload([file_id])
        imported = self._imported_pdf("2026-07-01", "3" * 64)

        with patch("monthly_report.web.import_daily_report_pdf", return_value=imported) as importer:
            first = self._upload_staged_file(upload_session_id, file_id)
            retry = self._upload_staged_file(upload_session_id, file_id)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.get_json()["cached"])
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.get_json()["cached"])
        self.assertEqual(importer.call_count, 1)

    def test_staged_upload_start_is_idempotent_with_client_session_id(self):
        upload_session_id = "6" * 32
        payload = {
            "upload_session_id": upload_session_id,
            "project_no": PROJECT_NO,
            "project_title": PROJECT_TITLE,
            "date_from": "2026-07-01",
            "date_to": "2026-07-02",
            "report_type": "monthly",
            "report_mode": "mtd",
            "files": [{
                "file_id": "5" * 32,
                "filename": "daily.pdf",
                "size_bytes": 1024,
            }],
        }

        first = self.client.post("/monthly/upload-session/start", json=payload)
        retry = self.client.post("/monthly/upload-session/start", json=payload)

        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertFalse(first.get_json()["cached"])
        self.assertEqual(retry.status_code, 200, retry.get_data(as_text=True))
        self.assertTrue(retry.get_json()["cached"])
        self.assertEqual(retry.get_json()["upload_session_id"], upload_session_id)
        session_root = self.data_dir / "monthly_reports" / "reza" / "upload_sessions"
        self.assertEqual([path.name for path in session_root.iterdir()], [upload_session_id])

    def test_staged_file_request_has_its_own_size_cap(self):
        file_id = "4" * 32
        upload_session_id = self._start_staged_upload([file_id])

        with patch("monthly_report.web._MAX_STAGED_REQUEST_BYTES", 10):
            response = self._upload_staged_file(upload_session_id, file_id)

        self.assertEqual(response.status_code, 413)
        self.assertIn("per-file limit", response.get_json()["error"])

    def test_staged_upload_failure_does_not_discard_a_later_valid_file(self):
        file_ids = ["d" * 32, "e" * 32]
        upload_session_id = self._start_staged_upload(file_ids)
        valid = self._imported_pdf("2026-07-02", "4" * 64)

        with patch(
            "monthly_report.web.import_daily_report_pdf",
            side_effect=[PDFImportError("encrypted PDF"), valid],
        ):
            rejected = self._upload_staged_file(upload_session_id, file_ids[0], "encrypted.pdf")
            accepted = self._upload_staged_file(upload_session_id, file_ids[1], "valid.pdf")

        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(rejected.get_json()["item"]["included"])
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.get_json()["item"]["included"])

        compiled = self.client.post(f"/monthly/upload-session/{upload_session_id}/compile")
        self.assertEqual(compiled.status_code, 200, compiled.get_data(as_text=True))
        body = compiled.get_json()
        self.assertEqual(body["draft"]["coverage"]["included_count"], 1)
        self.assertTrue(any("encrypted PDF" in warning for warning in body["draft"]["warnings"]))

    def test_staged_finalize_waits_for_every_planned_file(self):
        file_ids = ["f" * 32, "1" * 32]
        upload_session_id = self._start_staged_upload(file_ids)
        imported = self._imported_pdf("2026-07-01", "5" * 64)
        with patch("monthly_report.web.import_daily_report_pdf", return_value=imported):
            uploaded = self._upload_staged_file(upload_session_id, file_ids[0])
        self.assertEqual(uploaded.status_code, 200)

        compiled = self.client.post(f"/monthly/upload-session/{upload_session_id}/compile")
        self.assertEqual(compiled.status_code, 409)
        self.assertIn(file_ids[1], compiled.get_json()["pending_file_ids"])

    def test_staged_upload_session_is_scoped_to_logged_in_user(self):
        file_id = "9" * 32
        upload_session_id = self._start_staged_upload([file_id])
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = "other-user"

        response = self._upload_staged_file(upload_session_id, file_id)

        self.assertEqual(response.status_code, 404)

    def test_stale_upload_cleanup_never_touches_existing_reports(self):
        upload_session_id = self._start_staged_upload(["8" * 32])
        user_root = self.data_dir / "monthly_reports" / "reza"
        stale_dir = user_root / "upload_sessions" / upload_session_id
        report_path = user_root / "reports" / "existing-report.pdf"
        report_path.write_bytes(b"%PDF-existing-report")
        os.utime(stale_dir, (1, 1))

        self._start_staged_upload(["7" * 32])

        self.assertFalse(stale_dir.exists())
        self.assertEqual(report_path.read_bytes(), b"%PDF-existing-report")

    def test_delete_waits_for_active_operation_and_recovers_stale_lock(self):
        upload_session_id = self._start_staged_upload(["3" * 32])
        session_dir = (
            self.data_dir
            / "monthly_reports"
            / "reza"
            / "upload_sessions"
            / upload_session_id
        )
        lock_dir = session_dir / ".operation.lock"
        lock_dir.mkdir()

        busy = self.client.delete(f"/monthly/upload-session/{upload_session_id}")
        self.assertEqual(busy.status_code, 409)
        self.assertTrue(session_dir.is_dir())

        os.utime(lock_dir, (1, 1))
        deleted = self.client.delete(f"/monthly/upload-session/{upload_session_id}")
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        self.assertFalse(session_dir.exists())


if __name__ == "__main__":
    unittest.main()
