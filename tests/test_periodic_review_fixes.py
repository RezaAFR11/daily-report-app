import unittest

from monthly_report.importer import _section_area_notes, _merge_area_note
from monthly_report.report_quality import _append_coverage_issues


class PeriodicReviewFixTests(unittest.TestCase):
    def test_wrapped_constraint_area_and_note(self):
        area = 'Cold Commissioning Activities - DAY 4 - Turbine & Generator Unit 2'
        section = '''Area                         Constraint / Issue
Cold Commissi     Tag 77-TI-3152E not reading
oning Activities  in DCS
- DAY 4 -
Turbine &
Generator Unit
2'''
        self.assertEqual(_section_area_notes(section, area_ids=[area], kind='constraints'),
                         [(area, 'Tag 77-TI-3152E not reading in DCS')])

    def test_repeated_note_with_semicolons_stays_single(self):
        area = {'constraints': 'Signal missing; check wiring; destination unknown.'}
        _merge_area_note(area, 'constraints', area['constraints'])
        self.assertEqual(area['constraints'], 'Signal missing; check wiring; destination unknown.')

    def test_unknown_area_is_not_silently_discarded(self):
        result = _section_area_notes('Area     Constraint / Issue\nOther    Missing cable',
                                     area_ids=['MA-77'], kind='constraints')
        self.assertTrue(any('Missing cable' in note for _, note in result))

    def test_missing_dates_are_advisory(self):
        warnings, info = [], []
        _append_coverage_issues({'coverage': {'missing_dates': ['2026-08-04']}}, warnings, info)
        self.assertEqual(warnings[0]['code'], 'partial_coverage')
        self.assertIn('2026-08-04', warnings[0]['message'])


if __name__ == '__main__':
    unittest.main()
