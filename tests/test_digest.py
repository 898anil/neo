"""Trace digests: model-written HTML beside a trace, cleaned, cached and rebuilt only when stale."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
NOW = 1_790_000_000.0


class Proc:
    def __init__(self, stdout):
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def target(key="run:r1", mtime=NOW - 100, **kw):
    return {"key": key, "kind": "run", "run_id": "r1", "title": "t", "mtime": mtime, "working": False, **kw}


class CleanTest(unittest.TestCase):
    def setUp(self):
        from cos import digest
        self.d = digest

    def test_removes_scripts_handlers_and_script_urls(self):
        dirty = ('<div onclick="steal()" class="x">ok<script>alert(1)</script>'
                 '<a href="javascript:alert(1)">bad</a><a href=\'data:text/html,x\'>d</a>'
                 '<iframe src="https://evil"></iframe><img src=x onerror=alert(1)>'
                 '<meta http-equiv="refresh" content="0;url=https://evil"><form action="/x"><input></form></div>')
        out = self.d.clean(dirty)
        for bad in ("<script", "alert(1)</", "onclick", "onerror", "javascript:", "data:text", "<iframe", "<meta", "<form", "<input"):
            self.assertNotIn(bad, out.lower(), bad)
        self.assertIn('class="x">ok', out)

    def test_keeps_ordinary_markup_svg_and_links(self):
        html = ('<style>.box{color:var(--mint)}</style><div class="box">Cause</div>'
                '<svg viewBox="0 0 10 10"><rect width="4" height="4"/></svg>'
                '<a href="https://github.com/o/r/pull/1">PR #1</a>')
        self.assertEqual(self.d.clean(html), html)

    def test_caps_size(self):
        self.assertEqual(len(self.d.clean("a" * 300_000)), self.d.MAX_HTML)


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})
        self.env.start()
        from cos import digest
        self.d = digest
        self.d._building.clear()
        self.d._failed.clear()

    def tearDown(self):
        self.env.stop()

    def test_store_round_trips_and_keys_are_safe(self):
        self.d.put("agent:a/b:c", {"kind": "rca", "html": "<b>x</b>", "source_mtime": 1, "made_at": 2})
        self.assertEqual(self.d.get("agent:a/b:c")["html"], "<b>x</b>")
        self.assertIsNone(self.d.get("run:other"))
        self.assertNotIn("/", self.d._path("agent:a/b:c").name)

    def test_stale_rules(self):
        have = {"source_mtime": NOW - 100, "made_at": NOW - 90, "version": self.d.VERSION}
        self.assertTrue(self.d.needs_build(target(), None, NOW))
        self.assertFalse(self.d.needs_build(target(), have, NOW))
        self.assertTrue(self.d.needs_build(target(mtime=NOW - 10), have, NOW))
        working = target(mtime=NOW - 10, working=True)
        self.assertFalse(self.d.needs_build(working, have, NOW))  # remade under 5 min ago
        self.assertTrue(self.d.needs_build(working, {**have, "made_at": NOW - 400}, NOW))

    def test_a_new_prompt_rebuilds_old_digests(self):
        have = {"source_mtime": NOW - 100, "made_at": NOW - 90, "version": "old"}
        self.assertTrue(self.d.needs_build(target(), have, NOW))
        self.assertFalse(self.d.needs_build(target(), {**have, "version": self.d.VERSION}, NOW))

    def test_housekeeping_runs_get_no_digest(self):
        for kind in ("mentions", "loops", "watch", "brief", "mention-send"):
            with patch.object(self.d, "target_for", return_value=target(run_kind=kind)), \
                 patch.object(self.d, "start") as start:
                self.assertEqual(self.d.status("run:r1", {}, NOW), {"status": "none", "skipped": True})
                start.assert_not_called()

    def test_build_stores_cleaned_html(self):
        out = {"structured_output": {"kind": "rca", "html": "<div>Cause</div><script>x()</script>"}}
        with patch.object(self.d.cards, "load_events", return_value=[{"kind": "text", "body": "found it"}]), \
             patch.object(self.d.subprocess, "run", return_value=Proc(json.dumps(out))) as run:
            got = self.d.build(target())
        self.assertEqual((got["kind"], got["html"]), ("rca", "<div>Cause</div>"))
        self.assertEqual(self.d.get("run:r1")["source_mtime"], NOW - 100)
        cmd = run.call_args[0][0]
        self.assertIn(self.d.MODEL, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")

    def test_failed_call_stores_nothing(self):
        with patch.object(self.d.cards, "load_events", return_value=[{"kind": "text", "body": "x"}]), \
             patch.object(self.d.subprocess, "run", return_value=Proc("not json")):
            self.assertIsNone(self.d.build(target()))
        with patch.object(self.d.cards, "load_events", return_value=[{"kind": "text", "body": "x"}]), \
             patch.object(self.d.subprocess, "run", return_value=Proc(json.dumps({"structured_output": {"kind": "rca", "html": " "}}))):
            self.assertIsNone(self.d.build(target()))
        self.assertIsNone(self.d.get("run:r1"))

    def test_status_writes_when_missing_then_is_ready(self):
        made = {"kind": "plan", "html": "<p>plan</p>", "source_mtime": NOW - 100, "made_at": NOW, "version": self.d.VERSION}
        def fake_build(t):
            self.d.put(t["key"], made)
            return made
        with patch.object(self.d, "target_for", return_value=target()), patch.object(self.d, "build", side_effect=fake_build):
            self.assertEqual(self.d.status("run:r1", {}, NOW), {"status": "writing"})
            for _ in range(50):
                if not self.d._building:
                    break
                time.sleep(0.02)
            got = self.d.status("run:r1", {}, NOW)
        self.assertEqual((got["status"], got["kind"], got["stale"]), ("ready", "plan", False))

    def test_a_failed_build_is_not_retried_by_polling_until_refresh(self):
        with patch.object(self.d, "target_for", return_value=target()), patch.object(self.d, "build", return_value=None):
            self.d.status("run:r1", {}, NOW)
            for _ in range(50):
                if not self.d._building:
                    break
                time.sleep(0.02)
            with patch.object(self.d, "start") as start:
                self.assertEqual(self.d.status("run:r1", {}, time.time()), {"status": "none", "failed": True})
                start.assert_not_called()
            with patch.object(self.d, "start", return_value=True) as start:
                self.assertEqual(self.d.refresh("run:r1", {}), {"status": "writing"})
                start.assert_called_once()

    def test_stale_digest_is_shown_while_a_new_one_is_written(self):
        self.d.put("run:r1", {"kind": "fix", "html": "<p>old</p>", "source_mtime": NOW - 500, "made_at": NOW - 500,
                              "version": self.d.VERSION})
        with patch.object(self.d, "target_for", return_value=target()), patch.object(self.d, "start", return_value=True):
            got = self.d.status("run:r1", {}, NOW)
        self.assertEqual((got["status"], got["html"], got["stale"], got["writing"]), ("ready", "<p>old</p>", True, True))

    def test_unknown_key_is_none(self):
        self.assertFalse(self.d.valid_key("../../etc"))
        self.assertTrue(self.d.valid_key("run:20261002-030625-38b4ce"))
        self.assertEqual(self.d.status("run:nope", {"agents": []}, NOW), {"status": "none", "failed": False})


if __name__ == "__main__":
    unittest.main()
