"""OpenCode 2.x server API client (`opencode serve`, 2.3.x; the `/api/*` routes).

Checked against the OpenAPI document of anomalyco/opencode tag v2.3.2
(packages/protocol/openapi.json) and opencode.ai/docs (2026-11-22):

  POST /api/session                      {title, agent, model{id,providerID}, location{directory},
                                          metadata, permissions[{action,resource,effect}]} -> {data}
  POST /api/session/{id}/agent           {agent}         POST /api/session/{id}/model {model}
  POST /api/session/{id}/prompt          {text} -> {data: the queued user message}
  GET  /api/session/active               {data: {sessionID: {type: running}}}
  GET  /api/session/{id}                 {data: session} (404 when missing)
  GET  /api/session/{id}/message         ?limit&order&cursor -> {data: [typed messages], cursor}
  GET  /api/session?parentID=            children; ?directory= for one folder
  POST /api/session/{id}/interrupt       abort
  GET  /api/form                         pending forms (the question tool asks through these)
  POST /api/session/{sid}/form/{fid}/reply            {answer: {field key: value}}
  GET  /api/permission/request           pending permission asks
  POST /api/session/{sid}/permission/{rid}/reply      {decision: once|always|reject, message}
  GET  /api/config                       the config documents (a list)
  POST /api/location/reload              re-read config for the folder
  GET  /api/info                         {version, pid, ...}

The folder (project instance) of a request is chosen with the `x-opencode-directory` header.

Everything the coordinator reads comes back in the opencode 1.x shapes the rest of the code
already parses (`[{info, parts}]` messages, Kilo-style question / permission dicts), so the
engine, lead, helpers, evidence, context guard and telemetry need no second code path.
`to_oc1_message` is that translation; anything it cannot place is left out, never invented.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from foundry_automation.agent_backends import OPENCODE, OPENCODE_GUARD, oc2_rules

from .base import CodingClient, SessionState, parse_report
from .denial import foundry_denial
from .oc1 import AgentServerError, Oc1Client, _is_compaction


# -- 2.x -> 1.x message translation ---------------------------------------------------------

def _tool_output(state: dict) -> str:
    out = []
    for c in state.get("content") or []:
        if isinstance(c, dict):
            if c.get("type") == "text" or "text" in c:
                out.append(str(c.get("text") or ""))
            else:
                out.append(json.dumps(c, default=str)[:4000])
        elif isinstance(c, str):
            out.append(c)
    return "\n".join(x for x in out if x)


def _tool_part(c: dict, msg_id: str, session_id: str) -> dict:
    st = c.get("state") or {}
    status = st.get("status") or ""
    status = {"streaming": "pending"}.get(status, status)
    state = {"status": status, "input": st.get("input") if isinstance(st.get("input"), dict)
             else {"raw": st.get("input")} if st.get("input") else {},
             "metadata": dict(st.get("metadata") or {})}
    t = c.get("time") or {}
    if t:
        state["time"] = {"start": t.get("ran") or t.get("created"), "end": t.get("completed")}
    if status == "completed":
        state["output"] = _tool_output(st)
    if status == "error":
        err = st.get("error") or {}
        state["error"] = str(err.get("message") or err.get("type") or "error")
        state["output"] = _tool_output(st)
    return {"type": "tool", "tool": c.get("name") or "", "callID": c.get("id") or "",
            "messageID": msg_id, "sessionID": session_id, "state": state}


def _error(err) -> Optional[dict]:
    if not err:
        return None
    if isinstance(err, dict):
        # the 1.x shape: {name, data:{message, statusCode, responseBody}}
        data = {"message": err.get("message") or ""}
        if err.get("status") is not None:
            data["statusCode"] = err.get("status")
        body = (err.get("response") or {}).get("body") if isinstance(err.get("response"),
                                                                       dict) else None
        if body:
            data["responseBody"] = body
        return {"name": err.get("type") or "Error", "data": data}
    return {"name": "Error", "data": {"message": str(err)}}


def to_oc1_message(m: dict, session_id: str = "") -> Optional[dict]:
    """One 2.x message as a 1.x `{info, parts}` message, or None for messages the 1.x
    transcript never had (agent/model/location switches, system notes, idle markers)."""
    if not isinstance(m, dict):
        return None
    typ = m.get("type")
    mid = m.get("id") or ""
    t = m.get("time") or {}
    info = {"id": mid, "sessionID": session_id, "time": {"created": t.get("created")}}
    if typ == "user":
        info["role"] = "user"
        parts = [{"type": "text", "text": m.get("text") or ""}]
        for f in m.get("files") or []:
            if isinstance(f, dict):
                parts.append({"type": "file", "url": f.get("uri") or "", "filename": f.get("name")
                              or "", "mime": f.get("mime") or ""})
        return {"info": info, "parts": parts}
    if typ == "synthetic":
        info["role"] = "user"
        return {"info": info, "parts": [{"type": "text", "text": m.get("text") or "",
                                         "synthetic": True}]}
    if typ == "assistant":
        model = m.get("model") or {}
        info.update({"role": "assistant", "agent": m.get("agent") or "", "mode": m.get("agent") or "",
                     "modelID": model.get("id") or "", "providerID": model.get("providerID") or "",
                     "finish": m.get("finish"), "cost": m.get("cost")})
        if t.get("completed"):
            info["time"]["completed"] = t.get("completed")
        tok = m.get("tokens")
        if isinstance(tok, dict):
            info["tokens"] = {"input": tok.get("input") or 0, "output": tok.get("output") or 0,
                              "reasoning": tok.get("reasoning") or 0,
                              "cache": dict(tok.get("cache") or {"read": 0, "write": 0})}
        err = _error(m.get("error"))
        if err:
            info["error"] = err
        if m.get("retry"):
            info["retry"] = m.get("retry")
        parts = []
        for c in m.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append({"type": "text", "text": c.get("text") or ""})
            elif c.get("type") == "reasoning":
                parts.append({"type": "reasoning", "text": c.get("text") or ""})
            elif c.get("type") == "tool":
                parts.append(_tool_part(c, mid, session_id))
        return {"info": info, "parts": parts}
    if typ == "compaction":
        model = m.get("model") or {}
        info.update({"role": "assistant", "summary": True, "mode": "compaction",
                     "agent": "compaction", "modelID": model.get("id") or "",
                     "providerID": model.get("providerID") or ""})
        status = m.get("status")
        if status in ("completed", "failed"):
            info["time"]["completed"] = t.get("created")
        if status == "failed":
            info["error"] = _error(m.get("error")) or {"name": "CompactionFailed", "data": {
                "message": "Compaction failed"}}
        return {"info": info, "parts": [{"type": "compaction", "auto": m.get("reason") == "auto"},
                                        {"type": "text", "text": m.get("summary") or ""}]}
    return None


def to_oc1_messages(rows, session_id: str = "") -> list:
    """Translate a 2.x message list, oldest first. An `idle` marker that ended a run with
    an error puts that error on the run's last assistant message (1.x records it there)."""
    out: list = []
    for m in rows or []:
        if isinstance(m, dict) and m.get("type") == "idle":
            err = _error(m.get("error"))
            if err and m.get("outcome") == "failed":
                for prev in reversed(out):
                    if (prev.get("info") or {}).get("role") == "assistant":
                        prev["info"].setdefault("error", err)
                        break
            continue
        x = to_oc1_message(m, session_id)
        if x is not None:
            out.append(x)
    return out


