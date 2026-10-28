"""Per-run latency breakdown: where a neo run's wall time actually goes.

Reads a run's ``events.jsonl`` (every event carries a ``ts``) and attributes wall
time to three buckets so "why was that slow" has a concrete answer:

- model: generating/thinking (gaps when no tool is in flight) -- the LLM itself
- tools: wall-clock spent with at least one tool in flight (overlap counted once)
- per-tool: each tool_use_id's call->result duration, aggregated by tool name

Tool calls can overlap (the model emits several before their results), so tool
wall time is the union of in-flight intervals, not the sum of durations -- summing
would double-count parallel calls and exceed the wall clock.

Usage:
    python -m cos.timing <run_dir|run_id> [--json]
    python -m cos.timing --recent N        # summarize the N most recent runs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

RUNS = Path(__file__).resolve().parent.parent / "runs"


def _ts(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    """Total wall time covered by any interval, overlaps counted once."""
    if not intervals:
        return 0.0
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def summarize(run_dir: Path) -> dict[str, Any]:
    events = _events(run_dir)
    stamped = [e for e in events if e.get("ts")]
    if not stamped:
        return {"run": run_dir.name, "error": "no timestamped events"}

    times = [_ts(e["ts"]) for e in stamped]
    wall = max(times) - min(times)

    # Match tool_call -> tool_result by tool_use_id; keep each call's name + window.
    calls: dict[str, dict[str, Any]] = {}
    for e in stamped:
        tid = (e.get("data") or {}).get("tool_use_id")
        if not tid:
            continue
        if e.get("kind") == "tool_call":
            calls.setdefault(tid, {})["start"] = _ts(e["ts"])
            calls[tid]["name"] = e.get("title") or "?"
        elif e.get("kind") == "tool_result":
            calls.setdefault(tid, {})["end"] = _ts(e["ts"])
            calls[tid]["error"] = bool((e.get("data") or {}).get("is_error"))

    intervals = [(c["start"], c["end"]) for c in calls.values() if "start" in c and "end" in c]
    tool_wall = _union_seconds(intervals)
    model_wall = max(0.0, wall - tool_wall)

    per_tool: dict[str, dict[str, Any]] = {}
    for c in calls.values():
        if "start" not in c or "end" not in c:
            continue
        name = c["name"]
        slot = per_tool.setdefault(name, {"calls": 0, "seconds": 0.0, "errors": 0})
        slot["calls"] += 1
        slot["seconds"] += c["end"] - c["start"]
        slot["errors"] += 1 if c.get("error") else 0

    top = sorted(per_tool.items(), key=lambda kv: kv[1]["seconds"], reverse=True)
    turns = max((e.get("turn", 0) for e in stamped), default=0)
    return {
        "run": run_dir.name,
        "wall_s": round(wall, 1),
        "model_s": round(model_wall, 1),
        "tools_s": round(tool_wall, 1),
        "model_pct": round(100 * model_wall / wall) if wall else 0,
        "tools_pct": round(100 * tool_wall / wall) if wall else 0,
        "turns": turns,
        "tool_calls": sum(s["calls"] for s in per_tool.values()),
        "slowest_tools": [
            {"name": n, "calls": s["calls"], "seconds": round(s["seconds"], 1), "errors": s["errors"]}
            for n, s in top[:8]
        ],
    }


def _render(s: dict[str, Any]) -> str:
    if s.get("error"):
        return f"{s['run']}: {s['error']}"
    lines = [
        f"{s['run']}  wall={s['wall_s']}s  model={s['model_s']}s ({s['model_pct']}%)  "
        f"tools={s['tools_s']}s ({s['tools_pct']}%)  turns={s['turns']}  tool_calls={s['tool_calls']}",
        "  slowest tools (total call time):",
    ]
    for t in s["slowest_tools"]:
        err = f"  {t['errors']} err" if t["errors"] else ""
        lines.append(f"    {t['seconds']:>6.1f}s  x{t['calls']:<3} {t['name']}{err}")
    return "\n".join(lines)


def _resolve(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        return p
    return RUNS / arg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", nargs="?", help="run dir or run id")
    ap.add_argument("--recent", type=int, metavar="N", help="summarize the N most recent runs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.recent:
        dirs = sorted(RUNS.glob("*/"), key=lambda d: d.stat().st_mtime, reverse=True)[: args.recent]
    elif args.run:
        dirs = [_resolve(args.run)]
    else:
        ap.error("pass a run id/dir or --recent N")
        return 2

    summaries = [summarize(d) for d in dirs]
    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        print("\n".join(_render(s) for s in summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
