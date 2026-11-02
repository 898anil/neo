"""One-time CLI screens that stop an unattended live start are answered; anything else is left for a human."""

import unittest
from unittest import mock

from cos.herdr import HerdrError
from cos.interactive import InteractiveRun

TRUST = "Do you trust the files in this folder?\n❯ 1. No, exit\n  2. Yes, I trust this folder\n"
TRUST_SELECTED = "Do you trust the files in this folder?\n  1. No, exit\n❯ 2. Yes, I trust this folder\n"
RENDERER = "Try the new fullscreen renderer?\n❯ 1. Yes, try it\n  2. Not now\n"
RENDERER_SELECTED = "Try the new fullscreen renderer?\n  1. Yes, try it\n❯ 2. Not now\n"
READY = "> "


class FakePane:
    """A pane whose screen advances on down/enter like the CLI's selection menus."""

    def __init__(self, screens):
        self.screens = list(screens)  # [(question screen, selected screen)], answered in order
        self.keys = []
        self.state = "question" if self.screens else "ready"

    def visible(self):
        if not self.screens:
            return READY
        question, selected = self.screens[0]
        return selected if self.state == "selected" else question

    def run(self, args, timeout=None):
        if args[:2] == ["pane", "read"]:
            return mock.Mock(stdout=self.visible())
        if args[:2] == ["pane", "send-keys"]:
            key = args[3]
            self.keys.append(key)
            if key == "down" and self.screens:
                self.state = "selected"
            elif key == "enter" and self.screens:
                self.screens.pop(0)
                self.state = "question" if self.screens else "ready"
            return mock.Mock(stdout="")
        raise AssertionError(args)


def herdr_for(pane):
    herdr = mock.Mock()
    herdr._run.side_effect = pane.run
    herdr.get.side_effect = lambda target: {"agent_status": "idle", "name": target}
    return herdr


def not_ready():
    raise HerdrError("agent_not_ready", "claude is waiting for input")


class StartupScreensTest(unittest.TestCase):
    def setUp(self):
        sleep = mock.patch("cos.interactive.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def start(self, pane):
        herdr = herdr_for(pane)
        guard = mock.Mock()
        result = InteractiveRun._start_past_trust_prompt(herdr, "neo-x", "claude", "p1", not_ready, guard)
        return herdr, guard, result

    def test_the_trust_prompt_is_accepted(self):
        pane = FakePane([(TRUST, TRUST_SELECTED)])
        herdr, guard, result = self.start(pane)
        self.assertEqual(pane.keys, ["down", "enter"])
        herdr.call.assert_called_once_with("agent", "rename", "p1", "neo-x", timeout=15)
        self.assertEqual(result["name"], "neo-x")
        self.assertEqual(guard.call_count, 2)

    def test_the_fullscreen_renderer_notice_is_declined(self):
        pane = FakePane([(RENDERER, RENDERER_SELECTED)])
        self.start(pane)
        self.assertEqual(pane.keys, ["down", "enter"])

    def test_trust_then_renderer_are_both_answered(self):
        pane = FakePane([(TRUST, TRUST_SELECTED), (RENDERER, RENDERER_SELECTED)])
        self.start(pane)
        self.assertEqual(pane.keys, ["down", "enter", "down", "enter"])

    def test_an_unknown_screen_is_left_for_a_human(self):
        pane = FakePane([("Log in to continue\n❯ 1. Claude account\n", "")])
        with self.assertRaises(HerdrError):
            self.start(pane)
        self.assertEqual(pane.keys, [])

    def test_no_confirm_when_the_option_is_not_selected(self):
        # The screen does not move to "Not now" after down: never press enter on the wrong option.
        pane = FakePane([(RENDERER, RENDERER)])
        with self.assertRaises(HerdrError):
            self.start(pane)
        self.assertEqual(pane.keys, ["down"])


if __name__ == "__main__":
    unittest.main()
