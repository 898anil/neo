"""Names and grouping policy for Nio-owned Herdr lanes."""

from __future__ import annotations

import os
import re


def group_for_task(prompt: str, cwd: str) -> str:
    text = f"{prompt}\n{cwd}".lower()
    if re.search(r"\b(release|deploy|ship|promot(?:e|ion)|rollout|version bump|tag v?\d)", text):
        return "Nio · Releases"
    if re.search(r"\b(sentry|alert|incident|outage|production error|logs?|monitor|watch|ci failure)", text):
        return "Nio · Operations"
    if re.search(r"\b(nio|neo|herdr|slack bot|agent routing|automation)\b", text) or "/neo" in text:
        return "Nio · Agent operations"
    if re.search(r"(?:\b(?:issue|pr|pull request)\b|#\d+)", text):
        return "Nio · Issue & PR fixes"
    base = os.path.basename(os.path.normpath(cwd)).lower()
    if "backie" in base:
        return "Nio · Backend fixes"
    if "deskie" in base:
        return "Nio · Desktop fixes"
    if "mobile" in base:
        return "Nio · Mobile fixes"
    if "design" in base:
        return "Nio · Product design"
    return "Nio · Product work"


def task_label(prompt: str, limit: int = 72) -> str:
    text = re.sub(r"\s+", " ", prompt).strip()
    issue = re.search(r"(?:issue\s*)?#(\d+)\s*:?\s*", text, re.I)
    if issue:
        number = issue.group(1)
        text = (text[:issue.start()] + text[issue.end():]).strip(" :-")
        label = f"#{number} {text}"
    else:
        label = text
    if not label:
        label = "New task"
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"
