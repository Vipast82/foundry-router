"""Per-model-call performance time-series — the data behind the Performance tab.

The `models` table holds rolling AVERAGES (great for "how fast is this model?"),
but an average can't answer "WHEN did it start degrading?" over a two-day sprint,
or "does speculative acceptance fall as the KV context fills toward its cap?".
Those need the raw samples over time, which is what this module records and
queries. One row per completed model call, with every metric the backend
reported plus the derived rates, pruned to a retention window so a long run
doesn't grow the DB without bound.

Kept deliberately backend-agnostic: any field a backend doesn't report is stored
NULL rather than a misleading zero, so charts can skip gaps instead of drawing
false dips.

Run labels: every sample is stamped with the operator's current run label (set
in the Performance tab, e.g. "2x4060ti-16gb" then "2x2080ti-22gb"), so a
hardware or config change can be compared side by side per model instead of
the new numbers blurring into the old averages. Clearing (clear_samples) is the
other half: wipe a model / backend / run and start clean.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from .db import Database, utcnow

# Retention: how far back samples are kept. A multi-day sprint is the use case,
# so two weeks comfortably covers "compare this run to the last one" while
# capping unbounded growth. Pruned opportunistically (see record_sample).
RETENTION_DAYS = 14

RUN_LABEL_KEY = "perf_run_label"


def get_run_label(db: Database) -> str:
    try:
        return db.kv_get(RUN_LABEL_KEY) or ""
    except Exception:
        return ""


def set_run_label(db: Database, label: str) -> str:
    label = (label or "").strip()[:64]
    if label:
        db.kv_set(RUN_LABEL_KEY, label)
    else:
        db.kv_del(RUN_LABEL_KEY)
    return label


def _rate(n: Optional[int], duration_ns: Optional[int]) -> Optional[float]:
    """tokens/sec from a count and a nanosecond duration, or None when either is
    missing (so a backend that reports no timing leaves a gap, not a zero)."""
    if n and duration_ns and duration_ns > 0:
        return round(n / (duration_ns / 1e9), 2)
    return None


def _pct(num: Optional[int], denom: Optional[int]) -> Optional[float]:
    if num and denom and denom > 0:
        return round(100.0 * min(num, denom) / denom, 1)
    return None


def record_sample(db: Database, *, model: str, backend: str = "", persona: str = "",
                  mode: str = "", prompt_tokens: int = 0, completion_tokens: int = 0,
                  cached_tokens: int = 0, draft_n: int = 0, draft_n_accepted: int = 0,
                  eval_duration_ns: int = 0, prompt_eval_duration_ns: int = 0,
                  ttft_ms: Optional[float] = None, wall_ms: Optional[int] = None,
                  finish_reason: str = "", prefill_tokens: int = 0,
                  reasoning_tokens: int = 0, load_duration_ns: int = 0,
                  timing_source: str = "", run_label: Optional[str] = None) -> None:
    """Write one completed model call's metrics as a time-series point. Derived
    rates/percentages are computed here so every reader gets them consistently.
    Best-effort: a logging failure must never break the request that produced it.

    prefill_tokens: the tokens actually processed during prefill (llama.cpp's
    timings.prompt_n — excludes cache hits). Prefill tok/s divides THAT by the
    prefill time; 0 falls back to prompt_tokens (Ollama reports them as one)."""
    decode_tps = _rate(completion_tokens, eval_duration_ns)
    prefill_tps = _rate(prefill_tokens or prompt_tokens, prompt_eval_duration_ns)
    decode_ms = round(eval_duration_ns / 1e6, 1) if eval_duration_ns else None
    prefill_ms = round(prompt_eval_duration_ns / 1e6, 1) if prompt_eval_duration_ns else None
    eff_tps = (round(completion_tokens / (wall_ms / 1000), 2)
               if completion_tokens and wall_ms else None)
    cache_hit_pct = _pct(cached_tokens, prompt_tokens)
    spec_accept_pct = (round(100.0 * draft_n_accepted / draft_n, 1) if draft_n else None)
    if run_label is None:
        run_label = get_run_label(db)
    try:
        db.execute(
            "INSERT INTO perf_samples (ts, model, backend, persona, mode, "
            "prompt_tokens, completion_tokens, cached_tokens, draft_n, "
            "draft_n_accepted, decode_tps, prefill_tps, decode_ms, prefill_ms, "
            "ttft_ms, eff_tps, wall_ms, cache_hit_pct, spec_accept_pct, finish_reason, "
            "run_label, timing_src, prefill_tokens, reasoning_tokens, load_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), model, backend, persona, mode,
             int(prompt_tokens or 0), int(completion_tokens or 0), int(cached_tokens or 0),
             int(draft_n or 0), int(draft_n_accepted or 0),
             decode_tps, prefill_tps, decode_ms, prefill_ms,
             (round(ttft_ms, 1) if ttft_ms else None), eff_tps,
             (int(wall_ms) if wall_ms else None), cache_hit_pct, spec_accept_pct,
             finish_reason or "", run_label or "",
             timing_source or ("server" if eval_duration_ns else ""),
             (int(prefill_tokens) if prefill_tokens else None),
             (int(reasoning_tokens) if reasoning_tokens else None),
             (round(load_duration_ns / 1e6, 1) if load_duration_ns else None)))
    except Exception:
        return
    # Opportunistic prune (≈2% of inserts) — keeps the table bounded without a
    # DELETE on the hot path of every single request.
    if random.random() < 0.02:
        try:
            # datetime(ts) for the same format-mismatch reason as query_history.
            db.execute(
                "DELETE FROM perf_samples WHERE datetime(ts) < datetime('now', ?)",
                (f"-{RETENTION_DAYS} days",))
        except Exception:
            pass


# Columns exposed to the dashboard, in a stable order (also the CSV/export order).
SERIES_COLUMNS = ("ts", "model", "backend", "persona", "mode", "run_label",
                  "prompt_tokens", "prefill_tokens", "completion_tokens",
                  "reasoning_tokens", "cached_tokens", "draft_n", "draft_n_accepted",
                  "decode_tps", "prefill_tps", "decode_ms", "prefill_ms", "ttft_ms",
                  "eff_tps", "wall_ms", "load_ms", "cache_hit_pct", "spec_accept_pct",
                  "finish_reason", "timing_src")


def _filters(hours: Optional[float], model: Optional[str], backend: Optional[str],
             run_label: Optional[str]) -> tuple[str, list]:
    """WHERE clause + params shared by every reader (and clear_samples).

    datetime(ts) on BOTH sides of the window test: samples are stored as ISO8601
    with a 'T' separator (utcnow()), but datetime('now',...) yields a
    space-separated string — a raw `ts >= datetime(...)` string compare is WRONG
    ('T' > ' ', so same-day rows always pass and the window silently widens).
    datetime() normalizes both to the same UTC form so the comparison is real.
    run_label "" is a real value (unlabelled samples); None means "any"."""
    where: list[str] = []
    params: list[Any] = []
    if hours:
        where.append("datetime(ts) >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    if model:
        where.append("model = ?")
        params.append(model)
    if backend:
        where.append("backend = ?")
        params.append(backend)
    if run_label is not None:
        where.append("COALESCE(run_label,'') = ?")
        params.append(run_label)
    return (" AND ".join(where) or "1=1"), params


def _percentile(vals: list[float], q: float) -> Optional[float]:
    if not vals:
        return None
    vals = sorted(vals)
    idx = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return round(vals[idx], 1)


_AVG_KEYS = ("decode_tps", "prefill_tps", "eff_tps", "ttft_ms", "spec_accept_pct",
             "cache_hit_pct", "prompt_tokens", "completion_tokens", "wall_ms",
             "load_ms", "decode_ms", "prefill_ms")


def query_history(db: Database, hours: float = 72, model: Optional[str] = None,
                  limit: int = 4000, backend: Optional[str] = None,
                  run_label: Optional[str] = None) -> dict:
    """Raw samples within the window (newest first, capped) plus window-wide
    summary averages and a per-model breakdown, for the Performance tab.
    `model`/`backend`/`run_label` narrow the view; None spans everything.

    Summary + breakdown are computed over EVERY sample in the window (SQL
    aggregates), not just the capped series, so headline numbers stay honest on
    a long, busy run."""
    clause, params = _filters(hours, model, backend, run_label)
    cols = ", ".join(SERIES_COLUMNS)
    rows = db.query(
        f"SELECT {cols} FROM perf_samples WHERE {clause} "
        f"ORDER BY id DESC LIMIT ?", (*params, int(limit)))
    rows.reverse()   # chronological for charting

    avg_sql = ", ".join(f"ROUND(AVG({k}), 2) AS {k}" for k in _AVG_KEYS)
    agg = db.query_one(
        f"SELECT COUNT(*) AS samples, {avg_sql}, "
        f"SUM(CASE WHEN finish_reason='length' THEN 1 ELSE 0 END) AS truncations, "
        f"COALESCE(SUM(completion_tokens),0) AS out_tokens, "
        f"COALESCE(SUM(prompt_tokens),0) AS in_tokens, "
        f"SUM(CASE WHEN timing_src='estimated' THEN 1 ELSE 0 END) AS estimated "
        f"FROM perf_samples WHERE {clause}", tuple(params)) or {}
    summary = {k: agg.get(k) for k in ("samples", *_AVG_KEYS, "truncations",
                                       "out_tokens", "in_tokens", "estimated")}
    summary["samples"] = summary.get("samples") or 0
    summary["truncations"] = summary.get("truncations") or 0
    summary["estimated"] = summary.get("estimated") or 0

    # Per model × backend × run label — the "separate by model" and "before vs
    # after the hardware swap" view. Percentiles (median decode, p95 TTFT) are
    # computed from a lean 5-column fetch; averages come straight from SQL.
    groups = db.query(
        f"SELECT model, backend, COALESCE(run_label,'') AS run_label, "
        f"COUNT(*) AS n, {avg_sql}, "
        f"SUM(CASE WHEN finish_reason='length' THEN 1 ELSE 0 END) AS truncations, "
        f"COALESCE(SUM(completion_tokens),0) AS out_tokens, "
        f"MAX(prompt_tokens) AS max_prompt_tokens, "
        f"SUM(CASE WHEN timing_src='estimated' THEN 1 ELSE 0 END) AS estimated, "
        f"MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        f"FROM perf_samples WHERE {clause} "
        f"GROUP BY model, backend, COALESCE(run_label,'') "
        f"ORDER BY model, last_ts DESC", tuple(params))
    pct_rows = db.query(
        f"SELECT model, backend, COALESCE(run_label,'') AS run_label, "
        f"decode_tps, ttft_ms FROM perf_samples WHERE {clause}", tuple(params))
    dist: dict = {}
    for r in pct_rows:
        d = dist.setdefault((r["model"], r["backend"], r["run_label"]), ([], []))
        if r["decode_tps"] is not None:
            d[0].append(r["decode_tps"])
        if r["ttft_ms"] is not None:
            d[1].append(r["ttft_ms"])
    for g in groups:
        dec, tt = dist.get((g["model"], g["backend"], g["run_label"]), ([], []))
        g["p50_decode_tps"] = _percentile(dec, 0.5)
        g["p95_ttft_ms"] = _percentile(tt, 0.95)

    # Filter option lists span the window (not the other filters), so picking a
    # model doesn't make the other models vanish from the dropdown.
    wclause, wparams = _filters(hours, None, None, None)
    model_rows = db.query(
        f"SELECT model, COUNT(*) AS n FROM perf_samples WHERE {wclause} "
        f"GROUP BY model ORDER BY n DESC", tuple(wparams))
    backend_rows = db.query(
        f"SELECT backend, COUNT(*) AS n FROM perf_samples WHERE {wclause} "
        f"GROUP BY backend ORDER BY n DESC", tuple(wparams))
    label_rows = db.query(
        f"SELECT COALESCE(run_label,'') AS run_label, COUNT(*) AS n, "
        f"MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM perf_samples "
        f"WHERE {wclause} GROUP BY COALESCE(run_label,'') ORDER BY last_ts DESC",
        tuple(wparams))
    return {"hours": hours, "model": model or "", "backend": backend or "",
            "run_label": run_label, "series": rows,
            "models": [{"model": m["model"], "n": m["n"]} for m in model_rows],
            "backends": [{"backend": b["backend"], "n": b["n"]} for b in backend_rows],
            "run_labels": label_rows,
            "current_run_label": get_run_label(db),
            "by_model": groups,
            "summary": summary}


def clear_samples(db: Database, model: Optional[str] = None,
                  backend: Optional[str] = None,
                  run_label: Optional[str] = None) -> int:
    """Delete perf samples — everything, or narrowed to a model / backend / run
    label (all given filters must match). Returns the number of rows removed."""
    clause, params = _filters(None, model, backend, run_label)
    row = db.query_one(f"SELECT COUNT(*) AS n FROM perf_samples WHERE {clause}",
                       tuple(params)) or {}
    n = int(row.get("n") or 0)
    if n:
        db.execute(f"DELETE FROM perf_samples WHERE {clause}", tuple(params))
    return n