def _session_view(s: dict) -> dict:
    """A 2.x session as the 1.x session dict (id, title, parentID, time, model.id, agent).
    A child session's title carries "(@<agent> subagent)" as 1.x task-tool children do, so
    helpers.child_agent reads its purpose; the agent is the 2.x session's own `agent`."""
    s = s or {}
    title = str(s.get("title") or "")
    agent = s.get("agent") or ""
    if s.get("parentID") and agent and f"(@{agent} subagent)" not in title:
        title = (title + f" (@{agent} subagent)").strip()
    model = s.get("model") or {}
    return {"id": s.get("id") or "", "title": title, "parentID": s.get("parentID"),
            "agent": agent, "model": {"id": model.get("id") or "",
                                      "providerID": model.get("providerID") or ""},
            "time": dict(s.get("time") or {}), "metadata": s.get("metadata") or {},
            "directory": ((s.get("location") or {}).get("directory")) or s.get("directory")}


def _form_question(f: dict) -> dict:
    """A pending form as the Kilo question dict {request_id, kind, text, options}."""
    fields = [x for x in f.get("fields") or [] if isinstance(x, dict) and not x.get("hidden")]
    text = [str(f.get("title") or "")]
    options = []
    for x in fields:
        text.append(str(x.get("label") or x.get("description") or x.get("key") or ""))
        options.append([str(o.get("label") or o.get("value") or "") for o in x.get("options") or []])
    return {"request_id": f.get("id"), "kind": "question",
            "text": "\n".join(t for t in text if t)[:2000], "options": options}


