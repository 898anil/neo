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
import os
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
MAX_OPEN = 100  # open breaches listed per job (a longer list is cut and not synced as findings)
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


def launchctl_running() -> set[str]:
    """Labels of neo jobs running right now (launchctl list shows a PID)."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {p[2] for p in (line.split(None, 2) for line in out.splitlines()[1:])
            if len(p) == 3 and p[2].startswith(PREFIX) and p[0].isdigit()}


def last_line(log_path: object) -> str:
    """The last non-empty line of the job's log: what its last run said."""
    if not isinstance(log_path, str):
        return ""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            lines = [ln.strip() for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


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
        if not isinstance(state, dict):
            continue
        scope = path.parent.name if path.parent != folder else ""
        rows = {}  # posted ("open") and confirmed-but-not-yet-posted ("held": paused or Slack failed)
        for part, stamp in (("held", "since"), ("open", "posted_at")):
            for key, v in (state.get(part) if isinstance(state.get(part), dict) else {}).items():
                if isinstance(v, dict) and isinstance(v.get(stamp), (int, float)):
                    rows[key] = (v, v[stamp])
        for key, (v, stamp) in sorted(rows.items()):
            found.append({"key": f"{scope}: {key}" if scope else key, "detail": str(v.get("detail", ""))[:300],
                          "since": iso(stamp)})
    return found


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


SKIP = {".", "..", "lib", "worktrees"}
NEVER = {"com.anil.neo-slack-bot", "com.anil.neo-viewer"}  # neo itself: never started or stopped from here


def collect() -> list[dict]:
    """One row per ~/.neo folder; several jobs on one folder (e.g. hourly + daily) share a row."""
    status = launchctl_status()
    running = launchctl_running()
    rows: dict[str, dict] = {}
    for plist in sorted(agents_dir().glob(PREFIX + "*.plist")):
        try:
            row = _row(plist, status, running)
        except Exception:  # one odd job must not hide the others
            log.exception("system watches: skipping %s", plist.name)
            continue
        if not row:
            continue
        same = rows.get(row["folder"])
        if same is None:
            rows[row["folder"]] = row
            continue
        same["id"] = min(same["id"], row["id"])
        same["labels"] += row["labels"]
        same["plists"] += row["plists"]
        if row["kind"] != "check":
            same["kind"] = "job"
        same["running"] = same["running"] or row["running"]
        if row["last_run"] and (not same["last_run"] or row["last_run"] > same["last_run"]):
            same["summary"] = row["summary"] or same["summary"]
        same["schedule"] = f"{same['schedule']}; {row['schedule']}"
        if same["loaded"] is not None and row["loaded"] is not None:
            same["loaded"] = same["loaded"] or row["loaded"]
        same["last_exit"] = same["last_exit"] or row["last_exit"]  # a failure on either job shows
        same["last_run"] = max(filter(None, [same["last_run"], row["last_run"]]), default=None)
    return list(rows.values())


def _row(plist: Path, status: Optional[dict[str, Optional[int]]], running: frozenset | set = frozenset()) -> Optional[dict]:
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
    if label in NEVER:
        return None
    folder = neo_dir() / m.group(1)
    # Only a plain read-only check (python ~/.neo/<dir>/check.py ...) can be run or turned on/off from the
    # dashboard. Agent jobs (cos run --mode full), shell pipelines and the heartbeat are listed, not driven.
    check = (len(args) >= 2 and isinstance(args[1], str) and args[1] == str(folder / "check.py")
             and isinstance(args[0], str) and Path(args[0]).name.startswith("python"))
    if check:  # and it posts through neowatch (only on change), so an extra run is harmless
        try:
            check = "neowatch" in (folder / "check.py").read_text(errors="replace")
        except OSError:
            check = False
    log_path = job.get("StandardOutPath")
    try:
        last_run = iso(Path(log_path).stat().st_mtime) if isinstance(log_path, str) else None
    except OSError:
        last_run = None
    breaches = open_breaches(folder)
    return {
        "id": label,
        "labels": [label],
        "plists": [str(plist)],
        "kind": "check" if check else "job",
        "running": label in running,
        "summary": last_line(log_path),
        "name": m.group(1).replace("-", " ").capitalize(),
        "folder": str(folder),
        "about": describe(folder),
        "schedule": schedule(job),
        "loaded": None if status is None else label in status,
        "last_exit": (status or {}).get(label),
        "last_run": last_run,
        "open": len(breaches),
        "breaches": breaches[:MAX_OPEN],
        "complete": len(breaches) <= MAX_OPEN,
    }


def _find(watch_id: str) -> dict:
    """A system watch the dashboard may drive: a read-only check, never neo itself."""
    if watch_id in NEVER or not enabled():
        raise ValueError("no such system watch")
    for row in listing():
        if watch_id == row["id"] or watch_id in row["labels"]:
            if row.get("kind") != "check" or NEVER & set(row["labels"]):
                raise ValueError("this job runs an agent or a pipeline; ask neo in Slack to run or stop it")
            return row
    raise ValueError("no such system watch")


def _loaded() -> dict:
    status = launchctl_status()
    if status is None:
        raise RuntimeError("launchd status unavailable; try again")
    return status


def _launchctl(*args: str) -> None:
    try:
        done = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"launchctl {args[0]} is still working; refresh in a minute") from None
    if done.returncode != 0:
        raise RuntimeError(f"launchctl {args[0]} failed ({done.returncode}): {(done.stderr or done.stdout).strip()[:200]}")


def run(watch_id: str) -> dict:
    """Check now: start the job's run (launchd does nothing if it is already running)."""
    row = _find(watch_id)
    status = _loaded()
    loaded = [label for label in row["labels"] if label in status]
    if not loaded:
        raise ValueError("this watch is off; turn it on first")
    for label in loaded:
        _launchctl("kickstart", f"gui/{os.getuid()}/{label}")
    _cache["at"] = 0.0
    return {"ok": True}


def set_on(watch_id: str, on: bool) -> dict:
    """Turn a watch on (enable + load its LaunchAgent) or off (disable + unload; disabled survives a reboot,
    which a plain unload would not). Its files stay."""
    row = _find(watch_id)
    loaded = _loaded()
    for label, plist in zip(row["labels"], row["plists"]):
        target = f"gui/{os.getuid()}/{label}"
        if on:
            _launchctl("enable", target)
            if label not in loaded:
                _launchctl("bootstrap", f"gui/{os.getuid()}", plist)
        else:
            _launchctl("disable", target)
            if label in loaded:
                _launchctl("bootout", target)
    _cache["at"] = 0.0
    return {"ok": True}


def enabled() -> bool:
    return os.environ.get("NEO_SYSTEM_WATCHES", "1") == "1"


def listing() -> list[dict]:
    """Cached for CACHE_SECONDS: the Watches screen polls this. Each refresh also turns open breaches
    into findings (watches.sync_system), so they show as cards you can work on or dismiss."""
    if not enabled():
        return []
    with _lock:
        if time.time() - _cache["at"] > CACHE_SECONDS:
            try:
                rows = collect()
            except Exception:  # never take the Watches screen down; keep the last good rows
                log.exception("system watches: listing failed")
                rows = _cache["rows"]
            else:
                from . import watches  # watches imports this module
                try:
                    watches.sync_system(rows)
                except Exception:
                    log.exception("system watches: syncing findings failed")
            _cache.update(at=time.time(), rows=rows)
        return _cache["rows"]
