import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import commands  # noqa: E402
from cos import cli, pause  # noqa: E402


class PauseStateTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"COS_HOME": self.home})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_not_paused_by_default(self):
        self.assertFalse(pause.is_paused())
        self.assertEqual(pause.describe(), "Notifications are on.")

    def test_timed_pause_ends_by_itself(self):
        pause.pause(3600, now=1000.0)
        self.assertTrue(pause.is_paused(now=1000.0 + 3599))
        self.assertFalse(pause.is_paused(now=1000.0 + 3601))

    def test_pause_until_resumed(self):
        pause.pause()
        self.assertTrue(pause.is_paused(now=10**12))
        self.assertEqual(pause.state()["until"], "forever")
        self.assertTrue(pause.resume())
        self.assertFalse(pause.is_paused())
        self.assertFalse(pause.resume())

    def test_keeps_other_settings(self):
        Path(self.home, "settings.json").write_text(json.dumps({"default_engine": "codex"}))
        pause.pause(60)
        self.assertEqual(json.loads(Path(self.home, "settings.json").read_text())["default_engine"], "codex")

    def test_durations(self):
        self.assertEqual(pause.parse_duration("30m"), 1800)
        self.assertEqual(pause.parse_duration("2h"), 7200)
        self.assertEqual(pause.parse_duration("1d"), 86400)
        for bad in ("0h", "soon", "2w", ""):
            with self.assertRaises(ValueError):
                pause.parse_duration(bad)

    def test_dashboard_apply(self):
        self.assertTrue(pause.apply("8h")["paused"])
        self.assertEqual(pause.apply("forever")["until"], "forever")
        self.assertFalse(pause.apply("on")["paused"])
        with self.assertRaises(ValueError):
            pause.apply("later")

    def test_cli_paused_exit_code_for_jobs(self):
        self.assertEqual(cli.main(["paused", "--quiet"]), 1)
        self.assertEqual(cli.main(["pause", "2h"]), 0)
        self.assertEqual(cli.main(["paused", "--quiet"]), 0)
        self.assertEqual(cli.main(["resume"]), 0)
        self.assertEqual(cli.main(["paused", "--quiet"]), 1)


class HeldMessagesTest(unittest.TestCase):
    def test_take_held_and_put_back(self):
        jobs = Path(tempfile.mkdtemp())
        (jobs / "deploy-watch").mkdir()
        held = jobs / "deploy-watch" / "held.json"
        held.write_text(json.dumps([{"channel": "C1", "thread_ts": "1.2", "text": "regressed"}, {"text": "no channel"}]))
        (jobs / "other").mkdir()
        (jobs / "other" / "held.json").write_text("not json")
        with patch.dict(os.environ, {"NEO_JOBS_DIR": str(jobs)}):
            items = pause.take_held()
            self.assertEqual([(m["channel"], m["text"]) for m in items], [("C1", "regressed")])
            self.assertFalse(held.exists())
            self.assertEqual(pause.take_held(), [])
            self.assertTrue(pause.hold_again(items[0]))  # the post failed
            self.assertEqual(json.loads(held.read_text()),
                             [{"channel": "C1", "thread_ts": "1.2", "text": "regressed", "_attempts": 1}])

    def test_a_half_written_held_file_is_put_back_for_the_next_pass(self):
        jobs = Path(tempfile.mkdtemp())
        (jobs / "w").mkdir()
        held = jobs / "w" / "held.json"
        held.write_text('[{"channel": "C1", "te')
        with patch.dict(os.environ, {"NEO_JOBS_DIR": str(jobs)}):
            self.assertEqual(pause.take_held(), [])
            self.assertTrue(held.exists())
            held.write_text(json.dumps([{"channel": "C1", "text": "done"}]))
            self.assertEqual([m["text"] for m in pause.take_held()], ["done"])

    def test_a_message_that_never_posts_is_dropped(self):
        jobs = Path(tempfile.mkdtemp())
        (jobs / "w").mkdir()
        held = jobs / "w" / "held.json"
        held.write_text(json.dumps([{"channel": "CGONE", "text": "x"}]))
        with patch.dict(os.environ, {"NEO_JOBS_DIR": str(jobs)}):
            for _ in range(pause.MAX_HELD_ATTEMPTS - 1):
                (item,) = pause.take_held()
                self.assertTrue(pause.hold_again(item))
            (item,) = pause.take_held()
            self.assertFalse(pause.hold_again(item))
            self.assertEqual(pause.take_held(), [])


class PauseCommandTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual((commands.parse("pause").command, commands.parse("pause").prompt), ("pause", ""))
        self.assertEqual(commands.parse("pause 2h").prompt, "2h")
        self.assertEqual(commands.parse("pause 2 hours").prompt, "2hours")
        self.assertEqual(pause.parse_duration("2hours"), 7200)
        self.assertEqual(commands.parse("Resume").command, "resume")
        # Anything else after pause/resume is a task for the agent.
        self.assertEqual(commands.parse("pause the deploy").command, "run")
        self.assertEqual(commands.parse("resume the rollout now").command, "run")


class PausedChiefTest(unittest.TestCase):
    """While paused, the urgent pinger posts nothing and marks nothing sent."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_urgent_scheduler_is_silent_while_paused(self):
        import chief

        posted = []

        class Client:
            async def chat_postMessage(self, **kw):
                posted.append(kw)

        built = []
        items = [{"text": "PR 1 regressed", "ledger": "a"}, {"text": "PR 2 regressed", "ledger": "b"}]
        sent = []

        async def run():
            task = asyncio.create_task(chief.urgent_scheduler(Client(), "C1", "", None))
            await asyncio.sleep(0.05)
            self.assertEqual((built, posted, sent), ([], [], []))
            # After the pause, what piled up goes out as one message, not one ping each.
            pause.resume()
            await asyncio.sleep(0.05)
            task.cancel()

        with patch.object(chief, "URGENT_EVERY_SECONDS", 0.01), \
                patch.object(chief.home, "home", side_effect=lambda *a: built.append(a) or {}), \
                patch.object(chief.urgent, "due", side_effect=lambda *a, **k: list(items) if not sent else []), \
                patch.object(chief.urgent, "nudge", return_value=None), \
                patch.object(chief.urgent, "mark_sent", side_effect=sent.extend):
            pause.pause()
            asyncio.run(run())
        self.assertEqual(len(posted), 1)
        self.assertIn("While notifications were paused", posted[0]["text"])
        self.assertEqual(sent, items)


os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")


class PausedBotTest(unittest.TestCase):
    """The bot's own posts are held back; replies to the owner still go out."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def client(self):
        posted = []

        class Client:
            async def chat_postMessage(self, **kw):
                posted.append(kw)
                return {"ts": "1.0"}
        return Client(), posted

    def test_pause_and_resume_commands_answer_in_the_thread(self):
        import app
        client, posted = self.client()
        asyncio.run(app.handle_control(client, "C1", "t1", None, commands.parse("pause 2h")))
        self.assertTrue(pause.is_paused())
        self.assertIn("paused until", posted[-1]["text"])
        asyncio.run(app.handle_control(client, "C1", "t1", None, commands.parse("pause 0h")))
        self.assertIn("pause 30m", posted[-1]["text"])
        asyncio.run(app.handle_control(client, "C1", "t1", None, commands.parse("resume")))
        self.assertFalse(pause.is_paused())
        self.assertIn("back on", posted[-1]["text"])

    def test_watch_findings_not_posted_while_paused(self):
        import app
        client, posted = self.client()

        async def run():
            notify = app.watch_checked(client, asyncio.get_running_loop())
            with patch.object(app, "NOTIFY_CHANNEL", "C1"), patch.object(app.chief, "finding_loops"):
                notify({"id": "w", "name": "Sentry"}, {"status": "done", "id": "r"},
                       [{"severity": "high", "id": "f1", "title": "boom"}])
            await asyncio.sleep(0.05)

        pause.pause()
        asyncio.run(run())
        self.assertEqual(posted, [])

    def test_paused_alert_gives_back_its_rca_slot(self):
        from cos.alerts import AlertGate
        gate = AlertGate(str(Path(tempfile.mkdtemp()) / "gate.json"), daily_max=1)
        msg = {"ts": "1.0", "text": "[Alert] backie 5xx high", "subtype": "bot_message"}
        if gate.admit("C1", msg, msg["text"], "UBOT") is not None:
            self.skipTest("fixture is not an alert candidate")
        gate.release(msg["text"])
        again = {**msg, "ts": "2.0"}
        self.assertIsNone(gate.admit("C1", again, again["text"], "UBOT"))  # not capped, not a repeat

    def test_auto_rca_not_started_while_paused(self):
        import app
        client, posted = self.client()
        pause.pause()
        with patch.object(app.ALERT_GATE, "admit", return_value=None), \
                patch.object(app, "run_headless", side_effect=AssertionError("RCA started")) as started, \
                patch.object(app.chief, "alert_loop") as loop:
            asyncio.run(app.maybe_auto_rca(client, "C1", {"ts": "1.0", "text": "alert"}, "event"))
        started.assert_not_called()
        loop.assert_called_once()  # still on the dashboard
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
