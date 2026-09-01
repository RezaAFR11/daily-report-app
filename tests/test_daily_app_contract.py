import copy
import json
import os
import tempfile
import unittest
from io import BytesIO
from urllib.parse import quote
from unittest.mock import patch

import daily_report_app


DAILY_ROUTE_METHODS = {
    '/': {'GET'},
    '/admin': {'GET'},
    '/admin/activity_log': {'GET'},
    '/admin/add_user': {'POST'},
    '/admin/all_submissions': {'GET'},
    '/admin/delete_report': {'POST'},
    '/admin/download/<username>/<path:filename>': {'GET'},
    '/admin/merge_submissions': {'POST'},
    '/admin/remove_user': {'POST'},
    '/admin/reset_pin': {'POST'},
    '/admin/user_reports/<username>': {'GET'},
    '/ai/chat': {'POST'},
    '/draft_snapshots': {'GET'},
    '/draft_snapshots/load/<filename>': {'GET'},
    '/export_draft_bundle': {'POST'},
    '/field': {'GET'},
    '/field/load': {'GET'},
    '/field/submit': {'POST'},
    '/generate': {'POST'},
    '/get_config': {'GET'},
    '/health': {'GET'},
    '/import_draft_bundle': {'POST'},
    '/letters': {'GET'},
    '/letters/delete': {'POST'},
    '/letters/download/<path:filename>': {'GET'},
    '/letters/generate': {'POST'},
    '/load_draft': {'GET'},
    '/load_yesterday': {'GET'},
    '/login': {'GET', 'POST'},
    '/logo_status': {'GET'},
    '/logout': {'GET'},
    '/my_reports': {'GET'},
    '/preview': {'POST'},
    '/remove_logo': {'POST'},
    '/reports/check_date': {'GET'},
    '/reports/delete': {'POST'},
    '/reports/download/<path:filename>': {'GET'},
    '/reports/drive-upload': {'POST'},
    '/save_config': {'POST'},
    '/save_draft': {'POST'},
    '/temp_photo/<filename>': {'GET'},
    '/upload_logo': {'POST'},
    '/upload_temp_photo': {'POST'},
}


class DailyRouteContractTests(unittest.TestCase):
    def test_all_daily_routes_keep_their_http_methods(self):
        actual = {
            rule.rule: set(rule.methods) - {'HEAD', 'OPTIONS'}
            for rule in daily_report_app.app.url_map.iter_rules()
        }

        self.assertEqual(len(DAILY_ROUTE_METHODS), 43)
        for route, methods in DAILY_ROUTE_METHODS.items():
            with self.subTest(route=route):
                self.assertIn(route, actual)
                self.assertEqual(actual[route], methods)


class IsolatedDailyAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name
        self.users_dir = os.path.join(self.data_dir, 'users')
        self.logos_dir = os.path.join(self.data_dir, 'logos')
        os.makedirs(self.users_dir)
        os.makedirs(self.logos_dir)

        path_values = {
            'DATA_DIR': self.data_dir,
            'CONFIG_FILE': os.path.join(self.data_dir, 'app_config.json'),
            'LOGOS_DIR': self.logos_dir,
            'USERS_FILE': os.path.join(self.data_dir, 'users.json'),
            'USERS_DIR': self.users_dir,
            'ACTIVITY_LOG_FILE': os.path.join(self.data_dir, 'activity_log.json'),
        }
        self.path_patches = [
            patch.object(daily_report_app, name, value)
            for name, value in path_values.items()
        ]
        for path_patch in self.path_patches:
            path_patch.start()

        self.previous_testing = daily_report_app.app.config.get('TESTING', False)
        daily_report_app.app.config['TESTING'] = True
        self.client = daily_report_app.app.test_client()
        daily_report_app._save_users({
            'admin': {
                'pin_hash': daily_report_app.hash_pin('1234'),
                'is_admin': True,
                'created_at': '2026-09-01',
            },
            'worker': {
                'pin_hash': daily_report_app.hash_pin('5678'),
                'is_admin': False,
                'created_at': '2026-09-01',
            },
        })

    def tearDown(self):
        daily_report_app.app.config['TESTING'] = self.previous_testing
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temp_dir.cleanup()

    def login_as(self, username, *, is_admin):
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = username
            flask_session['is_admin'] = is_admin


