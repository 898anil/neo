"""Normalize Claude Code and Codex JSONL streams into one timeline event shape.

Every normalized event is a dict:
    kind   one of KINDS
    title  short label for the timeline row
    body   human-readable detail (may be empty)
    data   engine-specific fields worth keeping (ids, exit codes, cost)
"""

from __future__ import annotations

import json
import re
from typing import Any

KINDS = (
    "session",
    "text",
    "thinking",
    "tool_call",
    "tool_result",
    "plan",
    "usage",
    "result",
    "error",
    "debug",
)

MAX_BODY = 8000


def _clip(text: str, limit: int = MAX_BODY) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters]"


_SHELL_WRAPPER = re.compile(r"""^/bin/(?:zsh|bash|sh) -l?c (["'])(.*)\1$""", re.S)


def _unwrap_shell(command: str) -> str:
    """Codex runs `/bin/zsh -lc "<cmd>"`; show only <cmd>."""
    match = _SHELL_WRAPPER.match(command.strip())
    if not match:
        return command
    inner = match.group(2)
    return inner.replace('\\"', '"') if match.group(1) == '"' else inner


def _event(kind: str, title: str, body: str = "", **data: Any) -> dict:
    return {"kind": kind, "title": title, "body": _clip(body), "data": data}


def _summarize_tool_input(name: str, tool_input: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "prompt", "description"):
        if key in tool_input:
            return str(tool_input[key])
    return json.dumps(tool_input, indent=2)[:MAX_BODY]


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'block')}]")
        return "\n".join(parts)
    return json.dumps(content)


# --------------------------------------------------------------------- Claude


