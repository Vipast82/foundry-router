"""Foundry's refusals inside an agent server's recorded provider error.

Kilo and OpenCode both record the provider's error body on the assistant message (or the
retry status); Foundry's body carries `code` / `foundry_category`. Shared by every client.
"""

from __future__ import annotations

from typing import Optional

# Reported errors that are Foundry refusals: the provider error text
# carries Foundry's JSON body (code / foundry_category).
_FOUNDRY_CATEGORIES = ("budget", "pause", "route", "config", "auth")


def foundry_denial(text: str) -> Optional[dict]:
    """Find Foundry's refusal in a provider error Kilo recorded."""
    if not text:
        return None
    cat = None
    for c in _FOUNDRY_CATEGORIES:
        if f'"foundry_category": "{c}"' in text or f'\\"foundry_category\\":\\"{c}\\"' in text \
                or f'"foundry_category":"{c}"' in text or f'\\"foundry_category\\": \\"{c}\\"' in text:
            cat = c
            break
    if cat is None:
        for code, c in (("budget_denied", "budget"), ("optional_deferred", "budget"),
                        ("paused", "pause"),
                        ("route_unavailable", "route"), ("persona_disabled", "config")):
            if code in text:
                cat = c
                break
    if cat is None:
        return None
    retry = None
    for key in ("reset_seconds", "retry_after"):
        i = text.find(key)
        if i >= 0:
            digits = "".join(ch for ch in text[i + len(key):i + len(key) + 16] if ch.isdigit())
            if digits:
                retry = int(digits)
                break
    return {"category": cat, "retry_after": retry}
