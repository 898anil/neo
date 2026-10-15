"""Stop hook: a neo session may not go idle without saying where it stands."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cos import conclusion  # noqa: E402
from cos.runner import build_command  # noqa: E402


class ConclusionTest(unittest.TestCase):
    def test_status_lines_in_the_shapes_agents_write_them(self):
        for text in ("Merged.\n\nStatus: done — merged #12", "*Status:* needs-you — pick A or B", "**Status**: waiting on CI",
                     "Status - blocked: no prod access", "> Status: needs you — approve the promote", "status: FAILED — tests red",
                     "## Status: done", "Status: ✅ done — merged", "Status: ⚠️ blocked — no access", "Answer.\n\nStatus: answered — see above\n"):
            self.assertTrue(conclusion.concluded(text), text)

    def test_status_of_gives_state_and_line(self):
        self.assertEqual(conclusion.status_of("x\n*Status:* needs you - pick A or B"), {"state": "needs-you", "line": "pick A or B"})
        self.assertEqual(conclusion.status_of("Status: done — merged #12"), {"state": "done", "line": "merged #12"})
        self.assertIsNone(conclusion.status_of("Let me look."))
        self.assertIsNone(conclusion.status_of("The hook wants:\n```\nStatus: blocked — no access\n```"))
        self.assertIsNone(conclusion.status_of("Status: done-ish, mostly"))
        self.assertEqual(conclusion.status_of("Status: done."), {"state": "done", "line": ""})

    def test_plans_and_mentions_are_not_a_conclusion(self):
        for text in ("", "Next I'll check the deploy.", "The status: field is wrong", "Status of the deploy: done soon",
                     "I checked the job status", "Status:\n\nDone with prep, next I'll deploy",
                     "- Status: done\n- next I'll merge\n- then tag\n- then deploy"):
            self.assertFalse(conclusion.concluded(text), text)

    def test_blocks_once_then_always_lets_the_stop_through(self):
        out = conclusion.decide({"last_assistant_message": "Let me look at the logs."})
        self.assertEqual(out["decision"], "block")
        self.assertIn("Status:", out["reason"])
        self.assertIn("repeat your complete", out["reason"])  # Slack posts only the last message
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

    def test_only_claude_agent_runs_get_the_hook(self):
        cmd = build_command("claude", "hi", "full", conclude=True)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertIn("conclusion.py", settings["hooks"]["Stop"][0]["hooks"][0]["command"])
        # scheduled jobs (brief, watches, loops, mentions) and codex: no hook
        self.assertNotIn("--settings", build_command("claude", "hi", "full", extra_args=["--json-schema", "{}"]))
        self.assertNotIn("--settings", build_command("codex", "hi", "full", conclude=True))

    def test_hook_command_runs_from_a_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "has space" / "conclusion.py"
            spaced.parent.mkdir()
            shutil.copy(conclusion.__file__, spaced)
            with mock.patch.object(conclusion, "__file__", str(spaced)):
                command = json.loads(conclusion.settings_json())["hooks"]["Stop"][0]["hooks"][0]["command"]
            p = subprocess.run(["sh", "-c", command], input=json.dumps({"last_assistant_message": "Next I'll merge."}),
                               capture_output=True, text=True, timeout=10)
            self.assertEqual((p.returncode, json.loads(p.stdout)["decision"]), (0, "block"))


if __name__ == "__main__":
    unittest.main()
