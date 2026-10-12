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
.dg{gap:16px}
.dg .intent{font:400 15px/1.4 var(--serif),Georgia,serif;color:var(--muted)}
.dg .fig{margin:4px 0 0;padding:16px 14px 18px;border-radius:14px;background:var(--wash)}
.dg .fig figcaption{font:600 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-bottom:14px}
.dg .flow{display:flex;flex-wrap:wrap;align-items:stretch;row-gap:14px}
.dg .nd{flex:1 1 84px;min-width:84px;max-width:180px;position:relative;display:flex;align-items:center;justify-content:center;text-align:center;
  padding:12px 8px;border-radius:10px;font-size:12.5px;line-height:1.25;font-weight:600;color:var(--text);
  background:color-mix(in srgb,var(--c) 10%,var(--panel));box-shadow:inset 0 0 0 1.5px color-mix(in srgb,var(--c) 70%,transparent)}
.dg .nd.todo{color:var(--faint);font-weight:500;background:var(--panel);box-shadow:inset 0 0 0 1px var(--line)}
.dg .nd.broken{background:color-mix(in srgb,var(--coral) 16%,var(--panel))}
.dg .nd i{position:absolute;top:-8px;right:-6px;width:18px;height:18px;border-radius:50%;display:grid;place-items:center;
  font:700 11px/1 var(--mono);font-style:normal;background:var(--c);color:var(--panel)}
