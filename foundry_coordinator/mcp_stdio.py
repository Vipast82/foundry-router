"""Minimal MCP stdio client (stdlib only) for the Studio gate runner.

Starts an MCP server command, speaks newline-delimited JSON-RPC 2.0 over its
stdin/stdout, and calls tools. Verified shapes (2026-10-19/20, this PC):
  official Studio MCP  cmd.exe /d /c %LOCALAPPDATA%\\Roblox\\mcp.bat
                       every tool takes studio_id (from list_roblox_studios)
  community MCP        cmd.exe /d /c "C:\\Program Files\\nodejs\\npx.cmd" -y @chrrxs/robloxstudio-mcp@3.4.2
                       one Studio -> no studio_id

    with McpClient(argv, name="official") as c:     # start + initialize
        tools = c.list_tools()
        r = c.call_tool("execute_luau", {...}, timeout=60)
        r["text"], r["images"] == [(mime, bytes)], r["is_error"]

Fail closed: a timeout raises McpTimeout, a dead server raises McpError for
every pending request; nothing is ever reported as a successful empty result.
Env overrides (tests, unusual installs): FOUNDRY_MCP_OFFICIAL / FOUNDRY_MCP_COMMUNITY
hold a JSON argv list that replaces the default command.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import json
import os
import subprocess
import threading
import time
from typing import Optional

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "foundry-studio-gate", "version": "1"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
COMMUNITY_PACKAGE = "@chrrxs/robloxstudio-mcp@3.4.2"
CONNECT_WAIT_S = 30.0          # how long a fresh community server waits for the Studio plugin
NPX = r"C:\Program Files\nodejs\npx.cmd"


def kill_tree(pid: int, timeout: float = 15.0) -> bool:
    """Kill a process and all its children (Windows: taskkill /T /F; else SIGKILL to
    its process group when it leads one, else the pid). True when the kill ran."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True,
                               timeout=timeout, **_NO_WINDOW)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    import signal
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


class McpError(RuntimeError):
    pass


class McpTimeout(McpError):
    pass


class McpClient:
    """One MCP server process. Thread-safe request/response by id."""

    def __init__(self, argv: list, *, name: str = "mcp", env: Optional[dict] = None,
                 cwd: Optional[str] = None, init_timeout: float = 90.0, autostart: bool = True):
        self.argv, self.name, self.env, self.cwd = list(argv), name, env, cwd
        self.init_timeout = init_timeout
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._pending: dict = {}                    # id -> [Event, response|None]
        self._dead: Optional[str] = None
        self.stderr_tail = collections.deque(maxlen=200)
        self.notifications = collections.deque(maxlen=200)
        self.server_info: dict = {}
        self.tools: Optional[list] = None
        if autostart:
            self.start()

    # ---- lifecycle -------------------------------------------------------------
    def start(self) -> "McpClient":
        env = dict(os.environ)
        env.update(self.env or {})
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.cwd, env=env, bufsize=0, **_NO_WINDOW)
        except OSError as e:
            raise McpError(f"{self.name}: cannot start {self.argv[0]}: {e}") from e
        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()
        try:
            self.initialize(self.init_timeout)
        except Exception:
            self.close()
            raise
        return self

    def initialize(self, timeout: float) -> dict:
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": CLIENT_INFO}, timeout=timeout)
        self.server_info = res or {}
        self.notify("notifications/initialized")
        return self.server_info

    def close(self, grace: float = 3.0) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        if os.name == "nt":
            # cmd.exe wraps node / mcp.bat: kill the whole tree while the parent is
            # still alive (once cmd.exe has exited its children can no longer be
            # found by pid and would be orphaned). The servers hold no state.
            if p.poll() is None:
                kill_tree(p.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(grace)
        else:
            try:
                p.wait(grace)
            except subprocess.TimeoutExpired:
                p.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(grace)
        for pipe in (p.stdout, p.stderr):
            with contextlib.suppress(OSError, ValueError):
                if pipe:
                    pipe.close()
        self._fail_all("closed")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self._dead is None

    # ---- io --------------------------------------------------------------------
    def _read_stdout(self):
        p = self.proc
        try:
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.stderr_tail.append("[stdout non-json] " + line[:500])
                    continue
                if not isinstance(msg, dict):          # `42`, `[...]`: not JSON-RPC
                    self.stderr_tail.append("[stdout non-object] " + line[:500])
                    continue
                try:
                    self._dispatch(msg)
                except Exception as e:                 # noqa: BLE001 - never kill the reader
                    self.stderr_tail.append(f"[dispatch error] {type(e).__name__}: {e}"[:500])
        except (OSError, ValueError):
            pass
        finally:
            self._fail_all(f"server exited (rc={p.poll()}); stderr: "
                           + " | ".join(list(self.stderr_tail)[-5:]))

    def _read_stderr(self):
        p = self.proc
        try:
            for raw in iter(p.stderr.readline, b""):
                self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def _dispatch(self, msg: dict):
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                slot = self._pending.get(msg["id"])
            if slot is not None:
                slot[1] = msg
                slot[0].set()
            return
        if "method" in msg and "id" in msg:            # server -> client request
            if msg["method"] == "ping":
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": "method not supported"}})
            return
        self.notifications.append(msg)

    def _fail_all(self, why: str):
        with self._lock:
            self._dead = self._dead or why
            slots = list(self._pending.values())
        for s in slots:
            s[0].set()

    def _send(self, obj: dict):
        p = self.proc
        if p is None or p.stdin is None:
            raise McpError(f"{self.name}: not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wlock:
            try:
                p.stdin.write(data)
                p.stdin.flush()
            except OSError as e:
                raise McpError(f"{self.name}: write failed: {e}") from e

    def notify(self, method: str, params: Optional[dict] = None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: Optional[dict] = None, *, timeout: float = 60.0):
        with self._lock:
            if self._dead:
                raise McpError(f"{self.name}: {self._dead}")
            rid = self._next_id
            self._next_id += 1
            slot = [threading.Event(), None]
            self._pending[rid] = slot
        try:
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            self._send(msg)
            if not slot[0].wait(timeout):
                raise McpTimeout(f"{self.name}: {method} timed out after {timeout:.0f}s")
            resp = slot[1]
            if resp is None:
                raise McpError(f"{self.name}: {self._dead or 'no response'}")
            if "error" in resp:
                e = resp["error"] or {}
                raise McpError(f"{self.name}: {method} error {e.get('code')}: {e.get('message')}")
            return resp.get("result")
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    # ---- MCP methods -----------------------------------------------------------
    def list_tools(self, timeout: float = 60.0) -> list:
        tools, cursor = [], None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout) or {}
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        return tools

    def tool_names(self) -> list:
        if self.tools is None:
            self.list_tools()
        return [t.get("name") for t in self.tools or []]

    def call_tool(self, name: str, args: Optional[dict] = None, *, timeout: float = 120.0) -> dict:
        res = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout)
        return parse_tool_result(res)