class AuthenticationAndAdminContractTests(IsolatedDailyAppTestCase):
    def test_protected_pages_redirect_anonymous_users_to_login(self):
        for path in ('/admin', '/letters', '/field'):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers['Location'].endswith('/login'))

    def test_login_and_logout_preserve_the_session_contract(self):
        response = self.client.post(
            '/login',
            data={'username': 'worker', 'pin': '5678'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/'))
        with self.client.session_transaction() as flask_session:
            self.assertEqual(flask_session['username'], 'worker')
            self.assertFalse(flask_session['is_admin'])

        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertNotIn('username', flask_session)
            self.assertNotIn('is_admin', flask_session)

    def test_non_admin_cannot_open_admin_page(self):
        self.login_as('worker', is_admin=False)

        response = self.client.get('/admin')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/'))

    def test_admin_can_add_reset_and_remove_a_user(self):
        self.login_as('admin', is_admin=True)

        add_response = self.client.post(
            '/admin/add_user',
            json={'username': 'field-user', 'pin': '2468', 'is_admin': False},
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertIn('field-user', daily_report_app.load_users())

        reset_response = self.client.post(
            '/admin/reset_pin',
            json={'username': 'field-user', 'pin': '1357'},
        )
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(
            daily_report_app.load_users()['field-user']['pin_hash'],
            daily_report_app.hash_pin('1357'),
        )

        remove_response = self.client.post(
            '/admin/remove_user',
            json={'username': 'field-user'},
        )
        self.assertEqual(remove_response.status_code, 200)
        self.assertNotIn('field-user', daily_report_app.load_users())


class ConfigurationContractTests(IsolatedDailyAppTestCase):
    def test_allowed_configuration_round_trips_and_unknown_keys_are_ignored(self):
        self.login_as('worker', is_admin=False)

        response = self.client.post(
            '/save_config',
            json={
                'company_name': 'PT. Contract Test',
                'location': 'Temporary Site',
                'theme': {'primary': '#123456'},
                'unknown_setting': 'must-not-persist',
            },
        )

        self.assertEqual(response.status_code, 200)
        saved = daily_report_app.load_config()
        self.assertEqual(saved['company_name'], 'PT. Contract Test')
        self.assertEqual(saved['location'], 'Temporary Site')
        self.assertEqual(saved['theme']['primary'], '#123456')
        self.assertNotIn('unknown_setting', saved)

        response = self.client.get('/get_config')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['company_name'], 'PT. Contract Test')


@unittest.skipUnless(daily_report_app.DOCX_OK, 'python-docx is not installed')
class LetterContractTests(IsolatedDailyAppTestCase):
    def setUp(self):
        super().setUp()
        self.login_as('worker', is_admin=False)

    def test_all_supported_letter_types_generate_docx_streams(self):
        config = copy.deepcopy(daily_report_app.DEFAULT_CONFIG)
        payloads = {
            'umum': {'subject': 'General contract'},
            'pekerja': {'workers': []},
            'alat_berat': {'vehicles': []},
            'sticker': {'workers': []},
            'sp': {'employee_name': 'Worker', 'sp_level': '1'},
            'phk': {'employee_name': 'Worker'},
        }

        for letter_type, payload in payloads.items():
            with self.subTest(letter_type=letter_type):
                report = {'letter_type': letter_type, **payload}
                with patch.object(daily_report_app, 'next_letter_seq', return_value=1):
                    document, sequence = daily_report_app.generate_letter_docx(
                        report,
                        config,
                    )
                self.assertTrue(document.getvalue().startswith(b'PK'))
                self.assertRegex(sequence, r'^0001/GPA-KN/[IVX]+/\d{4}$')

    def test_letter_route_generates_downloads_indexes_and_deletes(self):
        generated = BytesIO(b'fake-docx-contract')
        sequence = '0001/GPA-KN/VIII/2026'
        with (
            patch.object(
                daily_report_app,
                'generate_letter_docx',
                return_value=(generated, sequence),
            ),
            patch.object(daily_report_app, 'log_activity') as activity_log,
        ):
            response = self.client.post(
                '/letters/generate',
                json={
                    'letter_type': 'umum',
                    'subject': 'Contract Test Letter',
                    'date': '01 September 2026',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'fake-docx-contract')
        activity_log.assert_called_once()
        index = daily_report_app.get_letters_index('worker')
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]['seq_str'], sequence)
        filename = index[0]['filename']

        response = self.client.get(
            f'/letters/download/{quote(filename, safe="")}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'fake-docx-contract')
        response.close()

        response = self.client.post('/letters/delete', json={'filename': filename})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(daily_report_app.get_letters_index('worker'), [])
        self.assertFalse(
            os.path.exists(os.path.join(daily_report_app.get_letters_dir('worker'), filename))
        )


class FieldSubmissionContractTests(IsolatedDailyAppTestCase):
    def setUp(self):
        super().setUp()
        self.login_as('worker', is_admin=False)

    def test_field_submission_requires_an_area(self):
        response = self.client.post('/field/submit', json={'date': '2026-09-01'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'area_id required')

    def test_field_submission_upserts_the_same_area(self):
        first = {
            'date': '2026-09-01',
            'area_id': 'MA-14',
            'activities_today': ['Initial activity'],
        }
        second = {
            **first,
            'activities_today': ['Updated activity'],
            'remarks': 'Latest field state',
        }

        self.assertEqual(self.client.post('/field/submit', json=first).status_code, 200)
        self.assertEqual(self.client.post('/field/submit', json=second).status_code, 200)
        response = self.client.get('/field/load?date=2026-09-01')

        self.assertEqual(response.status_code, 200)
        submissions = response.get_json()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0]['area_id'], 'MA-14')
        self.assertEqual(submissions[0]['activities_today'], ['Updated activity'])
        self.assertEqual(submissions[0]['remarks'], 'Latest field state')

    def test_admin_merge_preserves_existing_areas_and_creates_draft(self):
        self.login_as('admin', is_admin=True)
        draft_path = daily_report_app.get_draft_file('admin')
        daily_report_app._atomic_write_json(
            draft_path,
            {
                'date': '2026-09-01',
                'areas': [{'id': 'MA-23', 'remarks': 'Keep me'}],
            },
        )
        incoming = [{
            'date': '2026-09-01',
            'area_id': 'MA-14',
            'activities_today': ['Cable termination'],
            'remarks': 'Merged field work',
        }]

        with (
            patch.object(daily_report_app, 'save_draft_snapshot') as snapshot,
            patch.object(daily_report_app, 'log_activity') as activity_log,
        ):
            response = self.client.post(
                '/admin/merge_submissions',
                json={'submissions': incoming},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['merged'], 1)
        with open(draft_path, encoding='utf-8') as draft_file:
            draft = json.load(draft_file)
        areas = {area['id']: area for area in draft['areas']}
        self.assertEqual(set(areas), {'MA-23', 'MA-14'})
        self.assertEqual(areas['MA-23']['remarks'], 'Keep me')
        self.assertEqual(areas['MA-14']['activities_today'], ['Cable termination'])
        snapshot.assert_called_once()
        activity_log.assert_called_once()


if __name__ == '__main__':
    unittest.main()
