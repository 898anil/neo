import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import cli, zeroshot  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(base: Path) -> Path:
    """A checkout whose origin (a bare repo) has main with one commit."""
    git(base, "init", "-q", "--bare", "origin.git")
    repo = base / "app"
    git(base, "clone", "-q", str(base / "origin.git"), str(repo))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=n@x", "-c", "user.name=n", "commit", "-qm", "init")
    git(repo, "push", "-q", "origin", "HEAD:main")
    return repo


def write_events(run_dir: Path, events: list[dict]) -> None:
    run_dir.mkdir(parents=True)
    with closing(sqlite3.connect(run_dir / "runs.sqlite3")) as conn, conn:
        conn.execute("create table v2_run_events (run_id text, sequence integer, event_json text)")
        for i, event in enumerate(events):
            conn.execute("insert into v2_run_events values (?, ?, ?)", ("r1", i, json.dumps(event)))


def verdict(node, value, message):
    return {"kind": "node_completed", "completion": {
        "reference": {"node": node},
        "outcome": {"status": "verifier", "signals": {"verdict": value}, "diagnostic": {"message": message}}}}


class ZeroshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.env = patch.dict(os.environ, {"NEO_JOBS_DIR": str(self.tmp / "neo"), "NEO_ZEROSHOT_BIN": "/bin/zs",
                                         # no global gitignore: the worktree's own exclude must hide the settings
                                         "GIT_CONFIG_GLOBAL": "/dev/null"})
        self.env.start()
        self.calls = []

    def tearDown(self):
        self.env.stop()

    def fake_zeroshot(self, receipt=None, status=None):
        def run(*args, timeout=120):
            self.calls.append(args)
            return receipt if args[0] == "run" else status
        return patch.object(zeroshot, "_zeroshot", side_effect=run)

    def test_start_makes_an_untracked_worktree_and_a_detached_run(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "make add add", "Fix add", slug="fix-add")

        worktree = Path(job["worktree"])
        self.assertEqual(worktree, self.tmp / "app-wt-zs-fix-add")
        self.assertEqual(job["branch"], "neo/zs-fix-add")
        self.assertEqual(job["run_id"], "r1")
        upstream = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "@{u}"],
                                  capture_output=True, text=True)
        self.assertNotEqual(upstream.returncode, 0, "the run branch must not track origin/main")
        args = self.calls[0]
        self.assertEqual(args[0], "run")
        self.assertIn("--detach", args)
        self.assertEqual(args[args.index("--cwd") + 1], str(worktree))
        self.assertEqual(args[args.index("--submission-key") + 1], "neo-fix-add")
        self.assertEqual(zeroshot.load("r1")["slug"], "fix-add")

    def test_claude_gets_shell_access_in_the_worktree_but_not_push_or_merge(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        worktree = Path(job["worktree"])
        settings = json.loads((worktree / ".claude" / "settings.local.json").read_text())
        self.assertIn("Bash", settings["permissions"]["allow"])
        self.assertIn("Bash(git push:*)", settings["permissions"]["deny"])
        self.assertIn("Bash(gh:*)", settings["permissions"]["deny"])
        status = subprocess.run(["git", "-C", str(worktree), "status", "--short"],
                                capture_output=True, text=True).stdout
        self.assertNotIn(".claude", status, "the settings file must never be committed with the change")

    def test_repo_review_standard_is_appended_to_the_task(self):
        repo = make_repo(self.tmp)
        rules = self.tmp / "private" / "zeroshot"
        rules.mkdir(parents=True)
        (rules / "app.md").write_text("Reject any CRITICAL or HIGH finding.")
        with patch.object(zeroshot, "ROOT", self.tmp), self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "make add add", "Fix add", slug="fix-add")
        task = json.loads((zeroshot.job_dir(job["slug"]) / "input.json").read_text())["task"]
        self.assertTrue(task.startswith("make add add"))
        self.assertIn("Reject any CRITICAL or HIGH finding.", task)

    def test_an_existing_job_is_refused_not_overwritten(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
            with self.assertRaises(ValueError):
                zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")

    def test_bad_slug_is_rejected(self):
        with self.assertRaises(ValueError):
            zeroshot.job_dir("../etc")

    def test_status_reports_result_rounds_and_verdicts(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        write_events(zeroshot.state_dir() / "runs" / "r1", [
            {"kind": "node_started"},
            verdict("acceptance", "rejected", "tests not run"),
            verdict("code", "accepted", "fine"),
            verdict("acceptance", "accepted", "ran 3 tests"),
            verdict("code", "accepted", "fine"),
        ])
        finished = {"status": {"phase": "finished", "terminalResult": {"status": "succeeded", "output": None}}}
        with self.fake_zeroshot(status=finished):
            st = zeroshot.status("fix-add")
        self.assertTrue(st["finished"])
        self.assertEqual(st["result"], "succeeded")
        self.assertEqual(st["rounds"], 2)
        self.assertEqual([v["verdict"] for v in st["verdicts"]], ["rejected", "accepted", "accepted", "accepted"])
        self.assertEqual(st["verdicts"][0]["feedback"], "tests not run")

    def test_running_status_is_not_finished(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        running = {"status": {"phase": "running", "activeExecutions": [{"node": "worker"}]}}
        with self.fake_zeroshot(status=running):
            st = zeroshot.status("fix-add")
            self.assertFalse(st["finished"])
            self.assertEqual(st["active"], ["worker"])
            self.assertEqual(st["verdicts"], [])
            with patch.object(zeroshot.time, "sleep"):
                self.assertFalse(zeroshot.wait("fix-add", timeout=0)["finished"])

    def test_long_titles_make_a_valid_slug(self):
        slug = zeroshot.slugify("Fix the authentication token refresh race in websocket handler")
        self.assertLessEqual(len(slug), 48)
        zeroshot.job_dir(slug)  # does not raise

    def test_base_that_looks_like_an_option_is_refused_before_git_runs(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}), patch.object(zeroshot, "_git") as git_:
            for base in ("--upload-pack=touch PWNED", "main..x", ""):
                with self.assertRaises(ValueError):
                    zeroshot.start(str(repo), "task", "Fix add", base=base, slug="fix-add")
        git_.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_title_is_passed_so_it_cannot_become_a_flag(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            zeroshot.start(str(repo), "task", "--ship", slug="fix-add")
        self.assertIn("--title=--ship", self.calls[0])
        self.assertNotIn("--ship", self.calls[0])

    def test_failed_submission_can_be_retried_with_the_same_slug(self):
        repo = make_repo(self.tmp)
        with patch.object(zeroshot, "_zeroshot", side_effect=RuntimeError("controller not ready")):
            with self.assertRaises(RuntimeError):
                zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        self.assertIsNone(zeroshot.load("fix-add")["run_id"])
        with self.assertRaises(RuntimeError):
            zeroshot.status("fix-add")
        with self.fake_zeroshot(receipt={"runId": "r2"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        self.assertEqual(job["run_id"], "r2")
        self.assertEqual(self.calls[0][self.calls[0].index("--submission-key") + 1], "neo-fix-add")

    def test_settings_file_tracked_by_the_repo_is_refused(self):
        repo = make_repo(self.tmp)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}")
        git(repo, "add", "-f", ".claude/settings.local.json")
        git(repo, "-c", "user.email=n@x", "-c", "user.name=n", "commit", "-qm", "settings")
        with self.fake_zeroshot(receipt={"runId": "r1"}), self.assertRaises(ValueError):
            zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")

    def test_agents_cannot_rewrite_their_permissions_or_reach_push_through_a_shell(self):
        deny = zeroshot.CLAUDE_SETTINGS["permissions"]["deny"]
        for rule in ("Edit(.claude/**)", "Write(.claude/**)", "Bash(bash:*)", "Bash(sh:*)", "Bash(gh:*)",
                     "Bash(kubectl:*)", "Bash(gcloud:*)"):
            self.assertIn(rule, deny)

    def test_status_lists_commits_the_worker_made(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        worktree = Path(job["worktree"])
        (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(worktree, "-c", "user.email=n@x", "-c", "user.name=n", "commit", "-qam", "fix add")
        with self.fake_zeroshot(status={"status": {"phase": "finished", "terminalResult": {"status": "succeeded"}}}):
            st = zeroshot.status("fix-add")
        self.assertEqual(st["changed_files"], [])
        self.assertEqual(len(st["commits"]), 1)
        self.assertIn("fix add", st["commits"][0])

    def test_clean_removes_a_finished_job_but_keeps_uncommitted_work_unless_forced(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        worktree = Path(job["worktree"])
        (worktree / "calc.py").write_text("changed\n")
        finished = {"status": {"phase": "finished", "terminalResult": {"status": "succeeded"}}}
        with self.fake_zeroshot(status=finished):
            with self.assertRaises(ValueError):
                zeroshot.clean("fix-add")
            self.assertTrue(worktree.exists())
            zeroshot.clean("fix-add", force=True)
        self.assertFalse(worktree.exists())
        self.assertFalse(zeroshot.job_dir("fix-add").exists())
        branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "neo/zs-fix-add"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "")

    def test_clean_refuses_commits_no_remote_has(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        worktree = Path(job["worktree"])
        (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(worktree, "-c", "user.email=n@x", "-c", "user.name=n", "commit", "-qam", "fix add")
        finished = {"status": {"phase": "finished", "terminalResult": {"status": "succeeded"}}}
        with self.fake_zeroshot(status=finished):
            with self.assertRaises(ValueError) as ctx:
                zeroshot.clean("fix-add")
            self.assertIn("unpushed", str(ctx.exception))
            self.assertTrue(worktree.exists())
            git(worktree, "push", "-q", "origin", "neo/zs-fix-add")
            zeroshot.clean("fix-add")
        self.assertFalse(worktree.exists())

    def test_clean_only_deletes_neo_branches(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        job["branch"] = "main"
        zeroshot._save(job)
        with self.assertRaises(ValueError):
            zeroshot.clean("fix-add", force=True)

    def test_failed_setup_leaves_nothing_behind(self):
        repo = make_repo(self.tmp)
        with patch.object(zeroshot, "_allow_claude_tools", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        self.assertFalse((self.tmp / "app-wt-zs-fix-add").exists())
        self.assertFalse(zeroshot.job_dir("fix-add").exists())
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            self.assertEqual(zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")["run_id"], "r1")

    def test_retry_for_another_repo_is_refused(self):
        repo = make_repo(self.tmp)
        with patch.object(zeroshot, "_zeroshot", side_effect=RuntimeError("controller not ready")):
            with self.assertRaises(RuntimeError):
                zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        other = self.tmp / "other"
        other.mkdir()
        other = make_repo(other)
        with self.fake_zeroshot(receipt={"runId": "r2"}), self.assertRaises(ValueError):
            zeroshot.start(str(other), "task", "Fix add", slug="fix-add")

    def test_clean_force_skips_only_a_run_zeroshot_forgot(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            job = zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        with patch.object(zeroshot, "_zeroshot", side_effect=RuntimeError("zeroshot status failed: database is locked")):
            with self.assertRaises(RuntimeError):
                zeroshot.clean("fix-add", force=True)
        self.assertTrue(Path(job["worktree"]).exists())
        with patch.object(zeroshot, "_zeroshot", side_effect=RuntimeError("zeroshot status failed: run r1 was not found")):
            zeroshot.clean("fix-add", force=True)
        self.assertFalse(Path(job["worktree"]).exists())

    def test_shell_note_names_every_blocked_prefix(self):
        for prefix in ("git push", "git -C", "bash", "xargs", "kubectl"):
            self.assertIn(prefix, zeroshot.SHELL_NOTE)

    def test_clean_refuses_a_running_job(self):
        repo = make_repo(self.tmp)
        with self.fake_zeroshot(receipt={"runId": "r1"}):
            zeroshot.start(str(repo), "task", "Fix add", slug="fix-add")
        with self.fake_zeroshot(status={"status": {"phase": "running"}}), self.assertRaises(ValueError):
            zeroshot.clean("fix-add")

    def test_cli_timeouts_exit_3_not_a_traceback(self):
        with patch.object(zeroshot, "status", side_effect=subprocess.TimeoutExpired("zeroshot", 120)):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["zeroshot", "status", "fix-add"]), 3)

    def test_cli_exits_3_when_zeroshot_fails(self):
        with patch.object(zeroshot, "load", side_effect=RuntimeError("zeroshot status failed: boom")):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli.main(["zeroshot", "status", "fix-add"])
        self.assertEqual(code, 3)
        self.assertIn("boom", err.getvalue())

    def test_cli_unknown_job_is_bad_input(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["zeroshot", "status", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
