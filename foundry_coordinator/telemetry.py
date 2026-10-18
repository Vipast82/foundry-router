"""Workstation telemetry bridge for the Foundry control center (read-only).

Runs next to Kilo on the workstation, where `http://127.0.0.1:4096` is
reachable and the Kilo password lives. It reads:

  * the coordinator's SQLite store, read-only (task state, sessions, transitions,
    the engine's own heartbeat), and the project profile (approval artifacts,
    limits) — the coordinator's facts
  * Kilo's supported `kilo serve` API with GET requests and its `/event` stream:
    session status, parent/child sessions, messages and parts (text, the
    reasoning Kilo publishes, tools), todo, pending questions/permissions,
    workspace MCP status, LSP / indexing / memory status, an allowlisted
    projection of the effective config — the Kilo facts

and sends normalized, redacted, size-capped observations to Foundry
(`foundry.cc.v1`, POST /v1/foundry/telemetry/batch) plus a heartbeat. It never
prompts, answers, aborts or reconfigures anything, and makes no model call.
Kilo's credentials never leave this process. One bridge serves every viewer:
browsers read Foundry, not Kilo.

Endpoints are probed at runtime (server 7.8.8 vs extension 7.8.12): an endpoint
that 404s is recorded as unsupported and its data shown as unavailable.

Event names (message.updated, message.part.updated, message.part.delta,
session.status, todo.updated, question.asked/replied, permission.asked/replied)
come from the Kilo 7.8.12 SDK bundled with the VS Code extension. Events only
speed things up: every busy session is also re-read in full periodically, and
an idle session once after it settles, so a missed event can't lose a part.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Optional

CONTRACT = "foundry.cc.v1"
BATCH = 400
MAX_TEXT = 60000
MAX_TOOL = 20000


def _strip_ts(v):
    if isinstance(v, dict):
        return {k: _strip_ts(x) for k, x in v.items() if k != "observed_ts"}
    if isinstance(v, list):
        return [_strip_ts(x) for x in v]
    return v


def _h(v) -> str:
    """Change fingerprint: observation times don't count as a change."""
    return hashlib.sha256(json.dumps(_strip_ts(v), sort_keys=True, default=str).encode()
                          ).hexdigest()[:20]


def _ms(v) -> Optional[float]:
    try:
        return float(v) / 1000.0 if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _cap(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str) if v is not None else ""
    return s[:n]


# --------------------------------------------------------------------------- Kilo (GET only)