def normalize_claude(raw: dict) -> list[dict]:
    """Map one `claude -p --output-format stream-json --verbose` line to events."""
    etype = raw.get("type")
    subtype = raw.get("subtype")
    parent = raw.get("parent_tool_use_id")
    extra = {"parent_tool_use_id": parent} if parent else {}

    if etype == "system" and subtype == "init":
        return [
            _event(
                "session",
                "Claude session started",
                f"model {raw.get('model', '?')} in {raw.get('cwd', '?')}",
                session_id=raw.get("session_id"),
                model=raw.get("model"),
                cwd=raw.get("cwd"),
                permission_mode=raw.get("permissionMode"),
                tool_count=len(raw.get("tools") or []),
            )
        ]

    if etype == "assistant":
        events = []
        for block in (raw.get("message") or {}).get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                events.append(_event("text", "Claude", block["text"], **extra))
            elif btype == "thinking":
                thought = block.get("thinking", "")
                events.append(
                    _event("thinking", "Thinking", thought or "(thinking not shown by the API)", **extra)
                )
            elif btype == "tool_use":
                name = block.get("name", "tool")
                tool_input = block.get("input") or {}
                events.append(
                    _event(
                        "tool_call",
                        name,
                        _summarize_tool_input(name, tool_input),
                        tool_use_id=block.get("id"),
                        input=tool_input,
                        **extra,
                    )
                )
        return events

    if etype == "user":
        events = []
        content = (raw.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    is_error = bool(block.get("is_error"))
                    events.append(
                        _event(
                            "tool_result",
                            "Tool error" if is_error else "Tool output",
                            _tool_result_text(block.get("content")),
                            tool_use_id=block.get("tool_use_id"),
                            is_error=is_error,
                            **extra,
                        )
                    )
        return events

    if etype == "result":
        is_error = bool(raw.get("is_error"))
        return [
            _event(
                "error" if is_error else "result",
                "Run failed" if is_error else "Final result",
                str(raw.get("result") or ""),
                session_id=raw.get("session_id"),
                cost_usd=raw.get("total_cost_usd"),
                duration_ms=raw.get("duration_ms"),
                num_turns=raw.get("num_turns"),
                permission_denials=raw.get("permission_denials") or [],
            )
        ]

    label = f"{etype}/{subtype}" if subtype else str(etype)
    return [_event("debug", label, "", raw=raw)]


# ---------------------------------------------------------------------- Codex


def normalize_codex(raw: dict) -> list[dict]:
    """Map one `codex exec --json` line to events."""
    etype = raw.get("type", "")

    if etype == "thread.started":
        return [
            _event("session", "Codex session started", "", session_id=raw.get("thread_id"))
        ]

    if etype == "turn.completed":
        usage = raw.get("usage") or {}
        body = ", ".join(f"{k}={v}" for k, v in usage.items())
        return [_event("usage", "Turn usage", body, usage=usage)]

    if etype == "turn.failed":
        message = (raw.get("error") or {}).get("message", "")
        return [_event("error", "Turn failed", message)]

    if etype == "error":
        return [_event("error", "Error", raw.get("message", ""))]

    if etype.startswith("item."):
        return _normalize_codex_item(etype.split(".", 1)[1], raw.get("item") or {})

    return [_event("debug", etype or "unknown", "", raw=raw)]


def _normalize_codex_item(phase: str, item: dict) -> list[dict]:
    itype = item.get("type") or item.get("item_type") or ""
    item_id = item.get("id")

    if itype == "agent_message":
        if phase != "completed":
            return []
        return [_event("text", "Codex", item.get("text", ""), item_id=item_id)]

    if itype == "reasoning":
        if phase != "completed":
            return []
        return [_event("thinking", "Reasoning", item.get("text", ""), item_id=item_id)]

    if itype == "command_execution":
        if phase == "started":
            return [_event("tool_call", "shell", _unwrap_shell(item.get("command", "")), item_id=item_id)]
        if phase == "completed":
            exit_code = item.get("exit_code")
            failed = item.get("status") == "failed" or (exit_code not in (None, 0))
            return [
                _event(
                    "tool_result",
                    f"Command exited {exit_code}" if exit_code is not None else "Command output",
                    item.get("aggregated_output", ""),
                    item_id=item_id,
                    exit_code=exit_code,
                    is_error=failed,
                )
            ]
        return []

    if itype == "file_change":
        if phase != "completed":
            return []
        changes = item.get("changes") or []
        body = "\n".join(f"{c.get('kind', '?')}  {c.get('path', '?')}" for c in changes)
        return [_event("tool_result", "Files changed", body, item_id=item_id, changes=changes)]

    if itype == "mcp_tool_call":
        name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
        if phase == "started":
            args = item.get("arguments")
            title = args.get("title") if isinstance(args, dict) else None
            body = title or (json.dumps(args, indent=2) if args else "")
            return [_event("tool_call", name, body, item_id=item_id)]
        if phase == "completed":
            failed = item.get("status") == "failed"
            result = item.get("result") or item.get("error") or ""
            body = result if isinstance(result, str) else json.dumps(result, indent=2)
            return [_event("tool_result", f"{name} {'failed' if failed else 'done'}", body, item_id=item_id, is_error=failed)]
        return []

    if itype == "web_search":
        if phase != "started":
            return []
        return [_event("tool_call", "web_search", item.get("query", ""), item_id=item_id)]

    if itype == "todo_list":
        items = item.get("items") or []
        body = "\n".join(f"[{'x' if t.get('completed') else ' '}] {t.get('text', '')}" for t in items)
        return [_event("plan", "Plan", body, item_id=item_id)]

    if itype == "error":
        return [_event("error", "Error", item.get("message", ""), item_id=item_id)]

    return [_event("debug", f"item.{phase}/{itype or '?'}", "", raw=item)]


NORMALIZERS = {"claude": normalize_claude, "codex": normalize_codex}


def normalize(engine: str, raw: dict) -> list[dict]:
    return NORMALIZERS[engine](raw)


# ---------------------------------------------------------------- transcripts
#
# Interactive sessions (for example agents running inside herdr) do not stream
# JSON to us. Both engines write a session transcript to disk instead:
#   Claude: ~/.claude/projects/<cwd-slug>/<session_id>.jsonl
#   Codex:  ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl
# These normalizers turn transcript lines into the same timeline events and drop
# bookkeeping lines (attachments, mode changes, token counts).

_THINKING_PLACEHOLDER = "(thinking not shown by the API)"


def normalize_claude_transcript(raw: dict) -> list[dict]:
    etype = raw.get("type")
    if etype == "user":
        content = (raw.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content.strip()
            if not text or raw.get("isMeta") or raw.get("isCompactSummary") or text.startswith("<"):
                return []
            return [_event("prompt", "You", text)]
        return normalize_claude(raw)
    if etype == "assistant":
        return [
            e for e in normalize_claude(raw)
            if not (e["kind"] == "thinking" and e["body"] == _THINKING_PLACEHOLDER)
        ]
    return []


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _shell_command(command) -> str:
    # Codex records ["/bin/zsh", "-lc", "<command>"]; show only the command.
    if isinstance(command, list):
        if len(command) >= 3 and command[1] in ("-lc", "-c"):
            return str(command[-1])
        return " ".join(str(c) for c in command)
    return str(command or "")


def normalize_codex_transcript(raw: dict) -> list[dict]:
    etype = raw.get("type")
    payload = raw.get("payload") or {}
    if etype == "session_meta":
        return [_event("session", "Codex session", payload.get("cwd", ""), session_id=payload.get("id"))]
    if etype != "event_msg":
        return []

    ptype = payload.get("type")
    if ptype == "task_complete":
        return [_event("result", "Turn finished", payload.get("last_agent_message") or "")]
    if ptype == "turn_aborted":
        return [_event("error", "Turn interrupted", payload.get("reason", ""))]
    if ptype == "error":
        return [_event("error", "Error", str(payload.get("message", "")))]
    if ptype != "item_completed":
        return []

    item = payload.get("item") or {}
    itype = item.get("type")
    if itype == "UserMessage":
        text = _content_text(item.get("content")).strip()
        if not text or text.startswith("<"):
            return []
        return [_event("prompt", "You", text)]
    if itype == "AgentMessage":
        text = _content_text(item.get("content")).strip()
        return [_event("text", "Codex", text)] if text else []
    if itype == "Reasoning":
        text = "\n".join(str(s) for s in item.get("summary_text") or []).strip()
        return [_event("thinking", "Reasoning", text)] if text else []
    if itype == "CommandExecution":
        exit_code = item.get("exit_code")
        failed = item.get("status") == "failed" or exit_code not in (None, 0)
        return [
            _event("tool_call", "shell", _shell_command(item.get("command"))),
            _event(
                "tool_result",
                f"Command exited {exit_code}" if exit_code is not None else "Command output",
                item.get("aggregated_output") or item.get("stdout") or "",
                exit_code=exit_code,
                is_error=failed,
            ),
        ]
    if itype == "FileChange":
        changes = item.get("changes") or {}
        if isinstance(changes, dict):
            body = "\n".join(f"{(v or {}).get('type', '?')}  {k}" for k, v in changes.items())
        else:
            body = json.dumps(changes)
        return [_event("tool_result", "Files changed", body, is_error=item.get("status") == "failed")]
    if itype == "McpToolCall":
        name = f"{item.get('server', 'mcp')}.{item.get('tool', 'tool')}"
        args = item.get("arguments") or {}
        title = args.get("title") if isinstance(args, dict) else None
        failed = item.get("status") == "failed"
        result = item.get("result") or item.get("error") or ""
        body = _content_text(result.get("content")) if isinstance(result, dict) else str(result)
        return [
            _event("tool_call", name, title or json.dumps(args)[:MAX_BODY]),
            _event("tool_result", f"{name} {'failed' if failed else 'done'}", body, is_error=failed),
        ]
    if itype == "Extension" and item.get("kind") == "web.search":
        return [_event("tool_call", "web_search", item.get("query", ""))]
    return []


TRANSCRIPT_NORMALIZERS = {"claude": normalize_claude_transcript, "codex": normalize_codex_transcript}
