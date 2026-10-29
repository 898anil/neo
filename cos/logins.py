"""Signing a person's own account in to Claude and Codex from Slack (`@neo login claude`).

The CLI's own sign-in runs in their account through neo-run, with nothing of the owner's lent, so the
login it saves is theirs (~/.claude, ~/.codex in their home) and their runs use it from then on:
  claude  `claude auth login` prints a link and waits for the code the browser shows; neo DMs the
          link and passes the code they reply with to the CLI's stdin.
  codex   `codex login --device-auth` prints a link and a one-time code; it finishes by itself once
          they enter the code in the browser.
  gh      `gh auth login --web` does the same for GitHub (and sets git up to use it over https).
  gcloud  `gcloud auth login --no-launch-browser` works like claude: a link, then the code pasted back
          (kubectl for GKE uses it through gke-gcloud-auth-plugin).
  doppler `doppler login` works like codex: a link and an auth code to enter there.
A sign-in that nobody finishes is stopped after WAIT_SECONDS.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import accounts

TOOLS = ("claude", "codex", "gh", "gcloud", "doppler")
PASTE = ("claude", "gcloud")  # the browser shows a code they reply with; the others finish by themselves
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
    ("gcloud", "login"): ["gcloud", "auth", "login", "--no-launch-browser", "--update-adc"],
    ("gcloud", "logout"): ["gcloud", "auth", "revoke", "--all", "--quiet"],
    ("gcloud", "status"): ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
    ("doppler", "login"): ["doppler", "login", "--yes", "--no-check-version"],
    ("doppler", "logout"): ["doppler", "logout", "--yes", "--no-check-version"],
    ("doppler", "status"): ["doppler", "me", "--no-check-version"],
}
DEVICE_CODE = {  # the one-time code a device sign-in shows, per tool
    "codex": re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b"),
    "gh": re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b"),
    "doppler": re.compile(r"auth code is:\s*([a-z]+(?:_[a-z]+){2,})"),
}
_URL = re.compile(r"https://[^\s<>\"']+")
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
        device = DEVICE_CODE.get(self.tool)
        if device:
            found = self._wait_for(lambda text: _URL.search(text) and device.search(text), timeout)
        else:
            found = self._wait_for(lambda text: _URL.search(text), timeout)
        text = self.output()
        if not found:
            self.stop()
            raise RuntimeError(_tail(text) or "the sign-in didn't start")
        code = device.search(text) if device else None
        return _URL.search(text).group(0), (code.group(1) if code else None)

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


def code_from(text: str) -> str:
    """The sign-in code in someone's reply: without the @neo mention, Slack markup, backticks or other words.
    Claude's looks like "<code>#<state>"; Google's is one long token."""
    text = re.sub(r"<[^>]*>", " ", text or "")  # <@U123>, <https://...|...>
    words = [w.strip("`*_~'\".,;:()[]") for w in text.split()]
    words = [w for w in words if re.fullmatch(r"[A-Za-z0-9_\-#/.+=]{8,}", w)]
    if not words:
        return ""
    return next((w for w in words if "#" in w), max(words, key=len))


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
    if tool == "gcloud":
        return "@" in out if code == 0 else None
    if tool == "doppler":
        return code == 0 and "error" not in out.lower()
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


def stop_orphans() -> int:
    """Sign-ins a previous neo process started and lost track of (neo restarted while one waited): stop them,
    so an old link's code can't be pasted into a new sign-in's thread. Each one's sudo (still ours by real
    uid) gets the signal, which neo-run passes on, as Login.stop does."""
    logins = {tuple(argv) for (_, action), argv in ARGV.items() if action == "login"}
    stopped = 0
    for pid in [p for p in os.listdir("/proc") if p.isdigit()] if os.path.isdir("/proc") else []:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = tuple(os.path.basename(a.decode(errors="replace")) if i == 0 else a.decode(errors="replace")
                      for i, a in enumerate(argv) if a)
        if words not in logins:
            continue
        sudo = _sudo_above(int(pid))
        if sudo:
            try:
                os.killpg(os.getpgid(sudo), 15)
                stopped += 1
            except (ProcessLookupError, PermissionError):
                pass
    return stopped


def _sudo_above(pid: int):
    """The sudo neo started this process under (walking up its parents), if neo owns it."""
    for _ in range(4):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            status = Path(f"/proc/{ppid}/status").read_text()
            comm = Path(f"/proc/{ppid}/comm").read_text().strip()
        except (OSError, ValueError, IndexError):
            return None
        real_uid = int(next(l.split()[1] for l in status.splitlines() if l.startswith("Uid:")))
        if comm == "sudo" and real_uid == os.getuid():
            return ppid
        pid = ppid
    return None
