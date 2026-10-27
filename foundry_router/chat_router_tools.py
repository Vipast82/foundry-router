"""Operator chat tools for personas, backends and the whole router (v0.140.0).

Registered into the chat engine's catalog by ChatEngine._build_catalog. Every
write goes through a confirm card (classes persona / backend / router_config /
admin, each switchable to auto except irreversible admin calls), takes a state
snapshot first (configops.snapshot; config.yaml changes are also versioned in
Config history) and is audited in chat_audit_log by the engine.

Personas      set_persona_routes, set_persona_fields, create_persona,
              grant_persona_to_client, set_client_tool_policy, run_persona_evals —
              validated by
              persona_rules (the Personas tab's rules) and route_resolve
              ("local" / "Claude" / "ChatGPT" -> concrete models); every persona
              write reminds the operator to run that persona's evals.
Backends      set_backend_enabled — the same write as the Pool / Routing switch,
              with the personas it would leave without a usable route.
Whole router  list_admin_endpoints, admin_get (any GET /admin/api/...),
              get_router_config / set_router_config (config.yaml through
              configops.apply_raw: validated, versioned, applied live where it
              can be), admin_call (any other admin write, with a deny list for
              secrets), research_status / research_models, performance_report,
              get_chat_backend / set_chat_backend (the chat's own model chain).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import configops, persona_rules, route_resolve, router_ops
from .chat_engine import PERSONA_CHAIN, ChatError, Ctx, _compact, _uj

ADMIN_PREFIX = "/admin/api/"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")

# GETs that return files, archives or secret-bearing bundles
GET_DENY = [
    (re.compile(r"^/admin/api/export"), "the export bundle can carry secrets"),
    (re.compile(r"^/admin/api/diagnostics"), "a zip download"),
    (re.compile(r"^/admin/api/data/export"), "a file download"),
    (re.compile(r"^/admin/api/media/artifacts/"), "an image download"),
    (re.compile(r"^/admin/api/training/(capture/download|datasets/[^/]+/[^/]+)"), "a file download"),
    (re.compile(r"^/admin/api/interventions/export"), "a file download"),
    (re.compile(r"^/admin/api/meridian/auth"), "Meridian sign-in details"),
]
# writes that create, show or set secrets, or that a typed tool does better
WRITE_DENY = [
    (re.compile(r"^/admin/api/setup/clients/?$"), "creating a client key shows the key; make it "
     "in Setup -> Clients & keys"),
    (re.compile(r"^/admin/api/setup/clients/[^/]+/rotate"), "rotating a key shows the new key; do "
     "it in Setup -> Clients & keys"),
    (re.compile(r"^/admin/api/meridian/(reauth|auth_config|refresh)"), "Meridian sign-in is done "
     "in the dashboard (Pool -> Meridian)"),
    (re.compile(r"^/admin/api/mcp_servers/token"), "MCP tokens are secrets; set them in MCP Servers"),
    (re.compile(r"^/admin/api/import"), "an import can write secrets; use the dashboard's Import"),
    (re.compile(r"^/admin/api/config/backends$"), "this writes API keys verbatim; use "
     "set_router_config (backend_pool.internal.backends.<name>.<field>)"),
    (re.compile(r"^/admin/api/branding/logo"), "a file upload"),
    (re.compile(r"^/admin/api/restart"), "restarting the router is done from the dashboard or the "
     "host (docker compose restart)"),
    (re.compile(r"^/admin/api/personas/?$"), "use set_persona_fields / set_persona_routes / "
     "create_persona (validated, snapshotted, eval reminder)"),
    (re.compile(r"^/admin/api/personas/clone"), "use create_persona with copy_from"),
    (re.compile(r"^/admin/api/backends/enabled"), "use set_backend_enabled (shows orphaned personas)"),
    (re.compile(r"^/admin/api/config/cutoffs"), "use set_cutoffs"),
    (re.compile(r"^/admin/api/eval/run"), "use run_persona_evals"),
    (re.compile(r"^/admin/api/models/research$"), "use research_models"),
]
# can't be undone: always a card (even with the class on auto), drawn in red
IRREVERSIBLE = re.compile(
    r"^/admin/api/(data/(clear|prune)|events/clear|usage/clear|perf/clear|mcp-metrics/clear|"
    r"agent-runs/clear|devlog/clear|semcache/clear|personas/delete|ollama/delete|"
    r"mcp_servers/delete|agents/delete|eval/prompts/delete|pricing/delete|"
    r"models/named_benchmark/delete|models/reset_benchmarks|gateway/remove)")

TEMPLATES = {
    "strict-chat": {"execution_mode": "direct", "routing": "strict", "fallback_policy": "refuse",
                    "capabilities": ["text", "tools"], "kind": "chat",
                    "benchmark_category": "general_chat", "local_bias_strength": "prefer_paid",
                    "mcp_tools_in_direct": 1},
    "flexible-chat": {"execution_mode": None, "routing": None, "fallback_policy": None,
                      "capabilities": ["text", "tools"], "kind": "chat",
                      "benchmark_category": "general_chat", "local_bias_strength": "moderate"},
}
# set_persona_fields edits these; routes and the name have their own tools
FIELD_KEYS = ("context_window", "max_output_tokens", "reasoning_effort", "force_reasoning_effort",
              "local_bias_strength", "routing", "backend", "description", "preferred_mcp_tools",
              "mcp_tools_in_direct", "mcp_with_client_tools", "fallback_policy",
              "execution_mode", "capabilities",
              "enabled", "benchmark_category", "expose_tool_trail", "quiet_narration",
              "output_style", "sampling_options", "escalate_when_local_busy", "outcome_judge")


def eval_reminder(name: str) -> str:
    return (f"Run {name}'s evals now (convention: evals after every persona change): say "
            f"'run its evals' here (run_persona_evals), or Dashboard -> Evals -> Run, or "
            f"POST /admin/api/eval/run {{\"persona\": \"{name}\", \"wait\": true}}.")


def _persona(svc, name: str) -> dict:
    p = svc.personas.get(name or "")
    if not p:
        raise ChatError(f"no persona {name!r} (list_personas shows them)", "not_found", 404)
    return p


def _route_text(r: dict) -> str:
    fam = route_resolve.FAMILY_LABEL.get(r.get("family"), r.get("family") or "?")
    return (f"{r['model']}@{r.get('backend') or '?'} ({fam})"
            + ("" if r.get("enabled", True) else " (off)"))


def _snapshot(ctx: Ctx, what: str) -> int:
    return configops.snapshot(ctx.svc, f"operator chat: {what} (before change by {ctx.actor})")


def _scrub(svc, obj: Any) -> Any:
    """Drop secret values from anything an admin endpoint returned."""
    secrets = configops.secret_values(svc)
    text = json.dumps(obj, default=str)
    clean = configops.scrub(text, secrets)
    return json.loads(clean) if clean != text else obj


async def admin_request(eng, method: str, path: str, query: Optional[dict] = None,
                        body: Any = None) -> tuple[int, Any]:
    """One in-process call to the router's own admin API under the chat's
    internal admin identity."""
    if eng.app is None:
        raise ChatError("the chat engine has no app to call the admin API through", "no_app", 503)
    import httpx
    from .client_auth import INTERNAL_ADMIN_HEADER, INTERNAL_ADMIN_TOKEN
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=eng.app),
                                 base_url="http://foundry.internal",
                                 headers={INTERNAL_ADMIN_HEADER: INTERNAL_ADMIN_TOKEN},
                                 timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        r = await client.request(method, path, params={k: v for k, v in (query or {}).items()
                                                       if v is not None},
                                 json=body if method != "GET" else None)
    try:
        data = r.json()
    except ValueError:
        data = r.text[:6000]
    return r.status_code, _scrub(eng.svc, data)


def _admin_path_problem(path: str, method: str) -> str:
    if not isinstance(path, str) or not path.startswith(ADMIN_PREFIX):
        return f"only {ADMIN_PREFIX}... paths (list_admin_endpoints shows them)"
    if ".." in path or "?" in path or "#" in path:
        return "give the path without a query string (use query / body)"
    for rx, why in (GET_DENY if method == "GET" else WRITE_DENY):
        if rx.search(path):
            return f"{path} is not available from the chat: {why}"
    return ""


# ---------------------------------------------------------------------------- config paths

def _walk(raw: Any, parts: list[str], create: bool = False):
    """(parent container, last key) for a dotted path; named list items
    (backends, clients, mcp_servers, agents) are addressed by name."""
    cur = raw
    for i, seg in enumerate(parts[:-1]):
        if isinstance(cur, list):
            nxt = next((x for x in cur if isinstance(x, dict) and x.get("name") == seg), None)
            if nxt is None and seg.isdigit() and int(seg) < len(cur):
                nxt = cur[int(seg)]
            if nxt is None:
                raise KeyError(f"no item named {seg!r} under {'.'.join(parts[:i]) or 'the root'}")
            cur = nxt
        elif isinstance(cur, dict):
            if seg not in cur or cur[seg] is None:
                if not create:
                    raise KeyError(f"{'.'.join(parts[:i + 1])} is not set")
                cur[seg] = {}
            cur = cur[seg]
        else:
            raise KeyError(f"{'.'.join(parts[:i + 1])} is not a section")
    return cur, parts[-1]


def _get_path(raw: dict, path: str) -> Any:
    try:
        parent, key = _walk(raw, path.split("."))
    except KeyError:
        return None
    if isinstance(parent, list):
        return next((x for x in parent if isinstance(x, dict) and x.get("name") == key), None)
    return parent.get(key) if isinstance(parent, dict) else None


def plan_config(svc, changes: dict) -> tuple[dict, list, list]:
    """(new raw config, [{path, before, after}], sections) — validated.
    Raises ChatError with the reason."""
    if not isinstance(changes, dict) or not changes:
        raise ChatError("give changes as {\"section.key\": value}")
    store = svc.config_store
    new = copy.deepcopy(store.raw)
    rows = []
    for path, value in changes.items():
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,160}", path):
            raise ChatError(f"{path!r} is not a config path (section.key, list items by name)")
        parts = path.split(".")
        if parts[0] == "config_version":
            raise ChatError("config_version is managed by Foundry")
        if any(configops.is_secret_key(p) for p in parts):
            raise ChatError(f"{path} is a secret; secrets are set in config.yaml / the dashboard, "
                            "never through the chat")
        if "<redacted>" in json.dumps(value, default=str):
            raise ChatError(f"{path}: the value contains a redacted placeholder")
        if parts[0] == "clients" and len(parts) > 2 and parts[2] in ("key_hash", "key_hint",
                                                                      "created"):
            raise ChatError(f"{path} is managed by Setup -> Clients & keys")
        try:
            parent, key = _walk(new, parts, create=True)
        except KeyError as e:
            raise ChatError(str(e).strip("'\"")) from None
        if isinstance(parent, list):
            raise ChatError(f"{path}: name a field of the item (e.g. {path}.enabled)")
        before = copy.deepcopy(parent.get(key)) if isinstance(parent, dict) else None
        parent[key] = value
        rows.append({"path": path, "before": configops.redact({"v": before})["v"],
                     "after": value})
    from .config import AppConfig, _expand_env
    try:
        AppConfig.model_validate(_expand_env(copy.deepcopy(new)))
    except Exception as e:                                       # noqa: BLE001
        msg = str(e).replace("\n", " ")
        raise ChatError(f"the config would not validate: {msg[:500]}") from None
    sections = configops.changed_sections(store.raw, new)
    return new, rows, sections


def restart_forecast(sections: list) -> tuple[list, list]:
    live_now = {"backend_pool", "agent_brain", "guardrails", "meridian"} | configops._LIVE
    return ([s for s in sections if s in live_now], [s for s in sections if s not in live_now])


# ---------------------------------------------------------------------------- persona plans

def plan_routes(svc, name: str, routes_arg: list, backend_arg: Optional[str],
                persona: Optional[dict] = None, routing: Optional[str] = "<keep>",
                current: Optional[list] = None) -> dict:
    """Resolve and check a new route list. Raises ChatError (refusal)."""
    p = persona if persona is not None else svc.personas.get(name) or {}
    cur = current if current is not None else (route_resolve.current_routes(svc, p) if p else [])
    try:
        routes, notes = route_resolve.resolve(svc, routes_arg or [], cur)
    except route_resolve.ResolveError as e:
        raise ChatError(str(e)) from None
    if not routes:
        raise ChatError("give at least one route")
    if not any(r["enabled"] for r in routes):
        raise ChatError(f"{name} needs at least one route turned on (an empty list would allow "
                        "every model)")
    mode = p.get("routing") if routing == "<keep>" else routing
    old_backend = (p.get("backend") or "").strip() or None
    if backend_arg is not None:
        b = backend_arg.strip()
        if b and b not in persona_rules.configured_backends(svc):
            raise ChatError(f"no backend named {b!r}")
        new_backend = b or None
    else:
        new_backend = old_backend
    dead = route_resolve.strict_dead_routes(svc, routes, mode, new_backend)
    if dead:
        hint = ("pass backend=\"\" to untie it (each model then uses whichever backend serves it), "
                "or name a backend that serves them") if backend_arg is None else \
            "pick a backend that serves every enabled route, or backend=\"\""
        raise ChatError(f"{name} is a strict persona tied to backend {new_backend}, which does not "
                        f"serve {', '.join(dead)}: those routes would never be used; {hint}")
    warnings = route_resolve.reachability(svc, routes, mode, new_backend)
    fields: dict = {"pinned_models": [r["model"] for r in routes],
                    "model_allowlist": [r["model"] for r in routes if r["enabled"]]}
    if backend_arg is not None:
        fields["backend"] = new_backend
    return {"routes": routes, "notes": notes, "warnings": warnings, "fields": fields,
            "before": cur, "backend": (old_backend, new_backend)}


def _merge_mcp(current: list, add: list, remove: list) -> list:
    out = [copy.deepcopy(e) for e in current]

    def key(e):
        return e if isinstance(e, str) else (e.get("server") if isinstance(e, dict) else None)
    for e in add or []:
        k = key(e)
        if k and not any(key(x) == k for x in out):
            out.append(e)
        elif isinstance(e, dict) and e.get("tools"):
            for x in out:
                if isinstance(x, dict) and x.get("server") == k and x.get("tools") is not None:
                    x["tools"] = list(dict.fromkeys(list(x["tools"]) + list(e["tools"])))
    for r in remove or []:
        name = r if isinstance(r, str) else (r.get("server") if isinstance(r, dict) else None)
        if isinstance(r, dict) and r.get("tools"):
            for x in out:
                if isinstance(x, dict) and x.get("server") == name and x.get("tools") is not None:
                    x["tools"] = [t for t in x["tools"] if t not in r["tools"]]
            continue
        out = [x for x in out if key(x) != name]
        for x in out:               # a tool name inside a scoped grant
            if isinstance(x, dict) and x.get("tools") and name in x["tools"]:
                x["tools"] = [t for t in x["tools"] if t != name]
    return out


def plan_fields(svc, name: str, fields: dict, mcp_add=None, mcp_remove=None) -> dict:
    p = _persona(svc, name)
    fields = dict(fields or {})
    bad = [k for k in fields if k not in FIELD_KEYS]
    if bad:
        hint = (" (routes: set_persona_routes)" if {"pinned_models", "model_allowlist"} & set(bad)
                else "")
        raise ChatError(f"not editable here: {', '.join(bad)}{hint}; editable: "
                        + ", ".join(FIELD_KEYS))
    if mcp_add or mcp_remove:
        if "preferred_mcp_tools" in fields:
            raise ChatError("give preferred_mcp_tools OR mcp_add / mcp_remove, not both")
        cur = _uj(p.get("preferred_mcp_tools"), []) or []
        fields["preferred_mcp_tools"] = _merge_mcp(cur if isinstance(cur, list) else [],
                                                   mcp_add or [], mcp_remove or [])
    if not fields:
        raise ChatError("give fields to change")
    clean, errors, warnings = persona_rules.validate(svc, fields)
    if errors:
        raise ChatError("; ".join(errors))
    routing = clean.get("routing", p.get("routing")) if "routing" in clean else p.get("routing")
    backend = clean["backend"] if "backend" in clean else ((p.get("backend") or "").strip() or None)
    cur_routes = route_resolve.current_routes(svc, p)
    dead = route_resolve.strict_dead_routes(svc, cur_routes, routing, backend)
    if dead:
        raise ChatError(f"{name} would be a strict persona tied to {backend}, which does not serve "
                        f"its routes {', '.join(dead)}; change the routes (set_persona_routes) or "
                        "the backend in the same card")
    rows = []
    for k, v in clean.items():
        before = p.get(k)
        if k in ("preferred_mcp_tools", "capabilities", "sampling_options",
                 "mcp_with_client_tools"):
            before = _uj(before, before)
        rows.append({"field": k, "before": before, "after": v})
    return {"clean": clean, "warnings": warnings, "rows": rows}


# ---------------------------------------------------------------------------- registration

def register(eng) -> None:                    # noqa: C901 - the catalog in one place
    S = {"type": "string", "max": 120}

    def _try(fn):
        """A check hook from a plan function: '' or the refusal text."""
        def check(a, ctx):
            try:
                fn(a, ctx)
            except ChatError as e:
                return str(e)
            return ""
        return check

    # ---- personas ---------------------------------------------------------------
    def _routes_plan(a, ctx):
        _persona(ctx.svc, a["name"])
        return plan_routes(ctx.svc, a["name"], a["routes"], a.get("backend"))

    def _routes_preview(a, ctx):
        pl = _routes_plan(a, ctx)
        old_b, new_b = pl["backend"]
        return {"routes": [_route_text(r) for r in pl["routes"]],
                "before": [_route_text({**r, "family": route_resolve.family_of_model(
                    route_resolve.backends(ctx.svc), r["model"], r.get("backend", ""))})
                    for r in pl["before"]],
                "backend": f"{old_b or '(any)'} -> {new_b or '(any)'}" if old_b != new_b else None,
                "warnings": pl["warnings"], "notes": pl["notes"]}

    @eng._add("set_persona_routes", "Set a persona's route list: ordered models, each on or off "
              "(at least one on). Routes may be model ids, model@backend, a backend name, or the "
              "family words local / Claude / ChatGPT (GPT), which keep the persona's current "
              "routes of that family (so 'only local and Claude' drops the rest). Optional "
              "backend ties a strict persona to one backend (\"\" unties it); optional enabled "
              "turns the persona on/off. Snapshot first; the card shows the concrete models.",
              "persona", {"name": S, "routes": {"type": "array", "max_items": 12},
                          "backend": {"type": "string", "max": 120},
                          "enabled": {"type": "boolean"}},
              ("name", "routes"),
              summary=lambda a: f"persona {a.get('name')}: routes " + ", ".join(
                  (r.get("model") if isinstance(r, dict) else str(r))
                  + ("" if not isinstance(r, dict) or r.get("enabled", True) else " (off)")
                  for r in a.get("routes") or [])
              + ("" if a.get("backend") is None else f"; backend {a['backend'] or '(any)'}")
              + ("" if a.get("enabled") is None else
                 f"; persona {'enabled' if a['enabled'] else 'disabled'}"),
              check=_try(_routes_plan), preview=_routes_preview)
    async def set_persona_routes(ctx: Ctx, a: dict):
        p = _persona(ctx.svc, a["name"])
        pl = plan_routes(ctx.svc, a["name"], a["routes"], a.get("backend"))
        sid = _snapshot(ctx, f"routes of {p['virtual_name']}")
        fields = dict(pl["fields"])
        if a.get("enabled") is not None:
            fields["enabled"] = 1 if a["enabled"] else 0
        ctx.svc.personas.upsert(p["virtual_name"], **fields)
        on = fields["model_allowlist"]
        ctx.svc.db.log_event("info", "chat", f"persona {p['virtual_name']} routes changed by "
                             f"{ctx.actor} via operator chat: {', '.join(fields['pinned_models'])}")
        return {"result": {"snapshot": sid, "routes": [_route_text(r) for r in pl["routes"]],
                           "warnings": pl["warnings"],
                           "unknown_models": [r["model"] for r in pl["routes"]
                                              if not route_resolve.served_by(
                                                  route_resolve.backends(ctx.svc), r["model"])],
                           "evals": eval_reminder(p["virtual_name"])},
                "changed": [f"persona {p['virtual_name']}: routes {', '.join(on)} (snapshot {sid})"]}

    def _fields_plan(a, ctx):
        return plan_fields(ctx.svc, a["name"], a.get("fields") or {}, a.get("mcp_add"),
                           a.get("mcp_remove"))

    @eng._add("set_persona_fields", "Edit a persona's settings: context_window (0 = auto), "
              "max_output_tokens, reasoning_effort (off/low/medium/high/max) and "
              "force_reasoning_effort, local_bias_strength (strong/moderate/cost_aware_default/"
              "prefer_paid), routing (strict or flexible), backend, description, "
              "preferred_mcp_tools (MCP servers/tools it may use; or mcp_add / mcp_remove lists "
              "of server or tool names) and mcp_tools_in_direct, mcp_with_client_tools (which "
              "of those MCP tools still ride along when the client brings its OWN tools, e.g. "
              "AnythingLLM @agent or Cline: empty = default (all), off, all, dedupe (skip ones "
              "that duplicate the client's own), or a list of server/tool names; a client "
              "key's own setting wins, see set_client_tool_policy), fallback_policy (refuse or "
              "local), execution_mode, capabilities, enabled, and a few more. Same rules as the "
              "Personas tab; snapshot first.", "persona",
              {"name": S, "fields": {"type": "object"}, "mcp_add": {"type": "array"},
               "mcp_remove": {"type": "array"}}, ("name",),
              summary=lambda a: f"persona {a.get('name')}: " + ", ".join(
                  f"{k}={_compact(v, 60)}" for k, v in (a.get("fields") or {}).items())
              + (f"; MCP +{_compact(a.get('mcp_add'), 80)}" if a.get("mcp_add") else "")
              + (f"; MCP -{_compact(a.get('mcp_remove'), 80)}" if a.get("mcp_remove") else ""),
              check=_try(_fields_plan),
              preview=lambda a, ctx: {"fields": (pl := _fields_plan(a, ctx))["rows"],
                                      "warnings": pl["warnings"]})
    async def set_persona_fields(ctx: Ctx, a: dict):
        p = _persona(ctx.svc, a["name"])
        pl = _fields_plan(a, ctx)
        sid = _snapshot(ctx, f"fields of {p['virtual_name']}")
        ctx.svc.personas.upsert(p["virtual_name"], **pl["clean"])
        ctx.svc.db.log_event("info", "chat", f"persona {p['virtual_name']} fields changed by "
                             f"{ctx.actor} via operator chat: {', '.join(pl['clean'])}")
        return {"result": {"snapshot": sid, "changed": pl["rows"], "warnings": pl["warnings"],
                           "evals": eval_reminder(p["virtual_name"])},
                "changed": [f"persona {p['virtual_name']}: {', '.join(pl['clean'])} "
                            f"(snapshot {sid})"]}

    def _create_plan(a, ctx):
        svc = ctx.svc
        name = a["name"].strip()
        if not NAME_RE.match(name):
            raise ChatError("persona name: letters, digits and . _ : - (up to 80), starting with "
                            "a letter or digit")
        if svc.personas.get(name):
            raise ChatError(f"a persona named {name!r} already exists")
        src = a.get("copy_from")
        tpl = a.get("template")
        if bool(src) == bool(tpl):
            raise ChatError("give copy_from (an existing persona) or template "
                            f"({' / '.join(TEMPLATES)}), one of them")
        base: dict = {}
        current: list = []
        if src:
            sp = _persona(svc, src)
            current = route_resolve.current_routes(svc, sp)
            base = {"routing": sp.get("routing"), "backend": sp.get("backend")}
        else:
            base = dict(TEMPLATES[tpl])
        routes = None
        if a.get("routes"):
            routes = plan_routes(svc, name, a["routes"], a.get("backend"), persona=base,
                                 routing=base.get("routing"), current=current)
        elif tpl:
            raise ChatError("a new persona from a template needs routes")
        extra = {k: v for k, v in (a.get("fields") or {}).items()}
        clean, errors, warnings = persona_rules.validate(svc, extra) if extra else ({}, [], [])
        if errors:
            raise ChatError("; ".join(errors))
        return {"name": name, "src": src, "tpl": tpl, "routes": routes, "fields": clean,
                "warnings": warnings + (routes["warnings"] if routes else []),
                "clients": router_ops.clients_missing(svc, name)}

    @eng._add("create_persona", "Create a persona: copy_from an existing one (all its settings) "
              "or template strict-chat (direct, strict routing, refuses when every route is "
              "denied) / flexible-chat; then routes (same words as set_persona_routes), "
              "optional backend, description and fields (as set_persona_fields). Starts "
              "DISABLED unless enabled is true. The result says which client keys must allow "
              "it (grant_persona_to_client).", "persona",
              {"name": S, "copy_from": S,
               "template": {"type": "string", "enum": list(TEMPLATES)},
               "routes": {"type": "array", "max_items": 12}, "backend": S,
               "description": {"type": "string", "max": 2000}, "fields": {"type": "object"},
               "enabled": {"type": "boolean"}}, ("name",),
              summary=lambda a: f"new persona {a.get('name')} from "
              + (f"copy of {a['copy_from']}" if a.get("copy_from") else f"template {a.get('template')}")
              + (f"; routes {', '.join(str(r.get('model') if isinstance(r, dict) else r) for r in a['routes'])}"
                 if a.get("routes") else "")
              + ("; enabled" if a.get("enabled") else "; disabled"),
              check=_try(_create_plan),
              preview=lambda a, ctx: (lambda pl: {
                  "routes": [_route_text(r) for r in (pl["routes"] or {}).get("routes", [])],
                  "fields": [{"field": k, "after": v} for k, v in pl["fields"].items()],
                  "warnings": pl["warnings"],
                  "clients_to_allow": [c["client"] for c in pl["clients"]],
                  "note": "starts enabled" if a.get("enabled") else "starts disabled"})(
                  _create_plan(a, ctx)))
    async def create_persona(ctx: Ctx, a: dict):
        svc = ctx.svc
        pl = _create_plan(a, ctx)
        name = pl["name"]
        sid = _snapshot(ctx, f"create persona {name}")
        if pl["src"]:
            if svc.personas.clone(pl["src"], name) is None:
                raise ChatError(f"could not copy {pl['src']}")
            fields: dict = {}
        else:
            fields = dict(TEMPLATES[pl["tpl"]])
        if pl["routes"]:
            fields.update(pl["routes"]["fields"])
        fields.update(pl["fields"])
        if a.get("description") is not None:
            fields["description"] = a["description"]
        fields["enabled"] = 1 if a.get("enabled") else 0
        svc.personas.upsert(name, **fields)
        svc.db.log_event("info", "chat", f"persona {name} created by {ctx.actor} via operator chat"
                         + (f" (copy of {pl['src']})" if pl["src"] else f" (template {pl['tpl']})"))
        clients = [c["client"] for c in pl["clients"]]
        return {"result": {
            "snapshot": sid, "persona": name, "enabled": bool(fields["enabled"]),
            "routes": [_route_text(r) for r in (pl["routes"] or {}).get("routes", [])],
            "warnings": pl["warnings"],
            "client_keys_to_allow": clients,
            "next": ([f"Client keys {', '.join(clients)} have a persona allow-list without "
                      f"{name}: calls from them are refused until it is added "
                      f"(grant_persona_to_client, one card each)."] if clients else [])
            + ([] if fields["enabled"] else [f"{name} is disabled: enable it with "
                                             f"set_persona_fields enabled=true after its evals."])
            + [eval_reminder(name)]},
            "changed": [f"persona {name} created ({'enabled' if fields['enabled'] else 'disabled'};"
                        f" snapshot {sid})"]}

    def _grant_check(a, ctx):
        cur = next((c for c in ctx.svc.config_store.config.clients or []
                    if c.name == a["client"]), None)
        if cur is None:
            return f"no client named {a['client']!r}"
        if ctx.svc.personas.get(a["persona"]) is None:
            return f"no persona {a['persona']!r}"
        if not cur.personas:
            return f"client {a['client']} has no persona allow-list: it may use every persona already"
        if a["persona"] in cur.personas:
            return f"client {a['client']} already allows {a['persona']}"
        return ""

    @eng._add("grant_persona_to_client", "Add a persona to one client key's persona allow-list "
              "(a client with a list may only use the personas on it). config.yaml, versioned.",
              "persona", {"persona": S, "client": S}, ("persona", "client"),
              summary=lambda a: f"allow persona {a.get('persona')} on client key {a.get('client')}",
              check=_grant_check)
    async def grant_persona_to_client(ctx: Ctx, a: dict):
        sid = _snapshot(ctx, f"allow {a['persona']} on {a['client']}")
        try:
            new = router_ops.grant_persona(ctx.svc, a["client"], a["persona"],
                                           f"{ctx.actor} via operator chat")
        except router_ops.OpError as e:
            raise ChatError(str(e), "invalid", e.status) from None
        return {"result": {"client": a["client"], "personas": new, "snapshot": sid},
                "changed": [f"client {a['client']}: persona {a['persona']} allowed"]}

    @eng._add("set_client_tool_policy", "Which persona MCP tools ride along when ONE client "
              "(a client key name, or 'keyless' for requests without a key, e.g. an untracked "
              "AnythingLLM) sends its own tools. value: default (follow the persona), off, all, "
              "dedupe (skip persona tools that duplicate the client's own by name or purpose), "
              "or a comma list of MCP server/tool names, e.g. 'Media_Gen, "
              "searxng'. Wins over the persona's "
              "mcp_with_client_tools; never adds tools the persona isn't granted. "
              "config.yaml, versioned.",
              "persona", {"client": S, "value": {"type": "string", "max": 600}},
              ("client", "value"),
              summary=lambda a: f"client {a.get('client')}: MCP tools with client tools = "
                                f"{a.get('value')}")
    async def set_client_tool_policy(ctx: Ctx, a: dict):
        sid = _snapshot(ctx, f"MCP tools with client tools on {a['client']}")
        try:
            out, warnings = router_ops.set_client_tool_policy(
                ctx.svc, a["client"], a["value"], f"{ctx.actor} via operator chat")
        except router_ops.OpError as e:
            raise ChatError(str(e), "invalid", e.status) from None
        from . import tool_policy
        shown = tool_policy.describe(out) if out is not None else "follow the persona"
        return {"result": {"client": a["client"], "mcp_with_client_tools": out,
                           "warnings": warnings, "snapshot": sid},
                "changed": [f"{a['client']}: MCP tools with client tools = {shown}"]}

    @eng._add("run_persona_evals", "Start a persona's eval run (its eval prompts through its real "
              "route; frontier personas spend a few subscription calls). Results land in "
              "Dashboard -> Evals; read them later with admin_get /admin/api/eval/runs.",
              "persona", {"name": S, "categories": {"type": "array", "max_items": 8}}, ("name",),
              summary=lambda a: f"run evals for {a.get('name')}",
              check=lambda a, ctx: "" if ctx.svc.personas.get(a["name"]) else
              f"no persona {a['name']!r}")
    async def run_persona_evals(ctx: Ctx, a: dict):
        svc = ctx.svc
        p = _persona(svc, a["name"])
        if getattr(svc, "evals", None) is None:
            raise ChatError("the eval harness is not running", "unavailable", 503)
        task = asyncio.get_running_loop().create_task(
            svc.evals.run(p["virtual_name"], "", a.get("categories") or None))
        svc._eval_tasks = [t for t in getattr(svc, "_eval_tasks", []) if not t.done()] + [task]
        return {"result": {"started": True, "persona": p["virtual_name"],
                           "where": "Dashboard -> Evals, or admin_get /admin/api/eval/runs "
                                    f"with query {{\"persona\": \"{p['virtual_name']}\"}}"},
                "changed": [f"eval run started for {p['virtual_name']}"]}

    # ---- backends ---------------------------------------------------------------
    def _backend_check(a, ctx):
        names = persona_rules.configured_backends(ctx.svc)
        if a["name"] not in names:
            return f"no backend named {a['name']!r} (have: {', '.join(names) or 'none'})"
        return ""

    def _backend_preview(a, ctx):
        off = [] if a["enabled"] else [a["name"]]
        on = [a["name"]] if a["enabled"] else []
        orph = route_resolve.orphans(ctx.svc, off, on)
        b = route_resolve.backends(ctx.svc).get(a["name"]) or {}
        return {"backend": a["name"], "family": route_resolve.FAMILY_LABEL.get(b.get("family"), "?"),
                "now": "on" if b.get("enabled", True) else "off",
                "orphans": [f"{o['persona']}: {o['why']} -> {o['effect']}" for o in orph]}

    @eng._add("set_backend_enabled", "Turn a whole backend on or off (the Pool / Routing page "
              "switch: config.yaml + pool rebuild, live). The card lists every enabled persona "
              "that would be left with no usable route.", "backend",
              {"name": S, "enabled": {"type": "boolean"}}, ("name", "enabled"),
              summary=lambda a: f"backend {a.get('name')}: turn {'on' if a.get('enabled') else 'off'}",
              check=_backend_check, preview=_backend_preview)
    async def set_backend_enabled(ctx: Ctx, a: dict):
        pv = _backend_preview(a, ctx)
        sid = _snapshot(ctx, f"backend {a['name']} {'on' if a['enabled'] else 'off'}")
        try:
            await router_ops.set_backend_enabled(ctx.svc, a["name"], a["enabled"],
                                                 f"{ctx.actor} via operator chat")
        except router_ops.OpError as e:
            raise ChatError(str(e), "invalid", e.status) from None
        eng._usage_cache = (0.0, None)
        return {"result": {"backend": a["name"], "enabled": a["enabled"], "snapshot": sid,
                           "personas_without_a_route": pv["orphans"]},
                "changed": [f"backend {a['name']} {'enabled' if a['enabled'] else 'disabled'}"]}

    # ---- the whole router: reads ----------------------------------------------------
    @eng._add("list_admin_endpoints", "Every router admin API endpoint (method, path, what it "
              "does), generated from the running app: the map of everything Foundry can show "
              "or change. Optional filter (a word in the path or description).", "read",
              {"filter": {"type": "string", "max": 80}})
    async def list_admin_endpoints(ctx: Ctx, a: dict):
        if eng.app is None:
            raise ChatError("the chat engine has no app", "no_app", 503)
        f = (a.get("filter") or "").lower()
        out = []
        for r in eng.app.routes:
            path = getattr(r, "path", "")
            if not path.startswith(ADMIN_PREFIX):
                continue
            doc = ((getattr(r, "endpoint", None) and r.endpoint.__doc__) or "").strip()
            doc = " ".join(doc.split())[:140]
            for m in sorted(getattr(r, "methods", None) or []):
                if m in ("HEAD", "OPTIONS"):
                    continue
                if f and f not in path.lower() and f not in doc.lower():
                    continue
                blocked = _admin_path_problem(path, m)
                out.append(f"{m} {path}" + (f" — {doc}" if doc else "")
                           + (" [not from chat]" if blocked else ""))
        return {"endpoints": out[:220], "count": len(out),
                "how": "admin_get for GET, admin_call for writes (confirm card); typed tools for "
                       "personas, backends, cutoffs, evals and research"}

    @eng._add("admin_get", "Read any router admin GET endpoint (e.g. /admin/api/perf/advisor, "
              "/admin/api/scoreboard, /admin/api/usage, /admin/api/models, /admin/api/insights, "
              "/admin/api/events). query = its query parameters. Secrets are scrubbed.", "read",
              {"path": {"type": "string", "max": 200}, "query": {"type": "object"}}, ("path",),
              check=lambda a, ctx: _admin_path_problem(a["path"], "GET"))
    async def admin_get(ctx: Ctx, a: dict):
        status, data = await admin_request(eng, "GET", a["path"], a.get("query"))
        if status >= 400:
            raise ChatError(f"{a['path']} -> {status}: {_compact(data, 300)}", "admin_error", 400)
        return data

    @eng._add("get_router_config", "The router's config.yaml (secrets redacted): one section "
              "(agent_brain, backend_pool, guardrails, budgets, turn_assist, media, perf, "
              "operator_chat, ...) or, with no section, the list of sections.", "read",
              {"section": {"type": "string", "max": 120}})
    async def get_router_config(ctx: Ctx, a: dict):
        raw = configops.redact(ctx.svc.config_store.raw)
        if not a.get("section"):
            return {"sections": {k: (sorted(v)[:30] if isinstance(v, dict) else type(v).__name__)
                                 for k, v in raw.items()},
                    "how": "get_router_config(section) for one; set_router_config to change"}
        v = _get_path(raw, a["section"])
        if v is None:
            raise ChatError(f"{a['section']} is not set in config.yaml (defaults apply)", "not_found",
                            404)
        return {a["section"]: v}

    # ---- the whole router: writes ---------------------------------------------------
    def _cfg_preview(a, ctx):
        _, rows, sections = plan_config(ctx.svc, a["changes"])
        live, restart = restart_forecast(sections)
        return {"config": rows, "sections": sections, "applies_live": live,
                "restart_needed": restart}

    @eng._add("set_router_config", "Change config.yaml settings: changes = {\"section.key\": "
              "value} (named list items by name, e.g. backend_pool.internal.backends.strata."
              "priority, agent_brain.worker_tool_max_steps). Validated before the card, saved "
              "as a restorable version (Config history), applied live where possible; the card "
              "says what needs a restart. Secrets are refused.", "router_config",
              {"changes": {"type": "object"}}, ("changes",),
              summary=lambda a: "config: " + ", ".join(
                  f"{k}={_compact(v, 50)}" for k, v in (a.get("changes") or {}).items()),
              check=_try(lambda a, ctx: plan_config(ctx.svc, a["changes"])),
              preview=_cfg_preview)
    async def set_router_config(ctx: Ctx, a: dict):
        new, rows, _ = plan_config(ctx.svc, a["changes"])
        sid = _snapshot(ctx, "config change")
        out = await configops.apply_raw(ctx.svc, new, source=f"operator chat ({ctx.actor})")
        eng._usage_cache = (0.0, None)
        return {"result": {**out, "changes": rows, "snapshot": sid,
                           "undo": "Dashboard -> Config history -> restore the version before"},
                "changed": [f"config: {', '.join(r['path'] for r in rows)}"
                            + (f" (restart needed: {', '.join(out['restart_needed'])})"
                               if out.get("restart_needed") else "")]}

    def _call_check(a, ctx):
        m = (a.get("method") or "POST").upper()
        if m not in ("POST", "PUT", "PATCH", "DELETE"):
            return "method must be POST, PUT, PATCH or DELETE (reads: admin_get)"
        return _admin_path_problem(a["path"], m)

    def _irreversible(a) -> bool:
        return (str(a.get("method") or "").upper() == "DELETE"
                or bool(IRREVERSIBLE.search(str(a.get("path") or ""))))

    @eng._add("admin_call", "Any other router admin write: method POST/PUT/PATCH/DELETE, an "
              "/admin/api/... path (list_admin_endpoints) and its JSON body — e.g. toggle a "
              "model or tool, change pricing, add an MCP server, probe a backend. Not for "
              "secrets (client keys, Meridian sign-in, MCP tokens). Deletes and clears cannot be "
              "undone and always need the card.", "admin",
              {"method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
               "path": {"type": "string", "max": 200}, "body": {"type": "any"}},
              ("method", "path"),
              summary=lambda a: f"{a.get('method')} {a.get('path')}"
              + (f" {_compact(a.get('body'), 120)}" if a.get("body") is not None else ""),
              check=_call_check, force_confirm=_irreversible,
              preview=lambda a, ctx: {"request": f"{a['method']} {a['path']}",
                                      "body": a.get("body"),
                                      "irreversible": _irreversible(a)})
    async def admin_call(ctx: Ctx, a: dict):
        sid = _snapshot(ctx, f"{a['method']} {a['path']}")
        status, data = await admin_request(eng, a["method"].upper(), a["path"], None,
                                           a.get("body") if a.get("body") is not None else {})
        if status >= 400:
            err = data.get("error") if isinstance(data, dict) else data
            raise ChatError(f"{a['path']} -> {status}: {_compact(err, 400)}", "admin_error", 400)
        return {"result": {"status": status, "response": data, "snapshot": sid},
                "changed": [f"{a['method']} {a['path']}"]}

    # ---- model research -------------------------------------------------------------
    def _unresearched(svc) -> list[dict]:
        served = set()
        try:
            served = set(svc.pool.available_models())
        except Exception:                                        # noqa: BLE001
            pass
        rows = svc.db.query("SELECT id, provider, research_status, research_note, enabled, source "
                            "FROM models ORDER BY id")
        return [{"model": r["id"], "status": r.get("research_status") or "never",
                 "note": (r.get("research_note") or "")[:120], "served_now": r["id"] in served}
                for r in rows if (r.get("research_status") or "never") in ("never", "failed")
                and r.get("enabled", 1) and r.get("source") != "openrouter_api"]

    @eng._add("research_status", "Models in the registry that were never researched (or whose "
              "research failed), whether each is served right now, and whether research can run "
              "(the research agent's prerequisites).", "read")
    async def research_status(ctx: Ctx, a: dict):
        svc = ctx.svc
        ready, why = (svc.research.prerequisites() if getattr(svc, "research", None)
                      else (False, "research agent not initialized"))
        todo = _unresearched(svc)
        return {"ready": ready, "reason": why, "count": len(todo), "models": todo[:60]}

    def _research_check(a, ctx):
        svc = ctx.svc
        if getattr(svc, "research", None) is None:
            return "the research agent is not running"
        ready, why = svc.research.prerequisites()
        if not ready:
            return f"research can't run right now: {why}"
        if not a.get("models") and not a.get("all_unresearched"):
            return "name models, or all_unresearched=true"
        return ""

    @eng._add("research_models", "Queue model research (the research agent searches the web "
              "and records benchmarks, strengths and tags): the named models, or every model "
              "never researched / failed that is served right now (all_unresearched).", "admin",
              {"models": {"type": "array", "max_items": 40},
               "all_unresearched": {"type": "boolean"}}, (),
              summary=lambda a: "research " + (", ".join(map(str, a.get("models") or []))
                                               or "every unresearched served model"),
              check=_research_check,
              preview=lambda a, ctx: {"models": [str(m) for m in a.get("models") or []]
                                      or [m["model"] for m in _unresearched(ctx.svc)
                                          if m["served_now"]]})
    async def research_models(ctx: Ctx, a: dict):
        svc = ctx.svc
        models = [str(m) for m in a.get("models") or []] or [
            m["model"] for m in _unresearched(svc) if m["served_now"]]
        queued, already = [], []
        for m in models:
            (queued if svc.research.enqueue(m) else already).append(m)
        return {"result": {"queued": queued, "already_queued": already,
                           "where": "Model Registry tab (research status per model)"},
                "changed": [f"research queued: {', '.join(queued) or 'none'}"]}

    # ---- performance ----------------------------------------------------------------
    @eng._add("performance_report", "How the router is performing over the last `hours` "
              "(default 24): requests and errors per persona, speed per model (tokens/s, time "
              "to first token, wall time), backend health, tool failures, the performance "
              "advisor's findings and brain metrics when available. Use it to answer 'how is "
              "performance' and to recommend changes.", "read", {"hours": {"type": "number"}})
    async def performance_report(ctx: Ctx, a: dict):
        return await perf_report(eng, float(a.get("hours") or 24))

    # ---- the chat's own backend -------------------------------------------------------
    @eng._add("get_chat_backend", "The operator chat's own model chain (which backend and model "
              "answer this chat; never a persona) and the backends/models it could use.", "read")
    async def get_chat_backend(ctx: Ctx, a: dict):
        return chat_backend_view(eng)

    def _chat_routes_check(a, ctx):
        return chat_routes_problem(ctx.svc, a.get("routes"))

    @eng._add("set_chat_backend", "Set the operator chat's own model chain: routes = ordered "
              "[{backend, model}] (first that answers wins; each call pinned to its backend; "
              "the budget gate still applies). [] = the built-in chain (Claude Sonnet, GPT-6.1 "
              "Sol, local Qwen). Saved in config.yaml (operator_chat.routes).", "router_config",
              {"routes": {"type": "array", "max_items": 6}}, ("routes",),
              summary=lambda a: "chat backend: " + (", ".join(
                  f"{r.get('model')}@{r.get('backend')}" for r in a.get("routes") or []
                  if isinstance(r, dict)) or "built-in chain"),
              check=_chat_routes_check,
              preview=lambda a, ctx: {"now": chat_backend_view(eng)["chain"],
                                      "after": [f"{r['model']}@{r['backend']}" for r in a["routes"]]
                                      or ["built-in chain"]})
    async def set_chat_backend(ctx: Ctx, a: dict):
        routes = await apply_chat_routes(eng, a["routes"], f"operator chat ({ctx.actor})")
        return {"result": {"chain": chat_backend_view(eng)["chain"]},
                "changed": ["chat backend: " + (", ".join(f"{r['model']}@{r['backend']}"
                                                          for r in routes) or "built-in chain")]}


# ---------------------------------------------------------------------------- chat backend

def chat_routes_problem(svc, routes: Any) -> str:
    if not isinstance(routes, list):
        return "routes are [{backend, model}] (an empty list = the built-in chain)"
    bmap = route_resolve.backends(svc)
    for r in routes:
        if not isinstance(r, dict) or not r.get("backend") or not r.get("model"):
            return "routes are [{backend, model}] (an empty list = the built-in chain)"
        if r["backend"] not in bmap:
            return f"no backend named {r['backend']!r}"
        if r["model"] not in bmap[r["backend"]]["models"]:
            return f"{r['backend']} does not serve {r['model']} (get_chat_backend lists them)"
    return ""


async def apply_chat_routes(eng, routes: list, source: str) -> list:
    """Save operator_chat.routes (validated, versioned in Config history)."""
    why = chat_routes_problem(eng.svc, routes)
    if why:
        raise ChatError(why)
    clean = [{"backend": r["backend"], "model": r["model"]} for r in routes]
    new, _, _ = plan_config(eng.svc, {"operator_chat.routes": clean})
    await configops.apply_raw(eng.svc, new, source=source)
    eng.svc.db.log_event("info", "chat", f"chat backend set by {source}: "
                         + (", ".join(f"{r['model']}@{r['backend']}" for r in clean)
                            or "built-in chain"))
    return clean


# ---------------------------------------------------------------------------- views

def chat_backend_view(eng) -> dict:
    svc = eng.svc
    bmap = route_resolve.backends(svc)
    chain = eng.chat_routes()
    configured = bool(svc.config_store.config.operator_chat.routes)
    return {"source": "operator_chat.routes" if configured else "built-in chain "
            f"({', '.join(PERSONA_CHAIN)})",
            "chain": [f"{m}@{b}" + ("" if (bmap.get(b) or {}).get("enabled", False) else " (backend off)")
                      for b, m in chain],
            "available": {n: {"family": route_resolve.FAMILY_LABEL.get(b["family"], b["family"]),
                              "enabled": b["enabled"], "healthy": b["healthy"],
                              "models": b["models"][:40]}
                          for n, b in bmap.items()}}


def _pct(vals: list, q: float) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))], 1)


async def perf_report(eng, hours: float) -> dict:
    svc = eng.svc
    hours = max(0.1, min(720.0, hours))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out: dict = {"hours": hours}
    reqs = svc.db.query("SELECT persona, status, outcome, duration_ms FROM request_log "
                        "WHERE ts >= ?", (since,))
    by: dict = {}
    for r in reqs:
        d = by.setdefault(r.get("persona") or "(no persona)", {"n": 0, "errors": 0, "ms": []})
        d["n"] += 1
        d["errors"] += int((r.get("status") or "ok") != "ok" or
                           (r.get("outcome") or "ok") not in ("ok", None, ""))
        d["ms"].append(r.get("duration_ms"))
    out["requests"] = {"total": len(reqs), "by_persona": sorted(
        [{"persona": k, "requests": v["n"], "errors": v["errors"],
          "p50_s": (_pct(v["ms"], .5) or 0) / 1000, "p95_s": (_pct(v["ms"], .95) or 0) / 1000}
         for k, v in by.items()], key=lambda x: -x["requests"])[:15]}
    try:
        samples = svc.db.query("SELECT model, backend, decode_tps, prefill_tps, ttft_ms, wall_ms, "
                               "prompt_tokens, completion_tokens FROM perf_samples WHERE ts >= ?",
                               (since,))
    except Exception:                                            # noqa: BLE001
        samples = []
    pm: dict = {}
    for s in samples:
        d = pm.setdefault(f"{s.get('model')}@{s.get('backend')}",
                          {"n": 0, "dec": [], "pre": [], "ttft": [], "wall": [], "pt": []})
        d["n"] += 1
        d["dec"].append(s.get("decode_tps"))
        d["pre"].append(s.get("prefill_tps"))
        d["ttft"].append(s.get("ttft_ms"))
        d["wall"].append(s.get("wall_ms"))
        d["pt"].append(s.get("prompt_tokens"))
    out["models"] = sorted([{"model": k, "calls": v["n"], "decode_tps_p50": _pct(v["dec"], .5),
                             "prefill_tps_p50": _pct(v["pre"], .5),
                             "ttft_s_p50": round((_pct(v["ttft"], .5) or 0) / 1000, 1),
                             "ttft_s_p95": round((_pct(v["ttft"], .95) or 0) / 1000, 1),
                             "wall_s_p95": round((_pct(v["wall"], .95) or 0) / 1000, 1),
                             "prompt_tokens_p95": _pct(v["pt"], .95)}
                            for k, v in pm.items()], key=lambda x: -x["calls"])[:15]
    out["backends"] = [{"name": b.get("name"), "enabled": b.get("enabled", True),
                        "healthy": b.get("healthy"), "last_error": (b.get("last_error") or "")[:120],
                        "busy": b.get("busy")} for b in svc.pool.backend_status()]
    try:
        tf = svc.db.query("SELECT server, tool, COUNT(*) AS n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) "
                          "AS failed FROM tool_call_log WHERE ts >= ? GROUP BY server, tool "
                          "ORDER BY failed DESC LIMIT 10", (since,))
        out["tools"] = [t for t in tf if t.get("failed")]
    except Exception:                                            # noqa: BLE001
        out["tools"] = []
    try:
        ge = svc.db.query("SELECT guardrail_events FROM request_log WHERE ts >= ? AND "
                          "guardrail_events LIKE '%round cap%'", (since,))
        out["tool_round_cap_hits"] = len(ge)
    except Exception:                                            # noqa: BLE001
        pass
    for key, path, q in (("advisor", "/admin/api/perf/advisor", None),
                         ("brain", "/admin/api/brain/metrics", {"hours": hours})):
        try:
            status, data = await admin_request(eng, "GET", path, q)
            if status < 400:
                out[key] = data
        except Exception:                                        # noqa: BLE001
            pass
    if "brain" not in out:
        out["brain"] = "brain calls are not recorded in this version"
    return out
