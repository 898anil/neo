"""Maps a Slack thread to the run it drives, so replies continue the same session.

Each entry: {"run_id", "engine", "runtime": "headless" | "herdr"}. The run's own
meta.json (under runs/) holds the engine session id and herdr agent name.

Backed by a small JSON file so threads survive a bot restart. Access is guarded
by an asyncio lock because the Bolt app handles events concurrently.
"""

from __future__ import annotations

import asyncio
import json

from cos.runner import cos_home

_STORE_PATH = cos_home() / "sessions.json"
_lock = asyncio.Lock()


def _read() -> dict[str, dict]:
    if not _STORE_PATH.exists():
        return {}
    try:
        data = json.loads(_STORE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt store should not wedge the bot; start fresh.
        return {}
    # Before multi-engine support, entries were bare Codex session ids.
    return {
        thread: ({"engine": "codex", "runtime": "headless", "session_id": value} if isinstance(value, str) else value)
        for thread, value in data.items()
    }


def _write(data: dict[str, dict]) -> None:
    tmp = _STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_STORE_PATH)


async def get_thread(thread_ts: str) -> dict | None:
    """Return what this thread is bound to, if anything."""
    async with _lock:
        return _read().get(thread_ts)


async def set_thread(thread_ts: str, entry: dict) -> None:
    async with _lock:
        data = _read()
        data[thread_ts] = entry
        _write(data)


async def clear_thread(thread_ts: str) -> None:
    async with _lock:
        data = _read()
        data.pop(thread_ts, None)
        _write(data)
