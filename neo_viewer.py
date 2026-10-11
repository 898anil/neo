"""View Neo's Slack-bot conversations -- as a local web server or on the CLI.

Two front-ends over one shared transcript parser:

    python neo_viewer.py web            # browse at http://127.0.0.1:8787
    python neo_viewer.py logs           # list threads in the terminal
    python neo_viewer.py logs latest -f # follow the newest thread live
    python neo_viewer.py activity -f    # follow the high-level activity feed

Reads the per-thread transcripts written by runner.py (TRANSCRIPT_DIR) and the
app's activity log (stdout captured by launchd). No third-party dependencies --
stdlib only, so it runs under the same venv without extra installs.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# --- Locations -------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_DIR = Path(os.environ.get("NEO_TRANSCRIPT_DIR", BASE_DIR / "transcripts"))
SESSIONS_FILE = Path(os.environ.get("NEO_SESSIONS_FILE", BASE_DIR / "sessions.json"))
ACTIVITY_LOG = Path(
    os.environ.get("NEO_ACTIVITY_LOG", Path.home() / "Library/Logs/neo-slack-bot.log")
)

# A transcript entry begins at a line like "[2026-09-11 13:37:48] ...".
_TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$", re.DOTALL)
# The marker app.py injects before the actual new user message.
_NEW_MSG_MARKER = "Now respond to this new message directed at you:\n"


# --- Parsing ---------------------------------------------------------------

@dataclass
class Entry:
    """One logical transcript entry (may span several physical lines)."""

    ts: str          # "2026-09-11 13:37:48" or "" if the line had no stamp
    kind: str        # PROMPT / ASSISTANT_TEXT / TOOL_USE / RESULT_TEXT / ...
    body: str        # marker stripped, continuation lines joined
    raw: str         # original text incl. marker, for the raw view


_KINDS = [
    ("--- RUN start", "RUN_START"),
    ("--- RUN end", "RUN_END"),
    ("PROMPT:", "PROMPT"),
    ("SYSTEM", "SYSTEM"),
    ("ASSISTANT text:", "ASSISTANT_TEXT"),
    ("ASSISTANT thinking:", "THINKING"),
    ("TOOL_USE", "TOOL_USE"),
    ("TOOL_RESULT", "TOOL_RESULT"),
    ("RESULT text:", "RESULT_TEXT"),
    ("RESULT is_error", "RESULT_META"),
    ("EXCEPTION", "EXCEPTION"),
]

# Markers whose leading label we strip to get a clean body.
_STRIP = {
    "PROMPT": "PROMPT:",
    "ASSISTANT_TEXT": "ASSISTANT text:",
    "THINKING": "ASSISTANT thinking:",
    "RESULT_TEXT": "RESULT text:",
}


def _classify(first_line: str) -> str:
    for prefix, kind in _KINDS:
        if first_line.startswith(prefix):
            return kind
    return "OTHER"


def parse_transcript(path: Path) -> list[Entry]:
    """Parse a transcript file into ordered entries, grouping multi-line bodies."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    entries: list[Entry] = []
    cur_ts = ""
    cur_lines: list[str] = []

    def flush() -> None:
        if not cur_lines:
            return
        raw = "\n".join(cur_lines)
        kind = _classify(cur_lines[0])
        body = raw
        label = _STRIP.get(kind)
        if label and body.startswith(label):
            body = body[len(label):].lstrip()
        entries.append(Entry(ts=cur_ts, kind=kind, body=body, raw=raw))

    for line in text.splitlines():
        m = _TS_RE.match(line)
        if m:
            flush()
            cur_ts = m.group(1)
            cur_lines = [m.group(2)]
        elif cur_lines:
            cur_lines.append(line)
        else:
            # Stray leading line with no stamp; keep it so nothing is lost.
            cur_ts = ""
            cur_lines = [line]
    flush()
    return entries


def real_message(prompt_body: str) -> str:
    """Pull the actual new user message out of a PROMPT body.

    app.py prepends the Slack thread as context and separates the fresh ask with
    a fixed marker; if it's absent (first turn, no context) return the whole body.
    """
    idx = prompt_body.rfind(_NEW_MSG_MARKER)
    if idx == -1:
        return prompt_body.strip()
    return prompt_body[idx + len(_NEW_MSG_MARKER):].strip()


def _tool_name(body: str) -> str:
    # body looks like "TOOL_USE Bash id=... input=..."
    parts = body.split()
    return parts[1] if len(parts) > 1 else "?"


