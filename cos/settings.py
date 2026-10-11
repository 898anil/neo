"""Choices you make from the dashboard or Slack, kept on this machine (COS_HOME/settings.json).
They win over the environment, so you can change them from the phone without a restart."""

from __future__ import annotations

import json
import os
import threading

from .runner import ENGINES, cos_home

_lock = threading.Lock()


def _path():
    return cos_home() / "settings.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(**changes) -> dict:
    """Validate and store changes; returns every setting."""
    if "default_engine" in changes and changes["default_engine"] not in ENGINES:
        raise ValueError(f"default_engine must be one of {ENGINES}")
    with _lock:
        data = {**load(), **changes}
        tmp = _path().with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_path())
    return data
