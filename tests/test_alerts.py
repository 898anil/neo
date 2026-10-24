"""Auto RCA gate: which alert-channel messages get an RCA."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cos.alerts import AlertGate, is_resolved  # noqa: E402

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

    def test_releasing_a_quota_skipped_alert_allows_its_retry(self):
        msg = post("1.1", "[FIRING:1] PodCrashLooping api", subtype="bot_message", bot_id="B1")
        self.assertIsNone(self.admit(msg))
        self.gate.release(msg["text"], "C1", msg["ts"])
        self.assertIsNone(self.admit(msg))

    def test_quota_retry_survives_a_bot_restart(self):
        msg = post("1.1", "[FIRING:1] PodCrashLooping api", subtype="bot_message", bot_id="B1")
        self.gate.defer_retry("C1", msg, msg["text"], 2_000)
        restored = AlertGate(self.file, daily_max=3)
        self.assertEqual(restored.pending_retries(), [{
            "channel": "C1", "msg": {
                "ts": "1.1", "subtype": "bot_message", "text": msg["text"],
            }, "reset_at": 2_000,
        }])

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

    def test_is_resolved(self):
        self.assertTrue(is_resolved(":white_check_mark: [RESOLVED] X"))
        self.assertTrue(is_resolved("Status: resolved"))
        self.assertFalse(is_resolved("[FIRING:2] X (resolved_by=none)"))


if __name__ == "__main__":
    unittest.main()
