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


# Starter personas (design doc §4.8 + persona-expansion spec) — a starting
# point, not a fixed list; rows are freely editable/addable via the web UI.
# Seeded with INSERT OR IGNORE so user edits are never clobbered on restart.
STARTER_PERSONAS = [
    {
        "virtual_name": "Foundry-Coding",
        "description": "Coding pipeline: Prepare (cheapest Claude structures the request) -> "
                       "Execute (best measured local coder) -> Check (paid review, one retry).",
        "benchmark_category": "coding",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({"max_paid_calls_per_request": 2}),
        "execution_mode": "pipeline",
    },
    {
        "virtual_name": "Foundry-Chat",
        "description": "General chat (AnythingLLM / Open WebUI default). Cost-aware routing; "
                       "generative-media requests route to media MCP tools when connected.",
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
                       "SearXNG/Crawl4AI MCP tools; local-first with outcome-judged escalation.",
        "benchmark_category": "agentic",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps(["searxng", "crawl4ai"]),
        "guardrail_overrides": json.dumps({}),
        "outcome_judge": "local_large",
    },
    {
        "virtual_name": "Foundry-RAG",
        "description": "Retrieval-augmented answering: leans on the connecting workspace's "
                       "own retrieval; local-first with outcome-judged escalation.",
        "benchmark_category": "general_chat",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({}),
        "outcome_judge": "local_large",
    },
    {
        "virtual_name": "Foundry-Vision",
        "description": "Image/document UNDERSTANDING (describe this screenshot, read this "
                       "scan) — distinct from image generation. Candidates filtered to "
                       "vision-tagged models.",
        "benchmark_category": "general_chat",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({}),
        "required_tags": json.dumps(["vision"]),
    },
    {
        "virtual_name": "Foundry-Creative",
        "description": "Open creative writing / roleplay / NSFW front door. Strong local "
                       "bias; PERMISSIVE-tagged local models preferred by default.",
        "benchmark_category": "general_chat",
        "local_bias_strength": "strong",
        "escalation_triggers": json.dumps([]),
        "preferred_mcp_tools": json.dumps([]),
        "guardrail_overrides": json.dumps({"max_paid_calls_per_request": 1}),
        "prefer_permissive": 1,
    },
    {
        "virtual_name": "Foundry-Agent",
        "description": "Full multi-step agentic loop with tool access for genuinely "
                       "multi-part tasks across several tools.",
        "benchmark_category": "agentic",
        "local_bias_strength": "moderate",
        "escalation_triggers": json.dumps(["task requires coordinating several tools"]),
        "preferred_mcp_tools": json.dumps(["searxng", "crawl4ai"]),
        "guardrail_overrides": json.dumps({"max_steps_per_request": 20}),
    },
]


# Baseline client-compatibility notes (quality spec Phase 5) — documentation
# metadata, not behavior. One persona serves multiple clients; these record
# HOW each client experiences it, so nobody forks a persona just to write
# this down. Freely editable per persona in the web UI afterward.
_ANYTHINGLLM_AGENT_NOTE = (
    "no live HTML render (no canvas yet) — rich output shows as code blocks. "
    "Built-in Document Generation (PDF/DOCX/XLSX/PPTX) and Filesystem agent "
    "skills work in @agent mode, but depend on the routed model's tool-calling "
    "reliability (see the Models tab counters). @agent mode also brings its own "
    "web tools, bypassing Foundry's MCP servers (requests log as 'direct').")

