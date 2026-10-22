"""Usage cutoffs (percent used) per subscription backend, one place for the
admin API (ui/routes.py) and the operator chat (chat_engine.py).

`rows(svc)` reads what is in force (guardrails.effective_cutoff); `apply(svc,
body)` writes `{"claude": pct|null, "gpt": pct|null, "backends": {name: pct|null}}`
into config.yaml through the config store and updates the live pool in place.
"""

from __future__ import annotations

from typing import Optional


class CutoffError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def rows(svc) -> list[dict]:
    from .guardrails import effective_cutoff
    cfg = svc.config_store.config
    out = []
    for b in cfg.backend_pool.internal.backends:
        kind = ("claude" if b.type == "anthropic-compatible"
                else "gpt" if b.effective_flavor == "cliproxyapi" else None)
        if kind is None:
            continue
        out.append({"name": b.name, "kind": kind, "enabled": b.enabled,
                    "cutoff_pct": effective_cutoff(cfg, b.name),
                    "override": b.usage_limit_pct,
                    "source": ("backend" if b.usage_limit_pct else
                               "meridian.conserve_strong_at" if kind == "claude"
                               else "subscription_usage.limit_pct")})
    return out


def view(svc) -> dict:
    """The headline values plus one row per subscription backend."""
    cfg = svc.config_store.config
    rs = rows(svc)
    claude = next((r["cutoff_pct"] for r in rs if r["kind"] == "claude"),
                  cfg.meridian.conserve_strong_at * 100.0)
    su = cfg.subscription_usage
    return {"claude": claude, "gpt": su.limit_pct, "backends": rs,
            "reserve_pct": su.reserve_pct, "inflight_pct": su.inflight_pct,
            "fail_closed": su.fail_closed}


def _pct(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise CutoffError(f"invalid cutoff: {v!r} is not a number") from None
    if not 1.0 <= f <= 100.0:
        raise CutoffError("invalid cutoff: cutoff must be between 1 and 100")
    return f


def plan(svc, body: dict) -> tuple[dict, Optional[float]]:
    """({backend: pct|None}, gpt default|None) for a request body, or CutoffError."""
    cfg = svc.config_store.config
    per: dict = {}
    if "claude" in body:
        for b in cfg.backend_pool.internal.backends:
            if b.type == "anthropic-compatible":
                per[b.name] = _pct(body["claude"])
    known = {b.name for b in cfg.backend_pool.internal.backends}
    for name, v in (body.get("backends") or {}).items():
        if name not in known:
            raise CutoffError(f"no backend named {name!r}", 404)
        per[name] = _pct(v)
    gpt = _pct(body["gpt"]) if body.get("gpt") is not None else None
    return per, gpt


def apply(svc, body: dict) -> dict:
    per, gpt = plan(svc, body)

    def mutate(raw):
        for b in raw.setdefault("backend_pool", {}).setdefault("internal", {}).get("backends", []):
            if b.get("name") in per:
                if per[b["name"]] is None:
                    b.pop("usage_limit_pct", None)
                else:
                    b["usage_limit_pct"] = per[b["name"]]
        if gpt is not None:
            raw.setdefault("subscription_usage", {})["limit_pct"] = gpt
    svc.config_store.save(mutate)
    # The pool's live backend configs feed backend_info(): update in place.
    for name, st in (getattr(svc.pool, "backends", None) or {}).items():
        if name in per:
            st.config.usage_limit_pct = per[name]
    return view(svc)
