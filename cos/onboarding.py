"""The first time someone uses neo, a short guided setup by DM: connect Claude, connect GitHub, pick a
brief time. One step at a time, with buttons; a plain reply works too (reply()), and every step can be
skipped and done later from the Home tab. app.py posts the steps and acts on the choices.

State in COS_HOME/onboarding.json: Slack id -> {"step": the step waiting for an answer, or "done"}.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from .store import JsonStore

STEPS = ("claude", "gh", "brief")
ACTION = "onboard"  # action ids are ACTION_<n>; the value is "<step>:<choice>"
STATE = JsonStore("onboarding.json")


LAPSE_SECONDS = 24 * 3600  # an unanswered step stops catching typed replies after a day (buttons still work)


def step_of(user: str) -> Optional[str]:
    """The step waiting for their answer; "done"; or None when they haven't started."""
    return (STATE.read().get(user) or {}).get("step")


def step_ts(user: str) -> Optional[str]:
    """The Slack ts of the step message waiting for an answer, while typed replies may answer it."""
    row = STATE.read().get(user) or {}
    return row.get("ts") if time.time() - float(row.get("at") or 0) < LAPSE_SECONDS else None


def set_step(user: str, step: str, ts: Optional[str] = None) -> None:
    STATE.update(lambda d: d.setdefault(user, {}).update(step=step, ts=ts, at=time.time()))


def claim(user: str) -> bool:
    """Start onboarding for someone new: True for exactly one caller, however many arrive at once."""
    def start(data: dict) -> bool:
        if (data.get(user) or {}).get("step") is not None:
            return False
        data[user] = {"step": STEPS[0], "ts": None, "at": time.time()}
        return True
    return STATE.update(start)


def next_step(step: str) -> str:
    return STEPS[STEPS.index(step) + 1] if step in STEPS[:-1] else "done"


def _buttons(step: str, choices: list[tuple[str, str]], primary: str = "") -> dict:
    elements = []
    for n, (label, choice) in enumerate(choices):
        button = {"type": "button", "action_id": f"{ACTION}_{n}", "value": f"{step}:{choice}",
                  "text": {"type": "plain_text", "text": label}}
        if choice == primary:
            button["style"] = "primary"
        elements.append(button)
    return {"type": "actions", "elements": elements}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def welcome(name: str) -> str:
    return (f"Hi {name} :wave: I'm neo, your chief of staff. I run coding agents for you in your own workspace, "
            "keep an eye on things you care about, and send you a morning brief. Three quick steps to set you up; "
            "skip any of them and finish later from my *Home* tab.")


def message(step: str, lender: Optional[str], lends_claude: bool, lends_gh: bool) -> tuple[str, list]:
    """(fallback text, blocks) for one step."""
    n = STEPS.index(step) + 1 if step in STEPS else len(STEPS)
    if step == "claude":
        text = (f"*Step {n} of {len(STEPS)} · Your Claude*\nWith your own Claude account your agents run on your seat, "
                "and your morning brief can read your own calendar, mail and Slack.")
        later = (f"Use {lender}'s for now", "skip") if lends_claude else ("Skip for now", "skip")
        return text, [_section(text), _buttons(step, [("Connect my Claude", "connect"), later], "connect")]
    if step == "gh":
        text = (f"*Step {n} of {len(STEPS)} · GitHub*\nConnect GitHub so your agents can clone your repos and open "
                "pull requests as you.")
        later = (f"Use {lender}'s for now", "skip") if lends_gh else ("Skip for now", "skip")
        return text, [_section(text), _buttons(step, [("Connect GitHub", "connect"), later], "connect")]
    if step == "brief":
        text = (f"*Step {n} of {len(STEPS)} · Morning brief*\nWhen should your brief come on work days (your time zone)? "
                "It needs your own Claude. Or reply with a time.")
        return text, [_section(text), _buttons(step, [("08:00", "08:00"), ("09:00", "09:00"), ("10:00", "10:00"),
                                                      ("No brief", "off")], "09:00")]
    text = ("*You're set* :tada:\nJust write to me in plain words, here or with `@neo` in a channel:\n"
            "• \"fix the flaky login test in backie\"\n• \"watch CI failures on deskie every hour\"\n"
            "• \"send my digest at 6pm\" · \"connect my codex\" · \"what are you watching for me?\"\n"
            "Your sign-ins, tasks and watches are on my *Home* tab.")
    return text, [_section(text)]


def reply(step: str, text: str) -> Optional[str]:
    """A plain reply to a step, as the choice a button would give: "connect", "skip", "HH:MM" or "off";
    None when it doesn't answer the step (then it is an ordinary message)."""
    words = " ".join(re.sub(r"[.!,]+$", "", text.strip().lower()).split())
    if not words or len(words) > 40:  # longer is a message, not an answer to the step
        return None
    # the whole message must be the answer: "ok go ahead and merge it" or "fix PR 7" answer nothing
    if step in ("claude", "gh"):
        if re.fullmatch(r"(skip|later|no|nope|not now|skip for now|maybe later|use \w+'?s?( for now)?|"
                        r"(i'?ll )?use (yours|his|hers|theirs)( for now)?)( thanks| please)?", words):
            return "skip"
        if re.fullmatch(r"(yes|yep|yeah|sure|ok|okay|connect|connect it|sign me in|do it|go ahead)( please| thanks)?", words):
            return "connect"
        return None
    if step == "brief":
        if re.fullmatch(r"(no|none|off|skip|no brief|no thanks|no brief thanks|don'?t|never)", words):
            return "off"
        m = re.fullmatch(r"(?:at )?([01]?\d|2[0-3])(?::([0-5]\d))?\s*(am|pm)?", words)
        if not m or (not m.group(2) and not m.group(3)):  # a bare number ("7") isn't clearly a time
            return None
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}" if hour < 24 else None
    return None
