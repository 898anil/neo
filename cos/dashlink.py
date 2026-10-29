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


_SESSIONS = JsonStore("dash_sessions.json")  # Slack id -> session generation; revoke() bumps it


def _key() -> bytes:
    """The signing key: written whole (temp file, then rename) and never shorter than 32 bytes, so a failed
    first write can't leave an empty key that would let anyone sign a session."""
    path = Path(cos_home()) / "dash.key"
    try:
        key = path.read_bytes()
    except OSError:
        key = b""
    if len(key) >= 32:
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secrets.token_bytes(32))
    if len(path.read_bytes() if path.exists() else b"") < 32:
        os.replace(tmp, path)  # another thread may have written one first; the longest-lived wins
    else:
        tmp.unlink(missing_ok=True)
    key = path.read_bytes()
    if len(key) < 32:
        raise RuntimeError("dashboard signing key is unusable")
    return key


def _same(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(a.encode(), b.encode())
    except (TypeError, UnicodeEncodeError):
        return False


def _generation(user: str) -> int:
    try:
        return int((_SESSIONS.read() or {}).get(user, 0))
    except (TypeError, ValueError):
        return 0


def revoke(user: str) -> None:
    """Sign this person out everywhere (logout, blocked, gone from the workspace)."""
    _SESSIONS.update(lambda d: d.update({user: int(d.get(user, 0)) + 1}) or d.pop(f"{user}:issued", None))


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
    if not _same(_sign("link", payload), sig) or int(exp) < now:
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
    _SESSIONS.update(lambda d: d.update({f"{user}:issued": now or time.time()}))
    payload = f"{user}.{int((now or time.time()) + SESSION_DAYS * 86400)}.{_generation(user)}"
    return f"{payload}.{_sign('session', payload)}"


def session_user(value: str, now: Optional[float] = None) -> Optional[str]:
    try:
        user, exp, gen, sig = str(value).split(".")
    except ValueError:
        return None
    payload = f"{user}.{exp}.{gen}"
    try:
        alive = int(exp) >= (now or time.time()) and int(gen) == _generation(user)
    except ValueError:
        return None
    return user if alive and _same(_sign("session", payload), sig) else None


def public_url() -> str:
    return (os.environ.get("NEO_DASHBOARD_PUBLIC_URL") or os.environ.get("NEO_DASHBOARD_URL") or "").rstrip("/")


def link(user: str) -> Optional[str]:
    base = public_url()
    return f"{base}/login?t={link_token(user)}" if base else None


def session_alive(user: str) -> bool:
    """Whether a session for this person could still be valid (not revoked since the last link was made)."""
    issued = (_SESSIONS.read() or {}).get(f"{user}:issued")
    return bool(issued) and float(issued) > time.time() - SESSION_DAYS * 86400
