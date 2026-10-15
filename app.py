"""Slack listener: mention @neo to run Claude Code or Codex on this machine, with every step visible.

Uses Socket Mode (a persistent WebSocket), so no public URL or webhook is
needed -- the process just needs to stay running on this machine.

Two runtimes:
  headless  `claude -p` / `codex exec`; each step streams into a progress message
  herdr     a real interactive agent inside herdr (`@neo live ...`); you can attach
            from any machine, and approvals can be answered from Slack

Every run is recorded under runs/<run_id>/ and shown by the dashboard (cos/server.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from slack_bolt.app.async_app import AsyncApp  # noqa: E402
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler  # noqa: E402

from urllib.parse import quote  # noqa: E402

import chief  # noqa: E402
import commands  # noqa: E402
import sessions  # noqa: E402
from commands import Progress, clip, code_block  # noqa: E402
from cos import brief as morning, fleet, pause, settings, watches  # noqa: E402
from cos.alerts import AlertGate  # noqa: E402
from cos import autonomy, learn  # noqa: E402
from cos.config import OWNER_MESSAGE_MARK, THREAD_CONTEXT_HEAD, agent_brief, default_engine, exit_with_parent, workdir  # noqa: E402
from cos.herdr import Herdr, HerdrError  # noqa: E402
from cos.interactive import InteractiveRun  # noqa: E402
from cos.runner import ENGINES, MODES, Run, cos_home, list_runs, pid_alive  # noqa: E402

# Only these Slack user ids may trigger the bot. This is the security gate:
# a triggered session runs an agent on this machine with tool access, so never
# leave this empty in a shared workspace.
ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# Headless permission level: readonly | edit | full. "full" matches the bot's original behavior.
MODE = os.environ.get("NEO_MODE", "full")
WORKDIR = workdir()
DASHBOARD_PORT = os.environ.get("NEO_DASHBOARD_PORT")
DASHBOARD_HOST = os.environ.get("NEO_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_URL = os.environ.get("NEO_DASHBOARD_URL") or (
    f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}" if DASHBOARD_PORT else None
)

# Push notifications: DM this Slack user when any agent on this machine needs them.
NOTIFY_USER = os.environ.get("NEO_NOTIFY_USER") or (sorted(ALLOWED_USER_IDS)[0] if ALLOWED_USER_IDS else None)
NOTIFY_ENABLED = os.environ.get("NEO_NOTIFY", "1") != "0"
# Post notifications in this channel (mentioning NOTIFY_USER) instead of a DM. Replies work there
# with `@neo ...` in the thread; DM replies need the app's messages tab enabled (manifest.json).
NOTIFY_CHANNEL = os.environ.get("NEO_NOTIFY_CHANNEL")
# Only announce a finished turn if the agent worked at least this long (or ended with a question).
NOTIFY_MIN_WORK_SECONDS = int(os.environ.get("NEO_NOTIFY_MIN_WORK_SECONDS", "60"))
MONITOR_SECONDS = 5
# An agent "working" with no new activity for this long is probably stuck; you are told once.
STUCK_MINUTES = int(os.environ.get("NEO_STUCK_MINUTES", "15"))
# Morning brief: weekdays at this local time (HH:MM). NEO_BRIEF_AT=off disables it.
BRIEF_AT = os.environ.get("NEO_BRIEF_AT", "08:30")
BRIEF_DAYS = {int(d) for d in os.environ.get("NEO_BRIEF_DAYS", "0,1,2,3,4").split(",") if d.strip().isdigit()}  # Mon=0
# Agents you answered from a notification thread: their next result goes back to that thread.
# key -> [channel, thread_ts, last_text when you answered, polls waited for a new answer]
FOLLOW: dict[str, list] = {}

# Minimum seconds between Slack message edits, to stay under rate limits.
_EDIT_INTERVAL = 1.5
# Most thread replies to pull in as context (oldest kept if a thread is longer).
_THREAD_CONTEXT_LIMIT = 50

# Taken out of the environment: every agent this process starts inherits it, and agents read
# untrusted text (threads, web pages, repos) that could ask them to print it.
SLACK_APP_TOKEN = os.environ.pop("SLACK_APP_TOKEN", "")
app = AsyncApp(token=os.environ.pop("SLACK_BOT_TOKEN"))

# The bot's own user id, resolved at startup so its messages can be labelled as
# the assistant's when reconstructing a thread transcript.
BOT_USER_ID: str | None = None
# Workspace URL (https://<team>.slack.com/), for thread permalinks shown on the dashboard.
TEAM_URL: str | None = None


def thread_url(channel: str, thread_ts: str) -> Optional[str]:
    if not TEAM_URL:
        return None
    return f"{TEAM_URL.rstrip('/')}/archives/{channel}/p{thread_ts.replace('.', '')}"

# <@U123> or <@U123|name>
_mention_re = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
# Footer Slack adds to messages sent through an AI assistant ("*Sent using* <@...|Claude>").
_footer_re = re.compile(r"\s*[*_]?Sent using[*_]?\s*(?:<@[^>]+>|Claude)?\s*$")

log = logging.getLogger("neo")

# Runs in progress, by Slack thread.
HEADLESS: dict[str, Run] = {}
# Headless threads whose answer is posted while the engine still finishes background work.
ANSWERED: set[str] = set()
LIVE: dict[str, "LiveThread"] = {}
# Threads whose mention is being handled; claimed before any await so two quick
# mentions in one thread cannot both start work.
BUSY: set[str] = set()
# Watchers for live runs without a Slack thread, and tasks posting adopted runs, kept alive here.
RESUMED: list = []
_LIVE_LOCK = asyncio.Lock()


def _clean_prompt(text: str) -> str:
    """Strip the @bot mention (and an assistant's "Sent using" footer), leaving the instruction."""
    return _mention_re.sub("", _footer_re.sub("", text)).strip()


def _collect_text(node, out: list[str]) -> None:
    """Recursively gather every "text" string in a Block Kit / attachment tree."""
    if isinstance(node, dict):
        value = node.get("text")
        if isinstance(value, str):
            out.append(value)
        for child in node.values():
            _collect_text(child, out)
    elif isinstance(node, list):
        for child in node:
            _collect_text(child, out)


def _message_text(msg: dict) -> str:
    """Full readable text of a message, including blocks and attachments.

    Alert/bot messages often carry their real content in blocks or attachments
    with an empty top-level `text`, so reading `text` alone loses the context.
    """
    parts: list[str] = []
    if msg.get("text"):
        parts.append(msg["text"])
    for attachment in msg.get("attachments") or []:
        for key in ("pretext", "title", "text", "fallback"):
            if attachment.get(key):
                parts.append(attachment[key])
    _collect_text(msg.get("blocks") or [], parts)
    # De-dup while preserving order (blocks often repeat the fallback text).
    seen: dict[str, None] = {}
    for part in parts:
        cleaned = _mention_re.sub("", part).strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return "\n".join(seen)


async def _thread_transcript(client, channel: str, thread_ts: str, exclude_ts: str) -> str:
    """Return the thread's messages (excluding the triggering one) as a transcript.

    Needs channels:history / groups:history. If the scope is missing or the call
    fails, returns "" so the bot still answers, just without thread context.
    """
    try:
        resp = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=_THREAD_CONTEXT_LIMIT
        )
    except Exception as exc:  # noqa: BLE001 - context is best-effort, never fatal
        log.warning("Thread context unavailable: %s", exc)
        return ""

    lines: list[str] = []
    for msg in resp.get("messages", []):
        if msg.get("ts") == exclude_ts:
            continue
        text = _message_text(msg)
        if not text:
            continue
        # Only OUR bot's messages are the assistant's; other apps (e.g. an alert
        # bot that started the thread) are context from "someone else".
        speaker = "neo" if msg.get("user") == BOT_USER_ID else "user"
        lines.append(f"{speaker}: {text}")

    if not lines:
        return ""
    return THREAD_CONTEXT_HEAD + "\n" + "\n".join(lines)


