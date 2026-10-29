"""Talking to neo without commands: plain-text requests about neo, the guided first-time setup by DM,
and each person's Home tab."""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import home_tab, intent, onboarding, people  # noqa: E402

OWNER, AJAY = "UOWNER1", "UAJAY01"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = patch.dict(os.environ, {"COS_HOME": str(self.dir), "NEO_PEOPLE_DIR": str(self.dir / "people"),
                                           "NEO_ACCOUNTS_FILE": str(self.dir / "accounts.json"),
                                           "NEO_OWNER_USER": OWNER, "NEO_OWNER_NAME": "Anil"})
        self.env.start()

    def tearDown(self):
        self.env.stop()


class IntentTest(Base):
    def test_only_short_messages_that_mention_neo_things_cost_a_call(self):
        self.assertTrue(intent.likely("send my brief at 8"))
        self.assertTrue(intent.likely("connect my github"))
        self.assertFalse(intent.likely("fix the flaky test in backie"))
        self.assertFalse(intent.likely("watch " + "x" * 500))

    def test_answers_neo_cant_act_on_are_tasks(self):
        self.assertIsNone(intent.check({"intent": "none"}))
        self.assertIsNone(intent.check({"intent": "connect", "tool": "gcloud"}))
        self.assertIsNone(intent.check({"intent": "brief_time", "time": ""}))
        self.assertIsNone(intent.check({"intent": "watch_add", "name": "ci", "what": ""}))
        self.assertIsNone(intent.check("connect"))
        self.assertEqual(intent.check({"intent": "watch_add", "name": "ci", "what": "CI failures", "every": 5})["every"], 30)
        self.assertEqual(intent.check({"intent": "connect", "tool": "gh"})["tool"], "gh")


class OnboardingTest(Base):
    def test_plain_replies_answer_the_step(self):
        self.assertEqual(onboarding.reply("claude", "use anil's for now"), "skip")
        self.assertEqual(onboarding.reply("claude", "yes please"), "connect")
        self.assertEqual(onboarding.reply("gh", "later"), "skip")
        self.assertEqual(onboarding.reply("brief", "8:30am"), "08:30")
        self.assertEqual(onboarding.reply("brief", "7 pm"), "19:00")
        self.assertEqual(onboarding.reply("brief", "no brief thanks"), "off")
        self.assertIsNone(onboarding.reply("claude", "fix the flaky login test in backie and open a PR please"))
        self.assertIsNone(onboarding.reply("brief", "hello"))
        # the whole message must be the answer (review 2026-10-29)
        for step, text in (("claude", "ok go ahead and merge it"), ("claude", "go fix the flaky test"),
                           ("claude", "connect my github"), ("gh", "no, wrong branch"), ("brief", "add 2 more tests"),
                           ("brief", "fix PR 7"), ("brief", "retry step 3"), ("brief", "7")):
            self.assertIsNone(onboarding.reply(step, text), (step, text))

    def test_one_welcome_however_many_messages_arrive(self):
        self.assertTrue(onboarding.claim(AJAY))
        self.assertFalse(onboarding.claim(AJAY))
        self.assertEqual(onboarding.step_of(AJAY), "claude")

    def test_typed_answers_lapse(self):
        onboarding.set_step(AJAY, "brief", "5.5")
        self.assertEqual(onboarding.step_ts(AJAY), "5.5")
        onboarding.STATE.update(lambda d: d[AJAY].update(at=0))
        self.assertIsNone(onboarding.step_ts(AJAY))

    def test_steps_in_order_and_whose_login_is_offered(self):
        self.assertEqual([onboarding.next_step(s) for s in onboarding.STEPS], ["gh", "brief", "done"])
        text, blocks = onboarding.message("claude", "Anil", True, False)
        labels = [e["text"]["text"] for e in blocks[-1]["elements"]]
        self.assertEqual(labels, ["Connect my Claude", "Use Anil's for now"])
        _, blocks = onboarding.message("gh", "Anil", True, False)
        self.assertEqual(blocks[-1]["elements"][1]["text"]["text"], "Skip for now")  # GitHub isn't lent to them

    def test_choices_move_them_through_the_steps(self):
        import app
        posted = []
        client = AsyncMock()
        client.chat_postMessage.side_effect = lambda **kw: posted.append(kw) or {"ts": "1.1", "channel": "D1"}
        onboarding.set_step(AJAY, "claude")

        async def go():
            with patch.object(app, "handle_login", AsyncMock()) as login, patch.object(app, "publish_home", AsyncMock()):
                await app.onboarding_choice(client, AJAY, "D1", "claude", "connect")
                login.assert_awaited_once()
                self.assertEqual(login.await_args.args[4].engine, "claude")
                await app.onboarding_choice(client, AJAY, "D1", "claude", "skip")  # an old button: ignored
                await app.onboarding_choice(client, AJAY, "D1", "gh", "skip")
                await app.onboarding_choice(client, AJAY, "D1", "brief", "08:00")
        asyncio.run(go())
        self.assertEqual(onboarding.step_of(AJAY), "done")
        self.assertEqual(people.daily(AJAY, "brief"), (8, 0))
        self.assertIn("You're set", posted[-1]["text"])


