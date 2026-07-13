"""Model Registry (design doc §4.4): CRUD + the ranking query the live
Routing Agent uses before deciding which tool to call.

Manual-override protection is enforced here, in one place: a `models` row
whose source is "manual_override" only has its NULL fields filled by automatic
refreshes, never its user-set values replaced; a `model_benchmarks` row with
source_type "manual_override" blocks automatic rows for that model/category
pair from superseding it (the automatic row is still stored, but ranking
prefers the manual one).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ..db import Database, utcnow

log = logging.getLogger(__name__)

MODEL_FIELDS = [
    "display_name", "provider", "context_length", "cost_per_1k_input",
    "cost_per_1k_output", "relative_cost_tier", "reasoning_style", "good_for",
    "benefits_from_explicit_prompting", "tags", "content_policy",
]

# Cost is the FIRST sort key of candidate ranking (design: "don't reach for
# Opus when Haiku or a local model would do" as a structural property, not a
# prompt instruction). "free" = local/zero-cost; unknown tier sorts as
# mid-tier paid (conservative).
TIER_RANK = {"free": 0, "low": 1, "medium": 2, "high": 3, "very_high": 4}
UNKNOWN_TIER_RANK = 2.5

# ---- Multi-signal within-tier scoring --------------------------------------------
# The within-tier quality score is a CONFIDENCE-WEIGHTED AVERAGE of several
# signals (each a 0-100 benchmark): confidence modulates each signal's WEIGHT,
# not its value, so a low-confidence or low-sample signal moves the score only
# slightly — no single early verdict can tank a model. Signals: the persona's
# requested category ("primary"), plus tool_calling (when the persona has MCP
# tools), adequacy (observed outcome-judge verdicts), and latency (opt-in per
# persona). Reliability (call success rate) is applied separately as a
# MULTIPLIER — a penalty for flaky models, never a booster for healthy ones.
# Only the within-tier score changes; tier-first order and per-tier caps are
# untouched. All weights are persona-overridable (personas.selection_weights).
DEFAULT_SELECTION_WEIGHTS = {"primary": 1.0, "tool_calling": 0.0,
                             "adequacy": 0.6, "latency": 0.0}
SIGNAL_WEIGHT_TOOL_CALLING = 0.5   # applied when the persona has MCP tools attached
_SIGNAL_CATEGORIES = ("tool_calling", "adequacy", "latency")

# Data hygiene: a benchmark's stored confidence is further discounted by its
# SOURCE (a scraped community number is worth less than a vendor sheet, an
# independent eval, a live observation, or a hand-set override) and its AGE.
# Observed rows written from live traffic carry full weight.
SOURCE_CONFIDENCE_FACTOR = {"manual_override": 1.0, "observed": 1.0,
                            "independent": 1.0, "vendor": 0.9,
                            "community_report": 0.8, "estimated": 0.7}
_DEFAULT_SOURCE_FACTOR = 0.8
RECENCY_FULL_DAYS = 30       # no decay within a month
RECENCY_DECAY_DAYS = 365     # linear decay from full to floor over a year
RECENCY_FLOOR = 0.6          # never discount an old number below this

RELIABILITY_MIN_SAMPLE = 5   # below this, no reliability penalty (too little data)
RELIABILITY_FLOOR = 0.4      # a model that always fails still keeps this much score


def _recency_factor(last_updated: Optional[str]) -> float:
    if not last_updated:
        return 1.0
    try:
        dt = datetime.fromisoformat(last_updated)
    except (ValueError, TypeError):
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if age_days <= RECENCY_FULL_DAYS:
        return 1.0
    frac = (age_days - RECENCY_FULL_DAYS) / (RECENCY_DECAY_DAYS - RECENCY_FULL_DAYS)
    return max(RECENCY_FLOOR, 1.0 - (1.0 - RECENCY_FLOOR) * min(1.0, frac))


def _effective_confidence(bench_row: dict) -> float:
    """Stored confidence discounted by source trust and recency (0..1)."""
    conf = bench_row.get("confidence")
    conf = 0.5 if conf is None else conf
    src = SOURCE_CONFIDENCE_FACTOR.get(bench_row.get("source_type"), _DEFAULT_SOURCE_FACTOR)
    return max(0.0, min(1.0, conf)) * src * _recency_factor(bench_row.get("last_updated"))


# Observed-telemetry scoring (design: feed data the router already collects
# into ranking as a `measured`/`observed` benchmark source — more trustworthy
# than the estimated/community_report rows that dominate the registry).
#
# Warm tokens/sec that maps to a latency score of 100. Deliberately a fixed,
# transparent reference rather than a per-model-size normalization: the score
# only feeds ranking for a persona whose category is 'latency' (none ship that
# way), so it's forward-looking data, and a documented constant is easier to
# reason about than a clever curve. Tune if the local fleet's "fast" differs.
LATENCY_TARGET_TPS = 60.0


def observed_confidence(n: int) -> float:
    """Confidence for an observed benchmark, scaled by sample size so 2 lucky
    calls don't outrank 200: ~0.17 at n=2, 0.5 at n=10, 0.9 at n=90, capped
    at 0.95 (observation is strong evidence but never certainty)."""
    n = max(0, int(n))
    return round(min(0.95, n / (n + 10)), 3) if n else 0.0


class ModelRegistry:
    def __init__(self, db: Database):
        self.db = db

    # -- models table ------------------------------------------------------------

    def get(self, model_id: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM models WHERE id=?", (model_id,))

    def list_models(self) -> list[dict]:
        return self.db.query("SELECT * FROM models ORDER BY id")

    def upsert_auto(self, model_id: str, source: str, **fields) -> None:
        """Automatic upsert (discovery / OpenRouter poll / Research Agent).
        Creates the row if missing; on an existing row, respects manual
        overrides: user-set values are only *supplemented* (NULL fields
        filled), never replaced."""
        existing = self.get(model_id)
        fields = {k: v for k, v in fields.items() if k in MODEL_FIELDS and v is not None}
        now = utcnow()
        if existing is None:
            cols = ["id", "last_updated", "source"] + list(fields.keys())
            self.db.execute(
                f"INSERT INTO models ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [model_id, now, source] + list(fields.values()),
            )
            return
        manual = existing.get("source") == "manual_override"
        updates, params = [], []
        for k, v in fields.items():
            if manual and existing.get(k) is not None:
                continue  # never replace a hand-set value automatically
            updates.append(f"{k}=?")
            params.append(v)
        if not updates:
            return
        updates.append("last_updated=?")
        params.append(now)
        if not manual:
            updates.append("source=?")
            params.append(source)
        params.append(model_id)
        self.db.execute(f"UPDATE models SET {','.join(updates)} WHERE id=?", params)

    def manual_update(self, model_id: str, **fields) -> None:
        """Web-UI edit: sets fields and pins source=manual_override so the next
        automatic refresh supplements rather than replaces (§4.4)."""
        fields = {k: v for k, v in fields.items() if k in MODEL_FIELDS}
        if not fields:
            return
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE models SET {sets}, source='manual_override', last_updated=? WHERE id=?",
            list(fields.values()) + [utcnow(), model_id],
        )

    # -- benchmarks table -----------------------------------------------------------

    def benchmarks(self, model_id: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM model_benchmarks WHERE model_id=? ORDER BY category", (model_id,))

    def upsert_benchmark(self, model_id: str, category: str, score: float,
                         score_type: str, source_type: str, source_url: str = "",
                         confidence: float = 0.5) -> None:
        """One row per (model, category, source_type-class): automatic writes
        replace previous automatic rows for the pair but never touch a
        manual_override row (§4.4)."""
        # model_benchmarks has a FK to models(id) and foreign_keys is ON —
        # guarantee the parent row so callers never depend on upsert ordering.
        self.db.execute("INSERT OR IGNORE INTO models (id) VALUES (?)", (model_id,))
        if source_type != "manual_override":
            self.db.execute(
                "DELETE FROM model_benchmarks WHERE model_id=? AND category=? "
                "AND source_type != 'manual_override'",
                (model_id, category),
            )
        else:
            self.db.execute(
                "DELETE FROM model_benchmarks WHERE model_id=? AND category=? "
                "AND source_type = 'manual_override'",
                (model_id, category),
            )
        self.db.execute(
            """INSERT INTO model_benchmarks
               (model_id, category, score, score_type, source_type, source_url,
                confidence, last_updated)
               VALUES (?,?,?,?,?,?,?,?)""",
            (model_id, category, score, score_type, source_type, source_url,
             confidence, utcnow()),
        )

    def reset_benchmarks(self, model_id: str) -> int:
        """Delete a model's AUTOMATIC benchmark rows (research/seed/observed),
        preserving any manual_override. Used to clear corrupted rows so the
        reference seed / a fresh research pass can repopulate clean values —
        the fix path for a row that got stamped with a wrong high-confidence
        number (e.g. an extractor conflating two categories). Returns the count
        removed."""
        # Count first: db.execute returns lastrowid-or-rowcount, and SQLite's
        # lastrowid lingers from a prior INSERT, so it can't be trusted for a
        # DELETE's affected-row count.
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM model_benchmarks WHERE model_id=? "
            "AND source_type != 'manual_override'", (model_id,))
        self.db.execute(
            "DELETE FROM model_benchmarks WHERE model_id=? "
            "AND source_type != 'manual_override'", (model_id,))
        return row["n"] if row else 0

    # -- the routing query -------------------------------------------------------------

    def _resolve_weights(self, weights: Optional[dict], blend_tool_calling: bool,
                         category: str) -> dict:
        w = dict(DEFAULT_SELECTION_WEIGHTS)
        if blend_tool_calling and category != "tool_calling":
            w["tool_calling"] = SIGNAL_WEIGHT_TOOL_CALLING
        for k, v in (weights or {}).items():
            if k in w:
                try:
                    w[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return w

    @staticmethod
    def _reliability_multiplier(model_row: dict) -> float:
        """Flaky models (timeouts/empties/failures) are penalized; healthy ones
        are untouched. Below RELIABILITY_MIN_SAMPLE calls, no penalty."""
        ok = model_row.get("calls_ok") or 0
        fail = model_row.get("calls_failed") or 0
        n = ok + fail
        if n < RELIABILITY_MIN_SAMPLE:
            return 1.0
        return RELIABILITY_FLOOR + (1.0 - RELIABILITY_FLOOR) * (ok / n)

    def ranked_for_category(self, category: str, model_ids: list[str],
                            limit: int = 20, per_tier: int = 5,
                            blend_tool_calling: bool = False,
                            weights: Optional[dict] = None,
                            permissive_mode: str = "neutral",
                            min_context: Optional[int] = None) -> list[dict]:
        """Candidates among the currently-reachable models (`model_ids` from the
        Backend Pool), sorted: cost tier first (free/local -> ... -> premium),
        then WITHIN a tier a multi-signal quality score. Per-tier caps keep
        local leading AND premium visible (the escalation-dropout guard).

        Within-tier ordering, in precedence:
          1. tier (free first)
          2. context fit — a model whose known context_length can't hold
             `min_context` sinks below models that can (soft: unknown length
             never sinks, and it degrades rather than removing)
          3. permissive policy — content policy is NOT a ranking input for
             normal personas ('neutral', the default): a permissive model earns
             its rank on merit like any other, and the request-level refusal
             fallback (see AgentRunner) brings a permissive model in only when a
             standard one actually declines the specific request. The one
             exception is 'prefer' — an explicit permissive/creative front-door
             persona, which floats content_policy=permissive models to the top
          4. the multi-signal quality score (see DEFAULT_SELECTION_WEIGHTS):
             a confidence-weighted average of the requested category plus
             tool_calling / adequacy / latency, times a reliability multiplier

        Models with no benchmark row still appear (bottom of their tier) so the
        brain knows they exist and can fire request_model_research."""
        if not model_ids:
            return []
        w = self._resolve_weights(weights, blend_tool_calling, category)
        # Which benchmark categories to fetch: the requested one plus any
        # weighted signal (skipping a signal that IS the requested category, so
        # it's never double-counted).
        signals = [s for s in _SIGNAL_CATEGORIES if w.get(s, 0) > 0 and s != category]
        needed = [category] + signals

        ph = ",".join("?" * len(model_ids))
        models = {r["id"]: r for r in
                  self.db.query(f"SELECT * FROM models WHERE id IN ({ph})", model_ids)}
        cph = ",".join("?" * len(needed))
        bench = self.db.query(
            f"SELECT * FROM model_benchmarks WHERE model_id IN ({ph}) "
            f"AND category IN ({cph})", model_ids + needed)

        # Best row per (model, category): manual_override first, then highest
        # effective (source- and recency-discounted) score*confidence — so
        # observed/measured rows win over estimated ones as they accrue.
        grouped: dict = {}
        for b in bench:
            grouped.setdefault((b["model_id"], b["category"]), []).append(b)
        best: dict = {}
        for key, rowset in grouped.items():
            rowset.sort(
                key=lambda r: (r.get("source_type") == "manual_override",
                               (r.get("score") or 0) * _effective_confidence(r)),
                reverse=True)
            best[key] = rowset[0]

        def composite(mid: str, model_row: dict) -> Optional[float]:
            # Confidence-weighted average: each signal's WEIGHT is scaled by its
            # effective confidence, its VALUE is the raw 0-100 score. A missing
            # or low-confidence signal barely moves the result; a single early
            # verdict can't tank a model.
            num = den = 0.0
            for sig, cat in [("primary", category)] + [(s, s) for s in signals]:
                base = w.get(sig, 0.0)
                if base <= 0:
                    continue
                row = best.get((mid, cat))
                if not row or row.get("score") is None:
                    continue
                eff = base * _effective_confidence(row)
                num += eff * row["score"]
                den += eff
            if den <= 0:
                return None
            return (num / den) * self._reliability_multiplier(model_row)

        rows: list[dict] = []
        known = set(models)
        for mid in model_ids:
            m = models.get(mid)
            if m is None:
                # Reachable but unregistered: surface with empty metadata so the
                # brain can fire request_model_research (unknown tier => mid-paid).
                rows.append({"id": mid, "display_name": mid, "provider": None,
                             "context_length": None, "relative_cost_tier": None,
                             "reasoning_style": None, "good_for": None,
                             "benefits_from_explicit_prompting": 0,
                             "tags": None, "content_policy": None,
                             "cost_per_1k_input": None, "cost_per_1k_output": None,
                             "score": None, "score_type": None, "confidence": None,
                             "_composite": None})
                continue
            # Disabled is governance: excluded from candidacy, but its presence
            # in `known` stops it being resurrected as an unknown filler.
            if m.get("enabled") is not None and not m.get("enabled"):
                continue
            m = dict(m)
            prim = best.get((mid, category))
            m["score"] = prim.get("score") if prim else None
            m["score_type"] = prim.get("score_type") if prim else None
            m["confidence"] = prim.get("confidence") if prim else None
            m["_composite"] = composite(mid, m)
            rows.append(m)

        def sort_key(r: dict):
            tier = TIER_RANK.get(r.get("relative_cost_tier"), UNKNOWN_TIER_RANK)
            cl = r.get("context_length")
            fits = 1 if (min_context is not None and cl is not None and cl < min_context) else 0
            # 'prefer' floats permissive to the top (explicit front-door
            # persona); every other mode ignores content policy in ranking.
            is_perm = r.get("content_policy") == "permissive"
            perm = (0 if is_perm else 1) if permissive_mode == "prefer" else 0
            comp = r.get("_composite")
            weighted = comp if comp is not None else -1.0
            return (tier, fits, perm, -weighted)

        rows.sort(key=sort_key)
        out: list[dict] = []
        counts: dict = {}
        for r in rows:
            tier = r.get("relative_cost_tier")
            if counts.get(tier, 0) >= per_tier:
                continue
            counts[tier] = counts.get(tier, 0) + 1
            out.append(r)
            if len(out) >= limit:
                break
        return out

    # -- governance & empirical reliability --------------------------------------------

    def set_enabled(self, model_id: str, enabled: bool) -> None:
        """Governance switch (§ registry redesign item 2): independent of
        reachability, deliberately excludes a model from ranking and tool
        generation. Does NOT touch `source` — it's orthogonal to data
        provenance, so automatic refreshes keep updating a disabled model's
        metadata for the day it's re-enabled."""
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        self.db.execute("UPDATE models SET enabled=? WHERE id=?",
                        (1 if enabled else 0, model_id))

    def record_tool_call(self, model_id: str, ok: bool) -> None:
        """Empirical tool-calling reliability: updated from live traffic — the
        brain's own malformed-call retries and direct-dispatch outcomes —
        because a model can claim tool support in metadata and still misbehave
        in practice. Measures the MODEL's tool-emission validity, NOT whether
        an MCP server call succeeded (a searxng outage is not qwen's fault).

        Each update rolls the ok/failed counters into a `tool_calling`
        benchmark row so ranking can consume this observed signal — measured
        quality, confidence scaled by sample size."""
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        column = "tool_calls_ok" if ok else "tool_calls_failed"
        self.db.execute(
            f"UPDATE models SET {column} = COALESCE({column}, 0) + 1 WHERE id=?",
            (model_id,))
        row = self.get(model_id)
        okc = (row.get("tool_calls_ok") or 0) if row else 0
        failc = (row.get("tool_calls_failed") or 0) if row else 0
        total = okc + failc
        if total:
            self.upsert_benchmark(
                model_id, "tool_calling", 100.0 * okc / total,
                score_type="measured", source_type="observed",
                source_url="observed:live-traffic",
                confidence=observed_confidence(total))

    def record_outcome(self, model_id: str, adequate: bool) -> None:
        """Observed answer quality: the outcome judge's adequate/inadequate
        verdict on a model's real answer — the closest thing to ground truth on
        'was this a good pick', and deployment-specific. Rolls into an
        `adequacy` benchmark (measured/observed, confidence by sample size)."""
        if not model_id:
            return
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        column = "adequacy_ok" if adequate else "adequacy_failed"
        self.db.execute(
            f"UPDATE models SET {column} = COALESCE({column}, 0) + 1 WHERE id=?",
            (model_id,))
        row = self.get(model_id)
        okc = (row.get("adequacy_ok") or 0) if row else 0
        failc = (row.get("adequacy_failed") or 0) if row else 0
        total = okc + failc
        if total:
            self.upsert_benchmark(
                model_id, "adequacy", 100.0 * okc / total,
                score_type="measured", source_type="observed",
                source_url="observed:outcome-judge",
                confidence=observed_confidence(total))

    def record_call_outcome(self, model_id: str, ok: bool) -> None:
        """Call-level reliability: did the dispatch produce usable output, or
        fail/time out/come back empty? Accumulated per model and applied as a
        within-tier score MULTIPLIER (penalty for flaky models) — kept out of
        the additive signal blend so a healthy model gets no artificial boost,
        only an unreliable one gets marked down."""
        if not model_id:
            return
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        column = "calls_ok" if ok else "calls_failed"
        self.db.execute(
            f"UPDATE models SET {column} = COALESCE({column}, 0) + 1 WHERE id=?",
            (model_id,))

    def note_inference(self, model_id: str, eval_count: int,
                       eval_duration_ns: int, load_duration_ns: int = 0) -> None:
        """Fold one model response's timing into observed telemetry.

        WARM inference (eval_count / eval_duration) becomes a running mean of
        tokens/sec and a `latency` benchmark. COLD load (load_duration) is
        tracked in a SEPARATE informational field and never scored — on a
        shared pool most workers aren't resident, so raw latency would punish
        a model for not happening to be warm, not for being slow. Non-Ollama
        backends report zeros here and are skipped."""
        updated = False
        if eval_count > 0 and eval_duration_ns > 0:
            tps = eval_count / (eval_duration_ns / 1e9)
            if self.get(model_id) is None:
                self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
            # exact incremental mean: mean' = (mean*n + x) / (n+1)
            self.db.execute(
                "UPDATE models SET "
                "eval_tps_avg = (COALESCE(eval_tps_avg,0)*COALESCE(eval_samples,0) + ?) "
                "               / (COALESCE(eval_samples,0) + 1), "
                "eval_samples = COALESCE(eval_samples,0) + 1 WHERE id=?",
                (tps, model_id))
            updated = True
        # load_duration is 0 when the model was already warm — only cold loads
        # contribute, giving a "typical cold-load time" stat that stays honest.
        if load_duration_ns > 0:
            if self.get(model_id) is None:
                self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
            self.db.execute(
                "UPDATE models SET "
                "cold_load_ms_avg = (COALESCE(cold_load_ms_avg,0)*COALESCE(cold_load_samples,0) + ?) "
                "                   / (COALESCE(cold_load_samples,0) + 1), "
                "cold_load_samples = COALESCE(cold_load_samples,0) + 1 WHERE id=?",
                (load_duration_ns / 1e6, model_id))
        if updated:
            row = self.get(model_id)
            n = (row.get("eval_samples") or 0) if row else 0
            tps_avg = (row.get("eval_tps_avg") or 0.0) if row else 0.0
            score = min(100.0, 100.0 * tps_avg / LATENCY_TARGET_TPS)
            self.upsert_benchmark(
                model_id, "latency", score, score_type="measured",
                source_type="observed", source_url="observed:warm-eval",
                confidence=observed_confidence(n))

    @staticmethod
    def tool_reliability(row: Optional[dict]) -> Optional[float]:
        """ok/(ok+failed), or None below a minimum sample size."""
        if not row:
            return None
        ok = row.get("tool_calls_ok") or 0
        failed = row.get("tool_calls_failed") or 0
        if ok + failed < 3:
            return None
        return ok / (ok + failed)

    # -- research support -----------------------------------------------------------------

    def set_research_status(self, model_id: str, status: str, note: str = "") -> None:
        """Research lifecycle surfaced per model (queued/running/ok/failed) —
        the UI shows this so research can never fail silently. Also bumps
        last_updated so a failed attempt isn't re-queued every sweep cycle."""
        if self.get(model_id) is None:
            self.db.execute("INSERT INTO models (id) VALUES (?)", (model_id,))
        self.db.execute(
            "UPDATE models SET research_status=?, research_note=?, last_updated=? WHERE id=?",
            (status, (note or "")[:500], utcnow(), model_id))

    def stale_or_missing(self, model_ids: list[str], stale_days: int) -> list[str]:
        if not model_ids:
            return []
        placeholders = ",".join("?" * len(model_ids))
        fresh = {
            r["id"] for r in self.db.query(
                f"""SELECT m.id FROM models m
                    WHERE m.id IN ({placeholders})
                      AND (m.reasoning_style IS NOT NULL
                           OR m.research_status IN ('ok', 'failed'))
                      AND m.last_updated > datetime('now', ?)""",
                model_ids + [f"-{stale_days} days"],
            )
        }
        return [m for m in model_ids if m not in fresh]
