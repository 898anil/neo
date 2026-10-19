"""Auto RCA gate: which alert-channel messages get an RCA."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos.alerts import AlertGate, is_resolved, run_state  # noqa: E402

BOT = "UNEO"
NOW = 1_790_000_000.0


def post(ts, text="", **extra):
    return {"ts": ts, "text": text, **extra}


class AlertGateTest(unittest.TestCase):
    def setUp(self):
        self.file = os.path.join(tempfile.mkdtemp(), "auto_rca.json")
        self.gate = AlertGate(self.file, daily_max=3)

    def admit(self, msg, text=None, now=NOW, channel="C1"):
        return self.gate.admit(channel, msg, msg.get("text", "") if text is None else text, BOT, now=now)

    def test_bot_alert_is_admitted_once_across_event_and_poll(self):
        msg = post("1.1", "[FIRING:1] PodCrashLooping api", subtype="bot_message", bot_id="B1")
        self.assertIsNone(self.admit(msg))
        self.assertEqual(self.admit(msg), "already handled")

    def test_skips_own_posts_replies_and_non_posts(self):
        self.assertIsNotNone(self.admit(post("1.1", "x", user=BOT)))
        self.assertIsNotNone(self.admit(post("1.2", "x", thread_ts="1.0")))
        self.assertIsNotNone(self.admit(post("1.3", "x", subtype="channel_join")))
        self.assertIsNotNone(self.admit(post("1.4", "x", subtype="message_changed")))

    def test_thread_parent_counts_as_top_level(self):
        self.assertIsNone(self.admit(post("1.1", "error report", thread_ts="1.1")))

    def test_skips_resolved_and_empty(self):
        self.assertEqual(self.admit(post("1.1", "[RESOLVED] PodCrashLooping api")), "resolved notification")
        self.assertEqual(self.admit(post("1.2", "   ")), "empty")

    def test_repeat_within_window_is_deduped_then_allowed(self):
        self.assertIsNone(self.admit(post("1.1", "HighErrorRate api at 2026-10-01 10:00:00")))
        self.assertEqual(self.admit(post("1.2", "HighErrorRate api at 2026-10-01 10:05:00"), now=NOW + 60), "repeat of a recent alert")
        self.assertIsNone(self.admit(post("1.3", "HighErrorRate api at 2026-10-01 11:00:00"), now=NOW + 3600))

    def test_daily_cap_persists_across_restart(self):
        for i in range(3):
            self.assertIsNone(self.admit(post(f"1.{i}", f"alert {'abc'[i]}")))
        self.assertEqual(self.admit(post("1.9", "alert z")), "daily cap reached")
        self.assertEqual(AlertGate(self.file, daily_max=3).admit("C1", post("2.0", "alert y"), "alert y", BOT, now=NOW), "daily cap reached")

    def test_running_status_post_waits_for_its_final_edit(self):
        start = post("1.1", "E2E nightly\n*Web:* :arrows_counterclockwise: Running\n*macOS:* :hourglass_flowing_sand: Waiting")
        self.assertEqual(self.admit(start), "still running")
        self.assertEqual(self.gate.pending_ts("C1"), ["1.1"])
        failed = post("1.1", ":x: E2E nightly — Failed\n*Web:* :x: Failed\n*macOS:* :white_check_mark: Passed\n4/8 passed | 4 failed")
        self.assertIsNone(self.admit(failed, now=NOW + 600))
        self.assertEqual(self.gate.pending_ts("C1"), [])
        self.assertEqual(self.admit(failed, now=NOW + 700), "already handled")

    def test_status_post_that_ends_green_gets_no_rca(self):
        self.assertEqual(self.admit(post("1.1", "E2E nightly\n*Web:* Running")), "still running")
        self.assertEqual(self.admit(post("1.1", ":tada: E2E nightly — Passed\n*Web:* :white_check_mark: Passed")), "passed")

    def test_partial_failure_is_acted_on_without_waiting(self):
        self.assertIsNone(self.admit(post("1.1", ":x: E2E nightly — Failed\n*Web:* :x: Failed\n*macOS:* Running")))

    def test_sweep_failure_is_admitted(self):
        self.assertIsNone(self.admit(post("1.1", ":rotating_light: *main sweep failed* — `1e1b317`\n  - gate2-tests: failure")))

    def test_pending_expires(self):
        self.admit(post("1.1", "CI\n*Gate 1:* running"))
        self.admit(post("2.1", "unrelated plain alert"), now=NOW + 7 * 3600)
        self.assertEqual(self.gate.pending_ts("C1"), [])

    def test_commit_subject_does_not_decide_the_state(self):
        start = ("🔄 Running — macOS · Web · CI Nightly\n\n*main at start:* <https://x/c/1|1e1b317>\n"
                 "*Summary:* fix(release): fail closed when the error matrix is empty")
        self.assertEqual(run_state(start), "running")
        done = "✅ E2E nightly — Passed\n8/8 passed | *main at start:* <https://x/c/1|fix: keep failed runs out>"
        self.assertEqual(run_state(done), "passed")

    def test_run_state(self):
        self.assertEqual(run_state("[FIRING:1] PodCrashLooping api"), "other")
        self.assertEqual(run_state(":tada: CI success"), "passed")
        self.assertEqual(run_state("Gate 2: cancelled"), "failed")
        # Plain alerts that only happen to use a status word are not CI runs: they always get an RCA.
        self.assertEqual(run_state("Payment success rate dropped to 82%"), "other")
        self.assertEqual(run_state("DLQ: 5000 messages queued"), "other")
        self.assertEqual(run_state("Incident started for policy checkout-latency"), "other")

    def test_is_resolved(self):
        self.assertTrue(is_resolved(":white_check_mark: [RESOLVED] X"))
        self.assertTrue(is_resolved("Status: resolved"))
        self.assertFalse(is_resolved("[FIRING:2] X (resolved_by=none)"))


if __name__ == "__main__":
    unittest.main()
