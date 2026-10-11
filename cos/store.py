"""A JSON file in COS_HOME, read and written whole, atomically, under a lock."""

from __future__ import annotations

import json
import os
import threading
from typing import Callable, TypeVar

from .runner import cos_home

T = TypeVar("T")
_locks: dict[str, threading.RLock] = {}


class JsonStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = _locks.setdefault(name, threading.RLock())

    def path(self):
        return cos_home() / self.name

    def read(self) -> dict:
        with self.lock:
            try:
                data = json.loads(self.path().read_text())
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

    def write(self, data: dict) -> None:
        with self.lock:
            tmp = self.path().with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path())

    def update(self, fn: Callable[[dict], T]) -> T:
        with self.lock:
            data = self.read()
            result = fn(data)
            self.write(data)
            return result
