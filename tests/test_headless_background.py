"""A headless Claude turn that leaves background work running must still answer in Slack.

Claude Code -p prints its result when the turn ends, but keeps the process alive until the
background shells/agents it started finish, then wakes again and prints another result.
"""

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import app  # noqa: E402
import sessions  # noqa: E402


class FakeClient:
    def __init__(self):
        self.posts: list[str] = []
        self.deleted: list[str] = []

    async def chat_postMessage(self, channel, thread_ts, text, blocks=None):
        self.posts.append(text)
        return {"ts": f"ts-{len(self.posts)}"}

    async def chat_update(self, channel, ts, text):
        return {}

    async def chat_delete(self, channel, ts):
        self.deleted.append(ts)


class FakeRun:
    """Emits a result, then blocks (background work) until released, then a late result."""

    release = threading.Event()
    answered = threading.Event()

    def __init__(self, run_id="20261007-120000-abcdef"):
        self.id = run_id

    @classmethod
    def create(cls, *args, **kwargs):
        return cls()

    def meta(self):
        return {"id": self.id, "mode": "full", "engine": "claude"}

    def update_meta(self, **fields):
        return self.meta()

    def execute(self, prompt, on_event):
        on_event({"kind": "text", "body": "The answer.", "data": {}})
        on_event({"kind": "result", "body": "The answer.", "data": {"cost_usd": 1.0}})
        self.answered.set()
        assert self.release.wait(5), "test never released background work"
        on_event({"kind": "result", "body": "CI went green.", "data": {"cost_usd": 0.1}})
        return {"status": "done"}


class CancellableRun(FakeRun):
    """Background work that only ends when the run is cancelled (a follow-up arrived)."""

    cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()
        return True

    def execute(self, prompt, on_event):
        on_event({"kind": "result", "body": "The answer.", "data": {}})
        assert self.cancelled.wait(5), "background work was never stopped"
        on_event({"kind": "error", "title": "Cancelled", "body": "Stopped by user", "data": {}})
        return {"status": "cancelled"}


class HeadlessBackgroundTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        FakeRun.release.clear()
        FakeRun.answered.clear()
        self.patches = [
            patch.object(sessions, "_STORE_PATH", Path(self.tmp.name) / "sessions.json"),
            patch.object(app, "Run", FakeRun),
            patch.object(app, "agent_brief", lambda *a, **k: ""),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_answer_is_posted_before_background_work_ends(self):
        client = FakeClient()
        req = app.commands.parse("claude do the thing")
        task = asyncio.create_task(app.run_headless(client, "C1", "1.0", None, req, "do the thing"))

        for _ in range(100):
            if "The answer." in client.posts:
                break
            await asyncio.sleep(0.05)
        self.assertIn("The answer.", client.posts, "answer must not wait for background work")
        self.assertFalse(task.done())
        self.assertIn("1.0", app.ANSWERED)

        FakeRun.release.set()
        self.assertTrue(await task)
        self.assertEqual(client.posts.count("The answer."), 1)
        self.assertTrue(any("CI went green." in p for p in client.posts))
        self.assertNotIn("1.0", app.ANSWERED)
        self.assertNotIn("1.0", app.HEADLESS)

    async def test_follow_up_stops_leftover_background_work(self):
        CancellableRun.cancelled.clear()
        client = FakeClient()
        req = app.commands.parse("claude do the thing")
        with patch.object(app, "Run", CancellableRun):
            task = asyncio.create_task(app.run_headless(client, "C1", "2.0", None, req, "do the thing"))
            for _ in range(100):
                if "2.0" in app.ANSWERED:
                    break
                await asyncio.sleep(0.05)
            self.assertIn("2.0", app.ANSWERED)

            self.assertTrue(await app._end_background("2.0", wait_seconds=5))
            self.assertTrue(CancellableRun.cancelled.is_set())
            self.assertTrue(await task)  # cancelled after answering is not a failure
        self.assertNotIn("2.0", app.HEADLESS)
        self.assertFalse(any("warning" in p for p in client.posts))


class AdoptedSlackRunTest(unittest.IsolatedAsyncioTestCase):
    """After a bot restart, runs a Slack thread started must still answer, exactly once."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [
            patch.dict(os.environ, {"COS_HOME": str(self.home)}),
            patch.object(app, "_ADOPT_POLL_SECONDS", 0.01),
            patch.object(app, "_DEAD_ENGINE_GRACE_SECONDS", 0.0),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _run(self, events, **meta):
        run = app.Run.create("claude", "do it", str(self.home), "full", source="slack",
                             extra={"slack_channel": "C1", "slack_thread": "9.0"})
        for event in events:
            run.append_event(event, 1)
        run.update_meta(status="running", turns=1, **meta)
        return run

    async def _adopt(self, client):
        for task in await app.finish_adopted_slack_runs(client):
            await asyncio.wait_for(task, 10)

    async def test_dead_engine_is_closed_out_and_answer_posted(self):
        run = self._run([{"kind": "result", "body": "The answer.", "data": {}}], pid=None)
        client = FakeClient()
        await self._adopt(client)
        self.assertEqual(client.posts, ["The answer."])
        self.assertEqual(app.Run(run.id).meta()["status"], "done")

    async def test_answer_the_old_bot_posted_is_not_repeated(self):
        run = self._run([{"kind": "result", "body": "The answer.", "data": {}}], pid=None)
        run.update_meta(slack_posted_seq=0)
        client = FakeClient()
        await self._adopt(client)
        self.assertEqual(client.posts, [])

    async def test_long_dead_run_is_closed_quietly(self):
        run = self._run([{"kind": "result", "body": "Old answer.", "data": {}}], pid=None)
        with patch.object(app, "_age_hours", lambda _iso: 48.0):
            client = FakeClient()
            await self._adopt(client)
        self.assertEqual(client.posts, [])
        self.assertEqual(app.Run(run.id).meta()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
