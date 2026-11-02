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


class EveryonesPingsTest(unittest.TestCase):
    """Urgent pings and nudges reach each person about their own home, as if each had their own neo."""

    def setUp(self):
        home = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"COS_HOME": home, "NEO_OWNER_USER": "UOWNER1", "NEO_ACCOUNTS": "1",
                                           "NEO_PEOPLE_DIR": home + "/people", "NEO_ACCOUNTS_FILE": home + "/accounts.json"})
        self.env.start()
        from cos import accounts, people
        accounts._save_registry({"UOWNER1": "neo-anil", "UAJAY01": "neo-ajay", "UYOSH01": "neo-yosh"})
        people.save("UYOSH01", "UOWNER1", role="blocked")

    def tearDown(self):
        self.env.stop()

    def test_the_owner_in_their_channel_and_everyone_else_in_their_dm(self):
        import chief
        self.assertEqual(chief.recipients("C1", "<@UOWNER1> "), [(None, "C1", "<@UOWNER1> "), ("UAJAY01", "UAJAY01", "")])
        self.assertEqual(chief.recipients(None, ""), [("UAJAY01", "UAJAY01", "")])  # no owner channel: people still hear

    def test_each_pass_reads_that_persons_own_home(self):
        import asyncio
        import chief

        asked, posted = [], []

        class Client:
            async def chat_postMessage(self, **kw):
                posted.append(kw)

        def home(since, viewer):
            asked.append(viewer)
            return {"regressed": [{"key": "run:x", "title": "Login fix", "at": 1, "regressed_at": 5, "line": "broke again"}],
                    "decide": [], "unfinished": []}

        with patch.object(chief.home, "home", side_effect=home):
            asyncio.run(chief.urgent_pass(Client(), "UAJAY01", "UAJAY01", "", None, catch_up=False))
        self.assertEqual(asked, ["UAJAY01"])
        self.assertEqual([p["channel"] for p in posted], ["UAJAY01"])
        self.assertIn("Login fix", posted[0]["text"])
