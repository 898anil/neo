"""Stop hook: a neo session may not go idle without saying where it stands."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cos import conclusion  # noqa: E402
from cos.runner import build_command  # noqa: E402


class ConclusionTest(unittest.TestCase):
    def test_status_lines_in_the_shapes_agents_write_them(self):
        for text in ("Merged.\n\nStatus: done — merged #12", "*Status:* needs-you — pick A or B", "**Status**: waiting on CI",
                     "Status - blocked: no prod access", "> Status: needs you — approve the promote", "status: FAILED — tests red"):
            self.assertTrue(conclusion.concluded(text), text)

    def test_plans_and_mentions_are_not_a_conclusion(self):
        for text in ("", "Next I'll check the deploy.", "The status: field is wrong", "Status of the deploy: done soon",
                     "I checked the job status"):
            self.assertFalse(conclusion.concluded(text), text)

    def test_blocks_once_then_always_lets_the_stop_through(self):
        out = conclusion.decide({"last_assistant_message": "Let me look at the logs."})
        self.assertEqual(out["decision"], "block")
        self.assertIn("Status:", out["reason"])
        self.assertIsNone(conclusion.decide({"last_assistant_message": "Let me look.", "stop_hook_active": True}))
        self.assertIsNone(conclusion.decide({"last_assistant_message": "Started CI.", "background_tasks": [{"id": "b1"}]}))
        self.assertIsNone(conclusion.decide({"last_assistant_message": "Status: done — shipped"}))

    def test_script_blocks_on_no_conclusion_and_never_fails_on_bad_input(self):
        script = str(Path(conclusion.__file__).resolve())
        run = lambda stdin: subprocess.run([sys.executable, script], input=stdin, capture_output=True, text=True, timeout=10)  # noqa: E731
        p = run(json.dumps({"last_assistant_message": "Next I'll merge it."}))
        self.assertEqual((p.returncode, json.loads(p.stdout)["decision"]), (0, "block"))
        p = run("not json")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_claude_runs_get_the_hook_and_codex_runs_do_not(self):
        cmd = build_command("claude", "hi", "full")
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertIn("conclusion.py", settings["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("--settings", build_command("codex", "hi", "full"))


if __name__ == "__main__":
    unittest.main()
