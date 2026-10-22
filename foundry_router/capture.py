"""Opt-in inference capture for training data (config `training_capture`).

Off by default. When on, Foundry records, for the requests it may capture:

  received      the chat request as the client sent it (OpenAI or Ollama
                dialect): messages in order with roles, system/developer
                text, tool definitions, tool calls / tool results, sampling
                and generation settings. Credentials and auth headers are never
                part of it.
  backend_call  one record per model call attempt the pool made for that
                request: the EFFECTIVE request in Foundry's normalized form
                (model, messages after Foundry's own processing, tools,
                options, max tokens, think, format), the actual backend and
                model, the requested persona, and the outcome: text, tool calls
                (ids, names, arguments), finish reason, usage, timing, error or
                cancellation, and for streams whether the answer was
                reconstructed completely.

Capture boundary (recorded on every row): what Foundry received and what it
handed to the backend adapter. Foundry cannot see an upstream provider's
hidden system prompt or internal processing, nor the backend adapter's
wire-format translation (e.g. OpenAI -> Anthropic Messages).
Reasoning text is stored in its own field and marked as such; training
exports leave it out by default.

Which requests: `projects` (empty = every project a coordinator bound) minus
`exclude_projects`; unbound traffic only with `include_unbound` or when its
persona is listed in `personas`. Per call, `backends` / `exclude_backends` and
`personas` / `exclude_personas` (globs, empty include = all) decide whether
that backend call is recorded (begin_call). Records are
redacted (foundry_training.redact plus every secret Foundry knows) BEFORE they
are written, to a separate SQLite file (default <data>/training/capture.sqlite,
directory 0700, file 0600), pruned after `retention_days`.

The training pipeline reads them through GET /v1/foundry/capture/records
(orchestration scope), ordered by id, so ingestion is incremental and
idempotent.
"""

from __future__ import annotations

import contextvars
import fnmatch
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

BOUNDARY = ("foundry: request as received from the client, and the request Foundry handed "
            "to its backend adapter; not the provider's hidden prompt or the adapter's "
            "wire translation")
SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_uid TEXT UNIQUE,
  ts REAL,
  kind TEXT,                 -- received | backend_call
  request_id TEXT,
  attempt INTEGER,
  client_id TEXT, session_id TEXT, parent_task_id TEXT,
  project_id TEXT, work_task_id TEXT, run_id TEXT, role TEXT,
  persona TEXT, backend TEXT, model TEXT,
  stream INTEGER, status TEXT, reconstruction TEXT,
  redaction TEXT,            -- JSON {status, kinds}
  boundary TEXT,
  body TEXT,                 -- JSON, redacted
  bytes INTEGER, truncated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_capture_project ON capture_records(project_id, id);