# --- Thread discovery ------------------------------------------------------

@dataclass
class Thread:
    thread_ts: str
    path: Path
    mtime: float
    size: int
    session_id: str | None = None
    first_message: str = ""
    turns: int = 0
    errored: bool = False

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")


def _load_sessions() -> dict[str, str]:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_threads() -> list[Thread]:
    sessions = _load_sessions()
    threads: list[Thread] = []
    if not TRANSCRIPT_DIR.is_dir():
        return threads
    for path in TRANSCRIPT_DIR.glob("*.log"):
        try:
            st = path.stat()
        except OSError:
            continue
        entries = parse_transcript(path)
        first = ""
        turns = 0
        errored = False
        for e in entries:
            if e.kind == "PROMPT":
                turns += 1
                if not first:
                    first = real_message(e.body)
            elif e.kind == "RESULT_META" and "is_error=True" in e.raw:
                errored = True
            elif e.kind == "EXCEPTION":
                errored = True
        threads.append(
            Thread(
                thread_ts=path.stem,
                path=path,
                mtime=st.st_mtime,
                size=st.st_size,
                session_id=sessions.get(path.stem),
                first_message=first,
                turns=turns,
                errored=errored,
            )
        )
    threads.sort(key=lambda t: t.mtime, reverse=True)
    return threads


def resolve_thread(name: str) -> Thread | None:
    threads = list_threads()
    if not threads:
        return None
    if name in ("latest", "last", "-"):
        return threads[0]
    for t in threads:
        if t.name == name:
            return t
    # Allow a unique prefix match for convenience.
    matches = [t for t in threads if t.name.startswith(name)]
    return matches[0] if len(matches) == 1 else None


# --- Activity feed ---------------------------------------------------------

@dataclass
class Activity:
    ts: str
    kind: str   # "mention" | "result" | "other"
    text: str


_ACT_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ \w+: (mention|result): (.*)$")


def read_activity(limit: int | None = None) -> list[Activity]:
    if not ACTIVITY_LOG.exists():
        return []
    out: list[Activity] = []
    try:
        for line in ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ACT_RE.match(line)
            if m:
                out.append(Activity(ts=m.group(1), kind=m.group(2), text=m.group(3)))
    except OSError:
        return []
    if limit is not None:
        out = out[-limit:]
    return out


# --- Clean conversation rendering (shared by CLI + web) --------------------

@dataclass
class Turn:
    """One rendered turn: the user's ask and Neo's answer for a run."""

    ts: str
    user: str = ""
    tools: list[str] = field(default_factory=list)
    answer: str = ""
    streamed: str = ""
    errored: bool = False


def to_turns(entries: list[Entry]) -> list[Turn]:
    """Collapse raw entries into readable user/Neo turns."""
    turns: list[Turn] = []
    cur: Turn | None = None
    stream_parts: list[str] = []

    def close() -> None:
        nonlocal cur
        if cur is not None:
            cur.streamed = "\n".join(p for p in stream_parts if p.strip())
            if not cur.answer:
                cur.answer = cur.streamed
            turns.append(cur)
        stream_parts.clear()

    for e in entries:
        if e.kind == "PROMPT":
            close()
            cur = Turn(ts=e.ts, user=real_message(e.body))
        elif cur is None:
            continue
        elif e.kind == "ASSISTANT_TEXT":
            stream_parts.append(e.body)
        elif e.kind == "TOOL_USE":
            cur.tools.append(_tool_name(e.raw))
        elif e.kind == "RESULT_TEXT":
            cur.answer = e.body
        elif e.kind == "RESULT_META" and "is_error=True" in e.raw:
            cur.errored = True
        elif e.kind == "EXCEPTION":
            cur.errored = True
            stream_parts.append(e.raw)
    close()
    return turns


# --- CLI -------------------------------------------------------------------

_C = {
    "dim": "\033[2m", "bold": "\033[1m", "reset": "\033[0m",
    "blue": "\033[34m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "cyan": "\033[36m",
}


def _color(enabled: bool):
    return _C if enabled else {k: "" for k in _C}


def cmd_list(args) -> int:
    c = _color(sys.stdout.isatty() and not args.no_color)
    threads = list_threads()
    if not threads:
        print(f"No transcripts found in {TRANSCRIPT_DIR}")
        return 0
    print(f"{c['bold']}Neo threads{c['reset']} ({TRANSCRIPT_DIR})\n")
    for t in threads:
        flag = f"{c['red']}✗{c['reset']}" if t.errored else f"{c['green']}✓{c['reset']}"
        preview = (t.first_message or "").replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:69] + "…"
        print(f"{flag} {c['cyan']}{t.name:<20}{c['reset']} "
              f"{c['dim']}{t.when}  {t.turns:>2} turns{c['reset']}  {preview}")
    print(f"\n{c['dim']}View one:  neo_viewer.py logs <thread|latest> [-f] [--raw]{c['reset']}")
    return 0


