"""Which persona MCP tools ride along when the client brings its own tools (v0.142.0).

A direct-mode request that carries its own tools (AnythingLLM @agent, Cline,
Kilo, Open WebUI native tools) gets the persona's granted MCP tools merged in,
so the model has more to work with than the client alone offers. Every tool
definition is prompt, though: Foundry-Chat's 58 were ~13.4k tokens, read cold
on the first step of each new chat (~11 s on Strata). So the merge is a
setting, chosen per persona and, above that, per client key:

  "all"            every granted tool (the default; the pre-v0.142 behaviour)
  "off"            none: the client's own tools only
  "dedupe"         every granted tool except those that duplicate one of the
                   client's own, by name or by purpose (web search, page
                   fetch/crawl, files, shell, SQL, image / speech / music
                   generation)
  [names…]         only these: server or tool names, or {"server", "tools"}
                   objects (the grant syntax). Narrows the grants, never
                   widens them.

Where it is set (first one set wins):
  1. the client key (config clients[].mcp_with_client_tools), or for keyless
     requests client_auth.keyless_mcp_with_client_tools
  2. the persona (personas.mcp_with_client_tools)
  3. "all"

The result depends only on the persona, the client and the client's tool
list, never on the message: a tool list that changed between steps would
rewrite the prompt prefix and cost a full re-read of the conversation on
llama.cpp/Strata (the 4–6 min cache-bust finding). Requests with no client
tools are not affected; persona.mcp_tools_in_direct stays the master switch.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

OFF, ALL, LIST, DEDUPE = "off", "all", "list", "dedupe"
DEFAULT = ALL
_OFF_WORDS = ("off", "none", "false", "no")
_DEDUPE_WORDS = ("dedupe", "auto-dedupe", "auto_dedupe", "auto")


def parse(raw: Any) -> Optional[tuple[str, list]]:
    """(mode, entries) for a stored value, or None when it is unset
    (the next level decides). Anything unreadable counts as unset."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return (ALL, []) if raw else (OFF, [])
    if isinstance(raw, str):
        s = raw.strip()
        low = s.lower()
        if not s:
            return None
        if low in _OFF_WORDS:
            return OFF, []
        if low == ALL:
            return ALL, []
        if low in _DEDUPE_WORDS:
            return DEDUPE, []
        try:
            raw = json.loads(s)
        except ValueError:
            return None
    if isinstance(raw, list):
        entries = [e for e in raw if (isinstance(e, str) and e.strip())
                   or (isinstance(e, dict) and e.get("server"))]
        return (LIST, entries) if entries else (OFF, [])
    return None


def policy(persona: Optional[dict], override: Any = None) -> tuple[str, list, str]:
    """(mode, entries, source): the client override when set, else the
    persona's setting, else the default."""
    p = parse(override)
    if p is not None:
        return p[0], p[1], "client"
    p = parse((persona or {}).get("mcp_with_client_tools"))
    if p is not None:
        return p[0], p[1], "persona"
    return DEFAULT, [], "default"


# -- auto-dedupe -------------------------------------------------------------------------

_PURPOSES = {
    "web search": r"web search|web brows|search(?:es|ing)? (?:the )?(?:web|internet|online)"
                  r"|searx|google|bing|duckduckgo|tavily|serp|brave search",
    "page fetch": r"crawl|scrap|fetch(?:es|ing)? (?:a |the )?(?:url|web ?page|page|site)"
                  r"|read(?:s|ing)? (?:a |the )?(?:url|web ?page)|web (?:fetch|page)",
    "files": r"\b(?:read|write|list|edit|create|delete|move|search)(?: text)? "
             r"(?:files?|dir|directory|directories|folders?)\b|filesystem",
    "shell": r"\b(?:shell|terminal|bash|powershell)\b|(?:run|execute)s? (?:a )?command",
    "sql": r"\bsql\b|sqlite|database quer|query (?:the |a )?database",
    "image generation": r"generat\w* (?:an? )?(?:image|picture|photo)|text to image|txt2img"
                        r"|comfy|stable diffusion|image generat",
    "speech": r"\btts\b|text to speech|speech|speak\b|voice",
    "music / sound": r"music|\bsong\b|sound effect|\bsfx\b|audio generat"
                     r"|generat\w* (?:a )?(?:sound|audio)",
}
_PURPOSE_RE = {k: re.compile(v) for k, v in _PURPOSES.items()}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _text(name: str, desc: str) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", f"{name} {(desc or '')[:400]}")
    return re.sub(r"[_\-./:]+", " ", s.lower())