class KiloObserver:
    """Read-only client of `kilo serve`. GET requests and the event stream only."""

    def __init__(self, url: str, password: str = "", username: str = "kilo", timeout: float = 15):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._auth = ("Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
                      if password else "")
        self.caps: dict[str, bool] = {}
        self.secret = password

    def _open(self, path: str, query: Optional[dict] = None, timeout: Optional[float] = None,
              accept: str = "application/json"):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method="GET")
        req.add_header("accept", accept)
        if self._auth:
            req.add_header("authorization", self._auth)
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)

    def get(self, path: str, query: Optional[dict] = None, cap: str = "", optional: bool = False):
        """JSON or None. `optional` endpoints that 404/405/501 are marked
        unsupported in self.caps (under `cap`) instead of raising."""
        try:
            with self._open(path, query) as r:
                raw = r.read()
            if cap:
                self.caps[cap] = True
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code in (404, 405, 501) and optional:
                if cap:
                    self.caps[cap] = False
                return None
            raise KiloUnavailable(f"GET {path}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise KiloUnavailable(f"GET {path}: {e}") from None

    def health(self) -> dict:
        return self.get("/global/health") or {}

    def statuses(self, d: str) -> dict:
        return self.get("/session/status", {"directory": d}) or {}

    def session(self, sid: str, d: str) -> Optional[dict]:
        try:
            with self._open(f"/session/{urllib.parse.quote(sid)}", {"directory": d}) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise KiloUnavailable(f"GET /session/{sid}: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise KiloUnavailable(str(e)) from None

    def messages(self, sid: str, d: str) -> list:
        return self.get(f"/session/{urllib.parse.quote(sid)}/message", {"directory": d}) or []

    def children(self, sid: str, d: str) -> list:
        return self.get(f"/session/{urllib.parse.quote(sid)}/children", {"directory": d}) or []

    def todo(self, sid: str, d: str):
        return self.get(f"/session/{urllib.parse.quote(sid)}/todo", {"directory": d}, cap="todo",
                        optional=True)

    def questions(self, d: str) -> list:
        return self.get("/question", {"directory": d}, cap="question", optional=True) or []

    def permissions(self, d: str) -> list:
        return self.get("/permission", {"directory": d}, cap="permission", optional=True) or []

    def mcp(self, d: str):
        return self.get("/mcp", {"directory": d}, cap="mcp", optional=True)

    def config(self, d: str):
        return self.get("/config/effective", {"directory": d}, cap="config", optional=True)

    def capability(self, name: str, path: str, d: str):
        return self.get(path, {"directory": d}, cap=name, optional=True)

    def events(self, d: str, stop: threading.Event):
        """Yield event dicts from /event until stop, the stream ends or fails."""
        with self._open("/event", {"directory": d}, timeout=75, accept="text/event-stream") as r:
            self.caps["event_stream"] = True
            data = []
            for raw in r:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    try:
                        yield json.loads("\n".join(data))
                    except ValueError:
                        pass
                    data = []


class KiloUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------- normalization

def sanitize_server(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or "")


def norm_message(m: dict, ctx: dict) -> dict:
    info = m.get("info") or {}
    t = info.get("time") or {}
    tok = info.get("tokens") or {}
    cache = tok.get("cache") or {}
    err = info.get("error")
    return {"type": "message", "id": info.get("id"), "session_id": info.get("sessionID") or
            ctx["session_id"], "project_id": ctx["project_id"], "role": info.get("role"),
            "created_ts": _ms(t.get("created")), "completed_ts": _ms(t.get("completed")),
            "model_id": info.get("modelID") or (info.get("model") or {}).get("modelID"),
            "provider_id": info.get("providerID") or (info.get("model") or {}).get("providerID"),
            "agent": info.get("agent") or info.get("mode"), "mode": info.get("mode"),
            "tokens": {"input": tok.get("input"), "output": tok.get("output"),
                       "reasoning": tok.get("reasoning"), "cache_read": cache.get("read"),
                       "cache_write": cache.get("write")} if tok else None,
            "cost": info.get("cost"), "finish": info.get("finish"),
            "error": ({"name": err.get("name"), "message": _cap((err.get("data") or {}).get(
                "message") or err.get("message") or "", 2000)} if isinstance(err, dict) else
                      _cap(err, 2000) if err else None),
            "parent_id": info.get("parentID")}


def norm_part(p: dict, ctx: dict, mcp_names: list[str], label: str = "") -> dict:
    """One part, with only the fields the control center shows. Reasoning keeps
    only the text Kilo publishes; provider metadata (including encrypted
    reasoning content) is never copied."""
    typ = p.get("type") or "unknown"
    t = p.get("time") or {}
    base = {"type": "part", "id": p.get("id"), "message_id": p.get("messageID"),
            "session_id": p.get("sessionID") or ctx["session_id"], "project_id": ctx["project_id"],
            "raw_type": typ, "time": {"start": _ms(t.get("start")), "end": _ms(t.get("end"))}}
    if typ in ("text", "reasoning"):
        base.update(kind=typ, text=_cap(p.get("text"), MAX_TEXT + 1), ts=_ms(t.get("start")))
        if typ == "reasoning":
            base["provider_label"] = label
        if p.get("synthetic"):
            base["raw_type"] = "text:synthetic"
        return base
    if typ == "tool":
        st = p.get("state") or {}
        name = p.get("tool") or ""
        meta = st.get("metadata") or {}
        server = ""
        for s in mcp_names:
            if name.startswith(sanitize_server(s) + "_"):
                server = s
                break
        tt = st.get("time") or {}
        base.update(kind="tool", ts=_ms(tt.get("start")) or _ms(t.get("start")),
                    time={"start": _ms(tt.get("start")), "end": _ms(tt.get("end"))},
                    tool={"name": name, "call_id": p.get("callID"), "status": st.get("status"),
                          "title": _cap(st.get("title"), 300), "input": _cap(st.get("input"), MAX_TOOL + 1),
                          "output": _cap(st.get("output"), MAX_TOOL + 1) if st.get("output") is not None
                          else None, "error": _cap(st.get("error"), 4000) if st.get("error") else "",
                          "mcp_server": server,
                          "child_session": meta.get("sessionId") or meta.get("sessionID") or ""})
        return base
    kind = typ if typ in ("step-start", "step-finish", "file", "patch", "retry", "compaction",
                          "agent", "subtask", "snapshot") else "unknown"
    base.update(kind=kind, ts=_ms(t.get("start")) or _ms((p.get("time") or {}).get("created")))
    if typ == "step-finish":
        tok = p.get("tokens") or {}
        base.update(tokens={"input": tok.get("input"), "output": tok.get("output"),
                            "reasoning": tok.get("reasoning"),
                            "cache_read": (tok.get("cache") or {}).get("read"),
                            "cache_write": (tok.get("cache") or {}).get("write")},
                    cost=p.get("cost"), reason=p.get("reason"))
    elif typ == "retry":
        err = p.get("error") or {}
        base.update(attempt=p.get("attempt"), error=_cap((err.get("data") or {}).get("message")
                                                         or err.get("message") or err, 2000))
    elif typ == "file":
        base.update(filename=_cap(p.get("filename"), 300), mime=_cap(p.get("mime"), 80))
    elif typ == "patch":
        base.update(hash=_cap(p.get("hash"), 80), files=[_cap(f, 300) for f in (p.get("files") or [])[:50]])
    elif typ in ("agent", "subtask"):
        base.update(agent=_cap(p.get("name") or p.get("agent"), 80),
                    description=_cap(p.get("description"), 1000), prompt=_cap(p.get("prompt"), 4000))
    elif typ == "compaction":
        base.update(auto=p.get("auto"))
    elif kind == "unknown":
        base.update(text=_cap(p.get("text") or "", 2000))
    return base


def config_projection(cfg: dict) -> dict:
    """Allowlisted fields of Kilo's effective config: agent → model/mode/steps
    and boolean tool switches, model limits, MCP server type + enabled flag.
    No commands, environment, URLs, headers or provider options."""
    if not isinstance(cfg, dict):
        return {}
    agents = {}
    for name, a in (cfg.get("agent") or {}).items():
        if not isinstance(a, dict):
            continue
        tools = {k: v for k, v in (a.get("tools") or {}).items() if isinstance(v, bool)}
        agents[name] = {"model": a.get("model"), "mode": a.get("mode"),
                        "disabled": bool(a.get("disable")), "steps": a.get("steps") or a.get("maxSteps"),
                        "tools": tools or None}
    models = {}
    for pid, prov in (cfg.get("provider") or {}).items():
        for mid, m in ((prov or {}).get("models") or {}).items():
            lim = (m or {}).get("limit") or {}
            models[f"{pid}/{mid}"] = {"context": lim.get("context"), "output": lim.get("output")}
    mcp = {}
    for name, m in (cfg.get("mcp") or {}).items():
        if isinstance(m, dict):
            mcp[name] = {"type": m.get("type"), "enabled": m.get("enabled", True) is not False}
    return {"agents": agents, "models": models, "mcp": mcp,
            "subagent_depth": cfg.get("subagent_depth"),
            "enabled_providers": cfg.get("enabled_providers")}


def pending_obs(q: dict, kind: str, ctx: dict) -> dict:
    o = {"type": "pending", "kind": kind, "request_id": q.get("id"), "session_id": q.get("sessionID"),
         "project_id": ctx["project_id"], "task_id": ctx.get("task_id") or "",
         "created_ts": _ms((q.get("time") or {}).get("created")) or None}
    if kind == "question":
        qs = []
        for x in (q.get("questions") or [])[:10]:
            qs.append({"question": _cap(x.get("question"), 4000), "header": _cap(x.get("header"), 200),
                       "multiple": bool(x.get("multiple")), "custom": x.get("custom", True) is not False,
                       "options": [{"label": _cap(o.get("label"), 300),
                                    "description": _cap(o.get("description"), 1000)}
                                   for o in (x.get("options") or [])[:30] if isinstance(o, dict)]})
        o.update(questions=qs, blocking=q.get("blocking"))
    else:
        o.update(permission=_cap(q.get("permission") or q.get("type"), 120),
                 patterns=[_cap(x, 500) for x in (q.get("patterns") or q.get("pattern") or [])[:20]]
                 if isinstance(q.get("patterns") or q.get("pattern"), list) else
                 [_cap(q.get("pattern"), 500)] if q.get("pattern") else [],
                 always=[_cap(x, 500) for x in (q.get("always") or [])[:20]],
                 metadata=json.loads(_cap(q.get("metadata") or {}, 4000)) if len(json.dumps(
                     q.get("metadata") or {}, default=str)) <= 4000 else {"truncated": True},
                 tool=q.get("tool"))
    return o


# --------------------------------------------------------------------------- coordinator store

class CoordReader:
    """The coordinator's store, opened read-only (WAL: the live engine keeps
    writing undisturbed)."""

    def __init__(self, path: str):
        self.path = path

    def _q(self, sql: str, args=()) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        conn = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(self.path))}?mode=ro",
                               uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def owner(self) -> Optional[dict]:
        r = self._q("SELECT value FROM meta WHERE key='owner'")
        try:
            return json.loads(r[0]["value"]) if r else None
        except ValueError:
            return None

    def tasks(self) -> list[dict]:
        rows = self._q("SELECT * FROM tasks")
        for r in rows:
            r["data"] = _j(r.get("data"))
        return rows

    def sessions(self) -> list[dict]:
        rows = self._q("SELECT * FROM sessions ORDER BY created_ts")
        for r in rows:
            r["data"] = _j(r.get("data"))
        return rows

    def transitions_after(self, last: int, limit: int = 500) -> list[dict]:
        rows = self._q("SELECT * FROM transitions WHERE id > ? ORDER BY id LIMIT ?", (last, limit))
        for r in rows:
            r["evidence"] = _j(r.get("evidence"))
        return rows

    def events_after(self, last: int, limit: int = 200) -> list[dict]:
        rows = self._q("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last, limit))
        for r in rows:
            r["detail"] = _j(r.get("detail"))
        return rows

    def verification(self, set_id: str) -> list[dict]:
        return self._q("SELECT check_name, result, required, evidence, detail, revision, executed_ts "
                       "FROM verifications WHERE set_id=? ORDER BY id", (set_id,))

    def reviews(self, project_id: str, task_id: str) -> list[dict]:
        return self._q("SELECT review_id, reviewed_sha, role, persona, verdict, valid, error, ts FROM "
                       "reviews WHERE project_id=? AND task_id=? ORDER BY ts", (project_id, task_id))

    HELPER_KEYS = ("unit_id", "project_id", "task_id", "kind", "responsibility", "value", "source",
                   "status", "persona", "backend", "session_id", "parent_session", "reason",
                   "next_eligible", "attempts", "delivered_session", "updated_ts",
                   # v0.122 disposition / vocabulary (absent on older stores: left out)
                   "lane", "feedback", "feedback_detail", "reason_code", "phase", "started_ts",
                   "finished_ts", "tokens", "investigation_key")

    def helper_units(self) -> list[dict]:
        # SELECT * so a store with or without the v0.122 columns both work
        out = []
        for r in self._q("SELECT * FROM helper_units ORDER BY created_ts"):
            u = {k: r.get(k) for k in self.HELPER_KEYS if k in r}
            if "feedback_detail" in u:
                u["feedback_detail"] = _j(u["feedback_detail"]) if isinstance(
                    u["feedback_detail"], str) else u["feedback_detail"]
            for k in ("reason", "responsibility"):
                if isinstance(u.get(k), str):
                    u[k] = u[k][:600]
            out.append(u)
        return out

    # ---- v0.122 lifecycle tables (evidence / obligations / recovery). Each may
    # not exist yet (lifecycle off, or an older coordinator): a missing table
    # reads as empty, never as an error.

    LIFECYCLE_TABLES = ("ev_records", "ev_chains", "obligations", "findings", "decisions",
                        "budget_ledger", "repairs", "task_memory")

    def tables(self) -> set:
        return {r["name"] for r in self._q("SELECT name FROM sqlite_master WHERE type='table'")}

    def task_rows(self, table: str, project_id: str, task_id: str, limit: int = 400,
                  where: str = "", args: tuple = ()) -> list[dict]:
        """The NEWEST `limit` rows of one task (ORDER BY rowid DESC LIMIT n),
        returned oldest first. A bounded read never drops the latest records."""
        if table not in self.LIFECYCLE_TABLES:
            raise ValueError(table)
        try:
            rows = self._q(f"SELECT * FROM {table} WHERE project_id=? AND task_id=?"
                           f"{(' AND ' + where) if where else ''} ORDER BY rowid DESC LIMIT ?",
                           (project_id, task_id, *args, int(limit)))
        except Exception:                                        # noqa: BLE001
            return []
        return rows[::-1]

    def budget_sums(self, project_id: str, task_id: str) -> dict:
        """Per-counter totals, aggregated in SQL (never truncated by a row cap)."""
        out = {}
        for r in self._q("SELECT counter, SUM(CAST(delta AS INTEGER)) AS n FROM budget_ledger "
                         "WHERE project_id=? AND task_id=? GROUP BY counter",
                         (project_id, task_id)):
            if r.get("counter") is not None:
                out[str(r["counter"])] = int(r["n"] or 0)
        return out

    def latest_transition(self, project_id: str, task_id: str) -> dict:
        """The task's newest transition ({to_state, evidence}; {} when none)."""
        r = self._q("SELECT to_state, evidence FROM transitions WHERE project_id=? AND task_id=? "
                    "ORDER BY id DESC LIMIT 1", (project_id, task_id))
        return {"to_state": r[0]["to_state"], "evidence": _j(r[0]["evidence"])} if r else {}

    # in-place UPDATEs don't move COUNT/MAX(rowid): a cheap per-table extra
    # marker (a timestamp or a counter the writer bumps) catches them
    _SIG_EXTRA = {"obligations": "MAX(updated_ts)", "ev_chains": "MAX(checked_ts)",
                  "decisions": "MAX(COALESCE(answered_ts, opened_ts)), "
                               "SUM(status='open')",
                  "findings": "SUM(count)", "task_memory": "SUM(status='active')",
                  "ev_records": "SUM(freshness='fresh'), SUM(superseded_by IS NOT NULL)"}

    def lifecycle_signature(self) -> dict:
        """Per task: a cheap change marker over the lifecycle tables (row count,
        max rowid and, where the table has one, an in-place-update marker)."""
        sig: dict = {}
        present = self.tables()
        for i, t in enumerate(self.LIFECYCLE_TABLES):
            if t not in present:
                continue
            extra = self._SIG_EXTRA.get(t)
            rows = self._q(f"SELECT project_id, task_id, COUNT(*) AS n, MAX(rowid) AS m"
                           f"{', ' + extra if extra else ''} FROM {t} GROUP BY project_id, task_id")
            if not rows and extra:          # an older table without the extra column
                rows = self._q(f"SELECT project_id, task_id, COUNT(*) AS n, MAX(rowid) AS m "
                               f"FROM {t} GROUP BY project_id, task_id")
            for r in rows:
                key = (r.pop("project_id"), r.pop("task_id"))
                sig.setdefault(key, {})[t] = tuple(r.values())
        return sig

    def task_events(self, project_id: str, task_id: str, kinds: tuple, like: tuple = (),
                    limit: int = 20) -> list[dict]:
        cond = " OR ".join(["kind IN (" + ",".join("?" * len(kinds)) + ")"] +
                           ["kind LIKE ?"] * len(like))
        rows = self._q(f"SELECT event_id, ts, kind, session_id, detail FROM events WHERE "
                       f"project_id=? AND task_id=? AND ({cond}) ORDER BY id DESC LIMIT ?",
                       (project_id, task_id, *kinds, *like, int(limit)))
        for r in rows:
            r["detail"] = _j(r.get("detail"))
        return rows[::-1]

    LANE_COLS = ("unit_id, project_id, task_id, run_id, kind, lane, responsibility, value, "
                 "status, persona, backend, session_id, parent_session, parent_unit, depth, "
                 "artifact_id, snapshot_id, mode, reason, next_eligible, attempts, requests, "
                 "coalesced, feedback, feedback_detail, envelope, skills, served_model, "
                 "tokens, packet, created_ts, started_ts, finished_ts, delivered_ts, "
                 "delivered_session, updated_ts")

    def lane_units(self) -> list[dict]:
        """Proactive lane units (v0.118.0 columns; an older store has none),
        with the v0.122 phase / reason_code when the store has them."""
        rows = self._q(f"SELECT {self.LANE_COLS}, phase, reason_code FROM helper_units WHERE "
                       f"lane IS NOT NULL ORDER BY created_ts")
        if not rows:                     # a pre-0.122 store: no phase / reason_code columns
            rows = self._q(f"SELECT {self.LANE_COLS} FROM helper_units WHERE lane IS NOT NULL "
                           f"ORDER BY created_ts")
        return rows

    def artifacts(self) -> list[dict]:
        return self._q("SELECT * FROM artifacts ORDER BY created_ts")

    def meta_like(self, prefix: str) -> list[dict]:
        return self._q("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",))

    def operator_messages(self) -> list[dict]:
        return self._q("SELECT msg_id, action_id, project_id, task_id, scope, delivery, status, "
                       "delivered_session, delivered_ts, created_ts FROM operator_messages")


