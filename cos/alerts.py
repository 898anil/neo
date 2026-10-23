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
# Status posts that a CI bot creates when a run starts and then edits in place as jobs finish.
_FAILED_RE = re.compile(r"\b(fail(ed|ure|ing)?|error|cancel(l)?ed|timed out)\b|:x:|:rotating_light:|:red_circle:", re.I)
_RUNNING_RE = re.compile(r"\b(running|waiting|queued|in progress|started|starting)\b|:arrows_counterclockwise:|:hourglass", re.I)
_PASSED_RE = re.compile(r"\b(passed|succeeded|success|green)\b|:white_check_mark:|:tada:", re.I)
# Only posts that look like a CI run's status are held while running or skipped when green; a plain
# alert ("success rate dropped", "5000 messages queued") always gets its RCA.
_CI_RE = re.compile(r"actions/runs/|\b(ci|e2e|nightly|sweep|workflow|pipeline|builds?|gate \d+|checks?|jobs?)\b", re.I)
PENDING_HOURS = 6  # how long an in-progress status post is re-checked for its final state


def alert_signature(text: str) -> str:
    """Same alert, different ids/timestamps -> same signature, so repeats get one RCA."""
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{16,}|\b\d[\d:.\-TZ ]{4,}\b|<!date[^>]+>", "#", text.lower())
    return " ".join(text.split())[:160]


def is_resolved(text: str) -> bool:
    """Alertmanager's "[RESOLVED] ..." notifications: the alert is over, nothing to explain."""
    return bool(_RESOLVED_RE.search(text))


def _status_part(text: str) -> str:
    """The text minus links and summary lines, which quote commit subjects ("fix: fail closed ...")."""
    text = re.sub(r"<[^>|]+\|?[^>]*>", " ", text)
    return "\n".join(line for line in text.splitlines() if not re.match(r"\W*summary:", line, re.I))


def run_state(text: str) -> str:
    """'failed' if any failure shows, else 'running' while jobs are still going, else 'passed'
    when only success markers are left, else 'other' (a plain alert with no run status)."""
    is_ci = bool(_CI_RE.search(text))
    text = _status_part(text)
    if _FAILED_RE.search(text):
        return "failed"
    if not is_ci:
        return "other"
    if _RUNNING_RE.search(text):
        return "running"
    if _PASSED_RE.search(text):
        return "passed"
    return "other"


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
        self.pending: dict[str, float] = {}  # "channel:ts" of in-progress status posts -> first seen

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
        self._prune(now)
        if text.strip() and not is_resolved(text) and run_state(text) == "running":
            # Not final yet: the bot edits this post as jobs finish. Look again on the edit or poll.
            self.pending.setdefault(key, now)
            return "still running"
        self.pending.pop(key, None)
        self.taken[key] = now
        if not text.strip():
            return "empty"
        if is_resolved(text):
            return "resolved notification"
        if run_state(text) == "passed":
            return "passed"
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

    def pending_ts(self, channel: str) -> list[str]:
        """In-progress status posts in this channel still worth re-checking."""
        return [k.split(":", 1)[1] for k in self.pending if k.startswith(f"{channel}:")]

    def release(self, text: str) -> None:
        """Give back what admit() took for an alert whose RCA was not run (notifications paused):
        its daily-cap slot and its repeat window, so the same alert after the pause is still looked at."""
        self.seen.pop(alert_signature(text), None)
        self.state["count"] = max(0, self.state.get("count", 0) - 1)
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
        for key in [k for k, at in self.pending.items() if now - at > PENDING_HOURS * 3600]:
            del self.pending[key]
