"""Watches: standing jobs that look at something on a schedule (Sentry, production logs, CI) and
report what they found as findings you can act on.

A watch is a headless Claude run that can only read: its tools are limited to READ_ONLY_TOOLS by
Claude's own permission system (--allowedTools; anything else is refused), and it answers in a
fixed JSON shape (--json-schema), so findings are data rather than prose to scrape. Each check is
an ordinary run (kind "watch") and shows in History with every step it took.

Files in COS_HOME: watches.json (what to watch), findings.json (what was found, by id).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from . import system_watches
from .config import workdir
from .runner import Run, cos_home, list_runs, now_iso

EVERY = (30, 60, 120, 240, 1440)  # minutes
HOURS = ("work", "always")  # work: Monday to Friday, 08:00 to 20:00 local time
SEVERITIES = ("high", "medium", "low")
OPEN = ("new", "working")
MAX_INSTRUCTIONS = 4000

# What a watch may use. Claude refuses every other tool (and every other shell command).
READ_ONLY_TOOLS = [
    "Read", "Grep", "Glob", "ToolSearch",
    "mcp__claude_ai_Sentry",
    "Bash(gcloud logging read:*)", "Bash(gcloud logging logs list:*)", "Bash(gcloud projects list:*)",
    "Bash(gcloud config list:*)", "Bash(gcloud container clusters list:*)",
    "Bash(kubectl get:*)", "Bash(kubectl describe:*)", "Bash(kubectl logs:*)", "Bash(kubectl top:*)",
    "Bash(kubectl config get-contexts:*)",
    "Bash(gh run list:*)", "Bash(gh run view:*)", "Bash(gh pr list:*)", "Bash(gh pr view:*)",
    "Bash(gh pr checks:*)", "Bash(gh issue list:*)", "Bash(gh issue view:*)", "Bash(gh repo list:*)",
    "Bash(gh search issues:*)", "Bash(gh search prs:*)", "Bash(gh project item-list:*)",
    "Bash(gh project field-list:*)", "Bash(gh project view:*)",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(git blame:*)",
    "Bash(jq:*)", "Bash(sort:*)", "Bash(uniq:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(grep:*)", "Bash(cut:*)", "Bash(date:*)", "Bash(ls:*)", "Bash(cat:*)",
    "Bash(stat:*)", "Bash(tr:*)", "Bash(column:*)", "Bash(file:*)", "Bash(du:*)",
    # No sed or awk rules: they can write files (sed -i, awk system()). The readonly-allow hook
    # approves the read-only forms of those.
]
# Tools of an allowed connector that change things.
DENIED_TOOLS = ["mcp__claude_ai_Sentry__update_issue"]

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two or three sentences: what you checked and the overall state."},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Stable id for this problem, e.g. a Sentry short id or service:error-signature"},
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "what": {"type": "string", "description": "What is happening and its impact, with numbers"},
                    "evidence": {"type": "string", "description": "Counts, first and last seen, a short sample of the error or stack trace"},
                    "link": {"type": "string", "description": "URL to the issue, log query or run; empty if none"},
                    "repo": {"type": "string", "description": "GitHub repo it most likely lives in (owner/name), or empty"},
                    "next_step": {"type": "string", "description": "Instructions a coding agent can follow to find the root cause and fix it"},
                },
                "required": ["key", "title", "severity", "what", "evidence", "link", "repo", "next_step"],
            },
        },
    },
    "required": ["summary", "findings"],
}


def templates() -> list[dict]:
    """The built-in watches, read from the environment at call time. Production errors need a GCP project
    (NEO_GCP_PROD_PROJECT) and stay off without one; CI checks NEO_GITHUB_ORG's repositories, else the ones
    under the working directory."""
    project = os.environ.get("NEO_GCP_PROD_PROJECT", "").strip()
    org = os.environ.get("NEO_GITHUB_ORG", "").strip()
    repos = (f"the {org} repositories" if org
             else "the GitHub repositories under the working directory")
    return [
        {
            "id": "sentry", "name": "Sentry", "every": 120, "hours": "work", "enabled": True,
            "instructions": (
                "New and regressed Sentry issues in production since the last check (use the Sentry connector). "
                "Ignore issues that are resolved, ignored, or only seen once with no users affected. "
                "For each issue include the Sentry link, event and user counts, first and last seen, the culprit, "
                "and the top in-app stack frame."
            ),
        },
        {
            "id": "prod-logs", "name": "Production errors", "every": 120, "hours": "work", "enabled": bool(project),
            "instructions": (
                "Error logs in production since the last check. Most lines at severity ERROR are really INFO written to "
                "stderr, and stack traces arrive one line per entry, so count error signatures with this pipeline "
                "(set --freshness to the window since the last check):\n"
                "gcloud logging read 'severity>=ERROR AND resource.type=\"k8s_container\" AND NOT "
                "resource.labels.namespace_name=(\"monitoring\" OR \"kube-system\" OR \"gke-managed-system\" OR \"gmp-system\") "
                "AND NOT textPayload=~\"^(INFO|DEBUG|WARNING)[: ]\" AND NOT textPayload=~\"^[[:space:]^]\" AND "
                "(textPayload=~\"(Error|Exception|error|failed|Failed|FATAL|panic)\" OR jsonPayload.message=~\"(Error|Exception|error|failed)\")' "
                f"--project {project or '<your-gcp-project>'} --freshness=2h --limit 20000 "
                "--format='value(resource.labels.container_name,textPayload,jsonPayload.message)' "
                "| sed -E 's/[0-9a-f]{8}-[0-9a-f-]{27,}/<id>/g; s/[0-9]{2,}/N/g' | cut -c1-150 | sort | uniq -c | sort -rn | head -80\n"
                "Then read a few full entries for the signatures that matter. Report errors that are new, spiking, or "
                "clearly hurting users; skip steady known noise. Include counts, a sample message, and a Logs Explorer link."
            ),
        },
        {
            "id": "ci-main", "name": "CI on main", "every": 240, "hours": "work", "enabled": False,
            "instructions": (
                f"Failing GitHub Actions runs on the default branch of {repos} since the last check "
                "(`gh run list -R <repo> --branch main --status failure`). For each failure include the workflow, the "
                "failing job and step, the error from `gh run view <id> --log-failed`, and the run link."
            ),
        },
    ]


_lock = threading.RLock()
# Called with (watch, run_meta, new_findings) after every check; the Slack bot sets it to post them.
on_checked: Optional[Callable[[dict, dict, list[dict]], None]] = None


def _file(name: str) -> Path:
    return cos_home() / name


def _read(name: str, default):
    try:
        return json.loads(_file(name).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, data) -> None:
    tmp = _file(name).with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(_file(name))


# ------------------------------------------------------------------ watches


def load() -> list[dict]:
    """Every watch. The first time, the built-in ones (Sentry and production errors on, CI off)."""
    with _lock:
        data = _read("watches.json", None)
        if data is None:
            data = templates()
            _write("watches.json", data)
        return data


def get(watch_id: str) -> Optional[dict]:
    return next((w for w in load() if w["id"] == watch_id), None)


def _update(watch_id: str, **fields) -> None:
    with _lock:
        rows = load()
        for w in rows:
            if w["id"] == watch_id:
                w.update(fields)
        _write("watches.json", rows)


def save(data: dict) -> dict:
    """Create a watch (no id) or change one. Raises ValueError on bad input."""
    name = " ".join(str(data.get("name", "")).split())[:60]
    instructions = str(data.get("instructions", "")).strip()
    try:
        every = int(data.get("every", 120))
    except (TypeError, ValueError):
        every = 0
    hours = str(data.get("hours", "work"))
    at = str(data.get("at") or "").strip()
    if not name:
        raise ValueError("give the watch a name")
    if not instructions or len(instructions) > MAX_INSTRUCTIONS:
        raise ValueError(f"say what to watch (up to {MAX_INSTRUCTIONS} characters)")
    if every not in EVERY:
        raise ValueError(f"every must be one of {EVERY} minutes")
    if hours not in HOURS:
        raise ValueError(f"hours must be one of {HOURS}")
    if at and not _at_time(at):
        raise ValueError("at must be a local time as HH:MM, e.g. 10:00")
    fields = {"name": name, "instructions": instructions, "every": every, "hours": hours,
              "enabled": bool(data.get("enabled", True))}
    if "at" in data:
        fields["at"] = at
    with _lock:
        rows = load()
        watch_id = data.get("id")
        if watch_id:
            watch = next((w for w in rows if w["id"] == watch_id), None)
            if not watch:
                raise ValueError("no such watch")
            watch.update(fields)
        else:
            base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "watch"
            watch_id, n = base, 2
            while any(w["id"] == watch_id for w in rows):
                watch_id, n = f"{base}-{n}", n + 1
            watch = {"id": watch_id, **fields, "created_at": now_iso()}
            rows.append(watch)
        _write("watches.json", rows)
        return watch


def delete(watch_id: str) -> None:
    with _lock:
        _write("watches.json", [w for w in load() if w["id"] != watch_id])


def in_hours(watch: dict, now: datetime) -> bool:
    if watch.get("hours") == "always":
        return True
    return now.weekday() < 5 and 8 <= now.hour < 20


def _at_time(at: str) -> Optional[tuple[int, int]]:
    """"10:00" -> (10, 0); None when it is not a valid HH:MM."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", at or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return None
    return int(m[1]), int(m[2])