def _with_context(transcript: str, prompt: str) -> str:
    if not transcript:
        return prompt
    return f"{transcript}\n\n{OWNER_MESSAGE_MARK}\n{prompt}"


# ------------------------------------------------------------------- output


class Reporter:
    """One progress message per turn, edited in place as events arrive."""

    def __init__(self, client, channel: str, thread_ts: str, progress: Progress):
        self.client, self.channel, self.thread_ts, self.progress = client, channel, thread_ts, progress
        self.ts: Optional[str] = None
        self._last_edit = 0.0

    async def post(self) -> None:
        resp = await self.client.chat_postMessage(
            channel=self.channel, thread_ts=self.thread_ts, text=self.progress.render()
        )
        self.ts = resp["ts"]

    async def update(self, force: bool = False, finished: Optional[str] = None) -> None:
        if not self.ts:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < _EDIT_INTERVAL:
            return
        self._last_edit = now
        try:
            await self.client.chat_update(
                channel=self.channel, ts=self.ts, text=self.progress.render(finished)
            )
        except Exception as exc:  # noqa: BLE001 - a failed edit must not stop the run
            log.warning("progress update failed: %s", exc)

    async def finish(self, status: str, answered: bool) -> None:
        """Clear the progress card once the turn is over: gone after a clean answer,
        one line with the trace link otherwise."""
        if not self.ts:
            return
        try:
            if status == "done" and answered:
                await self.client.chat_delete(channel=self.channel, ts=self.ts)
            else:
                await self.client.chat_update(
                    channel=self.channel, ts=self.ts, text=self.progress.summary(status)
                )
        except Exception as exc:  # noqa: BLE001 - leave the full card rather than fail the run
            log.warning("progress finish failed: %s", exc)
            await self.update(force=True, finished=status)

    async def say(self, text: str) -> None:
        await post_markdown(self.client, self.channel, self.thread_ts, text)


