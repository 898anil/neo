"""The morning brief: one Claude run that reads your calendar, mail, Slack, GitHub, Sentry and
agents, and writes what needs you today. It is an ordinary run (kind "brief"), so it shows up in
History with every step it took."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Callable, Optional

from . import fleet
from .config import owner_fields, read_prompt, workdir
from .runner import Run, list_runs

_lock = threading.Lock()


def _agents_summary() -> str:
    try:
        rows = fleet.agents()
    except Exception:  # noqa: BLE001 - the brief still works without it
        return "My coding agents: (could not be listed)." + _findings_summary()
    if not rows:
        return "My coding agents: none running." + _findings_summary()
    lines = ["My coding agents right now:"]
    for a in rows:
        said = " ".join((a.get("last_text") or "").split())[:160]
        state = "asked me something" if a.get("asks") else a["status"]
        lines.append(f"- {a['title']} ({a['repo']}, {a['engine']}): {state}. Last said: {said}")
    return "\n".join(lines) + _findings_summary()


def _findings_summary() -> str:
    """Problems my watches found that nobody has handled yet (they already checked Sentry and logs)."""
    from . import watches

    try:
        rows = watches.open_findings()
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return "\n\nMy watches (Sentry, logs, CI) have no open findings."
    lines = ["", "", "Open findings from my watches (already checked; mention the important ones under Production):"]
    for f in rows[:12]:
        state = "an agent is on it" if f["status"] == "working" else "new"
        lines.append(f"- [{f['severity']}] {f['title']} ({f['watch']}, {state}) {f.get('link') or ''}".rstrip())
    return "\n".join(lines)


def prompt(now: Optional[datetime] = None) -> str:
    from .mentions import bot_names

    now = now or datetime.now()
    template = read_prompt("brief.md") or "Write my morning brief for {date}.\n\n{agents}"
    return template.format(date=now.strftime("%-d %B %Y"), weekday=now.strftime("%A"), agents=_agents_summary(),
                           bot=" or ".join(n.capitalize() for n in bot_names()), **owner_fields())


def running() -> Optional[dict]:
    for meta in list_runs(40):
        if meta.get("kind") == "brief" and meta["status"] in ("queued", "running"):
            return meta
    return None


def start(source: str = "dashboard", on_done: Optional[Callable[[dict], None]] = None) -> dict:
    """Start a brief in the background (or return the one already running)."""
    with _lock:
        current = running()
        if current:
            return current
        run = Run.create("claude", prompt(), workdir(), "full", source=source, extra={"kind": "brief", "title": "Morning brief"})

    def work():
        try:
            meta = run.execute()
        except Exception as exc:  # noqa: BLE001 - recorded in the run
            run.append_event({"kind": "error", "title": "Brief failed", "body": repr(exc), "data": {}}, 1)
            meta = run.update_meta(status="failed", pid=None)
        if on_done:
            on_done(latest() or meta)

    threading.Thread(target=work, daemon=True, name=f"brief-{run.id}").start()
    return run.meta()


def _final_text(run_id: str) -> str:
    text = ""
    with (Run(run_id).dir / "events.jsonl").open() as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event["kind"] in ("result", "text") and event.get("body"):
                text = event["body"]
    return text


def latest() -> Optional[dict]:
    """The newest finished brief, plus whether a new one is being written right now (the old
    one stays visible meanwhile)."""
    writing, done = None, None
    for meta in list_runs(200):
        if meta.get("kind") != "brief":
            continue
        if meta["status"] in ("queued", "running") and not writing:
            writing = meta
        elif meta["status"] == "done":
            done = meta
            break
    if not writing and not done:
        return None
    out = {"writing": bool(writing), "writing_run_id": writing["id"] if writing else None}
    if done:
        out.update(run_id=done["id"], created_at=done["created_at"], text=_final_text(done["id"]),
                   cost_usd=done.get("cost_usd"))
    return out
