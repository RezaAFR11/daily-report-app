import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import daily_report_app as daily


class MainMediaFixTests(unittest.TestCase):
    def setUp(self):
        self.client = daily.app.test_client()
        with self.client.session_transaction() as session:
            session['username'] = 'media-fix-test'

    def test_oversize_rejected_before_reading_body(self):
        for route, name in [('/generate', 'DAILY_GENERATE_MAX_BYTES'),
                            ('/save_draft', 'DAILY_SAVE_DRAFT_MAX_BYTES'),
                            ('/preview', 'DAILY_PREVIEW_MAX_BYTES')]:
            with self.subTest(route=route), patch.object(daily, name, 8):
                response = self.client.post(route, data=b'{}',
                    content_type='application/json',
                    environ_overrides={'CONTENT_LENGTH': '9'})
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.get_json()['max_bytes'], 8)

    def test_missing_photo_stops_generate_before_rendering(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            daily, 'get_temp_photos_dir', return_value=directory
        ), patch.object(daily, 'generate_pdf') as render:
            response = self.client.post('/generate', json={
                'areas': [{'photos': [{'photo_filename': 'missing.jpg'}]}]})
            self.assertEqual(response.status_code, 400)
            self.assertIn('photo is missing', response.get_json()['error'])
            render.assert_not_called()

    def test_uploaded_photo_is_available_to_existing_pdf_renderer(self):
        image = io.BytesIO()
        Image.new('RGB', (48, 32), 'blue').save(image, format='JPEG')
        image.seek(0)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            daily, 'get_temp_photos_dir', return_value=directory
        ):
            response = self.client.post('/upload_temp_photo', data={
                'photo': (image, 'photo.jpg')})
            self.assertEqual(response.status_code, 200)
            name = response.get_json()['photo_filename']
            self.assertTrue((Path(directory) / name).is_file())
            report = daily.resolve_photos({'areas': [{'photos': [{
                'photo_filename': name, 'img_data': '', 'desc': 'Test photo'
            }]}]}, 'media-fix-test')
            self.assertTrue(report['areas'][0]['photos'][0]['img_data'].startswith('data:image/jpeg;base64,'))

if __name__ == '__main__':
    unittest.main()