def _j(v):
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def _jl(v) -> list:
    """A JSON list column (or an already-decoded list); anything else -> []."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            x = json.loads(v)
        except ValueError:
            return []
        return x if isinstance(x, list) else []
    return []


def _jv(v):
    """A JSON column that may hold any value; undecodable text stays text."""
    if isinstance(v, str) and v[:1] in ("[", "{", '"'):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


# --------------------------------------------------------------------------- lifecycle fields
# v0.122 sprint management: the bridge reads the evidence / obligations /
# recovery tables (when they exist) and forwards a bounded per-task summary.
# Nothing is inferred: a check is "reusable" only when the ledger itself says
# it is fresh and at least verified; anything else names its reason.

TIER_RANK = {"claim": 0, "observed": 1, "verified": 2, "reviewed": 3, "approved": 4}
EVIDENCE_STATES = ("reusable", "stale", "missing", "disputed", "excluded")
LIFECYCLE_MAX_BYTES = 120_000


def _ref(r: dict) -> dict:
    """Provenance of one evidence record: where its bytes came from."""
    return {k: r.get(k) for k in ("record_id", "session_id", "message_id", "part_id", "call_id",
                                  "tool", "source", "output_sha", "artifact_id", "artifact_sha",
                                  "runtime_id", "source_revision") if r.get(k) not in (None, "")}


def _rec_ts(r: dict) -> float:
    try:
        return float(r.get("ended_ts") or r.get("created_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _best_record(usable: list) -> Optional[dict]:
    """The record that decides a check. The NEWEST fresh execution (observed or
    better: a prose claim is not an execution) decides the result, so a newer
    FAIL beats an older PASS whatever their tiers; among the fresh PASSes newer
    than the last fresh non-PASS the highest tier is shown. With no fresh
    execution: the best tier, then the newest (it then explains why it can't
    be reused: stale, unknown freshness, only a claim ...)."""
    if not usable:
        return None
    # recency = (timestamp, read order): rows are read oldest first, so a tie on
    # the timestamp goes to the later row
    order = {id(r): (_rec_ts(r), i) for i, r in enumerate(usable)}
    fresh = sorted((r for r in usable if r.get("freshness") == "fresh"
                    and TIER_RANK.get(r.get("tier"), -1) >= TIER_RANK["observed"]),
                   key=lambda r: order[id(r)], reverse=True)
    if fresh:
        newest = fresh[0]
        if newest.get("result") != "PASS":
            return newest
        last_bad = max((order[id(r)] for r in fresh if r.get("result") != "PASS"), default=None)
        passes = [r for r in fresh if r.get("result") == "PASS"
                  and (last_bad is None or order[id(r)] > last_bad)]
        return max(passes, key=lambda r: (TIER_RANK.get(r.get("tier"), -1), order[id(r)]))
    return max(usable, key=lambda r: (TIER_RANK.get(r.get("tier"), -1), _rec_ts(r)))


def evidence_summary(records: list, chains: list, required: list) -> dict:
    """Per check: reusable | stale | missing | disputed | excluded, with the
    reason it has to rerun (or may be reused) and the record's provenance."""
    by: dict[str, list] = {}
    for r in records:
        cid = str(r.get("check_id") or "")
        if cid:
            by.setdefault(cid, []).append(r)
    gaps: dict[str, str] = {}
    for c in chains:
        st = c.get("status")
        if st in ("gap", "order_violation", "void"):
            for g in _jl(c.get("gaps")):
                gid = g.get("check_id") if isinstance(g, dict) else g
                if gid:
                    gaps[str(gid)] = f"chain {c.get('chain_id')}: {st}"
    checks = []
    for cid in list(dict.fromkeys(list(by) + [str(x) for x in required] + list(gaps))):
        rows = by.get(cid, [])
        live = [r for r in rows if not r.get("superseded_by")]
        usable = [r for r in live if r.get("exec_status") != "excluded"]
        excluded = [r for r in rows if r.get("exec_status") == "excluded"]
        best = _best_record(usable)
        e = {"check_id": cid, "records": len(rows), "excluded": len(excluded),
             "excluded_reasons": list(dict.fromkeys(_cap(r.get("exclusion") or "excluded", 120)
                                                    for r in excluded))[:5]}
        if best is None:
            if excluded and cid not in gaps:
                e.update(state="excluded", reason="every recorded result is excluded ("
                         + ", ".join(e["excluded_reasons"]) + ")")
            else:
                e.update(state="missing", reason=gaps.get(cid) or "no recorded result")
            checks.append(e)
            continue
        e.update(result=best.get("result"), tier=best.get("tier"),
                 revision=best.get("source_revision"), freshness=best.get("freshness"),
                 chain_id=best.get("chain_id"), truncated=bool(best.get("truncated")),
                 ts=best.get("ended_ts") or best.get("created_ts"), refs=_ref(best))
        same_rev = {str(r.get("result")) for r in usable
                    if r.get("source_revision") == best.get("source_revision")
                    and r.get("result") in ("PASS", "FAIL")}
        tier_ok = TIER_RANK.get(best.get("tier"), -1) >= TIER_RANK["verified"]
        if same_rev == {"PASS", "FAIL"}:
            e.update(state="disputed", reason="PASS and FAIL recorded for the same revision")
        elif cid in gaps:
            e.update(state="missing", reason=gaps[cid])
        elif best.get("freshness") == "stale":
            e.update(state="stale", reason=_cap(best.get("stale_reason") or "dependencies changed "
                                                "since it ran", 300))
        elif best.get("truncated"):
            e.update(state="missing", reason="output was truncated; never credited above observed")
        elif best.get("freshness") == "fresh" and tier_ok and best.get("result") == "PASS":
            e.update(state="reusable", reason=f"fresh {best.get('tier')} PASS: do not rerun")
        elif best.get("result") == "FAIL":
            e.update(state="missing", reason="latest usable result is FAIL")
        elif best.get("freshness") == "fresh" and best.get("result") not in (None, "", "PASS"):
            e.update(state="missing", reason=f"latest usable result is {_cap(best.get('result'), 40)}")
        elif not tier_ok:
            e.update(state="missing", reason=f"only {best.get('tier') or 'unranked'} evidence "
                                             f"(needs verified)")
        else:
            e.update(state="missing", reason=f"freshness {best.get('freshness') or 'unknown'}")
        checks.append(e)
    counts = {s: sum(1 for c in checks if c["state"] == s) for s in EVIDENCE_STATES}
    return {"counts": counts, "checks": checks[:60], "more": max(0, len(checks) - 60),
            "chains": [{"chain_id": c.get("chain_id"), "status": c.get("status"),
                        "gaps": [g.get("check_id") if isinstance(g, dict) else g
                                 for g in _jl(c.get("gaps"))][:20],
                        "checked_ts": c.get("checked_ts")} for c in chains][:20]}


