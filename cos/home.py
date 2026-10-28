"""The home screen, for someone who looks every 30–60 minutes: what needs a decision, what
regressed, what moved since the last look (one line per item), what is in flight, and a count
of everything quiet. Nothing is dropped: every agent, run and loop lands in exactly one section.

build() is pure (tests feed it dicts); home() gathers the real sources.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Optional
from urllib.parse import quote

from . import conclusion, fleet
from .loops import STAGE_LABELS, STAGES

DECIDE_ATTENTION = {"approve", "reply", "stuck", "failed"}
# A stopped agent or run whose last result was one of these did not finish its task; it needs a look
# (continue it, redirect it, or close it) even when it asked nothing.
UNFINISHED_RESULTS = {"partial", "failed", "unclear"}
MERGED = STAGES.index("merged")
# What the agent's own closing `Status:` line (cos/conclusion.py) says it needs. done / answered fall through
# to the card; they only stop an idle agent from counting as "stopped without a conclusion".
STATUS_NEEDS = {"needs-you": "you", "blocked": "you", "waiting": "others", "failed": "unfinished"}
RESULT_WORDS = {"worked": "Worked", "partial": "Partly done", "failed": "Failed", "unclear": "Unclear", "answered": "Answered"}


def _agent_href(row: dict) -> str:
    return f"#/agent/{quote(row['session'], safe='')}/{quote(str(row['pane_id']), safe='')}"


def _loop_href(loop: dict) -> str:
    links = loop.get("links") or {}
    return (links.get("prs") or [None])[0] or links.get("source") or links.get("rca_doc") or ""


def _loop_trace(loop: dict, traces: dict) -> Optional[str]:
    """The agent trace for a loop, so tapping it opens something you can act on, not just a PR:
    an alert's newest neo run in its Slack thread (key slack:<channel>:<ts>), the agent started on a
    finding, or the agent that worked on one of its PRs (see origins.py)."""
    parts = str(loop.get("key") or "").split(":")
    run_id = traces.get((parts[1], parts[2])) if len(parts) == 3 and parts[0] == "slack" else None
    if run_id:
        return f"#/run/{run_id}"
    if len(parts) == 2 and parts[0] == "finding" and traces.get(("finding", parts[1])):
        return traces[("finding", parts[1])]
    for url in (loop.get("links") or {}).get("prs") or []:
        if traces.get(("pr", url)):
            return traces[("pr", url)]
    return None


def loop_traces(loops_rows: list[dict], overview: dict, runs: list[dict]) -> dict:
    """("finding", id) and ("pr", url) -> dashboard link to the agent that worked on it."""
    from . import origins, watches

    out: dict = {}
    agents_by_sid = {a["session_id"]: a for a in overview.get("agents", []) if a.get("session_id")}
    runs_by_sid = {r["session_id"]: r for r in runs if r.get("session_id")}
    origins.refresh()
    for loop in loops_rows:
        parts = str(loop.get("key") or "").split(":")
        if len(parts) == 2 and parts[0] == "finding":
            work = (watches.get_finding(parts[1]) or {}).get("work") or {}
            if work.get("run_id"):
                out[("finding", parts[1])] = f"#/run/{work['run_id']}"
        for url in (loop.get("links") or {}).get("prs") or []:
            href = origins.trace_for(url, agents_by_sid, runs_by_sid)
            if href:
                out[("pr", url)] = href
    return out


def thread_runs(limit: int = 500, runs: Optional[list[dict]] = None) -> dict:
    """(slack channel, thread ts) -> newest run id started in that thread."""
    from .runner import list_runs
    out: dict = {}
    for meta in runs if runs is not None else list_runs(limit):  # newest first
        if meta.get("slack_channel") and meta.get("slack_thread"):
            out.setdefault((meta["slack_channel"], meta["slack_thread"]), meta["id"])
    return out


MONITOR_DAYS = 2  # a fix in prod (or verified) shows under Monitoring this long, then drops off
def _age(seconds: float) -> str:
    seconds = max(0, seconds)
    return f"{int(seconds // 60)}m" if seconds < 3600 else f"{int(seconds // 3600)}h" if seconds < 86400 else f"{int(seconds // 86400)}d"


def monitor_line(loop: dict, now: float) -> str:
    """What was actually watched: a repeat firing would have flagged it (loops.note_refire), and when the
    follow-up check last looked."""
    head = (f"In prod {_age(now - loop['stage_at'])}" if loop["stage"] == "deployed" else "Fixed · verified")
    quiet = " · no repeat firing" if loop.get("kind") == "alert" else ""
    checked = f" · checked {_age(now - loop['checked_at'])} ago" if loop.get("checked_at") else " · not checked yet"
    return head + quiet + checked


MONITOR_STAGES = ("deployed", "verified")
CARD_SLACK_SECONDS = 60  # an agent's last line can land just after its card was written
# An agent idle this long whose last stop has no card (the card writer failed or never ran) stopped without a
# conclusion: it goes to "Stopped before finishing", not to In flight where it reads like it is still working.
NO_CONCLUSION_SECONDS = 30 * 60


def _first_line(text: Optional[str], limit: int = 160) -> str:
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


# Ids that differ between firings of the same alert ("User-visible error report <uuid>").
_IDS = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,}|\d{5,})\b", re.I)


def _title_shape(title) -> str:
    return " ".join(_IDS.sub("#", str(title or "")).split()).lower()


def _merge_repeats(rows: list[dict]) -> list[dict]:
    """An alert that fires again opens a loop per firing; show one row per open alert title, led by
    the furthest-along (then newest) loop, carrying every id so Mute covers them all."""
    groups: dict = {}
    for loop in rows:
        same = (loop.get("kind"), _title_shape(loop.get("title")))
        groups.setdefault(same if loop.get("status") == "open" else ("id", loop["id"]), []).append(loop)
    out = []
    for group in groups.values():
        lead = max(group, key=lambda l: (bool(l.get("regressed")), STAGES.index(l["stage"]), l.get("updated_at", 0)))
        out.append({**lead, "repeats": len(group), "loop_ids": [l["id"] for l in group]})
    return out


def _run_time(t: dict) -> float:
    return float(t.get("_mtime") or fleet._iso_ts(t.get("updated_at")) or 0)


def _group_topics(rows: list[dict]) -> list[dict]:
    """One row per card topic (the first in order wins); the others ride along in `also`, so the
    same LeadIQ question from seven alert threads is one decision, not seven."""
    out, by_topic = [], {}
    for r in rows:
        t = r.get("topic")
        if t and t in by_topic:
            by_topic[t].setdefault("also", []).append({"key": r["key"], "title": r["title"], "href": r["href"]})
            continue
        if t:
            by_topic[t] = r
        out.append(r)
    return out


def mention_row(m: dict, now: float) -> dict:
    """Someone tagged the owner: the decision is whether to send neo's draft (or hand the work to neo)."""
    where = f" in #{m['channel_name']}" if m.get("channel_name") else ""
    failed = m["status"] == "send_failed"
    return {"key": f"mention:{m['id']}", "kind": "mention", "title": f"{m['from']} tagged you{where}",
            "href": m.get("permalink") or "", "at": m["found_at"], "age": now - m["found_at"], "urgent": True,
            "attention": "failed" if failed else "reply", "question": m["asked"],
            "recommend": (f"Send failed: {m.get('send_error', '')}. " if failed else "") + (m.get("why") or ""),
            "mention": {k: m.get(k, "") for k in ("id", "draft_reply", "neo_task", "evidence", "status", "sent_text")}}


