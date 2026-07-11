"""Exception description that never comes back empty.

Found live: "all backends failed for 'qwen3.6:27b': truenas-ollama: " — the
real exception was swallowed because str(e) on many httpx transport errors
(ReadError, RemoteProtocolError) is an empty string, and Python 3.11
ExceptionGroups (anyio TaskGroups leaking through httpcore) stringify as an
unhelpful wrapper line. Every user-facing/logged failure message should go
through describe_exception so the class name and any nested causes survive.
"""

from __future__ import annotations


def describe_exception(e: BaseException, limit: int = 500) -> str:
    """Human-useful one-liner: unwraps ExceptionGroup trees, falls back to the
    class name when an exception has no message, dedupes repeats."""
    parts: list[str] = []

    def walk(x: BaseException) -> None:
        if isinstance(x, BaseExceptionGroup):
            for sub in x.exceptions:
                walk(sub)
            return
        text = str(x).strip()
        part = f"{type(x).__name__}: {text}" if text else f"{type(x).__name__} (no message)"
        if part not in parts:
            parts.append(part)
        # __cause__ often carries the real story (e.g. httpx wrapping httpcore)
        if x.__cause__ is not None and len(parts) < 6:
            walk(x.__cause__)

    walk(e)
    return " | ".join(parts)[:limit] or type(e).__name__