def due(now: Optional[datetime] = None) -> list[dict]:
    """Enabled watches whose time has come and that are not already checking.

    A watch with "at" (local HH:MM) runs once a day at that time, on the days its hours allow,
    instead of every N minutes; one missed while the machine was off runs when it is back."""
    now = now or datetime.now()
    out = []
    for w in load():
        if not w.get("enabled") or not in_hours(w, now) or running(w["id"]):
            continue
        last = w.get("last_started_at")
        at = _at_time(w.get("at") or "")
        if at:
            slot = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
            try:
                ran = bool(last) and datetime.fromisoformat(last) >= slot
            except ValueError:
                ran = False
            if now >= slot and not ran:
                out.append(w)
            continue
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(minutes=int(w.get("every") or 120)):
                    continue
            except ValueError:
                pass
        out.append(w)
    return out


def running(watch_id: str) -> Optional[dict]:
    for meta in list_runs(60):
        if meta.get("kind") == "watch" and meta.get("watch_id") == watch_id and meta["status"] in ("queued", "running"):
            return meta
    return None


def _since(watch: dict) -> str:
    last = watch.get("last_ok_at")
    if not last:
        return "never checked before: look at the last 24 hours"
    try:
        minutes = int((datetime.now() - datetime.fromisoformat(last)).total_seconds() // 60) + 5
    except ValueError:
        return "unknown: look at the last 24 hours"
    return f"{last[:16].replace('T', ' ')} local time, about {minutes} minutes ago (use a window of {minutes}m)"


def prompt(watch: dict) -> str:
    known = [f for f in _findings().values() if f["watch_id"] == watch["id"]]
    known.sort(key=lambda f: f["last_seen"], reverse=True)
    lines = [f"- {f['key']}: {f['title']} ({'dismissed by me' if f['status'] == 'dismissed' else 'already reported'})"
             for f in known[:40]]
    return "\n".join([
        f'You are my "{watch["name"]}" watch. Check the following and report what needs an engineer.',
        "",
        "What to watch:",
        watch["instructions"],
        "",
        f"Now: {datetime.now().strftime('%Y-%m-%d %H:%M')} local time. Last check: {_since(watch)}.",
        "",
        "Problems already known (reuse the exact key when you see the same problem again; report a dismissed "
        "one only if it got clearly worse, and say so):" if lines else "No problems are known yet.",
        *lines,
        "",
        "Rules:",
        "- Read only. Do not change anything anywhere: no resolving, assigning, commenting, deploying or pushing.",
        "- Shell: one command per Bash call, no `cd` (use absolute paths or --project flags), and the Read "
        "tool instead of cat/head/sed on files. A refused command costs a turn you may not get back.",
        "- Report problems that need an engineer, not noise. One finding per distinct problem; group repeats.",
        "- severity: high = users affected now or data at risk; medium = a real bug with limited impact; low = worth knowing.",
        "- key: stable across checks (a Sentry short id, or service:error-signature).",
        "- next_step: concrete instructions a coding agent can follow to find the root cause and fix it "
        "(which repo, where to look, how to reproduce).",
        "- Nothing wrong: return no findings and say so in the summary. A source you could not reach: say so in the summary.",
    ])


def cli_args() -> tuple[list[str], list[str]]:
    """(every turn, first turn only): tools limited to reading; the answer in the findings shape."""
    extra = [t.strip() for t in os.environ.get("NEO_WATCH_TOOLS", "").split(",") if t.strip()]
    every_turn = ["--allowedTools", ",".join(READ_ONLY_TOOLS + extra), "--disallowedTools", ",".join(DENIED_TOOLS)]
    return every_turn, ["--json-schema", json.dumps(FINDINGS_SCHEMA)]


def start(watch_id: str, source: str = "schedule") -> dict:
    """Check now in the background (or return the check already running)."""
    with _lock:
        watch = get(watch_id)
        if not watch:
            raise ValueError("no such watch")
        current = running(watch_id)
        if current:
            return current
        every_turn, first_turn = cli_args()
        run = Run.create("claude", prompt(watch), workdir(), "readonly", source=source,
                         extra={"kind": "watch", "watch_id": watch_id, "title": f"Watch: {watch['name']}",
                                "cli_args": every_turn, "first_turn_args": first_turn})
        _update(watch_id, last_started_at=datetime.now().isoformat(timespec="seconds"), last_run_id=run.id)

    def work():
        try:
            run.execute()
        except Exception as exc:  # noqa: BLE001 - recorded in the run
            run.append_event({"kind": "error", "title": "Watch failed", "body": repr(exc), "data": {}}, 1)
            run.update_meta(status="failed", pid=None)
        checked(run)

    threading.Thread(target=work, daemon=True, name=f"watch-{watch_id}").start()
    return run.meta()


# ------------------------------------------------------------------ findings


def _findings() -> dict:
    data = _read("findings.json", {})
    return data if isinstance(data, dict) else {}


def finding_id(watch_id: str, key: str) -> str:
    return hashlib.sha1(f"{watch_id}\0{key.strip().lower()}".encode()).hexdigest()[:12]


def parse_result(run: Run) -> Optional[dict]:
    """The watch's answer: Claude's structured_output, else the final text as JSON."""
    answer = None
    try:
        with (run.dir / "raw.jsonl").open() as fh:
            for line in fh:
                if '"structured_output"' not in line and '"result"' not in line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(raw, dict) and raw.get("type") == "result":
                    answer = raw.get("structured_output") or answer
                    if answer is None and isinstance(raw.get("result"), str):
                        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw["result"].strip())
                        try:
                            answer = json.loads(text)
                        except json.JSONDecodeError:
                            pass
    except OSError:
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("findings"), list):
        return None
    return answer


