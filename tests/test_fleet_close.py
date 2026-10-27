import os
import tempfile
import unittest
from unittest import mock

from cos import fleet


class CloseActionTest(unittest.TestCase):
    def test_close_shuts_the_workspace_and_marks_the_run_closed(self):
        herdr = mock.Mock()
        herdr.get.return_value = {"name": "neo-abc", "workspace_id": "w14", "pane_id": "w14:p1"}
        run = mock.Mock()
        with mock.patch.object(fleet, "Herdr", return_value=herdr), \
             mock.patch.object(fleet, "_run_index", return_value={("neo", "neo-abc"): {"id": "r1"}}), \
             mock.patch("cos.runner.Run", return_value=run) as Run:
            fleet.act("neo", "w14:p1", "close")
        herdr.close_workspace.assert_called_once_with("w14")
        Run.assert_called_once_with("r1")
        run.update_meta.assert_called_once_with(status="closed")

    def test_close_without_a_run_still_closes_the_pane(self):
        herdr = mock.Mock()
        herdr.get.return_value = {"name": "mine", "pane_id": "wB:p1"}
        with mock.patch.object(fleet, "Herdr", return_value=herdr), mock.patch.object(fleet, "_run_index", return_value={}):
            fleet.act("default", "wB:p1", "close")
        herdr.close_workspace.assert_called_once_with("wB")


class RenameActionTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.herdr = mock.Mock()
        self.herdr.get.return_value = {"name": "neo-abc", "pane_id": "w1:p1", "agent": "?", "terminal_title_stripped": "claude"}
        patcher = mock.patch.object(fleet, "Herdr", return_value=self.herdr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def row(self):
        return fleet._agent_row("neo", self.herdr.get.return_value, {})

    def test_rename_sets_the_title_and_empty_goes_back(self):
        fleet.act("neo", "w1:p1", "rename", "  HubSpot   sync fix ")
        self.assertEqual(self.row()["title"], "HubSpot sync fix")
        fleet.act("neo", "w1:p1", "rename", "")
        self.assertEqual(self.row()["title"], "claude")
        self.assertEqual(fleet.titles.read(), {})

    def test_a_name_given_before_the_session_id_moves_to_it(self):
        fleet.act("neo", "w1:p1", "rename", "Early name")
        self.assertEqual(self.row()["title"], "Early name")
        self.herdr.get.return_value = {**self.herdr.get.return_value, "agent_session": {"kind": "id", "value": "s-1"}}
        with mock.patch.object(fleet, "agent_session_id", return_value="s-1"):
            self.assertEqual(self.row()["title"], "Early name")  # still found by pane until renamed again
            fleet.act("neo", "w1:p1", "rename", "Later name")
            self.assertEqual(fleet.titles.read(), {"agent:s-1": "Later name"})
            self.assertEqual(self.row()["title"], "Later name")

    def test_another_agent_in_the_same_pane_does_not_inherit_the_name(self):
        fleet.act("neo", "w1:p1", "rename", "Mine")
        self.herdr.get.return_value = {**self.herdr.get.return_value, "name": "neo-xyz"}
        self.assertEqual(self.row()["title"], "claude")

    def test_rename_rejects_a_long_title(self):
        with self.assertRaises(ValueError):
            fleet.act("neo", "w1:p1", "rename", "x" * (fleet.TITLE_MAX + 1))
        self.assertEqual(fleet.titles.read(), {})


if __name__ == "__main__":
    unittest.main()
