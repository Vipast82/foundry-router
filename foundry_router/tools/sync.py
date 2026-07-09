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
                                  "Include all context it needs — it does not see the conversation."}},
    "required": ["prompt"],
}


def sanitize(model_id: str) -> str:
    """ask_<sanitized_model_id> naming convention: 'qwen3.6:27b' -> 'ask_qwen3_6_27b',
    'anthropic/claude-sonnet-5' -> 'ask_anthropic_claude_sonnet_5'."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", model_id).strip("_").lower()
    return f"ask_{s}"


def _describe_model(model_id: str, backends: list[str], meta: Optional[dict]) -> str:
    parts = [f"Send a prompt to the model '{model_id}' (served by: {', '.join(backends)})."]
    if meta:
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

    # -- queries -------------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolDef]:
        t = self.tools.get(name)
        return t if t and not t.disabled else None

    def enabled(self) -> list[ToolDef]:
        return [t for t in self.tools.values() if not t.disabled]

    def specs_for_persona(self, persona: Optional[dict] = None) -> list[dict]:
        """Model tools are always offered. MCP tools are offered only when the
        persona lists their server in preferred_mcp_tools — that's the
        'preferentially load for this persona's sessions' field from §4.8.
        (The Research Agent bypasses this and calls MCPManager directly.)"""
        import json
        preferred: set[str] = set()
        if persona and persona.get("preferred_mcp_tools"):
            try:
                preferred = set(json.loads(persona["preferred_mcp_tools"]))
            except (json.JSONDecodeError, TypeError):
                pass
        out = []
        for t in self.enabled():
            if t.kind == "mcp" and t.server not in preferred and t.name not in preferred:
                continue
            out.append(t.spec())
        return out

    def status(self) -> list[dict]:
        return [{"name": t.name, "kind": t.kind, "model_id": t.model_id,
                 "server": t.server, "mcp_tool": t.mcp_tool,
                 "description": t.description, "disabled": t.disabled}
                for t in sorted(self.tools.values(), key=lambda t: t.name)]

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