def status_of(it: dict) -> Optional[dict]:
    """The latest stop's Status line: parsed from an agent's last reply, or recorded on a run's meta when its turn
    ended (runner.py). Old runs and agents not started with the brief have none."""
    if it["kind"] == "agent":
        return conclusion.status_of(it.get("last_text") or "")
    return it.get("conclusion") or None


def needs_of(it: dict, card: Optional[dict], seen: Callable[[str], float]) -> tuple[str, bool, dict]:
    """(needs, opened, card in effect) for one agent or run: what it needs from the owner right now.

    needs: "you" (approve, reply, stuck, failed, or an open question on its card), "others" (waits on a
    reviewer, vendor or owner), "working", "unfinished" (stopped partly done / failed / unclear and asked
    nothing, or an agent idle for NO_CONCLUSION_SECONDS with no card for its last stop) or "none". The home
    screen sections and the session-row tags both come from this.
    `it` needs key, kind, at, attention and, for runs, source; for agents, prompts and now (to tell how long
    it has been idle)."""
    card = card or {}
    # Cards are written when an agent stops; once it is working again (the owner answered, it resumed), the
    # card is history: the row goes by the live state until the next card. A finished item keeps its
    # card until the new one lands, so it does not flicker out of Decide.
    if card and it["attention"] == "working" and card.get("source_mtime", 0) < (it["at"] or 0) - CARD_SLACK_SECONDS:
        card = {}
    kind = card.get("ask_kind") or "decide"  # cards made before ask_kind existed
    # An open ask stays until the owner answers it (the agent resumes, so the card is dropped above), a
    # newer card replaces it, or the owner closes it. Opening the agent or run is not an answer: a
    # question read on a phone and left for later must not turn into a quiet "Idle" row.
    not_closed = seen(waiting_key(it["key"])) < card.get("source_mtime", 0)
    opened = bool(card) and seen(it["key"]) >= card.get("source_mtime", 0)
    # The card describes the latest stop. If the agent ran again after it (the owner answered, then it stopped
    # again) the old ask was answered; it must not come back as "opened, not answered" while the next card is
    # still being written, or forever if that card never lands.
    card_current = card.get("source_mtime", 0) >= (it["at"] or 0) - CARD_SLACK_SECONDS
    status = status_of(it)
    # An unread ask from an older stop stays, unless the latest stop's own Status line says how it ended (the
    # owner may have answered in the terminal, which never marks the card seen).
    open_ask = bool(card.get("needs_you")) and not_closed and (card_current or (not opened and not status))
    if it["attention"] in DECIDE_ATTENTION:
        return "you", False, card
    if open_ask and kind == "decide":
        return "you", opened, card
    if open_ask and kind == "others":
        return "others", False, card
    if it["attention"] == "working":
        return "working", False, card
    by_status = STATUS_NEEDS.get((status or {}).get("state"))
    if by_status and it["at"] and seen(waiting_key(it["key"])) < it["at"]:
        # The agent said how its latest stop ended, so the row doesn't wait for the card writer (late, or wrong)
        # or the NO_CONCLUSION_SECONDS fallback. This includes Slack runs: a Slack answer that ends needs-you or
        # failed still needs the owner. Done closes it until the next stop.
        return by_status, seen(it["key"]) >= it["at"], card if card_current else {}
    if (card.get("result") in UNFINISHED_RESULTS and not_closed and card_current
            and not (it["kind"] == "run" and it.get("source") == "slack")):
        # Stopped without finishing and asked nothing: someone has to continue, redirect or close it. A Slack
        # run already delivered its answer in the thread, so it is not left hanging here.
        return "unfinished", opened, card
    if (it["kind"] == "agent" and it.get("prompts") and it["at"] and not card_current and not status
            and it.get("now", 0) - it["at"] >= NO_CONCLUSION_SECONDS and seen(waiting_key(it["key"])) < it["at"]):
        # Idle with nothing that says how its last stop ended: someone has to continue or close it. Done closes
        # it until the agent runs again. The stale card is not this stop's outcome, so it is dropped.
        return "unfinished", seen(it["key"]) >= it["at"], {}
    if it["kind"] == "agent" and not card_current:
        card = {}  # an older stop's outcome; don't show it as this one's (e.g. after Done on the row above)
    return "none", opened, card


