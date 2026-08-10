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

    def test_different_source_identities_require_explicit_confirmation(self):
        result = build_source_validation(
            self.records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )

        self.assertTrue(result["required"])
        self.assertEqual(len(result["project_groups"]), 2)
        ambiguous = [row for row in result["project_groups"] if row["requires_confirmation"]]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["decision"], "")

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
        validation = build_source_validation(
            self.records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )
        resolutions = []
        for group in validation["project_groups"]:
            decision = "separate" if group["project_no"].startswith("PC-") else "merge"
            resolutions.append({"group_key": group["key"], "decision": decision})

        included, excluded = resolve_project_records(
            self.records,
            validation,
            project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            project_title="REACTIVATION FOR TURBINES AND GENERATORS",
            resolutions=resolutions,
        )

        self.assertEqual([row["report_id"] for row in included], ["b2"])
        self.assertEqual([row["report_id"] for row in excluded], ["a1"])

    def test_unresolved_ambiguous_group_is_rejected(self):
        validation = build_source_validation(
            self.records,
            selected_project_no="001/KN-GPA/EPC-2F-P2/IV/2025",
            selected_project_title="REACTIVATION FOR TURBINES AND GENERATORS",
        )
        exact_group = next(row for row in validation["project_groups"] if row["matches_selected"])

        with self.assertRaisesRegex(ValueError, "Choose Merge"):
            resolve_project_records(
                self.records,
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


if __name__ == "__main__":
    unittest.main()
