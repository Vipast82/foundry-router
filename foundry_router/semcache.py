"""Semantic response cache (quality spec Phase 3).

A repeated question skips the whole routing loop and serves the stored answer
— visibly badged, never silently. Scope is deliberately narrow, because a
wrong cache hit costs more trust than a miss costs latency:

  - only fresh single-turn conversations (one user message, no assistant
    history): follow-ups derive meaning from context a similarity match on
    the last message can't see;
  - never for agent/tool-calling workloads (category TTL 0, or any persona
    with MCP tools attached) — freshness and side effects beat latency there;
  - never for requests carrying images or resuming a pending question.

Embeddings come from a CONFIGURED Ollama-compatible /api/embed endpoint
(host + model are settings, live-editable). Vector search uses sqlite-vec's
vec_distance_cosine when the extension loads into the existing SQLite
connection; otherwise a pure-Python scan over the same float32 BLOBs — same
one-file-SQLite infra either way, no new service. Vectors are L2-normalized
at store time so both paths agree.

Every failure degrades to "no cache": an embedding outage must never take
down routing.
"""

from __future__ import annotations

import json
import logging
import math
import struct
from datetime import datetime, timezone
from typing import Any, Optional

from .config import SemanticCacheConfig
from .db import Database, utcnow

log = logging.getLogger(__name__)


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _normalize(vec: list[float]) -> Optional[list[float]]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return None
    return [v / norm for v in vec]


