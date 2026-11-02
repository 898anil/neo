"""Each person's own Linux account: the runs they start happen there, not in the owner's.

On a Linux server with NEO_ACCOUNTS=1, everyone except the owner gets an account `neo-<name>` the first
time they use neo (deploy/linux/neo-account, through sudo). Their runs start in it through
deploy/linux/neo-run, so they have their own home, their own clones in ~/code, their own Claude, Codex
and GitHub logins, and can't read neo's files or anyone else's. The owner gets one too (owner_account): the owner's runs
and neo's scheduled jobs run there, with the owner's cloud logins. Live (herdr) agents run in the same
account as a headless run for the same person would (pane_command): herdr and its panes stay neo's, the
engine inside the pane is the person's. The service account keeps the Slack tokens, the dashboard and the
herdr server, and runs the tool-less model calls (cards, digests).

Logins: a person who has logged in to a tool in their account uses that login; for anything else neo
lends the owner's, if the owner set NEO_SHARED_CLAUDE_TOKEN / NEO_SHARED_GH_TOKEN / NEO_SHARED_OPENAI_KEY,
passed for that one turn on neo-run's pipe: the model logins to everyone not blocked, GitHub to the full role
only (shared_logins).

Who may use neo at all (members_only): full members of this workspace. Guests and people from other
workspaces (Slack Connect) are refused; that needs the users:read scope.

Which account is whose is kept in ~/.neo/accounts.json (NEO_ACCOUNTS_FILE), not in a person's
settings, so nobody can point their runs at someone else's account.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional

NEO_RUN = "/usr/local/bin/neo-run"
NEO_ACCOUNT = "/usr/local/sbin/neo-account"
ENV_FOR_RUNS = ("NEO_RUN_ID", "NEO_SLACK_CHANNEL", "NEO_SLACK_THREAD", "CLAUDE_MODEL", "CODEX_MODEL",
                "NEO_A2A_TOKEN", "NEO_A2A_URL")  # the last two: neo-ask, as this run (cos/broker.py)
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("NEO_ACCOUNTS") == "1"


def members_only() -> bool:
    """Refuse guests and other workspaces: on with accounts unless NEO_MEMBERS_ONLY=0."""
    value = os.environ.get("NEO_MEMBERS_ONLY")
    return value == "1" if value is not None else enabled()


def registry_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("NEO_ACCOUNTS_FILE") or "~/.neo/accounts.json"))


def _registry() -> dict:
    try:
        data = json.loads(registry_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_registry(data: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def isolated(user: Optional[str]) -> bool:
    """Whether this person's runs go to their own account (ensure()); False means the service account:
    accounts off, the owner, or a run nobody asked for."""
    from . import people

    return enabled() and people.valid_user(user) and not people.is_owner(user)


def slug(name: str) -> str:
    """"Ajay Kumar" -> "ajay": the first word of a name, as a Linux user name can spell it."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", text)
    first = words[0] if words else ""
    if not first or not first[0].isalpha():
        first = "user" + first
    return first[:18]


def ensure(user: str, name: str, create=None) -> str:
    """This person's account, created on first use. name: how Slack names them. A name another
    person already has gets a number (neo-ajay2). create(account, user) makes the Linux account
    (neo-account through sudo); it raises ValueError when the account belongs to someone else."""
    create = create or _create
    with _lock:
        data = _registry()
        if data.get(user):
            return data[user]
        taken = set(data.values())
        base = "neo-" + slug(name)
        for n in range(1, 20):
            account = base if n == 1 else f"{base}{n}"
            if account in taken:
                continue
            try:
                create(account, user)
            except ValueError:
                continue  # an account of that name already belongs to someone else
            data[user] = account
            _save_registry(data)
            return account
    raise RuntimeError(f"no free account name for {name!r}")


def everyone() -> dict:
    """Slack id -> account, for everyone who has one."""
    return {user: account for user, account in _registry().items() if isinstance(account, str)}


def _create(account: str, user: str) -> None:
    done = subprocess.run(["sudo", "-n", NEO_ACCOUNT, "ensure", account, user],
                          capture_output=True, text=True, timeout=60)
    if done.returncode == 0:
        return
    if "isn't" in done.stderr:
        raise ValueError(done.stderr.strip())
    raise RuntimeError(f"could not create {account}: {done.stderr.strip() or done.returncode}")


def home(account: str) -> str:
    try:
        return pwd.getpwnam(account).pw_dir
    except KeyError:
        return f"/home/{account}"


def owner_account() -> str:
    """The owner's own account: their runs and neo's scheduled jobs run there, so the service account
    keeps only the Slack tokens and the owner's cloud logins live where only the owner's work reaches."""
    from . import people
    from .config import owner_name

    user = people.owner_id()
    if not people.valid_user(user):
        raise RuntimeError("set NEO_OWNER_USER (or NEO_NOTIFY_USER) to the owner's Slack id")
    return ensure(user, owner_name())


