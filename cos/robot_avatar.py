"""Create recognizable robot-framed Slack avatars and serve them by opaque URL."""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import secrets
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from . import dashlink

OVERLAY = Path(__file__).resolve().parent / 'static' / 'robot-overlay.png'
_KEY_LOCK = threading.Lock()
MAX_PHOTO_BYTES = 4 * 1024 * 1024


def _directory() -> Path:
    return Path(os.environ.get('NEO_AVATAR_DIR') or Path.home() / '.neo' / 'avatars')


def _key() -> bytes:
    directory = _directory()
    with _KEY_LOCK:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'avatar.key'
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            fd, temporary = tempfile.mkstemp(prefix='.key-', dir=directory)
            try:
                with os.fdopen(fd, 'wb') as f:
                    f.write(secrets.token_bytes(32))
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                key = path.read_bytes()
            finally:
                os.unlink(temporary)
        if len(key) != 32:
            raise ValueError('Invalid avatar key')
        return key


def _safe_source(url: str) -> bool:
    try:
        p = urllib.parse.urlparse(url)
        return (p.scheme == 'https' and p.hostname == 'avatars.slack-edge.com' and p.port in (None, 443)
                and not p.username and not p.password and not p.fragment)
    except (TypeError, ValueError):
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'neo-avatar/1'})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=10) as response:
        photo = response.read(MAX_PHOTO_BYTES + 1)
    if len(photo) > MAX_PHOTO_BYTES:
        raise ValueError('Profile image is too large')
    return photo


def _public_base() -> str:
    explicit = os.environ.get('NEO_AVATAR_PUBLIC_URL') or os.environ.get('NEO_DASHBOARD_PUBLIC_URL')
    base = explicit or (dashlink.public_url() if os.environ.get('NEO_TAILSCALE_FUNNEL') == '1' else '')
    try:
        p = urllib.parse.urlparse(base)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
            return ''
        return base.rstrip('/')
    except ValueError:
        return ''


def url_for(user: str, photo_url: str) -> str | None:
    """Return a stable, unguessable public URL; None means use the original profile photo."""
    base = _public_base()
    if not base or not _safe_source(photo_url) or not re.fullmatch(r'[UW][A-Z0-9]+', user or ''):
        return None
    overlay = OVERLAY.read_bytes()
    message = b'neo-avatar-v1\0' + user.encode() + b'\0' + photo_url.encode() + hashlib.sha256(overlay).digest()
    token = hmac.new(_key(), message, hashlib.sha256).hexdigest()
    target = _directory() / (token + '.png')
    if not target.is_file():
        with Image.open(io.BytesIO(_download(photo_url))) as source:
            if source.width * source.height > 16_000_000:
                raise ValueError('Profile image dimensions are too large')
            image = ImageOps.fit(ImageOps.exif_transpose(source).convert('RGBA'), (256, 256),
                                 method=Image.Resampling.LANCZOS)
        with Image.open(io.BytesIO(overlay)) as frame:
            image.alpha_composite(frame.convert('RGBA').resize((256, 256), Image.Resampling.LANCZOS))
        fd, temporary = tempfile.mkstemp(prefix='.avatar-', dir=_directory())
        try:
            with os.fdopen(fd, 'wb') as out:
                image.save(out, format='PNG', optimize=True)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return f'{base}/neo-avatar/{token}.png'


def read(token: str) -> bytes | None:
    if not re.fullmatch(r'[a-f0-9]{64}', token or ''):
        return None
    try:
        return (_directory() / (token + '.png')).read_bytes()
    except OSError:
        return None