CREATE INDEX IF NOT EXISTS idx_capture_ts ON capture_records(ts);
"""

_decision: contextvars.ContextVar = contextvars.ContextVar("foundry_capture_decision",
                                                           default=None)
_received: contextvars.ContextVar = contextvars.ContextVar("foundry_capture_received",
                                                           default=False)
_attempt: contextvars.ContextVar = contextvars.ContextVar("foundry_capture_attempt", default=0)
# the request had no coordinator project (its persona may still switch it on)
_unbound: contextvars.ContextVar = contextvars.ContextVar("foundry_capture_unbound",
                                                          default=False)
# a `received` record held back until a persona-allowed backend call starts
_pending: contextvars.ContextVar = contextvars.ContextVar("foundry_capture_pending",
                                                          default=None)


def glob_match(globs, name: str) -> bool:
    n = (name or "").lower()
    return any(fnmatch.fnmatchcase(n, str(g).lower()) for g in globs or [])


class CaptureStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        new = not self.path.exists()
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def insert(self, row: dict) -> int:
        cols = list(row)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO capture_records ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))})", [row[c] for c in cols])
            return cur.lastrowid

    def query(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def prune(self, days: int) -> int:
        if days <= 0:
            return 0
        with self._lock:
            return self.conn.execute("DELETE FROM capture_records WHERE ts < ?",
                                     (time.time() - days * 86400,)).rowcount


class Capture:
    def __init__(self, get_config, data_dir: Path, secrets_fn=lambda: set()):
        self.get_config = get_config
        self.data_dir = Path(data_dir)
        self.secrets_fn = secrets_fn
        self._store: Optional[CaptureStore] = None
        self._secrets: tuple[float, set] = (0.0, set())
        self.errors = 0
        self.last_error = ""

    @property
    def cfg(self):
        return self.get_config()

    @property
    def store(self) -> CaptureStore:
        if self._store is None:
            p = self.cfg.store_path or str(self.data_dir / "training" / "capture.sqlite")
            self._store = CaptureStore(Path(p))
        return self._store

    # -- what to capture -------------------------------------------------------------

    def wanted(self) -> bool:
        """Decided once per request from the client context (cached)."""
        d = _decision.get()
        if d is not None:
            return d
        cfg = self.cfg
        ok = False
        if cfg and cfg.enabled:
            from . import client_auth
            c = client_auth.current()
            pid = c.get("project_id") or ""
            if c.get("internal"):
                ok = False
            elif pid:
                ok = (not cfg.projects or pid in cfg.projects) and pid not in cfg.exclude_projects
            else:
                # no coordinator project: on with include_unbound, or when a persona
                # was switched on by name (checked per call in allowed())
                ok = bool(cfg.include_unbound or cfg.personas)
                _unbound.set(True)
            if ok and cfg.clients and (c.get("client_id") or "") not in cfg.clients:
                ok = False
        _decision.set(ok)
        return ok

    def allowed(self, backend: str = "", persona: str = "", check_backend: bool = True) -> bool:
        """The per-call switches: backend and persona include / exclude globs.
        A persona named in `personas` is captured even with no coordinator
        project; an unbound call with no such persona needs include_unbound."""
        cfg = self.cfg
        if check_backend and backend:
            if glob_match(cfg.exclude_backends, backend):
                return False
            if cfg.backends and not glob_match(cfg.backends, backend):
                return False
        if glob_match(cfg.exclude_personas, persona):
            return False
        if cfg.personas and not glob_match(cfg.personas, persona):
            return False
        if _unbound.get() and not (cfg.include_unbound or cfg.personas):
            return False
        return True

    def _known_secrets(self) -> set:
        ts, vals = self._secrets
        if time.time() - ts > 60:
            try:
                vals = set(self.secrets_fn() or set())
            except Exception:                                     # noqa: BLE001
                vals = set()
            self._secrets = (time.time(), vals)
        return vals

    def _row(self, kind: str, body: dict, **extra) -> dict:
        from foundry_training.redact import Redactor
        from . import client_auth, request_context
        cfg = self.cfg
        c = client_auth.current()
        red, findings = Redactor(self._known_secrets(),
                                 allowed_email_domains=cfg.allowed_email_domains).obj(body)
        text = json.dumps(red, default=str, ensure_ascii=False)
        truncated = 0
        limit = max(10_000, int(cfg.max_body_bytes or 0) or 4_000_000)
        if len(text.encode()) > limit:
            truncated = 1
            text = json.dumps({"_capture_truncated": True, "bytes": len(text.encode()),
                               "head": text[:limit // 2]}, ensure_ascii=False)
        return {"record_uid": uuid.uuid4().hex, "ts": time.time(), "kind": kind,
                "request_id": request_context.request_id() or "",
                "client_id": c.get("client_id") or "", "session_id": c.get("session_id") or "",
                "parent_task_id": c.get("parent_task_id") or "",
                "project_id": c.get("project_id") or "", "work_task_id": c.get("work_task_id") or "",
                "run_id": c.get("run_id") or "", "role": c.get("role") or "",
                "persona": (request_context.route() or {}).get("persona") or "",
                "redaction": json.dumps({"status": findings.status(), "kinds": findings.kinds()}),
                "boundary": BOUNDARY, "body": text, "bytes": len(text.encode()),
                "truncated": truncated, **extra}

    def _write(self, row: dict) -> None:
        try:
            self.store.insert(row)
        except Exception as e:                                    # noqa: BLE001
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            log.warning("training capture write failed: %s", e)

    # -- hooks -----------------------------------------------------------------------

    def record_received(self, body: dict, dialect: str) -> None:
        if _received.get() or not self.wanted():
            return
        _received.set(True)
        keep = {k: body.get(k) for k in body if k not in ("api_key",)}
        row = ("received", {"dialect": dialect, "request": keep},
               dict(stream=1 if body.get("stream", dialect == "ollama") else 0,
                    status="received", model=str(body.get("model") or ""), ts=time.time()))
        cfg = self.cfg
        if cfg.backends or cfg.exclude_backends or cfg.personas or cfg.exclude_personas:
            # Held until a backend call the switches allow starts: the backend and
            # the routed persona are not known yet, and a request whose only calls
            # are filtered out leaves no orphan `received` behind.
            _pending.set([row])           # a list: shared by reference with child tasks
            return
        self._write(self._row(row[0], row[1], **row[2]))

    @staticmethod
    def _persona() -> str:
        from . import request_context
        return (request_context.route() or {}).get("persona") or ""

    def begin_call(self, backend: str = "", persona: Optional[str] = None) -> Optional[dict]:
        """Start one backend call record (None when not capturing this call)."""
        if not self.wanted():
            return None
        persona = self._persona() if persona is None else persona
        if not self.allowed(backend, persona):
            return None
        pend = _pending.get()
        if pend:
            row = pend.pop(0)             # popped in place: a sibling task's call won't repeat it
            self._write(self._row(row[0], row[1], **row[2]))
        n = _attempt.get() + 1
        _attempt.set(n)
        return {"attempt": n, "t0": time.time()}

    def finish_call(self, tok: Optional[dict], *, model: str, backend: str, messages: list,
                    tools: Any, settings: dict, result: Any = None, error: str = "",
                    stream: bool = False, reconstruction: str = "", status: str = "ok",
                    stream_acc: Optional[dict] = None) -> None:
        if tok is None:
            return
        out: dict[str, Any] = {}
        if result is not None:
            out = {"content": result.content, "tool_calls": result.tool_calls,
                   "reasoning": getattr(result, "thinking", "") or "",
                   "finish_reason": result.finish_reason,
                   "usage": {"prompt_tokens": result.prompt_tokens,
                             "completion_tokens": result.completion_tokens,
                             "cached_tokens": getattr(result, "cached_tokens", 0)}}
        elif stream_acc is not None:
            out = {"content": stream_acc.get("content", ""),
                   "tool_calls": stream_acc.get("tool_calls") or [],
                   "reasoning": stream_acc.get("thinking", ""),
                   "finish_reason": stream_acc.get("finish_reason") or "",
                   "usage": {"prompt_tokens": stream_acc.get("pt", 0),
                             "completion_tokens": stream_acc.get("ct", 0),
                             "cached_tokens": stream_acc.get("cached", 0)}}
        body = {"request": {"model": model, "messages": messages, "tools": tools,
                            "settings": {k: v for k, v in settings.items() if v is not None}},
                "response": out, "error": error[:2000] if error else "",
                "duration_ms": int((time.time() - tok["t0"]) * 1000),
                "reasoning_field": "response.reasoning (exclude from default training exports)"}
        self._write(self._row("backend_call", body, attempt=tok["attempt"], backend=backend,
                              model=model, stream=1 if stream else 0, status=status,
                              reconstruction=reconstruction or ("n/a" if not stream else "")))

    # -- reading ----------------------------------------------------------------------

    def records(self, after_id: int = 0, limit: int = 200, project: str = "", *,
                backend: str = "", persona: str = "", kind: str = "", since: float = 0.0,
                db=None) -> list[dict]:
        """Oldest first after `after_id`. With `db` (the router database) each
        row also carries `quality`: request outcome, feedback, perf and the
        coordinator's labels (training_quality.attach)."""
        q = "SELECT * FROM capture_records WHERE id > ?"
        args: list = [after_id]
        for col, v in (("project_id", project), ("backend", backend), ("persona", persona),
                       ("kind", kind)):
            if v:
                q += f" AND {col} = ?"
                args.append(v)
        if since:
            q += " AND ts >= ?"
            args.append(since)
        q += " ORDER BY id LIMIT ?"
        args.append(max(1, min(1000, limit)))
        rows = self.store.query(q, args)
        for r in rows:
            r["body"] = json.loads(r["body"] or "{}")
            r["redaction"] = json.loads(r["redaction"] or "{}")
        if db is not None:
            from . import training_quality
            training_quality.attach(db, rows)
        return rows

    def counts_today(self) -> dict:
        """Backend calls captured since local midnight, by backend and persona."""
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        out: dict = {"since": midnight, "backends": {}, "personas": {}, "total": 0}
        for col, key in (("backend", "backends"), ("persona", "personas")):
            for r in self.store.query(
                    f"SELECT {col} AS k, COUNT(*) AS n FROM capture_records "
                    f"WHERE kind='backend_call' AND ts >= ? GROUP BY {col}", (midnight,)):
                out[key][r["k"] or ""] = r["n"]
        out["total"] = sum(out["backends"].values())
        return out

    def status(self) -> dict:
        cfg = self.cfg
        out = {"enabled": bool(cfg and cfg.enabled), "projects": list(cfg.projects or []),
               "exclude_projects": list(cfg.exclude_projects or []),
               "include_unbound": bool(cfg.include_unbound), "retention_days": cfg.retention_days,
               "backends": list(cfg.backends or []),
               "exclude_backends": list(cfg.exclude_backends or []),
               "personas": list(cfg.personas or []),
               "exclude_personas": list(cfg.exclude_personas or []),
               "boundary": BOUNDARY, "errors": self.errors, "last_error": self.last_error}
        if self._store is not None or (cfg and cfg.enabled):
            try:
                rows = self.store.query(
                    "SELECT project_id, kind, COUNT(*) AS n, SUM(truncated) AS truncated, "
                    "SUM(CASE WHEN redaction LIKE '%\"redacted\"%' THEN 1 ELSE 0 END) AS redacted, "
                    "MAX(ts) AS last_ts FROM capture_records GROUP BY project_id, kind")
                out["counts"] = rows
                out["today"] = self.counts_today()
                out["store"] = str(self.store.path)
            except Exception as e:                                # noqa: BLE001
                out["store_error"] = str(e)[:200]
        return out


def reset_request() -> None:
    """A new HTTP request: decide afresh whether to capture it."""
    _decision.set(None)
    _received.set(False)
    _attempt.set(0)
    _unbound.set(False)
    _pending.set(None)


def stream_accumulator() -> dict:
    return {"content": "", "thinking": "", "tool_calls": [], "finish_reason": "", "pt": 0,
            "ct": 0, "cached": 0, "done": False}


def accumulate(acc: dict, chunk: dict) -> None:
    if chunk.get("content"):
        acc["content"] += chunk["content"]
    if chunk.get("thinking"):
        acc["thinking"] += chunk["thinking"]
    if chunk.get("tool_calls"):
        acc["tool_calls"].extend(chunk["tool_calls"])
    if chunk.get("done"):
        acc["done"] = True
        acc["finish_reason"] = chunk.get("finish_reason") or chunk.get("done_reason") or ""
        acc["pt"] = chunk.get("prompt_tokens") or 0
        acc["ct"] = chunk.get("completion_tokens") or 0
        acc["cached"] = chunk.get("cached_tokens") or 0
