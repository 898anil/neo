"""Dashboard + JSON API. Standard library only.

GET  /                              dashboard
GET  /api/home                      decide / regressed / moved since last look / in flight / quiet
POST /api/ask                       {question} -> read-only run that answers where/how your work is
POST /api/home/seen                 the owner looked at the home screen now
POST /api/home/do                   {key} -> carry out the card's recommendation (its do_it)
GET  /api/digest?key=run:<id>|agent:<id>  the trace page's visual digest (ready / writing / none)
POST /api/digest/refresh            {key} -> rebuild the digest now
POST /api/home/close                {key} -> close a card's ask: a decision, a wait on others, or a stopped-unfinished row (until a newer card)
POST /api/mentions/<id>/<send|neo|dismiss>   {text} for send: post as the owner in that thread
POST /api/mentions/check            look for new mentions of the owner now
POST /api/loops/<id>/<mute|close|open>
GET  /api/overview                  every agent on this machine (all herdr sessions) + runs
GET  /api/agents/<session>/<pane>   one agent: state, screen, timeline from its transcript
POST /api/agents/<session>/<pane>/<prompt|approve|deny|interrupt|keys|close>
POST /api/live                      {engine, cwd, prompt} -> interactive agent in herdr
GET  /api/runs                      list runs (newest first)
POST /api/runs                      {engine, prompt, cwd, mode} -> start a run
GET  /api/runs/<id>                 run metadata
GET  /api/runs/<id>/stream?from=N   Server-Sent Events: timeline events from seq N, then live
POST /api/runs/<id>/message         {prompt} -> follow-up turn in the same session
POST /api/runs/<id>/cancel          stop the running turn

Access (the server binds to 127.0.0.1; expose it with `tailscale serve`):
  - localhost: always allowed
  - Tailscale: requests proxied by `tailscale serve` carry Tailscale-User-Login; users in
    COS_TAILSCALE_USERS (comma-separated logins) are allowed, nobody else
  - COS_TOKEN: `Authorization: Bearer <token>`, ?token= (then remembered in a cookie)
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ask, brief, cards, digest, fleet, home, loops, memory, mentions, pause, settings, watches, work
from .config import agent_brief, default_engine, owner_fields, workdir
from .herdr import HerdrError
from .runner import ENGINES, TERMINAL, Run, list_runs, valid_run_id

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 200_000
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 15

# Runs started by this server, so cancel can reach the live process handle.
ACTIVE: dict[str, Run] = {}
ACTIVE_LOCK = threading.Lock()
# Live runs started from the dashboard; their watcher threads record them.
LIVE_RUNS: list = []

TAILNET = ipaddress.ip_network("100.64.0.0/10")
PUBLIC_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "icon.svg": "image/svg+xml",
    "icon-180.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
}
_whois_cache: dict[str, tuple[float, str | None]] = {}

# The dashboard polls /api/overview every few seconds; full run prompts (Slack threads, ~35 KB
# each) made it 1.4 MB a poll. The board only shows a line of the prompt, so send just that.
SLACK_PREAMBLE = re.compile(r"^Here is the Slack thread[\s\S]*?directed at you:\n")
RUN_HEAVY_KEYS = ("cli_args", "first_turn_args", "loop_ids", "system_prompt", "watch_summary")
PROMPT_CHARS = 300
GZIP_MIN_BYTES = 1024


def board_run(meta: dict) -> dict:
    """A run as the overview lists it: no CLI arguments, prompt cut to its first lines."""
    row = {k: v for k, v in meta.items() if k not in RUN_HEAVY_KEYS}
    prompt = SLACK_PREAMBLE.sub("", row.get("prompt") or "", count=1)
    row["prompt"] = prompt[:PROMPT_CHARS]
    return row


# An agent's last message can run to many KB and changes on every poll while it works. The board
# shows its first 240 characters and, for a question, its last 240, so send only the two ends.
LAST_TEXT_EDGE = 300


def clip_last_text(row):
    text = row.get("last_text") if isinstance(row, dict) else None
    if not isinstance(text, str) or len(text) <= 2 * LAST_TEXT_EDGE + 10:
        return row
    return {**row, "last_text": f"{text[:LAST_TEXT_EDGE]}\n…\n{text[-LAST_TEXT_EDGE:]}"}


def board_overview(data: dict) -> dict:
    return {**data, "runs": [board_run(r) for r in data.get("runs", [])],
            "tasks": [board_run(r) for r in data.get("tasks", [])],
            "agents": [clip_last_text(a) for a in data.get("agents", [])]}


def board_home(data: dict) -> dict:
    return {k: [clip_last_text(x) for x in v] if isinstance(v, list) else v
            for k, v in data.items()}


# The board lists findings by title; the detail sheet fetches /api/findings/<id> for the rest.
FINDING_LIST_KEYS = ("id", "title", "status", "severity", "what", "first_seen", "last_seen", "times_seen",
                     "status_at", "work", "watch", "watch_id", "repo", "link", "run_id")
FINDING_WHAT_CHARS = 400


def board_finding(f: dict) -> dict:
    row = {k: f[k] for k in FINDING_LIST_KEYS if k in f}
    if isinstance(row.get("what"), str):
        row["what"] = row["what"][:FINDING_WHAT_CHARS]
    return row


# The agent timeline shows a step's body when you open it and reads only status / is_error from data;
# a 700 KB transcript every 2.5 s was most of what made the phone slow.
EVENT_BODY_CHARS = 2000


CUT_KINDS = {"tool_call", "tool_result", "thinking"}  # never the agent's answers or your prompts


def board_event(ev: dict) -> dict:
    body = ev.get("body") or ""
    if ev.get("kind") in CUT_KINDS and len(body) > EVENT_BODY_CHARS:
        body = body[:EVENT_BODY_CHARS] + "\n… (cut; open the full trace for the rest)"
    data = {k: v for k, v in (ev.get("data") or {}).items() if k in ("status", "is_error")}
    return {**{k: v for k, v in ev.items() if k not in ("body", "data")}, "body": body, "data": data}


VOLATILE_KEYS = ("now", "generated_at")


def etag_view(value):
    """The payload minus the clock, so a poll where only the time moved still gets a 304; ages count
    in whole minutes, which is what the board shows."""
    if isinstance(value, dict):
        return {k: (int(v // 60) if k == "age" and isinstance(v, (int, float)) else etag_view(v))
                for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [etag_view(v) for v in value]
    return value


def event_sig(ev: dict) -> str:
    """What the phone and the server both compute for the last event it has (see agentUrl in index.html)."""
    return f"{ev.get('kind', '')}|{ev.get('title') or ''}|{len(ev.get('body') or '')}"


def events_since(events: list[dict], have: int, sig: str) -> tuple[list[dict], int]:
    """(events to send, index they start at): only the new ones when the phone's copy still lines up."""
    if 0 < have <= len(events) and event_sig(events[have - 1]) == sig:
        return events[have:], have
    return events, 0


