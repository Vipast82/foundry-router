"""Client comparison (e.g. Kilo vs Cline) from what Foundry itself observed.

Grouped by client key (or persona), over a window:
  volume      requests, sessions, requests per session
  outcomes    ok / error / timeout / cancelled / denied rates, retries
  speed       end-to-end duration (median, p90), time to first token,
              decode tokens/s (decode-only, never mixed with end-to-end)
  usage       input/output tokens, paid (frontier) calls, frontier calls per
              session, compactions, context size per session
  tool loop   tool calls the model returned to the client, immediately
              repeated tool calls, longest error streak in a session

Interventions, failed edits, acceptance gates and time to a verified result
happen inside the client; Foundry can't see them, and the report says so
rather than guessing.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta, timezone

NOT_OBSERVABLE = ["human interventions", "failed file edits", "acceptance-gate results",
                  "time to a verified result", "reopened issues"]


def _pct(values, q):
    if not values:
        return None
    vals = sorted(values)
    k = max(0, min(len(vals) - 1, round(q * (len(vals) - 1))))
    return vals[k]


def _jl(v):
    try:
        return json.loads(v) if v else None
    except (TypeError, ValueError):
        return None


def report(svc, hours: float = 168, group: str = "client") -> dict:
    col = {"client": "client_id", "persona": "persona"}.get(group, "client_id")
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = svc.db.query(
        f"SELECT id, {col} AS g, session_id, request_id, outcome, status, duration_ms, retries, "
        f"models_used, flags FROM request_log WHERE ts >= ? ORDER BY id", (since_iso,))
    perf = svc.db.query(
        "SELECT p.request_id, p.ttft_ms, p.decode_tps, p.prompt_tokens, p.timing_src, r.g "
        "FROM perf_samples p "
        f"JOIN (SELECT request_id, {col} AS g FROM request_log WHERE ts >= ?) r "
        "ON r.request_id = p.request_id WHERE p.ts >= ?", (since_iso, since_iso))
    ledger = svc.db.query(
        f"SELECT {'client_id' if group == 'client' else 'persona'} AS g, session_id, paid, status "
        "FROM usage_events WHERE ts >= ? AND status IN ('settled','error')",
        (time.time() - hours * 3600,))
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["g"] or "(none)", []).append(r)
    out = []
    for g, rs in groups.items():
        sessions: dict = {}
        for r in rs:
            sessions.setdefault(r["session_id"] or f"(no session) {g}", []).append(r)
        outcome = {}
        for r in rs:
            k = r["outcome"] or r["status"] or "?"
            outcome[k] = outcome.get(k, 0) + 1
        durations = [r["duration_ms"] for r in rs if r["duration_ms"] is not None
                     and (r["outcome"] or r["status"]) == "ok"]
        tin = tout = 0
        for r in rs:
            for u in _jl(r["models_used"]) or []:
                tin += int(u.get("prompt_tokens") or 0)
                tout += int(u.get("completion_tokens") or 0)
        compactions = tool_calls = repeated = 0
        streak_max = 0
        for srows in sessions.values():
            prev_tools, streak = None, 0
            for r in srows:
                fl = _jl(r["flags"]) or {}
                compactions += bool(fl.get("compaction"))
                tools = fl.get("tool_calls_out") or []
                tool_calls += len(tools)
                if tools and prev_tools and tools[0] == prev_tools[0]:
                    repeated += 1
                prev_tools = tools or prev_tools
                bad = (r["outcome"] or r["status"]) in ("error", "timeout")
                streak = streak + 1 if bad else 0
                streak_max = max(streak_max, streak)
        p = [x for x in perf if (x["g"] or "(none)") == g]
        # speeds from the backend's own timings only: a router estimate (Claude, GPT)
        # is a different quantity and never averaged in (perf_history.SPEED_KEYS)
        ttft = [x["ttft_ms"] for x in p if x["ttft_ms"] and x["timing_src"] == "server"]
        dtps = [x["decode_tps"] for x in p if x["decode_tps"] and x["timing_src"] == "server"]
        ctx_by_req: dict = {}
        for x in p:
            ctx_by_req[x["request_id"]] = max(ctx_by_req.get(x["request_id"], 0), x["prompt_tokens"] or 0)
        req_session = {r["request_id"]: (r["session_id"] or "") for r in rs}
        ctx_by_session: dict = {}
        for rid, c in ctx_by_req.items():
            s = req_session.get(rid, "")
            ctx_by_session[s] = max(ctx_by_session.get(s, 0), c)
        lg = [x for x in ledger if (x["g"] or "(none)") == g]
        paid_calls = sum(1 for x in lg if x["paid"])
        real_sessions = [s for s in sessions if not s.startswith("(no session)")]
        n = len(rs)
        out.append({
            "group": g, "requests": n,
            "sessions": len(real_sessions) or None,
            "requests_per_session": round(n / len(real_sessions), 1) if real_sessions else None,
            "outcomes": outcome,
            "ok_rate": round(100 * outcome.get("ok", 0) / n, 1) if n else None,
            "error_rate": round(100 * (outcome.get("error", 0) + outcome.get("timeout", 0)) / n, 1) if n else None,
            "cancel_rate": round(100 * outcome.get("cancelled", 0) / n, 1) if n else None,
            "denied": outcome.get("denied", 0),
            "retries": sum(r["retries"] or 0 for r in rs),
            "duration_ms_median": _pct(durations, 0.5), "duration_ms_p90": _pct(durations, 0.9),
            "ttft_ms_median": _pct(ttft, 0.5), "decode_tps_median": (round(statistics.median(dtps), 1) if dtps else None),
            "input_tokens": tin, "output_tokens": tout,
            "model_calls": len(lg) or None, "paid_calls": paid_calls,
            "paid_calls_per_session": round(paid_calls / len(real_sessions), 2) if real_sessions else None,
            "compactions": compactions,
            "context_tokens_median_max": _pct([c for c in ctx_by_session.values() if c], 0.5),
            "tool_calls_out": tool_calls, "repeated_tool_calls": repeated,
            "error_streak_max": streak_max,
        })
    out.sort(key=lambda e: -e["requests"])
    return {"hours": hours, "group": group, "groups": out, "not_observable": NOT_OBSERVABLE,
            "notes": {
                "sessions": "from the x-foundry-session header; clients that don't send it show no sessions",
                "speed": "duration = end-to-end request time; decode t/s = generation speed only "
                         "(never compare the two)",
                "repeated_tool_calls": "the model asked the client for the same tool as its previous "
                                       "turn in the same session",
                "paid_calls": "model calls to subscription / paid backends, from the budget ledger"}}
