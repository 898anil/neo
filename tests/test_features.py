"""Morning brief, GitHub work, memory search, worktrees, usage."""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        proj = self.home / ".claude" / "projects" / "-work-app"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "cwd": "/work/app", "message": {"content": "Why does the Slack token refresh fail?"}, "timestamp": "2026-09-29T10:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "The Slack token refresh fails because the refresh token was revoked."}]}},
        ]
        (proj / "sess-1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        internal = [{"type": "user", "message": {"content": "Is this shell command read-only? slack token"}}]
        (proj / "sess-2.jsonl").write_text(json.dumps(internal[0]) + "\n")

    def tearDown(self):
        self.env.stop()

    def test_search_finds_both_sides_and_skips_internal_sessions(self):
        from cos import memory
        out = memory.search("slack TOKEN refresh")
        self.assertEqual(len(out["results"]), 1)
        hit = out["results"][0]
        self.assertEqual(hit["session_id"], "sess-1")
        self.assertEqual(hit["cwd"], "/work/app")
        self.assertEqual({h["who"] for h in hit["hits"]}, {"you", "agent"})

    def test_only_transcripts_can_be_opened(self):
        from cos import memory
        self.assertEqual(memory.engine_of(self.home / ".claude" / "projects" / "-work-app" / "sess-1.jsonl"), "claude")
        self.assertIsNone(memory.engine_of(Path("/etc/passwd")))
        self.assertIsNone(memory.engine_of(self.home / ".claude" / "projects" / ".." / "settings.json"))

    def test_short_queries_do_nothing(self):
        from cos import memory
        self.assertEqual(memory.search("ab")["results"], [])

    def test_resume_arguments(self):
        from cos import memory
        self.assertEqual(memory.resume_args("claude", "s1"), ("--resume", "s1"))
        self.assertEqual(memory.resume_args("codex", "t1"), ("resume", "t1"))


class WorkTest(unittest.TestCase):
    def test_pr_parsing(self):
        from cos import work
        node = {"number": 7, "title": "Fix", "url": "https://github.com/o/r/pull/7", "isDraft": False, "updatedAt": "2026-09-29T00:00:00Z",
                "reviewDecision": "APPROVED", "headRefName": "fix", "author": {"login": "a"}, "repository": {"nameWithOwner": "o/r", "name": "r"},
                "additions": 3, "deletions": 2, "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}}
        pr = work._pr(node)
        self.assertEqual((pr["repo"], pr["ci"], pr["review"], pr["size"]), ("o/r", "FAILURE", "APPROVED", 5))
        self.assertIn("gh pr diff 7 -R o/r", work.review_prompt(pr))
        self.assertIn("ask before you push", work.fix_ci_prompt(pr))

    def test_new_worktree(self):
        from cos import work
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        with patch.dict(os.environ, {"HOME": tempfile.mkdtemp()}):
            path, branch = work.new_worktree(repo, "Fix the login test!")
        self.assertTrue(branch.startswith("neo/fix-the-login-test-"))
        self.assertEqual(subprocess.run(["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True).stdout.strip(), branch)
        with self.assertRaises(ValueError):
            work.new_worktree(tempfile.mkdtemp(), "x")


class UsageTest(unittest.TestCase):
    def test_spend_and_limits(self):
        from cos import runner
        home = tempfile.mkdtemp()
        with patch.dict(os.environ, {"COS_HOME": home}):
            run = runner.Run.create("claude", "hi", home, "readonly", extra={"kind": "brief"})
            run.update_meta(cost_usd=1.25)
            runner.save_rate_limits({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1}}})
            u = runner.usage_summary()
        self.assertEqual(u["today_usd"], 1.25)
        self.assertEqual(u["week_by_kind"], {"brief": 1.25})
        self.assertEqual(u["claude_limits"]["windows"]["five_hour"]["used"], 0.4)

    def test_rejected_unexpired_quota_blocks_scheduled_claude_work(self):
        from cos import runner

        home = tempfile.mkdtemp()
        with patch.dict(os.environ, {"COS_HOME": home}):
            runner.save_rate_limits({
                "status": "rejected",
                "unifiedWindows": {
                    "seven_day": {"utilization": 1, "resetsAt": 2_000},
                },
            })
            self.assertEqual(runner.claude_available(now=1_000), (False, 2_000))
            self.assertEqual(runner.claude_available(now=2_000), (True, None))

    def test_latest_of_multiple_exhausted_quota_windows_controls_backoff(self):
        from cos import runner

        home = tempfile.mkdtemp()
        with patch.dict(os.environ, {"COS_HOME": home}):
            runner.save_rate_limits({
                "status": "rejected",
                "unifiedWindows": {
                    "five_hour": {"utilization": 1, "resetsAt": 1_500},
                    "seven_day": {"utilization": 1, "resetsAt": 2_000},
                    "monthly": {"utilization": 0.5, "resetsAt": 3_000},
                },
            })
            self.assertEqual(runner.claude_available(now=1_000), (False, 2_000))
            self.assertEqual(runner.claude_available(now=2_000), (True, None))

    def test_malformed_quota_records_fail_open(self):
        from cos import runner

        home = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {"COS_HOME": str(home)}):
            for record in (
                "[]",
                '{"status": "rejected", "windows": []}',
                '{"status": "rejected", "windows": {"seven_day": []}}',
                '{"status": "rejected", "windows": {"seven_day": {"used": 1, "resets_at": 1e999}}}',
            ):
                (home / "usage.json").write_text(record)
                self.assertEqual(runner.claude_available(now=1_000), (True, None))


class BriefQuotaTest(unittest.TestCase):
    def test_scheduled_brief_skips_when_claude_quota_is_exhausted(self):
        from cos import brief

        with patch.object(brief, "claude_available", return_value=(False, 2_000), create=True), \
             patch.object(brief.Run, "create", side_effect=AssertionError("brief should be skipped")):
            self.assertIsNone(brief.start("schedule"))

    def test_dashboard_brief_still_starts_when_claude_quota_is_exhausted(self):
        from cos import brief

        run = unittest.mock.Mock()
        run.meta.return_value = {"id": "brief"}
        with patch.object(brief, "claude_available", return_value=(False, 2_000), create=True), \
             patch.object(brief, "running", return_value=None), \
             patch.object(brief.Run, "create", return_value=run) as create, \
             patch.object(brief.threading, "Thread") as thread:
            self.assertEqual(brief.start("dashboard"), {"id": "brief"})
        create.assert_called_once()
        thread.return_value.start.assert_called_once()


class CliQuotaTest(unittest.TestCase):
    def test_quota_guard_skips_claude_cli_run_before_creating_it(self):
        from cos import cli

        args = types.SimpleNamespace(
            engine="claude", prompt="check", cwd=".", mode="readonly", debug=False, scheduled=True,
        )
        with patch.object(cli, "claude_available", return_value=(False, 2_000), create=True), \
             patch.object(cli.Run, "create", side_effect=AssertionError("run should be skipped")):
            self.assertEqual(cli.cmd_run(args), 0)

    def test_manual_cli_run_is_not_suppressed_by_scheduled_quota_backoff(self):
        from cos import cli

        args = types.SimpleNamespace(
            engine="claude", prompt="check", cwd=".", mode="readonly", debug=False, scheduled=False,
        )
        run = unittest.mock.Mock(id="manual")
        run.meta.return_value = {"mode": "readonly"}
        run.execute.return_value = {"status": "done", "cost_usd": 0, "last_turn_seconds": 0}
        with patch.object(cli, "claude_available", return_value=(False, 2_000), create=True), \
             patch.object(cli.Run, "create", return_value=run) as create:
            self.assertEqual(cli.cmd_run(args), 0)
        create.assert_called_once()

    def test_scheduler_environment_marks_cli_runs_for_quota_backoff(self):
        from cos import cli

        args = types.SimpleNamespace(
            engine="claude", prompt="check", cwd=".", mode="readonly", debug=False, scheduled=False,
        )
        with patch.dict(os.environ, {"NEO_SCHEDULED": "1"}), \
             patch.object(cli, "claude_available", return_value=(False, 2_000), create=True), \
             patch.object(cli.Run, "create", side_effect=AssertionError("run should be skipped")):
            self.assertEqual(cli.cmd_run(args), 0)


if __name__ == "__main__":
    unittest.main()


class SurvivesRestartTest(unittest.TestCase):
    """An engine keeps running when the bot restarts; the next bot process adopts it."""

    def test_adopt_engine_started_by_previous_process(self):
        from cos import runner
        home = Path(tempfile.mkdtemp())
        fixture = ROOT / "tests" / "fixtures" / "claude_stream.jsonl"
        fake = home / "engine.sh"
        fake.write_text(f"#!/bin/sh\nhead -3 {fixture}\nsleep 1\ntail -n +4 {fixture}\n")
        fake.chmod(0o755)
        with patch.dict(os.environ, {"COS_HOME": str(home)}):
            run = runner.Run.create("claude", "hi", str(home), "readonly")
            raw = run.dir / "raw.jsonl"
            # What execute() does, then the bot "dies" before following it.
            with raw.open("ab") as out:
                proc = subprocess.Popen([str(fake)], stdout=out, stderr=subprocess.DEVNULL, start_new_session=True)
            run.update_meta(status="running", turns=1, pid=proc.pid, raw_offset=0, cost_before_turn=0.0)
            # A new bot process adopts it.
            events = []
            meta = runner.Run(run.id).adopt(events.append)
            proc.wait()
        self.assertEqual(meta["status"], "done")
        self.assertGreater(meta["cost_usd"], 0)
        kinds = [e["kind"] for e in events]
        self.assertIn("tool_call", kinds)
        self.assertEqual(kinds[-1], "result")

    def test_nothing_to_adopt_when_engine_is_gone(self):
        from cos import runner
        home = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {"COS_HOME": str(home)}):
            run = runner.Run.create("claude", "hi", str(home), "readonly")
            run.update_meta(status="running", pid=999999)
            self.assertIsNone(run.adopt())


class SlackRunsOnBoardTest(unittest.TestCase):
    """A task started from a Slack thread is an agent on the board for a day, named after its thread."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.home)})
        self.env.start()
        from cos import fleet
        self.fleet = fleet
        fleet.seen.reset()

    def tearDown(self):
        self.env.stop()

    def meta(self, **kw):
        from cos.runner import now_iso
        return {"id": "20261001-120000-abcdef", "source": "slack", "status": "done", "updated_at": now_iso(), **kw}

    def test_slack_answer_is_idle_dashboard_result_is_to_read(self):
        self.assertEqual(self.fleet.run_attention(self.meta()), ("idle", "Answered in Slack"))
        meta = self.meta(source="dashboard")
        self.assertEqual(self.fleet.run_attention(meta)[0], "review")
        self.fleet.mark_run_seen(meta["id"])
        self.assertEqual(self.fleet.run_attention(meta)[0], "idle")

    def test_running_and_failed(self):
        self.assertEqual(self.fleet.run_attention(self.meta(status="running"))[0], "working")
        self.assertEqual(self.fleet.run_attention(self.meta(status="failed"))[0], "failed")

    def test_running_with_a_dead_engine_is_failed_not_working(self):
        self.assertEqual(self.fleet.run_attention(self.meta(status="running", pid=999999))[0], "failed")
        self.assertEqual(self.fleet.run_attention(self.meta(status="running", pid=os.getpid()))[0], "working")
        self.assertIsNone(self.fleet.run_attention(self.meta(status="running", pid=999999, updated_at="2026-01-01T00:00:00+00:00")))

    def test_an_old_running_run_still_reaches_the_board(self):
        from cos import runner
        old_id = "20200101-000000-aaaaaa"  # created long before the others
        (runner.runs_dir() / old_id).mkdir(parents=True)
        (runner.runs_dir() / old_id / "meta.json").write_text(json.dumps(
            {"id": old_id, "status": "running", "source": "slack", "pid": os.getpid(), "updated_at": runner.now_iso()}))
        newer = []
        for _ in range(3):
            run = runner.Run.create("claude", "hi", str(self.home), "readonly")
            run.update_meta(status="done")
            newer.append(run.id)
        ids = lambda **kw: [m["id"] for m in runner.list_runs(2, **kw)]  # noqa: E731
        self.assertNotIn(old_id, ids())
        self.assertIn(old_id, ids(include_running=True))
        self.assertEqual(ids(include_running=True)[:2], sorted(newer, reverse=True)[:2])
        tasks = {t["id"]: t["attention"] for t in self.fleet._task_rows(runner.list_runs(2, include_running=True))}
        self.assertEqual(tasks[old_id], "working")

    def test_old_and_scheduled_runs_stay_off_the_board(self):
        self.assertIsNone(self.fleet.run_attention(self.meta(updated_at="2026-01-01T00:00:00+00:00")))
        self.assertIsNone(self.fleet.run_attention(self.meta(source="schedule")))

    def test_title_leads_with_the_thread(self):
        prompt = ("Here is the Slack thread so far (for context):\nuser: *[PROD][FIRING]* MessagingConsumerLagHigh (7)\n"
                  "*Summary:* lag\n\nNow respond to this new message directed at you:\ncheck this")
        self.assertEqual(self.fleet.run_title({"prompt": prompt}), "[PROD][FIRING] MessagingConsumerLagHigh (7) · check this")
        self.assertEqual(self.fleet.run_title({"prompt": "fix the <https://x.y/1|bug>"}), "fix the bug")
        self.assertEqual(self.fleet.run_title({"prompt": "x", "title": "Watch: Sentry"}), "Watch: Sentry")
