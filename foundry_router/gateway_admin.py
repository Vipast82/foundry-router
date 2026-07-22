"""Docker MCP Gateway admin — backend-initiated, operator-only (gateway-server
admin spec).

Foundry Router already holds an MCP connection to a Docker MCP Gateway, which
injects its own management tools (mcp-find / mcp-add / mcp-remove / …) into that
connection. This module lets the OPERATOR drive those tools from the admin UI as
a control panel — Foundry's backend acting as an MCP client against a connection
it already holds.

Hard boundaries:
  - These calls are NEVER routed through a persona or a model tool-loop; they run
    only from the /admin/api/gateway/* routes, same trust tier as the "add MCP
    connection" form. The gateway root-admin tools are also excluded from every
    persona's grantable set (tools.sync.is_gateway_admin_tool).
  - Adding a server that needs SECRETS is out of scope: the gateway's secrets
    live in a chmod-600 secrets.env on the host with no remote write path, so
    those rows are surfaced with Add disabled until a companion secrets service
    exists (see docs/GATEWAY_SERVERS.md).

The gateway's tool argument names and mcp-find result shape are the Gateway's,
not ours — so argument names are read from each tool's discovered input schema
(never hardcoded), and the catalog parser is deliberately tolerant of shape and
falls back to surfacing raw output.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

MCP_FIND = "mcp-find"
MCP_ADD = "mcp-add"
MCP_REMOVE = "mcp-remove"
MCP_CONFIG_SET = "mcp-config-set"

# Inspect companion service (optional). mcp-find returns only a summary; the
# richer per-server data (tool count, publisher, full config/secrets) comes from
# `docker mcp catalog server inspect`, a HOST CLI with no MCP tool. When the
# operator runs the contrib companion service on the gateway host and sets its
# URL here, the panel's per-row Inspect button fetches that detail. Stored in kv
# (like MCP auth tokens) so no config.yaml edit / restart is needed.
INSPECT_URL_KEY = "gateway_inspect_url"
INSPECT_TOKEN_KEY = "gateway_inspect_token"


def inspect_settings(db) -> dict:
    return {"url": db.kv_get(INSPECT_URL_KEY) or "",
            "has_token": bool(db.kv_get(INSPECT_TOKEN_KEY))}


def set_inspect_settings(db, url: str, token: Optional[str] = None) -> None:
    """Persist the inspect companion URL (and optionally its bearer token —
    write-only, blank keeps the existing one, empty string on an explicit clear)."""
    url = (url or "").strip()
    if url:
        db.kv_set(INSPECT_URL_KEY, url)
    else:
        db.kv_del(INSPECT_URL_KEY)
    if token is not None:
        if token:
            db.kv_set(INSPECT_TOKEN_KEY, token)
        else:
            db.kv_del(INSPECT_TOKEN_KEY)


def _loads_any(text: str) -> Any:
    """JSON first (mcp-find), then YAML (`docker mcp catalog server inspect`
    returns YAML). PyYAML is a project dependency."""
    data = _loads(text)
    if data is not None:
        return data
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        return None


def _server_block(data: dict) -> dict:
    """Locate the server-metadata block in an inspect payload, tolerant of
    nesting: snapshot.server, then server, else the top level."""
    if not isinstance(data, dict):
        return {}
    snap = data.get("snapshot")
    if isinstance(snap, dict) and isinstance(snap.get("server"), dict):
        return snap["server"]
    if isinstance(data.get("server"), dict):
        return data["server"]
    return data


def _inspect_tools(block: dict, data: dict) -> list[dict]:
    """Parsed tool table with ground-truth read/write from each tool's
    annotations (readOnlyHint/destructiveHint), falling back to the name
    heuristic (marked source='heuristic') only when a tool has no annotation."""
    from .tools.sync import tool_write_badge
    raw_tools = None
    for src in (block, data):
        if isinstance(src, dict) and isinstance(src.get("tools"), list):
            raw_tools = src["tools"]
            break
    out = []
    for t in raw_tools or []:
        if not isinstance(t, dict):
            continue
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        ro, de = ann.get("readOnlyHint"), ann.get("destructiveHint")
        badge = tool_write_badge(str(t.get("name") or ""), ro, de)
        out.append({"name": str(t.get("name") or ""),
                    "description": str(t.get("description") or ""),
                    "read_only": ro, "destructive": de,
                    "kind": badge["kind"], "source": badge["source"]})
    return out


def parse_inspect(text: str) -> dict:
    """Structured summary of a `docker mcp catalog server inspect` payload (YAML
    or JSON): title, image, publisher (image namespace), a tool table with
    annotation-based read/write badges, config schema, and secrets. Falls back
    to {} — the UI always shows the raw inspect output too, so nothing is hidden
    if the shape differs."""
    data = _loads_any(text)
    if not isinstance(data, dict):
        return {}
    block = _server_block(data)
    title = _first(block, "title", "displayName", "name") or ""
    image = _first(block, "image", "dockerImage", "ref") or ""
    publisher = (image.split("/", 1)[0] if image and "/" in image
                 else _publisher(block, ""))
    tools = _inspect_tools(block, data)
    cfg_req = _config_required(block)
    if cfg_req is None:
        cfg_req = _config_required(data)
    return {"title": str(title), "image": str(image), "publisher": publisher,
            "tools": tools,
            "tool_count": len(tools) or _tool_count(block) or _tool_count(data),
            "config_required": cfg_req,
            "config_schema": _config_schema(block) or _config_schema(data),
            "requires_secrets": _requires_secrets(block) or _requires_secrets(data)}


async def inspect_server(svc, server: str, catalog: Optional[str] = None) -> dict:
    """Fetch richer detail for one catalog server via the inspect companion
    service. Returns {configured: False} when no URL is set (the UI then hides
    the Inspect affordance); otherwise {configured, ok, raw, parsed}."""
    url = (svc.db.kv_get(INSPECT_URL_KEY) or "").rstrip("/")
    if not url:
        return {"configured": False}
    headers = {}
    token = svc.db.kv_get(INSPECT_TOKEN_KEY)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"server": server}
    if catalog:
        payload["catalog"] = catalog
    r = await svc.http.post(url + "/inspect", json=payload, headers=headers, timeout=35)
    r.raise_for_status()
    data = r.json()
    raw = data.get("raw") or ""
    return {"configured": True, "ok": bool(data.get("ok")),
            "raw": raw, "parsed": parse_inspect(raw)}


# --------------------------------------------------------------------------- #
# Tolerant parsing of catalog results                                        #
# --------------------------------------------------------------------------- #

def _loads(text: str) -> Any:
    """Best-effort JSON out of MCP text output: whole string, then the first
    balanced array or object embedded in prose/code fences."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    cleaned = text.replace("```json", "").replace("```", "")
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = cleaned.find(open_ch)
        while start != -1:
            depth = 0
            for i in range(start, len(cleaned)):
                if cleaned[i] == open_ch:
                    depth += 1
                elif cleaned[i] == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cleaned[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = cleaned.find(open_ch, start + 1)
    return None


def _as_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("results", "servers", "items", "matches", "catalog", "data"):
            if isinstance(data.get(k), list):
                return data[k]
        # a single-server object
        if any(k in data for k in ("name", "ref", "fullName", "id")):
            return [data]
    return []


def _first(item: dict, *keys: str) -> Optional[Any]:
    for k in keys:
        if item.get(k) not in (None, ""):
            return item[k]
    return None


_SECRET_FLAG_KEYS = ("requires_secrets", "requiresSecrets", "needs_secrets",
                     "secrets_required", "requiresSecret")
_SECRET_LIST_KEYS = ("secrets", "required_secrets", "requiredSecrets", "env",
                     "environment")


def _requires_secrets(item: dict) -> bool:
    """Whether a catalog server needs secrets — from whatever the catalog
    metadata exposes. Checked across the shapes the gateway might use; a true
    here disables Add until a companion secrets service exists."""
    for k in _SECRET_FLAG_KEYS:
        if k in item:
            return bool(item[k])
    for k in _SECRET_LIST_KEYS:
        v = item.get(k)
        if isinstance(v, (list, dict)) and len(v) > 0:
            return True
    for k in ("note", "label", "notes", "tags", "requirements"):
        v = item.get(k)
        if isinstance(v, str) and "secret" in v.lower():
            return True
        if isinstance(v, list) and any("secret" in str(x).lower() for x in v):
            return True
    return False


def _tool_count(item: dict) -> Optional[int]:
    v = item.get("tools")
    if isinstance(v, (list, dict)):
        return len(v)
    for k in ("tools", "toolCount", "tool_count", "num_tools", "toolsCount",
              "nTools", "tool_names", "toolNames"):
        vv = item.get(k)
        if isinstance(vv, int):
            return vv
        if isinstance(vv, (list, dict)):
            return len(vv)
    meta = item.get("metadata") or item.get("server")
    if isinstance(meta, dict) and meta is not item:
        return _tool_count(meta)
    return None


def _publisher(item: dict, ref: str) -> str:
    """Publisher/author of a catalog server, across likely key names; falls
    back to the namespace of a slash-namespaced ref (e.g. 'docker/playwright'
    -> 'docker') so near-duplicate entries are still distinguishable."""
    for k in ("publisher", "author", "owner", "vendor", "maintainer",
              "organization", "org", "namespace", "source", "provider"):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "id", "login", "title"):
                if v.get(kk):
                    return str(v[kk])
    meta = item.get("metadata")
    if isinstance(meta, dict):
        p = _publisher(meta, "")
        if p:
            return p
    if ref and "/" in ref:
        return ref.split("/", 1)[0]
    return ""


