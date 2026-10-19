"""Interactive runs: a real Claude Code / Codex TUI inside herdr, recorded like a headless run.

herdr keeps the agent alive in a terminal you can attach to from any machine.
This module adds what herdr does not keep: a structured timeline. A watcher
thread combines two sources into runs/<run_id>/events.jsonl:
  - herdr agent state (working / blocked / done / idle), polled once a second
  - the engine's own session transcript on disk, tailed for tool calls and text
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import conclusion
from .events import TRANSCRIPT_NORMALIZERS
from .herdr import Herdr, HerdrError, agent_session_id
from .runner import (
    AUTO_RESTART_DELAYS, AUTO_RESTART_PROMPT, CLAUDE_MODE_FLAGS, MAX_AUTO_RESTARTS_PER_PROMPT, MODES, Run,
    is_transient_engine_error,
)

log = logging.getLogger("cos.interactive")

POLL_SECONDS = 1.0
# Claude Remote Control prints this link; it opens the same live session in the Claude app.
RC_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]+")
# Transcript files already followed by a watcher in this process (Codex fallback lookup).
_CLAIMED: set[str] = set()
_CLAIMED_LOCK = threading.Lock()
READY_STATES = ("idle", "done")
# herdr reports a fresh Claude "idle" before its input box takes keystrokes; text typed in that
# gap is silently dropped. Wait this long for the session to settle before the first prompt...
STARTUP_SETTLE_SECONDS = 20.0
# ...then this long for proof the prompt arrived (the agent starts working, or the transcript
# records it), and retry a few times before giving up.
PROMPT_CONFIRM_SECONDS = 10.0
PROMPT_RETRIES = 2
# herdr state -> run status shown in meta / dashboard / Slack
STATUS_MAP = {"working": "running", "blocked": "blocked", "done": "waiting", "idle": "waiting", "unknown": "running"}


def agent_name_for(run_id: str) -> str:
    # herdr names: [a-z][a-z0-9_-]{0,31}; run ids are unique, so their tail is too.
    return "neo-" + run_id.replace("-", "")[-12:]


def find_transcript(engine: str, session_id: Optional[str], cwd: str, started_at: float) -> Optional[Path]:
    home = Path.home()
    if engine == "claude" and session_id:
        matches = glob.glob(str(home / ".claude" / "projects" / "*" / f"{session_id}.jsonl"))
        return Path(matches[0]) if matches else None
    if engine == "codex":
        pattern = str(home / ".codex" / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl")
        if session_id:
            for path in glob.glob(pattern):
                if path.endswith(f"{session_id}.jsonl"):
                    return Path(path)
        # herdr may not know the Codex thread id: take the newest rollout for this cwd.
        candidates = sorted(
            (p for p in glob.glob(pattern) if os.path.getmtime(p) >= started_at - 5),
            key=os.path.getmtime,
            reverse=True,
        )
        for path in candidates:
            with _CLAIMED_LOCK:
                if path in _CLAIMED:
                    continue  # another live run in the same folder already follows it
            try:
                with open(path) as fh:
                    first = json.loads(fh.readline())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(first, dict) and (first.get("payload") or {}).get("cwd") == cwd:
                return Path(path)
    return None


def engine_error_text(engine: str, raw: dict) -> Optional[str]:
    """The text of an API error the engine wrote to its transcript, or None for other records.
    Claude marks a request it gave up on with isApiErrorMessage; Codex logs an "error" event
    (its "stream_error" events are its own retries in progress, not a stop)."""
    if engine == "claude" and raw.get("type") == "assistant" and raw.get("isApiErrorMessage"):
        content = (raw.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
    if engine == "codex" and raw.get("type") == "event_msg" and (raw.get("payload") or {}).get("type") == "error":
        return str((raw.get("payload") or {}).get("message", ""))
    return None


class InteractiveRun:
    def __init__(self, run: Run, herdr: Herdr, on_event: Optional[Callable[[dict], None]] = None):
        self.run = run
        self.herdr = herdr
        self.on_event = on_event or (lambda _e: None)
        meta = run.meta()
        self.engine = meta["engine"]
        self.name = meta["herdr_agent"]
        self._sent_prompts: list[str] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status: Optional[str] = None
        self._transcript: Optional[Path] = Path(meta["transcript_path"]) if meta.get("transcript_path") else None
        self._offset = int(meta.get("transcript_offset") or 0)
        self._started_at = time.time()
        # Set when the engine's own transcript says a turn completed (Codex task_complete).
        self._turn_completed = False
        # A transient API error the agent stopped on, with nothing after it yet; and how many
        # times in a row we have sent "continue" for one (reset once a turn ends cleanly).
        self._engine_error: Optional[str] = None
        self._auto_restarts = 0
        self._restarts_since_human = 0  # never reset by progress: see MAX_AUTO_RESTARTS_PER_PROMPT
        self._gave_up = False
        self._restart_at: Optional[float] = None
        # send() runs on Slack's thread and on this watcher's: one prompt at a time into the pane.
        self._send_lock = threading.RLock()
        self._polled_ok = False  # set by _watch after each good herdr read; resets the failure count

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def start(
        cls,
        engine: str,
        prompt: str,
        cwd: str,
        label: str,
        source: str = "slack",
        extra: Optional[dict] = None,
        herdr: Optional[Herdr] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        agent_args: tuple[str, ...] = (),
        system: Optional[str] = None,
    ) -> "InteractiveRun":
        herdr = herdr or Herdr()
        herdr.ensure_server()
        # Same permission level as headless runs (NEO_MODE, default "full"): a session continued
        # from the phone must be able to edit and run commands just like one on the desktop.
        mode = os.environ.get("NEO_MODE", "full")
        if mode not in MODES:
            raise ValueError(f"NEO_MODE must be one of {MODES}")
        run = Run.create(engine, prompt, cwd, mode, source=source, runtime="herdr", extra=extra)
        name = agent_name_for(run.id)
        if engine == "claude":
            agent_args = (*CLAUDE_MODE_FLAGS[mode], *agent_args)
        if engine == "claude" and system:
            # An agent session (it has the brief): every turn ends with a Status line (cos/conclusion.py). As a
            # file, like the brief below. Without it the agent still starts; it just isn't held to a conclusion.
            try:
                agent_args = ("--settings", str(conclusion.write_settings(run.dir / "stop-hook-settings.json")), *agent_args)
            except OSError as exc:
                log.warning("stop hook settings not written for %s: %s", run.id, exc)
        if engine == "claude" and os.environ.get("NEO_REMOTE_CONTROL", "1") != "0":
            # Native Remote Control: continue this exact session from the Claude app.
            agent_args = ("--remote-control", name, *agent_args)
        if engine == "codex":
            # An "Update available" menu at startup would hold an unattended session forever.
            agent_args = ("-c", "check_for_update_on_startup=false", *agent_args)
            if mode == "full":  # as headless codex (runner.py): no approval prompts, no sandbox
                agent_args = ("--dangerously-bypass-approvals-and-sandbox", *agent_args)
        if system and engine == "claude":
            # From a file: herdr refuses to pass multi-line text through the pane's shell
            # ("agent arguments cannot be encoded safely"), and the agent would never start.
            brief = run.dir / "system-prompt.md"
            brief.write_text(system)
            agent_args = ("--append-system-prompt-file", str(brief), *agent_args)
        cwd_abs = run.meta()["cwd"]
        workspace = None
        try:
            workspace, pane = herdr.create_workspace(cwd_abs, label)
            run.update_meta(
                status="starting", herdr_session=herdr.session, herdr_agent=name,
                herdr_workspace=workspace, herdr_pane=pane, turns=0,
            )
            agent = cls._start_past_trust_prompt(herdr, name, engine, pane, agent_args)
        except HerdrError as exc:
            screen = ""
            if workspace:
                # Show what the agent was stuck on (a trust prompt, a login screen, ...).
                try:
                    proc = herdr._run(["pane", "read", pane, "--source", "visible", "--lines", "30"], timeout=10)
                    screen = "\n".join(l for l in proc.stdout.splitlines() if l.strip())[-1500:]
                except HerdrError:
                    screen = ""
                try:
                    herdr.close_workspace(workspace)  # do not leave an empty workspace behind
                except HerdrError:
                    pass
            detail = f"{exc}\n\nThe agent's screen when it gave up:\n{screen}" if screen else str(exc)
            run.append_event({"kind": "error", "title": "herdr failed", "body": detail, "data": {}}, 1)
            run.update_meta(status="failed")
            raise HerdrError(exc.code, detail) from exc
        run.update_meta(session_id=agent_session_id(agent))
        self = cls(run, herdr, on_event)
        self._started_at = time.time() - 2
        if system and engine == "codex":
            prompt = f"{system}\n\n---\n\n{prompt}"
        try:
            self._wait_until_ready()
            before = self._transcript_size()
            self.send(prompt)
            self._confirm_delivery(prompt, before)
        except HerdrError:
            self.close()  # the agent never got its task: do not leave it running in herdr
            raise
        self.watch()
        return self

    @staticmethod
    def _start_past_trust_prompt(herdr: Herdr, name: str, engine: str, pane: str, agent_args) -> dict:
        """Start the agent. If it stops on the CLI's one-time "do you trust this folder?" screen,
        accept it: an allowed user explicitly asked to run an agent in this folder (headless runs
        skip that screen already). Any other startup blocker is left for a human."""
        try:
            return herdr.start_agent(name, engine, pane, agent_args)
        except HerdrError as exc:
            if exc.code not in ("agent_not_ready", "timeout"):
                raise
            proc = herdr._run(["pane", "read", pane, "--source", "visible", "--lines", "40"], timeout=10)
            if not re.search(r"trust (this|the files in this) (folder|project|workspace)|project you trust", proc.stdout, re.I):
                raise
            # The highlighted default is "No, exit": move to "Yes, I trust this folder" first, and
            # confirm only when the screen shows that option selected.
            herdr._run(["pane", "send-keys", pane, "down"], timeout=15)
            time.sleep(0.5)
            screen = herdr._run(["pane", "read", pane, "--source", "visible", "--lines", "40"], timeout=10).stdout
            if not re.search(r"❯\s*Yes,? I trust", screen):
                raise
            herdr._run(["pane", "send-keys", pane, "enter"], timeout=15)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                try:
                    agent = herdr.get(pane)
                    if agent.get("agent_status") in ("idle", "done"):
                        herdr.call("agent", "rename", pane, name, timeout=15)
                        return herdr.get(name)
                except HerdrError:
                    pass
                time.sleep(1)
            raise

    @classmethod
    def attach(cls, run_id: str, herdr: Optional[Herdr] = None, on_event=None) -> "InteractiveRun":
        """Resume watching an existing run, for example after the bot restarts."""
        run = Run(run_id)
        meta = run.meta()
        herdr = herdr or Herdr(meta.get("herdr_session"))
        self = cls(run, herdr, on_event)
        self.watch()
        return self

    def watch(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"watch-{self.run.id}")
        self._thread.start()

    def stop_watching(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # --------------------------------------------------------------- control

    def status(self) -> str:
        """herdr agent state, or "closed" once the agent is gone. Other herdr errors
        (timeouts, a restarting server) propagate so the watcher retries instead."""
        try:
            return self.herdr.get(self.name).get("agent_status", "unknown")
        except HerdrError as exc:
            if exc.code in ("agent_not_found", "pane_not_found", "workspace_not_found"):
                return "closed"
            raise

    def send(self, text: str, via: str = "slack") -> None:
        """Submit a prompt. herdr refuses (agent_blocked) while an approval is pending."""
        with self._send_lock:
            self._send(text, via)

    def _send(self, text: str, via: str) -> None:
        meta = self.run.meta()
        turn = (meta.get("turns") or 0) + 1
        # Record the prompt before sending it, so it always precedes the "working" state
        # the watcher will see; listeners start a new turn on it.
        self.run.update_meta(turns=turn, status="running")
        self._turn_completed = False  # a new turn has not completed yet
        self._engine_error, self._restart_at = None, None
        if via == "auto-restart":
            self._restarts_since_human += 1
        else:
            self._auto_restarts, self._restarts_since_human, self._gave_up = 0, 0, False
        self._sent_prompts.append(text.strip())
        self._emit({"kind": "prompt", "title": "You", "body": text, "data": {"via": via}})
        try:
            self.herdr.prompt(self.name, text)
        except HerdrError as exc:
            self._emit({"kind": "error", "title": "Prompt not delivered", "body": str(exc), "data": {}})
            raise

    # ------------------------------------------------------ first-prompt delivery

    def _session_transcript(self) -> Optional[Path]:
        session_id = self.run.meta().get("session_id")
        return find_transcript(self.engine, session_id, self.run.meta()["cwd"], self._started_at) if session_id else None

    def _transcript_size(self) -> int:
        path = self._session_transcript()
        try:
            return path.stat().st_size if path else 0
        except OSError:
            return 0

    def _wait_until_ready(self) -> None:
        """Claude writes its transcript when its SessionStart hooks run, and draws the input box
        (and the Remote Control line, when on) only after that. Wait for both, then for the
        screen to stop changing. Best effort: _confirm_delivery is the real guarantee."""
        if self.engine != "claude":
            return
        deadline = time.monotonic() + STARTUP_SETTLE_SECONDS
        remote = os.environ.get("NEO_REMOTE_CONTROL", "1") != "0"
        last = None
        while time.monotonic() < deadline:
            path = self._session_transcript()
            screen = self.herdr.read(self.name, lines=30, source="visible")
            ready = bool(path and path.exists() and path.stat().st_size > 0) and "❯" in screen
            if ready and remote and "remote-control" not in screen.lower():
                ready = False
            if ready and screen == last:
                return
            last = screen if ready else None
            time.sleep(0.5)
        log.warning("run %s: agent did not look ready after %.0fs; sending anyway", self.run.id, STARTUP_SETTLE_SECONDS)

    def _landed(self, before: int) -> bool:
        """True once the agent shows it received a prompt: it is working (or asking to), or its
        transcript has a user prompt written after `before` bytes."""
        try:
            # "unknown": herdr often cannot read Codex's screen once it is busy.
            if self.herdr.get(self.name).get("agent_status") in ("working", "blocked", "unknown"):
                return True
        except HerdrError:
            pass
        path = self._session_transcript()
        if not path or self.engine not in TRANSCRIPT_NORMALIZERS:
            return False
        try:
            with path.open("rb") as fh:
                fh.seek(before)
                chunk = fh.read()
        except OSError:
            return False
        normalize = TRANSCRIPT_NORMALIZERS[self.engine]
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict) and any(e["kind"] == "prompt" for e in normalize(raw)):
                return True
        return False

    def _confirm_delivery(self, text: str, before: int) -> None:
        """Make sure the first prompt reached the agent. If it is sitting unsubmitted in the input
        box, press Enter; if it vanished, type it again. Raise when it never arrives, so the
        caller reports a failure instead of leaving an idle agent that looks like it is done."""
        head = text.strip().splitlines()[0][:40] if text.strip() else ""
        for attempt in range(PROMPT_RETRIES + 1):
            deadline = time.monotonic() + PROMPT_CONFIRM_SECONDS
            while time.monotonic() < deadline:
                if self._landed(before):
                    return
                time.sleep(0.5)
            if attempt == PROMPT_RETRIES:
                break
            screen = self.herdr.read(self.name, lines=30, source="visible")
            if "[Pasted text" in screen or (head and head in screen):
                log.warning("run %s: prompt is waiting in the input box; pressing Enter", self.run.id)
                self.herdr.send_keys(self.name, "enter")
            else:
                log.warning("run %s: prompt was dropped by the agent; sending it again (%d)", self.run.id, attempt + 1)
                self.herdr.prompt(self.name, text)
        message = f"the agent did not receive its task after {PROMPT_RETRIES + 1} tries"
        self._emit({"kind": "error", "title": "Prompt not delivered", "body": message, "data": {}})
        raise HerdrError("prompt_not_delivered", message)

    def approve(self) -> None:
        self.herdr.send_keys(self.name, "enter")

    def deny(self) -> None:
        self.herdr.send_keys(self.name, "esc")

    def interrupt(self) -> None:
        self.herdr.send_keys(self.name, "esc")

    def keys(self, *keys: str) -> None:
        self.herdr.send_keys(self.name, *keys)

    def remote_url(self, wait_seconds: float = 0) -> Optional[str]:
        """The claude.ai/code link for this session, if Remote Control is on."""
        meta = self.run.meta()
        if meta.get("remote_url") or self.engine != "claude":
            return meta.get("remote_url")
        deadline = time.monotonic() + wait_seconds
        while True:
            match = RC_URL_RE.search(self.herdr.read(self.name, lines=400, source="recent-unwrapped"))
            if match:
                self.run.update_meta(remote_url=match.group(0))
                return match.group(0)
            if time.monotonic() >= deadline:
                return None
            time.sleep(1)

    def screen(self, lines: int = 25) -> str:
        text = self.herdr.read(self.name, lines=lines, source="detection")
        return "\n".join(text.rstrip().splitlines()[-lines:])

    def close(self) -> None:
        self._stop.set()
        workspace = self.run.meta().get("herdr_workspace")
        if workspace:
            try:
                self.herdr.close_workspace(workspace)
            except HerdrError:
                pass
        self.run.update_meta(status="closed")
        self._emit({"kind": "status", "title": "closed", "body": "", "data": {"status": "closed"}})

    # --------------------------------------------------------------- watcher

    def _emit(self, event: dict) -> None:
        turn = self.run.meta().get("turns") or 1
        stored = self.run.append_event(event, turn)
        try:
            self.on_event(stored)
        except Exception:  # a failing listener must not kill the watcher
            pass

    def _tail(self) -> None:
        if self._transcript is None:
            meta = self.run.meta()
            session_id = meta.get("session_id")
            if not session_id:
                try:
                    session_id = agent_session_id(self.herdr.get(self.name))
                except HerdrError:
                    session_id = None
                if session_id:
                    self.run.update_meta(session_id=session_id)
            path = find_transcript(self.engine, session_id, meta["cwd"], self._started_at)
            if path is None:
                return
            self._transcript = path
            with _CLAIMED_LOCK:
                _CLAIMED.add(str(path))
            self.run.update_meta(transcript_path=str(path))
        normalize = TRANSCRIPT_NORMALIZERS[self.engine]
        try:
            with self._transcript.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        complete, self._offset = chunk[: end + 1], self._offset + end + 1
        for line in complete.decode("utf-8", errors="replace").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            error = engine_error_text(self.engine, raw)
            if error is not None:
                self._engine_error = error if is_transient_engine_error(error) else None
            for event in normalize(raw):
                if error is None and event["kind"] in ("prompt", "text", "tool_use", "tool_result"):
                    self._engine_error = None  # the agent moved on by itself
                    if event["kind"] == "tool_result":
                        # A finished tool call is real progress and ends the streak. Text is not:
                        # a response cut off mid-stream leaves partial text before its error.
                        self._auto_restarts, self._gave_up = 0, False
                if event["kind"] == "prompt":
                    self._turn_completed = False
                    if event["body"].strip() in self._sent_prompts:
                        self._sent_prompts.remove(event["body"].strip())
                        continue  # already recorded when we sent it
                    # Typed in the dashboard or the terminal: a new turn all the same.
                    self._auto_restarts, self._restarts_since_human, self._gave_up, self._restart_at = 0, 0, False, None
                    turns = (self.run.meta().get("turns") or 0) + 1
                    self.run.update_meta(turns=turns, status="running")
                    event["data"] = {**(event.get("data") or {}), "via": "elsewhere"}
                if event["kind"] == "session":
                    continue
                if event["kind"] == "result":
                    self._turn_completed = True
                elif event["kind"] == "prompt":
                    self._turn_completed = False
                self._emit(event)
        self.run.update_meta(transcript_offset=self._offset)

    def _maybe_auto_restart(self) -> None:
        """The agent is idle. If it stopped on a transient API error, send "continue" after a
        back-off: at most len(AUTO_RESTART_DELAYS) times in a row without progress, and at most
        MAX_AUTO_RESTARTS_PER_PROMPT times between two human prompts."""
        with self._send_lock:  # a Slack prompt in between must not see a half-made decision
            if self._engine_error is None:
                if self._restart_at is None and self._turn_completed:
                    self._auto_restarts, self._gave_up = 0, False  # a clean finish ends the streak
                self._restart_at = None
                return
            if (self._auto_restarts >= len(AUTO_RESTART_DELAYS)
                    or self._restarts_since_human >= MAX_AUTO_RESTARTS_PER_PROMPT):
                if not self._gave_up:
                    self._gave_up, self._restart_at = True, None
                    self._emit({"kind": "error", "title": "Auto-restart gave up",
                                "body": f"Still failing after {self._restarts_since_human} restarts: {self._engine_error}",
                                "data": {"auto_restart": "exhausted"}})
                return
            if self._restart_at is None:
                delay = AUTO_RESTART_DELAYS[self._auto_restarts]
                self._restart_at = time.monotonic() + delay
                self._emit({"kind": "status", "title": "Auto-restart",
                            "body": f"{self.engine} API error; sending \"{AUTO_RESTART_PROMPT}\" in {delay:.0f}s "
                                    f"({self._restarts_since_human + 1}/{MAX_AUTO_RESTARTS_PER_PROMPT})",
                            "data": {"auto_restart": self._restarts_since_human + 1, "error": self._engine_error[:500]}})
                return
            if time.monotonic() < self._restart_at:
                return
            self._auto_restarts += 1
            error = self._engine_error
            log.warning("run %s: %s stopped on an API error; sending %r (%d/%d)", self.run.id, self.engine,
                        AUTO_RESTART_PROMPT, self._restarts_since_human + 1, MAX_AUTO_RESTARTS_PER_PROMPT)
            try:
                self._send(AUTO_RESTART_PROMPT, via="auto-restart")
            except HerdrError:
                # send() recorded "Prompt not delivered"; try again after the next back-off.
                self._engine_error, self._restart_at = error, None

    def _last_text(self) -> str:
        last = ""
        with (self.run.dir / "events.jsonl").open() as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line still being written
                if event["kind"] == "prompt":
                    last = ""
                elif event["kind"] in ("text", "result") and event.get("body"):
                    last = event["body"]
        return last

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if not self.run.dir.exists():
                return  # the run was deleted; nothing left to record into
            try:
                if self._watch():
                    return
                failures = 0
            except HerdrError as exc:
                if self._polled_ok:  # herdr answered since the last failure: a fresh outage
                    failures, self._polled_ok = 0, False
                failures += 1
                if exc.code != "server_not_running":
                    log.exception("watcher error for run %s (attempt %d)", self.run.id, failures)
                else:
                    # After a reboot nothing starts herdr; without this every watcher retries forever.
                    if failures == 1:
                        log.warning("run %s: herdr session '%s' is down; starting it", self.run.id, self.herdr.session)
                    try:
                        self.herdr.ensure_server()  # herdr restores its panes; the next poll picks this run up
                    except HerdrError as start_exc:
                        level = logging.DEBUG if start_exc.code == "server_start_cooldown" else logging.WARNING
                        log.log(level, "run %s: herdr did not start (attempt %d): %s", self.run.id, failures, start_exc)
                    except Exception:  # a failed restart must not end the watcher
                        log.exception("run %s: herdr start failed (attempt %d)", self.run.id, failures)
                self._stop.wait(min(30, POLL_SECONDS * 2 ** min(failures, 5)))
            except Exception:  # keep watching; one bad read must not end the run's record
                failures += 1
                log.exception("watcher error for run %s (attempt %d)", self.run.id, failures)
                self._stop.wait(min(30, POLL_SECONDS * 2 ** min(failures, 5)))
        with _CLAIMED_LOCK:
            _CLAIMED.discard(str(self._transcript))

    def _watch(self) -> bool:
        """Poll until stopped. Returns True when the agent is gone for good."""
        while not self._stop.is_set():
            self._tail()
            state = self.status()
            self._polled_ok = True
            if state == "closed":
                self.run.update_meta(status="closed")
                self._emit({"kind": "status", "title": "closed", "body": "agent is gone from herdr", "data": {"status": "closed"}})
                with _CLAIMED_LOCK:
                    _CLAIMED.discard(str(self._transcript))
                return True
            if self._turn_completed and state in ("unknown", "working"):
                # herdr cannot always read Codex's screen ("unknown" after a reply); the engine's
                # own turn-complete event in its transcript is authoritative.
                state = "done"
            if state == "done" and self._status == "done" or state == "idle" and self._status == "done":
                self._maybe_auto_restart()
                self._stop.wait(POLL_SECONDS)
                continue
            if state != self._status:
                previous, self._status = self._status, state
                if state in READY_STATES:
                    time.sleep(1.5)  # let the transcript catch up with the screen
                    self._tail()
                data = {"status": state, "previous": previous}
                body = ""
                if state == "blocked":
                    body = self.screen()
                elif state in READY_STATES and (previous in ("working", "blocked", "unknown") or self._turn_completed):
                    data["last_text"] = self._last_text()
                    data["turn_completed"] = True
                self.run.update_meta(status=STATUS_MAP.get(state, "running"), agent_status=state)
                self._emit({"kind": "status", "title": state, "body": body, "data": data})
            if state in READY_STATES:
                self._maybe_auto_restart()  # after the idle event, so Slack shows the error first
            self._stop.wait(POLL_SECONDS)
        return False