class HomeTabTest(Base):
    def test_their_page(self):
        view = home_tab.view("Ajay", {"claude": "own", "codex": "lent", "gh": "none"}, "09:00", None,
                             [{"title": "fix login", "status": "done", "url": "https://s/1"}],
                             [{"id": "ci-abc", "name": "ci", "every": 60}], "Anil")
        text = str(view)
        self.assertEqual(view["type"], "home")
        self.assertIn("Hi Ajay", text)
        self.assertIn("using Anil's for now", text)
        self.assertIn("fix login", text)
        self.assertIn("home_watch_remove_ci-abc", text)
        brief = next(b for b in view["blocks"] if b["type"] == "actions")["elements"][0]
        self.assertEqual(brief["initial_option"]["value"], "09:00")
        self.assertNotIn("web dashboard", text)


class ActThenUndoTest(Base):
    def test_a_plain_request_changes_the_setting_and_undo_puts_it_back(self):
        import app
        people.save(AJAY, AJAY, brief="09:00")
        client = AsyncMock()
        posted = []
        client.chat_postMessage.side_effect = lambda **kw: posted.append(kw) or {"ts": "2.2"}

        async def go():
            with patch.object(app, "publish_home", AsyncMock()), patch.object(app, "may_use", AsyncMock(return_value=True)):
                await app.handle_intent(client, "C1", "1.0", AJAY, {"intent": "brief_time", "time": "08:00"})
                self.assertEqual(people.daily(AJAY, "brief"), (8, 0))
                token = posted[-1]["blocks"][0]["elements"][0]["value"]
                body = {"user": {"id": "USOMEONE"}, "actions": [{"value": token}], "channel": {"id": "C1"}, "message": {"ts": "2.2"}}
                await app.on_undo(AsyncMock(), body, client)  # not theirs
                self.assertEqual(people.daily(AJAY, "brief"), (8, 0))
                await app.on_undo(AsyncMock(), {**body, "user": {"id": AJAY}}, client)
        asyncio.run(go())
        self.assertEqual(people.daily(AJAY, "brief"), (9, 0))

    def test_removing_a_watch_by_name_only_finds_theirs(self):
        import app
        from cos import watches
        watches.save({"name": "ci failures", "instructions": "x", "every": 60, "hours": "work", "owner": AJAY})
        watches.save({"name": "ci failures", "instructions": "x", "every": 60, "hours": "work", "owner": OWNER})
        req, undo, _ = app._commands_for(AJAY, {"intent": "watch_remove", "name": "CI failures"})
        self.assertEqual(watches.get(req.target)["owner"], AJAY)
        self.assertEqual(undo["op"], "watch_removed")
        req, _, ask = app._commands_for(AJAY, {"intent": "watch_remove", "name": "deploys"})
        self.assertIsNone(req)
        self.assertIn("Which watch", ask)


