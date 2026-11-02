"""The owner's web dashboard lists only the owner's runs, not teammates' (their own pages show theirs)."""

import os
import unittest
from unittest.mock import patch

from cos import fleet, home

OWNER, BALA = "UOWNER1", "UBALA01"
RUNS = [
    {"id": "20261102-080000-aaaaaa", "slack_user": OWNER, "status": "done", "prompt": "mine from Slack"},
    {"id": "20261102-080001-bbbbbb", "slack_user": None, "status": "done", "prompt": "mine, scheduled"},
    {"id": "20261102-080002-cccccc", "slack_user": BALA, "status": "running", "prompt": "Bala's"},
]


class OwnerOnlyTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"NEO_OWNER_USER": OWNER})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def ids(self, rows):
        return [r["id"][-6:] for r in rows]

    def test_owners_keeps_the_owners_and_unassigned_runs(self):
        self.assertEqual(self.ids(fleet.owners(RUNS)), ["aaaaaa", "bbbbbb"])

    def test_overview_for_the_owner_leaves_out_teammates(self):
        with patch.object(fleet, "agents", return_value=[]), patch.object(fleet, "list_runs", return_value=RUNS):
            everyone = fleet.overview()
            mine = fleet.overview(owner_only=True)
        self.assertEqual(len(everyone["runs"]), 3)
        self.assertEqual(self.ids(mine["runs"]), ["aaaaaa", "bbbbbb"])
        self.assertNotIn("cccccc", self.ids(mine["tasks"]))
        self.assertEqual(mine["counts"]["headless_running"], 0)  # Bala's running run isn't counted either

    def test_home_for_the_owner_leaves_out_teammates(self):
        seen = {}
        with patch.object(fleet, "overview", side_effect=lambda owner_only=False: seen.update(owner_only=owner_only) or
                          {"agents": [], "runs": [], "tasks": []}), \
                patch("cos.runner.list_runs", return_value=RUNS), \
                patch.object(home, "build", side_effect=lambda overview, rows, cards, **kw: kw["traces"]), \
                patch.object(home, "thread_runs", side_effect=lambda runs: {r["id"]: 1 for r in runs}), \
                patch.object(home, "loop_traces", return_value={}), patch("cos.loops.listing", return_value=[]), \
                patch("cos.mentions.open_items", return_value=[]):
            traces = home.home(owner_only=True)
        self.assertTrue(seen["owner_only"])
        self.assertNotIn(RUNS[2]["id"], traces)


if __name__ == "__main__":
    unittest.main()