def for_user(user: Optional[str], name: Optional[str] = None) -> str:
    """The account a run for this Slack user goes to: their own (created on first use) when they are
    isolated, else the owner's (the owner, a run nobody asked for, a dashboard or scheduled one).
    name: how Slack names them, for a new account's name."""
    from . import people

    if isolated(user):
        return ensure(user, name or people.load(user).get("name") or user)
    return owner_account()


def cwd_for(account: str, requested: Optional[str]) -> str:
    """Where a run starts: ~/code unless they asked for a folder (~/x, a name under ~/code, or an
    absolute path; neo-run refuses anything outside their home). A folder under neo's own workdir
    (a scheduled job's, or one named before accounts) is the same place under the account's ~/code."""
    from .config import workdir

    root = home(account)
    if not requested:
        return f"{root}/code"
    if requested == "~" or requested.startswith("~/"):
        return root + requested[1:]
    if requested.startswith("/"):
        base = workdir().rstrip("/")
        if requested == base or requested.startswith(base + "/"):
            return f"{root}/code{requested[len(base):]}"
        return requested
    return f"{root}/code/{requested}"


def brief(account: str) -> str:
    """What a run in a person's account is told about where it is."""
    return ("## Where you run\n"
            f"- You run as the Linux account `{account}`, the asker's own, with home `{home(account)}`. "
            "Work in `~/code`; clone what you need there (`gh repo clone <owner>/<repo>`). "
            "Other people's folders and neo's own files are not readable from here.")


def shared_logins(role: str = "full") -> dict:
    """The owner's logins lent to people who have none of their own: only what the owner put in
    NEO_SHARED_*, never the bot's own variables by accident. The model logins (Claude, OpenAI) go to every
    role but blocked, so everyone's neo can run until they connect their own. The agent can read what it is
    lent, so GitHub goes to the full role only: with it, prs-only and read-only limits would be advice."""
    env = os.environ
    if role == "blocked":
        return {"claude_token": "", "gh_token": "", "openai_key": ""}
    return {"claude_token": env.get("NEO_SHARED_CLAUDE_TOKEN") or "",
            "gh_token": (env.get("NEO_SHARED_GH_TOKEN") or "") if role == "full" else "",
            "openai_key": env.get("NEO_SHARED_OPENAI_KEY") or ""}


def spec(meta: dict, argv: list[str], env: dict, files: dict) -> dict:
    """neo-run's launch spec for one turn. env: what the service account's engine would get; only
    the run's own variables pass (never the bot's Slack tokens), plus names in NEO_ACCOUNT_ENV.
    meta "lend" False: nothing of the owner's is lent (a personal brief must read the person's own
    mail and calendar, or nothing)."""
    lend = meta.get("lend", True)  # True, False, or "claude": the model only (a CoS answering someone else)
    extra = [k.strip() for k in os.environ.get("NEO_ACCOUNT_ENV", "").split(",") if k.strip()] if lend is True else []
    shared = shared_logins(meta.get("role") or "full") if lend else {}
    if lend == "claude":
        shared = {"claude_token": shared.get("claude_token", ""), "gh_token": "", "openai_key": ""}
    own_tokens = _own_tokens(meta) if lend != "claude" else {}
    if lend is True and meta.get("account") and meta.get("role") != "blocked":
        from . import people, tokens
        owner_tokens = tokens.env_for(people.owner_id())
        if owner_tokens.get("SENTRY_AUTH_TOKEN") and "SENTRY_AUTH_TOKEN" not in own_tokens:
            from . import logins
            if logins.connector_status(meta["account"], "sentry") in ("missing", "unavailable"):
                own_tokens["SENTRY_AUTH_TOKEN"] = owner_tokens["SENTRY_AUTH_TOKEN"]
        doppler = _shared_doppler(meta["account"])
        if doppler:
            own_tokens["DOPPLER_TOKEN"] = doppler
    argv = list(argv)
    claude_session = "-p" in argv or meta.get("runtime") == "herdr"  # headless, or live in a pane
    if own_tokens.get("SENTRY_AUTH_TOKEN") and argv and os.path.basename(argv[0]) == "claude" and claude_session and "--strict-mcp-config" not in argv:
        # Claude expands this variable at runtime; the token never appears in argv or a config file.
        skills = "?skills=inspect" if meta.get("role") in ("read-only", "prs-only") or meta.get("mode") == "readonly" else ""
        config = {"mcpServers": {"neo_sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp" + skills,
                  "headers": {"Authorization": "Sentry-Bearer ${SENTRY_AUTH_TOKEN}"}}}}
        at = argv.index("--") if "--" in argv else len(argv)
        argv[at:at] = ["--mcp-config", json.dumps(config)]
    return {"argv": argv, "cwd": meta["cwd"], "files": files,
            "env": {k: env[k] for k in (*ENV_FOR_RUNS, *extra) if env.get(k)},
            "shared": shared,
            # the person's own tool tokens (cos/tokens.py), never to a run answering someone else
            "tokens": own_tokens,
            "git_name": meta.get("git_name") or "", "git_email": meta.get("git_email") or ""}


