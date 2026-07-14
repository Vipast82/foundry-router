"""The live Routing Agent (design doc §4.2): a LangGraph loop where every
action is a tool the brain chooses at runtime — never a fixed
plan->code->review pipeline.

LangGraph provides the loop skeleton (StateGraph: brain -> tools -> brain);
model I/O goes through this project's own protocol adapters rather than
langchain provider packages — one shared HTTP layer for brain, pool, and
research keeps dependencies minimal (§2) and the wire behavior identical
everywhere.

Streaming: nodes emit AgentEvents into a queue that `run()` drains as an async
generator — the facade streams "think" events as Ollama's native `thinking`
field (§4.5's intent, corrected to the real wire format) live, while the
graph is still executing.

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

from ..errors import describe_exception
from ..guardrails import GuardrailEngine, RequestGuardState
from ..pool.base import AllBackendsFailed, ContextTooLarge
from ..usage import (MeridianUsage, RequestLogger, estimate_cost_usd,
                     log_subscription_usage)
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


def _json_list(value) -> list:
    import json as _json
    try:
        out = _json.loads(value or "[]")
        return out if isinstance(out, list) else []
    except (_json.JSONDecodeError, TypeError):
        return []


def _filter_required_tags(ranked: list[dict], persona: Optional[dict]) -> list[dict]:
    """Persona required_tags (Foundry-Vision): when any candidate carries one
    of the required tags, the list is FILTERED to matches — a vision persona
    must not quietly dispatch a text-only model. If nothing matches, the full
    list survives (degrade with options rather than fail with none)."""
    required = _json_list((persona or {}).get("required_tags"))
    if not required:
        return ranked
    def matches(row: dict) -> bool:
        return any(t in _json_list(row.get("tags")) for t in required)
    matching = [r for r in ranked if matches(r)]
    return matching or ranked


def _steer_vision_when_images(ranked: list[dict], messages: list[dict]) -> list[dict]:
    """When the request actually carries an image, ANY persona's candidates
    are filtered to vision-tagged models (not just Foundry-Vision) — routing a
    photo to a text-only model is always wrong. Degrades to the full list if
    no vision-tagged candidate is reachable (the brain's marker still tells it
    images exist, so it can say so instead of hallucinating)."""
    if not any(m.get("images") for m in messages if m.get("role") == "user"):
        return ranked
    matching = [r for r in ranked if "vision" in _json_list(r.get("tags"))]
    return matching or ranked


def _json_obj(value) -> dict:
    import json as _json
    try:
        out = _json.loads(value or "{}")
        return out if isinstance(out, dict) else {}
    except (_json.JSONDecodeError, TypeError):
        return {}


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 chars/token) — no tokenizer dependency in the
    tree, and a heuristic is enough for a fit/no-fit gate. Same basis as the
    ranking gate's min_context estimate, so the two agree."""
    return max(1, len(text or "") // 4)


# High-precision refusal phrases. Ollama exposes no structured safety/refusal
# signal (a decline is just ordinary text content), so refusal is detected by
# language — but ONLY in the head of the response, where a genuine refusal
# leads with its decline, to avoid false positives on long legitimate answers
# that merely mention a limitation partway through.
_REFUSAL_MARKERS = (
    "i can't help", "i cannot help", "i can't assist", "i cannot assist",
    "i can't provide", "i cannot provide", "i can't create", "i cannot create",
    "i can't generate", "i cannot generate", "i can't write", "i cannot write",
    "i won't help", "i will not help", "i won't be able to", "i'm not able to",
    "i am not able to", "i'm unable to", "i am unable to", "i must decline",
    "i have to decline", "i must refuse", "i cannot comply", "i can't comply",
    "cannot fulfill", "can't fulfill", "unable to fulfill",
    "against my guidelines", "against my programming",
    "i'm sorry, but i can't", "i'm sorry but i can't", "i'm sorry, i can't",
    "i apologize, but i can't", "i apologize but i can't",
    "i'm not comfortable", "i am not comfortable", "not appropriate for me to",
    "i must refrain", "i cannot in good conscience",
)


def _looks_like_refusal(text: Optional[str]) -> bool:
    if not text:
        return False
    head = text.strip().lower()[:400]
    return any(m in head for m in _REFUSAL_MARKERS)


def _apply_pins(ranked: list[dict], persona: Optional[dict]) -> list[dict]:
    """Reorder candidates so the persona's pinned_models lead, in pin order,
    marked _pinned for the prompt's [PINNED] group. Pins absent from the
    reachable set are silently skipped — pinning never invents a backend."""
    import json as _json
    try:
        pinned = _json.loads((persona or {}).get("pinned_models") or "[]")
    except (_json.JSONDecodeError, TypeError):
        pinned = []
    if not pinned:
        return ranked
    by_id = {r["id"]: r for r in ranked}
    front = [{**by_id[p], "_pinned": True} for p in pinned if p in by_id]
    pinned_set = {r["id"] for r in front}
    return front + [r for r in ranked if r["id"] not in pinned_set]


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
        attached = self._attached_images(ctx)
        emit("think",
             f"Persona {info['persona_name']} ({info['category']}): "
             f"{info['n_models']} candidate models across {info['n_backends']} "
             f"healthy backend(s). Claude window: {info['meridian_note']}."
             + (f" {len(attached)} image(s) attached — steering to vision-capable "
                f"models." if attached else ""))
        for tb in info.get("tiebreaks", []):
            emit("think", f"Tiebreaker — {tb}.")
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
        complete original to whichever worker the brain dispatches.

        Image handling: the brain NEVER sees image bytes (a single base64
        image would blow its context like the 23k-char paste did) — the
        images field is stripped from its view and replaced with a marker
        naming the delivery mechanism. ctx.messages keeps the originals for
        workers and the static fallback."""
        limit = self.brain.cfg.user_input_preview_chars
        last_user_idx = max((i for i, m in enumerate(messages)
                             if m.get("role") == "user"), default=-1)
        out = []
        for i, m in enumerate(messages):
            content = m.get("content") or ""
            if m.get("images"):
                n = len(m["images"])
                m = {k: v for k, v in m.items() if k != "images"}
                content = (content
                           + f"\n[ATTACHED: {n} image(s) — bytes hidden from you. "
                             f"Route to a vision-capable model (candidates tagged "
                             f"'vision') and set include_images=true on your ask_* "
                             f"call so the worker receives them. Vision-tagged "
                             f"workers also receive them automatically.]")
            if len(content) <= limit:
                out.append({**m, "content": content})
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

    @staticmethod
    def _attached_images(ctx: RequestContext) -> Optional[list]:
        """Images from the newest user message that carries any (originals
        live in ctx.messages — the brain only ever saw the marker)."""
        for m in reversed(ctx.messages):
            if m.get("role") == "user" and m.get("images"):
                return m["images"]
        return None

    # -------------------------------------------------------- request context --

    async def _build_context(self, ctx: RequestContext) -> tuple[str, list[dict], dict]:
        persona = ctx.persona
        category = (persona or {}).get("benchmark_category") or "general_chat"
        available = self.pool.available_models()
        # Multi-signal within-tier ranking (see ranked_for_category):
        #  - tool-attached personas blend tool_calling reliability, so a strong
        #    tool-caller isn't out-ranked on the primary category alone (found
        #    live: Foundry-Chat -> qwen3-14b over the stronger ornith:35b);
        #  - content policy is NOT a ranking input: a permissive model ranks on
        #    merit like any other, and the request-level refusal fallback (in
        #    _execute_tool) pulls a permissive model in only when a standard one
        #    actually declines a specific request. Only an explicit front-door
        #    persona (prefer_permissive) floats permissive models to the top;
        #  - a persona can override signal weights (e.g. weight latency);
        #  - a large request gates out models whose context can't hold it.
        has_tools = bool(_json_list((persona or {}).get("preferred_mcp_tools")))
        overrides = _json_obj((persona or {}).get("selection_weights"))
        permissive_mode = "prefer" if (persona or {}).get("prefer_permissive") else "neutral"
        approx_tokens = sum(len(m.get("content") or "")
                            for m in ctx.messages) // 4
        min_context = approx_tokens + 1024 if approx_tokens > 2000 else None
        ranked = self.model_registry.ranked_for_category(
            category, list(available.keys()), limit=12,
            blend_tool_calling=has_tools, weights=overrides or None,
            permissive_mode=permissive_mode, min_context=min_context)
        tool_name_for = {t.model_id: t.name for t in self.tool_registry.enabled()
                         if t.kind == "model" and t.model_id}

        # Real window state goes into the prompt so the brain factors current
        # usage pressure into tier choice (§4.7) — the guardrail check at
        # dispatch remains the hard stop.
        meridian_note = "no Claude/subscription backend configured"
        for s in getattr(self.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
            if s.healthy:
                snap = await self.meridian_usage.snapshot(s.config.url, s.config.api_key)
                meridian_note = (snap["note"] if snap["available"]
                                 else f"EXHAUSTED — do not route to Claude ({snap['note']})")
                break
            meridian_note = f"backend {s.config.name} currently unreachable"

        # Persona candidate shaping, in order: dynamic vision steer (an
        # attached image beats any persona default), required-tag filter
        # (Vision), then pins on top. Permissive preference/avoidance is handled
        # inside ranked_for_category (within-tier) so it respects tier order.
        ranked = _steer_vision_when_images(ranked, ctx.messages)
        ranked = _filter_required_tags(ranked, persona)
        ranked = _apply_pins(ranked, persona)

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
            # Auditable named-benchmark tiebreaks (surfaced as narration).
            "tiebreaks": [r["_tiebreak"] for r in ranked if r.get("_tiebreak")],
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

            def _on_retry():
                emit("think", "Brain produced a malformed tool call — retrying once "
                              "with corrective feedback...")
                # Empirical tool-calling reliability: a malformed call is a
                # data point against the brain model itself.
                self.model_registry.record_tool_call(self.brain.cfg.model, ok=False)

            t0 = time.monotonic()
            result = await self._with_heartbeat(
                self.brain.chat([{"role": "system", "content": system}]
                                + state["messages"], tools=specs, on_retry=_on_retry),
                emit, f"the routing brain ({self.brain.cfg.model or 'brain'})")
            took = time.monotonic() - t0
            if result.tool_calls:
                self.model_registry.record_tool_call(self.brain.cfg.model, ok=True)

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
            # Outcome-based escalation (cross-cutting spec §2): before a LOCAL
            # answer is delivered, the persona's configured judge decides
            # adequate/inadequate. Inadequate converts this return_to_user
            # into a tool result instructing the brain to escalate — normal
            # tier selection takes it from there. Judged at most once.
            judge_mode = (ctx.persona or {}).get("outcome_judge")
            if (judge_mode and not flags.get("judged")
                    and flags.get("last_worker_local")
                    and flags.get("last_tool_result")):
                flags["judged"] = True
                adequate, reasoning, judge_desc = await self._judge_outcome(
                    ctx, emit, flags, judge_mode, effective)
                if not adequate:
                    emit("think", f"Outcome judge ({judge_desc}): INADEQUATE — "
                                  f"escalating. {reasoning}")
                    ctx.logger.record_guardrail(
                        f"outcome judge escalation ({judge_desc}): {reasoning[:200]}")
                    return (f"OUTCOME JUDGE ({judge_desc}) found the local answer "
                            f"INADEQUATE: {reasoning} Escalate now: dispatch a "
                            f"higher-tier model per the SELECTION PROCEDURE (set "
                            f"include_full_user_message=true if the user message "
                            f"was truncated), then forward its result with "
                            f"return_to_user(use_last_result=true)."), False
                emit("think", f"Outcome judge ({judge_desc}): adequate — "
                              f"delivering the local answer. {reasoning}")
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
            # Image delivery, mirroring include_full_user_message: explicit
            # flag OR automatic for vision-tagged workers — a brain that
            # forgets the flag must not strand the photo.
            images = self._attached_images(ctx)
            if images:
                is_vision = "vision" in _json_list((meta or {}).get("tags"))
                if args.get("include_images") or is_vision:
                    emit("think", f"Attaching the user's {len(images)} image(s) "
                                  f"to the worker call.")
                else:
                    images = None
            emit("think", f"Routing to {model_id}"
                          + (f" via {backend_info['name']}" if backend_info else "")
                          + " — waiting on generation (long tasks can take a few minutes)...")
            t0 = time.monotonic()
            try:
                result, backend_name = await self._with_heartbeat(
                    self._dispatch_worker(model_id, prompt, images=images),
                    emit, model_id)
            except ContextTooLarge as e:
                # Distinct from a backend failure: the request is too big for
                # THIS model — steer the brain to a larger-context one rather
                # than let it retry the same too-small model.
                emit("think", f"{model_id} can't hold this request — {e}. "
                              f"Routing to a larger-context model.")
                ctx.logger.record_guardrail(f"context too large for {model_id}")
                return (f"ERROR: {e}. Choose a model with a LARGER context window "
                        f"(small-context models are de-prioritized for this "
                        f"request), or shorten/summarize the input first."), False
            except AllBackendsFailed as e:
                emit("think", f"All backends failed for {model_id} after "
                              f"{time.monotonic() - t0:.0f}s — the brain will pick "
                              f"an alternative.")
                return (f"ERROR: {e}. Pick a different model or finish with "
                        f"what you already have."), False
            # Request-level permissive fallback: content policy is NOT a ranking
            # input, so a standard model may be routed a request it declines. If
            # THIS response is a refusal and the model isn't already permissive,
            # retry once on the best permissive model — the one job permissive
            # models exist for. Retry-once-then-degrade, like the pipeline's
            # cold-load fix; never a loop.
            if (_looks_like_refusal(result.content)
                    and (meta or {}).get("content_policy") != "permissive"):
                category = (ctx.persona or {}).get("benchmark_category") or "general_chat"
                alt = self._best_permissive(category, exclude=model_id)
                if alt is None:
                    emit("think", f"{model_id} declined this request and no permissive "
                                  f"model is reachable — returning its response.")
                else:
                    alt_meta = self.model_registry.get(alt)
                    alt_info = self.pool.backend_info(alt)
                    alt_verdict = await self.guardrails.check_paid_call(
                        alt, alt_info, alt_meta, ctx.guard, effective)
                    if not alt_verdict.allowed:
                        emit("think", f"{model_id} declined; permissive fallback {alt} "
                                      f"blocked by guardrail ({alt_verdict.reason}) — "
                                      f"returning the original response.")
                    else:
                        emit("think", f"{model_id} declined this request. Retrying once "
                                      f"on the best permissive model ({alt}) — permissive "
                                      f"models are kept for exactly this case...")
                        try:
                            alt_result, alt_backend = await self._with_heartbeat(
                                self._dispatch_worker(alt, prompt, images=images),
                                emit, alt)
                            if _looks_like_refusal(alt_result.content):
                                emit("think", f"{alt} also declined — delivering the "
                                              f"original response.")
                            else:
                                model_id, meta, backend_info = alt, alt_meta, alt_info
                                result, backend_name = alt_result, alt_backend
                        except AllBackendsFailed as e:
                            emit("think", f"Permissive fallback {alt} failed "
                                          f"({describe_exception(e)}) — delivering the "
                                          f"original response.")
            if result.thinking:
                # The worker's own reasoning, scrubbed/collected at dispatch —
                # narration for the user's thought panel, never answer content.
                emit("think", f"[{model_id} reasoning] {result.thinking[:2000]}"
                              + ("…" if len(result.thinking) > 2000 else ""))
            cost = estimate_cost_usd(meta, result.prompt_tokens, result.completion_tokens)
            ctx.logger.record_model_call(model_id, backend_name,
                                         result.prompt_tokens, result.completion_tokens, cost)
            if backend_info and backend_info.get("type") == "anthropic-compatible":
                # Subscription consumption is measured in tokens, not dollars
                # — our own historical record alongside Meridian's snapshot.
                log_subscription_usage(self.model_registry.db, model_id,
                                       backend_name, result.prompt_tokens,
                                       result.completion_tokens)
            flags["last_tool_result"] = result.content
            # Tracked for the outcome judge: only LOCAL answers get judged
            # (escalated answers already cost paid budget — judging them again
            # would spend more to second-guess the expensive tier).
            flags["last_worker_model"] = model_id
            flags["last_worker_local"] = bool(
                backend_info and backend_info.get("type") == "ollama")
            emit("think", f"{model_id} responded in {time.monotonic() - t0:.0f}s "
                          f"({len(result.content)} chars"
                          + (f", ~${cost:.4f}" if cost else "") + ").")
            return result.content or "(model returned empty output)", False

        # tool.kind == "mcp"
        emit("think", f"Calling MCP tool {tool.server}/{tool.mcp_tool}...")
        t0 = time.monotonic()
        try:
            out = await self._with_heartbeat(
                self.tool_registry.mcp.call_tool(tool.server, tool.mcp_tool, args),
                emit, f"MCP tool {tool.server}/{tool.mcp_tool}")
        except Exception as e:
            detail = describe_exception(e)
            dur_ms = int((time.monotonic() - t0) * 1000)
            # Per-call visibility (spec item 5): the request_log row shows
            # which tools this request tried, and the Events entry makes
            # MCP-server failures diagnosable separately from backend/model
            # failures — same layer that logs backend_pool warnings.
            ctx.logger.record_tool_call(tool.mcp_tool, tool.server, dur_ms,
                                        ok=False, error=detail)
            self.model_registry.db.log_event(
                "warning", "mcp",
                f"tool {tool.server}/{tool.mcp_tool} failed after {dur_ms / 1000:.1f}s",
                detail)
            emit("think", f"MCP tool {name} failed after {dur_ms / 1000:.1f}s: {detail}")
            return f"ERROR: MCP tool {name} failed: {detail}", False
        dur_ms = int((time.monotonic() - t0) * 1000)
        ctx.logger.record_tool_call(tool.mcp_tool, tool.server, dur_ms, ok=True)
        emit("think", f"MCP tool {tool.server}/{tool.mcp_tool} completed in "
                      f"{dur_ms / 1000:.1f}s ({len(out)} chars).")
        flags["last_tool_result"] = out  # media artifacts (URLs) forward via use_last_result
        flags["last_worker_local"] = False  # MCP results aren't judged
        return out[:self.brain.cfg.mcp_result_limit_chars] or "(empty MCP result)", False

    # ------------------------------------------------- keep-alive heartbeat --

    async def _with_heartbeat(self, coro, emit, label: str):
        """Await a long call while emitting periodic 'still working' narration.

        Found live: a failover-then-cold-load chain took 422s and produced a
        real answer (request_log says ok), but the client/reverse-proxy had
        long since closed the idle connection. Raising proxy timeouts can't
        keep up with worst-case chains; flowing bytes can — every heartbeat
        resets NPM's and the client's idle clocks. heartbeat_seconds=0
        disables (UI brain card)."""
        hb = float(self.brain.cfg.heartbeat_seconds or 0)
        task = asyncio.ensure_future(coro)
        if hb <= 0:
            return await task
        t0 = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=hb)
                if done:
                    return task.result()
                emit("think", f"Still working — {label} has been running "
                              f"{time.monotonic() - t0:.0f}s (cold model loads "
                              f"and long generations can take several minutes)...")
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _heartbeat_events(self, task: asyncio.Task, label: str):
        """Pipeline flavor of _with_heartbeat: async generators can't emit
        into the queue, so keep-alives are YIELDED as events until `task`
        settles — the caller then awaits the task for its result/exception."""
        hb = float(self.brain.cfg.heartbeat_seconds or 0)
        t0 = time.monotonic()
        while True:
            done, _ = await asyncio.wait({task}, timeout=hb if hb > 0 else None)
            if done:
                return
            yield AgentEvent("think", f"Still working — {label} has been running "
                                      f"{time.monotonic() - t0:.0f}s (cold model "
                                      f"loads and long generations can take "
                                      f"several minutes)...")

    # ------------------------------------------------- canonical dispatch --

    async def _dispatch_worker(self, model_id: str, prompt: str,
                               max_tokens: Optional[int] = None,
                               images: Optional[list] = None):
        """THE single worker-dispatch path. Agent ask_* tools, every pipeline
        step, and outcome judges all call models through here, so timeout and
        dispatch behavior can never diverge between execution modes again
        (found live: the pipeline's Execute step failing on cold-load latency
        the ask_* path tolerated fine).

        Also the observed-exhaustion hook (Bug 2 mitigation): Meridian's quota
        sources can be completely blind (oauth null, sdk count 0 — confirmed
        live at 100% used), so an exhaustion-shaped failure on a subscription
        backend is recorded as real signal, and a success clears it."""
        from ..usage import looks_like_window_exhaustion
        info = self.pool.backend_info(model_id)
        is_subscription = bool(info and info.get("type") == "anthropic-compatible")
        # Pre-dispatch context-fit guard (general, any backend): if the request
        # plus the reserved reply wouldn't fit the target model's known context
        # window, reject it HERE rather than send it to earn a raw API error.
        # Critical for escalation — a request the brain assembled against a
        # 262K-context local model may not fit Claude's 200K. context_length is
        # None => unknown => don't gate (degrade gracefully, like the ranking
        # gate). Raises ContextTooLarge (an AllBackendsFailed) so every existing
        # degrade path reroutes to a model that fits.
        reserve = max_tokens or self.brain.cfg.worker_max_tokens
        ctx_limit = (self.model_registry.get(model_id) or {}).get("context_length")
        if ctx_limit:
            est = estimate_tokens(prompt)
            if est + reserve > ctx_limit:
                raise ContextTooLarge(
                    f"request (~{est} prompt tokens + {reserve} reserved for the "
                    f"reply) exceeds {model_id}'s {ctx_limit}-token context window")
        message: dict = {"role": "user", "content": prompt}
        if images:
            message["images"] = images  # protocols translate per wire format
        try:
            result, backend = await self.pool.chat(
                model_id, [message],
                max_tokens=max_tokens or self.brain.cfg.worker_max_tokens)
        except AllBackendsFailed as e:
            # Reliability signal: a failed/timed-out dispatch marks the model
            # down as a within-tier score multiplier (observed, deployment-real).
            self.model_registry.record_call_outcome(model_id, ok=False)
            if is_subscription and looks_like_window_exhaustion(str(e)):
                self.meridian_usage.note_observed_exhaustion(info["url"])
            raise
        if is_subscription:
            self.meridian_usage.note_successful_call(info["url"])
        # Reliability: an empty response is a soft failure (the model produced
        # nothing usable), everything else counts as a good call.
        self.model_registry.record_call_outcome(
            model_id, ok=bool(result.content and result.content.strip()))
        # Observed telemetry: warm tokens/sec (and cold-load time, separately)
        # roll into the registry as measured/observed benchmark signal. No-ops
        # for non-Ollama backends (they report no timing).
        self.model_registry.note_inference(
            model_id, result.completion_tokens,
            result.eval_duration_ns, result.load_duration_ns)
        # Scrub literal <think> tags out of the answer text — they ride to the
        # user verbatim via use_last_result otherwise (found live: a stray
        # ", etc. </think>" rendered as visible content in AnythingLLM). The
        # reasoning joins any native message.thinking the backend separated;
        # callers surface .thinking as narration.
        reasoning, clean = prompts.split_think(result.content)
        result.content = clean
        if reasoning:
            result.thinking = (f"{result.thinking}\n{reasoning}".strip()
                               if result.thinking else reasoning)
        return result, backend

    # ------------------------------------------------------ model pickers --

    def _cheapest_claude(self, exclude_level_above: int = 4) -> Optional[str]:
        """Lowest-tier Claude model currently reachable (Haiku first) — the
        'cheapest adequate paid tier' used by Prepare/Check and the paid
        judge. Conservation guardrails still apply at call time."""
        from ..usage import claude_premium_level
        best, best_level = None, 99
        for model_id in self.pool.available_models():
            info = self.pool.backend_info(model_id)
            if not info or info.get("type") != "anthropic-compatible":
                continue
            level = claude_premium_level(model_id)
            if 1 <= level <= exclude_level_above and level < best_level:
                best, best_level = model_id, level
        return best

    def _best_local(self, category: str, exclude: Optional[str] = None,
                    prefer_measured: bool = False) -> Optional[str]:
        """Best local model for a category by registry score. With
        prefer_measured, rows backed by real benchmark numbers outrank
        estimates (pipeline spec: 'real measured coding benchmark score,
        not just tag presence')."""
        local = [m for m in self.pool.available_models()
                 if m != exclude
                 and (self.pool.backend_info(m) or {}).get("type") == "ollama"]
        if not local:
            return None
        ranked = self.model_registry.ranked_for_category(
            category, local, limit=50, per_tier=50)
        def key(r: dict):
            measured = 0 if (prefer_measured and r.get("score_type") == "measured") \
                else (1 if r.get("score") is not None else 2)
            return (measured, -(r.get("score") or -1))
        ranked.sort(key=key)
        return ranked[0]["id"] if ranked else local[0]

    def _best_permissive(self, category: str, exclude: Optional[str] = None) -> Optional[str]:
        """Best-ranked reachable permissive model for the request-level refusal
        fallback — content_policy=permissive, ranked on merit (neutral mode)
        like everything else, excluding the model that just refused."""
        available = self.pool.available_models()
        ranked = self.model_registry.ranked_for_category(
            category, list(available.keys()), limit=50, per_tier=50)
        for r in ranked:
            if r["id"] != exclude and r.get("content_policy") == "permissive":
                return r["id"]
        return None

    # ------------------------------------------------------ outcome judge --

    async def _judge_outcome(self, ctx: RequestContext, emit, flags: dict,
                             mode: str, effective: dict) -> tuple[bool, str, str]:
        """Configurable adequacy judge (spec §2): 'paid' = cheapest Claude
        tier, 'local_large' = best other local model (genuinely free),
        'brain' = the routing brain itself. Judge failures fail OPEN — a
        broken judge must never block answers."""
        request = self._full_last_user_message(ctx)[:4000]
        answer = (flags.get("last_tool_result") or "")[:6000]
        prompt = prompts.JUDGE_PROMPT.format(request=request, answer=answer)
        emit("think", f"Outcome check ({mode}): judging whether the local answer "
                      f"is adequate...")

        judge_model: Optional[str] = None
        if mode == "paid":
            judge_model = self._cheapest_claude()
            if judge_model:
                verdict = await self.guardrails.check_paid_call(
                    judge_model, self.pool.backend_info(judge_model),
                    self.model_registry.get(judge_model), ctx.guard, effective)
                if not verdict.allowed:
                    emit("think", f"Paid judge unavailable ({verdict.reason}) — "
                                  f"falling back to a large local judge.")
                    judge_model = None
        if judge_model is None and mode in ("paid", "local_large"):
            judge_model = self._best_local(
                (ctx.persona or {}).get("benchmark_category") or "general_chat",
                exclude=flags.get("last_worker_model"))

        db = self.model_registry.db
        try:
            if judge_model:
                result, backend = await self._with_heartbeat(
                    self._dispatch_worker(judge_model, prompt, max_tokens=1024),
                    emit, f"outcome judge {judge_model}")
                text = result.content
                desc = judge_model
                info = self.pool.backend_info(judge_model)
                ctx.logger.record_model_call(judge_model, backend,
                                             result.prompt_tokens,
                                             result.completion_tokens, 0.0)
                if info and info.get("type") == "anthropic-compatible":
                    log_subscription_usage(db, judge_model, backend,
                                           result.prompt_tokens,
                                           result.completion_tokens)
            else:
                text = await self.brain.complete(prompt)
                desc = f"brain:{self.brain.cfg.model}"
        except Exception as e:
            db.log_event("warning", "outcome_judge",
                         "judge call failed — accepting local answer (fail-open)",
                         str(e))
            emit("think", "Outcome judge unavailable — accepting the local answer.")
            return True, f"judge unavailable ({e.__class__.__name__})", mode

        from ..registry.research_agent import extract_json
        data = extract_json(text) or {}
        adequate = bool(data.get("adequate", True))
        reasoning = str(data.get("reasoning") or text[:200]).strip()
        # Observed answer-quality signal: attribute the verdict to the model
        # that produced the judged answer, feeding the `adequacy` benchmark that
        # ranking now weighs. This is the closest thing to ground truth on
        # whether a selection was actually good.
        self.model_registry.record_outcome(flags.get("last_worker_model"), adequate)
        db.log_event("info", "outcome_judge",
                     f"verdict={'adequate' if adequate else 'INADEQUATE'} "
                     f"judge={desc} persona={(ctx.persona or {}).get('virtual_name')}",
                     reasoning[:500])
        return adequate, reasoning, desc

    # -------------------------------------- worker-side tool calling --

    async def _select_tool_worker(self, ctx: RequestContext, persona: dict,
                                  effective: dict):
        """Select the best worker to own the tool loop — the SAME ranking used
        for ordinary dispatch (composite + tool_calling blend + tiebreaks +
        pins/steer/tags), then the first candidate that clears the paid
        guardrail. Selection is unchanged; only what the worker is asked to do
        differs. Returns (model_id, meta, backend_info) or (None, None, None)."""
        category = persona.get("benchmark_category") or "general_chat"
        available = self.pool.available_models()
        overrides = _json_obj(persona.get("selection_weights"))
        permissive_mode = "prefer" if persona.get("prefer_permissive") else "neutral"
        ranked = self.model_registry.ranked_for_category(
            category, list(available.keys()), limit=12, blend_tool_calling=True,
            weights=overrides or None, permissive_mode=permissive_mode)
        ranked = _steer_vision_when_images(ranked, ctx.messages)
        ranked = _filter_required_tags(ranked, persona)
        ranked = _apply_pins(ranked, persona)
        for r in ranked:
            mid = r["id"]
            info = self.pool.backend_info(mid)
            if info is None:
                continue
            meta = self.model_registry.get(mid)
            verdict = await self.guardrails.check_paid_call(
                mid, info, meta, ctx.guard, effective)
            if verdict.allowed:
                return mid, meta, info
            ctx.logger.record_guardrail(f"worker-tools: {mid} denied ({verdict.reason})")
        return None, None, None

    async def run_worker_tools(self, ctx: RequestContext) -> AsyncIterator[AgentEvent]:
        """Opt-out default for tool-attached personas: the brain's job shrinks to
        selecting the best worker and handing off the request + tool schemas; the
        WORKER then owns the tool loop (search, evaluate, decide, repeat) and
        produces the final answer. Rationale: workers are bigger, longer-context,
        and better-reasoning than the 6GB brain, so they should own the loop.
        Any failure hands the request to the brain-mediated path (self.run)."""
        persona = ctx.persona or {}
        effective = self.guardrails.effective(persona)
        yield AgentEvent("think", "Worker-side tool mode: selecting a worker to "
                                  "own the tool loop for this request...")
        worker, meta, info = await self._select_tool_worker(ctx, persona, effective)
        if worker is None:
            yield AgentEvent("think", "No worker cleared selection — handing to the "
                                      "brain to handle tools itself.")
            async for ev in self.run(ctx):
                yield ev
            return

        preferred = set(_json_list(persona.get("preferred_mcp_tools")))
        mcp_tools = [t for t in self.tool_registry.enabled()
                     if t.kind == "mcp" and (t.server in preferred or t.name in preferred)]
        if not mcp_tools:
            # Tools attached but none reachable right now — hand to the brain,
            # which will narrate the same and route without them.
            yield AgentEvent("think", f"No MCP tools reachable for this persona — "
                                      f"handing to the brain.")
            async for ev in self.run(ctx):
                yield ev
            return

        yield AgentEvent("think", f"Handing the request plus {len(mcp_tools)} tool(s) "
                                  f"to {worker} — it will run its own tool loop and "
                                  f"answer directly.")
        async for ev in self._worker_tool_loop(ctx, worker, mcp_tools, effective):
            yield ev

    async def _worker_tool_loop(self, ctx: RequestContext, worker: str,
                                mcp_tools: list, effective: dict):
        """The worker's own bounded tool-call loop. On ANY failure (dispatch
        error, unknown tool, tool execution failure, step-cap exhaustion) the
        original request is handed to the brain-mediated path — the safety net
        while worker tool-calling reliability is still sparse across the fleet."""
        specs = [t.spec() for t in mcp_tools]
        by_name = {t.name: t for t in mcp_tools}
        client_system = "\n\n".join(
            m.get("content") or "" for m in ctx.messages
            if m.get("role") == "system" and (m.get("content") or "").strip())
        messages: list[dict] = [
            {"role": "system", "content": prompts.build_worker_tool_prompt(
                client_system or None)}]
        messages += [m for m in prompts.sanitize_history(ctx.messages)
                     if m.get("role") != "system"]
        images = self._attached_images(ctx)
        cap = int(self.brain.cfg.worker_tool_max_steps or 6)

        async def hand_to_brain(reason: str):
            yield AgentEvent("think", f"{reason} — handing this request to the brain "
                                      f"to complete with its own tool loop.")
            ctx.logger.record_guardrail(f"worker-tools fallback: {reason}")
            async for ev in self.run(ctx):
                yield ev

        for step in range(1, cap + 1):
            task = asyncio.create_task(self.pool.chat(
                worker, messages, tools=specs,
                max_tokens=self.brain.cfg.worker_max_tokens))
            async for hb in self._heartbeat_events(task, f"{worker} (tool step {step}/{cap})"):
                yield hb
            try:
                result, backend = await task
            except AllBackendsFailed as e:
                self.model_registry.record_call_outcome(worker, ok=False)
                async for ev in hand_to_brain(f"{worker} dispatch failed "
                                               f"({describe_exception(e)})"):
                    yield ev
                return
            self.model_registry.record_call_outcome(
                worker, ok=bool(result.content and result.content.strip()))
            self.model_registry.note_inference(
                worker, result.completion_tokens,
                result.eval_duration_ns, result.load_duration_ns)

            if not result.tool_calls:
                # No tool call => the worker produced its final answer.
                reasoning, clean = prompts.split_think(result.content)
                if reasoning.strip():
                    emit_txt = reasoning[:2000] + ("…" if len(reasoning) > 2000 else "")
                    yield AgentEvent("think", f"[{worker} reasoning] {emit_txt}")
                ctx.logger.record_model_call(worker, backend, result.prompt_tokens,
                                             result.completion_tokens, 0.0)
                yield AgentEvent("think", f"{worker} finished after {step - 1} tool "
                                          f"call(s).")
                yield AgentEvent("answer", clean.strip() or "(worker returned empty output)")
                return

            # The worker asked to call tools — execute each through the SAME MCP
            # infrastructure the brain uses (so request_log.tool_calls logging is
            # identical regardless of who initiated the call).
            messages.append({"role": "assistant", "content": result.content or "",
                             "tool_calls": [
                                 {"id": tc["id"], "type": "function",
                                  "function": {"name": tc["name"],
                                               "arguments": tc["arguments"]}}
                                 for tc in result.tool_calls]})
            for tc in result.tool_calls:
                tool = by_name.get(tc["name"])
                if tool is None:
                    self.model_registry.record_tool_call(worker, ok=False)
                    async for ev in hand_to_brain(
                            f"{worker} called an unavailable tool "
                            f"({tc['name']!r})"):
                        yield ev
                    return
                yield AgentEvent("think", f"{worker} → {tool.server}/{tool.mcp_tool}...")
                t0 = time.monotonic()
                try:
                    out = await self.tool_registry.mcp.call_tool(
                        tool.server, tool.mcp_tool, tc["arguments"])
                except Exception as e:
                    dur = int((time.monotonic() - t0) * 1000)
                    ctx.logger.record_tool_call(tc["name"], tool.server, dur,
                                                ok=False, error=describe_exception(e))
                    self.model_registry.record_tool_call(worker, ok=False)
                    async for ev in hand_to_brain(
                            f"{worker}'s {tool.server} call failed "
                            f"({describe_exception(e)})"):
                        yield ev
                    return
                dur = int((time.monotonic() - t0) * 1000)
                ctx.logger.record_tool_call(tc["name"], tool.server, dur, ok=True)
                self.model_registry.record_tool_call(worker, ok=True)
                yield AgentEvent("think", f"{tool.server}/{tool.mcp_tool} returned "
                                          f"{len(out)} chars in {dur}ms.")
                messages.append({"role": "tool", "name": tc["name"],
                                 "tool_call_id": tc.get("id"),
                                 "content": out[:self.brain.cfg.mcp_result_limit_chars]
                                            or "(empty tool result)"})

        # Step cap hit without a final answer.
        async for ev in hand_to_brain(f"{worker} hit the {cap}-step tool cap"):
            yield ev

    # ---------------------------------------------- coding pipeline (§1) --

    async def run_pipeline(self, ctx: RequestContext) -> AsyncIterator[AgentEvent]:
        """Prepare -> Execute -> Check: a distinct execution mode, not a
        variant of the generic brain loop. The cheapest adequate Claude tier
        structures the request, the best MEASURED local coder executes, and
        (unless the persona opts out) the paid tier reviews — one bounded
        retry on real problems, never a loop."""
        from ..registry.research_agent import extract_json

        persona = ctx.persona or {}
        effective = self.guardrails.effective(persona)
        user_text = self._full_last_user_message(ctx)
        db = self.model_registry.db
        yield AgentEvent("think", "Coding pipeline: Prepare → Execute → Check")

        async def paid_call(model_id: str, prompt: str, purpose: str):
            """Guardrail-checked paid step; returns content or None (skipped)."""
            verdict = await self.guardrails.check_paid_call(
                model_id, self.pool.backend_info(model_id),
                self.model_registry.get(model_id), ctx.guard, effective)
            if not verdict.allowed:
                ctx.logger.record_guardrail(f"pipeline {purpose}: {verdict.reason}")
                return None, f"{purpose} skipped — {verdict.reason}"
            t0 = time.monotonic()
            result, backend = await self._dispatch_worker(model_id, prompt,
                                                          max_tokens=4096)
            ctx.logger.record_model_call(model_id, backend, result.prompt_tokens,
                                         result.completion_tokens, 0.0)
            log_subscription_usage(db, model_id, backend,
                                   result.prompt_tokens, result.completion_tokens)
            return result.content, f"{purpose} done in {time.monotonic() - t0:.0f}s"

        # -- Prepare ---------------------------------------------------------
        spec = user_text
        claude = self._cheapest_claude()
        if claude:
            yield AgentEvent("think", f"Prepare: structuring the request via {claude}...")
            try:
                content, note = await paid_call(
                    claude, prompts.PIPELINE_PREPARE.format(request=user_text[:12000]),
                    "prepare")
                yield AgentEvent("think", f"Prepare: {note}.")
                if content and content.strip():
                    spec = content.strip()
            except AllBackendsFailed as e:
                yield AgentEvent("think", f"Prepare failed ({e}) — executing from "
                                          f"the raw request.")
        else:
            yield AgentEvent("think", "No Claude tier reachable — executing from "
                                      "the raw request.")

        # -- Execute ----------------------------------------------------------
        # Failure discipline (found live: transient transport error during a
        # normal 70s cold model load killed the whole request, with the real
        # exception text swallowed): retry the local coder ONCE, then degrade
        # to the paid tier, and always end with clean user-facing text — never
        # a raw internal error string in the chat.
        UNREACHABLE = ("The coding pipeline could not reach any model to execute "
                       "this request — details are in the Events log. Please "
                       "retry shortly.")

        async def paid_execute_or_apology():
            if claude:
                yield_events = []
                try:
                    content, note = await paid_call(claude, spec, "execute(paid)")
                    yield_events.append(AgentEvent(
                        "answer", content if content and content.strip() else UNREACHABLE))
                except AllBackendsFailed as e:
                    db.log_event("error", "pipeline",
                                 "paid execute fallback failed too", str(e))
                    yield_events.append(AgentEvent("answer", UNREACHABLE))
                return yield_events
            db.log_event("error", "pipeline",
                         "no local coder and no Claude tier reachable")
            return [AgentEvent("answer", UNREACHABLE)]

        executor = self._best_local("coding", prefer_measured=True)
        if executor is None:
            yield AgentEvent("think", "No local coding model reachable — "
                                      "executing on the paid tier directly.")
            for ev in await paid_execute_or_apology():
                yield ev
            return

        exec_prompt = prompts.PIPELINE_EXECUTE.format(
            spec=spec, request=user_text[:8000])
        code = None
        for attempt in (1, 2):
            yield AgentEvent("think", f"Execute: dispatching to {executor} (best "
                                      f"measured local coding score"
                                      + (", retry" if attempt == 2 else "")
                                      + ") — waiting on generation...")
            t0 = time.monotonic()
            task = asyncio.ensure_future(self._dispatch_worker(executor, exec_prompt))
            async for hb_ev in self._heartbeat_events(task, f"Execute on {executor}"):
                yield hb_ev
            try:
                result, backend = await task
            except AllBackendsFailed as e:
                if attempt == 1:
                    yield AgentEvent("think", f"Execute attempt failed ({e}) — "
                                              f"retrying once (cold model loads can "
                                              f"trip transport timeouts)...")
                    continue
                db.log_event("error", "pipeline",
                             f"execute failed twice on {executor}", str(e))
                yield AgentEvent("think", f"Execute failed twice on {executor} "
                                          f"({e}) — degrading to the paid tier.")
                for ev in await paid_execute_or_apology():
                    yield ev
                return
            code = result.content
            ctx.logger.record_model_call(executor, backend, result.prompt_tokens,
                                         result.completion_tokens, 0.0)
            if result.thinking:
                yield AgentEvent("think", f"[{executor} reasoning] "
                                          f"{result.thinking[:2000]}"
                                          + ("…" if len(result.thinking) > 2000 else ""))
            yield AgentEvent("think", f"Execute: {executor} responded in "
                                      f"{time.monotonic() - t0:.0f}s "
                                      f"({len(code)} chars).")
            break

        # -- Check ------------------------------------------------------------
        final = code
        check_on = persona.get("pipeline_check_enabled")
        check_on = True if check_on is None else bool(check_on)
        if check_on and claude and code.strip():
            yield AgentEvent("think", f"Check: {claude} reviewing the output "
                                      f"against the original request...")
            try:
                review_raw, note = await paid_call(
                    claude, prompts.PIPELINE_REVIEW.format(
                        request=user_text[:6000], code=code[:16000]), "check")
                yield AgentEvent("think", f"Check: {note}.")
                review = extract_json(review_raw or "") or {}
                if review and not review.get("adequate", True):
                    feedback = str(review.get("feedback") or "").strip()
                    yield AgentEvent("think", f"Check found problems — one retry "
                                              f"with feedback: {feedback[:300]}")
                    ctx.logger.record_guardrail("pipeline check: retry issued")
                    try:
                        retry, backend = await self._dispatch_worker(
                            executor, prompts.PIPELINE_REVISE.format(
                                spec=spec[:6000], code=code[:12000],
                                feedback=feedback[:4000]))
                        ctx.logger.record_model_call(
                            executor, backend, retry.prompt_tokens,
                            retry.completion_tokens, 0.0)
                        if retry.content.strip():
                            final = retry.content
                            yield AgentEvent("think", "Retry complete — delivering "
                                                      "the revised version.")
                    except AllBackendsFailed:
                        # Bounded degradation: forward original WITH the caveats.
                        final = (code + "\n\n---\nREVIEWER CAVEATS (retry "
                                        "unavailable):\n" + feedback)
                        yield AgentEvent("think", "Retry unavailable — forwarding "
                                                  "with reviewer caveats attached.")
                elif review:
                    yield AgentEvent("think", "Check passed — no real problems found.")
            except AllBackendsFailed as e:
                yield AgentEvent("think", f"Check unavailable ({e}) — delivering "
                                          f"unreviewed output.")
        elif not check_on:
            yield AgentEvent("think", "Check disabled for this persona "
                                      "(pipeline_check_enabled=false).")

        yield AgentEvent("answer", final)