def parse_tool_result(res) -> dict:
    """tools/call result -> {text, images:[(mime, bytes)], is_error, raw}."""
    res = res or {}
    texts, images = [], []
    for item in res.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            texts.append(item.get("text") or "")
        elif kind == "image":
            try:
                images.append((item.get("mimeType") or "application/octet-stream",
                               base64.b64decode(item.get("data") or "")))
            except (ValueError, TypeError):
                texts.append("[undecodable image item]")
        elif kind == "resource":
            r = item.get("resource") or {}
            if "text" in r:
                texts.append(r.get("text") or "")
    return {"text": "\n".join(texts), "images": images, "is_error": bool(res.get("isError")),
            "raw": res}


# ---- the two known servers -------------------------------------------------------
def _override(var: str) -> Optional[list]:
    v = os.environ.get(var)
    if not v:
        return None
    try:
        argv = json.loads(v)
    except ValueError:
        raise McpError(f"{var} must be a JSON argv list")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise McpError(f"{var} must be a JSON argv list")
    return argv


def official_command() -> list:
    return _override("FOUNDRY_MCP_OFFICIAL") or [
        "cmd.exe", "/d", "/c",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "mcp.bat")]


def community_command(package: str = COMMUNITY_PACKAGE) -> list:
    return _override("FOUNDRY_MCP_COMMUNITY") or ["cmd.exe", "/d", "/c", NPX, "-y", package]


SERVERS = {"official": official_command, "community": community_command}


def connect(kind: str, **kw) -> McpClient:
    if kind not in SERVERS:
        raise McpError(f"unknown MCP server {kind!r} (known: {', '.join(SERVERS)})")
    return McpClient(SERVERS[kind](), name=kind, **kw)


