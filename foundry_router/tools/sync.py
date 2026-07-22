"""Dynamic Tool Sync (design doc §4.2).

The Agent Brain's dynamic tools are NEVER a hand-maintained list — every sync:
  1. queries the Backend Pool for currently-healthy backends + live model lists,
  2. cross-references the Model Registry so each generated ask_* tool carries
     real metadata (good_for / reasoning_style / relative_cost_tier),
  3. queries every configured MCP server for its self-declared manifest,
  4. diffs against the in-memory registry: new tools added, vanished removed.

Removal grace: available_models() already excludes only backends the pool has
marked unhealthy (failure_threshold consecutive failures + cooldown), so tool
removal rides the pool's existing blip-absorption instead of a second timer.

Manual override: the ONE manual action here is disabling an auto-discovered
tool (web UI); persisted in tool_overrides so it survives future syncs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ..db import Database
from ..registry.models_db import ModelRegistry
from .mcp_client import MCPManager

log = logging.getLogger(__name__)


@dataclass
class ToolDef:
    name: str
    kind: str                     # "model" | "mcp"
    description: str
    parameters: dict
    model_id: Optional[str] = None      # kind=model
    server: Optional[str] = None        # kind=mcp
    mcp_tool: Optional[str] = None      # kind=mcp (original, unsanitized name)
    disabled: bool = False

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


_ASK_PARAMS = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string",
                   "description": "The complete, self-contained prompt to send to this model. "
                                  "Include all context it needs — it does not see the conversation."},
        "include_full_user_message": {
            "type": "boolean",
            "description": "Set true when the user's message was shown to you truncated "
                           "([preview truncated...]) — the COMPLETE original user message "
                           "is then appended verbatim to your prompt for this worker. "
                           "Never retype truncated content yourself."},
        "include_images": {
            "type": "boolean",
            "description": "Set true when the user attached image(s) ([ATTACHED: N "
                           "image(s)...]) — they are forwarded to this worker in its "
                           "native format. Vision-tagged workers receive them "
                           "automatically even without this flag."}},
    "required": ["prompt"],
}


def sanitize(model_id: str) -> str:
    """ask_<sanitized_model_id> naming convention: 'qwen3.6:27b' -> 'ask_qwen3_6_27b',
    'anthropic/claude-sonnet-5' -> 'ask_anthropic_claude_sonnet_5'."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", model_id).strip("_").lower()
    return f"ask_{s}"


# Write/execute-capable tool heuristic (per-tool-grant spec, stretch goal): a
# naming signal so the UI can pre-highlight the dangerous tools an admin must
# opt into deliberately. Exact word-part matching (split on non-alphanumerics)
# avoids substring false positives like "put" inside "output". It is a HINT for
# the operator, never an access decision — scoping is what actually gates a tool.
_WRITE_PARTS = {"create", "delete", "remove", "add", "write", "exec", "update",
                "put", "drop", "set", "activate", "deactivate", "install", "run"}


def is_write_tool(name: str) -> bool:
    n = (name or "").lower()
    if "code-mode" in n or "code_mode" in n:   # gateway execute meta-tool
        return True
    return any(p in _WRITE_PARTS for p in re.split(r"[^a-z0-9]+", n))


# Docker MCP Gateway management tools (gateway-server admin spec). The Gateway
# injects these into its own MCP connection when dynamic-tools is on. They are
# the router-operator's control surface — Foundry's backend calls them directly
# from the admin UI — and are identified by NAME (the Gateway's protocol), never
# by a hardcoded connection name, so detection works for any gateway connection.
#
# GATEWAY_ADMIN_TOOLS are root-level write/reconfigure over the whole gateway
# (choosing what code runs in a container). They must NEVER be exposed to a
# model or persona — excluded from every persona's grantable set below and from
# the persona editor — regardless of the admin page. mcp-find/mcp-discover are
# read-only and used only to identify a gateway and browse the catalog.
GATEWAY_ADMIN_TOOLS = {"mcp-add", "mcp-remove", "mcp-create-profile",
                       "mcp-activate-profile", "mcp-config-set", "mcp-config-write",
                       "mcp-exec", "code-mode"}   # exec-over-gateway: never grantable