def split_message(text: str, limit: int = commands.SLACK_TEXT_MAX) -> list[str]:
    """Split a long answer into Slack-sized messages, preferring line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks[:10]  # a runaway answer should not flood the thread; the dashboard has it all


_MARKDOWN_BLOCK_LIMIT = 11000  # Slack caps a markdown block at 12,000 characters


async def post_markdown(client, channel: str, thread_ts: str, text: str) -> None:
    """Post an agent's answer with Slack's native markdown block (headings, bold, links, code,
    tables render as written). Falls back to plain text chunks if the block is refused."""
    text = commands.slack_markdown(text)
    for chunk in split_message(text, _MARKDOWN_BLOCK_LIMIT):
        try:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk[:3000],
                blocks=[{"type": "markdown", "text": chunk}],
            )
        except Exception as exc:  # noqa: BLE001 - older workspaces may not accept markdown blocks
            log.warning("markdown block refused (%s); posting plain text", exc)
            for part in split_message(chunk):
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=part)


async def _react(client, channel: str, ts: str, name: str, remove: bool = False) -> None:
    try:
        if remove:
            await client.reactions_remove(channel=channel, name=name, timestamp=ts)
        else:
            await client.reactions_add(channel=channel, name=name, timestamp=ts)
    except Exception as exc:  # noqa: BLE001 - reactions are cosmetic (already_reacted, rate limits)
        log.debug("reaction %s failed: %s", name, exc)


# ----------------------------------------------------------------- headless


async def run_headless(client, channel: str, thread_ts: str, entry: Optional[dict], req, prompt: str,
                       kind: Optional[str] = None) -> bool:
    engine = req.engine or (entry or {}).get("engine") or default_engine()
    same_engine = entry and entry.get("runtime") == "headless" and entry.get("engine") == engine
    cwd = req.cwd or WORKDIR
    system = await asyncio.to_thread(agent_brief, cwd)  # reads memory and runs git: not on the event loop
    slack = {"slack_channel": channel, "slack_thread": thread_ts, "slack_url": thread_url(channel, thread_ts),
             "system_prompt": system, "conclude": bool(system)}
    if kind:
        slack["kind"] = kind

    follow_up = False
    if same_engine and entry.get("run_id"):
        run, follow_up = Run(entry["run_id"]), True
    else:
        run = Run.create(engine, prompt, cwd, MODE, source="slack", extra=slack)
        if same_engine and entry.get("session_id"):
            # Thread from before run tracking: resume its engine session.
            run.update_meta(session_id=entry["session_id"])
            follow_up = True
    await sessions.set_thread(thread_ts, {"run_id": run.id, "engine": engine, "runtime": "headless"})

    progress = Progress(engine, "headless", run.meta()["mode"], run.id, DASHBOARD_URL)
    reporter = Reporter(client, channel, thread_ts, progress)
    await reporter.post()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    HEADLESS[thread_ts] = run
    task = asyncio.create_task(asyncio.to_thread(run.execute, prompt if follow_up else None, on_event))
    meta: dict = {"status": "failed"}
    # Claude Code -p prints its "result" when the turn ends but keeps the process alive while
    # background work it started (run_in_background shells, background agents) is still going,
    # and wakes again for each one that finishes. Waiting for the exit held the answer back for
    # up to 90 minutes, so the answer goes out on the first result and later ones follow it.
    posted: list[str] = []
    try:
        while not (task.done() and queue.empty()):
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                await reporter.update()
                continue
            progress.add(event)
            if event["kind"] == "result" and progress.final.strip():
                if not posted:
                    await reporter.finish("done", True)
                if progress.final.strip() not in posted:
                    await reporter.say(progress.final if not posted else ":hourglass: _Background work finished:_\n" + progress.final)
                    posted.append(progress.final.strip())
                    run.update_meta(slack_posted_seq=event.get("seq"))  # a restarted bot must not repeat it
                ANSWERED.add(thread_ts)
                continue
            await reporter.update()
        meta = await task
    except Exception as exc:  # noqa: BLE001 - surface any failure back into Slack
        log.exception("headless run failed for thread=%s", thread_ts)
        progress.errors.append(f"{type(exc).__name__}: {exc}")
        if not task.done():
            # Do not leave the engine running with nothing reporting on it.
            await asyncio.to_thread(run.cancel)
            try:
                await task
            except Exception:  # noqa: BLE001 - already reporting a failure
                pass
    finally:
        HEADLESS.pop(thread_ts, None)
        ANSWERED.discard(thread_ts)

    status = meta.get("status", "failed")
    if posted:
        if status not in ("done", "cancelled"):
            await reporter.say(f":warning: Background work from this turn ended `{status}`. See the trace.")
        return status in ("done", "cancelled")
    answered = bool(progress.final.strip())
    await reporter.finish(status, answered)
    if answered:
        await reporter.say(progress.final)
    elif status != "done":
        await reporter.say(":warning: The run ended without an answer. See the trace.")
    return status == "done"


# -------------------------------------------------------------------- herdr


class LiveThread:
    """Bridges one interactive herdr agent and one Slack thread."""

    def __init__(self, client, channel: str, thread_ts: str):
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.irun: Optional[InteractiveRun] = None
        self.reporter: Optional[Reporter] = None
        self.task = asyncio.create_task(self._consume())

    def on_event(self, event: dict) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    async def _new_turn(self) -> None:
        if self.reporter:
            await self.reporter.update(force=True)  # a prompt arrived mid-turn; leave the old message as it is
        meta = self.irun.run.meta()
        progress = Progress(meta["engine"], "herdr", "interactive", meta["id"], DASHBOARD_URL)
        self.reporter = Reporter(self.client, self.channel, self.thread_ts, progress)
        await self.reporter.post()

    async def _consume(self) -> None:
        while True:
            event = await self.queue.get()
            while self.irun is None:  # start() emits the first prompt before it returns the run
                await asyncio.sleep(0.05)
            try:
                await self._handle(event)
            except Exception:  # noqa: BLE001 - keep consuming
                log.exception("live event handling failed")
            if event["kind"] == "status" and (event.get("data") or {}).get("status") == "closed":
                LIVE.pop(self.thread_ts, None)
                return

    async def _handle(self, event: dict) -> None:
        state = (event.get("data") or {}).get("status")
        if event["kind"] == "prompt" and (event.get("data") or {}).get("via") == "elsewhere":
            # Someone replied from the dashboard or the terminal: keep the thread readable.
            quoted = "\n".join("> " + line for line in (event.get("body") or "").splitlines()[:12])
            await self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts,
                text=clip(f":speech_balloon: _Sent from the dashboard or terminal:_\n{quoted}"),
            )
        restart = (event.get("data") or {}).get("auto_restart")
        if restart is not None:
            # neo is about to send "continue" for an API error, or has stopped trying.
            icon = ":warning:" if restart == "exhausted" else ":arrows_counterclockwise:"
            await self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text=clip(f"{icon} _{event.get('body', '')}_"),
            )
            return
        if self.reporter is None and event["kind"] == "status" and state not in ("working", "blocked"):
            # Between turns: no progress message to update.
            if state == "closed":
                await self.client.chat_postMessage(
                    channel=self.channel, thread_ts=self.thread_ts, text=":wave: Live session closed."
                )
            return
        if event["kind"] == "prompt" or self.reporter is None:
            await self._new_turn()
            if event["kind"] == "prompt":
                return
        progress = self.reporter.progress
        progress.add(event)
        if event["kind"] != "status":
            await self.reporter.update()
            return

        data = event.get("data") or {}
        if state == "blocked":
            await self.reporter.update(force=True)
            await self.reporter.say(
                ":raised_hand: *The agent is waiting for you.*\n"
                + code_block(event.get("body", ""))
                + "\nReply `@neo approve`, `@neo deny`, or `@neo keys <key> ...` "
                "(for example `keys 2 enter`), or attach: `herdr session attach "
                + (self.irun.herdr.session if self.irun else "neo") + "`"
            )
        elif state in ("done", "idle") and (data.get("turn_completed") or data.get("previous") in ("working", "blocked", "unknown")):
            final = data.get("last_text") or progress.final
            await self.reporter.finish("done", bool(final.strip()))
            if final.strip():
                await self.reporter.say(final)
            self.reporter = None  # the next event starts a new turn message
        elif state == "closed":
            await self.reporter.finish("done", True)
            await self.reporter.say(":wave: Live session closed.")
            self.reporter = None
        else:
            await self.reporter.update(force=True)


async def start_live(client, channel: str, thread_ts: str, req, prompt: str) -> bool:
    engine = req.engine or default_engine()
    cwd = req.cwd or WORKDIR
    live = LiveThread(client, channel, thread_ts)
    LIVE[thread_ts] = live
    label = f"slack-{thread_ts.split('.')[0][-6:]}"
    system = await asyncio.to_thread(agent_brief, cwd)
    try:
        live.irun = await asyncio.to_thread(
            InteractiveRun.start,
            engine,
            prompt,
            cwd,
            label,
            "slack",
            {"slack_channel": channel, "slack_thread": thread_ts, "slack_url": thread_url(channel, thread_ts)},
            Herdr(),
            live.on_event,
            (),
            system,
        )
    except Exception as exc:  # noqa: BLE001 - HerdrError, ValueError, OSError, ...
        log.exception("live start failed for thread=%s", thread_ts)
        LIVE.pop(thread_ts, None)
        live.task.cancel()
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=clip(f":x: Could not start the live agent:\n{code_block(str(exc), 2000)}"))
        return False
    meta = live.irun.run.meta()
    await sessions.set_thread(thread_ts, {"run_id": meta["id"], "engine": engine, "runtime": "herdr"})
    remote = await asyncio.to_thread(live.irun.remote_url, 8)
    links = []
    link = agent_link(meta["herdr_session"], meta["herdr_pane"])
    if link:
        links.append(f"<{link}|dashboard>")
    if remote:
        links.append(f"<{remote}|Claude app>")
    links.append(f"terminal: `herdr session attach {meta['herdr_session']}`")
    await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            f":computer: Live *{engine}* agent `{meta['herdr_agent']}` started in `{meta['cwd']}`.\n"
            f"Take over: " + "  ·  ".join(links)
        ),
    )
    return True


async def live_for(client, channel: str, thread_ts: str, entry: Optional[dict]) -> Optional[LiveThread]:
    """The thread's live controller, re-attaching to herdr after a bot restart."""
    async with _LIVE_LOCK:
        live = LIVE.get(thread_ts)
        if live:
            if live.irun and not live.irun.alive and live.irun.run.meta().get("status") != "closed":
                live.irun.watch()  # the watcher stopped; pick up where it left off
            return live
        if not entry or entry.get("runtime") != "herdr":
            return None
        run = Run(entry["run_id"])
        if run.meta().get("status") == "closed":
            return None
        live = LiveThread(client, channel, thread_ts)
        LIVE[thread_ts] = live
        live.irun = await asyncio.to_thread(InteractiveRun.attach, entry["run_id"], None, live.on_event)
        return live


# ------------------------------------------------------------------ auto RCA
# Channels where every new alert gets a Claude RCA in its thread (comma-separated channel ids).
# Off unless set: the bot posts in those channels. The bot must be a member of each.
# New posts arrive as `message` events (manifest: message.channels / message.groups); a poll of
# conversations.history every AUTO_RCA_POLL_SECONDS catches anything missed while disconnected.
AUTO_RCA_CHANNELS = [c.strip() for c in os.environ.get("NEO_AUTO_RCA_CHANNELS", "").split(",") if c.strip()]
AUTO_RCA_DAILY_MAX = int(os.environ.get("NEO_AUTO_RCA_DAILY_MAX", "10"))
AUTO_RCA_POLL_SECONDS = 300
AUTO_RCA_PROMPT = "An alert just arrived in this thread. Find the root cause (follow your RCA method) and say whether anyone needs to act."
# Channels where an alert should be fixed, not only explained. Their prompt is prompts/auto-fix.md.
AUTO_FIX_CHANNELS = [c.strip() for c in os.environ.get("NEO_AUTO_FIX_CHANNELS", "").split(",") if c.strip()]
AUTO_FIX_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "auto-fix.md")


