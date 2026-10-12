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


_BLOCK_MSGS = 20        # step-1 eligibility moves in blocks of this many messages
_LEDGER_SHARE = 0.12    # the tool ledger may use up to 12% of the budget
_DROP_BLOCK = 0.25      # step-2 drops in blocks of this share of the budget


def _one_line(text, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _content_text(c) -> str:
    if isinstance(c, list):
        return " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
    return str(c or "")


def _ledger_lines(units: list) -> list:
    """One line per dropped step, oldest first."""
    import hashlib
    lines = []
    n = 0
    for u in units:
        head = u[0]
        role = head.get("role")
        if role == "assistant" and head.get("tool_calls"):
            results = [m for m in u[1:] if m.get("role") == "tool"]
            said = _content_text(head.get("content"))
            if said.strip():
                lines.append(f"  note: {_one_line(said, 160)}")
            for k, tc in enumerate(head["tool_calls"]):
                n += 1
                fn = tc.get("function") or tc
                name = fn.get("name") or "tool"
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
                res = _content_text(results[k].get("content")) if k < len(results) else ""
                sha = hashlib.sha1(res.encode("utf-8", "replace")).hexdigest()[:10]
                status = ("ERROR " if res.lstrip().lower().startswith(("error", "failed"))
                          else "")
                lines.append(f"  #{n} {name}({_one_line(args, 140)}) → {status}"
                             f"{_one_line(res, 140) or '(no output)'} "
                             f"[{len(res):,} chars, sha1 {sha}]")
        elif role == "assistant":
            txt = _content_text(head.get("content"))
            if txt.strip():
                lines.append(f"  assistant: {_one_line(txt, 200)}")
        elif role == "user":
            lines.append(f"  user: {_one_line(_content_text(head.get('content')), 200)}")
    return lines


def _ledger_chars(units: list) -> int:
    return sum(len(x) + 1 for x in _ledger_lines(units)) + 400 if units else 0


def tool_ledger(units: list, window: int, cap_chars: int = 0) -> str:
    """The note that replaces dropped rounds: every tool call they contained,
    one line each, so completed work stays verifiable. Above `cap_chars` the
    oldest lines fold into per-tool counts."""
    lines = _ledger_lines(units)
    calls = sum(1 for x in lines if x.lstrip().startswith("#"))
    header = (f"[Foundry context guard — TOOL LEDGER. {calls} earlier tool call(s) were "
              f"collapsed so this conversation fits the model's {window:,}-token window. "
              f"Their full outputs were produced and read earlier in this task; each line "
              f"records the call, its arguments, a result excerpt, the result size and a "
              f"sha1 of the full output. Treat them as done — do not repeat them unless "
              f"you need the full output again.]")
    if cap_chars and sum(len(x) + 1 for x in lines) > cap_chars:
        keep, used = [], 0
        for x in reversed(lines):
            if used + len(x) + 1 > cap_chars * 0.8:
                break
            keep.append(x)
            used += len(x) + 1
        keep.reverse()
        older = lines[: len(lines) - len(keep)]
        counts: dict = {}
        for x in older:
            t = x.strip()
            if t.startswith("#"):
                name = t.split(" ", 1)[1].split("(", 1)[0]
                counts[name] = counts.get(name, 0) + 1
        folded = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        lines = [f"  (oldest {len(older)} step(s), summarized: {folded or 'notes only'})"] + keep
    return header + "\n" + "\n".join(lines)


# ---- sticky mode -----------------------------------------------------------------
# The stateless fit() below re-decides every turn, from an estimate that moves
# (learned chars/token, tool results cut when a block boundary passes). Measured
# on a real Cline session it flipped between trimmed (108K) and untrimmed (148K)
# prompts on consecutive turns and the cut point drifted 54 -> 56 -> 54 -> 58
# messages: every change rewrote the prompt from the first dropped message on
# and cost a 4-6 minute re-prefill. Sticky mode keeps one memo per conversation:
# the number of leading steps already collapsed. It only ever grows, is applied
# on every later turn whether or not the estimate still crosses the budget, and
# advances only when the prompt is over budget again — then by a whole block
# (down to _STICKY_TARGET of the budget), so the next advance is ~25% of the
# budget of growth away. No other history is touched in this mode.

_STICKY_TARGET = 0.72          # after an advance the prompt is cut down to this share of the budget
_memos: dict = {}              # conversation key -> {"n": steps collapsed, "sig": hash of them}
_MEMO_CAP = 64


def memo_for(key: str) -> dict:
    """The (mutable) sticky memo for one conversation; bounded LRU."""
    m = _memos.pop(key, None)
    if m is None:
        m = {}
    _memos[key] = m
    while len(_memos) > _MEMO_CAP:
        _memos.pop(next(iter(_memos)))
    return m


def _sig(units: list) -> str:
    import hashlib
    h = hashlib.sha1()
    for u in units:
        for m in u:
            h.update(json.dumps([m.get("role"), m.get("content"), m.get("tool_calls")],
                                sort_keys=True, default=str).encode("utf-8", "replace"))
    return h.hexdigest()


def _build(prefix: list, units: list, n: int, window: int, ledger_cap: float) -> tuple:
    """(messages, dropped message count) with the first n steps collapsed."""
    kept_users: list = []
    gone: list = []
    for u in units[:n]:
        if u[0].get("role") == "user":
            kept_users.extend(u)      # typed prompts are the task: never dropped
        else:
            gone.append(u)
    remaining = [m for u in units[n:] for m in u]
    dropped = sum(len(u) for u in gone)
    if not dropped:
        return prefix + kept_users + remaining, 0
    ledger = {"role": "user", "content": tool_ledger(gone, window, int(ledger_cap))}
    return prefix + kept_users + [ledger] + remaining, dropped


def fit_sticky(messages: list[dict], tools: Optional[list], window: int, reserve: int,
               model: str, memo: dict) -> tuple[list[dict], Optional[dict]]:
    """fit() for a conversation with a memo (see above). Always returns a report
    when it changed anything; report["advanced"] says whether the cut moved this
    turn (False = the same cut as last turn, nothing new to tell anyone)."""
    if not window or window <= 0 or not messages:
        return messages, None
    budget = int((window - max(0, reserve)) * 0.97)
    if budget <= 0:
        return messages, None
    r = ratio_for(model)
    msgs = [dict(m) for m in messages]
    first_user = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), None)
    cut_from = (first_user + 1) if first_user is not None else 0
    prefix, rest = msgs[:cut_from], msgs[cut_from:]
    units = _units(rest)
    before = estimate(messages, tools, model)

    n = int(memo.get("n") or 0)
    if n and (n > len(units) or memo.get("sig") != _sig(units[:n])):
        n = 0                                   # history isn't what the memo was made from
        memo.clear()
    # The ledger's size cap decides which lines fold into counts, so it is part of
    # the prompt: fixed when the cut is made, never recomputed from the learned
    # chars/token ratio (which wobbles every turn and would rewrite the ledger).
    ledger_cap = float(memo.get("cap") or budget * r * _LEDGER_SHARE)
    advanced = False
    out, dropped = _build(prefix, units, n, window, ledger_cap)
    now = estimate(out, tools, model)
    if now > budget:
        # over budget again: advance by a whole block, down to the target share
        target = budget * _STICKY_TARGET
        while now > target and n < len(units) - 1:
            n += 1
            out, dropped = _build(prefix, units, n, window, ledger_cap)
            now = estimate(out, tools, model)
        advanced = True
        ledger_cap = budget * r * _LEDGER_SHARE
        out, dropped = _build(prefix, units, n, window, ledger_cap)
        now = estimate(out, tools, model)
        memo["n"], memo["sig"], memo["cap"] = n, _sig(units[:n]), ledger_cap
    elif n:
        memo["n"], memo["sig"], memo["cap"] = n, _sig(units[:n]), ledger_cap
    if not n and not advanced:
        return messages, None

    trimmed = 0
    if now > budget:                             # one huge recent message: cut its middle
        biggest = max(range(len(out)), key=lambda i: message_chars(out[i]))
        m = out[biggest]
        if isinstance(m.get("content"), str):
            over_chars = int((now - budget) * r) + 200
            keep = max(1000, len(m["content"]) - over_chars)
            out[biggest] = {**m, "content": _cut(m["content"], keep, "message too large")}
            trimmed += 1
            now = estimate(out, tools, model)
    return out, {"window": window, "budget": budget, "before": before, "after": now,
                 "dropped": dropped, "trimmed": trimmed, "advanced": advanced, "steps": n}


