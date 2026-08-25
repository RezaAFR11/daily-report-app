import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from PIL import Image
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from monthly_report.photos import (
    extract_pdf_photo_candidates,
    store_photo_candidates,
)
from monthly_report.renderer import render_monthly_report
from monthly_report.web import (
    _bound_record_photo_candidates,
    _draft_photo_dir,
    _photo_references_for_records,
    _save_draft,
    register_monthly_routes,
)


def _jpeg(colour, size=(640, 480), quality=90) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, format="JPEG", quality=quality)
    return output.getvalue()


def _photo_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    logo = ImageReader(io.BytesIO(_jpeg("#14532d", (360, 140))))
    photo = ImageReader(io.BytesIO(_jpeg("#bf3f32")))
    duplicate_photo = ImageReader(io.BytesIO(_jpeg("#bf3f32", quality=82)))

    document.drawString(50, 800, "1. REPORT INFORMATION")
    document.drawImage(logo, 50, 730, 180, 70)
    document.showPage()

    document.drawString(50, 800, "9. PHOTO DOCUMENTATION")
    document.drawImage(logo, 50, 730, 180, 70)
    document.drawImage(photo, 50, 350, 320, 240)
    # The same photograph twice must become one review item.
    document.drawImage(duplicate_photo, 380, 350, 160, 120)
    document.showPage()

    document.drawString(50, 800, "SIGN-OFF")
    document.drawImage(logo, 50, 730, 180, 70)
    document.save()
    return output.getvalue()


class PeriodicPhotoExtractionTests(unittest.TestCase):
    def test_photo_page_is_used_and_repeated_header_logo_is_removed(self):
        photos, warnings = extract_pdf_photo_candidates(
            _photo_pdf(),
            filename="daily.pdf",
        )

        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0]["page"], 2)
        self.assertEqual(
            hashlib.sha256(photos[0]["content"]).hexdigest(),
            photos[0]["asset_id"],
        )
        self.assertTrue(any("header/logo" in warning for warning in warnings))

    def test_cross_report_exact_reuse_keeps_each_source_date_reference(self):
        asset_id = "a" * 64
        records = [
            {
                "report_id": "day-1",
                "report_date": "2026-08-17",
                "_photo_candidates": [{"asset_id": asset_id, "size_bytes": 100}],
            },
            {
                "report_id": "day-2",
                "report_date": "2026-08-18",
                "_photo_candidates": [{"asset_id": asset_id, "size_bytes": 100}],
            },
        ]

        warnings = _bound_record_photo_candidates(records)

        self.assertEqual(len(records[0]["_photo_candidates"]), 1)
        # The later source keeps its reference so selecting that source during
        # validation still works; the final photo list deduplicates by hash.
        self.assertEqual(len(records[1]["_photo_candidates"]), 1)
        references = _photo_references_for_records(records)
        self.assertEqual(len(references), 2)
        self.assertEqual(
            [item["source_date"] for item in references],
            ["2026-08-17", "2026-08-18"],
        )
        self.assertTrue(any("reuse" in warning for warning in warnings))

    def test_content_addressed_storage_keeps_binary_out_of_json_metadata(self):
        photos, _ = extract_pdf_photo_candidates(_photo_pdf())
        with tempfile.TemporaryDirectory() as temporary:
            references = store_photo_candidates(
                photos,
                temporary,
                source_report_id="daily-1",
            )

            self.assertEqual(len(references), 1)
            self.assertNotIn("content", references[0])
            self.assertNotIn("data", references[0])
            target = Path(temporary) / f"{references[0]['asset_id']}.jpg"
            self.assertTrue(target.is_file())
            self.assertEqual(
                hashlib.sha256(target.read_bytes()).hexdigest(),
                references[0]["asset_id"],
            )

    def test_asset_store_enforces_whole_draft_byte_limit(self):
        photos, _ = extract_pdf_photo_candidates(_photo_pdf())
        self.assertTrue(photos)
        with tempfile.TemporaryDirectory() as temporary:
            references = store_photo_candidates(
                photos,
                temporary,
                source_report_id="daily-1",
                max_total_bytes=len(photos[0]["content"]) - 1,
            )
        self.assertEqual(references, [])


class PeriodicPhotoRendererTests(unittest.TestCase):
    def test_reviewed_photo_is_rendered_in_dynamic_appendix(self):
        photo = _jpeg("#2563eb")
        asset_id = hashlib.sha256(photo).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, f"{asset_id}.jpg").write_bytes(photo)
            result = render_monthly_report(
                {
                    "photo_documentation": [{
                        "asset_id": asset_id,
                        "caption": "Switchgear inspection",
                        "source": "daily.pdf",
                        "page": 4,
                    }]
                },
                photo_base_dir=temporary,
            )

        reader = PdfReader(io.BytesIO(result.getvalue()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Appendix 6.1 - Photo Documentation", text)
        self.assertIn("Switchgear inspection", text)
        self.assertGreaterEqual(len(reader.pages), 8)


class PeriodicPhotoRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="photo-route-test")
        register_monthly_routes(
            self.app,
            data_dir=str(self.data_dir),
            config_provider=lambda: {"projects": []},
        )
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = "reza"

        photo = _jpeg("#f59e0b")
        self.asset_id = hashlib.sha256(photo).hexdigest()
        self.draft_id = "a" * 32
        assets = _draft_photo_dir(
            self.data_dir,
            "reza",
            self.draft_id,
        )
        Path(assets, f"{self.asset_id}.jpg").write_bytes(photo)
        _save_draft(
            self.data_dir,
            "reza",
            {
                "photo_documentation": [{
                    "schema_version": "periodic-photo/1",
                    "asset_id": self.asset_id,
                    "source_report_id": "daily-1",
                    "source": "daily.pdf",
                    "page": 2,
                    "width": 640,
                    "height": 480,
                    "size_bytes": len(photo),
                    "caption": "",
                    "order": 0,
                }],
            },
            draft_id=self.draft_id,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_owner_can_fetch_selected_asset_but_unknown_asset_is_hidden(self):
        response = self.client.get(
            f"/monthly/photos/{self.draft_id}/{self.asset_id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        response.close()

        response = self.client.get(
            f"/monthly/photos/{self.draft_id}/{'b' * 64}"
        )
        self.assertEqual(response.status_code, 404)

    def test_review_accepts_only_existing_references_and_never_client_data(self):
        unknown = self.client.patch(
            f"/monthly/photos/{self.draft_id}",
            json={"photos": [{"asset_id": "b" * 64, "caption": "bad"}]},
        )
        self.assertEqual(unknown.status_code, 400)

        valid = self.client.patch(
            f"/monthly/photos/{self.draft_id}",
            json={"photos": [{
                "asset_id": self.asset_id,
                "caption": "<script>alert(1)</script>",
                "data": "data:image/jpeg;base64,not-accepted",
            }]},
        )
        self.assertEqual(valid.status_code, 200)
        stored = valid.get_json()["photos"][0]
        self.assertNotIn("data", stored)
        self.assertEqual(stored["caption"], "<script>alert(1)</script>")


if __name__ == "__main__":
    unittest.main()
