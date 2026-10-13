"""System watches: the ~/.neo launchd jobs listed on the Watches screen."""

import json
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos import system_watches  # noqa: E402


class SystemWatchesTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        (self.home / "Library/Logs").mkdir(parents=True)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        system_watches._cache.update(at=0.0, rows=[])

    def job(self, name, program, **extra):
        label = f"com.anil.neo-{name}"
        log = self.home / f"Library/Logs/neo-{name}.log"
        body = {"Label": label, "ProgramArguments": program, "StandardOutPath": str(log), **extra}
        (self.home / f"Library/LaunchAgents/{label}.plist").write_bytes(plistlib.dumps(body))
        return label, log

    def folder(self, name):
        d = self.home / ".neo" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_lists_neo_jobs_with_schedule_status_and_open_breaches(self):
        feed = self.folder("feed-watch")
        (feed / "check.py").write_text('"""W6 desktop-update-feed-health: is the feed up?\n\nMore."""\n')
        (feed / "state.json").write_text(json.dumps({"open": {
            "missing/windows-11/arm64": {"posted_at": time.time(), "detail": "404 on latest.yml"},
            "broken": "not a dict"}}))
        label, log = self.job("feed-watch", ["/py", str(feed / "check.py")], StartInterval=1800)
        log.write_text("ran\n")
        daily = self.folder("vendor-balance")
        (daily / "backie").mkdir()
        (daily / "backie/state.json").write_text(json.dumps({"open": {"openrouter:credit": {"posted_at": 1.0, "detail": "$20 left"}}}))
        self.job("vendor-balance", ["/bin/sh", "-c", f"cd {daily}; run"],
                 StartCalendarInterval={"Hour": 9, "Minute": 0})
        self.job("slack-bot", ["/py", "app.py"], StartInterval=60)  # not a watch: no ~/.neo/<dir>/
        self.job("off-one", ["/py", str(self.folder("off-one") / "check.py")])

        with patch.object(system_watches, "launchctl_status",
                          return_value={label: 0, "com.anil.neo-vendor-balance": 1}):
            rows = {r["name"]: r for r in system_watches.collect()}

        self.assertEqual(set(rows), {"Feed watch", "Vendor balance", "Off one"})
        feed_row = rows["Feed watch"]
        self.assertEqual(feed_row["schedule"], "Every 30 minutes")
        self.assertEqual(feed_row["about"], "W6 desktop-update-feed-health: is the feed up?")
        self.assertTrue(feed_row["loaded"])
        self.assertEqual(feed_row["last_exit"], 0)
        self.assertIsNotNone(feed_row["last_run"])
        self.assertEqual(feed_row["open"], 1)
        self.assertEqual(feed_row["breaches"][0]["key"], "missing/windows-11/arm64")
        vb = rows["Vendor balance"]
        self.assertEqual(vb["schedule"], "Daily at 09:00")
        self.assertEqual(vb["last_exit"], 1)
        self.assertEqual(vb["breaches"][0]["key"], "backie: openrouter:credit")
        self.assertIsNone(vb["last_run"])  # no log yet
        self.assertFalse(rows["Off one"]["loaded"])
        self.assertEqual(rows["Off one"]["schedule"], "At load only")

    def test_unknown_launchctl_status_is_not_reported_as_off(self):
        self.job("x", ["/py", str(self.folder("x") / "check.py")], StartInterval=3600)
        with patch.object(system_watches, "launchctl_status", return_value=None):
            (row,) = system_watches.collect()
        self.assertIsNone(row["loaded"])
        self.assertEqual(row["schedule"], "Every hour")

    def test_bad_files_are_skipped(self):
        (self.home / "Library/LaunchAgents/com.anil.neo-bad.plist").write_text("not a plist")
        d = self.folder("y")
        (d / "check.py").write_text("def (:\n")
        (d / "state.json").write_text("{not json")
        self.job("y", ["/py", str(d / "check.py")])
        with patch.object(system_watches, "launchctl_status", return_value={}):
            (row,) = system_watches.collect()
        self.assertEqual((row["about"], row["open"]), ("", 0))

    def test_listing_caches_and_survives_a_failure(self):
        with patch.object(system_watches, "collect", return_value=[{"id": "a"}]) as c:
            self.assertEqual(system_watches.listing(), [{"id": "a"}])
            system_watches.listing()
            self.assertEqual(c.call_count, 1)
        system_watches._cache["at"] = 0.0
        with patch.object(system_watches, "collect", side_effect=RuntimeError("boom")):
            self.assertEqual(system_watches.listing(), [{"id": "a"}])

    def test_launchctl_status_parses_only_neo_jobs(self):
        out = "PID\tStatus\tLabel\n-\t0\tcom.anil.neo-a\n123\t-15\tcom.anil.neo-b\n-\t1\tcom.apple.x\n"
        with patch.object(system_watches.subprocess, "run",
                          return_value=system_watches.subprocess.CompletedProcess([], 0, out, "")):
            self.assertEqual(system_watches.launchctl_status(), {"com.anil.neo-a": 0, "com.anil.neo-b": -15})
        with patch.object(system_watches.subprocess, "run", side_effect=OSError):
            self.assertIsNone(system_watches.launchctl_status())

    def test_bad_minute_or_dotdot_folder_is_skipped_not_fatal(self):
        self.job("bad-cal", ["/py", str(self.folder("bad-cal") / "check.py")],
                 StartCalendarInterval={"Hour": 9, "Minute": "x"})
        self.job("up", ["/py", str(self.home / ".neo/../check.py")])
        with patch.object(system_watches, "launchctl_status", return_value={}):
            rows = system_watches.collect()
        self.assertEqual([r["name"] for r in rows], ["Bad cal"])
        self.assertEqual(rows[0]["schedule"], "On a calendar schedule")

    def test_watches_listing_includes_system_rows(self):
        from cos import watches
        with patch.object(system_watches, "listing", return_value=[{"id": "x"}]), \
                patch.object(watches, "load", return_value=[]), patch.object(watches, "open_findings", return_value=[]):
            self.assertEqual(watches.listing()["system"], [{"id": "x"}])


if __name__ == "__main__":
    unittest.main()
