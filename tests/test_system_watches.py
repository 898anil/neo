"""System watches: the ~/.neo launchd jobs listed on the Watches screen."""

import json
import os
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
        os.environ["NEO_SYSTEM_WATCHES"] = "1"
        self.addCleanup(os.environ.__setitem__, "NEO_SYSTEM_WATCHES", "0")
        cos = patch.dict(os.environ, {"COS_HOME": tempfile.mkdtemp()})  # never the live findings.json
        cos.start()
        self.addCleanup(cos.stop)
        self.home = Path(tempfile.mkdtemp())
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        (self.home / "Library/Logs").mkdir(parents=True)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        running = patch.object(system_watches, "launchctl_running", return_value=set())
        self.running = running.start()
        self.addCleanup(running.stop)
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
        with patch.object(system_watches, "collect", return_value=[{"id": "a"}]) as c, \
                patch("cos.watches.sync_system"):
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

    def test_two_jobs_on_one_folder_share_a_row(self):
        d = self.folder("security-drift")
        self.job("security-drift", ["/py", str(d / "check.py")], StartInterval=3600)
        self.job("security-drift-buckets", ["/py", str(d / "check.py"), "buckets"],
                 StartCalendarInterval={"Hour": 9, "Minute": 30})
        with patch.object(system_watches, "launchctl_status",
                          return_value={"com.anil.neo-security-drift": 0, "com.anil.neo-security-drift-buckets": 2}):
            (row,) = system_watches.collect()
        self.assertEqual(row["schedule"], "Daily at 09:30; Every hour")  # sorted plist names
        self.assertEqual(row["last_exit"], 2)

    def test_row_says_running_and_last_log_line(self):
        d = self.folder("feed-watch")
        label, log = self.job("feed-watch", ["/py", str(d / "check.py")], StartInterval=1800)
        log.write_text("first\n2026-10-13 feed-watch breaches=1 new=0\n\n")
        self.running.return_value = {label}
        with patch.object(system_watches, "launchctl_status", return_value={label: 0}):
            (row,) = system_watches.collect()
        self.assertTrue(row["running"])
        self.assertEqual(row["summary"], "2026-10-13 feed-watch breaches=1 new=0")
        self.assertEqual(row["labels"], [label])

    def test_check_now_and_on_off_drive_launchctl_for_every_job_of_the_watch(self):
        d = self.folder("security-drift")
        (d / "check.py").write_text("import neowatch as nw\n")
        self.job("security-drift", ["/x/bin/python", str(d / "check.py")], StartInterval=3600)
        self.job("security-drift-buckets", ["/x/bin/python", str(d / "check.py"), "buckets"])
        a = self.folder("deploy-watch")
        self.job("deploy-watch", ["/bin/sh", "-c", f"python -m cos run --mode full {a}/prompt.md"])
        both = {"com.anil.neo-security-drift": 0, "com.anil.neo-security-drift-buckets": 0}
        calls = []
        with patch.object(system_watches, "launchctl_status", return_value=both), \
                patch.object(system_watches, "_launchctl", side_effect=lambda *a: calls.append(a)):
            system_watches.run("com.anil.neo-security-drift-buckets")
            system_watches._cache["at"] = 0.0
            system_watches.set_on("com.anil.neo-security-drift", False)
            for job in ("com.anil.neo-deploy-watch", "com.anil.neo-slack-bot"):  # an agent job; neo itself
                with self.assertRaises(ValueError):
                    system_watches.set_on(job, False)
        uid = os.getuid()
        self.assertEqual(sorted(calls), [  # off = disable (survives reboot) + unload, per job
            ("bootout", f"gui/{uid}/com.anil.neo-security-drift"),
            ("bootout", f"gui/{uid}/com.anil.neo-security-drift-buckets"),
            ("disable", f"gui/{uid}/com.anil.neo-security-drift"),
            ("disable", f"gui/{uid}/com.anil.neo-security-drift-buckets"),
            ("kickstart", f"gui/{uid}/com.anil.neo-security-drift"),
            ("kickstart", f"gui/{uid}/com.anil.neo-security-drift-buckets")])
        calls.clear()
        system_watches._cache["at"] = 0.0
        with patch.object(system_watches, "launchctl_status", return_value={}), \
                patch.object(system_watches, "_launchctl", side_effect=lambda *a: calls.append(a)):
            with self.assertRaises(ValueError):
                system_watches.run("com.anil.neo-security-drift")  # off: turn it on first
            system_watches.set_on("com.anil.neo-security-drift", True)
            with self.assertRaises(ValueError):
                system_watches.run("com.anil.neo-not-a-watch")
        self.assertEqual(sorted(c[0] for c in calls), ["bootstrap", "bootstrap", "enable", "enable"])
        self.assertEqual(sorted(Path(c[2]).name for c in calls if c[0] == "bootstrap"),
                         ["com.anil.neo-security-drift-buckets.plist", "com.anil.neo-security-drift.plist"])

    def test_unknown_launchd_state_refuses_to_act(self):
        d = self.folder("feed-watch")
        (d / "check.py").write_text("import neowatch as nw\n")
        self.job("feed-watch", ["/x/bin/python", str(d / "check.py")])
        raw = self.folder("cost-report")
        (raw / "check.py").write_text("post_every_run()\n")  # no neowatch: a re-run would post again
        self.job("cost-report", ["/x/bin/python", str(raw / "check.py")])
        with patch.object(system_watches, "launchctl_status", return_value={"com.anil.neo-feed-watch": 0}):
            kinds = {r["name"]: r["kind"] for r in system_watches.listing()}
        self.assertEqual(kinds, {"Feed watch": "check", "Cost report": "job"})
        with patch.object(system_watches, "launchctl_status", return_value=None), \
                patch.object(system_watches, "_launchctl") as lc:
            with self.assertRaises(RuntimeError):
                system_watches.set_on("com.anil.neo-feed-watch", False)
            lc.assert_not_called()


