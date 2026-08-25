import json
import os
import tempfile
import unittest
from unittest.mock import patch

from daily_report_app import DEFAULT_PROJECTS, app, load_config


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, 'app_config.json')
        self.config_patch = patch('daily_report_app.CONFIG_FILE', self.config_path)
        self.config_patch.start()
        self.client = app.test_client()
        self.set_session(is_admin=True)

    def tearDown(self):
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def set_session(self, is_admin):
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = 'admin' if is_admin else 'worker'
            flask_session['is_admin'] = is_admin

    def test_default_config_contains_requested_project_pairs(self):
        self.assertEqual(
            [
                {"title": row["title"], "project_no": row["project_no"]}
                for row in DEFAULT_PROJECTS
            ],
            [
                {
                    'title': 'Electrical Construction and Installation - Manpower Supply',
                    'project_no': '002/KN-GPA/EPC-2K-P2/XI/2025',
                },
                {
                    'title': 'Repair & Services Control Valve & ON OFF Valve',
                    'project_no': 'P01.0825.J075',
                },
                {
                    'title': 'PROJECT REVAMPING PT KERTAS NUSANTARA - REACTIVATION FOR TURBINES AND GENERATORS',
                    'project_no': '001/KN-GPA/EPC-2F-P2/IV/2025',
                },
            ],
        )
        self.assertIn(
            "Electrical Installation and Construction - Manpower Supply",
            DEFAULT_PROJECTS[0]["title_aliases"],
        )
        self.assertIn(
            "RE-ACTIVATION TURBINES AND GENERATORS",
            DEFAULT_PROJECTS[2]["title_aliases"],
        )
        self.assertIn(
            "REACTIVATION FOR TURBINES AND GENERATORS",
            DEFAULT_PROJECTS[2]["title_aliases"],
        )

    def test_existing_default_project_pairs_gain_compatibility_aliases(self):
        legacy_projects = [
            {"title": row["title"], "project_no": row["project_no"]}
            for row in DEFAULT_PROJECTS
        ]
        with open(self.config_path, 'w', encoding='utf-8') as config_file:
            json.dump({'projects': legacy_projects}, config_file)

        projects = load_config()['projects']

        self.assertIn(
            "Electrical Installation and Construction - Manpower Supply",
            projects[0]["title_aliases"],
        )
        self.assertIn(
            "REACTIVATION FOR TURBINES AND GENERATORS",
            projects[2]["title_aliases"],
        )

    def test_project_aliases_and_work_hours_policy_round_trip(self):
        projects = [{
            "title": "Canonical Project",
            "project_no": "P-001",
            "title_aliases": ["Legacy Daily Title"],
            "work_hours_policy": {
                "mode": "elapsed_less_break",
                "break_minutes": 60,
                "deduct_when_elapsed_gte_minutes": 360,
                "allow_overnight": True,
            },
        }]

        response = self.client.post('/save_config', json={'projects': projects})

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        saved = load_config()['projects'][0]
        self.assertEqual(saved['title_aliases'], ["Legacy Daily Title"])
        self.assertEqual(saved['work_hours_policy']['mode'], "elapsed_less_break")
        self.assertEqual(saved['work_hours_policy']['break_minutes'], 60)

    def test_legacy_config_gains_projects_without_losing_old_defaults(self):
        with open(self.config_path, 'w', encoding='utf-8') as config_file:
            json.dump(
                {'project_title': 'Legacy Project', 'project_no': 'LEGACY-001'},
                config_file,
            )

        config = load_config()

        self.assertEqual(config['project_title'], 'Legacy Project')
        self.assertEqual(config['project_no'], 'LEGACY-001')
        self.assertEqual(config['projects'], DEFAULT_PROJECTS)

    def test_legacy_ai_key_is_removed_from_memory_disk_and_config_endpoint(self):
        with open(self.config_path, 'w', encoding='utf-8') as config_file:
            json.dump(
                {
                    'project_title': 'Legacy Project',
                    'project_no': 'LEGACY-001',
                    'ai_api_key': 'sk-ant-must-not-leak',
                },
                config_file,
            )

        config = load_config()

        self.assertNotIn('ai_api_key', config)
        with open(self.config_path, encoding='utf-8') as config_file:
            persisted = json.load(config_file)
        self.assertNotIn('ai_api_key', persisted)

        response = self.client.get('/get_config')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('ai_api_key', response.get_json())
        self.assertNotIn('sk-ant-must-not-leak', response.get_data(as_text=True))

    def test_ai_key_cannot_be_saved_in_application_config(self):
        response = self.client.post(
            '/save_config',
            json={'ai_api_key': 'sk-ant-browser-secret'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('ANTHROPIC_API_KEY', response.get_json()['error'])
        self.assertNotIn('ai_api_key', load_config())

    def test_admin_can_save_same_title_with_different_numbers(self):
        projects = [
            {'title': 'Repeated Title', 'project_no': 'NO-001'},
            {'title': 'Repeated Title', 'project_no': 'NO-002'},
        ]

        response = self.client.post('/save_config', json={'projects': projects})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(load_config()['projects'], projects)

    def test_duplicate_project_pair_is_rejected(self):
        response = self.client.post(
            '/save_config',
            json={
                'projects': [
                    {'title': 'Duplicate', 'project_no': 'NO-001'},
                    {'title': ' duplicate ', 'project_no': 'no-001'},
                ]
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('Duplicate', response.get_json()['error'])

    def test_incomplete_or_invalid_projects_are_rejected(self):
        invalid_values = [
            'not-a-list',
            [{'title': 'Missing number', 'project_no': ''}],
            [{'title': 123, 'project_no': 'NO-001'}],
        ]
        for projects in invalid_values:
            with self.subTest(projects=projects):
                response = self.client.post('/save_config', json={'projects': projects})
                self.assertEqual(response.status_code, 400)

    def test_non_admin_cannot_change_master_projects(self):
        self.set_session(is_admin=False)

        response = self.client.post(
            '/save_config',
            json={'projects': [{'title': 'Unauthorized', 'project_no': 'NO-403'}]},
        )

        self.assertEqual(response.status_code, 403)

    def test_invalid_settings_payload_is_rejected(self):
        response = self.client.post(
            '/save_config',
            data='not-json',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)

    def test_project_titles_are_safely_serialized_into_page(self):
        malicious = '</script><script>alert("project")</script>'
        config = load_config()
        config['projects'] = [{'title': malicious, 'project_no': 'SAFE-001'}]

        with (
            patch('daily_report_app.load_config', return_value=config),
            patch('daily_report_app.get_draft_file', return_value=os.path.join(self.temp_dir.name, 'missing.json')),
        ):
            response = self.client.get('/')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(malicious, html)
        self.assertIn(r'\u003c/script\u003e', html)


if __name__ == '__main__':
    unittest.main()
