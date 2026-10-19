import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slack_sdk.errors import SlackApiError  # noqa: E402

from cos import cli, pause, post  # noqa: E402


class PostTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"COS_HOME": self.home, "NEO_JOBS_DIR": self.home})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_posts_as_the_bot_in_the_thread(self):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C1"}
        result = post.post("C1", "regression", thread_ts="9.9", client=client)
        client.chat_postMessage.assert_called_once_with(channel="C1", thread_ts="9.9", text="regression",
                                                        unfurl_links=False)
        self.assertEqual(result, {"posted": True, "ts": "1.2", "channel": "C1"})

    def test_paused_message_is_held_for_the_job_not_posted(self):
        pause.pause()
        client = MagicMock()
        result = post.post("C1", "regression", thread_ts="9.9", job="deploy-watch", client=client)
        client.chat_postMessage.assert_not_called()
        self.assertFalse(result["posted"])
        held = json.loads(Path(result["held"]).read_text())
        self.assertEqual(held, [{"channel": "C1", "thread_ts": "9.9", "text": "regression"}])
        self.assertEqual(Path(result["held"]).parent.name, "deploy-watch")

    def test_paused_without_a_job_refuses_instead_of_dropping(self):
        pause.pause()
        with self.assertRaises(ValueError):
            post.post("C1", "regression", client=MagicMock())

    def test_token_comes_from_the_env_file_when_not_in_the_environment(self):
        env_file = Path(self.home) / ".env"
        os.environ.pop("SLACK_BOT_TOKEN", None)  # patch.dict restores it on stop()
        env_file.write_text("SLACK_BOT_TOKEN=xoxb-test\n")
        self.assertEqual(post.bot_token(env_file), "xoxb-test")
        env_file.write_text("SLACK_BOT_TOKEN=\n")
        with self.assertRaises(ValueError):
            post.bot_token(env_file)

    def test_held_messages_append_in_order(self):
        pause.pause()
        post.post("C1", "first", job="deploy-watch", client=MagicMock())
        result = post.post("C1", "second", thread_ts="9.9", job="deploy-watch", client=MagicMock())
        held = json.loads(Path(result["held"]).read_text())
        self.assertEqual([item["text"] for item in held], ["first", "second"])

    def test_job_name_cannot_leave_the_jobs_dir(self):
        pause.pause()
        with self.assertRaises(ValueError):
            post.post("C1", "x", job="../../tmp/x", client=MagicMock())

    def test_long_text_is_clipped(self):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C1"}
        post.post("C1", "x" * 10000, client=client)
        self.assertEqual(len(client.chat_postMessage.call_args.kwargs["text"]), post.MAX_TEXT)

    def test_cli_reports_a_slack_error_without_a_traceback_or_the_token(self):
        error = SlackApiError("invalid_auth", {"ok": False, "error": "invalid_auth"})
        stderr = io.StringIO()
        with patch("cos.post.bot_token", return_value="xoxb-SECRET"), \
                patch("cos.post.WebClient") as client_cls, patch("sys.stderr", stderr):
            client_cls.return_value.chat_postMessage.side_effect = error
            code = cli.main(["post", "--channel", "C1", "--text", "hi"])
        self.assertEqual(code, 4)
        self.assertIn("invalid_auth", stderr.getvalue())
        self.assertNotIn("xoxb", stderr.getvalue())

    def test_cli_missing_text_file_is_a_clean_error(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code = cli.main(["post", "--channel", "C1", "--text-file", str(Path(self.home) / "missing.txt")])
        self.assertEqual(code, 3)
        self.assertIn("FileNotFoundError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
