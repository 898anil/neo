"""Hand a code change to zeroshot (https://zeroshot.sh): a worker makes it, then reviewers that did not
write it judge it, and a rejection goes back to the worker until both accept or the attempts run out.

The point is the gate. In a plain run the agent that wrote the fix also decides it is reviewed; here
the graph decides, and the code reviewer runs on a different model (Codex) from the worker (Claude).

neo keeps everything around it: the thread, the commit and PR, PMR, deploy and monitoring. A run edits
a fresh worktree on its own branch and delivers nothing, so repo rules for PR bodies (backie's
placement evidence) stay with neo. Repo-specific review rules live in private/zeroshot/<repo>.md (or
prompts/zeroshot/<repo>.md) and are appended to the task, which every reviewer reads.

State: ~/.neo/zeroshot/state is zeroshot's own store (kept out of every workspace, as it requires);
~/.neo/zeroshot/jobs/<slug>/ holds neo's record of the run, its input and its runtime plan.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import pause

ROOT = Path(__file__).resolve().parent.parent
# zeroshot runs `claude --print` with no permission flags, so the user's allowlist applies and every
# shell command outside it is denied; reviewers then reject for lack of evidence until attempts run
# out (seen 2026-10-20). Each worktree gets local settings that allow the shell. The denies keep the
# usual forms of push, merge, PR and cloud commands with neo, and keep the agents from loosening this
# file. Prefix rules are not a sandbox (`python -c` can still shell out); the agents run with the same
# trust as neo's own runs. zeroshot strips the environment, so git/kube env overrides don't reach them.
_SHELLS = ("bash", "sh", "zsh", "env", "eval", "exec", "xargs", "command", "nohup")
CLAUDE_SETTINGS = {"permissions": {
    "allow": ["Bash", "Edit", "Write"],
    "deny": ["Bash(git push:*)", "Bash(git -C:*)", "Bash(git -c:*)", "Bash(gh:*)", "Bash(kubectl:*)",
             "Bash(gcloud:*)", "Bash(terraform:*)", "Bash(doppler:*)", "Bash(helm:*)",
             *(f"Bash({shell}:*)" for shell in _SHELLS),
             "Edit(.claude/**)", "Write(.claude/**)"],
}}
SETTINGS_PATH = ".claude/settings.local.json"
SHELL_NOTE = ("Shell note for Claude agents: run git from the worktree root and scripts directly (./scripts/x.sh, "
              "not `bash x.sh`). These command prefixes are blocked: "
              + ", ".join(rule[5:-3] for rule in CLAUDE_SETTINGS["permissions"]["deny"] if rule.startswith("Bash("))
              + ". Do not commit or push.")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")

# Worker and acceptance on Claude; the code reviewer on Codex, so the judge is not the writer's model.
DEFAULT_RUNTIME = {
    "harness": "claude",
    "provider": "anthropic",
    "size": "medium",
    "nodes": {
        "worker": {"kind": "agent", "model": "claude-opus-5-5", "effort": "high"},
        "acceptance": {"kind": "agent", "model": "claude-opus-5-5", "effort": "high"},
        "code": {"kind": "agent", "model": "gpt-5.6-terra", "effort": "high",
                 "lane": {"harness": "codex", "provider": "openai"}},
    },
}


def home() -> Path:
    return pause.jobs_dir() / "zeroshot"


def state_dir() -> Path:
    return home() / "state"


def binary() -> str:
    found = os.environ.get("NEO_ZEROSHOT_BIN") or shutil.which("zeroshot")
    local = Path.home() / ".local" / "bin" / "zeroshot"
    if not found and local.exists():
        found = str(local)
    if not found:
        raise ValueError("zeroshot is not installed (npm install -g --prefix ~/.local @the-open-engine-company/zeroshot)")
    return found


def _env() -> dict:
    """zeroshot looks up claude and codex on PATH; launchd's PATH lacks ~/.local/bin."""
    env = dict(os.environ, ZEROSHOT_STATE_DIR=str(state_dir()))
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    extra = [p for p in (str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin") if p not in path]
    env["PATH"] = os.pathsep.join(extra + path)
    return env


def _zeroshot(*args: str, timeout: float = 120) -> dict:
    proc = subprocess.run([binary(), *args], capture_output=True, text=True, env=_env(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"zeroshot {args[0]} failed: {(proc.stderr or proc.stdout).strip()[:500]}")
    return json.loads(proc.stdout)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout if proc.returncode == 0 else ""


def slugify(title: str) -> str:
    # 39 + "-MMDDHHMM" = 48, the longest slug job_dir accepts.
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:39].rstrip("-")
    return f"{slug or 'change'}-{time.strftime('%m%d%H%M')}"


def _check_base(base: str) -> None:
    ok = subprocess.run(["git", "check-ref-format", "--branch", base], capture_output=True).returncode == 0
    if base.startswith("-") or not ok:
        raise ValueError(f"bad base branch: {base!r}")


def standards(repo: Path) -> str:
    """Review rules for this repo (by folder name, e.g. anyteam-backend-v2), empty when there are none."""
    for base in (ROOT / "private" / "zeroshot", ROOT / "prompts" / "zeroshot"):
        path = base / f"{repo.name}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""


def runtime_plan() -> dict:
    """DEFAULT_RUNTIME, or the JSON file named by NEO_ZEROSHOT_RUNTIME to try other models."""
    override = os.environ.get("NEO_ZEROSHOT_RUNTIME")
    return json.loads(Path(override).read_text(encoding="utf-8")) if override else DEFAULT_RUNTIME


def job_dir(slug: str) -> Path:
    if not _SLUG.fullmatch(slug):
        raise ValueError(f"bad slug: {slug!r}")
    return home() / "jobs" / slug


def _save(job: dict) -> None:
    (job_dir(job["slug"]) / "job.json").write_text(json.dumps(job, indent=2))


def load(ref: str) -> dict:
    """A job by slug or zeroshot run id."""
    if _SLUG.fullmatch(ref) and (job_dir(ref) / "job.json").exists():
        return json.loads((job_dir(ref) / "job.json").read_text())
    for path in (home() / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text())
        if job.get("run_id") == ref:
            return job
    raise ValueError(f"no zeroshot job {ref!r}")


def start(repo: str, task: str, title: str, base: str = "main", slug: Optional[str] = None) -> dict:
    """Make a worktree of `repo` at origin/<base> on branch neo/zs-<slug> and start a detached run in it.

    The job is saved before the run is submitted, so if submission fails `start` with the same slug
    retries it (same submission key, so zeroshot never makes a second run) instead of refusing. A retry
    submits the original task, title and base; a different --base is refused."""
    if not task.strip() or not title.strip():
        raise ValueError("task and title are required")
    _check_base(base)
    repo_dir = Path(repo).expanduser().resolve()
    if not (repo_dir / ".git").exists():
        raise ValueError(f"{repo_dir} is not a git checkout")
    slug = slug or slugify(title)
    jobs = job_dir(slug)
    if (jobs / "job.json").exists():
        job = load(slug)
        if job.get("run_id"):
            raise ValueError(f"job {slug} already exists; pick another --slug or check `status {slug}`")
        if (job["repo"] != str(repo_dir) or job["base"] != base or not Path(job["worktree"]).exists()
                or not (jobs / "input.json").exists()):
            raise ValueError(f"job {slug} was set up for another repo or is incomplete; `clean {slug} --force` first")
        return _submit(job)
    worktree = repo_dir.parent / f"{repo_dir.name}-wt-zs-{slug}"
    if worktree.exists():
        raise ValueError(f"{worktree} already exists; pick another --slug")
    if _git(repo_dir, "ls-files", "--", SETTINGS_PATH).strip():
        raise ValueError(f"{repo_dir.name} tracks {SETTINGS_PATH}; zeroshot runs need their own copy")

    _git(repo_dir, "fetch", "--quiet", "origin", "--", base)
    # --no-track: the branch must not follow origin/<base>, so a bare `git push` has nowhere to go.
    branch = f"neo/zs-{slug}"
    _git(repo_dir, "worktree", "add", "--quiet", "--no-track", "-b", branch, str(worktree), f"origin/{base}")
    try:
        _allow_claude_tools(worktree)
        rules = standards(repo_dir)
        full_task = task.strip() + f"\n\n{SHELL_NOTE}" + (
            f"\n\nReview standard for this repository (reviewers must enforce it):\n{rules}" if rules else "")
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / "input.json").write_text(json.dumps({"task": full_task}, indent=2))
        (jobs / "runtime.json").write_text(json.dumps(runtime_plan(), indent=2))
        job = {"slug": slug, "run_id": None, "title": title, "repo": str(repo_dir),
               "worktree": str(worktree), "branch": branch, "base": base,
               "base_revision": _git(worktree, "rev-parse", "HEAD").strip(),
               "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _save(job)
    except BaseException:
        # Nothing was submitted and nothing is recorded: undo the worktree so the slug can be reused.
        _git(repo_dir, "worktree", "remove", "--force", str(worktree), check=False)
        _git(repo_dir, "branch", "-D", branch, check=False)
        shutil.rmtree(jobs, ignore_errors=True)
        raise
    return _submit(job)


def _submit(job: dict) -> dict:
    jobs = job_dir(job["slug"])
    state_dir().mkdir(parents=True, exist_ok=True)
    receipt = _zeroshot("run", f"--title={job['title']}", "--template", "software-change",
                        "--input", str(jobs / "input.json"), "--runtime-config", str(jobs / "runtime.json"),
                        "--cwd", job["worktree"], "--submission-key", f"neo-{job['slug']}", "--detach",
                        timeout=300)
    if not receipt.get("runId"):
        raise RuntimeError(f"zeroshot run returned no runId: {str(receipt)[:300]}")
    job["run_id"] = receipt["runId"]
    _save(job)
    return job


def _allow_claude_tools(worktree: Path) -> None:
    settings = worktree / SETTINGS_PATH
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps(CLAUDE_SETTINGS, indent=2))
    # A linked worktree shares the main checkout's info/exclude, so this hides the file there too.
    exclude = Path(_git(worktree, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else worktree / exclude
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if SETTINGS_PATH not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(lines + [SETTINGS_PATH]) + "\n")


def _verdicts(run_id: str) -> list[dict]:
    """Each reviewer verdict so far, oldest first, from the run's event store."""
    db = state_dir() / "runs" / run_id / "runs.sqlite3"
    if not db.exists():
        return []
    with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute("select event_json from v2_run_events where event_json like '%node_completed%' "
                            "order by sequence").fetchall()
    out = []
    for (raw,) in rows:
        done = json.loads(raw).get("completion") or {}
        outcome = done.get("outcome") or {}
        verdict = (outcome.get("signals") or {}).get("verdict")
        if verdict:
            out.append({"node": (done.get("reference") or {}).get("node"), "verdict": verdict,
                        "feedback": ((outcome.get("diagnostic") or {}).get("message") or "")[:1500]})
    return out


def status(ref: str, timeout: float = 120) -> dict:
    """The job, zeroshot's phase and result, the nodes running now, every reviewer verdict, and what
    changed: `changed_files` (uncommitted) and `commits` (made on the branch since it was cut).
    `result` is "succeeded" only when every reviewer accepted."""
    job = load(ref)
    if not job.get("run_id"):
        raise RuntimeError(f"job {job['slug']} was never submitted; run `start` again with --slug {job['slug']}")
    st = _zeroshot("status", job["run_id"], timeout=timeout).get("status") or {}
    terminal = st.get("terminalResult") or {}
    verdicts = _verdicts(job["run_id"])
    worktree = Path(job["worktree"])
    base = job.get("base_revision") or f"origin/{job['base']}"
    return {**job, "phase": st.get("phase", "unknown"), "finished": st.get("phase") == "finished",
            "result": terminal.get("status"), "reason": terminal.get("reason"),
            "active": [e.get("node") for e in st.get("activeExecutions") or []],
            "rounds": sum(1 for v in verdicts if v["node"] == "code"), "verdicts": verdicts,
            "tokens": (st.get("metadata") or {}).get("tokenUsage"),
            "changed_files": _git(worktree, "status", "--short").splitlines(),
            "commits": _git(worktree, "log", "--oneline", f"{base}..HEAD", check=False).splitlines()}


def wait(ref: str, timeout: int = 540, interval: int = 20) -> dict:
    """Poll until the run finishes or `timeout` seconds pass (stays under a 10-minute shell call)."""
    deadline = time.monotonic() + timeout
    while True:
        current = status(ref, timeout=max(10.0, min(120.0, deadline - time.monotonic())))
        left = deadline - time.monotonic()
        if current["finished"] or left <= 0:
            return current
        time.sleep(min(interval, left))


def clean(ref: str, force: bool = False) -> dict:
    """Remove a finished job's worktree, local branch and record once its change is pushed.
    Refuses a running job, uncommitted changes, and commits no remote has, unless `force`."""
    job = load(ref)
    if not job["branch"].startswith("neo/zs-"):
        raise ValueError(f"job {job['slug']} names branch {job['branch']!r}, not a neo/zs- branch")
    worktree, repo = Path(job["worktree"]), Path(job["repo"])
    if job.get("run_id"):
        try:
            phase = (_zeroshot("status", job["run_id"]).get("status") or {}).get("phase")
        except RuntimeError as exc:
            # Only a run zeroshot has forgotten is safe to skip; any other failure may hide a live run.
            if not force or f"run {job['run_id']} was not found" not in str(exc):
                raise
            phase = "finished"
        if phase != "finished":
            raise ValueError(f"job {job['slug']} is still running")
    if not force:
        if worktree.exists():
            dirty = [line for line in _git(worktree, "status", "--short").splitlines() if SETTINGS_PATH not in line]
            if dirty:
                raise ValueError(f"{worktree} has uncommitted changes; commit and push them or pass --force")
        unpushed = []
        if _git(repo, "branch", "--list", job["branch"]).strip():
            unpushed = _git(repo, "log", "--oneline", f"refs/heads/{job['branch']}", "--not", "--remotes").splitlines()
        if unpushed:
            raise ValueError(f"{job['branch']} has {len(unpushed)} unpushed commit(s); push them or pass --force")
    if worktree.exists():
        _git(repo, "worktree", "remove", "--force", str(worktree))
    _git(repo, "worktree", "prune")
    if _git(repo, "branch", "--list", job["branch"]).strip():
        _git(repo, "branch", "-D", job["branch"])
    shutil.rmtree(job_dir(job["slug"]))
    return {"removed": job["slug"], "worktree": str(worktree), "branch": job["branch"]}
