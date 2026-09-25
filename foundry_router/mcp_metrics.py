"""MCP tool-usage metrics — the data behind the MCP Metrics tab.

Every MCP call that passes through Foundry (brain, worker loop, direct-mode
persona tools, the Foundry-MCP aggregator used by external clients, research,
gateway admin) lands in mcp_call_log via MCPManager.call_tool_rich. This module
aggregates it for performance and debugging:

  * latency (p50 / p95 / max), error / timeout / 429 rates per tool and server
  * connection cost (session setup time, pooled-session reuse rate)
  * context cost of RESULTS (tokens a tool's output adds to the model's context)
  * who calls what (source + persona / aggregator scope + external client)

and computes the context cost of tool DEFINITIONS per persona / endpoint — the
number behind "this MCP server has 57 tools enabled that will consume context
in every chat".
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .db import Database


def _where(hours: Optional[float], source: Optional[str], server: Optional[str],
           tool: Optional[str], ok: Optional[bool] = None) -> tuple[str, list]:
    w, p = [], []
    if hours:
        w.append("datetime(ts) >= datetime('now', ?)")
        p.append(f"-{hours} hours")
    if source:
        w.append("source = ?")
        p.append(source)
    if server:
        w.append("server = ?")
        p.append(server)
    if tool:
        w.append("tool = ?")
        p.append(tool)
    if ok is not None:
        w.append("ok = ?")
        p.append(1 if ok else 0)
    return (" AND ".join(w) or "1=1"), p


def _pct(vals: list, q: float) -> Optional[int]:
    if not vals:
        return None
    vals = sorted(vals)
    return int(vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))])


def _group_stats(rows: list[dict]) -> dict:
    durs = [r["duration_ms"] for r in rows if r.get("duration_ms") is not None]
    n = len(rows)
    errs = sum(1 for r in rows if not r.get("ok"))
    toks = [r.get("result_tokens") or 0 for r in rows if r.get("ok")]
    return {
        "calls": n, "errors": errs,
        "error_pct": round(100.0 * errs / n, 1) if n else 0.0,
        "timeouts": sum(1 for r in rows if r.get("timed_out")),
        "rate_limited": sum(1 for r in rows if r.get("rate_limited")),
        "p50_ms": _pct(durs, 0.5), "p95_ms": _pct(durs, 0.95),
        "max_ms": max(durs) if durs else None,
        "avg_ms": round(sum(durs) / len(durs)) if durs else None,
        "avg_result_tokens": round(sum(toks) / len(toks)) if toks else None,
        "max_result_tokens": max(toks) if toks else None,
        "total_result_tokens": sum(toks),
    }


def summary(db: Database, hours: float = 24, source: Optional[str] = None,
            server: Optional[str] = None) -> dict:
    clause, params = _where(hours, source, server, None)
    rows = db.query(
        "SELECT ts, source, caller, client, server, tool, ok, duration_ms, connect_ms, "
        "pace_ms, attempts, rate_limited, timed_out, session, error, result_tokens, "
        f"content_types FROM mcp_call_log WHERE {clause} ORDER BY id", tuple(params))

    totals = _group_stats(rows)
    sessions = [r for r in rows if r.get("session")]
    totals["reuse_pct"] = (round(100.0 * sum(1 for r in sessions if r["session"] == "reused")
                                 / len(sessions), 1) if sessions else None)
    conn = [r["connect_ms"] for r in rows if r.get("connect_ms")]
    totals["avg_connect_ms"] = round(sum(conn) / len(conn)) if conn else None
    pace = [r["pace_ms"] for r in rows if r.get("pace_ms")]
    totals["avg_pace_ms"] = round(sum(pace) / len(pace)) if pace else None

    def grouped(keyf) -> list[dict]:
        g: dict = {}
        for r in rows:
            g.setdefault(keyf(r), []).append(r)
        return [(k, v) for k, v in g.items()]

    by_tool = []
    for (srv, tl), rs in grouped(lambda r: (r["server"], r["tool"])):
        last_err = next((r["error"] for r in reversed(rs) if r.get("error")), "")
        by_tool.append({"server": srv, "tool": tl, **_group_stats(rs),
                        "last_used": rs[-1]["ts"], "last_error": last_err,
                        "sources": sorted({r["source"] for r in rs if r.get("source")}),
                        "content_types": sorted({t for r in rs
                                                 for t in (r.get("content_types") or "").split(",") if t})})
    by_tool.sort(key=lambda x: -x["calls"])

    by_server = []
    for srv, rs in grouped(lambda r: r["server"]):
        conn = [r["connect_ms"] for r in rs if r.get("connect_ms")]
        sess = [r for r in rs if r.get("session")]
        by_server.append({"server": srv, **_group_stats(rs),
                          "tools_used": len({r["tool"] for r in rs}),
                          "avg_connect_ms": round(sum(conn) / len(conn)) if conn else None,
                          "reuse_pct": (round(100.0 * sum(1 for r in sess if r["session"] == "reused")
                                              / len(sess), 1) if sess else None)})
    by_server.sort(key=lambda x: -x["calls"])

    by_source = []
    for (src, caller, client), rs in grouped(lambda r: (r.get("source") or "other",
                                                        r.get("caller") or "",
                                                        r.get("client") or "")):
        by_source.append({"source": src, "caller": caller, "client": client, **_group_stats(rs),
                          "last_used": rs[-1]["ts"]})
    by_source.sort(key=lambda x: -x["calls"])

    # Hourly buckets for the chart (chronological).
    buckets: dict = {}
    for r in rows:
        k = (r["ts"] or "")[:13]
        b = buckets.setdefault(k, {"ts": k + ":00:00", "calls": 0, "errors": 0, "_d": []})
        b["calls"] += 1
        b["errors"] += 0 if r.get("ok") else 1
        if r.get("duration_ms") is not None:
            b["_d"].append(r["duration_ms"])
    series = []
    for k in sorted(buckets):
        b = buckets[k]
        d = b.pop("_d")
        b["p50_ms"] = _pct(d, 0.5)
        b["p95_ms"] = _pct(d, 0.95)
        series.append(b)

    wc, wp = _where(hours, None, None, None)
    sources = [r["source"] for r in db.query(
        f"SELECT DISTINCT source FROM mcp_call_log WHERE {wc} ORDER BY source", tuple(wp))]
    servers = [r["server"] for r in db.query(
        f"SELECT DISTINCT server FROM mcp_call_log WHERE {wc} ORDER BY server", tuple(wp))]
    return {"hours": hours, "totals": totals, "by_tool": by_tool, "by_server": by_server,
            "by_source": by_source, "series": series, "sources": sources, "servers": servers}


def recent(db: Database, limit: int = 100, hours: Optional[float] = None,
           source: Optional[str] = None, server: Optional[str] = None,
           tool: Optional[str] = None, errors_only: bool = False) -> list[dict]:
    clause, params = _where(hours, source, server, tool, False if errors_only else None)
    return db.query(f"SELECT * FROM mcp_call_log WHERE {clause} ORDER BY id DESC LIMIT ?",
                    (*params, max(1, min(int(limit), 1000))))


def clear(db: Database, server: Optional[str] = None) -> int:
    clause, params = _where(None, None, server, None)
    n = (db.query_one(f"SELECT COUNT(*) AS n FROM mcp_call_log WHERE {clause}",
                      tuple(params)) or {}).get("n") or 0
    if n:
        db.execute(f"DELETE FROM mcp_call_log WHERE {clause}", tuple(params))
    return int(n)


def _spec_tokens(specs: list[dict]) -> int:
    """~tokens the tool definitions add to EVERY request (chars / 4 of the JSON
    schema the model is sent — a close, conservative estimate)."""
    try:
        return (len(json.dumps(specs, ensure_ascii=False)) + 3) // 4
    except (TypeError, ValueError):
        return 0


def context_budget(svc: Any, context_tokens: Optional[int] = None) -> dict:
    """Context cost of tool definitions per persona and per aggregator endpoint.
    `context_tokens` (e.g. 262144) turns each cost into a % of the window."""
    reg = svc.tool_registry
    all_mcp = [t for t in reg.enabled() if t.kind == "mcp" and not t.disabled]

    def entry(name: str, kind: str, tools: list, where: str) -> dict:
        specs = [t.spec() for t in tools]
        tok = _spec_tokens(specs)
        per_tool = sorted(({"tool": t.name, "server": t.server,
                            "tokens": _spec_tokens([t.spec()])} for t in tools),
                          key=lambda x: -x["tokens"])
        return {"name": name, "kind": kind, "where": where, "tools": len(tools),
                "servers": sorted({t.server for t in tools if t.server}),
                "tokens": tok,
                "pct": round(100.0 * tok / context_tokens, 2) if context_tokens else None,
                "heaviest": per_tool[:5]}

    out = [entry("all Foundry MCP tools", "aggregator", all_mcp, "aggregator base endpoint")]
    cfg = svc.config_store.config.mcp_aggregator
    for pname, servers in (cfg.profiles or {}).items():
        out.append(entry(pname, "profile", [t for t in all_mcp if t.server in set(servers)],
                         "aggregator profile endpoint"))
    for p in svc.personas.list(enabled_only=True):
        tools = reg.mcp_tools_for_persona(p)
        if tools:
            modes = []
            if (p.get("execution_mode") or "agent") != "direct":
                modes.append("worker / brain")
            if p.get("mcp_tools_in_direct") is None or int(p.get("mcp_tools_in_direct") or 0):
                modes.append("direct (merged with client tools)")
            out.append(entry(p["virtual_name"], "persona", tools,
                             "persona: " + ", ".join(modes) + " · aggregator persona endpoint"))
    per_tool_all = sorted(({"tool": t.name, "server": t.server,
                            "tokens": _spec_tokens([t.spec()])} for t in all_mcp),
                          key=lambda x: -x["tokens"])
    return {"context_tokens": context_tokens, "scopes": out, "tools": per_tool_all}
