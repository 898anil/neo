"""Voice notes: a Slack clip is heard, run like a mention, and answered with a spoken clip too."""

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import app  # noqa: E402
import sessions  # noqa: E402
from cos import voice  # noqa: E402

CLIP = {"id": "F1", "subtype": "slack_audio", "mimetype": "audio/webm",
        "url_private_download": "https://files.slack.com/files-pri/T1-F1/download/audio_message.webm"}


class SpokenTextTest(unittest.TestCase):
    def test_strips_what_should_not_be_read_aloud(self):
        answer = ("*Root cause*: the <https://sentry.io/x|Sentry issue> spiked :fire:\n"
                  "- see `app.py:12`\n```\ntraceback\n```\nhttps://example.com/x\n"
                  "Status: answered — done")
        text = voice.spoken_text(answer)
        self.assertIn("Root cause: the Sentry issue spiked", text)
        self.assertIn("see app.py:12", text)
        self.assertIn("code is in the thread", text)
        self.assertIn("a link", text)
        for gone in ("*", "`", "traceback", "https://", ":fire:", "Status:"):
            self.assertNotIn(gone, text)

    def test_long_answer_is_cut_at_a_sentence(self):
        text = voice.spoken_text("This is one sentence. " * 200, limit=300)
        self.assertLessEqual(len(text), 300 + len(" The rest is in the thread."))
        self.assertTrue(text.endswith("sentence. The rest is in the thread."))

    def test_times_are_kept(self):
        self.assertIn("10:30:45", voice.spoken_text("Deployed at 10:30:45 :rocket:"))

    def test_failed_speech_leaves_no_files(self):
        before = set(Path(tempfile.gettempdir()).glob("neo-voice-*"))
        with patch.object(voice, "synthesize", side_effect=voice.VoiceError("no say")):
            with self.assertRaises(voice.VoiceError):
                voice.speak_answer("Something private.")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("neo-voice-*")), before)

    def test_nothing_to_say(self):
        self.assertIsNone(voice.speak_answer("Status: done"))


class AudioFilesTest(unittest.TestCase):
    def test_only_audio(self):
        event = {"files": [CLIP, {"id": "F2", "mimetype": "image/png"},
                           {"id": "F3", "mimetype": "audio/mp4"}, {"id": "F4", "subtype": "slack_video",
                                                                 "mimetype": "video/mp4"}]}
        self.assertEqual([f["id"] for f in voice.audio_files(event)], ["F1", "F3"])
        self.assertEqual(voice.audio_files({}), [])


class TranscribeTest(unittest.TestCase):
    def test_token_is_never_sent_off_slack(self):
        evil = {**CLIP, "url_private_download": "https://attacker.example/clip.webm"}
        with patch.object(voice._OPENER, "open") as opener:
            with self.assertRaises(voice.VoiceError):
                voice.transcribe(evil, "xoxb-secret")
            opener.assert_not_called()

    def test_redirect_off_slack_is_refused(self):
        handler = voice._SlackOnlyRedirects()
        req = voice.urllib.request.Request(CLIP["url_private_download"], headers={"Authorization": "Bearer x"})
        for url in ("https://attacker.example/x", "https://acme.slack.com/signin", "http://files.slack.com/x"):
            with self.assertRaises(voice.VoiceError):
                handler.redirect_request(req, None, 302, "Found", {}, url)
        ok = handler.redirect_request(req, None, 302, "Found", {}, "https://files.slack.com/files-pri/y")
        self.assertEqual(ok.full_url, "https://files.slack.com/files-pri/y")

    def test_falls_back_to_slacks_transcript(self):
        file = {**CLIP, "url_private_download": "http://files.slack.com/x",
                "transcription": {"status": "complete", "preview": {"content": "check the deploy"}}}
        self.assertEqual(voice.transcribe(file, "xoxb"), "check the deploy")

    def test_sign_in_page_means_missing_scope(self):
        class Resp:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def read(self, n): return b"<html>"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch.object(voice._OPENER, "open", return_value=Resp()):
            with self.assertRaisesRegex(voice.VoiceError, "files:read"):
                voice.transcribe(CLIP, "xoxb")


@unittest.skipUnless(shutil.which("say"), "macOS say not available")
class SynthesizeTest(unittest.TestCase):
    def test_makes_an_m4a(self):
        path = voice.speak_answer("*Done.* Deployed to stage.")
        try:
            self.assertEqual(path.suffix, ".m4a")
            self.assertGreater(path.stat().st_size, 1000)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)


class FakeClient:
    token = "xoxb-test"

    def __init__(self):
        self.posts: list[str] = []
        self.uploads: list[dict] = []

    async def chat_postMessage(self, channel, thread_ts, text, blocks=None):
        self.posts.append(text)
        return {"ts": f"ts-{len(self.posts)}"}

    async def chat_update(self, channel, ts, text):
        return {}

    async def chat_delete(self, channel, ts):
        return {}

    async def reactions_add(self, **kw):
        return {}

    async def reactions_remove(self, **kw):
        return {}

    async def conversations_replies(self, **kw):
        return {"messages": []}

    async def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {}


class VoiceRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mention = AsyncMock()
        self.patches = [
            patch.object(sessions, "_STORE_PATH", Path(self.tmp.name) / "sessions.json"),
            patch.object(app, "ALLOWED_USER_IDS", {"U1"}),
            patch.object(app, "BOT_USER_ID", "UNEO"),
            patch.object(app, "AUTO_RCA_CHANNELS", set()),
            patch.object(app, "handle_mention", self.mention),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _clip(self, **kw):
        return {"type": "message", "subtype": "file_share", "user": "U1", "channel": "C1",
                "ts": "5.0", "text": "", "files": [CLIP], **kw}

    async def test_dm_clip_starts_work(self):
        await app.handle_dm(self._clip(channel="D1", channel_type="im"), FakeClient())
        self.mention.assert_awaited_once()

    async def test_clip_in_neos_thread_continues_it(self):
        await sessions.set_thread("1.0", {"run_id": "r1", "engine": "claude", "runtime": "headless"})
        await app.handle_dm(self._clip(channel_type="channel", thread_ts="1.0"), FakeClient())
        self.mention.assert_awaited_once()

    async def test_clip_elsewhere_is_ignored(self):
        await app.handle_dm(self._clip(channel_type="channel", thread_ts="2.0"), FakeClient())
        await app.handle_dm(self._clip(channel_type="channel"), FakeClient())
        self.mention.assert_not_awaited()

    async def test_clip_with_mention_is_left_to_app_mention(self):
        await app.handle_dm(self._clip(channel_type="channel", text="<@UNEO> this"), FakeClient())
        self.mention.assert_not_awaited()

    async def test_dm_clip_with_mention_is_answered(self):
        # Slack sends no app_mention in a DM, so the message handler must take it.
        await app.handle_dm(self._clip(channel="D1", channel_type="im", text="<@UNEO> this"), FakeClient())
        self.mention.assert_awaited_once()

    async def test_file_stub_is_resolved(self):
        class Client(FakeClient):
            async def files_info(self, file):
                return {"file": CLIP}
        stub = self._clip(channel="D1", channel_type="im", files=[{"id": "F1", "file_access": "check_file_info"}])
        await app.handle_dm(stub, Client())
        self.mention.assert_awaited_once()

    async def test_someone_else_is_ignored(self):
        await app.handle_dm(self._clip(channel="D1", channel_type="im", user="U9"), FakeClient())
        self.mention.assert_not_awaited()

    async def test_bot_upload_is_ignored(self):
        await app.handle_dm(self._clip(channel="D1", channel_type="im", bot_id="B1"), FakeClient())
        self.mention.assert_not_awaited()


class VoiceMentionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = AsyncMock(return_value=True)
        self.patches = [
            patch.object(sessions, "_STORE_PATH", Path(self.tmp.name) / "sessions.json"),
            patch.object(app, "ALLOWED_USER_IDS", {"U1"}),
            patch.object(app, "run_headless", self.run),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_heard_words_become_the_prompt_and_reply_is_spoken(self):
        client = FakeClient()
        event = {"user": "U1", "channel": "C1", "ts": "5.0", "text": "", "files": [CLIP]}
        with patch.object(voice, "transcribe", return_value="check the stage deploy"):
            await app.handle_mention(event, client)
        self.assertTrue(any("Heard:_ check the stage deploy" in p for p in client.posts))
        args, kwargs = self.run.await_args
        self.assertIn("check the stage deploy", args[5])
        self.assertTrue(kwargs["spoken"])

    async def test_unheard_clip_says_so_and_runs_nothing(self):
        client = FakeClient()
        event = {"user": "U1", "channel": "C1", "ts": "5.0", "text": "", "files": [CLIP]}
        with patch.object(voice, "transcribe", side_effect=voice.VoiceError("needs files:read")):
            await app.handle_mention(event, client)
        self.assertTrue(any("couldn't make out" in p and "files:read" in p for p in client.posts))
        self.run.assert_not_awaited()

    async def test_typed_mention_is_not_spoken(self):
        client = FakeClient()
        await app.handle_mention({"user": "U1", "channel": "C1", "ts": "6.0", "text": "<@UNEO> hi"}, client)
        self.assertFalse(self.run.await_args.kwargs["spoken"])


class SpokenReplyTest(unittest.IsolatedAsyncioTestCase):
    """The first answer of a voice turn is spoken; background results that follow are not."""

    async def test_only_first_answer_is_spoken(self):
        release = threading.Event()

        class Run:
            id = "20261021-000000-abcdef"

            @classmethod
            def create(cls, *a, **k):
                return cls()

            def meta(self):
                return {"id": self.id, "mode": "full", "engine": "claude"}

            def update_meta(self, **f):
                return self.meta()

            def execute(self, prompt, on_event):
                on_event({"kind": "result", "body": "The answer.", "data": {}})
                release.wait(5)
                on_event({"kind": "result", "body": "CI went green.", "data": {}})
                return {"status": "done"}

        tmp = tempfile.TemporaryDirectory()
        spoken: list[str] = []

        async def say_aloud(client, channel, thread_ts, answer):
            spoken.append(answer)
            release.set()

        with patch.object(sessions, "_STORE_PATH", Path(tmp.name) / "s.json"), \
                patch.object(app, "Run", Run), patch.object(app, "agent_brief", lambda *a, **k: ""), \
                patch.object(app, "say_aloud", say_aloud):
            ok = await asyncio.wait_for(app.run_headless(
                FakeClient(), "C1", "7.0", None, app.commands.parse("do it"), "do it", spoken=True), 10)
        tmp.cleanup()
        self.assertTrue(ok)
        self.assertEqual(spoken, ["The answer."])

    async def test_say_aloud_uploads_and_cleans_up(self):
        client = FakeClient()
        folder = Path(tempfile.mkdtemp())
        clip = folder / "neo-reply.m4a"
        clip.write_bytes(b"x")
        with patch.object(voice, "speak_answer", return_value=clip):
            await app.say_aloud(client, "C1", "8.0", "Done.")
        self.assertEqual(client.uploads[0]["thread_ts"], "8.0")
        self.assertFalse(folder.exists())

    async def test_say_aloud_never_raises(self):
        with patch.object(voice, "speak_answer", side_effect=voice.VoiceError("no say")):
            await app.say_aloud(FakeClient(), "C1", "8.0", "Done.")


if __name__ == "__main__":
    unittest.main()
