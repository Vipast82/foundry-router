"""A stand-in for `opencode serve` 2.x (the `/api/*` server) for tests.

It is NOT OpenCode: /api/info reports version "fake-2.3.2". It serves the routes the
coordinator's Oc2Client and the telemetry Oc2Observer use, with the shapes of the
OpenCode 2.3.2 OpenAPI document, behind HTTP Basic auth (user `opencode`). The folder of
a request is read from the `x-opencode-directory` header. It has no /global/health, so
client detection sees a 2.x server.

`sse_events` (v0.181.256) is what GET /api/event streams; None makes it a 404.
Tests drive it from Python: `finish(sid, text)` ends a run with an assistant message,
`ask(sid, ...)` opens a form, `ask_permission(sid, ...)` a permission request,
`spawn_child(...)` a child session. `on_prompt(fake, sid, text)` runs on each prompt
(default: nothing; the session stays running until `finish`).
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


class FakeOpenCode2:
    def __init__(self, password: str = "fake-opencode-password"):
        self.password = password
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list] = {}
        self.active: set[str] = set()
        self.forms: dict[str, dict] = {}
        self.perm_requests: dict[str, dict] = {}
        self.replies: list[dict] = []
        self.prompts: list[dict] = []
        self.requests: list[dict] = []           # (method, path, directory header)
        self.interrupts: list[str] = []
        self.reloads: list[str] = []
        # v0.181.256: GET /api/event streams these 2.x events (server-sent events), then ends
        # the response; None = no event stream (404, like a server without one)
        self.sse_events: list | None = []
        self.config_docs = [
            {"type": "document", "path": "/home/u/.config/opencode/opencode.jsonc",
             "info": {"share": "disabled", "enabled_providers": ["foundry"],
                      "provider": {"foundry": {"npm": "@ai-sdk/openai-compatible",
                                               "options": {"baseURL": "http://router/v1",
                                                           "apiKey": "sk-never-leaves-oc-123"},
                                               "models": {"Foundry-Kilo-Local": {
                                                   "limit": {"context": 262144, "output": 8192}}}}}}},
            {"type": "document", "path": "/work/opencode.jsonc",
             "info": {"experimental": {"subagent_depth": 1},
                      "agents": {"build": {"mode": "primary"}, "au-helper": {"mode": "subagent"}}}},
            {"type": "directory", "path": "/work/.opencode"},
        ]
        self.on_prompt = None
        self.server = None
        self.thread = None

    # ------------------------------------------------------------------ driving

    def new_session(self, *, title="", agent="build", model=None, directory="", metadata=None,
                    permissions=None, parent=None) -> str:
        sid = _id("ses_")
        with self.lock:
            self.sessions[sid] = {"id": sid, "title": title, "agent": agent,
                                  "model": model or {"id": "", "providerID": "foundry"},
                                  "location": {"directory": directory}, "metadata": metadata or {},
                                  "permissions": list(permissions or []),
                                  "time": {"created": _now_ms(), "updated": _now_ms()},
                                  **({"parentID": parent} if parent else {})}
            self.messages[sid] = []
        return sid

    def _msg(self, sid: str, m: dict) -> dict:
        m.setdefault("id", _id("msg_"))
        m.setdefault("metadata", {})
        m.setdefault("time", {"created": _now_ms()})
        with self.lock:
            self.messages[sid].append(m)
        return m

    def finish(self, sid: str, text: str, *, tokens=None, error=None, tools=()) -> None:
        """End the running turn: an assistant message (text, tool calls) and an idle marker."""
        s = self.sessions[sid]
        now = _now_ms()
        content = [{"type": "tool", "id": _id("call_"), "name": t.get("name", "bash"),
                    "executed": True,
                    "state": {"status": "completed", "input": t.get("input") or {},
                              "content": [{"type": "text", "text": t.get("output", "")}],
                              "metadata": {}}, "time": {"created": now, "completed": now}}
                   for t in tools]
        if text:
            content.append({"type": "text", "text": text})
        msg = {"type": "assistant", "agent": s["agent"], "model": dict(s["model"]),
               "content": content, "finish": "error" if error else "stop",
               "tokens": tokens or {"input": 1200, "output": 300, "reasoning": 0,
                                    "cache": {"read": 400, "write": 0}},
               "time": {"created": now, "completed": now}}
        if error:
            msg["error"] = error
        self._msg(sid, msg)
        self._msg(sid, {"type": "idle", "outcome": "failed" if error else "succeeded",
                        **({"error": error} if error else {})})
        with self.lock:
            self.active.discard(sid)

    def compact(self, sid: str, summary: str = "summary of earlier work") -> None:
        s = self.sessions[sid]
        self._msg(sid, {"type": "compaction", "status": "completed", "reason": "auto",
                        "model": dict(s["model"]), "summary": summary,
                        "tokens": {"input": 9000, "output": 500, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}}})
        with self.lock:
            self.active.discard(sid)

    def retrying(self, sid: str, message: str) -> None:
        """A running turn whose provider call is being retried."""
        s = self.sessions[sid]
        self._msg(sid, {"type": "assistant", "agent": s["agent"], "model": dict(s["model"]),
                        "content": [], "retry": {"attempt": 2, "at": _now_ms(),
                                                 "error": {"type": "APIError",
                                                           "message": message}}})
        with self.lock:
            self.active.add(sid)

    def ask(self, sid: str, question: str, options=("yes", "no")) -> str:
        fid = _id("frm_")
        with self.lock:
            self.forms[fid] = {"id": fid, "sessionID": sid, "title": question, "metadata": {},
                               "fields": [{"key": "answer", "type": "string", "label": question,
                                           "options": [{"value": o, "label": o}
                                                       for o in options]}]}
        return fid

    def ask_permission(self, sid: str, action: str = "shell", resources=("git push",)) -> str:
        rid = _id("per_")
        with self.lock:
            self.perm_requests[rid] = {"id": rid, "sessionID": sid, "action": action,
                                       "resources": list(resources), "metadata": {}}
        return rid

    def spawn_child(self, parent: str, agent: str = "explore", title: str = "look around") -> str:
        p = self.sessions[parent]
        return self.new_session(title=title, agent=agent, model=dict(p["model"]),
                                directory=p["location"]["directory"], parent=parent)

    # ------------------------------------------------------------------ server

    def start(self, port: int = 0) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                want = "Basic " + base64.b64encode(f"opencode:{fake.password}".encode()).decode()
                if self.headers.get("authorization") != want:
                    self._send(401, {"error": "unauthorized"})
                    return False
                return True

            def _send(self, code, body):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _go(self, method):
                if not self._auth():
                    return
                u = urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                d = self.headers.get("x-opencode-directory") or ""
                with fake.lock:
                    fake.requests.append({"method": method, "path": u.path, "directory": d})
                code, out = fake.route(method, unquote(u.path), parse_qs(u.query), body, d)
                self._send(code, out)

            def do_GET(self):
                if urlsplit(self.path).path == "/api/event":
                    if not self._auth():
                        return
                    if fake.sse_events is None:
                        self._send(404, {"error": "not found"})
                        return
                    with fake.lock:
                        fake.requests.append({"method": "GET", "path": "/api/event",
                                              "directory": self.headers.get(
                                                  "x-opencode-directory") or ""})
                        evs = list(fake.sse_events)
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    for ev in evs:
                        self.wfile.write(f"event: {ev.get('type')}\ndata: {json.dumps(ev)}\n\n"
                                         .encode())
                    self.wfile.flush()
                    return
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    # ------------------------------------------------------------------ routes

    def route(self, method: str, path: str, q: dict, body, directory: str):
        one = lambda k: (q.get(k) or [""])[0]                    # noqa: E731
        parts = path.strip("/").split("/")
        cb = None
        with self.lock:
            if method == "GET" and path == "/api/info":
                return 200, {"version": "fake-2.3.2", "pid": 1, "urls": [], "paths": {"tmp": "/t"}}
            if method == "GET" and path == "/api/config":
                return 200, self.config_docs
            if method == "GET" and path == "/api/mcp":
                return 200, {"data": {}}
            if method == "POST" and path == "/api/location/reload":
                self.reloads.append(directory)
                return 204, None
            if method == "GET" and path == "/api/session/active":
                return 200, {"data": {sid: {"type": "running"} for sid in self.active}}
            if method == "GET" and path == "/api/form":
                return 200, {"location": {"directory": directory},
                             "data": list(self.forms.values())}
            if method == "GET" and path == "/api/permission/request":
                return 200, {"location": {"directory": directory},
                             "data": list(self.perm_requests.values())}
            if path == "/api/session" and method == "POST":
                b = body or {}
                extra = set(b) - {"id", "parentID", "title", "agent", "model", "location",
                                  "metadata", "permissions"}
                if extra:
                    return 400, {"error": f"unknown fields {sorted(extra)}"}
                for r in b.get("permissions") or []:
                    if set(r) != {"action", "resource", "effect"} or \
                            r["effect"] not in ("allow", "deny", "ask"):
                        return 400, {"error": f"bad permission rule {r}"}
                sid = self.new_session(title=b.get("title") or "", agent=b.get("agent") or "build",
                                       model=b.get("model"),
                                       directory=(b.get("location") or {}).get("directory") or "",
                                       metadata=b.get("metadata"), permissions=b.get("permissions"))
                return 200, {"data": self.sessions[sid]}
            if path == "/api/session" and method == "GET":
                rows = list(self.sessions.values())
                if one("parentID"):
                    rows = [s for s in rows if s.get("parentID") == one("parentID")]
                if one("directory"):
                    rows = [s for s in rows if s["location"]["directory"] == one("directory")]
                return 200, {"data": rows, "cursor": {"previous": None, "next": None}}
            if len(parts) >= 3 and parts[:2] == ["api", "session"]:
                sid = parts[2]
                s = self.sessions.get(sid)
                if s is None:
                    return 404, {"error": "no session"}
                rest = parts[3:]
                if not rest and method == "GET":
                    return 200, {"data": s}
                if rest == ["agent"] and method == "POST":
                    s["agent"] = (body or {})["agent"]
                    self._msg(sid, {"type": "agent-switched", "agent": s["agent"]})
                    return 200, {"data": s}
                if rest == ["model"] and method == "POST":
                    s["model"] = dict((body or {})["model"])
                    self._msg(sid, {"type": "model-switched", "model": s["model"]})
                    return 200, {"data": s}
                if rest == ["prompt"] and method == "POST":
                    if set(body or {}) - {"id", "text", "files", "agents", "skills", "metadata",
                                          "delivery", "resume"}:
                        return 400, {"error": "unknown prompt fields"}
                    text = (body or {}).get("text") or ""
                    m = self._msg(sid, {"type": "user", "text": text})
                    self.prompts.append({"session_id": sid, "text": text, "ts": time.time(),
                                         "model": s["model"].get("id"), "agent": s["agent"]})
                    self.active.add(sid)
                    cb = self.on_prompt
                    out = {"data": {"id": m["id"], "sessionID": sid, "time": m["time"],
                                    "type": "user", "payload": {"text": text},
                                    "delivery": "queue"}}
                elif rest == ["interrupt"] and method == "POST":
                    self.interrupts.append(sid)
                    self.active.discard(sid)
                    return 200, {"data": {"interrupted": True}}
                elif rest == ["message"] and method == "GET":
                    rows = list(self.messages[sid])
                    if one("order") == "desc":     # newest first; the client reverses it
                        rows.reverse()
                    lim = int(one("limit") or 0)
                    if lim:
                        rows = rows[:lim]
                    return 200, {"data": rows, "cursor": {"previous": None, "next": None}}
                elif len(rest) == 3 and rest[0] == "form" and rest[2] == "reply":
                    f = self.forms.pop(rest[1], None)
                    if f is None:
                        return 404, {"error": "no form"}
                    self.replies.append({"kind": "form", "request_id": rest[1],
                                         "session_id": sid, "body": body})
                    return 204, None
                elif len(rest) == 3 and rest[0] == "permission" and rest[2] == "reply":
                    p = self.perm_requests.pop(rest[1], None)
                    if p is None:
                        return 404, {"error": "no permission request"}
                    if (body or {}).get("decision") not in ("once", "always", "reject"):
                        return 400, {"error": "bad decision"}
                    self.replies.append({"kind": "permission", "request_id": rest[1],
                                         "session_id": sid, "body": body})
                    return 204, None
                else:
                    return 404, {"error": f"no route {method} {path}"}
            else:
                return 404, {"error": f"no route {method} {path}"}
        if cb:
            cb(self, sid, text)
        return 200, out


__all__ = ["FakeOpenCode2"]
