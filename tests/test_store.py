"""Shared JSON store for cos state files."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_missing_file_reads_empty_and_update_persists(self):
        from cos.store import JsonStore
        s = JsonStore("x.json")
        self.assertEqual(s.read(), {})
        s.update(lambda d: d.setdefault("a", 1))
        self.assertEqual(JsonStore("x.json").read(), {"a": 1})

    def test_corrupt_file_reads_empty(self):
        from cos.runner import cos_home
        from cos.store import JsonStore
        (cos_home() / "x.json").write_text("{nope")
        self.assertEqual(JsonStore("x.json").read(), {})


if __name__ == "__main__":
    unittest.main()
