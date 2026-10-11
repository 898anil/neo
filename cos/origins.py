"""Which agent conversation a pull request came from, so home links the trace instead of GitHub.

A PR on its own gives you nothing to act on; the agent that opened it does (its trace, and a
composer to tell it what to do next). The index maps every PR URL an agent touched with a tool
(gh pr create, gh pr view, a push that printed the URL) to the transcripts where that happened,
earliest first. Mentions in plain prompts don't count: cards and digests quote PR links in their
prompts without having worked on them.

The scan reads the Claude Code and Codex transcripts on this machine in a background thread,
only files changed since the last pass, so the home endpoint never waits on it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import memory

PR_RE = re.compile(rb"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# A line where the agent used a tool (Claude Code tool_use/tool_result, Codex function calls).
TOOL_MARKERS = (b'"tool_use"', b'"tool_result"', b'"function_call"', b'"function_call_output"', b"exec_command", b"custom_tool_call")
SCAN_DAYS = 60
REFRESH_SECONDS = 120
# neo's own runs that read PR links without working on them.
HOUSEKEEPING_KINDS = {"loops", "mentions", "watch", "brief", "ask", "mention-send"}

_lock = threading.Lock()
_mentions: dict[str, dict[str, tuple[str, str]]] = {}  # pr url -> {path: (first ts, engine)}
_files: dict[str, tuple[float, int]] = {}  # path -> (mtime, size) at the last scan
_sids: dict[str, Optional[str]] = {}  # path -> session id
_housekeeping: set[str] = set()  # session ids of neo's own background runs, from every run on disk
_runs_read: set[str] = set()
_scanned_at = 0.0
_scanning = False


def normalize(url: str) -> str:
    m = PR_RE.match((url or "").encode())
    return m.group(0).decode() if m else ""


def tool_mentions(data: bytes) -> dict[str, str]:
    """PR url -> timestamp of its first mention on a tool line in one transcript."""
    found: dict[str, str] = {}
    if b"/pull/" not in data:
        return found
    for line in data.splitlines():
        if b"/pull/" not in line or not any(m in line for m in TOOL_MARKERS):
            continue
        urls = {u.decode() for u in PR_RE.findall(line)}
        new = urls - found.keys()
        if not new:
            continue
        try:
            ts = str(json.loads(line).get("timestamp") or "")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            ts = ""
        for url in new:
            found[url] = ts or "~"  # no timestamp sorts last
    return found


def housekeeping(meta: dict) -> bool:
    return meta.get("kind") in HOUSEKEEPING_KINDS or meta.get("source") == "schedule"


def _read_runs() -> None:
    from .runner import runs_dir

    for path in runs_dir().iterdir():
        if path.name in _runs_read:
            continue
        try:
            meta = json.loads((path / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not meta.get("session_id"):
            continue  # not started yet; look again next pass
        _runs_read.add(path.name)
        if housekeeping(meta):
            with _lock:
                _housekeeping.add(meta["session_id"])


def _scan() -> None:
    global _scanned_at, _scanning
    try:
        _read_runs()
        cutoff = time.time() - SCAN_DAYS * 86400
        for engine, root in memory.roots().items():
            if not root.is_dir():
                continue
            for path in root.rglob("*.jsonl"):
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                if st.st_mtime < cutoff or _files.get(key) == (st.st_mtime, st.st_size):
                    continue
                try:
                    found = tool_mentions(path.read_bytes())
                except OSError:
                    continue
                with _lock:
                    _files[key] = (st.st_mtime, st.st_size)
                    for url, ts in found.items():
                        _mentions.setdefault(url, {})[key] = (ts, engine)
    finally:
        with _lock:
            _scanned_at = time.time()
            _scanning = False


def refresh(wait: bool = False) -> None:
    """Start a background scan if the index is older than REFRESH_SECONDS."""
    global _scanning
    with _lock:
        if _scanning or time.time() - _scanned_at < REFRESH_SECONDS:
            return
        _scanning = True
    if wait:
        _scan()
    else:
        threading.Thread(target=_scan, daemon=True, name="pr-origins").start()


def _session_id(path: str, engine: str) -> Optional[str]:
    if path not in _sids:
        try:
            info = memory.session_info(Path(path), engine)
            _sids[path] = None if memory.is_internal(info) else info.get("session_id")
        except OSError:
            _sids[path] = None
    return _sids[path]


def trace_for(url: str, agents_by_sid: dict, runs_by_sid: dict) -> Optional[str]:
    """Dashboard link to the conversation that first worked on this PR: its live agent if one is
    still open, else its neo run, else the saved session. None if no agent here touched it."""
    url = normalize(url)
    with _lock:
        candidates = sorted((ts, path, engine) for path, (ts, engine) in _mentions.get(url, {}).items())
    for _ts, path, engine in candidates:
        sid = _session_id(path, engine)
        run = runs_by_sid.get(sid) if sid else None
        if (run and housekeeping(run)) or sid in _housekeeping:
            continue
        if sid is None and engine == "claude":
            continue  # a bookkeeping session (see memory.INTERNAL_TITLES)
        agent = agents_by_sid.get(sid) if sid else None
        if agent:
            return f"#/agent/{quote(agent['session'], safe='')}/{quote(str(agent['pane_id']), safe='')}"
        if run:
            return f"#/run/{run['id']}"
        return f"#/session/{quote(path, safe='')}"
    return None
