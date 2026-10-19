"""Mentions: when someone tags the owner in Slack, neo works out what they should say or what neo can do,
and puts it on the home screen as a decision. Nothing is sent without their tap.

A check (kind "mentions", every EVERY_MINUTES from 08:00 to 22:00 local) is a read-only headless
Claude run. It searches Slack as the owner (the claude.ai Slack connector) for messages tagging them
since the last check, reads each thread, looks up what it needs (code, Sentry, gh, logs; read
only), and answers in MENTIONS_SCHEMA: who asked what, whether it needs the owner, a reply drafted in
their voice, and a task neo could take on.

"Send as you" is a second run whose only allowed tool is the Slack connector's send_message. The
send counts only when that run's tool call carried exactly the approved text to the right thread;
otherwise the item says it failed and shows why.

State: COS_HOME/mentions.json = {"state": {...}, "items": {id: item}}.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from . import watches
from .config import owner_fields, owner_profile, workdir
from .runner import Run, list_runs
from .store import JsonStore

log = logging.getLogger("neo")
STORE = JsonStore("mentions.json")
EVERY_MINUTES = int(os.environ.get("NEO_MENTIONS_EVERY", "15"))
HOURS = (8, 22)  # local time, every day: people tag the owner at weekends too
FIRST_LOOK_HOURS = 24
KNOWN_SHOWN = 60
SLACK = "mcp__claude_ai_Slack__"
SLACK_READ = [SLACK + t for t in ("slack_search_public_and_private", "slack_search_public", "slack_read_thread",
                                  "slack_read_channel", "slack_search_users", "slack_read_user_profile",
                                  "slack_search_channels", "slack_list_user_channels")]
SLACK_WRITE = [SLACK + t for t in ("slack_send_message", "slack_send_message_draft", "slack_schedule_message",
                                   "slack_create_canvas", "slack_update_canvas", "slack_add_reaction")]
SEND_TOOL = SLACK + "slack_send_message"
STATUSES = ("new", "fyi", "sending", "sent", "send_failed", "working", "dismissed")
OPEN = ("new", "send_failed")

MENTIONS_SCHEMA = {
    "type": "object",
    "required": ["summary", "mentions"],
    "properties": {
        "summary": {"type": "string", "description": "One or two sentences: what you searched and found. Say if Slack could not be reached."},
        "mentions": {"type": "array", "items": {
            "type": "object",
            "required": ["channel_id", "ts", "thread_ts", "permalink", "from", "asked", "needs_owner", "why",
                         "draft_reply", "neo_task"],
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string", "description": "ts of the message that tags the owner."},
                "thread_ts": {"type": "string", "description": "ts of the thread root to reply in (the message's own ts if it is not in a thread)."},
                "permalink": {"type": "string"},
                "channel_name": {"type": "string"},
                "from": {"type": "string", "description": "Name of who tagged the owner."},
                "asked": {"type": "string", "description": "What they want from the owner, one line."},
                "needs_owner": {"type": "boolean", "description": "False for FYI, thanks, a broadcast, or when the owner already answered after it."},
                "why": {"type": "string", "description": "Why it needs the owner (or not), in a few words."},
                "draft_reply": {"type": "string", "description": "The reply the owner could post in the thread, in their voice. Empty if none is needed."},
                "neo_task": {"type": "string", "description": "If neo could do the work behind this (investigate, fix, open a PR, file an issue), "
                             "self-contained instructions for a coding agent with repo and links. Empty if not."},
                "evidence": {"type": "string", "description": "What you checked to write the draft (links, commands)."},
            },
        }},
    },
}


def _id(channel: str, ts: str) -> str:
    return hashlib.sha1(f"{channel}:{ts}".encode()).hexdigest()[:12]


def is_pr_notification(m: dict) -> bool:
    """GitHub app posts (review requests, PR updates in the gh-* channels). They are never asks."""
    return str(m.get("channel_name") or "").startswith("gh-") or str(m.get("from") or "").startswith("GitHub")


def bot_names() -> list[str]:
    """What neo's Slack app is called: "neo", plus the name Slack gave this app (NEO_BOT_NAME, set at
    startup from auth.test), which can be anything ("Nio")."""
    names = ["neo"]
    for n in re.split(r"[,\s]+", os.environ.get("NEO_BOT_NAME", "").strip().lower()):
        if n and n not in names:
            names.append(n)
    return names


def is_from_neo(m: dict) -> bool:
    """neo's own posts (briefs, reminders, approval pings, repeats of its findings) tag the owner but are
    not asks: an agent's question is already a card, and the ping goes stale once the agent is gone."""
    sender = str(m.get("from") or "").strip().lower()
    return any(sender == n or sender.startswith((f"{n} (", f"{n} bot")) for n in bot_names())


