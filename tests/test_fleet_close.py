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


if __name__ == "__main__":
    unittest.main()
