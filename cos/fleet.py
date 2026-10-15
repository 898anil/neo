"""Everything running on this machine, in one picture.

Sources, all native:
  - herdr: every agent in every running herdr session (yours and the bot's), with its
    state (working / blocked / done / idle) and engine session id
  - the engine's own session transcript on disk, for a structured timeline
  - Claude Remote Control: the claude.ai/code link a live Claude session prints
  - runs/: headless runs and the bot's live runs (Slack thread, cost, turns)
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from .events import TRANSCRIPT_NORMALIZERS
from .config import OWNER_MESSAGE_MARK, THREAD_CONTEXT_HEAD
from .herdr import Herdr, HerdrError, agent_session_id
from .interactive import RC_URL_RE, find_transcript
from .runner import list_runs

PANE_RE = re.compile(r"^w[0-9A-Za-z]{1,8}:p[0-9A-Za-z]{1,8}$")  # herdr ids go past 9 with letters: wB:p1
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TRANSCRIPT_TAIL_BYTES = 3_000_000
TIMELINE_EVENTS = 200
CACHE_SECONDS = 2.0

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_transcripts: dict[str, Optional[str]] = {}
_rc_urls: dict[str, str] = {}
_last_text: dict[str, tuple[float, str, bool, int]] = {}  # path -> (mtime, last text, turn completed, prompts seen)
LAST_TEXT_BYTES = 400_000


def _cached(key: str, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def herdr_sessions() -> Optional[list[str]]:
    """Running herdr sessions, or None when herdr could not be asked."""
    try:
        proc = Herdr("default")._run(["session", "list", "--json"], timeout=10)
        sessions = json.loads(proc.stdout).get("sessions", [])
    except (HerdrError, json.JSONDecodeError):
        return None
    return [s["name"] for s in sessions if s.get("running")]


def _transcript_for(engine: str, session_id: Optional[str], cwd: str) -> Optional[Path]:
    key = f"{engine}:{session_id or cwd}"
    if key not in _transcripts or _transcripts[key] is None:
        path = find_transcript(engine, session_id, cwd, started_at=0)
        _transcripts[key] = str(path) if path else None
    value = _transcripts[key]
    return Path(value) if value else None


def _run_index() -> dict[tuple, dict]:
    """(herdr session, agent name) -> run meta, for agents the bot started."""
    index = {}
    for meta in list_runs(200):
        if meta.get("runtime") == "herdr" and meta.get("herdr_agent"):
            index[(meta.get("herdr_session"), meta["herdr_agent"])] = meta
    return index


def _agent_row(session: str, agent: dict, runs: dict) -> dict:
    engine = agent.get("agent") or "?"
    cwd = agent.get("foreground_cwd") or agent.get("cwd") or ""
    session_id = agent_session_id(agent)
    transcript = _transcript_for(engine, session_id, cwd) if engine in TRANSCRIPT_NORMALIZERS else None
    last_active = transcript.stat().st_mtime if transcript and transcript.exists() else None
    last_text, turn_done, prompts = _last_text_for(engine, transcript, last_active) if last_active else ("", False, 0)
    status = agent.get("agent_status") or "unknown"
    if status == "unknown" and turn_done:
        status = "done"  # herdr cannot read every screen (Codex); the transcript says the turn ended
    run = runs.get((session, agent.get("name")))
    row = {
        "session": session,
        "pane_id": agent.get("pane_id"),
        "name": agent.get("name"),
        "engine": engine,
        "status": status,
        "seq": agent.get("state_change_seq"),  # bumps on every state change, even ones we did not see
        "title": agent.get("terminal_title_stripped") or agent.get("name") or engine,
        "cwd": cwd,
        "repo": os.path.basename(cwd.rstrip("/")) or cwd,
        "session_id": session_id,
        "last_active": last_active,
        "last_text": last_text,
        # herdr says "idle", but an agent whose last words are a question is waiting on you.
        "asks": status in ("idle", "done") and last_text.rstrip().rstrip("*_` ").endswith("?"),
        "remote_url": _rc_urls.get(f"{session}/{agent.get('pane_id')}") or (run or {}).get("remote_url"),
        "run_id": run["id"] if run else None,
        "slack_thread": run.get("slack_thread") if run else None,
        "prompts": prompts,
        "created_at": (run or {}).get("created_at"),
    }
    row["attention"], row["reason"] = attention(row, run)
    return row


# What each agent needs from you, most urgent first. The home screen groups agents by this.
ATTENTION_ORDER = {"approve": 0, "stuck": 1, "reply": 2, "failed": 3, "review": 4, "working": 5, "idle": 6}
STUCK_AFTER_SECONDS = 45


def attention(row: dict, run: Optional[dict]) -> tuple[str, str]:
    """(kind, reason): approve / stuck / reply / review / working / idle."""
    status = row["status"]
    if status == "blocked":
        return "approve", "Waiting for your approval"
    if run and status in ("idle", "done") and not row["prompts"]:
        started = _iso_ts(run.get("created_at"))
        if started and time.time() - started > STUCK_AFTER_SECONDS:
            return "stuck", "Never received its task. Resend it or close the agent."
    if row["asks"]:
        return "reply", "Asked you a question"
    if status in ("working", "unknown"):
        return "working", "Working"
    if row["prompts"] and row["last_active"] and row["last_active"] > seen.get(_seen_key(row)):
        return "review", "Finished. Read the result."
    return "idle", "Idle"


def _iso_ts(value) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _seen_key(row: dict) -> str:
    # The engine session id survives herdr pane ids being reused by later agents.
    return f"agent:{row.get('session_id') or row['session'] + '/' + str(row['pane_id'])}"


class SeenStore:
    """When you last looked at each agent and run (COS_HOME/seen.json), so a finished agent stays
    on "Finished" until you open it or mark it read."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    def _path(self) -> Path:
        from .runner import cos_home

        return cos_home() / "seen.json"

    def _load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self._path().read_text())
            except (OSError, json.JSONDecodeError):
                self._data = {}
        return self._data

    def ready(self) -> bool:
        with self._lock:
            return self._path().exists()

    def get(self, key: str) -> float:
        with self._lock:
            return float(self._load().get(key) or 0)

    def mark(self, items: dict[str, float]) -> None:
        with self._lock:
            data = self._load()
            changed = {k: v for k, v in items.items() if v and v > float(data.get(k) or 0)}
            if not changed and self._path().exists():
                return
            data.update(changed)
            if len(data) > 2000:  # keep the newest
                data = dict(sorted(data.items(), key=lambda kv: kv[1])[-1500:])
                self._data = data
            tmp = self._path().with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._path())

    def reset(self) -> None:
        with self._lock:
            self._data = None


