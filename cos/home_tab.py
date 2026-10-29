"""Each person's page in neo's Slack Home tab: their sign-ins, today's tasks, their brief and digest,
and their watches, with buttons. view() only lays it out; app.py gathers what it shows and handles the
buttons (action ids below)."""

from __future__ import annotations

from typing import Optional

CONNECT, BRIEF, DIGEST, WATCH_REMOVE = "home_connect", "home_brief", "home_digest", "home_watch_remove"
NOTE_APPROVE, NOTE_REJECT = "home_note_approve", "home_note_reject"
TOOL_NAMES = {"claude": "Claude", "codex": "Codex", "gh": "GitHub"}
BRIEF_TIMES = [f"{h:02d}:{m:02d}" for h in range(6, 12) for m in (0, 30)]
DIGEST_TIMES = [f"{h:02d}:{m:02d}" for h in range(16, 22) for m in (0, 30)]
STATUS_MARK = {"done": ":white_check_mark:", "failed": ":x:", "stopped": ":black_square_for_stop:"}


def _text(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _time_select(action: str, times: list[str], current: Optional[str]) -> dict:
    options = [{"text": {"type": "plain_text", "text": "Off"}, "value": "off"}] + [
        {"text": {"type": "plain_text", "text": t}, "value": t} for t in times]
    select = {"type": "static_select", "action_id": action, "options": options,
              "placeholder": {"type": "plain_text", "text": "Pick a time"}}
    chosen = next((o for o in options if o["value"] == (current or "off")), None)
    if chosen:
        select["initial_option"] = chosen
    return select


def view(name: str, logins: dict, brief: Optional[str], digest: Optional[str], runs: list[dict],
         watches: list[dict], lender: Optional[str] = None, dashboard_url: Optional[str] = None,
         pending: Optional[list[dict]] = None, policy: str = "", exchanges: Optional[list[dict]] = None,
         cos: str = "", work: Optional[dict] = None) -> dict:
    """logins: tool -> "own" | "lent" | "none" | "unknown". brief/digest: "HH:MM" or None (off).
    runs: today's {title, status, url}. watches: {id, name, every, enabled}. lender: whose logins are lent."""
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Hi {name}"[:150]}},
              _text(f"I'm {('*' + cos + '*') if cos else 'neo'}, your chief of staff. Ask me anything in a DM or with "
                    "`@neo` in a channel: a task (\"fix the flaky test in backie\") or something about me "
                    "(\"send my brief at 8\"). To rename me, say \"call my neo Friday\".")]

    blocks += [{"type": "divider"}, _text("*Your sign-ins*")]
    for tool, label in TOOL_NAMES.items():
        state = logins.get(tool, "unknown")
        shown = {"own": ":white_check_mark: your own account",
                 "lent": f":large_blue_circle: using {lender or 'the owner'}'s for now",
                 "none": ":white_circle: not connected",
                 "unknown": ":hourglass: checking…"}[state]
        row = _text(f"*{label}*  {shown}")
        if state != "unknown":
            row["accessory"] = {"type": "button", "action_id": f"{CONNECT}_{tool}", "value": tool,
                                "text": {"type": "plain_text", "text": "Reconnect" if state == "own" else "Connect"}}
        blocks.append(row)

    blocks += [{"type": "divider"}, _text("*Morning brief*: your calendar, mail and Slack, by DM on work days. "
                                          "Needs your own Claude.")]
    blocks.append({"type": "actions", "elements": [_time_select(BRIEF, BRIEF_TIMES, brief)]})
    blocks.append(_text("*Evening digest*: what your tasks did today, by DM."))
    blocks.append({"type": "actions", "elements": [_time_select(DIGEST, DIGEST_TIMES, digest)]})

    blocks += [{"type": "divider"}, _text("*Today*")]
    if runs:
        lines = []
        for run in runs[:15]:
            link = f" <{run['url']}|thread>" if run.get("url") else ""
            lines.append(f"{STATUS_MARK.get(run['status'], ':hourglass:')} {run['title'][:120]}{link}")
        blocks.append(_text("\n".join(lines)))
    else:
        blocks.append(_text("_Nothing yet today._"))

    blocks += [{"type": "divider"}, _text("*Your watches*: checks I run on a schedule; findings come by DM.")]
    for watch in watches[:20]:
        row = _text(f"`{watch['id']}` {watch['name']}, every {watch['every']} min{'' if watch.get('enabled', True) else ' (off)'}")
        row["accessory"] = {"type": "button", "action_id": f"{WATCH_REMOVE}_{watch['id']}", "value": watch["id"],
                            "text": {"type": "plain_text", "text": "Remove"}, "style": "danger"}
        blocks.append(row)
    blocks.append(_text("_Add one by telling me, e.g. \"watch CI failures on backie every hour\"._"))
    if policy:
        blocks += [{"type": "divider"}, _text("*Questions from other people's CoS*: yours answers, declines or asks you, "
                                              f"by this policy (change it by telling me):\n>{policy[:1500]}")]
        decided = {"answer": "answered", "deny": "declined", "ask_owner": "asked you", "sent": "you sent it",
                   "declined": "you declined"}
        for row in (exchanges or [])[:6]:
            arrow = f"<@{row['other']}> asked" if row["incoming"] else f"You asked <@{row['other']}>"
            blocks.append(_text(f"{arrow}: _{row['question'][:150]}_ ({decided.get(row['decision'], row['decision'])})"))
    asked, doing = (work or {}).get("asked") or [], (work or {}).get("doing") or []
    if asked or doing:
        blocks += [{"type": "divider"}, _text("*Work between neos*")]
        for row in asked[:5]:
            blocks.append(_text(f"You asked <@{row['target']}>'s neo: _{row['request'][:150]}_ ({row.get('status')}, `{row['id']}`)"))
        for row in doing[:5]:
            blocks.append(_text(f"For <@{row['asker']}>'s neo: _{row['request'][:150]}_ ({row.get('status')}, `{row['id']}`)"))
    if pending:  # the owner's: shared notes from other people, reaching everyone's runs only once approved
        blocks += [{"type": "divider"}, _text(f"*Shared notes waiting for you* ({len(pending)}): these would reach "
                                              "everyone's agents, yours too.")]
        for item in pending[:10]:
            blocks.append(_text(f"<@{item['by']}> → `{item['target']}`\n{item['text'][:400]}"))
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "action_id": f"{NOTE_APPROVE}_{item['id']}", "value": item["id"], "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve"}},
                {"type": "button", "action_id": f"{NOTE_REJECT}_{item['id']}", "value": item["id"],
                 "text": {"type": "plain_text", "text": "Reject"}}]})
    if dashboard_url:
        blocks += [{"type": "divider"}, _text(f"<{dashboard_url}|Open the web dashboard> for full traces.")]
    return {"type": "home", "blocks": blocks[:100]}
