"""neo's own memory: what it learned from its mistakes and from what you told it.

Plain Markdown, so you can read, edit, delete and version it like any notes. In NEO_MEMORY_DIR
(default ~/.neo/memory):

  me.md             the owner's standing rules, preferences and decisions, in their words
  team.md           rules for everyone's runs (only the owner adds them: "remember for team: ...")
  people/<id>.md    each other person's own rules, by Slack user id (see people.py)
  lessons.md        mistakes: what went wrong, and what to do instead (shared by everyone)
  repos/<repo>.md   rules and lessons that only apply in one repository (shared by everyone)

Each entry is one bullet, with where it came from in a comment that Markdown viewers hide:

  - Prod checks fail without the prod exit node: set it first <!-- 2026-10-11 lesson run:20261011-... -->

Written: after a session you drove finishes (the card call proposes at most a few items, see
cards.py), and when you say "remember ..." in Slack. Routine work that went fine leaves nothing.
Read: brief(cwd, user) is added to the standing instructions of every agent neo starts, Claude or
Codex, so a fact learned in one engine is known to the other. A run gets the team rules, the rules of
the person who asked for it (me.md for the owner), and the shared repo notes and lessons. Memory is notes, not orders: the brief says
your current request wins.

Not to be confused with memory.py, which searches the CLIs' own transcripts.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import shutil
import threading
import time
from collections import Counter
from datetime import date
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

ME, LESSONS, TEAM, PEOPLE = "me.md", "lessons.md", "team.md", "people"
KINDS = ("rule", "lesson")
MAX_ENTRY = 300  # characters in one entry
MAX_PER_CAPTURE = 3  # entries one finished session may add
BRIEF_BUDGET = 6000  # characters of memory in an agent's brief
HEADINGS = {ME: "# What you told me", LESSONS: "# Lessons from mistakes", TEAM: "# Team rules"}
_ENTRY_RE = re.compile(r"^- (?P<text>.+?)(?:\s*<!--\s*(?P<meta>.*?)\s*-->)?\s*$")
_REPO_RE = re.compile(r"[^A-Za-z0-9._-]+")
_COMMENT_RE = re.compile(r"<!-*|-*->")
_DETAIL_RE = re.compile(r"\d|[a-z0-9][/._-][a-z0-9]")  # a version, path, flag or host; not a full stop
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{3,}")
RULE_OVERLAP = 0.6  # share of a rule's words that must come from the owner's own messages
LESSON_OVERLAP = 0.75  # a lesson sharing this share of the shorter one's words with a stored one is a repeat
LESSON_MIN_WORDS = 5  # shorter lessons are too short to judge by overlap
_lock = threading.RLock()


@contextmanager
def _locked():
    """One writer at a time, across threads and processes (the bot and a hand-run CLI)."""
    with _lock:
        memory_dir().mkdir(parents=True, exist_ok=True)
        with open(memory_dir() / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def memory_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("NEO_MEMORY_DIR") or "~/.neo/memory"))


def _repo_file(repo: str) -> Optional[Path]:
    name = _REPO_RE.sub("-", repo).strip(".-")
    return memory_dir() / "repos" / f"{name}.md" if name else None


def rules_file(user: Optional[str] = None, team: bool = False) -> Path:
    """Where rules go: team.md for the team, me.md for the owner (user None or the owner's id),
    people/<id>.md for anyone else."""
    from . import people

    if team:
        return memory_dir() / TEAM
    if people.is_owner(user):
        return memory_dir() / ME
    if not people.valid_user(user):
        raise ValueError(f"not a Slack user id: {user!r}")
    return memory_dir() / PEOPLE / f"{user}.md"


def _clean(text: str) -> str:
    text = " ".join(str(text or "").split())  # one line
    while True:  # nested markers ("<<!--!--") must not survive one pass
        cleaned = _COMMENT_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    text = text.lstrip("#>-* ").strip()
    return text[:MAX_ENTRY].rstrip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def entries(path: Path) -> list[dict]:
    """The bullets in one memory file, oldest first: {text, meta}."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = _ENTRY_RE.match(line.strip())
        if m:
            out.append({"text": m.group("text").strip(), "meta": m.group("meta") or ""})
    return out


def add(text: str, kind: str, repo: Optional[str] = None, source: str = "",
        user: Optional[str] = None, team: bool = False) -> Optional[str]:
    """Append one entry. Returns the file it went to, relative to the memory folder ("me.md",
    "repos/api.md", "people/U123.md"); None when it is empty, of an unknown kind, or already there.
    user / team: whose rule it is (see rules_file); lessons and repo notes are shared either way."""
    text = _clean(text)
    if not text or kind not in KINDS:
        return None
    path = _repo_file(repo) if repo else None
    if path is None:
        path = rules_file(user, team) if kind == "rule" else memory_dir() / LESSONS
    meta = " ".join(x for x in (date.today().isoformat(), kind, _clean(source)[:80]) if x)
    with _locked():
        if any(_norm(e["text"]) == _norm(text) or (kind == "lesson" and adds_little(text, e["text"])) for e in entries(path)):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if current is None:
            heading = HEADINGS.get(path.name) if path.parent == memory_dir() else None
            if path.parent.name == PEOPLE:
                heading = f"# What <@{path.stem}> told me"
            lead = (heading or f"# {path.stem}") + "\n\n"
        else:
            lead = "" if not current or current.endswith(b"\n") else "\n"
        # Append in place: keeps hand edits made a moment ago, and a symlinked file stays a symlink.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{lead}- {text} <!-- {meta} -->\n")
    return str(path.relative_to(memory_dir()))


def adds_little(new: str, old: str) -> bool:
    """Does `new` say nothing `old` doesn't? At least LESSON_OVERLAP of new's words are already in old,
    and none of the rest is a detail (a version, path, flag, host name). Automatic capture re-learns the
    same mistake in new words ("Shell is zsh ..." nine times); "... exit node dev-exit-1" is new."""
    wn, wo = _words(new), _words(old)
    if len(wn) < LESSON_MIN_WORDS or len(wo) < LESSON_MIN_WORDS:
        return False
    extra = wn - wo
    return len(wn & wo) / len(wn) >= LESSON_OVERLAP and not any(_DETAIL_RE.search(w) for w in extra)


def is_lesson(entry: dict) -> bool:
    """Was this entry captured as a lesson (meta "<date> lesson <source>")? Rules and hand notes aren't."""
    return entry.get("meta", "").split()[1:2] == ["lesson"]


def remove(path: Path, texts: list[str]) -> int:
    """Delete the lessons with these texts from one memory file (one line per text; rules and hand
    notes are never touched). Returns how many went. The old file is kept as <name>.bak-<time>."""
    drop = Counter(_norm(t) for t in texts)
    with _locked():
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return 0
        kept, removed = [], 0
        for line in lines:
            m = _ENTRY_RE.match(line.strip())
            key = _norm(m.group("text")) if m else None
            if key and drop[key] > 0 and is_lesson({"meta": m.group("meta") or ""}):
                drop[key] -= 1
                removed += 1
                continue
            kept.append(line)
        if removed:
            real = path.resolve()  # write through a symlink, as add() does
            shutil.copy2(real, real.with_name(f"{real.name}.bak-{time.time_ns()}"))
            tmp = real.with_name(real.name + ".tmp")
            tmp.write_text("".join(kept), encoding="utf-8")
            shutil.copymode(real, tmp)
            tmp.replace(real)
    return removed


def repo_of(cwd: Optional[str]) -> Optional[str]:
    """The repository a folder belongs to, by its main checkout's name (so a worktree counts as its
    repo). None outside a git repository."""
    if not cwd or not Path(cwd).expanduser().is_dir():
        return None
    try:
        out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(Path(cwd).expanduser()),
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    common = Path(out.stdout.strip())
    name = (common.parent if common.name == ".git" else common).name
    return name.removesuffix(".git") or None  # a bare repo "api.git" is "api"


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def from_owner(text: str, owner_text: str) -> bool:
    """Is this rule really the owner's? Most of its words must appear in what the owner wrote, so a
    rule can't be invented from a tool's output or someone else's message."""
    words = _words(text)
    if not words:
        return False
    return len(words & _words(owner_text)) >= max(1, RULE_OVERLAP * len(words))


def capture(card: dict, cwd: Optional[str], source: str, owner_text: str = "", user: Optional[str] = None,
            role: Optional[str] = None) -> int:
    """Store what a finished session's card proposes to remember. Returns how many were added.
    owner_text: everything the person who drove the session wrote; a rule must come from it.
    user: that person's Slack id (None for the owner); their rules go to their own file.
    role: the role the run had (people.ROLES); their current role when not recorded."""
    from . import people

    added = 0
    repo = None
    restricted = (role or people.role(user)) != "full"
    for item in card.get("remember") or []:
        if added >= MAX_PER_CAPTURE:
            break
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        if item["kind"] == "rule" and not from_owner(item.get("text", ""), owner_text):
            continue
        if restricted and item["kind"] == "lesson":
            continue  # shared lessons reach every run, the owner's too: only full-role sessions add them
        if restricted:
            item = {**item, "scope": "own"}  # their repo rules stay theirs
        if item.get("scope") == "repo" and repo is None:
            repo = repo_of(cwd) or ""
        if add(item.get("text", ""), item["kind"], repo=(repo or None) if item.get("scope") == "repo" else None,
               source=source, user=user):
            added += 1
    return added


_REMEMBER_RE = re.compile(r"^\s*remember(?:\s+in\s+(?P<repo>[\w.-]+)\s*:|\s+for\s+(?:the\s+)?(?P<team>team)\s*:|\s*:|\s+that\b)\s*(?P<text>\S.*)$",
                          re.I | re.S)
TEAM_REPO = "@team"  # parse_remember's "repo" for "remember for team: X"


def parse_remember(text: str) -> Optional[tuple[str, Optional[str]]]:
    """"remember: X", "remember that X", "remember in <repo>: X" -> (X, repo); "remember for team: X"
    -> (X, TEAM_REPO). None otherwise, so a task that merely starts with the word ("remember the bug?
    check it") still runs as a task."""
    m = _REMEMBER_RE.match(text or "")
    if not m or m.group("text").rstrip().endswith("?"):
        return None
    return m.group("text").strip(), TEAM_REPO if m.group("team") else m.group("repo")


def _section(title: str, path: Path, budget: int) -> str:
    head = f"### {title}\n"
    lines, used = [], len(head)
    for e in reversed(entries(path)):  # newest first
        line = f"- {e['text']}"
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return head + "\n".join(lines) if lines else ""


def brief(cwd: Optional[str], budget: int = BRIEF_BUDGET, user: Optional[str] = None) -> str:
    """The memory for an agent working in cwd for `user` (None: the owner): team rules, that person's
    rules, then notes for its repo, then lessons, newest first within each, cut to the budget. Nobody
    gets another person's rules. Empty when there is nothing to tell."""
    from . import people

    repo = repo_of(cwd)
    try:
        own = rules_file(user)
    except ValueError:
        own = None  # not a real Slack id: team rules and shared notes only
    who = "the owner" if people.is_owner(user) else f"<@{user}>"
    parts = [("Team rules (everyone's runs)", memory_dir() / TEAM, budget // 6)]
    if own:
        parts.append((f"What {who} told you (their standing rules)", own, budget // 2))
    if repo and _repo_file(repo):
        parts.append((f"Notes for the {repo} repository", _repo_file(repo), budget // 4))
    parts.append(("Lessons from earlier mistakes", memory_dir() / LESSONS, budget // 4))
    sections, left, carry = [], budget, 0
    for i, (title, path, share) in enumerate(parts):
        cap = left if i == len(parts) - 1 else min(left, share + carry)  # unused share rolls forward
        text = _section(title, path, cap)
        carry = cap - len(text)
        if text:
            sections.append(text)
            left -= len(text)
    if not sections:
        return ""
    return ("## neo's memory\nNotes neo kept from earlier sessions: team rules, the asker's own rules, and lessons from past mistakes. "
            "Use them, but if one conflicts with the current request, the current request wins.\n\n" + "\n\n".join(sections))


def all_files(user: Optional[str] = None, everyone: bool = True) -> list[dict]:
    """Memory files, for the dashboard and `@neo memory`: {name, path, entries}. everyone=True: all of
    them (the owner's view); otherwise the team's, `user`'s own and the shared ones."""
    root = memory_dir()
    if everyone:
        own = [root / ME, root / TEAM] + sorted((root / PEOPLE).glob("*.md"))
    else:
        own = [root / TEAM] + ([rules_file(user)] if user is None or _valid(user) else [])
    paths = own + [root / LESSONS] + sorted((root / "repos").glob("*.md"))
    return [{"name": str(p.relative_to(root)), "path": str(p), "entries": entries(p)} for p in paths if p.exists()]


def _valid(user: str) -> bool:
    from . import people

    return people.valid_user(user)