def _render_turns_text(turns: list[Turn], c: dict) -> str:
    lines: list[str] = []
    for t in turns:
        lines.append(f"{c['dim']}{t.ts}{c['reset']}")
        lines.append(f"{c['blue']}{c['bold']}▶ You:{c['reset']} {t.user}")
        if t.tools:
            from collections import Counter
            summary = ", ".join(f"{n}×{k}" if n > 1 else k
                                for k, n in Counter(t.tools).items())
            lines.append(f"  {c['yellow']}🔧 {summary}{c['reset']}")
        head = f"{c['red']}⚠ Neo (error):{c['reset']}" if t.errored else f"{c['green']}{c['bold']}◆ Neo:{c['reset']}"
        answer = t.answer or "(no output)"
        lines.append(f"{head} {answer}")
        lines.append("")
    return "\n".join(lines)


def _print_transcript(thread: Thread, raw: bool, color: bool) -> None:
    c = _color(color)
    entries = parse_transcript(thread.path)
    if raw:
        for e in entries:
            sys.stdout.write(e.raw + "\n")
    else:
        sys.stdout.write(_render_turns_text(to_turns(entries), c))
    sys.stdout.flush()


def _follow(path: Path, render, poll: float = 1.0) -> int:
    """Print existing content via render(), then stream appended lines raw."""
    render()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(poll)
    except KeyboardInterrupt:
        return 0


def cmd_logs(args) -> int:
    if not args.thread:
        return cmd_list(args)
    thread = resolve_thread(args.thread)
    if thread is None:
        print(f"No thread matching {args.thread!r}. Run without args to list threads.",
              file=sys.stderr)
        return 1
    color = sys.stdout.isatty() and not args.no_color
    if args.follow:
        c = _color(color)
        print(f"{c['dim']}— following {thread.name} (Ctrl-C to stop) —{c['reset']}")
        return _follow(thread.path, lambda: _print_transcript(thread, args.raw, color))
    _print_transcript(thread, args.raw, color)
    return 0


def cmd_activity(args) -> int:
    c = _color(sys.stdout.isatty() and not args.no_color)

    def render() -> None:
        for a in read_activity(limit=args.limit):
            col = c["blue"] if a.kind == "mention" else c["green"]
            print(f"{c['dim']}{a.ts}{c['reset']} {col}{a.kind:<7}{c['reset']} {a.text}")

    if args.follow:
        if not ACTIVITY_LOG.exists():
            print(f"Activity log not found: {ACTIVITY_LOG}", file=sys.stderr)
            return 1
        print(f"{c['dim']}— following activity (Ctrl-C to stop) —{c['reset']}")
        return _follow(ACTIVITY_LOG, render)
    if not read_activity():
        print(f"No activity yet ({ACTIVITY_LOG})")
    render()
    return 0


