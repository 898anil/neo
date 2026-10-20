"""Voice notes: a Slack audio clip becomes the prompt, and the answer also comes back spoken.

Speech-to-text runs on this machine: mlx-whisper in its own venv (NEO_VOICE_PYTHON, default
~/.neo/voice/venv), driven by cos/stt_worker.py. Text-to-speech is macOS `say`. Audio only
leaves the machine as the Slack download and upload; no speech API is called.

Needs the Slack scopes files:read (download the clip) and files:write (upload the reply).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

VOICE_HOME = Path(os.environ.get("NEO_VOICE_HOME") or Path.home() / ".neo" / "voice")
VOICE_PYTHON = Path(os.environ.get("NEO_VOICE_PYTHON") or VOICE_HOME / "venv" / "bin" / "python")
STT_MODEL = os.environ.get("NEO_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
TTS_VOICE = os.environ.get("NEO_TTS_VOICE", "Samantha")
STT_WORKER = Path(__file__).resolve().parent / "stt_worker.py"

MAX_AUDIO_BYTES = 50 * 1024 * 1024  # a ~30 minute clip; anything bigger is not a voice note
STT_TIMEOUT = 300
TTS_TIMEOUT = 120
MAX_SPOKEN_CHARS = 1500  # about a minute and a half of speech; the full answer is in the thread
_SLACK_FILE_HOSTS = {"files.slack.com"}


class VoiceError(RuntimeError):
    pass


def audio_files(event: dict) -> list[dict]:
    """The audio clips attached to a Slack message (voice clips, or uploaded audio files)."""
    return [f for f in event.get("files") or []
            if f.get("subtype") == "slack_audio" or str(f.get("mimetype", "")).startswith("audio/")]


class _SlackOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto redirects, so never follow one off Slack's file
    host. Slack redirects a token without files:read to its sign-in page, which lands here too."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in _SLACK_FILE_HOSTS:
            raise VoiceError("Slack would not hand over the clip (the app needs the files:read scope)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SlackOnlyRedirects)


def _download(file: dict, token: str, dest: Path) -> None:
    url = file.get("url_private_download") or file.get("url_private")
    if not url:
        raise VoiceError("the clip has no download link (is the files:read scope granted?)")
    # The bot token goes in the request, so only ever send it to Slack's own file host.
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _SLACK_FILE_HOSTS:
        raise VoiceError(f"refusing to download the clip from {parsed.hostname!r}")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with _OPENER.open(req, timeout=60) as resp:  # https to files.slack.com only, redirects included
        if "text/html" in resp.headers.get("Content-Type", ""):
            # Slack answers a token without files:read with its sign-in page, not an error.
            raise VoiceError("Slack would not hand over the clip (the app needs the files:read scope)")
        data = resp.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise VoiceError("the clip is too long")
    dest.write_bytes(data)


def _slack_transcript(file: dict) -> str:
    """Slack's own transcript of a clip, used only when local speech-to-text fails."""
    t = file.get("transcription") or {}
    if t.get("status") != "complete":
        return ""
    return ((t.get("preview") or {}).get("content") or "").strip()


def transcribe(file: dict, token: str) -> str:
    """Download one Slack audio file and return what was said. Raises VoiceError."""
    with tempfile.TemporaryDirectory(prefix="neo-voice-") as tmp:
        clip = Path(tmp) / "clip"
        try:
            _download(file, token, clip)
        except VoiceError:
            if fallback := _slack_transcript(file):
                return fallback
            raise
        except OSError as exc:
            if fallback := _slack_transcript(file):
                return fallback
            raise VoiceError(f"download failed: {exc}") from exc
        if not VOICE_PYTHON.exists():
            if fallback := _slack_transcript(file):
                return fallback
            raise VoiceError(f"speech-to-text is not installed ({VOICE_PYTHON} is missing)")
        try:
            out = subprocess.run(
                [str(VOICE_PYTHON), "-I", str(STT_WORKER), STT_MODEL, str(clip)],
                capture_output=True, text=True, timeout=STT_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if fallback := _slack_transcript(file):
                return fallback
            raise VoiceError("speech-to-text timed out") from exc
        text = out.stdout.strip()
        if out.returncode != 0 or not text:
            if fallback := _slack_transcript(file):
                return fallback
            detail = (out.stderr.strip().splitlines() or ["no speech found"])[-1]
            raise VoiceError(f"speech-to-text failed: {detail[:200]}")
        return text


_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_SLACK_LINK = re.compile(r"<(?:https?|mailto):[^|>]+\|([^>]+)>")
_SLACK_BARE = re.compile(r"<(?:https?|mailto):[^>]+>")
_SLACK_REF = re.compile(r"<[@#!][^>|]*(?:\|([^>]+))?>")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile(r":[a-z][a-z0-9_+\-]*:")  # not "10:30:45"
_STATUS_LINE = re.compile(r"^\s*Status:.*$", re.M)
_MARKERS = re.compile(r"[*_~`>#|]+")
_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)


def spoken_text(answer: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """The answer as something worth listening to: no code, links read as their label, no
    markdown symbols, and cut at a sentence once it gets long."""
    text = _STATUS_LINE.sub("", answer)
    text = _CODE_BLOCK.sub(" (code is in the thread) ", text)
    text = _SLACK_LINK.sub(r"\1", text)
    text = _SLACK_BARE.sub("a link", text)
    text = _SLACK_REF.sub(lambda m: m.group(1) or "", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _EMOJI.sub("", text)
    text = _BULLET.sub("", text)
    text = _MARKERS.sub("", text)
    # Each line becomes a sentence so headings and list items get a pause.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
    head = text[: cut + 1] if cut > limit // 3 else text[:limit].rsplit(" ", 1)[0] + "."
    return head + " The rest is in the thread."


def synthesize(text: str, dest: Path) -> Path:
    """Speak `text` into an AAC .m4a file Slack plays inline. Raises VoiceError."""
    script = dest.with_suffix(".txt")
    script.write_text(text, encoding="utf-8")  # -f, so the text is never read as options
    base = ["say", "-o", str(dest), "--file-format=m4af", "--data-format=aac", "-f", str(script)]
    for cmd in ([*base[:1], "-v", TTS_VOICE, *base[1:]], base):  # fall back to the system voice
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=TTS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceError(f"text-to-speech failed: {exc}") from exc
        if out.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
    raise VoiceError(f"text-to-speech failed: {out.stderr.strip()[:200]}")


def speak_answer(answer: str, workdir: Optional[Path] = None) -> Optional[Path]:
    """An .m4a of the answer for upload, or None when there is nothing worth saying."""
    text = spoken_text(answer)
    if not text:
        return None
    folder = Path(tempfile.mkdtemp(prefix="neo-voice-", dir=workdir))
    try:
        return synthesize(text, folder / "neo-reply.m4a")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)  # it holds the answer's text
        raise
