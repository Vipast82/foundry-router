"""System-prompt construction for the routing brain.

Everything variable comes in as data (persona row, registry rankings, window
state) — the prompt is assembled per request so a registry update or persona
edit changes behavior on the very next request with no restart.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

# ask_user session state (§4.6). The old design embedded an HTML-comment marker
# in the assistant's content so it survived round-trips in conversation history
# (no server-side state). But HTML comments are NOT universally hidden — found
# live: AnythingLLM renders the raw marker into visible text, duplicating the
# question. So the marker never goes into content anymore; state is kept
# server-side in `kv`, keyed by a fingerprint of the conversation's USER
# messages (which clients echo back verbatim, unlike assistant text they may
# reformat/strip). PENDING_MARKER_PREFIX and _PENDING_RE are retained only so
# sanitize_history still scrubs any legacy marker echoed back from an old turn.
PENDING_MARKER_PREFIX = "<!--foundry-router:pending_question "
PENDING_MARKER_SUFFIX = "-->"
_PENDING_RE = re.compile(
    re.escape(PENDING_MARKER_PREFIX) + r"(.*?)" + re.escape(PENDING_MARKER_SUFFIX),
    re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def _conversation_fingerprint(messages: list[dict], drop_last_user: bool = False) -> str:
    """Stable key over the USER-message contents (clients preserve those). At
    ask_user time the conversation is [U1..Un]; the resuming request is
    [U1..Un, assistant-question, U(reply)] — dropping its last user message
    yields the same [U1..Un], so the fingerprints match."""
    users = [m.get("content") or "" for m in messages if m.get("role") == "user"]
    if drop_last_user and users:
        users = users[:-1]
    return hashlib.sha256("".join(users).encode("utf-8")).hexdigest()[:32]


def store_pending_question(db, messages: list[dict], question: str) -> None:
    db.kv_set(f"pending_q:{_conversation_fingerprint(messages)}",
              json.dumps({"question": question[:500]}))


def find_pending_question(db, messages: list[dict]) -> Optional[str]:
    """Look up (and consume) a pending question for the resuming conversation.
    Server-side now — nothing is read from message content."""
    key = f"pending_q:{_conversation_fingerprint(messages, drop_last_user=True)}"
    raw = db.kv_get(key)
    if not raw:
        return None
    db.kv_del(key)  # consume — a question is answered at most once
    try:
        return json.loads(raw).get("question")
    except json.JSONDecodeError:
        return None


def store_pending_paid(db, messages: list[dict], payload: dict) -> None:
    """Server-side state for the paid-usage confirmation handshake (same
    fingerprint mechanism as pending questions): payload carries the user's
    requested target + matching model ids so the resuming request can restore
    the steering and read the yes/no reply."""
    db.kv_set(f"pending_paid:{_conversation_fingerprint(messages)}",
              json.dumps(payload))


def find_pending_paid(db, messages: list[dict]) -> Optional[dict]:
    """Look up (and consume) a pending paid-usage confirmation for the
    resuming conversation."""
    key = f"pending_paid:{_conversation_fingerprint(messages, drop_last_user=True)}"
    raw = db.kv_get(key)
    if not raw:
        return None
    db.kv_del(key)  # consume — a confirmation is answered at most once
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None


# Output-style steering (quality spec Phase 5): the one BEHAVIORAL piece of
# client-aware profiles. plain_text serves messaging-platform bridges
# (Hermes/OpenClaw) that can't render HTML at all — everything else is
# documentation metadata (personas.client_compat), not formatting code.
PLAIN_TEXT_STYLE = (
    "OUTPUT STYLE — PLAIN TEXT: this persona serves clients (messaging-"
    "platform bridges) that cannot render HTML or rich markup. Ensure every "
    "answer — and every worker prompt you write — calls for clean plain text "
    "or simple markdown: no HTML/SVG, no wide tables, code blocks only when "
    "code itself is the answer. For rich or visual content, describe it "
    "plainly or reference a generated file/URL instead of embedding markup.")


def build_worker_tool_prompt(client_system: Optional[str] = None,
                             output_style: Optional[str] = None) -> str:
    """System prompt for worker-side tool calling: the selected worker owns the
    tool loop for this request (search, read results, decide, repeat) and
    produces the final answer itself, instead of the brain doing that work and
    handing off only a synthesis task."""
    parts = [
        "You are answering the user's request directly. You have tools available "
        "(web search, page fetch, etc.). Use them as needed: call a tool, read "
        "its result, and decide whether to call another tool or answer.",
        # Steer toward actually reading pages, and toward the crawler for it —
        # models otherwise lean entirely on search snippets and never open a
        # page (found live: SearXNG used constantly, Crawl4AI never).
        "Tool strategy: use a web-SEARCH tool to find relevant pages, then — when "
        "a search snippet is not enough to answer accurately — OPEN the most "
        "relevant result and read its full content. For reading a full page, "
        "PREFER a crawler tool (e.g. a crawl4ai markdown/crawl tool) over a plain "
        "URL fetch: crawlers render JavaScript and are far less likely to be "
        "blocked by bot detection. Don't answer a factual/current question from "
        "search snippets alone if opening the top source would make it correct.",
        "When you have gathered enough information, STOP calling tools and write "
        "your complete final answer as ordinary text (no tool call). Do not "
        "narrate that you are about to answer — just answer.",
        "Be efficient: only call tools that genuinely help, and prefer to finish "
        "in as few tool calls as possible.",
    ]
    if output_style == "plain_text":
        parts.append(PLAIN_TEXT_STYLE)
    if client_system and client_system.strip():
        parts.append("The user's workspace provided these instructions; honor "
                     f"them:\n{client_system[:4000]}")
    return "\n\n".join(parts)


_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_think(text: str) -> tuple[str, str]:
    """Separate a model's literal <think> reasoning from its answer text.

    Reasoning workers (qwen3.6 etc.) sometimes emit <think> tags directly in
    content — the backend's think-parsing misfired, or the model re-emits a
    stray closing tag after the parser already consumed the opener. Found
    live: a stray ", etc. </think>" rendered as visible answer text in
    AnythingLLM. Nothing in the router writes these tags; they arrive in
    worker output, so they're scrubbed at the dispatch layer and rerouted to
    the NATIVE thinking field.

    Handles, in order: complete <think>...</think> blocks; a dangling
    </think> with no opener (everything before it is the reasoning tail);
    a dangling <think> opener (tag stripped, text kept — hiding a possible
    answer is worse than showing unlabeled reasoning). If scrubbing would
    leave the answer EMPTY (model wrapped its whole reply in think tags),
    the reasoning is returned as the answer instead: an empty reply is the
    one outcome always worse than an unpolished one.

    Returns (reasoning, clean_answer)."""
    if "<think>" not in text and "</think>" not in text:
        return "", text
    thinking: list[str] = []
    rest = _THINK_BLOCK_RE.sub(lambda m: (thinking.append(m.group(1).strip()), "")[1],
                               text)
    if "</think>" in rest:  # dangling closer — opener consumed upstream
        head, _, rest = rest.partition("</think>")
        if head.strip():
            thinking.append(head.strip())
    rest = rest.replace("<think>", "").strip()
    reasoning = "\n".join(t for t in thinking if t)
    if not rest and reasoning:
        return "", reasoning
    return reasoning, rest


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
    # image-only messages (photo with no caption) must survive the filter
    return [m for m in out if m["content"] or m.get("tool_calls") or m.get("images")]


# ---- Outcome judge (cross-cutting escalation mechanism) ----------------------

JUDGE_PROMPT = """You are judging whether an answer adequately serves a request. \
Be pragmatic: adequate means correct, complete enough to use, and on-topic — not \
perfect. Reply ONLY with JSON: {{"adequate": true/false, "reasoning": "<one or two \
sentences>"}}