def _shared_doppler(account: str) -> str:
    """Lend the owner's CLI token only if this account has no Doppler login."""
    from . import logins, people
    try:
        owner = _registry().get(people.owner_id())
        if not owner or account == owner or logins.signed_in(account, "doppler") is not False:
            return ""
        return logins.doppler_token(owner)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ""


def _own_tokens(meta: dict) -> dict:
    from . import people, tokens

    return tokens.env_for(meta.get("slack_user") or people.owner_id())


def user_of(account: str) -> str:
    """Whose account this is, for a command run in it without a run (cos/logins.start): the person's Slack id;
    None for the owner's own account; "-" (nobody, so no tokens at all) for an account neo doesn't know.
    A lookup only: it never creates an account."""
    from . import people

    for user, name in everyone().items():
        if name == account:
            return None if people.is_owner(user) else user
    return "-"


def command(account: str, spec_fd: int, *more_fds: int) -> list[str]:
    """sudo keeps fds below -C open: one above the highest fd neo-run needs (the spec pipe, and for a live
    agent the transcript mirror)."""
    keep = max((spec_fd, *more_fds)) + 1
    return ["sudo", "-n", "-C", str(max(keep, 3)), "-u", account, NEO_RUN, "--spec-fd", str(spec_fd)]


# A live agent's launch, typed into its herdr pane's shell (cos/interactive.py). The shell opens the spec
# FIFO on SPEC_FD and the transcript mirror on MIRROR_FD for sudo; neither is ever readable by anyone else.
SPEC_FD, MIRROR_FD = 3, 4
SPEC_WAIT_SECONDS = 120.0


def pane_command(account: str, spec_fifo: str, mirror: str) -> str:
    """The line a herdr pane runs to start a live agent as `account`: sudo + neo-run, exactly the sudoers
    rule's `neo-run --spec-fd *`. Nothing secret is on it (that is all in the spec, read from a FIFO
    offer_spec feeds). The leading space keeps it out of the shell's history (HISTCONTROL=ignorespace)."""
    argv = command(account, SPEC_FD, MIRROR_FD)
    return (" " + shlex.join(argv) + f" {SPEC_FD}<{shlex.quote(spec_fifo)} {MIRROR_FD}>>{shlex.quote(mirror)}")


def offer_spec(path: Path, data: bytes, wait: float = SPEC_WAIT_SECONDS) -> threading.Thread:
    """Hand data to whoever opens the FIFO at path first (the pane's shell, for neo-run), then remove it.
    A FIFO, not a file: the spec (lent logins, tokens) is never on disk, and it is gone once read. Made
    0600 and owned by neo, so only neo (or root) can open it; nobody opening it within wait: removed."""
    if path.exists() or path.is_symlink():
        path.unlink()
    os.mkfifo(path, 0o600)

    def feed() -> None:
        deadline = time.monotonic() + wait
        fd = None
        try:
            while fd is None:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)  # ENXIO until a reader has it open
                except OSError:
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.1)
            try:
                path.unlink()  # its reader has it open: nobody else can open it from here on
            except OSError:
                pass
            try:
                os.set_blocking(fd, True)
                if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                    return
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            except OSError:
                pass  # the reader went away; its pane says why
        finally:
            if fd is not None:
                os.close(fd)
            try:
                path.unlink()
            except OSError:
                pass

    thread = threading.Thread(target=feed, daemon=True, name=f"spec-{path.parent.name}")
    thread.start()
    return thread


def member_refusal(info: Optional[dict], own_team: Optional[str]) -> Optional[str]:
    """Why this Slack user may not use neo (users.info's user), or None. Unknown means no."""
    if not info:
        return "I couldn't check your Slack account, so I can't run anything for you yet."
    if info.get("deleted") or info.get("is_bot"):
        return "I only work for people."
    if info.get("is_restricted") or info.get("is_ultra_restricted"):
        return "I'm not available to guests in this workspace."
    if info.get("is_stranger") or (own_team and info.get("team_id") and info["team_id"] != own_team):
        return "I only work for members of this workspace."
    return None


def display_name(info: Optional[dict]) -> str:
    profile = (info or {}).get("profile") or {}
    return (profile.get("display_name") or profile.get("real_name") or (info or {}).get("real_name")
            or (info or {}).get("name") or "")
