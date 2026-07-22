import io
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from daily_report_app import PHOTO_MAX_DIMENSION, app, compress_photo_bytes


class PhotoCompressionTests(unittest.TestCase):
    def test_large_png_becomes_bounded_jpeg(self):
        source = Image.new('RGB', (2400, 1600), (40, 120, 200))
        raw = io.BytesIO()
        source.save(raw, format='PNG')

        compressed = compress_photo_bytes(raw.getvalue())

        with Image.open(io.BytesIO(compressed)) as result:
            self.assertEqual(result.format, 'JPEG')
            self.assertLessEqual(max(result.size), PHOTO_MAX_DIMENSION)
            self.assertEqual(result.size, (1280, 853))

    def test_exif_orientation_is_applied_and_removed(self):
        source = Image.new('RGB', (400, 800), (80, 160, 40))
        exif = Image.Exif()
        exif[274] = 6
        raw = io.BytesIO()
        source.save(raw, format='JPEG', quality=90, exif=exif)

        compressed = compress_photo_bytes(raw.getvalue())

        with Image.open(io.BytesIO(compressed)) as result:
            self.assertEqual(result.size, (800, 400))
            self.assertNotIn(274, result.getexif())

    def test_transparent_png_is_flattened_on_white(self):
        source = Image.new('RGBA', (20, 20), (255, 0, 0, 0))
        raw = io.BytesIO()
        source.save(raw, format='PNG')

        compressed = compress_photo_bytes(raw.getvalue())

        with Image.open(io.BytesIO(compressed)) as result:
            r, g, b = result.convert('RGB').getpixel((10, 10))
            self.assertGreater(r, 245)
            self.assertGreater(g, 245)
            self.assertGreater(b, 245)

    def test_invalid_image_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Invalid or unsupported image'):
            compress_photo_bytes(b'not-an-image')

    def test_upload_endpoint_stores_compressed_jpeg(self):
        source = Image.new('RGB', (2000, 1000), (30, 100, 170))
        raw = io.BytesIO()
        source.save(raw, format='PNG')
        raw.seek(0)

        client = app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['username'] = 'compression-test'

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = client.post(
                    '/upload_temp_photo',
                    data={'photo': (raw, 'camera.png')},
                    content_type='multipart/form-data',
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload['ok'])
            self.assertTrue(payload['photo_filename'].endswith('.jpg'))

            stored_path = os.path.join(temp_dir, payload['photo_filename'])
            with Image.open(stored_path) as stored:
                self.assertEqual(stored.format, 'JPEG')
                self.assertEqual(stored.size, (1280, 640))


if __name__ == '__main__':
    unittest.main()
