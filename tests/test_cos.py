import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import runner  # noqa: E402
from cos.events import normalize  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def load(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def flatten(engine, name):
    return [e for raw in load(name) for e in normalize(engine, raw)]


class ClaudeNormalizerTest(unittest.TestCase):
    def setUp(self):
        self.events = flatten("claude", "claude_stream.jsonl")
        self.kinds = [e["kind"] for e in self.events if e["kind"] != "debug"]

    def test_captures_every_step_in_order(self):
        self.assertEqual(
            self.kinds,
            ["session", "tool_call", "tool_result", "tool_call", "tool_result", "thinking", "text", "result"],
        )

    def test_tool_call_shows_what_ran(self):
        calls = [e for e in self.events if e["kind"] == "tool_call"]
        self.assertEqual([c["title"] for c in calls], ["Read", "Bash"])
        self.assertEqual(calls[1]["body"], "ls")

    def test_result_carries_cost_and_session(self):
        result = [e for e in self.events if e["kind"] == "result"][0]
        self.assertGreater(result["data"]["cost_usd"], 0)
        self.assertTrue(result["data"]["session_id"])


class CodexNormalizerTest(unittest.TestCase):
    def setUp(self):
        self.events = flatten("codex", "codex_synthetic.jsonl")

    def test_captures_every_step_in_order(self):
        kinds = [e["kind"] for e in self.events if e["kind"] != "debug"]
        self.assertEqual(
            kinds,
            ["session", "thinking", "tool_call", "tool_result", "tool_call", "tool_result",
             "tool_result", "plan", "text", "usage"],
        )

    def test_failed_command_is_marked_error(self):
        results = [e for e in self.events if e["kind"] == "tool_result" and "exit_code" in e["data"]]
        self.assertFalse(results[0]["data"]["is_error"])
        self.assertTrue(results[1]["data"]["is_error"])
        self.assertIn("No such file", results[1]["body"])

    def test_unknown_events_are_kept_as_debug(self):
        events = normalize("codex", {"type": "something.new", "x": 1})
        self.assertEqual(events[0]["kind"], "debug")
        self.assertEqual(events[0]["data"]["raw"]["x"], 1)


class RealCodexStreamTest(unittest.TestCase):
    """Captured from a real `codex exec --json` run of this bot."""

    def test_real_stream(self):
        events = flatten("codex", "codex_stream_real.jsonl")
        kinds = [e["kind"] for e in events if e["kind"] != "debug"]
        self.assertEqual(kinds, ["session", "text", "tool_call", "tool_result", "text", "usage"])
        call = next(e for e in events if e["kind"] == "tool_call")
        self.assertIn("sed -n", call["body"])


class CommandTest(unittest.TestCase):
    def test_claude_resume(self):
        cmd = runner.build_command("claude", "hi", "edit", "sess-1")
        self.assertIn("--resume", cmd)
        self.assertIn("acceptEdits", cmd)

    def test_codex_readonly_and_resume(self):
        self.assertIn("read-only", runner.build_command("codex", "hi", "readonly"))
        cmd = runner.build_command("codex", "hi", "readonly", "thread-1")
        self.assertEqual(cmd[1:3], ["exec", "resume"])
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertEqual(cmd[-2:], ["thread-1", "hi"])

    def test_codex_full_keeps_original_bypass_flag(self):
        cmd = runner.build_command("codex", "hi", "full", "thread-1")
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("-s", cmd)

    def test_model_override(self):
        os.environ["CODEX_MODEL"] = "gpt-x"
        try:
            cmd = runner.build_command("codex", "hi", "edit")
        finally:
            del os.environ["CODEX_MODEL"]
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-x")
        self.assertEqual(cmd[-1], "hi")

    def test_validation(self):
        self.assertIsNotNone(runner.validate_request("gemini", "hi", "/", "readonly"))
        self.assertIsNotNone(runner.validate_request("claude", " ", "/", "readonly"))
        self.assertIsNotNone(runner.validate_request("claude", "hi", "/nope/nope", "readonly"))
        self.assertIsNone(runner.validate_request("claude", "hi", "/", "readonly"))

    def test_run_id_rejects_traversal(self):
        self.assertFalse(runner.valid_run_id("../etc"))
        with self.assertRaises(ValueError):
            runner.Run("../../x")


class RunnerEndToEndTest(unittest.TestCase):
    """Runs the real runner against a fake `claude` binary that replays the fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        bin_dir = home / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(f"#!/bin/sh\ncat {FIXTURES / 'claude_stream.jsonl'}\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.old = {k: os.environ.get(k) for k in ("PATH", "COS_HOME")}
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        os.environ["COS_HOME"] = str(home)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_run_records_timeline_and_meta(self):
        run = runner.Run.create("claude", "read a.txt", self.tmp.name, "readonly")
        seen = []
        meta = run.execute(on_event=seen.append)
        self.assertEqual(meta["status"], "done")
        self.assertTrue(meta["session_id"])
        self.assertGreater(meta["cost_usd"], 0)
        stored = [json.loads(l) for l in (run.dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(len(stored), len(seen))
        self.assertEqual([e["seq"] for e in stored], list(range(len(stored))))
        self.assertEqual(stored[0]["kind"], "prompt")
        self.assertEqual(runner.list_runs()[0]["id"], run.id)

    def test_follow_up_is_a_second_turn(self):
        run = runner.Run.create("claude", "first", self.tmp.name, "readonly")
        run.execute()
        meta = run.execute("second")
        self.assertEqual(meta["turns"], 2)
        turns = {json.loads(l)["turn"] for l in (run.dir / "events.jsonl").read_text().splitlines()}
        self.assertEqual(turns, {1, 2})


if __name__ == "__main__":
    unittest.main()
