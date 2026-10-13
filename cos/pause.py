"""Pause neo's notifications: while paused, nothing is posted to Slack unless you asked for it.

Stored as `notifications_paused_until` in COS_HOME/settings.json: an epoch time, or "forever"
(until `@neo resume`). Replies in a thread where you just mentioned @neo still go out; what
stops is everything neo posts on its own (agent notifications, briefs, digests, urgent pings,
watch findings, auto RCA, and the ~/.neo scheduled checks, which read the same file).

Scheduled agent jobs (~/.neo/<job>/, see their launchd plists) are told when they run paused to keep
what they would have posted in ~/.neo/<job>/held.json: [{"channel", "thread_ts", "text"}].
The bot posts those once the pause ends (take_held).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

from . import settings

KEY = "notifications_paused_until"
FOREVER = "forever"
_UNITS = {"m": 60, "h": 3600, "d": 86400}
DURATION = re.compile(r"^(\d{1,4})\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?)$")


def parse_duration(text: str) -> float:
    """'30m' / '2h' / '1d' -> seconds. Raises ValueError for anything else."""
    match = DURATION.match(text.strip().lower())
    if not match or int(match.group(1)) == 0:
        raise ValueError(f"not a duration: {text!r} (use 30m, 2h or 1d)")
    return int(match.group(1)) * _UNITS[match.group(2)[0]]


def paused_until(now: Optional[float] = None) -> Optional[float]:
    """When the pause ends (float('inf') for until-resumed), or None when not paused."""
    value = settings.load().get(KEY)
    if value == FOREVER:
        return float("inf")
    if isinstance(value, (int, float)) and value > (time.time() if now is None else now):
        return float(value)
    return None


def is_paused(now: Optional[float] = None) -> bool:
    return paused_until(now) is not None


def pause(seconds: Optional[float] = None, now: Optional[float] = None) -> Optional[float]:
    """Pause for `seconds`, or until resumed when None. Returns the end time (None = forever)."""
    until = None if seconds is None else (time.time() if now is None else now) + seconds
    settings.save(**{KEY: FOREVER if until is None else until})
    return until


def resume() -> bool:
    """Lift the pause. Returns whether one was active."""
    was = is_paused()
    settings.save(**{KEY: None})
    return was


def describe(now: Optional[float] = None) -> str:
    until = paused_until(now)
    if until is None:
        return "Notifications are on."
    if until == float("inf"):
        return "Notifications are paused until you say `@neo resume`."
    return f"Notifications are paused until {datetime.fromtimestamp(until):%a %H:%M}."


def state(now: Optional[float] = None) -> dict:
    """For the dashboard: {"paused": bool, "until": epoch | "forever" | None, "text": describe()}."""
    until = paused_until(now)
    return {"paused": until is not None, "until": FOREVER if until == float("inf") else until, "text": describe(now).replace("`", "")}


def apply(choice: Optional[str]) -> dict:
    """Dashboard choice: None / "on" resumes, "forever" pauses until resumed, "2h" pauses that long."""
    if choice in (None, "on"):
        resume()
    else:
        pause(None if choice == FOREVER else parse_duration(choice))
    return state()


def jobs_dir() -> Path:
    return Path(os.environ.get("NEO_JOBS_DIR", Path.home() / ".neo"))


def take_held() -> list[dict]:
    """Remove and return the messages scheduled jobs held back while paused (~/.neo/<job>/held.json).
    Each item keeps its source file under "_held_in", so a failed post can be put back (hold_again)."""
    out = []
    for path in sorted(jobs_dir().glob("*/held.json")):
        try:
            held = json.loads(path.read_text())
            path.unlink()
        except (OSError, ValueError):
            continue
        out += [{**m, "_held_in": str(path)} for m in held if isinstance(m, dict) and m.get("channel") and m.get("text")] \
            if isinstance(held, list) else []
    return out


def hold_again(item: dict) -> None:
    """Put a held message whose post failed back, to try again on the next pass."""
    path = Path(item["_held_in"])
    try:
        held = json.loads(path.read_text())
    except (OSError, ValueError):
        held = []
    held.append({k: v for k, v in item.items() if k != "_held_in"})
    path.write_text(json.dumps(held, indent=2))
