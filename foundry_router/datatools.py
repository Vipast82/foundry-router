"""Data management: per-table stats, retention pruning, export, clearing.

Everything Foundry records lives in one SQLite file. This module lets the
operator (Logs -> Data) see how big each table is, keep log tables bounded
(`data_retention.days`), export any table — or all of them as one zip — as
JSONL / CSV / JSON for analysis by an AI or a spreadsheet, and clear log
tables. The `kv` table is never exported: it holds auth tokens.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import Database

log = logging.getLogger(__name__)

# Never exported or cleared: holds Meridian / gateway auth tokens.
PRIVATE_TABLES = {"kv", "sqlite_sequence"}
# Append-only logs: safe to prune by age and to clear. Everything else is
# configuration or learned state (personas, models, pricing, eval prompts…)
# — exportable, never cleared from here.
LOG_TABLES = {"request_log", "perf_samples", "server_samples", "event_log", "claude_usage_log",
              "response_feedback", "tool_call_log", "review_log", "eval_runs",
              "eval_results", "semantic_cache", "mcp_call_log", "agent_runs",
              "interventions", "assist_turns", "brain_calls"}
_TS_COLS = ("ts", "created_at", "started_at", "updated_at", "last_updated")
FORMATS = ("jsonl", "csv", "json")


def tables(db: Database) -> list[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r["name"] for r in rows if r["name"] not in PRIVATE_TABLES
            and not r["name"].startswith("sqlite_")]


def _columns(db: Database, table: str) -> list[str]:
    return [r["name"] for r in db.query(f"PRAGMA table_info({table})")]


def _ts_sql(col: str) -> str:
    """A time column as a comparable datetime: ISO text in most tables, epoch
    seconds (REAL/INTEGER) in a few (usage_events)."""
    return (f"(CASE WHEN typeof({col}) IN ('real','integer') THEN datetime({col}, 'unixepoch') "
            f"ELSE datetime({col}) END)")


def ts_column(db: Database, table: str) -> Optional[str]:
    cols = _columns(db, table)
    return next((c for c in _TS_COLS if c in cols), None)


def _check(db: Database, table: str) -> None:
    if table not in tables(db):
        raise ValueError(f"unknown or private table: {table!r}")


def stats(db: Database, retention: Optional[dict] = None) -> list[dict]:
    out = []
    for t in tables(db):
        ts = ts_column(db, t)
        row = db.query_one(
            f"SELECT COUNT(*) AS n" + (f", MIN({ts}) AS oldest, MAX({ts}) AS newest" if ts else "")
            + f" FROM {t}")
        out.append({"table": t, "rows": row["n"] if row else 0,
                    "ts_column": ts, "oldest": (row or {}).get("oldest"),
                    "newest": (row or {}).get("newest"),
                    "log": t in LOG_TABLES,
                    "retention_days": (retention or {}).get(t, 0) if t in LOG_TABLES else None})
    return out


def _cutoff(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _rows(db: Database, table: str, hours: float = 0, limit: int = 0,
          where: Optional[dict] = None) -> list[dict]:
    _check(db, table)
    cols = set(_columns(db, table))
    q, args = f"SELECT * FROM {table}", []
    conds = []
    ts = ts_column(db, table)
    if hours and ts:
        conds.append(f"{_ts_sql(ts)} >= datetime(?)")
        args.append(_cutoff(hours))
    for k, v in (where or {}).items():
        if k in cols and v not in (None, ""):
            conds.append(f"{k} = ?")
            args.append(v)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += f" ORDER BY {ts or 'rowid'}"
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    rows = db.query(q, args)
    for r in rows:                       # blobs (embeddings) aren't text
        for k, v in list(r.items()):
            if isinstance(v, (bytes, bytearray, memoryview)):
                r[k] = f"<{len(bytes(v))} bytes>"
    return rows


def render(rows: list[dict], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=1)
    if fmt == "csv":
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return buf.getvalue()
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def export(db: Database, table: str, fmt: str = "jsonl", hours: float = 0,
           limit: int = 0, where: Optional[dict] = None) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    return render(_rows(db, table, hours, limit, where), fmt)


def export_all(db: Database, fmt: str = "jsonl", hours: float = 0,
               only: Optional[list] = None) -> bytes:
    """Every exportable table (or `only`) as one zip, plus a manifest.json
    describing the tables and their columns — enough for an AI to parse."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    buf = io.BytesIO()
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(),
                "window_hours": hours or None, "format": fmt, "tables": []}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tables(db):
            if only and t not in only:
                continue
            rows = _rows(db, t, hours)
            z.writestr(f"{t}.{fmt}", render(rows, fmt))
            manifest["tables"].append({"table": t, "rows": len(rows),
                                       "columns": _columns(db, t),
                                       "time_column": ts_column(db, t),
                                       "kind": "log" if t in LOG_TABLES else "state"})
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buf.getvalue()


def clear(db: Database, table: str, older_than_days: float = 0) -> int:
    """Delete rows from a LOG table — all of them, or those older than N days."""
    _check(db, table)
    if table not in LOG_TABLES:
        raise ValueError(f"{table} holds configuration/state, not logs — not clearable here")
    ts = ts_column(db, table)
    if older_than_days and ts:
        return db.execute_count(f"DELETE FROM {table} WHERE {_ts_sql(ts)} < datetime(?)",
                          (_cutoff(older_than_days * 24),))
    return db.execute_count(f"DELETE FROM {table}")


def prune(db: Database, days: dict) -> dict:
    """Apply retention: {table: days}; 0 / missing = keep."""
    done = {}
    known = set(tables(db))
    for t, d in (days or {}).items():
        try:
            d = float(d or 0)
        except (TypeError, ValueError):
            continue
        if d <= 0 or t not in LOG_TABLES or t not in known:
            continue
        try:
            n = clear(db, t, older_than_days=d)
            if n:
                done[t] = n
        except Exception:                                         # noqa: BLE001
            log.exception("retention prune failed for %s", t)
    if done:
        db.log_event("info", "retention",
                     "pruned " + ", ".join(f"{t}: {n}" for t, n in done.items()))
    return done
