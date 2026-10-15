"""Regression tests for the code-review findings (dashboard CSRF, run cleanup, herdr errors)."""

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import runner  # noqa: E402
from cos.herdr import Herdr, HerdrError  # noqa: E402


class DashboardSecurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"COS_HOME": self.tmp.name}, clear=False)
        self.env.start()
        os.environ.pop("COS_TOKEN", None)
        from cos import server
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def post(self, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/runs", body=body, headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_cross_site_form_post_is_refused(self):
        body = json.dumps({"engine": "claude", "prompt": "x", "cwd": "/", "mode": "full"})
        # What a malicious page can send without a CORS preflight.
        self.assertEqual(self.post(body, {"Content-Type": "text/plain", "Origin": "https://evil.example"}), 403)
        # JSON from a foreign origin (would need a preflight, which is never approved).
        self.assertEqual(self.post(body, {"Content-Type": "application/json", "Origin": "https://evil.example"}), 403)

    def test_foreign_host_header_is_refused(self):
        body = json.dumps({"engine": "claude", "prompt": "x", "cwd": "/nope", "mode": "full"})
        status = self.post(body, {"Content-Type": "application/json", "Host": "attacker.example:80"})
        self.assertEqual(status, 403)

    def test_same_origin_json_reaches_validation(self):
        body = json.dumps({"engine": "claude", "prompt": "x", "cwd": "/definitely/not/here", "mode": "full"})
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.post(body, headers), 400)  # passed the security checks, failed on cwd


class RunnerHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.bin = home / "bin"
        self.bin.mkdir()
        self.env = patch.dict(os.environ, {"COS_HOME": str(home), "PATH": f"{self.bin}:{os.environ['PATH']}"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_claude(self, script):
        path = self.bin / "claude"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_prompts_follow_end_of_options(self):
        claude = runner.build_command("claude", "-rm everything", "readonly")
        self.assertEqual(claude[-2:], ["--", "-rm everything"])
        codex = runner.build_command("codex", "-x", "edit", "thread-1")
        self.assertEqual(codex[-3:], ["--", "thread-1", "-x"])

    def test_engine_is_not_orphaned_when_processing_fails(self):
        # Prints one line, then sleeps: the runner must kill it after the listener raises.
        self.fake_claude('echo \'{"type":"system","subtype":"init","session_id":"s"}\'\nexec sleep 30\n')
        run = runner.Run.create("claude", "hi", self.tmp.name, "readonly")

        def boom(event):
            if event["kind"] == "session":
                raise RuntimeError("listener failed")

        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            run.execute(on_event=boom)
        self.assertLess(time.monotonic() - started, 10)
        meta = run.meta()
        self.assertEqual(meta["status"], "failed")
        self.assertIsNone(meta["pid"])

    def test_cancel_marks_run_cancelled(self):
        self.fake_claude("exec sleep 30\n")
        run = runner.Run.create("claude", "hi", self.tmp.name, "readonly")
        result = {}
        t = threading.Thread(target=lambda: result.update(run.execute()))
        t.start()
        deadline = time.monotonic() + 5
        while run.meta().get("pid") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(run.cancel())
        t.join(10)
        self.assertEqual(result["status"], "cancelled")

    def test_second_turn_is_refused_while_running(self):
        self.fake_claude("exec sleep 30\n")
        run = runner.Run.create("claude", "hi", self.tmp.name, "readonly")
        t = threading.Thread(target=run.execute)
        t.start()
        deadline = time.monotonic() + 5
        while run.meta().get("pid") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        other = runner.Run(run.id)  # a second object for the same run, as the dashboard would create
        with self.assertRaises(RuntimeError):
            other.execute("again")
        run.cancel()
        t.join(10)

    def test_live_runs_cannot_be_executed_headless(self):
        run = runner.Run.create("claude", "hi", self.tmp.name, "full", runtime="herdr")
        with self.assertRaises(RuntimeError):
            run.execute("hi")


class HerdrErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Path(self.tmp.name) / "herdr"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, body):
        self.fake.write_text(f"#!{sys.executable}\n" + body)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def test_timeout_becomes_herdr_error(self):
        self.write("import time; time.sleep(5)\n")
        herdr = Herdr("t", binary=str(self.fake))
        with self.assertRaises(HerdrError) as ctx:
            herdr.call("agent", "get", "x", timeout=0.5)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_prompt_starting_with_dash_is_not_an_option(self):
        log = Path(self.tmp.name) / "args.json"
        self.write(f"import json, sys; json.dump(sys.argv[1:], open({str(log)!r}, 'w')); print(json.dumps({{'result': {{}}}}))\n")
        Herdr("t", binary=str(self.fake)).prompt("a", "-x do it")
        self.assertEqual(json.loads(log.read_text())[-1], " -x do it")

    def test_only_missing_agent_counts_as_closed(self):
        from cos.interactive import InteractiveRun

        irun = InteractiveRun.__new__(InteractiveRun)
        irun.name = "a"
        self.write("import sys; print('{\"error\":{\"code\":\"agent_not_found\",\"message\":\"x\"}}', file=sys.stderr); sys.exit(1)\n")
        irun.herdr = Herdr("t", binary=str(self.fake))
        self.assertEqual(irun.status(), "closed")
        self.write("import time; time.sleep(5)\n")
        irun.herdr.call = lambda *a, **k: (_ for _ in ()).throw(HerdrError("timeout", "slow"))
        with self.assertRaises(HerdrError):
            irun.status()


if __name__ == "__main__":
    unittest.main()


class FleetAsksTest(unittest.TestCase):
    def test_question_at_end_means_needs_you(self):
        from cos import fleet
        with patch.object(fleet, "_transcript_for", return_value=Path("/nonexistent")), \
             patch.object(fleet, "_last_text_for", return_value=("Done. Want me to open a PR?**", False, 1)):
            with patch("pathlib.Path.exists", return_value=True), patch("pathlib.Path.stat") as st:
                st.return_value.st_mtime = 1.0
                idle = fleet._agent_row("default", {"agent": "claude", "agent_status": "idle", "pane_id": "w1:p1", "cwd": "/x/repo"}, {})
                working = fleet._agent_row("default", {"agent": "claude", "agent_status": "working", "pane_id": "w1:p1", "cwd": "/x/repo"}, {})
        self.assertTrue(idle["asks"])
        self.assertFalse(working["asks"], "a working agent is not waiting on you")
        self.assertEqual(idle["repo"], "repo")

    def row(self, status, last_text="All done.", prompts=1, run=None, last_active=100.0):
        from cos import fleet
        with patch.object(fleet, "_transcript_for", return_value=Path("/nonexistent")), \
             patch.object(fleet, "_last_text_for", return_value=(last_text, False, prompts)), \
             patch("pathlib.Path.exists", return_value=True), patch("pathlib.Path.stat") as st:
            st.return_value.st_mtime = last_active
            runs = {("neo", "a1"): run} if run else {}
            return fleet._agent_row("neo", {"agent": "claude", "agent_status": status, "pane_id": "w1:p1", "name": "a1",
                                            "cwd": "/x/repo", "agent_session": {"kind": "id", "value": "s-1"}}, runs)

    def test_attention(self):
        from cos import fleet
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"COS_HOME": home}):
            fleet.seen.reset()
            old_run = {"id": "r1", "created_at": "2020-01-01T00:00:00+00:00", "prompt": "Fix it"}
            self.assertEqual(self.row("blocked")["attention"], "approve")
            self.assertEqual(self.row("idle", "", prompts=0, run=old_run)["attention"], "stuck",
                             "a neo agent that never got its task needs you")
            new_run = {**old_run, "created_at": datetime.now(timezone.utc).isoformat()}
            self.assertNotEqual(self.row("idle", "", prompts=0, run=new_run)["attention"], "stuck", "still starting")
            self.assertEqual(self.row("idle", "Open a PR?")["attention"], "reply")
            self.assertEqual(self.row("working")["attention"], "working")
            finished = self.row("idle")
            self.assertEqual(finished["attention"], "review", "finished and not read yet")
            fleet.mark_seen([finished])
            self.assertEqual(self.row("idle")["attention"], "idle", "read: no longer needs you")
            self.assertEqual(self.row("idle", last_active=200.0)["attention"], "review", "a newer result is unread again")
            fleet.seen.reset()

    def test_last_text_is_cached_with_its_prompt_count(self):
        from cos import fleet
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
                            + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done?"}]}}) + "\n")
            mtime = path.stat().st_mtime
            first = fleet._last_text_for("claude", path, mtime)
            self.assertEqual(fleet._last_text_for("claude", path, mtime), first, "a cache hit returns the same shape")
            self.assertEqual(first[0], "Done?")
            self.assertEqual(first[2], 1)

    def test_last_text_is_only_the_latest_turns_reply(self):
        from cos import fleet
        user = lambda t: json.dumps({"type": "user", "message": {"content": t}}) + "\n"  # noqa: E731
        said = lambda t: json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n"  # noqa: E731
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(user("go") + said("Status: needs-you — pick A or B") + user("A"))
            # interrupted before it said anything: the old ask is not this turn's reply
            self.assertEqual(fleet._last_text_for("claude", path, path.stat().st_mtime)[0], "")

    def test_valid_target(self):
        from cos import fleet
        self.assertTrue(fleet.valid_target("default", "w12:p3"))
        self.assertTrue(fleet.valid_target("neo", "wB:p1"))
        self.assertFalse(fleet.valid_target("neo", "wB:p1 --help"))
        self.assertFalse(fleet.valid_target("../x", "w1:p1"))
        self.assertFalse(fleet.valid_target("default", "w1:p1; rm"))