def obligations_checklist(rows: list) -> list:
    out = []
    for r in rows[:80]:
        req = _jl(r.get("required_evidence"))
        out.append({"id": r.get("obligation_id"), "phase": r.get("phase"),
                    "title": _cap(r.get("title"), 300), "source": _cap(r.get("source"), 120),
                    "status": r.get("status"), "blocking_reason": _cap(r.get("blocking_reason"), 400),
                    "next_action": _cap(r.get("next_action"), 400),
                    "verified_rev": r.get("verified_rev"), "verified_by": _cap(r.get("verified_by"), 120),
                    "findings": len(_jl(r.get("findings"))),
                    "required_evidence": [{"kind": x.get("kind"), "ref": _cap(x.get("ref"), 200)}
                                          for x in req if isinstance(x, dict)][:10],
                    "updated_ts": r.get("updated_ts")})
    return out


def finding_history(findings: list, repairs: list) -> dict:
    reps = sorted(repairs, key=lambda r: (int(r.get("attempt") or 0), float(r.get("ts") or 0)))
    by_attempt = {int(r.get("attempt") or 0): r for r in reps}
    fs = []
    for f in sorted(findings, key=lambda f: -int(f.get("count") or 0))[:40]:
        last = by_attempt.get(int(f.get("last_seen_attempt") or 0))
        samples = _jl(f.get("samples"))
        fs.append({"fingerprint": _cap(f.get("fingerprint"), 64), "class": f.get("class"),
                   "check_id": f.get("check_id"), "path": _cap(f.get("path"), 300),
                   "count": int(f.get("count") or 0),
                   "first_seen_attempt": f.get("first_seen_attempt"),
                   "last_seen_attempt": f.get("last_seen_attempt"),
                   "obligation_id": f.get("obligation_id"),
                   "sample": _cap(samples[0] if samples else "", 400),
                   "last_strategy": _cap((last or {}).get("strategy"), 400) or None})
    rs = [{"repair_id": r.get("repair_id"), "attempt": r.get("attempt"),
           "strategy": _cap(r.get("strategy"), 600), "strategy_fp": _cap(r.get("strategy_fp"), 64),
           "diagnosis": _cap(r.get("diagnosis"), 400), "outcome": r.get("outcome"),
           "diff_files": len(_jl(r.get("diff_files"))), "start_rev": r.get("start_rev"),
           "end_rev": r.get("end_rev"), "ts": r.get("ts")} for r in reps[-6:]]
    fps = [r["strategy_fp"] for r in rs if r["strategy_fp"]]
    return {"findings": fs, "repeated": sum(1 for f in fs if f["count"] >= 2), "repairs": rs,
            "approach_changed": (fps[-1] != fps[-2]) if len(fps) >= 2 else None}


def budgets_view(t: dict, limits: dict, ledger: list, reviews: int, now: float,
                 sums: Optional[dict] = None) -> dict:
    """Used/max per counter. The ledger (when present) is the source; otherwise
    the task row's own counters. Guided extra is shown, never folded in.
    `sums` (per-counter totals aggregated in SQL) wins over summing `ledger`,
    which may be a bounded sample of the newest rows."""
    d = t.get("data") or {}
    summed = sums is not None
    sums = dict(sums or {})
    authorized = []
    for r in ledger:
        c = str(r.get("counter") or "")
        if not summed:
            try:
                sums[c] = sums.get(c, 0) + int(r.get("delta") or 0)
            except (TypeError, ValueError):
                continue
        if r.get("decision_id"):
            authorized.append({"counter": c, "delta": r.get("delta"), "decision_id": r.get("decision_id"),
                               "actor": _cap(r.get("actor"), 80), "reason": _cap(r.get("reason"), 200)})

    def led(*names):
        for n in names:
            if n in sums:
                return sums[n], "budget_ledger"
        return None, None

    rg = d.get("retry_guidance") if isinstance(d.get("retry_guidance"), dict) else {}
    granted = 1 if rg.get("status") == "repair" and rg.get("extra_attempts") == 1 else 0
    out = {}
    v, src = led("attempt_ordinary", "ordinary", "attempts")
    out["ordinary"] = {"used": v if v is not None else int(t.get("attempt") or 0),
                       "max": limits.get("max_attempts"), "source": src or "task row"}
    v, src = led("attempt_guided", "guided_extra")
    out["guided_extra"] = {"used": v if v is not None else (1 if granted and int(
        t.get("attempt") or 0) > int(limits.get("max_attempts") or 0) else 0),
        "max": granted, "source": src or "retry_guidance",
        "explained": ("retry guidance granted one extra attempt after the ordinary budget ran out"
                      + (f" ({_cap(rg.get('persona'), 80)})" if rg.get("persona") else "")
                      if granted else "no guided extra attempt granted")}
    v, src = led("attempt_authorized", "authorized")
    if v is not None:
        out["authorized"] = {"used": v, "max": None, "source": src,
                             "explained": "operator-authorized attempts (each backed by a decision)"}
    v, src = led("sessions")
    out["sessions"] = {"used": v if v is not None else int(t.get("sessions") or 0),
                       "max": limits.get("max_sessions_per_task"), "source": src or "task row"}
    v, src = led("review_calls")
    out["review_calls"] = {"used": v if v is not None else reviews,
                           "max": limits.get("max_review_calls"), "source": src or "reviews table"}
    v, src = led("closeout")
    out["closeout"] = {"used": v if v is not None else int(d.get("closeout_attempts") or 0),
                       "max": limits.get("max_closeout_attempts"), "source": src or "task data"}
    for name, keys in (("transport_retries", ("transport_retries", "provider_retries")),
                       ("missing_evidence_continuations", ("missing_evidence_continuations",
                                                           "same_session_continuations")),
                       ("review_errors", ("review_errors",))):
        v, src = led(*keys)
        if v is None and name == "review_errors" and d.get("review_errors") is not None:
            v, src = int(d.get("review_errors") or 0), "task data"
        if v is not None:
            out[name] = {"used": v, "max": None, "source": src}
    if t.get("started_ts") and limits.get("max_task_minutes"):
        out["task_minutes"] = {"used": round((now - float(t["started_ts"])) / 60, 1),
                               "max": limits.get("max_task_minutes"), "source": "task row"}
    return {"counters": out, "authorized": authorized[-10:],
            "ledger": {k: v for k, v in sorted(sums.items())},
            "source": "budget_ledger" if (ledger or sums) else "task row + profile limits"}


def decisions_view(rows: list) -> list:
    out = []
    for r in rows:
        ev = _jv(r.get("evidence"))
        out.append({"decision_id": r.get("decision_id"), "kind": r.get("kind"),
                    "status": r.get("status"), "question": _cap(r.get("question"), 1500),
                    "evidence": [_cap(x, 600) for x in (ev if isinstance(ev, list) else
                                                        [ev] if ev else [])][:10],
                    "options": [_cap(x.get("label") if isinstance(x, dict) else x, 300)
                                for x in _jl(r.get("options"))][:12],
                    "recommendation": _cap(r.get("recommendation"), 800),
                    "blocks": [_cap(x, 200) for x in _jl(r.get("blocks"))][:12],
                    "answer": _cap(r.get("answer"), 1500), "authority": r.get("authority"),
                    "actor": _cap(r.get("actor"), 120), "opened_ts": r.get("opened_ts"),
                    "answered_ts": r.get("answered_ts"), "supersedes": r.get("supersedes"),
                    "superseded_by": r.get("superseded_by")})
    out.sort(key=lambda x: (x["status"] != "open", -(float(x.get("opened_ts") or 0))))
    n_open = sum(1 for x in out if x["status"] == "open")
    return out[:max(30, n_open)]                 # every open decision is kept


def readiness_view(items, failing: bool = False) -> list:
    """`failing`: the items come from the engine's readiness_block, which only
    holds the failing blocking checks (they carry no `ok` of their own)."""
    out = []
    for x in (items if isinstance(items, list) else [])[:40]:
        if isinstance(x, dict):
            ok = x.get("ok", False if failing else None)
            out.append({"name": _cap(x.get("name") or x.get("check"), 120),
                        "ok": ok if ok in (True, False, None) else None,
                        "cls": _cap(x.get("cls"), 40), "detail": _cap(x.get("detail"), 600),
                        "blocking": bool(x.get("blocking", x.get("required", True))),
                        "remedy": _cap(x.get("remedy"), 300) or None})
    return out


