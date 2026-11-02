import unittest
from unittest.mock import patch

from cos import shared_drive


class SharedDriveTest(unittest.TestCase):
    def test_connected_personal_drive_takes_precedence(self):
        with patch.object(shared_drive, 'connector_status', return_value='connected'):
            result = shared_drive.request({'account': 'neo-yosh', 'role': 'full'}, 'read a document')
        self.assertEqual(result['status'], 'refused')
        self.assertIn('own', result['text'])

    def test_missing_personal_drive_uses_owner_connector(self):
        with patch.object(shared_drive, 'connector_status', side_effect=['missing', 'connected']), \
             patch.object(shared_drive, 'owner_account', return_value='neo-anil'), \
             patch.object(shared_drive, 'run_owner', return_value='found document') as run:
            result = shared_drive.request({'account': 'neo-yosh', 'role': 'full'}, 'read a document')
        self.assertEqual(result, {'status': 'ok', 'text': 'found document'})
        run.assert_called_once_with('neo-anil', 'read a document')

    def test_inconclusive_personal_status_does_not_fall_back(self):
        with patch.object(shared_drive, 'connector_status', return_value='unknown'), \
             patch.object(shared_drive, 'run_owner') as run:
            self.assertEqual(shared_drive.request({'account': 'neo-yosh', 'role': 'full'}, 'read')['status'], 'refused')
        run.assert_not_called()

    def test_private_or_restricted_runs_cannot_use_shared_drive(self):
        for meta in ({'account':'neo-yosh','role':'read-only'}, {'account':'neo-yosh','role':'full','lend':False},
                     {'account':'neo-yosh','role':'full','lend':'claude'}, {'account':'neo-yosh','role':'full','kind':'a2a'}):
            with self.subTest(meta=meta), patch.object(shared_drive, 'run_owner') as run:
                self.assertEqual(shared_drive.request(meta, 'read')['status'], 'refused')
                run.assert_not_called()
