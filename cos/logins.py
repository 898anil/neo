"""Signing a person's own account in to Claude and Codex from Slack (`@neo login claude`).

The CLI's own sign-in runs in their account through neo-run, with nothing of the owner's lent, so the
login it saves is theirs (~/.claude, ~/.codex in their home) and their runs use it from then on:
  claude  `claude auth login` prints a link and waits for the code the browser shows; neo DMs the
          link and passes the code they reply with to the CLI's stdin.
  codex   `codex login --device-auth` prints a link and a one-time code; it finishes by itself once
          they enter the code in the browser.
  gh      `gh auth login --web` does the same for GitHub (and sets git up to use it over https).
A sign-in that nobody finishes is stopped after WAIT_SECONDS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Optional

from . import accounts

TOOLS = ("claude", "codex", "gh")
WAIT_SECONDS = 15 * 60
ARGV = {
    ("claude", "login"): ["claude", "auth", "login"],
    ("claude", "logout"): ["claude", "auth", "logout"],
    ("claude", "status"): ["claude", "auth", "status"],
    ("codex", "login"): ["codex", "login", "--device-auth"],
    ("codex", "logout"): ["codex", "logout"],
    ("codex", "status"): ["codex", "login", "status"],
    ("gh", "login"): ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
    ("gh", "logout"): ["gh", "auth", "logout", "--hostname", "github.com"],
    ("gh", "status"): ["gh", "auth", "status", "--hostname", "github.com"],
}
_URL = re.compile(r"https://[^\s<>\"']+")
_DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def start(account: str, argv: list[str], stdin=subprocess.PIPE) -> subprocess.Popen:
    """The CLI in this account, nothing lent, stdout piped (stderr joins it); stdin piped unless given."""
    spec = json.dumps(accounts.spec({"cwd": accounts.cwd_for(account, None), "lend": False}, argv, {}, {})).encode()
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(accounts.command(account, read_fd), cwd="/", stdin=stdin,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
                                pass_fds=(read_fd,), env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    with os.fdopen(write_fd, "wb") as fh:  # small: fits the pipe's buffer
        fh.write(spec)
    return proc


class Login:
    """One sign-in in progress. Reads the CLI's output on a thread; link() waits for what to show."""

    def __init__(self, user: str, tool: str, proc: subprocess.Popen) -> None:
        self.user, self.tool, self.proc = user, tool, proc
        self.started = time.monotonic()
        self._out: list[str] = []
        self._changed = threading.Condition()
        threading.Thread(target=self._read, daemon=True, name=f"login-{tool}").start()

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            with self._changed:
                self._out.append(_ANSI.sub("", line.decode(errors="replace")))
                self._changed.notify_all()
        self.proc.wait()
        for pipe in (self.proc.stdout, self.proc.stdin):
            try:
                pipe.close()
            except OSError:
                pass
        with self._changed:
            self._changed.notify_all()

    def output(self) -> str:
        with self._changed:
            return "".join(self._out)

    def _wait_for(self, test, timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self._changed:
            while not test("".join(self._out)):
                left = end - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return test("".join(self._out))
                self._changed.wait(min(left, 1.0))
            return True

    def link(self, timeout: float = 60) -> tuple[str, Optional[str]]:
        """(sign-in link, codex's one-time code or None). Raises RuntimeError when the CLI shows none."""
        if self.tool != "claude":
            found = self._wait_for(lambda text: _URL.search(text) and _DEVICE_CODE.search(text), timeout)
        else:
            found = self._wait_for(lambda text: _URL.search(text), timeout)
        text = self.output()
        if not found:
            self.stop()
            raise RuntimeError(_tail(text) or "the sign-in didn't start")
        code = _DEVICE_CODE.search(text)
        return _URL.search(text).group(0), (code.group(0) if self.tool != "claude" and code else None)

    def answer(self, code: str, timeout: float = 90) -> tuple[bool, str]:
        """Pass the browser's code to `claude auth login`. (signed in, what to tell them)."""
        before = len(self.output())
        try:
            self.proc.stdin.write(code.strip().encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):  # ValueError: closed after the CLI ended
            return False, "That sign-in had already ended. Start again with `@neo login claude`."
        rejected = lambda text: "invalid" in text[before:].lower()  # noqa: E731
        self._wait_for(lambda text: rejected(text) or self.proc.poll() is not None, timeout)
        if self.proc.poll() == 0:
            self._wait_for(lambda text: self.proc.stdout.closed, 5)
            return True, ""
        if rejected(self.output()):
            return False, "That code didn't work. Copy the whole code from the browser and reply with it again."
        if self.proc.poll() is None:
            return False, "Still waiting for Claude to confirm; reply with the code again if nothing happens."
        return False, _tail(self.output()[before:]) or "the sign-in failed"

    def wait(self, timeout: float = WAIT_SECONDS) -> bool:
        """For codex: True once they finished in the browser and the CLI saved the login."""
        try:
            ok = self.proc.wait(timeout=timeout) == 0
            self._wait_for(lambda text: self.proc.stdout.closed, 5)
            return ok
        except subprocess.TimeoutExpired:
            self.stop()
            return False

    def expired(self) -> bool:
        return time.monotonic() - self.started > WAIT_SECONDS

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)  # sudo passes it to neo-run, which stops the CLI
            except (ProcessLookupError, PermissionError):
                pass


def begin(user: str, account: str, tool: str) -> Login:
    if tool not in TOOLS:
        raise ValueError(f"I can sign you in to {' or '.join(TOOLS)}")
    return Login(user, tool, start(account, ARGV[(tool, "login")]))


def run(account: str, tool: str, action: str, timeout: float = 60) -> tuple[int, str]:
    """A quick logout/status command in the account: (exit code, output)."""
    proc = start(account, ARGV[(tool, action)], stdin=subprocess.DEVNULL)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 15)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            out, _ = proc.communicate()
    return proc.returncode, _ANSI.sub("", out.decode(errors="replace"))


def signed_in(account: str, tool: str) -> Optional[bool]:
    """Whether the account has its own login for this tool; None when that can't be told."""
    try:
        code, out = run(account, tool, "status")
    except (OSError, subprocess.SubprocessError):
        return None
    if tool == "claude":
        match = re.search(r"\{.*\}", out, re.S)
        try:
            return bool(json.loads(match.group(0)).get("loggedIn")) if match else None
        except json.JSONDecodeError:
            return None
    if "not logged in" in out.lower() or "not logged into" in out.lower():
        return False
    return True if code == 0 and "logged in" in out.lower() else None


def _tail(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("neo-run:")]
    return " ".join(lines[-3:])[:300]