def tailscale_login(ip: str) -> str | None:
    """Who is behind a tailnet address, via `tailscale whois` (cached for a minute)."""
    try:
        if ipaddress.ip_address(ip) not in TAILNET:
            return None
    except ValueError:
        return None
    hit = _whois_cache.get(ip)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    login = None
    cli = os.environ.get("NEO_TAILSCALE_CLI") or "tailscale"
    try:
        out = subprocess.run([cli, "whois", "--json", ip], capture_output=True, text=True, timeout=5).stdout
        login = json.loads(out)["UserProfile"]["LoginName"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        login = None
    _whois_cache[ip] = (time.monotonic(), login)
    return login


def allowed_logins() -> set[str]:
    return {u.strip() for u in os.environ.get("COS_TAILSCALE_USERS", "").split(",") if u.strip()}


def own_hosts() -> set[str]:
    """Host headers that address this dashboard directly (anything else may be DNS rebinding)."""
    port = os.environ.get("NEO_DASHBOARD_PORT", "8788")
    hosts = set()
    for value in (os.environ.get("NEO_DASHBOARD_HOST"), urlparse(os.environ.get("NEO_DASHBOARD_URL", "")).hostname):
        if value:
            hosts |= {value, f"{value}:{port}"}
    return hosts

LOGIN_PAGE = b"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Sign in</title><style>body{font:16px -apple-system,sans-serif;background:#151514;color:#ecece8;display:grid;place-items:center;min-height:90vh;margin:0}
form{display:grid;gap:12px;width:min(340px,90vw)}input,button{font:inherit;padding:12px;border-radius:10px;border:1px solid #444;background:#1d1d1b;color:inherit}
button{background:#7aa2ff;color:#111;border:0}p{color:#9a9a93;font-size:14px}</style></head><body>
<form onsubmit="location.href='/?token='+encodeURIComponent(this.t.value);return false">
<b>Agents dashboard</b><p>Open this from one of your own Tailscale devices, or enter the dashboard token (COS_TOKEN in the bot's .env).</p>
<input name=t type=password placeholder="Token" autocomplete=current-password><button>Sign in</button></form></body></html>"""


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


LOOP_ACTIONS = {"mute": "muted", "close": "closed", "open": "open"}


def fresh_meta(run: Run) -> dict:
    """Mark runs whose engine process vanished (for example, the server restarted) as failed."""
    meta = run.meta()
    if meta.get("runtime", "headless") != "headless":
        return meta  # interactive runs have no child process; their watcher owns status
    if meta["status"] == "running" and not pid_alive(meta.get("pid")) and not pid_alive(meta.get("retry_owner")):
        with ACTIVE_LOCK:
            owned = run.id in ACTIVE
        if not owned:
            meta = run.update_meta(status="failed", pid=None, note="engine process ended without a result")
    return meta


def start_turn(run: Run, prompt: str | None) -> None:
    def work():
        with ACTIVE_LOCK:
            ACTIVE[run.id] = run
        try:
            run.execute(prompt)
        except Exception as exc:  # recorded in the timeline rather than lost in a thread
            turn = run.meta().get("turns", 0)
            run.append_event({"kind": "error", "title": "Runner error", "body": repr(exc), "data": {}}, turn)
            run.update_meta(status="failed", pid=None)
        finally:
            with ACTIVE_LOCK:
                ACTIVE.pop(run.id, None)

    threading.Thread(target=work, daemon=True, name=f"run-{run.id}").start()


DO_IT_PROMPT = ("{Owner} read your summary and accepted the recommendation. Do it now: {do_it}\n"
                "Ask them first only for something irreversible beyond that (a prod deploy, an admin merge, deleting data).")


def do_recommendation(key: str) -> dict:
    """The "Do it" button: hand the card's do_it to the agent or run that wrote it, so it keeps its
    context; a run that can't take another turn (read-only ask, still running, live) gets a new full
    run instead. The text comes from the stored card, never from the request."""
    card = cards.get(key)
    if not card or not (card.get("do_it") or "").strip():
        raise LookupError("nothing to do for this item")
    prompt = DO_IT_PROMPT.format(do_it=card["do_it"].strip(), **owner_fields())
    if key.startswith("agent:"):
        row = next((r for r in fleet.overview().get("agents", []) if fleet._seen_key(r) == key), None)
        if not row:
            raise LookupError("that agent is gone; open its run and ask again")
        fleet.act(row["session"], str(row["pane_id"]), "prompt", prompt)
        fleet.mark_seen([row])
        return {"ok": True, "where": "agent", "href": f"#/agent/{row['session']}/{row['pane_id']}"}
    if key.startswith("run:") and valid_run_id(key[4:]):
        run = Run(key[4:])
        meta = fresh_meta(run)
        fleet.mark_run_seen(run.id)
        if meta.get("mode") == "full" and meta.get("runtime", "headless") == "headless" and meta["status"] in TERMINAL:
            start_turn(run, prompt)
            return {"ok": True, "where": "run", "id": run.id}
        context = f"Context: an earlier neo run ({run.id}, {card.get('title', '')!r}) did: {card.get('did', '')}\n"
        new = Run.create("claude", context + prompt, workdir(), "full", source="dashboard",
                         extra={"kind": "do", "title": f"Do: {card.get('title', key)[:100]}"})
        start_turn(new, None)
        return {"ok": True, "where": "run", "id": new.id}
    raise LookupError("unknown item")


class Handler(BaseHTTPRequestHandler):
    server_version = "cos/0.1"

    def log_message(self, fmt, *args):  # quieter access log
        if os.environ.get("COS_ACCESS_LOG"):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- helpers

    def _tailnet_user(self) -> str | None:
        """Your own tailnet device connecting directly (http://<mini tailscale ip>:<port>)."""
        if (self.headers.get("Host") or "") not in own_hosts():
            return None  # a rebound hostname: refuse even though the device is ours
        login = tailscale_login(self.client_address[0])
        return login if login and login in allowed_logins() else None

    def _tailscale_user(self) -> str | None:
        """The tailnet login `tailscale serve` attached, if it is on the allowlist.

        Only trusted when the request arrives over loopback, which is how `tailscale serve`
        forwards; the server itself never listens beyond 127.0.0.1 in this setup.
        """
        login = self.headers.get("Tailscale-User-Login")
        allowed = {u.strip() for u in os.environ.get("COS_TAILSCALE_USERS", "").split(",") if u.strip()}
        # The header is only meaningful on requests `tailscale serve` forwarded for our own tailnet
        # name. Requiring that Host stops a DNS-rebound page (or a local process talking to
        # 127.0.0.1) from simply sending the header itself.
        expected = urlparse(os.environ.get("NEO_DASHBOARD_URL", "")).netloc
        if not (expected.endswith(".ts.net") and self.headers.get("Host") == expected):
            return None
        if login and login in allowed and self.client_address[0] in ("127.0.0.1", "::1"):
            return login
        return None

    def _token_ok(self) -> bool:
        token = os.environ.get("COS_TOKEN")
        if not token:
            return False
        header = self.headers.get("Authorization", "")
        supplied = header[7:] if header.startswith("Bearer ") else ""
        if not supplied:
            supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if not supplied:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "cos_token":
                    supplied = value
        return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())

    def _local_host(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _authorized(self) -> bool:
        if self._tailscale_user() or self._tailnet_user() or self._token_ok():
            return True
        # Plain localhost, unless a token is configured. The Host check blocks DNS rebinding.
        return not os.environ.get("COS_TOKEN") and self._local_host() and "Tailscale-User-Login" not in self.headers

    def _trusted_host(self) -> bool:
        return True  # folded into _authorized

    def _same_origin_json(self) -> bool:
        """Block cross-site POSTs: a web page cannot send application/json without a CORS
        preflight (which this server never approves), and its Origin would not match."""
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser clients (curl, the Slack bot) send no Origin
        return urlparse(origin).netloc == self.headers.get("Host")

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, separators=(",", ":")).encode()
        if self.command == "GET" and status == HTTPStatus.OK:
            # The dashboard polls every few seconds; when nothing changed it gets an empty 304 and
            # keeps what it has (api() in index.html sends If-None-Match).
            stable = json.dumps(etag_view(payload), separators=(",", ":"), sort_keys=True).encode()
            etag = '"' + hashlib.blake2b(stable, digest_size=12).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
        else:
            etag = None
        self.send_response(status)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Type", "application/json")
        if len(body) >= GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=5)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _route(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        return parts

    def _run_or_404(self, run_id: str) -> Run | None:
        if not valid_run_id(run_id) or not (Run(run_id).dir / "meta.json").exists():
            self._error(HTTPStatus.NOT_FOUND, "run not found")
            return None
        with ACTIVE_LOCK:
            return ACTIVE.get(run_id) or Run(run_id)

    # ------------------------------------------------------------------ verbs

    def do_GET(self):
        parts = self._route()
        if len(parts) == 1 and parts[0] in PUBLIC_FILES:
            # App icon and manifest: harmless, and fetched by the phone without cookies.
            return self._static(parts[0], PUBLIC_FILES[parts[0]])
        if not self._authorized():
            if not parts:  # a person opened the page: show a sign-in form, not raw JSON
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(LOGIN_PAGE)))
                self.end_headers()
                self.wfile.write(LOGIN_PAGE)
                return
            return self._error(HTTPStatus.FORBIDDEN, "not allowed: use localhost, an allowed Tailscale login, or COS_TOKEN")
        if not parts:
            return self._static("index.html", "text/html; charset=utf-8")
        if parts == ["api", "overview"]:
            # Each agent and task carries needs / result (home.annotate), so its row can say Idle, Partly done, Needs you.
            data = home.annotate(fleet.overview(), cards.STORE.read(), fleet.seen.get)
            return self._json({**board_overview(data), "default_engine": default_engine(), "notifications": pause.state(),
                               "findings": [board_finding(f) for f in watches.open_findings()]})
        if parts == ["api", "home"]:
            raw = (parse_qs(urlparse(self.path).query).get("since") or [""])[0]
            try:
                since = float(raw) if raw else None
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "since must be a number")
            return self._json(board_home(home.home(since)))
        if parts == ["api", "watches"]:
            return self._json(watches.listing())
        if parts == ["api", "settings"]:
            return self._json({"default_engine": default_engine(), "notifications": pause.state()})
        if len(parts) == 3 and parts[:2] == ["api", "findings"]:
            found = watches.get_finding(parts[2])
            return self._json(watches.view(found)) if found else self._error(HTTPStatus.NOT_FOUND, "no such finding")
        if parts == ["api", "sessions"]:
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0][:200]
            if q.strip():
                return self._json(memory.search(q))
            return self._json({"results": memory.recent(), "searched": 0, "total": 0})
        if parts == ["api", "session"]:
            raw_path = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            path = Path(raw_path)
            engine = memory.engine_of(path) if raw_path else None
            if not engine:
                return self._error(HTTPStatus.BAD_REQUEST, "not a session transcript")
            info = memory.session_info(path, engine)
            events = fleet.recent_turns(fleet.transcript_events(engine, path), turns=8)
            return self._json({**info, "events": events})
        if parts == ["api", "memory"]:
            from . import autonomy, learn
            return self._json({"dir": str(learn.memory_dir()), "files": learn.all_files(), "review": autonomy.view()})
        if parts == ["api", "usage"]:
            from .runner import usage_summary

            return self._json(usage_summary())
        if parts == ["api", "work"]:
            return self._json(work.prs())
        if parts == ["api", "brief"]:
            return self._json(brief.latest() or {})
        if len(parts) == 4 and parts[:2] == ["api", "agents"]:
            session, pane = unquote(parts[2]), unquote(parts[3])
            if not fleet.valid_target(session, pane):
                return self._error(HTTPStatus.BAD_REQUEST, "bad agent target")
            try:
                detail = fleet.agent_detail(session, pane)
                query = parse_qs(urlparse(self.path).query)
                try:
                    have = int((query.get("n") or ["0"])[0])
                except ValueError:
                    have = 0
                events, start = events_since([board_event(e) for e in detail["events"]], have, (query.get("sig") or [""])[0])
                return self._json({**detail, "events": events, "events_from": start,
                                   "card": cards.get(fleet._seen_key(detail)), "card_key": fleet._seen_key(detail)})
            except HerdrError as exc:
                status = HTTPStatus.NOT_FOUND if exc.code == "agent_not_found" else HTTPStatus.BAD_GATEWAY
                return self._error(status, str(exc))
        if parts == ["api", "digest"]:
            key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            if not digest.valid_key(key):
                return self._error(HTTPStatus.BAD_REQUEST, "bad key")
            return self._json(digest.status(key, fleet.overview()))
        if parts == ["api", "runs"]:
            runs = [fresh_meta(Run(m["id"])) for m in list_runs()]
            return self._json({"runs": runs})
        if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            run = self._run_or_404(parts[2])
            if not run:
                return
            if len(parts) == 3:
                return self._json({**fresh_meta(run), "card": cards.get(f"run:{run.id}")})
            if parts[3] == "stream":
                return self._stream(run)
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):
        if not self._authorized():
            return self._error(HTTPStatus.FORBIDDEN, "not allowed: use localhost, an allowed Tailscale login, or COS_TOKEN")
        if not self._same_origin_json():
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin or non-JSON request refused")
        parts = self._route()
        body = self._read_json()
        if len(parts) == 5 and parts[:2] == ["api", "agents"]:
            session, pane, action = unquote(parts[2]), unquote(parts[3]), parts[4]
            if not fleet.valid_target(session, pane) or action not in fleet.ACTIONS:
                return self._error(HTTPStatus.BAD_REQUEST, "bad agent target or action")
            keys = (body or {}).get("keys") or []
            if not isinstance(keys, list) or not all(isinstance(k, str) and 0 < len(k) < 32 and not k.startswith("-") for k in keys):
                return self._error(HTTPStatus.BAD_REQUEST, "keys must be a list of key names")
            try:
                fleet.act(session, pane, action, str((body or {}).get("text", "")), keys)
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except HerdrError as exc:
                return self._error(HTTPStatus.CONFLICT if exc.code == "agent_blocked" else HTTPStatus.BAD_GATEWAY, str(exc))
            return self._json({"ok": True})
        if parts == ["api", "brief"]:
            return self._json(brief.start("dashboard"), HTTPStatus.ACCEPTED)
        if parts == ["api", "ask"]:
            question = str((body or {}).get("question", "")).strip()
            if not question:
                return self._error(HTTPStatus.BAD_REQUEST, "question required")
            try:
                run = Run.create("claude", question[:2000], workdir(), "readonly", source="dashboard",
                                 extra={"kind": "ask", "title": f"Ask: {question[:100]}",
                                        "system_prompt": ask.system_prompt(ask.current_snapshot()),
                                        "cli_args": watches.cli_args()[0]})
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            start_turn(run, None)
            return self._json(run.meta(), HTTPStatus.CREATED)
        if parts == ["api", "memory", "apply"]:
            from . import autonomy
            which, shown = (body or {}).get("which"), (body or {}).get("review")
            if not (which == "all" or (isinstance(which, list) and which
                                        and all(isinstance(n, int) and not isinstance(n, bool) and 0 < n < 100 for n in which))):
                return self._error(HTTPStatus.BAD_REQUEST, "which must be \"all\" or a list of proposal numbers")
            if not isinstance(shown, str) or not shown:
                return self._error(HTTPStatus.BAD_REQUEST, "the id of the review shown is required; reload the page")
            try:
                return self._json({"results": autonomy.apply_from_dashboard(which, shown), "review": autonomy.view()})
            except ValueError as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
        if parts == ["api", "home", "seen"]:
            home.mark_seen()
            return self._json({"ok": True})
        if parts == ["api", "mentions", "check"]:
            return self._json(mentions.start("dashboard"), HTTPStatus.ACCEPTED)
        if len(parts) == 4 and parts[:2] == ["api", "mentions"] and parts[3] in ("send", "neo", "dismiss"):
            try:
                if parts[3] == "send":
                    return self._json(mentions.send(parts[2], str((body or {}).get("text", ""))), HTTPStatus.ACCEPTED)
                if parts[3] == "neo":
                    run = mentions.hand_to_neo(parts[2])
                    start_turn(run, None)
                    return self._json(run.meta(), HTTPStatus.CREATED)
                return self._json(mentions.dismiss(parts[2]))
            except LookupError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
        if parts == ["api", "digest", "refresh"]:
            key = str((body or {}).get("key", ""))
            if not digest.valid_key(key):
                return self._error(HTTPStatus.BAD_REQUEST, "bad key")
            return self._json(digest.refresh(key, fleet.overview()), HTTPStatus.ACCEPTED)
        if parts == ["api", "home", "close"]:
            try:
                return self._json(home.close_waiting(str((body or {}).get("key", ""))))
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        if parts == ["api", "home", "do"]:
            try:
                return self._json(do_recommendation(str((body or {}).get("key", ""))), HTTPStatus.ACCEPTED)
            except LookupError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (ValueError, HerdrError) as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
        if len(parts) == 4 and parts[:2] == ["api", "loops"] and parts[3] in LOOP_ACTIONS:
            try:
                return self._json(loops.set_status(parts[2], LOOP_ACTIONS[parts[3]]))
            except ValueError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
        if parts == ["api", "settings"]:
            try:
                body = body or {}
                choice = body.get("notifications")
                if "notifications" in body and choice not in (None, "on", pause.FOREVER):
                    if not isinstance(choice, str):
                        raise ValueError("notifications must be \"on\", \"forever\" or a duration like \"2h\"")
                    pause.parse_duration(choice)  # validate before saving anything
                settings.save(**{k: v for k, v in body.items() if k in ("default_engine",)})
                if "notifications" in body:
                    pause.apply(choice)
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json({"default_engine": default_engine(), "notifications": pause.state()})
        if parts == ["api", "watches"]:
            try:
                return self._json(watches.save(body or {}), HTTPStatus.CREATED)
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        if len(parts) == 4 and parts[:2] == ["api", "watches"] and parts[3] in ("run", "delete"):
            if not watches.get(parts[2]):
                return self._error(HTTPStatus.NOT_FOUND, "no such watch")
            if parts[3] == "delete":
                watches.delete(parts[2])
                return self._json({"ok": True})
            return self._json(watches.start(parts[2], "dashboard"), HTTPStatus.ACCEPTED)
        if len(parts) == 4 and parts[:2] == ["api", "system-watches"] and parts[3] in ("run", "on", "off"):
            from . import system_watches
            try:
                if parts[3] == "run":
                    return self._json(system_watches.run(parts[2]), HTTPStatus.ACCEPTED)
                return self._json(system_watches.set_on(parts[2], parts[3] == "on"))
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                return self._error(HTTPStatus.BAD_GATEWAY, str(exc))
        if len(parts) == 4 and parts[:2] == ["api", "findings"] and parts[3] in ("dismiss", "reopen"):
            try:
                return self._json(watches.set_status(parts[2], "dismissed" if parts[3] == "dismiss" else "new"))
            except ValueError as exc:
                return self._error(HTTPStatus.NOT_FOUND, str(exc))
        if parts == ["api", "session", "resume"]:
            body = body or {}
            path = Path(str(body.get("path", "")))
            engine = memory.engine_of(path) if body.get("path") else None
            prompt = str(body.get("prompt", "")).strip()
            if not engine or not prompt:
                return self._error(HTTPStatus.BAD_REQUEST, "a session and a message are required")
            info = memory.session_info(path, engine)
            if not info.get("session_id"):
                return self._error(HTTPStatus.BAD_REQUEST, "this session has no id to resume")
            cwd = info.get("cwd") if info.get("cwd") and os.path.isdir(info["cwd"]) else workdir()
            from .interactive import InteractiveRun

            try:
                irun = InteractiveRun.start(engine, prompt, cwd, label="resume", source="dashboard",
                                            extra={"kind": "resume", "resumed_from": str(path), "title": f"Continued: {info['title']}"},
                                            agent_args=memory.resume_args(engine, info["session_id"]), system=agent_brief(cwd))
            except (ValueError, HerdrError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            LIVE_RUNS.append(irun)
            meta = irun.run.meta()
            return self._json({"session": meta["herdr_session"], "pane_id": meta["herdr_pane"]}, HTTPStatus.CREATED)
        if len(parts) == 3 and parts[:2] == ["api", "work"] and parts[2] in ("review", "fix", "merge"):
            body = body or {}
            listing = work.prs()
            pr = next((x for x in listing["mine"]
                       if x["repo"] == body.get("repo") and x["number"] == body.get("number")), None)
            if not pr:
                return self._error(HTTPStatus.NOT_FOUND, "pull request not in your lists; refresh and try again")
            if parts[2] == "merge":
                if pr not in listing["mine"]:
                    return self._error(HTTPStatus.FORBIDDEN, "you can only merge your own pull requests from here")
                try:
                    return self._json({"ok": True, "message": work.merge(pr)})
                except ValueError as exc:
                    return self._error(HTTPStatus.CONFLICT, str(exc))
            cwd = work.repo_dir(pr["repo"]) or workdir()
            standing = agent_brief(cwd)
            extra = {"kind": f"pr-{parts[2]}", "pr_url": pr["url"], "system_prompt": standing, "conclude": bool(standing),
                     "title": f"Review: {pr['title']}" if parts[2] == "review" else f"Fix CI: {pr['title']}"}
            try:
                if parts[2] == "review":
                    run = Run.create(default_engine(), work.review_prompt(pr), cwd, "full", source="dashboard", extra=extra)
                    start_turn(run, None)
                    return self._json({"run_id": run.id}, HTTPStatus.CREATED)
                from .interactive import InteractiveRun

                irun = InteractiveRun.start(default_engine(), work.fix_ci_prompt(pr), cwd, label=f"fix-{pr['repo_name']}-{pr['number']}",
                                            source="dashboard", extra={"kind": "pr-fix", "pr_url": pr["url"], "title": f"Fix CI: {pr['title']}"}, system=agent_brief(cwd))
                LIVE_RUNS.append(irun)
                meta = irun.run.meta()
                return self._json({"run_id": meta["id"], "session": meta["herdr_session"], "pane_id": meta["herdr_pane"]}, HTTPStatus.CREATED)
            except (ValueError, HerdrError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        if parts == ["api", "live"]:
            body = body or {}
            engine = str(body.get("engine", ""))
            if engine not in ENGINES:
                return self._error(HTTPStatus.BAD_REQUEST, f"engine must be one of {ENGINES}")
            try:
                from .interactive import InteractiveRun

                cwd = os.path.expanduser(str(body.get("cwd", "")))
                branch = None
                if body.get("branch") and str(body.get("prompt", "")).strip():
                    cwd, branch = work.new_worktree(cwd, str(body.get("prompt", "")))
                irun = InteractiveRun.start(
                    engine, str(body.get("prompt", "")), cwd,
                    label="dash", source="dashboard", system=agent_brief(cwd),
                    extra={"branch": branch} if branch else None,
                )
            except (ValueError, HerdrError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            LIVE_RUNS.append(irun)
            meta = irun.run.meta()
            self._mark_finding(body, {"run_id": meta["id"], "session": meta["herdr_session"], "pane_id": meta["herdr_pane"]})
            return self._json({"run_id": meta["id"], "session": meta["herdr_session"], "pane_id": meta["herdr_pane"]}, HTTPStatus.CREATED)
        if parts == ["api", "runs"]:
            if body is None:
                return self._error(HTTPStatus.BAD_REQUEST, "JSON body required")
            try:
                cwd = os.path.expanduser(str(body.get("cwd", "")))
                branch = None
                if body.get("branch") and str(body.get("prompt", "")).strip():
                    cwd, branch = work.new_worktree(cwd, str(body.get("prompt", "")))
                run = Run.create(
                    engine=str(body.get("engine", "")),
                    prompt=str(body.get("prompt", "")),
                    cwd=cwd,
                    mode=str(body.get("mode", "readonly")),
                    source="dashboard",
                    extra={"system_prompt": (standing := agent_brief(cwd)), "conclude": bool(standing), "branch": branch},
                )
            except ValueError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            start_turn(run, None)
            self._mark_finding(body, {"run_id": run.id})
            return self._json(run.meta(), HTTPStatus.CREATED)

        if len(parts) == 4 and parts[:2] == ["api", "runs"]:
            run = self._run_or_404(parts[2])
            if not run:
                return
            meta = fresh_meta(run)
            if parts[3] == "seen":
                fleet.mark_run_seen(run.id)
                return self._json({"ok": True})
            if meta.get("runtime", "headless") != "headless":
                return self._error(HTTPStatus.CONFLICT, "live runs are controlled from Slack or `herdr session attach`")
            if parts[3] == "cancel":
                if meta["status"] != "running":
                    return self._error(HTTPStatus.CONFLICT, "run is not running")
                run.cancel()
                return self._json({"ok": True})
            if parts[3] == "message":
                prompt = str((body or {}).get("prompt", "")).strip()
                if not prompt:
                    return self._error(HTTPStatus.BAD_REQUEST, "prompt is required")
                if meta["status"] == "running":
                    return self._error(HTTPStatus.CONFLICT, "wait for the current turn to finish")
                if not meta.get("session_id"):
                    return self._error(HTTPStatus.CONFLICT, "no session to continue")
                start_turn(run, prompt)
                return self._json({"ok": True}, HTTPStatus.ACCEPTED)
        self._error(HTTPStatus.NOT_FOUND, "not found")

    @staticmethod
    def _mark_finding(body: dict | None, work_ref: dict) -> None:
        """A task started from a watch finding: remember which agent is working on it."""
        fid = str((body or {}).get("finding") or "")
        if fid and watches.get_finding(fid):
            watches.set_status(fid, "working", work_ref)

    # ----------------------------------------------------------------- stream

    def _stream(self, run: Run):
        fleet.mark_run_seen(run.id)  # you opened it: it no longer needs reading on the board
        query = parse_qs(urlparse(self.path).query)
        try:
            start = max(0, int((query.get("from") or ["0"])[0]))
        except ValueError:
            start = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        path = run.dir / "events.jsonl"
        seq = 0
        last_meta = None
        last_beat = time.monotonic()
        try:
            with path.open() as fh:
                while True:
                    line = fh.readline()
                    if line.endswith("\n"):
                        if seq >= start:
                            self.wfile.write(f"event: timeline\ndata: {line.strip()}\n\n".encode())
                            self.wfile.flush()
                        seq += 1
                        continue
                    if line:  # partial line still being written
                        fh.seek(fh.tell() - len(line.encode()))
                    meta = fresh_meta(run)
                    snapshot = (meta["status"], meta.get("session_id"), meta.get("cost_usd"), meta.get("turns"))
                    if snapshot != last_meta:
                        self.wfile.write(f"event: meta\ndata: {json.dumps(meta)}\n\n".encode())
                        self.wfile.flush()
                        last_meta = snapshot
                    if time.monotonic() - last_beat > HEARTBEAT_SECONDS:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    time.sleep(POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _static(self, name: str, content_type: str):
        body = (STATIC / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if content_type.startswith("text/") and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=6)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        query_token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        if query_token and self._token_ok():
            # Remember the token so a phone bookmark without ?token= keeps working.
            self.send_header("Set-Cookie", f"cos_token={query_token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)


def adopt_orphans() -> list[str]:
    """After a restart, keep following every background run whose engine is still running."""
    adopted = []
    for meta in list_runs(200, include_running=True):
        if meta.get("runtime", "headless") != "headless" or meta.get("status") != "running":
            continue
        run = Run(meta["id"])
        if not pid_alive(meta.get("pid")):
            if not meta.get("pid") and meta.get("retry_owner") and not pid_alive(meta["retry_owner"]):
                # Waiting to auto-restart when the bot that owned it stopped: nobody will resume it.
                run.update_meta(status="failed", retry_owner=None, note="bot restarted during an auto-restart wait")
            continue

        def follow(run=run):
            with ACTIVE_LOCK:
                ACTIVE[run.id] = run
            try:
                run.adopt()
            finally:
                with ACTIVE_LOCK:
                    ACTIVE.pop(run.id, None)
            if run.meta().get("kind") == "watch":
                watches.checked(run)

        threading.Thread(target=follow, daemon=True, name=f"adopt-{run.id}").start()
        adopted.append(run.id)
    return adopted


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return  # a phone dropped the connection (sleep, network change): not an error
        super().handle_error(request, client_address)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    if host not in ("127.0.0.1", "localhost") and not os.environ.get("COS_TOKEN"):
        print("warning: listening beyond localhost without COS_TOKEN; anyone who can reach this port can run agents")
    adopted = adopt_orphans()
    if adopted:
        print(f"following {len(adopted)} run(s) started before this restart")
    # At boot launchd can start us before Tailscale has assigned its address; wait for it.
    for attempt in range(60):
        try:
            httpd = Server((host, port), Handler)
            break
        except OSError as e:
            if e.errno != errno.EADDRNOTAVAIL or attempt == 59:
                raise
            if attempt == 0:
                print(f"waiting for {host} to become available (Tailscale not up yet?)")
            time.sleep(5)
    print(f"cos dashboard on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