GATEWAY_MANAGEMENT_TOOLS = GATEWAY_ADMIN_TOOLS | {
    "mcp-find", "mcp-discover", "mcp-config-get", "mcp-registry"}


def is_gateway_admin_tool(name: str) -> bool:
    return (name or "").lower() in GATEWAY_ADMIN_TOOLS


def is_gateway_management_tool(name: str) -> bool:
    return (name or "").lower() in GATEWAY_MANAGEMENT_TOOLS


def parse_preferred_mcp(persona: Optional[dict]) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Parse a persona's preferred_mcp_tools into the three grant shapes.

    Entries are a mixed JSON list (per-tool-grant spec; strict superset of the
    old bare-string-only format):
      - a bare string  -> may name a whole SERVER or an individual TOOL; kept
        permissive (matched against either) so every existing persona and the
        legacy per-tool-name grant keep working unchanged;
      - {"server": S, "tools": [...]}  -> only those tools on S;
      - {"server": S}  (no "tools")    -> the whole server S.

    Returns (whole_servers, scoped {server -> tool names}, bare_names).
    Malformed entries are skipped, never fatal."""
    import json
    whole: set[str] = set()
    scoped: dict[str, set[str]] = {}
    bare: set[str] = set()
    raw = (persona or {}).get("preferred_mcp_tools")
    try:
        entries = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        entries = []
    if not isinstance(entries, list):
        entries = []
    for e in entries:
        if isinstance(e, str):
            bare.add(e)
        elif isinstance(e, dict) and e.get("server"):
            srv = str(e["server"])
            tools = e.get("tools")
            if tools is None:
                whole.add(srv)
            elif isinstance(tools, list):
                scoped.setdefault(srv, set()).update(str(t) for t in tools)
    return whole, scoped, bare


def _describe_model(model_id: str, backends: list[str], meta: Optional[dict]) -> str:
    import json as _json
    parts = [f"Send a prompt to the model '{model_id}' (served by: {', '.join(backends)})."]
    if meta:
        try:
            tags = _json.loads(meta.get("tags") or "[]")
        except (_json.JSONDecodeError, TypeError):
            tags = []
        if tags:
            parts.append("Tags: " + ", ".join(str(t) for t in tags) + ".")
        if meta.get("content_policy") == "permissive":
            parts.append("Content policy: permissive/uncensored.")
        if meta.get("good_for"):
            parts.append(f"Good for: {meta['good_for']}.")
        if meta.get("reasoning_style"):
            parts.append(f"Reasoning style: {meta['reasoning_style']}.")
        tier = meta.get("relative_cost_tier")
        if tier:
            parts.append(f"Relative cost: {tier}.")
        elif meta.get("cost_per_1k_output") is None:
            parts.append("Cost: unknown — treat as moderate; consider request_model_research.")
        if meta.get("benefits_from_explicit_prompting"):
            parts.append("Benefits from explicit prompting — consider refine_prompt first.")
    else:
        parts.append("No registry data yet — treat as unknown capability and moderate "
                     "cost; you may call request_model_research for it.")
    return " ".join(parts)


class ToolRegistry:
    def __init__(self, db: Database, registry: ModelRegistry, mcp: MCPManager):
        self.db = db
        self.registry = registry
        self.mcp = mcp
        self.tools: dict[str, ToolDef] = {}
        self._sync_lock = asyncio.Lock()
        self.last_sync: Optional[str] = None
        # (server, tool) scoped grants already reported missing — so the
        # drop-and-log for a vanished scoped tool fires once, not per request.
        self._logged_missing_scoped: set[tuple[str, str]] = set()

    # -- queries -------------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolDef]:
        t = self.tools.get(name)
        return t if t and not t.disabled else None

    def enabled(self) -> list[ToolDef]:
        return [t for t in self.tools.values() if not t.disabled]

    def mcp_tools_for_persona(self, persona: Optional[dict] = None) -> list[ToolDef]:
        """The enabled MCP ToolDefs this persona may call, honoring both grant
        shapes (per-tool-grant spec): a whole-server grant exposes every tool
        currently registered for that server (dynamic — grows/shrinks with Tool
        Sync); a scoped grant exposes only the named tools.

        A scoped tool that Tool Sync no longer registers (renamed/removed
        upstream, or its server offline) is simply absent from the result —
        dropped silently — and reported ONCE to the event log per (server,
        tool), never erroring the persona out."""
        whole, scoped, bare = parse_preferred_mcp(persona)
        out: list[ToolDef] = []
        registered: dict[str, set[str]] = {}   # server -> names actually present
        for t in self.enabled():
            if t.kind != "mcp":
                continue
            # Gateway root-admin tools are never grantable to a model/persona,
            # even under a whole-server grant — they reconfigure the whole
            # gateway (gateway-server admin spec security note).
            if is_gateway_admin_tool(t.mcp_tool or t.name):
                continue
            srv = t.server or ""
            names = {n for n in (t.mcp_tool, t.name) if n}
            if srv in scoped:
                registered.setdefault(srv, set()).update(names)
            if srv in whole or srv in bare or (names & bare) \
                    or (srv in scoped and (names & scoped[srv])):
                out.append(t)
        self._report_missing_scoped(persona, scoped, registered)
        return out

    def _report_missing_scoped(self, persona: Optional[dict],
                               scoped: dict[str, set[str]],
                               registered: dict[str, set[str]]) -> None:
        pname = (persona or {}).get("virtual_name", "?")
        for srv, present in registered.items():
            for tool in present:               # reappeared -> allow a future re-log
                self._logged_missing_scoped.discard((srv, tool))
        for srv, wanted in scoped.items():
            for tool in sorted(wanted - registered.get(srv, set())):
                if (srv, tool) in self._logged_missing_scoped:
                    continue
                self._logged_missing_scoped.add((srv, tool))
                self.db.log_event(
                    "info", "tool_sync",
                    f"persona {pname}: scoped MCP tool {srv}/{tool} is not "
                    f"currently registered — dropped from this persona's tools",
                    "renamed/removed upstream, or the server is offline")

    def specs_for_persona(self, persona: Optional[dict] = None) -> list[dict]:
        """Model tools are always offered. MCP tools are offered only per the
        persona's preferred_mcp_tools grants (whole-server or scoped-tool) —
        the 'preferentially load for this persona's sessions' field from §4.8.
        (The Research Agent bypasses this and calls MCPManager directly.)"""
        out = [t.spec() for t in self.enabled() if t.kind != "mcp"]
        out += [t.spec() for t in self.mcp_tools_for_persona(persona)]
        return out

    def status(self) -> list[dict]:
        return [{"name": t.name, "kind": t.kind, "model_id": t.model_id,
                 "server": t.server, "mcp_tool": t.mcp_tool,
                 "description": t.description, "disabled": t.disabled,
                 # write/execute heuristic (per-tool-grant spec stretch goal) —
                 # a hint for the UI to pre-highlight dangerous tools. is_admin
                 # marks gateway root-admin tools the UI hides from persona grants.
                 **({"is_write": is_write_tool(t.mcp_tool or t.name),
                     "is_admin": is_gateway_admin_tool(t.mcp_tool or t.name)}
                    if t.kind == "mcp" else {})}
                for t in sorted(self.tools.values(), key=lambda t: t.name)]

    # -- Docker MCP Gateway admin (gateway-server admin spec) -------------------

    def gateway_servers(self) -> list[str]:
        """MCP connections that expose the Docker MCP Gateway management tools —
        detected by tool presence (mcp-find/mcp-add), never by a hardcoded
        connection name, so it works for any gateway connection the operator
        configured."""
        found: dict[str, set[str]] = {}
        for t in self.tools.values():
            if t.kind == "mcp" and is_gateway_management_tool(t.mcp_tool or ""):
                found.setdefault(t.server or "", set()).add((t.mcp_tool or "").lower())
        return sorted(s for s, ms in found.items()
                      if s and ({"mcp-find", "mcp-add"} & ms))

    def mcp_tool_def(self, server: str, mcp_tool: str) -> Optional[ToolDef]:
        """The registered ToolDef for a specific (server, original tool name),
        so callers can read its input schema — used to pick the right argument
        name for a gateway call instead of guessing a wire vocabulary."""
        for t in self.tools.values():
            if t.kind == "mcp" and t.server == server and (t.mcp_tool or "") == mcp_tool:
                return t
        return None

    def gateway_downstream_tools(self, server: str) -> list[ToolDef]:
        """Enabled tools on a gateway connection that are NOT its own management
        tools — i.e. tools contributed by attached catalog servers. This is the
        'what's currently attached' signal derived from Tool Sync data."""
        return [t for t in self.enabled()
                if t.kind == "mcp" and t.server == server
                and not is_gateway_management_tool(t.mcp_tool or "")]

    # -- overrides ------------------------------------------------------------------

    def set_disabled(self, name: str, disabled: bool) -> None:
        from ..db import utcnow
        if disabled:
            self.db.execute(
                "INSERT INTO tool_overrides(tool_name, disabled, updated_at) VALUES(?,1,?) "
                "ON CONFLICT(tool_name) DO UPDATE SET disabled=1, updated_at=excluded.updated_at",
                (name, utcnow()))
        else:
            self.db.execute("DELETE FROM tool_overrides WHERE tool_name=?", (name,))
        if name in self.tools:
            self.tools[name].disabled = disabled

    def _disabled_set(self) -> set[str]:
        return {r["tool_name"] for r in
                self.db.query("SELECT tool_name FROM tool_overrides WHERE disabled=1")}

    # -- the sync itself ---------------------------------------------------------------

    async def sync(self, pool) -> dict:
        """Rebuild the dynamic tool set from live state. Called on pool state
        changes and on a periodic fallback sweep.

        DESIGN DECISION (see design doc §7): a sync never mutates a tool set an
        in-flight request is using — the agent captures its tool specs once at
        request start (agent.py), so sync results apply to the *next* request.
        Executing a tool that vanished mid-request degrades to a tool-result
        error the brain can react to, which is the safe half of the race.
        """
        async with self._sync_lock:
            new_tools: dict[str, ToolDef] = {}
            disabled = self._disabled_set()

            # 1+2: model tools from pool state, described from the registry
            for model_id, backends in pool.available_models().items():
                meta_row = self.registry.get(model_id)
                if meta_row is not None and meta_row.get("enabled") == 0:
                    # Governance disable (registry `enabled`): excluded from
                    # tool generation entirely, not just deprioritized.
                    continue
                name = sanitize(model_id)
                if name in new_tools:
                    # Two distinct model ids sanitizing identically is rare;
                    # first (highest-priority backend order) wins, and we log it.
                    self.db.log_event("warning", "tool_sync",
                                      f"tool-name collision: {model_id} also maps to {name}")
                    continue
                meta = self.registry.get(model_id)
                new_tools[name] = ToolDef(
                    name=name, kind="model",
                    description=_describe_model(model_id, backends, meta),
                    parameters=_ASK_PARAMS, model_id=model_id,
                    disabled=name in disabled)

            # 3: MCP manifests — self-describing, names/descriptions pass through
            manifests = await self.mcp.list_all()
            for server, tools in manifests.items():
                for t in tools:
                    name = t["name"]
                    if name in new_tools:
                        name = f"{server}_{name}"
                    new_tools[name] = ToolDef(
                        name=name, kind="mcp",
                        description=f"[MCP:{server}] {t['description']}",
                        parameters=t["input_schema"],
                        server=server, mcp_tool=t["name"],
                        disabled=name in disabled)

            # 4: diff for the log, then swap atomically
            added = sorted(set(new_tools) - set(self.tools))
            removed = sorted(set(self.tools) - set(new_tools))
            if added or removed:
                self.db.log_event("info", "tool_sync",
                                  f"tool set changed: +{len(added)} -{len(removed)}",
                                  f"added={added} removed={removed}")
            self.tools = new_tools
            from ..db import utcnow
            self.last_sync = utcnow()
            return {"added": added, "removed": removed, "total": len(new_tools)}
