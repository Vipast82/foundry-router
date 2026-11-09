"""Find-on-demand MCP tools (v0.181.109).

A persona with `mcp_tool_mode = "finder"` is offered two Foundry-owned tools instead of every
MCP tool it is granted:

  foundry_find_tools(query, limit?)   search the persona's granted tools; returns each match's
                                      name, server, description and argument schema
  foundry_call_tool(name, arguments)  run one of the persona's granted tools by that name

The persona's grants (preferred_mcp_tools) stay the gate: search only sees granted tools and a
call to anything else is refused, so a persona can be given every server ("*") and still have
some excluded ("!server" / "!tool"). The offered list is the same two tools on every turn, so it
never rewrites the prompt prefix (the llama.cpp/Strata cache-bust finding), and the context cost
is about 400 tokens instead of 13k+ for Foundry-Chat's grants.

Used by every path that offers persona tools: the brain's own loop, the worker tool loop,
direct mode, and the aggregator's /mcp/persona/<name> endpoint.
"""

from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, Optional

FINDER_SERVER = "foundry-finder"          # pseudo MCP server, never synced
FIND_TOOL = "foundry_find_tools"
CALL_TOOL = "foundry_call_tool"
MODES = ("full", "finder")
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
_DESC_CHARS = 400

FIND_PARAMS = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "What you need to do, in a few words (e.g. 'web search', "
                                 "'generate an image', 'read a GitHub issue'). Empty lists "
                                 "the servers you can use."},
        "limit": {"type": "integer",
                  "description": f"How many matches to return (1-{MAX_LIMIT}, default "
                                 f"{DEFAULT_LIMIT})."}},
    "required": ["query"]}

CALL_PARAMS = {
    "type": "object",
    "properties": {
        "name": {"type": "string",
                 "description": "The tool's exact name as foundry_find_tools returned it."},
        "arguments": {"type": "object",
                      "description": "The tool's arguments, matching the schema "
                                     "foundry_find_tools returned for it."}},
    "required": ["name", "arguments"]}

FIND_DESC = ("Search the tools you can use (web search, page fetch, image / audio generation, "
             "code, files, databases and more) and get their argument schemas. Call this "
             "first whenever a task needs a tool you have not been given directly, then run "
             "the tool with foundry_call_tool.")
CALL_DESC = ("Run a tool that foundry_find_tools returned, by its exact name, with arguments "
             "that match its schema.")


def mode(persona: Optional[dict]) -> str:
    raw = str((persona or {}).get("mcp_tool_mode") or "").strip().lower()
    return raw if raw in MODES else "full"


def is_finder(persona: Optional[dict]) -> bool:
    return mode(persona) == "finder"


def tooldefs():
    """The two finder ToolDefs, in a fixed order (prompt-cache stable)."""
    from .tools.sync import ToolDef
    return [ToolDef(name=FIND_TOOL, kind="mcp", description=FIND_DESC, parameters=FIND_PARAMS,
                    server=FINDER_SERVER, mcp_tool=FIND_TOOL, read_only=True, destructive=False),
            ToolDef(name=CALL_TOOL, kind="mcp", description=CALL_DESC, parameters=CALL_PARAMS,
                    server=FINDER_SERVER, mcp_tool=CALL_TOOL)]


def tooldef(name: str):
    return next((t for t in tooldefs() if t.name == name), None)


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "with", "i", "me", "my",
         "need", "want", "use", "tool", "tools", "do", "some", "it", "is", "be", "can", "get"}


def _words(text: str) -> list[str]:
    out = []
    for w in _WORD.findall((text or "").lower().replace("_", " ").replace("-", " ")):
        if w in _STOP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)   # crude plural fold
    return out


def search(tools: list, query: str, limit: int = DEFAULT_LIMIT) -> list:
    """The best `limit` of `tools` for `query`: words in the tool name count 3, the server
    name 2, the description 1; ties keep the registry order. No words: []."""
    q = _words(query)
    if not q:
        return []
    scored = []
    for i, t in enumerate(tools):
        name = set(_words(t.mcp_tool or t.name) + _words(t.name))
        server = set(_words(t.server or ""))
        desc = _words(t.description or "")
        dset = set(desc)
        s = 0.0
        for w in q:
            s += 3 * (w in name) + 2 * (w in server) + (w in dset)
            s += 0.5 * any(w in n and w != n for n in name)       # partial name hit
        if s > 0:
            scored.append((-s, i, t))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [t for _, _, t in scored[:limit]]


def _entry(t) -> dict:
    desc = (t.description or "").strip()
    if len(desc) > _DESC_CHARS:
        desc = desc[:_DESC_CHARS].rstrip() + "…"
    return {"name": t.name, "server": t.server, "description": desc,
            "arguments": t.parameters or {"type": "object", "properties": {}}}


def find_text(tools: list, args: Optional[dict]) -> str:
    args = args if isinstance(args, dict) else {}
    try:
        limit = max(1, min(MAX_LIMIT, int(args.get("limit") or DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    query = str(args.get("query") or "")
    if not tools:
        return "No tools are available to you."
    hits = search(tools, query, limit)
    if not hits:
        servers: dict = {}
        for t in tools:
            servers.setdefault(t.server or "?", []).append(t.mcp_tool or t.name)
        lines = [f"- {s} ({len(n)}): {', '.join(n[:6])}{', …' if len(n) > 6 else ''}"
                 for s, n in sorted(servers.items())]
        head = (f"No tool matched {query!r}. " if query.strip() else "") + \
            f"You can use {len(tools)} tools on these servers; search again with other words:"
        return head + "\n" + "\n".join(lines)
    return json.dumps({"tools": [_entry(t) for t in hits],
                       "next": "Run one with foundry_call_tool(name, arguments)."},
                      ensure_ascii=False)


async def run(tools: list, tool: str, args: Optional[dict],
              call: Callable[[object, dict], Awaitable]):
    """A finder call. `tools` is the persona's granted set (the gate); `call(td, arguments)`
    runs a real tool the caller's usual way (media, jobs, logging) and returns a ToolResult."""
    from .tools.mcp_client import ToolResult
    args = args if isinstance(args, dict) else {}
    if tool == FIND_TOOL:
        return ToolResult([{"type": "text", "text": find_text(tools, args)}])
    if tool != CALL_TOOL:
        return ToolResult([{"type": "text", "text": f"ERROR: unknown finder tool {tool!r}."}])
    name = str(args.get("name") or "").strip()
    inner = args.get("arguments")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner) if inner.strip() else {}
        except ValueError:
            return ToolResult([{"type": "text", "text": "ERROR: arguments must be a JSON "
                                                        "object matching the tool's schema."}])
    if inner is None:
        inner = {}
    if not isinstance(inner, dict):
        return ToolResult([{"type": "text", "text": "ERROR: arguments must be a JSON object."}])
    by_name = {}
    for t in tools:
        by_name.setdefault(t.name, t)
        if t.mcp_tool:
            by_name.setdefault(t.mcp_tool, t)
    td = by_name.get(name)
    if td is None or td.server == FINDER_SERVER:
        return ToolResult([{"type": "text", "text": (
            f"ERROR: {name!r} is not a tool you can use. Call foundry_find_tools to find "
            "the right one and use its exact name.")}])
    return await call(td, inner)
