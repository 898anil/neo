"""Your GitHub work: your own open pull requests.

One GraphQL query through the logged-in `gh` CLI (no tokens of our own). The result is
cached and refreshed in the background, because the query takes a few seconds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .config import workdir

QUERY = """
query {
  mine: search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 25) { nodes { ...pr } }
}
fragment pr on PullRequest {
  number title url isDraft updatedAt reviewDecision headRefName
  author { login }
  repository { nameWithOwner name }
  additions deletions
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""
FRESH_SECONDS = 120

_lock = threading.Lock()
_state: dict = {"data": None, "fetched_at": 0.0, "error": None, "refreshing": False}
_repo_dirs: dict = {"map": {}, "at": 0.0}


def _gh() -> str:
    return os.environ.get("GH_CLI_PATH") or shutil.which("gh") or "gh"


def _pr(node: dict) -> dict:
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    return {
        "repo": node["repository"]["nameWithOwner"],
        "repo_name": node["repository"]["name"],
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "draft": node.get("isDraft", False),
        "updated_at": node.get("updatedAt"),
        "review": node.get("reviewDecision"),  # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | None
        "ci": (commit.get("statusCheckRollup") or {}).get("state"),  # SUCCESS | FAILURE | PENDING | ERROR | None
        "branch": node.get("headRefName"),
        "author": (node.get("author") or {}).get("login"),
        "size": (node.get("additions") or 0) + (node.get("deletions") or 0),
    }


def _fetch() -> None:
    try:
        proc = subprocess.run(
            [_gh(), "api", "graphql", "-f", f"query={QUERY}"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or "gh failed")
        data = json.loads(proc.stdout)["data"]
        result = {
            "mine": [_pr(n) for n in data["mine"]["nodes"] if n],
        }
        with _lock:
            _state.update(data=result, fetched_at=time.time(), error=None)
    except Exception as exc:  # noqa: BLE001 - report it on the page instead
        with _lock:
            _state.update(error=str(exc))
    finally:
        with _lock:
            _state["refreshing"] = False


def prs(force: bool = False) -> dict:
    """Cached lists; starts a background refresh when stale. The first call waits for data."""
    with _lock:
        stale = force or time.time() - _state["fetched_at"] > FRESH_SECONDS
        start = stale and not _state["refreshing"]
        if start:
            _state["refreshing"] = True
        have = _state["data"] is not None
    if start:
        if have:
            threading.Thread(target=_fetch, daemon=True).start()
        else:
            _fetch()
    with _lock:
        return {**(_state["data"] or {"mine": []}), "fetched_at": _state["fetched_at"],
                "error": _state["error"], "refreshing": _state["refreshing"]}


def repo_dir(name_with_owner: str) -> Optional[str]:
    """The local clone of a GitHub repo under the work folder, matched by its origin remote."""
    if time.time() - _repo_dirs["at"] > 600:
        found = {}
        root = Path(workdir())
        try:
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError:  # e.g. EINTR listing ~/Documents under launchd: keep the last map, retry next call
            return _repo_dirs["map"].get(name_with_owner)
        for child in children:
            if not (child / ".git").exists():
                continue
            try:
                url = subprocess.run(["git", "-C", str(child), "remote", "get-url", "origin"],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            slug = url.rstrip("/").removesuffix(".git").split("github.com")[-1].lstrip(":/")
            if slug and slug not in found:  # first match wins (the main clone sorts before -wt copies)
                found[slug] = str(child)
        _repo_dirs.update(map=found, at=time.time())
    return _repo_dirs["map"].get(name_with_owner)


def merge(pr: dict) -> str:
    """Squash-merge one of your pull requests. Only allowed when it is approved and CI passed."""
    if pr.get("review") != "APPROVED" or pr.get("ci") != "SUCCESS" or pr.get("draft"):
        raise ValueError("only approved pull requests with passing CI can be merged from here")
    proc = subprocess.run([_gh(), "pr", "merge", str(pr["number"]), "-R", pr["repo"], "--squash", "--delete-branch"],
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise ValueError((proc.stderr or proc.stdout).strip()[:300] or "merge failed")
    prs(force=True)
    return (proc.stdout or "merged").strip()[:300]


def review_prompt(pr: dict) -> str:
    return (
        f"Review pull request {pr['url']} ({pr['repo']} #{pr['number']}: {pr['title']}).\n"
        f"Use `gh pr view {pr['number']} -R {pr['repo']}` and `gh pr diff {pr['number']} -R {pr['repo']}`; "
        "read surrounding code in the local clone when the diff alone is not enough.\n"
        "Report: what it changes (two sentences), blocking problems (bugs, security, data loss, missing tests) "
        "with file:line, smaller suggestions, and a verdict: approve, approve with nits, or request changes. "
        "Do not post anything on GitHub; I will decide."
    )


def fix_ci_prompt(pr: dict) -> str:
    return (
        f"CI is failing on my pull request {pr['url']} ({pr['repo']} #{pr['number']}, branch `{pr['branch']}`).\n"
        f"1. Find the failing checks: `gh pr checks {pr['number']} -R {pr['repo']}` and the failed job logs "
        "(`gh run view <id> --log-failed`).\n"
        f"2. Work in a separate git worktree of this repo on branch `{pr['branch']}` so nothing else is disturbed.\n"
        "3. Find the root cause and fix it; run the relevant tests locally.\n"
        "4. Show me the diff and ask before you push."
    )


def new_worktree(cwd: str, task: str) -> tuple[str, str]:
    """A fresh branch in its own git worktree, off the latest default branch, outside every
    checkout (~/.neo/worktrees/<repo>/<name>), so parallel tasks never collide.
    Returns (path, branch). Raises ValueError if cwd is not inside a git repository."""
    import re
    from datetime import datetime

    git = lambda *args, **kw: subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=kw.get("timeout", 30))  # noqa: E731
    top = git("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ValueError(f"{cwd} is not inside a git repository, so it can't get its own branch")
    root = top.stdout.strip()
    git("fetch", "-q", "origin", timeout=60)
    base = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip() or "HEAD"
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:32].strip("-") or "task"
    stamp = datetime.now().strftime("%m%d-%H%M")
    branch = f"neo/{slug}-{stamp}"
    path = Path.home() / ".neo" / "worktrees" / Path(root).name / f"{slug}-{stamp}"
    path.parent.mkdir(parents=True, exist_ok=True)
    made = subprocess.run(["git", "-C", root, "worktree", "add", "-b", branch, str(path), base],
                          capture_output=True, text=True, timeout=120)
    if made.returncode != 0:
        raise ValueError(f"could not create a worktree: {(made.stderr or made.stdout).strip()[:300]}")
    return str(path), branch
