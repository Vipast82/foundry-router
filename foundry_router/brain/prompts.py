"""System-prompt construction for the routing brain.

Everything variable comes in as data (persona row, registry rankings, window
state) — the prompt is assembled per request so a registry update or persona
edit changes behavior on the very next request with no restart.
"""

from __future__ import annotations

import json
import re
from typing import Optional

# Hidden marker embedded in ask_user responses (§4.6). HTML comments render as
# nothing in every chat client, but survive round-trips in conversation
# history, which is the entire session-state mechanism — no server-side state.
PENDING_MARKER_PREFIX = "<!--foundry-router:pending_question "
PENDING_MARKER_SUFFIX = "-->"
_PENDING_RE = re.compile(
    re.escape(PENDING_MARKER_PREFIX) + r"(.*?)" + re.escape(PENDING_MARKER_SUFFIX),
    re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def make_pending_marker(question: str) -> str:
    return PENDING_MARKER_PREFIX + json.dumps({"question": question[:500]}) + PENDING_MARKER_SUFFIX


def find_pending_question(messages: list[dict]) -> Optional[str]:
    """Scan backwards for the most recent assistant message; if it carries the
    marker, the new user message is the answer to that question (§4.6)."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            match = _PENDING_RE.search(m.get("content") or "")
            if match:
                try:
                    return json.loads(match.group(1)).get("question")
                except json.JSONDecodeError:
                    return None
            return None
    return None


def sanitize_history(messages: list[dict]) -> list[dict]:
    """Strip <think> narration and hidden markers from history before feeding
    it back to the brain — clients echo our own output back to us, and routing
    narration from past turns is token-expensive noise for a small model."""
    out = []
    for m in messages:
        content = m.get("content") or ""
        if m.get("role") == "assistant":
            content = _THINK_RE.sub("", content)
            content = _COMMENT_RE.sub("", content)
        out.append({**m, "content": content.strip()})
    return [m for m in out if m["content"] or m.get("tool_calls")]


REFINE_INSTRUCTION = """Rewrite the following request as an explicit, well-scoped \
specification for another AI model that will not see any other context. Tighten vague \
wording into concrete scope, state reasonable assumptions explicitly, and keep it short. \
Do NOT add requirements the user didn't imply. Reply with ONLY the rewritten request.

Request:
{request}"""


def _model_line(row: dict, tool_name: str) -> str:
    score = f"{row['score']:.0f}" if row.get("score") is not None else "?"
    stype = f" ({row['score_type']})" if row.get("score_type") else ""
    bits = [f"- {tool_name}: score {score}{stype}"]
    try:
        tags = json.loads(row.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    if tags:
        bits.append("tags: " + ", ".join(str(t) for t in tags))
    if row.get("content_policy") == "permissive":
        bits.append("PERMISSIVE (handles requests standard-alignment models decline)")
    ok = row.get("tool_calls_ok") or 0
    failed = row.get("tool_calls_failed") or 0
    if ok + failed >= 3 and failed and ok / (ok + failed) < 0.9:
        bits.append(f"WARNING: tool-calling unreliable in practice "
                    f"({ok}/{ok + failed} calls ok)")
    if row.get("good_for"):
        bits.append(f"good for: {row['good_for']}")
    if row.get("benefits_from_explicit_prompting"):
        bits.append("benefits from refine_prompt")
    return "; ".join(bits)


# Display order + labels for the tier-grouped candidate list. Cost tier is
# assigned at ingestion (local backends => free); unknown means no registry
# data yet — treated as mid-tier paid so the brain stays conservative.
_TIER_DISPLAY = [
    ("free", "FREE / LOCAL — always try here first"),
    ("low", "CHEAP PAID"),
    ("medium", "MID PAID"),
    ("high", "PREMIUM (subscription window / expensive)"),
    ("very_high", "PREMIUM+ (most expensive)"),
    (None, "UNKNOWN COST — treat as mid-tier paid"),
]


def _models_block(ranked: list[dict], tool_name_for: dict[str, str]) -> str:
    pinned_lines: list[str] = []
    groups: dict = {}
    for row in ranked:
        tool_name = tool_name_for.get(row["id"])
        if not tool_name:
            continue
        if row.get("_pinned"):
            pinned_lines.append(_model_line(row, tool_name))
            continue
        tier = row.get("relative_cost_tier")
        tier = tier if tier in dict(_TIER_DISPLAY) else None
        groups.setdefault(tier, []).append(_model_line(row, tool_name))
    blocks = []
    if pinned_lines:
        blocks.append("[PINNED FOR THIS PERSONA — boosted defaults, in priority "
                      "order; not mandatory]\n" + "\n".join(pinned_lines))
    blocks += [f"[{label}]\n" + "\n".join(groups[tier])
               for tier, label in _TIER_DISPLAY if tier in groups]
    return "\n".join(blocks) or "- (no models currently reachable)"


def build_system_prompt(persona: Optional[dict], ranked: list[dict],
                        tool_name_for: dict[str, str], meridian_note: str,
                        pending_question: Optional[str],
                        client_system: Optional[str] = None) -> str:
    p = persona or {}
    name = p.get("virtual_name", "Foundry-Chat")
    triggers = p.get("escalation_triggers") or "[]"
    try:
        triggers_list = json.loads(triggers)
    except (json.JSONDecodeError, TypeError):
        triggers_list = []

    models_block = _models_block(ranked, tool_name_for)

    parts = [f"""You are the routing brain (dispatcher) of Foundry Router. You are a \
small local model with a training cutoff and no live knowledge. You NEVER answer the \
user from your own knowledge — not facts, not current events, not code, not claims \
about real people. Your entire job each turn: understand the request, pick the best \
worker model or tool for it, dispatch a complete self-contained prompt, and forward \
the worker's result back. Every substantive statement the user reads must have come \
from a worker model, never from you.

ACTIVE PERSONA: {name} — {p.get('description', '')}
- Local bias: {p.get('local_bias_strength', 'cost_aware_default')} \
(strong = stay local unless an escalation trigger clearly applies; \
cost_aware_default = weigh quality gain against cost; moderate = in between)
- Task category to optimize for: {p.get('benchmark_category', 'general_chat')}
- Escalation triggers for this persona: {json.dumps(triggers_list)}

CANDIDATE MODELS grouped by cost tier (cheapest tier first; best score first \
within a tier; scores 0-100, '?' = unknown):
{models_block}

CLAUDE USAGE WINDOW: {meridian_note}

SELECTION PROCEDURE (apply in order — this is structural policy, follow it \
even when a pricier model would also work):
a0. If a [PINNED FOR THIS PERSONA] group is listed, try those first, in order \
— they are boosted, not hard-required: if one is denied by a guardrail, \
unavailable, or clearly unsuited, fall through to the next pin, then continue \
below.
a. Start in the FREE/LOCAL tier: pick the model whose tags match the task; \
break ties by score.
b. Escalate to a paid tier ONLY when the task genuinely exceeds every local \
candidate (a persona escalation trigger applies, or a local attempt already \
failed) — and then use the CHEAPEST tier that suffices. Never reach for a \
premium model when a cheap or local one would do.
c. Within any tier, prefer a model whose tags match the task over a \
higher-scored mismatch.
d. If the task specifically needs permissive/uncensored handling, prefer a \
local model marked PERMISSIVE over any paid escalation — paid models' \
behavior is fixed regardless of how you route.
e. Guardrails apply to every choice. If one denies a model, take the next \
candidate down the list — do not give up.
f. Watch CLAUDE USAGE WINDOW above: as it fills, escalate to progressively \
cheaper Claude tiers for the same task (Sonnet instead of Opus, Haiku instead \
of Sonnet) — the guardrails enforce this too, but choosing well up front \
avoids wasted denials.
g. CLAUDE TIER GUIDE (when escalating, pick the LOWEST tier that fits):
   - Haiku: quick answers, summaries, high-volume simple work.
   - Sonnet: everyday strong coding and analysis — the default escalation.
   - Opus: complex debugging, multi-file architecture, hard review — covers \
almost every "really hard" task.
   - Fable/Mythos: LAST resort for the very hardest work Opus demonstrably \
cannot handle. It draws a separate, much tighter weekly budget (its own \
"Fable" window above) — do not spend it on tasks Opus can do.

RULES:
1. Every turn MUST end with exactly one of: return_to_user (deliver the result) or \
ask_user (one genuinely necessary clarifying question). Never stop without one.
2. NEVER author the user's answer yourself — not even for questions you are sure \
about; your knowledge is stale and unverifiable. Every answer, fact, explanation, and \
piece of code must come from a worker model via an ask_<model> tool and be forwarded \
with return_to_user(use_last_result=true). return_to_user's answer field is reserved \
for short router status notes only (e.g. "no backend can serve this request").
3. Questions touching real-world facts, current events, dates, or real people: prefer \
a worker with search/research tools if one is in your tool list; otherwise the \
strongest available general model. If the request needs live information nothing here \
can fetch, still dispatch — and instruct the worker to state its knowledge cutoff \
plainly in its answer.
4. Forward outputs verbatim with use_last_result=true. NEVER retype or write code or \
multi-line content inside tool arguments — you will run out of output budget mid-JSON \
and the call will fail.
5. Simple requests: ONE well-chosen dispatch with a complete, self-contained prompt \
(the worker sees nothing else). Multi-step work (design -> implement -> review) is \
allowed but each paid call must earn its cost.
6. refine_prompt vs ask_user are different tools for different situations: if a vague \
request can be confidently tightened yourself, refine_prompt; if it is genuinely \
ambiguous (you would be guessing user intent), ask_user. Do not use refine_prompt to \
avoid asking a necessary question.
7. Prefer refine_prompt before dispatching to a model marked 'benefits from refine_prompt'.
8. If a model has no data ('?' score), you may call request_model_research(model_name) — \
it is non-blocking and helps FUTURE routing; still make today's decision with what you \
have, treating the model as unknown capability and moderate cost.
9. If a tool call fails, react: try another backend/model or forward what you already \
have. Do not retry the same failing call more than once.
10. NARRATE: alongside every ask_<model> call, write ONE short line of plain text \
saying which model you chose and why (e.g. "Best local coding score — keeping it \
free."). It streams to the user live as status while they wait. One line, no more."""]

    if client_system:
        # Workspace/client system instructions can't appear as a second
        # system message (brain templates require exactly one, first), so
        # they ride here — and must be relayed into worker prompts, since
        # worker models only ever see prompts the brain writes.
        parts.append(
            f"\nCLIENT WORKSPACE INSTRUCTIONS (from the connecting client's own "
            f"system prompt — honor them in your routing and INCLUDE the relevant "
            f"parts in every prompt you write for a worker model, since workers "
            f"never see them otherwise):\n{client_system[:4000]}")

    if pending_question:
        parts.append(
            f"\nRESUMING: on the previous turn you asked the user: {pending_question!r}. "
            f"The newest user message is their answer. Do not re-ask; resume the "
            f"original task with this new information.")
    return "\n".join(parts)


CORE_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "return_to_user",
        "description": "Finish the request and deliver the final result. NORMAL use: "
                       "set use_last_result=true to forward the most recent worker "
                       "output verbatim. The answer field is ONLY for short router "
                       "status notes (e.g. 'no backend can serve this request') — "
                       "never author substantive content, facts, or code in it "
                       "yourself; answers must come from a worker model.",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string",
                       "description": "Short router status note ONLY (ignored if "
                                      "use_last_result is true). Not for answers."},
            "use_last_result": {"type": "boolean", "description": "Forward the last worker output verbatim."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Pause and ask the user ONE clarifying question. Only for genuine "
                       "ambiguity that would otherwise force you to guess their intent.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "refine_prompt",
        "description": "Rewrite/tighten a vague request into an explicit spec before "
                       "dispatch. Use when you can confidently fill in the gaps yourself "
                       "(especially before models flagged as benefiting from explicit "
                       "prompting). The refined text is shown to the user before dispatch.",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "The request text to refine."}},
            "required": ["request"]}}},
    {"type": "function", "function": {
        "name": "request_model_research",
        "description": "Non-blocking: queue background research on a model with missing/"
                       "stale registry data. Returns immediately; results inform FUTURE "
                       "routing decisions, not this one.",
        "parameters": {"type": "object", "properties": {
            "model_name": {"type": "string"}}, "required": ["model_name"]}}},
]

CORE_TOOL_NAMES = {t["function"]["name"] for t in CORE_TOOL_SPECS}
