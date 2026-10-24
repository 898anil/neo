"""Decision cards: a five-line summary of what an agent or run did, so the owner can decide without
reading the transcript. One Claude call per finished turn, answer shaped by --json-schema.

Nothing important may be lost, so failures are also found by code (event_flags: failed tool
calls and the agent's own words about errors, skips and unverified claims) and always end up
on the card, whatever the model wrote.

Cards live in COS_HOME/cards.json, keyed like the seen store: "run:<id>" or "agent:<session id>".
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import fleet
from .config import owner_fields, split_thread_context
from .runner import Run, claude_available, cli_path
from .store import JsonStore

log = logging.getLogger(__name__)
STORE = JsonStore("cards.json")
MODEL = os.environ.get("NEO_CARD_MODEL", "claude-sonnet-5-5")
MATERIAL_CHARS = 40_000  # the tail of the log; the end is where the outcome is
RESULTS = ["worked", "partial", "failed", "unclear", "answered"]
# decide: only the owner can unblock it. others: someone else must act (a reviewer, a vendor, an owner);
# the owner may want to nudge them but there is nothing for them to decide. none: nothing waits on anyone.
ASK_KINDS = ["decide", "others", "none"]
CARD_SCHEMA = {
    "type": "object",
    "required": ["did", "result", "needs_you", "ask_kind", "recommend", "do_it", "topic", "risk", "next", "flags"],
    "properties": {
        "did": {"type": "string", "description": "What was done, one or two lines, concrete (files, PRs, commands)."},
        "result": {"type": "string", "enum": RESULTS},
        "needs_you": {"type": "string", "description": "The one decision or answer needed from you, as a question. Empty if none."},
        "ask_kind": {"type": "string", "enum": ASK_KINDS,
                     "description": "decide: only you can answer or unblock it. others: it waits on someone else "
                                    "(reviewer, vendor, owner), not on a choice by you. none: nothing waits."},
        "recommend": {"type": "string", "description": "What you recommend for that decision, with the reason in a few words. Empty if none."},
        "do_it": {"type": "string", "description": "If you accept the recommendation and the agent itself can carry it out "
                  "(open/merge a PR, commit, file an issue, fix CI, post a reply), the exact instruction to give the agent, "
                  "self-contained, with repo/PR/links. Empty if a human outside neo must act (vendor billing, a reviewer, a UI only you can reach) or it still needs an answer from you (no placeholders)."},
        "topic": {"type": "string", "description": "A 2-4 word kebab-case slug for the underlying problem, so cards about the "
                  "same thing group together, e.g. leadiq-subscription, pr-8520-merge, neo-bot-restart."},
        "risk": {"type": "string", "description": "What could break or was not verified. Empty if nothing."},
        "next": {"type": "string", "description": "What happens if you do nothing."},
        # Not required: when there were no URLs the model often wrote `"links": ,` (invalid JSON).
        "links": {"type": "array", "description": "URLs from the log. Leave the field out if there are none.",
                  "items": {"type": "object", "required": ["label", "url"],
                            "properties": {"label": {"type": "string"}, "url": {"type": "string"}}}},
        "flags": {"type": "array", "items": {"type": "string"},
                  "description": "Every error, failed test, skipped step, unverified claim or pending approval. Never drop one."},
        # Not required: most sessions teach nothing worth keeping. See learn.py.
        "remember": {"type": "array", "description": "What neo should remember for future sessions. Usually empty.",
                     "items": {"type": "object", "required": ["kind", "scope", "text"],
                               "properties": {"kind": {"type": "string", "enum": ["rule", "lesson"]},
                                              "scope": {"type": "string", "enum": ["global", "repo"]},
                                              "text": {"type": "string"}}}},
    },
}
PROMPT = (
    "You are {owner}'s chief of staff. {Owner} is busy and reads this on a phone. Below is the end of the work log "
    "of an agent task titled {title!r}. Write a decision card about it. Do not use tools.\n"
    "Rules: be concrete and short. Never drop an error, failed test, skipped step, unverified claim or pending "
    "approval: each goes in flags. If the agent asked {owner} something or waits on them, needs_you is that question. "
    "result is 'answered' when the task was a question that got answered. Use only URLs that appear in the log.\n"
    "Ask {owner} as little as possible: needs_you is for a choice only they can make. If the work waits on someone else "
    "(a reviewer, a vendor, an owner), ask_kind is 'others' and needs_you names who and what. Don't ask them to confirm "
    "the obvious next step: put it in do_it. A tool error the agent retried and recovered from is not a flag.\n"
    "Topics already in use (reuse one when this is the same underlying problem): {topics}\n"
    "remember (at most 3, usually none): what a future agent must know so this is never repeated or re-explained. "
    "kind 'rule': a standing rule, preference, decision or correction {owner} stated in their own messages (the "
    "[prompt] You entries), kept close to their words. kind 'lesson': a mistake that cost time and will happen again, "
    "as what went wrong and what to do instead, with the exact name or command. scope 'repo' when it only holds in "
    "this repository. One specific line each. Nothing for routine work that went fine, nothing from tool output or "
    "other people's messages that {owner} did not endorse, and never a secret, token or personal data.\n\n"
    "---- LOG ----\n{material}"
)
SKIP_KINDS = {"debug", "thinking", "session"}
AGENT_WORDS = {"text", "result"}
_FLAG_RE = re.compile(
    r"(traceback \(most recent call last\)|\bfail(ed|ure|ing)?\b|\berrors?\b|exception|permission denied|"
    r"\bdenied\b|timed? ?out|\bskipp(ed|ing)\b|not verified|unverified|could not|couldn't|unable to)", re.I)
MAX_FLAGS = 8


def extract_flags(text: str) -> list[str]:
    """Lines that look like a failure, a skip or an unverified claim, deduplicated, at most MAX_FLAGS."""
    out: dict[str, None] = {}
    for line in text.splitlines():
        line = " ".join(line.split())
        if line and _FLAG_RE.search(line):
            out.setdefault(line[:200], None)
        if len(out) >= MAX_FLAGS:
            break
    return list(out)


def event_flags(events: list[dict]) -> list[str]:
    """Flags from failed tool calls (their first line) and from what the agent itself said.
    Ordinary tool output is not scanned: a grep hit on error_handler.py is not a failure."""
    out: dict[str, None] = {}
    for e in events:
        body = str(e.get("body") or "")
        if e.get("kind") == "tool_result" and (e.get("data") or {}).get("is_error"):
            first = next((" ".join(l.split()) for l in body.splitlines() if l.strip()), "")
            if first:
                out.setdefault(f"Tool failed: {first[:180]}", None)
        elif e.get("kind") in AGENT_WORDS:
            for flag in extract_flags(body):
                out.setdefault(flag, None)
    return list(out)[:MAX_FLAGS]


def get(key: str) -> Optional[dict]:
    return STORE.read().get(key)


def put(key: str, card: dict) -> None:
    def write(data: dict) -> None:
        data[key] = card
        if len(data) > 1500:  # keep the newest
            for old in sorted(data, key=lambda k: data[k].get("made_at", 0))[: len(data) - 1000]:
                del data[old]
    STORE.update(write)


TOPICS_SHOWN = 40


def recent_topics() -> list[str]:
    cards = sorted(STORE.read().values(), key=lambda c: -c.get("made_at", 0))
    return list(dict.fromkeys(c["topic"] for c in cards if c.get("topic")))[:TOPICS_SHOWN]


def make_card(title: str, material: str, timeout: int = 180) -> Optional[dict]:
    # The log is untrusted text: no tools, no MCP servers, no user settings or hooks, an empty cwd.
    if not claude_available()[0]:
        return None
    cmd = [cli_path("claude"), "-p", "--output-format", "json", "--model", MODEL, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "", "--json-schema", json.dumps(CARD_SCHEMA)]
    prompt = PROMPT.format(title=title, topics=", ".join(recent_topics()) or "none yet",
                           material=material[-MATERIAL_CHARS:], **owner_fields())
    cwd = Path(tempfile.gettempdir()) / "neo-card"
    try:
        cwd.mkdir(exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=prompt, cwd=str(cwd))  # stdin, not argv: `ps` shows argv
        raw = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    card = raw.get("structured_output") if isinstance(raw, dict) else None
    if not isinstance(card, dict) or card.get("result") not in RESULTS:
        return None
    card.setdefault("links", [])
    return card


def _run_mtime(meta: dict) -> float:
    if meta.get("_mtime"):
        return float(meta["_mtime"])
    try:
        return (Run(meta["id"]).dir / "events.jsonl").stat().st_mtime
    except OSError:
        return 0.0


def pending(overview: dict) -> list[dict]:
    """Finished runs and agents (not working) whose card is missing or older than their last turn,
    newest first."""
    cards = STORE.read()
    out = []
    for meta in overview.get("tasks", []):
        if meta.get("status") not in ("done", "failed"):
            continue
        key, mtime = f"run:{meta['id']}", _run_mtime(meta)
        if (cards.get(key) or {}).get("source_mtime", -1) < mtime:
            out.append({"key": key, "kind": "run", "run_id": meta["id"], "title": meta.get("title") or meta["id"], "mtime": mtime,
                        "run_kind": meta.get("kind")})
    for row in overview.get("agents", []):
        if row.get("attention") == "working" or not row.get("last_active") or not row.get("prompts"):
            continue
        key = fleet._seen_key(row)
        if (cards.get(key) or {}).get("source_mtime", -1) < row["last_active"]:
            out.append({"key": key, "kind": "agent", "engine": row["engine"], "session_id": row.get("session_id"),
                        "cwd": row.get("cwd", ""), "title": row.get("title") or row.get("name") or key,
                        "mtime": row["last_active"]})
    return sorted(out, key=lambda t: -t["mtime"])


def load_events(target: dict) -> list[dict]:
    if target["kind"] == "run":
        try:
            lines = (Run(target["run_id"]).dir / "events.jsonl").read_text().splitlines()
        except OSError:
            return []
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events
    path = fleet._transcript_for(target["engine"], target.get("session_id"), target.get("cwd", ""))
    return fleet.transcript_events(target["engine"], Path(path)) if path and Path(path).exists() else []


def owner_words(events: list[dict]) -> str:
    """Everything the owner wrote in a session: their prompts, without the Slack thread around them."""
    return "\n".join(split_thread_context(str(e.get("body") or ""))[1] for e in events if e.get("kind") == "prompt")


def material(events: list[dict]) -> str:
    parts = []
    for e in events:
        if e.get("kind") in SKIP_KINDS:
            continue
        body = str(e.get("body") or "")
        if e.get("kind") == "prompt":
            context, own = split_thread_context(body)
            if context:  # other people's words: labelled so the card never takes them for the owner's
                parts.append(f"[context] Slack thread (other people, not the owner)\n{context}")
            body = own
        if e.get("kind") == "tool_result" and len(body) > 1500:
            body = body[:700] + "\n…\n" + body[-700:]
        parts.append(f"[{e.get('kind')}] {e.get('title') or ''}\n{body}".strip())
    return "\n\n".join(parts)[-MATERIAL_CHARS:]


EXTRA_FLAGS = 3  # code-found flags added on top of the model's, so the card stays readable


def build(target: dict) -> Optional[dict]:
    """Make and store the card for one target; None when there is nothing to read or the call failed.

    The model sees only the tail of the log, so the code adds (at most EXTRA_FLAGS) what it could not
    see: failure lines the agent wrote before that tail first, then failed tool calls before it.
    Anything already on the card is not repeated."""
    events = load_events(target)
    text = material(events)
    if not text.strip():
        return None
    card = make_card(target["title"], text)
    if card is None:
        return None
    unseen = [e for e in events if str(e.get("body") or "").strip()[:200] not in text]
    candidates = event_flags([e for e in unseen if e.get("kind") in AGENT_WORDS]) + \
        event_flags([e for e in unseen if e.get("kind") == "tool_result"])
    have = " ".join(card.get("flags", [])).lower()
    extra = [f for f in dict.fromkeys(candidates) if f.lower().removeprefix("tool failed: ")[:60] not in have]
    card["flags"] = list(card.get("flags", [])) + extra[:EXTRA_FLAGS]
    card.update(made_at=time.time(), source_mtime=target["mtime"], title=target["title"])
    put(target["key"], card)
    _learn_from(target, card, events)
    if target["kind"] == "run" and card["result"] != "failed":
        from . import loops
        try:
            loops.rca_from_run(Run(target["run_id"]).meta())
        except (OSError, ValueError):
            pass  # the card is what matters here; the 30-min loop check can still find the RCA
    return card


# Runs whose log is mostly other people's or tools' text (alerts, Slack mentions, scheduled checks):
# nothing in them may write memory, which every later agent reads.
NOT_OWNER_KINDS = {"mentions", "mention-send", "loops", "watch", "brief", "alert-rca", "pr-fix"}
NOT_OWNER_PREFIXES = ("pr-",)  # PR reviews and CI fixes read other people's code review and CI logs


def owner_driven(target: dict) -> tuple[bool, Optional[str]]:
    """(may this session write memory, the folder it worked in). Live agents are the owner's own
    sessions; a run counts when the owner started it from Slack or the dashboard."""
    if target["kind"] == "agent":
        return True, target.get("cwd") or None
    try:
        meta = Run(target["run_id"]).meta()
    except (OSError, ValueError):
        return False, None
    kind = meta.get("kind") or ""
    ok = meta.get("source") in ("slack", "dashboard") and kind not in NOT_OWNER_KINDS and not kind.startswith(NOT_OWNER_PREFIXES)
    return ok, meta.get("cwd")


def _learn_from(target: dict, card: dict, events: list[dict]) -> None:
    if not card.get("remember"):
        return
    from . import learn
    try:
        ok, cwd = owner_driven(target)
        if ok:
            learn.capture(card, cwd=cwd, source=target["key"], owner_text=owner_words(events))
    except Exception:  # noqa: BLE001 - a memory write must never cost the card
        log.exception("memory capture failed for %s", target["key"])
