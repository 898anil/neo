"""System watches: the launchd jobs under ~/.neo (com.anil.neo-*) that check things on a schedule
outside the bot, so the Watches screen can show them next to the bot's own watches. Read-only.

A job counts when its LaunchAgent runs something in ~/.neo/<dir>/. Its row says whether launchd has
it loaded, its schedule, when its log last changed (the last run), its last exit status, and the
breaches it is holding open (state.json written by ~/.neo/lib/neowatch.py; one per sub-watch, e.g.
vendor-balance/backie/state.json).
"""

from __future__ import annotations

import ast
import json
import logging
import plistlib
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PREFIX = "com.anil.neo-"
CACHE_SECONDS = 30
MAX_OPEN = 20  # open breaches listed per job
_DIR = re.compile(r"/\.neo/([A-Za-z0-9_.-]+)(?=[/\s;'\"]|$)")

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "rows": []}


def agents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def neo_dir() -> Path:
    return Path.home() / ".neo"


def launchctl_status() -> Optional[dict[str, Optional[int]]]:
    """Label -> last exit status (None when it has not exited yet) for every loaded job, or None if launchctl failed."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    loaded: dict[str, Optional[int]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].startswith(PREFIX):
            try:
                loaded[parts[2]] = int(parts[1])
            except ValueError:
                loaded[parts[2]] = None
    return loaded


def schedule(job: dict) -> str:
    every = job.get("StartInterval")
    if isinstance(every, int) and every > 0:
        if every % 3600 == 0:
            return "Every hour" if every == 3600 else f"Every {every // 3600} hours"
        minutes = max(1, every // 60)
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    cal = job.get("StartCalendarInterval")
    cal = [cal] if isinstance(cal, dict) else cal if isinstance(cal, list) else []
    times = sorted({f"{c.get('Hour', 0):02d}:{c.get('Minute', 0):02d}" for c in cal
                    if isinstance(c, dict) and isinstance(c.get("Hour"), int)
                    and isinstance(c.get("Minute", 0), int)})
    if times:
        return "Daily at " + ", ".join(times[:4])
    if cal:
        return "On a calendar schedule"
    return "At load only"


def describe(folder: Path) -> str:
    """First line of check.py's docstring, the job's own one-line description."""
    try:
        doc = ast.get_docstring(ast.parse((folder / "check.py").read_text()))
    except (OSError, SyntaxError, ValueError):
        return ""
    lines = (doc or "").strip().splitlines()
    return lines[0][:200] if lines else ""


def open_breaches(folder: Path) -> list[dict]:
    """Breaches held open in neowatch state files (folder/state.json and folder/*/state.json)."""
    found = []
    for path in sorted([folder / "state.json", *folder.glob("*/state.json")]):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows = state.get("open") if isinstance(state, dict) else None
        if not isinstance(rows, dict):
            continue
        scope = path.parent.name if path.parent != folder else ""
        for key, v in sorted(rows.items()):
            if isinstance(v, dict) and isinstance(v.get("posted_at"), (int, float)):
                found.append({"key": f"{scope}: {key}" if scope else key, "detail": str(v.get("detail", ""))[:300],
                              "since": iso(v["posted_at"])})
    return found


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


SKIP = {".", "..", "lib", "worktrees"}


def collect() -> list[dict]:
    status = launchctl_status()
    rows = []
    for plist in sorted(agents_dir().glob(PREFIX + "*.plist")):
        try:
            row = _row(plist, status)
        except Exception:  # one odd job must not hide the others
            log.exception("system watches: skipping %s", plist.name)
            continue
        if row:
            rows.append(row)
    return rows


def _row(plist: Path, status: Optional[dict[str, Optional[int]]]) -> Optional[dict]:
    try:
        job = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(job, dict):
        return None
    args = job.get("ProgramArguments") or []
    m = _DIR.search(" ".join(a for a in args if isinstance(a, str)))
    if not m or m.group(1) in SKIP or not (neo_dir() / m.group(1)).is_dir():
        return None  # the bot and the viewer, not watches
    label = str(job.get("Label") or plist.stem)
    folder = neo_dir() / m.group(1)
    log_path = job.get("StandardOutPath")
    try:
        last_run = iso(Path(log_path).stat().st_mtime) if isinstance(log_path, str) else None
    except OSError:
        last_run = None
    breaches = open_breaches(folder)
    return {
        "id": label,
        "name": m.group(1).replace("-", " ").capitalize(),
        "folder": str(folder),
        "about": describe(folder),
        "schedule": schedule(job),
        "loaded": None if status is None else label in status,
        "last_exit": (status or {}).get(label),
        "last_run": last_run,
        "open": len(breaches),
        "breaches": breaches[:MAX_OPEN],
    }


def listing() -> list[dict]:
    """Cached for CACHE_SECONDS: the Watches screen polls this."""
    with _lock:
        if time.time() - _cache["at"] > CACHE_SECONDS:
            try:
                rows = collect()
            except Exception:  # never take the Watches screen down; keep the last good rows
                log.exception("system watches: listing failed")
                rows = _cache["rows"]
            _cache.update(at=time.time(), rows=rows)
        return _cache["rows"]
