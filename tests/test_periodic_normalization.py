import unittest

from monthly_report.area_normalization import (
    activity_area_conflicts,
    canonical_source_area_label,
    reporting_activity_area,
)
from monthly_report.identity import (
    looks_like_daily_report_document_no,
    project_title_match,
)
from monthly_report.importer import parse_daily_report_pages
from monthly_report.report_quality import build_report_preflight
from monthly_report.web import _project_title_aliases, _record_from_uploaded_pdf


class PeriodicIdentityTests(unittest.TestCase):
    def test_selected_project_aliases_are_resolved_from_configuration(self):
        config = {
            "projects": [{
                "project_no": "MASTER-001",
                "title": "Canonical Turbine Project",
                "title_aliases": ["Legacy Turbine Project", "Legacy Turbine Project", ""],
            }]
        }

        self.assertEqual(
            _project_title_aliases(
                config,
                project_no="MASTER-001",
                project_title="Canonical Turbine Project",
            ),
            ["Legacy Turbine Project"],
        )

    def test_daily_document_number_supports_old_and_new_templates(self):
        self.assertTrue(looks_like_daily_report_document_no("001-KN-GPA-DAR"))
        self.assertTrue(
            looks_like_daily_report_document_no(
                "PC-26-006-KN-GPA-362-DAR",
                "Day 362",
            )
        )
        self.assertFalse(
            looks_like_daily_report_document_no(
                "PC-26-006-KN-GPA-362-DAR",
                "Day 361",
            )
        )
        self.assertFalse(
            looks_like_daily_report_document_no("LEGACY-PC-DAR", "Day 362")
        )
        self.assertTrue(looks_like_daily_report_document_no("NO.421/GPA/VIII/2026", "Day 421"))
        self.assertFalse(looks_like_daily_report_document_no("NO.421/GPA/VIII/2026", "Day 420"))
        self.assertFalse(looks_like_daily_report_document_no("001/KN-GPA/EPC-2F-P2/IV/2025", 421))

    def test_title_matching_is_deterministic_and_alias_aware(self):
        reordered = project_title_match(
            "Electrical Installation and Construction - Manpower Supply",
            "Electrical Construction and Installation - Manpower Supply",
        )
        self.assertTrue(reordered["matched"])
        self.assertEqual(reordered["method"], "title_token_equivalent")

        legacy_daily = project_title_match(
            "RE-ACTIVATION TURBINES AND GENERATORS",
            "REACTIVATION FOR TURBINES AND GENERATORS",
        )
        self.assertTrue(legacy_daily["matched"])
        self.assertEqual(legacy_daily["method"], "title_token_equivalent")

        approved = project_title_match(
            "Legacy Turbine Contract",
            "Turbine Reactivation Master",
            approved_aliases=["Legacy Turbine Contract"],
        )
        self.assertEqual(approved["method"], "approved_alias")
        self.assertFalse(
            project_title_match("General Maintenance", "Turbine Reactivation Master")["matched"]
        )

    def test_generic_title_subsets_are_suggestions_not_identity_matches(self):
        ordered = project_title_match(
            "Turbine Generator Reactivation Scope",
            "Turbine Generator Reactivation Scope for Kertas Nusantara",
        )
        self.assertFalse(ordered["matched"])
        self.assertTrue(ordered["suggested"])
        self.assertEqual(ordered["method"], "ordered_title_subset")

        meaningful = project_title_match(
            "Turbine Generator Reactivation",
            "Kertas Nusantara Turbine Generator Reactivation",
        )
        self.assertFalse(meaningful["matched"])
        self.assertTrue(meaningful["suggested"])
        self.assertEqual(meaningful["method"], "meaningful_title_subset")

    def test_exact_number_and_approved_alias_flow_without_manual_review(self):
        project_no = "MASTER-ALIAS-001"
        canonical_title = "Canonical Turbine Reactivation Contract"
        alias = "Arbitrary Legacy Commissioning Name"
        imported = parse_daily_report_pages(
            ["\n".join([
                f"Project No. {project_no}",
                f"Project Name {alias}",
                "Date 2026-08-04",
                "Working Day Day 404",
            ])],
            known_projects=[{
                "project_id": "master-project",
                "title": canonical_title,
                "project_no": project_no,
                "title_aliases": [alias],
            }],
        )

        record, _warnings = _record_from_uploaded_pdf(
            imported,
            filename="daily-alias.pdf",
            username="reza",
            project_no=project_no,
            project_title=canonical_title,
            date_from="2026-08-01",
            date_to="2026-08-31",
        )

        self.assertIsNotNone(record)
        self.assertFalse(record["review_required"])
        self.assertEqual(record["source_identity"]["review_state"], "matched")
        self.assertEqual(record["source_identity"]["match_method"], "approved_alias")
        self.assertEqual(record["source_identity"]["matched_title_alias"], alias)


class PeriodicAreaTests(unittest.TestCase):
    def test_area_reassignment_requires_explicit_leading_area(self):
        explicit = reporting_activity_area("MA 42/59/67", "MA-59 install cable support")
        self.assertEqual(explicit["source_area"], "MA 42/59/67")
        self.assertEqual(explicit["reporting_area"], "MA-59")
        self.assertEqual(explicit["method"], "leading_explicit")

        routing = reporting_activity_area(
            "MA 42/59/67",
            "Pull cable from MA 59 to MA 42 and check route",
        )
        self.assertEqual(routing["reporting_area"], "MA 42/59/67")
        self.assertEqual(routing["method"], "source_fallback")

    def test_area_alias_and_conflict_helpers_are_conservative(self):
        self.assertEqual(canonical_source_area_label("MA 059"), "MA-59")
        self.assertTrue(activity_area_conflicts("MA-42", "MA-59 install support"))
        self.assertFalse(activity_area_conflicts("MA-42", "Route from MA-59 to MA-42"))


class PeriodicPreflightTests(unittest.TestCase):
    def test_preview_surfaces_final_identity_risk_without_blocking(self):
        report = {
            "project_no": "P-001",
            "project_title": "Project One",
            "source_validation": {
                "selected_project_no": "P-001",
                "selected_project_title": "Project One",
                "applied": False,
                "confirmed": False,
                "project_groups": [{
                    "project_no": "P-OTHER",
                    "project_title": "Other",
                    "requires_confirmation": True,
                    "decision": "",
                }],
                "duplicate_groups": [],
                "issues": [],
            },
            "coverage": {"missing_dates": []},
        }
        preview = build_report_preflight(report, for_final=False)
        final = build_report_preflight(report, for_final=True)

        self.assertTrue(preview["ready"])
        self.assertEqual(preview["blockers"], [])
        self.assertFalse(preview["readiness"]["final_ready"])
        self.assertFalse(final["ready"])
        self.assertIn("source_identity_unresolved", {row["code"] for row in final["blockers"]})

    def test_pending_ai_is_warning_not_blocker(self):
        report = {
            "project_no": "P-001",
            "project_title": "Project One",
            "source_validation": {
                "selected_project_no": "P-001",
                "selected_project_title": "Project One",
                "applied": True,
                "confirmed": True,
                "project_groups": [],
                "duplicate_groups": [],
                "issues": [],
            },
            "ai_summary": {"status": "suggested"},
            "coverage": {"missing_dates": []},
        }
        final = build_report_preflight(report, for_final=True)
        self.assertNotIn("ai_review_pending", {row["code"] for row in final["blockers"]})
        self.assertIn("ai_review_pending", {row["code"] for row in final["warnings"]})


if __name__ == "__main__":
    unittest.main()