def _alert_prompt(channel: str) -> str:
    if channel in AUTO_FIX_CHANNELS:
        try:
            with open(AUTO_FIX_PROMPT_FILE) as fh:
                return fh.read().strip()
        except OSError:
            log.warning("auto fix prompt %s missing; falling back to RCA only", AUTO_FIX_PROMPT_FILE)
    return AUTO_RCA_PROMPT
ALERT_GATE = AlertGate(str(cos_home() / "auto_rca.json"), AUTO_RCA_DAILY_MAX)


async def maybe_auto_rca(client, channel: str, msg: dict, via: str) -> None:
    text = _message_text(msg)
    skip = ALERT_GATE.admit(channel, msg, text, BOT_USER_ID)
    if skip:
        if skip not in ("already handled", "not a new top-level post"):
            log.info("auto RCA skipped %s in %s (%s): %s", msg.get("ts"), channel, via, skip)
        return
    if pause.is_paused():  # the alert still shows on the dashboard; no RCA is posted into its thread
        log.info("auto RCA skipped %s in %s (%s): notifications paused", msg["ts"], channel, via)
        ALERT_GATE.release(text)
        chief.alert_loop(channel, msg["ts"], text, thread_url(channel, msg["ts"]))
        return
    prompt = _alert_prompt(channel)
    log.info("auto %s for alert %s in %s (%s)", "fix" if prompt is not AUTO_RCA_PROMPT else "RCA", msg["ts"], channel, via)
    req = commands.parse(prompt)
    transcript = await _thread_transcript(client, channel, msg["ts"], "")
    asyncio.create_task(run_headless(client, channel, msg["ts"], None, req, _with_context(transcript, prompt),
                                    kind="alert-rca"))
    chief.alert_loop(channel, msg["ts"], text, thread_url(channel, msg["ts"]))


async def auto_rca(client) -> None:
    """Backstop poll: picks up alerts whose `message` event never reached us."""
    if not AUTO_RCA_CHANNELS:
        return
    log.info("auto RCA on for %s (daily max %d)", AUTO_RCA_CHANNELS, AUTO_RCA_DAILY_MAX)
    while True:
        try:
            for channel in AUTO_RCA_CHANNELS:
                oldest = ALERT_GATE.cursor(channel)
                if oldest is None:  # first run: start from now, don't RCA the whole backlog
                    ALERT_GATE.set_cursor(channel, f"{time.time():.6f}")
                    continue
                resp = await client.conversations_history(channel=channel, oldest=oldest, limit=50)
                for msg in sorted(resp.get("messages", []), key=lambda m: float(m["ts"])):
                    ALERT_GATE.set_cursor(channel, msg["ts"])
                    await maybe_auto_rca(client, channel, msg, "poll")
                # Status posts that were still running: their final state arrives as an edit.
                for ts in ALERT_GATE.pending_ts(channel):
                    one = await client.conversations_history(channel=channel, latest=ts, oldest=ts, inclusive=True, limit=1)
                    for msg in one.get("messages", []):
                        await maybe_auto_rca(client, channel, msg, "poll-edit")
            ALERT_GATE.save()
        except Exception:  # noqa: BLE001 - keep watching
            log.exception("auto RCA pass failed")
        await asyncio.sleep(AUTO_RCA_POLL_SECONDS)


# ------------------------------------------------------------------ monitor


def agent_link(session: str, pane_id: str) -> Optional[str]:
    if not DASHBOARD_URL:
        return None
    return f"{DASHBOARD_URL.rstrip('/')}/#/agent/{quote(session, safe='')}/{quote(pane_id, safe='')}"


def _notification(row: dict, kind: str, screen: str = "") -> str:
    where = f"`{row['repo']}` · {row['engine']}"
    links = []
    link = agent_link(row["session"], row["pane_id"])
    if link:
        links.append(f"<{link}|Open in dashboard>")
    if row.get("remote_url"):
        links.append(f"<{row['remote_url']}|Open in Claude app>")
    tail = ("\n" + "  ·  ".join(links)) if links else ""
    if NOTIFY_CHANNEL:
        tail += "\n_Answer in this thread: `@neo <message>`, `@neo approve` / `deny` / `stop`._"
    else:
        tail += "\n_Answer in this thread: any message, or `approve` / `deny` / `esc`._"
    if kind == "blocked":
        return f":raised_hand: *{row['title']}* is waiting for approval ({where}){tail}\n{code_block(screen, 1200)}"
    last = row.get("last_text") or ""
    last = ("…" + last[-600:]) if len(last) > 600 else last
    quoted = "\n".join("> " + line for line in last.splitlines() if line.strip())
    if kind == "asks":
        return f":speech_balloon: *{row['title']}* asked you something ({where}){tail}\n{quoted}"
    return f":white_check_mark: *{row['title']}* finished ({where}){tail}\n{quoted}"


