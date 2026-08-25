import unittest

from monthly_report.validation import (
    build_source_validation,
    resolve_duplicate_records,
    resolve_project_records,
)


def _record(report_id, project_no, project_title, report_date):
    return {
        "record_type": "final_daily_report",
        "report_id": report_id,
        "date": report_date,
        "project_no": project_no,
        "project_title": project_title,
        "payload": {
            "date": report_date,
            "project_no": project_no,
            "project_title": project_title,
            "areas": [],
        },
        "source": {"filename": f"{report_id}.pdf", "sha256": report_id * 8},
    }


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            _record(
                "a1",
                "PC-26-006-KN-GPA-359-DAR",
                "RE-ACTIVATION TURBINES AND GENERATORS",
                "2026-08-10",
            ),
            _record(
                "b2",
                "001/KN-GPA/EPC-2F-P2/IV/2025",
                "REACTIVATION FOR TURBINES AND GENERATORS",
                "2026-08-11",
            ),
        ]

    def test_daily_document_number_and_matching_title_share_canonical_identity(self):
        result = build_source_validation(
            self.records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )

        self.assertFalse(result["required"])
        self.assertTrue(result["applied"])
        self.assertEqual(len(result["project_groups"]), 1)
        self.assertEqual(
            result["project_groups"][0]["source_document_nos"],
            ["PC-26-006-KN-GPA-359-DAR"],
        )

    def test_merge_canonicalizes_output_but_preserves_source_identity(self):
        validation = build_source_validation(
            self.records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )
        resolutions = [
            {"group_key": group["key"], "decision": "merge"}
            for group in validation["project_groups"]
        ]

        included, excluded = resolve_project_records(
            self.records,
            validation,
            project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            project_title="REACTIVATION FOR TURBINES AND GENERATORS",
            resolutions=resolutions,
        )

        self.assertEqual(len(included), 2)
        self.assertEqual(excluded, [])
        self.assertEqual(included[0]["project_no"], "001/KN-GPA/EPC-2F-P2/IV/2025")
        self.assertEqual(
            included[0]["source_identity"]["project_no"],
            "PC-26-006-KN-GPA-359-DAR",
        )
        self.assertEqual(
            included[0]["payload"]["project_title"],
            "REACTIVATION FOR TURBINES AND GENERATORS",
        )

    def test_keep_separate_excludes_only_that_group(self):
        records = [
            self.records[1],
            _record("other", "OTHER-001", "Other Project", "2026-08-12"),
        ]
        validation = build_source_validation(
            records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )
        resolutions = []
        for group in validation["project_groups"]:
            decision = "separate" if group["project_no"] == "OTHER-001" else "merge"
            resolutions.append({"group_key": group["key"], "decision": decision})

        included, excluded = resolve_project_records(
            records,
            validation,
            project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            project_title="REACTIVATION FOR TURBINES AND GENERATORS",
            resolutions=resolutions,
        )

        self.assertEqual([row["report_id"] for row in included], ["b2"])
        self.assertEqual([row["report_id"] for row in excluded], ["other"])

    def test_unresolved_ambiguous_group_is_rejected(self):
        records = [
            self.records[1],
            _record("other", "OTHER-001", "Other Project", "2026-08-12"),
        ]
        validation = build_source_validation(
            records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )
        exact_group = next(row for row in validation["project_groups"] if row["matches_selected"])

        with self.assertRaisesRegex(ValueError, "Choose Merge"):
            resolve_project_records(
                records,
                validation,
                project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
                project_title="REACTIVATION FOR TURBINES AND GENERATORS",
                resolutions=[{"group_key": exact_group["key"], "decision": "merge"}],
            )

    def test_unidentified_files_remain_separate_project_decisions(self):
        records = [
            _record("unknown-a", "", "", "2026-08-10"),
            _record("unknown-b", "", "", "2026-08-11"),
        ]
        validation = build_source_validation(
            records,
            selected_project_no="TARGET-001",
            selected_project_title="Target Project",
        )

        self.assertEqual(len(validation["project_groups"]), 2)
        self.assertTrue(all(group["requires_confirmation"] for group in validation["project_groups"]))

    def test_same_date_duplicate_requires_explicit_source_selection(self):
        first = _record("duplicate-a", "P-001", "Project", "2026-08-10")
        second = _record("duplicate-b", "P-001", "Project", "2026-08-10")
        validation = build_source_validation(
            [first, second],
            selected_project_no="P-001",
            selected_project_title="Project",
        )
        duplicate = validation["duplicate_groups"][0]

        with self.assertRaisesRegex(ValueError, "Choose which Daily Report"):
            resolve_duplicate_records([first, second], validation, resolutions=[])

        included, excluded = resolve_duplicate_records(
            [first, second],
            validation,
            resolutions=[{
                "group_key": duplicate["key"],
                "selected_record_id": "duplicate-a",
            }],
        )
        self.assertEqual([record["report_id"] for record in included], ["duplicate-a"])
        self.assertEqual([record["report_id"] for record in excluded], ["duplicate-b"])

    def test_similar_title_alone_never_auto_merges_a_daily_document_number(self):
        record = _record(
            "fuzzy",
            "PC-26-006-KN-GPA-360-DAR",
            "REACTIVATON FOR TURBINES AND GENERATORS",
            "2026-08-12",
        )

        validation = build_source_validation(
            [record],
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )

        self.assertTrue(validation["required"])
        self.assertFalse(validation["applied"])
        self.assertTrue(validation["project_groups"][0]["requires_confirmation"])
        self.assertEqual(validation["project_groups"][0]["project_no"], "")

    def test_legacy_subset_provenance_is_not_trusted_for_automatic_identity(self):
        record = _record(
            "subset",
            "PC-26-006-KN-GPA-360-DAR",
            "Turbine Generator Reactivation",
            "2026-08-12",
        )
        record["source_identity"] = {
            "project_no": "PC-26-006-KN-GPA-360-DAR",
            "project_title": "Turbine Generator Reactivation",
            "canonical_project_no": "MASTER-001",
            "canonical_project_title": "Kertas Nusantara Turbine Generator Reactivation",
            "match_method": "meaningful_title_subset",
            "review_state": "matched",
        }

        validation = build_source_validation(
            [record],
            selected_project_no="MASTER-001",
            selected_project_title="Kertas Nusantara Turbine Generator Reactivation",
        )

        self.assertTrue(validation["required"])
        self.assertFalse(validation["applied"])
        self.assertTrue(validation["project_groups"][0]["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