class OfficialStudio:
    """The official server bound to one Studio: resolves studio_id once and adds it to
    every call. place: substring of the Studio name to pick (case-insensitive)."""

    def __init__(self, client: McpClient, place: Optional[str] = None):
        self.client, self.place = client, place
        self.studio_id: Optional[str] = None
        self.studio_name: Optional[str] = None

    def list_studios(self, timeout: float = 30.0) -> list:
        r = self.client.call_tool("list_roblox_studios", {}, timeout=timeout)
        if r["is_error"]:
            raise McpError("list_roblox_studios: " + r["text"][:300])
        try:
            data = json.loads(r["text"])
        except ValueError:
            raise McpError("list_roblox_studios: not JSON: " + r["text"][:300])
        return list((data or {}).get("studios") or [])

    def resolve(self, timeout: float = 30.0) -> str:
        studios = self.list_studios(timeout)
        pick = studios
        if self.place:
            pick = [s for s in studios if self.place.lower() in str(s.get("name") or "").lower()]
        if len(pick) != 1:
            names = ", ".join(str(s.get("name")) for s in studios) or "none"
            want = f" matching {self.place!r}" if self.place else ""
            raise McpError(f"need exactly one Studio{want}, found {len(pick)} (open: {names})")
        self.studio_id = str(pick[0].get("id"))
        self.studio_name = str(pick[0].get("name"))
        return self.studio_id

    def call(self, name: str, args: Optional[dict] = None, *, timeout: float = 120.0) -> dict:
        if self.studio_id is None:
            self.resolve()
        a = dict(args or {})
        a.setdefault("studio_id", self.studio_id)
        return self.client.call_tool(name, a, timeout=timeout)

    def execute_luau(self, code: str, datamodel: str = "Edit", *, timeout: float = 120.0) -> dict:
        return self.call("execute_luau", {"code": code, "datamodel_type": datamodel}, timeout=timeout)


class CommunityStudio:
    """The community server bound to one Studio instance. robloxstudio-mcp routes by
    `instance_id` (get_connected_instances -> {"instances":[{id, placeName, placeId}]},
    3.2.1 dist/index.js). place: substring of placeName (case-insensitive). With exactly
    one instance connected and no place, calls go out unchanged (the verified live
    path); with several, an exact pick is required and instance_id is added to every
    call whose tool schema accepts it - anything ambiguous raises McpError."""

    def __init__(self, client: McpClient, place: Optional[str] = None):
        self.client, self.place = client, place
        self.instance_id: Optional[str] = None
        self.place_name: Optional[str] = None
        self._resolved = False
        self._accepts: dict = {}

    def instances(self, timeout: float = 30.0) -> list:
        r = self.client.call_tool("get_connected_instances", {}, timeout=timeout)
        if r["is_error"]:
            raise McpError("get_connected_instances: " + r["text"][:300])
        try:
            data = json.loads(r["text"])
        except ValueError:
            raise McpError("get_connected_instances: not JSON: " + r["text"][:300])
        return [i for i in (data or {}).get("instances") or [] if isinstance(i, dict)]

    def resolve(self, timeout: float = 30.0) -> Optional[str]:
        names = self.client.tool_names()
        if "get_connected_instances" not in names:
            if self.place:
                raise McpError("community server cannot bind --place (no get_connected_instances)")
            self._resolved = True
            return None
        inst = self.instances(timeout)
        # a freshly started server has no instance yet: the Studio plugin polls and
        # connects a few seconds later (live 2026-10-20: 0 at start, connected at 8 s;
        # every doctor/preflight checked at 0 s and failed after a Studio restart)
        deadline = time.monotonic() + CONNECT_WAIT_S
        while not inst and time.monotonic() < deadline:
            time.sleep(2.0)
            inst = self.instances(timeout)
        pick = inst
        if self.place:
            pick = [i for i in inst if self.place.lower() in str(i.get("placeName") or "").lower()]
        if len(pick) != 1:
            open_ = ", ".join(f"{i.get('placeName')}({i.get('id')})" for i in inst) or "none"
            want = f" matching {self.place!r}" if self.place else ""
            raise McpError(f"community: need exactly one Studio instance{want}, found {len(pick)} "
                           f"(connected: {open_})")
        self.place_name = str(pick[0].get("placeName") or "")
        # bind explicitly only when it matters (several connected); one instance keeps
        # the unscoped calls that were verified live on 2026-10-20
        self.instance_id = str(pick[0].get("id")) if len(inst) > 1 else None
        self._resolved = True
        return self.instance_id

    def _takes_instance(self, name: str) -> bool:
        if name not in self._accepts:
            props = {}
            for t in self.client.tools or []:
                if t.get("name") == name:
                    props = ((t.get("inputSchema") or {}).get("properties") or {})
            self._accepts[name] = "instance_id" in props
        return self._accepts[name]

    def call_tool(self, name: str, args: Optional[dict] = None, *, timeout: float = 120.0) -> dict:
        if not self._resolved:
            self.resolve()
        a = dict(args or {})
        if self.instance_id is not None:
            if not self._takes_instance(name):
                raise McpError(f"community tool {name} cannot be bound to instance {self.instance_id}")
            a.setdefault("instance_id", self.instance_id)
        return self.client.call_tool(name, a, timeout=timeout)

    def tool_names(self) -> list:
        return self.client.tool_names()

    def close(self) -> None:
        self.client.close()


def wait_until(pred, timeout: float, step: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())
