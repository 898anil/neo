"""A session that stops on a passing Claude/Codex API error is restarted with "continue"."""

import json
import os
import stat
import tempfile
import subprocess
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cos import runner
from tests import test_bot

API_ERROR = "API Error: Connection lost mid-response. The response above may be incomplete."


def claude_api_error(text=API_ERROR):
    return {"type": "assistant", "isApiErrorMessage": True,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]}}


class TransientErrorTest(unittest.TestCase):
    def test_classifies_errors_seen_in_neo_sessions(self):
        for text in (
            API_ERROR,
            "API Error: The response stopped arriving. The response above may be incomplete.",
            'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}',
            "API Error: 500 Internal server error",
            "stream disconnected before completion: error sending request",
        ):
            self.assertTrue(runner.is_transient_engine_error(text), text)
        for text in (
            "Login expired · Please run /login",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            "API Error: 400 prompt is too long: 210000 tokens > 200000 maximum",
            "You've hit your usage limit",
            "",
            None,
        ):
            self.assertFalse(runner.is_transient_engine_error(text), text)


class EngineErrorTextTest(unittest.TestCase):
    def test_reads_claude_and_codex_error_records(self):
        from cos.interactive import engine_error_text

        self.assertEqual(engine_error_text("claude", claude_api_error()), API_ERROR)
        self.assertIsNone(engine_error_text("claude", {"type": "assistant", "message": {"content": [{"type": "text", "text": API_ERROR}]}}))
        codex = {"type": "event_msg", "payload": {"type": "error", "message": "stream disconnected before completion"}}
        self.assertEqual(engine_error_text("codex", codex), "stream disconnected before completion")
        self.assertIsNone(engine_error_text("codex", {"type": "event_msg", "payload": {"type": "stream_error", "message": "Reconnecting... 1/5"}}))


class LiveAutoRestartTest(test_bot.InteractiveRunTest):
    def setUp(self):
        super().setUp()
        self.delays = patch.object(self.interactive, "AUTO_RESTART_DELAYS", (0.2, 0.2))
        self.delays.start()

    def tearDown(self):
        self.delays.stop()
        super().tearDown()

    def prompts(self):
        return [c[3] for c in self.calls() if c[:2] == ["agent", "prompt"]]

    def wait_for_continues(self, n, timeout=8):
        # The prompt event is recorded before herdr is called: wait on herdr's own call log.
        deadline = time.monotonic() + timeout
        while self.prompts().count("continue") < n and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.prompts().count("continue"), n)

    def start(self, events):
        from cos.herdr import Herdr

        self.set_status("working")
        os.environ["FAKE_TRANSCRIPT"] = str(self.transcript)  # restored by the base tearDown
        irun = self.interactive.InteractiveRun.start(
            "claude", "fix it", str(self.work), "l", "slack", None, Herdr("testsess"), events.append,
        )
        self.runs.append(irun)
        return irun

    def test_api_error_is_continued_then_gives_up(self):
        events = []
        self.start(events)
        self.write_transcript(claude_api_error())
        self.set_status("idle")
        self.wait_for(events, lambda e: (e.get("data") or {}).get("via") == "auto-restart")
        self.wait_for_continues(1)
        notice = [e for e in events if e["title"] == "Auto-restart"][0]
        self.assertIn("1/6", notice["body"])

        # Each restarted turn fails the same way: neo stops after the cap (2 here) and says so.
        def fail_turn():
            self.set_status("working")
            self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "working")
            events.clear()
            self.write_transcript(claude_api_error())
            self.set_status("idle")

        fail_turn()
        self.wait_for(events, lambda e: (e.get("data") or {}).get("via") == "auto-restart")
        fail_turn()
        self.wait_for(events, lambda e: e["title"] == "Auto-restart gave up")
        time.sleep(0.6)
        self.assertEqual(self.prompts().count("continue"), 2)
        self.assertEqual(len([e for e in events if e["title"] == "Auto-restart gave up"]), 1)

    def test_finished_tool_call_resets_the_streak(self):
        events = []
        self.start(events)
        for _ in range(3):  # three recoveries in one task, each followed by real work: never gives up
            events.clear()
            self.write_transcript(claude_api_error())
            self.set_status("idle")
            self.wait_for(events, lambda e: (e.get("data") or {}).get("via") == "auto-restart")
            self.set_status("working")
            self.write_transcript(
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
            )
            self.wait_for(events, lambda e: e["kind"] == "tool_result")
        self.wait_for_continues(3)
        self.assertFalse(any(e["title"] == "Auto-restart gave up" for e in events))

    def test_progress_between_failures_still_hits_the_per_prompt_cap(self):
        events = []
        self.start(events)
        with patch.object(self.interactive, "MAX_AUTO_RESTARTS_PER_PROMPT", 3):
            for _ in range(3):
                self.write_transcript(claude_api_error())
                self.set_status("idle")
                self.wait_for(events, lambda e: (e.get("data") or {}).get("via") == "auto-restart")
                events.clear()
                self.set_status("working")
                self.write_transcript(
                    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
                )
                self.wait_for(events, lambda e: e["kind"] == "tool_result")
            self.write_transcript(claude_api_error())
            self.set_status("idle")
            self.wait_for(events, lambda e: e["title"] == "Auto-restart gave up")
            time.sleep(0.6)
        self.assertEqual(self.prompts().count("continue"), 3)

    def test_login_error_is_left_alone(self):
        events = []
        self.start(events)
        self.write_transcript(claude_api_error("Login expired · Please run /login"))
        self.set_status("idle")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "idle")
        time.sleep(0.8)
        self.assertNotIn("continue", self.prompts())

    def test_agent_that_recovers_by_itself_is_not_nudged(self):
        events = []
        self.start(events)
        self.write_transcript(
            claude_api_error(),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done after all."}]}},
        )
        self.set_status("idle")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "idle")
        time.sleep(0.8)
        self.assertNotIn("continue", self.prompts())


