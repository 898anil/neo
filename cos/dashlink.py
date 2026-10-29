"""Signing in to the web dashboard from Slack, for everyone.

Slack already knows who someone is, so neo hands them a link (the Home tab's "Open my dashboard", or
"open my dashboard" in plain words). The link is signed, works once, and expires after LINK_SECONDS;
opening it sets a signed session cookie for SESSION_DAYS. A person's session sees only their own work
(cos/server.py, /api/me); the owner's session sees the whole dashboard. The signing key lives in
COS_HOME/dash.key (0600).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from .runner import cos_home
from .store import JsonStore

LINK_SECONDS = 600
SESSION_DAYS = 7
COOKIE = "neo_session"
_USED = JsonStore("dash_used.json")  # link nonces already redeemed (until they would have expired anyway)


def _key() -> bytes:
    path = Path(cos_home()) / "dash.key"
    try:
        return path.read_bytes()
    except OSError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes()
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


def _sign(kind: str, payload: str) -> str:
    return hmac.new(_key(), f"{kind}\0{payload}".encode(), hashlib.sha256).hexdigest()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _unb64(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()


def link_token(user: str, now: Optional[float] = None) -> str:
    payload = f"{user}.{int((now or time.time()) + LINK_SECONDS)}.{secrets.token_hex(8)}"
    return f"{_b64(payload)}.{_sign('link', payload)}"


def redeem(token: str, now: Optional[float] = None) -> Optional[str]:
    """The Slack id a sign-in link was made for, once; None when it's forged, expired or used."""
    try:
        packed, sig = str(token).rsplit(".", 1)
        payload = _unb64(packed)
        user, exp, nonce = payload.split(".")
    except (ValueError, UnicodeDecodeError):
        return None
    now = now or time.time()
    if not hmac.compare_digest(_sign("link", payload), sig) or int(exp) < now:
        return None

    def spend(used: dict) -> bool:
        for old in [n for n, e in used.items() if e < now]:
            used.pop(old)
        if nonce in used:
            return False
        used[nonce] = int(exp)
        return True
    return user if _USED.update(spend) else None


def session_value(user: str, now: Optional[float] = None) -> str:
    payload = f"{user}.{int((now or time.time()) + SESSION_DAYS * 86400)}"
    return f"{payload}.{_sign('session', payload)}"


def session_user(value: str, now: Optional[float] = None) -> Optional[str]:
    try:
        user, exp, sig = str(value).split(".")
    except ValueError:
        return None
    payload = f"{user}.{exp}"
    try:
        alive = int(exp) >= (now or time.time())
    except ValueError:
        return None
    return user if alive and hmac.compare_digest(_sign("session", payload), sig) else None


def public_url() -> str:
    return (os.environ.get("NEO_DASHBOARD_PUBLIC_URL") or os.environ.get("NEO_DASHBOARD_URL") or "").rstrip("/")


def link(user: str) -> Optional[str]:
    base = public_url()
    return f"{base}/login?t={link_token(user)}" if base else None
