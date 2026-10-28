"""Decision cards: short summaries of finished runs and agents that never drop a failure."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GOOD = {"did": "Fixed retry in gateway", "result": "worked", "needs_you": "Merge PR #12?",
        "recommend": "Merge", "risk": "", "next": "Waits for you", "links": [], "flags": []}


def ev(kind, body, **data):
    return {"kind": kind, "title": kind, "body": body, "data": data}


class Proc:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_extract_flags_finds_buried_failures(self):
        from cos.cards import extract_flags
        text = "all good\nFAILED tests/test_x.py::test_a - AssertionError\nmore\nPermission denied: /etc\nok"
        flags = extract_flags(text)
        self.assertTrue(any("FAILED tests/test_x.py" in f for f in flags))
        self.assertTrue(any("Permission denied" in f for f in flags))

    def test_extract_flags_dedupes_and_caps(self):
        from cos.cards import MAX_FLAGS, extract_flags
        self.assertEqual(len(extract_flags("Traceback (most recent call last):\n" * 50)), 1)
        self.assertLessEqual(len(extract_flags("\n".join(f"error {i}" for i in range(100)))), MAX_FLAGS)

    def test_event_flags_use_failed_tools_and_agent_words_not_plain_output(self):
        from cos.cards import event_flags
        events = [ev("tool_result", "src/error_handler.py:12", is_error=False),
                  ev("tool_result", "exit code 1\nFAILED test_a", is_error=True),
                  ev("text", "I could not reach Sentry, so impact is unverified.")]
        flags = event_flags(events)
        self.assertFalse(any("error_handler" in f for f in flags))
        self.assertTrue(any("exit code 1" in f for f in flags))
        self.assertTrue(any("could not reach Sentry" in f for f in flags))

    def test_material_skips_debug_and_thinking(self):
        from cos.cards import material
        text = material([ev("debug", "noise"), ev("thinking", "hmm"), ev("text", "Done.")])
        self.assertNotIn("noise", text)
        self.assertIn("Done.", text)

    def test_flags_survive_a_card_that_omits_them(self):
        from cos import cards
        events = [ev("text", "Deployed.\nERROR: migration 0042 failed\nAll else fine."),
                  ev("tool_result", "exit 1: psql: connection refused", is_error=True),
                  ev("text", "x" * 200)]
        with patch.object(cards, "MATERIAL_CHARS", 150), patch.object(cards, "make_card", return_value=dict(GOOD)), \
             patch.object(cards, "load_events", return_value=events):
            card = cards.build({"key": "run:1", "title": "t", "kind": "run", "run_id": "1", "mtime": 5.0})
        self.assertTrue(any("migration 0042 failed" in f for f in card["flags"]))
        self.assertTrue(any("connection refused" in f for f in card["flags"]))
        self.assertEqual(cards.get("run:1")["source_mtime"], 5.0)

    def test_code_flags_do_not_repeat_the_model_and_are_capped(self):
        from cos import cards
        said = "Root cause not found; could not reach Sentry"
        events = [ev("text", said)] + [ev("tool_result", f"exit 1: cmd {i} failed", is_error=True) for i in range(10)]
        events.append(ev("text", "z" * 400))  # the model's window sees only this
        with patch.object(cards, "MATERIAL_CHARS", 300), patch.object(cards, "make_card", return_value={**GOOD, "flags": [said]}), \
             patch.object(cards, "load_events", return_value=events):
            card = cards.build({"key": "run:2", "title": "t", "kind": "run", "run_id": "2", "mtime": 1.0})
        self.assertEqual(card["flags"][0], said)
        self.assertEqual(len(card["flags"]), 1 + cards.EXTRA_FLAGS)

    def test_make_card_returns_none_on_bad_output(self):
        from cos import cards
        with patch("subprocess.run", return_value=Proc("not json")):
            self.assertIsNone(cards.make_card("t", "material"))
        with patch("subprocess.run", return_value=Proc(json.dumps({"type": "result", "structured_output": {"result": "meh"}}))):
            self.assertIsNone(cards.make_card("t", "material"))

    def test_make_card_reads_structured_output(self):
        from cos import cards
        with patch("subprocess.run", return_value=Proc(json.dumps({"type": "result", "structured_output": GOOD}))):
            self.assertEqual(cards.make_card("t", "m")["result"], "worked")

    def test_make_card_skips_when_claude_quota_is_exhausted(self):
        from cos import cards
        with patch.object(cards, "claude_available", return_value=(False, 2_000), create=True), \
             patch("subprocess.run", side_effect=AssertionError("card should be skipped")):
            self.assertIsNone(cards.make_card("t", "material"))

    def test_links_may_be_omitted(self):
        # A required "links" with nothing to put in it made the model write `"links": ,` (invalid
        # JSON): 66 of 68 StructuredOutput parse failures. Optional, and filled in when left out.
        from cos import cards
        self.assertNotIn("links", cards.CARD_SCHEMA["required"])
        no_links = {k: v for k, v in GOOD.items() if k != "links"}
        with patch("subprocess.run", return_value=Proc(json.dumps({"type": "result", "structured_output": no_links}))):
            self.assertEqual(cards.make_card("t", "m")["links"], [])

    def test_pending_skips_fresh_cards_and_working_items(self):
        from cos import cards
        cards.put("run:a", {**GOOD, "source_mtime": 100.0, "made_at": 101.0})
        ov = {"tasks": [
            {"id": "a", "status": "done", "_mtime": 100.0, "attention": "idle"},
            {"id": "b", "status": "done", "_mtime": 200.0, "attention": "review"},
            {"id": "c", "status": "running", "_mtime": 300.0, "attention": "working"},
        ], "agents": [
            {"session": "neo", "pane_id": "p1", "session_id": "s1", "engine": "claude", "attention": "working", "last_active": 50.0, "prompts": 1},
            {"session": "neo", "pane_id": "p2", "session_id": "s2", "engine": "claude", "attention": "review", "last_active": 60.0, "prompts": 1, "title": "x"},
        ]}
        self.assertEqual([t["key"] for t in cards.pending(ov)], ["run:b", "agent:s2"])


    def test_card_call_has_no_tools_settings_or_mcp(self):
        from cos import cards
        seen = {}
        def fake(cmd, **kw):
            seen["cmd"], seen["cwd"] = cmd, kw.get("cwd")
            return Proc(json.dumps({"type": "result", "structured_output": GOOD}))
        with patch("subprocess.run", side_effect=fake):
            cards.make_card("t", "m")
        cmd = seen["cmd"]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
        self.assertTrue(seen["cwd"] and "neo-card" in seen["cwd"])

    def test_buried_agent_words_beat_early_tool_noise(self):
        from cos import cards
        events = [ev("tool_result", f"File has not been read yet {i}", is_error=True) for i in range(3)]
        events += [ev("text", "2 tests failed, skipping migration"), ev("text", "y" * 400)]
        with patch.object(cards, "MATERIAL_CHARS", 300), patch.object(cards, "make_card", return_value=dict(GOOD)), \
             patch.object(cards, "load_events", return_value=events):
            card = cards.build({"key": "run:3", "title": "t", "kind": "run", "run_id": "3", "mtime": 1.0})
        self.assertIn("2 tests failed, skipping migration", card["flags"][0])

    def test_tool_failures_the_model_saw_are_not_added(self):
        from cos import cards
        events = [ev("text", "start"), ev("tool_result", "exit 1: grep found nothing", is_error=True)]
        with patch.object(cards, "make_card", return_value=dict(GOOD)), patch.object(cards, "load_events", return_value=events):
            card = cards.build({"key": "run:4", "title": "t", "kind": "run", "run_id": "4", "mtime": 1.0})
        self.assertEqual(card["flags"], [])

if __name__ == "__main__":
    unittest.main()
