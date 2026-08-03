import io
import json
import os
import re
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import daily_report_app
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

from daily_report_app import (
    BUNDLED_HEADER_LOGOS,
    BUNDLED_LOGOS,
    DEFAULT_CONFIG,
    _NC,
    _group_report_photos,
    _normalise_overall_progress,
    _overall_progress_totals,
    _progress_number,
    app,
    generate_pdf,
    resolve_logo_path,
)


MINIMAL_REPORT = {
    'date': '2026-07-28',
    'day_no': '47',
    'project_no': 'PC-TEST',
    'location': 'Berau',
    'customer': 'PT. Test',
    'equipment': '-',
    'project_title': 'PDF Test',
    'prepared_by': 'Tester',
    'checked_by': 'Checker',
    'approved_by': 'Approver',
    'global_remarks': '',
    'weather': {},
    'indirect_manpower': [],
    'areas': [],
    'sign_offs': [],
}


class PDFGenerationTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = 'pdf-test'

    def test_generate_downloads_and_archives_pdf(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reports_dir = os.path.join(temp_dir, 'reports')
            with patch('daily_report_app.get_reports_dir', return_value=reports_dir):
                response = self.client.post('/generate', json=MINIMAL_REPORT)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['X-Report-Archive-Status'], 'saved')
            self.assertTrue(response.data.startswith(b'%PDF'))

            with open(os.path.join(reports_dir, 'index.json'), encoding='utf-8') as index_file:
                index = json.load(index_file)
            self.assertEqual(len(index), 1)
            self.assertEqual(index[0]['date'], '2026-07-28')
            self.assertTrue(os.path.isfile(os.path.join(reports_dir, index[0]['filename'])))

    def test_archive_failure_does_not_block_pdf_download(self):
        fake_pdf = io.BytesIO(b'%PDF-1.4\nvalid test pdf\n%%EOF')
        with (
            patch('daily_report_app.generate_pdf', return_value=fake_pdf),
            patch(
                'daily_report_app.archive_generated_report',
                side_effect=OSError('volume temporarily unavailable'),
            ),
        ):
            response = self.client.post('/generate', json=MINIMAL_REPORT)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Report-Archive-Status'], 'failed')
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_generate_rejects_invalid_json(self):
        response = self.client.post(
            '/generate',
            data='not-json',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'Invalid report data')

    def test_very_long_photo_caption_does_not_break_pdf_layout(self):
        report = deepcopy(MINIMAL_REPORT)
        report['areas'] = [{
            'id': 'Turbine Unit 2',
            'activities_today': [],
            'activities_tomorrow': [],
            'manpower': [],
            'indirect_manpower': [],
            'constraints': '',
            'remarks': '',
            'photos': [{
                'desc': 'Long photo caption ' * 2000,
                'img_data': '',
            }],
        }]

        response = self.client.post('/preview', json=report)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_photos_from_different_areas_use_separate_grids(self):
        areas = [
            {'id': 'MA-39', 'photos': [{'desc': 'First'}]},
            {'id': 'MA-40', 'photos': [{'desc': 'Second'}]},
        ]

        area_groups = _group_report_photos(areas, per_row=3)

        self.assertEqual([area_id for area_id, _ in area_groups], ['MA-39', 'MA-40'])
        self.assertEqual(
            [[entry_area for row in rows for entry_area, _ in row] for _, rows in area_groups],
            [['MA-39'], ['MA-40']],
        )

    def test_photo_grid_keeps_rows_and_order_inside_each_area(self):
        areas = [
            {'id': 'AREA-A', 'photos': [
                {'desc': 'A1'}, {'desc': 'A2'}, {'desc': 'A3'}, {'desc': 'A4'},
            ]},
            {'id': 'AREA-B', 'photos': [{'desc': 'B1'}, {'desc': 'B2'}]},
        ]

        area_groups = _group_report_photos(areas, per_row=3)

        self.assertEqual(
            [
                (area_id, [[photo['desc'] for _, photo in row] for row in rows])
                for area_id, rows in area_groups
            ],
            [('AREA-A', [['A1', 'A2', 'A3'], ['A4']]), ('AREA-B', [['B1', 'B2']])],
        )

    def test_separate_area_photo_grids_generate_without_layout_error(self):
        report = deepcopy(MINIMAL_REPORT)
        report['areas'] = [
            {
                'id': 'MA-39', 'activities_today': [], 'activities_tomorrow': [],
                'manpower': [], 'indirect_manpower': [], 'constraints': '', 'remarks': '',
                'photos': [{'desc': 'First photo ' * 80, 'img_data': ''}],
            },
            {
                'id': 'MA-40', 'activities_today': [], 'activities_tomorrow': [],
                'manpower': [], 'indirect_manpower': [], 'constraints': '', 'remarks': '',
                'photos': [{'desc': 'Second photo', 'img_data': ''}],
            },
        ]

        response = self.client.post('/preview', json=report)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_photo_documentation_title_is_repeated_on_each_photo_card(self):
        title = 'Cold Commissioning Activities - DAY 4 - Turbine & Generator Unit 2'
        escaped_title = title.replace('&', '&amp;')
        report = deepcopy(MINIMAL_REPORT)
        report['photo_documentation_title'] = title
        report['areas'] = [
            {
                'id': 'MA-39', 'activities_today': [], 'activities_tomorrow': [],
                'manpower': [], 'indirect_manpower': [], 'constraints': '', 'remarks': '',
                'photos': [{'desc': 'First', 'img_data': ''}],
            },
            {
                'id': 'MA-40', 'activities_today': [], 'activities_tomorrow': [],
                'manpower': [], 'indirect_manpower': [], 'constraints': '', 'remarks': '',
                'photos': [{'desc': 'Second', 'img_data': ''}],
            },
        ]
        paragraph_texts = []
        original_paragraph = daily_report_app.Paragraph

        def paragraph_spy(text, *args, **kwargs):
            paragraph_texts.append(text)
            return original_paragraph(text, *args, **kwargs)

        with patch('daily_report_app.Paragraph', side_effect=paragraph_spy):
            generate_pdf(report, None, deepcopy(DEFAULT_CONFIG))

        self.assertEqual(paragraph_texts.count(escaped_title), 1)
        self.assertEqual(paragraph_texts.count(f'<b>{escaped_title}</b>'), 2)
        self.assertIn('MA-39', paragraph_texts)
        self.assertIn('MA-40', paragraph_texts)
        self.assertNotIn('<b>MA-39</b>', paragraph_texts)
        self.assertNotIn('<b>MA-40</b>', paragraph_texts)

    def test_minimal_report_uses_one_compact_page(self):
        pdf = generate_pdf(deepcopy(MINIMAL_REPORT), None, deepcopy(DEFAULT_CONFIG))

        page_count = len(re.findall(rb'/Type\s*/Page\b', pdf.getvalue()))

        self.assertEqual(page_count, 1)

    def test_overall_progress_weighted_totals_match_reference(self):
        rows = []
        for description, weight, previous_actual, period, cumulative in [
            ('Overhaul Turbine', '18.32%', '100%', '0%', '100%'),
            ('Overhaul Generator', '20.43%', '100%', '0%', '100%'),
            ('Vibration Monitoring System Shinkawa', '28.86%', '100%', '0%', '100%'),
            ('Upgrade Woodward 501 to Micronet', '27.39%', '100%', '0%', '100%'),
            ('Commissioning & Hand Over', '5.00%', '0%', '5%', '5%'),
        ]:
            rows.append({
                'description': description,
                'weight_factor': weight,
                'cumulative_previous_plan': '100%',
                'cumulative_previous_actual': previous_actual,
                'this_period_plan': period,
                'this_period_actual': period,
                'cumulative_to_date_plan': cumulative,
                'cumulative_to_date_actual': cumulative,
            })

        totals = _overall_progress_totals(_normalise_overall_progress(rows))

        self.assertAlmostEqual(totals['cumulative_previous_plan'], 100.0)
        self.assertAlmostEqual(totals['cumulative_previous_actual'], 95.0)
        self.assertAlmostEqual(totals['this_period_plan'], 0.25)
        self.assertAlmostEqual(totals['this_period_actual'], 0.25)
        self.assertAlmostEqual(totals['cumulative_to_date_plan'], 95.25)
        self.assertAlmostEqual(totals['cumulative_to_date_actual'], 95.25)
        self.assertAlmostEqual(totals['deviation'], 0.0)

    def test_progress_parser_accepts_comma_percent_and_ignores_bad_rows(self):
        self.assertEqual(_progress_number('95,25%'), 95.25)
        self.assertIsNone(_progress_number('not a number'))
        self.assertEqual(_normalise_overall_progress(None), [])
        self.assertEqual(_normalise_overall_progress({'description': 'invalid'}), [])
        self.assertEqual(
            _normalise_overall_progress([None, {}, {'description': 'Valid row'}])[0]['description'],
            'Valid row',
        )
        self.assertEqual(
            _normalise_overall_progress([{'description': 'Zero', 'this_period_plan': 0}])[0]['this_period_plan'],
            '0',
        )

    def test_many_overall_progress_rows_generate_without_layout_error(self):
        report = deepcopy(MINIMAL_REPORT)
        report['overall_progress'] = [{
            'description': f'Long progress description {index} ' * 8,
            'duration': '273',
            'weight_factor': '2.5%',
            'start': '23-Jun-25',
            'finish': '23-Mar-26',
            'cumulative_previous_plan': '100%',
            'cumulative_previous_actual': '95%',
            'this_period_plan': '1%',
            'this_period_actual': '1%',
            'cumulative_to_date_plan': '96%',
            'cumulative_to_date_actual': '96%',
            'deviation': '0%',
        } for index in range(40)]

        response = self.client.post('/preview', json=report)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'%PDF'))

    def test_overall_progress_controls_are_present_in_form(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_draft = os.path.join(temp_dir, 'missing-draft.json')
            with (
                patch('daily_report_app.load_config', return_value=deepcopy(DEFAULT_CONFIG)),
                patch('daily_report_app.get_draft_file', return_value=missing_draft),
            ):
                response = self.client.get('/')

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('4 · Overall Progress', page)
        self.assertIn('id="overallProgressContainer"', page)
        self.assertIn('function addOverallProgressRow', page)
        self.assertIn('id="f_photo_documentation_title"', page)

    def test_long_project_title_wraps_without_truncation(self):
        title = (
            'PROJECT REVAMPING PT KERTAS NUSANTARA - '
            'REACTIVATION FOR TURBINES AND GENERATORS'
        )
        canvas = _NC(io.BytesIO(), pagesize=A4)

        with patch.object(canvas, 'drawCentredString') as draw_line:
            canvas._draw_wrapped_center(
                title,
                0,
                100 * mm,
                100,
                'Helvetica-Bold',
                6.2,
                5.2,
                3.1 * mm,
                max_lines=2,
            )

        lines = [call.args[2] for call in draw_line.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertEqual(' '.join(lines), title)
        self.assertNotIn('...', ''.join(lines))

    def test_missing_or_invalid_logo_uses_bundled_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for which in ('gpa', 'kn'):
                with self.subTest(which=which):
                    self.assertTrue(os.path.isfile(BUNDLED_LOGOS[which]))
                    self.assertTrue(os.path.isfile(BUNDLED_HEADER_LOGOS[which]))
                    self.assertEqual(resolve_logo_path('', which), BUNDLED_LOGOS[which])
                    self.assertEqual(
                        resolve_logo_path(os.path.join(temp_dir, 'missing.png'), which),
                        BUNDLED_LOGOS[which],
                    )

    def test_signoff_helper_text_is_removed_from_ui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_draft = os.path.join(temp_dir, 'missing-draft.json')
            with (
                patch('daily_report_app.load_config', return_value=deepcopy(DEFAULT_CONFIG)),
                patch('daily_report_app.get_draft_file', return_value=missing_draft),
            ):
                response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            'Each column = one sign-off block in the PDF.',
            response.get_data(as_text=True),
        )


if __name__ == '__main__':
    unittest.main()
