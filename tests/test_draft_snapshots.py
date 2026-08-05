import json
import os
import tempfile
import unittest
from unittest.mock import patch

from daily_report_app import app


class DraftSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session['username'] = 'snapshot-test'

    def test_snapshot_list_returns_metadata_objects(self):
        with tempfile.TemporaryDirectory() as user_dir:
            drafts_dir = os.path.join(user_dir, 'drafts')
            os.makedirs(drafts_dir)
            snapshot_path = os.path.join(drafts_dir, '20260722_123456.json')
            with open(snapshot_path, 'w', encoding='utf-8') as snapshot_file:
                json.dump({'date': '2026-07-22'}, snapshot_file)

            # Files outside the timestamp naming scheme are not snapshots.
            with open(os.path.join(drafts_dir, 'notes.json'), 'w', encoding='utf-8') as other_file:
                json.dump({}, other_file)

            with patch('daily_report_app.get_user_dir', return_value=user_dir):
                response = self.client.get('/draft_snapshots')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['filename'], '20260722_123456.json')
        self.assertEqual(payload[0]['ts'], '22 Jul 2026 12:34:56')
        self.assertIn('size_kb', payload[0])

    def test_valid_snapshot_can_be_restored(self):
        snapshot = {'date': '2026-07-22', 'day_no': '47'}
        with tempfile.TemporaryDirectory() as user_dir:
            drafts_dir = os.path.join(user_dir, 'drafts')
            os.makedirs(drafts_dir)
            with open(
                os.path.join(drafts_dir, '20260722_123456.json'),
                'w',
                encoding='utf-8',
            ) as snapshot_file:
                json.dump(snapshot, snapshot_file)

            with patch('daily_report_app.get_user_dir', return_value=user_dir):
                response = self.client.get(
                    '/draft_snapshots/load/20260722_123456.json'
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), snapshot)

    def test_invalid_snapshot_filename_is_rejected(self):
        with tempfile.TemporaryDirectory() as user_dir:
            drafts_dir = os.path.join(user_dir, 'drafts')
            os.makedirs(drafts_dir)
            with open(os.path.join(drafts_dir, 'notes.json'), 'w', encoding='utf-8') as other_file:
                json.dump({'private': True}, other_file)

            with patch('daily_report_app.get_user_dir', return_value=user_dir):
                response = self.client.get('/draft_snapshots/load/notes.json')

        self.assertEqual(response.status_code, 404)

    def test_overall_progress_round_trips_through_draft_storage(self):
        payload = {
            'date': '2026-08-02',
            'section_order': [
                'report_information',
                'indirect_manpower',
                'weather',
                'overall_progress',
                'daily_activities',
                'constraints',
                'remarks',
                'sign_off',
                'photo_documentation',
            ],
            'show_overall_progress': False,
            'photo_documentation_title': (
                'Cold Commissioning Activities - DAY 4 - Turbine & Generator Unit 2'
            ),
            'overall_progress': [{
                'description': 'Commissioning & Hand Over',
                'weight_factor': '5.00%',
                'cumulative_to_date_plan': '5%',
                'cumulative_to_date_actual': '5%',
                'deviation': '0.00%',
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            draft_path = os.path.join(temp_dir, 'draft.json')
            with (
                patch('daily_report_app.get_draft_file', return_value=draft_path),
                patch('daily_report_app.save_draft_snapshot'),
                patch('daily_report_app.log_activity'),
            ):
                save_response = self.client.post('/save_draft', json=payload)
                load_response = self.client.get('/load_draft')

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(load_response.status_code, 200)
        self.assertEqual(load_response.get_json(), payload)


if __name__ == '__main__':
    unittest.main()
