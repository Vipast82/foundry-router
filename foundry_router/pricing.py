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
    # a deleted row's model family is remembered, so the automatic update doesn't
    # add it back as a "newly released model" (pricing_update.new_families)
    row = db.query_one("SELECT source_id FROM service_pricing WHERE id=?", (id,)) or {}
    if row.get("source_id"):
        from . import pricing_update
        pricing_update.dismiss(db, row["source_id"])
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
        r["kind"] = ("subscription" if t in ("anthropic-compatible",
                                              "openai-compatible-subscription")
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


# -- head to head: the local model against each paid service (v0.181.119) -------------

H2H_CATEGORIES = ("coding", "agentic", "reasoning", "general_chat", "tool_calling")
H2H_LOW_CONFIDENCE = 0.3


def _id_variants(model_id: str) -> list[str]:
    """Ids a model's benchmark rows may be stored under: the OpenRouter id, the
    bare slug, and the router's own spelling (dots as hyphens)."""
    mid = str(model_id or "")
    if not mid:
        return []
    out = [mid]
    slug = mid.split("/", 1)[1] if "/" in mid else mid
    for v in (slug, slug.replace(".", "-")):
        if v not in out:
            out.append(v)
    return out


def _rating(registry, model_id: str, category: str) -> Optional[dict]:
    """The model's score in a category (0-100) with where it came from: the most
    trusted row among the ids it may be stored under."""
    from .registry.models_db import score_source
    best = None
    for mid in _id_variants(model_id):
        for b in registry.benchmarks(mid) or []:
            if b.get("category") != category or b.get("score") is None:
                continue
            conf = 0.5 if b.get("confidence") is None else float(b["confidence"])
            if best is None or conf > best["confidence"]:
                best = {"score": round(float(b["score"]), 1), "confidence": round(conf, 2),
                        "source": score_source(b), "model_id": mid,
                        "low_confidence": conf < H2H_LOW_CONFIDENCE}
    return best


def head_to_head(db: Database, registry, costs: dict, category: str = "coding") -> dict:
    """The window's busiest LOCAL model against each paid service: benchmark
    score in one category, the gap, and what the same tokens would have cost.

    `like_for_like` is the like-for-like saving: the cheapest paid service
    rated at least as high as the local model. Scores come from the model
    registry (estimates, research, seeds, observed); each carries its source and
    confidence, and a low-confidence score is marked, never hidden."""
    category = category if category in H2H_CATEGORIES else "coding"
    out: dict = {"category": category, "categories": list(H2H_CATEGORIES), "local": None,
                 "rows": [], "like_for_like": None, "nearest": None, "unrated": []}
    local_rows = [r for r in costs.get("by_model") or [] if r.get("kind") == "local"]
    if not local_rows:
        out["note"] = "No local model served calls in this window."
        return out
    top = max(local_rows, key=lambda r: (r.get("input") or 0) + (r.get("output") or 0))
    self_cost = float((costs.get("local") or {}).get("cost") or 0.0)
    mine = _rating(registry, top["model"], category)
    out["local"] = {"model": top["model"], "cost": self_cost, **(mine or {"score": None})}
    source_ids = {s["id"]: s.get("source_id") for s in list_services(db)}
    rows = []
    for s in costs.get("services") or []:
        r = _rating(registry, source_ids.get(s.get("id")) or "", category) \
            or _rating(registry, s.get("name") or "", category)
        cost = float(s.get("cost_cached") if s.get("cost_cached") is not None else s.get("cost") or 0)
        row = {"name": s.get("name"), "cost": round(cost, 4),
               "extra": round(cost - self_cost, 4),
               "times": round(cost / self_cost, 1) if self_cost > 0 else None,
               **(r or {"score": None})}
        if r is None:
            out["unrated"].append(s.get("name"))
        elif mine is not None:
            row["delta"] = round(r["score"] - mine["score"], 1)
            # dollars per benchmark point gained over the local model
            row["per_point"] = (round((cost - self_cost) / row["delta"], 4)
                                if row["delta"] > 0 and cost > self_cost else None)
        rows.append(row)
    rows.sort(key=lambda r: (r.get("score") is None, -(r.get("score") or 0), r["cost"]))
    out["rows"] = rows
    if mine is not None:
        rated = [r for r in rows if r.get("score") is not None]
        peers = [r for r in rated if r["score"] >= mine["score"]]
        if peers:
            p = min(peers, key=lambda r: r["cost"])
            out["like_for_like"] = {"name": p["name"], "score": p["score"], "cost": p["cost"],
                                    "saving": round(p["cost"] - self_cost, 4)}
        if rated:
            n = min(rated, key=lambda r: (abs(r["score"] - mine["score"]), r["cost"]))
            out["nearest"] = {"name": n["name"], "score": n["score"], "cost": n["cost"],
                              "saving": round(n["cost"] - self_cost, 4)}
    else:
        out["note"] = (f"{top['model']} has no {category} score in the model registry "
                       f"(Backends → Model Registry).")
    return out


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
