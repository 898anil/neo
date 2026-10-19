"""Watches (scheduled read-only checks that report findings) and the default-engine setting."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
assert "--json-schema" in args and "--allowedTools" in args, args
assert args[args.index("--permission-mode") + 1] == "default", args
open(sys.argv[0] + ".args", "w").write(json.dumps(args))
answer = {"summary": "Two problems.", "findings": [
    {"key": "API-12", "title": "TypeError in /v1/meetings", "severity": "high", "what": "500s for 40 users",
     "evidence": "212 events", "link": "https://sentry.io/issues/12", "repo": "acme/api",
     "next_step": "Look at meetings.ts"},
    {"key": "worker:timeout", "title": "Worker timeouts", "severity": "low", "what": "a few timeouts",
     "evidence": "3 events", "link": "", "repo": "", "next_step": "Check the queue"}]}
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
print(json.dumps({"type": "result", "subtype": "success", "result": json.dumps(answer),
                  "structured_output": answer, "total_cost_usd": 0.1, "session_id": "s-1"}))
'''


class WatchTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "work").mkdir()
        fake = self.home / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.fake = fake
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.home), "NEO_WORKDIR": str(self.home / "work"),
                                           "CLAUDE_CLI_PATH": str(fake), "NEO_GCP_PROD_PROJECT": "acme-prod",
                                           "NEO_GITHUB_ORG": "acme"})
        self.env.start()
        from cos import watches
        self.watches = watches
        watches.on_checked = None

    def tearDown(self):
        self.env.stop()

    def wait_for_check(self, watch_id):
        from cos.runner import list_runs
        for _ in range(100):
            runs = [m for m in list_runs() if m.get("watch_id") == watch_id]
            if runs and runs[0]["status"] not in ("queued", "running") and "watch_new" in runs[0]:
                return runs[0]
            time.sleep(0.1)
        self.fail("the check did not finish")


class WatchesTest(WatchTestCase):
    def test_built_in_watches_are_created_once(self):
        rows = self.watches.load()
        self.assertEqual([w["id"] for w in rows], ["sentry", "prod-logs", "ci-main"])
        self.assertFalse(rows[2]["enabled"])
        self.watches.delete("ci-main")
        self.assertEqual(len(self.watches.load()), 2)

    def test_save_validates_and_makes_unique_ids(self):
        with self.assertRaises(ValueError):
            self.watches.save({"name": "x", "instructions": "y", "every": 7})
        with self.assertRaises(ValueError):
            self.watches.save({"name": "", "instructions": "y"})
        a = self.watches.save({"name": "Sentry", "instructions": "stage too", "every": 60, "hours": "always"})
        self.assertEqual(a["id"], "sentry-2")
        b = self.watches.save({"id": a["id"], "name": "Stage Sentry", "instructions": "stage", "every": 30, "hours": "work", "enabled": False})
        self.assertEqual((b["id"], b["every"], b["enabled"]), ("sentry-2", 30, False))

    def test_builtin_watches_use_the_configured_project_and_org(self):
        rows = {w["id"]: w for w in self.watches.load()}
        self.assertTrue(rows["prod-logs"]["enabled"])
        self.assertIn("--project acme-prod ", rows["prod-logs"]["instructions"])
        self.assertIn("the acme repositories", rows["ci-main"]["instructions"])

    def test_builtin_watches_without_infrastructure_config_are_generic(self):
        with patch.dict(os.environ, {"NEO_GCP_PROD_PROJECT": "", "NEO_GITHUB_ORG": ""}):
            rows = {w["id"]: w for w in self.watches.templates()}
        self.assertTrue(rows["sentry"]["enabled"])
        self.assertFalse(rows["prod-logs"]["enabled"], "no project: production logs stay off")
        self.assertIn("--project <your-gcp-project> ", rows["prod-logs"]["instructions"])
        self.assertFalse(rows["ci-main"]["enabled"])
        self.assertIn("repositories under the working directory", rows["ci-main"]["instructions"])

    def test_due_respects_hours_interval_and_enabled(self):
        monday_10 = datetime(2026, 9, 28, 10, 0)
        saturday_10 = datetime(2026, 10, 3, 10, 0)
        self.assertEqual({w["id"] for w in self.watches.due(monday_10)}, {"sentry", "prod-logs"})
        self.assertEqual(self.watches.due(saturday_10), [])
        self.assertEqual(self.watches.due(datetime(2026, 9, 28, 21, 0)), [])
        self.watches._update("sentry", last_started_at="2026-09-28T09:00:00")
        self.assertEqual([w["id"] for w in self.watches.due(monday_10)], ["prod-logs"])
        self.assertEqual({w["id"] for w in self.watches.due(datetime(2026, 9, 28, 11, 1))}, {"sentry", "prod-logs"})

    def test_due_at_a_fixed_time_runs_once_a_day(self):
        self.watches.save({"name": "Bugs", "instructions": "open bugs", "every": 1440, "hours": "work", "at": "10:00"})
        due = lambda when: "bugs" in {w["id"] for w in self.watches.due(when)}
        self.assertFalse(due(datetime(2026, 9, 28, 9, 59)))
        self.assertTrue(due(datetime(2026, 9, 28, 10, 0)))
        self.watches._update("bugs", last_started_at="2026-09-28T10:00:20")
        self.assertFalse(due(datetime(2026, 9, 28, 15, 0)))
        self.assertFalse(due(datetime(2026, 9, 29, 9, 59)))  # not 24h-after-last drift
        self.assertTrue(due(datetime(2026, 9, 29, 10, 0)))
        self.watches._update("bugs", last_started_at="2026-10-02T10:00:20")  # Friday
        self.assertFalse(due(datetime(2026, 10, 3, 10, 0)))  # Saturday is outside work hours
        self.assertFalse(due(datetime(2026, 10, 5, 8, 0)))  # Monday before 10:00
        self.assertTrue(due(datetime(2026, 10, 5, 10, 0)))
        self.assertTrue(due(datetime(2026, 10, 6, 14, 0)))  # missed while off: catches up
        with self.assertRaises(ValueError):
            self.watches.save({"id": "bugs", "name": "Bugs", "instructions": "x", "every": 1440, "at": "25:00"})
        self.watches.save({"id": "bugs", "name": "Bugs", "instructions": "x", "every": 1440})
        self.assertEqual(self.watches.get("bugs")["at"], "10:00")  # an edit without "at" keeps it

    def test_check_records_findings_once_and_tells_the_bot(self):
        told = []
        self.watches.on_checked = lambda watch, meta, new: told.append((watch["id"], meta["status"], [f["key"] for f in new]))
        self.watches.start("sentry", "dashboard")
        meta = self.wait_for_check("sentry")
        self.assertEqual(meta["status"], "done")
        self.assertEqual((meta["watch_found"], meta["watch_new"]), (2, 2))
        self.assertEqual(meta["watch_summary"], "Two problems.")
        self.assertEqual(told, [("sentry", "done", ["API-12", "worker:timeout"])])
        # Only reading tools, and Sentry's write tool refused.
        args = json.loads(Path(str(self.fake) + ".args").read_text())
        allowed = args[args.index("--allowedTools") + 1].split(",")
        self.assertIn("mcp__claude_ai_Sentry", allowed)
        self.assertFalse([t for t in allowed if t.startswith("Bash(") and not t.endswith(":*)")])
        self.assertNotIn("Bash", allowed)
        self.assertIn("mcp__claude_ai_Sentry__update_issue", args[args.index("--disallowedTools") + 1])

        rows = self.watches.open_findings()
        self.assertEqual([f["key"] for f in rows], ["API-12", "worker:timeout"])  # high before low
        self.assertIn("Look at meetings.ts", rows[0]["task"])
        self.assertIn("https://sentry.io/issues/12", rows[0]["task"])

        # The same problems again: not new, counted, and the next prompt lists them by key.
        self.assertIn("API-12: TypeError in /v1/meetings", self.watches.prompt(self.watches.get("sentry")))
        self.watches.set_status(rows[1]["id"], "dismissed")
        self.watches._update("sentry", last_started_at=None)
        told.clear()
        self.watches.start("sentry", "schedule")
        time.sleep(0.3)
        self.wait_for_check("sentry")
        for _ in range(50):
            if told:
                break
            time.sleep(0.1)
        self.assertEqual(told[-1][2], [])
        again = self.watches.get_finding(rows[0]["id"])
        self.assertEqual(again["times_seen"], 2)
        self.assertEqual(self.watches.get_finding(rows[1]["id"])["status"], "dismissed")
        self.assertEqual([f["key"] for f in self.watches.open_findings()], ["API-12"])

    def test_unexpected_answer_is_reported_not_crashed(self):
        from cos.runner import Run
        run = Run.create("claude", "x", str(self.home / "work"), "readonly", extra={"kind": "watch", "watch_id": "sentry"})
        (run.dir / "raw.jsonl").write_text(json.dumps({"type": "result", "result": "I could not reach Sentry."}) + "\n")
        run.update_meta(status="done")
        self.assertEqual(self.watches.checked(run), [])
        self.assertIn("expected shape", run.meta()["watch_summary"])

    def test_status_changes(self):
        with self.assertRaises(ValueError):
            self.watches.set_status("nope", "dismissed")


    def test_read_only_tools_cover_common_reads_but_nothing_that_writes(self):
        bash = [t for t in self.watches.READ_ONLY_TOOLS if t.startswith("Bash(")]
        for needed in ("Bash(stat:*)", "Bash(cut:*)", "Bash(gcloud logging read:*)", "Bash(cat:*)"):
            self.assertIn(needed, bash)
        for writer in ("sed", "awk", "tee", "xargs", "rm", "cp", "mv", "cd"):
            self.assertFalse([t for t in bash if t.startswith(f"Bash({writer}")], writer)

    def test_prompt_tells_the_agent_how_to_avoid_refused_commands(self):
        text = self.watches.prompt(self.watches.save({"name": "Logs", "instructions": "check logs"}))
        self.assertIn("one command per Bash call", text)
        self.assertIn("Read tool", text)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"COS_HOME": self.home, "NEO_DEFAULT_ENGINE": "claude"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_dashboard_choice_wins_over_environment(self):
        from cos import settings
        from cos.config import default_engine
        self.assertEqual(default_engine(), "claude")
        settings.save(default_engine="codex")
        self.assertEqual(default_engine(), "codex")
        with self.assertRaises(ValueError):
            settings.save(default_engine="gpt")
        self.assertEqual(default_engine(), "codex")

    def test_slack_grammar(self):
        from commands import parse
        self.assertEqual((parse("default codex").command, parse("default codex").engine), ("default", "codex"))
        self.assertEqual((parse("default").command, parse("default").engine), ("default", None))
        self.assertEqual(parse("default the config to prod").command, "run")
        self.assertEqual(parse("watches").command, "watches")

    def test_extra_args_only_on_first_turn_when_asked(self):
        from cos.runner import build_command
        cmd = build_command("claude", "hi", "readonly", extra_args=["--json-schema", "{}"])
        self.assertEqual(cmd[-4:], ["--json-schema", "{}", "--", "hi"])


if __name__ == "__main__":
    unittest.main()
