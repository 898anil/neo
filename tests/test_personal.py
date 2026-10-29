"""neo as each person's own chief of staff: the owner in an account too, sign-ins from Slack, each
person's own brief, digest and watches."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import accounts, brief, logins, people  # noqa: E402

OWNER, AJAY = "UOWNER1", "UAJAY01"

# Stand-in for sudo + neo-run + the CLI's sign-in: saves the spec, prints a link, then takes a code.
FAKE_LOGIN = r"""
import json, os, sys
spec = json.loads(os.read(int(sys.argv[sys.argv.index('--spec-fd') + 1]), 65536))
open(sys.argv[1], 'w').write(json.dumps(spec))
argv = spec['argv']
if argv[:3] == ['claude', 'auth', 'login']:
    print('If the browser didn\'t open, visit: https://claude.com/cai/oauth/authorize?code=true&x=1', flush=True)
    for line in sys.stdin:
        if line.strip() == 'good#code':
            print('Login successful.', flush=True)
            sys.exit(0)
        print('Paste code here if prompted > Invalid code. Please make sure the full code was copied.', flush=True)
elif argv == ['codex', 'login', '--device-auth']:
    print('1. Open this link\n   https://auth.openai.com/codex/device\n2. Enter this one-time code\n   MKSA-RJI4C', flush=True)
    sys.exit(0)
elif argv == ['claude', 'auth', 'status']:
    print('{"loggedIn": %s, "authMethod": "claude.ai"}' % sys.argv[2])
elif argv == ['codex', 'login', 'status']:
    print('Logged in using ChatGPT' if sys.argv[2] == 'true' else 'Not logged in')
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_OWNER_USER": OWNER, "NEO_OWNER_NAME": "Anil", "NEO_ACCOUNTS": "1",
                                           "NEO_WORKDIR": "/home/neo/code"})
        self.env.start()
        self.made = []
        self.create = patch.object(accounts, "_create", side_effect=lambda account, user: self.made.append(account))
        self.create.start()

    def tearDown(self):
        self.create.stop()
        self.env.stop()

    def fake(self, logged_in: bool = False):
        out, state = str(self.dir / "spec.json"), "true" if logged_in else "false"
        return patch.object(accounts, "command", side_effect=lambda account, fd: [
            sys.executable, "-c", FAKE_LOGIN, out, state, "--spec-fd", str(fd)])

    def spec(self) -> dict:
        return json.loads((self.dir / "spec.json").read_text())


class OwnerAccountTest(Base):
    def test_the_owner_gets_an_account_too(self):
        self.assertEqual(accounts.owner_account(), "neo-anil")
        self.assertEqual(accounts.owner_account(), "neo-anil")  # once
        self.assertEqual(self.made, ["neo-anil"])

    def test_neos_workdir_maps_into_the_accounts_code(self):
        home = accounts.home("neo-anil")
        self.assertEqual(accounts.cwd_for("neo-anil", "/home/neo/code"), f"{home}/code")
        self.assertEqual(accounts.cwd_for("neo-anil", "/home/neo/code/backie"), f"{home}/code/backie")
        self.assertEqual(accounts.cwd_for("neo-anil", "/home/neo/codex"), "/home/neo/codex")  # not under it: neo-run decides

    def test_runs_nobody_placed_go_to_the_owners_account_not_the_service_account(self):
        from cos.runner import Run
        run = Run.create("claude", "check the loops", "/home/neo/code", "readonly", source="schedule", extra={"kind": "loops"})
        self.assertEqual(run.meta()["account"], "neo-anil")
        self.assertEqual(run.meta()["cwd"], str(Path(accounts.home("neo-anil") + "/code").resolve()))

    def test_live_agents_and_explicit_accounts_are_left_alone(self):
        from cos.runner import Run
        mine = Run.create("claude", "x", "/home/neo-ajay/code", "full", extra={"account": "neo-ajay"})
        self.assertEqual(mine.meta()["account"], "neo-ajay")
        with patch.dict(os.environ, {"NEO_ACCOUNTS": "0"}):
            here = Run.create("claude", "x", str(self.dir), "full")
        self.assertNotIn("account", here.meta())
        self.assertEqual(self.made, [])

    def test_nothing_is_lent_when_a_run_says_so(self):
        with patch.dict(os.environ, {"NEO_SHARED_CLAUDE_TOKEN": "sk-owner"}):
            self.assertEqual(accounts.spec({"cwd": "/x", "lend": False}, ["claude"], {}, {})["shared"], {})
            self.assertEqual(accounts.spec({"cwd": "/x"}, ["claude"], {}, {})["shared"]["claude_token"], "sk-owner")


