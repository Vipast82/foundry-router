"""A stand-in for `kilo serve` for tests and the control-center dev fixture.

It is NOT Kilo and never claims to be: health reports version "fake-7.8.8".
It serves the subset of the API the coordinator (adapters/kilo.py) and the
telemetry bridge (telemetry.py) use, with the shapes observed on Kilo 7.8.8 /
the 7.8.12 SDK, behind HTTP Basic auth, plus an `/event` SSE stream. Tests
drive it through Python methods (add parts, stream text, ask a question,
spawn a helper, finish a turn).
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _now_ms() -> int:
    return int(time.time() * 1000)


class FakeKilo:
    def __init__(self, password: str = "fake-kilo-password", unsupported: tuple = ()):
        self.password = password
        self.unsupported = set(unsupported)      # paths that 404 (capability probing)
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.questions: dict[str, dict] = {}
        self.permissions: dict[str, dict] = {}
        self.replies: list[dict] = []
        self.prompts: list[dict] = []
        self.aborts: list[str] = []
        self.subscribers: list[queue.Queue] = []
        self.mcp_status = {"Roblox_Studio": {"status": "connected"},
                           "searxng": {"status": "connected"},
                           "playwright": {"status": "disabled"},
                           "robloxstudio-mcp": {"status": "failed", "error": "bridge not running"}}
        self.config = {
            "enabled_providers": ["foundry"], "subagent_depth": 1,
            "provider": {"foundry": {"options": {"apiKey": "sk-should-never-leave-kilo-123456"},
                                     "models": {"Foundry-Kilo-Local": {"limit": {"context": 262144,
                                                                                 "output": 8192}},
                                                "Foundry-Kilo-Planner": {"limit": {"context": 200000,
                                                                                   "output": 8192}},
                                                "Foundry-Kilo-Research": {"limit": {"context": 32768,
                                                                                    "output": 4096}}}}},
            "agent": {"code": {"model": "foundry/Foundry-Kilo-Local", "mode": "primary"},
                      "au-planner": {"model": "foundry/Foundry-Kilo-Planner", "mode": "subagent",
                                     "tools": {"bash": False, "edit": False, "Roblox_Studio*": False}},
                      "au-research": {"model": "foundry/Foundry-Kilo-Research", "mode": "subagent"}},
            "mcp": {"Roblox_Studio": {"type": "local", "command": ["studio-mcp"], "enabled": True,
                                      "environment": {"TOKEN": "secret-env-token-9999"}},
                    "searxng": {"type": "remote", "url": "http://user:pw@searx.local", "enabled": True},
                    "playwright": {"type": "local", "command": ["npx", "pw"], "enabled": False},
                    "robloxstudio-mcp": {"type": "local", "command": ["x"], "enabled": True}}}
        self.todos: dict[str, list] = {}
        self.on_prompt = None                    # callable(fake, session_id, text)
        self.server = None
        self.thread = None

    # ------------------------------------------------------------------ server

    def start(self, port: int = 0) -> str:
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                want = "Basic " + base64.b64encode(f"kilo:{fake.password}".encode()).decode()
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
                self.wfile.write(raw)

            def do_GET(self):
                if not self._auth():
                    return
                u = urlsplit(self.path)
                if u.path == "/event":
                    return fake._sse(self)
                code, body = fake.get(u.path, parse_qs(u.query))
                self._send(code, body)

            def do_POST(self):
                if not self._auth():
                    return
                u = urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                code, out = fake.post(u.path, parse_qs(u.query), body)
                if code == 204:
                    self.send_response(204)
                    self.send_header("content-length", "0")
                    self.end_headers()
                else:
                    self._send(code, out)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        for q in list(self.subscribers):
            q.put(None)
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def _sse(self, h) -> None:
        if "/event" in self.unsupported:
            return h._send(404, {"error": "not found"})
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
        h.send_response(200)
        h.send_header("content-type", "text/event-stream")
        h.send_header("cache-control", "no-cache")
        h.end_headers()
        try:
            h.wfile.write(b'data: {"type":"server.connected","properties":{}}\n\n')
            h.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=5)
                except queue.Empty:
                    h.wfile.write(b'data: {"type":"server.heartbeat","properties":{}}\n\n')
                    h.wfile.flush()
                    continue
                if ev is None:
                    return
                h.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                h.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            return
        finally:
            with self.lock:
                if q in self.subscribers:
                    self.subscribers.remove(q)

    def emit(self, typ: str, props: dict) -> None:
        ev = json.loads(json.dumps({"type": typ, "properties": props}))   # as of now
        with self.lock:
            for q in list(self.subscribers):
                q.put(ev)

    # ------------------------------------------------------------------ routing

    def get(self, path: str, q: dict):
        if path in self.unsupported:
            return 404, {"error": "not found"}
        with self.lock:
            if path == "/global/health":
                return 200, {"healthy": True, "version": "fake-7.8.8"}
            if path == "/session/status":
                return 200, {sid: {"type": s["status"]} for sid, s in self.sessions.items()
                             if s["status"]}
            if path == "/question":
                return 200, list(self.questions.values())
            if path == "/permission":
                return 200, list(self.permissions.values())
            if path == "/mcp":
                return 200, self.mcp_status
            if path == "/config/effective":
                return 200, self.config
            if path == "/lsp":
                return 200, [{"id": "luau-lsp", "status": "connected"}]
            if path == "/memory/status":
                return 200, {"enabled": False}
            if path == "/indexing/status":
                return 200, {"state": "disabled"}
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "session":
                s = self.sessions.get(parts[1])
                if s is None:
                    return 404, {"error": "no session"}
                if len(parts) == 2:
                    return 200, s["info"]
                if parts[2] == "message":
                    return 200, s["messages"]
                if parts[2] == "children":
                    return 200, [self.sessions[c]["info"] for c in s["children"]]
                if parts[2] == "todo":
                    return 200, self.todos.get(parts[1], [])
        return 404, {"error": f"no route {path}"}

    def post(self, path: str, q: dict, body):
        with self.lock:
            parts = path.strip("/").split("/")
            if path == "/session":
                sid = self.new_session(title=(body or {}).get("title") or "",
                                       agent=(body or {}).get("agent") or "code",
                                       model=((body or {}).get("model") or {}).get("id") or "",
                                       metadata=(body or {}).get("metadata") or {})
                return 200, self.sessions[sid]["info"]
            if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
                sid = parts[1]
                text = "".join(p.get("text") or "" for p in (body or {}).get("parts") or [])
                self.prompts.append({"session_id": sid, "text": text, "ts": time.time()})
                self.add_message(sid, "user", [{"type": "text", "text": text}],
                                 agent=(body or {}).get("agent"))
                self.set_status(sid, "busy")
                cb = self.on_prompt
            elif len(parts) == 3 and parts[0] == "session" and parts[2] == "abort":
                self.aborts.append(parts[1])
                self.set_status(parts[1], None)
                return 200, True
            elif len(parts) == 3 and parts[0] == "question" and parts[2] == "reply":
                qq = self.questions.pop(parts[1], None)
                if qq is None:
                    return 404, {"error": "no such question"}
                self.replies.append({"kind": "question", "request_id": parts[1],
                                     "session_id": qq["sessionID"], "body": body})
                self.emit("question.replied", {"sessionID": qq["sessionID"], "requestID": parts[1]})
                self.set_status(qq["sessionID"], "busy")
                return 200, True
            elif len(parts) == 3 and parts[0] == "permission" and parts[2] == "reply":
                pp = self.permissions.pop(parts[1], None)
                if pp is None:
                    return 404, {"error": "no such permission"}
                self.replies.append({"kind": "permission", "request_id": parts[1],
                                     "session_id": pp["sessionID"], "body": body})
                self.emit("permission.replied", {"sessionID": pp["sessionID"], "requestID": parts[1]})
                self.set_status(pp["sessionID"], "busy")
                return 200, True
            else:
                return 404, {"error": f"no route {path}"}
        if cb:
            threading.Thread(target=cb, args=(self, sid, text), daemon=True).start()
        return 204, None

    # ------------------------------------------------------------------ scenario API

    def new_session(self, title: str = "", agent: str = "code", model: str = "",
                    parent: str = "", metadata: dict | None = None) -> str:
        sid = f"ses_{uuid.uuid4().hex[:20]}"
        with self.lock:
            self.sessions[sid] = {"info": {"id": sid, "title": title, "parentID": parent or None,
                                           "time": {"created": _now_ms(), "updated": _now_ms()},
                                           "metadata": metadata or {}},
                                  "messages": [], "status": None, "children": [],
                                  "agent": agent, "model": model}
            if parent and parent in self.sessions:
                self.sessions[parent]["children"].append(sid)
        self.emit("session.created", {"info": self.sessions[sid]["info"]})
        return sid

    def set_status(self, sid: str, status) -> None:
        with self.lock:
            if sid in self.sessions:
                self.sessions[sid]["status"] = status
        self.emit("session.status", {"sessionID": sid, "status": {"type": status or "idle"}})

    def add_message(self, sid: str, role: str, parts: list[dict], agent: str = "",
                    model: str = "", tokens: dict | None = None, completed: bool = True,
                    cost: float = 0.0, error: dict | None = None) -> str:
        mid = f"msg_{uuid.uuid4().hex[:20]}"
        with self.lock:
            s = self.sessions[sid]
            info = {"id": mid, "sessionID": sid, "role": role, "time": {"created": _now_ms()},
                    "agent": agent or s["agent"]}
            if role == "assistant":
                info.update(modelID=model or s["model"] or "Foundry-Kilo-Local",
                            providerID="foundry", mode=agent or s["agent"], cost=cost,
                            tokens=tokens or {"input": 0, "output": 0, "reasoning": 0,
                                              "cache": {"read": 0, "write": 0}})
                if completed:
                    info["time"]["completed"] = _now_ms()
                if error:
                    info["error"] = error
            msg = {"info": info, "parts": []}
            s["messages"].append(msg)
            for p in parts:
                self._add_part(sid, msg, p)
        self.emit("message.updated", {"info": info})
        return mid

    def _add_part(self, sid, msg, p: dict) -> dict:
        part = {"id": p.get("id") or f"prt_{uuid.uuid4().hex[:20]}", "sessionID": sid,
                "messageID": msg["info"]["id"], **p}
        part.setdefault("time", {"start": _now_ms(), "end": _now_ms()})
        msg["parts"].append(part)
        self.emit("message.part.updated", {"part": part})
        return part

    def add_part(self, sid: str, part: dict, message_id: str = "") -> dict:
        with self.lock:
            s = self.sessions[sid]
            msg = next((m for m in s["messages"] if m["info"]["id"] == message_id),
                       s["messages"][-1])
            return self._add_part(sid, msg, part)

    def update_part(self, sid: str, part_id: str, **fields) -> dict:
        with self.lock:
            for m in self.sessions[sid]["messages"]:
                for p in m["parts"]:
                    if p["id"] == part_id:
                        p.update(fields)
                        self.emit("message.part.updated", {"part": p})
                        return p
        raise KeyError(part_id)

    def delta(self, sid: str, part_id: str, text: str, field: str = "text") -> None:
        with self.lock:
            for m in self.sessions[sid]["messages"]:
                for p in m["parts"]:
                    if p["id"] == part_id:
                        p[field] = (p.get(field) or "") + text
                        self.emit("message.part.delta", {"sessionID": sid, "messageID": m["info"]["id"],
                                                         "partID": part_id, "field": field,
                                                         "delta": text})
                        return

    def complete_last(self, sid: str, tokens: dict | None = None) -> None:
        with self.lock:
            m = self.sessions[sid]["messages"][-1]
            m["info"]["time"]["completed"] = _now_ms()
            if tokens:
                m["info"]["tokens"] = tokens
        self.emit("message.updated", {"info": m["info"]})

    def set_todo(self, sid: str, items: list[dict]) -> None:
        with self.lock:
            self.todos[sid] = items
        self.emit("todo.updated", {"sessionID": sid, "todos": items})

    def ask_question(self, sid: str, questions: list[dict]) -> str:
        rid = f"que_{uuid.uuid4().hex[:16]}"
        with self.lock:
            self.questions[rid] = {"id": rid, "sessionID": sid, "questions": questions,
                                   "time": {"created": _now_ms()}}
            self.sessions[sid]["status"] = None
        self.emit("question.asked", self.questions[rid])
        return rid

    def ask_permission(self, sid: str, permission: str, patterns: list[str],
                       metadata: dict | None = None) -> str:
        rid = f"per_{uuid.uuid4().hex[:16]}"
        with self.lock:
            self.permissions[rid] = {"id": rid, "sessionID": sid, "permission": permission,
                                     "patterns": patterns, "always": [p + "*" for p in patterns],
                                     "metadata": metadata or {}, "time": {"created": _now_ms()}}
            self.sessions[sid]["status"] = None
        self.emit("permission.asked", self.permissions[rid])
        return rid

    def finish(self, sid: str, text: str, tokens: dict | None = None) -> None:
        self.add_message(sid, "assistant", [{"type": "text", "text": text}],
                         tokens=tokens or {"input": 900, "output": 120, "reasoning": 0,
                                           "cache": {"read": 4000, "write": 0}})
        self.set_status(sid, None)
