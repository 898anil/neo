"""Urgent Slack pings (once each) and the evening digest."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
NOW = 1_790_000_000.0


def payload(decide=(), regressed=(), moved=(), in_flight=(), quiet=0, moved_more=0, unfinished=()):
    p = {"since": NOW - 3600, "now": NOW, "decide": list(decide), "unfinished": list(unfinished), "regressed": list(regressed), "moved": list(moved),
         "moved_more": moved_more, "in_flight": list(in_flight), "quiet": quiet}
    p["counts"] = {"decide": len(p["decide"]), "regressed": len(p["regressed"]), "moved": len(p["moved"]) + moved_more,
                   "in_flight": len(p["in_flight"]), "quiet": quiet, "unfinished": len(p["unfinished"])}
    return p


def ask(key, age, kind="agent", at=NOW - 1000, **kw):
    return {"key": key, "kind": kind, "title": key, "question": "Approve?", "recommend": "Yes", "age": age, "at": at,
            "href": "#/x", "urgent": kind != "loop", **kw}


class UrgentTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        from cos import urgent
        self.urgent = urgent

    def tearDown(self):
        self.env.stop()

    def test_regression_pings_immediately(self):
        p = payload(regressed=[{"key": "loop:1", "title": "HubSpot invalid_client", "line": "14 events after deploy",
                                "evidence": "Sentry", "href": "https://x", "at": NOW - 10}])
        due = self.urgent.due(p, NOW)
        self.assertEqual([d["reason"] for d in due], ["regressed"])
        self.assertIn("HubSpot invalid_client", due[0]["text"])

    def test_decision_pings_after_30_min_only(self):
        self.assertEqual(self.urgent.due(payload(decide=[ask("agent:a", 600)]), NOW), [])
        self.assertEqual(len(self.urgent.due(payload(decide=[ask("agent:a", 1900)]), NOW)), 1)

    def test_stalled_loops_wait_for_the_digest(self):
        self.assertEqual(self.urgent.due(payload(decide=[ask("loop:1", 90000, kind="loop")]), NOW), [])

    def test_urgent_pings_once_across_restart(self):
        p = payload(decide=[ask("agent:a", 1900)])
        self.urgent.mark_sent(self.urgent.due(p, NOW), NOW)
        import importlib
        fresh = importlib.reload(self.urgent)
        self.assertEqual(fresh.due(p, NOW + 600), [])

    def test_a_new_question_from_the_same_agent_pings_again(self):
        self.urgent.mark_sent(self.urgent.due(payload(decide=[ask("agent:a", 1900, at=NOW - 1900)]), NOW), NOW)
        self.assertEqual(len(self.urgent.due(payload(decide=[ask("agent:a", 1900, at=NOW - 100)]), NOW)), 1)

    def test_regression_reported_again_pings_once(self):
        reg = lambda at: payload(regressed=[{"key": "loop:1", "title": "X", "line": "back", "href": "", "at": at}])  # noqa: E731
        self.urgent.mark_sent(self.urgent.due(reg(NOW - 10), NOW), NOW)
        self.assertEqual(self.urgent.due(reg(NOW + 1800), NOW + 1800), [])

    def test_card_questions_never_ping(self):
        self.assertEqual(self.urgent.due(payload(decide=[ask("run:r1", 90000, kind="run", urgent=False)]), NOW), [])

    def test_digest_is_short_and_counts_match(self):
        moved = [{"key": f"run:{i}", "title": f"task {i}", "line": "worked: did it", "result": "worked", "href": "#/run/x", "at": NOW} for i in range(40)]
        p = payload(decide=[ask("agent:a", 100)], moved=moved, in_flight=[{"key": "loop:1", "title": "PR 7", "line": "Fix PR open", "href": ""}], quiet=4)
        text = self.urgent.digest_text(p, "http://dash")
        self.assertLessEqual(len(text), 3500)
        self.assertIn("1 to decide", text)
        self.assertIn("40 moved", text)
        self.assertIn("more on the dashboard", text)

    def test_digest_when_all_quiet(self):
        self.assertIn("Nothing needs you", self.urgent.digest_text(payload(quiet=3), None))


class NudgeTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        from cos import urgent
        self.urgent = urgent

    def tearDown(self):
        self.env.stop()

    def card_ask(self, key, hours):
        return ask(key, hours * 3600, kind="run", urgent=False, recommend="Yes, merge")

    def test_nudges_old_card_decisions_once_in_one_message(self):
        p = payload(decide=[self.card_ask("run:a", 5), self.card_ask("run:b", 3), self.card_ask("run:new", 0.5)])
        n = self.urgent.nudge(p, NOW, 11)
        self.assertIn("2 decisions waiting", n["text"])
        self.assertIn("Yes, merge", n["text"])
        self.assertNotIn("run:new", n["text"])
        self.urgent.mark_sent([n], NOW)
        self.assertIsNone(self.urgent.nudge(p, NOW + 4 * 3600, 15))  # same items: not again

    def test_no_nudge_at_night_or_too_soon(self):
        p = payload(decide=[self.card_ask("run:a", 5)])
        self.assertIsNone(self.urgent.nudge(p, NOW, 23))
        n = self.urgent.nudge(p, NOW, 10)
        self.urgent.mark_sent([n], NOW)
        p2 = payload(decide=[self.card_ask("run:a", 5), self.card_ask("run:c", 4)])
        self.assertIsNone(self.urgent.nudge(p2, NOW + 3600, 11))
        self.assertIn("run:c", self.urgent.nudge(p2, NOW + 4 * 3600, 14)["text"])

    def test_opened_question_and_stopped_agents_are_nudged(self):
        opened = {**self.card_ask("agent:g", 3), "opened": True}
        stopped = {"key": "agent:p", "title": "Guidance lease race", "href": "#/agent/neo/p", "at": NOW - 4 * 3600,
                   "age": 4 * 3600, "result": "partial", "line": "Fixed 2 of 3 tests"}
        n = self.urgent.nudge(payload(decide=[opened], unfinished=[stopped]), NOW, 11)
        self.assertIn("you opened it, not answered", n["text"])
        self.assertIn("1 agent stopped before finishing", n["text"])
        self.assertIn("Fixed 2 of 3 tests", n["text"])
        self.urgent.mark_sent([n], NOW)
        self.assertIsNone(self.urgent.nudge(payload(unfinished=[stopped]), NOW + 4 * 3600, 15))  # once per stop

    def test_digest_lists_stopped_agents_even_when_nothing_else_moved(self):
        stopped = {"key": "agent:p", "title": "Lease race", "href": "", "result": "partial", "line": "Fixed 2 of 3 tests"}
        text = self.urgent.digest_text(payload(unfinished=[stopped]), None)
        self.assertIn("1 stopped unfinished", text)
        self.assertIn("*Stopped before finishing*", text)
        self.assertNotIn("Nothing needs you", text)
        self.assertNotIn("stopped unfinished", self.urgent.digest_text(payload(quiet=3), None))

    def test_digest_marks_idle_rows_in_flight(self):
        rows = [{"key": "agent:i", "title": "Lease race", "href": "", "idle": True, "result": "partial", "line": "Fixed 2 of 3"},
                {"key": "agent:w", "title": "Busy", "href": "", "line": "Running tests"}]
        text = self.urgent.digest_text(payload(in_flight=rows), None)
        self.assertIn("• Idle :large_yellow_circle: Lease race: Fixed 2 of 3", text)
        self.assertIn("• Busy: Running tests", text)

    def test_waiting_agents_and_loops_are_not_in_the_nudge(self):
        p = payload(decide=[ask("agent:a", 5 * 3600), ask("loop:l", 5 * 3600, kind="loop")])
        self.assertIsNone(self.urgent.nudge(p, NOW, 11))



if __name__ == "__main__":
    unittest.main()
