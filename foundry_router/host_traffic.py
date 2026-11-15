"""What the router itself sent to one backend (v0.181.156).

Host Admin shows what a server reports about itself, and servers differ a lot in
how much that is: Strata reports nearly everything, a generic OpenAI-compatible
endpoint or a subscription proxy only a model list. The router's own record of
the calls it made (perf_samples, one row per completed model call) is the same
for every kind of backend, so every Host Admin page can show the same view:
calls and tokens over the last day, speed, and which models were used.

Read-only, from the performance history; nothing is asked of the backend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MODELS = 8


def _r(v: Any, d: int = 1):
    return round(v, d) if isinstance(v, (int, float)) else None


def traffic(db, backend: str, hours: int = 24) -> dict:
    """Totals, one bucket per hour (oldest first, empty hours included) and the
    most used models for `backend` over the last `hours`."""
    hours = max(1, min(int(hours or 24), 24 * 14))
    where = "backend = ? AND datetime(ts) >= datetime('now', ?)"
    args = [backend, f"-{hours} hours"]
    q = getattr(db, "ro_query", None) or db.query
    tot = (q("SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
             "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
             "COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
             "AVG(ttft_ms) AS ttft_ms, AVG(eff_tps) AS eff_tps, "
             # the engine's own decode speed only where the server timed it: for a
             # subscription backend it is derived from the stream and means nothing
             "AVG(CASE WHEN timing_src = 'server' THEN decode_tps END) AS decode_tps, "
             "AVG(wall_ms) AS wall_ms, MAX(ts) AS last_ts, "
             "SUM(CASE WHEN finish_reason = 'length' THEN 1 ELSE 0 END) AS cut_short "
             f"FROM perf_samples WHERE {where}", args) or [{}])[0]
    rows = q("SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS calls, "
             "COALESCE(SUM(completion_tokens),0) AS completion_tokens "
             f"FROM perf_samples WHERE {where} GROUP BY hour", args)
    by_hour = {r["hour"]: r for r in rows}
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(hours - 1, -1, -1):
        key = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H")
        r = by_hour.get(key) or {}
        buckets.append({"hour": key + ":00:00Z", "calls": r.get("calls") or 0,
                        "completion_tokens": r.get("completion_tokens") or 0})
    models = q("SELECT model, COUNT(*) AS calls, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
               "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
               "AVG(ttft_ms) AS ttft_ms, AVG(eff_tps) AS eff_tps "
               f"FROM perf_samples WHERE {where} GROUP BY model ORDER BY calls DESC LIMIT {MODELS}", args)
    return {
        "backend": backend, "hours": hours,
        "calls": tot.get("calls") or 0,
        "prompt_tokens": tot.get("prompt_tokens") or 0,
        "completion_tokens": tot.get("completion_tokens") or 0,
        "cached_tokens": tot.get("cached_tokens") or 0,
        "ttft_ms": _r(tot.get("ttft_ms"), 0), "eff_tps": _r(tot.get("eff_tps")),
        "decode_tps": _r(tot.get("decode_tps")), "wall_ms": _r(tot.get("wall_ms"), 0),
        "cut_short": tot.get("cut_short") or 0, "last_ts": tot.get("last_ts") or "",
        "buckets": buckets,
        "models": [{"model": m["model"], "calls": m["calls"], "prompt_tokens": m["prompt_tokens"],
                    "completion_tokens": m["completion_tokens"], "ttft_ms": _r(m["ttft_ms"], 0),
                    "eff_tps": _r(m["eff_tps"])} for m in models],
    }