async def monitor_agents(client) -> None:
    """DM the owner when any herdr agent on this machine needs them or finishes a long task.

    Covers agents you started yourself, not just the bot's. Agents that belong to a Slack
    thread are skipped: that thread already gets their updates.
    """
    if not (NOTIFY_ENABLED and NOTIFY_USER):
        log.info("agent notifications off (set ALLOWED_USER_IDS or NEO_NOTIFY_USER)")
        return
    previous: Optional[dict[str, dict]] = None
    # Approvals and questions that came up while notifications were paused, sent once the pause
    # ends if the agent is still waiting: key -> "blocked" | "asks"
    held: dict[str, str] = {}
    working_since: dict[str, float] = {}
    stuck_told: set[str] = set()
    while True:
        try:
            rows, complete = await asyncio.to_thread(fleet.agents_checked)
            if not complete:
                await asyncio.sleep(MONITOR_SECONDS)
                continue  # a partial listing would look like agents vanishing and coming back
            current = {f"{r['session']}/{r['pane_id']}": r for r in rows}
            now = time.monotonic()
            for key, row in current.items():
                try:
                    old = (previous or {}).get(key)
                    if row["status"] in ("working", "unknown") and (not old or old["status"] not in ("working", "unknown")):
                        working_since[key] = now
                    if row["status"] in ("working", "unknown") and row.get("last_active"):
                        quiet = time.time() - row["last_active"]
                        episode = f"{key}@{row.get('seq')}"
                        if (quiet > STUCK_MINUTES * 60 and episode not in stuck_told and previous is not None
                                and not pause.is_paused()):  # told after the pause if it is still stuck
                            stuck_told.add(episode)
                            await client.chat_postMessage(channel=NOTIFY_CHANNEL or NOTIFY_USER, text=clip(
                                f"{'<@' + NOTIFY_USER + '> ' if NOTIFY_CHANNEL else ''}:hourglass: *{row['title']}* has shown no activity for "
                                f"{int(quiet // 60)} minutes while working (`{row['repo']}`). It may be stuck."
                                + (f"\n<{agent_link(row['session'], row['pane_id'])}|Open it>" if agent_link(row['session'], row['pane_id']) else "")))
                    if previous is None or row.get("slack_thread"):
                        continue  # first pass only learns the current state
                    follow = FOLLOW.get(key)
                    if follow and row["status"] == "blocked" and (not old or old["status"] != "blocked"):
                        screen = await asyncio.to_thread(Herdr(row["session"]).read, row["pane_id"], 14, "detection")
                        await client.chat_postMessage(channel=follow[0], thread_ts=follow[1], text=clip(
                            f":raised_hand: Waiting for approval. `@neo approve` / `@neo deny`\n{code_block(screen, 1200)}"))
                        continue
                    if follow and row["status"] in ("idle", "done"):
                        # You answered this agent from a thread: its next answer goes back there. herdr can
                        # report "done" a moment before the transcript has the new text, so wait for it.
                        fresh = row.get("last_text") and row["last_text"] != follow[2]
                        follow[3] += 1
                        if fresh or follow[3] > 6:
                            FOLLOW.pop(key)
                            text = row["last_text"] if fresh else "_(finished without a new message)_"
                            await post_markdown(client, follow[0], follow[1], text)
                            working_since.pop(key, None)
                        continue
                    message = None
                    ready = row["status"] in ("idle", "done")
                    # A turn ended: we saw it working, or herdr's change counter moved while we were not
                    # looking (a turn shorter than one poll).
                    finished = bool(old) and ready and (
                        old["status"] in ("working", "blocked", "unknown") or old.get("seq") != row.get("seq"))
                    wants = (row["status"] == "blocked" and (not old or old["status"] != "blocked")) or finished
                    if wants and row["engine"] == "claude" and not row.get("remote_url"):
                        # The Claude app link works from a phone even when the dashboard is not reachable.
                        row["remote_url"] = await asyncio.to_thread(fleet.remote_url, row["session"], row["pane_id"])
                    if row["status"] == "blocked" and (not old or old["status"] != "blocked"):
                        screen = await asyncio.to_thread(Herdr(row["session"]).read, row["pane_id"], 14, "detection")
                        message = _notification(row, "blocked", screen)
                    elif finished:
                        worked = now - working_since.pop(key, now)
                        if row["asks"]:
                            message = _notification(row, "asks")
                        elif worked >= NOTIFY_MIN_WORK_SECONDS:
                            message = _notification(row, "done")
                    paused = pause.is_paused()
                    if message and paused:
                        log.info("agent notification for %s held back: notifications paused", key)
                        if row["status"] == "blocked" or row["asks"]:
                            held[key] = "blocked" if row["status"] == "blocked" else "asks"
                        message = None
                    elif not message and not paused and key in held:
                        kind = held.pop(key)
                        if kind == "blocked" and row["status"] == "blocked":
                            screen = await asyncio.to_thread(Herdr(row["session"]).read, row["pane_id"], 14, "detection")
                            message = _notification(row, "blocked", screen)
                        elif kind == "asks" and ready and row["asks"]:
                            message = _notification(row, "asks")
                    if message:
                        target = {"runtime": "agent", "session": row["session"], "pane_id": row["pane_id"]}
                        if NOTIFY_CHANNEL:
                            resp = await client.chat_postMessage(channel=NOTIFY_CHANNEL, text=clip(f"<@{NOTIFY_USER}> {message}"))
                            await sessions.set_thread(resp["ts"], target)  # `@neo ...` replies route to the agent
                        else:
                            resp = await client.chat_postMessage(channel=NOTIFY_USER, text=clip(message))
                            await sessions.set_thread(f"dm:{resp['ts']}", target)  # DM replies (see handle_dm)
                except Exception:  # noqa: BLE001 - one agent's failure must not re-notify the others
                    log.exception("agent monitor failed for %s", key)
            for gone in set(held) - set(current):
                held.pop(gone)
            previous = current
        except Exception:  # noqa: BLE001 - keep monitoring
            log.exception("agent monitor pass failed")
        await asyncio.sleep(MONITOR_SECONDS)


async def follow_agent(entry: dict, channel: str, thread_ts: str, action: str) -> None:
    """Remember where to post this agent's next answer. Read its last words *before* sending,
    so a fast reply is not mistaken for the old one."""
    if action not in ("prompt", "approve", "deny"):
        return
    before = next((r.get("last_text") for r in await asyncio.to_thread(fleet.agents)
                   if r["session"] == entry["session"] and r["pane_id"] == entry["pane_id"]), "")
    FOLLOW[f"{entry['session']}/{entry['pane_id']}"] = [channel, thread_ts, before, 0]
    log.info("following %s/%s for thread %s", entry["session"], entry["pane_id"], thread_ts)


async def brief_scheduler(client) -> None:
    """Write the morning brief once a day at BRIEF_AT and post it where notifications go.
    If the Mac mini was asleep or restarting at that minute, it catches up the same day."""
    if BRIEF_AT == "off" or not (NOTIFY_CHANNEL or NOTIFY_USER):
        return
    try:
        hour, minute = (int(x) for x in BRIEF_AT.split(":"))
    except ValueError:
        log.warning("NEO_BRIEF_AT must be HH:MM, got %r", BRIEF_AT)
        return
    loop = asyncio.get_running_loop()

    def posted(result: dict) -> None:
        if pause.is_paused():
            log.info("morning brief written but not posted: notifications paused")
            return
        text = (result or {}).get("text") or "The brief could not be written; open the run in History."
        link = f"\n\n[Open the dashboard]({DASHBOARD_URL})" if DASHBOARD_URL else ""
        mention = f"<@{NOTIFY_USER}> " if NOTIFY_CHANNEL and NOTIFY_USER else ""
        asyncio.run_coroutine_threadsafe(
            post_markdown(client, NOTIFY_CHANNEL or NOTIFY_USER, None, f"{mention}**Morning brief**\n\n{text}{link}"), loop)

    while True:
        try:
            now = datetime.now()
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now.weekday() in BRIEF_DAYS and now >= due:
                today = now.strftime("%Y-%m-%d")
                already = any(m.get("kind") == "brief" and m.get("source") == "schedule" and m["created_at"][:10] >= today
                              for m in list_runs(60))
                if not already and not pause.is_paused():  # held back while paused; written if the pause ends today
                    log.info("writing the morning brief")
                    await asyncio.to_thread(morning.start, "schedule", posted)
        except Exception:  # noqa: BLE001 - try again next minute
            log.exception("brief scheduler failed")
        await asyncio.sleep(60)


def finding_link(fid: str) -> str:
    return f"{DASHBOARD_URL}/#/finding/{fid}" if DASHBOARD_URL else ""


def findings_markdown(rows: list[dict]) -> str:
    lines = []
    for i, f in enumerate(rows, 1):
        repo = f" ({f['repo'].split('/')[-1]})" if f.get("repo") else ""
        what = " ".join((f.get("what") or "").split())
        what = what if len(what) <= 240 else what[:239] + "…"
        links = [f"[source]({f['link']})"] if f.get("link", "").startswith("http") else []
        if DASHBOARD_URL:
            links.append(f"[details]({finding_link(f['id'])})")
        lines.append(f"{i}. **[{f['severity'].capitalize()}] {f['title']}**{repo}. {what} {' '.join(links)}")
    return "\n".join(lines)


def is_owner(user: Optional[str]) -> bool:
    """Only the people the bot works for may change what every later agent is told."""
    return bool(user) and (user in ALLOWED_USER_IDS or user == NOTIFY_USER)


