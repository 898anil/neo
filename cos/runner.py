"""Launch Claude Code or Codex headless and record every event to disk.

Layout per run (under COS_HOME/runs/<run_id>/):
    meta.json     engine, prompt, cwd, mode, status, session_id, cost, timestamps
    events.jsonl  normalized timeline events (what the dashboard renders)
    raw.jsonl     untouched engine output lines, for debugging the normalizer
    stderr.log    engine stderr
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import conclusion
from .events import normalize

ENGINES = ("claude", "codex")
MODES = ("readonly", "edit", "full")
TERMINAL = ("done", "failed", "cancelled")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
MAX_PROMPT = 100_000

CLAUDE_MODE_FLAGS = {
    # Headless default mode auto-denies anything that needs approval; denials show in the timeline.
    "readonly": ["--permission-mode", "default"],
    "edit": ["--permission-mode", "acceptEdits"],
    "full": ["--permission-mode", "bypassPermissions"],
}
CODEX_SANDBOX = {"readonly": "read-only", "edit": "workspace-write", "full": "danger-full-access"}


def cos_home() -> Path:
    return Path(os.environ.get("COS_HOME", Path(__file__).resolve().parent.parent)).resolve()


def runs_dir() -> Path:
    path = cos_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id or ""))


def cli_path(engine: str) -> str:
    """Explicit paths matter under launchd, whose PATH differs from an interactive shell."""
    return os.environ.get(f"{engine.upper()}_CLI_PATH") or shutil.which(engine) or engine


def build_command(
    engine: str, prompt: str, mode: str, session_id: Optional[str] = None, system: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """system: standing instructions. Claude takes them natively (--append-system-prompt, on every
    turn); Codex has no equivalent flag, so they lead the first prompt of the session.
    extra_args: more CLI options for the engine (for example Claude's --allowedTools, --json-schema)."""
    extra_args = list(extra_args or [])
    model = os.environ.get(f"{engine.upper()}_MODEL")
    if engine == "claude":
        cmd = [cli_path("claude"), "-p", "--output-format", "stream-json", "--verbose"]
        cmd += CLAUDE_MODE_FLAGS[mode]
        cmd += ["--settings", conclusion.settings_json()]  # every turn ends with a Status line
        if system:
            cmd += ["--append-system-prompt", system]
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd + extra_args + ["--", prompt]  # "--": a prompt starting with "-" is not an option
    if engine == "codex":
        if system and not session_id:
            prompt = f"{system}\n\n---\n\n{prompt}"
        if mode == "full":
            access = ["--dangerously-bypass-approvals-and-sandbox"]
        elif session_id:
            access = ["-c", f'sandbox_mode="{CODEX_SANDBOX[mode]}"']
        else:
            access = ["-s", CODEX_SANDBOX[mode]]
        cmd = [cli_path("codex"), "exec"]
        if session_id:
            cmd += ["resume"]
        cmd += ["--json", "--skip-git-repo-check", *access]
        if model:
            cmd += ["--model", model]
        cmd += extra_args
        cmd += ["--"]  # a prompt (or id) starting with "-" is not an option
        if session_id:
            cmd += [session_id]
        return cmd + [prompt]
    raise ValueError(f"unknown engine: {engine}")


def validate_request(engine: str, prompt: str, cwd: str, mode: str) -> Optional[str]:
    if engine not in ENGINES:
        return f"engine must be one of {ENGINES}"
    if mode not in MODES:
        return f"mode must be one of {MODES}"
    if not prompt or not prompt.strip():
        return "prompt is required"
    if len(prompt) > MAX_PROMPT:
        return f"prompt is longer than {MAX_PROMPT} characters"
    if not cwd or not Path(cwd).expanduser().is_dir():
        return f"cwd is not a directory: {cwd}"
    return None


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _run_lock(run_id: str) -> threading.RLock:
    """One lock per run id, shared by every Run object in this process."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(run_id, threading.RLock())


class Run:
    """One conversation with one engine; each prompt or follow-up is a turn."""

    def __init__(self, run_id: str):
        if not valid_run_id(run_id):
            raise ValueError(f"invalid run id: {run_id}")
        self.id = run_id
        self.dir = runs_dir() / run_id
        self._lock = _run_lock(run_id)
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    # ---------------------------------------------------------------- storage

    @classmethod
    def create(
        cls,
        engine: str,
        prompt: str,
        cwd: str,
        mode: str,
        source: str = "cli",
        runtime: str = "headless",
        extra: Optional[dict] = None,
    ) -> "Run":
        """runtime "headless" runs are executed by this process; "herdr" runs are
        interactive agents in herdr that a watcher records (see cos.interactive)."""
        if runtime == "herdr":
            mode = "interactive"
            error = validate_request(engine, prompt, cwd, "full")
        else:
            error = validate_request(engine, prompt, cwd, mode)
        if error:
            raise ValueError(error)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        run = cls(run_id)
        run.dir.mkdir(parents=True)
        run.write_meta(
            {
                "id": run_id,
                "engine": engine,
                "prompt": prompt,
                "cwd": str(Path(cwd).expanduser().resolve()),
                "mode": mode,
                "runtime": runtime,
                "source": source,
                **(extra or {}),
                "status": "queued",
                "session_id": None,
                "turns": 0,
                "cost_usd": 0.0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "pid": None,
            }
        )
        (run.dir / "events.jsonl").touch()
        return run

    def meta(self) -> dict:
        return json.loads((self.dir / "meta.json").read_text())

    def write_meta(self, meta: dict) -> None:
        meta["updated_at"] = now_iso()
        # Unique temp name: several processes (bot, dashboard, CLI) may write one run.
        tmp = self.dir / f"meta.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(meta, indent=2))
        tmp.replace(self.dir / "meta.json")

    def update_meta(self, **fields) -> dict:
        with self._lock:
            meta = self.meta()
            meta.update(fields)
            self.write_meta(meta)
            return meta

    def _next_seq(self) -> int:
        path = self.dir / "events.jsonl"
        with path.open("rb") as fh:
            return sum(1 for _ in fh)

    def append_event(self, event: dict, turn: int) -> dict:
        with self._lock:
            event = {"seq": self._next_seq(), "ts": now_iso(), "turn": turn, **event}
            with (self.dir / "events.jsonl").open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        return event

    # -------------------------------------------------------------- execution

    def execute(self, prompt: Optional[str] = None, on_event: Optional[Callable[[dict], None]] = None) -> dict:
        """Run one turn to completion. Without a prompt, runs the initial prompt."""
        with self._lock:  # claim the run so two callers cannot start turns at once
            meta = self.meta()
            if meta.get("runtime", "headless") != "headless":
                raise RuntimeError("live runs are driven through herdr, not execute()")
            if meta["status"] == "running":
                raise RuntimeError("run is already executing a turn")
            is_follow_up = prompt is not None
            if is_follow_up and not meta.get("session_id"):
                raise RuntimeError("no session id recorded yet; cannot send a follow-up")
            turn = meta["turns"] + 1
            self._cancelled = False
            self.write_meta({**meta, "status": "running", "turns": turn, "pid": None})

        engine, mode, cwd = meta["engine"], meta["mode"], meta["cwd"]
        text = prompt if is_follow_up else meta["prompt"]
        emit = on_event or (lambda _e: None)
        try:
            cmd = build_command(engine, text, mode, meta["session_id"] if is_follow_up else None,
                                system=meta.get("system_prompt"),
                                extra_args=(meta.get("cli_args") or []) + ([] if is_follow_up else meta.get("first_turn_args") or []))
            emit(self.append_event({"kind": "prompt", "title": "You", "body": text, "data": {}}, turn))
            raw_path = self.dir / "raw.jsonl"
            offset = raw_path.stat().st_size if raw_path.exists() else 0
            # The engine writes straight to files, not to a pipe into this process: if the bot
            # restarts, the engine keeps running and a new bot picks up where this one stopped.
            with raw_path.open("ab") as out, (self.dir / "stderr.log").open("ab") as err:
                self._proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                              start_new_session=True)
        except FileNotFoundError as exc:
            emit(self.append_event({"kind": "error", "title": "Engine not found", "body": str(exc), "data": {}}, turn))
            return self.update_meta(status="failed", pid=None)
        except Exception:
            self.update_meta(status="failed", pid=None)
            raise
        self.update_meta(pid=self._proc.pid, raw_offset=offset, cost_before_turn=meta.get("cost_usd") or 0.0,
                         turn_started_at=time.time())
        if self._cancelled:  # stop requested while we were starting
            self._kill(self._proc.pid)
        return self._follow(emit, turn, engine, offset, meta.get("cost_usd") or 0.0, time.time(), proc=self._proc)

    def _follow(self, emit, turn: int, engine: str, offset: int, cost: float, started: float,
                proc: Optional[subprocess.Popen] = None, pid: Optional[int] = None) -> dict:
        """Read the engine's output file as it grows until the engine exits, then record the outcome.
        Works for an engine we started (proc) and for one a previous bot process started (pid)."""
        raw_path = self.dir / "raw.jsonl"
        last_text, saw_result, completed, failed_turn = "", False, False, False
        status, code = "failed", None
        buffered = b""
        last_save = 0.0
        try:
            while True:
                alive = (proc.poll() is None) if proc else pid_alive(pid)
                with raw_path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                if chunk:
                    buffered += chunk
                    offset += len(chunk)
                    *lines, buffered = buffered.split(b"\n")
                    for raw_line in lines:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            emit(self.append_event({"kind": "debug", "title": "non-JSON output", "body": line, "data": {}}, turn))
                            continue
                        if not isinstance(raw, dict):
                            continue
                        rtype = raw.get("type")
                        if rtype == "rate_limit_event":
                            save_rate_limits(raw.get("rate_limit_info") or {})
                        completed = completed or rtype in ("result", "turn.completed")
                        failed_turn = failed_turn or rtype == "turn.failed" or (rtype == "result" and raw.get("is_error"))
                        for event in normalize(engine, raw):
                            data = event.get("data") or {}
                            if event["kind"] == "session" and data.get("session_id"):
                                self.update_meta(session_id=data["session_id"])
                            if event["kind"] == "text":
                                last_text = event["body"]
                            if event["kind"] in ("result", "error") and "cost_usd" in data:
                                saw_result = True
                                cost += data.get("cost_usd") or 0.0
                            emit(self.append_event(event, turn))
                    if time.monotonic() - last_save > 1:
                        last_save = time.monotonic()
                        self.update_meta(raw_offset=offset - len(buffered))
                if not alive and not chunk:
                    break
                if not chunk:
                    time.sleep(0.15)

            if proc is not None:
                code = proc.wait()
            else:  # not our child: judge by what the engine wrote
                code = 0 if completed and not failed_turn else 1
            if engine == "codex" and not saw_result and code == 0:
                emit(self.append_event({"kind": "result", "title": "Final result", "body": last_text, "data": {}}, turn))

            if self._cancelled:
                status = "cancelled"
                emit(self.append_event({"kind": "error", "title": "Cancelled", "body": "Stopped by user", "data": {}}, turn))
            elif code != 0:
                tail = (self.dir / "stderr.log").read_text(errors="replace")[-2000:]
                emit(self.append_event({"kind": "error", "title": f"Engine exited {code}", "body": tail, "data": {"exit_code": code}}, turn))
            else:
                status = "done"
        finally:
            if proc is not None:
                self._proc = None
                if proc.poll() is None:  # leaving early (an exception): do not orphan the engine
                    self._kill(proc.pid)
                    proc.wait()
            final = self.update_meta(
                status=status,
                pid=None,
                cost_usd=round(cost, 4),
                raw_offset=offset,
                last_turn_seconds=round(time.time() - started, 1),
            )
        return final

    def adopt(self, on_event: Optional[Callable[[dict], None]] = None) -> Optional[dict]:
        """Keep following a turn a previous bot process started, if its engine is still running.
        Returns the final meta, or None when there is nothing to adopt."""
        meta = self.meta()
        if meta.get("runtime", "headless") != "headless" or meta["status"] != "running" or not pid_alive(meta.get("pid")):
            return None
        return self._follow(on_event or (lambda _e: None), meta.get("turns") or 1, meta["engine"],
                            int(meta.get("raw_offset") or 0), float(meta.get("cost_before_turn") or 0.0),
                            float(meta.get("turn_started_at") or time.time()), pid=meta["pid"])

    @staticmethod
    def _kill(pid: int) -> bool:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def cancel(self) -> bool:
        """Stop the running turn, whether it was started by this process or another."""
        self._cancelled = True
        proc = self._proc
        pid = proc.pid if proc else self.meta().get("pid")
        if not pid:
            return False
        if not self._kill(pid):
            return False
        if proc is None:
            # Another process owns the run; record the outcome ourselves.
            self.update_meta(status="cancelled", pid=None)
        return True


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        # If it is our own child that has exited, reap it: a zombie still answers kill(0).
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass  # not our child (for example, started by a previous bot process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def save_rate_limits(info: dict) -> None:
    """Claude reports its usage windows (5-hour, weekly) in the stream; keep the latest."""
    windows = info.get("unifiedWindows") or {}
    record = {"at": now_iso(), "status": info.get("status"), "windows": {
        name: {"used": w.get("utilization"), "resets_at": w.get("resetsAt")} for name, w in windows.items() if isinstance(w, dict)}}
    tmp = cos_home() / "usage.json.tmp"
    try:
        tmp.write_text(json.dumps(record))
        tmp.replace(cos_home() / "usage.json")
    except OSError:
        pass


def usage_summary() -> dict:
    """Spend today and over the last 7 days (from run records), plus Claude's latest limits."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now.timestamp() - 7 * 86400)
    spent_today = spent_week = 0.0
    runs_today = 0
    by_kind: dict[str, float] = {}
    for meta in list_runs(2000):
        try:
            created = datetime.fromisoformat(meta["created_at"])
        except (KeyError, ValueError):
            continue
        if created.timestamp() < week_start:
            break
        cost = float(meta.get("cost_usd") or 0)
        spent_week += cost
        kind = meta.get("kind") or meta.get("source") or "other"
        by_kind[kind] = by_kind.get(kind, 0) + cost
        if meta["created_at"][:10] == today:
            spent_today += cost
            runs_today += 1
    try:
        limits = json.loads((cos_home() / "usage.json").read_text())
    except (OSError, json.JSONDecodeError):
        limits = None
    return {"today_usd": round(spent_today, 2), "week_usd": round(spent_week, 2), "runs_today": runs_today,
            "week_by_kind": {k: round(v, 2) for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])}, "claude_limits": limits}


def list_runs(limit: int = 100, include_running: bool = False) -> list[dict]:
    """The newest `limit` runs by creation. include_running also keeps every older run that is still
    running: a Slack thread resumes its first run for each follow-up, so a live turn can belong to a
    run created days and hundreds of runs ago."""
    runs = []
    for path in sorted(runs_dir().iterdir(), reverse=True):
        if len(runs) >= limit and not include_running:
            break
        if not (path / "meta.json").exists() or not valid_run_id(path.name):
            continue
        try:
            meta = json.loads((path / "meta.json").read_text())
        except json.JSONDecodeError:
            continue
        if len(runs) < limit or meta.get("status") == "running":
            runs.append(meta)
    return runs
