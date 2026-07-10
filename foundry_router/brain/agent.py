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
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from ..guardrails import GuardrailEngine, RequestGuardState
from ..pool.base import AllBackendsFailed
from ..usage import MeridianUsage, RequestLogger, estimate_cost_usd
from . import prompts
from .client import BrainClient, BrainUnreachable

log = logging.getLogger(__name__)

# Context-budget limits (worker output tokens, chars of a tool result fed
# back to the brain) are deployment-specific config, not constants — a 6GB
# local brain and a Claude-class brain need wildly different sizing, and
# getting it wrong shows up as silent context truncation, not an error. See
# AgentBrainConfig in config.py (tool_result_limit_chars /
# mcp_result_limit_chars / worker_max_tokens); editable live from the web UI.


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

        # Narration policy (§4.5, tightened per operator preference): stream a
        # <think> line for every wait point and decision — routed requests take
        # longer than a direct model call, and the narration is simultaneously
        # the user's progress indicator and the first debugging artifact.
        emit("think", "Analyzing request and checking available backends...")
        system, specs, info = await self._build_context(ctx)
        emit("think",
             f"Persona {info['persona_name']} ({info['category']}): "
             f"{info['n_models']} candidate models across {info['n_backends']} "
             f"healthy backend(s). Claude window: {info['meridian_note']}.")
        graph, flags = self._build_graph(ctx, emit, system, specs)

        max_steps = self.guardrails.effective(ctx.persona)["max_steps_per_request"]

        # The brain's chat template (Ornith, and most instruct templates)
        # requires exactly one system message, strictly first. Clients like
        # AnythingLLM send their own workspace system message in the history;
        # prepending ours on top of it 400s the backend. Their system content
        # is NOT dropped — _build_context folds it into the router's own
        # system prompt; only ctx.messages keeps the original (the static
        # fallback forwards the raw conversation and should keep it).
        #
        # Large messages are also previewed down to user_input_preview_chars
        # for the BRAIN's view only (input-side twin of the tool-result cap:
        # a 23k-char pasted file otherwise blows the brain's context before
        # its first routing decision). ctx.messages stays untouched — workers
        # get the full original via include_full_user_message, and the static
        # fallback forwards the raw conversation.
        non_system = self._preview_for_brain(
            [m for m in ctx.messages if m.get("role") != "system"])

        async def runner() -> None:
            try:
                await graph.ainvoke(
                    {"messages": non_system, "done": False},
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

    # ------------------------------------------------- brain-view previewing --

    def _preview_for_brain(self, messages: list[dict]) -> list[dict]:
        """Truncate oversized message contents for the brain's view only, with
        an explicit notice — without it a small brain assumes the preview IS
        the message. The newest user message's notice additionally names the
        include_full_user_message flag, the delivery mechanism that gets the
        complete original to whichever worker the brain dispatches."""
        limit = self.brain.cfg.user_input_preview_chars
        last_user_idx = max((i for i, m in enumerate(messages)
                             if m.get("role") == "user"), default=-1)
        out = []
        for i, m in enumerate(messages):
            content = m.get("content") or ""
            if len(content) <= limit:
                out.append(m)
                continue
            if i == last_user_idx:
                notice = (f"\n...[preview truncated at {limit} of {len(content)} "
                          f"chars to fit your context. The COMPLETE message will be "
                          f"appended verbatim to your worker prompt if you set "
                          f"include_full_user_message=true on your ask_* call.]")
            else:
                notice = (f"\n...[preview truncated at {limit} of {len(content)} "
                          f"chars — older history, full text retained in the "
                          f"conversation record.]")
            out.append({**m, "content": content[:limit] + notice})
        return out

    def _full_last_user_message(self, ctx: RequestContext) -> str:
        for m in reversed(ctx.messages):
            if m.get("role") == "user":
                return m.get("content") or ""
        return ""

    # -------------------------------------------------------- request context --

    async def _build_context(self, ctx: RequestContext) -> tuple[str, list[dict], dict]:
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

        # Client/workspace system messages can't ride along in the message list
        # (the brain template demands a single, first system message) — carry
        # their content inside our system prompt instead so the brain can honor
        # them and relay them when writing worker prompts.
        client_system = "\n\n".join(
            m.get("content") or "" for m in ctx.messages
            if m.get("role") == "system" and (m.get("content") or "").strip())
        system = prompts.build_system_prompt(
            persona, ranked, tool_name_for, meridian_note, ctx.pending_question,
            client_system=client_system or None)
        # Snapshot: core tools + this persona's view of the dynamic set. Tool
        # Sync may swap the registry mid-request; this request keeps its list.
        specs = prompts.CORE_TOOL_SPECS + self.tool_registry.specs_for_persona(persona)
        info = {
            "persona_name": (persona or {}).get("virtual_name", "(none)"),
            "category": category,
            "n_models": len(available),
            "n_backends": len({b for names in available.values() for b in names}),
            "meridian_note": meridian_note,
        }
        return system, specs, info

    # --------------------------------------------------------------- the graph --

    def _build_graph(self, ctx: RequestContext, emit, system: str, specs: list[dict]):
        flags: dict[str, Any] = {"finalized": False, "last_tool_result": "",
                                 "nudged": False}
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
            emit("think", f"Consulting routing brain ({self.brain.cfg.model or 'brain'}, "
                          f"step {ctx.guard.steps}/{effective['max_steps_per_request']})...")

            t0 = time.monotonic()
            result = await self.brain.chat(
                [{"role": "system", "content": system}] + state["messages"], tools=specs,
                on_retry=lambda: emit(
                    "think", "Brain produced a malformed tool call — retrying once "
                             "with corrective feedback..."))
            took = time.monotonic() - t0

            msg: dict = {"role": "assistant", "content": result.content or ""}
            if result.tool_calls:
                msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in result.tool_calls]
                names = ", ".join(tc["name"] for tc in result.tool_calls)
                emit("think", f"Brain decided in {took:.1f}s → {names}")
                if result.content.strip():
                    # Free-text alongside tool calls = the brain explaining its
                    # choice (prompt rule 10); surface it as narration.
                    emit("think", result.content.strip())
                return {"messages": state["messages"] + [msg]}

            # No tool call at all: the brain tried to author an answer in
            # prose (or returned nothing). Its role forbids that — nudge it
            # ONCE, structurally, back toward delegation. This is the code-
            # level enforcement of prompt rule 2; prose alone proved
            # insufficient (a 9B brain confidently "answered" a current-events
            # question from stale training data).
            if not flags["nudged"]:
                flags["nudged"] = True
                emit("think", f"Brain replied in prose instead of dispatching "
                              f"({took:.1f}s) — redirecting it to delegate to a "
                              f"worker model...")
                new = state["messages"] + ([msg] if (result.content or "").strip() else [])
                new.append({"role": "user", "content":
                            "(router) You replied with prose instead of a tool call. "
                            "You must not author answers yourself — your knowledge is "
                            "stale and unverifiable. Dispatch the task to a worker via "
                            "an ask_<model> tool, then forward its output with "
                            "return_to_user(use_last_result=true) — or call ask_user "
                            "if the request is genuinely ambiguous."})
                return {"messages": new}
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
                # Truncation here caps only what the BRAIN sees — the full
                # result is preserved in flags["last_tool_result"] and reaches
                # the user via return_to_user(use_last_result=true). The
                # explicit truncation notice matters: without it a small brain
                # assumes the preview IS the output and retypes it, truncated.
                limit = self.brain.cfg.tool_result_limit_chars
                if len(result_text) > limit:
                    result_text = (
                        result_text[:limit]
                        + f"\n...[preview truncated at {limit} of {len(result_text)} "
                          f"chars to fit your context. The COMPLETE output is stored "
                          f"and will reach the user verbatim if you call "
                          f"return_to_user with use_last_result=true.]")
                msgs.append({"role": "tool", "content": result_text,
                             "tool_call_id": tc.get("id"), "name": name})
            return {"messages": msgs, "done": done}

        def route_after_brain(state: AgentState) -> str:
            if state.get("done") or flags["finalized"]:
                return "end"
            last = state["messages"][-1]
            if last.get("role") == "user":
                # brain_node appended a nudge correction — give it another try
                return "brain"
            if last.get("tool_calls"):
                return "tools"
            # Prose again even after the nudge: accept it rather than loop
            # forever, but say plainly (to the user and the log) that this
            # answer bypassed the worker-model path — that transparency is the
            # difference between a quiet lie and a flagged degradation.
            content = (last.get("content") or "").strip()
            if content:
                emit("think", "Brain insisted on answering directly — forwarding its "
                              "answer. Note: NOT produced or verified by a worker model.")
                ctx.logger.record_guardrail("brain answered directly (prose, post-nudge)")
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
        g.add_conditional_edges("brain", route_after_brain,
                                {"tools": "tools", "end": END, "brain": "brain"})
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
                if flags["last_tool_result"]:
                    emit("think", f"Forwarding the worker's full output verbatim "
                                  f"({len(flags['last_tool_result'])} chars).")
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
                original = self._full_last_user_message(ctx)
            # refine works on a preview too — it tightens intent, it doesn't
            # need (and the brain can't afford) the full pasted payload.
            original = original[:self.brain.cfg.user_input_preview_chars]
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
            if args.get("include_full_user_message"):
                # Delivery half of the input-preview mechanism: the brain only
                # saw a capped preview of a large user message, so the
                # complete original rides to the worker here — mirroring how
                # use_last_result preserves full tool output for the user.
                full = self._full_last_user_message(ctx)
                if full:
                    emit("think", f"Attaching the user's complete original message "
                                  f"({len(full)} chars) to the worker prompt.")
                    prompt = (prompt + "\n\n--- FULL ORIGINAL USER MESSAGE "
                                       "(verbatim) ---\n" + full)
            emit("think", f"Routing to {model_id}"
                          + (f" via {backend_info['name']}" if backend_info else "")
                          + " — waiting on generation (long tasks can take a few minutes)...")
            t0 = time.monotonic()
            try:
                result, backend_name = await self.pool.chat(
                    model_id, [{"role": "user", "content": prompt}],
                    max_tokens=self.brain.cfg.worker_max_tokens)
            except AllBackendsFailed as e:
                emit("think", f"All backends failed for {model_id} after "
                              f"{time.monotonic() - t0:.0f}s — the brain will pick "
                              f"an alternative.")
                return (f"ERROR: {e}. Pick a different model or finish with "
                        f"what you already have."), False
            cost = estimate_cost_usd(meta, result.prompt_tokens, result.completion_tokens)
            ctx.logger.record_model_call(model_id, backend_name,
                                         result.prompt_tokens, result.completion_tokens, cost)
            flags["last_tool_result"] = result.content
            emit("think", f"{model_id} responded in {time.monotonic() - t0:.0f}s "
                          f"({len(result.content)} chars"
                          + (f", ~${cost:.4f}" if cost else "") + ").")
            return result.content or "(model returned empty output)", False

        # tool.kind == "mcp"
        emit("think", f"Calling MCP tool {tool.server}/{tool.mcp_tool}...")
        try:
            out = await self.tool_registry.mcp.call_tool(tool.server, tool.mcp_tool, args)
            return out[:self.brain.cfg.mcp_result_limit_chars] or "(empty MCP result)", False
        except Exception as e:
            emit("think", f"MCP tool {name} failed: {e}")
            return f"ERROR: MCP tool {name} failed: {e}", False