.dg .nd.doing i{animation:p 1.6s ease-in-out infinite}
.dg .ar{flex:0 0 18px;align-self:center;height:2px;background:var(--faint);position:relative;opacity:.7}
.dg .ar::after{content:"";position:absolute;right:-1px;top:-4px;border:5px solid transparent;border-left:6px solid var(--faint);border-right:0}
.dg .out{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:0;background:none}
.dg .out .h{flex:1 1 220px;font-size:15px;font-weight:600;line-height:1.35}
.dg .meter{cursor:help}
.dg .prog{display:flex;align-items:center;gap:10px;margin:0;font:600 12px var(--mono);color:var(--muted)}
.dg .prog .seg{flex:1;display:flex;gap:3px}
.dg .prog .seg i{flex:1;height:8px;border-radius:2px;opacity:.9}
.dg .prog .seg i.todo,.dg .prog .seg i.skipped{background:var(--line)!important}
.dg .now{display:flex;gap:8px;align-items:baseline;font-size:13.5px;color:var(--text);margin-top:-6px}
.dg .now b{font:600 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--azure);flex:none}
.dg details>summary{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dg details>summary::before{content:"▸";font-size:10px;color:var(--faint)}
.dg details[open]>summary::before{content:"▾"}
.dg details.chg>summary .tag{font-size:10px;padding:2px 6px;border-radius:4px;background:color-mix(in srgb,var(--c) 12%,transparent)}
.dg details.chg>summary .n{font:11px var(--mono);color:var(--faint);letter-spacing:0;text-transform:none}
.dg details ol.steps{margin-top:6px}
.dg .tl .ev span{font-size:11.5px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.dg .next{font-size:13.5px}
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
    shown_note = not _blank(d.get("state_note")) and _blank(d.get("needs_you"))
    note = f'<span class="note">{_t(d.get("state_note"), 80)}</span>' if shown_note else ""
    # The kind (Fix, RCA…) is already in the frame's top line on the trace page.
    head = f'<div class="top"><span class="pill{live}" style="{_c(colour)}"><i></i>{label}</span>{note}</div>'
    if not _blank(d.get("intent")):
        head += f'<p class="intent">{_t(d.get("intent"), 220)}</p>'
    if not _blank(d.get("needs_you")):
        head += f'<div class="need"><b>Needs you</b><span>{_t(d.get("needs_you"), 200)}</span></div>'
    return head


def _steps(d: dict) -> list[tuple[str, str]]:
    """(text, status) for each plan step, unknown statuses read as still to do."""
    return [(str(s.get("step") or ""), _k(s.get("status")) if _k(s.get("status")) in STEP else "todo") for s in _rows(d.get("plan"), 8)]


def _has_chain(d: dict) -> bool:
    chain = d.get("chain") if isinstance(d.get("chain"), dict) else {}
    return len([n for n in _rows(chain.get("nodes"), 6) if not _blank(n.get("label"))]) >= 2


def _diagram(d: dict) -> str:
    """The picture at the top: the cause chain or flow when the model drew one, otherwise the plan as a flow."""
    chain = d.get("chain") if isinstance(d.get("chain"), dict) else {}
    nodes = [n for n in _rows(chain.get("nodes"), 6) if not _blank(n.get("label"))]
    if len(nodes) >= 2:
        title = _t(chain.get("title"), 60) or "How it works"
        boxes = [(n.get("label"), _k(n.get("tone")) if _k(n.get("tone")) in TONE else "neutral", n.get("detail"), n.get("via")) for n in nodes]
    else:
        steps = _steps(d)
        if len(steps) < 2:
            return ""
        title = "Plan"
        tone_of = {"done": "ok", "doing": "doing", "failed": "broken", "todo": "todo", "skipped": "todo"}
        boxes = [(text, tone_of[status], "", "") for text, status in steps]
    parts = []
    for i, (label, tone, detail, via) in enumerate(boxes):
        colour = {"doing": "azure", "todo": "faint"}.get(tone, TONE.get(tone, "muted"))
        tip = f' title="{escape(str(detail).strip()[:160], quote=True)}"' if not _blank(detail) else ""
        mark = {"broken": "!", "new": "+", "changed": "~", "ok": "✓", "doing": "●"}.get(tone, "")
        badge = f'<i>{mark}</i>' if mark else ""
        parts.append(f'<div class="nd {tone}" style="{_c(colour)}"{tip}>{badge}<span>{_t(label, 60)}</span></div>')
        if i < len(boxes) - 1:
            vtip = f' title="{escape(str(via).strip()[:80], quote=True)}"' if not _blank(via) else ""
            parts.append(f'<span class="ar"{vtip}></span>')
    return f'<figure class="fig"><figcaption>{title}</figcaption><div class="flow">{"".join(parts)}</div></figure>'


def _outcome(d: dict) -> str:
    out = d.get("outcome") if isinstance(d.get("outcome"), dict) else {}
    label, colour = RESULT.get(_k(out.get("result")), RESULT["unclear"])
    n = CONF.get(_k(d.get("confidence")), 0)
    meter = ""
    if n:
        conf_colour = {3: "mint", 2: "amber", 1: "coral"}[n]
        bars = "".join(f'<i class="{"on" if i < n else ""}"></i>' for i in range(3))
        why = f' — {str(d.get("confidence_why")).strip()[:120]}' if not _blank(d.get("confidence_why")) else ""
        meter = f'<span class="meter" style="{_c(conf_colour)}" title="{escape(d["confidence"] + " confidence" + why, quote=True)}">{bars}</span>'
    headline = _t(out.get("headline"), 160)
    if not headline:
        return ""
    return f'<div class="out"><span class="pill" style="{_c(colour)}"><i></i>{label}</span><p class="h">{headline}</p>{meter}</div>'


def _progress(d: dict) -> str:
    """Plan progress as one segmented bar, the step in hand, and the full checklist folded away."""
    steps = _steps(d)
    if not steps:
        return ""
    done = sum(1 for _, s in steps if s == "done")
    segs = "".join(f'<i style="background:var(--{STEP[s]})" class="{s}"></i>' for _, s in steps)
    now = next((text for text, s in steps if s in ("doing", "failed")), None) or next((text for text, s in steps if s == "todo"), None)
    now_line = f'<p class="now"><b>{"Now" if any(s == "doing" for _, s in steps) else "Next"}</b>{_t(now, 120)}</p>' if now else ""
    items = []
    for i, s in enumerate(_rows(d.get("plan"), 8), 1):
        status = _k(s.get("status")) if _k(s.get("status")) in STEP else "todo"
        mark = {"done": ("fill", "✓"), "failed": ("fill", "✕"), "doing": ("half", ""), "skipped": ("", "–")}.get(status, ("", str(i)))
        cls = {"todo": " dim", "skipped": " skip"}.get(status, "")
        note = f'<small>{_t(s.get("note"), 100)}</small>' if not _blank(s.get("note")) else ""
        items.append(f'<li><span class="ic {mark[0]}" style="{_c(STEP[status])}">{mark[1]}</span>'
                     f'<span class="st{cls}">{_t(s.get("step"), 120)}{note}</span></li>')
    return (f'<div class="prog"><div class="seg">{segs}</div><span>{done}/{len(steps)}</span></div>{now_line}'
            f'<details><summary>Plan · {len(steps)} steps</summary><ol class="steps">{"".join(items)}</ol></details>')


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
    kinds = {"new": 0, "edited": 0, "deleted": 0}
    added = removed = 0
    rows = []
    for f in files:
        path = str(f.get("path") or "").strip()[:160]
        head, _, base = path.rpartition("/")
        # The file name first and whole; the folder after it, trimmed from the left when long.
        shown = f"<b>{escape(base or path)}</b>" + (f"<span>&lrm;{escape(head)}/&lrm;</span>" if head else "")
        add, rem = _num(f.get("added")), _num(f.get("removed"))
        added, removed = added + add, removed + rem
        kind = _k(f.get("kind")) if _k(f.get("kind")) in kinds else "edited"
        kinds[kind] += 1
        if add or rem:
            bar = (f'<span class="b"><i style="width:{add / widest * 100:.1f}%;background:var(--mint)"></i>'
                   f'<i style="width:{rem / widest * 100:.1f}%;background:var(--coral)"></i></span>')
            nums = f'<span class="n">+{add} −{rem}</span>'
        else:
            bar = ""
            nums = f'<span class="n"><span class="tag" style="{_c({"new": "mint", "deleted": "coral"}.get(kind, "amber"))}">{kind}</span></span>'
        rows.append(f'<div class="file"><span class="p" title="{escape(path, quote=True)}">{shown}</span><span class="r">{bar}{nums}</span></div>')
    tags = "".join(f'<span class="tag" style="{_c(c)}">{n} {k}</span>' for k, c in (("new", "mint"), ("edited", "amber"), ("deleted", "coral"))
                   if (n := kinds[k]))
    lines = f'<span class="n">+{added} −{removed}</span>' if added or removed else ""
    return (f'<details class="chg"><summary>{len(files)} file{"s" if len(files) != 1 else ""} {tags}{lines}</summary>'
            f'<div class="files">{"".join(rows)}</div></details>')


def _checks(d: dict) -> str:
    pills = []
    for x in _rows(d.get("checks"), 8):
        colour, mark = CHECK.get(_k(x.get("status")), CHECK["none"])
        url = _url(x.get("url"))
        inner = f'{mark} {_t(x.get("label"), 40)}'
        pills.append(f'<a class="chk" style="{_c(colour)}" href="{url}" target="_blank" rel="noopener">{inner}</a>' if url
                     else f'<span class="chk" style="{_c(colour)}">{inner}</span>')
    return f'<div class="checks">{"".join(pills)}</div>' if pills else ""


def _timeline(d: dict) -> str:
    events = [e for e in _rows(d.get("timeline"), 6) if not _blank(e.get("when"))]
    if len(events) < 2:
        return ""
    cells = "".join(f'<div class="ev" style="{_c(TONE.get(_k(e.get("tone")), "muted"))}" title="{escape(str(e.get("event") or "")[:120], quote=True)}">'
                    f'<i></i><b>{_t(e.get("when"), 16)}</b><span>{_t(e.get("event"), 60)}</span></div>' for e in events)
    return f'<div class="tl">{cells}</div>'


def _evidence(d: dict) -> str:
    items = []
    for x in _rows(d.get("evidence"), 6):
        url, label = _url(x.get("url")), _t(x.get("label"), 120)
        if label:
            items.append(f'<li><a href="{url}" target="_blank" rel="noopener">{label}</a></li>' if url else f"<li>{label}</li>")
    return f'<details><summary>Evidence · {len(items)}</summary><ul>{"".join(items)}</ul></details>' if items else ""


def render(d: dict) -> str:
    """The digest page body for structured digest fields: the picture first, words only where they carry
    the point (the outcome, the step in hand, what is next); the rest is folded away."""
    nxt = f'<p class="next">{_t(d.get("next"), 200)}</p>' if not _blank(d.get("next")) else ""
    # When the plan itself is the picture, the progress bar under it would only repeat it.
    progress = _progress(d) if _has_chain(d) else ""
    body = [_header(d), _diagram(d), _outcome(d), _numbers(d), progress, _checks(d), _timeline(d), _changes(d), nxt, _evidence(d)]
    return f'<style>{CSS}</style><div class="dg">{"".join(p for p in body if p)}</div>'
