import unittest

from monthly_report.web import _progress_arithmetic_warnings, _compact_review_warnings


class WarningSourceReferenceTests(unittest.TestCase):
    def test_warning_uses_row_source_date_and_filename(self):
        progress = {
            'latest_snapshot_date': '2026-08-24',
            'rows': [{'description': 'Commissioning', 'last_source_date': '2026-08-23',
                      'cumulative_previous_actual': 5, 'this_period_actual': 40,
                      'cumulative_to_date_actual': 40}],
        }
        warning = _progress_arithmetic_warnings(progress, source_manifest=[
            {'report_date': '2026-08-23', 'filename': 'Daily 23.pdf'},
            {'report_date': '2026-08-24', 'filename': 'Daily 24.pdf'},
        ])[0]
        self.assertEqual(warning['severity'], 'warning')
        self.assertEqual(warning['filename'], 'Daily 23.pdf')
        self.assertIn('2026-08-23', warning['message'])
        self.assertIn('Daily 23.pdf', _compact_review_warnings([warning])[0])
        self.assertEqual(progress['rows'][0]['cumulative_to_date_actual'], 40)

    def test_unknown_or_ambiguous_source_is_not_guessed(self):
        progress = {'latest_snapshot_date': '2026-08-24', 'rows': [
            {'cumulative_previous_actual': 5, 'this_period_actual': 40,
             'cumulative_to_date_actual': 40}]}
        for manifest in [[], [
            {'report_date': '2026-08-24', 'filename': 'A.pdf'},
            {'report_date': '2026-08-24', 'filename': 'B.pdf'},
        ]]:
            warning = _progress_arithmetic_warnings(progress, source_manifest=manifest)[0]
            self.assertEqual(warning['filename'], '')
            self.assertEqual(warning['report_date'], '2026-08-24')


if __name__ == '__main__':
    unittest.main()