FAKE_FLAKY_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    # Fails its first call (how is set by the "mode" file), then succeeds when resumed.
    import json, os, sys
    state = {state!r}
    count_file = os.path.join(state, "calls")
    n = int(open(count_file).read()) if os.path.exists(count_file) else 0
    open(count_file, "w").write(str(n + 1))
    open(os.path.join(state, "args-%d" % n), "w").write(json.dumps(sys.argv[1:]))
    mode = open(os.path.join(state, "mode")).read().strip() if os.path.exists(os.path.join(state, "mode")) else "drop"
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "s-1"}}))
    if n == 0 or mode == "always":
        if mode == "answered":
            print(json.dumps({{"type": "result", "is_error": False, "session_id": "s-1", "result": "answer", "total_cost_usd": 0}}))
        error = "Failed to authenticate: OAuth session expired" if mode == "auth" else "API Error: Connection lost mid-response."
        print(json.dumps({{"type": "result", "is_error": True, "session_id": "s-1", "result": error, "total_cost_usd": 0}}))
        sys.exit(1)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "session_id": "s-1",
                      "result": "all done", "total_cost_usd": 0.01}}))
    """
)


class HeadlessAutoRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        fake = self.base / "claude"
        fake.write_text(FAKE_FLAKY_CLAUDE.format(python=os.sys.executable, state=str(self.base)))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(fake), "COS_HOME": str(self.base)})
        self.env.start()
        self.delays = patch.object(runner, "AUTO_RESTART_DELAYS", (0.01, 0.01))
        self.delays.start()

    def tearDown(self):
        self.delays.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_failed_turn_is_resumed_with_continue(self):
        run = runner.Run.create("claude", "check prod", str(self.base), "readonly")
        run.update_meta(first_turn_args=["--json-schema", "{}"])
        events = []
        meta = run.execute(on_event=events.append)
        self.assertEqual(meta["status"], "done")
        retry_args = json.loads((self.base / "args-1").read_text())
        self.assertIn("--resume", retry_args)
        self.assertEqual(retry_args[-1], "continue")
        self.assertIn("--json-schema", retry_args, "a retried first turn keeps its output schema")
        self.assertTrue(any(e["title"] == "Auto-restart" for e in events))
        self.assertEqual((self.base / "calls").read_text(), "2")

    def run_with_mode(self, mode):
        (self.base / "mode").write_text(mode)
        run = runner.Run.create("claude", "check prod", str(self.base), "readonly")
        events = []
        return run, run.execute(on_event=events.append), events

    def test_auth_error_is_not_retried(self):
        _, meta, events = self.run_with_mode("auth")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual((self.base / "calls").read_text(), "1")
        self.assertFalse(any(e["title"] == "Auto-restart" for e in events))

    def test_turn_that_already_answered_is_not_retried(self):
        _, meta, _ = self.run_with_mode("answered")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual((self.base / "calls").read_text(), "1")

    def test_gives_up_after_the_cap_and_says_so(self):
        _, meta, events = self.run_with_mode("always")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual((self.base / "calls").read_text(), "3")  # 1 + len(AUTO_RESTART_DELAYS)
        self.assertEqual([e["title"] for e in events].count("Auto-restart gave up"), 1)
        self.assertIsNone(meta.get("retry_owner"))

    def test_waiting_run_stays_busy_and_cancel_ends_the_wait(self):
        from cos import server

        self.delays.stop()
        self.delays = patch.object(runner, "AUTO_RESTART_DELAYS", (30.0,))
        self.delays.start()
        (self.base / "mode").write_text("drop")
        run = runner.Run.create("claude", "check prod", str(self.base), "readonly")
        result = {}
        worker = threading.Thread(target=lambda: result.update(run.execute()))
        worker.start()
        deadline = time.monotonic() + 5
        while not run.meta().get("retry_owner") and time.monotonic() < deadline:
            time.sleep(0.02)
        # While it waits it is "running": the dashboard refuses a second turn, and fresh_meta
        # (another Run object, not in ACTIVE) does not mistake it for a dead engine.
        self.assertEqual(server.fresh_meta(runner.Run(run.id))["status"], "running")
        started = time.monotonic()
        self.assertTrue(runner.Run(run.id).cancel())
        worker.join(timeout=5)
        self.assertLess(time.monotonic() - started, 5, "cancel must cut the 30s wait short")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual((self.base / "calls").read_text(), "1")

    def test_waiting_run_left_by_a_dead_bot_is_closed_out(self):
        from cos import server

        run = runner.Run.create("claude", "check prod", str(self.base), "readonly")
        dead = subprocess.Popen(["true"])
        dead.wait()
        run.update_meta(status="running", pid=None, retry_owner=dead.pid, turns=1)
        self.assertEqual(server.fresh_meta(run)["status"], "failed")

    def test_startup_closes_out_a_wait_left_by_a_dead_bot(self):
        from cos import server

        run = runner.Run.create("claude", "check prod", str(self.base), "readonly")
        dead = subprocess.Popen(["true"])
        dead.wait()
        run.update_meta(status="running", pid=None, retry_owner=dead.pid, turns=1)
        server.adopt_orphans()
        self.assertEqual(run.meta()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