def _keep_min(path: str, lst: list) -> int:
    """How many leading items of a list _bound must never drop: the open
    decisions (decisions_view sorts them first) are what the operator answers."""
    if path == "decisions":
        return sum(1 for x in lst if isinstance(x, dict) and x.get("status") == "open")
    return 1


def _bound(o: dict, max_bytes: int = LIFECYCLE_MAX_BYTES) -> dict:
    """Keep the lifecycle payload bounded: halve the longest lists until it
    fits. Open decisions are never dropped."""
    def size():
        return len(json.dumps(o, default=str))
    guard = 0
    while size() > max_bytes and guard < 30:
        guard += 1
        best = None
        for k, v in o.items():
            for path, lst in ((k, v),) + (tuple((f"{k}.{kk}", vv) for kk, vv in v.items())
                                           if isinstance(v, dict) else ()):
                if isinstance(lst, list) and len(lst) > max(1, _keep_min(path, lst)) and (
                        best is None or len(lst) > len(best[1])):
                    best = (path, lst)
        if best is None:
            break
        del best[1][max(len(best[1]) // 2, _keep_min(best[0], best[1])):]
        o["truncated"] = True
    return o


# --------------------------------------------------------------------------- the bridge

class TelemetryBridge:
    def __init__(self, projects: list, store_path: str, foundry, observers: Optional[dict] = None,
                 source_id: str = "", poll_s: float = 2.0, replay_sessions: int = 40,
                 use_events: bool = True, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time, extra_secrets: tuple = ()):
        self.projects = {P.id: P for P in projects}
        self.reader = CoordReader(store_path)
        self.foundry = foundry
        self.obs = observers or {
            P.id: KiloObserver(P.profile.client.get("url") or "http://127.0.0.1:4096",
                               password=P.profile.secret(P.profile.client, "password"))
            for P in projects}
        self.source_id = source_id or f"bridge:{socket.gethostname()}"
        self.poll_s = poll_s
        self.replay_sessions = replay_sessions
        self.use_events = use_events
        self.log = log
        self.clock = clock
        self.started = clock()
        secrets_ = {o.secret for o in self.obs.values() if o.secret}
        secrets_ |= {getattr(foundry, "key", "")} | set(extra_secrets)
        from foundry_training.redact import Redactor
        self.redactor = Redactor({s for s in secrets_ if s})
        # state (committed only after Foundry accepted a batch)
        self.fp: dict[str, str] = {}
        self.cursor_transitions = 0
        self.cursor_events = 0
        self.watch: dict[str, dict] = {}          # session id -> {project_id, task_id, ...}
        self.pending_open: dict[str, dict] = {}
        self.kilo_ok: dict[str, bool] = {}
        self.kilo_error = ""
        self.kilo_version = ""
        self._last = {"beat": 0.0, "slow": 0.0, "children": 0.0}
        self._full_fetch: dict[str, float] = {}
        self._idle_fetched: set[str] = set()
        self._dirty: set[str] = set()
        self._live: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ev_threads: dict[str, threading.Thread] = {}
        self.event_state: dict[str, str] = {}
        self.errors = 0
        self.backoff = 0.0
        self.mcp_names: dict[str, list[str]] = {}
        self.replayed = False
        self._task_cache: dict[tuple, tuple] = {}
        self._tables: Optional[set] = None
        # one id per bridge process: the server tells a restart from a retry
        self.boot_id = uuid.uuid4().hex
        # monotonically increasing per bridge process; part of every batch id
        self.send_seq = 1
        self.compat: dict = {}

    # ---------------------------------------------------------------- lifecycle

    def run(self, max_loops: Optional[int] = None) -> None:
        n = 0
        if self.use_events:
            self._start_events()
        while not self._stop.is_set():
            self.step()
            n += 1
            if max_loops and n >= max_loops:
                break
            self._stop.wait(self.poll_s + self.backoff)
        self.stop()

    def stop(self) -> None:
        self._stop.set()

    def _start_events(self) -> None:
        for pid, P in self.projects.items():
            d = P.profile.workspace
            if d in self._ev_threads:
                continue
            th = threading.Thread(target=self._event_loop, args=(pid, d), daemon=True,
                                  name=f"kilo-events-{pid}")
            self._ev_threads[d] = th
            th.start()

    def _event_loop(self, pid: str, d: str) -> None:
        o = self.obs[pid]
        delay = 2.0
        while not self._stop.is_set():
            try:
                self.event_state[pid] = "connecting"
                for ev in o.events(d, self._stop):
                    self.event_state[pid] = "connected"
                    delay = 2.0
                    self._on_event(ev)
                self.event_state[pid] = "reconnecting"
            except urllib.error.HTTPError as e:
                if e.code in (404, 405, 501):
                    self.event_state[pid] = "unsupported (polling only)"
                    o.caps["event_stream"] = False
                    return
                self.event_state[pid] = f"error HTTP {e.code} (polling continues)"
            except Exception as e:                               # noqa: BLE001
                self.event_state[pid] = f"error: {type(e).__name__} (polling continues)"
            self._stop.wait(delay)
            delay = min(60.0, delay * 2)

    def _on_event(self, ev: dict) -> None:
        typ = ev.get("type") or ""
        pr = ev.get("properties") or {}
        sid = pr.get("sessionID") or (pr.get("info") or {}).get("sessionID") or \
            (pr.get("part") or {}).get("sessionID")
        with self._lock:
            if typ == "message.part.updated" and pr.get("part"):
                part = pr["part"]
                if part.get("sessionID") in self.watch:
                    self._live[part["id"]] = part
            elif typ == "message.part.delta":
                p = self._live.get(pr.get("partID") or "")
                if p is not None and pr.get("field") and isinstance(pr.get("delta"), str):
                    p[pr["field"]] = (p.get(pr["field"]) or "") + pr["delta"]
                else:
                    sid and self._dirty.add(sid)
            if sid:
                self._dirty.add(sid)

    # ---------------------------------------------------------------- one iteration

    def step(self) -> dict:
        now = self.clock()
        obs: list[dict] = []
        commits: list[Callable[[], None]] = []
        try:
            self._coordinator_facts(obs, commits)
        except Exception as e:                                   # noqa: BLE001
            self.log(f"bridge: coordinator store: {type(e).__name__}: {e}")
        for pid, P in self.projects.items():
            try:
                self._kilo_facts(pid, P, obs, commits, now)
                self.kilo_ok[pid] = True
            except KiloUnavailable as e:
                self.kilo_ok[pid] = False
                self.kilo_error = str(e)[:300]
        sent = self._send(obs, commits)
        if now - self._last["beat"] >= 15 or not self._last["beat"]:
            self._heartbeat()
            self._last["beat"] = now
        return {"observations": len(obs), "sent": sent}

    def _send(self, obs: list[dict], commits: list) -> bool:
        if not obs:
            for c in commits:
                c()
            self.backoff = 0.0
            return True
        clean, _ = self.redactor.obj(obs)
        try:
            for i in range(0, len(clean), BATCH):
                chunk = clean[i:i + BATCH]
                # the same chunk re-sent after a failure carries the same id, so a
                # batch the server already stored is recognized, not re-applied
                # the send sequence makes a NEW send unique even when a fact
                # returns to an earlier value (A -> B -> A): it advances only
                # after a whole send succeeded, so a retry keeps its ids
                bid = f"{self.boot_id[:12]}-{self.send_seq}.{i // BATCH}-{_h(chunk)}"
                self.foundry.telemetry({"contract": CONTRACT, "batch_id": bid, "source": {
                    "id": self.source_id, "kind": "bridge", "version": _version(),
                    "boot_id": self.boot_id}, "sent_ts": self.clock(), "observations": chunk})
        except Exception as e:                                   # noqa: BLE001
            self.errors += 1
            self.backoff = min(60.0, max(2.0, self.backoff * 2))
            self.log(f"bridge: Foundry ingest failed ({e}); retrying in {self.backoff:.0f}s")
            return False
        self.send_seq += 1
        for c in commits:
            c()
        self.backoff = 0.0
        return True

    def _heartbeat(self) -> None:
        own = self.reader.owner() or {}
        payload = {"contract": CONTRACT,
                   "source": {"id": self.source_id, "kind": "bridge", "host": socket.gethostname(),
                              "version": _version(), "boot_id": self.boot_id, "capabilities": {
                                  pid: dict(o.caps) for pid, o in self.obs.items()}},
                   "coordinator_version": _coord_version(), "foundry_version": _version(),
                   "boot_id": self.boot_id, "store_schema": self._schema(),
                   "features": list(FEATURES),
                   "sent_ts": self.clock(), "uptime_s": round(self.clock() - self.started, 1),
                   "kilo": {"ok": all(self.kilo_ok.values()) if self.kilo_ok else None,
                            "error": self.kilo_error if not all(self.kilo_ok.values() or [True])
                            else "", "version": self.kilo_version},
                   "coordinator_owner": {"present": bool(own), "id": own.get("id"),
                                         "beat_ts": own.get("beat")},
                   "projects": sorted(self.projects), "sessions_tracked": len(self.watch),
                   "event_stream": dict(self.event_state), "backoff_s": self.backoff,
                   "errors": self.errors,
                   "capture_boundary": {"bridge_started_ts": self.started,
                                        "replayed_sessions": self.replay_sessions}}
        try:
            r = self.foundry.heartbeat(self.redactor.obj(payload)[0])
        except Exception as e:                                   # noqa: BLE001
            self.log(f"bridge: heartbeat failed: {e}")
            return
        comp = (r or {}).get("compat") if isinstance(r, dict) else None
        if comp:
            if comp != self.compat.get("state") and comp != "ok":
                self.log(f"bridge: Foundry version gate says {comp}: "
                         f"{(r or {}).get('compat_reason') or ''} (heart-beating continues)")
            self.compat = {"state": comp, "reason": (r or {}).get("compat_reason"),
                           "server_version": (r or {}).get("server_version")}

    def _schema(self) -> dict:
        out = {}
        for r in self.reader.meta_like("schema"):
            try:
                out[r["key"]] = int(r["value"])
            except (TypeError, ValueError):
                out[r["key"]] = r["value"]
        return out

    def _emit(self, obs: list, commits: list, key: str, o: dict, force: bool = False) -> None:
        h = _h(o)
        if self.fp.get(key) == h and not force:
            return
        obs.append(o)
        commits.append(lambda k=key, v=h: self.fp.__setitem__(k, v))

    # ---------------------------------------------------------------- coordinator facts

    def _coordinator_facts(self, obs: list, commits: list) -> None:
        tasks = self.reader.tasks()
        by_project: dict[str, list] = {}
        for t in tasks:
            by_project.setdefault(t["project_id"], []).append(t)
        omsgs = self.reader.operator_messages()
        units = self.reader.helper_units()
        now = self.clock()
        self._tables = self.reader.tables()
        lsig = (self.reader.lifecycle_signature()
                if set(CoordReader.LIFECYCLE_TABLES) & self._tables else {})
        # per task: one task's operator message / helper / lifecycle change must
        # not rebuild every other task's observation
        om_by: dict = {}
        for m in omsgs:
            om_by.setdefault((m["project_id"], m["task_id"]), []).append(m)
        units_by: dict = {}
        for u in units:
            units_by.setdefault((u["project_id"], u["task_id"]), []).append(u)
        for t in tasks:
            P = self.projects.get(t["project_id"])
            if P is None:
                continue
            key = (t["project_id"], t["task_id"])
            hit = self._task_cache.get(key)
            om_sig = _h([om_by.get(key, []), units_by.get(key, []), lsig.get(key, {})])
            sig = (t.get("updated_ts"), t["state"], om_sig,
                   tuple(x["state"] for x in by_project[t["project_id"]]))
            # an approval card is re-hashed every pass: artifact bytes can change
            # without the task row changing
            if hit and hit[0] == sig and t["state"] != "AWAITING_HUMAN" and (
                    t["state"] in ("COMPLETE", "CANCELLED") or now - hit[1] < 10):
                o = hit[2]
            else:
                o = self._task_obs(P, t, by_project[t["project_id"]], omsgs)
                # ordinary units only: lane units travel as lane_unit observations
                o["helpers"] = [u for u in units_by.get(key, []) if not u.get("lane")][-30:]
                self._task_cache[key] = (sig, now, o)
            self._emit(obs, commits, f"task:{t['project_id']}/{t['task_id']}", o)
        trs = self.reader.transitions_after(self.cursor_transitions)
        for r in trs:
            if r["project_id"] not in self.projects:
                continue
            ev = r["evidence"] or {}
            obs.append({"type": "transition", "project_id": r["project_id"], "task_id": r["task_id"],
                        "src_id": r["id"], "ts": r["ts"], "from": r["from_state"], "to": r["to_state"],
                        "actor": r["actor"], "session_id": ev.get("session_id") or "",
                        "evidence": {k: (v if isinstance(v, (int, float, bool)) or v is None
                                         else _cap(v, 500)) for k, v in ev.items()
                                     if k in ("reason", "blocker", "head_sha", "session_id", "run_id",
                                              "attempt", "verdict", "awaiting", "repair", "resumed",
                                              "invalidated", "reviewed_commit_sha", "published",
                                              "blocker_class")}})
        if trs:
            last = trs[-1]["id"]
            commits.append(lambda v=last: setattr(self, "cursor_transitions", v))
        evs = self.reader.events_after(self.cursor_events)
        for e in evs:
            if e["kind"] == "error" and e["project_id"] in self.projects:
                obs.append({"type": "coord_event", "kind": "error", "event_id": e["event_id"],
                            "project_id": e["project_id"], "task_id": e["task_id"],
                            "detail": {"error": _cap((e["detail"] or {}).get("error"), 600)}})
        if evs:
            last_e = evs[-1]["id"]
            commits.append(lambda v=last_e: setattr(self, "cursor_events", v))
        self._lane_facts(obs, commits)
        # direct Foundry calls (visual observer) are not Kilo sessions: never poll Kilo for them
        sessions = [s for s in self.reader.sessions() if s.get("client") != "foundry-direct"]
        recent = sessions[-self.replay_sessions:] if not self.replayed else sessions
        tasks_by = {(t["project_id"], t["task_id"]): t for t in tasks}
        for s in recent:
            if s["project_id"] not in self.projects:
                continue
            t = tasks_by.get((s["project_id"], s["task_id"])) or {}
            info = self.watch.setdefault(s["session_id"], {})
            info.update(project_id=s["project_id"], task_id=s["task_id"], run_id=s["run_id"],
                        role=s["role"], phase=s["phase"], attempt=s["attempt"],
                        parent_id=(s["data"] or {}).get("parent") or "",
                        persona=s["persona"], coord_status=s["status"], outcome=s["outcome"],
                        ended_ts=s["ended_ts"], created_ts=s["created_ts"],
                        active=t.get("session_id") == s["session_id"])
        self.replayed = True

    THUMB_MAX_BYTES = 400_000

    def _lane_facts(self, obs: list, commits: list) -> None:
        """Lane units, artifacts and the per-project kill switch. Each is
        re-sent only when it changed (fingerprint), patches and packets capped."""
        for u in self.reader.lane_units():
            if u["project_id"] not in self.projects:
                continue
            env = _j(u.pop("envelope", None))
            pk = _j(u.pop("packet", None))
            o = {"type": "lane_unit", **{k: v for k, v in u.items()}}
            o["feedback_detail"] = _j(u.get("feedback_detail"))
            o["skills"] = _j(u.get("skills"))
            o["envelope"] = {k: env.get(k) for k in (
                "structured", "summary", "image_seen", "subject_present", "validity",
                "checks_run", "checks_proposed", "skills_used", "sources", "limitations",
                "next_action", "routed_to", "not_inspected", "base", "affected_paths")
                if env.get(k) not in (None, "", [])}
            o["envelope"]["findings"] = (env.get("findings") or [])[:12]
            if env.get("patch"):
                o["envelope"]["patch"] = _cap(env["patch"], 20000)
            ch = env.get("checks") or {}
            o["envelope"]["checks"] = {k: ch[k] for k in ("patch", "drift", "violations",
                                                          "base_mismatch") if k in ch}
            o["assignment"] = {k: pk.get(k) for k in (
                "subject", "path", "generation", "viewport", "references", "reference_notes",
                "sent", "skills_loaded", "skills_missing", "vision_routes", "evidence",
                "signature", "command", "base", "head", "dirty", "snapshot", "snapshot_status")
                if pk.get(k) not in (None, "", [])}
            if pk.get("path"):
                o["artifact_path"], o["artifact_generation"] = pk.get("path"), pk.get("generation")
            self._emit(obs, commits, f"lane:{u['unit_id']}", o)
        for a in self.reader.artifacts():
            if a["project_id"] not in self.projects:
                continue
            o = {"type": "artifact", **{k: a.get(k) for k in (
                "artifact_id", "project_id", "task_id", "run_id", "session_id", "kind", "rel_path",
                "generation", "sha256", "bytes", "width", "height", "mime", "valid", "source",
                "revision", "subject", "status", "superseded_by", "prev_artifact", "created_ts")}}
            o["validity"] = _j(a.get("validity"))
            key = f"art:{a['artifact_id']}"
            if self.fp.get(key) is None and (a.get("bytes") or 0) <= self.THUMB_MAX_BYTES and \
                    a.get("status") != "expired":
                try:
                    with open(a["store_path"], "rb") as f:
                        raw = f.read(self.THUMB_MAX_BYTES + 1)
                    if hashlib.sha256(raw).hexdigest() == a["sha256"]:
                        o["thumbnail_b64"] = base64.b64encode(raw).decode()
                except OSError:
                    pass
            if "thumbnail_b64" not in o:
                o["thumbnail_note"] = ("image over the bridge thumbnail limit; hash and metadata "
                                       "only" if (a.get("bytes") or 0) > self.THUMB_MAX_BYTES
                                       else "snapshot copy unavailable or already sent")
            self._emit(obs, commits, key, o)
        paused = {r["key"].split(":", 1)[1]: r["value"] not in ("", "0")
                  for r in self.reader.meta_like("lanes_paused:")}
        for pid, P in self.projects.items():
            raw = dict((getattr(P.profile, "raw", None) or {}).get("lanes") or {})
            self._emit(obs, commits, f"lanestate:{pid}", {
                "type": "lane_state", "project_id": pid, "paused": paused.get(pid, False),
                "enabled": bool(raw.get("enabled", False)),
                "lanes": {k: bool(v.get("enabled")) for k, v in raw.items() if isinstance(v, dict)}})

    def _task_obs(self, P, t: dict, siblings: list, omsgs: list) -> dict:
        d = t["data"] or {}
        o = {"type": "task", "project_id": t["project_id"], "task_id": t["task_id"],
             **{k: t.get(k) for k in ("state", "resume_state", "attempt", "sessions", "run_id",
                                      "session_id", "phase", "base_sha", "head_sha", "reviewed_sha",
                                      "review_id", "verification_id", "closeout_id", "blocker",
                                      "awaiting", "next_action", "pause_reason", "pause_until",
                                      "started_ts", "updated_ts")},
             "source_ts": t.get("updated_ts"), "stage": d.get("stage"),
             "findings": (d.get("findings") or [])[:10], "last_report": d.get("last_report"),
             "limits": {k: P.profile.limit(k, v) for k, v in (
                 ("max_attempts", 3), ("max_sessions_per_task", 8), ("max_review_calls", 4),
                 ("max_closeout_attempts", 2), ("session_timeout_minutes", 0),
                 ("max_task_minutes", 0))},
             # optional profile [telemetry] context_checkpoints = [140000, 190000]:
             # operational markers drawn on the context bar (not model limits)
             "context_checkpoints": list((getattr(P.profile, "raw", {}) or {}).get(
                 "telemetry", {}).get("context_checkpoints") or []),
             "operator_messages": [{k: m[k] for k in ("action_id", "status", "delivery", "scope",
                                                      "delivered_session", "delivered_ts")}
                                   for m in omsgs if m["project_id"] == t["project_id"]
                                   and m["task_id"] == t["task_id"]][-20:]}
        q = d.get("question")
        if q:
            o["question"] = {"request_id": q.get("request_id"), "kind": q.get("kind"),
                             "text": _cap(q.get("text"), 4000), "options": q.get("options")}
        kinds = d.get("awaiting_kinds") or []
        if t["state"] == "AWAITING_HUMAN" and kinds:
            revision = t["reviewed_sha"] if d.get("stage") == "before_close" else t["head_sha"]
            arts = {}
            try:
                spec = P.adapter.describe(t["task_id"])
                for k in kinds:
                    rule = next((r for r in P.profile.approvals if r.kind == k), None)
                    arts[k] = (P.adapter.approval_artifacts(spec, rule.artifacts)
                               if rule and rule.artifacts else {})
            except Exception as e:                               # noqa: BLE001
                o["approval_error"] = f"artifacts unavailable: {e}"[:300]
            o["approval"] = {"kinds": kinds, "stage": d.get("stage"), "revision": revision,
                             "artifacts": arts}
        if t.get("verification_id"):
            o["verification"] = [{**v, "detail": _cap(v.get("detail"), 600)}
                                 for v in self.reader.verification(t["verification_id"])][:40]
        revs = self.reader.reviews(t["project_id"], t["task_id"])
        if revs:
            o["reviews"] = revs[-5:]
        try:
            o["workspace_head"] = P.ws.head()
        except Exception:                                        # noqa: BLE001
            o["workspace_head"] = None
        if all(x["state"] in ("COMPLETE", "CANCELLED") for x in siblings):
            o["queue_exhausted"] = len(siblings)
        rg = d.get("retry_guidance")
        if isinstance(rg, dict) and rg:
            o["retry_guidance"] = {k: (_cap(rg.get(k), 300) if isinstance(rg.get(k), str)
                                       else rg.get(k)) for k in ("status", "extra_attempts",
                                                                 "persona", "summary")
                                   if rg.get(k) is not None}
        try:
            o.update(self._lifecycle(P, t, o["limits"], len(revs)))
        except Exception as e:                                   # noqa: BLE001
            o["lifecycle_error"] = f"{type(e).__name__}: {e}"[:300]
        return o

    RECOVERED_EVENTS = ("helper_reconciled", "review_not_reused", "lane_reused", "helper_duplicate",
                        "evidence_recovered", "recovery")

    def _lifecycle(self, P, t: dict, limits: dict, n_reviews: int) -> dict:
        """v0.122 task fields from the lifecycle tables. A table that doesn't
        exist yet reads as empty; `lifecycle_tables` says which ones were there."""
        pid, tid = t["project_id"], t["task_id"]
        d = t.get("data") or {}
        present = self._tables if self._tables is not None else self.reader.tables()
        have = [x for x in CoordReader.LIFECYCLE_TABLES if x in present]

        def rows(table, **kw):
            return self.reader.task_rows(table, pid, tid, **kw) if table in present else []

        recs = rows("ev_records", limit=600)
        chains = rows("ev_chains", limit=50)
        obls = rows("obligations", limit=200)
        required = []
        for ob in obls:
            for x in _jl(ob.get("required_evidence")):
                if isinstance(x, dict) and x.get("kind") == "check" and x.get("ref"):
                    required.append(x["ref"])
        # every open decision, then the newest closed ones (never bounded away)
        decs = rows("decisions", limit=500, where="status='open'")
        decs += rows("decisions", limit=100, where="COALESCE(status,'')!='open'")
        sums = self.reader.budget_sums(pid, tid) if "budget_ledger" in present else None
        ledger = rows("budget_ledger", limit=200)
        if "budget_ledger" in present:
            # authorized (decision-backed) rows are listed even when older than
            # the newest-200 sample
            seen = {r.get("id") for r in ledger}
            ledger = [r for r in rows("budget_ledger", limit=10, where="decision_id IS NOT NULL "
                                      "AND decision_id != ''") if r.get("id") not in seen] + ledger
        readiness, blocker_class = self._readiness(t, d)
        out = {"readiness": readiness, "blocker_class": blocker_class,
               "obligations": obligations_checklist(obls),
               "evidence": evidence_summary(recs, chains, required),
               "finding_history": finding_history(rows("findings", limit=200),
                                                  rows("repairs", limit=50)),
               "budgets": budgets_view(t, limits, ledger, n_reviews, self.clock(),
                                       sums=sums or None),
               "decisions": decisions_view(decs),
               "lifecycle_tables": have}
        rec = []
        for r in recs:
            if r.get("source") == "recovery" and r.get("exec_status") != "excluded":
                rec.append({"kind": "evidence", "check_id": r.get("check_id"),
                            "result": r.get("result"), "tier": r.get("tier"),
                            "freshness": r.get("freshness"), "ts": r.get("created_ts"),
                            "detail": f"recovered from session {r.get('session_id') or '?'}",
                            "refs": _ref(r)})
        for e in self.reader.task_events(pid, tid, self.RECOVERED_EVENTS,
                                         ("%reconciled%", "readiness_%")):
            det = e.get("detail") or {}
            rec.append({"kind": e.get("kind"), "ts": e.get("ts"), "session_id": e.get("session_id"),
                        "detail": _cap(det.get("detail") or det.get("reason") or det.get("note")
                                       or det, 400)})
        out["recovered"] = sorted(rec, key=lambda x: float(x.get("ts") or 0))[-20:]
        mem = rows("task_memory", limit=200)
        out["memory"] = [{"mem_id": m.get("mem_id"), "kind": m.get("kind"),
                          "authority": m.get("authority"), "status": m.get("status"),
                          "text": _cap(m.get("text"), 400), "actor": _cap(m.get("actor"), 80),
                          "created_ts": m.get("created_ts")}
                         for m in mem if m.get("status") in (None, "", "active")][-15:]
        return _bound(out)

    def _readiness(self, t: dict, d: dict) -> tuple:
        """(readiness items, blocker class). The engine stores the failing
        readiness checks as task data `readiness_block: {cls, items, ...}` and
        the class in the BLOCKED transition's evidence; older rows (or an
        integrator that merged them) carry `readiness` / `blocker_class` keys."""
        rb = d.get("readiness_block") if isinstance(d.get("readiness_block"), dict) else {}
        if isinstance(rb.get("items"), list):
            items = readiness_view(rb["items"], failing=True)
        else:
            items = readiness_view(d.get("readiness"))
        bc = rb.get("cls") or d.get("blocker_class")
        if not bc and isinstance(d.get("decision_wait"), dict) and t.get("state") == "BLOCKED":
            bc = "decision"
        if (not bc or not items) and t.get("state") == "BLOCKED":
            tr = self.reader.latest_transition(t["project_id"], t["task_id"])
            ev = tr.get("evidence") if tr.get("to_state") == "BLOCKED" else None
            if isinstance(ev, dict):
                bc = bc or ev.get("blocker_class")
                if not items and isinstance(ev.get("readiness"), list):
                    items = readiness_view(ev["readiness"], failing=True)
        return items, (_cap(bc, 40) or None)

    # ---------------------------------------------------------------- Kilo facts

    def _kilo_facts(self, pid: str, P, obs: list, commits: list, now: float) -> None:
        o = self.obs[pid]
        d = P.profile.workspace
        if now - self._last["slow"] >= 30 or not self._last["slow"]:
            self._slow(pid, P, o, d, obs, commits)
            if pid == list(self.projects)[-1]:
                self._last["slow"] = now
        statuses = o.statuses(d)
        watched = {sid: w for sid, w in self.watch.items() if w.get("project_id") == pid}
        if now - self._last["children"] >= 10:
            for sid, w in list(watched.items()):
                if w.get("ended_ts") and sid in self._idle_fetched and sid not in statuses:
                    continue
                for k in o.children(sid, d) or []:
                    kid = k.get("id")
                    if kid and kid not in self.watch:
                        self.watch[kid] = {"project_id": pid, "task_id": w.get("task_id"),
                                           "run_id": w.get("run_id"), "role": "helper",
                                           "phase": w.get("phase"), "attempt": w.get("attempt"),
                                           "parent_id": sid, "created_ts": _ms((k.get("time") or {})
                                                                               .get("created"))}
                        watched[kid] = self.watch[kid]
            self._last["children"] = now
        with self._lock:
            dirty, self._dirty = set(self._dirty), set()
            live = {k: v for k, v in self._live.items()}
            # keep streaming parts only while their session is busy (a full read
            # follows when it settles)
            for k, v in list(self._live.items()):
                if (statuses.get(v.get("sessionID")) or {}).get("type") not in ("busy", "retry"):
                    self._live.pop(k, None)
        mcp_names = self.mcp_names.get(pid, [])
        for sid, w in watched.items():
            st = statuses.get(sid) or {}
            busy = st.get("type") in ("busy", "retry")
            ctx = {"project_id": pid, "session_id": sid}
            meta = None
            if f"info:{sid}" not in self.fp or sid in dirty:
                meta = o.session(sid, d)
                if meta is None:
                    w["missing"] = True
            sess = {"type": "session", "id": sid, "project_id": pid,
                    **{k: w.get(k) for k in ("task_id", "run_id", "role", "phase", "attempt",
                                             "parent_id", "created_ts", "ended_ts", "outcome")},
                    "status": self._status(st, w), "coordinator": {
                        "status": w.get("coord_status"), "outcome": w.get("outcome"),
                        "active_for_task": w.get("active")}}
            if meta:
                tm = meta.get("time") or {}
                w.update(title=meta.get("title"), parent_id=w.get("parent_id") or meta.get("parentID"),
                         archived=bool(tm.get("archived")))
                sess["created_ts"] = sess.get("created_ts") or _ms(tm.get("created"))
            sess.update(title=w.get("title"), parent_id=w.get("parent_id"),
                        archived=w.get("archived"), agent=w.get("agent"),
                        model={"id": w.get("model"), "provider": w.get("provider")}
                        if w.get("model") else None)
            if meta is not None or w.get("missing"):
                commits.append(lambda s=sid: self.fp.__setitem__(f"info:{s}", "1"))
            # parts that streamed in through events since the last pass
            for part in live.values():
                if part.get("sessionID") == sid:
                    self._emit(obs, commits, f"part:{part.get('id')}",
                               norm_part(part, ctx, mcp_names, w.get("label") or ""))
            due_full = (sid not in self._full_fetch
                        or (busy and now - self._full_fetch[sid] >= (30 if self.event_state.get(pid)
                                                                     == "connected" else self.poll_s))
                        or (not busy and sid not in self._idle_fetched)
                        or sid in dirty and not busy and now - self._full_fetch.get(sid, 0) > 5)
            if due_full and not w.get("missing"):
                self._messages(o, d, sid, w, ctx, mcp_names, obs, commits)
                self._full_fetch[sid] = now
                if not busy:
                    self._idle_fetched.add(sid)
                else:
                    self._idle_fetched.discard(sid)
            if busy or sid in dirty or f"todo:{sid}" not in self.fp:
                if o.caps.get("todo") is not False:
                    td = o.todo(sid, d)
                    if td is not None:
                        self._emit(obs, commits, f"todo:{sid}", {
                            "type": "todo", "project_id": pid, "session_id": sid,
                            "source": "kilo /session/{id}/todo", "items": td if isinstance(td, list)
                            else td.get("todos") or [], "observed_ts": now})
            self._emit(obs, commits, f"session:{sid}", sess)
        self._pending(pid, P, o, d, watched, obs, commits)

    @staticmethod
    def _status(st: dict, w: dict) -> dict:
        now = time.time()
        if w.get("missing"):
            return {"value": "missing", "source": "kilo GET /session/{id}: 404 (deleted or "
                                                  "archived in Kilo)", "observed_ts": now}
        typ = st.get("type")
        if typ in ("busy", "retry", "offline"):
            return {"value": typ, "source": "kilo /session/status", "observed_ts": now,
                    "message": _cap(st.get("message"), 300) if st.get("message") else None}
        return {"value": "idle", "source": "kilo /session/status (not listed = idle)",
                "observed_ts": now}

    def _messages(self, o, d, sid, w, ctx, mcp_names, obs, commits) -> None:
        msgs = o.messages(sid, d)
        for m in msgs:
            info = m.get("info") or {}
            if info.get("role") == "assistant" and info.get("modelID"):
                w["model"], w["provider"] = info.get("modelID"), info.get("providerID")
                w["label"] = f"{info.get('providerID') or '?'}/{info.get('modelID')}"
            if info.get("role") == "user" and info.get("agent"):
                w["agent"] = info.get("agent")
            nm = norm_message(m, ctx)
            if nm.get("id"):
                self._emit(obs, commits, f"msg:{nm['id']}", nm)
            for p in m.get("parts") or []:
                if p.get("id"):
                    self._emit(obs, commits, f"part:{p['id']}",
                               norm_part(p, ctx, mcp_names, w.get("label") or ""))

    def _pending(self, pid, P, o, d, watched, obs, commits) -> None:
        cur = {}
        for kind, rows in (("question", o.questions(d)), ("permission", o.permissions(d))):
            for q in rows or []:
                sid = q.get("sessionID")
                if sid in watched and q.get("id"):
                    w = watched[sid]
                    cur[q["id"]] = pending_obs(q, kind, {"project_id": pid,
                                                          "task_id": w.get("task_id")})
        for rid, p in cur.items():
            self._emit(obs, commits, f"pending:{rid}", p)
        gone = [rid for rid, p in self.pending_open.items() if p["project_id"] == pid and rid not in cur]
        for rid in gone:
            p = self.pending_open[rid]
            obs.append({**{k: p[k] for k in ("type", "kind", "request_id", "session_id", "project_id",
                                             "task_id")}, "resolved": True})
        open_now = {rid: p for rid, p in self.pending_open.items() if p["project_id"] != pid}
        open_now.update(cur)
        commits.append(lambda v=open_now: setattr(self, "pending_open", v))
        for rid in gone:
            commits.append(lambda r=rid: self.fp.pop(f"pending:{r}", None))

    def _slow(self, pid, P, o, d, obs, commits) -> None:
        now = self.clock()
        try:
            h = o.health()
            self.kilo_version = str(h.get("version") or "")
        except KiloUnavailable:
            raise
        cfg = o.config(d)
        proj = config_projection(cfg) if isinstance(cfg, dict) else {}
        if proj:
            proj["kilo_version"] = self.kilo_version
            self._emit(obs, commits, f"config:{pid}", force=True, o={"type": "config", "project_id": pid,
                                                       "scope": "workspace", "observed_ts": now,
                                                       **proj})
        configured = proj.get("mcp") or {}
        self.mcp_names[pid] = sorted(configured)
        live = o.mcp(d)
        servers = []
        names = sorted(set(configured) | set((live or {}).keys() if isinstance(live, dict) else []))
        for n in names:
            st = (live or {}).get(n) if isinstance(live, dict) else None
            servers.append({"name": n, "configured_enabled": (configured.get(n) or {}).get("enabled"),
                            "type": (configured.get(n) or {}).get("type"),
                            "status": (st or {}).get("status") if isinstance(st, dict) else
                            ("unknown" if live is None else "not reported"),
                            "error": _cap((st or {}).get("error"), 400) if isinstance(st, dict)
                            and st.get("error") else None})
        if servers or live is not None:
            self._emit(obs, commits, f"mcp:{pid}", force=True, o={
                "type": "mcp", "project_id": pid, "scope": "workspace", "observed_ts": now,
                "source": "kilo /mcp (live) + /config/effective (configured)",
                "status_endpoint": live is not None, "servers": servers})
        caps = {}
        for name, path in (("lsp", "/lsp"), ("indexing", "/indexing/status"),
                           ("memory", "/memory/status")):
            v = o.capability(name, path, d)
            caps[name] = {"supported": o.caps.get(name, v is not None),
                          "status": json.loads(_cap(v, 3000)) if v is not None and
                          len(json.dumps(v, default=str)) <= 3000 else
                          ({"truncated": True} if v is not None else None)}
        self._emit(obs, commits, f"caps:{pid}", force=True, o={"type": "capabilities", "project_id": pid,
                                                 "scope": "workspace", "observed_ts": now,
                                                 "source": "kilo /lsp, /indexing/status, /memory/status",
                                                 "items": caps})


def _version() -> str:
    try:
        from foundry_router import __version__
        return __version__
    except Exception:                                            # noqa: BLE001
        return ""


def _coord_version() -> str:
    try:
        from foundry_coordinator import __version__
        return __version__
    except Exception:                                            # noqa: BLE001
        return ""


FEATURES = ("batch_id", "boot_id", "lifecycle_v1", "readiness", "decisions")
