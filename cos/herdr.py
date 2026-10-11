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
import time
from typing import Optional

AGENT_NAME_MAX = 32


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

    def running(self) -> bool:
        try:
            proc = self._run(["session", "list", "--json"], timeout=15)
        except HerdrError:
            return False
        try:
            sessions = json.loads(proc.stdout).get("sessions", [])
        except json.JSONDecodeError:
            return False
        return any(s.get("name") == self.session and s.get("running") for s in sessions)

    def ensure_server(self, wait_seconds: float = 15) -> None:
        """Start the session's server if needed.

        herdr starts a server when a client attaches, and the client needs a
        terminal, so we attach once inside a detached tmux session. The herdr
        server keeps running on its own after that.
        """
        if self.running():
            return
        tmux = shutil.which("tmux")
        if not tmux:
            raise HerdrError("no_tmux", f"herdr session '{self.session}' is not running and tmux is not installed; run `herdr session attach {self.session}` once")
        name = f"herdr-{self.session}"
        # A stale tmux session from an earlier start would make new-session fail.
        subprocess.run([tmux, "kill-session", "-t", name], check=False, capture_output=True)
        subprocess.run(
            [tmux, "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
             f"{shlex.quote(self.binary)} --session {shlex.quote(self.session)}"],
            env=self.env,
            check=False,
            capture_output=True,
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.running():
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
