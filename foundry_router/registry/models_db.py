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

    # -- the routing query -------------------------------------------------------------

    def ranked_for_category(self, category: str, model_ids: list[str],
                            limit: int = 20, per_tier: int = 5) -> list[dict]:
        """Candidates among the currently-reachable models (`model_ids` from
        the Backend Pool), two-level sorted: cost tier first (free/local ->
        cheap -> ... -> premium), confidence-weighted quality score second
        within a tier. Manual_override benchmark rows outrank automatic ones.

        Capped PER TIER (not just overall) deliberately: a flat top-N with
        tier-first sorting would fill every slot with local models and push
        Claude off the brain's candidate list entirely, silently breaking
        escalation — the earlier LIMIT 12 dropout, inverted. Per-tier caps
        keep local leading AND premium visible.

        Models with no benchmark row still appear (bottom of their tier) so
        the brain knows they exist and can fire request_model_research."""
        if not model_ids:
            return []
        placeholders = ",".join("?" * len(model_ids))
        rows = self.db.query(
            f"""
            SELECT m.*, b.score, b.score_type, b.confidence
            FROM models m
            LEFT JOIN model_benchmarks b
              ON b.model_id = m.id AND b.category = ?
             AND b.id = (
                   SELECT b2.id FROM model_benchmarks b2
                   WHERE b2.model_id = m.id AND b2.category = ?
                   ORDER BY (b2.source_type = 'manual_override') DESC,
                            (b2.score * COALESCE(b2.confidence, 0.5)) DESC
                   LIMIT 1)
            WHERE m.id IN ({placeholders})
            """,
            [category, category] + model_ids,
        )
        # `known` includes disabled models ON PURPOSE: they're filtered out
        # below, but must not be resurrected as unknown-model fillers —
        # disable is governance (exclude entirely), not absence of data.
        known = {r["id"] for r in rows}
        rows = [r for r in rows if (r.get("enabled") if r.get("enabled") is not None else 1)]
        # Reachable models with no registry row at all still matter — surface
        # them with empty metadata (unknown tier => mid-paid conservative) so
        # the brain can fire request_model_research (§4.4 on-demand trigger).
        for mid in model_ids:
            if mid not in known:
                rows.append({"id": mid, "display_name": mid, "provider": None,
                             "context_length": None, "relative_cost_tier": None,
                             "reasoning_style": None, "good_for": None,
                             "benefits_from_explicit_prompting": 0,
                             "tags": None, "content_policy": None,
                             "cost_per_1k_input": None, "cost_per_1k_output": None,
                             "score": None, "score_type": None, "confidence": None})

        def sort_key(r: dict):
            tier = TIER_RANK.get(r.get("relative_cost_tier"), UNKNOWN_TIER_RANK)
            if r.get("score") is not None:
                weighted = r["score"] * (r.get("confidence") or 0.5)
            else:
                weighted = -1.0  # unscored sinks to the bottom of its tier
            return (tier, -weighted)

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