async def handle_memory(client, channel: str, thread_ts: str, user: Optional[str], req) -> None:
    async def say(text: str) -> None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=clip(text))

    if not is_owner(user):
        await say(":lock: Only the people I work for (ALLOWED_USER_IDS) can read or change my memory.")
        return
    if req.command == "memory":  # memory can hold internal details: never post it in a shared channel
        await post_markdown(client, user, None, await asyncio.to_thread(memory_digest))
        if channel != user and not channel.startswith("D"):
            await say(":brain: Sent you my memory in a DM.")
        return
    if req.command == "apply":
        await say(await asyncio.to_thread(autonomy.apply, autonomy.parse_apply(req.prompt), thread_ts))
        return
    where = await asyncio.to_thread(learn.add, req.prompt, "rule", req.repo, f"slack:{channel}/{thread_ts}")
    if not where:
        await say(":brain: I already remember that (or there was nothing to remember).")
        return
    note = ""
    if req.repo and where.startswith("repos/") and not await asyncio.to_thread(_known_repo, req.repo):
        note = f"\n_I don't see a repository named {req.repo} under {WORKDIR}; I'll use this note when an agent works in one with that name._"
    await say(f":brain: Saved to `{where}`. Every agent I start from now on will know it.{note}")


async def route_apply(req, thread_ts: str):
    """`apply 1 3` is for the autonomy review's thread only; anywhere else it is a reply to that thread's
    agent (e.g. picking from the fixes it offered)."""
    if req.command == "apply" and not await asyncio.to_thread(autonomy.is_review_thread, thread_ts):
        return commands.Request("run", prompt=req.prompt)
    return req


def _known_repo(name: str) -> bool:
    root = Path(WORKDIR)
    return any(p.name == name for p in [root, *root.glob("*"), *root.glob("*/*")] if (p / ".git").exists())


def memory_digest() -> str:
    files = learn.all_files()
    if not files:
        return ("I don't remember anything yet. I learn from mistakes in sessions you run and from what you tell me: "
                "`@neo remember <rule>`.")
    out = [f"My memory ({learn.memory_dir()}), newest last. Edit or delete lines there any time."]
    for f in files:
        out.append(f"\n*{f['name']}*")
        out += [f"• {e['text']}" for e in f["entries"][-15:]]
        if len(f["entries"]) > 15:
            out.append(f"_…and {len(f['entries']) - 15} older_")
    return "\n".join(out)


def watches_digest() -> str:
    rows = watches.open_findings()
    active = [w["name"] for w in watches.load() if w.get("enabled")]
    head = f"Watching: {', '.join(active) or 'nothing (add watches in the dashboard, Work tab)'}."
    if not rows:
        return f"{head}\n\nNothing open."
    return (f"{head}\n\n**Open findings**\n{findings_markdown(rows[:15])}\n\n"
            "Reply `@neo live fix 2` (or any instruction) in this thread to start an agent on one.")


def watch_checked(client, loop):
    """Post what a watch found: new problems (not low severity), or that a scheduled check broke."""
    def notify(watch: dict, meta: dict, new: list[dict]) -> None:
        chief.finding_loops(new)
        where = NOTIFY_CHANNEL or NOTIFY_USER
        if not where:
            return
        if pause.is_paused():
            log.info("watch %s findings not posted: notifications paused (still on the dashboard)", watch["id"])
            return
        mention = f"<@{NOTIFY_USER}> " if NOTIFY_CHANNEL and NOTIFY_USER else ""
        if meta["status"] != "done":
            earlier = [m for m in list_runs(300) if m.get("kind") == "watch" and m.get("watch_id") == watch["id"] and m["id"] < meta["id"]]
            if meta.get("source") != "schedule" or (earlier and earlier[0]["status"] != "done"):
                return  # say it once, not every time it keeps failing
            link = f" [Open the run]({DASHBOARD_URL}/#{meta['id']})." if DASHBOARD_URL else ""
            text = f"{mention}The **{watch['name']}** watch could not finish its check ({meta['status']}).{link}"
        else:
            worth = [f for f in new if f["severity"] != "low"]
            if not worth:
                return
            noun = "problem" if len(worth) == 1 else "problems"
            text = (f"{mention}**{watch['name']}** found {len(worth)} new {noun}\n\n{findings_markdown(worth)}\n\n"
                    "Reply `@neo live fix 1` (or any instruction) in this thread to start an agent on one.")
        asyncio.run_coroutine_threadsafe(post_markdown(client, where, None, text), loop)
    return notify


async def held_scheduler(client) -> None:
    """Once notifications are back on, post what the scheduled ~/.neo jobs held back while paused."""
    while True:
        try:
            if not pause.is_paused():
                for item in await asyncio.to_thread(pause.take_held):
                    try:
                        await client.chat_postMessage(channel=item["channel"], thread_ts=item.get("thread_ts") or None,
                                                      text=clip(f"_(held while notifications were paused)_\n{item['text']}"))
                    except Exception as exc:  # noqa: BLE001 - keep it for the next pass, post the others
                        try:
                            kept = await asyncio.to_thread(pause.hold_again, item)
                        except OSError:
                            log.exception("held message for %s could not be put back", item["channel"])
                            continue
                        if kept:
                            log.warning("held message for %s not posted (%s); trying again next minute", item["channel"], exc)
                        else:
                            log.error("held message for %s dropped after %d failed posts: %s",
                                      item["channel"], pause.MAX_HELD_ATTEMPTS, exc)
        except Exception:  # noqa: BLE001 - try again next minute
            log.exception("posting held messages failed")
        await asyncio.sleep(60)


async def watch_scheduler(client) -> None:
    """Start every watch whose time has come (see cos/watches.py). Checked once a minute."""
    watches.on_checked = watch_checked(client, asyncio.get_running_loop())
    while True:
        try:
            for watch in await asyncio.to_thread(watches.due):
                log.info("watch %s is due", watch["id"])
                await asyncio.to_thread(watches.start, watch["id"], "schedule")
        except Exception:  # noqa: BLE001 - try again next minute
            log.exception("watch scheduler failed")
        await asyncio.sleep(60)


async def answer_agent(client, channel: str, thread_ts: str, ts: str, entry: dict, req, raw: str) -> None:
    """`@neo ...` in a notification thread: send it to the agent that raised the notification."""
    action = {"approve": "approve", "deny": "deny", "stop": "interrupt"}.get(req.command, "prompt")
    if req.command == "keys":
        action = "keys"
    elif req.command == "status":
        detail = await asyncio.to_thread(fleet.agent_detail, entry["session"], entry["pane_id"])
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=clip(
            f"`{detail['status']}` · {detail['title']}\n{code_block(detail['screen'][-1500:])}"))
        return
    try:
        await follow_agent(entry, channel, thread_ts, action)
        await asyncio.to_thread(fleet.act, entry["session"], entry["pane_id"], action, raw, req.keys)
        await _react(client, channel, ts, "white_check_mark")
    except (HerdrError, ValueError) as exc:
        hint = " It is waiting for approval: `@neo approve` or `@neo deny`." if getattr(exc, "code", "") == "agent_blocked" else ""
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: `{exc}`.{hint}")