def checked(run: Run) -> list[dict]:
    """A check ended (also one a previous bot process started): record it and tell on_checked."""
    meta = run.meta()
    watch = get(meta.get("watch_id") or "") or {"id": meta.get("watch_id"), "name": meta.get("title", "Watch")}
    new = finish(watch, run, meta["created_at"]) if meta["status"] == "done" else []
    if on_checked:
        try:
            on_checked(watch, run.meta(), new)
        except Exception:  # noqa: BLE001 - a failed notification must not lose the findings
            logging.getLogger("neo").exception("watch notification failed")
    return new


def finish(watch: dict, run: Run, started: str) -> list[dict]:
    """Record a finished check: merge its findings into what we know. Returns the new ones."""
    answer = parse_result(run)
    if answer is None:
        run.update_meta(watch_summary="The watch did not return findings in the expected shape; open the run to see why.")
        return []
    new = []
    with _lock:
        store = _findings()
        now = now_iso()
        for item in answer["findings"]:
            if not isinstance(item, dict) or not str(item.get("key", "")).strip():
                continue
            fid = finding_id(watch["id"], str(item["key"]))
            fields = {k: str(item.get(k) or "")[:4000] for k in ("title", "what", "evidence", "link", "repo", "next_step")}
            fields["severity"] = item.get("severity") if item.get("severity") in SEVERITIES else "medium"
            if fid in store:
                store[fid].update(fields, last_seen=now, run_id=run.id, times_seen=store[fid].get("times_seen", 1) + 1)
            else:
                store[fid] = {"id": fid, "watch_id": watch["id"], "watch": watch["name"], "key": str(item["key"])[:200],
                              **fields, "status": "new", "first_seen": now, "last_seen": now, "times_seen": 1,
                              "run_id": run.id, "work": None}
                new.append(store[fid])
        _write("findings.json", store)
    run.update_meta(watch_summary=str(answer.get("summary") or "")[:2000], watch_found=len(answer["findings"]),
                    watch_new=len(new))
    try:  # when this check started, in local time (the next check looks from there)
        started = datetime.fromisoformat(started).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
        started = datetime.now().isoformat(timespec="seconds")
    _update(watch["id"], last_ok_at=started)
    return new


