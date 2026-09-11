import copy
import unittest

from monthly_report.workforce_matching import match_employee, reconcile_timesheet
from monthly_report.workforce import (
    standardise_timesheet_preview, set_timesheet_preview, decide_timesheet,
    prepare_overtime_review, _accepted_overtime_records,
)
from monthly_report.web import _issued_report_copy

DATE = '2026-07-20'


def employee(name, status='present'):
    return {'name': name, 'employee_key': name.lower(), 'role': 'Technician', 'section': 'direct',
            'statuses': [{'date': DATE, 'status': status, 'physical_manhours': 10 if status == 'present' else 0}]}


def baseline(*names):
    return {'daily': [{'date': DATE, 'people': [
        {'name': name, 'section': 'direct', 'role': 'Technician', 'hours': 8} for name in names]}]}


def timesheet(*employees):
    return {'employees': list(employees), 'daily_totals': [{'date': DATE}], 'warnings': [], 'unresolved': []}


class WorkforceMatchingTests(unittest.TestCase):
    def test_abbreviations_typo_and_ambiguous_names(self):
        for short, full in [('M. Ali', 'Muhammad Ali'), ('Primus Tarigan', 'Primus Pitah Tarigan'),
                            ('Peter Paulung', 'Peter Pulung'), ('Febryan', 'M. Febryan')]:
            with self.subTest(short=short):
                self.assertEqual(match_employee(short, [employee(full)])[0]['name'], full)
        self.assertEqual(match_employee('M. Ali', [employee('Muhammad Ali'), employee('Mustafa Ali')]), (None, 'ambiguous'))
        self.assertEqual(match_employee('', [employee('')]), (None, 'unmatched'))
        self.assertIsNone(match_employee('Andi Lubis', [employee('Andi Irawan')])[0])

    def test_distinct_employee_ids_are_not_merged(self):
        worker = dict(employee('Muhammad Ali'), employee_id='A')
        self.assertEqual(match_employee('M. Ali', [worker], 'B'), (None, 'unmatched'))

    def test_timesheet_absence_overrides_daily_and_unknown_worker_falls_back(self):
        preview = timesheet(employee('Muhammad Ali', 'nonpresent'), employee('Budi'))
        original = copy.deepcopy(preview)
        result = standardise_timesheet_preview(reconcile_timesheet(preview, baseline('M. Ali', 'New Worker')))
        self.assertEqual(result['manpower']['totals']['total_person_days'], 2)
        self.assertEqual(result['manpower']['totals']['total_man_hours'], 18)
        self.assertEqual(result['daily'][0]['direct_man_hours'], 18)
        self.assertEqual(sum(r['physical_manhours'] for r in result['roles']), 18)
        self.assertEqual(result['daily_fallback_count'], 1)
        self.assertEqual(preview, original)

    def test_blank_timesheet_cell_is_unknown_not_daily_fallback(self):
        result = standardise_timesheet_preview(reconcile_timesheet(
            timesheet(employee('Muhammad Ali', 'missing')), baseline('M. Ali')))
        self.assertEqual(result['daily_fallback_count'], 0)
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['requires_confirmation'])

    def test_missing_daily_hours_remain_partial(self):
        source = baseline('Worker')
        source['daily'][0]['people'][0]['hours'] = None
        result = standardise_timesheet_preview(reconcile_timesheet(timesheet(), source))
        self.assertFalse(result['manpower']['totals']['hours_complete'])
        self.assertTrue(result['requires_confirmation'])

    def test_zero_attendance_can_override_daily(self):
        draft = {'manpower': baseline('M. Ali'), 'safety': {}}
        set_timesheet_preview(draft, timesheet(employee('Muhammad Ali', 'nonpresent')), actor='test')
        decide_timesheet(draft, 'apply', confirm_exceptions=True, actor='test')
        self.assertEqual(draft['safety']['total_manpower'], 0)
        self.assertEqual(draft['safety']['total_man_hours'], 0)

    def test_matched_overtime_duplicate_counts_once_and_absence_warns(self):
        records = [{'record_id': str(i), 'employee': name, 'employee_key': name.lower(), 'date': DATE,
                    'start': '17:00', 'end': '21:00', 'duration_hours': 4,
                    'included_in_total': True, 'requires_review': False} for i, name in enumerate(['M. Ali', 'Muhammad Ali'])]
        overtime = {'records': records, 'employees': [
            {'employee': r['employee'], 'employee_key': r['employee_key']} for r in records],
            'daily': [{'date': DATE}], 'period': {'start': DATE, 'end': DATE},
            'coverage': {'selected_populated_dates': [DATE], 'not_supplied_dates': []}}
        preview = prepare_overtime_review(overtime, timesheet(employee('Muhammad Ali', 'nonpresent')))
        self.assertEqual(preview['totals']['actual_ot_man_hours'], 4)
        self.assertEqual(preview['totals']['participant_count'], 1)
        self.assertTrue(any(w['code'] == 'overtime_attendance_difference' for w in preview['warnings']))
        daily, accepted = _accepted_overtime_records(preview, {'m. ali': 'direct', 'muhammad ali': 'direct'}, {}, [DATE])
        self.assertEqual(daily[DATE]['direct'], 4)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]['employee_key'], 'muhammad ali')

    def test_daily_identities_remain_draft_only(self):
        draft = {'manpower': baseline('Worker')}
        self.assertNotIn('people', _issued_report_copy(draft)['manpower']['daily'][0])
        self.assertIn('people', draft['manpower']['daily'][0])


if __name__ == '__main__':
    unittest.main()
