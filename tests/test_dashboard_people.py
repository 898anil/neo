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

    def test_the_link_shows_a_button_and_only_the_post_signs_in(self):
        token = dashlink.link_token(AJAY)
        sent = []

        def capture(h):
            h.send_response = lambda code: sent.append(("status", int(code)))
            h.send_header = lambda k, v: sent.append((k, v))
            h.end_headers = lambda: None
            return h
        capture(self.handler(f"/login?t={token}")).do_GET()  # a link preview or scanner fetching it
        self.assertNotIn("Set-Cookie", dict(sent))
        sent.clear()
        h = capture(self.handler("/login", method="POST", body=json.dumps({"t": token}).encode(),
                                 headers={"Origin": "https://spark.example.ts.net:8443"}))
        h.do_POST()
        cookie = dict(sent)["Set-Cookie"]
        self.assertIn(("status", 204), sent)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(dashlink.session_user(cookie.split(";")[0].split("=", 1)[1]), AJAY)
        sent.clear()
        capture(self.handler("/login", method="POST", body=json.dumps({"t": token}).encode())).do_POST()
        self.assertNotIn("Set-Cookie", dict(sent))  # once

    def test_logout_and_revoke_end_sessions(self):
        value = dashlink.session_value(AJAY)
        self.assertTrue(dashlink.session_alive(AJAY))
        dashlink.revoke(AJAY)
        self.assertIsNone(dashlink.session_user(value))
        self.assertFalse(dashlink.session_alive(AJAY))
        self.assertEqual(dashlink.session_user(dashlink.session_value(AJAY)), AJAY)  # a new link works again

    def test_odd_signatures_and_a_broken_key_are_safe(self):
        self.assertIsNone(dashlink.redeem(dashlink._b64("U1.9999999999.ab") + ".\u00e9"))
        self.assertIsNone(dashlink.session_user("U1.9999999999.0.\u00e9"))
        (self.dir / "dash.key").write_bytes(b"")
        self.assertGreaterEqual(len(dashlink._key()), 32)


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

    def test_public_traffic_faking_localhost_gets_nothing(self):
        with patch.dict(os.environ, {"COS_TOKEN": ""}), patch.object(server, "LINUX", True):
            faked = self.handler("/api/overview", headers={"Host": "127.0.0.1", "Tailscale-Funnel-Request": "?1"})
            self.assertFalse(faked._authorized())
            root = self.handler("/api/overview", headers={"Host": "127.0.0.1"})  # tailscaled forwarding serve traffic
            self.assertFalse(root._authorized())
            neo = self.handler("/api/overview", headers={"Host": "127.0.0.1"})
            neo._peer_uid = lambda: os.getuid()
            self.assertTrue(neo._authorized())

    def test_a_blocked_persons_session_stops_working(self):
        cookie = dashlink.session_value(AJAY)
        people.save(AJAY, OWNER, role="blocked")
        h = self.handler("/api/me", cookie=cookie)
        h.do_GET()
        self.assertEqual(h.out[-1][0], 403)

    def test_starting_tasks_is_limited(self):
        for _ in range(me.ACTIVE_PER_PERSON):
            Run.create("claude", "x", str(self.dir), "full", source="dashboard", extra={"slack_user": AJAY}).update_meta(status="running")
        with self.assertRaises(ValueError):
            me.check_rate(AJAY)

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


class ConnectSlackTest(Base):
    """"connect my slack": the person's own Slack, used only through neo-slack."""

    def setUp(self):
        super().setUp()
        self.slack_env = patch.dict(os.environ, {"SLACK_CLIENT_ID": "123.456", "SLACK_CLIENT_SECRET": "s3cret"})
        self.slack_env.start()

    def tearDown(self):
        self.slack_env.stop()
        super().tearDown()

    def state(self, user):
        from cos import slackauth
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(slackauth.authorize_url(user)).query)["state"][0]

    def test_connects_once_and_only_as_the_person_the_link_was_for(self):
        from cos import slackauth
        ok = lambda code: {"ok": True, "authed_user": {"id": AJAY, "access_token": "xoxp-ajay"}}  # noqa: E731
        state = self.state(AJAY)
        self.assertEqual(slackauth.connect("c1", state, exchange=ok), AJAY)
        self.assertEqual(slackauth.token(AJAY), "xoxp-ajay")
        with self.assertRaises(ValueError):
            slackauth.connect("c1", state, exchange=ok)  # once
        with self.assertRaises(ValueError):  # someone else's Slack can't land on Ajay's neo
            slackauth.connect("c2", self.state(AJAY), exchange=lambda c: {"ok": True, "authed_user": {"id": YOSH, "access_token": "x"}})
        from cos import tokens
        self.assertNotIn("xoxp-ajay", str(tokens.env_for(AJAY)))  # never handed to runs as an env var

    def test_the_pasted_address_is_read_through_slack_formatting(self):
        from cos import slackauth
        url = "https://spark.example.ts.net:8443/slack/oauth?code=abc.123&amp;state=U1.99.n.sig"
        self.assertEqual(slackauth.from_pasted(f"<{url}>"), ("abc.123", "U1.99.n.sig"))
        self.assertEqual(slackauth.from_pasted("hello"), ("", ""))

    def test_search_needs_their_own_slack_and_uses_it(self):
        from cos import slackauth, slackread, tokens
        run = Run.create("claude", "x", str(self.dir), "full", source="slack", extra={"slack_user": AJAY, "slack_channel": "C1"})
        h = self.handler()
        h._slack_read(run.meta(), {"slack": "search", "query": "terms"})
        self.assertIn("connect my slack", h.out[-1][1]["text"])
        tokens.STORE.update(lambda d: d.setdefault(AJAY, {}).update({"_slack": "xoxp-ajay"}))
        seen = []
        with patch.object(slackread, "_api", side_effect=lambda m, token=None, **p: seen.append((m, token)) or
                          {"ok": True, "messages": {"matches": []}}):
            h._slack_read(run.meta(), {"slack": "search", "query": "terms"})
        self.assertEqual(seen[0], ("search.messages", "xoxp-ajay"))

    def test_hints_say_what_to_do(self):
        import app
        self.assertIn("connect my claude", app.setup_hint("Not logged in · Please run /login"))
        self.assertIn("connect my github", app.setup_hint("fatal: could not read Username for 'https://github.com'"))
        self.assertIsNone(app.setup_hint("All done, PR #12 is open."))

    def test_the_nudge_comes_once_a_day(self):
        import asyncio
        import app
        from unittest.mock import AsyncMock
        client = AsyncMock()

        async def go():
            with patch.object(app, "login_states", AsyncMock(return_value={"claude": "lent", "gh": "none"})):
                await app.nudge_setup(client, AJAY)
                await app.nudge_setup(client, AJAY)
        asyncio.run(go())
        self.assertEqual(client.chat_postMessage.await_count, 1)
        text = client.chat_postMessage.await_args.kwargs["text"]
        self.assertIn("connect my github", text)
        self.assertIn("connect my slack", text)

    def test_home_tab_and_commands(self):
        import commands
        from cos import home_tab
        self.assertIn("home_connect_slack", str(home_tab.view("Ajay", {}, None, None, [], [], slack=False)))
        self.assertEqual(commands.parse("login slack").engine, "slack")
