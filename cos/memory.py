"""Search every Claude Code and Codex session on this machine, and reopen any of them.

Both CLIs already keep full transcripts on disk (~/.claude/projects, ~/.codex/sessions), so this
is the memory: "what did we decide about X?" searches what you and your agents actually said.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .events import TRANSCRIPT_NORMALIZERS

MAX_FILES = 40
PER_FILE = 3


def roots() -> dict[str, Path]:
    home = Path.home()
    return {"claude": home / ".claude" / "projects", "codex": home / ".codex" / "sessions"}


def engine_of(path: Path) -> Optional[str]:
    """Which CLI wrote this transcript, or None if it is outside both transcript folders."""
    resolved = path.resolve()
    for engine, root in roots().items():
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return engine if resolved.suffix == ".jsonl" and resolved.is_file() else None
    return None


def session_info(path: Path, engine: str) -> dict:
    """Title, working folder and session id, read from the first part of the transcript."""
    info = {"path": str(path), "engine": engine, "mtime": path.stat().st_mtime, "title": None, "cwd": None,
            "session_id": path.stem if engine == "claude" else None}
    with path.open("rb") as fh:
        for i, line in enumerate(fh):
            if i > 400 or (info["title"] and info["cwd"] and info["session_id"]):
                break
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            if engine == "claude":
                info["cwd"] = info["cwd"] or raw.get("cwd")
                if raw.get("type") == "ai-title":
                    info["title"] = raw.get("title") or raw.get("aiTitle") or info["title"]
                if not info["title"] and raw.get("type") == "user" and isinstance((raw.get("message") or {}).get("content"), str):
                    text = raw["message"]["content"].strip()
                    if text and not text.startswith("<"):
                        info["title"] = text[:90]
            else:
                payload = raw.get("payload") or {}
                if raw.get("type") == "session_meta":
                    info["cwd"] = payload.get("cwd")
                    info["session_id"] = payload.get("id")
                for event in TRANSCRIPT_NORMALIZERS["codex"](raw):
                    if event["kind"] == "prompt" and not info["title"]:
                        info["title"] = event["body"][:90]
    info["title"] = info["title"] or Path(info["cwd"] or path.parent.name).name
    info["repo"] = Path(info["cwd"]).name if info["cwd"] else ""
    return info


def _snippet(text: str, needle: str, width: int = 90) -> str:
    low = text.lower()
    i = low.find(needle)
    start = max(0, i - width)
    end = min(len(text), i + len(needle) + width)
    return ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


SEARCH_SECONDS = 8.0
# Sessions the CLIs start for their own bookkeeping (permission classifiers and the like).
INTERNAL_TITLES = ("Is this shell command read-only", "Reply with exactly")


def is_internal(info: dict) -> bool:
    return (info.get("title") or "").startswith(INTERNAL_TITLES)


def _candidates() -> list[tuple[Path, str]]:
    files = []
    for engine, root in roots().items():
        if root.is_dir():
            files += [(p, engine) for p in root.rglob("*.jsonl")]
    return sorted(files, key=lambda pe: pe[0].stat().st_mtime, reverse=True)


def search(query: str, limit: int = 25) -> dict:
    """Newest sessions first, under a time budget, so even a very large history answers quickly.
    Returns {"results": [...], "searched": n, "total": m}."""
    import re
    import time

    needle = " ".join(query.split()).lower()
    if len(needle) < 3:
        return {"results": [], "searched": 0, "total": 0}
    pattern = re.compile(re.escape(needle.encode()), re.IGNORECASE)
    files = _candidates()
    deadline = time.monotonic() + SEARCH_SECONDS
    results, searched = [], 0
    for path, engine in files:
        if time.monotonic() > deadline or len(results) >= limit:
            break
        searched += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not pattern.search(data):
            continue
        normalize = TRANSCRIPT_NORMALIZERS[engine]
        hits = []
        for line in data.splitlines():
            if not pattern.search(line):
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            for event in normalize(raw):
                if event["kind"] in ("prompt", "text", "result") and needle in (event.get("body") or "").lower():
                    hits.append({"who": "you" if event["kind"] == "prompt" else "agent",
                                 "text": _snippet(event["body"], needle), "ts": raw.get("timestamp")})
            if len(hits) >= PER_FILE:
                break
        if hits:
            info = session_info(path, engine)
            if not is_internal(info):
                results.append({**info, "hits": hits})
    return {"results": results, "searched": searched, "total": len(files)}


def recent(limit: int = 30) -> list[dict]:
    """The most recently active sessions, for browsing without a query."""
    out = []
    for path, engine in _candidates()[: limit * 4]:
        info = session_info(path, engine)
        if not is_internal(info):
            out.append(info)
        if len(out) >= limit:
            break
    return out


def resume_args(engine: str, session_id: str) -> tuple[str, ...]:
    """CLI arguments that reopen a session interactively."""
    if engine == "claude":
        return ("--resume", session_id)
    return ("resume", session_id)
