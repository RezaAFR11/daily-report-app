import base64
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image

from daily_report_app import PHOTO_MAX_DIMENSION, _write_imported_photos, app


def _image_bytes(fmt='PNG', size=(1800, 900), colour=(25, 120, 190)):
    output = io.BytesIO()
    Image.new('RGB', size, colour).save(output, format=fmt)
    return output.getvalue()


def _report(photo=None):
    return {
        'date': '2026-06-19',
        'project_title': 'Electrical Construction and Installation',
        'areas': [{
            'id': 'MA-39',
            'activities_today': ['Installation'],
            'manpower': [{
                'name': 'Worker',
                'role': 'Technician',
                'task': 'Cable termination',
                'hours': '07:00 - 17:00',
            }],
            'constraints': 'None',
            'remarks': 'Keep this data',
            'photos': [photo] if photo else [],
        }],
    }


class DraftBundleTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = 'bundle-test'

    def test_export_zip_contains_report_and_referenced_photo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_name = 'existing-photo.jpg'
            with open(os.path.join(temp_dir, source_name), 'wb') as output:
                output.write(_image_bytes('JPEG'))

            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/export_draft_bundle',
                    json=_report({
                        'img_data': '',
                        'photo_filename': source_name,
                        'desc': 'Site photo',
                    }),
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Draft-Photo-Count'], '1')
        self.assertEqual(response.headers['X-Draft-Missing-Photos'], '0')
        with zipfile.ZipFile(io.BytesIO(response.data), 'r') as archive:
            self.assertIn('report.json', archive.namelist())
            self.assertIn('manifest.json', archive.namelist())
            exported = json.loads(archive.read('report.json'))
            photo = exported['areas'][0]['photos'][0]
            self.assertEqual(photo['img_data'], '')
            self.assertTrue(photo['photo_filename'].endswith('.jpg'))
            self.assertIn(f"photos/{photo['photo_filename']}", archive.namelist())
            self.assertEqual(exported['areas'][0]['constraints'], 'None')
            self.assertEqual(exported['areas'][0]['remarks'], 'Keep this data')
            self.assertEqual(exported['areas'][0]['manpower'][0]['task'], 'Cable termination')

    def test_export_deduplicates_repeated_photo_content(self):
        raw_photo = _image_bytes('PNG', size=(400, 200))
        inline = 'data:image/png;base64,' + base64.b64encode(raw_photo).decode('ascii')
        report = _report()
        report['areas'][0]['photos'] = [
            {'img_data': inline, 'photo_filename': '', 'desc': 'First reference'},
            {'img_data': inline, 'photo_filename': '', 'desc': 'Second reference'},
        ]

        response = self.client.post('/export_draft_bundle', json=report)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['X-Draft-Photo-Count'], '1')
        with zipfile.ZipFile(io.BytesIO(response.data), 'r') as archive:
            exported = json.loads(archive.read('report.json'))
            photos = exported['areas'][0]['photos']
            self.assertEqual(photos[0]['photo_filename'], photos[1]['photo_filename'])
            bundled_names = [
                name for name in archive.namelist() if name.startswith('photos/')
            ]
            self.assertEqual(len(bundled_names), 1)

    def test_import_zip_compresses_photo_and_rewrites_reference(self):
        report = _report({
            'img_data': '',
            'photo_filename': 'camera.png',
            'desc': 'Imported camera photo',
        })
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('report.json', json.dumps(report))
            archive.writestr('photos/camera.png', _image_bytes('PNG'))
        bundle.seek(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/import_draft_bundle',
                    data={'file': (bundle, 'portable-draft.zip')},
                    content_type='multipart/form-data',
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['imported_photos'], 1)
            self.assertEqual(payload['missing_photos'], [])
            imported_photo = payload['data']['areas'][0]['photos'][0]
            self.assertEqual(imported_photo['img_data'], '')
            self.assertNotEqual(imported_photo['photo_filename'], 'camera.png')
            self.assertRegex(imported_photo['photo_filename'], r'^[a-f0-9]{32}\.jpg$')
            stored_path = os.path.join(temp_dir, imported_photo['photo_filename'])
            with Image.open(stored_path) as stored:
                self.assertEqual(stored.format, 'JPEG')
                self.assertLessEqual(max(stored.size), PHOTO_MAX_DIMENSION)

    def test_import_reuses_one_stored_file_for_duplicate_references(self):
        report = _report()
        report['areas'][0]['photos'] = [
            {'img_data': '', 'photo_filename': 'shared.png', 'desc': 'First'},
            {'img_data': '', 'photo_filename': 'shared.png', 'desc': 'Second'},
        ]
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('report.json', json.dumps(report))
            archive.writestr('photos/shared.png', _image_bytes('PNG'))
        bundle.seek(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/import_draft_bundle',
                    data={'file': (bundle, 'duplicates.zip')},
                    content_type='multipart/form-data',
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['imported_photos'], 2)
            photos = payload['data']['areas'][0]['photos']
            self.assertEqual(photos[0]['photo_filename'], photos[1]['photo_filename'])
            stored_photos = [
                name for name in os.listdir(temp_dir) if name.endswith('.jpg')
            ]
            self.assertEqual(len(stored_photos), 1)

    def test_import_photo_write_failure_removes_files_and_temporary_output(self):
        prepared = [
            {'filename': 'first.jpg', 'contents': b'first'},
            {'filename': 'second.jpg', 'contents': b'second'},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            replace_calls = 0

            def fail_second_replace(source, target):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError('storage unavailable')
                os.rename(source, target)

            with (
                patch(
                    'daily_report_app.os.replace',
                    side_effect=fail_second_replace,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    'Imported photos could not be saved',
                ),
            ):
                _write_imported_photos(prepared, temp_dir)

            self.assertEqual(os.listdir(temp_dir), [])

    def test_legacy_json_with_inline_photo_is_still_supported(self):
        raw_photo = _image_bytes('PNG', size=(400, 200))
        inline = 'data:image/png;base64,' + base64.b64encode(raw_photo).decode('ascii')
        draft = io.BytesIO(json.dumps(_report({
            'img_data': inline,
            'photo_filename': '',
            'desc': 'Legacy inline photo',
        })).encode('utf-8'))

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/import_draft_bundle',
                    data={'file': (draft, 'legacy.json')},
                    content_type='multipart/form-data',
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['imported_photos'], 1)
            filename = payload['data']['areas'][0]['photos'][0]['photo_filename']
            self.assertTrue(os.path.isfile(os.path.join(temp_dir, filename)))

    def test_legacy_filename_only_json_reports_missing_photo_without_losing_data(self):
        draft = io.BytesIO(json.dumps(_report({
            'img_data': '',
            'photo_filename': 'missing-photo.jpg',
            'desc': 'Must remain attached to the row',
        })).encode('utf-8'))

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/import_draft_bundle',
                    data={'file': (draft, 'legacy.json')},
                    content_type='multipart/form-data',
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['imported_photos'], 0)
        self.assertEqual(len(payload['missing_photos']), 1)
        photo = payload['data']['areas'][0]['photos'][0]
        self.assertEqual(photo['photo_filename'], 'missing-photo.jpg')
        self.assertEqual(photo['desc'], 'Must remain attached to the row')
        self.assertTrue(photo['photo_missing'])

    def test_legacy_filename_json_rehomes_photo_for_current_user(self):
        original_name = 'old-server-photo.jpg'
        draft = io.BytesIO(json.dumps(_report({
            'img_data': '',
            'photo_filename': original_name,
            'desc': 'Existing legacy photo',
        })).encode('utf-8'))

        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, original_name), 'wb') as output:
                output.write(_image_bytes('JPEG', size=(500, 250)))
            with patch('daily_report_app.get_temp_photos_dir', return_value=temp_dir):
                response = self.client.post(
                    '/import_draft_bundle',
                    data={'file': (draft, 'legacy.json')},
                    content_type='multipart/form-data',
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['imported_photos'], 1)
            new_name = payload['data']['areas'][0]['photos'][0]['photo_filename']
            self.assertNotEqual(new_name, original_name)
            self.assertTrue(os.path.isfile(os.path.join(temp_dir, new_name)))

    def test_zip_with_unsafe_member_is_rejected(self):
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr('report.json', json.dumps(_report()))
            archive.writestr('../outside.jpg', _image_bytes('JPEG'))
        bundle.seek(0)

        response = self.client.post(
            '/import_draft_bundle',
            data={'file': (bundle, 'unsafe.zip')},
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('unsafe file path', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
