"""One person's CoS asks another's through the broker (cos/broker.py)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import accounts, broker, people  # noqa: E402
from cos.runner import Run  # noqa: E402

OWNER, AJAY, YOSH = "UOWNER1", "UAJAY01", "UYOSH01"
TRIED = "read the deploy workflow and the last three CI runs; no failure there"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_MEMORY_DIR": str(self.dir / "memory"),
                                           "NEO_OWNER_USER": OWNER, "NEO_OWNER_NAME": "Anil"})
        self.env.start()
        os.environ.pop("NEO_ACCOUNTS", None)
        accounts._save_registry({AJAY: "neo-ajay", YOSH: "neo-yosh"})
        people.save(AJAY, AJAY, name="Ajay", repos="backie")
        people.save(YOSH, YOSH, name="Yosh", repos="deskie")

    def tearDown(self):
        self.env.stop()

    def run_of(self, user, **extra):
        return Run.create("claude", "task", str(self.dir), "full", extra={"slack_user": user, "slack_channel": "C1",
                                                                          "slack_thread": "1.1", **extra})

    def ask(self, decision, answer="", to="anil", user=AJAY, tried=TRIED):
        run = self.run_of(user)
        with patch.object(broker, "answer", return_value={"decision": decision, "answer": answer, "reason": "policy", "run": "r"}):
            return broker.ask(run.id, "why did last night's deploy fail?", tried, to)


class TokenTest(Base):
    def test_a_run_asks_only_as_itself(self):
        run = self.run_of(AJAY)
        self.assertTrue(broker.verify(run.id, broker.token(run.id)))
        self.assertFalse(broker.verify(run.id, broker.token("20260101-000000-abcdef")))
        self.assertFalse(broker.verify(run.id, ""))
        self.assertEqual(oct((self.dir / "a2a.key").stat().st_mode & 0o777), "0o600")

    def test_an_answering_run_gets_no_token(self):
        from cos.runner import engine_env
        with patch.dict(os.environ, {"NEO_DASHBOARD_PORT": "8788"}):
            asking = engine_env(self.run_of(AJAY).meta())
            answering = engine_env(self.run_of(OWNER, kind="a2a-answer").meta())
        self.assertEqual(asking["NEO_A2A_URL"], "http://127.0.0.1:8788")
        self.assertNotIn("NEO_A2A_TOKEN", answering)


class RefusalTest(Base):
    def test_try_yourself_first(self):
        out = self.ask("answer", "x", tried="nothing")
        self.assertEqual(out["status"], "refused")
        self.assertIn("yourself first", out["text"])

    def test_no_asking_while_answering(self):
        run = self.run_of(OWNER, kind="a2a-answer")
        self.assertEqual(broker.ask(run.id, "q", TRIED)["status"], "refused")

    def test_a_daily_limit(self):
        with patch.object(broker, "asks_today", return_value=broker.ASKS_PER_DAY):
            self.assertEqual(self.ask("answer", "x")["status"], "refused")

    def test_unknown_names_and_blocked_people(self):
        self.assertIn("don't know anyone", self.ask("answer", "x", to="zed")["text"])
        people.save(YOSH, OWNER, role="blocked")
        self.assertIsNone(broker.route("q", AJAY, "yosh"))


class RouteTest(Base):
    def test_by_name_or_account_never_the_asker(self):
        self.assertEqual(broker.route("q", AJAY, "Yosh"), YOSH)
        self.assertEqual(broker.route("q", AJAY, "neo-yosh"), YOSH)
        self.assertEqual(broker.route("q", AJAY, "anil"), OWNER)
        self.assertIsNone(broker.route("q", AJAY, "ajay"))

    def test_the_models_pick_or_the_owner(self):
        with patch.object(broker, "_pick", return_value=YOSH):
            self.assertEqual(broker.route("deskie build broke", AJAY), YOSH)
        with patch.object(broker, "_pick", return_value=None):
            self.assertEqual(broker.route("deskie build broke", AJAY), OWNER)


class ExchangeTest(Base):
    def test_answered_and_the_owner_is_told(self):
        out = self.ask("answer", "The migration timed out; rerun with a longer lock timeout.")
        self.assertEqual((out["status"], out["by"]), ("answered", "Anil"))
        sent = broker.take_outbox()
        self.assertEqual(sent[0]["user"], OWNER)
        self.assertIn("I answered", sent[0]["text"])
        self.assertEqual(broker.take_outbox(), [])

    def test_declined(self):
        out = self.ask("deny", "", to="yosh")
        self.assertEqual(out["status"], "denied")
        self.assertIn("declined", broker.take_outbox()[0]["text"])

    def test_a_reply_with_a_credential_waits_for_its_owner(self):
        out = self.ask("answer", "use token ghp_abcdefghijklmnopqrstuvwxyz0123")
        self.assertEqual(out["status"], "pending")
        self.assertNotIn("ghp_", out["text"])
        escalation = broker.take_outbox()[0]
        self.assertTrue(escalation["buttons"])
        self.assertEqual(escalation["user"], OWNER)

    def test_only_the_answerers_owner_releases_it(self):
        self.ask("ask_owner", "The deploy failed on the migration step.")
        request = broker.take_outbox()[0]["request"]
        self.assertIsNone(broker.resolve(request, AJAY, True))  # the asker can't release it
        self.assertIsNotNone(broker.resolve(request, OWNER, True))
        delivered = broker.take_outbox()[0]
        self.assertEqual((delivered["user"], delivered["channel"], delivered["thread"]), (AJAY, "C1", "1.1"))
        self.assertIn("migration step", delivered["text"])
        self.assertIsNone(broker.resolve(request, OWNER, True))  # once


class CharterTest(Base):
    def test_each_cos_knows_its_job_and_asks_last(self):
        people.save(AJAY, OWNER, role="prs-only")
        text = broker.charter(AJAY)
        self.assertIn("Ajay's chief of staff", text)
        self.assertIn("never merge", text)
        self.assertIn("Resolve things yourself first", text)
        self.assertIn("neo-ask --tried", text)
        self.assertNotIn("neo-ask", broker.charter(OWNER, may_ask=False))

    def test_policy(self):
        self.assertEqual(broker.policy(AJAY), broker.DEFAULT_POLICY)
        broker.set_policy(AJAY, "Only answer about backie.")
        self.assertEqual(broker.policy(AJAY), "Only answer about backie.")
        broker.set_policy(AJAY, "")
        self.assertEqual(broker.policy(AJAY), broker.DEFAULT_POLICY)


class NeoAskTest(unittest.TestCase):
    def test_outside_a_run_it_says_so(self):
        out = subprocess.run([sys.executable, str(ROOT / "deploy" / "linux" / "neo-ask"), "--tried", "x", "q"],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(out.returncode, 2)
        self.assertIn("only works inside a run", out.stdout)


if __name__ == "__main__":
    unittest.main()