def owner_id() -> str:
    return os.environ.get("NEO_NOTIFY_USER", "")


def enabled() -> bool:
    return bool(owner_id()) and os.environ.get("NEO_MENTIONS", "1") != "0"


def _read() -> dict:
    data = STORE.read()
    data.setdefault("state", {})
    data.setdefault("items", {})
    return data


# ------------------------------------------------------------------ the check


def due(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if not enabled() or not HOURS[0] <= now.hour < HOURS[1] or running():
        return False
    last = _read()["state"].get("last_started_at")
    return not last or now - datetime.fromisoformat(last) >= timedelta(minutes=EVERY_MINUTES)


def running() -> Optional[dict]:
    for meta in list_runs(60):
        if meta.get("kind") == "mentions" and meta["status"] in ("queued", "running"):
            return meta
    return None


def _since(state: dict, now: datetime) -> datetime:
    last = state.get("last_ok_at")
    try:
        return datetime.fromisoformat(last) - timedelta(minutes=10) if last else now - timedelta(hours=FIRST_LOOK_HOURS)
    except ValueError:
        return now - timedelta(hours=FIRST_LOOK_HOURS)


def prompt(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    data = _read()
    since = _since(data["state"], now)
    known = sorted(data["items"].values(), key=lambda i: -i.get("found_at", 0))[:KNOWN_SHOWN]
    me = owner_id()
    o = owner_fields()
    profile = f", {owner_profile()}" if owner_profile() else ""
    # Extra rules for the check, one per line (a literal backslash-n also separates them in a .env file).
    rules = [r.strip() for r in re.split(r"\\n|\n", os.environ.get("NEO_MENTIONS_RULES", "")) if r.strip()]
    return "\n".join([
        f"You work for {o['owner']} (Slack user <@{me}>){profile}. Find the Slack messages that tag them and work out, "
        "for each, what they should reply and whether you (an engineering agent with their repos, Sentry, gh, gcloud and logs) "
        "could do the work behind it.",
        "",
        f"Now: {now.strftime('%Y-%m-%d %H:%M')} local time. Look at messages since {since.strftime('%Y-%m-%d %H:%M')}.",
        f"Search with slack_search_public_and_private, query `<@{me}> after:{(since - timedelta(days=1)).strftime('%Y-%m-%d')}`, "
        "sorted by time, and drop results older than the window. Read each hit's thread with slack_read_thread.",
        "",
        "Already handled (leave these out):" if known else "Nothing handled yet.",
        *[f"- {i['channel_id']} {i['ts']} ({i['from']}: {i['asked'][:80]})" for i in known],
        "",
        "Rules:",
        "- Read only. Never send, react, draft or schedule anything in Slack, and change nothing anywhere else.",
        "- Skip GitHub app posts and anything in gh-* channels (PR review requests, PR opened/merged/CI notices).",
        f"- Skip neo's own posts (the neo bot, named {' or '.join(n.capitalize() for n in bot_names())} in Slack: briefs, reminders, "
        "digests, agents waiting for approval or asking something, repeats of its findings).",
        f"- Skip messages {o['owner']} wrote themselves and bot posts that only list names. needs_owner is false when {o['owner']} (<@{me}>) "
        "already replied after the tag, or it is a thank-you, FYI or broadcast.",
        "- Before drafting, check the facts the reply depends on (the PR, the Sentry issue, the code, the deploy). "
        f"Never put an unverified claim in a draft; if you could not verify, the draft says what {o['owner']} will check.",
        f"- draft_reply is {o['owner']} speaking: first person, short, plain, Slack formatting, links where useful, no greeting "
        "fluff, never mention being an AI. If neo should take the work, the draft can say so (\"neo is on it, will "
        "post the PR here\").",
        "- Shell: one command per Bash call, no `cd`, and the Read tool for files.",
        "- Nothing new: return an empty list and say so in the summary.",
        *[f"- {r}" for r in rules],
    ])


def cli_args() -> tuple[list[str], list[str]]:
    every_turn, _ = watches.cli_args()
    allowed = every_turn[every_turn.index("--allowedTools") + 1].split(",") + SLACK_READ
    denied = every_turn[every_turn.index("--disallowedTools") + 1].split(",") + SLACK_WRITE
    return (["--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied)],
            ["--json-schema", json.dumps(MENTIONS_SCHEMA)])


_lock = threading.Lock()


def start(source: str = "schedule") -> dict:
    with _lock:
        current = running()
        if current:
            return current
        every_turn, first_turn = cli_args()
        run = Run.create("claude", prompt(), workdir(), "readonly", source=source,
                         extra={"kind": "mentions", "title": f"Mentions of {owner_fields()['owner']}", "cli_args": every_turn,
                                "first_turn_args": first_turn})
        STORE.update(lambda d: d.setdefault("state", {}).update(
            last_started_at=datetime.now().isoformat(timespec="seconds"), last_run_id=run.id))

    def work():
        try:
            run.execute()
        except Exception as exc:  # noqa: BLE001 - recorded in the run
            run.append_event({"kind": "error", "title": "Mentions check failed", "body": repr(exc), "data": {}}, 1)
            run.update_meta(status="failed", pid=None)
        try:
            finish(run)
        except Exception:  # noqa: BLE001
            log.exception("mentions: could not record check %s", run.id)

    threading.Thread(target=work, daemon=True, name="mentions").start()
    return run.meta()


def parse_result(run: Run) -> Optional[dict]:
    answer = None
    try:
        with (run.dir / "raw.jsonl").open() as fh:
            for line in fh:
                if '"type"' not in line or '"result"' not in line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict) and raw.get("type") == "result":
                    answer = raw.get("structured_output") or answer
    except OSError:
        return None
    return answer if isinstance(answer, dict) and isinstance(answer.get("mentions"), list) else None


def merge(answer: dict, run_id: str, now: Optional[float] = None) -> list[dict]:
    """Add the check's mentions; known ones are left as they are. Returns the new ones needing the owner."""
    now = time.time() if now is None else now
    new = []

    def write(data: dict) -> None:
        items = data.setdefault("items", {})
        for m in answer.get("mentions", []):
            if not isinstance(m, dict) or not m.get("channel_id") or not m.get("ts") or is_pr_notification(m) or is_from_neo(m):
                continue
            mid = _id(str(m["channel_id"]), str(m["ts"]))
            if mid in items:
                continue
            item = {k: str(m.get(k) or "")[:4000] for k in ("channel_id", "ts", "thread_ts", "permalink", "channel_name",
                                                             "from", "asked", "why", "draft_reply", "neo_task", "evidence")}
            item["thread_ts"] = item["thread_ts"] or item["ts"]
            if not item["permalink"].startswith("https://"):
                item["permalink"] = ""
            needs = bool(m.get("needs_owner")) and bool(item["draft_reply"] or item["neo_task"])
            item.update(id=mid, status="new" if needs else "fyi", found_at=now, run_id=run_id, history=[])
            items[mid] = item
            if needs:
                new.append(item)
        if len(items) > 1000:
            for old in sorted(items, key=lambda k: items[k]["found_at"])[: len(items) - 800]:
                del items[old]
    STORE.update(write)
    return new


def finish(run: Run) -> list[dict]:
    meta = run.meta()
    if meta["status"] != "done":
        return []
    answer = parse_result(run)
    if answer is None:
        run.update_meta(watch_summary="The mentions check did not answer in the expected shape; open the run.")
        return []
    new = merge(answer, run.id)
    try:  # the next check looks from when this one started
        started = datetime.fromisoformat(meta["created_at"]).astimezone().replace(tzinfo=None)
    except ValueError:
        started = datetime.now()
    STORE.update(lambda d: d.setdefault("state", {}).update(last_ok_at=started.isoformat(timespec="seconds")))
    run.update_meta(watch_summary=str(answer.get("summary") or "")[:2000], watch_found=len(answer["mentions"]),
                    watch_new=len(new))
    return new


# ------------------------------------------------------------------ the owner's decisions


def get(mid: str) -> Optional[dict]:
    return _read()["items"].get(mid)


def open_items() -> list[dict]:
    # is_from_neo again: neo's own posts picked up before its Slack name was known stay out of the decisions.
    return sorted((i for i in _read()["items"].values() if i["status"] in OPEN and not is_from_neo(i)),
                  key=lambda i: i["found_at"])


def _set(mid: str, **fields) -> dict:
    def write(data: dict) -> dict:
        item = data.setdefault("items", {}).get(mid)
        if not item:
            raise LookupError("no such mention")
        note = fields.pop("note", "")
        item.update(fields)
        item.setdefault("history", []).append({"at": time.time(), "status": item["status"], "note": note[:300]})
        return item
    return STORE.update(write)


def dismiss(mid: str) -> dict:
    return _set(mid, status="dismissed")


SEND_PROMPT = """Post one Slack message for {owner}, then stop. Use the {tool} tool exactly once with:
channel_id: {channel}
thread_ts: {thread}
message: the text between the markers below, character for character. Do not fix, shorten, translate or add anything.
<<<MESSAGE
{text}
MESSAGE>>>
If the tool fails, say why and do not retry with different text."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def sent_ok(run: Run, item: dict, text: str) -> tuple[bool, str]:
    """Did the send run post exactly the approved text to the right thread?"""
    try:
        lines = (run.dir / "events.jsonl").read_text().splitlines()
    except OSError:
        return False, "no record of the send"
    calls, ok_results = [], 0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "tool_call" and e.get("title") == SEND_TOOL:
            try:
                calls.append(json.loads(e.get("body") or "{}"))
            except ValueError:
                calls.append({})
        elif e.get("kind") == "tool_result" and calls and not (e.get("data") or {}).get("is_error"):
            ok_results += 1
    if len(calls) != 1:
        return False, f"expected one send, saw {len(calls)}"
    c = calls[0]
    if c.get("channel_id") != item["channel_id"] or str(c.get("thread_ts") or "") != item["thread_ts"]:
        return False, "sent to a different place than approved"
    if _norm(c.get("message", "")) != _norm(text):
        return False, "the text sent differs from the approved text"
    if not ok_results:
        return False, "Slack did not confirm the send"
    return True, "sent"


def send(mid: str, text: str) -> dict:
    """Post text in the mention's thread as the owner, in the background. Returns the send run."""
    text = text.strip()
    if not text:
        raise ValueError("nothing to send")
    item = get(mid)
    if not item:
        raise LookupError("no such mention")
    if item["status"] in ("sending", "sent"):
        raise ValueError(f"already {item['status']}")
    run = Run.create("claude", SEND_PROMPT.format(**owner_fields(), tool=SEND_TOOL, channel=item["channel_id"], thread=item["thread_ts"], text=text),
                     workdir(), "readonly", source="dashboard",
                     extra={"kind": "mention-send", "title": f"Reply to {item['from']} as {owner_fields()['owner']}",
                            "cli_args": ["--allowedTools", f"{SEND_TOOL},ToolSearch", "--disallowedTools",
                                         ",".join(t for t in SLACK_WRITE if t != SEND_TOOL) + ",Bash,Edit,Write"]})
    _set(mid, status="sending", sent_text=text, send_run=run.id, note=f"{owner_fields()['owner']} approved")

    def work():
        try:
            run.execute()
            ok, why = sent_ok(run, item, text)
        except Exception as exc:  # noqa: BLE001
            ok, why = False, repr(exc)[:200]
        _set(mid, status="sent" if ok else "send_failed", send_error="" if ok else why, note=why)
        log.info("mention %s: %s (%s)", mid, "sent" if ok else "send failed", why)

    threading.Thread(target=work, daemon=True, name=f"mention-send-{mid}").start()
    return run.meta()


def hand_to_neo(mid: str) -> dict:
    """Start a full neo run on the mention's neo_task; it should post its result in the thread itself."""
    item = get(mid)
    if not item or not item.get("neo_task"):
        raise LookupError("nothing for neo to do here")
    o = owner_fields()
    task = (f"{item['from']} tagged {o['owner']} in Slack ({item['permalink'] or item['channel_id']}): {item['asked']}\n"
            f"{o['Owner']} asked you to take it on:\n{item['neo_task']}\n"
            "When done, reply in that Slack thread with what you did and the links (channel "
            f"{item['channel_id']}, thread_ts {item['thread_ts']}). Ask {o['owner']} before anything irreversible "
            "(a prod deploy, an admin merge, deleting data).")
    run = Run.create("claude", task, workdir(), "full", source="dashboard",
                     extra={"kind": "mention-task", "title": f"For {item['from']}: {item['asked'][:80]}"})
    _set(mid, status="working", task_run=run.id, note="handed to neo")
    return run