STARTER_CLIENT_COMPAT = {
    "Foundry-Chat": {
        "openwebui": "HTML/SVG/JS code blocks render as live Artifacts natively "
                     "— no special prompting needed.",
        "anythingllm": _ANYTHINGLLM_AGENT_NOTE,
        "messaging-bridges": "for Hermes/OpenClaw-style bridges prefer a persona "
                             "with output_style=plain_text — chat platforms "
                             "can't render HTML.",
    },
    "Foundry-Coding": {
        "openwebui": "generated HTML/SVG/JS previews live as Artifacts.",
        "anythingllm": "code shows as standard code blocks; no live preview.",
        "kilo-cline": "agent loops send their own tool definitions — requests "
                      "direct-dispatch to one model per persona policy.",
    },
    "Foundry-Research": {
        "openwebui": "works as a normal chat model; sources cited as links.",
        "anythingllm": _ANYTHINGLLM_AGENT_NOTE,
    },
    "Foundry-RAG": {
        "anythingllm": "designed for workspace RAG: AnythingLLM injects "
                       "retrieved context into the system prompt; Foundry "
                       "relays it to workers.",
        "openwebui": "pairs with Open WebUI's knowledge collections the same way.",
    },
    "Foundry-Vision": {
        "openwebui": "attach images in chat; routed to vision-tagged models.",
        "anythingllm": "image attachments supported in chat mode.",
    },
    "Foundry-Creative": {
        "openwebui": "plain prose output — renders everywhere.",
        "messaging-bridges": "suitable as-is for plain-text bridges; set "
                             "output_style=plain_text to enforce it.",
    },
    "Foundry-Agent": {
        "anythingllm": _ANYTHINGLLM_AGENT_NOTE,
        "messaging-bridges": "for Hermes/OpenClaw bridges set "
                             "output_style=plain_text — tool results and "
                             "answers arrive as clean text.",
    },
}


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
            # 1 = embedding-only model (no /api/chat): excluded from chat routing
            # candidacy. Set from a name heuristic + Ollama capabilities, and
            # learned from a "does not support chat" dispatch error.
            ("models", "embedding", "INTEGER DEFAULT 0"),
            ("models", "research_status", "TEXT"),
            ("models", "research_note", "TEXT"),
            ("models", "enabled", "INTEGER DEFAULT 1"),
            ("models", "tool_calls_ok", "INTEGER DEFAULT 0"),
            ("models", "tool_calls_failed", "INTEGER DEFAULT 0"),
            ("personas", "pinned_models", "TEXT"),
            ("personas", "execution_mode", "TEXT"),
            ("personas", "pipeline_check_enabled", "INTEGER DEFAULT 1"),
            ("personas", "outcome_judge", "TEXT"),
            ("personas", "required_tags", "TEXT"),
            ("personas", "prefer_permissive", "INTEGER DEFAULT 0"),
            ("personas", "prefer_loaded", "INTEGER DEFAULT 0"),
            ("request_log", "tool_calls", "TEXT"),
            ("models", "eval_tps_avg", "REAL"),
            ("models", "eval_samples", "INTEGER DEFAULT 0"),
            ("models", "cold_load_ms_avg", "REAL"),
            ("models", "cold_load_samples", "INTEGER DEFAULT 0"),
            ("models", "adequacy_ok", "INTEGER DEFAULT 0"),
            ("models", "adequacy_failed", "INTEGER DEFAULT 0"),
            ("models", "calls_ok", "INTEGER DEFAULT 0"),
            ("models", "calls_failed", "INTEGER DEFAULT 0"),
            ("personas", "selection_weights", "TEXT"),
            ("model_named_benchmarks", "source", "TEXT DEFAULT 'research'"),
            ("personas", "brain_handles_tools", "INTEGER DEFAULT 0"),
            ("personas", "context_window", "INTEGER"),
            # Tiered review pass (quality spec Phase 2): OFF by default;
            # enabling requires an explicit review_model selection. The
            # prefilter is the free brain-based "is review even warranted"
            # screen that runs before the (possibly paid) review model.
            ("personas", "review_enabled", "INTEGER DEFAULT 0"),
            ("personas", "review_model", "TEXT"),
            ("personas", "review_prefilter", "INTEGER DEFAULT 1"),
            # Client-aware output profiles (quality spec Phase 5): NOT a
            # formatter — client_compat is documentation metadata (JSON object
            # client -> note) surfaced in the GUI so it's visible which
            # clients a persona is tuned for, instead of forking a persona
            # per client. output_style is the one behavioral knob:
            # "plain_text" steers workers to markdown/plain output for
            # messaging-bridge clients (Hermes/OpenClaw) that can't render
            # HTML at all.
            ("personas", "client_compat", "TEXT"),
            ("personas", "output_style", "TEXT"),
            # Code-sandbox audit trail: the submitted code (call arguments) and
            # a flag marking calls that ran on an executes_code server.
            ("tool_call_log", "arguments", "TEXT"),
            ("tool_call_log", "executed_code", "INTEGER DEFAULT 0"),
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
                        preferred_mcp_tools, guardrail_overrides,
                        execution_mode, pipeline_check_enabled, outcome_judge,
                        required_tags, prefer_permissive, enabled,
                        created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (p["virtual_name"], p["description"], p["benchmark_category"],
                     p["local_bias_strength"], p["escalation_triggers"],
                     p["preferred_mcp_tools"], p["guardrail_overrides"],
                     p.get("execution_mode"), p.get("pipeline_check_enabled", 1),
                     p.get("outcome_judge"), p.get("required_tags"),
                     p.get("prefer_permissive", 0), now, now),
                )
            self._conn.commit()
        self._seed_upgrades()
        self._seed_client_compat()

    def _seed_upgrades(self) -> None:
        """One-time data upgrades for EXISTING deployments (INSERT OR IGNORE
        can't change rows that already exist). kv-flagged so it never re-runs
        and never fights later manual edits. New installs get the same values
        straight from STARTER_PERSONAS."""
        if self.kv_get("persona_seed_v2"):
            return
        now = utcnow()
        # Persona-expansion spec §3: RAG/Research go strong + outcome-judged
        # escalation instead of static trigger phrases; §1: Foundry-Coding
        # becomes the Prepare->Execute->Check pipeline.
        self.execute(
            "UPDATE personas SET local_bias_strength='strong', "
            "outcome_judge='local_large', escalation_triggers='[]', updated_at=? "
            "WHERE virtual_name IN ('Foundry-RAG', 'Foundry-Research')", (now,))
        self.execute(
            "UPDATE personas SET execution_mode='pipeline', updated_at=? "
            "WHERE virtual_name='Foundry-Coding'", (now,))
        self.kv_set("persona_seed_v2", utcnow())
        self.log_event("info", "main",
                       "persona seed v2 applied (pipeline mode, outcome-judged escalation)")

    def _seed_client_compat(self) -> None:
        """Baseline client-compat notes (Phase 5), once, and only into rows
        that don't already carry notes — the field is the operator's to edit."""
        if self.kv_get("persona_seed_v3_compat"):
            return
        now = utcnow()
        for name, compat in STARTER_CLIENT_COMPAT.items():
            self.execute(
                "UPDATE personas SET client_compat=?, updated_at=? "
                "WHERE virtual_name=? AND (client_compat IS NULL OR client_compat='')",
                (json.dumps(compat), now, name))
        self.kv_set("persona_seed_v3_compat", now)
        self.log_event("info", "main",
                       "persona seed v3 applied (baseline client-compat notes)")

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

    def kv_del(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key=?", (key,))

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
