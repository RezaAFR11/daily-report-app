import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from monthly_report.photos import (
    DEFAULT_PHOTO_LIMITS,
    attach_canonical_photo_candidates,
)
from monthly_report.storage import list_canonical_records


def _jpeg(colour: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 480), colour).save(output, format="JPEG", quality=88)
    return output.getvalue()


def _asset_metadata(content: bytes, filename: str) -> dict:
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "asset_path": f"assets/{filename}",
    }


def _record(
    *,
    username: str = "reza",
    report_id: str = "daily-20260811-r1",
    asset: dict | None = None,
    filename: str = "field-photo.jpg",
    desc: str = "Control valve inspection",
) -> dict:
    photo = {"photo_filename": filename, "desc": desc}
    if asset is not None:
        photo["asset"] = asset
    return {
        "report_id": report_id,
        "username": username,
        "_canonical_owner": username,
        "date": "2026-08-11",
        "payload": {
            "date": "2026-08-11",
            "areas": [{"id": "Valve Area", "photos": [photo]}],
        },
    }


def _write_canonical_asset(
    data_dir: str,
    username: str,
    filename: str,
    content: bytes,
) -> Path:
    target = (
        Path(data_dir)
        / "users"
        / username
        / "reports"
        / "canonical"
        / "assets"
        / filename
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


class StoredJsonPhotoAdapterTests(unittest.TestCase):
    def test_valid_owner_asset_is_normalized_stored_and_described(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            raw = _jpeg("#0f766e")
            filename = f"{hashlib.sha256(raw).hexdigest()}.jpg"
            _write_canonical_asset(data_dir, "reza", filename, raw)
            record = _record(asset=_asset_metadata(raw, filename))

            warnings = attach_canonical_photo_candidates(
                [record], data_dir, target_dir
            )

            self.assertEqual(warnings, [])
            self.assertEqual(len(record["_photo_candidates"]), 1)
            reference = record["_photo_candidates"][0]
            self.assertEqual(reference["caption"], "Control valve inspection")
            self.assertEqual(
                reference["source"], "Stored JSON - 2026-08-11 - Valve Area"
            )
            self.assertEqual(reference["source_date"], "2026-08-11")
            self.assertEqual(reference["source_area"], "Valve Area")
            self.assertNotIn("page", reference)
            self.assertNotIn("content", reference)
            json.dumps(record)
            stored = Path(target_dir, f"{reference['asset_id']}.jpg")
            self.assertTrue(stored.is_file())
            self.assertEqual(
                hashlib.sha256(stored.read_bytes()).hexdigest(),
                reference["asset_id"],
            )

    def test_cross_owner_path_is_rejected_even_when_target_exists(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            raw = _jpeg("#1d4ed8")
            filename = f"{hashlib.sha256(raw).hexdigest()}.jpg"
            _write_canonical_asset(data_dir, "other-user", filename, raw)
            asset = _asset_metadata(raw, filename)
            asset["asset_path"] = (
                f"../../../other-user/reports/canonical/assets/{filename}"
            )
            record = _record(username="reza", asset=asset)

            warnings = attach_canonical_photo_candidates(
                [record], data_dir, target_dir
            )

            self.assertEqual(record["_photo_candidates"], [])
            self.assertTrue(any("escapes its owner" in warning for warning in warnings))
            self.assertEqual(list(Path(target_dir).glob("*.jpg")), [])

    def test_forged_json_username_cannot_select_another_users_asset(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            raw = _jpeg("#1d4ed8")
            filename = f"{hashlib.sha256(raw).hexdigest()}.jpg"
            _write_canonical_asset(data_dir, "other-user", filename, raw)
            record = _record(username="other-user", asset=_asset_metadata(raw, filename))
            record["_canonical_owner"] = "reza"

            warnings = attach_canonical_photo_candidates([record], data_dir, target_dir)

            self.assertEqual(record["_photo_candidates"], [])
            self.assertTrue(any("file is missing" in warning for warning in warnings))

    def test_storage_loader_binds_owner_to_traversed_user_directory(self):
        with tempfile.TemporaryDirectory() as data_dir:
            canonical = (
                Path(data_dir) / "users" / "reza" / "reports" / "canonical"
            )
            canonical.mkdir(parents=True)
            record = _record(username="forged-user", asset=None)
            record.update({
                "record_type": "final_daily_report",
                "project_no": "P-001",
                "project_title": "Valve Project",
            })
            record["payload"].update({
                "project_no": "P-001",
                "project_title": "Valve Project",
            })
            (canonical / "record.json").write_text(
                json.dumps(record),
                encoding="utf-8",
            )

            loaded = list_canonical_records(data_dir, username="reza")

            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["_canonical_owner"], "reza")
            self.assertEqual(loaded[0]["username"], "reza")

    def test_hash_size_missing_and_legacy_references_generate_warnings(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            raw = _jpeg("#be123c")
            filename = "stored.jpg"
            path = _write_canonical_asset(data_dir, "reza", filename, raw)
            bad_hash = _asset_metadata(raw, filename)
            bad_hash["sha256"] = "0" * 64
            wrong_size = _asset_metadata(raw, filename)
            wrong_size["size_bytes"] += 1
            missing = _asset_metadata(raw, "missing.jpg")
            records = [
                _record(report_id="bad-hash", asset=bad_hash),
                _record(report_id="wrong-size", asset=wrong_size),
                _record(report_id="missing", asset=missing),
                _record(report_id="legacy", asset=None),
            ]
            self.assertTrue(path.is_file())

            warnings = attach_canonical_photo_candidates(
                records, data_dir, target_dir
            )

            self.assertTrue(all(not record["_photo_candidates"] for record in records))
            warning_text = "\n".join(warnings)
            self.assertIn("hash does not match", warning_text)
            self.assertIn("size does not match", warning_text)
            self.assertIn("file is missing", warning_text)
            self.assertIn("legacy filename-only", warning_text)

    def test_per_record_and_whole_draft_limits_are_enforced(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            first = _jpeg("#ca8a04")
            second = _jpeg("#7c3aed")
            first_name = "first.jpg"
            second_name = "second.jpg"
            _write_canonical_asset(data_dir, "reza", first_name, first)
            _write_canonical_asset(data_dir, "reza", second_name, second)
            records = [
                _record(
                    report_id="first",
                    asset=_asset_metadata(first, first_name),
                ),
                _record(
                    report_id="second",
                    asset=_asset_metadata(second, second_name),
                ),
            ]
            limits = replace(DEFAULT_PHOTO_LIMITS, max_images_per_draft=1)

            warnings = attach_canonical_photo_candidates(
                records, data_dir, target_dir, limits=limits
            )

            self.assertEqual(len(records[0]["_photo_candidates"]), 1)
            self.assertEqual(records[1]["_photo_candidates"], [])
            self.assertTrue(any("draft photo count or byte limit" in warning for warning in warnings))
            self.assertEqual(len(list(Path(target_dir).glob("*.jpg"))), 1)

    def test_per_daily_report_byte_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            raw = _jpeg("#c2410c")
            filename = "large-for-test.jpg"
            _write_canonical_asset(data_dir, "reza", filename, raw)
            record = _record(asset=_asset_metadata(raw, filename))
            limits = replace(
                DEFAULT_PHOTO_LIMITS,
                max_total_asset_bytes_per_pdf=1,
            )

            warnings = attach_canonical_photo_candidates(
                [record], data_dir, target_dir, limits=limits
            )

            self.assertEqual(record["_photo_candidates"], [])
            self.assertTrue(any("per-Daily-Report" in warning for warning in warnings))

    def test_invalid_entry_scanning_is_bounded(self):
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as target_dir:
            record = _record(asset=None)
            record["payload"]["areas"][0]["photos"] = [
                {"photo_filename": f"missing-{index}.jpg", "desc": ""}
                for index in range(10)
            ]
            limits = replace(DEFAULT_PHOTO_LIMITS, max_images_per_pdf=1)

            warnings = attach_canonical_photo_candidates(
                [record], data_dir, target_dir, limits=limits
            )

            self.assertEqual(record["_photo_candidates"], [])
            self.assertEqual(
                sum("legacy filename-only" in warning for warning in warnings),
                4,
            )
            self.assertTrue(any("first 4 photo entries" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
