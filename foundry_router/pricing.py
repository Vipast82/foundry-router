"""Local-vs-cloud cost calculator.

The router already records prompt/completion/cached token counts per call
(perf_samples). This module prices those tokens against paid-service API rates so
an operator can answer "what would this sprint have cost on Claude / GPT / Gemini,
and was self-hosting actually cheaper?".

The seeded rates are current public list prices (USD per 1M tokens) captured at
build time — they drift, so every field is editable in the UI. Accuracy is
"semi-close for a decision", not billing-grade: real bills vary with long-context
surcharges, batch discounts, and each vendor's own prompt-cache accounting. The
cached-input column applies OUR measured cache-hit tokens at the vendor's cached
rate, which is an approximation (a paid API caches differently) but a useful one.
"""

from __future__ import annotations

from typing import Any, Optional

from .db import Database, utcnow

# (name, input/1M, output/1M, cached-input/1M).  Sept 2026 public list prices.
# Sources: Anthropic, OpenAI, Google, DeepSeek, xAI pricing pages (see the cost
# tab's note). Cached-input ≈ the vendor's prompt-cache-hit rate where published.
DEFAULT_SERVICES: list[tuple] = [
    ("Claude Opus 4.8",      5.00, 25.00, 0.50),
    ("Claude Sonnet 5",      3.00, 15.00, 0.30),
    ("Claude Haiku 4.5",     1.00,  5.00, 0.10),
    ("GPT-5.6",              2.00, 12.00, 0.50),
    ("GPT-5.6 Sol",          4.00, 20.00, 1.00),
    ("GPT-5.6 Luna",         0.20,  1.20, 0.05),
    ("Gemini 3.1 Pro",       2.00, 12.00, 0.50),
    ("Gemini 2.5 Flash",     0.30,  2.50, 0.075),
    ("DeepSeek V4.1 Flash",  0.30,  1.20, 0.03),
    ("Grok 4.6",             2.00,  6.00, 0.50),
]

# Rough default power draw for the "cost to self-host" estimate: a dual-GPU box
# under inference load plus system overhead. Editable; only a ballpark.
DEFAULT_WATTS = 400.0
DEFAULT_KWH_RATE = 0.15          # USD per kWh (US average-ish)


def ensure_seed(db: Database) -> None:
    """Populate the pricing table once, on first use. INSERT OR IGNORE so a user's
    edits/deletions are never resurrected on restart."""
    row = db.query_one("SELECT COUNT(*) AS n FROM service_pricing")
    if row and row.get("n"):
        return
    now = utcnow()
    for name, i, o, c in DEFAULT_SERVICES:
        db.execute(
            "INSERT OR IGNORE INTO service_pricing "
            "(name, input_per_1m, output_per_1m, cached_input_per_1m, enabled, updated_at, "
            "source) VALUES (?,?,?,?,1,?,'seed')", (name, i, o, c, now))


def list_services(db: Database) -> list[dict]:
    ensure_seed(db)
    return db.query("SELECT * FROM service_pricing ORDER BY input_per_1m ASC, name ASC")


def upsert_service(db: Database, *, id: Optional[int] = None, name: str,
                   input_per_1m: float, output_per_1m: float,
                   cached_input_per_1m: Optional[float] = None,
                   enabled: bool = True, notes: str = "",
                   locked: Optional[bool] = None) -> None:
    """Manual add/edit. A hand edit marks the row source='manual'; `locked`
    keeps the automatic price update away from it (None = leave as is). A
    renamed service forgets its remembered OpenRouter match."""
    now = utcnow()
    if id:
        prev = db.query_one("SELECT name, input_per_1m, output_per_1m, cached_input_per_1m "
                            "FROM service_pricing WHERE id=?", (id,)) or {}
        price_changed = any(prev.get(k) != v for k, v in (
            ("input_per_1m", input_per_1m), ("output_per_1m", output_per_1m),
            ("cached_input_per_1m", cached_input_per_1m)))
        db.execute(
            "UPDATE service_pricing SET name=?, input_per_1m=?, output_per_1m=?, "
            "cached_input_per_1m=?, enabled=?, notes=?, updated_at=?"
            + (", source='manual'" if price_changed else "")
            + (", source_id=NULL" if prev.get("name") not in (None, name) else "")
            + (", locked=?" if locked is not None else "") + " WHERE id=?",
            (name, input_per_1m, output_per_1m, cached_input_per_1m,
             1 if enabled else 0, notes, now,
             *((1 if locked else 0,) if locked is not None else ()), id))
    else:
        db.execute(
            "INSERT INTO service_pricing (name, input_per_1m, output_per_1m, "
            "cached_input_per_1m, enabled, notes, updated_at, source, locked) "
            "VALUES (?,?,?,?,?,?,?,'manual',?) "
            "ON CONFLICT(name) DO UPDATE SET input_per_1m=excluded.input_per_1m, "
            "output_per_1m=excluded.output_per_1m, "
            "cached_input_per_1m=excluded.cached_input_per_1m, "
            "enabled=excluded.enabled, notes=excluded.notes, updated_at=excluded.updated_at, "
            "source='manual', locked=excluded.locked",
            (name, input_per_1m, output_per_1m, cached_input_per_1m,
             1 if enabled else 0, notes, now, 1 if locked else 0))


