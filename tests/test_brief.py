"""The morning brief's prompt."""

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class BriefPromptTest(unittest.TestCase):
    def test_brief_ignores_neo_own_slack_posts_and_trusts_the_agent_list(self):
        from cos import brief
        with patch.dict(os.environ, {"NEO_BOT_NAME": "nio"}), patch.object(brief, "_agents_summary", return_value="AGENTS"):
            text = " ".join(brief.prompt(datetime(2026, 10, 19, 8, 0)).split())
        self.assertIn("19 October 2026", text)
        self.assertIn("AGENTS", text)
        # neo's own Slack alerts repeat its records and go stale; the brief must not report them as asks.
        self.assertIn("nio", text.lower())
        self.assertIn("own Slack bot", text)
        self.assertIn("only if the agent list", text)


if __name__ == "__main__":
    unittest.main()
