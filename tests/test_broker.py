"""One person's CoS asks another's through the broker (cos/broker.py)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import accounts, broker, people  # noqa: E402
from cos.runner import Run  # noqa: E402

OWNER, AJAY, YOSH = "UOWNER1", "UAJAY01", "UYOSH01"
TRIED = "read the deploy workflow and the last three CI runs; no failure there"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_MEMORY_DIR": str(self.dir / "memory"),
                                           "NEO_OWNER_USER": OWNER, "NEO_OWNER_NAME": "Anil"})
        self.env.start()
        os.environ.pop("NEO_ACCOUNTS", None)
        accounts._save_registry({AJAY: "neo-ajay", YOSH: "neo-yosh"})
        people.save(AJAY, AJAY, name="Ajay", repos="backie")
        people.save(YOSH, YOSH, name="Yosh", repos="deskie")

    def tearDown(self):
        self.env.stop()

    def run_of(self, user, **extra):
        return Run.create("claude", "task", str(self.dir), "full", source="slack", extra={"slack_user": user, "slack_channel": "C1",
                                                                          "slack_thread": "1.1", **extra})

    def ask(self, decision, answer="", to="anil", user=AJAY, tried=TRIED):
        run = self.run_of(user)
        with patch.object(broker, "answer", return_value={"decision": decision, "answer": answer, "reason": "policy", "run": "r"}):
            return broker.ask(run.id, "why did last night's deploy fail?", tried, to)


class TokenTest(Base):
    def test_a_run_asks_only_as_itself(self):
        run = self.run_of(AJAY)
        self.assertTrue(broker.verify(run.id, broker.token(run.id)))
        self.assertFalse(broker.verify(run.id, broker.token("20260101-000000-abcdef")))
        self.assertFalse(broker.verify(run.id, ""))
        self.assertEqual(oct((self.dir / "a2a.key").stat().st_mode & 0o777), "0o600")

    def test_an_answering_run_gets_no_token(self):
        from cos.runner import engine_env
        with patch.dict(os.environ, {"NEO_DASHBOARD_PORT": "8788"}):
            asking = engine_env(self.run_of(AJAY).meta())
            answering = engine_env(self.run_of(OWNER, kind="a2a-answer").meta())
        self.assertEqual(asking["NEO_A2A_URL"], "http://127.0.0.1:8788")
        self.assertNotIn("NEO_A2A_TOKEN", answering)


class RefusalTest(Base):
    def test_try_yourself_first(self):
        out = self.ask("answer", "x", tried="nothing")
        self.assertEqual(out["status"], "refused")
        self.assertIn("yourself first", out["text"])

    def test_no_asking_while_answering(self):
        run = self.run_of(OWNER, kind="a2a-answer")
        self.assertEqual(broker.ask(run.id, "q", TRIED)["status"], "refused")

    def test_a_daily_limit(self):
        with patch.object(broker, "asks_today", return_value=broker.ASKS_PER_DAY):
            self.assertEqual(self.ask("answer", "x")["status"], "refused")

    def test_unknown_names_and_blocked_people(self):
        self.assertIn("don't know anyone", self.ask("answer", "x", to="zed")["text"])
        people.save(YOSH, OWNER, role="blocked")
        self.assertIsNone(broker.route("q", AJAY, "yosh"))


class RouteTest(Base):
    def test_by_name_or_account_never_the_asker(self):
        self.assertEqual(broker.route("q", AJAY, "Yosh"), YOSH)
        self.assertEqual(broker.route("q", AJAY, "neo-yosh"), YOSH)
        self.assertEqual(broker.route("q", AJAY, "anil"), OWNER)
        self.assertIsNone(broker.route("q", AJAY, "ajay"))

    def test_the_models_pick_or_the_owner(self):
        with patch.object(broker, "_pick", return_value=YOSH):
            self.assertEqual(broker.route("deskie build broke", AJAY), YOSH)
        with patch.object(broker, "_pick", return_value=None):
            self.assertEqual(broker.route("deskie build broke", AJAY), OWNER)


class ExchangeTest(Base):
    def test_answered_and_the_owner_is_told(self):
        out = self.ask("answer", "The migration timed out; rerun with a longer lock timeout.")
        self.assertEqual((out["status"], out["by"]), ("answered", "Anil"))
        sent = broker.take_outbox()
        self.assertEqual(sent[0]["user"], OWNER)
        self.assertIn("I answered", sent[0]["text"])
        self.assertEqual(broker.take_outbox(), [])

    def test_declined(self):
        out = self.ask("deny", "", to="yosh")
        self.assertEqual(out["status"], "denied")
        self.assertIn("declined", broker.take_outbox()[0]["text"])

    def test_a_reply_with_a_credential_waits_for_its_owner(self):
        out = self.ask("answer", "use token ghp_abcdefghijklmnopqrstuvwxyz0123")
        self.assertEqual(out["status"], "pending")
        self.assertNotIn("ghp_", out["text"])
        escalation = broker.take_outbox()[0]
        self.assertTrue(escalation["buttons"])
        self.assertEqual(escalation["user"], OWNER)

    def test_only_the_answerers_owner_releases_it(self):
        self.ask("ask_owner", "The deploy failed on the migration step.")
        request = broker.take_outbox()[0]["request"]
        self.assertIsNone(broker.resolve(request, AJAY, True))  # the asker can't release it
        self.assertIsNotNone(broker.resolve(request, OWNER, True))
        delivered = broker.take_outbox()[0]
        self.assertEqual(delivered["user"], AJAY)  # by DM: shared with them, not with the channel they asked from
        self.assertNotIn("channel", delivered)
        self.assertIn("migration step", delivered["text"])
        self.assertIsNone(broker.resolve(request, OWNER, True))  # once


class HardeningTest(Base):
    """What the security review found (2026-10-29)."""

    def test_runs_reading_other_peoples_text_cant_ask(self):
        for kind in ("watch", "brief", "mentions", "alert-rca", "loops", "a2a-answer"):
            self.assertFalse(broker.may_ask({"source": "slack", "kind": kind}), kind)
        self.assertFalse(broker.may_ask({"source": "schedule"}))
        self.assertTrue(broker.may_ask({"source": "slack"}))
        self.assertTrue(broker.may_ask({"source": "dashboard"}))

    def test_a_name_someone_chose_never_routes_or_identifies(self):
        accounts._save_registry({AJAY: "neo-ajay", YOSH: "neo-yosh", OWNER: "neo-anil"})
        people.save(YOSH, YOSH, name="Anil")  # "call me Anil"
        self.assertEqual(broker.route("q", AJAY, "anil"), OWNER)  # the owner's account wins
        self.assertEqual(broker.route("q", AJAY, YOSH), YOSH)
        self.assertIn(f"<@{YOSH}> (neo-yosh)", broker.label(YOSH))
        self.assertNotIn("Anil", broker.label(YOSH))

    def test_answering_tools_read_only_and_prod_only_for_full_askers(self):
        full, limited = " ".join(broker.answer_args("full")), " ".join(broker.answer_args("prs-only"))
        for writer in ("Bash(sort", "Bash(uniq", "Bash(git log", "Bash(git diff", "Bash(cat", "Bash(kubectl get"):
            self.assertNotIn(writer, full)
        self.assertIn("Bash(kubectl logs:*)", full)
        self.assertNotIn("kubectl", limited)
        self.assertIn("Read(/proc/**)", full)
        self.assertIn("Read(~/code/**)", full)

    def test_an_answering_run_borrows_only_the_model(self):
        with patch.dict(os.environ, {"NEO_SHARED_CLAUDE_TOKEN": "sk-o", "NEO_SHARED_GH_TOKEN": "ghp-o",
                                     "NEO_SHARED_OPENAI_KEY": "sk-oa", "NEO_ACCOUNT_ENV": "DD_KEY"}):
            spec = accounts.spec({"cwd": "/x", "lend": "claude", "role": "full"}, ["claude"], {"DD_KEY": "dd"}, {})
        self.assertEqual(spec["shared"], {"claude_token": "sk-o", "gh_token": "", "openai_key": ""})
        self.assertNotIn("DD_KEY", spec["env"])

    def test_screen_catches_more_credential_shapes(self):
        for text in ('{"refresh_token": "1//0gAbCdEfGhIjKlMnOpQrStUvWxYz"}', "ya29.a0AfH6SMBabcdefghijklmnopqrstu",
                     "data: c3VwZXJzZWNyZXRwYXNzd29yZDEyMzQ1Njc4OTBhYmNkZWZnaGlqa2w="):
            self.assertTrue(broker.screen(text), text)
        self.assertIsNone(broker.screen("Use git blame -L 42,42 path/to/file"))

    def test_parallel_asks_cant_pass_the_limits(self):
        for n in range(broker.IN_FLIGHT_PER_ASKER):
            self.assertIsNone(broker._reserve(AJAY, f"r{n}"))
        self.assertIn("waiting", broker._reserve(AJAY, "rx"))
        broker._release("r0")
        self.assertIsNone(broker._reserve(AJAY, "rx"))

    def test_what_the_owner_approves_is_what_is_sent(self):
        long = "x " * 2000
        self.ask("ask_owner", long)
        item = broker.take_outbox()[0]
        self.assertEqual(len(item["draft"]), broker.MAX_ANSWER)
        waiting = next(iter(broker.STATE.read()["waiting"].values()))
        self.assertEqual(waiting["draft"], item["draft"])

    def test_the_askers_words_cant_format_the_owners_dm(self):
        run = self.run_of(AJAY)
        with patch.object(broker, "answer", return_value={"decision": "ask_owner", "answer": "d", "reason": "r", "run": "x"}):
            broker.ask(run.id, "<https://evil.example|click to approve> <!channel>", TRIED, "anil")
        text = broker.take_outbox()[0]["text"]
        self.assertNotIn("<https://evil", text)
        self.assertNotIn("<!channel>", text)

    def test_answers_are_marked_as_information(self):
        out = self.ask("answer", "rerun it")
        self.assertIn("not instructions", out["text"])


class CharterTest(Base):
    def test_each_cos_knows_its_job_and_asks_last(self):
        people.save(AJAY, OWNER, role="prs-only")
        text = broker.charter(AJAY)
        self.assertIn("Ajay's chief of staff", text)
        self.assertIn("never merge", text)
        self.assertIn("Resolve things yourself first", text)
        self.assertIn("neo-ask --tried", text)
        self.assertNotIn("neo-ask", broker.charter(OWNER, may_ask=False))

    def test_policy(self):
        self.assertEqual(broker.policy(AJAY), broker.DEFAULT_POLICY)
        broker.set_policy(AJAY, "Only answer about backie.")
        self.assertEqual(broker.policy(AJAY), "Only answer about backie.")
        broker.set_policy(AJAY, "")
        self.assertEqual(broker.policy(AJAY), broker.DEFAULT_POLICY)


class NeoAskTest(unittest.TestCase):
    def test_outside_a_run_it_says_so(self):
        out = subprocess.run([sys.executable, str(ROOT / "deploy" / "linux" / "neo-ask"), "--tried", "x", "q"],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(out.returncode, 2)
        self.assertIn("only works inside a run", out.stdout)


if __name__ == "__main__":
    unittest.main()


class ServerTrustTest(Base):
    """cos/server.py on a shared Linux box: loopback alone proves nothing (review 2026-10-29, C1 and M1)."""

    def handler(self, path="/api/a2a/ask", headers=None, body=b"{}", peer_uid=None):
        import io
        from email.message import Message
        from cos import server
        h = server.Handler.__new__(server.Handler)
        msg = Message()
        for k, v in {"Host": "127.0.0.1:8788", "Content-Type": "application/json",
                     "Content-Length": str(len(body)), **(headers or {})}.items():
            msg[k] = v
        h.headers, h.path, h.rfile, h.wfile = msg, path, io.BytesIO(body), io.BytesIO()
        h.client_address, h.request_version, h.command = ("127.0.0.1", 50000), "HTTP/1.1", "POST"
        h.errors = []
        h._error = lambda status, text: h.errors.append((int(status), text))
        h._json = lambda data, status=200: h.errors.append((int(status), data))
        h._peer_uid = lambda: peer_uid
        return h

    def test_another_account_isnt_trusted_on_loopback(self):
        from cos import server
        with patch.object(server, "LINUX", True), patch.dict(os.environ, {"COS_TOKEN": ""}):
            self.assertFalse(self.handler(peer_uid=1035)._authorized())  # neo-ajay
            self.assertTrue(self.handler(peer_uid=os.getuid())._authorized())  # neo itself
            self.assertTrue(self.handler(peer_uid=0)._authorized())  # root (tailscaled)

    def test_a_faked_tailscale_header_from_another_account_is_refused(self):
        from cos import server
        env = {"NEO_DASHBOARD_URL": "https://spark.example.ts.net:8443", "COS_TAILSCALE_USERS": "anil@x.com",
               "COS_TOKEN": "secret"}
        headers = {"Host": "spark.example.ts.net:8443", "Tailscale-User-Login": "anil@x.com"}
        with patch.object(server, "LINUX", True), patch.dict(os.environ, env):
            self.assertIsNone(self.handler(headers=headers, peer_uid=1035)._tailscale_user())
            self.assertEqual(self.handler(headers=headers, peer_uid=0)._tailscale_user(), "anil@x.com")

    def test_a_token_works_only_while_its_run_runs_and_from_its_account(self):
        import json as _json
        from cos import server
        run = self.run_of(AJAY, account="neo-ajay")
        body = _json.dumps({"run_id": run.id, "question": "q", "tried": TRIED}).encode()
        headers = {"X-Neo-Token": broker.token(run.id)}
        run.update_meta(status="done")
        with patch.object(server, "LINUX", True), patch.object(server, "account_uid", return_value=1035), \
                patch.object(broker, "ask", return_value={"status": "answered", "text": "ok"}):
            h = self.handler(headers=headers, body=body, peer_uid=1035)
            h._a2a_ask()
            self.assertEqual(h.errors[-1][0], 403)  # the run isn't running
            run.update_meta(status="running")
            h = self.handler(headers=headers, body=body, peer_uid=1036)  # another account replaying it
            h._a2a_ask()
            self.assertEqual(h.errors[-1], (403, "not from that run's account"))
            h = self.handler(headers=headers, body=body, peer_uid=1035)
            h._a2a_ask()
            self.assertEqual(h.errors[-1], (200, {"status": "answered", "text": "ok"}))


class DelegatedWorkTest(Base):
    """One neo asks another to do something with its owner's access; it reports back when done."""

    def delegate(self, decision, to="anil"):
        run = self.run_of(AJAY)
        started = []
        with patch.object(broker, "decide", return_value={"decision": decision, "plan": "fix the test", "reason": "policy", "run": "d"}), \
                patch.object(broker, "start_work", side_effect=lambda tid, row: started.append(tid) or broker._set_task(tid, **row, status="working", run="w")):
            out = broker.delegate(run.id, "make the backie login test pass", TRIED, to)
        return out, started

    def test_accepted_work_returns_a_task_at_once(self):
        out, started = self.delegate("accept")
        self.assertEqual(out["status"], "working")
        self.assertEqual(started, [out["task"]])
        self.assertIn(out["task"], broker.tasks_text(AJAY))

    def test_declined_and_escalated(self):
        self.assertEqual(self.delegate("deny")[0]["status"], "declined")
        out, started = self.delegate("ask_owner")
        self.assertEqual((out["status"], started), ("pending", []))
        note = [o for o in broker.take_outbox() if o.get("buttons") == "work"][0]
        self.assertEqual(note["user"], OWNER)
        self.assertIsNone(broker.approve_work(out["task"], AJAY, True))  # only the owner of that neo decides

    def test_finishing_tells_both_and_the_askers_neo_gets_a_turn(self):
        out, _ = self.delegate("accept")
        work = Run.create("claude", "x", str(self.dir), "full", source="a2a", extra={"kind": "a2a-work"})
        (work.dir / "events.jsonl").write_text('{"kind": "result", "body": "Fixed the test in PR #12; CI is green."}\n')
        broker._set_task(out["task"], run=work.id)
        broker.finish(out["task"], work.update_meta(status="done"))
        notes = broker.take_outbox()
        to_asker = [o for o in notes if o["user"] == AJAY][0]
        self.assertTrue(to_asker["followup"])
        self.assertIn("PR #12", to_asker["text"])
        self.assertTrue(any(o["user"] == OWNER and "Finished" in o["text"] for o in notes))
        self.assertIn("result: Fixed the test", broker.tasks_text(AJAY, out["task"]))

    def test_work_runs_cant_delegate_further(self):
        self.assertFalse(broker.may_ask({"source": "slack", "kind": "a2a-work"}))

    def test_recover_finishes_work_that_ended_while_neo_was_down(self):
        out, _ = self.delegate("accept")
        work = Run.create("claude", "x", str(self.dir), "full", source="a2a", extra={"kind": "a2a-work"})
        work.update_meta(status="done")
        broker._set_task(out["task"], run=work.id)
        self.assertEqual(broker.recover(), [out["task"]])
        self.assertEqual(broker._task(out["task"])["status"], "done")


class ProgressTest(Base):
    def test_status_shows_steps_and_the_latest_one(self):
        work = Run.create("claude", "x", str(self.dir), "full", source="a2a", extra={"kind": "a2a-work"})
        (work.dir / "events.jsonl").write_text(
            '{"kind": "tool_call", "title": "Read: pricing.py"}\n{"kind": "tool_call", "title": "Bash: python3 -m unittest"}\n')
        self.assertEqual(broker.progress({"run": work.id}), "2 steps so far; latest: Bash: python3 -m unittest")
