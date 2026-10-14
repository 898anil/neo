"""Loops: one per issue or PR, followed from detection to a verified fix.

  detected -> rca -> rca_doc -> fix_pr -> merged -> deployed -> verified

Loops open from alerts (auto RCA), serious watch findings and the owner's own open PRs. A read-only
Claude check (kind "loops", every 30 min) looks up each open loop's evidence (gh, the deploy
workflows (NEO_DEPLOY_REPO), Sentry, the alert's Slack thread, Drive) and proposes updates.
apply_update() refuses an update without evidence, a backwards move that is not a regression,
and "verified" unless the fix was deployed and quiet for VERIFY_QUIET_HOURS. Stalls are decided
here by rule, not by the model.

State: COS_HOME/loops.json, by loop id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from .config import owner_fields, workdir
from .runner import Run, list_runs, pid_alive
from .store import JsonStore

STORE = JsonStore("loops.json")
STAGES = ["detected", "rca", "rca_doc", "fix_pr", "merged", "deployed", "verified"]
STAGE_LABELS = {"detected": "Detected", "rca": "RCA done", "rca_doc": "RCA doc", "fix_pr": "Fix PR open",
                "merged": "Merged", "deployed": "In prod", "verified": "Verified fixed"}
VERIFY_QUIET_HOURS = 24
KEEP_VERIFIED_DAYS = 7  # verified loops are still checked for regressions this long
STALL_HOURS = {"detected": (4, "No RCA after {h}h"), "rca": (24, "RCA done, no doc or fix after {h}h"),
               "rca_doc": (24, "RCA doc, no fix PR after {h}h"), "fix_pr": (48, "Fix PR open {h}h, not merged"),
               "merged": (24, "Merged {h}h ago, not deployed")}
on_checked: Optional[Callable[[dict, list[dict]], None]] = None  # (run meta, finish_check output)
_check_lock = threading.Lock()


def _web(url) -> bool:
    """Only web links reach the dashboard; anything else (javascript:, data:) is dropped."""
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def _id(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def open_loop(kind: str, key: str, title: str, links: dict, now: Optional[float] = None,
              stage: str = "detected") -> dict:
    """The loop for key, created if new (an existing loop is returned unchanged)."""
    now = time.time() if now is None else now

    def write(data: dict) -> dict:
        lid = _id(key)
        if lid not in data:
            data[lid] = {"id": lid, "kind": kind, "key": key, "title": " ".join(title.split())[:200], "stage": stage,
                         "status": "open", "regressed": False,
                         "links": {"prs": [u for u in links.get("prs", []) if _web(u)],
                                   **{k: (v if _web(v) else "") for k, v in links.items() if k != "prs"}}, "stage_at": now,
                         "created_at": now, "updated_at": now,
                         "history": [{"at": now, "stage": stage, "note": "opened", "evidence": ""}]}
        return data[lid]
    return STORE.update(write)


def sync_prs(mine: list[dict]) -> list[dict]:
    """A loop for each of the owner's own open PRs (from work.prs()["mine"]; pass it only from a successful
    fetch). A PR still on that list is marked pr_open and not checked: it can't have merged yet."""
    rows = [open_loop("pr", f"pr:{p['repo']}#{p['number']}", p["title"], {"prs": [p["url"]]}, stage="fix_pr")
            for p in mine if not p.get("draft")]
    still_open = {r["id"] for r in rows}

    def mark(data: dict) -> None:
        for loop in data.values():
            if loop["kind"] == "pr":
                loop["pr_open"] = loop["id"] in still_open
    STORE.update(mark)
    return rows


def apply_update(loop_id: str, update: dict, now: Optional[float] = None) -> tuple[bool, str]:
    """Apply one proposed update; (applied, reason)."""
    now = time.time() if now is None else now
    stage, evidence = update.get("stage"), str(update.get("evidence") or "").strip()
    regressed = bool(update.get("regressed"))
    if stage not in STAGES:
        return False, f"unknown stage {stage!r}"
    if not evidence:
        return False, "no evidence"

    def write(data: dict) -> tuple[bool, str]:
        loop = data.get(loop_id)
        if not loop:
            return False, "no such loop"
        old, new = STAGES.index(loop["stage"]), STAGES.index(stage)
        if new < old and not regressed:
            return False, "backwards without regression"
        if stage == "verified" and loop["stage"] != "verified":
            if loop["stage"] != "deployed":
                return False, "verified needs deployed first"
            if now - loop["stage_at"] < VERIFY_QUIET_HOURS * 3600:
                return False, f"verified needs {VERIFY_QUIET_HOURS}h quiet after deploy"
        for k, v in (update.get("links") or {}).items():
            if k == "prs" and isinstance(v, list):
                loop["links"]["prs"] = list(dict.fromkeys(loop["links"].get("prs", []) + [u for u in v if _web(u)]))
            elif _web(v):
                loop["links"][k] = v
        newly_regressed = regressed and not loop["regressed"]
        if regressed:
            loop.update(regressed=True, status="open")
        elif new > old:
            loop["regressed"] = False  # a later check moved it on without seeing it come back
        if update.get("close"):
            loop["status"] = "closed"
            loop["history"].append({"at": now, "stage": loop["stage"], "note": str(update.get("note") or "closed")[:300],
                                    "evidence": evidence[:500]})
        if new != old or newly_regressed:
            loop.update(stage=stage, stage_at=now)
            loop["history"].append({"at": now, "stage": stage, "note": str(update.get("note") or "")[:300],
                                    "evidence": evidence[:500], "regressed": regressed})
            loop["history"] = loop["history"][-50:]
        loop["updated_at"] = now
        return True, "ok"
    return STORE.update(write)


