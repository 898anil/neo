"""Parse @neo messages and render run progress for Slack. No Slack or I/O here.

Grammar (after the @neo mention is stripped):
    [live] [claude|codex] [cwd=<path>] <task>     start or continue a run
    stop | status | close | approve | deny         control the thread's run
    keys <key> [<key> ...]                        raw keys to a live agent (e.g. `keys 2 enter`)
    default [claude|codex]                        show or set the agent used when you name none
    watches                                       what your watches found
    pause [30m|2h|1d] | resume                    stop / restart the messages neo posts on its own
    help
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from cos.autonomy import parse_apply
from cos.learn import parse_remember
from cos.pause import DURATION

ENGINES = ("claude", "codex")
CONTROL = ("stop", "status", "close", "approve", "deny", "help")
STEP_LINES = 8
SLACK_TEXT_MAX = 3800

HELP = (
    "*How to use me*\n"
    "`@neo <task>` run the default engine headless, with live steps in this thread\n"
    "`@neo claude <task>` / `@neo codex <task>` pick the engine\n"
    "`@neo live claude <task>` run it interactively in herdr; you can attach and take over\n"
    "`@neo cwd=~/path/to/repo <task>` choose the working folder\n"
    "`@neo default codex` / `@neo default claude` set the agent used when you name none\n"
    "`@neo watches` what your watches (Sentry, production errors, ...) found and haven't been handled\n"
    "`@neo pause` / `@neo pause 2h` stop the messages I post on my own (notifications, briefs, digests, "
    "watch findings, auto RCA) until `@neo resume`; replies to you still come through\n"
    "`@neo remember <rule>` / `@neo remember in <repo>: <rule>` add to neo's memory; `@neo memory` show it\n"
    "`@neo apply 1 3` / `@neo apply all` in the nightly autonomy review thread: save the proposals you pick\n"
    "In a thread: reply `@neo <message>` to continue, `@neo stop` to interrupt, "
    "`@neo status`, and for live runs `@neo approve` / `@neo deny` / `@neo keys ...` / `@neo close`.\n"
    "*On the go*: when any agent on the machine (yours included) needs approval, asks you something, "
    "or finishes a long task, I post it with links to the dashboard and the Claude app. Answer with "
    "`@neo <message>` in that thread and I bring the agent's reply back there."
)


@dataclass
class Request:
    command: str  # "run" or one of CONTROL or "keys"
    prompt: str = ""
    engine: Optional[str] = None
    live: bool = False
    cwd: Optional[str] = None
    keys: list[str] = field(default_factory=list)
    repo: Optional[str] = None  # for "remember in <repo>: ..."


def parse(text: str) -> Request:
    words = text.strip().split()
    if not words:
        return Request("help")
    head = words[0].lower()
    if len(words) == 1 and head in CONTROL:
        return Request(head)
    if head == "keys":
        return Request("keys", keys=words[1:])
    if head == "default" and (len(words) == 1 or (len(words) == 2 and words[1].lower() in ENGINES)):
        return Request("default", engine=words[1].lower() if len(words) == 2 else None)
    if head == "watches" and len(words) == 1:
        return Request("watches")
    if head == "memory" and len(words) == 1:
        return Request("memory")
    if head == "resume" and len(words) == 1:
        return Request("resume")
    if head == "pause" and len(words) == 1:
        return Request("pause")
    # `pause 2h`; anything else after `pause` is a task ("pause the deploy").
    if head == "pause" and 2 <= len(words) <= 3 and DURATION.match("".join(words[1:]).lower()):
        return Request("pause", prompt="".join(words[1:]).lower())  # `pause 2h`, `pause 2 hours`
    if parse_apply(text) is not None:  # `apply 1 3` in an autonomy review thread
        return Request("apply", prompt=text.strip())
    remembered = parse_remember(text)
    if remembered:
        return Request("remember", prompt=remembered[0], repo=remembered[1])

    live = engine = cwd = None
    rest = list(words)
    while rest:
        token = rest[0]
        low = token.lower()
        if low == "live" and live is None:
            live = True
        elif low in ENGINES and engine is None:
            engine = low
        elif low.startswith("cwd=") and cwd is None:
            cwd = os.path.expanduser(token[4:])
        else:
            break
        rest.pop(0)
    # Keep the task's original spacing and line breaks: cut the option words off the front.
    prompt = text.strip()
    for token in words[: len(words) - len(rest)]:
        prompt = prompt[len(token):].lstrip()
    return Request("run", prompt=prompt, engine=engine, live=bool(live), cwd=cwd)


# ------------------------------------------------------------------ progress


def _one_line(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_label(name: str) -> str:
    """mcp__claude_ai_Sentry__get_sentry_resource -> Sentry · get_sentry_resource"""
    if name.startswith("mcp__"):
        parts = name[5:].split("__", 1)
        server = parts[0].removeprefix("claude_ai_").replace("_", " ")
        return f"{server} · {parts[1]}" if len(parts) == 2 else server
    return name


def _elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


class Progress:
    """Accumulates timeline events for one turn and renders a compact Slack message."""

    def __init__(self, engine: str, runtime: str, mode: str, run_id: str, dashboard_url: Optional[str] = None):
        self.engine, self.runtime, self.mode, self.run_id = engine, runtime, mode, run_id
        self.dashboard_url = dashboard_url
        self.started = time.monotonic()
        self.steps: list[str] = []
        self.step_count = 0
        self.latest = ""
        self.state = "working"
        self.cost: Optional[float] = None
        self.final = ""
        self.errors: list[str] = []

    def add(self, event: dict) -> None:
        kind, data = event["kind"], event.get("data") or {}
        if kind == "tool_call":
            self.step_count += 1
            self.steps.append(f"`{_one_line(tool_label(event['title']), 40)}` {_one_line(event['body'])}")
        elif kind == "tool_result" and data.get("is_error") and self.steps:
            self.steps[-1] += "  :x:"
        elif kind == "text":
            self.latest = event["body"]
            self.final = event["body"]
        elif kind == "result":
            if event.get("body"):
                self.final = event["body"]
            if data.get("cost_usd") is not None:
                self.cost = (self.cost or 0) + data["cost_usd"]
        elif kind == "error":
            self.errors.append(f"{event['title']}: {_one_line(event.get('body', ''), 300)}")
        elif kind == "status":
            self.state = data.get("status", self.state)

    def summary(self, finished: str) -> str:
        """The one line left in the thread once a run has ended without a clean answer."""
        icon = {"failed": ":x:", "cancelled": ":no_entry_sign:", "blocked": ":raised_hand:"}.get(finished, ":white_check_mark:")
        line = f"{icon} `{finished}` · {_elapsed(time.monotonic() - self.started)}"
        if self.dashboard_url:
            line += f" · <{self.dashboard_url.rstrip('/')}/#{self.run_id}|trace>"
        return line

    def render(self, finished: Optional[str] = None) -> str:
        icon = {"done": ":white_check_mark:", "failed": ":x:", "cancelled": ":no_entry_sign:", "blocked": ":raised_hand:"}
        state = finished or self.state
        head = (
            f"{icon.get(state, ':hourglass_flowing_sand:')} *{self.engine}* · {self.runtime}"
            f"{'' if self.runtime == 'herdr' else ' · ' + self.mode} · `{state}` · "
            f"{_elapsed(time.monotonic() - self.started)} · {self.step_count} step{'s' if self.step_count != 1 else ''}"
        )
        if self.cost:
            head += f" · ${self.cost:.2f}"
        lines = [head]
        hidden = len(self.steps) - STEP_LINES
        if hidden > 0:
            lines.append(f"_…{hidden} earlier step{'s' if hidden != 1 else ''}_")
        lines += [f"• {s}" for s in self.steps[-STEP_LINES:]]
        if self.latest and not finished:
            lines.append(f"_latest:_ {_one_line(self.latest, 200)}")
        lines += [f":warning: {e}" for e in self.errors[-3:]]
        if self.dashboard_url:
            lines.append(f"<{self.dashboard_url.rstrip('/')}/#{self.run_id}|full trace>  ·  run `{self.run_id}`")
        else:
            lines.append(f"run `{self.run_id}`")
        return clip("\n".join(lines))


def clip(text: str, limit: int = SLACK_TEXT_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def code_block(text: str, limit: int = 2800) -> str:
    text = (text or "").replace("```", "'''")
    if len(text) > limit:
        text = "…" + text[-limit:]
    return f"```{text}```"


_LOCAL_LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]+)\)")


def slack_markdown(text: str) -> str:
    """Agent Markdown, made readable in Slack: local file links (useless off the Mac mini)
    become `path:line` in code format."""
    from cos.config import workdir

    root = os.path.join(os.path.abspath(workdir()), "")
    home = os.path.join(os.path.expanduser("~"), "")

    def local(match: re.Match) -> str:
        label, path = match.group(1), match.group(2)
        # Relative to the agents' working directory, else to the home folder.
        short = path[len(root):] if path.startswith(root) else "~/" + path[len(home):] if path.startswith(home) else path
        return f"{label} (`{short}`)" if label.strip("` ") not in short else f"`{short}`"

    return _LOCAL_LINK.sub(local, text)
