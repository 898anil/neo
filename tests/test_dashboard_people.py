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
        self.assertEqual(h.out[-1], (200, "index.html"))  # the same dashboard as the owner's, with their own things
        h = self.handler("/api/me", cookie=cookie)
        h.do_GET()
        self.assertEqual([r["id"] for r in h.out[-1][1]["runs"]], [mine.id])
        h = self.handler(f"/api/me/runs/{theirs.id}", cookie=cookie)
        h.do_GET()
        self.assertEqual(h.out[-1][0], 404)
        h = self.handler(f"/api/runs/{theirs.id}", cookie=cookie)
        h.do_GET()
        self.assertEqual(h.out[-1][0], 404)  # someone else's run doesn't exist for them
        with patch("cos.fleet.agents", return_value=[]):
            h = self.handler("/api/overview", cookie=cookie)
            h.do_GET()
        self.assertEqual([r["id"] for r in h.out[-1][1]["runs"]], [mine.id])
        for path in ("/api/usage", "/api/sessions"):  # the owner's extras
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
            h = self.handler("/api/runs", method="POST", cookie=dashlink.session_value(AJAY),
                             body=json.dumps({"prompt": "fix it", "cwd": "/home/neo", "mode": "full"}).encode())
            h.do_POST()
            self.assertEqual(h.out[-1][0], 201)  # the dashboard's + button: still their account, whatever it asks for
        self.assertEqual({m["account"] for m in started}, {"neo-ajay"})
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
    """Slack connection instructions and the conversation-only bot reader."""

    def test_connection_guidance_uses_claude_connector(self):
        import asyncio
        import app
        from unittest.mock import AsyncMock
        client, say = AsyncMock(), AsyncMock()
        with patch.object(app.accounts, "enabled", return_value=True), \
                patch.object(app, "_account_of", AsyncMock(return_value="neo-ajay")), \
                patch.object(app.logins, "connector_status", return_value="missing"):
            asyncio.run(app._slack_sign_in(client, "C1", "1.0", AJAY, False, say))
        self.assertEqual(client.chat_postMessage.await_count, 1)
        message = str(client.chat_postMessage.await_args.kwargs["text"])
        self.assertIn("Claude", message)
        self.assertIn("Connectors", message)
        self.assertNotIn("client id", message.lower())

    def test_already_connected_does_not_send_setup_instructions(self):
        import asyncio
        import app
        from unittest.mock import AsyncMock
        client, say = AsyncMock(), AsyncMock()
        with patch.object(app.accounts, "enabled", return_value=True), \
                patch.object(app, "_account_of", AsyncMock(return_value="neo-ajay")), \
                patch.object(app.logins, "connector_status", return_value="connected"):
            asyncio.run(app._slack_sign_in(client, "D1", "1.0", AJAY, False, say))
        self.assertIn("connected", say.await_args.args[0])
        client.chat_postMessage.assert_not_awaited()

    def test_disconnect_removes_old_token_and_points_to_claude(self):
        import asyncio
        import app
        from cos import tokens
        from unittest.mock import AsyncMock
        tokens.STORE.update(lambda d: d.setdefault(AJAY, {}).update({"_slack": "xoxp-old"}))
        say = AsyncMock()
        asyncio.run(app._slack_sign_in(AsyncMock(), "D1", "1.0", AJAY, True, say))
        self.assertNotIn("_slack", tokens.STORE.read()[AJAY])
        self.assertIn("Claude", say.await_args.args[0])

    def test_legacy_token_does_not_grant_search(self):
        from cos import slackread, tokens
        run = Run.create("claude", "x", str(self.dir), "full", source="slack", extra={"slack_user": AJAY, "slack_channel": "C1"})
        tokens.STORE.update(lambda d: d.setdefault(AJAY, {}).update({"_slack": "xoxp-old"}))
        h = self.handler()
        with patch.object(slackread, "_api") as api:
            h._slack_read(run.meta(), {"slack": "search", "query": "terms"})
        api.assert_not_called()
        self.assertIn("Claude", h.out[-1][1]["text"])

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

    def test_home_tab_reports_verified_connector_status(self):
        from cos import home_tab
        text = str(home_tab.view("Ajay", {}, None, None, [], [], slack="connected"))
        self.assertIn("connected in Claude", text)
        self.assertNotIn("not connected", text.split("*Slack*")[1].split("action_id")[0])

    def test_home_tab_and_commands(self):
        import commands
        from cos import home_tab
        self.assertIn("home_connect_slack", str(home_tab.view("Ajay", {}, None, None, [], [], slack=False)))
        self.assertEqual(commands.parse("login slack").engine, "slack")
