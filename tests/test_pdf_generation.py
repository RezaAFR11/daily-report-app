import io
import json
import os
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from daily_report_app import app


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


if __name__ == '__main__':
    unittest.main()