@app.event("message")
async def handle_dm(event: dict, client) -> None:
    """A reply in the thread of a notification DM goes to that agent; a new post in an
    auto RCA channel gets an RCA.

    Needs the Slack app to receive `message.im` (scope im:history) and, for auto RCA,
    `message.channels` / `message.groups`; see manifest.json.
    """
    if event.get("channel") in AUTO_RCA_CHANNELS:
        if event.get("subtype") == "message_changed" and event.get("message"):
            # A status post edited in place (e.g. "running" -> "failed"): judge its new text.
            await maybe_auto_rca(client, event["channel"], event["message"], "edit")
        else:
            await maybe_auto_rca(client, event["channel"], event, "event")
        return
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return
    user = event.get("user")
    if ALLOWED_USER_IDS and user not in ALLOWED_USER_IDS:
        return
    channel, text = event["channel"], _clean_prompt(event.get("text", ""))
    thread_ts = event.get("thread_ts")
    target = await sessions.get_thread(f"dm:{thread_ts}") if thread_ts else None
    if not target:
        await client.chat_postMessage(
            channel=channel, thread_ts=event["ts"],
            text="Reply in the thread of an agent notification to answer that agent. "
                 "To start work, mention me in a channel: `@neo <task>`.",
        )
        return
    # Only explicit control words; "yes"/"no" are ordinary answers to a question.
    action = {"approve": "approve", "deny": "deny", "esc": "interrupt", "stop": "interrupt"}.get(text.lower().strip(), "prompt")
    try:
        await follow_agent(target, channel, thread_ts, action)
        await asyncio.to_thread(fleet.act, target["session"], target["pane_id"], action, text)
        await _react(client, channel, event["ts"], "white_check_mark")
    except (HerdrError, ValueError) as exc:
        hint = " It is waiting for approval: reply `approve` or `deny`." if getattr(exc, "code", "") == "agent_blocked" else ""
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: `{exc}`.{hint}")


# ----------------------------------------------------------------- controls


async def handle_control(client, channel: str, thread_ts: str, entry: Optional[dict], req) -> None:
    async def say(text: str) -> None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=clip(text))

    if req.command == "help":
        await say(commands.HELP + (f"\n*Dashboard*: <{DASHBOARD_URL}|everything running on this machine>" if DASHBOARD_URL else ""))
        return
    if req.command == "default":
        if req.engine:
            await asyncio.to_thread(settings.save, default_engine=req.engine)
            await say(f"From now on I use *{req.engine}* when you don't name an agent.")
        else:
            await say(f"I use *{default_engine()}* when you don't name an agent. Change it with `@neo default claude` or `@neo default codex`.")
        return
    if req.command == "watches":
        await post_markdown(client, channel, thread_ts, await asyncio.to_thread(watches_digest))
        return
    if req.command == "pause":
        try:
            seconds = pause.parse_duration(req.prompt) if req.prompt else None
        except ValueError:
            await say("Say `@neo pause` (until `@neo resume`), or a length like `@neo pause 30m`, `2h`, `1d`.")
            return
        await asyncio.to_thread(pause.pause, seconds)
        await say(f":zipper_mouth_face: {pause.describe()} I still answer when you mention me. "
                  "`@neo resume` to turn them back on.")
        return
    if req.command == "resume":
        was = await asyncio.to_thread(pause.resume)
        await say(":bell: Notifications are back on." if was else "Notifications were not paused.")
        return

    live = await live_for(client, channel, thread_ts, entry)
    if live and live.irun:
        irun = live.irun
        try:
            if req.command == "stop":
                await asyncio.to_thread(irun.interrupt)
                await say(":no_entry_sign: Sent interrupt (Esc).")
            elif req.command == "approve":
                await asyncio.to_thread(irun.approve)
                await say(":white_check_mark: Approved (Enter).")
            elif req.command == "deny":
                await asyncio.to_thread(irun.deny)
                await say(":no_entry_sign: Denied (Esc).")
            elif req.command == "keys":
                if not req.keys:
                    await say("Usage: `@neo keys <key> [<key> ...]`, for example `@neo keys 2 enter`.")
                    return
                await asyncio.to_thread(irun.keys, *req.keys)
                await say(f"Sent keys: `{' '.join(req.keys)}`")
            elif req.command == "close":
                await asyncio.to_thread(irun.close)
                await sessions.clear_thread(thread_ts)
            elif req.command == "status":
                state = await asyncio.to_thread(irun.status)
                screen = await asyncio.to_thread(irun.screen, 20)
                await say(f"Live agent `{irun.name}` is `{state}`.\n{code_block(screen)}")
        except HerdrError as exc:
            await say(f":x: herdr: `{exc}`")
        return

    run = HEADLESS.get(thread_ts)
    if req.command == "stop":
        if run:
            await asyncio.to_thread(run.cancel)
            await say(":no_entry_sign: Stopping the run.")
        else:
            await say("Nothing is running in this thread.")
    elif req.command == "status":
        if entry and entry.get("run_id"):
            meta = Run(entry["run_id"]).meta()
            link = f" · <{DASHBOARD_URL}/#{meta['id']}|full trace>" if DASHBOARD_URL else ""
            await say(
                f"Run `{meta['id']}`: *{meta['engine']}* {meta.get('runtime', 'headless')} · `{meta['status']}` · "
                f"{meta.get('turns', 0)} turn(s) · ${meta.get('cost_usd') or 0}{link}"
            )
        else:
            await say("No run in this thread yet.")
    else:
        await say(f"`{req.command}` only works on a live (herdr) run. Start one with `@neo live claude <task>`.")


# -------------------------------------------------------------------- entry


async def _end_background(thread_ts: str, wait_seconds: float = 30.0) -> bool:
    """A new message arrived after the last turn was answered but while its background work
    (agents, long shells) still runs. The answer is out, so the new message wins: stop the old
    process and wait for its turn to wind down. False if it would not stop in time."""
    run = HEADLESS.get(thread_ts)
    if run is not None:
        await asyncio.to_thread(run.cancel)
    deadline = time.monotonic() + wait_seconds
    while thread_ts in BUSY or thread_ts in HEADLESS:
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.2)
    return True  # no await after the last check: the caller claims the thread next


@app.event("app_mention")
async def handle_mention(event: dict, client) -> None:
    user = event.get("user")
    if ALLOWED_USER_IDS and user not in ALLOWED_USER_IDS:
        return  # Silently ignore anyone not on the allowlist.

    channel = event["channel"]
    # Reply inside the mention's thread; start a new thread on a top-level mention.
    thread_ts = event.get("thread_ts") or event["ts"]
    req = commands.parse(_clean_prompt(event.get("text", "")))
    entry = await sessions.get_thread(thread_ts)
    log.info("mention: user=%s channel=%s thread=%s command=%s engine=%s live=%s",
             user, channel, thread_ts, req.command, req.engine, req.live)

    req = await route_apply(req, thread_ts)
    if req.command in ("remember", "memory", "apply"):
        try:
            await handle_memory(client, channel, thread_ts, user, req)
        except Exception as exc:  # noqa: BLE001 - always answer in the thread
            log.exception("%s failed for thread=%s", req.command, thread_ts)
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: `{req.command}` failed: `{type(exc).__name__}: {exc}`")
        return

    if req.command == "resume" and entry and not pause.is_paused():
        req = commands.Request("run", prompt="resume")  # "carry on" for this thread's run
    if req.command in ("pause", "resume"):
        await handle_control(client, channel, thread_ts, entry, req)
        return

    if entry and entry.get("runtime") == "agent" and req.command != "help":
        await answer_agent(client, channel, thread_ts, event["ts"], entry, req, _clean_prompt(event.get("text", "")))
        return

    if req.command != "run":
        try:
            await handle_control(client, channel, thread_ts, entry, req)
        except Exception as exc:  # noqa: BLE001 - always answer in the thread
            log.exception("control %s failed for thread=%s", req.command, thread_ts)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=f":x: `{req.command}` failed: `{type(exc).__name__}: {exc}`"
            )
        return
    if not req.prompt:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=commands.HELP)
        return
    if req.cwd and not os.path.isdir(req.cwd):
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: Not a folder on this machine: `{req.cwd}`")
        return
    stopped_background = thread_ts in ANSWERED and await _end_background(thread_ts)
    if not stopped_background and (thread_ts in BUSY or thread_ts in HEADLESS):
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Still working on the previous message. `@neo stop` to interrupt it.",
        )
        return
    BUSY.add(thread_ts)  # no await between the check and this line
    if stopped_background:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=":scissors: Stopped the background work left from the last answer to take this message.",
        )

    await _react(client, channel, event["ts"], "eyes")
    ok = True
    try:
        live = await live_for(client, channel, thread_ts, entry)
        if live and live.irun and not req.live and not req.engine:
            # Follow-up in a live thread goes straight to the running agent.
            await asyncio.to_thread(live.irun.send, req.prompt)
        else:
            transcript = await _thread_transcript(client, channel, thread_ts, event["ts"])
            prompt = _with_context(transcript, req.prompt)
            if req.live:
                ok = await start_live(client, channel, thread_ts, req, prompt)
            else:
                ok = await run_headless(client, channel, thread_ts, entry, req, prompt)
    except HerdrError as exc:
        ok = False
        detail = "the agent is waiting for approval; use `@neo approve` or `@neo deny` first" if exc.code == "agent_blocked" else str(exc)
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=f":x: {detail}")
    except Exception as exc:  # noqa: BLE001 - surface any failure back into Slack
        ok = False
        log.exception("mention failed for thread=%s", thread_ts)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=f":x: Failed: `{type(exc).__name__}: {exc}`"
        )
    finally:
        BUSY.discard(thread_ts)

    await _react(client, channel, event["ts"], "eyes", remove=True)
    await _react(client, channel, event["ts"], "white_check_mark" if ok else "x")


