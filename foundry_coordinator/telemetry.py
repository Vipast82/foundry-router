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

    def helper_units(self) -> list[dict]:
        return self._q("SELECT unit_id, project_id, task_id, kind, responsibility, value, source, "
                       "status, persona, backend, session_id, parent_session, reason, "
                       "next_eligible, attempts, delivered_session, updated_ts FROM helper_units "
                       "ORDER BY created_ts")

    def operator_messages(self) -> list[dict]:
        return self._q("SELECT msg_id, action_id, project_id, task_id, scope, delivery, status, "
                       "delivered_session, delivered_ts, created_ts FROM operator_messages")


def _j(v):
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


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
                self.foundry.telemetry({"contract": CONTRACT, "source": {
                    "id": self.source_id, "kind": "bridge"}, "sent_ts": self.clock(),
                    "observations": clean[i:i + BATCH]})
        except Exception as e:                                   # noqa: BLE001
            self.errors += 1
            self.backoff = min(60.0, max(2.0, self.backoff * 2))
            self.log(f"bridge: Foundry ingest failed ({e}); retrying in {self.backoff:.0f}s")
            return False
        for c in commits:
            c()
        self.backoff = 0.0
        return True

    def _heartbeat(self) -> None:
        own = self.reader.owner() or {}
        payload = {"source": {"id": self.source_id, "kind": "bridge", "host": socket.gethostname(),
                              "version": _version(), "capabilities": {
                                  pid: dict(o.caps) for pid, o in self.obs.items()}},
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
            self.foundry.heartbeat(self.redactor.obj(payload)[0])
        except Exception as e:                                   # noqa: BLE001
            self.log(f"bridge: heartbeat failed: {e}")

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
        om_sig = _h([omsgs, units])
        for t in tasks:
            P = self.projects.get(t["project_id"])
            if P is None:
                continue
            key = (t["project_id"], t["task_id"])
            hit = self._task_cache.get(key)
            sig = (t.get("updated_ts"), t["state"], om_sig,
                   tuple(x["state"] for x in by_project[t["project_id"]]))
            # an approval card is re-hashed every pass: artifact bytes can change
            # without the task row changing
            if hit and hit[0] == sig and t["state"] != "AWAITING_HUMAN" and (
                    t["state"] in ("COMPLETE", "CANCELLED") or now - hit[1] < 10):
                o = hit[2]
            else:
                o = self._task_obs(P, t, by_project[t["project_id"]], omsgs)
                o["helpers"] = [u for u in units if u["project_id"] == t["project_id"]
                                and u["task_id"] == t["task_id"]][-30:]
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
                                              "invalidated", "reviewed_commit_sha", "published")}})
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
        sessions = self.reader.sessions()
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
        return o

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
