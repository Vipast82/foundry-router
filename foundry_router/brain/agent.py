"""The live Routing Agent (design doc §4.2): a LangGraph loop where every
action is a tool the brain chooses at runtime — never a fixed
plan->code->review pipeline.

LangGraph provides the loop skeleton (StateGraph: brain -> tools -> brain);
model I/O goes through this project's own protocol adapters rather than
langchain provider packages — one shared HTTP layer for brain, pool, and
research keeps dependencies minimal (§2) and the wire behavior identical
everywhere.

Streaming: nodes emit AgentEvents into a queue that `run()` drains as an async
generator — the facade turns "think" events into <think> blocks (§4.5) live,
while the graph is still executing.

The graph is built per request (cheap: a handful of node closures) because the
tool set, system prompt, and guardrail state are all request-scoped. This is
also what makes Tool Sync race-free from the agent's perspective: the specs
snapshot taken here is immutable for the request's lifetime.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from ..guardrails import GuardrailEngine, RequestGuardState
from ..pool.base import AllBackendsFailed
from ..usage import MeridianUsage, RequestLogger, estimate_cost_usd
from . import prompts
from .client import BrainClient, BrainUnreachable

log = logging.getLogger(__name__)

# Worker-model output budget. Generous because implementation tasks produce
# long code; distinct from the brain's own max_tokens (routing is terse).
WORKER_MAX_TOKENS = 8192
TOOL_RESULT_LIMIT = 24000     # chars of a tool result fed back to a small brain
MCP_RESULT_LIMIT = 12000


@dataclass
class AgentEvent:
    kind: str          # "think" | "answer" | "ask_user" | "brain_down" | "error"
    text: str = ""


@dataclass
class RequestContext:
    persona: Optional[dict]
    messages: list[dict]                 # sanitized conversation, no system msg
    guard: RequestGuardState
    logger: RequestLogger
    pending_question: Optional[str] = None


class AgentState(TypedDict, total=False):
    messages: list
    done: bool


class AgentRunner:
    def __init__(self, brain: BrainClient, pool, tool_registry, model_registry,
                 guardrails: GuardrailEngine, meridian_usage: MeridianUsage,
                 research=None):
        self.brain = brain
        self.pool = pool
        self.tool_registry = tool_registry
        self.model_registry = model_registry
        self.guardrails = guardrails
        self.meridian_usage = meridian_usage
        self.research = research

    # ------------------------------------------------------------------ run --

    async def run(self, ctx: RequestContext) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[Optional[AgentEvent]] = asyncio.Queue()

        def emit(kind: str, text: str = "") -> None:
            queue.put_nowait(AgentEvent(kind, text))

        emit("think", "Checking model registry and available backends...")
        system, specs = await self._build_context(ctx)
        graph, flags = self._build_graph(ctx, emit, system, specs)

        max_steps = self.guardrails.effective(ctx.persona)["max_steps_per_request"]

        async def runner() -> None:
            try:
                await graph.ainvoke(
                    {"messages": list(ctx.messages), "done": False},
                    config={"recursion_limit": 4 * max_steps + 16})
                if not flags["finalized"]:
                    emit("answer", flags["last_tool_result"]
                         or "The routing agent finished without producing an answer — "
                            "please retry.")
            except BrainUnreachable as e:
                emit("brain_down", str(e))
            except Exception as e:  # any other bug degrades to an error answer
                log.exception("agent loop crashed")
                emit("error", f"Router error: {e}")
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(runner())
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task

    # -------------------------------------------------------- request context --

    async def _build_context(self, ctx: RequestContext) -> tuple[str, list[dict]]:
        persona = ctx.persona
        category = (persona or {}).get("benchmark_category") or "general_chat"
        available = self.pool.available_models()
        ranked = self.model_registry.ranked_for_category(
            category, list(available.keys()), limit=12)
        tool_name_for = {t.model_id: t.name for t in self.tool_registry.enabled()
                         if t.kind == "model" and t.model_id}

        # Window state goes into the prompt so the brain factors it into the
        # decision (§4.7) — the guardrail check at dispatch remains the hard stop.
        meridian_note = "no Claude/subscription backend configured"
        for s in getattr(self.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
            if s.healthy:
                ok, note = await self.meridian_usage.window_available(s.config.url)
                meridian_note = note if ok else f"EXHAUSTED — do not route to Claude ({note})"
                break
            meridian_note = f"backend {s.config.name} currently unreachable"

        system = prompts.build_system_prompt(
            persona, ranked, tool_name_for, meridian_note, ctx.pending_question)
        # Snapshot: core tools + this persona's view of the dynamic set. Tool
        # Sync may swap the registry mid-request; this request keeps its list.
        specs = prompts.CORE_TOOL_SPECS + self.tool_registry.specs_for_persona(persona)
        return system, specs

    # --------------------------------------------------------------- the graph --

    def _build_graph(self, ctx: RequestContext, emit, system: str, specs: list[dict]):
        flags: dict[str, Any] = {"finalized": False, "last_tool_result": ""}
        effective = self.guardrails.effective(ctx.persona)

        async def brain_node(state: AgentState) -> AgentState:
            verdict = self.guardrails.check_step(ctx.guard, effective)
            if not verdict.allowed:
                emit("think", f"Guardrail fired: {verdict.reason}. Finalizing with what we have.")
                ctx.logger.record_guardrail(verdict.reason)
                emit("answer", flags["last_tool_result"]
                     or "I hit the routing step limit before completing this request — "
                        "please retry or simplify it.")
                flags["finalized"] = True
                return {"done": True}
            ctx.guard.steps += 1
            ctx.logger.steps = ctx.guard.steps

            result = await self.brain.chat(
                [{"role": "system", "content": system}] + state["messages"], tools=specs)
            msg: dict = {"role": "assistant", "content": result.content or ""}
            if result.tool_calls:
                msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in result.tool_calls]
                if result.content.strip():
                    # Free-text alongside tool calls = the brain thinking out
                    # loud; surface it as narration.
                    emit("think", result.content.strip())
            return {"messages": state["messages"] + [msg]}

        async def tools_node(state: AgentState) -> AgentState:
            msgs = list(state["messages"])
            done = False
            for tc in msgs[-1].get("tool_calls") or []:
                name = tc["function"]["name"]
                args = tc["function"].get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                result_text, finalize = await self._execute_tool(
                    name, args, ctx, emit, flags, effective)
                if finalize:
                    done = True
                    break
                msgs.append({"role": "tool", "content": result_text[:TOOL_RESULT_LIMIT],
                             "tool_call_id": tc.get("id"), "name": name})
            return {"messages": msgs, "done": done}

        def route_after_brain(state: AgentState) -> str:
            if state.get("done") or flags["finalized"]:
                return "end"
            last = state["messages"][-1]
            if last.get("tool_calls"):
                return "tools"
            # Content with no tool call: small brains skip return_to_user and
            # just answer — accept it as the final answer rather than looping.
            content = (last.get("content") or "").strip()
            emit("answer", content or flags["last_tool_result"]
                 or "The routing agent produced no answer — please retry.")
            flags["finalized"] = True
            return "end"

        def route_after_tools(state: AgentState) -> str:
            return "end" if state.get("done") or flags["finalized"] else "brain"

        g = StateGraph(AgentState)
        g.add_node("brain", brain_node)
        g.add_node("tools", tools_node)
        g.add_edge(START, "brain")
        g.add_conditional_edges("brain", route_after_brain, {"tools": "tools", "end": END})
        g.add_conditional_edges("tools", route_after_tools, {"brain": "brain", "end": END})
        return g.compile(), flags

    # ---------------------------------------------------------- tool execution --

    async def _execute_tool(self, name: str, args: dict, ctx: RequestContext,
                            emit, flags: dict, effective: dict) -> tuple[str, bool]:
        """Returns (tool_result_text, finalize). Tool failures come back as
        ERROR strings the brain can react to — never exceptions that kill the
        request (§4.3: the agent decides whether to fall back a tier)."""

        # ---- core tools (fixed, intrinsic — §4.2) ----
        if name == "return_to_user":
            answer = str(args.get("answer") or "")
            if args.get("use_last_result") or not answer.strip():
                answer = flags["last_tool_result"] or answer
            emit("answer", answer.strip() or "(empty answer)")
            flags["finalized"] = True
            return "", True

        if name == "ask_user":
            question = str(args.get("question") or "Could you clarify what you need?")
            emit("ask_user", question)
            flags["finalized"] = True
            return "", True

        if name == "refine_prompt":
            original = str(args.get("request") or "")
            if not original:
                for m in reversed(ctx.messages):
                    if m["role"] == "user":
                        original = m["content"]
                        break
            emit("think", "Refining the request before dispatch...")
            try:
                refined = (await self.brain.complete(
                    prompts.REFINE_INSTRUCTION.format(request=original))).strip()
            except BrainUnreachable:
                raise
            if not refined:
                return "Refinement produced nothing; proceed with the original request.", False
            # §4.2/§4.5: the refined prompt MUST stream before dispatch, so
            # drift is visible while it's still correctable.
            emit("think", f'Refined prompt: "{refined}"')
            return f"Refined request (use this as the prompt):\n{refined}", False

        if name == "request_model_research":
            model_name = str(args.get("model_name") or "")
            queued = bool(self.research and model_name and self.research.enqueue(model_name))
            emit("think", f"Queued background research on {model_name}."
                 if queued else f"Research for {model_name!r} not queued (disabled or duplicate).")
            return ("Research queued — non-blocking; proceed with conservative assumptions "
                    "(unknown capability, moderate cost) for this request."), False

        # ---- dynamic tools (Tool Sync, §4.2) ----
        tool = self.tool_registry.get(name)
        if tool is None:
            emit("think", f"Tool {name} is no longer available (backend change mid-request).")
            return (f"ERROR: tool {name} is no longer available — its backend went "
                    f"offline or the model was removed. Choose a different model."), False

        if tool.kind == "model":
            model_id = tool.model_id
            meta = self.model_registry.get(model_id)
            backend_info = self.pool.backend_info(model_id)
            verdict = await self.guardrails.check_paid_call(
                model_id, backend_info, meta, ctx.guard, effective)
            if not verdict.allowed:
                emit("think", f"Guardrail blocked {model_id}: {verdict.reason}")
                ctx.logger.record_guardrail(f"denied {model_id}: {verdict.reason}")
                return f"DENIED by guardrail: {verdict.reason}", False

            prompt = str(args.get("prompt") or "")
            if not prompt.strip():
                return f"ERROR: ask tool called with an empty prompt.", False
            emit("think", f"Routing to {model_id}"
                          + (f" via {backend_info['name']}" if backend_info else "") + "...")
            try:
                result, backend_name = await self.pool.chat(
                    model_id, [{"role": "user", "content": prompt}],
                    max_tokens=WORKER_MAX_TOKENS)
            except AllBackendsFailed as e:
                emit("think", f"All backends failed for {model_id} — the brain will pick "
                              f"an alternative.")
                return (f"ERROR: {e}. Pick a different model or finish with "
                        f"what you already have."), False
            cost = estimate_cost_usd(meta, result.prompt_tokens, result.completion_tokens)
            ctx.logger.record_model_call(model_id, backend_name,
                                         result.prompt_tokens, result.completion_tokens, cost)
            flags["last_tool_result"] = result.content
            emit("think", f"{model_id} responded ({len(result.content)} chars).")
            return result.content or "(model returned empty output)", False

        # tool.kind == "mcp"
        emit("think", f"Calling MCP tool {tool.server}/{tool.mcp_tool}...")
        try:
            out = await self.tool_registry.mcp.call_tool(tool.server, tool.mcp_tool, args)
            return out[:MCP_RESULT_LIMIT] or "(empty MCP result)", False
        except Exception as e:
            emit("think", f"MCP tool {name} failed: {e}")
            return f"ERROR: MCP tool {name} failed: {e}", False