async def resume_live_runs(client) -> None:
    """After a restart, keep recording every live run that is still open in herdr."""
    for meta in list_runs(200):
        if meta.get("runtime") != "herdr" or meta.get("status") in ("closed", "failed"):
            continue
        try:
            if meta.get("slack_thread") and meta.get("slack_channel"):
                entry = {"run_id": meta["id"], "runtime": "herdr"}
                await live_for(client, meta["slack_channel"], meta["slack_thread"], entry)
            else:
                RESUMED.append(await asyncio.to_thread(InteractiveRun.attach, meta["id"]))
        except Exception:  # noqa: BLE001 - one broken run must not stop the bot
            log.exception("could not resume live run %s", meta["id"])


def _age_hours(iso: Optional[str]) -> float:
    try:
        return (time.time() - datetime.fromisoformat(iso).timestamp()) / 3600
    except (TypeError, ValueError):
        return 0.0


_ADOPT_POLL_SECONDS = 3.0
_DEAD_ENGINE_GRACE_SECONDS = 15.0  # the dashboard's adopt thread updates status within this


async def finish_adopted_slack_runs(client) -> list:
    """Runs started from a Slack thread before a restart keep going (see Run.adopt); post their
    answer in the thread as usual. Like run_headless, an answer goes out when its result is
    written, not when background work the engine is still finishing ends. A run whose engine
    died with the old bot is closed out instead of being waited on forever."""
    pending = [m for m in list_runs(200, include_running=True) if m.get("runtime", "headless") == "headless" and m.get("status") == "running"
               and m.get("slack_thread") and m.get("slack_channel")]
    tasks = []
    for meta in pending:
        async def wait_and_post(meta=meta):
            run = Run(meta["id"])
            channel, thread = meta["slack_channel"], meta["slack_thread"]
            turn = meta.get("turns") or 1
            posted_seq = meta.get("slack_posted_seq")
            posted: list[str] = []
            if not pid_alive(meta.get("pid")) and _age_hours(meta.get("updated_at")) > 6:
                run.update_meta(status="failed", pid=None)  # long dead; the thread has moved on
                return
            offset, last_text, dead_since = 0, "", None
            while True:
                final = run.meta()
                with (run.dir / "events.jsonl").open() as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                complete = chunk[: chunk.rfind("\n") + 1]
                offset += len(complete.encode())
                for line in complete.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    body = (event.get("body") or "").strip()
                    if event.get("turn") != turn or not body:
                        continue
                    if event["kind"] == "text":
                        last_text = body
                    elif event["kind"] == "result" and posted_seq is not None and event.get("seq", -1) <= posted_seq:
                        posted.append(body)  # the old bot already posted this one
                    elif event["kind"] == "result" and body not in posted:
                        await post_markdown(client, channel, thread,
                                            body if not posted else ":hourglass: _Background work finished:_\n" + body)
                        posted.append(body)
                if final["status"] != "running":
                    break
                if pid_alive(final.get("pid")):
                    dead_since = None
                elif dead_since is None:
                    dead_since = time.monotonic()
                elif time.monotonic() - dead_since >= _DEAD_ENGINE_GRACE_SECONDS:
                    # Nobody is following this run (its engine died with the old bot): close it out.
                    final = run.update_meta(status="done" if posted else "failed", pid=None)
                    break
                await asyncio.sleep(_ADOPT_POLL_SECONDS)
            if not posted:
                text = last_text if final["status"] == "done" and last_text else f":warning: The run ended ({final['status']}). See the dashboard."
                await post_markdown(client, channel, thread, text)
        tasks.append(asyncio.create_task(wait_and_post()))
    return tasks


def _start_dashboard() -> None:
    if not DASHBOARD_PORT:
        return
    from cos.server import serve

    threading.Thread(target=serve, args=(DASHBOARD_HOST, int(DASHBOARD_PORT)), daemon=True, name="dashboard").start()


async def main() -> None:
    global BOT_USER_ID
    exit_with_parent()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if MODE not in MODES:
        raise SystemExit(f"NEO_MODE must be one of {MODES}")
    if default_engine() not in ENGINES:
        raise SystemExit(f"NEO_DEFAULT_ENGINE must be one of {ENGINES}")
    if not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty: anyone in the workspace can run agents on this machine")
    global TEAM_URL
    for attempt in range(60):  # at boot the network (DNS) can lag behind launchd
        try:
            auth = await app.client.auth_test()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("Slack not reachable yet (%s); retrying", type(exc).__name__)
            await asyncio.sleep(min(30, 2 + attempt))
    else:
        raise SystemExit("Slack unreachable")
    BOT_USER_ID = auth["user_id"]
    TEAM_URL = auth.get("url")
    _start_dashboard()
    log.info(
        "Starting Neo bot (bot=%s, engine=%s, mode=%s, workdir=%s, dashboard=%s, allowlist=%s)",
        BOT_USER_ID, default_engine(), MODE, WORKDIR, DASHBOARD_URL, sorted(ALLOWED_USER_IDS),
    )
    monitor = asyncio.create_task(monitor_agents(app.client))  # noqa: F841 - runs for the process lifetime
    briefs = asyncio.create_task(brief_scheduler(app.client))  # noqa: F841
    alerts = asyncio.create_task(auto_rca(app.client))  # noqa: F841
    watchers = asyncio.create_task(watch_scheduler(app.client))  # noqa: F841
    held = asyncio.create_task(held_scheduler(app.client))  # noqa: F841
    chief_jobs = chief.start(app.client, NOTIFY_CHANNEL, NOTIFY_USER, DASHBOARD_URL)  # noqa: F841
    RESUMED.extend(await finish_adopted_slack_runs(app.client))  # hold the tasks so they are not collected
    await resume_live_runs(app.client)
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