# --- Web server ------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Neo conversations</title>
<style>
  :root{ --bg:#f7f7f8; --panel:#fff; --border:#e4e4e7; --text:#1c1c1f; --dim:#71717a;
         --accent:#4f46e5; --user:#2563eb; --neo:#059669; --err:#dc2626; --tool:#b45309;
         --chip:#f0f0f2; }
  @media (prefers-color-scheme:dark){ :root{ --bg:#0e0e11; --panel:#17171b; --border:#2a2a30;
         --text:#e7e7ea; --dim:#8a8a94; --accent:#818cf8; --user:#60a5fa; --neo:#34d399;
         --err:#f87171; --tool:#fbbf24; --chip:#232329; } }
  *{box-sizing:border-box} html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column}
  header{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
  header h1{font-size:15px;margin:0;font-weight:650} header .grow{flex:1}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--neo);display:inline-block}
  .toggle{display:flex;gap:4px;background:var(--chip);border-radius:8px;padding:3px}
  .toggle button,.btn{font:inherit;border:0;background:transparent;color:var(--dim);padding:4px 10px;border-radius:6px;cursor:pointer}
  .toggle button.on{background:var(--panel);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.12)}
  label.auto{color:var(--dim);display:flex;align-items:center;gap:5px;cursor:pointer;font-size:13px}
  main{flex:1;display:flex;min-height:0}
  aside{width:300px;border-right:1px solid var(--border);overflow-y:auto;background:var(--panel);flex-shrink:0}
  aside .tab{width:100%;text-align:left;border:0;border-bottom:1px solid var(--border);background:transparent;color:var(--text);padding:10px 14px;cursor:pointer;display:block}
  aside .tab:hover{background:var(--chip)} aside .tab.on{background:var(--chip);border-left:3px solid var(--accent);padding-left:11px}
  aside .tab .top{display:flex;justify-content:space-between;gap:8px} aside .tab .nm{font-weight:600;font-size:12px}
  aside .tab .msg{color:var(--dim);font-size:12.5px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  aside .tab .meta{color:var(--dim);font-size:11px;white-space:nowrap}
  section{flex:1;overflow-y:auto;padding:20px 24px;min-width:0}
  .turn{margin:0 0 22px} .who{font-weight:650;font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
  .who.u{color:var(--user)} .who.n{color:var(--neo)} .who.e{color:var(--err)}
  .ts{color:var(--dim);font-size:11px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px}
  .bubble{white-space:pre-wrap;word-wrap:break-word;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 13px}
  .bubble.u{border-color:color-mix(in srgb,var(--user) 35%,var(--border))}
  .tools{margin:6px 0;color:var(--tool);font-size:12.5px}
  .chip{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;margin-right:5px;font-size:11.5px}
  pre.raw{white-space:pre-wrap;word-wrap:break-word;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
  .act{padding:6px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:baseline}
  .act .k{font-weight:600;font-size:11px;text-transform:uppercase;width:56px;flex-shrink:0}
  .act .k.mention{color:var(--user)} .act .k.result{color:var(--neo)}
  .act .t{color:var(--dim);font-size:11px;width:135px;flex-shrink:0} .act .x{white-space:pre-wrap;word-break:break-word}
  .empty{color:var(--dim);padding:40px;text-align:center} .err{color:var(--err)}
  code{background:var(--chip);border-radius:4px;padding:1px 4px;font:12.5px ui-monospace,monospace}
</style></head>
<body>
<header>
  <span class="dot" id="live" title="polling"></span>
  <h1>Neo conversations</h1>
  <div class="toggle" id="view">
    <button data-v="clean" class="on">Clean</button>
    <button data-v="raw">Raw</button>
    <button data-v="activity">Activity</button>
  </div>
  <div class="grow"></div>
  <label class="auto"><input type="checkbox" id="auto" checked> auto-refresh</label>
  <button class="btn" id="refresh">↻</button>
</header>
<main>
  <aside id="threads"></aside>
  <section id="body"><div class="empty">Loading…</div></section>
</main>
<script>
const S={view:"clean",thread:null,threads:[],auto:true};
const esc=s=>s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function jget(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json();}

async function loadThreads(){
  S.threads=await jget("/api/threads");
  if(!S.thread&&S.threads.length)S.thread=S.threads[0].name;
  const el=document.getElementById("threads");
  el.innerHTML=S.threads.map(t=>`
    <button class="tab ${t.name===S.thread?'on':''}" data-t="${t.name}">
      <div class="top"><span class="nm ${t.errored?'err':''}">${t.errored?'✗ ':''}${esc(t.name)}</span>
        <span class="meta">${esc(t.when)}</span></div>
      <div class="msg">${esc(t.first_message||'(no prompt)')}</div>
      <div class="meta">${t.turns} turn${t.turns===1?'':'s'}${t.session_id?' · '+t.session_id.slice(0,8):''}</div>
    </button>`).join("")||'<div class="empty">No threads yet</div>';
  el.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{S.thread=b.dataset.t;render();});
}

function renderClean(turns){
  if(!turns.length)return '<div class="empty">No messages in this thread yet</div>';
  return turns.map(t=>`<div class="turn">
    <div class="who u">You<span class="ts">${esc(t.ts)}</span></div>
    <div class="bubble u">${esc(t.user||'')}</div>
    ${t.tools.length?`<div class="tools">🔧 ${t.tools.map(x=>`<span class="chip">${esc(x)}</span>`).join("")}</div>`:""}
    <div class="who ${t.errored?'e':'n'}">${t.errored?'Neo · error':'Neo'}</div>
    <div class="bubble">${esc(t.answer||'(no output)')}</div>
  </div>`).join("");
}

async function render(){
  await loadThreads();
  const body=document.getElementById("body");
  document.querySelectorAll("#view button").forEach(b=>b.classList.toggle("on",b.dataset.v===S.view));
  try{
    if(S.view==="activity"){
      const a=await jget("/api/activity");
      body.innerHTML=a.length?a.map(x=>`<div class="act"><span class="t">${esc(x.ts)}</span>
        <span class="k ${x.kind}">${x.kind}</span><span class="x">${esc(x.text)}</span></div>`).join(""):
        '<div class="empty">No activity logged yet</div>';
      return;
    }
    if(!S.thread){body.innerHTML='<div class="empty">No threads yet</div>';return;}
    const d=await jget("/api/thread/"+encodeURIComponent(S.thread)+"?raw="+(S.view==="raw"?1:0));
    body.innerHTML=S.view==="raw"
      ? '<pre class="raw">'+esc(d.raw||"")+'</pre>'
      : renderClean(d.turns||[]);
  }catch(e){body.innerHTML='<div class="empty err">Error: '+esc(String(e))+'</div>';}
}

document.querySelectorAll("#view button").forEach(b=>b.onclick=()=>{S.view=b.dataset.v;render();});
document.getElementById("refresh").onclick=render;
document.getElementById("auto").onchange=e=>{S.auto=e.target.checked;};
setInterval(()=>{if(S.auto)render();},3000);
render();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "NeoViewer/1.0"

    def log_message(self, *a):  # quiet by default
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/threads":
            self._json([
                {
                    "name": t.name, "when": t.when, "turns": t.turns,
                    "errored": t.errored, "session_id": t.session_id,
                    "first_message": t.first_message,
                }
                for t in list_threads()
            ])
        elif path == "/api/activity":
            self._json([
                {"ts": a.ts, "kind": a.kind, "text": a.text}
                for a in read_activity(limit=400)
            ])
        elif path.startswith("/api/thread/"):
            name = path[len("/api/thread/"):]
            from urllib.parse import unquote
            thread = resolve_thread(unquote(name))
            if thread is None:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            entries = parse_transcript(thread.path)
            raw = "raw=1" in (urlparse(self.path).query or "")
            self._json({
                "name": thread.name,
                "raw": "\n".join(e.raw for e in entries) if raw else "",
                "turns": [] if raw else [
                    {"ts": t.ts, "user": t.user, "tools": t.tools,
                     "answer": t.answer, "errored": t.errored}
                    for t in to_turns(entries)
                ],
            })
        else:
            self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET


def cmd_web(args) -> int:
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Neo viewer serving {TRANSCRIPT_DIR}")
    print(f"  → {url}   (Ctrl-C to stop)")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


# --- Entry point -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="View Neo Slack-bot conversations.")
    sub = p.add_subparsers(dest="cmd")

    w = sub.add_parser("web", help="serve a local web viewer")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=int(os.environ.get("NEO_VIEWER_PORT", "8787")))
    w.add_argument("--open", action="store_true", help="open a browser on start")
    w.set_defaults(func=cmd_web)

    lo = sub.add_parser("logs", help="list threads, or view/follow one")
    lo.add_argument("thread", nargs="?", help="thread id, a unique prefix, or 'latest'")
    lo.add_argument("-f", "--follow", action="store_true", help="stream new lines live")
    lo.add_argument("--raw", action="store_true", help="show the raw transcript")
    lo.add_argument("--no-color", action="store_true")
    lo.set_defaults(func=cmd_logs)

    ls = sub.add_parser("list", help="list threads")
    ls.add_argument("--no-color", action="store_true")
    ls.set_defaults(func=cmd_list)

    ac = sub.add_parser("activity", help="show/follow the high-level activity feed")
    ac.add_argument("-f", "--follow", action="store_true")
    ac.add_argument("-n", "--limit", type=int, default=200)
    ac.add_argument("--no-color", action="store_true")
    ac.set_defaults(func=cmd_activity)
    return p


def main(argv: list[str] | None = None) -> int:
    # Behave like a normal CLI when piped into head/less: exit quietly on a
    # closed pipe instead of dumping a BrokenPipeError traceback.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare invocation → list threads (handy default).
        args = parser.parse_args(["list"])
    try:
        return args.func(args)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
