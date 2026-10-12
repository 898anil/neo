"""Draw a trace digest from its structured fields (see digest.SCHEMA) as one fixed HTML layout.

The model only fills fields; the page decides how they look, so every digest reads the same way:
state, what was asked, what came of it, the plan as a checklist, then the cause chain, numbers,
changes, checks and timeline when the work has them. Everything here is escaped text; the only
links are http(s) URLs. Colours come from the trace page's CSS variables (digestDoc in index.html).
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

STATE = {"working": ("Working", "azure"), "needs_you": ("Needs you", "amber"), "blocked": ("Blocked", "amber"),
         "done": ("Done", "mint"), "failed": ("Failed", "coral"), "stopped": ("Stopped", "muted")}
RESULT = {"worked": ("Worked", "mint"), "partial": ("Partly done", "amber"), "failed": ("Failed", "coral"),
          "unclear": ("Unclear", "amber"), "pending": ("In progress", "azure")}
STEP = {"done": "mint", "doing": "azure", "todo": "faint", "failed": "coral", "skipped": "faint"}
TONE = {"ok": "mint", "new": "mint", "changed": "amber", "unsure": "amber", "broken": "coral", "neutral": "muted"}
TONE_WORD = {"new": "new", "changed": "changed", "broken": "broken", "unsure": "unsure"}
CHECK = {"pass": ("mint", "✓"), "fail": ("coral", "✕"), "pending": ("amber", "◷"), "none": ("faint", "–")}
KIND = {"rca": "Root cause", "plan": "Plan", "fix": "Fix", "investigation": "Investigation", "other": "Digest"}
CONF = {"high": 3, "medium": 2, "low": 1}

CSS = """
.dg{--wash:color-mix(in srgb,var(--text) 6%,transparent);display:grid;gap:22px;max-width:640px;margin:0 auto;font-size:14px;line-height:1.5}
.dg *{margin:0}
.dg>*{min-width:0}
.dg .lbl{font:600 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px}
.dg .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.dg .pill{display:inline-flex;align-items:center;gap:7px;font:600 12px/1 var(--mono);padding:6px 10px;border-radius:999px;
  color:var(--c);background:color-mix(in srgb,var(--c) 12%,transparent)}
.dg .pill i{width:7px;height:7px;border-radius:50%;background:var(--c)}
.dg .pill.live i{animation:p 1.6s ease-in-out infinite}@keyframes p{50%{opacity:.25}}
.dg .note{font:12px var(--mono);color:var(--muted)}
.dg .kind{margin-left:auto;font:11px var(--mono);color:var(--faint)}
.dg .need{display:flex;gap:10px;align-items:baseline;padding:10px 12px;border-radius:10px;background:color-mix(in srgb,var(--amber) 13%,transparent);
  color:var(--text);font-size:14px}
.dg .need b{font:600 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--amber);flex:none}
.dg .intent{font:500 20px/1.3 var(--serif),Georgia,serif;letter-spacing:-.005em;color:var(--text);text-wrap:balance}
.dg .out{display:grid;gap:8px;padding:14px 16px;border-radius:12px;background:var(--wash)}
.dg .out .h{font-size:16px;font-weight:600;color:var(--text);text-wrap:balance}
.dg .out .row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
.dg .meter{display:inline-flex;align-items:center;gap:6px;font:11px var(--mono);color:var(--faint)}
.dg .meter i{width:14px;height:4px;border-radius:2px;background:var(--line)}
.dg .meter i.on{background:var(--c)}
.dg .why{font:11.5px/1.4 var(--mono);color:var(--faint)}
.dg .prog{display:flex;align-items:center;gap:10px;font:11px var(--mono);color:var(--faint);margin-bottom:10px}
.dg .prog .bar{flex:1;height:4px;border-radius:2px;background:var(--line);overflow:hidden;display:flex}
.dg .prog .bar i{height:100%}
.dg ol.steps{list-style:none;padding:0;display:grid;position:relative}
.dg ol.steps::before{content:"";position:absolute;left:9px;top:12px;bottom:12px;width:1px;background:var(--line)}
.dg ol.steps li{display:grid;grid-template-columns:20px 1fr;gap:12px;padding:6px 0;position:relative}
.dg .ic{width:19px;height:19px;border-radius:50%;display:grid;place-items:center;font:700 10px/1 var(--mono);background:var(--panel);
  box-shadow:inset 0 0 0 1.5px var(--c);color:var(--c)}
