"""Trace digests: one screen beside an agent or run trace that says what was asked, what state the work
is in, the plan as a checklist, what came of it, and (when the work has them) the cause chain, numbers,
changed files, checks and timeline. The model only fills structured fields (SCHEMA); digest_view draws
them with one fixed layout, so every digest reads the same way. The trace page shows it in a sandboxed
frame that cannot run scripts.

Built right after a card (chief.card_scheduler) and on demand when the page finds it missing or stale.
State: COS_HOME/digests/<key>.json = {kind, data, source_mtime, made_at, model, version}. Digests from
before the structured layout carry model-written `html` instead of `data` until they are remade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from . import cards, digest_view, fleet
from .config import owner_fields, owner_name
from .runner import Run, claude_available, cli_path, cos_home, valid_run_id

log = logging.getLogger(__name__)
MODEL = os.environ.get("NEO_DIGEST_MODEL", "claude-sonnet-5-5")
KINDS = ["rca", "plan", "fix", "investigation", "other"]
SKIP_RUN_KINDS = {"mentions", "loops", "watch", "brief", "mention-send"}  # neo's housekeeping
WORKING_EVERY_SECONDS = 300  # a working agent's digest is remade at most this often
RETRY_SECONDS = 600  # after a failed build, wait this long before trying again on its own
MAX_HTML = 200_000
_S = {"type": "string"}
_TONE = {"type": "string", "enum": ["ok", "new", "changed", "broken", "unsure", "neutral"]}


def _rows(props: dict, most: int) -> dict:
    return {"type": "array", "maxItems": most, "items": {"type": "object", "required": list(props),
                                                         "properties": props, "additionalProperties": False}}


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "state", "state_note", "intent", "outcome_result", "outcome_headline", "needs_you", "plan",
                 "chain", "numbers", "changes", "checks", "timeline", "next", "confidence", "confidence_why", "evidence"],
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "state": {"type": "string", "enum": ["working", "needs_you", "blocked", "done", "failed", "stopped"]},
        "state_note": _S, "intent": _S, "needs_you": _S, "next": _S, "confidence_why": _S,
        # Flat on purpose: as a nested object, outcome was where the model's StructuredOutput call most often broke
        # into XML parameters, and the next key (needs_you) came out as a call to a tool by that name. make() nests it.
        "outcome_result": {"type": "string", "enum": ["worked", "partial", "failed", "unclear", "pending"]},
        "outcome_headline": _S,
        "plan": _rows({"step": _S, "status": {"type": "string", "enum": ["done", "doing", "todo", "failed", "skipped"]}, "note": _S}, 8),
        "chain": {"type": "object", "required": ["title", "nodes"], "additionalProperties": False, "properties": {
            "title": _S, "nodes": _rows({"label": _S, "detail": _S, "tone": _TONE, "via": _S}, 6)}},
        "numbers": _rows({"value": _S, "label": _S, "tone": _TONE}, 4),
        "changes": _rows({"path": _S, "added": {"type": ["integer", "null"]}, "removed": {"type": ["integer", "null"]},
                          "kind": {"type": "string", "enum": ["new", "edited", "deleted"]}}, 10),
        "checks": _rows({"label": _S, "status": {"type": "string", "enum": ["pass", "fail", "pending", "none"]}, "url": _S}, 8),
        "timeline": _rows({"when": _S, "event": _S, "tone": _TONE}, 6),
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": _rows({"label": _S, "url": _S}, 6),
    },
}
PROMPT = """You fill the digest for {owner_intro}, who glances at it beside an agent's work log to know in five seconds
what was asked, where the work stands, and what came of it.
Below is the end of the work log of a task titled {title!r}.{needs}
Do not use tools. Fill every field; leave a text field as an empty string and a list empty when the log has
nothing for it (never write the word none or a pair of quote marks). Never invent facts, numbers,
files or URLs: use only what the log shows, and say "guess" in the text when something is a guess.

Write for a phone screen: plain words, no markdown, no emoji, no sentence over 14 words.

- kind: rca (root cause of an alert or bug), plan (a design or implementation plan), fix (code changed, a PR),
  investigation (a question answered or something looked into), other.
