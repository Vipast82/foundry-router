"""The one-chat-persona setup (v0.181.140).

Personas used to be one per kind of work (chat, agent, research, RAG, vision,
creative). With main-model mode one persona does all of it: every tool server
attached, tools found on demand, the main model deciding what it needs. What
still differs between personas is WHERE a chat may be answered:

* Foundry-Chat   the main local model first, paid models when it is busy or fails
* Foundry-Local  local models only (nothing leaves the house)
* Foundry-Max    a frontier model first

`apply` moves an install to that set. It never deletes: a retired persona is
switched off and recorded in the `personas.retired` kv entry with the persona
that answers in its place, so a chat program still asking for the old name
keeps working (PersonaStore.resolve) and the starter seeder does not bring a
deleted one back. `remove_retired` is the delete, and only the operator's
click runs it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

MAIN, LOCAL, MAX = "Foundry-Chat", "Foundry-Local", "Foundry-Max"
RETIRE = ("Foundry-Agent", "Foundry-Coding", "Foundry-Creative", "Foundry-RAG",
          "Foundry-Research", "Foundry-Vision")
KV = "personas.retired"


def retired(db) -> dict:
    """{retired persona name: the persona that answers for it ("" = none)}."""
    try:
        raw = db.kv_get(KV)
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:                                             # noqa: BLE001
        return {}


def _save_retired(db, data: dict) -> None:
    db.kv_set(KV, json.dumps(data, sort_keys=True))


def forget(db, name: str) -> None:
    """A persona the operator deleted by hand: a starter is not seeded again."""
    data = retired(db)
    data.setdefault(name, "")
    _save_retired(db, data)


def _jl(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def traffic(db, days: int = 30) -> dict:
    """{persona: {"requests": n, "last": iso}} over the last `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    read = getattr(db, "ro_query", None) or db.query
    rows = read("SELECT persona, COUNT(*) AS n, MAX(ts) AS last FROM request_log "
                "WHERE ts >= ? GROUP BY persona", (since,))
    return {r["persona"]: {"requests": int(r["n"] or 0), "last": r["last"] or ""}
            for r in rows if r.get("persona")}


def _is_local(svc, model_id: str) -> Optional[bool]:
    info = svc.pool.backend_info(model_id)
    if not info:
        return None
    from .facade.ollama_api import _is_paid_backend
    return not _is_paid_backend(info)


def _split_route(svc, pins: list) -> tuple[list, list]:
    local, paid = [], []
    for m in pins:
        (local if _is_local(svc, m) else paid).append(m)        # unknown counts as paid
    return local, paid


def plan(svc, days: int = 30) -> dict:
    """What `apply` would do, with each persona's recent traffic. Read-only."""
    used = traffic(svc.db, days)
    gone = retired(svc.db)
    main = svc.personas.get(MAIN)
    pins = [str(p) for p in _jl((main or {}).get("pinned_models"))]
    local, paid = _split_route(svc, pins)
    rows = []
    for name in RETIRE:
        p = svc.personas.get(name)
        u = used.get(name) or {}
        rows.append({"name": name, "exists": p is not None,
                     "enabled": bool(p and p.get("enabled", 1)),
                     "requests": u.get("requests", 0), "last_used": u.get("last", ""),
                     "retired": name in gone})
    return {
        "days": days,
        "main": {"name": MAIN, "exists": main is not None,
                 "mode": (main or {}).get("execution_mode") or "agent",
                 "is_main": ((main or {}).get("execution_mode") or "") == "main",
                 "route": pins, "requests": (used.get(MAIN) or {}).get("requests", 0)},
        "variants": [
            {"name": LOCAL, "exists": svc.personas.get(LOCAL) is not None,
             "route": local, "what": "local models only; paid calls are never allowed"},
            {"name": MAX, "exists": svc.personas.get(MAX) is not None,
             "route": paid + local, "what": "paid models first, local last"}],
        "retire": rows,
        "retired": gone,
        "applied": bool(main) and ((main or {}).get("execution_mode") or "") == "main"
        and svc.personas.get(LOCAL) is not None and svc.personas.get(MAX) is not None,
    }


def apply(svc, retire: Optional[list] = None, actor: str = "operator") -> dict:
    """Switch to the one-chat-persona set. `retire` = the names to switch off
    (default: every persona in RETIRE that exists). Nothing is deleted."""
    from .personas import PERSONA_FIELDS
    done: list[str] = []
    main = svc.personas.get(MAIN)
    if main is None:
        svc.personas.upsert(
            MAIN, description="The one chat persona: every tool on demand; the main model "
                              "answers and asks for help when it needs it.",
            benchmark_category="general_chat", local_bias_strength="cost_aware_default",
            preferred_mcp_tools=["*"], mcp_tool_mode="finder", execution_mode="main",
            escalate_when_local_busy=1, expose_tool_trail=1, enabled=1,
            capabilities=["text", "tools", "image_input"])
        done.append(f"{MAIN} created in main-model mode")
        main = svc.personas.get(MAIN)
    else:
        fields = {}
        if (main.get("execution_mode") or "") != "main":
            fields["execution_mode"] = "main"
        if not _jl(main.get("preferred_mcp_tools")):
            fields.update(preferred_mcp_tools=["*"], mcp_tool_mode="finder")
        if not main.get("enabled", 1):
            fields["enabled"] = 1
        if fields:
            svc.personas.upsert(MAIN, **fields)
            done.append(f"{MAIN}: " + ", ".join(sorted(fields)) + " set")
            main = svc.personas.get(MAIN)
    pins = [str(p) for p in _jl(main.get("pinned_models"))]
    local, paid = _split_route(svc, pins)
    base = {k: main.get(k) for k in PERSONA_FIELDS}
    if svc.personas.get(LOCAL) is None:
        f = dict(base)
        guard = {}
        try:
            guard = json.loads(main.get("guardrail_overrides") or "{}") or {}
        except (ValueError, TypeError):
            guard = {}
        guard["max_paid_calls_per_request"] = 0
        f.update(description="Local models only: nothing in this chat leaves the house. "
                             "Same tools as Foundry-Chat.",
                 pinned_models=local, model_allowlist=local, guardrail_overrides=guard,
                 prefer_permissive=1, local_bias_strength="strong",
                 escalate_when_local_busy=0, execution_mode="main", enabled=1)
        svc.personas.upsert(LOCAL, **f)
        done.append(f"{LOCAL} created (route: {', '.join(local) or 'any local model'})")
    if svc.personas.get(MAX) is None:
        f = dict(base)
        f.update(description="Frontier model first, local last. Same tools as Foundry-Chat.",
                 pinned_models=paid + local, local_bias_strength="prefer_paid",
                 execution_mode="main", enabled=1)
        svc.personas.upsert(MAX, **f)
        done.append(f"{MAX} created (route: {', '.join(paid + local) or 'ranked'})")
    gone = retired(svc.db)
    names = [n for n in (RETIRE if retire is None else retire) if n in RETIRE]
    for name in names:
        p = svc.personas.get(name)
        if p is None:
            continue
        if p.get("enabled", 1):
            svc.personas.upsert(name, enabled=0)
            done.append(f"{name} switched off; chats asking for it are answered by {MAIN}")
        gone[name] = MAIN
    _save_retired(svc.db, gone)
    svc.db.log_event("info", "admin", f"one-chat-persona setup applied by {actor}",
                     "; ".join(done) or "nothing to change")
    return {"ok": True, "changes": done, "at": time.time()}


def restore(svc, name: str) -> bool:
    """Undo one retirement: the persona is switched on again and answers for itself."""
    gone = retired(svc.db)
    p = svc.personas.get(name)
    if name not in gone and p is None:
        return False
    gone.pop(name, None)
    _save_retired(svc.db, gone)
    if p is not None and not p.get("enabled", 1):
        svc.personas.upsert(name, enabled=1)
    svc.db.log_event("info", "admin", f"persona {name} restored")
    return True


def remove_retired(svc) -> list[str]:
    """Delete the retired personas that are still switched off. Their names keep
    answering through the persona recorded for them."""
    out = []
    for name in retired(svc.db):
        p = svc.personas.get(name)
        if p is not None and not p.get("enabled", 1):
            svc.personas.delete(p["virtual_name"])
            out.append(p["virtual_name"])
    if out:
        svc.db.log_event("info", "admin", "retired personas removed", ", ".join(out))
    return out