seen = SeenStore()


def mark_seen(rows: list[dict]) -> None:
    seen.mark({_seen_key(r): r["last_active"] or time.time() for r in rows})


def mark_run_seen(run_id: str) -> None:
    seen.mark({f"run:{run_id}": time.time()})


def _engine_gone(pid: Optional[int]) -> bool:
    """A recorded engine pid that no longer exists. No pid means a turn is starting, so not gone.
    kill(0) only: unlike runner.pid_alive this never reaps a child the runner is still waiting on."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def run_attention(meta: dict) -> Optional[tuple[str, str]]:
    """Background (headless) tasks you started, from the dashboard or a Slack thread, stay on the
    board for a day like any other agent. Failures need you; an unread dashboard result is to read;
    a Slack answer was already delivered in its thread, so it waits with the idle agents.
    Watches and the brief report through findings and the Today tab."""
    status = meta.get("status")
    if status == "running" and not _engine_gone(meta.get("pid")):
        return "working", "Working in the background"
    source = meta.get("source")
    if source not in ("dashboard", "slack"):
        return None
    updated = _iso_ts(meta.get("updated_at")) or 0
    if time.time() - updated > 86400:
        return None
    if status == "running":  # its engine died without writing a result (for example, a crash)
        return "failed", "Engine process ended without a result. Open it to see why."
    unread = updated > seen.get(f"run:{meta['id']}")
    if status == "failed":
        return ("failed", meta.get("note") or "Failed. Open it to see why.") if unread else ("idle", "Failed")
    if status == "done":
        if source == "slack":
            return "idle", "Answered in Slack"  # you read the answer there; don't ask twice
        return ("review", "Finished. Read the result.") if unread else ("idle", "Finished")
    return None


SLACK_CONTEXT = THREAD_CONTEXT_HEAD + "\n"
SLACK_ASK = "\n\n" + OWNER_MESSAGE_MARK + "\n"
SLACK_LINK_RE = re.compile(r"<(?:https?://[^|>]+\|)?([^>]+)>")


def _one_line(text: str, limit: int) -> str:
    text = " ".join(SLACK_LINK_RE.sub(r"\1", text).replace("*", "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def run_title(meta: dict) -> str:
    """A name you can recognise a task by. A Slack thread's follow-ups ("check this", "fix these")
    say nothing on their own, so lead with the thread's first message."""
    if meta.get("title"):
        return meta["title"]
    prompt = meta.get("prompt") or ""
    if not prompt.startswith(SLACK_CONTEXT):
        return _one_line(prompt, 120)
    thread, _, ask = prompt[len(SLACK_CONTEXT):].partition(SLACK_ASK)
    first = re.sub(r"^[^:\n]{1,40}:\s*", "", thread.strip().split("\n", 1)[0])
    ask = _one_line(ask, 60)
    return _one_line(first, 90) + (f" \u00b7 {ask}" if ask else "") if first else ask