- state: where the work is NOW. working (still going), needs_you (waiting on {owner}'s answer or approval),
  blocked (stuck on something else: CI, a reviewer, access), done, failed, stopped (ended before finishing).
  state_note: at most 6 words of detail, e.g. "CI running on #8935" or "waiting on a review".
- intent: what {owner} asked for or what the task is for, in one sentence of at most 18 words. The goal, not the steps.
- outcome_result: worked | partial | failed | unclear | pending (still going), and outcome_headline: the single
  most useful fact about what came of it, at most 14 words. For an rca this is the root cause; for an investigation
  the answer; for a fix what changed and whether it is proven.
- needs_you: what {owner} must decide or do, at most 16 words; empty when nothing is needed.
- plan: the agent's steps as a checklist, in order, at most 8, each at most 8 words, with status done | doing |
  todo | failed | skipped and a note of at most 8 words (evidence, a number, a PR) or "". Include steps still to do.
- chain: the picture at the top of the digest, so always draw it: 3 to 5 nodes in order, label at most 4 words
  (a thing, not a sentence), detail at most 9 words, tone, and via: at most 4 words for the arrow to the next
  node (empty on the last). rca: the cause chain from trigger to symptom, the broken node tone "broken". plan: the
  new flow, new parts "new", changed parts "changed". fix: the code path the change sits on, the changed part
  "changed" or "new". investigation: question, the sources checked, the answer. title at most 4 words.
- numbers: up to 4 stat tiles from counts in the log (value at most 8 characters, label at most 4 words);
  tone broken for bad numbers, ok for good ones, neutral otherwise.
- changes: files changed, with lines added and removed when the log shows them (null otherwise) and kind.
- checks: tests, lint, type checks, PR, CI, deploy, review: label at most 3 words, status pass | fail | pending |
  none (not run yet), url when the log has one, else "".
- timeline: up to 6 dated events (when: a short time like "10:35Z" or "Oct 3"), only when the log has times.
- next: the next step, at most 16 words.
- confidence high | medium | low in the outcome, and confidence_why: at most 10 words on what would change it.
- evidence: up to 6 links from the log (PRs, Sentry, CI runs, dashboards) with a short label; url "" for a fact
  without a link.

---- LOG ----
{material}"""

VERSION = hashlib.sha1((PROMPT + json.dumps(SCHEMA, sort_keys=True)).encode()).hexdigest()[:8]  # a new prompt remakes every digest

_DROP_BLOCKS = re.compile(r"<(script|iframe|object|embed|form|template|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_DROP_TAGS = re.compile(r"</?(?:script|iframe|object|embed|form|template|noscript|link|meta|base|input|button|"
                        r"textarea|select|frame|frameset|applet)\b[^>]*>", re.I)
_EVENTS = re.compile(r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_BAD_URLS = re.compile(r"""(\s(?:href|src|xlink:href|action|formaction|srcset)\s*=\s*)"""
                       r"""(?:"\s*(?:javascript|data|vbscript):[^"]*"|'\s*(?:javascript|data|vbscript):[^']*'|"""
                       r"""(?:javascript|data|vbscript):[^\s>]*)""", re.I)

_building: set[str] = set()
_failed: dict[str, float] = {}
_state_lock = threading.Lock()
_build_lock = threading.Lock()  # one model call at a time


def clean(html: str) -> str:
    """Strip anything active. The sandboxed frame is the real boundary; this is the second one."""
    html = _DROP_BLOCKS.sub("", html or "")
    html = _DROP_TAGS.sub("", html)
    html = _EVENTS.sub("", html)
    html = _BAD_URLS.sub(r'\1"#"', html)
    return html[:MAX_HTML]


def valid_key(key: str) -> bool:
    return isinstance(key, str) and 4 < len(key) < 200 and key.startswith(("run:", "agent:"))


def _path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80]
    return cos_home() / "digests" / f"{safe}-{hashlib.sha1(key.encode()).hexdigest()[:8]}.json"


def get(key: str) -> Optional[dict]:
    try:
        data = json.loads(_path(key).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and (isinstance(data.get("data"), dict) or isinstance(data.get("html"), str))
    return data if ok else None


def put(key: str, digest: dict) -> None:
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(digest))
    tmp.replace(path)


def target_for(key: str, overview: dict) -> Optional[dict]:
    """The card-style target for a key (see cards.pending), or None when the run or agent is unknown."""
    if key.startswith("run:"):
        run_id = key[4:]
        if not valid_run_id(run_id):
            return None
        try:
            meta = Run(run_id).meta()
        except (OSError, ValueError):
            return None
        return {"key": key, "kind": "run", "run_id": run_id, "title": meta.get("title") or run_id,
                "mtime": cards._run_mtime(meta), "run_kind": meta.get("kind"),
                "working": meta.get("status") in ("queued", "running")}
    for row in overview.get("agents", []):
        if fleet._seen_key(row) == key:
            return {"key": key, "kind": "agent", "engine": row["engine"], "session_id": row.get("session_id"),
                    "cwd": row.get("cwd", ""), "title": row.get("title") or row.get("name") or key,
                    "mtime": row.get("last_active") or 0, "working": row.get("attention") == "working"}
    return None


