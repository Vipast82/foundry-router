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
"""

from __future__ import annotations

import random
from typing import Any, Optional

from .db import Database, utcnow

# Retention: how far back samples are kept. A multi-day sprint is the use case,
# so two weeks comfortably covers "compare this run to the last one" while
# capping unbounded growth. Pruned opportunistically (see record_sample).
RETENTION_DAYS = 14


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
                  finish_reason: str = "") -> None:
    """Write one completed model call's metrics as a time-series point. Derived
    rates/percentages are computed here so every reader gets them consistently.
    Best-effort: a logging failure must never break the request that produced it."""
    decode_tps = _rate(completion_tokens, eval_duration_ns)
    prefill_tps = _rate(prompt_tokens, prompt_eval_duration_ns)
    decode_ms = round(eval_duration_ns / 1e6, 1) if eval_duration_ns else None
    prefill_ms = round(prompt_eval_duration_ns / 1e6, 1) if prompt_eval_duration_ns else None
    eff_tps = (round(completion_tokens / (wall_ms / 1000), 2)
               if completion_tokens and wall_ms else None)
    cache_hit_pct = _pct(cached_tokens, prompt_tokens)
    spec_accept_pct = (round(100.0 * draft_n_accepted / draft_n, 1) if draft_n else None)
    try:
        db.execute(
            "INSERT INTO perf_samples (ts, model, backend, persona, mode, "
            "prompt_tokens, completion_tokens, cached_tokens, draft_n, "
            "draft_n_accepted, decode_tps, prefill_tps, decode_ms, prefill_ms, "
            "ttft_ms, eff_tps, wall_ms, cache_hit_pct, spec_accept_pct, finish_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), model, backend, persona, mode,
             int(prompt_tokens or 0), int(completion_tokens or 0), int(cached_tokens or 0),
             int(draft_n or 0), int(draft_n_accepted or 0),
             decode_tps, prefill_tps, decode_ms, prefill_ms,
             (round(ttft_ms, 1) if ttft_ms else None), eff_tps,
             (int(wall_ms) if wall_ms else None), cache_hit_pct, spec_accept_pct,
             finish_reason or ""))
    except Exception:
        return
    # Opportunistic prune (≈2% of inserts) — keeps the table bounded without a
    # DELETE on the hot path of every single request.
    if random.random() < 0.02:
        try:
            db.execute(
                "DELETE FROM perf_samples WHERE ts < datetime('now', ?)",
                (f"-{RETENTION_DAYS} days",))
        except Exception:
            pass


# Columns exposed to the dashboard, in a stable order (also the CSV/export order).
SERIES_COLUMNS = ("ts", "model", "backend", "mode", "prompt_tokens",
                  "completion_tokens", "cached_tokens", "draft_n", "draft_n_accepted",
                  "decode_tps", "prefill_tps", "decode_ms", "prefill_ms", "ttft_ms",
                  "eff_tps", "wall_ms", "cache_hit_pct", "spec_accept_pct",
                  "finish_reason")


def query_history(db: Database, hours: float = 72, model: Optional[str] = None,
                  limit: int = 4000) -> dict:
    """Raw samples within the window (newest first, capped) plus window-wide
    summary averages, for the Performance tab's charts and headline numbers.
    `model=None` spans the whole fleet; a value narrows to one model."""
    where = ["ts >= datetime('now', ?)"]
    params: list[Any] = [f"-{hours} hours"]
    if model:
        where.append("model = ?")
        params.append(model)
    clause = " AND ".join(where)
    cols = ", ".join(SERIES_COLUMNS)
    rows = db.query(
        f"SELECT {cols} FROM perf_samples WHERE {clause} "
        f"ORDER BY id DESC LIMIT ?", (*params, int(limit)))
    rows.reverse()   # chronological for charting

    # Models seen in the window (for the per-model filter), most-active first.
    model_rows = db.query(
        f"SELECT model, COUNT(*) AS n FROM perf_samples WHERE {clause} "
        f"GROUP BY model ORDER BY n DESC", tuple(params))

    def _avg(key: str) -> Optional[float]:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    summary = {
        "samples": len(rows),
        "decode_tps": _avg("decode_tps"),
        "prefill_tps": _avg("prefill_tps"),
        "eff_tps": _avg("eff_tps"),
        "ttft_ms": _avg("ttft_ms"),
        "spec_accept_pct": _avg("spec_accept_pct"),
        "cache_hit_pct": _avg("cache_hit_pct"),
        "prompt_tokens": _avg("prompt_tokens"),
        "truncations": sum(1 for r in rows if r.get("finish_reason") == "length"),
    }
    return {"hours": hours, "model": model or "", "series": rows,
            "models": [{"model": m["model"], "n": m["n"]} for m in model_rows],
            "summary": summary}
