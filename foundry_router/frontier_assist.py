"""Frontier helpers for turn assist: the completion auditor (E1) and the
`consult_senior` advisor tool (E2).

Both send a frontier model (Claude via Meridian, ChatGPT via CLIProxyAPI — the
persona's `assist.frontier_models`, tried in order) a PACKET built by code from
the conversation: the kickoff, the most recent turns condensed head+tail, and
the latest evidence outputs. No local model summarises anything.

Every call goes through GuardrailEngine.check_paid_call (subscription call
budgets included) and counts against the persona's per-conversation
`frontier_max_calls`. When nothing is allowed the helpers return None and the
caller degrades: the auditor falls back to its rule check, the advisor tells
the model no senior is available.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import interventions
from .turn_assist import NOTE_TAG, _text, error_rx, events

log = logging.getLogger(__name__)

AUDIT_TAG = "[Foundry auditor]"
ADVISOR_TAG = "[Foundry advisor"

CONSULT_TOOL_NAME = "consult_senior"
CONSULT_TOOL = {
    "type": "function",
    "function": {
        "name": CONSULT_TOOL_NAME,
        "description": (
            "Ask a senior engineer (a frontier model) for help. ONLY use this after "
            "two different approaches have failed on the same problem, or right before "
            "an irreversible decision (deleting data, rewriting history, a schema "
            "change). The budget is a few calls per task — do not use it for routine "
            "questions. You get back the likely root cause and the next 3 actions."),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "The specific question, one or two sentences."},
                "what_i_tried": {"type": "string",
                                 "description": "Each approach you tried and what happened."},
                "evidence": {"type": "string",
                             "description": "The exact error / output lines that matter."},
            },
            "required": ["question", "what_i_tried"],
        },
    },
}

ADVISOR_SYSTEM = (
    "You are a senior engineer helping a local coding model that is stuck. Answer in "
    "at most 300 words: 1) the most likely root cause, 2) the next 3 concrete actions "
    "in order, 3) what NOT to do. Use only the evidence given; say what to check when "
    "the evidence is insufficient. No preamble.")

AUDITOR_SYSTEM = (
    "You audit a coding agent's final report against the evidence in its session. "
    "Reply with exactly ACCEPT on the first line if every claim in the report is "
    "backed by tool output in the evidence and the task's stated checks were run and "
    "passed. Otherwise reply NOT ACCEPTED on the first line, then a numbered list of "
    "at most 6 defects (claim without evidence, check not run, failing output "
    "ignored, missing step from the task). At most 300 words.")

DEFAULT_FORBIDDEN = [
    r"\bshould (now )?(work|pass|be fixed)\b",
    r"\bI (believe|think|assume) (it|this|that) (works|is fixed|passes)\b",
    r"\b(probably|likely) (works|passes|fixed)\b",
]
_EVIDENCE_RX = re.compile(r"git (show|status|log|diff)|done_check|PASS|FAIL|passed|failed|"
                          r"exit code|tests?\b", re.I)


# ---- packet ---------------------------------------------------------------------

def _clip(s: str, head: int, tail: int) -> str:
    s = s or ""
    if len(s) <= head + tail + 40:
        return s
    return s[:head] + f"\n…[{len(s) - head - tail} chars cut]…\n" + s[-tail:]


def build_packet(messages: list[dict], turns: int = 15, max_chars: int = 60000) -> str:
    """Kickoff + the last `turns` assistant turns (with their tool results,
    head+tail) + the latest evidence outputs — all cut to max_chars."""
    kickoff = next((_text(m.get("content")) for m in messages if m.get("role") == "user"), "")
    asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    start = asst[-turns] if len(asst) >= turns else 0
    parts = ["## Task (kickoff message)\n" + _clip(kickoff, 5000, 1500)]
    body = []
    for m in messages[start:]:
        role = m.get("role")
        if role == "assistant":
            calls = "; ".join(
                f"{(tc.get('function') or tc).get('name')}("
                f"{_clip(json.dumps((tc.get('function') or tc).get('arguments'), ensure_ascii=False)[:600], 300, 150)})"
                for tc in m.get("tool_calls") or [])
            body.append("ASSISTANT: " + _clip(_text(m.get("content")), 800, 300)
                        + (f"\n  calls: {calls}" if calls else ""))
        elif role == "tool":
            body.append("RESULT: " + _clip(_text(m.get("content")), 1200, 800))
        elif role == "user":
            body.append("USER: " + _clip(_text(m.get("content")), 1500, 500))
    parts.append(f"## Last {turns} turns\n" + "\n".join(body))
    ev = [e for e in events(messages) if e.result_idx >= 0 and _EVIDENCE_RX.search(
        json.dumps(e.args)[:400])]
    if ev:
        parts.append("## Latest evidence outputs\n" + "\n\n".join(
            f"{e.name} {json.dumps(e.args)[:200]}\n{_clip(e.result, 1500, 1500)}" for e in ev[-4:]))
    out = "\n\n".join(parts)
    return _clip(out, max_chars // 3, max_chars - max_chars // 3)


# ---- frontier call ------------------------------------------------------------------

@dataclass
class FrontierAnswer:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


async def call_frontier(svc, cfg: dict, persona: dict, conversation_key: str,
                        system: str, user: str, max_tokens: int = 0
                        ) -> tuple[Optional[FrontierAnswer], str]:
    """(answer, why-not). Tries cfg.frontier_models in order, each through the
    paid-call guardrail; respects the per-conversation frontier budget."""
    from .guardrails import RequestGuardState
    from .usage import log_subscription_usage
    max_tokens = int(max_tokens or cfg.get("frontier_max_tokens") or 1500)
    models = [m for m in (cfg.get("frontier_models") or []) if m]
    if not models:
        return None, "no frontier_models configured"
    cap = int(cfg.get("frontier_max_calls") or 0)
    used = interventions.frontier_calls(svc.db, conversation_key)
    if cap and used >= cap:
        return None, f"frontier budget for this conversation used ({used}/{cap})"
    eff = svc.guardrails.effective(persona)
    eff = {**eff, "max_paid_calls_per_request": max(1, eff.get("max_paid_calls_per_request") or 1)}
    reasons = []
    avail = set(svc.pool.available_models())
    for m in models:
        if m not in avail:
            reasons.append(f"{m}: not available")
            continue
        info = svc.pool.backend_info(m)
        v = await svc.guardrails.check_paid_call(m, info, svc.registry.get(m),
                                                 RequestGuardState(), eff)
        if not v.allowed:
            reasons.append(f"{m}: {v.reason}")
            continue
        try:
            from . import capture as _capture
            with _capture.purpose("frontier_assist"):    # not one of the agent's own turns
                res, backend = await svc.pool.chat(
                    m, [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens)
        except Exception as e:                                     # noqa: BLE001
            reasons.append(f"{m}: {str(e)[:120]}")
            continue
        if (info or {}).get("subscription") or (info or {}).get("type") == "anthropic-compatible":
            log_subscription_usage(svc.db, m, backend, res.prompt_tokens, res.completion_tokens)
        from . import request_context, telemetry
        telemetry.record_call(svc.db, svc.registry, model=m, backend=backend, result=res,
                              persona=request_context.route().get("persona") or "",
                              mode="assist", max_tokens=max_tokens)
        text = (res.content or "").strip()
        if not text:
            reasons.append(f"{m}: empty answer")
            continue
        return FrontierAnswer(text, m, res.prompt_tokens or 0, res.completion_tokens or 0), ""
    return None, "; ".join(reasons)[:400]


# ---- auditor -------------------------------------------------------------------------

def tool_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))


def rejections_since_user(messages: list[dict]) -> int:
    """Audit rejections already visible in this stretch of the conversation."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"
                     and NOTE_TAG not in _text(m.get("content"))[:200]), default=-1)
    return sum(_text(m.get("content")).count(AUDIT_TAG + " NOT ACCEPTED")
               for m in messages[last_user + 1:] if m.get("role") == "assistant")


