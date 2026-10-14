"""Settings shared by the Slack bot and the dashboard server."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def workdir() -> str:
    """Folder agents work in unless told otherwise (usually the projects root)."""
    return os.path.expanduser(
        os.environ.get("NEO_WORKDIR") or os.environ.get("CODEX_WORKDIR") or os.environ.get("CLAUDE_WORKDIR") or "~"
    )


def default_engine() -> str:
    """The agent used when you don't name one: your dashboard choice, else NEO_DEFAULT_ENGINE, else Claude."""
    from .settings import load

    chosen = load().get("default_engine")
    return chosen or os.environ.get("NEO_DEFAULT_ENGINE", "claude")


# How a Slack task carries the thread it was asked in: the thread (other people's words) first, then
# this line, then the owner's own message. cards.py splits on it so only the owner's words count as theirs.
THREAD_CONTEXT_HEAD = "Here is the Slack thread so far (for context):"
OWNER_MESSAGE_MARK = "Now respond to this new message directed at you:"


def split_thread_context(prompt: str) -> tuple[str, str]:
    """(thread context, the owner's own message) of a task's prompt; context is "" when there is none."""
    head, mark, own = prompt.rpartition(OWNER_MESSAGE_MARK)
    return (head.strip(), own.strip()) if mark and head.lstrip().startswith(THREAD_CONTEXT_HEAD) else ("", prompt)


def prompt_file(name: str) -> Path:
    return ROOT / "prompts" / name


def read_prompt(name: str) -> Optional[str]:
    try:
        text = prompt_file(name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _base_brief() -> Optional[str]:
    override = os.environ.get("NEO_BRIEF_FILE")
    if override:
        try:
            return Path(override).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return read_prompt("slack-agent.md")


def agent_brief(cwd: Optional[str] = None) -> Optional[str]:
    """Standing instructions for every agent we start (how to do an RCA, how to answer), plus neo's
    memory for the folder it works in (cos/learn.py). Re-read on each run, so editing the files
    changes behaviour without a restart. A broken memory never stops an agent from starting."""
    from . import learn

    try:
        remembered = learn.brief(cwd)
    except Exception:  # noqa: BLE001 - memory is a bonus, the task is what matters
        remembered = ""
    parts = [p for p in (_base_brief(), remembered) if p]
    return "\n\n".join(parts) or None


def owner_name() -> str:
    """Who neo works for, as prompts name them (NEO_OWNER_NAME). Read at call time."""
    return (os.environ.get("NEO_OWNER_NAME") or "").strip() or "the owner"


def owner_profile() -> str:
    """Optional one-line description of the owner for prompts, e.g. "an engineer at Acme" (NEO_OWNER_PROFILE)."""
    return (os.environ.get("NEO_OWNER_PROFILE") or "").strip()


def owner_fields() -> dict:
    """Format fields for prompts: {owner}, {Owner} (capitalised for the start of a sentence) and
    {owner_intro} (the name plus ", <profile>" when NEO_OWNER_PROFILE is set)."""
    name = owner_name()
    profile = owner_profile()
    return {"owner": name, "Owner": name[:1].upper() + name[1:],
            "owner_intro": f"{name}, {profile}" if profile else name}


def exit_with_parent() -> None:
    """When an app runs neo as its child (NEO_PARENT_PID, the Mac app), exit once that app is gone,
    even if it crashed or was force-quit, so no orphan keeps the port or a second Slack connection."""
    try:
        parent = int(os.environ.pop("NEO_PARENT_PID", None) or 0)  # agents we start must not inherit it
    except ValueError:
        return
    if parent <= 1:
        return

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()