def _same_title(a: str, b: str) -> bool:
    return " ".join(str(a).split()).lower() == " ".join(str(b).split()).lower()


def note_refire(key: str, title: str, source: str = "", now: Optional[float] = None) -> list[str]:
    """The same alert firing again after its fix reached prod (deployed or verified) is a regression of
    that loop. Decided here by rule, without the model; muted and closed loops are left alone."""
    now = time.time() if now is None else now
    new_id = _id(key)
    hits = [l for l in STORE.read().values()
            if l["id"] != new_id and l.get("kind") == "alert" and l["status"] == "open" and not l.get("regressed")
            and l["stage"] in ("deployed", "verified") and _same_title(l["title"], title)]
    done = []
    for l in hits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        ok, _ = apply_update(l["id"], {
            "stage": "detected", "regressed": True, "note": "Fired again after the fix reached prod",
            "evidence": f"The same alert fired at {when}, {(now - l['stage_at']) / 3600:.1f}h after this loop reached "
                        f"{STAGE_LABELS[l['stage']].lower()}" + (f": {source}" if source else "")}, now)
        if ok:
            done.append(l["id"])
    return done


def mark_checked(loop_ids: list[str], now: Optional[float] = None) -> None:
    """Stamp the loops a follow-up check covered, so the dashboard can say when each was last looked at."""
    now = time.time() if now is None else now

    def write(data: dict) -> None:
        for lid in loop_ids:
            if lid in data:
                data[lid]["checked_at"] = now
    STORE.update(write)


def rca_from_run(meta: dict, now: Optional[float] = None) -> tuple[bool, str]:
    """An alert's loop reaches "rca" when neo's own run in that alert's Slack thread finishes: the
    thread reply is the RCA. Without this, alerts neo already answered showed as "No RCA after 21h"."""
    channel, thread = meta.get("slack_channel"), meta.get("slack_thread")
    if meta.get("status") != "done" or not channel or not thread:
        return False, "not a finished Slack-thread run"
    where = meta.get("slack_url") or f"thread {channel}/{thread}"
    return apply_update(_id(f"slack:{channel}:{thread}"),
                        {"stage": "rca", "note": "neo replied with an RCA in the thread",
                         "evidence": f"neo run {meta.get('id')} finished in the alert thread ({where})"}, now)


def stalled(loop: dict, now: float) -> Optional[str]:
    if loop["status"] != "open" or loop["stage"] not in STALL_HOURS:
        return None
    hours, text = STALL_HOURS[loop["stage"]]
    age = (now - loop["stage_at"]) / 3600
    return text.format(h=int(age)) if age >= hours else None


def set_status(loop_id: str, status: str) -> dict:
    if status not in ("open", "muted", "closed"):
        raise ValueError("bad status")

    def write(data: dict) -> dict:
        if loop_id not in data:
            raise ValueError("no such loop")
        data[loop_id].update(status=status, updated_at=time.time())
        return data[loop_id]
    return STORE.update(write)


def listing(now: Optional[float] = None) -> list[dict]:
    """Every loop except ones closed over a day ago, with `stalled` filled in, newest change first."""
    now = time.time() if now is None else now
    rows = []
    for loop in STORE.read().values():
        if loop["status"] == "closed" and now - loop["updated_at"] > 86400:
            continue
        rows.append({**loop, "stalled": stalled(loop, now)})
    return sorted(rows, key=lambda l: -l["updated_at"])


# ------------------------------------------------------------------ the check
UPDATE_SCHEMA = {
    "type": "object", "required": ["updates"],
    "properties": {"updates": {"type": "array", "items": {
        "type": "object", "required": ["id", "stage", "note", "evidence"],
        "properties": {"id": {"type": "string"}, "stage": {"type": "string", "enum": STAGES},
                       "regressed": {"type": "boolean"},
                       "close": {"type": "boolean", "description": "True when the PR was closed without merging or the issue no longer applies."},
                       "note": {"type": "string", "description": "One short line for the owner: what changed."},
                       "evidence": {"type": "string", "description": "What you looked at and what you saw (command and result, or link)."},
                       "links": {"type": "object", "description": "prs (list of URLs), rca, rca_doc, deploy, sentry (URLs) you found."}}}}},
}
CHECK_PROMPT = """You follow up on open issues and pull requests for {owner}. For each loop below, find out where it
really is now. Report only loops whose stage changed, that regressed, or where you found a new link.
Stages, in order: {stages}.
How to check (read only, change nothing anywhere):
- PRs: `gh pr view <url> --json state,mergedAt,mergeCommit,title,body`. A closed, unmerged PR is not progress.
- Deployed: the merge commit reached production. {deploy} Say "deployed" only with the
  deploy run or image as evidence.
- Verified: after the deploy, the alert has not fired again and the Sentry issue (links.sentry) has no new events
  for {quiet}h. If it fired again or has new events after the deploy, set regressed=true and stage "rca".
- RCA: an RCA reply in the alert's Slack thread (links.source). RCA doc: a Google Doc or Slack canvas linked from
  the thread, or found in Drive by the alert title.
- Fix PR: a PR that references the alert, the Sentry issue or the RCA.
- Closed: a PR closed without merging, or an issue that no longer applies: keep its stage and set close=true.
If you cannot reach a source, leave that loop out rather than guessing.
Loops (JSON):
{loops}"""



