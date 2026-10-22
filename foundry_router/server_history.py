"""History of what each inference engine reports about itself.

`pool.server_metrics()` reads llama.cpp / Strata (`/metrics`, `/slots`) and
Meridian (`/metrics`, `/health`) live, but only the Live tab ever saw it: KV
fill, busy slots, queue depth and the engine's own tokens/s were gone a few
seconds later. A background task (main.py) samples it every
`perf.server_sample_seconds` and keeps one row per backend, model and metric:

    server_samples(ts, backend, model, metric, value)

Every numeric value the engine reports is stored under its normalised name
(prom.summarize): kv_cache_usage_pct, slots_busy, slots_total, requests_running,
requests_waiting, avg_decode_tps, avg_prompt_tps, live_decode_tps,
live_prefill_tps, prefix_cache_hit_pct, spec_accept_pct, mean_ttft_ms, ...
For Meridian that is requests_total / requests_failed and the latency means.
These are the ENGINE's numbers (lifetime averages and gauges), not the
per-call timings of perf_samples. Kept for `perf.retention_days`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger("foundry.server_history")

# keys that are not metrics (labels, flags, nested lists)
_SKIP = {"slots", "swap_running", "metrics_ok", "logged_in"}
MAX_POINTS = 600


def flatten(m: dict) -> list[tuple[str, float]]:
    """The numeric metrics in one backend's server_metrics() dict."""
    out = []
    for k, v in (m or {}).items():
        if k in _SKIP or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out.append((k, float(v)))
    return out


def _model_of(db: Any, m: dict, backend: str) -> str:
    """The model the engine had loaded: its own report, else the model that
    backend last served."""
    running = m.get("swap_running") or []
    if running and running[0].get("model"):
        return str(running[0]["model"])
    if m.get("model"):
        return str(m["model"])
    try:
        r = db.query_one("SELECT model FROM perf_samples WHERE backend=? "
                         "ORDER BY id DESC LIMIT 1", (backend,))
        return (r or {}).get("model") or ""
    except Exception:                                            # noqa: BLE001
        return ""


async def sample_once(svc: Any, now: Optional[float] = None) -> int:
    """Read every backend's engine metrics once and store them. Returns the
    number of rows written. Backends without a metrics surface write nothing."""
    servers = await svc.pool.server_metrics()
    ts = now or time.time()
    rows = []
    for m in servers or []:
        backend = m.get("backend") or ""
        if not backend or not m.get("metrics_ok", True) and not flatten(m):
            continue
        model = _model_of(svc.db, m, backend)
        rows += [(ts, backend, model, k, v) for k, v in flatten(m)]
    if rows:
        svc.db.executemany("INSERT INTO server_samples (ts, backend, model, metric, value) "
                           "VALUES (?,?,?,?,?)", rows)
    return len(rows)


def prune(db: Any, days: int) -> int:
    return db.execute_count("DELETE FROM server_samples WHERE ts < ?", (time.time() - days * 86400,))


def query(db: Any, hours: float = 24, backend: str = "", metrics: Optional[list] = None,
          max_points: int = MAX_POINTS) -> dict:
    """Series per backend and metric over the window, averaged into at most
    `max_points` buckets each (so a 14-day chart stays light)."""
    hours = max(0.1, min(float(hours or 24), 24 * 365))
    since = time.time() - hours * 3600
    bucket = max(15.0, hours * 3600 / max(10, int(max_points)))
    where, args = ["ts >= ?"], [since]
    if backend:
        where.append("backend = ?")
        args.append(backend)
    if metrics:
        where.append("metric IN (%s)" % ",".join("?" * len(metrics)))
        args += list(metrics)
    rows = db.query(
        f"SELECT backend, metric, CAST(ts / ? AS INTEGER) * ? AS b, AVG(value) AS v, "
        f"MAX(model) AS model, MAX(ts) AS last_ts FROM server_samples WHERE {' AND '.join(where)} "
        f"GROUP BY backend, metric, b ORDER BY b", (bucket, bucket, *args))
    out: dict = {}
    for r in rows:
        be = out.setdefault(r["backend"], {"model": "", "last_ts": 0, "series": {}})
        be["series"].setdefault(r["metric"], []).append([r["b"], round(r["v"], 3)])
        if r["last_ts"] >= be["last_ts"]:
            be["last_ts"], be["model"] = r["last_ts"], r["model"] or be["model"]
    return {"hours": hours, "bucket_s": bucket, "backends": out,
            "samples": (db.query_one("SELECT COUNT(*) AS n FROM server_samples WHERE ts >= ?",
                                     (since,)) or {}).get("n", 0)}
