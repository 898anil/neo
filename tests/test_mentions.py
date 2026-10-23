"""Mentions of the owner: found by a read-only check, decided by the owner, sent as them only when verified."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
NOW = 1_790_000_000.0


def hit(ts="1.1", **kw):
    return {"channel_id": "C1", "ts": ts, "thread_ts": "", "permalink": "https://x.slack.com/p1", "from": "Ajay",
            "asked": "Can you check the deploy?", "needs_owner": True, "why": "direct ask",
            "draft_reply": "Looking now.", "neo_task": "", **kw}


class FakeRun:
    def __init__(self, events):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))


def call(msg, channel="C1", thread="1.1"):
    return {"kind": "tool_call", "title": "mcp__claude_ai_Slack__slack_send_message",
            "body": json.dumps({"channel_id": channel, "thread_ts": thread, "message": msg})}


OK = {"kind": "tool_result", "body": "ok", "data": {}}


class MentionsTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp(), "NEO_NOTIFY_USER": "U9"})
        self.env.start()
        from cos import mentions
        self.m = mentions

    def tearDown(self):
        self.env.stop()

    def test_merge_keeps_asks_open_and_fyis_out_and_is_idempotent(self):
        new = self.m.merge({"mentions": [hit("1.1"), hit("2.2", needs_owner=False), hit("3.3", draft_reply="", neo_task="")]}, "r", NOW)
        self.assertEqual([n["ts"] for n in new], ["1.1"])
        self.assertEqual(new[0]["thread_ts"], "1.1")  # not in a thread: reply under the message itself
        self.assertEqual([i["ts"] for i in self.m.open_items()], ["1.1"])
        self.assertEqual(self.m.merge({"mentions": [hit("1.1", asked="changed")]}, "r2", NOW), [])

    def test_github_pr_notifications_are_dropped(self):
        new = self.m.merge({"mentions": [hit("1.1", channel_name="gh-acme-api", **{"from": "GitHub (PR by Praveen)"}),
                                         hit("2.2", **{"from": "GitHub"}),
                                         hit("3.3", channel_name="tech-core")]}, "r", NOW)
        self.assertEqual([i["ts"] for i in new], ["3.3"])
        self.assertIn("GitHub", self.m.prompt())

    def test_neo_own_posts_are_dropped(self):
        new = self.m.merge({"mentions": [hit("1.1", channel_name="ai-testing", **{"from": "neo (bot)"}),
                                         hit("2.2", **{"from": "Neo"}),
                                         hit("3.3", **{"from": "Neha"})]}, "r", NOW)
        self.assertEqual([i["ts"] for i in new], ["3.3"])
        self.assertIn("neo's own", self.m.prompt())

    def test_neo_posts_under_its_slack_bot_name_are_dropped(self):
        # The Slack app can be named anything ("Nio"): its own approval pings must not come back as asks.
        with patch.dict(os.environ, {"NEO_BOT_NAME": "nio"}):
            new = self.m.merge({"mentions": [hit("1.1", **{"from": "Nio (bot)"}), hit("2.2", **{"from": "Nio"}),
                                             hit("3.3", **{"from": "Nikhil"})]}, "r", NOW)
            self.assertEqual([i["ts"] for i in new], ["3.3"])
            self.assertIn("Nio", self.m.prompt())

    def test_neo_posts_found_before_the_fix_leave_the_open_list(self):
        self.m.merge({"mentions": [hit("1.1", **{"from": "Nio (bot)"}), hit("2.2")]}, "r", NOW)  # no bot name known yet
        with patch.dict(os.environ, {"NEO_BOT_NAME": "nio"}):
            self.assertEqual([i["ts"] for i in self.m.open_items()], ["2.2"])

    def test_prompt_names_the_owner_and_adds_configured_rules(self):
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "Alex", "NEO_OWNER_PROFILE": "an engineer at Acme",
                                     "NEO_MENTIONS_RULES": "Alex does not review PRs.\\nNo replies on Fridays."}):
            p = self.m.prompt()
        self.assertIn("You work for Alex (Slack user <@U9>), an engineer at Acme.", p)
        self.assertIn("- Alex does not review PRs.\n- No replies on Fridays.", p)
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "", "NEO_OWNER_PROFILE": "", "NEO_MENTIONS_RULES": ""}):
            p = self.m.prompt()
        self.assertIn("You work for the owner (Slack user <@U9>). Find", p)
        self.assertTrue(p.endswith("say so in the summary."))

    def test_prompt_lists_handled_mentions_and_forbids_sending(self):
        self.m.merge({"mentions": [hit("1.1")]}, "r", NOW)
        p = self.m.prompt()
        self.assertIn("<@U9>", p)
        self.assertIn("C1 1.1", p)
        self.assertIn("Never send", p)

    def test_check_cannot_write_to_slack(self):
        every, first = self.m.cli_args()
        allowed, denied = every[1].split(","), every[3].split(",")
        self.assertIn("mcp__claude_ai_Slack__slack_read_thread", allowed)
        self.assertFalse(any("send" in t or "canvas" in t for t in allowed))
        self.assertIn("mcp__claude_ai_Slack__slack_send_message", denied)

    def test_send_counts_only_when_exact_text_reached_the_right_thread(self):
        item = {"channel_id": "C1", "thread_ts": "1.1"}
        self.assertEqual(self.m.sent_ok(FakeRun([call("Looking  now."), OK]), item, "Looking now."), (True, "sent"))
        self.assertFalse(self.m.sent_ok(FakeRun([call("Looking now!"), OK]), item, "Looking now.")[0])
        self.assertFalse(self.m.sent_ok(FakeRun([call("Looking now.", thread="9.9"), OK]), item, "Looking now.")[0])
        self.assertFalse(self.m.sent_ok(FakeRun([call("Looking now.")]), item, "Looking now.")[0])
        self.assertFalse(self.m.sent_ok(FakeRun([call("a"), OK, call("a"), OK]), item, "a")[0])

    def test_dismissed_and_handed_off_mentions_leave_the_open_list(self):
        self.m.merge({"mentions": [hit("1.1"), hit("2.2", neo_task="fix it")]}, "r", NOW)
        ids = [i["id"] for i in self.m.open_items()]
        self.m.dismiss(ids[0])
        with patch.object(self.m.Run, "create", return_value=type("R", (), {"id": "r9"})()):
            self.m.hand_to_neo(ids[1])
        self.assertEqual(self.m.open_items(), [])
        self.assertEqual(self.m.get(ids[1])["status"], "working")

    def test_mentions_are_decisions_and_ping_at_once_with_the_draft(self):
        from cos import home, urgent
        self.m.merge({"mentions": [hit("1.1")]}, "r", NOW)
        out = home.build({"agents": [], "tasks": []}, [], {}, NOW - 3600, NOW + 60, mentions=self.m.open_items())
        self.assertEqual(out["decide"][0]["kind"], "mention")
        self.assertEqual(urgent.due(out, NOW + 60, local_hour=3), [])
        pings = urgent.due(out, NOW + 60, local_hour=11)
        self.assertEqual(len(pings), 1)
        self.assertIn("Looking now.", pings[0]["text"])


if __name__ == "__main__":
    unittest.main()
