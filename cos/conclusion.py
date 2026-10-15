"""Every neo agent session ends its turn with a conclusion, so an idle session says where it stands.

A Claude Code Stop hook (run as a script, standard library only): when the last lines of the agent's last
message have no `Status: <state> — <one line>` line, the stop is blocked once and the agent is told to
repeat its whole reply with that line (Slack posts only the final message, so a bare Status line would
replace the answer). A second stop always goes through (stop_hook_active), so an agent can never be
trapped, and any error lets the stop through too.

Only agent sessions get it (the ones started with the agent brief, which asks for the line): neo passes it
with --settings (settings_json / write_settings), including when neo resumes one of the owner's sessions.
Scheduled jobs (brief, watches, loops, mentions) and Claude sessions neo did not start are not affected.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

STATES = ("done", "answered", "needs-you", "blocked", "failed", "waiting")
# "*Status:* done — merged #12", "Status: needs you - pick A or B", "## **Status**: waiting on CI".
_STATUS_RE = re.compile(r"^[ \t*_>`#-]*status[ \t*_`]*[:\-–—][ \t*_`]*(?:\W+[ \t]*)?"
                        r"(done|answered|needs[ \t_-]?you|blocked|failed|waiting)(?![\w-])", re.I)
TAIL_LINES = 3  # the line must close the reply, not sit in a list with more plans after it
REASON = (
    "You stopped without a conclusion. If this task still has work you can do, and the user did not ask you "
    "to hold off, keep going. Do not start anything the user did not ask for. Otherwise repeat your complete "
    "final reply (only the last message is delivered) and end it with one line: `Status: <done | answered | "
    "needs-you | blocked | failed | waiting> — <what happened, or the one thing needed and from whom>`."
)


def status_of(text: str) -> dict | None:
    """{"state": one of STATES, "line": what follows the state} from the reply's closing Status line, or None."""
    tail = [line for line in (text or "").splitlines() if line.strip()][-TAIL_LINES:]
    for line in reversed(tail):
        if line.lstrip().startswith("```"):
            return None  # the reply ends in a code block: a Status line there is an example, not this reply's
        found = _STATUS_RE.match(line)
        if found:
            state = re.sub(r"[ \t_-]+", "-", found.group(1).lower())
            rest = line[found.end():].strip(" \t*_`")
            return {"state": state, "line": re.sub(r"^[:\-–—.\s]+", "", rest).strip()}
    return None


def concluded(text: str) -> bool:
    return status_of(text) is not None


def decide(payload: dict) -> dict | None:
    """The hook's answer: None lets the stop through, {"decision": "block", ...} sends the agent back once."""
    if payload.get("stop_hook_active") or payload.get("background_tasks"):
        return None  # second stop, or it is waiting on its own background work and will be woken
    if concluded(payload.get("last_assistant_message") or ""):
        return None
    return {"decision": "block", "reason": REASON}


def settings_json() -> str:
    """--settings value that installs this hook for one session."""
    # -I: cos/ is the script's folder; a future cos/types.py must not shadow the standard library.
    command = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(Path(__file__).resolve()))}"
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
