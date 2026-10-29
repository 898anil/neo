"""Reading Slack for an agent: the channel (or DM) its request came from, with its threads and shared files.

An agent runs `neo-slack` (deploy/linux/neo-slack) with its run's token, like neo-ask. It may read only the
conversation the run was started from (the run's slack_channel): its person asked there, so they can see it,
and neo's bot is in it. Nothing else: no other channels, no search. app.py hands over the bot token at start.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

TOKEN: Optional[str] = None  # set by app.py
MAX_MESSAGES = 300
MAX_FILE_BYTES = 20 * 1024 * 1024
_names: dict[str, str] = {}


def _api(method: str, **params) -> dict:
    if not TOKEN:
        raise RuntimeError("neo's Slack connection isn't available here")
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    data = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if not data.get("ok"):
        raise RuntimeError(f"Slack said: {data.get('error')}")
    return data


def _name(user: Optional[str]) -> str:
    if not user:
        return "someone"
    if user not in _names:
        try:
            profile = _api("users.info", user=user)["user"].get("profile") or {}
            _names[user] = profile.get("real_name") or profile.get("display_name") or user
        except Exception:  # noqa: BLE001
            _names[user] = user
    return _names[user]


def _message(m: dict) -> dict:
    files = [{"id": f.get("id"), "name": f.get("name") or f.get("title"), "type": f.get("filetype"),
              "size": f.get("size")} for f in m.get("files") or []]
    return {"ts": m.get("ts"), "who": m.get("username") or _name(m.get("user")), "text": m.get("text", ""),
            "replies": m.get("reply_count", 0), "files": files}


def history(channel: str, limit: int = 100, before: Optional[str] = None) -> list[dict]:
    """The channel's latest messages, oldest first."""
    data = _api("conversations.history", channel=channel, limit=max(1, min(int(limit), MAX_MESSAGES)), latest=before)
    return [_message(m) for m in reversed(data.get("messages") or [])]


def thread(channel: str, ts: str) -> list[dict]:
    data = _api("conversations.replies", channel=channel, ts=ts, limit=MAX_MESSAGES)
    return [_message(m) for m in data.get("messages") or []]


def file(channel: str, file_id: str) -> tuple[str, bytes]:
    """(name, bytes) of a file shared in this channel; refused if it wasn't shared there."""
    info = _api("files.info", file=file_id)["file"]
    shared = set(info.get("channels") or []) | set(info.get("groups") or []) | set(info.get("ims") or [])
    shared |= set(((info.get("shares") or {}).get("public") or {}).keys()) | set(((info.get("shares") or {}).get("private") or {}).keys())
    if channel not in shared:
        raise PermissionError("that file wasn't shared in this conversation")
    if int(info.get("size") or 0) > MAX_FILE_BYTES:
        raise ValueError("that file is over 20 MB")
    url = info.get("url_private_download") or info.get("url_private")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    return info.get("name") or file_id, urllib.request.urlopen(req, timeout=60).read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]