class LoginTest(Base):
    def test_claude_sign_in_takes_the_code_from_slack(self):
        with self.fake(), patch.dict(os.environ, {"NEO_SHARED_CLAUDE_TOKEN": "sk-owner"}):
            login = logins.begin(AJAY, "neo-ajay", "claude")
            url, code = login.link(timeout=10)
            self.assertTrue(url.startswith("https://claude.com/cai/oauth/authorize"))
            self.assertIsNone(code)
            self.assertEqual(self.spec()["shared"], {})  # signs in as themselves, never as the owner
            self.assertEqual(self.spec()["argv"], ["claude", "auth", "login"])
            ok, why = login.answer("wrong", timeout=10)
            self.assertFalse(ok)
            self.assertIn("didn't work", why)
            ok, why = login.answer("good#code", timeout=10)
            self.assertTrue(ok, why)

    def test_codex_sign_in_shows_the_device_code(self):
        with self.fake():
            login = logins.begin(AJAY, "neo-ajay", "codex")
            self.assertEqual(login.link(timeout=10), ("https://auth.openai.com/codex/device", "MKSA-RJI4C"))
            self.assertTrue(login.wait(timeout=10))

    def test_only_tools_neo_knows(self):
        with self.assertRaises(ValueError):
            logins.begin(AJAY, "neo-ajay", "jira")

    def test_signed_in_reads_each_clis_status(self):
        with self.fake():
            self.assertFalse(logins.signed_in("neo-ajay", "claude"))
            self.assertFalse(logins.signed_in("neo-ajay", "codex"))
        with self.fake(logged_in=True):
            self.assertTrue(logins.signed_in("neo-ajay", "claude"))
            self.assertTrue(logins.signed_in("neo-ajay", "codex"))


class DailyTest(Base):
    def test_brief_and_digest_times(self):
        self.assertEqual(people.daily(AJAY, "brief"), (9, 0))
        people.save(AJAY, AJAY, brief="7:45", digest="off")
        self.assertEqual(people.daily(AJAY, "brief"), (7, 45))
        self.assertIsNone(people.daily(AJAY, "digest"))
        with self.assertRaises(ValueError):
            people.save(AJAY, AJAY, brief="7pm")
        self.assertIn("brief: 07:45", people.describe(AJAY))

    def test_set_commands(self):
        import commands
        req = commands.parse("set my digest 18:30")
        self.assertEqual((req.command, req.field, req.prompt), ("set", "digest", "18:30"))
        self.assertEqual(commands.parse("set my brief to be shorter").command, "run")
        self.assertEqual((commands.parse("login").command, commands.parse("login").engine), ("login", "claude"))
        self.assertEqual(commands.parse("logout codex").engine, "codex")
        self.assertEqual(commands.parse("login page is broken").command, "run")


class PersonalBriefTest(Base):
    def test_their_brief_runs_in_their_account_on_their_login_only(self):
        from cos.runner import Run
        started = []
        with patch.object(Run, "execute", lambda self: started.append(self.meta()) or self.update_meta(status="done")):
            meta = brief.start_for(AJAY, "neo-ajay", "Ajay", datetime(2026, 10, 29, 9, 0))
            for _ in range(50):
                if started:
                    break
                asyncio.run(asyncio.sleep(0.02))
        self.assertEqual((meta["account"], meta["slack_user"], meta["lend"]), ("neo-ajay", AJAY, False))
        self.assertIn("Ajay", meta["prompt"])
        self.assertNotIn("Anil", meta["prompt"])
        self.assertIsNone(brief.running())  # the owner's brief isn't theirs
        self.assertIsNone(brief.latest())

    def test_their_brief_isnt_kept_after_it_was_sent(self):
        from cos import cards
        from cos.runner import Run
        run = Run.create("claude", "brief", "/home/neo-ajay/code", "full",
                         extra={"kind": "brief", "account": "neo-ajay", "slack_user": AJAY, "lend": False})
        (run.dir / "events.jsonl").write_text('{"kind": "result", "body": "Mail from the CFO about layoffs"}\n')
        (run.dir / "raw.jsonl").write_text("secret mail\n")
        run.update_meta(status="done")
        brief.forget(run)
        self.assertEqual(sorted(p.name for p in run.dir.iterdir()), ["events.jsonl", "meta.json"])
        self.assertNotIn("CFO", (run.dir / "events.jsonl").read_text())
        self.assertEqual(cards.pending({"tasks": [run.meta()]}), [])

    def test_nothing_extra_reaches_a_run_that_lends_nothing(self):
        with patch.dict(os.environ, {"NEO_ACCOUNT_ENV": "DATADOG_API_KEY"}):
            env = {"DATADOG_API_KEY": "dd-owner", "NEO_RUN_ID": "r1"}
            self.assertNotIn("DATADOG_API_KEY", accounts.spec({"cwd": "/x", "lend": False}, ["claude"], env, {})["env"])
            self.assertEqual(accounts.spec({"cwd": "/x"}, ["claude"], env, {})["env"]["DATADOG_API_KEY"], "dd-owner")


