"""Reading Slack for an agent: the channel (or DM) its request came from, with its threads and shared files.

An agent runs `neo-slack` (deploy/linux/neo-slack) with its run's token, like neo-ask. When its person has
connected their own Slack (cos/slackauth.py), it reads and searches with their token: whatever they can see.
Otherwise it may read only the conversation the run was started from (the run's slack_channel), through neo's
bot: its person asked there, so they can see it. app.py hands over the bot token at start.
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


def _api(method: str, token: Optional[str] = None, **params) -> dict:
    """A Slack Web API call with the person's own token when given, else neo's bot."""
    token = token or TOKEN
    if not token:
        raise RuntimeError("neo's Slack connection isn't available here")
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
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


def history(channel: str, limit: int = 100, before: Optional[str] = None, token: Optional[str] = None) -> list[dict]:
    """The channel's latest messages, oldest first."""
    data = _api("conversations.history", token, channel=channel, limit=max(1, min(int(limit), MAX_MESSAGES)), latest=before)
    return [_message(m) for m in reversed(data.get("messages") or [])]


def thread(channel: str, ts: str, token: Optional[str] = None) -> list[dict]:
    data = _api("conversations.replies", token, channel=channel, ts=ts, limit=MAX_MESSAGES)
    return [_message(m) for m in data.get("messages") or []]


def search(query: str, token: str, limit: int = 30) -> list[dict]:
    """Slack search with the person's own token (bots can't search): what they could find themselves."""
    data = _api("search.messages", token, query=query, count=max(1, min(int(limit), 100)), sort="timestamp")
    out = []
    for m in (data.get("messages") or {}).get("matches") or []:
        row = _message(m)
        row["channel"] = (m.get("channel") or {}).get("id")
        row["channel_name"] = (m.get("channel") or {}).get("name")
        row["link"] = m.get("permalink")
        out.append(row)
    return out


def find_channel(name: str, token: str) -> Optional[str]:
    """A channel id from a name the person gave (#eng-backend), among channels they're in."""
    name = name.strip().lstrip("#").lower()
    cursor = None
    for _ in range(10):
        data = _api("users.conversations", token, types="public_channel,private_channel", limit=500, cursor=cursor,
                    exclude_archived="true")
        for c in data.get("channels") or []:
            if c.get("name", "").lower() == name:
                return c["id"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None
    return None


def file(channel: Optional[str], file_id: str, token: Optional[str] = None) -> tuple[str, bytes]:
    """(name, bytes) of a file. With neo's bot, only one shared in this conversation; with the person's own
    token, any file Slack lets them open."""
    info = _api("files.info", token, file=file_id)["file"]
    shared = set(info.get("channels") or []) | set(info.get("groups") or []) | set(info.get("ims") or [])
    shared |= set(((info.get("shares") or {}).get("public") or {}).keys()) | set(((info.get("shares") or {}).get("private") or {}).keys())
    if not token and channel not in shared:
        raise PermissionError("that file wasn't shared in this conversation (say \"connect my slack\" to let your neo open files you can see)")
    if int(info.get("size") or 0) > MAX_FILE_BYTES:
        raise ValueError("that file is over 20 MB")
    url = info.get("url_private_download") or info.get("url_private")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token or TOKEN}"})
    return info.get("name") or file_id, urllib.request.urlopen(req, timeout=60).read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]
