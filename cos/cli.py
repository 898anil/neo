"""Command line: run an agent with a live timeline, list runs, tail a run, serve the dashboard."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import pause
from .runner import ENGINES, MODES, TERMINAL, Run, list_runs, valid_run_id

COLORS = {
    "prompt": "\033[1;36m",
    "session": "\033[2m",
    "text": "\033[0m",
    "thinking": "\033[2;3m",
    "tool_call": "\033[33m",
    "tool_result": "\033[2m",
    "plan": "\033[35m",
    "usage": "\033[2m",
    "result": "\033[1;32m",
    "error": "\033[1;31m",
    "debug": "\033[2m",
}
RESET = "\033[0m"
BODY_LINES = 12


def render(event: dict, show_debug: bool = False, color: bool = True) -> str | None:
    kind = event["kind"]
    if kind == "debug" and not show_debug:
        return None
    c, r = (COLORS.get(kind, ""), RESET) if color else ("", "")
    stamp = event["ts"][11:19]
    marker = {"tool_call": ">", "tool_result": "<", "thinking": "~", "error": "!"}.get(kind, "*")
    head = f"{c}{stamp} {marker} [{kind}] {event['title']}{r}"
    body = (event.get("body") or "").rstrip()
    if not body:
        return head
    lines = body.splitlines()
    if kind in ("tool_result", "thinking", "debug") and len(lines) > BODY_LINES:
        lines = lines[:BODY_LINES] + [f"... ({len(lines) - BODY_LINES} more lines)"]
    indented = "\n".join("    " + line for line in lines)
    return f"{head}\n{c}{indented}{r}"


def cmd_run(args) -> int:
    run = Run.create(args.engine, args.prompt, args.cwd, args.mode, source="cli")
    print(f"run {run.id}  ({args.engine}, {args.mode})  dashboard: /#{run.id}", file=sys.stderr)
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def show(event):
        text = render(event, args.debug, color)
        if text:
            print(text, flush=True)

    try:
        meta = run.execute(on_event=show)
    except KeyboardInterrupt:
        run.cancel()
        return 130
    print(f"\nstatus={meta['status']} cost=${meta.get('cost_usd', 0)} time={meta.get('last_turn_seconds')}s", file=sys.stderr)
    return 0 if meta["status"] == "done" else 1


def cmd_continue(args) -> int:
    if not valid_run_id(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 2
    run = Run(args.run_id)
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    meta = run.execute(args.prompt, on_event=lambda e: (t := render(e, args.debug, color)) and print(t, flush=True))
    return 0 if meta["status"] == "done" else 1


def cmd_list(args) -> int:
    for meta in list_runs(args.limit):
        prompt = meta["prompt"].replace("\n", " ")[:70]
        print(f"{meta['id']}  {meta['engine']:6}  {meta['status']:9}  ${meta.get('cost_usd') or 0:<7}  {prompt}")
    return 0


def cmd_tail(args) -> int:
    if not valid_run_id(args.run_id):
        print("invalid run id", file=sys.stderr)
        return 2
    run = Run(args.run_id)
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    with (run.dir / "events.jsonl").open() as fh:
        while True:
            line = fh.readline()
            if line.endswith("\n"):
                text = render(json.loads(line), args.debug, color)
                if text:
                    print(text, flush=True)
                continue
            if run.meta()["status"] in TERMINAL and not args.follow:
                return 0
            time.sleep(0.25)


def cmd_serve(args) -> int:
    from .config import exit_with_parent
    from .server import serve

    exit_with_parent()
    serve(args.host, args.port)
    return 0


def cmd_pause(args) -> int:
    pause.pause(pause.parse_duration(args.duration) if args.duration else None)
    print(pause.describe())
    return 0


def cmd_resume(args) -> int:
    pause.resume()
    print(pause.describe())
    return 0


def cmd_paused(args) -> int:
    """Exit 0 while notifications are paused, 1 otherwise: `python -m cos paused && exit 0` in a job."""
    if not args.quiet:
        print(pause.describe())
    return 0 if pause.is_paused() else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cos", description="Run Claude Code or Codex with a visible timeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start a run and stream its timeline")
    p.add_argument("prompt")
    p.add_argument("--engine", choices=ENGINES, default="claude")
    p.add_argument("--mode", choices=MODES, default="readonly",
                   help="readonly: no edits or unapproved commands; edit: file edits allowed; full: no guardrails")
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--debug", action="store_true", help="also show hooks, rate limits and other low-level events")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("continue", help="send a follow-up prompt to an existing run's session")
    p.add_argument("run_id")
    p.add_argument("prompt")
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_continue)

    p = sub.add_parser("list", help="list recent runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("tail", help="print a run's timeline; waits while it is running")
    p.add_argument("run_id")
    p.add_argument("--follow", action="store_true", help="keep waiting after the run finishes")
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("serve", help="start the dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("pause", help="stop the Slack messages neo posts on its own")
    p.add_argument("duration", nargs="?", help="30m, 2h, 1d; omit to pause until `resume`")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="turn neo's Slack notifications back on")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("paused", help="exit 0 while notifications are paused (for scheduled jobs)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_paused)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
