import asyncio
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import commands  # noqa: E402
import sessions  # noqa: E402
from cos.events import normalize_claude_transcript, normalize_codex_transcript  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_plain_task_keeps_text(self):
        req = commands.parse("why is the build red?\nsee logs")
        self.assertEqual((req.command, req.engine, req.live), ("run", None, False))
        self.assertEqual(req.prompt, "why is the build red?\nsee logs")

    def test_options_in_any_order(self):
        req = commands.parse("live claude cwd=~/code/app fix the flaky test")
        self.assertEqual(req.engine, "claude")
        self.assertTrue(req.live)
        self.assertEqual(req.cwd, os.path.expanduser("~/code/app"))
        self.assertEqual(req.prompt, "fix the flaky test")

    def test_engine_word_later_in_text_is_prompt(self):
        req = commands.parse("explain how codex exec works")
        self.assertIsNone(req.engine)
        self.assertEqual(req.prompt, "explain how codex exec works")

    def test_controls(self):
        self.assertEqual(commands.parse("stop").command, "stop")
        self.assertEqual(commands.parse("Approve").command, "approve")
        self.assertEqual(commands.parse("keys 2 enter").keys, ["2", "enter"])
        self.assertEqual(commands.parse("").command, "help")
        # A control word followed by more text is a task, not a command.
        self.assertEqual(commands.parse("stop the deploy script").command, "run")

    def test_options_only_means_no_prompt(self):
        self.assertEqual(commands.parse("live claude").prompt, "")


class CleanupTest(unittest.TestCase):
    def test_shell_wrapper_is_removed(self):
        from cos.events import _unwrap_shell
        self.assertEqual(_unwrap_shell("/bin/zsh -lc \"sed -n '1,240p' a.md\""), "sed -n '1,240p' a.md")
        self.assertEqual(_unwrap_shell("/bin/bash -lc 'ls -la'"), "ls -la")
        self.assertEqual(_unwrap_shell("git status"), "git status")


class ProgressTest(unittest.TestCase):
    def test_render_steps_errors_and_link(self):
        p = commands.Progress("claude", "headless", "full", "20260929-120000-abcdef", "http://mini:8788")
        p.add({"kind": "tool_call", "title": "Bash", "body": "ls `-la`", "data": {}})
        p.add({"kind": "tool_result", "title": "Tool error", "body": "boom", "data": {"is_error": True}})
        p.add({"kind": "text", "title": "Claude", "body": "Looking at it", "data": {}})
        p.add({"kind": "result", "title": "Final result", "body": "All good", "data": {"cost_usd": 0.25}})
        out = p.render("done")
        self.assertIn(":white_check_mark: *claude*", out)
        self.assertIn("1 step", out)
        self.assertIn("`Bash` ls '-la'  :x:", out)
        self.assertIn("$0.25", out)
        self.assertIn("<http://mini:8788/#20260929-120000-abcdef|full trace>", out)
        self.assertEqual(p.final, "All good")

    def test_only_recent_steps_shown(self):
        p = commands.Progress("codex", "headless", "full", "20260929-120000-abcdef")
        for i in range(12):
            p.add({"kind": "tool_call", "title": "shell", "body": f"step {i}", "data": {}})
        out = p.render()
        self.assertIn("…4 earlier steps", out)
        self.assertNotIn("step 3\n", out)
        self.assertIn("step 11", out)


class SessionsStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sessions.json"
        self.patcher = patch.object(sessions, "_STORE_PATH", self.path)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_legacy_codex_entries_are_migrated(self):
        self.path.write_text(json.dumps({"111.222": "thread-abc"}))
        entry = await sessions.get_thread("111.222")
        self.assertEqual(entry, {"engine": "codex", "runtime": "headless", "session_id": "thread-abc"})

    async def test_set_and_clear(self):
        await sessions.set_thread("1.2", {"run_id": "r", "engine": "claude", "runtime": "herdr"})
        self.assertEqual((await sessions.get_thread("1.2"))["runtime"], "herdr")
        await sessions.clear_thread("1.2")
        self.assertIsNone(await sessions.get_thread("1.2"))


