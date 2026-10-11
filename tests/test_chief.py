"""Chief-of-staff jobs: loops from alerts/findings, digest timing."""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ChiefTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_digest_due_once_a_day_after_the_hour(self):
        from chief import digest_due
        self.assertFalse(digest_due(datetime(2026, 10, 1, 18, 59), "19:00", None))
        self.assertTrue(digest_due(datetime(2026, 10, 1, 19, 0), "19:00", None))
        self.assertTrue(digest_due(datetime(2026, 10, 1, 23, 0), "19:00", "2026-09-30"))
        self.assertFalse(digest_due(datetime(2026, 10, 1, 23, 0), "19:00", "2026-10-01"))

    def test_digest_covers_the_whole_day(self):
        from chief import digest_since
        self.assertEqual(digest_since(datetime(2026, 10, 1, 19, 15)), datetime(2026, 10, 1, 0, 0).timestamp())

    def test_alert_opens_one_loop_titled_by_its_first_line(self):
        import chief
        from cos import loops
        chief.alert_loop("C1", "1.1", "\n[FIRING:1] LLMCircuitBreakerOpen\nmore", "https://slack/x")
        chief.alert_loop("C1", "1.1", "[FIRING:1] LLMCircuitBreakerOpen", None)
        rows = loops.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["links"]["source"]), ("[FIRING:1] LLMCircuitBreakerOpen", "https://slack/x"))

    def test_only_serious_findings_open_loops(self):
        import chief
        from cos import loops
        chief.finding_loops([{"id": "a", "severity": "high", "title": "A"}, {"id": "b", "severity": "low", "title": "B"}])
        self.assertEqual([l["title"] for l in loops.listing()], ["A"])


if __name__ == "__main__":
    unittest.main()
