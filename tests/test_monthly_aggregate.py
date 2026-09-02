import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from monthly_report import (
    aggregate_monthly_records,
    archive_final_daily_record,
    list_canonical_records,
    load_canonical_record,
)


def canonical_record(payload, *, report_id, revision=1, generated_at="2026-08-01T00:00:00Z"):
    return {
        "schema_version": 1,
        "record_type": "final_daily_report",
        "report_id": report_id,
        "username": "reporter",
        "date": payload["date"],
        "project_no": payload.get("project_no", "P-001"),
        "project_title": payload.get("project_title", "Reactivation"),
        "generated_at": generated_at,
        "revision": revision,
        "payload": payload,
        "assets": [],
    }


class CanonicalDailyStorageTests(unittest.TestCase):
    def test_archive_strips_inline_base64_and_hashes_referenced_photos(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as photo_dir:
            photo_bytes = b"not-a-real-jpeg-but-stable-test-bytes"
            photo_path = Path(photo_dir, "site-photo.jpg")
            photo_path.write_bytes(photo_bytes)
            payload = {
                "date": "2026-07-30",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "areas": [{
                    "id": "Turbine 2",
                    "photos": [{
                        "photo_filename": "site-photo.jpg",
                        "img_data": "data:image/jpeg;base64,QUJDRA==",
                        "desc": "Simulator preparation",
                    }],
                }],
                "sign_offs": [{"name": "Approver", "sig": "data:image/png;base64,QUJDRA=="}],
            }

            record = archive_final_daily_record(
                temp_dir,
                "nafis",
                payload,
                generated_at="2026-07-30T12:28:00Z",
                report_id="daily-20260730-r1",
                photo_base_dir=photo_dir,
            )

            serialized = json.dumps(record)
            self.assertNotIn("base64", serialized)
            photo = record["payload"]["areas"][0]["photos"][0]
            expected_hash = hashlib.sha256(photo_bytes).hexdigest()
            self.assertEqual(photo["asset"]["sha256"], expected_hash)
            self.assertEqual(record["assets"][0]["sha256"], expected_hash)
            asset_path = Path(
                temp_dir,
                "users",
                "nafis",
                "reports",
                "canonical",
                photo["asset"]["asset_path"],
            )
            self.assertEqual(asset_path.read_bytes(), photo_bytes)
            self.assertEqual(
                load_canonical_record(temp_dir, "nafis", "daily-20260730-r1"),
                record,
            )

            with self.assertRaises(FileExistsError):
                archive_final_daily_record(
                    temp_dir,
                    "nafis",
                    payload,
                    generated_at="2026-07-30T12:29:00Z",
                    report_id="daily-20260730-r1",
                    photo_base_dir=photo_dir,
                )

    def test_storage_path_safety_and_cross_user_listing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = archive_final_daily_record(
                temp_dir,
                "alpha",
                {
                    "date": "2026-07-01",
                    "project_no": "P-001",
                    "project_title": "Reactivation",
                    "areas": [],
                },
                generated_at="2026-07-01T10:00:00Z",
                report_id="alpha-july-1",
            )
            second = archive_final_daily_record(
                temp_dir,
                "beta",
                {
                    "date": "2026-07-02",
                    "project_no": "P-002",
                    "project_title": "Other Project",
                    "areas": [],
                },
                generated_at="2026-07-02T10:00:00Z",
                report_id="beta-july-2",
            )

            self.assertEqual(
                [record["report_id"] for record in list_canonical_records(temp_dir)],
                [first["report_id"], second["report_id"]],
            )
            self.assertEqual(
                [record["report_id"] for record in list_canonical_records(
                    temp_dir, project_no=" p-001 ", date_from="2026-07-01", date_to="2026-07-31"
                )],
                ["alpha-july-1"],
            )

            with self.assertRaises(ValueError):
                archive_final_daily_record(
                    temp_dir,
                    "../escape",
                    {"date": "2026-07-03", "areas": []},
                )
            with self.assertRaises(ValueError):
                archive_final_daily_record(
                    temp_dir,
                    "alpha",
                    {
                        "date": "2026-07-03",
                        "areas": [{"photos": [{"photo_filename": "../outside.jpg"}]}],
                    },
                )

    def test_archive_hashes_filename_based_signature_media(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as media_dir:
            signature_bytes = b'stable-signature-test-bytes'
            signature_path = Path(media_dir, 'prepared-signature.jpg')
            signature_path.write_bytes(signature_bytes)
            payload = {
                'date': '2026-07-31',
                'project_no': 'P-001',
                'project_title': 'Reactivation',
                'areas': [],
                'sign_offs': [{
                    'label': 'Prepared By',
                    'sig': '',
                    'sig_filename': signature_path.name,
                }],
            }

            record = archive_final_daily_record(
                temp_dir,
                'nafis',
                payload,
                photo_paths={signature_path.name: signature_path},
            )

        sign_off = record['payload']['sign_offs'][0]
        self.assertEqual(sign_off['sig_filename'], signature_path.name)
        self.assertEqual(
            sign_off['asset']['sha256'],
            hashlib.sha256(signature_bytes).hexdigest(),
        )

    def test_revisions_are_automatic_and_records_remain_immutable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = {
                "date": "2026-07-05",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "areas": [],
            }
            first = archive_final_daily_record(temp_dir, "alpha", payload)
            second = archive_final_daily_record(temp_dir, "alpha", payload)

            self.assertEqual(first["revision"], 1)
            self.assertEqual(second["revision"], 2)
            self.assertNotEqual(first["report_id"], second["report_id"])
            self.assertEqual(len(list_canonical_records(temp_dir, username="alpha")), 2)


class MonthlyAggregationTests(unittest.TestCase):
    def test_dedupes_latest_revision_and_reports_partial_coverage(self):
        day_one = canonical_record(
            {
                "date": "2026-07-01",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "areas": [{
                    "id": "Turbine 2",
                    "activities_today": ["Old activity"],
                    "activities_tomorrow": ["Do not use this earlier plan"],
                    "constraints": "Oil seepage",
                }],
            },
            report_id="day-1-r1",
        )
        stale_day_two = canonical_record(
            {
                "date": "2026-07-02",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "areas": [{"id": "Turbine 2", "activities_today": ["Stale text"]}],
            },
            report_id="day-2-r1",
            revision=1,
            generated_at="2026-07-02T08:00:00Z",
        )
        latest_day_two = canonical_record(
            {
                "date": "2026-07-02",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "areas": [{
                    "id": "Turbine 2",
                    "activities_today": ["Function test", " function   test "],
                    "activities_tomorrow": ["Stroking test"],
                }],
            },
            report_id="day-2-r2",
            revision=2,
            generated_at="2026-07-02T09:00:00Z",
        )

        result = aggregate_monthly_records(
            [day_one, stale_day_two, latest_day_two],
            project_no="P-001",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )

        self.assertEqual(result["coverage"]["covered_dates"], ["2026-07-01", "2026-07-02"])
        self.assertEqual(result["coverage"]["missing_dates"], ["2026-07-03"])
        self.assertTrue(result["coverage"]["is_partial"])
        self.assertEqual(result["coverage"]["duplicate_dates"], ["2026-07-02"])
        self.assertEqual(result["coverage"]["superseded_report_ids"], ["day-2-r1"])
        descriptions = [entry["description"] for entry in result["activities"]]
        self.assertEqual(descriptions, ["Old activity", "Function test"])
        tomorrow = result["tomorrow_activities"][0]
        self.assertEqual(tomorrow["source_date"], "2026-07-02")
        self.assertEqual(tomorrow["source_area"], "Turbine 2")
        self.assertEqual(tomorrow["reporting_area"], "Turbine 2")
        self.assertEqual(tomorrow["description"], "Stroking test")
        self.assertEqual(tomorrow["source_report_id"], "day-2-r2")
        self.assertEqual(result["constraints"][0]["text"], "Oil seepage")

    def test_daily_headcount_and_hours_are_unique_across_area_assignments(self):
        payload = {
            "date": "2026-07-30",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "indirect_manpower": [
                {"name": "Faiz", "role": "Project Control", "hours": "07:00 - 17:00"},
                {"name": "Shared Person", "role": "Coordinator", "hours": "07:00 - 17:00"},
            ],
            "areas": [
                {
                    "id": "Turbine 1",
                    "manpower": [
                        {"name": "Agus", "role": "Foreman", "hours": "07:00 - 17:00"},
                        {"name": "Bambang", "role": "Mechanic", "hours": "07:00 - 17:00"},
                        {"name": "Shared Person", "role": "Coordinator", "hours": "07:00 - 17:00"},
                    ],
                },
                {
                    "id": "Turbine 2",
                    "manpower": [
                        {"name": " agus ", "role": "Foreman", "hours": "07:00 – 19:00"},
                        {"name": "Bambang", "role": "Mechanic", "hours": "invalid"},
                    ],
                    "indirect_manpower": [
                        {"name": "Faiz", "role": "Project Control", "hours": "07:00 - 17:00"},
                    ],
                },
            ],
        }
        result = aggregate_monthly_records(
            [canonical_record(payload, report_id="headcount")],
            project_no="P-001",
            date_from="2026-07-30",
            date_to="2026-07-30",
        )

        day = result["manpower"]["daily"][0]
        self.assertEqual(day["direct_headcount"], 3)
        self.assertEqual(day["indirect_headcount"], 1)
        self.assertEqual(day["total_headcount"], 4)
        self.assertEqual(day["direct_man_hours"], 32.0)
        self.assertEqual(day["indirect_man_hours"], 10.0)
        self.assertEqual(day["total_man_hours"], 42.0)
        self.assertEqual(result["manpower"]["totals"]["total_person_days"], 4)

    def test_manpower_normalises_legacy_role_and_hours_aliases(self):
        payload = {
            "date": "2026-08-10",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "indirect_manpower": [
                {
                    "name": "Admin",
                    "position": "Project Admin",
                    "working_hours": "07:00-17:00",
                },
            ],
            "areas": [
                {
                    "id": "Unit 1",
                    "manpower": [
                        {
                            "name": "Engineer",
                            "role_position": "Woodward Engineer",
                            "work_hours": "07:00 – 17:00",
                        },
                        {
                            "name": "Specialist",
                            "position": "Specialist",
                            "hours": "07:00-17:00",
                            "man_hours": 8,
                        },
                        {
                            "name": "Standby",
                            "role": "Helper",
                            "hours": "07:00-17:00",
                            "man_hours": 0,
                        },
                    ],
                },
            ],
        }

        result = aggregate_monthly_records(
            [canonical_record(payload, report_id="legacy-aliases")],
            project_no="P-001",
            date_from="2026-08-10",
            date_to="2026-08-10",
        )

        day = result["manpower"]["daily"][0]
        self.assertEqual(day["direct_headcount"], 3)
        self.assertEqual(day["indirect_headcount"], 1)
        self.assertEqual(day["total_man_hours"], 28.0)
        self.assertEqual(day["parsed_hours_count"], 4)
        self.assertEqual(day["zero_hours_count"], 1)
        roles = {row["role"]: row for row in result["manpower"]["roles"]}
        self.assertEqual(roles["Project Admin"]["man_hours"], 10.0)
        self.assertEqual(roles["Woodward Engineer"]["man_hours"], 10.0)
        self.assertEqual(roles["Specialist"]["man_hours"], 8.0)

    def test_explicit_man_hours_remains_authoritative_when_person_is_duplicated(self):
        payload = {
            "date": "2026-08-11",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "areas": [
                {
                    "id": "Unit 1",
                    "manpower": [
                        {"name": "Same Person", "position": "Technician", "hours": "07:00-17:00"},
                    ],
                },
                {
                    "id": "Unit 2",
                    "manpower": [
                        {"name": " same  person ", "role": "Technician", "man_hours": "8"},
                    ],
                },
            ],
        }

        result = aggregate_monthly_records(
            [canonical_record(payload, report_id="explicit-hours")],
            project_no="P-001",
            date_from="2026-08-11",
            date_to="2026-08-11",
        )

        day = result["manpower"]["daily"][0]
        self.assertEqual(day["direct_headcount"], 1)
        self.assertEqual(day["direct_man_hours"], 8.0)
        self.assertEqual(result["manpower"]["totals"]["total_person_days"], 1)

    def test_hours_completeness_distinguishes_zero_missing_and_invalid(self):
        payload = {
            "date": "2026-08-12",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "areas": [
                {
                    "id": "Unit 1",
                    "manpower": [
                        {"name": "Valid", "role": "Foreman", "hours": "07:00-17:00"},
                        {"name": "Zero", "role": "Standby", "man_hours": 0},
                        {"name": "Invalid", "role": "Helper", "working_hours": "unknown"},
                        {"name": "Missing", "role": "Technician"},
                    ],
                },
            ],
        }

        result = aggregate_monthly_records(
            [canonical_record(payload, report_id="completeness")],
            project_no="P-001",
            date_from="2026-08-12",
            date_to="2026-08-12",
        )

        day = result["manpower"]["daily"][0]
        self.assertEqual(day["total_man_hours"], 10.0)
        self.assertEqual(day["parsed_hours_count"], 2)
        self.assertEqual(day["zero_hours_count"], 1)
        self.assertEqual(day["missing_hours_count"], 1)
        self.assertEqual(day["invalid_hours_count"], 1)
        self.assertEqual(day["unparsed_hours_count"], 2)
        self.assertFalse(day["hours_complete"])
        totals = result["manpower"]["totals"]
        self.assertEqual(totals["zero_hours_count"], 1)
        self.assertEqual(totals["missing_hours_count"], 1)
        self.assertEqual(totals["invalid_hours_count"], 1)
        self.assertFalse(totals["hours_complete"])

    def test_progress_preserves_latest_daily_snapshot_without_recalculating_fields(self):
        first = canonical_record(
            {
                "date": "2026-07-01",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "overall_progress": [{
                    "description": "Commissioning",
                    "weight_factor": "40%",
                    "cumulative_previous_plan": "10%",
                    "cumulative_previous_actual": "8%",
                    "cumulative_to_date_plan": "12%",
                    "cumulative_to_date_actual": "9%",
                }],
            },
            report_id="progress-first",
        )
        last = canonical_record(
            {
                "date": "2026-07-31",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "show_overall_progress": True,
                "overall_progress": [
                    {
                        "description": " commissioning ",
                        "weight_factor": "40",
                        "cumulative_to_date_plan": "30,5%",
                        "cumulative_to_date_actual": "28%",
                        "this_period_plan": "999",  # must not be summed or preferred
                        "this_period_actual": "999",
                    },
                    {
                        "description": "Invalid row remains safe",
                        "weight_factor": "not-a-number",
                        "cumulative_to_date_plan": "NaN",
                    },
                ],
            },
            report_id="progress-last",
        )

        progress = aggregate_monthly_records(
            [first, last],
            project_no="P-001",
            date_from="2026-07-01",
            date_to="2026-07-31",
        )["overall_progress"]

        self.assertTrue(progress["available"])
        commissioning = progress["rows"][0]
        self.assertEqual(commissioning["this_period_plan"], 999.0)
        self.assertEqual(commissioning["this_period_actual"], 999.0)
        self.assertIsNone(commissioning["deviation"])
        self.assertAlmostEqual(progress["totals"]["cumulative_to_date_plan"], 12.2)
        self.assertAlmostEqual(progress["totals"]["cumulative_to_date_actual"], 11.2)
        self.assertAlmostEqual(progress["totals"]["deviation"], -1.0)

    def test_work_hours_policy_is_opt_in_and_explicit_man_hours_stays_authoritative(self):
        payload = {
            "date": "2026-08-13",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "manpower_status": "reported",
            "areas": [{
                "id": "MA-42",
                "manpower": [
                    {"name": "Shift Worker", "hours": "07:00-17:00"},
                    {"name": "Explicit Worker", "hours": "07:00-17:00", "man_hours": 8},
                ],
            }],
        }
        record = canonical_record(payload, report_id="hours-policy")

        legacy = aggregate_monthly_records(
            [record],
            project_no="P-001",
            date_from="2026-08-13",
            date_to="2026-08-13",
        )
        configured = aggregate_monthly_records(
            [record],
            project_no="P-001",
            date_from="2026-08-13",
            date_to="2026-08-13",
            work_hours_policy={
                "mode": "elapsed_less_break",
                "break_minutes": 60,
                "deduct_when_elapsed_gte_minutes": 360,
                "allow_overnight": True,
            },
        )

        self.assertEqual(legacy["manpower"]["daily"][0]["total_man_hours"], 18.0)
        self.assertEqual(configured["manpower"]["daily"][0]["total_man_hours"], 17.0)
        self.assertEqual(
            configured["manpower"]["work_hours_policy"]["mode"],
            "elapsed_less_break",
        )

    def test_missing_manpower_is_not_silently_reported_as_zero(self):
        payload = {
            "date": "2026-08-14",
            "project_no": "P-001",
            "project_title": "Reactivation",
            "manpower_status": "not_supplied",
            "areas": [],
        }
        result = aggregate_monthly_records(
            [canonical_record(payload, report_id="missing-manpower")],
            project_no="P-001",
            date_from="2026-08-14",
            date_to="2026-08-14",
        )

        day = result["manpower"]["daily"][0]
        self.assertFalse(day["supplied"])
        self.assertIsNone(day["total_headcount"])
        self.assertIsNone(day["total_man_hours"])
        self.assertIsNone(result["manpower"]["totals"]["peak_headcount"])
        self.assertFalse(result["manpower"]["totals"]["headcount_complete"])

    def test_area_provenance_and_constraint_register_are_exact_and_source_backed(self):
        records = []
        for index, report_date in enumerate(("2026-08-15", "2026-08-16"), start=1):
            records.append(canonical_record(
                {
                    "date": report_date,
                    "project_no": "P-001",
                    "project_title": "Reactivation",
                    "areas": [{
                        "id": "MA 42/59/67",
                        "activities_today": ["MA-59 install cable support"],
                        "constraints": [{"text": "Access permit pending"}],
                    }],
                    "global_remarks": "Coordination meeting recorded",
                },
                report_id=f"provenance-{index}",
            ))

        result = aggregate_monthly_records(
            records,
            project_no="P-001",
            date_from="2026-08-15",
            date_to="2026-08-16",
        )

        activity = result["activities"][0]
        self.assertEqual(activity["source_area"], "MA 42/59/67")
        self.assertEqual(activity["reporting_area"], "MA-59")
        register = result["constraint_register"][0]
        self.assertEqual(register["occurrence_count"], 2)
        self.assertEqual(register["status"], "reported")
        self.assertEqual(register["corrective_action"], "")
        self.assertEqual(register["reported_dates"], ["2026-08-15", "2026-08-16"])
        self.assertEqual(len([row for row in result["remarks"] if row["area"] == "General"]), 2)

    def test_progress_explicitly_disabled_in_daily_report_is_not_aggregated(self):
        hidden = canonical_record(
            {
                "date": "2026-07-15",
                "project_no": "P-001",
                "project_title": "Reactivation",
                "show_overall_progress": False,
                "overall_progress": [{
                    "description": "Hidden stale row",
                    "weight_factor": "100%",
                    "cumulative_to_date_actual": "80%",
                }],
            },
            report_id="hidden-progress",
        )

        progress = aggregate_monthly_records(
            [hidden],
            project_no="P-001",
            date_from="2026-07-01",
            date_to="2026-07-31",
        )["overall_progress"]

        self.assertFalse(progress["available"])
        self.assertEqual(progress["rows"], [])


if __name__ == "__main__":
    unittest.main()
