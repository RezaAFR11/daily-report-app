import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from pypdf import PdfReader
from monthly_report.scurve import apply_history_curve
from monthly_report.renderer import render_monthly_report
from monthly_report.web import _monthly_user_dir, _save_monthly_index, _refresh_history_s_curve


def report(month, value, revision=1):
    return {'report_type': 'monthly', 'project_no': 'P1', 'status': 'FINAL',
            'revision': revision, 'period': {'start': f'2026-{month:02d}-01', 'end': f'2026-{month:02d}-28'},
            'progress': {'rows': [{'description': 'OVERALL PROGRESS', 'is_total': True,
                                 'source_date': f'2026-{month:02d}-20', 'plan': value + 5, 'to_date': value}]}}


class HistoryCurveTests(unittest.TestCase):
    def test_snapshots_are_not_added_and_actual_source_dates_used(self):
        current = report(6, 30)
        before = copy.deepcopy(current['progress'])
        apply_history_curve(current, [report(5, 20)])
        self.assertEqual(current['s_curve']['actual'], [20, 30])
        self.assertEqual(current['s_curve']['labels'], ['2026-05-20', '2026-06-20'])
        self.assertEqual(current['progress'], before)

    def test_project_final_revision_future_and_weekly_filters(self):
        other = report(4, 99); other['project_no'] = 'P2'
        weekly = report(3, 5); weekly['report_type'] = 'weekly'
        draft = report(2, 4); draft['status'] = 'DRAFT'
        current = report(6, 30)
        apply_history_curve(current, [other, weekly, draft, report(7, 45), report(5, 20), report(5, 25, 2)])
        self.assertEqual(current['s_curve']['actual'], [25, 30])

    def test_missing_values_hide_curve_and_latest_invalid_revision_does_not_revert(self):
        latest = report(5, 25, 2); latest['progress']['rows'][0]['plan'] = None
        current = report(6, 30)
        apply_history_curve(current, [report(5, 20), latest])
        self.assertNotIn('s_curve', current)
        apply_history_curve(current, [report(5, 20)])
        current['progress']['rows'] = []
        apply_history_curve(current, [report(5, 20)])
        self.assertFalse(current['include_s_curve'])
        self.assertNotIn('s_curve', current)

    def test_manual_curve_and_weekly_unchanged(self):
        current = report(6, 30); current['s_curve'] = {'approved': True}
        before = copy.deepcopy(current)
        apply_history_curve(current, [report(5, 20)])
        self.assertEqual(current, before)
        current = report(6, 30); current['report_type'] = 'weekly'
        before = copy.deepcopy(current)
        apply_history_curve(current, [report(5, 20)])
        self.assertEqual(current, before)

    def test_saved_history_and_pdf_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = _monthly_user_dir(directory, 'alice') / 'reports'
            root.mkdir(exist_ok=True, parents=True)
            (root / 'may.json').write_text(json.dumps(report(5, 20)), encoding='utf-8')
            _save_monthly_index(directory, 'alice', [{'status': 'FINAL', 'project_no': 'P1', 'json_filename': 'may.json'}])
            current = report(6, 30)
            _refresh_history_s_curve(directory, 'alice', current)
            self.assertTrue(current['include_s_curve'])
            pdf = render_monthly_report(current)
            text = '\n'.join(p.extract_text() for p in PdfReader(pdf).pages)
            self.assertIn('Progress S-Curve', text)
            self.assertIn('2026-05-20', text)
            self.assertIn('not an approved schedule baseline', text)
            other_user = report(6, 30)
            _refresh_history_s_curve(directory, 'bob', other_user)
            self.assertFalse(other_user['include_s_curve'])
            _save_monthly_index(directory, 'alice', [])
            _refresh_history_s_curve(directory, 'alice', current)
            self.assertFalse(current['include_s_curve'])
