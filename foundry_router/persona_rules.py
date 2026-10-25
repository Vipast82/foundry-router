"""The one rule set for persona fields (v0.140.0).

Used by the Personas tab's save (POST /admin/api/personas) and by the operator
chat's persona tools, so a value the GUI would refuse is refused everywhere and
a value the GUI offers is accepted everywhere. The allowed values mirror the
editor's dropdowns (ui/static/index.html, persona editor) and what the router
reads (facade/ollama_api.py, thinking.py, tools/sync.py).

validate(svc, fields) -> (clean, errors, warnings)
  clean     normalized values ready for PersonaStore.upsert (bools as 0/1,
            lists/dicts as Python objects, "flexible"/"agent"/"local" spelled
            the way the store keeps them)
  errors    refusals: nothing should be written while there is one
  warnings  accepted, but worth telling the operator (an MCP server that isn't
            connected right now, a profile placeholder backend, ...)
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .personas import PERSONA_FIELDS

# field -> allowed values (None = unset). Aliases are normalized below.
ENUMS: dict[str, tuple] = {
    "reasoning_effort": (None, "off", "low", "medium", "high", "max"),
    "local_bias_strength": (None, "strong", "moderate", "cost_aware_default", "prefer_paid"),
    "routing": (None, "strict"),
    "fallback_policy": (None, "refuse"),
    "execution_mode": (None, "direct", "pipeline"),
    "kind": (None, "chat", "media"),
    "output_style": (None, "plain_text"),
    "outcome_judge": (None, "paid", "local_large", "brain"),
    "benchmark_category": (None, "coding", "reasoning", "general_chat", "tool_calling",
                           "agentic"),
}
# spellings the GUI or older rows use for the same thing
ALIASES: dict[str, dict] = {
    "reasoning_effort": {"xhigh": "max", "": None},
    "routing": {"flexible": None, "": None},
    "fallback_policy": {"local": None, "": None},
    "execution_mode": {"agent": None, "": None},
    "kind": {"image": "media", "": None},
    "output_style": {"default": None, "": None},
    "outcome_judge": {"off": None, "": None},
    "local_bias_strength": {"": None},
    "benchmark_category": {"": None},
}
CAPABILITIES = ("text", "tools", "image_input", "image_output")
BOOL_FIELDS = ("enabled", "force_reasoning_effort", "mcp_tools_in_direct", "quiet_narration",
               "brain_handles_tools", "pipeline_check_enabled", "prefer_permissive",
               "prefer_loaded", "escalate_when_local_busy", "expose_tool_trail",
               "review_enabled", "review_prefilter", "route_footer")
STR_LIST_FIELDS = ("pinned_models", "model_allowlist", "required_tags", "escalation_triggers",
                   "capabilities")
DICT_FIELDS = ("guardrail_overrides", "selection_weights", "client_compat",
               "sampling_options", "assist")
# (min, max); context_window 0 = auto (advertise the largest worker window)
INT_RANGES = {"context_window": (0, 10_000_000), "max_output_tokens": (256, 1_000_000)}
TEXT_LIMITS = {"description": 2000, "review_model": 200, "agent_backend": 120,
               "backend": 120, "output_format": 20000}


def _as_json(v: Any) -> Any:
    """A JSON string (how the store and old clients send lists/objects) -> value."""
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except ValueError:
            return v
    return v


def known_mcp(svc) -> tuple[set, set]:
    """(servers, tool names) Foundry knows about: discovered tools plus the
    configured MCP servers (an offline server is still configured)."""
    servers, tools = set(), set()
    try:
        for t in svc.tool_registry.status():
            if t.get("kind") == "mcp":
                if t.get("server"):
                    servers.add(t["server"])
                for k in ("mcp_tool", "name"):
                    if t.get(k):
                        tools.add(t[k])
    except Exception:                                            # noqa: BLE001
        pass
    try:
        for s in svc.config_store.config.mcp_servers or []:
            name = getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else None)
            if name:
                servers.add(name)
    except Exception:                                            # noqa: BLE001
        pass
    return servers, tools


def configured_backends(svc) -> list[str]:
    try:
        return [b.name for b in svc.config_store.config.backend_pool.internal.backends or []]
    except Exception:                                            # noqa: BLE001
        return []


def validate_mcp_grants(svc, value: Any) -> tuple[Optional[list], list, list]:
    """preferred_mcp_tools: a list of "server-or-tool" strings and
    {"server": S, "tools": [...]} objects (tools/sync.parse_preferred_mcp)."""
    errors, warnings = [], []
    v = _as_json(value)
    if v is None:
        return [], errors, warnings
    if not isinstance(v, list):
        return None, ["preferred_mcp_tools must be a list of server names or "
                      "{server, tools} objects"], warnings
    servers, tools = known_mcp(svc)
    out = []
    for e in v:
        if isinstance(e, str) and e.strip():
            out.append(e.strip())
            if servers and e.strip() not in servers and e.strip() not in tools:
                warnings.append(f"MCP grant {e!r} matches no connected server or tool right now")
        elif isinstance(e, dict) and isinstance(e.get("server"), str) and e["server"].strip():
            entry: dict = {"server": e["server"].strip()}
            if e.get("tools") is not None:
                if not isinstance(e["tools"], list) or not all(isinstance(t, str) for t in e["tools"]):
                    errors.append(f"MCP grant for {e['server']!r}: tools must be a list of names")
                    continue
                entry["tools"] = [t for t in e["tools"] if t]
            out.append(entry)
            if servers and entry["server"] not in servers:
                warnings.append(f"MCP server {entry['server']!r} is not connected right now")
        else:
            errors.append(f"bad MCP grant {e!r}: use a server name or {{server, tools}}")
    return out, errors, warnings


def validate_client_tools_policy(svc, value: Any) -> tuple[Any, list, list]:
    """mcp_with_client_tools (persona or client key): unset (None), "off",
    "all", "dedupe", or a list of server/tool names in the grant syntax
    (tool_policy). A comma list typed by hand ("searxng, kokoro-tts") is a list."""
    from . import tool_policy
    out, err = tool_policy.normalize(value)
    if err:
        return None, [f"mcp_with_client_tools: {err}"], []
    warnings = []
    if isinstance(out, list):
        servers, tools = known_mcp(svc)
        for e in out:
            n = e if isinstance(e, str) else e["server"]
            if servers and n not in servers and n not in tools:
                warnings.append(f"mcp_with_client_tools: {n!r} matches no connected MCP server "
                                "or tool right now")
    return out, [], warnings


def validate(svc, fields: dict, *, allow_unknown: bool = False) -> tuple[dict, list, list]:
    """Check and normalize persona fields (any subset). Unknown field names are
    an error unless allow_unknown (the admin API keeps its old drop-silently
    behaviour, with a warning)."""
    clean: dict = {}
    errors: list[str] = []
    warnings: list[str] = []
    for k, v in (fields or {}).items():
        if k not in PERSONA_FIELDS:
            (warnings if allow_unknown else errors).append(f"unknown persona field {k!r}"
                                                           + (" (ignored)" if allow_unknown else ""))
            continue
        if k in ENUMS:
            if isinstance(v, str):
                v = v.strip()
            v = ALIASES.get(k, {}).get(v, v) if isinstance(v, (str, type(None))) else v
            if v not in ENUMS[k]:
                allowed = ", ".join(x for x in ENUMS[k] if x)
                errors.append(f"{k} must be one of: {allowed} (or empty)")
                continue
            clean[k] = v
        elif k in BOOL_FIELDS:
            if isinstance(v, bool) or v in (0, 1):
                clean[k] = int(bool(v))
            elif v is None and k != "enabled":
                clean[k] = None
            else:
                errors.append(f"{k} must be true or false")
        elif k in INT_RANGES:
            lo, hi = INT_RANGES[k]
            if v is None or v == "":
                clean[k] = None
                continue
            try:
                n = int(v)
                if isinstance(v, bool) or (isinstance(v, float) and v != n):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{k} must be a whole number")
                continue
            if k == "max_output_tokens" and n == 0:
                clean[k] = None                     # 0 / blank = the global default
            elif not lo <= n <= hi:
                errors.append(f"{k} must be between {lo} and {hi:,}"
                              + (" (0 = auto)" if k == "context_window" else ""))
            else:
                clean[k] = n
        elif k in STR_LIST_FIELDS:
            lv = _as_json(v)
            if lv is None or lv == {}:           # {} is stored on some old rows
                lv = []
            if not isinstance(lv, list) or not all(isinstance(x, str) for x in lv):
                errors.append(f"{k} must be a list of names")
                continue
            lv = [x.strip() for x in lv if x.strip()]
            if k == "capabilities":
                bad = [x for x in lv if x not in CAPABILITIES]
                if bad:
                    errors.append(f"capabilities: unknown {', '.join(bad)} "
                                  f"(allowed: {', '.join(CAPABILITIES)})")
                    continue
            clean[k] = list(dict.fromkeys(lv))
        elif k in DICT_FIELDS:
            dv = _as_json(v)
            if dv is None:
                dv = {}
            if not isinstance(dv, dict):
                errors.append(f"{k} must be a JSON object")
                continue
            clean[k] = dv
        elif k == "mcp_with_client_tools":
            out, e, w = validate_client_tools_policy(svc, v)
            errors += e
            warnings += w
            if not e:
                clean[k] = out
        elif k == "preferred_mcp_tools":
            out, e, w = validate_mcp_grants(svc, v)
            errors += e
            warnings += w
            if out is not None and not e:
                clean[k] = out
        elif k == "backend":
            b = (v or "").strip() if isinstance(v, (str, type(None))) else None
            if b is None:
                errors.append("backend must be a backend name or empty")
            elif not b:
                clean[k] = None
            elif b.startswith("<") and b.endswith(">"):
                warnings.append(f"backend {b} is a profile placeholder: strict routing refuses "
                                "until the real backend exists")
                clean[k] = b
            elif b not in configured_backends(svc):
                errors.append(f"no backend named {b!r} (configured: "
                              f"{', '.join(configured_backends(svc)) or 'none'})")
            else:
                clean[k] = b
        elif k == "output_format":
            if v in (None, ""):
                clean[k] = None
            elif isinstance(v, dict):
                clean[k] = json.dumps(v)
            elif isinstance(v, str) and (v.strip() == "json" or v.strip().startswith("{")):
                if v.strip().startswith("{"):
                    try:
                        json.loads(v)
                    except ValueError:
                        errors.append("output_format: the JSON schema does not parse")
                        continue
                clean[k] = v.strip()
            else:
                errors.append("output_format must be 'json', a JSON schema, or empty")
        else:                                  # free text: description, review_model, ...
            if v is not None and not isinstance(v, str):
                errors.append(f"{k} must be text")
                continue
            if v and len(v) > TEXT_LIMITS.get(k, 2000):
                errors.append(f"{k} is over {TEXT_LIMITS.get(k, 2000)} characters")
                continue
            clean[k] = v if v != "" or k == "description" else None
    # cross-field: a strict persona with no enabled route would allow every model
    if clean.get("routing") == "strict" and "model_allowlist" in clean \
            and not clean["model_allowlist"]:
        errors.append("a strict persona needs at least one model in model_allowlist")
    return clean, errors, warnings
