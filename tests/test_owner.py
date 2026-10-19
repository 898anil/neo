"""Who neo works for and their infrastructure come from the environment, read when a prompt is made."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cos import ask, cards, config, digest, loops, server  # noqa: E402


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp(), "NEO_OWNER_NAME": "",
                                           "NEO_OWNER_PROFILE": "", "NEO_DEPLOY_REPO": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_owner_name_defaults_and_is_read_at_call_time(self):
        self.assertEqual(config.owner_name(), "the owner")
        self.assertEqual(config.owner_fields(), {"owner": "the owner", "Owner": "The owner", "owner_intro": "the owner"})
        with patch.dict(os.environ, {"NEO_OWNER_NAME": " Alex ", "NEO_OWNER_PROFILE": "an engineer at Acme"}):
            self.assertEqual(config.owner_name(), "Alex")
            self.assertEqual(config.owner_fields()["owner_intro"], "Alex, an engineer at Acme")

    def test_card_prompt_names_the_owner(self):
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "Alex"}), \
                patch.object(cards.subprocess, "run", side_effect=OSError("no claude")) as run:
            self.assertIsNone(cards.make_card("Fix CI", "log"))
        prompt = run.call_args[0][0][-1]
        self.assertTrue(prompt.startswith("You are Alex's chief of staff. Alex is busy"), prompt[:80])
        self.assertNotIn("{owner}", prompt)

    def test_digest_ask_and_do_it_prompts_name_the_owner(self):
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "Alex"}), \
                patch.object(digest.subprocess, "run", side_effect=OSError("no claude")) as run:
            digest.make("Fix CI", "log", needs_you="merge?")
        prompt = run.call_args[0][0][-1]
        self.assertIn("digest for Alex, who glances", prompt)
        self.assertIn("needs Alex for: merge?", prompt)
        self.assertTrue(ask.system_prompt("snap").startswith("The owner is asking about their work"))
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "Alex"}):
            self.assertTrue(ask.system_prompt("snap").startswith("Alex is asking"))
            self.assertTrue(server.DO_IT_PROMPT.format(do_it="x", **config.owner_fields()).startswith("Alex read"))

    def test_brief_template_takes_the_owner_intro(self):
        template = config.read_prompt("brief.md")
        with patch.dict(os.environ, {"NEO_OWNER_NAME": "Alex", "NEO_OWNER_PROFILE": "an engineer at Acme"}):
            text = template.format(date="1 May", weekday="Monday", agents="", bot="Neo", **config.owner_fields())
        self.assertIn("I am Alex, an engineer at Acme, reading this", text)

    def test_loop_check_deploy_hint(self):
        self.assertNotIn("gh run list -R", loops.deploy_hint())
        with patch.dict(os.environ, {"NEO_DEPLOY_REPO": "acme-deploy"}):
            self.assertIn("`gh run list -R <owner>/acme-deploy ...`", loops.deploy_hint())
        with patch.dict(os.environ, {"NEO_DEPLOY_REPO": "acme/deploy"}):
            self.assertIn("`gh run list -R acme/deploy ...`", loops.deploy_hint())

    def test_loop_check_prompt_with_nothing_configured(self):
        loops.STORE.update(lambda d: d.update({"l1": {"id": "l1", "kind": "pr", "title": "t", "stage": "fix_pr",
                                                       "status": "open", "links": {}, "stage_at": 0, "updated_at": 0}}))
        with patch.object(loops, "Run") as run, patch.object(loops.threading, "Thread"), \
                patch.object(loops, "check_running", return_value=False):
            loops.start_check()
        prompt = run.create.call_args[0][1]
        self.assertIn("pull requests for the owner.", prompt)
        self.assertIn("wherever production is deployed", prompt)
        self.assertNotIn("{", prompt.split("Loops (JSON):")[0])


if __name__ == "__main__":
    unittest.main()
