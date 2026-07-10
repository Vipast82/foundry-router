"""SQLite access layer.

One SQLite file on the /data volume holds everything: model registry,
benchmarks, personas, tool overrides, usage log, event log. Plain sqlite3 with
a process-wide lock rather than an async driver — write volume here is tiny
(admin edits, per-request log rows, daily registry refreshes) and one fewer
dependency matches the design doc's §2 minimalism. WAL mode keeps readers from
blocking on writers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# The four starter personas from design doc §4.8 — a starting point, not a
# fixed list; rows are freely editable/addable via the web UI. Seeded with
# INSERT OR IGNORE so user edits are never clobbered on restart.
STARTER_PERSONAS = [
    {
        "virtual_name": "Foundry-Coding",
        "description": "Local-first coding policy (Kilo/Cline default). Escalates only on "
                       "multi-file architecture or an explicit review request.",
        "benchmark_category": "coding",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([
            "multi-file architecture or system design",
            "explicit request for review by Claude",
            "local model failed twice on the same task",
        ]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({"max_paid_calls_per_request": 2}),
    },
    {
        "virtual_name": "Foundry-Chat",
        "description": "General chat (AnythingLLM / Open WebUI default). Cost-aware routing.",
        "benchmark_category": "general_chat",
        "local_bias_strength": "cost_aware_default",
        "escalation_triggers": json.dumps([
            "complex multi-step reasoning",
            "long-document analysis beyond local context limits",
        ]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({}),
    },
    {
        "virtual_name": "Foundry-Research",
        "description": "User-facing research: 'go research X and summarize it'. Uses the "
                       "SearXNG/Crawl4AI MCP tools. Distinct from the background Research "
                       "Agent that maintains the model registry (design doc §4.8 note).",
        "benchmark_category": "agentic",
        "local_bias_strength": "moderate",
        "escalation_triggers": json.dumps([
            "synthesis across many sources",
        ]),
        "preferred_mcp_tools": json.dumps(["searxng", "crawl4ai"]),
        "guardrail_overrides": json.dumps({}),
    },
    {
        "virtual_name": "Foundry-RAG",
        "description": "Retrieval-augmented answering: leans on the connecting workspace's "
                       "own retrieval (e.g., AnythingLLM vector search) rather than picking "
                       "a specific model itself.",
        "benchmark_category": "general_chat",
        "local_bias_strength": "moderate",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({}),
    },
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate()
        self._seed_personas()

    # -- setup ----------------------------------------------------------------

    def _init_schema(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema)
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations. schema.sql's CREATE TABLE IF NOT EXISTS
        covers fresh installs but never alters an existing table, so columns
        added after first deployment are applied here, guarded by PRAGMA
        table_info. Additive-only by design — nothing here drops or rewrites."""
        added = [
            ("models", "tags", "TEXT"),
            ("models", "content_policy", "TEXT"),
            ("models", "research_status", "TEXT"),
            ("models", "research_note", "TEXT"),
            ("models", "enabled", "INTEGER DEFAULT 1"),
            ("models", "tool_calls_ok", "INTEGER DEFAULT 0"),
            ("models", "tool_calls_failed", "INTEGER DEFAULT 0"),
        ]
        with self._lock:
            for table, column, ddl in added:
                cols = {r["name"] for r in
                        self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    log.info("migrated: %s.%s added", table, column)
            self._conn.commit()

    def _seed_personas(self) -> None:
        now = utcnow()
        with self._lock:
            for p in STARTER_PERSONAS:
                self._conn.execute(
                    """INSERT OR IGNORE INTO personas
                       (virtual_name, description, benchmark_category,
                        local_bias_strength, escalation_triggers,
                        preferred_mcp_tools, guardrail_overrides, enabled,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,1,?,?)""",
                    (p["virtual_name"], p["description"], p["benchmark_category"],
                     p["local_bias_strength"], p["escalation_triggers"],
                     p["preferred_mcp_tools"], p["guardrail_overrides"], now, now),
                )
            self._conn.commit()

    # -- generic helpers --------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or cur.rowcount

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(p) for p in seq])
            self._conn.commit()

    # -- kv ----------------------------------------------------------------------

    def kv_get(self, key: str) -> Optional[str]:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- event log (troubleshooting, §4.9 item 7) ---------------------------------

    def log_event(self, level: str, source: str, message: str, detail: str = "") -> None:
        try:
            self.execute(
                "INSERT INTO event_log(ts, level, source, message, detail) VALUES(?,?,?,?,?)",
                (utcnow(), level, source, message[:500], (detail or "")[:4000]),
            )
        except Exception:  # logging must never take down a request
            log.exception("failed to write event_log row")
        if level == "error":
            log.error("[%s] %s %s", source, message, detail[:500] if detail else "")
        elif level == "warning":
            log.warning("[%s] %s", source, message)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