def _last_text_for(engine: str, path: Path, mtime: float) -> tuple[str, bool]:
    """What the agent said last and whether its last turn completed; re-read only on change."""
    key = str(path)
    hit = _last_text.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2], hit[3]
    last, done, prompts = "", False, 0
    try:
        for event in _tail_events(engine, path, 60, LAST_TEXT_BYTES):
            if event["kind"] == "prompt":
                done = False
                prompts += 1
                last = ""  # what it said before this prompt is not this turn's reply (home reads its Status line)
            elif event["kind"] == "result":
                done = True
            if event["kind"] in ("text", "result") and event.get("body"):
                last = event["body"]
    except OSError:
        pass
    if not prompts and path.stat().st_size > LAST_TEXT_BYTES:
        prompts = 1  # a long session whose prompts are older than the tail we read
    _last_text[key] = (mtime, last, done, prompts)
    return last, done, prompts


def agents_checked() -> tuple[list[dict], bool]:
    """(rows, complete). complete is False when herdr could not list everything, so callers
    that compare snapshots (notifications) can skip the pass instead of seeing agents vanish."""
    def load():
        runs = _run_index()
        rows, complete = [], True
        sessions = herdr_sessions()
        if sessions is None:
            return [], False
        for session in sessions:
            try:
                listed = Herdr(session).call("agent", "list", timeout=15).get("agents", [])
            except HerdrError:
                complete = False
                continue
            rows += [_agent_row(session, a, runs) for a in listed]
        if complete and not seen.ready():
            mark_seen(rows)  # first run: start from "everything read", not a flood of "Finished"
            for r in rows:
                r["attention"], r["reason"] = attention(r, runs.get((r["session"], r["name"])))
        rows.sort(key=lambda r: (ATTENTION_ORDER.get(r["attention"], 9), -(r["last_active"] or 0)))
        return rows, complete

    return _cached("agents", load)


def agents() -> list[dict]:
    return agents_checked()[0]


def overview() -> dict:
    rows = agents()
    runs = [{**m, "title": run_title(m)} for m in list_runs(60, include_running=True)
            if m.get("runtime", "headless") == "headless" or m.get("status") != "closed"]
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    running_runs = sum(1 for m in runs if m.get("status") == "running" and m.get("runtime", "headless") == "headless")
    return {
        "host": socket.gethostname().removesuffix(".local"),
        "generated_at": time.time(),
        "needs_you": [r for r in rows if r["status"] == "blocked" or r["asks"]],
        "tasks": _task_rows(runs),
        "agents": rows,
        "runs": runs,
        "counts": {**counts, "headless_running": running_runs, "agents": len(rows)},
    }


def _task_rows(runs: list[dict]) -> list[dict]:
    """Headless runs that belong on the agents board, with their attention."""
    rows = []
    for meta in runs:
        if meta.get("runtime", "headless") != "headless":
            continue
        found = run_attention(meta)
        if found:
            rows.append({**meta, "attention": found[0], "reason": found[1]})
    return rows


# ------------------------------------------------------------- one agent


def valid_target(session: str, pane_id: str) -> bool:
    return bool(SESSION_RE.match(session or "") and PANE_RE.match(pane_id or ""))


_parsed: dict[str, dict] = {}  # transcript path -> {"offset": bytes read, "events": normalized events}
_parsed_lock = threading.Lock()
FULL_HISTORY_BYTES = 24_000_000


def transcript_events(engine: str, path: Path) -> list[dict]:
    """Every timeline event in a session transcript (its last 24 MB), parsed once and then
    extended with only the bytes appended since the previous call."""
    key = str(path)
    size = path.stat().st_size
    with _parsed_lock:
        state = _parsed.get(key)
        if state is None or size < state["offset"]:  # new, or rewritten
            state = {"offset": max(0, size - FULL_HISTORY_BYTES), "events": [], "skip_partial": size > FULL_HISTORY_BYTES}
            _parsed[key] = state
        if size == state["offset"]:
            return list(state["events"])
        with path.open("rb") as fh:
            fh.seek(state["offset"])
            chunk = fh.read(size - state["offset"])
        end = chunk.rfind(b"\n")
        if end < 0:
            return list(state["events"])
        chunk, state["offset"] = chunk[: end + 1], state["offset"] + end + 1
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if state.pop("skip_partial", False) and lines:
            lines = lines[1:]  # started mid-line
        normalize = TRANSCRIPT_NORMALIZERS[engine]
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            ts = raw.get("timestamp")
            for event in normalize(raw):
                if event["kind"] == "session":
                    continue
                event["ts"] = ts
                state["events"].append(event)
        return list(state["events"])


