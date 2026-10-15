"""A headless run is told which Slack thread it answers in.

Agents register deploy watches (~/.neo/deploy-watch/state.json) with the deploy's slack_channel and
slack_thread. The engine was never told its thread, so it wrote slack_thread null and deploy-watch refused
the registration (deploy-nothread/deskie-web-stage-v3.13.70, 2026-10-13).
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from cos import runner


class RunContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"COS_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    SLACK = {"slack_channel": "C0C2QBJTSCU", "slack_thread": "1791814887.170329", "system_prompt": "BRIEF"}

    def start_turn(self, engine="claude", follow_up=None, run=None, **extra):
        run = run or runner.Run.create(engine, "deploy it", self.tmp.name, "full", source="slack", extra=extra)
        if follow_up:
            run.update_meta(session_id="sess-1", status="done")
        proc = MagicMock(pid=4242)
        with patch.object(runner.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(runner.Run, "_follow", return_value={"status": "done"}):
            run.execute(follow_up)
        cmd, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.cmd = cmd
        system = cmd[cmd.index("--append-system-prompt") + 1] if "--append-system-prompt" in cmd else None
        return run, system, kwargs.get("env")

    def test_claude_follow_up_turn_still_knows_its_thread(self):
        run, _, _ = self.start_turn(**self.SLACK)
        _, system, env = self.start_turn(follow_up="and prod?", run=run)
        self.assertIn("1791814887.170329", system)
        self.assertEqual(env["NEO_SLACK_THREAD"], "1791814887.170329")

    def test_codex_gets_context_in_first_prompt_and_env_on_follow_ups(self):
        run, _, env = self.start_turn(engine="codex", **self.SLACK)
        self.assertIn("1791814887.170329", self.cmd[-1])
        self.assertEqual(env["NEO_SLACK_THREAD"], "1791814887.170329")
        _, _, env = self.start_turn(follow_up="and prod?", run=run)
        self.assertEqual(self.cmd[-1], "and prod?")
        self.assertEqual(env["NEO_SLACK_CHANNEL"], "C0C2QBJTSCU")

    def test_stale_run_id_from_the_bot_is_replaced(self):
        with patch.dict(os.environ, {"NEO_RUN_ID": "old-run"}):
            run, _, env = self.start_turn(**self.SLACK)
        self.assertEqual(env["NEO_RUN_ID"], run.id)

    def test_slack_run_gets_its_thread_in_prompt_and_env(self):
        run, system, env = self.start_turn(slack_channel="C0C2QBJTSCU", slack_thread="1791814887.170329",
                                           system_prompt="BRIEF")
        self.assertTrue(system.startswith("BRIEF"))
        self.assertIn("C0C2QBJTSCU", system)
        self.assertIn("1791814887.170329", system)
        self.assertIn("deploy-watch", system)
        self.assertEqual(env["NEO_SLACK_CHANNEL"], "C0C2QBJTSCU")
        self.assertEqual(env["NEO_SLACK_THREAD"], "1791814887.170329")
        self.assertEqual(env["NEO_RUN_ID"], run.id)
        self.assertEqual(env["PATH"], os.environ["PATH"], "the engine keeps the bot's environment")
        self.assertNotIn("1791814887", run.meta()["system_prompt"], "stored brief stays as written")

    def test_run_without_a_thread_gets_no_slack_context(self):
        with patch.dict(os.environ, {"NEO_SLACK_CHANNEL": "STALE", "NEO_SLACK_THREAD": "1.0"}):
            _, system, env = self.start_turn(system_prompt="BRIEF")
        self.assertEqual(system, "BRIEF")
        self.assertNotIn("NEO_SLACK_CHANNEL", env, "a thread from the parent process must not leak in")
        self.assertNotIn("NEO_SLACK_THREAD", env)

    def test_no_brief_and_no_thread_sends_no_system_prompt(self):
        _, system, _ = self.start_turn()
        self.assertIsNone(system)


if __name__ == "__main__":
    unittest.main()
