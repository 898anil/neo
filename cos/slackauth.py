"""Connecting a person's own Slack to their neo ("connect my slack"), so it can search and read Slack with
their access: the channels they're in, and files shared there.

Slack's OAuth: neo DMs a link to Slack's consent page with a signed, single-use state naming the person.
After they allow it, Slack sends their browser to neo's /slack/oauth page; if that page isn't reachable
from where they are, they copy the address from the browser and reply with it in the DM thread. neo
swaps the code for a user token and keeps it only if the Slack account that allowed it is the person the
link was made for. The token stays in neo (tokens.json, 0600): agents reach Slack through `neo-slack`,
which neo serves with it, so they never see it. DMs aren't in the scopes.

Needs SLACK_CLIENT_ID and SLACK_CLIENT_SECRET (the app's Basic Information page) and
<dashboard URL>/slack/oauth listed as a Redirect URL in the app's OAuth & Permissions page.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from typing import Optional

from . import dashlink
from .store import JsonStore

USER_SCOPES = ("search:read", "channels:history", "groups:history", "channels:read", "groups:read", "files:read", "users:read")
LINK_SECONDS = 600
_USED = JsonStore("slack_oauth_used.json")


def configured() -> bool:
    return bool(os.environ.get("SLACK_CLIENT_ID") and os.environ.get("SLACK_CLIENT_SECRET") and dashlink.public_url())


def redirect_uri() -> str:
    return f"{dashlink.public_url()}/slack/oauth"


def authorize_url(user: str) -> str:
    payload = f"{user}.{int(time.time() + LINK_SECONDS)}.{secrets.token_hex(8)}"
    state = f"{payload}.{dashlink._sign('slack-oauth', payload)}"
    return "https://slack.com/oauth/v2/authorize?" + urllib.parse.urlencode({
        "client_id": os.environ.get("SLACK_CLIENT_ID", ""), "user_scope": ",".join(USER_SCOPES),
        "redirect_uri": redirect_uri(), "state": state})


def _state_user(state: str) -> Optional[str]:
    """The Slack id a link was made for, once; None when forged, expired or used."""
    try:
        user, exp, nonce, sig = str(state).split(".")
    except ValueError:
        return None
    payload = f"{user}.{exp}.{nonce}"
    try:
        if not dashlink._same(dashlink._sign("slack-oauth", payload), sig) or int(exp) < time.time():
            return None
    except ValueError:
        return None

    def spend(used: dict) -> bool:
        now = time.time()
        for old in [n for n, e in used.items() if e < now]:
            used.pop(old)
        if nonce in used:
            return False
        used[nonce] = int(exp)
        return True
    return user if _USED.update(spend) else None


def from_pasted(text: str) -> tuple[str, str]:
    """(code, state) from the address someone copied out of their browser; ("", "") if there isn't one."""
    text = re.sub(r"<([^|>]+)(\|[^>]*)?>", r"\1", text or "").replace("&amp;", "&")
    m = re.search(r"\S*[?&]code=[^\s&]+\S*", text)
    if not m:
        return "", ""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(m.group(0)).query)
    return (query.get("code") or [""])[0], (query.get("state") or [""])[0]


def _exchange(code: str) -> dict:
    body = urllib.parse.urlencode({"client_id": os.environ.get("SLACK_CLIENT_ID", ""),
                                   "client_secret": os.environ.get("SLACK_CLIENT_SECRET", ""),
                                   "code": code, "redirect_uri": redirect_uri()}).encode()
    req = urllib.request.Request("https://slack.com/api/oauth.v2.access", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def connect(code: str, state: str, exchange=None) -> str:
    """Finish a sign-in: the Slack id it connected. Raises ValueError with a reason people can act on."""
    from . import tokens

    user = _state_user(state)
    if not user:
        raise ValueError("that sign-in link expired or was already used; say \"connect my slack\" for a new one")
    data = (exchange or _exchange)(code)
    if not data.get("ok"):
        raise ValueError(f"Slack didn't accept it ({data.get('error')}); say \"connect my slack\" to try again")
    authed = data.get("authed_user") or {}
    if authed.get("id") != user:
        raise ValueError("that Slack account isn't the one the link was for; sign in as yourself")
    if not authed.get("access_token"):
        raise ValueError("Slack didn't give a user token; say \"connect my slack\" to try again")
    tokens.STORE.update(lambda d: d.setdefault(user, {}).update({"_slack": authed["access_token"]}))
    tokens._private()
    return user


def token(user: Optional[str]) -> Optional[str]:
    from . import tokens

    return (tokens.STORE.read().get(user) or {}).get("_slack") if user else None


def disconnect(user: str) -> bool:
    from . import tokens

    return bool(tokens.STORE.update(lambda d: (d.get(user) or {}).pop("_slack", None)))