def _form_answer(f: dict, answers: list) -> dict:
    """Kilo-style answers (one list of labels per question) as a 2.x form answer."""
    out = {}
    fields = [x for x in f.get("fields") or [] if isinstance(x, dict) and not x.get("hidden")]
    for i, x in enumerate(fields):
        got = answers[i] if i < len(answers) else []
        got = got if isinstance(got, list) else [got]
        labels = {str(o.get("label")): o.get("value") for o in x.get("options") or []
                  if isinstance(o, dict)}
        vals = [labels.get(str(g), g) for g in got]
        typ = x.get("type")
        key = x.get("key")
        if not key:
            continue
        if typ == "multiselect":
            out[key] = vals
        elif typ == "boolean":
            out[key] = str(vals[0] if vals else "").strip().lower() in ("yes", "true", "1", "y")
        elif typ in ("number", "integer"):
            try:
                out[key] = (int if typ == "integer" else float)(vals[0])
            except (IndexError, TypeError, ValueError):
                continue
        else:
            out[key] = str(vals[0]) if vals else ""
    return out


class Oc2Client(CodingClient):
    """`CodingClient` over the OpenCode 2.x `/api/*` server."""
    name = "opencode"
    spec = OPENCODE
    label = "OpenCode"
    dialect = "oc2"

    def __init__(self, url: str, password: str = "", username: str = "", provider: str = "foundry",
                 timeout: float = 30):
        username = username or self.spec.default_username
        self.url = url.rstrip("/")
        self.provider = provider
        self.timeout = timeout
        self._auth = ("Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
                      if password else "")
        self._totals: dict = {}
        self._set: dict = {}           # session id -> (agent, persona) last set on the server
        self._lock = threading.Lock()

    # -- http ------------------------------------------------------------------------------

    def _req(self, method: str, path: str, body=None, query: Optional[dict] = None,
             directory: str = "", ok404: bool = False):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("content-type", "application/json")
        req.add_header("accept", "application/json")
        if directory:
            req.add_header("x-opencode-directory", directory)
        if self._auth:
            req.add_header("authorization", self._auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404 and ok404:
                return None
            raise AgentServerError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise AgentServerError(f"{method} {path}: {e}") from None

    @staticmethod
    def _data(r):
        return r.get("data") if isinstance(r, dict) and "data" in r else r

    def health(self) -> dict:
        info = self._req("GET", "/api/info") or {}
        return {"healthy": True, "version": info.get("version"), "pid": info.get("pid")}

    def reload(self, directory: str) -> bool:
        """Re-read the folder's config (2.x has no instance dispose)."""
        self._req("POST", "/api/location/reload", directory=directory)
        return True

    dispose = reload

    # -- sessions -----------------------------------------------------------------------------

    def start_session(self, *, directory, title, persona, agent, metadata, permissions) -> str:
        body = {"title": title[:120], "agent": agent,
                "model": {"providerID": self.provider, "id": persona},
                "location": {"directory": directory},
                "metadata": {"coordinator": metadata},
                "permissions": oc2_rules(list(permissions or []) + OPENCODE_GUARD)}
        s = self._data(self._req("POST", "/api/session", body, directory=directory))
        if not s or not s.get("id"):
            raise AgentServerError(f"{self.label} did not return a session id")
        with self._lock:
            self._set[s["id"]] = (agent, persona)
        return s["id"]

    def _ensure(self, session_id: str, directory: str, agent: str, persona: str) -> None:
        """2.x prompts carry no agent or model: set them on the session when they change
        (the frontier switch and continuation prompts rely on a per-prompt model)."""
        with self._lock:
            have = self._set.get(session_id)
        if have and have[0] == agent and have[1] == persona:
            return
        if agent and (not have or have[0] != agent):
            self._req("POST", f"/api/session/{session_id}/agent", {"agent": agent},
                      directory=directory)
        if persona and (not have or have[1] != persona):
            self._req("POST", f"/api/session/{session_id}/model",
                      {"model": {"providerID": self.provider, "id": persona}}, directory=directory)
        with self._lock:
            self._set[session_id] = (agent, persona)

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        self._ensure(session_id, directory, agent, persona)
        self._req("POST", f"/api/session/{session_id}/prompt", {"text": text}, directory=directory)

    def session(self, session_id: str, directory: str = "") -> Optional[dict]:
        r = self._req("GET", f"/api/session/{session_id}", directory=directory, ok404=True)
        return _session_view(self._data(r)) if r is not None else None

    def exists(self, session_id: str, *, directory: str) -> bool:
        return self._req("GET", f"/api/session/{session_id}", directory=directory,
                         ok404=True) is not None

    def find_session(self, directory: str, marker: str) -> str:
        rows = self._data(self._req("GET", "/api/session", query={"directory": directory,
                                                                  "limit": 200},
                                    directory=directory)) or []
        hits = [r for r in rows if isinstance(r, dict) and marker in str(r.get("title") or "")]
        hits.sort(key=lambda r: ((r.get("time") or {}).get("created") or 0), reverse=True)
        return str(hits[0].get("id") or "") if hits else ""

    def _raw_messages(self, session_id: str, directory: str, limit: int = 0) -> list:
        """Oldest first. limit>0: only the newest `limit`."""
        if limit > 0:
            r = self._req("GET", f"/api/session/{session_id}/message",
                          query={"limit": limit, "order": "desc"}, directory=directory)
            rows = list(self._data(r) or [])
            rows.reverse()
            return rows
        rows: list = []
        cursor = None
        for _ in range(200):                       # 200 pages is far past any real session
            # a cursor page must not repeat `order` (the API refuses the combination)
            q = {"limit": 200, "cursor": cursor} if cursor else {"limit": 200, "order": "asc"}
            r = self._req("GET", f"/api/session/{session_id}/message", query=q,
                          directory=directory) or {}
            rows.extend(self._data(r) or [])
            nxt = ((r.get("cursor") or {}).get("next")) if isinstance(r, dict) else None
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return rows

    def messages(self, session_id: str, directory: str) -> list:
        return to_oc1_messages(self._raw_messages(session_id, directory), session_id)

    def recent(self, session_id: str, *, directory: str, n: int = 4) -> list:
        n = max(1, int(n))
        # ask for a few more: switches and idle markers are dropped by the translation
        rows = to_oc1_messages(self._raw_messages(session_id, directory, limit=n * 3 + 2),
                               session_id)
        return rows[-n:]

    def has_prompt(self, session_id: str, *, directory: str) -> bool:
        return any(((m.get("info") or {}).get("role") == "user")
                   for m in self.recent(session_id, directory=directory, n=50))

    def transcript(self, session_id, *, directory) -> list:
        return self.messages(session_id, directory)

    def children(self, session_id, *, directory) -> list[dict]:
        rows = self._data(self._req("GET", "/api/session", query={"parentID": session_id},
                                    directory=directory)) or []
        return [_session_view(r) for r in rows if isinstance(r, dict)]

    def _token_totals(self, session_id: str, directory: str, last: dict) -> dict:
        info = (last or {}).get("info") or {}
        key = (session_id, info.get("id") or "", (info.get("time") or {}).get("completed"))
        hit = self._totals.get(key)
        if hit is not None and info.get("id"):
            return dict(hit)
        tokens = {"input": 0, "output": 0}
        for m in self.messages(session_id, directory):
            t = ((m.get("info") or {}).get("tokens") or {})
            tokens["input"] += int(t.get("input") or 0)
            tokens["output"] += int(t.get("output") or 0)
        if len(self._totals) > 256:
            self._totals.clear()
        self._totals[key] = dict(tokens)
        return tokens

    def _session_status(self, session_id: str, directory: str) -> dict:
        active = self._data(self._req("GET", "/api/session/active", directory=directory)) or {}
        if session_id not in active:
            return {}
        last = self.recent(session_id, directory=directory, n=1)
        info = (last[-1].get("info") or {}) if last else {}
        retry = info.get("retry") if not (info.get("time") or {}).get("completed") else None
        if retry:
            err = (retry.get("error") or {}) if isinstance(retry, dict) else {}
            return {"type": "retry", "attempt": (retry or {}).get("attempt"),
                    "message": err.get("message") or json.dumps(err)[:300]}
        return {"type": "busy"}

    def statuses(self, directory: str) -> dict:
        """{session id: {type}} of the running sessions (the 1.x /session/status shape)."""
        active = self._data(self._req("GET", "/api/session/active", directory=directory)) or {}
        return {sid: {"type": "busy"} for sid in active}

    def state(self, session_id, *, directory, since_ts) -> SessionState:
        # the same decisions, in the same order, as Oc1Client.state
        if not self.exists(session_id, directory=directory):
            return SessionState(status="missing", error=f"{self.label} has no such session")
        q = self._pending(session_id, directory)
        if q:
            return SessionState(status="attention", question=q)
        st = self._session_status(session_id, directory)
        if st.get("type") in ("busy", "retry"):
            s = SessionState(status="busy")
            if st.get("type") == "retry":
                s.retry = True
                s.error = str(st.get("message") or "")[:300]
                s.denial = foundry_denial(s.error)
            return s
        msgs = self.recent(session_id, directory=directory, n=2)
        last = msgs[-1] if msgs else None
        info = (last or {}).get("info") or {}
        times = info.get("time") or {}
        created = (times.get("created") or 0) / 1000.0
        completed = (times.get("completed") or 0) / 1000.0
        if not last or info.get("role") != "assistant" or max(created, completed) + 1 < since_ts:
            return SessionState(status="busy", error="waiting for the prompt to be admitted",
                                unanswered=True)
        if not times.get("completed") and not info.get("error"):
            return SessionState(status="busy", stalled=True)
        if _is_compaction(last) and not info.get("error"):
            return SessionState(status="busy", error=f"context compacted; waiting for "
                                f"{self.label} to continue",
                                compaction={"message_id": info.get("id") or ""})
        text = "\n".join(p.get("text") or "" for p in last.get("parts") or []
                         if p.get("type") == "text")
        err = info.get("error") or {}
        err_text = json.dumps(err)[:1500] if err else ""
        tokens = self._token_totals(session_id, directory, last)
        lt = info.get("tokens") or {}
        cache = lt.get("cache") or {}
        context = (int(lt.get("input") or 0) + int(cache.get("read") or 0)
                   + int(cache.get("write") or 0)) if lt else None
        return SessionState(status="error" if err else "idle", error=err_text,
                            denial=foundry_denial(err_text) if err else None, final_text=text,
                            report=parse_report(text), tokens=tokens, context=context)

    # -- questions and permissions -----------------------------------------------------------

    def forms(self, directory: str) -> list:
        return list(self._data(self._req("GET", "/api/form", directory=directory)) or [])

    def permission_requests(self, directory: str) -> list:
        return list(self._data(self._req("GET", "/api/permission/request",
                                         directory=directory)) or [])

    def questions(self, directory: str) -> list:
        """Pending forms in the 1.x /question shape (for the telemetry observer)."""
        out = []
        for f in self.forms(directory):
            q = _form_question(f)
            out.append({"id": f.get("id"), "sessionID": f.get("sessionID"),
                        "questions": [{"question": q["text"],
                                       "options": [{"label": o} for o in (q["options"] or [[]])[0]]}]})
        return out

    def permissions(self, directory: str) -> list:
        """Pending permission asks in the 1.x /permission shape."""
        return [{"id": p.get("id"), "sessionID": p.get("sessionID"),
                 "permission": {"shell": "bash", "subagent": "task"}.get(p.get("action"),
                                                                        p.get("action")),
                 "patterns": list(p.get("resources") or []), "metadata": p.get("metadata") or {}}
                for p in self.permission_requests(directory)]

    def _pending(self, session_id: str, directory: str) -> Optional[dict]:
        for f in self.forms(directory):
            if f.get("sessionID") == session_id:
                return _form_question(f)
        for p in self.permission_requests(directory):
            if p.get("sessionID") == session_id:
                perm = {"shell": "bash", "subagent": "task"}.get(p.get("action"), p.get("action"))
                return {"request_id": p.get("id"), "kind": "permission",
                        "text": f"permission {perm} for "
                                f"{', '.join(p.get('resources') or [])}"[:2000], "options": []}
        return None

    def abort(self, session_id, *, directory) -> bool:
        try:
            self._req("POST", f"/api/session/{session_id}/interrupt", directory=directory)
            return True
        except AgentServerError:
            return False

    def answer(self, session_id, request_id, answers, *, directory, kind="question") -> None:
        if kind == "permission":
            body = {"decision": answers[0] if answers else "reject"}
            if len(answers) > 1 and answers[1]:
                body["message"] = str(answers[1])[:500]
            self._req("POST", f"/api/session/{session_id}/permission/{request_id}/reply", body,
                      directory=directory)
            return
        form = next((f for f in self.forms(directory) if f.get("id") == request_id), None)
        if form is None:
            raise AgentServerError(f"{self.label} has no pending form {request_id}")
        self._req("POST", f"/api/session/{session_id}/form/{request_id}/reply",
                  {"answer": _form_answer(form, list(answers or []))}, directory=directory)

    # -- configuration ------------------------------------------------------------------------

    # the same check as 1.x: read the provider's baseURL / key from the merged config and ask
    # the router's /models with the agent's own key (provider options stay in memory)
    inference_models = Oc1Client.inference_models

    def effective_config(self, directory: str) -> dict:
        """The folder's config documents merged in order (later wins), in the 1.x key names
        the checks read: provider, agent, enabled_providers, subagent_depth, mcp."""
        r = self._req("GET", "/api/config", directory=directory)
        docs = self._data(r)
        if isinstance(docs, dict):
            docs = [docs]
        merged: dict = {}
        for d in docs or []:
            # Config.Entry: {type: "document", path, info: <config>} | {type: "directory", path}
            if not isinstance(d, dict) or d.get("type") == "directory":
                continue
            cfg = d.get("info") if isinstance(d.get("info"), dict) else \
                d.get("config") if isinstance(d.get("config"), dict) else d
            if isinstance(cfg, dict):
                _deep_merge(merged, cfg)
        return oc1_config_view(merged)

    def model_config_problem(self, directory: str, persona: str, cfg: dict | None = None) -> str:
        if cfg is None:
            cfg = self.effective_config(directory)
        provider = (cfg.get("provider") or {}).get(self.provider)
        if provider is None:
            return f"{self.label} provider {self.provider!r} is not configured"
        models = provider.get("models")
        if isinstance(models, dict) and models and persona not in models:
            return f"{self.label} provider {self.provider} has no model {persona}"
        return ""

    def preflight(self, directory: str) -> list[tuple[str, str, str]]:
        out = []
        try:
            h = self.health()
            out.append(("PASS", "opencode serve", f"reachable, version {h.get('version')}"))
        except AgentServerError as e:
            return [("FAIL", "opencode serve", str(e))]
        try:
            cfg = self.effective_config(directory)
        except AgentServerError as e:
            return out + [("FAIL", "opencode config", str(e))]
        providers = list((cfg.get("provider") or {}).keys())
        if self.provider not in providers:
            out.append(("FAIL", "provider", f"no '{self.provider}' provider in OpenCode's config "
                                            f"(have {providers})"))
        else:
            out.append(("PASS", "provider", f"'{self.provider}' configured"))
        enabled = cfg.get("enabled_providers")
        if enabled != [self.provider]:
            out.append(("WARN", "enabled_providers",
                        f"is {enabled!r}; set it to [\"{self.provider}\"] so a subagent can't "
                        f"fall back to OpenCode's own providers (paid or free models)"))
        else:
            out.append(("PASS", "enabled_providers", f"only {self.provider}"))
        share = cfg.get("share")
        out.append(("PASS" if share == "disabled" else "FAIL", "share",
                    f"{share!r} (must be \"disabled\": sessions are never uploaded)"))
        depth = cfg.get("subagent_depth")
        out.append(("PASS" if depth in (0, 1) else "WARN", "subagent_depth",
                    f"{depth!r} (1 = helpers can't start their own helpers)"))
        return out


def _deep_merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def oc1_config_view(cfg: dict) -> dict:
    """A 2.x config (native keys, 1.x keys, or a mix) in the 1.x names: `providers` ->
    `provider`, `agents` -> `agent`, `experimental.subagent_depth` -> `subagent_depth`,
    `mcp.servers` -> `mcp`. Values already under the 1.x names win only when the 2.x name
    is absent (2.x itself prefers its native value)."""
    out = dict(cfg or {})
    if isinstance(out.get("providers"), dict):
        prov = dict(out.get("provider") or {})
        _deep_merge(prov, out["providers"])
        out["provider"] = prov
    if isinstance(out.get("agents"), dict):
        ag = dict(out.get("agent") or {})
        _deep_merge(ag, out["agents"])
        out["agent"] = ag
    exp = out.get("experimental") if isinstance(out.get("experimental"), dict) else {}
    if "subagent_depth" in exp:
        out["subagent_depth"] = exp["subagent_depth"]
    mcp = out.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
        flat = {k: v for k, v in mcp.items() if k != "servers"}
        flat.update(mcp["servers"])
        out["mcp"] = flat
    return out


__all__ = ["Oc2Client", "to_oc1_message", "to_oc1_messages", "oc1_config_view"]