_CONFIG_KEYS = ("config", "configuration", "required_config", "requiredConfig",
                "configSchema", "config_schema", "parameters", "settings")


def _config_required(item: dict) -> Optional[bool]:
    """Non-secret required config, SEPARATE from secrets (a server can need
    config yet no secrets — the case that failed silently). Tri-state: True =
    config present/required, False = a config field exists but is empty, None =
    the payload says nothing about config (unknown, shown as '—')."""
    seen = False
    for k in _CONFIG_KEYS:
        if k in item:
            seen = True
            v = item[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (list, dict)) and len(v) > 0:
                return True
    meta = item.get("metadata")
    if isinstance(meta, dict):
        nested = _config_required(meta)
        if nested is not None:
            return nested
    return False if seen else None


def _norm_cfg_block(b: Any) -> Optional[dict]:
    if not isinstance(b, dict):
        return None
    props = b.get("properties")
    return {"name": str(b.get("name") or ""),
            "description": str(b.get("description") or ""),
            "properties": props if isinstance(props, dict) else {}}


def _config_schema(item: dict) -> list[dict]:
    """Config schema blocks for the config-set FORM (structured-inspect spec):
    a list of {name, description, properties (JSON-schema object)}. Handles a
    list of blocks, a single block, or a bare {prop: {type}} map. Only blocks
    with actual properties are returned."""
    if not isinstance(item, dict):
        return []
    for k in ("config_schema", "configSchema", "config", "configuration"):
        v = item.get(k)
        if isinstance(v, list):
            blocks = [_norm_cfg_block(b) for b in v]
            return [b for b in blocks if b and b["properties"]]
        if isinstance(v, dict):
            if "properties" in v:
                b = _norm_cfg_block(v)
                return [b] if b and b["properties"] else []
            # a bare {propname: {type: ...}} map
            if v and all(isinstance(x, dict) for x in v.values()):
                return [{"name": "", "description": "", "properties": v}]
    meta = item.get("metadata")
    if isinstance(meta, dict):
        return _config_schema(meta)
    return []