class SemanticCache:
    def __init__(self, cfg: SemanticCacheConfig, http, db: Database):
        self.cfg = cfg
        self.http = http
        self.db = db
        self._embed_down_logged = False  # edge-triggered outage alert
        self._vec_loaded = self._try_load_sqlite_vec()

    # -- sqlite-vec (optional acceleration; identical semantics) --------------

    def _try_load_sqlite_vec(self) -> bool:
        try:
            import sqlite_vec  # type: ignore
            conn = self.db._conn
            with self.db._lock:
                conn.enable_load_extension(True)
                try:
                    sqlite_vec.load(conn)
                finally:
                    conn.enable_load_extension(False)
            log.info("sqlite-vec loaded — cache similarity runs in SQL")
            return True
        except Exception as e:
            log.info("sqlite-vec unavailable (%s) — using Python cosine fallback", e)
            return False

    # -- embedding client ------------------------------------------------------

    async def embed(self, text: str) -> Optional[list[float]]:
        """One L2-normalized embedding from the configured endpoint, or None
        (unconfigured, unreachable, bad payload — all degrade to no-cache)."""
        url = (self.cfg.embed_url or "").rstrip("/")
        model = (self.cfg.embed_model or "").strip()
        if not url or not model:
            return None
        headers = {}
        if self.cfg.embed_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.embed_api_key}"
        try:
            # Modern Ollama shape first; fall back to the legacy endpoint.
            r = await self.http.post(f"{url}/api/embed",
                                     json={"model": model, "input": text},
                                     headers=headers, timeout=20)
            if r.status_code == 404:
                r = await self.http.post(f"{url}/api/embeddings",
                                         json={"model": model, "prompt": text},
                                         headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            vec = (data.get("embeddings") or [None])[0] or data.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise ValueError("no embedding in response")
            if self._embed_down_logged:
                self._embed_down_logged = False
                self.db.log_event("info", "semcache",
                                  "embedding endpoint recovered — cache active again")
            return _normalize([float(v) for v in vec])
        except Exception as e:
            if not self._embed_down_logged:
                self._embed_down_logged = True
                self.db.log_event(
                    "warning", "semcache",
                    f"embedding endpoint unavailable — semantic cache inactive "
                    f"until it recovers ({self.cfg.embed_url})", str(e))
            return None

    async def test_embed(self, url: Optional[str] = None,
                         model: Optional[str] = None,
                         api_key: Optional[str] = None) -> dict:
        """Verify-in-place probe (the GUI 'Test embedding' button), same
        pattern as the brain/backend/research tests. Unlike embed() — which
        swallows failures to degrade to no-cache — this surfaces the real
        error, the embedding dimension, and latency so misconfiguration is
        diagnosable without watching the Events log. Tests the values PASSED
        (what's in the form) so it works before Save; falls back to the saved
        config when a field is omitted."""
        import time

        from .errors import describe_exception
        url = ((url if url is not None else self.cfg.embed_url) or "").rstrip("/")
        model = (model if model is not None else self.cfg.embed_model or "").strip()
        api_key = api_key if api_key is not None else self.cfg.embed_api_key
        if not url or not model:
            return {"ok": False, "error": "embed_url and embed_model are both "
                    "required", "sqlite_vec": self._vec_loaded}
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        t0 = time.monotonic()
        try:
            r = await self.http.post(f"{url}/api/embed",
                                     json={"model": model, "input": "health check"},
                                     headers=headers, timeout=20)
            endpoint = "/api/embed"
            if r.status_code == 404:
                r = await self.http.post(f"{url}/api/embeddings",
                                         json={"model": model, "prompt": "health check"},
                                         headers=headers, timeout=20)
                endpoint = "/api/embeddings"
            r.raise_for_status()
            data = r.json()
            vec = (data.get("embeddings") or [None])[0] or data.get("embedding")
            if not isinstance(vec, list) or not vec:
                return {"ok": False, "error": "endpoint reachable but returned no "
                        "embedding (is the model name correct and pulled on the "
                        "host?)", "latency_ms": int((time.monotonic() - t0) * 1000),
                        "sqlite_vec": self._vec_loaded}
            return {"ok": True, "dimension": len(vec), "endpoint": endpoint,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "sqlite_vec": self._vec_loaded, "error": ""}
        except Exception as e:
            return {"ok": False, "error": describe_exception(e),
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    "sqlite_vec": self._vec_loaded}

    # -- eligibility -----------------------------------------------------------

    def _category_ttl(self, category: str) -> int:
        ttl = self.cfg.category_ttls.get(category or "general_chat")
        return int(self.cfg.default_ttl_seconds if ttl is None else ttl)

    def eligibility(self, persona: Optional[dict],
                    messages: list[dict]) -> tuple[bool, str]:
        """(eligible, reason). The reasons feed narration/logs so a bypass is
        explainable, not mysterious."""
        if not self.cfg.enabled:
            return False, "cache disabled"
        if not persona:
            return False, "no persona"
        try:
            has_tools = bool(json.loads(persona.get("preferred_mcp_tools") or "[]"))
        except (json.JSONDecodeError, TypeError):
            has_tools = False
        if has_tools:
            return False, "persona has MCP tools attached (agent workload)"
        category = persona.get("benchmark_category") or "general_chat"
        if self._category_ttl(category) <= 0:
            return False, f"category {category} bypasses cache"
        users = [m for m in messages if m.get("role") == "user"]
        if len(users) != 1 or any(m.get("role") == "assistant" for m in messages):
            return False, "multi-turn conversation"
        if any(m.get("images") for m in messages):
            return False, "images attached"
        if not (users[0].get("content") or "").strip():
            return False, "empty prompt"
        return True, ""

    # -- lookup / store --------------------------------------------------------

    async def lookup(self, persona: dict, user_text: str) -> Optional[dict]:
        """Best sufficiently-similar unexpired entry for this persona, or
        None. A hit bumps the entry's hit counters."""
        vec = await self.embed(user_text)
        if vec is None:
            return None
        persona_name = persona.get("virtual_name")
        blob = _pack(vec)
        if self._vec_loaded:
            rows = self.db.query(
                "SELECT id, ts, answer, ttl_seconds, "
                "1.0 - vec_distance_cosine(embedding, ?) AS sim "
                "FROM semantic_cache WHERE persona=? AND dim=? "
                "ORDER BY sim DESC LIMIT 5", (blob, persona_name, len(vec)))
        else:
            rows = []
            for r in self.db.query(
                    "SELECT id, ts, answer, ttl_seconds, embedding "
                    "FROM semantic_cache WHERE persona=? AND dim=?",
                    (persona_name, len(vec))):
                stored = _unpack(r["embedding"])
                r["sim"] = sum(a * b for a, b in zip(vec, stored))
                rows.append(r)
            rows.sort(key=lambda r: -r["sim"])
        now = datetime.now(timezone.utc)
        for r in rows[:5]:
            if r["sim"] < self.cfg.min_similarity:
                break  # sorted — nothing further can clear the bar
            try:
                age = (now - datetime.fromisoformat(r["ts"])).total_seconds()
            except (ValueError, TypeError):
                continue
            if age > (r["ttl_seconds"] or 0):
                continue  # expired; purge sweeps it later
            self.db.execute(
                "UPDATE semantic_cache SET hits=hits+1, last_hit=? WHERE id=?",
                (utcnow(), r["id"]))
            return {"answer": r["answer"], "similarity": round(r["sim"], 4),
                    "age_seconds": int(age), "id": r["id"]}
        return None

    async def store(self, persona: dict, user_text: str, answer: str) -> bool:
        if not answer.strip():
            return False
        vec = await self.embed(user_text)
        if vec is None:
            return False
        category = persona.get("benchmark_category") or "general_chat"
        ttl = self._category_ttl(category)
        if ttl <= 0:
            return False
        self.db.execute(
            "INSERT INTO semantic_cache (ts, persona, category, prompt, answer, "
            "embedding, dim, ttl_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (utcnow(), persona.get("virtual_name"), category,
             user_text[:2000], answer, _pack(vec), len(vec), ttl))
        self.purge()
        return True

    def purge(self) -> int:
        """Drop expired entries, then enforce max_entries (oldest-activity
        first). Called on every store — cheap at these sizes."""
        removed = self.db.execute(
            "DELETE FROM semantic_cache WHERE "
            "strftime('%s','now') - strftime('%s', ts) > ttl_seconds")
        over = self.db.query_one("SELECT COUNT(*) AS n FROM semantic_cache")
        excess = (over["n"] if over else 0) - int(self.cfg.max_entries)
        if excess > 0:
            self.db.execute(
                "DELETE FROM semantic_cache WHERE id IN ("
                "SELECT id FROM semantic_cache "
                "ORDER BY COALESCE(last_hit, ts) ASC LIMIT ?)", (excess,))
        return max(0, removed)

    def clear(self) -> int:
        return self.db.execute("DELETE FROM semantic_cache")

    def stats(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits "
            "FROM semantic_cache") or {"entries": 0, "hits": 0}
        return {"entries": row["entries"], "total_hits": row["hits"],
                "sqlite_vec": self._vec_loaded,
                "enabled": self.cfg.enabled,
                "embed_url": self.cfg.embed_url,
                "embed_model": self.cfg.embed_model}


def cache_badge(similarity: float, age_seconds: int) -> str:
    """Visible cache-hit marker (same transparency convention as the review
    pass's 🔎): a served-from-cache answer must never look freshly generated."""
    if age_seconds < 3600:
        age = f"{max(1, age_seconds // 60)}m ago"
    elif age_seconds < 86400:
        age = f"{age_seconds // 3600}h ago"
    else:
        age = f"{age_seconds // 86400}d ago"
    return (f"\n\n---\n⚡ *cached answer (similarity {similarity:.0%}, "
            f"originally answered {age})*")
