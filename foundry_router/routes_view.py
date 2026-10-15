"""Route view: the routing graph generated from the live configuration,
overlaid with what actually happened.

Columns: client -> persona -> model -> backend (image jobs drawn separately).

Edges
  config    what the configuration allows: a client's persona restriction, a
            persona's allowlist (in pinned order: the first is the primary
            route, the rest an explicit fallback chain), the backend a model
            is served by (the persona's pinned backend for strict routes)
  traffic   requests in the window (count, failures, mean duration)
  denied    budget-ledger denials (rule + reason)
  failed    requests that ended in error / timeout on that route
  image     image jobs (client -> image model -> backend)

Every traffic / denied / failed edge can be opened to the requests behind
it, and every request to its full trace (request row, model calls with
timing, ledger rows, MCP tool calls).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone


def _jl(v) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _since_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def graph(svc, hours: float = 24, show_all: bool = False) -> dict:
    from .facade.ollama_api import is_strict
    cfg = svc.config_store.config
    since_iso, since_ts = _since_iso(hours), time.time() - hours * 3600
    reqs = svc.db.query(
        "SELECT client_id, persona, client_model, mode, served_model, served_backend, outcome, "
        "status, duration_ms, request_id, routing FROM request_log WHERE ts >= ?", (since_iso,))
    denials = svc.db.query(
        "SELECT client_id, persona, model, backend, rule, detail FROM usage_events "
        "WHERE ts >= ? AND status='denied'", (since_ts,))
    try:
        jobs = svc.db.query("SELECT client_id, persona, model, backend, status FROM media_jobs "
                            "WHERE created_ts >= ?", (since_ts,))
    except Exception:                                             # noqa: BLE001
        jobs = []
    nodes: dict = {}
    edges: dict = {}

    def node(kind, name, **kw):
        nid = f"{kind}:{name}"
        n = nodes.setdefault(nid, {"id": nid, "kind": kind, "label": name, "count": 0,
                                   "errors": 0, **kw})
        for k, v in kw.items():
            n.setdefault(k, v)
        return nid

    def edge(kind, a, b, **kw):
        e = edges.setdefault((kind, a, b), {"kind": kind, "from": a, "to": b, "count": 0,
                                            "errors": 0, "ms": 0, **kw})
        return e

    personas = {p["virtual_name"]: p for p in svc.personas.list()}
    used_personas = {r["persona"] for r in reqs if r["persona"]} | \
        {d["persona"] for d in denials if d["persona"]}
    backends = getattr(svc.pool, "backends", None) or {}

    # traffic
    for r in reqs:
        client = r["client_id"] or "anonymous"
        persona = r["persona"] or ""
        c = node("client", client)
        nodes[c]["count"] += 1
        target = node("persona", persona) if persona else node("model", r["client_model"] or "?", raw=True)
        bad = (r["outcome"] or r["status"]) in ("error", "timeout")
        e = edge("traffic", c, target)
        e["count"] += 1
        e["errors"] += bad
        e["ms"] += r["duration_ms"] or 0
        nodes[target]["count"] += 1
        nodes[target]["errors"] += bad
        model = r["served_model"]
        if persona and model:
            m = node("model", model)
            e2 = edge("failed" if bad else "traffic", target, m)
            e2["count"] += 1
            e2["errors"] += bad
            e2["ms"] += r["duration_ms"] or 0
            nodes[m]["count"] += 1
        if model and r["served_backend"]:
            m = node("model", model)
            b = node("backend", r["served_backend"])
            e3 = edge("traffic", m, b)
            e3["count"] += 1
            e3["errors"] += bad
            nodes[b]["count"] += 1
            nodes[b]["errors"] += bad

    # denials
    for d in denials:
        src = node("persona", d["persona"]) if d["persona"] else node("client", d["client_id"] or "anonymous")
        m = node("model", d["model"] or "?")
        e = edge("denied", src, m)
        e["count"] += 1
        e.setdefault("rules", {})
        e["rules"][d["rule"]] = e["rules"].get(d["rule"], 0) + 1
        e["last_detail"] = d["detail"]

    # configuration
    clients_cfg = {c.name: c for c in cfg.clients}
    rules = cfg.budgets.rules if cfg.budgets.enabled else []
    for name, c in clients_cfg.items():
        cid = node("client", name, configured=True, enabled=c.enabled,
                   scopes=list(c.scopes or []), restricted=bool(c.personas))
        nodes[cid]["gates"] = [r.name for r in rules if r.enabled and _client_rule(r, name)]
        for p in c.personas or []:
            if p in personas:
                edge("config", cid, node("persona", p))
    for name, p in personas.items():
        enabled = bool(p.get("enabled"))
        relevant = show_all or name in used_personas or (enabled and is_strict(p)) or p.get("profile")
        if not relevant:
            continue
        strict = is_strict(p)
        kind = p.get("kind") or "chat"
        mode = ("image policy" if kind == "media" else
                "direct" if strict or (p.get("execution_mode") or "agent") == "direct"
                else (p.get("execution_mode") or "agent") + " (brain)")
        pinned_cfg = (backends.get((p.get("backend") or "").strip()) or None)
        persona_paid = None
        if pinned_cfg is not None:
            from .budget import is_paid
            persona_paid = is_paid(pinned_cfg.config)
        gates = [r.name for r in rules if r.enabled and _rule_may_cover(r, name)
                 and not ("paid" in (r.match or {}) and persona_paid is not None
                          and bool(r.match["paid"]) != persona_paid)]
        pid = node("persona", name, enabled=enabled, strict=strict, mode=mode, pkind=kind,
                   backend=p.get("backend"), gates=gates,
                   validation=(json.loads(p["validation"]).get("status")
                               if p.get("validation") else None))
        nodes[pid].update(enabled=enabled, strict=strict, mode=mode, pkind=kind,
                          backend=p.get("backend"), gates=gates)
        allow = [str(a) for a in _jl(p.get("model_allowlist"))]
        pins = [str(x) for x in _jl(p.get("pinned_models"))]
        order = [x for x in pins if x in allow] + [a for a in allow if a not in pins]
        for i, m in enumerate(order):
            mid = node("model", m)
            edge("config" if i == 0 else "fallback", pid, mid, order=i + 1)
            pinned = (p.get("backend") or "").strip()
            if pinned:
                bid = node("backend", pinned, missing=pinned not in backends)
                edge("config", mid, bid)
    # model -> backend from the pool (for models already on the graph)
    for nid, n in list(nodes.items()):
        if n["kind"] != "model":
            continue
        for s in backends.values():
            if n["label"] in s.models or n["label"] in (s.config.models or []):
                bid = node("backend", s.config.name)
                if not any(k[0] in ("config",) and k[1] == nid and k[2] == bid for k in edges):
                    edge("config", nid, bid)
    for s in backends.values():
        b = node("backend", s.config.name)
        nodes[b].update(healthy=s.healthy if s.ever_checked else None,
                        flavor=s.config.effective_flavor, priority=s.config.priority)
    for c in getattr(svc.pool, "disabled", []) or []:
        b = node("backend", c.name)
        nodes[b]["disabled"] = True
    # image jobs
    for j in jobs:
        c = node("client", j["client_id"] or "anonymous")
        m = node("model", j["model"] or "?", image=True)
        e = edge("image", c, m)
        e["count"] += 1
        e["errors"] += j["status"] == "failed"
        if j["backend"]:
            b = node("backend", j["backend"])
            e2 = edge("image", m, b)
            e2["count"] += 1
    for e in edges.values():
        e["avg_ms"] = round(e["ms"] / e["count"]) if e["count"] and e["ms"] else None
        e.pop("ms", None)
    # health status for backends
    try:
        st = {h["backend"]: h["status"] for h in svc.health.status()}
        for n in nodes.values():
            if n["kind"] == "backend":
                n["health"] = st.get(n["label"], "disabled" if n.get("disabled") else None)
    except Exception:                                             # noqa: BLE001
        pass
    return {"hours": hours, "nodes": list(nodes.values()), "edges": list(edges.values()),
            "requests": len(reqs), "denials": len(denials), "image_jobs": len(jobs)}


def _rule_may_cover(rule, persona: str) -> bool:
    """Whether a budget rule gates this persona: its persona match (if any)
    matches the name. Client-scoped rules are shown on the client instead."""
    import fnmatch
    if "client" in (rule.match or {}):
        return False
    pat = (rule.match or {}).get("persona")
    if pat is None:
        return True
    pats = pat if isinstance(pat, list) else [pat]
    return any(fnmatch.fnmatchcase(persona, str(x)) for x in pats)


def _client_rule(rule, client: str) -> bool:
    import fnmatch
    pat = (rule.match or {}).get("client")
    if pat is None:
        return False
    pats = pat if isinstance(pat, list) else [pat]
    return any(fnmatch.fnmatchcase(client, str(x)) for x in pats)


def edge_requests(svc, kind: str, a: str, b: str, hours: float = 24, limit: int = 50) -> list:
    """The requests (or denials) behind one edge of the graph."""
    ka, va = a.split(":", 1)
    kb, vb = b.split(":", 1)
    if kind == "denied":
        where, args = ["status='denied'", "ts >= ?"], [time.time() - hours * 3600]
        col = {"persona": "persona", "client": "client_id"}[ka]
        where.append(f"{col}=?")
        args.append(va)
        where.append("model=?")
        args.append(vb)
        rows = svc.db.query("SELECT * FROM usage_events WHERE " + " AND ".join(where) +
                            " ORDER BY id DESC LIMIT ?", (*args, limit))
        return [{"ts": datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(),
                 "request_id": r["request_id"], "client": r["client_id"], "persona": r["persona"],
                 "model": r["model"], "backend": r["backend"], "outcome": "denied",
                 "error": f"{r['rule']}: {r['detail']}"} for r in rows]
    if kind == "image":
        try:
            rows = svc.db.query(
                "SELECT * FROM media_jobs WHERE created_ts >= ? AND " +
                ("client_id=? AND model=?" if ka == "client" else "model=? AND backend=?") +
                " ORDER BY created_ts DESC LIMIT ?",
                (time.time() - hours * 3600, va, vb, limit))
        except Exception:                                         # noqa: BLE001
            return []
        return [{"ts": r["created_at"], "request_id": r["id"], "client": r["client_id"],
                 "persona": r["persona"], "model": r["model"], "backend": r["backend"],
                 "outcome": r["status"], "duration_ms": r["duration_ms"], "error": r["error"]}
                for r in rows]
    where, args = ["ts >= ?"], [_since_iso(hours)]
    for k, v in ((ka, va), (kb, vb)):
        if k == "client":
            where.append("client_id=?")
        elif k == "persona":
            where.append("persona=?")
        elif k == "model":
            where.append("(served_model=? OR (persona IS NULL OR persona='') AND client_model=?)")
            args.append(v)
        elif k == "backend":
            where.append("served_backend=?")
        args.append(v)
    if kind == "failed":
        where.append("outcome IN ('error','timeout')")
    rows = svc.db.query("SELECT * FROM request_log WHERE " + " AND ".join(where) +
                        " ORDER BY id DESC LIMIT ?", (*args, limit))
    return [_req_view(r) for r in rows]


def _req_view(r: dict) -> dict:
    used = _jl(r.get("models_used"))
    return {"ts": r["ts"], "request_id": r.get("request_id"), "client": r.get("client_id"),
            "session": r.get("session_id"), "task": r.get("task_id"),
            "persona": r.get("persona"), "model": r.get("served_model") or r.get("client_model"),
            "backend": r.get("served_backend"), "mode": r.get("mode"), "routing": r.get("routing"),
            "outcome": r.get("outcome") or r.get("status"), "duration_ms": r.get("duration_ms"),
            "attempts": r.get("attempts"), "retries": r.get("retries"),
            "input_tokens": sum(int(u.get("prompt_tokens") or 0) for u in used),
            "output_tokens": sum(int(u.get("completion_tokens") or 0) for u in used),
            "error": r.get("error") or None, "summary": r.get("summary")}


def trace(svc, request_id: str) -> dict:
    """Everything recorded for one request, as a timeline."""
    req = svc.db.query_one("SELECT * FROM request_log WHERE request_id=? ORDER BY id DESC LIMIT 1",
                           (request_id,))
    calls = svc.db.query(
        "SELECT ts, model, backend, prompt_tokens, completion_tokens, cached_tokens, ttft_ms, "
        "prefill_ms, decode_ms, decode_tps, prefill_tps, wall_ms, finish_reason, load_ms, "
        "router_ms FROM perf_samples WHERE request_id=? ORDER BY id", (request_id,))
    ledger = svc.db.query(
        "SELECT ts, settled_ts, backend, model, account, status, input_tokens, output_tokens, "
        "rule, detail FROM usage_events WHERE request_id=? ORDER BY id", (request_id,))
    try:
        tools = svc.db.query(
            "SELECT ts, source, server, tool, ok, duration_ms, attempts, timed_out, error "
            "FROM mcp_call_log WHERE request_id=? ORDER BY id", (request_id,))
    except Exception:                                             # noqa: BLE001
        tools = []
    if req is None and not calls and not ledger:
        return {}
    out = {"request_id": request_id, "request": _req_view(req) if req else None,
           "guardrail_events": _jl(req.get("guardrail_events")) if req else [],
           "flags": json.loads(req["flags"]) if req and req.get("flags") else {},
           "model_calls": calls, "tool_calls": tools,
           "ledger": [{**l, "ts": datetime.fromtimestamp(l["ts"], timezone.utc).isoformat(),
                       "ms": round((l["settled_ts"] - l["ts"]) * 1000) if l["settled_ts"] else None}
                      for l in ledger]}
    return out