.dg .ic.fill{background:var(--c);color:var(--panel);box-shadow:none}
.dg .ic.half{background:conic-gradient(var(--c) 0 50%,var(--panel) 0);box-shadow:inset 0 0 0 1.5px var(--c)}
.dg .st{color:var(--text)}
.dg .st.dim{color:var(--faint)}.dg .st.skip{color:var(--faint);text-decoration:line-through}
.dg .st small{display:block;font:11.5px/1.4 var(--mono);color:var(--faint);margin-top:2px}
.dg .chain{display:grid;gap:0}
.dg .node{display:grid;gap:3px;padding:10px 12px;border-radius:10px;background:var(--panel);box-shadow:0 0 0 1px var(--line),inset 3px 0 0 var(--c)}
.dg .node.broken{background:color-mix(in srgb,var(--coral) 7%,var(--panel))}
.dg .node .t{display:flex;justify-content:space-between;gap:8px;font-weight:600;color:var(--text)}
.dg .node .t em{font:600 10px var(--mono);font-style:normal;text-transform:uppercase;letter-spacing:.06em;color:var(--c)}
.dg .node small{font-size:12.5px;color:var(--muted)}
.dg .via{display:flex;align-items:center;gap:8px;padding:3px 0 3px 18px;font:11px var(--mono);color:var(--faint)}
.dg .via::before{content:"↓";font:13px var(--mono);color:var(--faint)}
.dg .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(140px,45%),1fr));gap:8px}
.dg .tile{padding:12px;border-radius:10px;background:var(--wash)}
.dg .tile b{display:block;font:500 26px/1.1 var(--serif),Georgia,serif;color:var(--c)}
.dg .tile span{font:11px var(--mono);color:var(--faint)}
.dg .files{display:grid;border-top:1px solid var(--line)}
.dg .file{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 12px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line);font:12px var(--mono)}
.dg .file .p{min-width:0;display:grid;gap:1px}
.dg .file .p b{font-weight:600;color:var(--text);overflow-wrap:anywhere}
.dg .file .p span{color:var(--faint);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
.dg .file .r{display:flex;align-items:center;gap:10px}
.dg .file .b{display:flex;width:70px;height:6px;border-radius:3px;overflow:hidden;background:var(--wash)}
.dg .file .n{text-align:right;color:var(--faint);white-space:nowrap}
.dg .tag{font:600 10px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--c)}
.dg .checks{display:flex;flex-wrap:wrap;gap:6px}
.dg .chk{display:inline-flex;gap:6px;align-items:center;font:12px var(--mono);padding:5px 10px;border-radius:999px;color:var(--c);
  background:color-mix(in srgb,var(--c) 11%,transparent);text-decoration:none}