def delete_service(db: Database, id: int) -> None:
    db.execute("DELETE FROM service_pricing WHERE id=?", (id,))


def compute_costs(db: Database, *, hours: float = 72, model: Optional[str] = None,
                  watts: float = DEFAULT_WATTS,
                  kwh_rate: float = DEFAULT_KWH_RATE,
                  backend: Optional[str] = None,
                  run_label: Optional[str] = None,
                  backend_type=None) -> dict:
    """Total the window's tokens and price them against every enabled service,
    plus a rough self-hosting electricity cost, so the two are directly
    comparable. `model=None` spans the whole fleet."""
    ensure_seed(db)
    where = ["datetime(ts) >= datetime('now', ?)"]      # same ts-normalize fix as perf_history
    params: list[Any] = [f"-{hours} hours"]
    if model:
        where.append("model = ?")
        params.append(model)
    if backend:
        where.append("backend = ?")
        params.append(backend)
    if run_label is not None:
        where.append("COALESCE(run_label,'') = ?")
        params.append(run_label)
    clause = " AND ".join(where)
    agg = db.query_one(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens),0) AS input_tokens, "
        "COALESCE(SUM(completion_tokens),0) AS output_tokens, "
        "COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
        "COALESCE(SUM(wall_ms),0) AS wall_ms, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        f"FROM perf_samples WHERE {clause}", tuple(params)) or {}

    inp = int(agg.get("input_tokens") or 0)
    out = int(agg.get("output_tokens") or 0)
    cached = min(int(agg.get("cached_tokens") or 0), inp)   # never exceed input
    uncached = max(inp - cached, 0)
    calls = int(agg.get("calls") or 0)

    # Actual data span (for honest per-day / per-month extrapolation): use the
    # real first→last spread, not the requested window (which may be mostly idle).
    span_hours = _span_hours(agg.get("first_ts"), agg.get("last_ts")) or 0.0

    services = []
    for s in list_services(db):
        if not s.get("enabled"):
            continue
        pin = s.get("input_per_1m") or 0.0
        pout = s.get("output_per_1m") or 0.0
        pcache = s.get("cached_input_per_1m")
        naive = inp / 1e6 * pin + out / 1e6 * pout
        # With prompt caching: cached input priced at the vendor's cache rate
        # (falls back to full input price when the vendor has none configured).
        crate = pcache if pcache is not None else pin
        cached_cost = (uncached / 1e6 * pin + cached / 1e6 * crate + out / 1e6 * pout)
        services.append({
            "id": s.get("id"), "name": s.get("name"),
            "input_per_1m": pin, "output_per_1m": pout,
            "cached_input_per_1m": pcache,
            "cost": round(naive, 4), "cost_cached": round(cached_cost, 4),
            "per_day": round(naive / span_hours * 24, 2) if span_hours else None,
            "per_month": round(naive / span_hours * 720, 2) if span_hours else None,
        })
    services.sort(key=lambda x: x["cost"])

    active_hours = (agg.get("wall_ms") or 0) / 3.6e6      # sum of per-call wall time
    local_cost = round(watts / 1000.0 * active_hours * kwh_rate, 4)

    # Where the window's tokens actually went, per model: local (free, costs
    # electricity), Claude subscription (Meridian — counts against the plan
    # window, not dollars) or metered API. The comparison above prices ALL of
    # them as if sent to each paid service.
    by_model = db.query(
        "SELECT model, backend, COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens),0) AS input, COALESCE(SUM(completion_tokens),0) AS output, "
        "COALESCE(SUM(cached_tokens),0) AS cached, COALESCE(SUM(wall_ms),0) AS wall_ms "
        f"FROM perf_samples WHERE {clause} GROUP BY model, backend "
        "ORDER BY input + output DESC", tuple(params))
    for r in by_model:
        t = backend_type(r["model"]) if backend_type else ""
        r["kind"] = ("subscription" if t == "anthropic-compatible"
                     else "local" if t in ("ollama", "openai-compatible-local")
                     else "metered" if t == "openai-compatible" else (t or "unknown"))
        r["share_pct"] = round(100.0 * (r["input"] + r["output"]) / (inp + out), 1) if inp + out else 0

    return {
        "hours": hours, "model": model or "", "span_hours": round(span_hours, 2),
        "by_model": by_model,
        "tokens": {"input": inp, "output": out, "cached": cached,
                   "uncached": uncached, "calls": calls},
        "local": {"watts": watts, "kwh_rate": kwh_rate,
                  "active_hours": round(active_hours, 2), "cost": local_cost,
                  "per_day": round(local_cost / span_hours * 24, 2) if span_hours else None,
                  "per_month": round(local_cost / span_hours * 720, 2) if span_hours else None},
        "services": services,
    }


def _span_hours(first_ts: Optional[str], last_ts: Optional[str]) -> float:
    if not first_ts or not last_ts:
        return 0.0
    import datetime as dt

    def _p(s: str):
        s = s.replace(" ", "T")
        try:
            return dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    a, b = _p(first_ts), _p(last_ts)
    if not a or not b:
        return 0.0
    return max((b - a).total_seconds() / 3600.0, 0.0)