REQUEST:
{request}

ANSWER TO JUDGE:
{answer}"""

# ---- Tiered review pass (quality spec Phase 2) --------------------------------

REVIEW_PREFILTER_PROMPT = """You are a fast pre-filter deciding whether an \
answer needs a closer review by a stronger model. Reply ONLY with JSON: \
{{"review": true/false, "reason": "<one sentence>"}}. review=true if the answer \
might contain factual errors, unfulfilled parts of the request, broken code, or \
internal contradictions; false if it plainly and completely serves the request.

REQUEST:
{request}

ANSWER:
{answer}"""

REVIEW_PASS_PROMPT = """Review the following answer against the request. Look \
for real problems only: incorrect facts, missing requirements, broken code, \
contradictions — not style preferences. Reply ONLY with JSON. \
If the answer is adequate: {{"adequate": true, "notes": "<one sentence>"}}. \
If it has real problems: {{"adequate": false, "notes": "<the specific \
problems>", "corrected_answer": "<the COMPLETE corrected answer, full text — \
it replaces the original verbatim>"}}.

REQUEST:
{request}

ANSWER TO REVIEW:
{answer}"""


def review_marker(model: str) -> str:
    """Visible transparency marker appended to any answer the review pass
    changed (same convention as the ⚡ loaded badge): a correction must never
    be silent."""
    return f"\n\n---\n🔎 *Corrected by review pass ({model})*"


# ---- Coding pipeline (Prepare -> Execute -> Check) -----------------------------

PIPELINE_PREPARE = """Turn the following raw coding request into a precise, \
structured prompt for a code-generation model — like a good engineering ticket: \
clarified requirements, concrete edge cases, relevant constraints (language, \
runtime, style), and what "done" looks like. Do NOT write the code yourself. \
Reply with only the structured prompt.

