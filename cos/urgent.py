"""What is worth interrupting the owner for in Slack, and the evening digest.

Pinged at once: a loop that regressed. Pinged after DECIDE_PING_MINUTES: an agent or run waiting
on them. Everything else (stalls, finished work) waits for the home screen and the digest, except
the nudge: decisions from cards that sat unanswered for NUDGE_AFTER_HOURS are listed together in
one message, at most every NUDGE_EVERY_HOURS during the day, so nothing waits on the owner remembering.
Each item pings once per reason; a new question from the same agent (a later `at`) pings again.
The ledger is COS_HOME/urgent.json, so a restart does not repeat pings.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from .store import JsonStore

STORE = JsonStore("urgent.json")
DECIDE_PING_MINUTES = 30
KEEP_DAYS = 7
DIGEST_MAX = 3500
SECTION_ROWS = 6
NUDGE_AFTER_HOURS = 2
NUDGE_EVERY_HOURS = 3
NUDGE_ROWS = 3
NUDGE_HOURS = (9, 21)  # local time; no nudges at night
RESULT_EMOJI = {"worked": ":white_check_mark:", "answered": ":white_check_mark:", "partial": ":large_yellow_circle:",
                "unclear": ":large_yellow_circle:", "failed": ":x:", "moving": ":arrow_right:"}


def _ledger_key(item: dict, reason: str) -> str:
    # A waiting item pings again when it asks something new (a later `at`); a regression pings again
    # only when it regresses again (a later regressed_at), not every time the checker re-reports it.
    at = item.get("regressed_at") if reason == "regressed" else item.get("at")
    return f"{item['key']}|{reason}|{int(at or 0)}"


def _link(href: str, base_url: Optional[str], label: str) -> str:
    if href.startswith("#") and base_url:
        href = base_url.rstrip("/") + "/" + href
    return f"<{href}|{label}>" if href.startswith("http") else label


def due(payload: dict, now: float, base_url: Optional[str] = None, local_hour: Optional[int] = None) -> list[dict]:
    sent = STORE.read()
    out = []
    for r in payload.get("regressed", []):
        out.append({"ledger": _ledger_key(r, "regressed"), "reason": "regressed",
                    "text": f":rotating_light: *Regressed:* {_link(r.get('href', ''), base_url, r['title'])}: {r.get('line', '')}"
                            + (f"\n_Evidence:_ {r['evidence']}" if r.get("evidence") else "")})
    for d in payload.get("decide", []):
        if d.get("kind") == "mention":  # someone is waiting on the owner: tell them now, with neo's draft
            hour = datetime.fromtimestamp(now).hour if local_hour is None else local_hour
            if not NUDGE_HOURS[0] <= hour < NUDGE_HOURS[1]:
                continue  # not at night: it pings in the morning, and is on the home screen meanwhile
            m = d["mention"]
            out.append({"ledger": _ledger_key(d, "mention" if d.get("attention") != "failed" else "send_failed"),
                        "reason": "mention",
                        "text": f":speech_balloon: *{_link(d.get('href', ''), base_url, d['title'])}:* {d.get('question', '')}"
                                + (f"\n_Draft reply:_ {m['draft_reply']}" if m.get("draft_reply") else "")
                                + (f"\n_neo can:_ {m['neo_task'][:300]}" if m.get("neo_task") else "")
                                + (f"\n_{d['recommend']}_" if d.get("attention") == "failed" else "")
                                + (f"\n<{base_url.rstrip('/')}/|Send it or hand it to neo on the dashboard>" if base_url else "")})
            continue
        if not d.get("urgent") or d.get("age", 0) < DECIDE_PING_MINUTES * 60:
            continue
        mins = int(d["age"] // 60)
        out.append({"ledger": _ledger_key(d, "waiting"), "reason": "waiting",
                    "text": f":raised_hand: *Waiting {mins} min:* {_link(d.get('href', ''), base_url, d['title'])}\n{d.get('question', '')}"
                            + (f"\n_Recommend:_ {d['recommend']}" if d.get("recommend") else "")})
    return [o for o in out if o["ledger"] not in sent]


def nudge(payload: dict, now: float, local_hour: int, base_url: Optional[str] = None) -> Optional[dict]:
    """One message listing card decisions waiting over NUDGE_AFTER_HOURS that were not in an earlier
    nudge, oldest first, each with neo's recommendation. None when there is nothing new or it is
    too soon or night. Send it, then mark_sent([it])."""
    if not NUDGE_HOURS[0] <= local_hour < NUDGE_HOURS[1]:
        return None
    sent = STORE.read()
    if now - sent.get("nudge|last", 0) < NUDGE_EVERY_HOURS * 3600:
        return None
    fresh = lambda d: (d.get("age", 0) >= NUDGE_AFTER_HOURS * 3600  # noqa: E731
                       and f"nudge|{d['key']}|{int(d.get('at') or 0)}" not in sent)
    # A question you opened and left is still a question: it stays in decide (see home.build) and is nudged.
    rows = [d for d in payload.get("decide", []) if not d.get("urgent") and d.get("kind") != "loop" and fresh(d)]
    # Agents that stopped partway and asked nothing: they sit idle until someone continues or closes them.
    stopped = [u for u in payload.get("unfinished", []) if fresh(u)]
    if not rows and not stopped:
        return None
    rows.sort(key=lambda d: -d["age"])
    stopped.sort(key=lambda u: -u["age"])
    lines = []
    if rows:
        lines.append(f":hourglass: *{len(rows)} decision{'s' if len(rows) > 1 else ''} waiting on you* (tap Do it on the dashboard to accept)")
        for d in rows[:NUDGE_ROWS]:
            lines.append(f"• {_link(d.get('href', ''), base_url, d['title'][:80])} ({int(d['age'] // 3600)}h"
                         + (", you opened it, not answered" if d.get("opened") else "") + f"): {d.get('question', '')}"
                         + (f"\n   _Recommend:_ {d['recommend']}" if d.get("recommend") else ""))
        if len(rows) > NUDGE_ROWS:
            lines.append(f"• +{len(rows) - NUDGE_ROWS} more on the dashboard")
    if stopped:
        lines.append(f":double_vertical_bar: *{len(stopped)} agent{'s' if len(stopped) > 1 else ''} stopped before finishing* "
                     "(continue or close them on the dashboard)")
        for u in stopped[:NUDGE_ROWS]:
            lines.append(f"• {RESULT_EMOJI.get(u.get('result'), '•')} {_link(u.get('href', ''), base_url, u['title'][:80])} "
                         f"(idle {int(u['age'] // 3600)}h): {u.get('line', '')[:200]}")
        if len(stopped) > NUDGE_ROWS:
            lines.append(f"• +{len(stopped) - NUDGE_ROWS} more on the dashboard")
    ledger = ["nudge|last"] + [f"nudge|{d['key']}|{int(d.get('at') or 0)}" for d in rows + stopped]
    return {"ledger": ledger, "reason": "nudge", "text": "\n".join(lines)}


def mark_sent(items: list[dict], now: Optional[float] = None) -> None:
    now = time.time() if now is None else now

    def write(data: dict) -> None:
        for item in items:
            for key in item["ledger"] if isinstance(item["ledger"], list) else [item["ledger"]]:
                data[key] = now
        for key in [k for k, at in data.items() if now - at > KEEP_DAYS * 86400]:
            del data[key]
    STORE.update(write)


def digest_text(payload: dict, base_url: Optional[str]) -> str:
    c = payload["counts"]
    stopped = f"{c['unfinished']} stopped unfinished · " if c.get("unfinished") else ""
    head = (f"*Today:* {c['decide']} to decide · {stopped}{c['regressed']} regressed · {c['moved']} moved · "
            f"{c['in_flight']} in flight · {c['quiet']} quiet")
    if not (c["decide"] or c.get("unfinished") or c["regressed"] or c["moved"] or c["in_flight"]):
        return head + "\nNothing needs you."
    lines = [head]

    def section(title: str, rows: list[dict], fmt, total: int) -> None:
        if not rows:
            return
        lines.append(f"\n*{title}*")
        lines.extend(f"• {fmt(r)}" for r in rows[:SECTION_ROWS])
        if total > SECTION_ROWS:
            lines.append(f"• +{total - SECTION_ROWS} more on the dashboard")

    title = lambda r: _link(r.get("href", ""), base_url, r["title"])  # noqa: E731
    section("Regressed", payload["regressed"], lambda r: f"{title(r)}: {r.get('line', '')}", c["regressed"])
    section("Decide", payload["decide"], lambda r: f"{title(r)}: {r.get('question', '')}", c["decide"])
    section("Stopped before finishing", payload.get("unfinished", []),
            lambda r: f"{RESULT_EMOJI.get(r.get('result'), '•')} {title(r)}: {r.get('line', '')}", c.get("unfinished", 0))
    section("Moved", payload["moved"], lambda r: f"{RESULT_EMOJI.get(r.get('result'), '•')} {title(r)}: {r.get('line', '')}", c["moved"])
    section("Waiting on others", payload.get("others", []), lambda r: f"{title(r)}: {r.get('line', '')}", c.get("others", 0))
    section("In flight", payload["in_flight"], lambda r: f"{title(r)}: {r.get('line', '')}", c["in_flight"])
    text = "\n".join(lines)
    return text if len(text) <= DIGEST_MAX else text[: DIGEST_MAX - 40].rsplit("\n", 1)[0] + "\n• …more on the dashboard"
