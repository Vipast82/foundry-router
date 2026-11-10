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
# v0.181.111: a search result keeps its schemas whole (the brain's MCP preview is 2k chars),
# and turns that only search do not use up a loop's step cap (at most this many per request)
RESULT_CHARS = 12000
FREE_FINDS = 6
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
    query = str(_first(args, QUERY_KEYS) or "")
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


# v0.181.110: smaller local models get the shape of a finder call wrong in a few common ways;
# each is accepted instead of costing a turn on an error
QUERY_KEYS = ("query", "q", "search", "text", "keywords", "task")
NAME_KEYS = ("name", "tool", "tool_name", "toolName", "function")
ARG_KEYS = ("arguments", "args", "parameters", "params", "input", "tool_input", "kwargs")
_SEP = re.compile(r"[\s./:\\-]+|_{2,}")


def _first(d: dict, keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _as_object(v):
    """A dict for a tool's arguments: a dict as is, a JSON object string parsed (also one
    wrapped in a code fence), None/"" as {}; anything else None (an error)."""
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        t = v.strip()
        if t.startswith("```"):
            t = t.strip("`")
            t = t[4:] if t.lower().startswith("json") else t
            t = t.strip()
        if not t:
            return {}
        try:
            v = json.loads(t)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None
    return None


def _norm(name: str) -> str:
    return _SEP.sub("_", str(name or "").strip().strip("`'\"").lower())


def _resolve(tools: list, name: str):
    """The granted tool `name` means: exact registry or bare name, then the same ignoring
    case and separators ("searxng/web_search", "Searxng.web_search", "searxng__web_search"),
    then a server-qualified bare name. None when it matches no granted tool or several."""
    exact = {}
    for t in tools:
        exact.setdefault(t.name, t)
        if t.mcp_tool:
            exact.setdefault(t.mcp_tool, t)
    if name in exact:
        return exact[name]
    want = _norm(name)
    hits = {id(t): t for t in tools
            if want in (_norm(t.name), _norm(t.mcp_tool or ""),
                        _norm(f"{t.server}_{t.mcp_tool or t.name}"))}
    if len(hits) == 1:
        return next(iter(hits.values()))
    return None


async def run(tools: list, tool: str, args: Optional[dict],
              call: Callable[[object, dict], Awaitable]):
    """A finder call. `tools` is the persona's granted set (the gate); `call(td, arguments)`
    runs a real tool the caller's usual way (media, jobs, logging) and returns a ToolResult.

    Loose shapes are accepted: `tool` / `tool_name` for `name`, `args` / `parameters` /
    `input` for `arguments`, arguments as a JSON string (fenced or not), the tool's own
    arguments placed next to `name` instead of inside `arguments`, and a name that differs
    only in case or separators. A name that is still unknown gets the closest granted tools
    back, with their schemas, so the next call can be right."""
    from .tools.mcp_client import ToolResult

    def text(t: str):
        return ToolResult([{"type": "text", "text": t}])
    if isinstance(args, str):
        args = _as_object(args)
    args = args if isinstance(args, dict) else {}
    if tool == FIND_TOOL:
        return text(find_text(tools, args))
    if tool != CALL_TOOL:
        return text(f"ERROR: unknown finder tool {tool!r}.")
    name = str(_first(args, NAME_KEYS) or "").strip()
    raw = _first(args, ARG_KEYS)
    if raw is None:
        # the tool's own arguments beside the name ({"name": "x", "query": "..."})
        raw = {k: v for k, v in args.items() if k not in NAME_KEYS and k not in ARG_KEYS}
    inner = _as_object(raw)
    if inner is None:
        return text("ERROR: arguments must be a JSON object matching the tool's schema, "
                    'e.g. foundry_call_tool(name="...", arguments={"query": "..."}).')
    td = _resolve(tools, name) if name else None
    if td is None or td.server == FINDER_SERVER:
        close = search(tools, name, 3) if name else []
        tip = ("" if not close else " Closest tools you can use: "
               + json.dumps([_entry(t) for t in close], ensure_ascii=False))
        return text(f"ERROR: {name or '(no name)'!r} is not a tool you can use. Use the exact "
                    f"name foundry_find_tools returns." + tip)
    return await call(td, inner)