class SyncFindingsTest(unittest.TestCase):
    def setUp(self):
        self.cos = tempfile.mkdtemp()
        patcher = patch.dict(os.environ, {"COS_HOME": self.cos})
        patcher.start()
        self.addCleanup(patcher.stop)
        from cos import watches
        self.w = watches

    def rows(self, *keys, complete=True):
        return [{"name": "Feed watch", "folder": "/x/.neo/feed-watch", "complete": complete,
                 "breaches": [{"key": k, "detail": f"{k} is broken", "since": "2026-10-13T00:00:00+00:00"} for k in keys]}]

    def cards(self):
        return {f["key"]: f["status"] for f in self.w._findings().values()}

    def test_breaches_become_cards_and_resolve_when_they_clear(self):
        self.w.sync_system(self.rows("a", "b"))
        self.assertEqual(self.cards(), {"a": "new", "b": "new"})
        f = next(f for f in self.w._findings().values() if f["key"] == "a")
        self.assertEqual((f["watch_id"], f["title"], f["watch"]), ("system:feed-watch", "Feed watch: a", "Feed watch"))
        self.w.sync_system(self.rows("b"))
        self.assertEqual(self.cards(), {"a": "resolved", "b": "new"})
        self.w.sync_system(self.rows("a", "b"))  # recurs: a fresh card
        self.assertEqual(self.cards(), {"a": "new", "b": "new"})

    def test_dismissed_stays_dismissed_while_open_and_working_is_kept(self):
        self.w.sync_system(self.rows("a", "b"))
        ids = {f["key"]: f["id"] for f in self.w._findings().values()}
        self.w.set_status(ids["a"], "dismissed")
        self.w.set_status(ids["b"], "working")
        self.w.sync_system(self.rows("a"))
        self.assertEqual(self.cards(), {"a": "dismissed", "b": "working"})

    def test_removed_job_cards_resolve(self):
        self.w.sync_system(self.rows("a"))
        other = [{"name": "Other", "folder": "/x/.neo/other", "breaches": []}]
        neo = Path(tempfile.mkdtemp())
        with patch.object(system_watches, "neo_dir", return_value=neo):
            (neo / "feed-watch").mkdir()  # folder still there: only unreadable this time, keep the card
            self.w.sync_system(other)
            self.assertEqual(self.cards(), {"a": "new"})
            (neo / "feed-watch").rmdir()  # really removed
            self.w.sync_system(other)
        self.assertEqual(self.cards(), {"a": "resolved"})

    def test_cut_list_resolves_nothing(self):
        self.w.sync_system(self.rows("a", "b"))
        self.w.sync_system(self.rows("a", complete=False))
        self.assertEqual(self.cards(), {"a": "new", "b": "new"})


if __name__ == "__main__":
    unittest.main()