def lint(report: str, messages: list[dict], cfg: dict) -> list[str]:
    """Free mechanical checks on a final report. Returns defects."""
    defects = []
    for rx in cfg.get("auditor_required_patterns") or []:
        try:
            if not re.search(rx, report, re.I | re.M):
                defects.append(f"the report does not contain required evidence matching /{rx}/")
        except re.error:
            pass
    for rx in cfg.get("auditor_forbidden_patterns") or DEFAULT_FORBIDDEN:
        try:
            mm = re.search(rx, report, re.I)
        except re.error:
            continue
        if mm:
            defects.append(f"unverified claim \"{mm.group(0)}\" — run the check and paste its output")
    recent = events(messages, error_rx(cfg))[-4:]
    # a failure counts only when no later call of the same tool succeeded
    failed = [e for i, e in enumerate(recent) if not e.ok
              and not any(x.ok and x.name == e.name for x in recent[i + 1:])]
    if failed and not re.search(r"\b(fail|error|not (yet )?fixed|blocked|known issue)", report, re.I):
        e = failed[-1]
        defects.append(f"a recent {e.name} call failed and the report doesn't mention it: "
                       f"{_clip(e.result, 200, 100)!r}")
    return defects


def is_final_report(res, summary_req: bool, messages: list[dict], cfg: dict) -> bool:
    return (not summary_req and not (res.tool_calls or [])
            and len((res.content or "").strip()) > int(cfg.get("auditor_min_report_chars") or 0)
            and tool_turns(messages) >= int(cfg.get("auditor_min_tool_turns") or 0))


