import unittest
from unittest.mock import Mock

from cos.herdr import Herdr
from cos.layout import group_for_task, task_label


class TaskLayoutTest(unittest.TestCase):
    def test_groups_release_operations_agent_and_product_work(self):
        self.assertEqual(group_for_task("deploy Deskie v3.15.9 to production", "/work/anyteam-deskie"), "Nio · Releases")
        self.assertEqual(group_for_task("investigate the Sentry alert", "/work/anyteam-backie"), "Nio · Operations")
        self.assertEqual(group_for_task("fix Nio Herdr task routing", "/work/neo"), "Nio · Agent operations")
        self.assertEqual(group_for_task("fix the auth handler", "/work/anyteam-backie"), "Nio · Backend fixes")
        self.assertEqual(group_for_task("fix the tray", "/work/anyteam-deskie"), "Nio · Desktop fixes")
        self.assertEqual(group_for_task("fix issue #2739", "/work/anyteam-one"), "Nio · Issue & PR fixes")

    def test_task_label_surfaces_issue_and_action(self):
        self.assertEqual(task_label("Fix issue #2739: recording sync leaves a meeting orphaned"),
                         "#2739 Fix recording sync leaves a meeting orphaned")

    def test_acquire_lane_reuses_group_as_new_tab(self):
        herdr = Herdr("nio", "herdr")
        herdr.call = Mock(side_effect=[
            {"type": "workspace_list", "workspaces": [{"workspace_id": "w2", "label": "Backend fixes"}]},
            {"tab": {"tab_id": "w2:t3"}, "root_pane": {"pane_id": "w2:p3"}},
            {},
        ])
        self.assertEqual(herdr.acquire_lane("/work/backie", "Backend fixes", "#2739 Fix recording sync"),
                         ("w2", "w2:t3", "w2:p3"))
        self.assertEqual(herdr.call.call_args_list[1].args[:2], ("tab", "create"))

    def test_close_lane_closes_only_task_tab_in_shared_group(self):
        herdr = Herdr("nio", "herdr")
        herdr.call = Mock(side_effect=[
            {"type": "tab_list", "tabs": [{"tab_id": "w2:t1"}, {"tab_id": "w2:t2"}]},
            {},
        ])
        herdr.close_lane("w2", "w2:t2")
        herdr.call.assert_called_with("tab", "close", "w2:t2")


if __name__ == "__main__":
    unittest.main()