def deploy_hint() -> str:
    """How the check finds out what production runs. NEO_DEPLOY_REPO names the repo whose workflows deploy
    to production ("name" for the PR's owner, or "owner/name"); without it the check looks for itself."""
    repo = os.environ.get("NEO_DEPLOY_REPO", "").strip()
    if not repo:
        return ("Look at the production deploy workflows (in the PR's repo, or wherever production is deployed\n"
                "  from) and which commit or image prod runs.")
    full = repo if "/" in repo else f"<owner>/{repo}"
    return (f"Look at the prod deploy workflows in the {repo} repo\n"
            f"  (`gh run list -R {full} ...`) and which commit or image prod runs.")

def _open_for_check(now: float) -> list[dict]:
    rows = []
    for l in STORE.read().values():
        recent_verified = l["stage"] == "verified" and now - l["stage_at"] < KEEP_VERIFIED_DAYS * 86400
        if l["status"] != "open" and not recent_verified:
            continue
        if l.get("pr_open"):
            continue  # still in the owner's open PR list: nothing to follow up yet
        rows.append({k: l[k] for k in ("id", "kind", "title", "stage", "links")})
    return rows


def check_running() -> bool:
    # retry_owner: a run waiting to auto-restart after an API error has no pid but is still live
    return any(m.get("kind") == "loops" and m.get("status") == "running"
               and (pid_alive(m.get("pid")) or pid_alive(m.get("retry_owner"))) for m in list_runs(40))


def start_check() -> Optional[dict]:
    """Start the follow-up check in the background; None when one is running or nothing is open."""
    from . import watches  # read-only tool limits

    with _check_lock:
        if check_running():
            return None
        rows = _open_for_check(time.time())
        if not rows:
            return None
        every_turn, _ = watches.cli_args()
        from .mentions import SLACK_READ  # the alert threads live in Slack: read-only access to them
        at = every_turn.index("--allowedTools") + 1 if "--allowedTools" in every_turn else None
        if at:
            every_turn = every_turn[:at] + [",".join([every_turn[at], *SLACK_READ])] + every_turn[at + 1:]
        prompt = CHECK_PROMPT.format(stages=" -> ".join(STAGES), quiet=VERIFY_QUIET_HOURS, loops=json.dumps(rows, indent=1),
                                     deploy=deploy_hint(), **owner_fields())
        run = Run.create("claude", prompt, workdir(), "readonly", source="schedule",
                         extra={"kind": "loops", "title": f"Follow-up: {len(rows)} open loops", "loop_ids": [r["id"] for r in rows],
                                "cli_args": every_turn, "first_turn_args": ["--json-schema", json.dumps(UPDATE_SCHEMA)]})

    def work():
        try:
            run.execute()
        except Exception as exc:  # noqa: BLE001 - recorded in the run
            run.append_event({"kind": "error", "title": "Follow-up failed", "body": repr(exc), "data": {}}, 1)
            run.update_meta(status="failed", pid=None)
        out = finish_check(run)
        if on_checked:
            try:
                on_checked(run.meta(), out)
            except Exception:  # noqa: BLE001
                logging.getLogger("neo").exception("loops notification failed")

    threading.Thread(target=work, daemon=True, name="loops-check").start()
    return run.meta()


def _answer(run: Run) -> Optional[dict]:
    try:
        lines = (run.dir / "raw.jsonl").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict) and raw.get("type") == "result" and isinstance(raw.get("structured_output"), dict):
            return raw["structured_output"]
    return None


def finish_check(run: Run) -> list[dict]:
    """Apply the check's updates. Returns [{loop_id, applied, reason, update}]."""
    out = []
    answer = _answer(run)
    if answer is not None:  # the check ran to the end: every loop it was given has been looked at
        mark_checked(list(run.meta().get("loop_ids") or []))
    for upd in (answer or {}).get("updates") or []:
        if isinstance(upd, dict) and upd.get("id"):
            ok, why = apply_update(str(upd["id"]), upd)
            out.append({"loop_id": upd["id"], "applied": ok, "reason": why, "update": upd})
    run.update_meta(loops_applied=sum(1 for o in out if o["applied"]), loops_refused=sum(1 for o in out if not o["applied"]))
    return out