class ReviewFixesTest(Base):
    def test_undo_brings_a_removed_watch_back_as_it_was(self):
        import app
        from cos import watches
        mine = watches.save({"name": "ci failures", "instructions": "x", "every": 60, "hours": "work", "owner": AJAY})
        client = AsyncMock()
        posted = []
        client.chat_postMessage.side_effect = lambda **kw: posted.append(kw) or {"ts": "2.2"}

        async def go():
            with patch.object(app, "publish_home", AsyncMock()), patch.object(app, "may_use", AsyncMock(return_value=True)):
                await app.handle_intent(client, "C1", "1.0", AJAY, {"intent": "watch_remove", "name": "ci failures"})
                self.assertIsNone(watches.get(mine["id"]))
                token = posted[-1]["blocks"][0]["elements"][0]["value"]
                await app.on_undo(AsyncMock(), {"user": {"id": AJAY}, "actions": [{"value": token}],
                                                "channel": {"id": "C1"}, "message": {"ts": "2.2"}}, client)
        asyncio.run(go())
        back = watches.get(mine["id"])
        self.assertEqual((back["owner"], back["name"]), (AJAY, "ci failures"))

    def test_undo_for_a_watch_that_wasnt_made_deletes_nothing(self):
        import app
        from cos import watches
        existing = watches.save({"name": "ci failures", "instructions": "x", "every": 60, "hours": "work", "owner": AJAY})
        client = AsyncMock()
        posted = []
        client.chat_postMessage.side_effect = lambda **kw: posted.append(kw) or {"ts": "2.2"}

        async def go():
            with patch.object(app, "publish_home", AsyncMock()), \
                    patch.object(watches, "save", side_effect=ValueError("you already have 3 watches")):
                await app.handle_intent(client, "C1", "1.0", AJAY, {"intent": "watch_add", "name": "ci failures",
                                                                    "what": "CI", "every": 60})
        asyncio.run(go())
        self.assertFalse(any(p.get("blocks") for p in posted))  # no Undo offered
        self.assertIsNotNone(watches.get(existing["id"]))

    def test_a_code_for_an_ended_sign_in_never_becomes_a_task(self):
        import app
        app.SIGN_IN_THREADS.update(lambda d: d.update({"7.7": AJAY}))
        client = AsyncMock()
        event = {"channel": "D1", "channel_type": "im", "user": AJAY, "text": "abc123#def456ghi789jkl012mno345pqr678", "ts": "8.8",
                 "thread_ts": "7.7"}

        async def go():
            with patch.object(app, "refusal", AsyncMock(return_value=None)), patch.object(app, "handle_mention", AsyncMock()) as mention:
                await app.handle_dm(event, client)
                mention.assert_not_awaited()
        asyncio.run(go())
        self.assertIn("sign-in has ended", client.chat_postMessage.await_args.kwargs["text"])

    def test_a_second_sign_in_only_replaces_one_for_the_same_tool(self):
        import app

        class Waiting:
            def __init__(self, tool):
                self.user, self.tool, self.stopped = AJAY, tool, False

            def stop(self):
                self.stopped = True
        claude = Waiting("claude")
        app.LOGINS["1.1"] = claude

        async def go():
            with patch.object(app, "_account_of", AsyncMock(return_value="neo-ajay")), \
                    patch.object(app.logins, "begin", side_effect=RuntimeError("stop here")), \
                    patch.dict(os.environ, {"NEO_ACCOUNTS": "1"}):
                with self.assertRaises(RuntimeError):
                    await app.handle_login(AsyncMock(), "D1", None, AJAY, __import__("commands").Request("login", engine="gh"))
        asyncio.run(go())
        self.assertFalse(claude.stopped)
        self.assertIn("1.1", app.LOGINS)
        app.LOGINS.pop("1.1", None)

    def test_repos_named_like_memory_files_still_wait(self):
        from cos import learn
        with patch.dict(os.environ, {"NEO_MEMORY_DIR": str(self.dir / "memory")}):
            self.assertEqual(learn.add("x is y", "rule", repo="me", user=AJAY), learn.PENDING)
            self.assertEqual(learn.add("x is z", "rule", repo="team", user=AJAY), learn.PENDING)

    def test_login_commands_include_github(self):
        import commands
        self.assertEqual(commands.parse("login github").engine, "gh")
        self.assertEqual(commands.parse("logout gh").engine, "gh")


class DirectMessageTest(Base):
    def test_plain_text_in_a_dm_is_a_request(self):
        import app
        event = {"channel": "D1", "channel_type": "im", "user": AJAY, "text": "fix the flaky test", "ts": "5.5"}

        async def go():
            with patch.object(app, "handle_mention", AsyncMock()) as mention:
                await app.reply_to_dm_agent(AsyncMock(), event, "D1", None, None, "fix the flaky test")
                mention.assert_awaited_once()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