.dg .tl{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(96px,1fr);overflow-x:auto;position:relative;padding-top:2px}
.dg .tl::before{content:"";position:absolute;left:0;right:0;top:7px;height:1px;background:var(--line)}
.dg .ev{display:grid;gap:6px;padding-right:10px;position:relative}
.dg .ev i{width:11px;height:11px;border-radius:50%;background:var(--c);box-shadow:0 0 0 3px var(--panel)}
.dg .ev b{font:600 11px var(--mono);color:var(--muted)}
.dg .ev span{font-size:12.5px;line-height:1.35;color:var(--text)}
.dg .next{display:flex;gap:10px;align-items:baseline;font-size:14.5px;color:var(--text)}
.dg .next::before{content:"→";font:600 14px var(--mono);color:var(--azure)}
.dg details summary{cursor:pointer;font:600 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint);list-style:none}
.dg details summary::-webkit-details-marker{display:none}
.dg details[open] summary{margin-bottom:8px}
.dg details ul{padding:0;list-style:none;display:grid;gap:4px;font-size:13px}
.dg a{color:var(--azure);text-decoration:none}
"""


_EMPTY = {"", '""', "''", "none", "n/a", "na", "-", "—", "null"}


def _blank(value: Any) -> bool:
    """A field the model left empty, including the ways it writes "nothing" ('""', "none", "n/a")."""
    return str(value or "").strip().lower() in _EMPTY


def _k(value: Any) -> str:
    """An enum field as a lookup key; anything that is not a string (bad stored data) matches nothing."""
    return value if isinstance(value, str) else ""


def _t(value: Any, limit: int = 240) -> str:
    return "" if _blank(value) else escape(str(value).strip()[:limit])


def _url(value: Any) -> str:
    u = str(value or "").strip()
    return escape(u, quote=True) if u.startswith(("https://", "http://")) and len(u) < 2000 else ""


def _c(name: str) -> str:
    return f"--c:var(--{name})"


def _rows(value: Any, limit: int) -> list[dict]:
    return [x for x in (value if isinstance(value, list) else []) if isinstance(x, dict)][:limit]


def _section(label: str, body: str) -> str:
    """label is HTML: pass fixed text or text already escaped by _t."""
    return f'<section><p class="lbl">{label}</p>{body}</section>' if body else ""


def _header(d: dict) -> str:
    label, colour = STATE.get(_k(d.get("state")), STATE["stopped"])
    live = " live" if _k(d.get("state")) == "working" else ""
    note = f'<span class="note">{_t(d.get("state_note"), 80)}</span>' if not _blank(d.get("state_note")) else ""
    head = (f'<div class="top"><span class="pill{live}" style="{_c(colour)}"><i></i>{label}</span>{note}'
            f'<span class="kind">{escape(KIND.get(_k(d.get("kind")), "Digest"))}</span></div>')
    need = d.get("needs_you")
    if not _blank(need):
        head += f'<div class="need"><b>Needs you</b><span>{_t(need, 200)}</span></div>'
    return head


def _outcome(d: dict) -> str:
    out = d.get("outcome") if isinstance(d.get("outcome"), dict) else {}
    label, colour = RESULT.get(_k(out.get("result")), RESULT["unclear"])
    n = CONF.get(_k(d.get("confidence")), 0)
    meter = ""
    if n:
        conf_colour = {3: "mint", 2: "amber", 1: "coral"}[n]
        bars = "".join(f'<i class="{"on" if i < n else ""}"></i>' for i in range(3))
        meter = f'<span class="meter" style="{_c(conf_colour)}">{bars} {escape(d["confidence"])} confidence</span>'
    why = f'<p class="why">{_t(d.get("confidence_why"), 120)}</p>' if not _blank(d.get("confidence_why")) else ""
    headline = _t(out.get("headline"), 160)
    if not headline and not meter:
        return ""
    return (f'<div class="out"><div class="row"><span class="pill" style="{_c(colour)}"><i></i>{label}</span>{meter}</div>'
            f'<p class="h">{headline}</p>{why}</div>')


def _plan(d: dict) -> str:
    steps = _rows(d.get("plan"), 8)
    if not steps:
        return ""
    done = sum(1 for s in steps if _k(s.get("status")) == "done")
    segs = "".join(f'<i style="width:{100 / len(steps):.3f}%;background:var(--{STEP.get(_k(s.get("status")), "faint")})"></i>'
                   for s in steps if _k(s.get("status")) in ("done", "doing", "failed"))
    items = []
    for i, s in enumerate(steps, 1):
        status = _k(s.get("status")) if _k(s.get("status")) in STEP else "todo"
        colour = STEP[status]
        mark = {"done": ("fill", "✓"), "failed": ("fill", "✕"), "doing": ("half", ""), "skipped": ("", "–")}.get(status, ("", str(i)))
        cls = {"todo": " dim", "skipped": " skip"}.get(status, "")
        note = f'<small>{_t(s.get("note"), 100)}</small>' if not _blank(s.get("note")) else ""
        items.append(f'<li><span class="ic {mark[0]}" style="{_c(colour)}">{mark[1]}</span>'
                     f'<span class="st{cls}">{_t(s.get("step"), 120)}{note}</span></li>')
    prog = f'<div class="prog"><span>{done}/{len(steps)} done</span><span class="bar">{segs}</span></div>'
    return _section("Plan", prog + f'<ol class="steps">{"".join(items)}</ol>')


def _chain(d: dict) -> str:
    chain = d.get("chain") if isinstance(d.get("chain"), dict) else {}
    nodes = _rows(chain.get("nodes"), 6)
    if len(nodes) < 2:
        return ""
    parts = []
    for i, n in enumerate(nodes):
        tone = _k(n.get("tone")) if _k(n.get("tone")) in TONE else "neutral"
        word = f'<em>{TONE_WORD[tone]}</em>' if tone in TONE_WORD else ""
        detail = f'<small>{_t(n.get("detail"), 120)}</small>' if not _blank(n.get("detail")) else ""
        parts.append(f'<div class="node {tone}" style="{_c(TONE[tone])}"><div class="t"><span>{_t(n.get("label"), 80)}</span>{word}</div>{detail}</div>')
        if i < len(nodes) - 1:
            parts.append(f'<div class="via">{_t(n.get("via"), 60)}</div>')
    return _section(_t(chain.get("title"), 60) or "How it fits together", f'<div class="chain">{"".join(parts)}</div>')


def _numbers(d: dict) -> str:
    tiles = "".join(f'<div class="tile" style="{_c(TONE.get(_k(x.get("tone")), "text"))}"><b>{_t(x.get("value"), 12)}</b><span>{_t(x.get("label"), 40)}</span></div>'
                    for x in _rows(d.get("numbers"), 4) if not _blank(x.get("value")))
    return f'<div class="tiles">{tiles}</div>' if tiles else ""


def _num(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _changes(d: dict) -> str:
    files = _rows(d.get("changes"), 10)
    if not files:
        return ""
    widest = max([_num(f.get("added")) + _num(f.get("removed")) for f in files] + [1])
    rows = []
    for f in files:
        path = str(f.get("path") or "").strip()[:160]
        head, _, base = path.rpartition("/")
        # The file name first and whole; the folder after it, trimmed from the left when long.
        shown = f"<b>{escape(base or path)}</b>" + (f"<span>&lrm;{escape(head)}/&lrm;</span>" if head else "")
        add, rem = _num(f.get("added")), _num(f.get("removed"))
        kind = _k(f.get("kind")) if _k(f.get("kind")) in ("new", "edited", "deleted") else "edited"
        if add or rem:
            bar = (f'<span class="b"><i style="width:{add / widest * 100:.1f}%;background:var(--mint)"></i>'
                   f'<i style="width:{rem / widest * 100:.1f}%;background:var(--coral)"></i></span>')
            nums = f'<span class="n">+{add} −{rem}</span>'
        else:
            bar = ""
            nums = f'<span class="n"><span class="tag" style="{_c({"new": "mint", "deleted": "coral"}.get(kind, "amber"))}">{kind}</span></span>'
        rows.append(f'<div class="file"><span class="p" title="{escape(path, quote=True)}">{shown}</span><span class="r">{bar}{nums}</span></div>')
    return _section(f"Changes · {len(files)} file{'s' if len(files) != 1 else ''}", f'<div class="files">{"".join(rows)}</div>')


def _checks(d: dict) -> str:
    pills = []
    for x in _rows(d.get("checks"), 8):
        colour, mark = CHECK.get(_k(x.get("status")), CHECK["none"])
        url = _url(x.get("url"))
        inner = f'{mark} {_t(x.get("label"), 40)}'
        pills.append(f'<a class="chk" style="{_c(colour)}" href="{url}" target="_blank" rel="noopener">{inner}</a>' if url
                     else f'<span class="chk" style="{_c(colour)}">{inner}</span>')
    return _section("Checks", f'<div class="checks">{"".join(pills)}</div>') if pills else ""


def _timeline(d: dict) -> str:
    events = _rows(d.get("timeline"), 6)
    if len(events) < 2:
        return ""
    cells = "".join(f'<div class="ev" style="{_c(TONE.get(_k(e.get("tone")), "muted"))}"><i></i><b>{_t(e.get("when"), 16)}</b><span>{_t(e.get("event"), 80)}</span></div>'
                    for e in events)
    return _section("Timeline", f'<div class="tl">{cells}</div>')


def _evidence(d: dict) -> str:
    items = []
    for x in _rows(d.get("evidence"), 6):
        url, label = _url(x.get("url")), _t(x.get("label"), 120)
        if label:
            items.append(f'<li><a href="{url}" target="_blank" rel="noopener">{label}</a></li>' if url else f"<li>{label}</li>")
    return f'<details><summary>Evidence · {len(items)}</summary><ul>{"".join(items)}</ul></details>' if items else ""


def render(d: dict) -> str:
    """The digest page body for structured digest fields."""
    intent = _section("Asked", f'<p class="intent">{_t(d.get("intent"), 220)}</p>') if not _blank(d.get("intent")) else ""
    # Cause-first work (an RCA, an answer) leads with the chain; build work leads with the plan.
    cause_first = _k(d.get("kind")) in ("rca", "investigation")
    middle: Iterable[str] = ([_chain(d), _numbers(d), _plan(d)] if cause_first else [_plan(d), _chain(d), _numbers(d)])
    nxt = _section("Next", f'<p class="next">{_t(d.get("next"), 200)}</p>') if not _blank(d.get("next")) else ""
    body = [_header(d), intent, _outcome(d), *middle, _changes(d), _checks(d), _timeline(d), nxt, _evidence(d)]
    return f'<style>{CSS}</style><div class="dg">{"".join(p for p in body if p)}</div>'
