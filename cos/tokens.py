"""Tools people connect with a personal token instead of a sign-in (Sentry).

A person pastes the token into a Slack form (a modal: what is typed there is never a message, so Slack
keeps no copy in any conversation). neo keeps it in COS_HOME/tokens.json (0600, the service account's)
and passes it only to that person's own runs, as the tool's usual environment variable, through
neo-run's launch spec (accounts.spec). A CoS answering someone else never gets it.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from .store import JsonStore

TOOLS = {  # tool -> (name people know, environment variable, where they make a token)
    "sentry": ("Sentry", "SENTRY_AUTH_TOKEN", "https://sentry.io/settings/account/api/auth-tokens/"),
}
STORE = JsonStore("tokens.json")
_TOKEN = re.compile(r"^[\x21-\x7e]{8,400}$")  # printable, no spaces


def save(user: str, tool: str, token: str) -> None:
    from . import people

    token = (token or "").strip()
    if tool not in TOOLS:
        raise ValueError(f"I don't know {tool!r}")
    if not people.valid_user(user):
        raise ValueError("not a Slack user")
    if not _TOKEN.match(token):
        raise ValueError(f"that doesn't look like a {TOOLS[tool][0]} token")
    STORE.update(lambda d: d.setdefault(user, {}).update({tool: token}))
    _private()


def remove(user: str, tool: str) -> bool:
    return bool(STORE.update(lambda d: (d.get(user) or {}).pop(tool, None)))


def connected(user: Optional[str]) -> set[str]:
    return set((STORE.read().get(user) or {}).keys()) & set(TOOLS) if user else set()


def env_for(user: Optional[str]) -> dict:
    """{VARIABLE: token} for this person's own runs."""
    mine = STORE.read().get(user) or {} if user else {}
    return {TOOLS[tool][1]: token for tool, token in mine.items() if tool in TOOLS and token}


def _private() -> None:
    try:
        os.chmod(STORE.path(), 0o600)
    except OSError:
        pass