SYSTEM = "system:"  # watch_id prefix of findings from neo's launchd watches (cos/system_watches.py)


def sync_system(rows: list[dict]) -> None:
    """Mirror the system watches' open breaches into findings: a new breach becomes a "new" card, a
    breach that cleared is marked "resolved" (unless an agent is working on it). A dismissed card stays
    dismissed while its breach is open (neowatch then stops posting it) and comes back if it recurs."""
    with _lock:
        store = _findings()
        now = now_iso()
        changed = False
        live: set[str] = set()
        checked: set[str] = set()  # watches whose full breach list we have
        present = {SYSTEM + Path(row["folder"]).name for row in rows}
        for row in rows:
            wid = SYSTEM + Path(row["folder"]).name
            if row.get("complete", True):
                checked.add(wid)
            for b in row.get("breaches") or []:
                key = str(b.get("key", ""))
                if not key.strip() or len(key) > 200:  # neowatch could not match a cut key to mute it
                    continue
                fid = finding_id(wid, key)
                live.add(fid)
                what = str(b.get("detail") or "")[:4000]
                f = store.get(fid)
                if f is None or f.get("status") == "resolved":
                    store[fid] = {"id": fid, "watch_id": wid, "watch": row["name"], "key": key,
                                  "title": f"{row['name']}: {key}"[:200], "what": what, "evidence": "", "link": "",
                                  "repo": "", "next_step": "", "severity": "medium", "status": "new",
                                  "first_seen": b.get("since") or now, "last_seen": now,
                                  "times_seen": (f or {}).get("times_seen", 0) + 1, "run_id": None, "work": None}
                    changed = True
                elif f.get("what") != what:
                    f.update(what=what, last_seen=now)
                    changed = True
        for fid, f in store.items():
            wid = str(f.get("watch_id", ""))
            gone = (rows and wid not in present  # its job was removed, not just unreadable this time
                    and not (system_watches.neo_dir() / wid[len(SYSTEM):]).is_dir())
            if (wid.startswith(SYSTEM) and (wid in checked or gone) and fid not in live
                    and f.get("status") in ("new", "dismissed")):
                f.update(status="resolved", status_at=now)
                changed = True
        if changed:
            _write("findings.json", store)


