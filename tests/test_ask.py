"""Ask neo: a question about the owner's work, answered from a snapshot of agents, runs and loops."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cos.ask import SNAPSHOT_CHARS, snapshot, system_prompt  # noqa: E402

NOW = 1_790_000_000.0
AGENT = {"session": "neo", "pane_id": "w1:p1", "session_id": "s1", "title": "HubSpot token refresh", "repo": "acme-api",
         "cwd": "/w/acme-api-wt-hubspot", "engine": "claude", "status": "idle", "attention": "review",
         "last_active": NOW - 600, "transcript": "/t/s1.jsonl"}
RUN = {"id": "r1", "title": "Why is LLMCircuitBreakerOpen firing?", "status": "done", "source": "slack", "updated_at": "2026-10-01T19:00:00",
       "slack_url": "https://x.slack.com/archives/C/p1"}
LOOP = {"id": "l1", "kind": "pr", "title": "fix(integrations): classify HubSpot invalid_client", "stage": "fix_pr", "status": "open",
        "links": {"prs": ["https://github.com/o/r/pull/9"]}, "history": [{"at": NOW, "stage": "fix_pr", "note": "opened", "evidence": ""}],
        "stalled": None, "regressed": False}
CARDS = {"agent:s1": {"result": "partial", "did": "Classified invalid_client as reauth-needed; PR #9 open", "needs_you": "Merge #9?"},
         "run:r1": {"result": "answered", "did": "False positive: breaker metric double-counted", "needs_you": ""}}


class AskTest(unittest.TestCase):
    def test_snapshot_says_where_each_piece_of_work_is_and_how_it_is_doing(self):
        text = snapshot({"agents": [AGENT], "tasks": [RUN]}, [LOOP], CARDS, NOW)
        for expected in ("HubSpot token refresh", "acme-api-wt-hubspot", "#/agent/neo/w1%3Ap1", "partial",
                         "Merge #9?", "/t/s1.jsonl", "LLMCircuitBreakerOpen", "#/run/r1", "False positive",
                         "Fix PR open", "https://github.com/o/r/pull/9"):
            self.assertIn(expected, text)

    def test_snapshot_is_bounded(self):
        many = [{**AGENT, "pane_id": f"p{i}", "session_id": f"s{i}", "title": "x" * 300} for i in range(500)]
        self.assertLessEqual(len(snapshot({"agents": many, "tasks": []}, [], {}, NOW)), SNAPSHOT_CHARS + 200)

    def test_system_prompt_carries_snapshot_and_read_only_rules(self):
        p = system_prompt("SNAP")
        self.assertIn("SNAP", p)
        self.assertIn("8 short lines", p)
        self.assertIn("read only", p.lower())


if __name__ == "__main__":
    unittest.main()
