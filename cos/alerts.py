"""Auto RCA gate: decides which messages in an alert channel get a Claude RCA.

A message can reach us twice (the Slack `message` event and the backstop poll of
conversations.history), so the gate remembers which ts it already took. Repeats of the same
alert within DEDUPE_MINUTES get one RCA, and a daily cap bounds the cost of an alert storm.
State (the day's count) is kept in a JSON file so a restart does not reset the cap.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Optional

DEDUPE_MINUTES = 30
# Message subtypes that are real posts. Joins, edits, deletes, topic changes etc. are not alerts.
_POST_SUBTYPES = (None, "bot_message", "file_share")
_RESOLVED_RE = re.compile(r"\[resolved\b|^\W*resolved\b|\bstatus:\s*resolved\b", re.I)


def alert_signature(text: str) -> str:
    """Same alert, different ids/timestamps -> same signature, so repeats get one RCA."""
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{16,}|\b\d[\d:.\-TZ ]{4,}\b|<!date[^>]+>", "#", text.lower())
    return " ".join(text.split())[:160]


def is_resolved(text: str) -> bool:
    """Alertmanager's "[RESOLVED] ..." notifications: the alert is over, nothing to explain."""
    return bool(_RESOLVED_RE.search(text))


def is_candidate(msg: dict, bot_user_id: Optional[str]) -> bool:
    """A new top-level post by someone other than neo (alert bots included)."""
    if msg.get("subtype") not in _POST_SUBTYPES or msg.get("hidden"):
        return False
    if msg.get("thread_ts") not in (None, msg.get("ts")):
        return False
    return not (bot_user_id and msg.get("user") == bot_user_id)


class AlertGate:
    def __init__(self, state_file: str, daily_max: int) -> None:
        self.state_file, self.daily_max = state_file, daily_max
        try:
            with open(state_file) as fh:
                self.state: dict = json.load(fh)
        except (OSError, ValueError):
            self.state = {}
        self.seen: dict[str, float] = {}  # signature -> when we last took it
        self.taken: dict[str, float] = {}  # "channel:ts" -> when we took or rejected it

    def admit(self, channel: str, msg: dict, text: str, bot_user_id: Optional[str], now: Optional[float] = None) -> Optional[str]:
        """Return None to run an RCA for this message, else the reason it is skipped.

        Synchronous on purpose: the event handler and the poll share one event loop, so a check
        and its bookkeeping cannot interleave.
        """
        now = time.time() if now is None else now
        key = f"{channel}:{msg.get('ts')}"
        if key in self.taken:
            return "already handled"
        if not is_candidate(msg, bot_user_id):
            return "not a new top-level post"
        self.taken[key] = now
        self._prune(now)
        if not text.strip():
            return "empty"
        if is_resolved(text):
            return "resolved notification"
        sig = alert_signature(text)
        if now - self.seen.get(sig, 0) < DEDUPE_MINUTES * 60:
            return "repeat of a recent alert"
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.state.get("day") != today:
            self.state.update(day=today, count=0)
        if self.state["count"] >= self.daily_max:
            return "daily cap reached"
        self.seen[sig] = now
        self.state["count"] += 1
        self.save()
        return None

    def release(self, text: str, channel: Optional[str] = None, ts: Optional[str] = None) -> None:
        """Give back what admit() took for an alert whose RCA was not run.

        Passing the alert identity also allows a deliberate retry of the same message;
        callers that only need to free the daily-cap/repeat reservation may omit it.
        """
        self.seen.pop(alert_signature(text), None)
        self.state["count"] = max(0, self.state.get("count", 0) - 1)
        if channel and ts:
            self.taken.pop(f"{channel}:{ts}", None)
        self.save()

    def defer_retry(self, channel: str, msg: dict, text: str, reset_at: int) -> dict:
        """Persist one quota-skipped alert until the provider's stated reset time."""
        key = f"{channel}:{msg.get('ts')}"
        retries = self.state.get("quota_retries")
        if not isinstance(retries, dict):
            retries = self.state["quota_retries"] = {}
        saved = {k: msg[k] for k in ("ts", "subtype", "thread_ts", "user") if k in msg}
        saved["text"] = text
        retry = {"channel": channel, "msg": saved, "reset_at": int(reset_at)}
        retries[key] = retry
        self.save()
        return retry

    def pending_retries(self) -> list[dict]:
        retries = self.state.get("quota_retries")
        if not isinstance(retries, dict):
            return []
        return [retry for retry in retries.values() if isinstance(retry, dict)]

    def clear_retry(self, channel: str, ts: str) -> None:
        retries = self.state.get("quota_retries")
        if isinstance(retries, dict):
            retries.pop(f"{channel}:{ts}", None)
            self.save()

    def cursor(self, channel: str) -> Optional[str]:
        return self.state.get(channel)

    def set_cursor(self, channel: str, ts: str) -> None:
        self.state[channel] = ts

    def save(self) -> None:
        with open(self.state_file, "w") as fh:
            json.dump(self.state, fh)

    def _prune(self, now: float) -> None:
        day = 24 * 3600
        for store in (self.taken, self.seen):
            for key in [k for k, at in store.items() if now - at > day]:
                del store[key]