class TranscriptTest(unittest.TestCase):
    def test_claude_transcript(self):
        lines = [
            {"type": "permission-mode", "mode": "auto"},
            {"type": "user", "message": {"role": "user", "content": "Read README.md"}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command>"}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "# hi"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "It is a bot."}]}},
            {"type": "attachment"},
        ]
        events = [e for raw in lines for e in normalize_claude_transcript(raw)]
        self.assertEqual([e["kind"] for e in events], ["prompt", "tool_call", "tool_result", "text"])

    def test_codex_rollout(self):
        item = lambda it: {"type": "event_msg", "payload": {"type": "item_completed", "item": it}}  # noqa: E731
        lines = [
            {"type": "session_meta", "payload": {"id": "thread-1", "cwd": "/x"}},
            {"type": "response_item", "payload": {"type": "reasoning"}},
            item({"type": "UserMessage", "content": [{"type": "text", "text": "fix it"}]}),
            item({"type": "Reasoning", "summary_text": []}),
            item({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "pytest -q"], "aggregated_output": "1 failed", "exit_code": 1, "status": "completed"}),
            item({"type": "FileChange", "changes": {"/x/a.py": {"type": "update"}}, "status": "completed"}),
            item({"type": "McpToolCall", "server": "node_repl", "tool": "js", "arguments": {"title": "Open page"}, "status": "completed", "result": {"content": [{"type": "text", "text": "ok"}]}}),
            item({"type": "AgentMessage", "content": [{"type": "Text", "text": "Fixed."}]}),
            {"type": "event_msg", "payload": {"type": "token_count"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Fixed."}},
        ]
        events = [e for raw in lines for e in normalize_codex_transcript(raw)]
        kinds = [e["kind"] for e in events]
        self.assertEqual(
            kinds,
            ["session", "prompt", "tool_call", "tool_result", "tool_result", "tool_call", "tool_result", "text", "result"],
        )
        self.assertEqual(events[2]["body"], "pytest -q")
        self.assertTrue(events[3]["data"]["is_error"])
        self.assertIn("update  /x/a.py", events[4]["body"])
        self.assertEqual(events[5]["body"], "Open page")


FAKE_HERDR = textwrap.dedent(
    """\
    #!{python}
    # Fake herdr CLI: agent status comes from a state file the test controls.
    import json, os, sys
    state_dir = os.environ["FAKE_HERDR_DIR"]
    args = sys.argv[1:]
    with open(os.path.join(state_dir, "calls.log"), "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    def status():
        try:
            return open(os.path.join(state_dir, "status")).read().strip()
        except OSError:
            return "idle"
    agent = {{"agent": "claude", "agent_status": None, "agent_session": {{"kind": "id", "value": "sess-1"}}, "name": "a"}}
    if args[:2] == ["session", "list"]:
        print(json.dumps({{"sessions": [{{"name": os.environ.get("HERDR_SESSION"), "running": True}}]}}))
    elif args[:2] == ["workspace", "create"]:
        print(json.dumps({{"result": {{"workspace": {{"workspace_id": "w9"}}, "root_pane": {{"pane_id": "w9:p1"}}}}}}))
    elif args[:2] == ["agent", "start"]:
        if any("\\n" in a for a in args):  # like real herdr: it can't pass these through the pane's shell
            print(json.dumps({{"error": {{"code": "invalid_agent_argument", "message": "agent arguments cannot be encoded safely for the target shell"}}}}), file=sys.stderr)
            sys.exit(1)
        open(os.path.join(state_dir, "start_args"), "w").write(json.dumps(args))
        agent["agent_status"] = "idle"
        print(json.dumps({{"result": {{"agent": agent}}}}))
    elif args[:2] == ["agent", "get"]:
        s = status()
        if s == "gone":
            print(json.dumps({{"error": {{"code": "agent_not_found", "message": "no agent"}}}}), file=sys.stderr)
            sys.exit(1)
        agent["agent_status"] = s
        print(json.dumps({{"result": {{"agent": agent}}}}))
    elif args[:2] == ["agent", "prompt"]:
        # Like Claude: a delivered prompt lands in the transcript. "drop" makes the next N vanish,
        # the way a just-started Claude swallows keystrokes typed before its input box is ready.
        drop_file = os.path.join(state_dir, "drop")
        drop = int(open(drop_file).read()) if os.path.exists(drop_file) else 0
        if drop:
            open(drop_file, "w").write(str(drop - 1))
        elif os.environ.get("FAKE_TRANSCRIPT"):
            with open(os.environ["FAKE_TRANSCRIPT"], "a") as fh:
                fh.write(json.dumps({{"type": "user", "message": {{"content": args[3]}}}}) + "\\n")
        print(json.dumps({{"result": {{"type": "ok"}}}}))
    elif args[:2] == ["agent", "read"]:
        print("Do you want to proceed?\\n 1. Yes\\n 2. No")
    else:
        print(json.dumps({{"result": {{"type": "ok"}}}}))
    """
)


class InteractiveRunTest(unittest.TestCase):
    """Drives InteractiveRun against a fake herdr binary and a fake Claude transcript."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.state = base / "state"
        self.state.mkdir()
        fake = base / "herdr"
        fake.write_text(FAKE_HERDR.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.home = base / "home"
        self.transcript = self.home / ".claude" / "projects" / "-work" / "sess-1.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.work = base / "work"
        self.work.mkdir()
        self.env = patch.dict(os.environ, {
            "HERDR_CLI_PATH": str(fake), "FAKE_HERDR_DIR": str(self.state),
            "COS_HOME": str(base), "HOME": str(self.home),
        })
        self.env.start()
        from cos import interactive
        self.interactive = interactive
        self.runs = []
        self.old_poll = interactive.POLL_SECONDS
        interactive.POLL_SECONDS = 0.05
        self.timing = patch.multiple(interactive, STARTUP_SETTLE_SECONDS=0.3, PROMPT_CONFIRM_SECONDS=0.6)
        self.timing.start()

    def tearDown(self):
        for irun in self.runs:
            irun.stop_watching()
            if irun._thread:
                irun._thread.join(timeout=2)
        self.interactive.POLL_SECONDS = self.old_poll
        self.timing.stop()
        self.env.stop()
        self.tmp.cleanup()

    def set_status(self, status):
        (self.state / "status").write_text(status)

    def write_transcript(self, *records):
        with self.transcript.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def wait_for(self, events, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(predicate(e) for e in events):
                return
            time.sleep(0.05)
        self.fail(f"timed out; saw {[ (e['kind'], e['title']) for e in events]}")

    def test_multi_line_brief_reaches_claude_as_a_file(self):
        from cos.herdr import Herdr

        self.set_status("working")
        irun = self.interactive.InteractiveRun.start(
            "claude", "Read README.md", str(self.work), "dash", "dashboard", None, Herdr("testsess"),
            system="How to do an RCA:\n1. `reproduce` it\n2. find the cause",
        )
        self.runs.append(irun)
        args = json.loads((self.state / "start_args").read_text())
        path = args[args.index("--append-system-prompt-file") + 1]
        self.assertIn("1. `reproduce` it", Path(path).read_text())
        self.assertIn("--remote-control", args)
        # An agent session (it has the brief) gets the Stop hook that asks for a Status line.
        hook = json.loads(Path(args[args.index("--settings") + 1]).read_text())
        self.assertIn("conclusion.py", hook["hooks"]["Stop"][0]["hooks"][0]["command"])
        # Not left in default (approve-everything) mode: the phone gets the same powers as desktop.
        self.assertEqual(args[args.index("--permission-mode") + 1], "bypassPermissions")

    def test_full_turn_blocked_and_close(self):
        from cos.herdr import Herdr

        events = []
        self.set_status("working")
        irun = self.interactive.InteractiveRun.start(
            "claude", "Read README.md", str(self.work), "slack-1", "slack",
            {"slack_thread": "1.2"}, Herdr("testsess"), events.append,
        )
        self.runs.append(irun)
        self.write_transcript(
            {"type": "user", "message": {"content": "Read README.md"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "# hi"}]}},
        )
        self.wait_for(events, lambda e: e["kind"] == "tool_result")

        self.set_status("blocked")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "blocked")
        blocked = [e for e in events if e["title"] == "blocked"][0]
        self.assertIn("Do you want to proceed?", blocked["body"])
        irun.approve()

        self.write_transcript({"type": "assistant", "message": {"content": [{"type": "text", "text": "It is a bot."}]}})
        self.set_status("done")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "done")
        done = [e for e in events if e["title"] == "done"][0]
        self.assertEqual(done["data"]["last_text"], "It is a bot.")

        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("prompt"), 1, "our own prompt must not be recorded twice")
        meta = irun.run.meta()
        self.assertEqual(meta["runtime"], "herdr")
        self.assertEqual(meta["session_id"], "sess-1")
        self.assertEqual(meta["herdr_session"], "testsess")
        self.assertEqual(meta["status"], "waiting")

        calls = [json.loads(l) for l in (self.state / "calls.log").read_text().splitlines()]
        self.assertIn(["agent", "send-keys", meta["herdr_agent"], "enter"], calls)

        self.set_status("gone")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "closed")
        self.assertEqual(irun.run.meta()["status"], "closed")

    def test_attach_resumes_from_saved_offset(self):
        from cos.herdr import Herdr

        self.set_status("working")
        events = []
        irun = self.interactive.InteractiveRun.start(
            "claude", "hi", str(self.work), "l", "slack", None, Herdr("testsess"), events.append,
        )
        self.set_status("idle")
        self.assertNotIn("--settings", json.loads((self.state / "start_args").read_text()))  # no brief, no Stop hook
        self.runs.append(irun)
        self.write_transcript({"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}})
        self.wait_for(events, lambda e: e["kind"] == "text")
        irun.stop_watching()
        time.sleep(0.2)

        self.write_transcript({"type": "assistant", "message": {"content": [{"type": "text", "text": "second"}]}})
        later = []
        self.runs.append(self.interactive.InteractiveRun.attach(irun.run.id, Herdr("testsess"), later.append))
        self.wait_for(later, lambda e: e["kind"] == "text")
        texts = [e["body"] for e in later if e["kind"] == "text"]
        self.assertEqual(texts, ["second"])


    def calls(self):
        return [json.loads(l) for l in (self.state / "calls.log").read_text().splitlines()]

    def test_dropped_first_prompt_is_sent_again(self):
        from cos.herdr import Herdr

        self.set_status("idle")  # a dropped prompt leaves Claude idle, as on 2026-10-01
        self.transcript.touch()
        (self.state / "drop").write_text("1")
        with patch.dict(os.environ, {"FAKE_TRANSCRIPT": str(self.transcript)}):
            irun = self.interactive.InteractiveRun.start(
                "claude", "Fix the bug", str(self.work), "l", "dashboard", None, Herdr("testsess"),
            )
        self.runs.append(irun)
        prompts = [c for c in self.calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 2, "the dropped prompt must be typed again")
        self.assertIn("Fix the bug", self.transcript.read_text())
        kinds = [json.loads(l)["kind"] for l in (irun.run.dir / "events.jsonl").read_text().splitlines()]
        self.assertNotIn("error", kinds)
        self.assertEqual(kinds.count("prompt"), 1, "a resend is not a new turn")

    def test_prompt_that_never_arrives_fails_the_run(self):
        from cos.herdr import Herdr, HerdrError

        self.set_status("idle")
        self.transcript.touch()
        (self.state / "drop").write_text("99")
        with patch.dict(os.environ, {"FAKE_TRANSCRIPT": str(self.transcript)}):
            with self.assertRaises(HerdrError) as caught:
                self.interactive.InteractiveRun.start(
                    "claude", "Fix the bug", str(self.work), "l", "dashboard", None, Herdr("testsess"),
                )
        self.assertEqual(caught.exception.code, "prompt_not_delivered")
        prompts = [c for c in self.calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1 + self.interactive.PROMPT_RETRIES)
        self.assertIn(["workspace", "close", "w9"], self.calls(), "an agent that never got its task is closed")


if __name__ == "__main__":
    unittest.main()


class CodexTurnCompleteTest(InteractiveRunTest):
    """herdr may report Codex as "unknown" forever; the transcript's task_complete ends the turn."""

    def test_task_complete_ends_turn_while_herdr_says_unknown(self):
        from cos.herdr import Herdr

        rollout = self.home / ".codex" / "sessions" / "2026" / "09" / "30" / "rollout-x-sess-1.jsonl"
        rollout.parent.mkdir(parents=True)
        events = []
        self.set_status("unknown")
        irun = self.interactive.InteractiveRun.start(
            "codex", "list files", str(self.work), "l", "slack", None, Herdr("testsess"), events.append,
        )
        self.runs.append(irun)
        item = lambda it: {"type": "event_msg", "payload": {"type": "item_completed", "item": it}}  # noqa: E731
        with rollout.open("w") as fh:
            for rec in (
                {"type": "session_meta", "payload": {"id": "sess-1", "cwd": str(self.work)}},
                item({"type": "AgentMessage", "content": [{"type": "Text", "text": "Two files."}]}),
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Two files."}},
            ):
                fh.write(json.dumps(rec) + "\n")
        self.wait_for(events, lambda e: e["kind"] == "status" and e["title"] == "done")
        done = [e for e in events if e["kind"] == "status" and e["title"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["data"]["last_text"], "Two files.")
        # Full mode (the default): an interactive codex never stops on an approval or the sandbox.
        args = json.loads((self.state / "start_args").read_text())
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)


class CodexSecondTurnTest(CodexTurnCompleteTest):
    def test_second_turn_gets_its_own_done(self):
        from cos.herdr import Herdr

        rollout = self.home / ".codex" / "sessions" / "2026" / "09" / "30" / "rollout-x-sess-1.jsonl"
        rollout.parent.mkdir(parents=True)
        events = []
        self.set_status("unknown")
        irun = self.interactive.InteractiveRun.start(
            "codex", "first", str(self.work), "l", "slack", None, Herdr("testsess"), events.append,
        )
        self.runs.append(irun)
        item = lambda it: {"type": "event_msg", "payload": {"type": "item_completed", "item": it}}  # noqa: E731

        def turn(text):
            with rollout.open("a") as fh:
                for rec in (
                    item({"type": "AgentMessage", "content": [{"type": "Text", "text": text}]}),
                    {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": text}},
                ):
                    fh.write(json.dumps(rec) + "\n")

        with rollout.open("w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"id": "sess-1", "cwd": str(self.work)}}) + "\n")
        turn("one")
        self.wait_for(events, lambda e: e["kind"] == "status" and (e["data"] or {}).get("last_text") == "one")
        irun.send("second")
        time.sleep(0.3)  # the agent is working on turn 2 (herdr still says "unknown")
        turn("two")
        self.wait_for(events, lambda e: e["kind"] == "status" and (e["data"] or {}).get("last_text") == "two")


class BriefAndMarkdownTest(unittest.TestCase):
    def test_brief_goes_to_claude_natively_and_leads_codex_first_prompt(self):
        from cos import runner
        claude = runner.build_command("claude", "why?", "full", system="BRIEF")
        self.assertEqual(claude[claude.index("--append-system-prompt") + 1], "BRIEF")
        self.assertEqual(claude[-1], "why?")
        codex = runner.build_command("codex", "why?", "full", system="BRIEF")
        self.assertTrue(codex[-1].startswith("BRIEF") and codex[-1].endswith("why?"))
        resumed = runner.build_command("codex", "and?", "full", "thread-1", system="BRIEF")
        self.assertEqual(resumed[-1], "and?", "the brief is only sent once per Codex session")

    def test_local_file_links_become_readable_paths(self):
        work = tempfile.mkdtemp()
        with patch.dict(os.environ, {"NEO_WORKDIR": work}):
            md = f"See [Resolver]({work}/acme-api/pkg/ops.py:558) and [docs](https://example.com)."
            out = commands.slack_markdown(md)
        self.assertIn("Resolver (`acme-api/pkg/ops.py:558`)", out)
        self.assertIn("[docs](https://example.com)", out)
        home = os.path.expanduser("~")
        with patch.dict(os.environ, {"NEO_WORKDIR": work}):
            out = commands.slack_markdown(f"[Todo list]({home}/notes/todo.md)")
        self.assertEqual(out, "Todo list (`~/notes/todo.md`)")


class ToolLabelTest(unittest.TestCase):
    def test_mcp_names_are_readable(self):
        self.assertEqual(commands.tool_label("mcp__claude_ai_Sentry__get_sentry_resource"), "Sentry · get_sentry_resource")
        self.assertEqual(commands.tool_label("mcp__pal__chat"), "pal · chat")
        self.assertEqual(commands.tool_label("Bash"), "Bash")
