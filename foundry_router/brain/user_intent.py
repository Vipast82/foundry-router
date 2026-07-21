"""User-directed model steering: detect an EXPLICIT request in the user's own
message to use a specific model or tier ("use claude", "route this to opus",
"use a paid model"), so routing can honor it instead of the free-first default.

Detection is deterministic (regex over the newest user message), not delegated
to the routing brain — a small local brain following prose rules proved
unreliable for exactly this kind of instruction, and a wrong guess here either
burns paid budget or ignores the user. The same conservatism applies inside
the patterns: a directive verb must precede the model name, so a question
ABOUT a model ("what do you think of claude?") never reads as a request to
route to it.

Also home to parse_confirmation(), the yes/no reader for the paid-usage
confirmation handshake (agent-side gate stores a pending_paid record; the
next turn's reply is parsed here).
"""

from __future__ import annotations

import re
from typing import Optional

# Claude tier ladder shared with usage.claude_premium_level (4=Fable/Mythos,
# 3=Opus, 2=Sonnet, 1=Haiku). "claude" alone = any tier (generic request).
_TIER_LEVELS = {"fable": 4, "mythos": 4, "opus": 3, "sonnet": 2, "haiku": 1}

# A directive verb phrase that must IMMEDIATELY precede the target name.
# Deliberately excludes bare "to" ("compare it to sonnet" is not a request)
# — "to" only counts inside route/send/dispatch/escalate/switch phrases.
_DIRECTIVE = (
    r"(?:use|using|want|wants|need|prefer|try|ask|have|call|pick|choose|"
    r"run(?:\s+(?:this|it))?\s+(?:on|through|with)|"
    r"route(?:\s+(?:this|it))?\s+(?:to|through)|"
    r"send(?:\s+(?:this|it))?\s+to|"
    r"dispatch(?:\s+(?:this|it))?\s+to|"
    r"hand(?:\s+(?:this|it))?\s+to|"
    r"escalate\s+to|switch\s+to|go\s+with|via|with|on)"
)

# Negation immediately before the directive ("don't use claude", "would
# rather not use opus") cancels the match — steering AWAY is not steering TO.
_NEGATION = re.compile(
    r"(?:\bnot\b|n't|\bnever\b|\bwithout\b|\bavoid\b|\binstead\s+of\b|"
    r"\brather\s+than\b|\bstop\s+using\b)\s*(?:\w+\s*){0,2}$")

_ARTICLE = r"(?:the\s+|a\s+|an\s+)?"


def _directive_hit(text: str, alias_re: str) -> bool:
    """True when a non-negated directive-verb phrase precedes the alias."""
    pat = re.compile(r"\b" + _DIRECTIVE + r"\s+" + _ARTICLE + alias_re + r"\b")
    for m in pat.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def _model_aliases(model_id: str) -> list[str]:
    """Ways a user plausibly names a model: full id, base name before the
    Ollama ':tag', last path segment of a provider-prefixed id. Aliases that
    collide with tier words are dropped (tier matching handles those), as are
    trivially short ones ('a3') that would false-positive everywhere."""
    mid = model_id.lower()
    aliases = {mid, mid.split(":")[0], mid.split("/")[-1].split(":")[0]}
    return [a for a in aliases
            if len(a) >= 3 and a not in _TIER_LEVELS and a != "claude"]


def detect_model_request(text: str,
                         candidate_ids: Optional[list[str]] = None) -> Optional[dict]:
    """Scan a user message for an explicit model/tier request.

    Returns None (no explicit request), or one of:
      {"kind": "model",  "target": "<exact model id>"}
      {"kind": "tier",   "target": "opus", "level": 3}   (fable/opus/sonnet/haiku)
      {"kind": "claude", "target": "claude"}             (any Claude tier)
      {"kind": "paid",   "target": "a paid model"}       (generic paid request)

    Specificity wins: an exact model id beats a tier word beats bare "claude"
    beats generic "paid model" — "use claude sonnet" is a Sonnet request."""
    t = (text or "").lower()
    if not t:
        return None

    for mid in candidate_ids or []:
        for alias in _model_aliases(mid):
            if _directive_hit(t, re.escape(alias)):
                return {"kind": "model", "target": mid}

    for tier, level in _TIER_LEVELS.items():
        # "claude sonnet" / "claude-sonnet" name the tier, not bare claude.
        if _directive_hit(t, r"(?:claude[\s\-]+)?" + tier):
            return {"kind": "tier", "target": tier, "level": level}

    if _directive_hit(t, r"claude"):
        return {"kind": "claude", "target": "claude"}

    if _directive_hit(t, r"(?:paid|premium|cloud)\s+(?:model|tier)"):
        return {"kind": "paid", "target": "a paid model"}
    return None


# ---- paid-usage confirmation reply ------------------------------------------

_AFFIRM = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok|okay|proceed|continue|confirm|confirmed|"
    r"absolutely|affirmative|go\s+ahead|go\s+for\s+it|do\s+it|send\s+it|"
    r"use\s+it)\b")
_DECLINE = re.compile(
    r"\b(?:no|nope|nah|cancel|stop|abort|negative|don't|do\s+not|"
    r"never\s*mind|stay\s+local|use\s+local|go\s+local|keep\s+it\s+local|"
    r"free\s+model)\b")


def parse_confirmation(text: str) -> Optional[bool]:
    """Read the user's reply to a paid-usage confirmation question.

    True = proceed, False = declined, None = unclear (treated as neither —
    the brain sees the RESUMING note and handles the reply as new input).
    Only the head of the message is read: a decisive reply leads with its
    decision, and scanning deep into a long message invites false hits.
    Earliest keyword wins so "no, go ahead" reads as the 'no' it leads with."""
    head = (text or "").strip().lower()[:100]
    if head in ("y",):
        return True
    if head in ("n",):
        return False
    a = _AFFIRM.search(head)
    d = _DECLINE.search(head)
    if a and (not d or a.start() < d.start()):
        return True
    if d:
        return False
    return None