def skipped(target: dict) -> bool:
    return target.get("run_kind") in SKIP_RUN_KINDS


def needs_build(target: dict, have: Optional[dict], now: float) -> bool:
    if not have or have.get("version") != VERSION:
        return True
    if have.get("source_mtime", 0) >= target["mtime"]:
        return False
    return not (target.get("working") and now - have.get("made_at", 0) < WORKING_EVERY_SECONDS)


def make(title: str, material: str, needs_you: str = "", timeout: int = 420) -> Optional[dict]:
    # The log is untrusted text: no tools, no MCP servers, no user settings or hooks, an empty cwd.
    if not claude_available()[0]:
        return None
    needs = f"\nThe card for this task says it needs {owner_name()} for: {needs_you}" if needs_you else ""
    cmd = [cli_path("claude"), "-p", "--output-format", "json", "--model", MODEL, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "", "--json-schema", json.dumps(SCHEMA)]
    prompt = PROMPT.format(title=title, needs=needs, material=material[-cards.MATERIAL_CHARS:], **owner_fields())
    cwd = Path(tempfile.gettempdir()) / "neo-digest"
    try:
        cwd.mkdir(exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=prompt, cwd=str(cwd))  # stdin, not argv: `ps` shows argv
        raw = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    out = raw.get("structured_output") if isinstance(raw, dict) else None
    if not isinstance(out, dict) or out.get("kind") not in KINDS or not str(out.get("intent") or "").strip():
        return None
    out["outcome"] = {"result": out.pop("outcome_result", ""), "headline": out.pop("outcome_headline", "")}
    return out


def build(target: dict) -> Optional[dict]:
    """Make and store the digest for one target; None when there is nothing to read or the call failed."""
    text = cards.material(cards.load_events(target))
    if not text.strip():
        return None
    out = make(target["title"], text, (cards.get(target["key"]) or {}).get("needs_you", ""))
    if out is None:
        return None
    digest = {"kind": out["kind"], "data": out, "source_mtime": target["mtime"],
              "made_at": time.time(), "model": MODEL, "version": VERSION}
    put(target["key"], digest)
    return digest


def start(target: dict) -> bool:
    """Build in the background unless one is already being built for this key."""
    key = target["key"]
    with _state_lock:
        if key in _building:
            return False
        _building.add(key)

    def work() -> None:
        try:
            with _build_lock:
                ok = build(target) is not None
            with _state_lock:
                if ok:
                    _failed.pop(key, None)
                else:
                    _failed[key] = time.time()
            log.info("digest for %s: %s", key, "made" if ok else "none (nothing to read or the call failed)")
        except Exception:  # noqa: BLE001 - a digest must never take anything else down
            log.exception("digest for %s failed", key)
            with _state_lock:
                _failed[key] = time.time()
        finally:
            with _state_lock:
                _building.discard(key)

    threading.Thread(target=work, daemon=True, name=f"digest-{key[:24]}").start()
    return True


def after_card(target: dict) -> None:
    """Called once a card is made, so the digest is ready by the time the owner opens the trace."""
    if not skipped(target) and needs_build(target, get(target["key"]), time.time()):
        start(target)


def _public(digest: dict) -> dict:
    # Drawn on every read, so a layout change shows at once without remaking any digest.
    html = digest_view.render(digest["data"]) if isinstance(digest.get("data"), dict) else clean(digest.get("html", ""))
    return {"kind": digest.get("kind"), "html": html, "made_at": digest.get("made_at"), "model": digest.get("model")}


def status(key: str, overview: dict, now: Optional[float] = None) -> dict:
    """What the trace page shows: ready (maybe stale and being rewritten), writing, or none."""
    now = time.time() if now is None else now
    target, have = target_for(key, overview), get(key)
    if target is not None and skipped(target):
        return {"status": "none", "skipped": True}
    with _state_lock:
        writing = key in _building
        failed_recently = now - _failed.get(key, 0) < RETRY_SECONDS
    stale = bool(target and needs_build(target, have, now))
    if stale and not writing and not failed_recently:
        writing = start(target) or writing
    if have:
        return {**_public(have), "status": "ready", "stale": stale, "writing": writing}
    if writing:
        return {"status": "writing"}
    return {"status": "none", "failed": failed_recently}


def refresh(key: str, overview: dict) -> dict:
    """Rebuild now (the page's Refresh / Retry)."""
    target = target_for(key, overview)
    if target is None or skipped(target):
        return {"status": "none"}
    with _state_lock:
        _failed.pop(key, None)
    start(target)
    return {"status": "writing"}