@dataclass
class Audit:
    accepted: bool
    defects: list
    verdict_text: str
    frontier: Optional[FrontierAnswer] = None
    note: str = ""


async def audit(svc, cfg: dict, persona: dict, conversation_key: str,
                messages: list[dict], report: str) -> Audit:
    """Rule check first; then (enforce mode) the frontier judge. Observe mode
    never calls a paid model and never rejects."""
    defects = lint(report, messages, cfg)
    mode = cfg.get("auditor") or "off"
    if defects:
        return Audit(False, defects, "\n".join(f"{i}. {d}" for i, d in enumerate(defects, 1)))
    if mode != "enforce":
        return Audit(True, [], "rule check passed")
    packet = build_packet(messages, int(cfg.get("packet_turns") or 15),
                          int(cfg.get("packet_max_chars") or 60000))
    ans, why = await call_frontier(
        svc, cfg, persona, conversation_key, cfg.get("auditor_prompt") or AUDITOR_SYSTEM,
        packet + "\n\n## FINAL REPORT TO AUDIT\n" + _clip(report, 8000, 4000))
    if ans is None:
        return Audit(True, [], "rule check passed; frontier judge unavailable", note=why)
    first = ans.text.strip().splitlines()[0].strip().upper() if ans.text.strip() else ""
    if first.startswith("ACCEPT"):
        return Audit(True, [], ans.text, ans)
    body = "\n".join(ans.text.strip().splitlines()[1:]).strip() or ans.text
    return Audit(False, [body], body, ans)


def reject_turn(verdict: str, n: int, cap: int) -> str:
    return (f"{AUDIT_TAG} NOT ACCEPTED ({n}/{cap}):\n{verdict}\n\n"
            f"Fix every point above with real tool output, then write the final report again.")


# ---- advisor ---------------------------------------------------------------------------

async def consult(svc, cfg: dict, persona: dict, conversation_key: str,
                  messages: list[dict], args: dict) -> tuple[str, Optional[FrontierAnswer], str]:
    """Run consult_senior. Returns (tool result text, answer or None, why-not)."""
    q = str(args.get("question") or "").strip()
    tried = str(args.get("what_i_tried") or "").strip()
    evidence = str(args.get("evidence") or "").strip()
    user = (build_packet(messages, int(cfg.get("packet_turns") or 15),
                         int(cfg.get("packet_max_chars") or 60000) * 3 // 4)
            + f"\n\n## The model's question\n{q}\n\n## What it tried\n{tried}"
            + (f"\n\n## Evidence it selected\n{_clip(evidence, 4000, 2000)}" if evidence else ""))
    ans, why = await call_frontier(svc, cfg, persona, conversation_key,
                                   cfg.get("advisor_prompt") or ADVISOR_SYSTEM, user)
    if ans is None:
        return (f"{NOTE_TAG}] No senior is available right now ({why}). Continue on your "
                f"own: write a STUCK note in the plan and try one different approach."), None, why
    return f"{ADVISOR_TAG} · {ans.model}]\n{ans.text}", ans, ""


async def auto_advice(svc, cfg: dict, persona: dict, conversation_key: str,
                      messages: list[dict], finding: str) -> tuple[str, Optional[FrontierAnswer]]:
    """Supervisor escalation step 3: ask the advisor on the model's behalf."""
    text, ans, _ = await consult(svc, cfg, persona, conversation_key, messages, {
        "question": f"The router's supervisor flagged: {finding}. What is going wrong and "
                    f"what should the model do next?",
        "what_i_tried": "(see the last turns in the packet)"})
    return text, ans