def annotate(overview: dict, cards_by_key: dict, seen: Callable[[str], float]) -> dict:
    """The agent board's rows with the same needs / result the home screen uses, for their tags."""
    _seen = lambda key: seen(key) or 0.0  # noqa: E731
    now = time.time()

    def tag(row: dict, it: dict) -> dict:
        needs, opened, card = needs_of(it, cards_by_key.get(it["key"]), _seen)
        return {**row, "needs": needs, "opened": opened, "result": card.get("result")}
    return {**overview,
            "agents": [tag(a, {"key": fleet._seen_key(a), "kind": "agent", "at": a.get("last_active") or 0,
                               "attention": a.get("attention"), "prompts": a.get("prompts"), "now": now,
                               "last_text": a.get("last_text") or ""})
                       for a in overview.get("agents", [])],
            "tasks": [tag(t, {"key": f"run:{t['id']}", "kind": "run", "at": _run_time(t), "source": t.get("source"),
                              "conclusion": t.get("conclusion"),
                              "attention": t.get("attention") or ("working" if t.get("status") == "running" else None)})
                      for t in overview.get("tasks", [])]}


def build(overview: dict, loops_rows: list[dict], cards_by_key: dict, since: float, now: float,
          seen: Optional[Callable[[str], float]] = None, mentions: Optional[list] = None,
          traces: Optional[dict] = None) -> dict:
    _seen = seen or (lambda key: 0.0)
    seen = lambda key: _seen(key) or 0.0  # noqa: E731 - a missing key is never seen
    traces = traces or {}
    items = []  # every agent, run and loop once: {key, kind, title, href, at, attention, card, ...}
    for row in overview.get("agents", []):
        items.append({"key": fleet._seen_key(row), "kind": "agent", "title": row.get("title") or row.get("name"),
                      "href": _agent_href(row), "at": row.get("last_active") or 0, "attention": row.get("attention"),
                      "reason": row.get("reason", ""), "session": row["session"], "pane_id": row["pane_id"],
                      "repo": row.get("repo", ""), "last_text": row.get("last_text") or "", "prompts": row.get("prompts"),
                      "now": now})
    for t in overview.get("tasks", []):
        items.append({"key": f"run:{t['id']}", "kind": "run", "title": t.get("title") or t["id"],
                      "href": f"#/run/{t['id']}", "at": _run_time(t), "status": t.get("status"),
                      "attention": t.get("attention") or ("working" if t.get("status") == "running" else None),
                      "reason": t.get("reason", ""), "run_id": t["id"], "source": t.get("source"),
                      "conclusion": t.get("conclusion")})
    for it in items:
        it["card"] = cards_by_key.get(it["key"])
        it["needs"], it["opened"], it["live_card"] = needs_of(it, it["card"], seen)

    decide, regressed, moved, in_flight, others, monitoring, unfinished = [], [], [], [], [], [], []
    quiet = 0
    for it in items:
        card = it.pop("live_card")
        needs = it["needs"]
        # The agent's own one-line ask from its Status line; an approval prompt or a stuck agent says it better.
        said = "" if it["attention"] in ("approve", "stuck") else (status_of(it) or {}).get("line", "")
        if needs == "you":
            decide.append({**it, "question": card.get("needs_you") or said or it["reason"], "recommend": card.get("recommend", ""),
                           "do_it": card.get("do_it", ""), "topic": card.get("topic", ""), "result": card.get("result"),
                           "age": now - it["at"] if it["at"] else 0, "urgent": it["attention"] in DECIDE_ATTENTION})
        elif needs == "others":
            others.append({**it, "line": card.get("needs_you") or said or it["reason"], "recommend": card.get("recommend", ""), "topic": card.get("topic", "")})
        elif needs == "working":
            in_flight.append({**it, "line": _first_line(it.get("last_text")) or "Working"})
        elif needs == "unfinished":
            # A Status line that said failed put it here; the card's own result could still say "worked".
            unfinished.append({**it, "result": "failed" if (status_of(it) or {}).get("state") == "failed" else card.get("result"),
                               "line": card.get("did") or said or _first_line(it.get("last_text")),
                               "risk": card.get("risk", ""), "next": card.get("next", ""),
                               "age": now - it["at"] if it["at"] else 0})
        else:
            changed = max(it["at"], card.get("made_at", 0))
            if changed > since:
                line = f"{RESULT_WORDS.get(card['result'], card['result'])}: {card['did']}" if card else "Finished — summary pending, open it"
                moved.append({**it, "line": line, "at": changed, "changes": 1, "result": card.get("result")})
            elif it["kind"] == "agent":
                # An open session stays visible until the owner closes it, even if it stopped without a
                # conclusion; quiet would hide it.
                # The row's tags say Idle / Partly done / Needs you (index.html stateTags); the line is what it did.
                did = card.get("did") or _first_line(it.get("last_text"))
                in_flight.append({**it, "idle": True, "result": card.get("result"), "line": did or "No result yet"})
            else:
                quiet += 1

    for loop in _merge_repeats(loops_rows):
        if loop.get("kind") == "pr" and STAGES.index(loop["stage"]) < MERGED and not loop.get("regressed"):
            continue  # an open PR lives in the PRs tab; home picks it up once merged
        key = f"loop:{loop['id']}"
        links = loop.get("links") or {}
        trace = _loop_trace(loop, traces)
        base = {"key": key, "kind": "loop", "title": loop["title"], "href": trace or _loop_href(loop), "loop": loop,
                "trace_href": trace, "slack_href": links.get("source") or None, "pr_href": (links.get("prs") or [None])[0],
                "stage_index": STAGES.index(loop["stage"]), "stage_label": STAGE_LABELS[loop["stage"]],
                "loop_ids": loop.get("loop_ids") or [loop["id"]]}
        fired = f" · fired {loop['repeats']} times" if loop.get("repeats", 1) > 1 else ""
        recent = [h for h in loop.get("history", []) if h["at"] > since]
        if loop["status"] == "open" and loop.get("regressed"):
            last = next((h for h in reversed(loop.get("history", [])) if h.get("regressed")), loop["history"][-1] if loop.get("history") else {})
            regressed.append({**base, "line": (last.get("note") or "Came back after the fix") + fired, "evidence": last.get("evidence", ""),
                              "at": last.get("at", loop["stage_at"]), "regressed_at": last.get("at", loop["stage_at"])})
        elif loop["stage"] in MONITOR_STAGES and loop["status"] == "open":
            if now - loop["stage_at"] <= MONITOR_DAYS * 86400:
                monitoring.append({**base, "line": monitor_line(loop, now) + fired, "at": loop["stage_at"],
                                   "result": "worked" if loop["stage"] == "verified" else "moving"})
            else:
                quiet += 1
        elif loop.get("stalled") and not (trace or base["pr_href"]):
            # Stalled but nothing to open except the Slack thread: not a decision, so it waits in flight.
            in_flight.append({**base, "line": loop["stalled"] + fired, "at": loop["stage_at"]})
        elif loop.get("stalled"):
            decide.append({**base, "question": loop["stalled"] + fired, "recommend": "", "age": now - loop["stage_at"], "at": loop["stage_at"],
                           "urgent": False, "stalled": True})
        elif recent:
            last = recent[-1]
            moved.append({**base, "line": f"{STAGE_LABELS[last['stage']]}" + (f": {last['note']}" if last.get("note") and last["note"] != "opened" else "") + fired,
                          "at": last["at"], "changes": len(recent), "result": "worked" if last["stage"] == "verified" else "moving"})
        elif loop["status"] == "open" and loop["stage"] != "verified":
            in_flight.append({**base, "line": base["stage_label"] + fired, "at": loop["stage_at"]})
        else:
            quiet += 1

    decide.extend(mention_row(m, now) for m in mentions or [])
    # An agent or person asking you comes before an alert that stalled (those pile up over days and would
    # bury a fresh question); then waiting on someone it is, then the ones neo can do on a tap, then the longest waiting.
    decide.sort(key=lambda d: (bool(d.get("stalled")), not d.get("urgent"), not d.get("do_it"), -d["age"]))
    decide = _group_topics(decide)
    unfinished.sort(key=lambda u: -(u.get("at") or 0))
    others = _group_topics(sorted(others, key=lambda o: -(o.get("at") or 0)))
    moved.sort(key=lambda m: -m["at"])
    in_flight.sort(key=lambda m: -(m.get("at") or 0))
    monitoring.sort(key=lambda m: -(m.get("at") or 0))
    return {
        "since": since, "now": now,
        "counts": {"decide": len(decide), "asks": sum(1 for d in decide if not d.get("stalled")),
                   "stalled": sum(1 for d in decide if d.get("stalled")), "unfinished": len(unfinished),
                   "regressed": len(regressed), "moved": len(moved),
                   "in_flight": len(in_flight), "others": len(others), "monitoring": len(monitoring), "quiet": quiet},
        "decide": decide, "unfinished": unfinished, "regressed": regressed, "moved": moved, "in_flight": in_flight,
        "others": others, "monitoring": monitoring, "quiet": quiet,
    }


def home(since: Optional[float] = None) -> dict:
    """since: the dashboard pins the start of a visit, so "moved" doesn't empty while you read it."""
    from . import cards, loops, mentions

    from .runner import list_runs

    overview = fleet.overview()
    since = fleet.seen.get("home") if since is None else since
    rows, runs = loops.listing(), list_runs(500)
    return build(overview, rows, cards.STORE.read(), since=since, now=time.time(), seen=fleet.seen.get,
                 mentions=mentions.open_items(), traces={**thread_runs(runs=runs), **loop_traces(rows, overview, runs)})


def waiting_key(key: str) -> str:
    return f"waiting:{key}"


def close_waiting(key: str) -> dict:
    """The owner is done with this card's ask (a decision, a wait on others, an unfinished result); a newer
    card from the same agent or run brings it back."""
    if not key or len(key) > 300:
        raise ValueError("key required")
    fleet.seen.mark({waiting_key(key): time.time()})
    return {"ok": True}


def mark_seen() -> None:
    fleet.seen.mark({"home": time.time()})
