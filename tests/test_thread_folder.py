"""A thread's follow-up runs in today's work folder unless the thread named one with cwd=."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")

import app  # noqa: E402
import sessions  # noqa: E402


class FakeClient:
    async def chat_postMessage(self, channel, thread_ts, text, blocks=None):
        return {"ts": "ts-1"}

    async def chat_update(self, channel, ts, text):
        return {}

    async def chat_delete(self, channel, ts):
        return {}


class FolderRun:
    runs: dict = {}
    executed: list = []

    def __init__(self, run_id):
        self.id = run_id

    @classmethod
    def create(cls, engine, prompt, cwd, mode, **kwargs):
        run = cls(f"run-{len(cls.runs) + 1}")
        cls.runs[run.id] = {"id": run.id, "mode": "full", "engine": engine, "cwd": str(Path(cwd).resolve())}
        return run

    def meta(self):
        return self.runs[self.id]

    def update_meta(self, **fields):
        self.runs[self.id].update(fields)
        return self.meta()

    def execute(self, prompt, on_event):
        self.executed.append((self.id, prompt))
        on_event({"kind": "result", "body": "ok", "data": {}})
        return {"status": "done"}


class ThreadFolderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old, self.new, self.pin = (Path(self.tmp.name) / n for n in ("old", "new", "pin"))
        for d in (self.old, self.new, self.pin):
            d.mkdir()
        FolderRun.runs, FolderRun.executed = {}, []
        self.patches = [
            patch.object(sessions, "_STORE_PATH", Path(self.tmp.name) / "sessions.json"),
            patch.object(app, "Run", FolderRun),
            patch.object(app, "agent_brief", lambda *a, **k: ""),
            patch.object(app, "WORKDIR", str(self.old)),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def _say(self, text):
        entry = await sessions.get_thread("9.0")
        req = app.commands.parse(text)
        self.assertTrue(await app.run_headless(FakeClient(), "C1", "9.0", entry, req, req.prompt))
        return FolderRun.executed[-1]

    async def test_follow_up_resumes_in_same_folder(self):
        await self._say("claude first")
        run_id, prompt = await self._say("second")
        self.assertEqual(run_id, "run-1")
        self.assertEqual(prompt, "second")  # resumed with the new message

    async def test_follow_up_moves_to_new_workdir(self):
        await self._say("claude first")
        with patch.object(app, "WORKDIR", str(self.new)):
            run_id, prompt = await self._say("second")
        self.assertEqual(run_id, "run-2")
        self.assertIsNone(prompt)  # fresh run starts from its own prompt
        self.assertEqual(FolderRun.runs["run-2"]["cwd"], str(self.new.resolve()))

    async def test_cwd_sticks_to_thread_and_can_change(self):
        await self._say(f"claude cwd={self.pin} first")
        with patch.object(app, "WORKDIR", str(self.new)):
            run_id, _ = await self._say("second")
            self.assertEqual(run_id, "run-1")  # pinned folder survives a new default
            run_id, _ = await self._say(f"cwd={self.new} third")
        self.assertEqual(run_id, "run-2")
        self.assertEqual(FolderRun.runs["run-2"]["cwd"], str(self.new.resolve()))

    async def test_legacy_thread_keeps_a_non_default_folder(self):
        await self._say(f"claude cwd={self.pin} first")
        await sessions.set_thread("9.0", {k: v for k, v in (await sessions.get_thread("9.0")).items() if k != "cwd"})
        run_id, _ = await self._say("second")
        self.assertEqual(run_id, "run-1")

    async def test_running_turn_is_not_doubled_by_a_folder_change(self):
        await self._say("claude first")
        FolderRun.runs["run-1"]["status"] = "running"
        with patch.object(app, "WORKDIR", str(self.new)):
            run_id, _ = await self._say("second")
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(FolderRun.runs), 1)

    async def test_deleted_pinned_folder_falls_back_to_workdir(self):
        await self._say(f"claude cwd={self.pin} first")
        self.pin.rmdir()
        run_id, _ = await self._say("second")
        self.assertEqual(FolderRun.runs[run_id]["cwd"], str(self.old.resolve()))


if __name__ == "__main__":
    unittest.main()
