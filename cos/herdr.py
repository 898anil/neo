"""Thin wrapper over the herdr CLI, pinned to one named herdr session.

The bot uses its own session (default "neo") so it never touches the agents
you run in your default herdr session. Attach to watch or take over:
    herdr session attach neo
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Optional

AGENT_NAME_MAX = 32
# One start at a time per bot: every run's watcher may notice a dead server at once, and a second
# start would kill the tmux session the first one just made.
_START_LOCK = threading.Lock()
_START_FAILED_AT: dict[str, float] = {}
START_RETRY_SECONDS = 60.0


class HerdrError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Herdr:
    def __init__(self, session: Optional[str] = None, binary: Optional[str] = None):
        self.session = session or os.environ.get("NEO_HERDR_SESSION", "neo")
        self.binary = binary or os.environ.get("HERDR_CLI_PATH") or shutil.which("herdr") or "herdr"
        self.env = {**os.environ, "HERDR_SESSION": self.session}  # "default" targets your own session
        # Never inherit a parent pane's context; commands must target explicit ids.
        for key in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH"):
            self.env.pop(key, None)

    # ------------------------------------------------------------------ core

    def _run(self, args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.binary, *args],
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise HerdrError("timeout", f"herdr {' '.join(args[:2])} took longer than {timeout}s") from exc
        except OSError as exc:
            raise HerdrError("exec_failed", str(exc)) from exc

    def call(self, *args: str, timeout: float = 60) -> dict:
        """Run a herdr command that answers with JSON; raise HerdrError on failure."""
        proc = self._run(list(args), timeout)
        for stream in (proc.stdout, proc.stderr):
            for line in stream.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in data:
                    err = data["error"] or {}
                    raise HerdrError(err.get("code", "error"), err.get("message", line))
                if "result" in data:
                    return data["result"]
        if proc.returncode != 0:
            raise HerdrError("exit_" + str(proc.returncode), (proc.stderr or proc.stdout).strip()[:500])
        raise HerdrError("no_json", (proc.stdout or proc.stderr).strip()[:500])

    # --------------------------------------------------------------- session

    def running(self, timeout: float = 15) -> bool:
        try:
            proc = self._run(["session", "list", "--json"], timeout=timeout)
        except HerdrError:
            return False
        try:
            sessions = json.loads(proc.stdout).get("sessions") or []
        except (json.JSONDecodeError, AttributeError):
            return False
        return any(isinstance(s, dict) and s.get("name") == self.session and s.get("running") for s in sessions)

    def ensure_server(self, wait_seconds: float = 15) -> None:
        """Start the session's server if needed.

        herdr starts a server when a client attaches, and the client needs a
        terminal, so we attach once inside a detached tmux session. The herdr
        server keeps running on its own after that.
        """
        if self.running():
            return
        with _START_LOCK:
            if self.running(timeout=5):
                return  # another watcher started it while we waited
            failed_at = _START_FAILED_AT.get(self.session)
            if failed_at is not None and time.monotonic() - failed_at < START_RETRY_SECONDS:
                raise HerdrError("server_start_cooldown", f"herdr session '{self.session}' failed to start recently")
            try:
                self._start_server(wait_seconds)
            except HerdrError:
                _START_FAILED_AT[self.session] = time.monotonic()
                raise
            _START_FAILED_AT.pop(self.session, None)

    def _start_server(self, wait_seconds: float) -> None:
        tmux = shutil.which("tmux")
        if not tmux:
            raise HerdrError("no_tmux", f"herdr session '{self.session}' is not running and tmux is not installed; run `herdr session attach {self.session}` once")
        name = f"herdr-{self.session}"
        # A stale tmux session from an earlier start would make new-session fail.
        # Timeouts: callers hold _START_LOCK, so a hung tmux must not block every watcher.
        try:
            subprocess.run([tmux, "kill-session", "-t", name], check=False, capture_output=True, timeout=10)
            subprocess.run(
                [tmux, "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
                 f"{shlex.quote(self.binary)} --session {shlex.quote(self.session)}"],
                env=self.env,
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise HerdrError("server_start_failed", f"tmux could not start herdr session '{self.session}': {exc}") from exc
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.running(timeout=3):
                return
            time.sleep(0.5)
        raise HerdrError("server_start_timeout", f"herdr session '{self.session}' did not start")

    # --------------------------------------------------------------- layout

    def create_workspace(self, cwd: str, label: str) -> tuple[str, str]:
        result = self.call("workspace", "create", "--cwd", cwd, "--label", label, "--no-focus")
        return result["workspace"]["workspace_id"], result["root_pane"]["pane_id"]

    def close_workspace(self, workspace_id: str) -> None:
        self.call("workspace", "close", workspace_id)

    # ---------------------------------------------------------------- agent

    def start_agent(self, name: str, kind: str, pane_id: str, args: tuple[str, ...] = (), shell_wait: float = 15) -> dict:
        cmd = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "60000"]
        if args:
            cmd += ["--", *args]
        # A new pane's shell needs a moment (login rc files) before herdr treats it as available.
        deadline = time.monotonic() + shell_wait
        while True:
            try:
                return self.call(*cmd, timeout=90)["agent"]
            except HerdrError as exc:
                if exc.code != "agent_pane_busy" or time.monotonic() > deadline:
                    raise
                time.sleep(0.5)

    def start_in_pane(self, name: str, kind: str, pane_id: str, command: str, timeout: float = 60) -> dict:
        """Start an agent with a command line of our own: `agent start` only runs the kind's own
        executable, and a live agent in a person's account starts through sudo and neo-run
        (cos/accounts.pane_command). herdr still sees the engine in the pane's foreground job and
        recognises it. Then as `agent start`: wait until it is ready and name it; raise agent_not_ready
        when it stops on a question at startup (the folder trust prompt), timeout when it never shows."""
        proc = self._run(["pane", "run", pane_id, command], timeout=30)
        if proc.returncode != 0:
            raise HerdrError("pane_run_failed", (proc.stderr or proc.stdout).strip()[:500])
        deadline = time.monotonic() + timeout
        while True:
            try:
                agent = self.get(pane_id)
            except HerdrError as exc:
                if exc.code in ("pane_not_found", "workspace_not_found"):
                    # `exec`: the pane closes with its command, so a launch refused at once leaves no pane
                    raise HerdrError("agent_exited", f"{kind} exited as it started in pane {pane_id}") from exc
                if exc.code != "agent_not_found":
                    raise
                agent = {}
            if agent.get("agent") == kind:
                if agent.get("agent_status") in ("idle", "done"):
                    self.call("agent", "rename", pane_id, name, timeout=15)
                    return self.get(name)
                if agent.get("agent_status") == "blocked":
                    raise HerdrError("agent_not_ready", f"{kind} is waiting for input in pane {pane_id}")
            if time.monotonic() > deadline:
                raise HerdrError("timeout", f"{kind} did not start in pane {pane_id} within {timeout:.0f}s")
            time.sleep(0.5)

    def foreground(self, pane_id: str) -> list[str]:
        """Names of the processes in the pane's foreground job (what keys typed into it reach)."""
        result = self.call("pane", "process-info", "--pane", pane_id, timeout=15)
        info = result.get("process_info") or {}
        return [os.path.basename(str(p.get("name") or "")).lstrip("-") for p in info.get("foreground_processes") or []]

    def prompt(self, name: str, text: str) -> dict:
        # No --wait: the watcher follows state changes, the bot must not block here.
        # herdr has no "--" separator, so keep text that starts with "-" from reading as an option.
        if text.startswith("-"):
            text = " " + text
        return self.call("agent", "prompt", name, text, timeout=60)

    def get(self, name: str) -> dict:
        return self.call("agent", "get", name, timeout=15)["agent"]

    def read(self, name: str, lines: int = 40, source: str = "recent-unwrapped") -> str:
        try:
            proc = self._run(["agent", "read", name, "--source", source, "--lines", str(lines)], timeout=15)
        except HerdrError:
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout

    def send_keys(self, name: str, *keys: str) -> dict:
        return self.call("agent", "send-keys", name, *keys, timeout=15)


def agent_session_id(agent: dict) -> Optional[str]:
    session = agent.get("agent_session") or {}
    return session.get("value") if session.get("kind") == "id" else None
