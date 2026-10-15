"""Message normalization shared by episodes, export and validation.

Neutral form (model-agnostic):
  {"role": system|user|assistant|tool, "content": str,
   "tool_calls": [{"id", "name", "arguments": dict}],     # assistant
   "tool_call_id": str,                                    # tool
   "author": {"kind": local|frontier|human|coordinator|tool|client|unknown,
              "model": str|None},
   "images": [{"sha256"|"status", ...}]}

Accepts OpenAI chat messages, Ollama/Foundry canonical messages and
Foundry ChatResult tool calls. Reasoning is never copied into content.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def _args(a: Any) -> dict:
    if isinstance(a, dict):
        return a
    if isinstance(a, str):
        try:
            v = json.loads(a)
            return v if isinstance(v, dict) else {"_raw": a}
        except ValueError:
            return {"_raw": a}
    return {}


def tool_calls(raw) -> list[dict]:
    out = []
    for i, tc in enumerate(raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        out.append({"id": str(tc.get("id") or fn.get("id") or f"call_{i}"),
                    "name": tc.get("name") or fn.get("name") or "",
                    "arguments": _args(tc.get("arguments") if "arguments" in tc
                                       else fn.get("arguments"))})
    return out


def text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
    return "" if content is None else str(content)


def images(m: dict) -> list:
    out = []
    for img in m.get("images") or []:
        out.append({"status": "inline-base64", "chars": len(img) if isinstance(img, str) else 0})
    c = m.get("content")
    if isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"):
                url = (p.get("image_url") or {}).get("url") if isinstance(p.get("image_url"),
                                                                           dict) else p.get("url")
                out.append({"status": "inline-data-url" if str(url or "").startswith("data:")
                            else "reference", "ref": None if str(url or "").startswith("data:")
                            else url})
    return out


def neutral(m: dict, author: Optional[dict] = None) -> dict:
    role = m.get("role") or "user"
    if role == "developer":
        role = "system"
    out = {"role": role, "content": text(m.get("content"))}
    tcs = tool_calls(m.get("tool_calls"))
    if tcs:
        out["tool_calls"] = tcs
    if role == "tool":
        out["tool_call_id"] = str(m.get("tool_call_id") or "")
    imgs = images(m)
    if imgs:
        out["images"] = imgs
    out["author"] = author or {"kind": {"tool": "tool", "system": "client", "user": "client"}
                               .get(role, "unknown"), "model": None}
    return out


def neutral_tools(tools) -> list[dict]:
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({"name": fn["name"], "description": fn.get("description") or "",
                    "parameters": fn.get("parameters") or fn.get("input_schema") or
                    {"type": "object", "properties": {}}})
    return out


def pairing(messages: list[dict]) -> dict:
    """Tool-call/result structure: every call answered exactly once, results
    only for calls made earlier, no dangling calls before the next assistant
    turn."""
    open_ids: dict = {}
    problems = []
    for i, m in enumerate(messages):
        if m["role"] == "assistant":
            if open_ids:
                problems.append(f"turn {i}: assistant spoke while {sorted(open_ids)} had no result")
            for tc in m.get("tool_calls") or []:
                if tc["id"] in open_ids:
                    problems.append(f"turn {i}: duplicate tool call id {tc['id']}")
                open_ids[tc["id"]] = i
        elif m["role"] == "tool":
            tid = m.get("tool_call_id")
            if tid not in open_ids:
                problems.append(f"turn {i}: result for unknown tool call {tid!r}")
            else:
                open_ids.pop(tid)
    if open_ids:
        problems.append(f"calls without results at the end: {sorted(open_ids)}")
    return {"ok": not problems, "problems": problems}


def canonical_text(messages: list[dict]) -> str:
    """Content-only rendering used for hashing and near-duplicate detection
    (ids, authors and timestamps left out)."""
    parts = []
    for m in messages:
        parts.append(m["role"] + ": " + (m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            parts.append("call " + tc["name"] + " " + json.dumps(tc["arguments"], sort_keys=True))
    return "\n".join(parts)
