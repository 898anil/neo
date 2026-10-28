"""Each person's own Linux account: the runs they start happen there, not in the owner's.

On a Linux server with NEO_ACCOUNTS=1, everyone except the owner gets an account `neo-<name>` the first
time they use neo (deploy/linux/neo-account, through sudo). Their runs start in it through
deploy/linux/neo-run, so they have their own home, their own clones in ~/code, their own Claude, Codex
and GitHub logins, and can't read neo's files or anyone else's. The owner gets one too (owner_account): the owner's runs
and neo's scheduled jobs run there, with the owner's cloud logins. The service account keeps the Slack
tokens, the dashboard and live (herdr) agents, and runs the tool-less model calls (cards, digests).

Logins: a person who has logged in to a tool in their account uses that login; for anything else neo
lends the owner's, if the owner set NEO_SHARED_CLAUDE_TOKEN / NEO_SHARED_GH_TOKEN / NEO_SHARED_OPENAI_KEY,
passed for that one turn on neo-run's pipe, to the full role only (shared_logins).

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
import subprocess
import threading
import unicodedata
from pathlib import Path
from typing import Optional

NEO_RUN = "/usr/local/bin/neo-run"
NEO_ACCOUNT = "/usr/local/sbin/neo-account"
ENV_FOR_RUNS = ("NEO_RUN_ID", "NEO_SLACK_CHANNEL", "NEO_SLACK_THREAD", "CLAUDE_MODEL", "CODEX_MODEL")
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
    NEO_SHARED_*, never the bot's own variables by accident. The agent can read what it is lent, so
    only the full role gets any: prs-only and read-only people use their own logins or none."""
    env = os.environ
    if role != "full":
        return {"claude_token": "", "gh_token": "", "openai_key": ""}
    return {"claude_token": env.get("NEO_SHARED_CLAUDE_TOKEN") or "", "gh_token": env.get("NEO_SHARED_GH_TOKEN") or "",
            "openai_key": env.get("NEO_SHARED_OPENAI_KEY") or ""}


def spec(meta: dict, argv: list[str], env: dict, files: dict) -> dict:
    """neo-run's launch spec for one turn. env: what the service account's engine would get; only
    the run's own variables pass (never the bot's Slack tokens), plus names in NEO_ACCOUNT_ENV.
    meta "lend" False: nothing of the owner's is lent (a personal brief must read the person's own
    mail and calendar, or nothing)."""
    lend = meta.get("lend", True)
    extra = [k.strip() for k in os.environ.get("NEO_ACCOUNT_ENV", "").split(",") if k.strip()] if lend else []
    return {"argv": argv, "cwd": meta["cwd"], "files": files,
            "env": {k: env[k] for k in (*ENV_FOR_RUNS, *extra) if env.get(k)},
            "shared": shared_logins(meta.get("role") or "full") if lend else {},
            "git_name": meta.get("git_name") or "", "git_email": meta.get("git_email") or ""}


def command(account: str, spec_fd: int) -> list[str]:
    """sudo keeps fds below -C open: the spec pipe is the highest one neo-run needs."""
    return ["sudo", "-n", "-C", str(max(spec_fd + 1, 3)), "-u", account, NEO_RUN, "--spec-fd", str(spec_fd)]


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
