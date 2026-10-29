"""A person's own dashboard (/api/me): their tasks with traces, their watches, work between neos, their tools.
Everything here is scoped to one Slack id; cos/server.py sends a person's session nowhere else."""

from __future__ import annotations

import json
import os
from typing import Optional

from . import accounts, broker, people, tokens, watches
from .runner import ENGINES, Run, list_runs, valid_run_id

EVENT_LIMIT = 400
ACTIVE_PER_PERSON = 3  # tasks running at once, started from the dashboard
STARTS_PER_HOUR = 20


def _name(user: str) -> str:
    name = people.load(user).get("name")
    if name:
        return name
    account = accounts.everyone().get(user) or ""
    return account.removeprefix("neo-").capitalize() or user


def _title(meta: dict) -> str:
    from .config import OWNER_MESSAGE_MARK

    text = " ".join((meta.get("title") or meta.get("prompt") or "").split())
    return (text.rpartition(OWNER_MESSAGE_MARK)[2].strip() or text)[:160]


def _row(meta: dict) -> dict:
    return {"id": meta["id"], "title": _title(meta), "status": meta.get("status"), "engine": meta.get("engine"),
            "created_at": meta.get("created_at"), "source": meta.get("source"), "kind": meta.get("kind") or "task",
            "slack_url": meta.get("slack_url"), "cost_usd": meta.get("cost_usd")}


def overview(user: str) -> dict:
    mine = [m for m in list_runs(500) if m.get("slack_user") == user and m.get("kind") not in ("brief",)]
    when = {f: people.daily(user, f) for f in ("brief", "digest")}
    return {
        "user": user, "name": _name(user), "role": people.role(user),
        "runs": [_row(m) for m in mine[:50]],
        "watches": [{"id": w["id"], "name": w["name"], "every": w["every"], "enabled": w.get("enabled", True)}
                    for w in watches.owned_by(user)],
        "asked": [{k: t.get(k) for k in ("id", "request", "status", "target", "result", "created")} for t in broker.tasks_for(user)][:20],
        "doing": [{k: t.get(k) for k in ("id", "request", "status", "asker", "created")} for t in broker.tasks_for(user, asked=False)][:20],
        "tokens": sorted(tokens.connected(user)),
        "brief": f"{when['brief'][0]:02d}:{when['brief'][1]:02d}" if when["brief"] else None,
        "digest": f"{when['digest'][0]:02d}:{when['digest'][1]:02d}" if when["digest"] else None,
        "engines": list(ENGINES),
    }


def run_detail(user: str, run_id: str) -> Optional[dict]:
    """One of their runs with its steps; None when it isn't theirs (or doesn't exist)."""
    if not valid_run_id(run_id):
        return None
    run = Run(run_id)
    try:
        meta = run.meta()
    except (OSError, ValueError):
        return None
    if meta.get("slack_user") != user or meta.get("kind") == "brief":
        return None
    events = []
    try:
        with (run.dir / "events.jsonl").open() as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("kind") in ("debug", "usage", "session"):
                    continue
                if meta.get("kind", "").startswith("a2a") and event.get("kind") in ("prompt", "tool_result"):
                    continue  # another person's words and raw tool output: their neo's answer is what's theirs
                events.append({k: event.get(k) for k in ("kind", "title", "body", "ts")})
    except OSError:
        pass
    return {**_row(meta), "prompt": _title(meta), "events": events[-EVENT_LIMIT:]}


def check_rate(user: str) -> None:
    """At most ACTIVE_PER_PERSON running and STARTS_PER_HOUR started from the dashboard per person."""
    from datetime import datetime

    mine = [m for m in list_runs(300) if m.get("slack_user") == user and m.get("source") == "dashboard"]
    if sum(1 for m in mine if m.get("status") in ("queued", "running")) >= ACTIVE_PER_PERSON:
        raise ValueError(f"You have {ACTIVE_PER_PERSON} tasks running; wait for one to finish.")
    hour_ago = datetime.now().timestamp() - 3600
    recent = 0
    for m in mine:
        try:
            recent += datetime.fromisoformat(m["created_at"]).timestamp() >= hour_ago
        except (KeyError, ValueError):
            pass
    if recent >= STARTS_PER_HOUR:
        raise ValueError("That's a lot of tasks this hour; try again a little later.")


def start(user: str, prompt: str, engine: Optional[str] = None) -> Run:
    """A task they start from the dashboard: in their own account, with their role, like one from Slack."""
    from . import learn

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Say what you'd like done.")
    if people.role(user) == "blocked":
        raise PermissionError("not set up to use neo")
    if not accounts.enabled():
        raise PermissionError("starting tasks from the dashboard needs each person's own account on this machine")
    account = accounts.ensure(user, _name(user))
    settings = people.run_settings(user, os.environ.get("NEO_MODE", "full"))
    from .config import default_engine
    engine = settings["engine"] or (engine if engine in ENGINES else None) or people.load(user).get("engine") or default_engine()
    system = "\n\n".join(filter(None, (learn.brief(None, user=user), broker.charter(user), broker.guide(), accounts.brief(account))))
    extra = {"account": account, "slack_user": user, "role": people.role(user), "system_prompt": system, "conclude": True,
             "title": prompt[:80]}
    if settings["cli_args"]:
        extra["cli_args"] = settings["cli_args"]
    if settings["model"]:
        extra["model"] = settings["model"]
    return Run.create(engine, prompt, accounts.cwd_for(account, None), settings["mode"], source="dashboard", extra=extra)
