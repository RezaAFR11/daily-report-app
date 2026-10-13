import base64
import io
import unittest
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import fitz
from PIL import Image
from reportlab.platypus import Table, Spacer
import daily_report_app as app
from kn_daily_pdf import WIDTH, MARGIN, build_story, numbered


class KNLayoutTests(unittest.TestCase):
    def render(self, report):
        return fitz.open(stream=app.generate_pdf(report, None, app.DEFAULT_CONFIG).getvalue(), filetype='pdf')

    def report(self, count=0, caption='Sample caption'):
        b = io.BytesIO()
        Image.new('RGB', (160, 90), 'teal').save(b, 'JPEG')
        source = 'data:image/jpeg;base64,' + base64.b64encode(b.getvalue()).decode()
        return {'pdf_layout': 'kn', 'project_title': 'LAYOUT TEST ONLY',
                'date': '2026-10-13', 'work_package': 'Template check',
                'areas': [{'id': 'TEST AREA', 'photos': [
                    {'img_data': source, 'desc': f'{i+1}: {caption}'} for i in range(count)]}]}

    def test_empty_tables_width_and_one_placeholder(self):
        for item in build_story(self.report(), lambda *args: Spacer(1, 1)):
            if isinstance(item, Table):
                self.assertAlmostEqual(sum(item._colWidths), WIDTH)
        self.assertEqual(len(numbered('F. CONSTRAINTS / PROBLEM', [])._cellvalues), 2)
        self.assertEqual(len(numbered('F. CONSTRAINTS / PROBLEM', ['a', 'b'])._cellvalues), 3)
        doc = self.render(self.report())
        self.assertEqual(len(doc), 3)
        for page in doc:
            self.assertIn('DAILY ACTIVITY REPORT', page.get_text())
            self.assertIn('Template check', page.get_text())
        self.assertIn('Page 3 of 3', doc[-1].get_text())

    def test_twelve_photos_one_page_thirteenth_next(self):
        doc = self.render(self.report(13))
        self.assertEqual(len(doc), 4)
        self.assertEqual(doc[2].get_text().count('Sample caption'), 12)
        self.assertEqual(doc[3].get_text().count('Sample caption'), 1)
        for page in doc:
            for block in page.get_text('blocks'):
                self.assertGreaterEqual(block[0], 0)
                self.assertLessEqual(block[2], page.rect.width + .1)
                self.assertLessEqual(block[3], page.rect.height)

    def test_long_captions_continue_without_losing_photos(self):
        doc = self.render(self.report(12, 'Long caption with wrapped text. ' * 12))
        self.assertGreater(len(doc), 3)
        body = ''.join(p.get_text() for p in doc)
        for i in range(1, 13):
            self.assertIn(f'{i}: Long caption', body)
        for page in list(doc)[2:]:
            self.assertIn('DOCUMENTATION PHOTO', page.get_text())

    def test_long_tables_repeat_header_and_preserve_input(self):
        report = self.report()
        report['areas'][0]['activities_today'] = [f'Activity {i}' for i in range(75)]
        report['kn_conclusion'] = [{'description': 'Measured item', 'bq_contract': '20',
                                  'progress_today': '2', 'total_accum': '8', 'progress_percent': '40'}]
        original = deepcopy(report)
        doc = self.render(report)
        self.assertGreater(len(doc), 3)
        body = ''.join(p.get_text() for p in doc)
        self.assertIn('Activity 74', body)
        self.assertIn('Measured item', body)
        for page in doc:
            self.assertIn('DAILY ACTIVITY REPORT', page.get_text())
        self.assertEqual(report, original)

    def test_missing_photo_fails_instead_of_silent_omission(self):
        report = self.report()
        report['areas'][0]['photos'] = [{'photo_filename': 'missing.jpg'}]
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.render(report)

    def test_legacy_format_flag_cannot_select_a_different_layout(self):
        report = self.report(1)
        report['pdf_layout'] = 'standard'
        doc = self.render(report)
        self.assertIn('DAILY ACTIVITY REPORT', doc[0].get_text())
        page = doc[-1]
        headings = [span for block in page.get_text('dict')['blocks']
                    for line in block.get('lines', []) for span in line['spans']
                    if span['text'] == 'DOCUMENTATION PHOTO']
        self.assertEqual(len(headings), 1)
        heading = headings[0]
        self.assertAlmostEqual(heading['size'], 16)
        self.assertAlmostEqual((heading['bbox'][0] + heading['bbox'][2]) / 2,
                               page.rect.width / 2, delta=1)
        expected = (214 / 255, 227 / 255, 188 / 255)
        self.assertTrue(any(d.get('fill') and all(abs(a-b) < .001 for a,b in zip(d['fill'], expected))
                            for d in page.get_drawings()))

    def test_no_layout_selector_or_special_format_label_in_form(self):
        html = Path(app.__file__).with_name('templates').joinpath('index.html').read_text(encoding='utf-8')
        for label in ('PDF Format', 'Report layout', 'KN format', 'id="pdf_layout"', 'Existing GPA format'):
            self.assertNotIn(label, html)

    def test_independent_conclusions_render_centered_icons(self):
        report = self.report()
        report['kn_weather_hourly'] = ['Sunny'] * 10 + ['Fog']
        report['kn_weather_intervals'] = ['Sunny', 'Cloud', 'Rain', 'Lightning']
        original = deepcopy(report)
        page = self.render(report)[0]
        self.assertEqual(len(page.get_images()), 5)  # Four icons plus the report logo.
        for condition in ('Sunny', 'Cloud', 'Rain', 'Lightning', 'Fog'):
            self.assertIn(condition, page.get_text())
        self.assertEqual(report, original)
        icons = sorted((i for i in page.get_image_info() if i['bbox'][1] > 150), key=lambda i: i['bbox'][0])
        self.assertEqual(len(icons), 4)
        for icon, center in zip(icons, (1.5, 4.5, 7.5, 10)):
            self.assertAlmostEqual((icon['bbox'][0] + icon['bbox'][2]) / 2,
                                   MARGIN + WIDTH * center / 11, delta=1)

    def test_period_weather_does_not_invent_interval_conclusions(self):
        report = self.report()
        report['weather'] = {'Morning': 'Sunny', 'Afternoon': 'Rainy', 'Impact': 'Work paused'}
        page = self.render(report)[0]
        self.assertEqual(len(page.get_images()), 1)  # Only the logo; no chosen icons.
        rendered = page.get_text()
        self.assertIn('Morning', rendered)
        self.assertIn('Afternoon', rendered)
        self.assertIn('Work paused', rendered)
        self.assertEqual(rendered.count('Sunny'), 1)
        self.assertEqual(rendered.count('Rainy'), 1)

    def test_hourly_conditions_do_not_automatically_select_icons(self):
        report = self.report()
        report['kn_weather_hourly'] = ['Sunny'] * 11
        page = self.render(report)[0]
        self.assertEqual(len(page.get_images()), 1)
        self.assertEqual(page.get_text().count('Sunny'), 11)

    def test_kn_fields_survive_draft_save_and_load(self):
        report = self.report()
        report['kn_weather_hourly'] = ['Rain'] + [''] * 10
        report['kn_weather_intervals'] = ['Cloud', '', 'Lightning', 'Sunny']
        report['kn_conclusion'] = [{'description': 'Measured item', 'progress_today': '0'}]
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['username'] = 'kn-layout-test'
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app, 'get_draft_file', return_value=str(Path(directory) / 'draft.json')
        ), patch.object(app, 'save_draft_snapshot'), patch.object(app, 'log_activity'):
            self.assertEqual(client.post('/save_draft', json=report).status_code, 200)
            self.assertEqual(client.get('/load_draft').get_json(), report)
