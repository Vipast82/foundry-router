"""Context guard — never send a backend more than its context window.

Clients compact their own history (Cline's auto-compact), but not always in
time: Cline decides from the token count of the PREVIOUS request, a jump (a
big file read, a long tool result) can overshoot in one turn, and Ollama's
prompt_eval_count excludes cached tokens so the client can badly
underestimate. When the next request would overflow, llama.cpp / vLLM /
Claude reject it and the session is stuck.

Foundry knows the serving model's window (persona context_window, the
engine's n_ctx / max_model_len, the registry). Before dispatch it estimates
the prompt (messages + tool definitions) and, if it exceeds
window - output reserve, trims — least valuable first:

  1. oversized tool results in OLDER turns are cut to a head + tail excerpt;
  2. the oldest turns after the task are dropped (system prompt and the first
     user message — the task — are always kept; an assistant tool call and
     its results are dropped together so no result is orphaned);
  3. if one recent message alone is still too big, its middle is cut.

Only the copy sent to the model is trimmed — the client keeps its full
history, and the turn says what was cut (thinking line + Usage Log).

Estimates are chars / ratio; the ratio is learned per model from the prompt
token counts backends report, so it converges on the real tokenizer.
"""

from __future__ import annotations

import json
from typing import Optional

DEFAULT_RATIO = 3.2           # chars per token (conservative for code + prose)
_ratio: dict[str, float] = {}  # model -> learned chars/token
_KEEP_RECENT = 6              # messages at the end whose tool results aren't cut in step 1
_IMAGE_TOKENS = 1200


def _content_chars(c) -> int:
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(str(p.get("text") or "")) for p in c if isinstance(p, dict))
    return len(str(c or ""))


def message_chars(m: dict) -> int:
    n = _content_chars(m.get("content")) + len(m.get("thinking") or "") + 12
    if m.get("tool_calls"):
        n += len(json.dumps(m["tool_calls"], ensure_ascii=False, default=str))
    return n


def ratio_for(model: str) -> float:
    return _ratio.get(model, DEFAULT_RATIO)


def estimate(messages: list[dict], tools: Optional[list], model: str = "") -> int:
    chars = sum(message_chars(m) for m in messages or [])
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False, default=str))
    images = sum(len(m.get("images") or []) for m in messages or [])
    return int(chars / ratio_for(model)) + images * _IMAGE_TOKENS


def learn(model: str, messages: list[dict], tools: Optional[list], prompt_tokens: int) -> None:
    """Calibrate chars/token from a backend's TOTAL prompt token count (only
    call with totals — not Ollama's cache-excluded prompt_eval_count)."""
    if not model or not prompt_tokens or prompt_tokens < 500:
        return
    chars = sum(message_chars(m) for m in messages or [])
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False, default=str))
    images = sum(len(m.get("images") or []) for m in messages or [])
    text_tokens = prompt_tokens - images * _IMAGE_TOKENS
    if text_tokens <= 0:
        return
    r = max(2.0, min(5.0, chars / text_tokens))
    prev = _ratio.get(model)
    _ratio[model] = r if prev is None else prev * 0.7 + r * 0.3


def _cut(text: str, keep_chars: int, why: str) -> str:
    if len(text) <= keep_chars:
        return text
    head = int(keep_chars * 0.6)
    tail = max(0, keep_chars - head)
    return (text[:head] + f"\n\n[… {len(text) - keep_chars} chars cut by Foundry's context "
            f"guard ({why}) …]\n\n" + (text[-tail:] if tail else ""))


def _units(msgs: list[dict]) -> list[list[dict]]:
    """Group messages so an assistant tool call and its tool results move
    together (dropping one without the other breaks every backend format)."""
    out: list[list[dict]] = []
    for m in msgs:
        if m.get("role") == "tool" and out and (out[-1][0].get("tool_calls")):
            out[-1].append(m)
        else:
            out.append([m])
    return out


def fit(messages: list[dict], tools: Optional[list], window: int, reserve: int,
        model: str = "") -> tuple[list[dict], Optional[dict]]:
    """Return (messages to send, report or None when nothing was trimmed)."""
    if not window or window <= 0 or not messages:
        return messages, None
    budget = int((window - max(0, reserve)) * 0.97)      # 3% margin for estimate error
    before = estimate(messages, tools, model)
    if before <= budget or budget <= 0:
        return messages, None
    r = ratio_for(model)
    msgs = [dict(m) for m in messages]
    trimmed = 0

    # 1) cut oversized tool results in older turns
    limit_chars = int(max(2000, budget * r * 0.04))       # ≤4% of the budget each
    for i, m in enumerate(msgs[:-_KEEP_RECENT] if len(msgs) > _KEEP_RECENT else []):
        if m.get("role") == "tool" and isinstance(m.get("content"), str) \
                and len(m["content"]) > limit_chars:
            msgs[i] = {**m, "content": _cut(m["content"], limit_chars, "older tool result")}
            trimmed += 1
    now = estimate(msgs, tools, model)

    # 2) drop the oldest turns after the protected prefix (system + first user)
    dropped = 0
    if now > budget:
        first_user = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), None)
        cut_from = (first_user + 1) if first_user is not None else 0
        prefix, rest = msgs[:cut_from], msgs[cut_from:]
        units = _units(rest)
        while now > budget and len(units) > 1:
            gone = units.pop(0)
            dropped += len(gone)
            now = estimate(prefix + [m for u in units for m in u], tools, model)
        rest = [m for u in units for m in u]
        if dropped:
            note = (f"[Foundry context guard: {dropped} earlier message(s) were removed "
                    f"so this conversation fits the model's {window:,}-token context "
                    f"window. Continue from the recent context below.]")
            if prefix and prefix[-1].get("role") == "user" and isinstance(prefix[-1].get("content"), str):
                prefix[-1] = {**prefix[-1], "content": prefix[-1]["content"] + "\n\n" + note}
            else:
                rest = [{"role": "user", "content": note}] + rest
        msgs = prefix + rest

    # 3) a single recent message still too big: cut its middle
    if now > budget:
        biggest = max(range(len(msgs)), key=lambda i: message_chars(msgs[i]))
        m = msgs[biggest]
        if isinstance(m.get("content"), str):
            over_chars = int((now - budget) * r) + 200
            keep = max(1000, len(m["content"]) - over_chars)
            msgs[biggest] = {**m, "content": _cut(m["content"], keep, "message too large")}
            trimmed += 1
            now = estimate(msgs, tools, model)

    return msgs, {"window": window, "budget": budget, "before": before, "after": now,
                  "dropped": dropped, "trimmed": trimmed}


def describe(rep: dict) -> str:
    parts = []
    if rep.get("dropped"):
        parts.append(f"dropped {rep['dropped']} older message(s)")
    if rep.get("trimmed"):
        parts.append(f"cut {rep['trimmed']} oversized tool result/message(s)")
    return (f"context guard: ~{rep['before'] / 1000:.0f}k tokens would overflow the "
            f"{rep['window'] / 1000:.0f}k window — " + ", ".join(parts or ["trimmed"])
            + f" → ~{rep['after'] / 1000:.0f}k sent (your client keeps its full history)")