class TailscaleHeaderTest(DashboardSecurityTest):
    """The Tailscale identity header is only honoured for our own tailnet Host name."""

    def get(self, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/runs", headers=headers)
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_spoofed_header_is_refused(self):
        with patch.dict(os.environ, {"COS_TAILSCALE_USERS": "me@example.com", "NEO_DASHBOARD_URL": "https://mini.tail1.ts.net"}):
            # DNS rebinding: the browser sends the attacker's Host.
            self.assertEqual(self.get({"Host": "evil.example", "Tailscale-User-Login": "me@example.com"}), 403)
            # A local process talking to 127.0.0.1 with the header.
            self.assertEqual(self.get({"Host": f"127.0.0.1:{self.port}", "Tailscale-User-Login": "me@example.com"}), 403)
            # What `tailscale serve` forwards for us.
            self.assertEqual(self.get({"Host": "mini.tail1.ts.net", "Tailscale-User-Login": "me@example.com"}), 200)
            self.assertEqual(self.get({"Host": "mini.tail1.ts.net", "Tailscale-User-Login": "other@example.com"}), 403)

    def test_keys_starting_with_dash_are_refused(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/agents/default/w1%3Ap1/keys", body=json.dumps({"keys": ["--help"]}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        self.assertEqual(resp.status, 400)


class TailnetWhoisTest(DashboardSecurityTest):
    """Direct access from your own tailnet device, identified with `tailscale whois`."""

    def status(self, host, peer_login):
        from cos import server
        with patch.object(server, "tailscale_login", return_value=peer_login), \
             patch.dict(os.environ, {"COS_TOKEN": "t0ken", "COS_TAILSCALE_USERS": "me@example.com",
                                     "NEO_DASHBOARD_HOST": "127.0.0.1", "NEO_DASHBOARD_PORT": str(self.port),
                                     "NEO_DASHBOARD_URL": f"http://127.0.0.1:{self.port}"}):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/api/runs", headers={"Host": host})
            resp = conn.getresponse(); resp.read(); conn.close()
            return resp.status

    def test_own_device_needs_no_token(self):
        self.assertEqual(self.status(f"127.0.0.1:{self.port}", "me@example.com"), 200)

    def test_other_tailnet_user_is_refused(self):
        self.assertEqual(self.status(f"127.0.0.1:{self.port}", "someone@example.com"), 403)

    def test_rebound_hostname_is_refused_even_for_own_device(self):
        self.assertEqual(self.status("evil.example", "me@example.com"), 403)

    def test_browser_gets_sign_in_page(self):
        with patch.dict(os.environ, {"COS_TOKEN": "t0ken"}):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/", headers={"Host": "elsewhere.example"})
            resp = conn.getresponse(); body = resp.read(); conn.close()
        self.assertEqual(resp.status, 403)
        self.assertIn(b"Sign in", body)


class RecentTurnsTest(unittest.TestCase):
    def test_keeps_last_turns_not_last_events(self):
        from cos.fleet import recent_turns
        events = []
        for turn in range(10):
            events.append({"kind": "prompt", "body": f"q{turn}"})
            events += [{"kind": "tool_call", "body": "x"}] * 50
            events.append({"kind": "text", "body": f"a{turn}"})
        kept = recent_turns(events, turns=3, cap=1000)
        prompts = [e["body"] for e in kept if e["kind"] == "prompt"]
        self.assertEqual(prompts, ["q7", "q8", "q9"])
        self.assertEqual(kept[0]["data"]["status"], "earlier")

    def test_short_conversation_is_untouched(self):
        from cos.fleet import recent_turns
        events = [{"kind": "prompt", "body": "q"}, {"kind": "text", "body": "a"}]
        self.assertEqual(recent_turns(events), events)


class RepoDirUnreadableTest(unittest.TestCase):
    def setUp(self):
        from cos import work
        self.work = work
        work._repo_dirs.update(map={"acme/app": "/old/app"}, at=0.0)

    def test_unreadable_work_folder_keeps_last_map(self):
        # launchd's python sometimes gets EINTR listing ~/Documents; the dashboard must still load.
        with patch.object(Path, "iterdir", side_effect=InterruptedError(4, "Interrupted system call")):
            self.assertEqual(self.work.repo_dir("acme/app"), "/old/app")
            self.assertIsNone(self.work.repo_dir("acme/other"))
        self.assertEqual(self.work._repo_dirs["at"], 0.0)  # not cached: retried on the next call