def purposes(name: str, description: str = "") -> set[str]:
    t = _text(name, description)
    return {k for k, rx in _PURPOSE_RE.items() if rx.search(t)}


def _client_view(client_tools) -> tuple[set, set]:
    names, covered = set(), set()
    for t in client_tools or []:
        fn = (t or {}).get("function") or {}
        n = fn.get("name") or ""
        names.add(_norm(n))
        covered |= purposes(n, fn.get("description") or "")
    return names, covered


def duplicates(tool, client_tools) -> Optional[str]:
    """Why this persona tool duplicates one of the client's, or None. A tool
    is a duplicate when its name matches a client tool's (ignoring case and
    punctuation), or every purpose it has is already one of the client's."""
    names, covered = _client_view(client_tools)
    for n in (getattr(tool, "name", None), getattr(tool, "mcp_tool", None)):
        if n and _norm(n) in names:
            return f"same name as the client's {n}"
    mine = purposes(getattr(tool, "mcp_tool", None) or getattr(tool, "name", ""),
                    getattr(tool, "description", "") or "")
    if mine and mine <= covered:
        return "client already has " + " / ".join(sorted(mine))
    return None


# -- selection ---------------------------------------------------------------------------

def _matches(tool, whole: set, scoped: dict, bare: set) -> bool:
    srv = getattr(tool, "server", None) or ""
    names = {n for n in (getattr(tool, "mcp_tool", None), getattr(tool, "name", None)) if n}
    return bool(srv in whole or srv in bare or (names & bare)
                or (srv in scoped and (names & scoped[srv])))


def select(persona: Optional[dict], granted: list, client_tools=None,
           override: Any = None) -> tuple[list, str, str]:
    """(tools to offer, mode, source) from the persona's granted MCP tools,
    in grant order (stable). Without client tools every granted tool is
    offered, as before."""
    if not client_tools:
        return list(granted), ALL, "no client tools"
    mode, entries, source = policy(persona, override)
    if mode == OFF:
        return [], mode, source
    if mode == ALL:
        return list(granted), mode, source
    if mode == DEDUPE:
        return [t for t in granted if not duplicates(t, client_tools)], mode, source
    from .tools.sync import parse_preferred_mcp
    whole, scoped, bare = parse_preferred_mcp({"preferred_mcp_tools": json.dumps(entries)})
    return [t for t in granted if _matches(t, whole, scoped, bare)], mode, source


def describe(raw: Any) -> str:
    """Short wording of one stored value, for status lines and views."""
    p = parse(raw)
    if p is None:
        return "default (all)"
    mode, entries = p
    if mode == LIST:
        names = [e if isinstance(e, str) else e.get("server") for e in entries]
        return "only " + ", ".join(str(n) for n in names[:6]) + ("…" if len(names) > 6 else "")
    return {DEDUPE: "auto-dedupe"}.get(mode, mode)


def normalize(value: Any) -> tuple[Any, Optional[str]]:
    """A value from the GUI / API / chat -> (stored value, error).
    Stored: None (unset), "off", "all", "dedupe", or a list of names."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return ("all" if value else "off"), None
    if isinstance(value, str):
        s = value.strip()
        low = s.lower()
        if not s or low in ("default", "inherit", "unset"):
            return None, None
        if low in _OFF_WORDS:
            return OFF, None
        if low == ALL:
            return ALL, None
        if low in _DEDUPE_WORDS:
            return DEDUPE, None
        if s.startswith("["):
            try:
                value = json.loads(s)
            except ValueError:
                return None, "the list does not parse as JSON"
        else:
            value = [x.strip() for x in s.split(",") if x.strip()]
    if not isinstance(value, list):
        return None, "use off, all, dedupe, or a list of MCP server/tool names"
    out = []
    for e in value:
        if isinstance(e, str) and e.strip():
            out.append(e.strip())
        elif isinstance(e, dict) and isinstance(e.get("server"), str) and e["server"].strip():
            entry: dict = {"server": e["server"].strip()}
            if e.get("tools") is not None:
                if not isinstance(e["tools"], list) or not all(isinstance(t, str) for t in e["tools"]):
                    return None, f"for {e['server']!r}: tools must be a list of names"
                entry["tools"] = [t for t in e["tools"] if t]
            out.append(entry)
        else:
            return None, f"bad entry {e!r}: use a server or tool name, or {{server, tools}}"
    return (out or OFF), None
