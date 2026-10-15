"""Cline sessions -> normalized events, read-only.

Cline keeps each session as <sessions>/<id>/<id>.json (metadata, incl. cwd)
and <id>.messages.json (Anthropic-style messages: text / tool_use /
tool_result blocks), the same files AnimalUprising's checkpoint.py and
Foundry's replay harness read. The project comes from the configured
workspace that contains the session's cwd; no match -> skipped.

Not in these files: the system prompt Cline sent, tool definitions (Cline
describes tools in its system prompt), and image bytes beyond what a block
embeds. Recorded as gaps.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Iterator

from ..schema import Event

ADAPTER = "cline"


def sessions(sessions_dir: str, projects: list) -> Iterator[tuple[str, str, dict, dict]]:
    from ..config import by_workspace
    for d in sorted(glob.glob(os.path.join(sessions_dir, "*"))):
        sid = os.path.basename(d)
        meta_p = os.path.join(d, sid + ".json")
        msgs_p = os.path.join(d, sid + ".messages.json")
        if not (os.path.isfile(meta_p) and os.path.isfile(msgs_p)):
            continue
        with open(meta_p, encoding="utf-8") as f:
            meta = json.load(f)
        with open(msgs_p, encoding="utf-8") as f:
            raw = json.load(f)
        msgs = raw.get("messages", raw) if isinstance(raw, dict) else raw
        p = by_workspace(projects, meta.get("cwd") or meta.get("workspace_root") or "")
        yield (sid, p.id if p else "", {"meta": meta, "messages": msgs, "session_id": sid},
               {"adapter": ADAPTER, "project_by": "configured workspace path" if p else "none",
                "sanitization": "none"})


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    sid = p["session_id"]
    common = dict(project_id=project_id, session_id=sid, raw_ref=raw_ref,
                  redaction_status=redaction.get("status", "clean"))
    ev = [Event(ADAPTER, sid, "session_start", timestamp=0, part="session", seq=0,
                facts={"meta": {k: v for k, v in (p.get("meta") or {}).items()
                                if k in ("status", "cwd", "model", "task", "mode")},
                       "missing": ["system_prompt", "tool_definitions"]}, **common)]
    seq = 0
    for i, m in enumerate(p.get("messages") or []):
        ts = m.get("ts")
        ts = (ts / 1000.0 if isinstance(ts, (int, float)) and ts > 1e11 else ts) or 0
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for j, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            seq += 1
            base = dict(timestamp=ts, part=f"{i}:{j}", seq=seq, **common)
            t = b.get("type")
            if t == "text":
                ev.append(Event(ADAPTER, sid, "user_message" if m.get("role") == "user"
                                else "assistant_message",
                                actor_role=m.get("role"), facts={"text": b.get("text") or ""},
                                **base))
            elif t == "thinking":
                ev.append(Event(ADAPTER, sid, "reasoning", actor_role="assistant",
                                facts={"text": b.get("thinking") or ""}, **base))
            elif t == "tool_use":
                ev.append(Event(ADAPTER, sid, "tool_call", actor_role="assistant",
                                tool_call_id=b.get("id") or f"{i}:{j}",
                                facts={"name": b.get("name"), "arguments": b.get("input") or {}},
                                **base))
            elif t == "tool_result":
                body = b.get("content")
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                ev.append(Event(ADAPTER, sid, "tool_result", actor_role="tool",
                                tool_call_id=b.get("tool_use_id") or f"{i}:{j}",
                                facts={"output": text, "status": "error" if b.get("is_error")
                                       else "completed", "truncated": False}, **base))
            elif t == "image":
                ev.append(Event(ADAPTER, sid, "file_ref", actor_role=m.get("role"),
                                capture_status="missing",
                                facts={"artifact_status": "not-extracted",
                                       "note": "embedded image block not stored by this adapter"},
                                **base))
    return ev
