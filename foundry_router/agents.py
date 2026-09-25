"""External agents — Hermes Agent (Nous Research) behind Foundry.

An agent is not a model: it runs its own multi-step loop with its own tools,
skills and memory. Foundry uses one through Hermes' OpenAI-compatible API
server (`API_SERVER_ENABLED=true`, default port 8642) in two ways, sharing
this one layer:

  * AS A TOOL — `<agent>_run` / `<agent>_status` / `<agent>_stop` are offered
    under the pseudo MCP server "agent-<agent>". Because they look like any
    other MCP server's tools, everything MCP already does applies unchanged:
    per-persona grants, the Foundry-MCP aggregator (Cline / AnythingLLM can
    load them), direct-mode injection, the worker / brain tool loops, and one
    mcp_call_log row per call. `<agent>_run` uses Hermes' Runs API
    (POST /v1/runs + the /events SSE stream), relays the agent's tool activity
    as MCP progress, and hands back a run_id to poll if the task outlasts
    `tool_wait_seconds`.

  * AS A BACKEND — a persona whose `agent_backend` names an agent forwards the
    conversation to POST /v1/chat/completions and streams the answer back; the
    agent's `hermes.tool.progress` frames become thinking lines, and each
    client conversation maps to one Hermes session (X-Hermes-Session-Id / -Key)
    so the agent keeps its memory of the chat.

Every task, either way, is one agent_runs row (the Agents metrics).
Loop protection lives in request_context: requests carrying an agent's
caller_token are never routed back into an agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, AsyncIterator, Optional

import httpx

from .config import AgentConfig
from .db import Database, utcnow

log = logging.getLogger(__name__)

SERVER_PREFIX = "agent-"
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class AgentError(RuntimeError):
    pass


def server_name(agent: str) -> str:
    return SERVER_PREFIX + agent


def conversation_key(messages: list[dict], client_session: str = "") -> str:
    """Stable id for one client conversation: the client's own session header
    when it sends one, else a hash of the system prompt + first user turn
    (constant across the turns of one chat, different between chats)."""
    if client_session:
        return "c-" + hashlib.sha256(client_session.encode()).hexdigest()[:24]
    sys_txt = next((str(m.get("content") or "") for m in messages
                    if m.get("role") == "system"), "")
    first = next((str(m.get("content") or "") for m in messages
                  if m.get("role") == "user"), "")
    return "f-" + hashlib.sha256((sys_txt + "\x00" + first).encode()).hexdigest()[:24]


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """(event, data) pairs from an SSE response; event is "" for unnamed frames."""
    event, data = "", []
    async for line in resp.aiter_lines():
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


def _json(s: str) -> Any:
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def progress_line(data: Any) -> str:
    """One readable line for a Hermes tool-progress / tool-lifecycle frame.
    The payload shape varies by version, so read it tolerantly."""
    if not isinstance(data, dict):
        return str(data or "").strip()[:300]
    tool = data.get("tool") or data.get("name") or data.get("tool_name") or ""
    label = data.get("label") or data.get("message") or data.get("status") or ""
    preview = data.get("preview") or data.get("args") or data.get("text") or ""
    if isinstance(preview, (dict, list)):
        preview = json.dumps(preview, ensure_ascii=False)
    emoji = data.get("emoji") or "🔧"
    parts = [p for p in (str(tool), str(label)) if p]
    head = " ".join(parts) or "working"
    return (f"{emoji} {head}" + (f": {str(preview)[:200]}" if preview else "")).strip()


class _Run:
    """Book-keeping for one in-flight task (for the agent_runs row)."""
    def __init__(self, agent: str, mode: str, caller: str, input_chars: int):
        self.agent, self.mode, self.caller = agent, mode, caller
        self.t0 = time.monotonic()
        self.ttft_ms: Optional[int] = None
        self.tools: list[str] = []
        self.run_id = ""
        self.session_id = ""
        self.usage: dict = {}
        self.model = ""
        self.input_chars = input_chars
        self.output_chars = 0
        self.row_id: Optional[int] = None

    def first_output(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = int((time.monotonic() - self.t0) * 1000)


class AgentManager:
    def __init__(self, agents: list[AgentConfig], http: httpx.AsyncClient, db: Database):
        self.http = http
        self.db = db
        self.agents: dict[str, AgentConfig] = {}
        self._health: dict[str, dict] = {}
        self._runs: dict[str, _Run] = {}          # run_id -> book-keeping (tool mode)
        self._watchers: set = set()
        self.set_agents(agents)

    # -- config ---------------------------------------------------------------------

    def set_agents(self, agents: list[AgentConfig]) -> None:
        from . import request_context
        self.agents = {a.name: a for a in (agents or [])}
        request_context.set_agent_tokens(
            {a.caller_token: a.name for a in self.agents.values() if a.caller_token})

    def get(self, name: str) -> Optional[AgentConfig]:
        a = self.agents.get(name or "")
        return a if a is not None and a.enabled else None

    def names(self) -> list[str]:
        return [n for n, a in self.agents.items() if a.enabled]

    @staticmethod
    def is_agent_server(server: str) -> bool:
        return (server or "").startswith(SERVER_PREFIX)

    def agent_for_server(self, server: str) -> Optional[AgentConfig]:
        if not self.is_agent_server(server):
            return None
        return self.get(server[len(SERVER_PREFIX):])

    def _headers(self, a: AgentConfig, extra: Optional[dict] = None) -> dict:
        h = {"x-foundry-source": "foundry-router"}
        if a.api_key:
            h["Authorization"] = f"Bearer {a.api_key}"
        from . import request_context
        rid = request_context.request_id()
        if rid:
            h["x-request-id"] = rid
        h.update(extra or {})
        return h

    def _url(self, a: AgentConfig, path: str) -> str:
        return a.url.rstrip("/") + path

    # -- discovery / health ----------------------------------------------------------

    async def probe(self, name: str) -> dict:
        """Health + what the agent can do: /health, /v1/models,
        /v1/capabilities, /v1/skills, /v1/toolsets (each best-effort)."""
        a = self.agents.get(name)
        if a is None:
            return {"name": name, "healthy": False, "error": "unknown agent"}
        out: dict = {"name": name, "kind": a.kind, "url": a.url, "enabled": a.enabled,
                     "expose_as_tool": a.expose_as_tool, "healthy": False,
                     "checked_at": utcnow()}
        t0 = time.monotonic()
        try:
            r = await self.http.get(self._url(a, "/health"), headers=self._headers(a),
                                    timeout=5.0)
            out["latency_ms"] = int((time.monotonic() - t0) * 1000)
            out["healthy"] = r.status_code < 400
            if r.status_code >= 400:
                out["error"] = f"HTTP {r.status_code}"
            body = _json(r.text)
            if isinstance(body, dict):
                out["health"] = body
        except Exception as e:                                   # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"[:300]
            self._health[name] = out
            return out
        for key, path in (("models", "/v1/models"), ("capabilities", "/v1/capabilities"),
                          ("skills", "/v1/skills"), ("toolsets", "/v1/toolsets")):
            try:
                r = await self.http.get(self._url(a, path), headers=self._headers(a),
                                        timeout=5.0)
                if r.status_code < 400:
                    body = _json(r.text)
                    if isinstance(body, dict) and isinstance(body.get("data"), list):
                        body = body["data"]
                    out[key] = body
            except Exception:                                    # noqa: BLE001
                pass
        models = out.get("models")
        if isinstance(models, list) and models:
            out["model"] = (models[0] or {}).get("id") if isinstance(models[0], dict) else None
        self._health[name] = out
        return out

    async def probe_all(self) -> list[dict]:
        return [await self.probe(n) for n in self.agents]

    def health(self, name: str) -> dict:
        return dict(self._health.get(name) or {})

    def healthy(self, name: str) -> Optional[bool]:
        return (self._health.get(name) or {}).get("healthy")

    def model_for(self, a: AgentConfig) -> str:
        return a.model or (self._health.get(a.name) or {}).get("model") or "hermes-agent"

    # -- tool manifests ----------------------------------------------------------------

    def tool_manifests(self) -> dict[str, list[dict]]:
        """{"agent-<name>": [tool dicts shaped like MCPManager.list_tools]} for
        every enabled agent that is exposed as a tool."""
        out: dict[str, list[dict]] = {}
        for a in self.agents.values():
            if not (a.enabled and a.expose_as_tool):
                continue
            n = a.name
            wait = a.tool_wait_seconds
            out[server_name(n)] = [
                {"name": f"{n}_run",
                 "description": (
                     f"Delegate a multi-step task to the {n} agent ({a.kind}). It plans and "
                     f"runs its OWN tools (terminal, files, web, browser, skills, memory) "
                     f"until done and returns the final answer plus the tools it used. "
                     f"Use for research, repo investigation, running/debugging commands, "
                     f"or anything needing many steps; don't use it for a quick answer you "
                     f"can give yourself. Waits up to {wait}s; a longer task returns a "
                     f"run_id to check with {n}_status."),
                 "input_schema": {
                     "type": "object",
                     "properties": {
                         "task": {"type": "string",
                                  "description": "The complete task, with every detail the "
                                                 "agent needs (it can't see this chat)."},
                         "context": {"type": "string",
                                     "description": "Optional extra instructions / context "
                                                    "(paths, constraints, output format)."},
                         "session_id": {"type": "string",
                                        "description": "Optional: continue an earlier agent "
                                                       "session (its memory of that work)."},
                     },
                     "required": ["task"]},
                 "read_only": False, "destructive": None,
                 "annotations": {"title": f"Run {n} agent task", "openWorldHint": True}},
                {"name": f"{n}_status",
                 "description": (f"Check a {n} agent task started by {n}_run: status and, "
                                 f"once finished, its answer. Waits up to wait_seconds "
                                 f"(default 60) for it to finish, so call it sparingly."),
                 "input_schema": {
                     "type": "object",
                     "properties": {"run_id": {"type": "string"},
                                    "wait_seconds": {"type": "integer", "minimum": 0,
                                                     "maximum": 600}},
                     "required": ["run_id"]},
                 "read_only": True, "destructive": False,
                 "annotations": {"title": f"{n} task status", "readOnlyHint": True}},
                {"name": f"{n}_stop",
                 "description": f"Stop a running {n} agent task (by run_id).",
                 "input_schema": {"type": "object",
                                  "properties": {"run_id": {"type": "string"}},
                                  "required": ["run_id"]},
                 "read_only": False, "destructive": True,
                 "annotations": {"title": f"Stop {n} task", "destructiveHint": True}},
            ]
        return out

    def timeout_for(self, server: str) -> int:
        a = self.agent_for_server(server)
        return (a.timeout_seconds if a else 1800) + 30

    async def call_tool(self, server: str, tool: str, arguments: dict,
                        progress_callback=None):
        """Execute an agent tool; returns a ToolResult (same type MCP calls
        return) so callers can't tell the difference."""
        from . import request_context
        from .tools.mcp_client import ToolResult
        a = self.agent_for_server(server)
        if a is None:
            raise AgentError(f"agent for {server!r} is not configured or disabled")
        caller = request_context.agent_caller()
        if caller:
            raise AgentError(f"refused: this request came from agent {caller!r} — agents "
                             f"can't call agents through Foundry (loop protection)")
        suffix = tool[len(a.name) + 1:] if tool.startswith(a.name + "_") else tool
        args = arguments or {}
        if suffix == "run":
            text = await self._tool_run(a, args, progress_callback)
        elif suffix == "status":
            text = await self._tool_status(a, str(args.get("run_id") or ""),
                                           int(args.get("wait_seconds") or 60))
        elif suffix == "stop":
            text = await self._tool_stop(a, str(args.get("run_id") or ""))
        else:
            raise AgentError(f"unknown agent tool {tool!r}")
        return ToolResult([{"type": "text", "text": text}])

    # -- Runs API (tool mode) -------------------------------------------------------------

    async def _tool_run(self, a: AgentConfig, args: dict, progress_callback) -> str:
        from . import request_context
        task = str(args.get("task") or args.get("input") or "").strip()
        if not task:
            raise AgentError("task is required")
        attr = request_context.mcp_attribution()
        caller = ":".join(x for x in (attr.get("source"), attr.get("caller")) if x)
        run = _Run(a.name, "tool", caller, len(task) + len(str(args.get("context") or "")))
        body: dict = {"input": task}
        if args.get("context"):
            body["instructions"] = str(args["context"])
        if args.get("session_id"):
            body["session_id"] = str(args["session_id"])
        if a.model:
            body["model"] = a.model
        try:
            r = await self.http.post(self._url(a, "/v1/runs"), json=body,
                                     headers=self._headers(a), timeout=30.0)
        except Exception as e:                                   # noqa: BLE001
            self._record(run, "failed", error=f"{type(e).__name__}: {e}")
            raise AgentError(f"{a.name} unreachable: {e}") from e
        if r.status_code == 404:
            # Older Hermes without the Runs API: one blocking chat call.
            return await self._tool_run_via_chat(a, task, args, run)
        if r.status_code == 429:
            self._record(run, "failed", error="429: agent at max concurrent runs")
            raise AgentError(f"{a.name} is at its concurrent-run limit (HTTP 429) — "
                             f"try again shortly")
        if r.status_code >= 400:
            self._record(run, "failed", error=f"HTTP {r.status_code}: {r.text[:200]}")
            raise AgentError(f"{a.name} refused the task: HTTP {r.status_code} {r.text[:300]}")
        rid = str((_json(r.text) or {}).get("run_id") or "")
        if not rid:
            self._record(run, "failed", error="no run_id in response")
            raise AgentError(f"{a.name} returned no run_id: {r.text[:200]}")
        run.run_id = rid
        run.session_id = str(body.get("session_id") or "")
        self._runs[rid] = run
        run.row_id = self._record(run, "running")
        wait = max(5, a.tool_wait_seconds)
        try:
            out = await asyncio.wait_for(self._follow_events(a, run, progress_callback),
                                         timeout=wait)
        except asyncio.TimeoutError:
            self._record(run, "handed_off")
            # Keep the metrics row honest even if nobody calls <agent>_status.
            self._watchers.add(asyncio.ensure_future(self._watch(a, rid)))
            return (f"The {a.name} agent is still working (run_id={rid}, "
                    f"{int(time.monotonic() - run.t0)}s so far"
                    + (f", tools used: {', '.join(run.tools[-8:])}" if run.tools else "")
                    + f"). Check back with {a.name}_status(run_id=\"{rid}\").")
        except asyncio.CancelledError:
            # the caller gave up (client disconnected) — stop the agent too
            await self._stop(a, rid)
            self._record(run, "cancelled", error="caller cancelled")
            raise
        return out

    async def _follow_events(self, a: AgentConfig, run: _Run, progress_callback) -> str:
        """Consume /v1/runs/{id}/events until a terminal event; relay tool
        activity as progress; return the model-ready result text."""
        text: list[str] = []
        final: Optional[str] = None
        status, error = "completed", ""
        n = 0

        async def progress(msg: str) -> None:
            nonlocal n
            n += 1
            if progress_callback is not None:
                try:
                    await progress_callback(float(n), None, msg[:300])
                except Exception:                                # noqa: BLE001
                    pass

        url = self._url(a, f"/v1/runs/{run.run_id}/events")
        async with self.http.stream("GET", url, headers=self._headers(
                a, {"Accept": "text/event-stream"}),
                timeout=httpx.Timeout(a.timeout_seconds, connect=10.0)) as resp:
            if resp.status_code >= 400:
                raise AgentError(f"{a.name} events: HTTP {resp.status_code}")
            async for event, raw in iter_sse(resp):
                data = _json(raw)
                if event == "assistant.delta":
                    t = (data or {}).get("text") if isinstance(data, dict) else raw
                    if t:
                        run.first_output()
                        text.append(str(t))
                elif event == "tool.started":
                    tool = (data or {}).get("tool") or "tool"
                    run.tools.append(str(tool))
                    await progress(progress_line(data))
                elif event == "tool.completed":
                    if isinstance(data, dict) and data.get("error"):
                        await progress(f"⚠ {data.get('tool')} failed: "
                                       f"{str(data.get('preview') or '')[:150]}")
                elif event in ("subagent.start", "subagent.complete"):
                    await progress(("↳ subagent started: " if event.endswith("start")
                                    else "↳ subagent done: ") + progress_line(data))
                elif event in ("assistant.commentary", "message.interim"):
                    if isinstance(data, dict) and data.get("text"):
                        await progress("💬 " + str(data["text"])[:200])
                elif event == "run.completed":
                    final = (data or {}).get("output") if isinstance(data, dict) else None
                    status = str((data or {}).get("status") or "completed")
                    break
                elif event in ("run.failed", "run.cancelled"):
                    status = "failed" if event == "run.failed" else "cancelled"
                    error = str((data or {}).get("error") or status) if isinstance(data, dict) \
                        else status
                    break
        # Pick up usage / model / session from the run object.
        info = await self._get_run(a, run.run_id)
        if info:
            self._absorb(run, info)
            if final is None:
                final = info.get("output")
        output = final if final is not None else "".join(text)
        run.output_chars = len(output or "")
        self._record(run, status, error=error)
        self._runs.pop(run.run_id, None)
        if status != "completed":
            raise AgentError(f"{a.name} task {status}: {error or 'no detail'}"
                             + (f" (after tools: {', '.join(run.tools[-8:])})" if run.tools else ""))
        return self._result_text(a, run, output or "")

    def _result_text(self, a: AgentConfig, run: _Run, output: str) -> str:
        meta = [f"{a.name} agent finished in {int(time.monotonic() - run.t0)}s"]
        if run.tools:
            uniq = list(dict.fromkeys(run.tools))
            meta.append(f"{len(run.tools)} tool call(s): {', '.join(uniq[:12])}")
        if run.session_id:
            meta.append(f"session_id={run.session_id}")
        return output.strip() + "\n\n[" + " · ".join(meta) + "]"

    async def _tool_run_via_chat(self, a: AgentConfig, task: str, args: dict,
                                 run: _Run) -> str:
        msgs = []
        if args.get("context"):
            msgs.append({"role": "system", "content": str(args["context"])})
        msgs.append({"role": "user", "content": task})
        out: list[str] = []
        try:
            async for ev in self.chat_stream(a.name, msgs, _run=run):
                if ev.get("content"):
                    out.append(ev["content"])
        except AgentError as e:
            self._record(run, "failed", error=str(e))
            raise
        run.output_chars = len("".join(out))
        self._record(run, "completed")
        return self._result_text(a, run, "".join(out))

    async def _get_run(self, a: AgentConfig, rid: str) -> Optional[dict]:
        try:
            r = await self.http.get(self._url(a, f"/v1/runs/{rid}"),
                                    headers=self._headers(a), timeout=10.0)
            if r.status_code < 400:
                body = _json(r.text)
                return body if isinstance(body, dict) else None
        except Exception:                                        # noqa: BLE001
            pass
        return None

    def _absorb(self, run: _Run, info: dict) -> None:
        u = info.get("usage") or {}
        run.usage = {"prompt_tokens": u.get("input_tokens", u.get("prompt_tokens")),
                     "completion_tokens": u.get("output_tokens", u.get("completion_tokens")),
                     "cache_read_tokens": u.get("cache_read_tokens")}
        rt = info.get("runtime") or {}
        run.model = str(rt.get("model") or info.get("model") or run.model or "")
        run.session_id = str(info.get("session_id") or run.session_id or "")

    async def _watch(self, a: AgentConfig, rid: str, interval: float = 15.0) -> None:
        """Background: poll a handed-off run until it ends (or its budget
        runs out) and finalize its agent_runs row."""
        try:
            deadline = time.monotonic() + a.timeout_seconds
            while rid in self._runs and time.monotonic() < deadline:
                await asyncio.sleep(interval)
                info = await self._get_run(a, rid)
                run = self._runs.get(rid)
                if run is None:
                    return                      # finalized by <agent>_status
                if info and info.get("status") in _TERMINAL:
                    st = str(info["status"])
                    self._absorb(run, info)
                    run.output_chars = len(str(info.get("output") or ""))
                    self._runs.pop(rid, None)
                    self._record(run, st, error=str(info.get("error") or "")
                                 if st != "completed" else "")
                    return
            run = self._runs.pop(rid, None)
            if run is not None:
                self._record(run, "timeout", error=f"no result within {a.timeout_seconds}s")
        except asyncio.CancelledError:
            pass
        finally:
            self._watchers = {t for t in self._watchers if not t.done()}

    async def _tool_status(self, a: AgentConfig, rid: str, wait_seconds: int) -> str:
        if not rid:
            raise AgentError("run_id is required")
        deadline = time.monotonic() + max(0, min(wait_seconds, 600))
        info = await self._get_run(a, rid)
        while info and info.get("status") not in _TERMINAL and time.monotonic() < deadline:
            await asyncio.sleep(min(5.0, max(0.5, deadline - time.monotonic())))
            info = await self._get_run(a, rid)
        if not info:
            return f"No {a.name} task with run_id={rid} (finished long ago, or unknown)."
        st = str(info.get("status") or "unknown")
        run = self._runs.get(rid)
        if st in _TERMINAL:
            if run is not None:
                self._runs.pop(rid, None)
                self._absorb(run, info)
                run.output_chars = len(str(info.get("output") or ""))
                self._record(run, "completed" if st == "completed" else st,
                             error=str(info.get("error") or "") if st != "completed" else "")
            if st == "completed":
                out = str(info.get("output") or "")
                return (self._result_text(a, run, out) if run is not None
                        else out + f"\n\n[{a.name} task {rid} completed]")
            return f"{a.name} task {rid} ended: {st}. {info.get('error') or ''}".strip()
        extra = f", tools so far: {', '.join(run.tools[-8:])}" if run and run.tools else ""
        return (f"{a.name} task {rid} is still {st}{extra}. It is working — don't poll "
                f"rapidly; check again in a minute or two.")

    async def _stop(self, a: AgentConfig, rid: str) -> bool:
        try:
            r = await self.http.post(self._url(a, f"/v1/runs/{rid}/stop"),
                                     headers=self._headers(a), timeout=10.0)
            return r.status_code < 400
        except Exception:                                        # noqa: BLE001
            return False

    async def _tool_stop(self, a: AgentConfig, rid: str) -> str:
        if not rid:
            raise AgentError("run_id is required")
        ok = await self._stop(a, rid)
        run = self._runs.pop(rid, None)
        if run is not None:
            self._record(run, "cancelled", error="stopped via tool")
        return (f"Stop requested for {a.name} task {rid}." if ok
                else f"Could not stop {a.name} task {rid} (already finished or unknown).")

    # -- chat (backend mode) --------------------------------------------------------------

    async def chat_stream(self, name: str, messages: list[dict], *, session_key: str = "",
                          caller: str = "", _run: Optional[_Run] = None
                          ) -> AsyncIterator[dict]:
        """Stream one agent turn over /v1/chat/completions. Yields
        {"content"} / {"thinking", "progress": True, "tool"} events and a final
        {"done": True, usage...} event. Raises AgentError on failure."""
        a = self.get(name)
        if a is None:
            raise AgentError(f"agent {name!r} is not configured or disabled")
        run = _run or _Run(a.name, "backend", caller,
                           sum(len(str(m.get("content") or "")) for m in messages))
        extra = {"Accept": "text/event-stream"}
        if a.session_continuity and session_key:
            extra["X-Hermes-Session-Id"] = session_key
            extra["X-Hermes-Session-Key"] = session_key
            run.session_id = session_key
        body = {"model": self.model_for(a), "stream": True,
                "messages": [self._plain_message(m) for m in messages],
                "stream_options": {"include_usage": True}}
        out_chars = 0
        usage: dict = {}
        finish = "stop"
        status, error = "completed", ""
        try:
            async with self.http.stream(
                    "POST", self._url(a, "/v1/chat/completions"), json=body,
                    headers=self._headers(a, extra),
                    timeout=httpx.Timeout(a.timeout_seconds, connect=10.0)) as resp:
                if resp.status_code >= 400:
                    txt = (await resp.aread()).decode(errors="replace")[:300]
                    raise AgentError(f"{a.name}: HTTP {resp.status_code} {txt}")
                sid = resp.headers.get("x-hermes-session-id")
                if sid:
                    run.session_id = sid
                async for event, raw in iter_sse(resp):
                    if raw.strip() == "[DONE]":
                        break
                    data = _json(raw)
                    if event == "hermes.tool.progress" or (
                            isinstance(data, dict) and data.get("object") == "hermes.tool.progress"):
                        d0 = data if isinstance(data, dict) else {}
                        tool = d0.get("tool") or d0.get("name") or "tool"
                        run.tools.append(str(tool))
                        run.first_output()
                        yield {"thinking": progress_line(data) + "\n", "progress": True,
                               "tool": str(tool)}
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("error"):
                        err = data["error"]
                        raise AgentError(f"{a.name}: " + (err.get("message") if isinstance(
                            err, dict) else str(err)))
                    if data.get("usage"):
                        usage = data["usage"]
                    if data.get("model") and not run.model:
                        run.model = str(data["model"])
                    for ch in data.get("choices") or []:
                        d = ch.get("delta") or ch.get("message") or {}
                        th = d.get("reasoning_content") or d.get("reasoning") or ""
                        c = d.get("content") or ""
                        if th:
                            run.first_output()
                            yield {"thinking": th}
                        if c:
                            run.first_output()
                            out_chars += len(c)
                            yield {"content": c}
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
        except AgentError as e:
            status, error = "failed", str(e)
            raise
        except (asyncio.CancelledError, GeneratorExit):
            status, error = "cancelled", "client disconnected"
            raise
        except httpx.TimeoutException as e:
            status, error = "timeout", f"no answer within {a.timeout_seconds}s"
            raise AgentError(f"{a.name}: {error}") from e
        except httpx.HTTPError as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            raise AgentError(f"{a.name} unreachable: {e}") from e
        finally:
            run.usage = {"prompt_tokens": usage.get("prompt_tokens"),
                         "completion_tokens": usage.get("completion_tokens"),
                         "cache_read_tokens": usage.get("cache_read_tokens")}
            run.output_chars = out_chars
            if _run is None:
                self._record(run, status, error=error)
        yield {"done": True, "finish_reason": finish,
               "prompt_tokens": usage.get("prompt_tokens") or 0,
               "completion_tokens": usage.get("completion_tokens") or 0,
               "cached_tokens": usage.get("cache_read_tokens") or 0,
               "tools": list(run.tools), "session_id": run.session_id,
               "agent_model": run.model, "ttft_ms": run.ttft_ms,
               "duration_ms": int((time.monotonic() - run.t0) * 1000)}

    @staticmethod
    def _plain_message(m: dict) -> dict:
        """OpenAI-shaped message; Foundry-internal fields dropped, tool turns
        from a client's earlier tool loop flattened to text (the agent runs its
        own tools, it never sees the client's)."""
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(str(p.get("text") or "") for p in content
                                if isinstance(p, dict) and p.get("type") == "text")
        content = str(content or "")
        if role == "tool":
            return {"role": "user",
                    "content": f"[tool result {m.get('tool_name') or ''}]\n{content}"}
        if role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(str((tc.get("function") or tc).get("name") or "")
                              for tc in m["tool_calls"])
            content = (content + f"\n[called tools: {calls}]").strip()
        if role not in ("system", "user", "assistant"):
            role = "user"
        return {"role": role, "content": content}

    # -- metrics ----------------------------------------------------------------------------

    def _record(self, run: _Run, status: str, error: str = "") -> Optional[int]:
        """Insert (first call) or update the agent_runs row for this task."""
        u = run.usage or {}
        vals = (status, int((time.monotonic() - run.t0) * 1000), run.ttft_ms,
                len(run.tools), ",".join(list(dict.fromkeys(run.tools))[:40])[:1000],
                u.get("prompt_tokens"), u.get("completion_tokens"), u.get("cache_read_tokens"),
                run.model[:120], run.output_chars, (error or "")[:500],
                run.run_id, run.session_id[:120])
        try:
            if run.row_id is None:
                cur = self.db.execute(
                    "INSERT INTO agent_runs (status, duration_ms, ttft_ms, tool_calls, "
                    "tools_used, prompt_tokens, completion_tokens, cache_read_tokens, model, "
                    "output_chars, error, run_id, session_id, ts, agent, mode, caller, "
                    "input_chars) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*vals, utcnow(), run.agent, run.mode, run.caller[:120], run.input_chars))
                run.row_id = int(cur) if cur else None
            else:
                self.db.execute(
                    "UPDATE agent_runs SET status=?, duration_ms=?, ttft_ms=?, tool_calls=?, "
                    "tools_used=?, prompt_tokens=?, completion_tokens=?, cache_read_tokens=?, "
                    "model=?, output_chars=?, error=?, run_id=?, session_id=? WHERE id=?",
                    (*vals, run.row_id))
        except Exception:                                        # noqa: BLE001
            log.debug("agent_runs write failed", exc_info=True)
        return run.row_id

    async def close(self) -> None:
        for t in list(self._watchers):
            t.cancel()

    def active(self) -> list[dict]:
        now = time.monotonic()
        return [{"agent": r.agent, "run_id": rid, "caller": r.caller,
                 "seconds": round(now - r.t0, 1), "tools": r.tools[-5:]}
                for rid, r in self._runs.items()]

    def summary(self, hours: float = 24) -> dict:
        rows = self.db.query(
            "SELECT * FROM agent_runs WHERE datetime(ts) >= datetime('now', ?) ORDER BY id",
            (f"-{hours} hours",))
        by: dict = {}
        for r in rows:
            by.setdefault((r["agent"], r["mode"]), []).append(r)
        groups = []
        for (agent, mode), rs in by.items():
            durs = sorted(r["duration_ms"] or 0 for r in rs
                          if r["status"] not in ("running", "handed_off"))
            done = [r for r in rs if r["status"] == "completed"]
            tools: dict = {}
            for r in rs:
                for t in (r.get("tools_used") or "").split(","):
                    if t:
                        tools[t] = tools.get(t, 0) + 1
            groups.append({
                "agent": agent, "mode": mode, "runs": len(rs), "completed": len(done),
                "failed": sum(1 for r in rs if r["status"] in ("failed", "timeout")),
                "cancelled": sum(1 for r in rs if r["status"] == "cancelled"),
                "running": sum(1 for r in rs if r["status"] in ("running", "handed_off")),
                "p50_ms": durs[len(durs) // 2] if durs else None,
                "p95_ms": durs[min(len(durs) - 1, int(0.95 * (len(durs) - 1) + 0.5))] if durs else None,
                "avg_tool_calls": round(sum(r["tool_calls"] or 0 for r in rs) / len(rs), 1),
                "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rs),
                "completion_tokens": sum(r["completion_tokens"] or 0 for r in rs),
                "top_tools": sorted(tools.items(), key=lambda kv: -kv[1])[:10],
            })
        return {"hours": hours, "groups": groups, "active": self.active()}

    def recent(self, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?",
                             (max(1, min(int(limit), 500)),))

    def clear(self) -> int:
        n = (self.db.query_one("SELECT COUNT(*) AS n FROM agent_runs") or {}).get("n") or 0
        self.db.execute("DELETE FROM agent_runs")
        return int(n)
