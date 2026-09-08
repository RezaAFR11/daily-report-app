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


def _current_photo_card_pdf(*, include_area=True, unknown_area=False) -> bytes:
    """Create the coordinate pattern used by current GPA photo cards."""

    output = io.BytesIO()
    document = canvas.Canvas(output)
    image_top = 597
    image_y = 450
    image_height = image_top - image_y
    x_values = (50, 216, 383)
    colours = ("#2563eb", "#f59e0b", "#16a34a")
    document.drawString(50, 700, "10. PHOTO DOCUMENTATION")
    captions = (
        ("PI Checking Unit 2",),
        ("PI Checking Unit 2",),
        ("Tightening the temporary blind flange bolts", "on TG#2"),
    )
    for x, colour, caption_lines in zip(x_values, colours, captions):
        if include_area:
            heading = (
                "Cold Commissioning Activities - Day 12 -"
                if unknown_area
                else "Turbine & Generators Unit 1 & 2 - DAY 10"
            )
            continuation = "Turbines & Generators" if unknown_area else "COLD COMMISSIONING"
            document.drawString(x, image_top + 37, heading)
            document.drawString(x, image_top - 53, continuation)
        document.drawString(x, image_top + 9, caption_lines[0])
        for index, line in enumerate(caption_lines[1:], start=1):
            document.drawString(x, image_top - 54 - ((index - 1) * 58), line)
        document.drawImage(
            ImageReader(io.BytesIO(_jpeg(colour))),
            x,
            image_y,
            155,
            image_height,
        )
    document.save()
    return output.getvalue()


def _caption_only_photo_card_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    image_top = 681
    image_y = 534
    document.drawString(50, 720, "10. PHOTO DOCUMENTATION")
    document.drawString(53, image_top + 9, "Woodward Training")
    document.drawImage(
        ImageReader(io.BytesIO(_jpeg("#7c3aed"))),
        50,
        image_y,
        155,
        image_top - image_y,
    )
    document.drawString(219, image_top + 13, "Seal Repair and Replacement on Hydraulic")
    document.drawString(219, image_top - 54, "Piston")
    document.drawImage(
        ImageReader(io.BytesIO(_jpeg("#dc2626"))),
        216,
        image_y,
        155,
        image_top - image_y,
    )
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

    def test_current_photo_cards_keep_duplicate_and_wrapped_captions(self):
        area = "Turbine & Generators Unit 1 & 2 - DAY 10 COLD COMMISSIONING"

        photos, _ = extract_pdf_photo_candidates(
            _current_photo_card_pdf(),
            filename="daily.pdf",
            areas=[{"id": area}],
        )

        self.assertEqual(len(photos), 3)
        self.assertEqual(
            [photo.get("caption") for photo in photos],
            [
                "PI Checking Unit 2",
                "PI Checking Unit 2",
                "Tightening the temporary blind flange bolts on TG#2",
            ],
        )
        self.assertEqual({photo.get("source_area") for photo in photos}, {area})
        self.assertTrue(all(
            photo.get("photo_match_method") == "photo_card_geometry"
            and photo.get("caption_match_confidence") == "high"
            and not photo.get("caption_review_required")
            for photo in photos
        ))

    def test_photo_card_heading_is_recovered_when_active_area_is_blank(self):
        photos, _ = extract_pdf_photo_candidates(
            _current_photo_card_pdf(unknown_area=True),
            filename="daily.pdf",
            areas=[{"id": ""}],
        )

        self.assertEqual(len(photos), 3)
        self.assertEqual(
            {photo.get("source_area") for photo in photos},
            {"Cold Commissioning Activities - Day 12 - Turbines & Generators"},
        )
        self.assertEqual(photos[2]["caption"], "Tightening the temporary blind flange bolts on TG#2")

    def test_caption_only_cards_do_not_borrow_one_caption_for_every_photo(self):
        photos, _ = extract_pdf_photo_candidates(
            _caption_only_photo_card_pdf(),
            filename="daily.pdf",
            areas=[{"id": ""}],
        )

        self.assertEqual(len(photos), 2)
        self.assertEqual(
            [photo.get("caption") for photo in photos],
            ["Woodward Training", "Seal Repair and Replacement on Hydraulic Piston"],
        )
        self.assertTrue(all(photo.get("caption_match_confidence") == "high" for photo in photos))

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
        self.assertIn("Appendix 1.1 - Photo Documentation", text)
        self.assertIn("Switchgear inspection", text)
        self.assertEqual(len(reader.pages), 3)
        appendix_page = reader.pages[-1].extract_text() or ""
        self.assertIn("1. Appendices", appendix_page)
        self.assertIn("Appendix 1.1 - Photo Documentation", appendix_page)

    def test_repeated_source_caption_is_visible_below_each_distinct_photo(self):
        first = _jpeg("#2563eb")
        second = _jpeg("#16a34a")
        first_id = hashlib.sha256(first).hexdigest()
        second_id = hashlib.sha256(second).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, f"{first_id}.jpg").write_bytes(first)
            Path(temporary, f"{second_id}.jpg").write_bytes(second)
            result = render_monthly_report(
                {
                    "photo_documentation": [
                        {
                            "asset_id": first_id,
                            "caption": "PI Checking Unit 2",
                            "source_area": "Turbine Unit 2",
                            "source_date": "2026-08-09",
                        },
                        {
                            "asset_id": second_id,
                            "caption": "PI Checking Unit 2",
                            "source_area": "Turbine Unit 2",
                            "source_date": "2026-08-09",
                        },
                    ]
                },
                photo_base_dir=temporary,
            )

        reader = PdfReader(io.BytesIO(result.getvalue()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertEqual(text.count("PI Checking Unit 2"), 2)


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
                "source_area": "Cold Commissioning - Turbines & Generators",
                "data": "data:image/jpeg;base64,not-accepted",
            }]},
        )
        self.assertEqual(valid.status_code, 200)
        stored = valid.get_json()["photos"][0]
        self.assertNotIn("data", stored)
        self.assertEqual(stored["caption"], "<script>alert(1)</script>")
        self.assertEqual(stored["source_area"], "Cold Commissioning - Turbines & Generators")
        self.assertEqual(stored["caption_match_confidence"], "reviewed")
        self.assertFalse(stored["caption_review_required"])

        # Older clients editing only captions must retain the reviewed area.
        repeated = self.client.patch(
            f"/monthly/photos/{self.draft_id}",
            json={"photos": [{"asset_id": self.asset_id, "caption": "Updated caption"}]},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.get_json()["photos"][0]["source_area"], stored["source_area"])


if __name__ == "__main__":
    unittest.main()