def open_findings() -> list[dict]:
    system_watches.listing()  # cached; brings system watch breaches into the store
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    rows = [f for f in _findings().values() if f["status"] in OPEN]
    rows.sort(key=lambda f: f["last_seen"], reverse=True)
    rows.sort(key=lambda f: (f["status"] != "new", rank.get(f["severity"], 1)))
    return [view(f) for f in rows]


def view(f: dict) -> dict:
    """A finding as the dashboard shows it: with the task an agent would get, and where."""
    cwd = finding_cwd(f)
    return {**f, "task": task_prompt(f), "cwd": cwd, "in_repo": cwd != workdir()}


def get_finding(fid: str) -> Optional[dict]:
    return _findings().get(fid)


def set_status(fid: str, status: str, work: Optional[dict] = None) -> dict:
    if status not in ("new", "working", "dismissed"):
        raise ValueError("status must be new, working or dismissed")
    with _lock:
        store = _findings()
        if fid not in store:
            raise ValueError("no such finding")
        store[fid]["status"] = status
        if work is not None:
            store[fid]["work"] = work
        store[fid]["status_at"] = now_iso()
        _write("findings.json", store)
        return store[fid]


def finding_cwd(f: dict) -> str:
    """The local clone of the finding's repo, else the work folder."""
    from .work import repo_dir

    repo = (f.get("repo") or "").strip()
    if "/" in repo:
        found = repo_dir(repo)
        if found:
            return found
    if repo:
        candidate = Path(workdir()) / repo.split("/")[-1]
        if (candidate / ".git").exists():
            return str(candidate)
    return workdir()


def task_prompt(f: dict) -> str:
    """What an agent is asked when you start working on a finding (you can edit it first)."""
    return "\n".join(filter(None, [
        f.get("next_step") or f"Investigate and fix: {f['title']}",
        "",
        f"Context: found by my \"{f['watch']}\" watch" + (f" ({f['link']})" if f.get("link") else "") + ".",
        f"{f['title']}. {f.get('what', '')}",
        f"Evidence: {f['evidence']}" if f.get("evidence") else "",
        "",
        "Find the root cause first and tell me what it is. Then fix it with a test, and show me the diff "
        "before you commit or push anything.",
    ]))


def listing() -> dict:
    """Everything the Watches screen shows."""
    recent = {m["id"]: m for m in list_runs(300) if m.get("kind") == "watch"}
    store = _findings()
    rows = []
    for w in load():
        last = recent.get(w.get("last_run_id") or "")
        rows.append({
            **w,
            "running": bool(running(w["id"])),
            "open": sum(1 for f in store.values() if f["watch_id"] == w["id"] and f["status"] in OPEN),
            "last_run": {k: last.get(k) for k in ("id", "status", "created_at", "watch_summary", "watch_found", "watch_new", "cost_usd")} if last else None,
        })
    return {"watches": rows, "findings": open_findings(), "templates": templates(), "system": system_watches.listing()}
