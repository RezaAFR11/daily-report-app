import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from daily_report_app import app
from google_drive_integration import (
    GoogleDriveConfig,
    GoogleDriveError,
    GoogleDriveNotConfigured,
    GoogleDrivePermissionError,
    GoogleDriveReauthorizationRequired,
    GoogleDriveUploader,
    GoogleDriveUploadError,
    ProjectCategoryError,
    build_drive_folder_path,
    resolve_project_category,
)


PDF_BYTES = b"%PDF-1.4\nGoogle Drive route test"


class GoogleDriveMappingTests(unittest.TestCase):
    def test_maps_three_current_projects_and_legacy_electrical_number(self):
        self.assertEqual(
            resolve_project_category(
                "Electrical Construction and Installation - Manpower Supply",
                "002/KN-GPA/EPC-2K-P2/XI/2025",
            ),
            "electrical",
        )
        self.assertEqual(
            resolve_project_category(
                "Repair & Services Control Valve & ON OFF Valve",
                "P01.0825.J075",
            ),
            "control_valve",
        )
        self.assertEqual(
            resolve_project_category(
                "REACTIVATION FOR TURBINES AND GENERATORS",
                "001/KN-GPA/EPC-2F-P2/IV/2025",
            ),
            "turbine_generator",
        )
        self.assertEqual(
            resolve_project_category("Legacy title", "PC-26-0004-KN-GPA-029-DAR"),
            "electrical",
        )

    def test_unknown_titles_use_other_projects_and_ambiguous_titles_are_not_guessed(self):
        self.assertEqual(
            resolve_project_category("Civil foundation", "UNKNOWN"),
            "other_projects",
        )
        self.assertEqual(
            resolve_project_category("Plant Reactivation", "UNKNOWN"),
            "other_projects",
        )
        with self.assertRaises(ProjectCategoryError):
            resolve_project_category("Electrical Turbine and Generator", "UNKNOWN")
        with self.assertRaises(ProjectCategoryError):
            resolve_project_category("", "UNKNOWN")
        with self.assertRaises(ProjectCategoryError):
            resolve_project_category("", "PC-26-0004-KN-GPA-029-DAR")

    def test_title_keywords_work_without_known_number_and_conflicts_need_review(self):
        self.assertEqual(
            resolve_project_category("Electrical Installation", "UNKNOWN"),
            "electrical",
        )
        self.assertEqual(
            resolve_project_category("Control Valve Repair", "UNKNOWN"),
            "control_valve",
        )
        self.assertEqual(
            resolve_project_category("Turbine Maintenance", "UNKNOWN"),
            "turbine_generator",
        )
        self.assertEqual(
            resolve_project_category("Generator Repair", "UNKNOWN"),
            "turbine_generator",
        )
        with self.assertRaises(ProjectCategoryError):
            resolve_project_category(
                "Control Valve Repair",
                "PC-26-0004-KN-GPA-029-DAR",
            )

    def test_folder_path_uses_year_then_indonesian_month_name(self):
        category, path = build_drive_folder_path(
            project_title="Electrical Installation & Construction",
            project_no="PC-26-0004-KN-GPA-029-DAR",
            report_date="2026-08-06",
        )
        self.assertEqual(category, "electrical")
        self.assertEqual(
            path,
            ["Daily Reports", "Daily Reports Electrical", "2026", "Agustus"],
        )

    def test_other_project_folder_uses_same_year_month_structure(self):
        category, path = build_drive_folder_path(
            project_title="Civil Foundation Improvement",
            project_no="CIVIL-001",
            report_date="2026-08-11",
        )
        self.assertEqual(category, "other_projects")
        self.assertEqual(
            path,
            ["Daily Reports", "Daily Reports Other Projects", "2026", "Agustus"],
        )

    def test_upload_creates_folder_tree_and_pdf_with_stable_report_key(self):
        service = MagicMock()
        files = service.files.return_value
        files.list.return_value.execute.side_effect = [
            {"files": []},
            {"files": []},
            {"files": []},
            {"files": []},
            {"files": []},
        ]
        files.create.return_value.execute.side_effect = [
            {"id": "folder-root", "name": "Daily Reports"},
            {"id": "folder-project", "name": "Daily Reports Electrical"},
            {"id": "folder-year", "name": "2026"},
            {"id": "folder-month", "name": "Agustus"},
            {
                "id": "pdf-file",
                "name": "Daily Report.pdf",
                "webViewLink": "https://drive.google.com/file/d/pdf-file/view",
                "md5Checksum": hashlib.md5(PDF_BYTES, usedforsecurity=False).hexdigest(),
            },
        ]
        uploader = GoogleDriveUploader(
            GoogleDriveConfig("client", "secret", "refresh"),
            service=service,
            media_factory=lambda *args, **kwargs: object(),
        )

        result = uploader.upload_pdf(
            PDF_BYTES,
            filename="Daily Report.pdf",
            project_title="Electrical Installation & Construction",
            project_no="PC-26-0004-KN-GPA-029-DAR",
            report_date="2026-08-06",
        )

        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(result["file_id"], "pdf-file")
        self.assertEqual(files.create.call_count, 5)
        pdf_body = files.create.call_args_list[-1].kwargs["body"]
        self.assertEqual(pdf_body["name"], "Daily Report.pdf")
        self.assertEqual(pdf_body["parents"], ["folder-month"])
        self.assertEqual(pdf_body["appProperties"]["gpaReportKey"], result["report_key"])

    def test_identical_retry_reuses_existing_drive_file(self):
        checksum = hashlib.md5(PDF_BYTES, usedforsecurity=False).hexdigest()
        service = MagicMock()
        files = service.files.return_value
        files.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-root"}]},
            {"files": [{"id": "folder-project"}]},
            {"files": [{"id": "folder-month"}]},
            {"files": [{"id": "folder-year"}]},
            {"files": [{
                "id": "existing-pdf",
                "md5Checksum": checksum,
                "webViewLink": "https://drive.google.com/file/d/existing-pdf/view",
            }]},
        ]
        uploader = GoogleDriveUploader(
            GoogleDriveConfig("client", "secret", "refresh"),
            service=service,
            media_factory=lambda *args, **kwargs: object(),
        )

        result = uploader.upload_pdf(
            PDF_BYTES,
            filename="Daily Report.pdf",
            project_title="Repair & Services Control Valve",
            project_no="P01.0825.J075",
            report_date="2026-08-06",
        )

        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["file_id"], "existing-pdf")
        files.update.assert_not_called()
        files.create.assert_not_called()

    def test_changed_pdf_updates_existing_drive_file(self):
        service = MagicMock()
        files = service.files.return_value
        files.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-root"}]},
            {"files": [{"id": "folder-project"}]},
            {"files": [{"id": "folder-month"}]},
            {"files": [{"id": "folder-year"}]},
            {"files": [{
                "id": "existing-pdf",
                "md5Checksum": "outdated-checksum",
                "webViewLink": "https://drive.google.com/file/d/existing-pdf/view",
            }]},
        ]
        files.update.return_value.execute.return_value = {
            "id": "existing-pdf",
            "md5Checksum": hashlib.md5(PDF_BYTES, usedforsecurity=False).hexdigest(),
            "webViewLink": "https://drive.google.com/file/d/existing-pdf/view",
        }
        uploader = GoogleDriveUploader(
            GoogleDriveConfig("client", "secret", "refresh"),
            service=service,
            media_factory=lambda *args, **kwargs: object(),
        )

        result = uploader.upload_pdf(
            PDF_BYTES,
            filename="Daily Report.pdf",
            project_title="Electrical Installation",
            project_no="002/KN-GPA/EPC-2K-P2/XI/2025",
            report_date="2026-08-06",
        )

        self.assertEqual(result["status"], "updated")
        files.update.assert_called_once()
        files.create.assert_not_called()


class GoogleDriveRouteTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, SECRET_KEY="drive-route-test")
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = "reza"
            flask_session["is_admin"] = False

    def test_upload_route_uses_only_owned_archived_pdf_and_saves_drive_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_dir = os.path.join(temporary, "reports")
            os.makedirs(reports_dir)
            filename = "Daily Report - PT GPA - KN - 2026-08-06 (Day 1).pdf"
            with open(os.path.join(reports_dir, filename), "wb") as handle:
                handle.write(PDF_BYTES)
            with open(os.path.join(reports_dir, "index.json"), "w", encoding="utf-8") as handle:
                json.dump([{
                    "filename": filename,
                    "date": "2026-08-06",
                    "day_no": "1",
                    "project_no": "PC-26-0004-KN-GPA-029-DAR",
                    "project_title": "Electrical Installation & Construction",
                    "canonical_report_id": "report-1",
                }], handle)

            uploaded = {
                "status": "uploaded",
                "file_id": "drive-file",
                "web_view_link": "https://drive.google.com/file/d/drive-file/view",
                "filename": filename,
                "category": "electrical",
                "folder_path": ["Daily Reports", "Daily Reports Electrical", "2026", "Agustus"],
                "folder_ids": ["1", "2", "3", "4"],
                "md5_checksum": "abc",
                "report_key": "stable-key",
            }
            with (
                patch("daily_report_app.get_reports_dir", return_value=reports_dir),
                patch("daily_report_app.upload_daily_report_pdf", return_value=uploaded) as drive_upload,
            ):
                response = self.client.post(
                    "/reports/drive-upload",
                    json={"filename": filename, "report_id": "report-1"},
                )

            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            drive_upload.assert_called_once()
            with open(os.path.join(reports_dir, "index.json"), encoding="utf-8") as handle:
                rows = json.load(handle)
            self.assertEqual(rows[0]["drive_file_id"], "drive-file")
            self.assertEqual(rows[0]["drive_status"], "uploaded")

    def test_upload_route_rejects_unowned_path(self):
        response = self.client.post(
            "/reports/drive-upload",
            json={"filename": "../secret.pdf"},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_route_rejects_non_object_json(self):
        response = self.client.post(
            "/reports/drive-upload",
            json=["not", "an", "object"],
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_route_preserves_drive_error_contracts(self):
        cases = [
            (
                GoogleDriveNotConfigured('Drive not configured'),
                503,
                'drive_not_configured',
                None,
            ),
            (
                ProjectCategoryError('Project needs review'),
                422,
                'project_needs_review',
                'needs_review',
            ),
            (
                GoogleDriveReauthorizationRequired('Reconnect Drive'),
                503,
                'drive_reauth_required',
                'reauth_required',
            ),
            (
                GoogleDrivePermissionError('Permission denied'),
                503,
                'drive_permission_denied',
                'permission_denied',
            ),
            (
                GoogleDriveUploadError('Temporary upload failure'),
                502,
                'drive_upload_failed',
                'failed',
            ),
            (
                GoogleDriveError('Invalid report metadata'),
                422,
                'invalid_drive_report',
                None,
            ),
            (
                RuntimeError('Unexpected failure'),
                500,
                'drive_upload_failed',
                'failed',
            ),
        ]

        for error, status_code, response_code, failure_status in cases:
            with self.subTest(error=type(error).__name__):
                with (
                    patch(
                        'daily_report_app._report_entry_for_drive',
                        return_value={'filename': 'Owned.pdf'},
                    ),
                    patch(
                        'daily_report_app.get_owned_report_path',
                        return_value=__file__,
                    ),
                    patch(
                        'daily_report_app._perform_report_drive_upload',
                        side_effect=error,
                    ),
                    patch(
                        'daily_report_app._record_drive_upload_failure',
                    ) as record_failure,
                    patch('daily_report_app.app.logger.warning'),
                    patch('daily_report_app.app.logger.exception'),
                ):
                    response = self.client.post(
                        '/reports/drive-upload',
                        json={'filename': 'Owned.pdf'},
                    )

                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.get_json()['code'], response_code)
                if failure_status is None:
                    record_failure.assert_not_called()
                else:
                    self.assertEqual(record_failure.call_args.args[4], failure_status)

    def test_archive_id_selects_matching_pdf_when_display_names_are_equal(self):
        with tempfile.TemporaryDirectory() as temporary:
            reports_dir = os.path.join(temporary, "reports")
            os.makedirs(reports_dir)
            filename = "Daily Report - PT GPA - KN - 2026-08-06 (Day 1).pdf"
            old_storage = "report-" + ("a" * 32) + ".pdf"
            new_storage = "report-" + ("b" * 32) + ".pdf"
            old_pdf = b"%PDF-1.4\nold electrical report"
            new_pdf = b"%PDF-1.4\nnew control report"
            with open(os.path.join(reports_dir, old_storage), "wb") as handle:
                handle.write(old_pdf)
            with open(os.path.join(reports_dir, new_storage), "wb") as handle:
                handle.write(new_pdf)
            rows = [
                {
                    "archive_id": "b" * 32,
                    "storage_filename": new_storage,
                    "filename": filename,
                    "date": "2026-08-06",
                    "project_no": "P01.0825.J075",
                    "project_title": "Control Valve Repair",
                    "canonical_report_id": "new-report",
                },
                {
                    "archive_id": "a" * 32,
                    "storage_filename": old_storage,
                    "filename": filename,
                    "date": "2026-08-06",
                    "project_no": "002/KN-GPA/EPC-2K-P2/XI/2025",
                    "project_title": "Electrical Installation",
                    "canonical_report_id": "old-report",
                },
            ]
            with open(os.path.join(reports_dir, "index.json"), "w", encoding="utf-8") as handle:
                json.dump(rows, handle)

            uploaded = {
                "status": "uploaded",
                "file_id": "old-drive-file",
                "web_view_link": "https://drive.google.com/file/d/old-drive-file/view",
                "filename": filename,
                "category": "electrical",
                "folder_path": ["Daily Reports", "Daily Reports Electrical", "2026", "Agustus"],
                "folder_ids": ["1", "2", "3", "4"],
                "md5_checksum": "abc",
                "report_key": "old-key",
            }
            with (
                patch("daily_report_app.get_reports_dir", return_value=reports_dir),
                patch("daily_report_app.upload_daily_report_pdf", return_value=uploaded) as upload,
            ):
                response = self.client.post(
                    "/reports/drive-upload",
                    json={
                        "filename": filename,
                        "archive_id": "a" * 32,
                        "report_id": "old-report",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(upload.call_args.args[0], old_pdf)


if __name__ == "__main__":
    unittest.main()
