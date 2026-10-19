"""Post a Slack message as neo (the bot), for scheduled jobs.

Jobs used the Slack connector, which posts as Anil ("Sent using Claude"). This posts with
the bot token instead. The token stays out of agent runs: app.py pops it from the
environment, and this reads it from the repo's .env only for the one request.

While notifications are paused the message is held in ~/.neo/<job>/held.json, the same
place jobs already hold messages, and neo posts it when the pause ends.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
from slack_sdk import WebClient

from . import pause

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
MAX_TEXT = 3800  # same clip as the held-message scheduler in app.py
_JOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def bot_token(env_file: Path = ENV_FILE) -> str:
    token = os.environ.get("SLACK_BOT_TOKEN") or dotenv_values(env_file).get("SLACK_BOT_TOKEN") or ""
    if not token.startswith("xoxb-"):
        raise ValueError(f"no Slack bot token (SLACK_BOT_TOKEN) in the environment or {env_file}")
    return token


def hold(job: str, channel: str, thread_ts: Optional[str], text: str) -> Path:
    """Queue a message in ~/.neo/<job>/held.json. A corrupt file raises ValueError (nothing is saved)."""
    if not _JOB_NAME.fullmatch(job):
        raise ValueError(f"bad job name: {job!r}")
    path = pause.jobs_dir() / job / "held.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    held = json.loads(path.read_text()) if path.exists() else []
    held.append({"channel": channel, "thread_ts": thread_ts, "text": text})
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(held, indent=2))
    os.replace(tmp, path)
    return path


def post(channel: str, text: str, thread_ts: Optional[str] = None, job: Optional[str] = None,
         client: Optional[WebClient] = None) -> dict:
    """Post as neo, or hold the message while paused. Returns {"posted": bool, "ts"|"held": ...}."""
    if not channel or not text.strip():
        raise ValueError("channel and text are required")
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    if pause.is_paused():
        if not job:
            raise ValueError("notifications are paused; pass --job so the message is held, not lost")
        return {"posted": False, "held": str(hold(job, channel, thread_ts, text))}
    client = client or WebClient(token=bot_token())
    resp = client.chat_postMessage(channel=channel, thread_ts=thread_ts or None, text=text, unfurl_links=False)
    return {"posted": True, "ts": resp["ts"], "channel": resp["channel"]}
