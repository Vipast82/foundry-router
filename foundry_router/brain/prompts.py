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
    tier = row.get("relative_cost_tier") or ("free/local" if not row.get("cost_per_1k_output") else "?")
    bits = [f"- {tool_name}: score {score}{stype}, cost {tier}"]
    if row.get("good_for"):
        bits.append(f"good for: {row['good_for']}")
    if row.get("benefits_from_explicit_prompting"):
        bits.append("benefits from refine_prompt")
    return "; ".join(bits)


def build_system_prompt(persona: Optional[dict], ranked: list[dict],
                        tool_name_for: dict[str, str], meridian_note: str,
                        pending_question: Optional[str]) -> str:
    p = persona or {}
    name = p.get("virtual_name", "Foundry-Chat")
    triggers = p.get("escalation_triggers") or "[]"
    try:
        triggers_list = json.loads(triggers)
    except (json.JSONDecodeError, TypeError):
        triggers_list = []

    model_lines = []
    for row in ranked:
        tn = tool_name_for.get(row["id"])
        if tn:
            model_lines.append(_model_line(row, tn))
    models_block = "\n".join(model_lines) or "- (no models currently reachable)"

    parts = [f"""You are the routing brain of Foundry Router. You do not answer the user \
yourself — you decide which model or tool handles the work, dispatch it, and hand the \
result back. Your own output is never shown to the user except through your tool calls.

ACTIVE PERSONA: {name} — {p.get('description', '')}
- Local bias: {p.get('local_bias_strength', 'cost_aware_default')} \
(strong = stay local unless an escalation trigger clearly applies; \
cost_aware_default = weigh quality gain against cost; moderate = in between)
- Task category to optimize for: {p.get('benchmark_category', 'general_chat')}
- Escalation triggers for this persona: {json.dumps(triggers_list)}

CANDIDATE MODELS (best first for this category; scores 0-100, '?' = unknown):
{models_block}

CLAUDE USAGE WINDOW: {meridian_note}

RULES:
1. Every turn MUST end with exactly one of: return_to_user (deliver the answer) or \
ask_user (a genuinely necessary clarifying question). Never stop without one.
2. To forward a model's output verbatim as the final answer, call \
return_to_user with use_last_result set to true instead of retyping it — retyping \
long outputs truncates them.
3. Simple requests: dispatch to ONE well-chosen model with a complete, self-contained \
prompt (the model sees nothing else), then return its answer. Multi-step work \
(design -> implement -> review) is allowed but each paid call must earn its cost.
4. refine_prompt vs ask_user are different tools for different situations: if a vague \
request can be confidently tightened yourself, refine_prompt; if it is genuinely \
ambiguous (you would be guessing user intent), ask_user. Do not use refine_prompt to \
avoid asking a necessary question.
5. Prefer refine_prompt before dispatching to a model marked 'benefits from refine_prompt'.
6. If a model has no data ('?' score), you may call request_model_research(model_name) — \
it is non-blocking and helps FUTURE routing; still make today's decision with what you \
have, treating the model as unknown capability and moderate cost.
7. If a tool call fails, react: try another backend/model or return what you have. \
Do not retry the same failing call more than once.
8. Be decisive and terse. Any plain text you produce is shown to the user only as \
collapsed routing narration."""]

    if pending_question:
        parts.append(
            f"\nRESUMING: on the previous turn you asked the user: {pending_question!r}. "
            f"The newest user message is their answer. Do not re-ask; resume the "
            f"original task with this new information.")
    return "\n".join(parts)


CORE_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "return_to_user",
        "description": "Finish the request and deliver the final answer to the user. "
                       "Set use_last_result=true to forward the most recent tool result "
                       "verbatim (preferred for long outputs).",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string", "description": "Final answer text (ignored if use_last_result is true)."},
            "use_last_result": {"type": "boolean", "description": "Forward the last tool result verbatim."}},
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
