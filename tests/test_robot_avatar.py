import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from cos import robot_avatar


class AvatarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'NEO_AVATAR_DIR': self.tmp.name, 'NEO_TAILSCALE_FUNNEL': '1',
                                      'NEO_DASHBOARD_URL': 'https://neo.example:8443'})
        env.start()
        self.addCleanup(env.stop)
        image = Image.new('RGB', (192, 192), (120, 80, 40))
        data = io.BytesIO()
        image.save(data, format='PNG')
        self.photo = data.getvalue()

    def test_composes_personal_photo_and_reuses_private_url(self):
        source = 'https://avatars.slack-edge.com/user/photo.png'
        with patch.object(robot_avatar, '_download', return_value=self.photo) as download:
            url = robot_avatar.url_for('U123', source)
            self.assertEqual(robot_avatar.url_for('U123', source), url)
        download.assert_called_once_with(source)
        self.assertTrue(url.startswith('https://neo.example:8443/neo-avatar/'))
        token = url.rsplit('/', 1)[-1].removesuffix('.png')
        image = Image.open(io.BytesIO(robot_avatar.read(token)))
        self.assertEqual(image.size, (256, 256))
        self.assertEqual(image.convert('RGB').getpixel((128, 128)), (120, 80, 40))
        self.assertNotEqual(image.convert('RGB').getpixel((128, 24)), (120, 80, 40))
        self.assertNotIn('U123', url)
        self.assertEqual(Path(self.tmp.name, 'avatar.key').stat().st_mode & 0o777, 0o600)

    def test_distinct_users_get_distinct_urls(self):
        source = 'https://avatars.slack-edge.com/user/photo.png'
        with patch.object(robot_avatar, '_download', return_value=self.photo):
            self.assertNotEqual(robot_avatar.url_for('U123', source), robot_avatar.url_for('U456', source))

    def test_never_fetches_untrusted_urls_and_refuses_unsafe_paths(self):
        with patch.object(robot_avatar, '_download') as download:
            self.assertIsNone(robot_avatar.url_for('U123', 'https://evil.example/p.png'))
            download.assert_not_called()
        self.assertIsNone(robot_avatar.read('../avatar.key'))
        self.assertIsNone(robot_avatar.read('0' * 64))

    def test_uses_original_photo_when_no_public_base_is_configured(self):
        with patch.dict(os.environ, {'NEO_TAILSCALE_FUNNEL': '0'}, clear=False):
            self.assertIsNone(robot_avatar.url_for('U123', 'https://avatars.slack-edge.com/u.png'))

    def test_simultaneous_processes_use_one_complete_avatar_key(self):
        code = 'import hashlib; from cos.robot_avatar import _key; print(hashlib.sha256(_key()).hexdigest())'
        processes = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for _ in range(5)]
        digests = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error.decode())
            digests.append(output.strip())
        self.assertEqual(len(set(digests)), 1)
        self.assertEqual(len(Path(self.tmp.name, 'avatar.key').read_bytes()), 32)
