"""Large responses without the slow encoder (v0.181.127).

FastAPI passes whatever an endpoint returns through `jsonable_encoder`: a pure
Python walk that copies every dict, list and value before the response class turns
the copy into JSON. For a response of a few megabytes (a work view, a model list, a
session with its parts) that walk takes seconds, on the event loop: the stall
watchdog recorded 3.2 s inside it on 2026-11-11, with every stream frozen meanwhile.

Almost every endpoint here returns plain dicts and lists that need no conversion.
`install()` wraps the encoder FastAPI's router calls: plain JSON data is checked by
the C serializer (milliseconds) and passed through untouched; anything else (a
datetime, a pydantic model, a set, bytes, a Path) goes to the real encoder exactly as
before. The JSON that reaches the client is the same.
"""

from __future__ import annotations

import json
from typing import Any

_INSTALLED = False


def is_plain(obj: Any) -> bool:
    """True when `obj` serializes as it stands: dicts with string keys, lists, strings,
    numbers, booleans and None, at any depth."""
    try:
        json.dumps(obj, check_circular=False, default=_refuse)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _refuse(o: Any):
    raise TypeError("not plain JSON")


def install() -> None:
    """Idempotent. Only the call made for endpoints WITHOUT a response model is
    short-circuited (the one with no filtering arguments)."""
    global _INSTALLED
    if _INSTALLED:
        return
    from fastapi import routing
    real = routing.jsonable_encoder

    def encoder(obj: Any, *args: Any, **kwargs: Any) -> Any:
        if not args and not kwargs and isinstance(obj, (dict, list)) and is_plain(obj):
            return obj
        return real(obj, *args, **kwargs)

    encoder.__wrapped__ = real                       # type: ignore[attr-defined]
    routing.jsonable_encoder = encoder
    _INSTALLED = True