def parse_catalog(text: str) -> list[dict]:
    """Normalize an mcp-find result into rows the UI can render. Tolerant of the
    exact gateway shape; unknown items degrade rather than crash. Each row:
    {name, ref, publisher, description, tools (int|None),
     config_required (bool|None), requires_secrets (bool), raw (orig item)}.
    `raw` is kept so the UI can show the untouched payload per row — the fastest
    way to see what mcp-find actually returned when a field is missing."""
    items = _as_list(_loads(text))
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"name": it, "ref": it, "publisher": "", "description": "",
                        "tools": None, "config_required": None, "config_schema": [],
                        "requires_secrets": False, "raw": it})
            continue
        if not isinstance(it, dict):
            continue
        name = _first(it, "name", "title", "ref", "fullName", "id", "server") or "?"
        ref = _first(it, "ref", "fullName", "id", "name", "server") or name
        out.append({
            "name": str(name),
            "ref": str(ref),
            "publisher": _publisher(it, str(ref)),
            "description": str(_first(it, "description", "summary", "desc") or ""),
            "tools": _tool_count(it),
            "config_required": _config_required(it),
            "config_schema": _config_schema(it),
            "requires_secrets": _requires_secrets(it),
            "raw": it,
        })
    return out


# --------------------------------------------------------------------------- #
# Schema-driven argument names + orchestration                               #
# --------------------------------------------------------------------------- #

def _arg_name(tooldef, prefer: list[str]) -> str:
    """Pick the argument name for a gateway tool call from its DISCOVERED input
    schema — so we send whatever key this gateway build actually expects instead
    of hardcoding one. Prefers a known name, else the first string property,
    else the first preference as a last resort."""
    props = {}
    if tooldef is not None and isinstance(getattr(tooldef, "parameters", None), dict):
        props = tooldef.parameters.get("properties") or {}
    for p in prefer:
        if p in props:
            return p
    for k, v in props.items():
        if isinstance(v, dict) and v.get("type") == "string":
            return k
    return prefer[0]


