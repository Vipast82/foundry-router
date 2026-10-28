"""JSON that fits a database column, and JSON that always reads back.

Never slice serialized JSON to make it fit (``json.dumps(x)[:N]``): the cut
text is not JSON any more, and the reader that ``json.loads`` it later fails.
That is what broke the Training tab (v0.151.x): episode cards over 60 000
characters were stored cut in half and ``GET /admin/api/training`` answered a
plain-text 500. ``dumps_fit`` shrinks the *object* (long strings, long lists)
until its JSON fits; ``loads_or`` reads a stored value without ever raising.
Both turn NaN / Infinity into null: Starlette's JSONResponse refuses them.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable

TRUNCATED_KEY = "truncated_for_storage"

# (longest string, longest list) tried in turn until the JSON fits
_STEPS = ((4000, 200), (1000, 60), (300, 40), (120, 20), (40, 8), (16, 2))


def finite(obj: Any) -> Any:
    """`obj` with NaN / ±Infinity replaced by None (tuples become lists)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj


def _shrink(obj: Any, max_str: int, max_list: int) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + "…"
    if isinstance(obj, dict):
        return {k: _shrink(v, max_str, max_list) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_shrink(v, max_str, max_list) for v in list(obj)[:max_list]]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, allow_nan=False)


def dumps_fit(obj: Any, limit: int, keep: Iterable[str] = ()) -> str:
    """JSON text of `obj` no longer than `limit` characters, always valid.
    Too long: long strings and lists are cut (``truncated_for_storage`` is set
    on a dict) until it fits; failing that, only the `keep` keys are stored."""
    obj = finite(obj)
    s = _dumps(obj)
    if len(s) <= limit:
        return s
    for max_str, max_list in _STEPS:
        small = _shrink(obj, max_str, max_list)
        if isinstance(small, dict):
            small[TRUNCATED_KEY] = True
        s = _dumps(small)
        if len(s) <= limit:
            return s
    stub: dict = {k: obj.get(k) for k in keep} if isinstance(obj, dict) else {}
    stub[TRUNCATED_KEY] = True
    s = _dumps(_shrink(stub, 200, 20))
    return s if len(s) <= limit else _dumps({TRUNCATED_KEY: True})


def loads_or(raw: Any, default: Any = None) -> Any:
    """Parsed stored JSON (NaN made null), or `default` when it does not parse."""
    if raw is None or raw == "":
        return default
    try:
        return finite(json.loads(raw))
    except (TypeError, ValueError):
        return default
