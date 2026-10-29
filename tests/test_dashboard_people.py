"""Everyone's own web dashboard: sign-in links from Slack, sessions, and a person seeing only their own work."""

import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import accounts, dashlink, me, people, server  # noqa: E402
from cos.runner import Run  # noqa: E402

OWNER, AJAY, YOSH = "UOWNER1", "UAJAY01", "UYOSH01"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_MEMORY_DIR": str(self.dir / "memory"), "NEO_OWNER_USER": OWNER,
                                           "NEO_OWNER_NAME": "Anil", "COS_TOKEN": "owner-secret",
                                           "NEO_DASHBOARD_URL": "https://spark.example.ts.net:8443"})
        self.env.start()
        accounts._save_registry({AJAY: "neo-ajay", YOSH: "neo-yosh"})

    def tearDown(self):
        self.env.stop()

    def handler(self, path="/", method="GET", cookie=None, headers=None, body=b""):
        h = server.Handler.__new__(server.Handler)
        msg = Message()
        for k, v in {"Host": "spark.example.ts.net:8443", "Content-Type": "application/json",
                     "Content-Length": str(len(body)), **(headers or {})}.items():
            msg[k] = v
        if cookie:
            msg["Cookie"] = f"{dashlink.COOKIE}={cookie}"
        h.headers, h.path, h.rfile, h.wfile = msg, path, io.BytesIO(body), io.BytesIO()
        h.client_address, h.request_version, h.command = ("127.0.0.1", 50000), "HTTP/1.1", method
        h.out = []
        h._error = lambda status, text: h.out.append((int(status), text))
        h._json = lambda data, status=200: h.out.append((int(status), data))
        h._static = lambda name, ctype: h.out.append((200, name))
        h._peer_uid = lambda: 0  # tailscaled forwarding Funnel / serve traffic
        return h


class LinkTest(Base):
    def test_a_link_works_once_and_expires(self):
        token = dashlink.link_token(AJAY)
        self.assertEqual(dashlink.redeem(token), AJAY)
        self.assertIsNone(dashlink.redeem(token))  # once
        old = dashlink.link_token(AJAY, now=1000)
        self.assertIsNone(dashlink.redeem(old))  # expired
        packed, sig = dashlink.link_token(AJAY).rsplit(".", 1)
        forged = dashlink._b64(dashlink._unb64(packed).replace(AJAY, OWNER)) + "." + sig
        self.assertIsNone(dashlink.redeem(forged))
        self.assertTrue(dashlink.link(AJAY).startswith("https://spark.example.ts.net:8443/login?t="))

    def test_sessions_are_signed_and_expire(self):
        value = dashlink.session_value(AJAY)
        self.assertEqual(dashlink.session_user(value), AJAY)
        self.assertIsNone(dashlink.session_user(value.replace(AJAY, OWNER)))
        self.assertIsNone(dashlink.session_user(dashlink.session_value(AJAY, now=1000)))

    def test_opening_the_link_sets_the_session(self):
        sent = []
        h = self.handler(f"/login?t={dashlink.link_token(AJAY)}")
        h.send_response = lambda code: sent.append(("status", int(code)))
        h.send_header = lambda k, v: sent.append((k, v))
        h.end_headers = lambda: None
        h.do_GET()
        cookie = dict(sent)["Set-Cookie"]
        self.assertIn(("status", 303), sent)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(dashlink.session_user(cookie.split(";")[0].split("=", 1)[1]), AJAY)


class PersonViewTest(Base):
    def test_a_person_sees_only_their_page_and_runs(self):
        mine = Run.create("claude", "fix the login test", str(self.dir), "full", source="slack", extra={"slack_user": AJAY})
        theirs = Run.create("claude", "owner things", str(self.dir), "full", source="slack", extra={"slack_user": YOSH})
        cookie = dashlink.session_value(AJAY)
        h = self.handler("/", cookie=cookie)
        h.do_GET()
        self.assertEqual(h.out[-1], (200, "me.html"))
        h = self.handler("/api/me", cookie=cookie)
        h.do_GET()
        self.assertEqual([r["id"] for r in h.out[-1][1]["runs"]], [mine.id])
        h = self.handler(f"/api/me/runs/{theirs.id}", cookie=cookie)
        h.do_GET()
        self.assertEqual(h.out[-1][0], 404)
        for path in ("/api/overview", "/api/home", f"/api/runs/{theirs.id}", "/api/watches"):
            h = self.handler(path, cookie=cookie)
            h.do_GET()
            self.assertEqual(h.out[-1][0], 403, path)

    def test_no_session_and_no_owner_proof_gets_nothing(self):
        h = self.handler("/api/me")
        h.do_GET()
        self.assertEqual(h.out[-1][0], 403)

    def test_the_owner_signed_in_from_slack_gets_the_full_dashboard(self):
        h = self.handler("/", cookie=dashlink.session_value(OWNER))
        h.do_GET()
        self.assertEqual(h.out[-1], (200, "index.html"))

    def test_public_funnel_traffic_cant_claim_a_tailnet_identity(self):
        env = {"COS_TAILSCALE_USERS": "anil@x.com"}
        headers = {"Tailscale-User-Login": "anil@x.com", "Tailscale-Funnel-Request": "?1"}
        with patch.dict(os.environ, env):
            self.assertIsNone(self.handler(headers=headers)._tailscale_user())
            self.assertEqual(self.handler(headers={"Tailscale-User-Login": "anil@x.com"})._tailscale_user(), "anil@x.com")

    def test_starting_a_task_runs_it_in_their_own_account(self):
        started = []
        body = json.dumps({"prompt": "fix the flaky test", "engine": "claude"}).encode()
        with patch.dict(os.environ, {"NEO_ACCOUNTS": "1"}), patch.object(accounts, "_create"), \
                patch.object(server, "start_turn", side_effect=lambda run, prompt: started.append(run.meta())):
            h = self.handler("/api/me/runs", method="POST", cookie=dashlink.session_value(AJAY), body=body)
            h.do_POST()
            self.assertEqual(h.out[-1][0], 201)
            h = self.handler("/api/runs", method="POST", cookie=dashlink.session_value(AJAY), body=body)
            h.do_POST()
            self.assertEqual(h.out[-1][0], 403)  # the owner's run endpoint isn't theirs
        meta = started[0]
        self.assertEqual((meta["account"], meta["slack_user"], meta["source"]), ("neo-ajay", AJAY, "dashboard"))
        self.assertIn("Ajay", meta["system_prompt"] + me._name(AJAY))

    def test_their_role_still_applies(self):
        people.save(AJAY, OWNER, role="read-only")
        with patch.dict(os.environ, {"NEO_ACCOUNTS": "1"}), patch.object(accounts, "_create"):
            run = me.start(AJAY, "look at the logs", "codex")
        self.assertEqual((run.meta()["engine"], run.meta()["mode"]), ("claude", "readonly"))
        people.save(AJAY, OWNER, role="blocked")
        with patch.dict(os.environ, {"NEO_ACCOUNTS": "1"}), self.assertRaises(PermissionError):
            me.start(AJAY, "x")


class SlackSideTest(Base):
    def test_home_tab_button_and_plain_words(self):
        from cos import home_tab, intent
        self.assertIn("home_dashboard", str(home_tab.view("Ajay", {}, None, None, [], [])))
        self.assertTrue(intent.likely("open my dashboard"))
        self.assertTrue(intent.likely("give me the neo dashboard"))


if __name__ == "__main__":
    unittest.main()
