"""Ask neo about your work from the dashboard: "where is the HubSpot work happening?", "what's the
status of the Doppler fix?". The answer is a read-only Claude run (kind "ask") that starts from a
snapshot of every agent, recent run and loop (with their decision cards), and can open transcripts
when the snapshot is not enough. Follow-up questions continue the same run."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import fleet
from .config import owner_fields, owner_name
from .loops import STAGE_LABELS

SNAPSHOT_CHARS = 30_000
RUN_HOURS = 48

PROMPT = """{Owner} is asking about their work, on their phone. Answer from the snapshot below; open a transcript
(paths given) or run read-only commands only when the snapshot does not answer it. Read only: change nothing.

Answer in at most 8 short lines:
- Where: which agent, run or PR, its repo / worktree, and a link to it (dashboard links are given as
  [title](#/...); keep them exactly so they can tap them).
- Status: what is done, what is not, and how sure you are.
- Next: the one thing that would move it forward, and whether it needs them.
If nothing matches, say so and name the closest matches.

---- SNAPSHOT (taken when they asked; for later follow-ups, look things up again if it matters) ----
{snapshot}"""


def _ago(t: Optional[float], now: float) -> str:
    if not t:
        return "?"
    s = now - t
    return f"{int(s // 60)}m ago" if s < 3600 else f"{int(s // 3600)}h ago" if s < 86400 else f"{int(s // 86400)}d ago"


def _card_line(card: Optional[dict]) -> str:
    if not card:
        return "    summary: none yet"
    out = f"    summary: {card.get('result')}: {card.get('did', '')}"
    if card.get("needs_you"):
        out += f"\n    needs {owner_name()}: {card['needs_you']}"
    return out


def snapshot(overview: dict, loops_rows: list[dict], cards_by_key: dict, now: float) -> str:
    parts = ["## Agents (interactive)"]
    for a in overview.get("agents", []):
        href = f"#/agent/{quote(a['session'], safe='')}/{quote(str(a['pane_id']), safe='')}"
        parts.append(f"- [{a.get('title') or a.get('name')}]({href}) — {a.get('attention') or a.get('status')}, "
                     f"last active {_ago(a.get('last_active'), now)}; repo {a.get('repo')}, cwd {a.get('cwd')}, "
                     f"{a.get('engine')}; transcript {a.get('transcript') or '?'}\n" + _card_line(cards_by_key.get(fleet._seen_key(a))))
    parts.append("\n## Background runs (Slack, alerts, dashboard)")
    for r in overview.get("tasks", []):
        updated = fleet._iso_ts(r.get("updated_at"))
        if updated and now - updated > RUN_HOURS * 3600:
            continue
        slack = f", Slack {r['slack_url']}" if r.get("slack_url") else ""
        parts.append(f"- [{r.get('title') or r['id']}](#/run/{r['id']}) — {r.get('status')}, from {r.get('source')}, "
                     f"updated {_ago(updated, now)}{slack}\n" + _card_line(cards_by_key.get(f"run:{r['id']}")))
    parts.append("\n## Issues and PRs being followed (loops)")
    for l in loops_rows:
        last = (l.get("history") or [{}])[-1]
        links = ", ".join([*(l["links"].get("prs") or []), *[v for k, v in l["links"].items() if k != "prs" and v]])
        flags = "".join([" REGRESSED" if l.get("regressed") else "", f" STALLED: {l['stalled']}" if l.get("stalled") else ""])
        parts.append(f"- {l['title']} ({l['kind']}) — {STAGE_LABELS.get(l['stage'], l['stage'])}, {l['status']}{flags}; "
                     f"last: {last.get('note', '')} {last.get('evidence', '')}; links: {links or 'none'}")
    text = "\n".join(parts)
    return text if len(text) <= SNAPSHOT_CHARS else text[:SNAPSHOT_CHARS] + "\n… (snapshot cut; open the dashboard data for more)"


def system_prompt(snap: str) -> str:
    """Standing instructions plus the snapshot; the visible prompt is just their question."""
    return PROMPT.format(snapshot=snap, **owner_fields())


def current_snapshot() -> str:
    from . import cards, loops

    overview = fleet.overview()
    for a in overview.get("agents", []):
        path = fleet._transcript_for(a["engine"], a.get("session_id"), a.get("cwd", "")) if a.get("engine") in ("claude", "codex") else None
        a["transcript"] = str(path) if path and Path(path).exists() else None
    return snapshot(overview, loops.listing(), cards.STORE.read(), time.time())