async def find_servers(svc, gateway: str, query: str) -> dict:
    tool = svc.tool_registry.mcp_tool_def(gateway, MCP_FIND)
    qp = _arg_name(tool, ["query", "q", "search", "keyword", "name", "term"])
    raw = await svc.mcp.call_tool(gateway, MCP_FIND, {qp: query})
    return {"raw": raw, "results": parse_catalog(raw)}


async def _ref_call(svc, gateway: str, tool_name: str, ref: str) -> str:
    tool = svc.tool_registry.mcp_tool_def(gateway, tool_name)
    rp = _arg_name(tool, ["ref", "server", "serverName", "name", "id", "fullName"])
    return await svc.mcp.call_tool(gateway, tool_name, {rp: ref})


async def add_server(svc, gateway: str, ref: str) -> dict:
    """Attach a catalog server, then re-run Tool Sync so the caller can confirm
    the new tools appeared (the spec's confirm-via-Tool-Sync pattern)."""
    raw = await _ref_call(svc, gateway, MCP_ADD, ref)
    svc.db.log_event("info", "gateway", f"operator added gateway server {ref!r}", raw[:500])
    sync = await svc.tool_registry.sync(svc.pool)
    return {"raw": raw, "sync": sync}


async def remove_server(svc, gateway: str, ref: str) -> dict:
    raw = await _ref_call(svc, gateway, MCP_REMOVE, ref)
    svc.db.log_event("info", "gateway", f"operator removed gateway server {ref!r}", raw[:500])
    sync = await svc.tool_registry.sync(svc.pool)
    return {"raw": raw, "sync": sync}


async def config_set(svc, gateway: str, server: str, values: dict) -> dict:
    """Set non-secret config for a catalog server via the gateway's
    mcp-config-set tool (backend admin call — same trust tier as mcp-add, never
    persona-exposed). The tool's argument shape is unverified, so it's built
    tolerantly: the server-name arg and the config-map arg are read from the
    tool's discovered input schema (an object-typed property carries the map;
    if none, the values are flattened as top-level args). The raw response is
    always returned, so a shape mismatch surfaces as the gateway's own
    (actionable) error rather than a silent failure."""
    tool = svc.tool_registry.mcp_tool_def(gateway, MCP_CONFIG_SET)
    server_arg = _arg_name(tool, ["server", "serverName", "name", "ref", "id"])
    props = {}
    if tool is not None and isinstance(getattr(tool, "parameters", None), dict):
        props = tool.parameters.get("properties") or {}
    obj_arg = None
    for cand in ("config", "values", "settings", "configuration", "data"):
        p = props.get(cand)
        if isinstance(p, dict) and p.get("type") in ("object", None):
            obj_arg = cand
            break
    if obj_arg is None:
        for k, v in props.items():
            if k != server_arg and isinstance(v, dict) and v.get("type") == "object":
                obj_arg = k
                break
    args = ({server_arg: server, obj_arg: values} if obj_arg
            else {server_arg: server, **values})
    raw = await svc.mcp.call_tool(gateway, MCP_CONFIG_SET, args)
    svc.db.log_event("info", "gateway",
                     f"operator set config for gateway server {server!r}",
                     json.dumps({"args": sorted(args)})[:500])
    return {"raw": raw, "args_sent": sorted(args)}


def attached_view(svc) -> list[dict]:
    """Per-gateway 'what's attached' view derived from Tool Sync data: the
    downstream (non-management) tools, grouped by an explicit namespace prefix
    when the gateway namespaces them, so an operator gets per-server Remove
    buttons. Ungrouped tools are shown together; the manual remove-by-ref box
    covers anything the grouping can't name."""
    out = []
    for g in svc.tool_registry.gateway_servers():
        tools = svc.tool_registry.gateway_downstream_tools(g)
        groups: dict[str, list[str]] = {}
        for t in tools:
            mt = t.mcp_tool or t.name
            grp = None
            for sep in (".", "__", ":", "/"):
                if sep in mt:
                    grp = mt.split(sep, 1)[0]
                    break
            groups.setdefault(grp or "(ungrouped)", []).append(mt)
        out.append({
            "gateway": g,
            "tool_count": len(tools),
            "groups": [{"name": k, "tools": sorted(v), "removable": k != "(ungrouped)"}
                       for k, v in sorted(groups.items())],
        })
    return out