RAW REQUEST:
{request}"""

PIPELINE_EXECUTE = """{spec}

---
ORIGINAL USER REQUEST (verbatim, for reference):
{request}"""

PIPELINE_REVIEW = """Review the following code against the original request. \
Look for real problems only: incorrect behavior, missing requirements, bugs, \
security issues — not style nitpicks. Reply ONLY with JSON: \
{{"adequate": true/false, "feedback": "<specific, actionable problems, or empty>"}}

ORIGINAL REQUEST:
{request}

CODE TO REVIEW:
{code}"""

PIPELINE_REVISE = """Your previous output was reviewed and specific problems were \
found. Fix them and output the complete corrected version (full code, not a diff).

SPECIFICATION:
{spec}

YOUR PREVIOUS OUTPUT:
{code}

REVIEW FEEDBACK (fix all of these):
{feedback}"""


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
    if row.get("_user_requested"):
        bits.append("REQUESTED BY THE USER this turn")
    if row.get("_loaded"):
        bits.append("⚡ already loaded in VRAM")
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
    requested_lines: list[str] = []
    pinned_lines: list[str] = []
    groups: dict = {}
    for row in ranked:
        tool_name = tool_name_for.get(row["id"])
        if not tool_name:
            continue
        if row.get("_user_requested"):
            requested_lines.append(_model_line(row, tool_name))
            continue
        if row.get("_pinned"):
            pinned_lines.append(_model_line(row, tool_name))
            continue
        tier = row.get("relative_cost_tier")
        tier = tier if tier in dict(_TIER_DISPLAY) else None
        groups.setdefault(tier, []).append(_model_line(row, tool_name))
    blocks = []
    if requested_lines:
        blocks.append("[REQUESTED BY THE USER THIS TURN — honor the USER MODEL "
                      "REQUEST below]\n" + "\n".join(requested_lines))
    if pinned_lines:
        blocks.append("[PINNED FOR THIS PERSONA — boosted defaults, in priority "
                      "order; not mandatory]\n" + "\n".join(pinned_lines))
    blocks += [f"[{label}]\n" + "\n".join(groups[tier])
               for tier, label in _TIER_DISPLAY if tier in groups]
    return "\n".join(blocks) or "- (no models currently reachable)"


def _user_request_block(user_request: Optional[dict]) -> str:
    """The USER MODEL REQUEST override block: the user's own message named a
    model/tier, so the free-first procedure yields to it. Rendered between the
    window state and the selection procedure so a small brain reads it before
    the rules it overrides."""
    if not user_request:
        return ""
    target = user_request.get("target", "the requested model")
    lines = [
        f"\nUSER MODEL REQUEST: the user's message EXPLICITLY asks to use "
        f"{target}. This OVERRIDES the free-first SELECTION PROCEDURE below: "
        f"dispatch to a candidate marked \"REQUESTED BY THE USER\" (the cheapest "
        f"matching tier if several match). Do not substitute a different model "
        f"unless every requested candidate is unreachable or denied by a "
        f"guardrail — in that case say so plainly and offer the best "
        f"alternative. If spending paid usage requires the user's confirmation, "
        f"the router pauses and asks them automatically — just dispatch."]
    confirmed = user_request.get("confirmed")
    if confirmed is True:
        lines.append(
            "The user has ALREADY CONFIRMED spending paid usage for this — "
            "dispatch to the requested model now; do not ask again.")
    elif confirmed is False:
        lines.append(
            "The user DECLINED the paid-usage warning — route to the best "
            "FREE/LOCAL model instead, and say in your narration that you "
            "stayed local at their request.")
    return "\n".join(lines) + "\n"


def build_system_prompt(persona: Optional[dict], ranked: list[dict],
                        tool_name_for: dict[str, str], meridian_note: str,
                        pending_question: Optional[str],
                        client_system: Optional[str] = None,
                        user_request: Optional[dict] = None,
                        spend_note: Optional[str] = None) -> str:
    p = persona or {}
    name = p.get("virtual_name", "Foundry-Chat")
    triggers = p.get("escalation_triggers") or "[]"
    try:
        triggers_list = json.loads(triggers)
    except (json.JSONDecodeError, TypeError):
        triggers_list = []

    models_block = _models_block(ranked, tool_name_for)
    spend_line = f"\nMETERED SPEND: {spend_note}" if spend_note else ""
    request_block = _user_request_block(user_request)

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

CLAUDE USAGE WINDOW: {meridian_note}{spend_line}
{request_block}
SELECTION PROCEDURE (apply in order — this is structural policy, follow it \
even when a pricier model would also work; a USER MODEL REQUEST above \
overrides steps a-d):
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
higher-scored mismatch. Within that, if a candidate is marked "⚡ already \
loaded in VRAM" and it suits the task, prefer it over an equally-suitable \
one that isn't — it avoids a slow model reload on the backend.
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
free."). It streams to the user live as status while they wait. One line, no more.
11. GENERATIVE MEDIA (images, video, music, voice): if your tool list contains a \
matching media MCP tool (ComfyUI, TTS, music, transcription...), dispatch it — the \
result comes back as a URL or artifact reference; forward it with \
return_to_user(use_last_result=true). If NO media tool is present, say plainly that \
no media backend is configured — never attempt a text-only imitation of media.
12. WEB RESEARCH: a web-SEARCH tool (searxng...) finds pages; it does not read \
them. When search snippets aren't enough to answer accurately, dispatch a worker \
to OPEN the top result and read its full content — and for that, prefer a crawler \
tool (crawl4ai markdown/crawl) over a plain URL fetch: crawlers render JavaScript \
and are far less likely to be blocked (a bare fetch often 403s on bot detection)."""]

    if p.get("output_style") == "plain_text":
        parts.append("\n" + PLAIN_TEXT_STYLE)

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