class WatchTest(Base):
    def test_someones_own_watch_runs_in_their_account(self):
        from cos import watches
        accounts._save_registry({AJAY: "neo-ajay"})
        watch = watches.save({"name": "my errors", "instructions": "check my service", "every": 60, "hours": "always", "owner": AJAY})
        with patch("cos.watches.claude_available", return_value=(True, "")), \
                patch("cos.runner.Run.execute", lambda self: self.update_meta(status="done")), \
                patch.object(watches, "checked"):
            meta = watches.start(watch["id"], "dashboard")
        self.assertEqual((meta["account"], meta["slack_user"]), ("neo-ajay", AJAY))
        self.assertEqual(meta["cwd"], str(Path(accounts.home("neo-ajay") + "/code").resolve()))

    def test_their_watch_gets_only_what_their_role_is_lent_and_stops_when_blocked(self):
        from cos import watches
        accounts._save_registry({AJAY: "neo-ajay"})
        people.save(AJAY, OWNER, role="prs-only")
        watch = watches.save({"name": "ci", "instructions": "check CI", "every": 60, "hours": "always", "owner": AJAY})
        with patch("cos.watches.claude_available", return_value=(True, "")), \
                patch("cos.runner.Run.execute", lambda self: self.update_meta(status="done")), \
                patch.object(watches, "checked"), \
                patch.dict(os.environ, {"NEO_SHARED_GH_TOKEN": "ghp-owner", "NEO_SHARED_CLAUDE_TOKEN": "sk-owner"}):
            meta = watches.start(watch["id"], "dashboard")
            self.assertEqual(meta["role"], "prs-only")
            self.assertEqual(accounts.spec(meta, ["claude"], {}, {})["shared"]["gh_token"], "")  # GitHub: full only
            people.save(AJAY, OWNER, role="blocked")
            for _ in range(50):
                if not watches.running(watch["id"]):
                    break
                asyncio.run(asyncio.sleep(0.02))
            self.assertIsNone(watches.start(watch["id"], "schedule"))

    def test_watch_scheduler_skips_people_neo_no_longer_works_for(self):
        import app
        from cos import watches
        watch = watches.save({"name": "ci", "instructions": "check CI", "every": 60, "hours": "always", "owner": AJAY})
        started = []

        async def go():
            with patch.object(watches, "due", return_value=[watch]), \
                    patch.object(watches, "start", side_effect=lambda *a: started.append(a)), \
                    patch.object(app, "may_use", return_value=False), \
                    patch("app.asyncio.sleep", side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await app.watch_scheduler(None)
        asyncio.run(go())
        self.assertEqual(started, [])


class SlackLoginTest(Base):
    class Client:
        def __init__(self):
            self.posts = []

        async def chat_postMessage(self, channel, text, thread_ts=None, **_):
            self.posts.append((channel, thread_ts, text))
            return {"ts": f"9.{len(self.posts)}", "channel": "D1" if channel.startswith("U") else channel}

        async def users_info(self, user):
            return {"user": {"id": user, "tz": "Asia/Kolkata", "real_name": "Ajay Arora", "profile": {"real_name": "Ajay Arora"}}}

    def test_login_dms_the_link_and_the_reply_finishes_it(self):
        import app
        import commands
        client = self.Client()
        accounts._save_registry({AJAY: "neo-ajay"})

        async def go():
            with self.fake():
                await app.handle_login(client, "C1", "1.0", AJAY, commands.parse("login claude"))
                dm_ts = next(ts for ts, login in app.LOGINS.items() if login.user == AJAY)
                await app.answer_login(client, "D1", dm_ts, "USOMEONE", "good#code")  # not theirs: ignored
                self.assertIn(dm_ts, app.LOGINS)
                await app.answer_login(client, "D1", dm_ts, AJAY, "`good#code`")
                self.assertNotIn(dm_ts, app.LOGINS)
        asyncio.run(go())
        dm = [p for p in client.posts if p[0] == AJAY]
        self.assertIn("https://claude.com/cai/oauth/authorize", dm[0][2])
        self.assertTrue(any("sent you a DM" in p[2] for p in client.posts if p[0] == "C1"))
        self.assertIn("Signed in to Claude", client.posts[-1][2])
        self.assertIn("morning brief at 09:00", client.posts[-1][2])

    def test_personal_digest_lists_only_their_runs_today(self):
        import app
        from cos.runner import Run
        Run.create("claude", "fix the login bug", "/home/neo-ajay/code", "full",
                   extra={"account": "neo-ajay", "slack_user": AJAY, "slack_url": "https://s/1"}).update_meta(status="done")
        Run.create("claude", "owner things", "/home/neo-anil/code", "full", extra={"account": "neo-anil", "slack_user": OWNER})
        text = app.personal_digest(AJAY, datetime.now().replace(hour=0, minute=0).timestamp())
        self.assertIn("fix the login bug", text)
        self.assertNotIn("owner things", text)
        self.assertEqual(app.personal_digest("UNOBODY1", 0), "")


if __name__ == "__main__":
    unittest.main()


class SharedNotesTest(Base):
    """What reaches everyone's runs (lessons, repo notes) is the owner's call."""

    def setUp(self):
        super().setUp()
        self.mem = patch.dict(os.environ, {"NEO_MEMORY_DIR": str(self.dir / "memory")})
        self.mem.start()

    def tearDown(self):
        self.mem.stop()
        super().tearDown()

    def test_a_teammates_shared_note_waits_and_the_owners_doesnt(self):
        from cos import learn
        self.assertEqual(learn.add("deploys need the exit node", "lesson", user=AJAY), learn.PENDING)
        self.assertEqual(learn.add("backie tests need docker", "rule", repo="backie", user=AJAY), learn.PENDING)
        self.assertEqual(learn.add("answer in bullets", "rule", user=AJAY), f"people/{AJAY}.md")  # their own: no wait
        self.assertEqual(learn.add("staging is us-east1", "lesson"), learn.LESSONS)  # the owner's
        self.assertNotIn("exit node", learn.brief(None))  # not in anyone's runs yet, the owner's included
        waiting = learn.pending()
        self.assertEqual({(p["target"], p["by"]) for p in waiting}, {("lessons.md", AJAY), ("repos/backie.md", AJAY)})
        lesson = next(p for p in waiting if p["target"] == "lessons.md")
        self.assertEqual(learn.approve(lesson["id"]), "lessons.md")
        self.assertIn("exit node", learn.brief(None))
        repo_note = next(p for p in learn.pending())
        self.assertTrue(learn.reject(repo_note["id"]))
        self.assertEqual(learn.pending(), [])
        self.assertFalse((self.dir / "memory" / "repos" / "backie.md").exists())

    def test_only_the_owner_approves(self):
        import app
        from cos import learn
        learn.add("deploys need the exit node", "lesson", user=AJAY)
        item = learn.pending()[0]
        body = {"user": {"id": AJAY}, "actions": [{"action_id": f"home_note_approve_{item['id']}", "value": item["id"]}]}

        async def go():
            from unittest.mock import AsyncMock
            with patch.object(app, "publish_home", AsyncMock()):
                await app.on_home_note(AsyncMock(), body, AsyncMock())
                self.assertEqual(len(learn.pending()), 1)  # Ajay can't approve his own
                await app.on_home_note(AsyncMock(), {**body, "user": {"id": OWNER}}, AsyncMock())
        asyncio.run(go())
        self.assertEqual(learn.pending(), [])
        self.assertIn("exit node", learn.brief(None))

    def test_the_owners_home_tab_shows_them(self):
        from cos import home_tab
        view = home_tab.view("Anil", {}, None, None, [], [], pending=[
            {"id": "abc123", "text": "deploys need the exit node", "target": "lessons.md", "by": AJAY}])
        self.assertIn("home_note_approve_abc123", str(view))
        self.assertIn("exit node", str(view))


class ToolsTest(Base):
    """gcloud and Doppler sign in from Slack; Sentry, Neon and Linear connect with a token typed into a form."""

    def test_neo_run_allows_exactly_the_sign_ins_neo_sends(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_accounts import load_script
        mod = load_script("neo-run")
        for (tool, action), argv in logins.ARGV.items():
            if tool in ("claude", "codex"):
                continue
            self.assertIn(tuple(argv[1:]), mod.SIGN_INS[tool], (tool, action))
        for tool, args in (("gcloud", ["auth", "print-access-token"]), ("doppler", ["secrets", "download"]),
                           ("gcloud", ["auth", "login", "--no-launch-browser", "--cred-file=/x"])):
            with self.assertRaises(SystemExit, msg=args):
                mod.engine_path(tool, args)

    def test_doppler_and_gcloud_sign_ins(self):
        text = ("Complete authorization at https://dashboard.doppler.com/workplace/auth/cli\nYour auth code is:\n"
                "yonkers_feet_indianapolis_pudding_hollywood\nWaiting...")
        self.assertEqual(logins.DEVICE_CODE["doppler"].search(text).group(1), "yonkers_feet_indianapolis_pudding_hollywood")
        self.assertIn("gcloud", logins.PASTE)
        with patch.object(logins, "run", return_value=(0, "ajay@acme.com\n")):
            self.assertTrue(logins.signed_in("neo-ajay", "gcloud"))
        with patch.object(logins, "run", return_value=(0, "")):
            self.assertFalse(logins.signed_in("neo-ajay", "gcloud"))
        with patch.object(logins, "run", return_value=(1, "Doppler Error: you must provide a token")):
            self.assertFalse(logins.signed_in("neo-ajay", "doppler"))

    def test_a_token_reaches_only_its_persons_own_runs(self):
        from cos import tokens
        tokens.save(AJAY, "sentry", "sntrys_abcdefghijklmnop")
        with self.assertRaises(ValueError):
            tokens.save(AJAY, "sentry", "has spaces in it")
        with self.assertRaises(ValueError):
            tokens.save(AJAY, "gitlab", "abcdefghijkl")
        self.assertEqual(oct(tokens.STORE.path().stat().st_mode & 0o777), "0o600")
        mine = accounts.spec({"cwd": "/x", "slack_user": AJAY}, ["claude"], {}, {})
        self.assertEqual(mine["tokens"], {"SENTRY_AUTH_TOKEN": "sntrys_abcdefghijklmnop"})
        self.assertEqual(accounts.spec({"cwd": "/x", "slack_user": OWNER}, ["claude"], {}, {})["tokens"], {})
        answering = accounts.spec({"cwd": "/x", "slack_user": AJAY, "lend": "claude"}, ["claude"], {}, {})
        self.assertEqual(answering["tokens"], {})  # a CoS answering someone else never gets them
        self.assertTrue(tokens.remove(AJAY, "sentry"))
        self.assertEqual(tokens.env_for(AJAY), {})

    def test_neo_run_takes_only_tool_like_names(self):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_accounts import load_script
        mod = load_script("neo-run")
        self.assertEqual(mod.token_env({"SENTRY_AUTH_TOKEN": "t", "LD_PRELOAD": "/x.so", "PATH": "/evil", "lower": "x"}),
                         {"SENTRY_AUTH_TOKEN": "t"})

    def test_the_form_saves_a_good_token_and_explains_a_bad_one(self):
        import app
        from unittest.mock import AsyncMock
        from cos import tokens

        def body(value):
            return {"user": {"id": AJAY}, "view": {"private_metadata": "neon",
                                                   "state": {"values": {"token": {"value": {"value": value}}}}}}

        async def go():
            ack, client = AsyncMock(), AsyncMock()
            with patch.object(app, "may_use", AsyncMock(return_value=True)), patch.object(app, "publish_home", AsyncMock()):
                await app.on_token_form(ack, body("bad token"), client)
                self.assertEqual(ack.await_args.kwargs.get("response_action"), "errors")
                await app.on_token_form(ack, body("napi_abcdefghijklmnop"), client)
        asyncio.run(go())
        self.assertEqual(tokens.env_for(AJAY), {"NEON_API_KEY": "napi_abcdefghijklmnop"})

    def test_commands_and_plain_words_know_the_tools(self):
        import commands
        from cos import intent
        self.assertEqual(commands.parse("login gcloud").engine, "gcloud")
        self.assertEqual(commands.parse("login sentry").engine, "sentry")
        self.assertTrue(intent.likely("connect my google cloud"))
        self.assertTrue(intent.likely("connect my sentry"))


class SignInCodeTest(unittest.TestCase):
    def test_the_code_comes_out_of_whatever_they_typed_around_it(self):
        code = "AbCdEf123_-xyz#State_456-abc"
        for reply in (code, f"<@U0C0W6X3ZNV> {code}", f"`{code}`", f"here it is: {code} thanks", f"<@U0C0W6X3ZNV>\n```{code}```"):
            self.assertEqual(logins.code_from(reply), code, reply)
        self.assertEqual(logins.code_from("4/0AfJohXk_long-google_code"), "4/0AfJohXk_long-google_code")
        self.assertEqual(logins.code_from("<@U0C0W6X3ZNV> hi"), "")
