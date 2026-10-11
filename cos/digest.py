"""Trace digests: one screen of model-written HTML for an agent or run trace, laid out for the kind of
work (an RCA's cause chain, a plan's architecture change, a fix's changes and tests). The trace page
shows it beside the trace in a sandboxed frame that cannot run scripts.

Built right after a card (chief.card_scheduler) and on demand when the page finds it missing or stale.
State: COS_HOME/digests/<key>.json = {kind, html, source_mtime, made_at, model}.
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

from . import cards, fleet
from .config import owner_fields, owner_name
from .runner import Run, cli_path, cos_home, valid_run_id

log = logging.getLogger(__name__)
MODEL = os.environ.get("NEO_DIGEST_MODEL", "claude-sonnet-5-5")
KINDS = ["rca", "plan", "fix", "investigation", "other"]
SKIP_RUN_KINDS = {"mentions", "loops", "watch", "brief", "mention-send"}  # neo's housekeeping
WORKING_EVERY_SECONDS = 300  # a working agent's digest is remade at most this often
RETRY_SECONDS = 600  # after a failed build, wait this long before trying again on its own
MAX_HTML = 200_000
SCHEMA = {"type": "object", "required": ["kind", "html"],
          "properties": {"kind": {"type": "string", "enum": KINDS}, "html": {"type": "string"}}}
PROMPT = """You turn an agent's work log into a VISUAL digest for {owner_intro}, who glances at it beside the full log.
Below is the end of the work log of a task titled {title!r}.{needs}
Do not use tools. Return kind and html.

kind: rca (root cause of an alert or bug), plan (a design or implementation plan), fix (code changed, a PR),
investigation (a question answered or something looked into), other.

The digest is a picture first, words second. It must be understood in five seconds and must not look like a
document. Hard rules:
- The centrepiece is a real diagram drawn in inline SVG (boxes, arrows, colour, small icons or emoji as markers).
  It takes at least half of the digest.
- Word budget: at most 45 words outside the diagram. Labels at most 6 words. No paragraphs, no sentence over 10
  words, no list longer than 3 items.
- Turn every number into a visual: stat tiles (big number, small label), tiny bar charts or sparklines drawn in
  SVG from counts in the log, a timeline strip with dots for events (deploys, first seen, spikes), a confidence
  meter (three segments).
- Colour carries meaning: var(--coral) broken or failing, var(--amber) needs attention or unsure, var(--mint)
  fixed, new or passing, var(--azure) links and neutral flow.
- Supporting detail (evidence links, file paths, commands) goes in ONE closed <details><summary>Evidence</summary>
  at the very bottom, as at most 5 short lines with links.

Layout by kind:
- rca: a flow diagram of the components involved (service, queue, page, check...) with arrows; the broken link
  drawn in coral with a warning marker and a label of a few words. Then a timeline strip and 2 or 3 stat tiles if
  the log has times or counts. Then the fix as up to 3 chips. Then the confidence meter.
- plan: the architecture BEFORE and AFTER as two SVG diagrams (stacked); new parts mint, changed parts amber,
  removed parts coral and struck through; a small legend. Then up to 3 chips for the steps.
- fix: a mini file tree with a green/red bar per file for lines added/removed, test results as pass/fail pills,
  and PR and CI badges.
- investigation: the answer as one big headline of at most 12 words, then a diagram or stat tiles showing the
  key facts.
- other: pick the diagram that explains it best.
If the agent is waiting on {owner}, start with a slim amber "Needs you" bar of at most 12 words.

Technical: ONE self-contained HTML fragment (no <html>, <head> or <body>). Width is 360 to 640px: SVGs use a
viewBox with width="100%" and stay legible at 360px (font-size 11 or more). Colours only through the CSS variables
var(--ink) page, var(--panel) and var(--raise) boxes, var(--line) borders, var(--text), var(--muted), var(--faint),
var(--amber), var(--azure), var(--mint), var(--coral); in SVG use them in style attributes, e.g.
style="fill:var(--panel);stroke:var(--coral)". Inline styles or one <style> block. Fonts are inherited. No scripts,
event handlers, forms, iframes, images or external resources. Use only URLs that appear in the log. Never invent
facts or numbers; label a guess as a guess.

---- LOG ----
{material}"""

VERSION = hashlib.sha1(PROMPT.encode()).hexdigest()[:8]  # a new prompt remakes every digest

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
    return data if isinstance(data, dict) and isinstance(data.get("html"), str) else None


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
    needs = f"\nThe card for this task says it needs {owner_name()} for: {needs_you}" if needs_you else ""
    cmd = [cli_path("claude"), "-p", "--output-format", "json", "--model", MODEL, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "", "--json-schema", json.dumps(SCHEMA),
           "--", PROMPT.format(title=title, needs=needs, material=material[-cards.MATERIAL_CHARS:], **owner_fields())]
    cwd = Path(tempfile.gettempdir()) / "neo-digest"
    try:
        cwd.mkdir(exist_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=str(cwd))
        raw = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    out = raw.get("structured_output") if isinstance(raw, dict) else None
    if not isinstance(out, dict) or out.get("kind") not in KINDS or not str(out.get("html") or "").strip():
        return None
    return out


def build(target: dict) -> Optional[dict]:
    """Make and store the digest for one target; None when there is nothing to read or the call failed."""
    text = cards.material(cards.load_events(target))
    if not text.strip():
        return None
    out = make(target["title"], text, (cards.get(target["key"]) or {}).get("needs_you", ""))
    if out is None:
        return None
    digest = {"kind": out["kind"], "html": clean(out["html"]), "source_mtime": target["mtime"],
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
    return {k: digest.get(k) for k in ("kind", "html", "made_at", "model")}


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