def fit(messages: list[dict], tools: Optional[list], window: int, reserve: int,
        model: str = "", memo: Optional[dict] = None) -> tuple[list[dict], Optional[dict]]:
    """Return (messages to send, report or None when nothing was trimmed).

    Prompt-cache friendly: llama.cpp / vLLM / Claude reuse the KV cache for
    the longest unchanged PREFIX of the prompt. Trimming a little more every
    turn would move the cut point each turn and force a full re-prefill of a
    200k-token context every time. So every cut is quantized — tool-result
    cutting moves in blocks of messages, turn dropping in blocks of 25% of the
    budget, and the note is constant — making the trimmed prefix identical
    from turn to turn until the next block boundary is crossed (the same idea
    as Cline dropping half its history at once)."""
    if memo is not None:
        return fit_sticky(messages, tools, window, reserve, model, memo)
    if not window or window <= 0 or not messages:
        return messages, None
    budget = int((window - max(0, reserve)) * 0.97)      # 3% margin for estimate error
    before = estimate(messages, tools, model)
    if before <= budget or budget <= 0:
        return messages, None
    r = ratio_for(model)
    msgs = [dict(m) for m in messages]
    trimmed = 0

    # 1) cut oversized tool results in older turns; "older" = before a
    #    boundary that only advances every _BLOCK_MSGS messages (stable prefix)
    limit_chars = int(max(2000, budget * r * 0.04))       # ≤4% of the budget each
    boundary = max(0, ((len(msgs) - _KEEP_RECENT) // _BLOCK_MSGS) * _BLOCK_MSGS)
    for i in range(boundary):
        m = msgs[i]
        if m.get("role") == "tool" and isinstance(m.get("content"), str) \
                and len(m["content"]) > limit_chars:
            msgs[i] = {**m, "content": _cut(m["content"], limit_chars, "older tool result")}
            trimmed += 1
    now = estimate(msgs, tools, model)

    # 2) drop the oldest turns after the protected prefix (system + first
    #    user), in blocks: the amount dropped is rounded UP to a multiple of
    #    25% of the budget, measured over the (append-only) history, so the
    #    cut stays at the same message while the conversation grows.
    dropped = 0
    if now > budget:
        first_user = next((i for i, m in enumerate(msgs) if m.get("role") == "user"), None)
        cut_from = (first_user + 1) if first_user is not None else 0
        prefix, rest = msgs[:cut_from], msgs[cut_from:]
        units = _units(rest)
        # Typed user prompts are the task and its requirements (after a client
        # compaction the first user message is only the summary; the request
        # that started this long tool loop, and any later "continue", come
        # after it). They are small — never drop them; they keep their order.
        # Only tool rounds and the model's own text collapse into the ledger.
        over_chars = (now - budget) * r
        block = max(1.0, budget * r * _DROP_BLOCK)
        target = -(-over_chars // block) * block               # ceil to a block
        ledger_cap = budget * r * _LEDGER_SHARE
        gone_chars = 0.0
        kept_users: list = []
        gone_units: list = []
        while len(units) > 1 and gone_chars - min(_ledger_chars(gone_units), ledger_cap) < target:
            u = units.pop(0)
            if u[0].get("role") == "user":
                kept_users.extend(u)
                continue
            dropped += len(u)
            gone_units.append(u)
            gone_chars += sum(message_chars(m) for m in u)
        remaining = [m for u in units for m in u]
        rest = kept_users + remaining
        if dropped:
            # The dropped rounds are not lost silently: they collapse into a
            # TOOL LEDGER — one line per call (tool, arguments, result excerpt,
            # size, sha1) — so the model keeps a verifiable record of what was
            # already done and checked. Deterministic text: the same dropped
            # set gives byte-identical output, so the prompt cache holds until
            # the next block boundary.
            rest = kept_users + [{"role": "user", "content": tool_ledger(
                gone_units, window, int(ledger_cap))}] + remaining
        msgs = prefix + rest
        now = estimate(msgs, tools, model)
        msgs = prefix + rest
        now = estimate(msgs, tools, model)

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
        parts.append(f"collapsed {rep['dropped']} older message(s) into a tool ledger "
                     f"(one verifiable line per call)")
    if rep.get("trimmed"):
        parts.append(f"cut {rep['trimmed']} oversized tool result/message(s)")
    return (f"context guard: ~{rep['before'] / 1000:.0f}k tokens would overflow the "
            f"{rep['window'] / 1000:.0f}k window — " + ", ".join(parts or ["trimmed"])
            + f" → ~{rep['after'] / 1000:.0f}k sent. Your client keeps its full history and "
            f"is told its real size (~{rep['before'] / 1000:.0f}k), so it can compact.")