def _tail_events(engine: str, path: Path, limit: int, tail_bytes: int = TRANSCRIPT_TAIL_BYTES) -> list[dict]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()  # drop the partial first line
        data = fh.read()
    normalize = TRANSCRIPT_NORMALIZERS[engine]
    events = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        ts = raw.get("timestamp")
        for event in normalize(raw):
            if event["kind"] == "session":
                continue
            event["ts"] = ts
            events.append(event)
    return events[-limit:]


def recent_turns(events: list[dict], turns: int = 6, cap: int = 600) -> list[dict]:
    """The conversation from your last `turns` messages on, so a long session still shows what you
    asked, not just the last few hundred tool calls. Adds a note when earlier history is left out."""
    prompts = [i for i, e in enumerate(events) if e["kind"] == "prompt"]
    start = prompts[-turns] if len(prompts) >= turns else 0
    kept = events[start:]
    if len(kept) > cap:
        kept = kept[-cap:]
    if len(kept) < len(events):
        note = {"kind": "status", "title": "earlier", "body": "", "data": {"status": "earlier", "hidden": len(events) - len(kept)}}
        kept = [note] + kept
    return kept


def agent_detail(session: str, pane_id: str) -> dict:
    herdr = Herdr(session)
    agent = herdr.get(pane_id)
    runs = _run_index()
    row = _agent_row(session, agent, runs)
    screen = herdr.read(pane_id, lines=45, source="detection")
    if not row["remote_url"] and row["engine"] == "claude":
        # Remote Control prints its claude.ai link near the top of the session.
        history = herdr.read(pane_id, lines=400, source="recent-unwrapped")
        match = RC_URL_RE.search(history)
        if match:
            _rc_urls[f"{session}/{pane_id}"] = row["remote_url"] = match.group(0)
    events: list[dict] = []
    transcript = _transcript_for(row["engine"], row["session_id"], row["cwd"]) if row["engine"] in TRANSCRIPT_NORMALIZERS else None
    if transcript and transcript.exists():
        events = recent_turns(transcript_events(row["engine"], transcript))
    run = runs.get((session, agent.get("name"))) or {}
    if row["attention"] == "review":
        mark_seen([row])  # you are reading the result now
        _drop_cache()
    return {
        **row,
        "host": socket.gethostname().removesuffix(".local"),
        "slack_url": run.get("slack_url"),
        "screen": "\n".join(screen.rstrip().splitlines()[-40:]),
        "events": events,
    }


def remote_url(session: str, pane_id: str) -> Optional[str]:
    key = f"{session}/{pane_id}"
    if key not in _rc_urls:
        match = RC_URL_RE.search(Herdr(session).read(pane_id, lines=400, source="recent-unwrapped"))
        if match:
            _rc_urls[key] = match.group(0)
    return _rc_urls.get(key)


ACTIONS = ("prompt", "approve", "deny", "interrupt", "keys", "seen", "resend", "close")


def _drop_cache() -> None:
    with _cache_lock:
        _cache.pop("agents", None)


def act(session: str, pane_id: str, action: str, text: str = "", keys: Optional[list[str]] = None) -> None:
    herdr = Herdr(session)
    if action == "prompt":
        if not text.strip():
            raise ValueError("prompt text is required")
        herdr.prompt(pane_id, text)
    elif action == "approve":
        herdr.send_keys(pane_id, "enter")
    elif action in ("deny", "interrupt"):
        herdr.send_keys(pane_id, "esc")
    elif action == "keys":
        if not keys:
            raise ValueError("keys are required")
        herdr.send_keys(pane_id, *keys)
    elif action == "seen":
        row = _agent_row(session, herdr.get(pane_id), {})
        mark_seen([row])
    elif action == "resend":
        # The agent never got the task it was started with (see attention "stuck"): type it again.
        agent = herdr.get(pane_id)
        run = _run_index().get((session, agent.get("name")))
        if not run or not (run.get("prompt") or "").strip():
            raise ValueError("this agent was not started by neo, so there is no task to resend")
        herdr.prompt(pane_id, run["prompt"])
    elif action == "close":
        # The owner is done with it: close its herdr workspace (ends the agent) and mark the run closed,
        # so it leaves the board instead of sitting there idle.
        agent = herdr.get(pane_id)
        workspace = agent.get("workspace_id") or str(pane_id).split(":")[0]
        herdr.close_workspace(workspace)
        run = _run_index().get((session, agent.get("name")))
        if run:
            from .runner import Run
            Run(run["id"]).update_meta(status="closed")
    else:
        raise ValueError(f"unknown action {action}")
    _drop_cache()
