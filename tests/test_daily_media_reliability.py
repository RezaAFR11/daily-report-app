import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import daily_report_app
from daily_report_app import app, resolve_photos


class DailyMediaReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = 'media-reliability-test'

    def test_daily_json_routes_reject_declared_oversize_before_parsing(self):
        cases = (
            ('/save_draft', 'DAILY_SAVE_DRAFT_MAX_BYTES'),
            ('/generate', 'DAILY_GENERATE_MAX_BYTES'),
            ('/preview', 'DAILY_PREVIEW_MAX_BYTES'),
        )
        for route, constant_name in cases:
            with self.subTest(route=route), patch.object(
                daily_report_app,
                constant_name,
                8,
            ):
                response = self.client.post(
                    route,
                    data=b'{}',
                    content_type='application/json',
                    environ_overrides={'CONTENT_LENGTH': '9'},
                )

            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.get_json()['max_bytes'], 8)

    def test_resolve_photos_uses_private_paths_without_mutating_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_name = 'stored-media.jpg'
            media_path = os.path.join(temp_dir, media_name)
            Image.new('RGB', (80, 40), 'white').save(media_path, format='JPEG')
            payload = {
                'areas': [{
                    'id': 'Area 1',
                    'photos': [{
                        'img_data': '',
                        'photo_filename': media_name,
                        'desc': 'Stored photo',
                    }],
                }],
                'sign_offs': [{
                    'label': 'Prepared By',
                    'sig': '',
                    'sig_filename': media_name,
                }],
            }

            with patch(
                'daily_report_app.get_temp_photos_dir',
                return_value=temp_dir,
            ):
                resolved = resolve_photos(payload, 'media-reliability-test')

        self.assertIsNot(resolved, payload)
        self.assertNotIn('_photo_path', payload['areas'][0]['photos'][0])
        self.assertNotIn('_sig_path', payload['sign_offs'][0])
        self.assertEqual(
            resolved['areas'][0]['photos'][0]['_photo_path'],
            media_path,
        )
        self.assertEqual(resolved['sign_offs'][0]['_sig_path'], media_path)
        self.assertEqual(resolved['areas'][0]['photos'][0]['img_data'], '')

    def test_frontend_blocks_inline_media_from_compact_report_requests(self):
        template = Path(daily_report_app.__file__).with_name('templates').joinpath(
            'index.html'
        ).read_text(encoding='utf-8')

        self.assertIn('new AbortController()', template)
        self.assertIn("_startMediaUpload(box, dataUrl, 'signature')", template)
        self.assertIn("if (!await _ensureMediaReady('generate the PDF'))", template)
        self.assertIn("if (!await _ensureMediaReady('preview the PDF'))", template)
        self.assertIn('function _payloadContainsInlineMedia(payload)', template)
        self.assertIn('sig_filename:', template)
        self.assertNotIn("window.addEventListener('beforeunload', saveDraft)", template)


if __name__ == '__main__':
    unittest.main()
