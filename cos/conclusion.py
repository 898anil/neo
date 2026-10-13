"""Every neo Claude session ends its turn with a conclusion, so an idle session says where it stands.

A Claude Code Stop hook (run as a script, standard library only): when the agent's last message has no
`Status: <state> — <one line>` line, the stop is blocked once and the agent is told to finish the work or
write that line. A second stop always goes through (stop_hook_active), so an agent can never be trapped,
and any error lets the stop through too.

neo passes the hook with --settings (settings_json / write_settings) to the sessions it starts; the
owner's own Claude sessions are not affected.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

STATES = ("done", "answered", "needs-you", "blocked", "failed", "waiting")
# "*Status:* done — merged #12", "Status: needs you - pick A or B", "**Status**: waiting on CI".
_STATUS_RE = re.compile(r"^[\s*_>`-]*status[\s*_`]*[:\-–—][\s*_`]*(done|answered|needs[\s_-]?you|blocked|failed|waiting)\b",
                        re.I | re.M)
REASON = (
    "You stopped without a conclusion. If there is still work you can do for this task, keep going. "
    "Otherwise end your reply with one line: `Status: <done | answered | needs-you | blocked | failed | waiting> — "
    "<what happened, or the one thing needed and from whom>`."
)


def concluded(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def decide(payload: dict) -> dict | None:
    """The hook's answer: None lets the stop through, {"decision": "block", ...} sends the agent back once."""
    if payload.get("stop_hook_active") or payload.get("background_tasks"):
        return None  # second stop, or it is waiting on its own background work and will be woken
    if concluded(payload.get("last_assistant_message") or ""):
        return None
    return {"decision": "block", "reason": REASON}


def settings_json() -> str:
    """--settings value that installs this hook for one session."""
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]}})


def write_settings(path: Path) -> Path:
    """The same settings as a file, for agents started through herdr (it can't pass JSON through a shell)."""
    path.write_text(settings_json())
    return path


def main() -> int:
    try:
        out = decide(json.loads(sys.stdin.read() or "{}"))
    except Exception:  # noqa: BLE001 - a broken hook must never keep an agent from stopping
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
