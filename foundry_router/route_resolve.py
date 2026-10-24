"""Model-name resolution and route reachability for persona and backend edits
(v0.140.0, operator chat).

backends(svc)        {backend: {family, enabled, healthy, models}} from the
                     live pool (discovered + configured models; a disabled
                     backend keeps its configured list)
resolve(svc, ...)    route words -> concrete {model, backend, family, enabled}:
                     "local" / "claude" / "chatgpt" / "gpt" (a backend family),
                     a backend name, "model@backend", or a model id
reachability(...)    warnings for a persona's routes: unknown models, models
                     only on disabled backends, routes a strict persona's
                     pinned backend can't serve
orphans(svc, ...)    personas left with no usable route if backends go off
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

from .automation_view import backend_family

FAMILY_WORDS = {
    "local": "local", "locals": "local",
    "claude": "claude", "anthropic": "claude",
    "chatgpt": "chatgpt", "gpt": "chatgpt", "openai": "chatgpt", "codex": "chatgpt",
}
FAMILY_LABEL = {"local": "local", "claude": "Claude", "chatgpt": "ChatGPT", "other": "other"}


class ResolveError(ValueError):
    pass


def _jl(v) -> list:
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return x if isinstance(x, list) else []


def status_with_models(svc) -> list[dict]:
    """Backend status with each backend's discovered AND configured models, so
    a route keeps its backend while that backend is down or not probed yet
    (also used by the Automation page's seed)."""
    status = svc.pool.backend_status()
    states = getattr(svc.pool, "backends", None) or {}
    disabled = {b.name: b for b in getattr(svc.pool, "disabled", None) or []}
    for b in status:
        s = states.get(b.get("name"))
        conf = list(getattr(getattr(s, "config", None), "models", None) or []) if s else []
        if not s and b.get("name") in disabled:
            conf = list(getattr(disabled[b["name"]], "models", None) or [])
        b["models"] = list(dict.fromkeys(list(b.get("models") or []) + conf))
    return status


def backends(svc) -> dict:
    out = {}
    for b in status_with_models(svc):
        name = b.get("name")
        if not name:
            continue
        out[name] = {"family": backend_family(name, b.get("flavor") or "", b.get("type") or ""),
                     "enabled": b.get("enabled", True) is not False,
                     "healthy": b.get("healthy"), "models": list(b.get("models") or [])}
    return out


def _base(m: str) -> str:
    return str(m).split(":")[0]


def served_by(bmap: dict, model: str) -> list[str]:
    """Backends whose model list has `model` (tag-tolerant like strict routing)."""
    return [n for n, b in bmap.items()
            if model in b["models"] or _base(model) in {_base(x) for x in b["models"]}]


def family_of_model(bmap: dict, model: str, backend: str = "") -> str:
    if backend and backend in bmap:
        return bmap[backend]["family"]
    fams = {bmap[n]["family"] for n in served_by(bmap, model)}
    return fams.pop() if len(fams) == 1 else ("mixed" if fams else "unknown")


def _family_models(bmap: dict, fam: str) -> list[tuple[str, str]]:
    out = []
    for n, b in bmap.items():
        if b["family"] == fam and b["enabled"]:
            out += [(m, n) for m in b["models"]]
    return out


def resolve(svc, items: Iterable, current: Optional[list] = None) -> tuple[list, list]:
    """Route words -> ([{model, backend, family, enabled, from}], notes).

    A family word keeps the persona's current routes of that family, in order
    ("make it local and Claude only" drops the rest); with none, it is an error
    naming the family's models so a concrete one is picked. Raises ResolveError
    on anything that can't be made concrete."""
    bmap = backends(svc)
    current = list(current or [])
    out: list[dict] = []
    notes: list[str] = []
    for it in items:
        if isinstance(it, str):
            it = {"model": it, "enabled": True}
        if not isinstance(it, dict) or not isinstance(it.get("model"), str) or not it["model"].strip():
            raise ResolveError("routes are model names or {model, enabled, backend?}")
        word = it["model"].strip()
        enabled = it.get("enabled", True) is not False
        backend = (it.get("backend") or "").strip()
        if "@" in word and not backend:
            word, backend = (x.strip() for x in word.rsplit("@", 1))
        key = word.lower()
        fam = FAMILY_WORDS.get(key, "") if not backend else ""
        if key in bmap and not backend:            # a backend name: all its current routes
            mine = [r for r in current if r.get("backend") == key or key in served_by(bmap, r["model"])]
            if not mine:
                raise ResolveError(f"{word!r} is a backend; name one of its models: "
                                   + ", ".join(bmap[key]["models"][:12]))
            for r in mine:
                out.append({"model": r["model"], "backend": key, "family": bmap[key]["family"],
                            "enabled": enabled, "from": word})
            continue
        if fam:
            mine = [r for r in current if family_of_model(bmap, r["model"], r.get("backend", "")) == fam]
            if not mine:
                avail = _family_models(bmap, fam)
                raise ResolveError(
                    f"{word!r} means the {FAMILY_LABEL[fam]} backends, and this persona has no "
                    f"{FAMILY_LABEL[fam]} route yet; name the model: "
                    + (", ".join(f"{m}@{b}" for m, b in avail[:12]) or "none is served right now"))
            for r in mine:
                b = r.get("backend") or next(iter(served_by(bmap, r["model"])), "")
                out.append({"model": r["model"], "backend": b, "family": fam, "enabled": enabled,
                            "from": word})
            notes.append(f"{word} -> " + ", ".join(r["model"] for r in mine))
            continue
        if backend and backend not in bmap:
            raise ResolveError(f"no backend named {backend!r} (have: {', '.join(bmap)})")
        hosts = served_by(bmap, word)
        b = backend or (hosts[0] if hosts else "")
        out.append({"model": word, "backend": b, "family": family_of_model(bmap, word, b),
                    "enabled": enabled, "from": word})
    seen, uniq = set(), []
    for r in out:
        if r["model"] in seen:
            continue
        seen.add(r["model"])
        uniq.append(r)
    return uniq, notes


def current_routes(svc, persona: dict) -> list[dict]:
    """The persona's routes the way the Routing page shows them."""
    from .automation_view import persona_routes
    bmap = backends(svc)
    return persona_routes(persona, {n: b["models"] for n, b in bmap.items()})


def reachability(svc, routes: list, routing: Optional[str], backend: Optional[str]) -> list[str]:
    """Warnings for a route list (after resolve)."""
    bmap = backends(svc)
    warn = []
    for r in routes:
        hosts = served_by(bmap, r["model"])
        if not hosts:
            warn.append(f"{r['model']} is not served by any backend right now (unknown model?)")
            continue
        if r["enabled"] and not any(bmap[h]["enabled"] for h in hosts):
            warn.append(f"{r['model']} is only on turned-off backends ({', '.join(hosts)})")
        if (routing or "") == "strict" and backend and r["enabled"] and backend not in hosts:
            warn.append(f"{r['model']} is not served by {backend}, the backend this strict "
                        "persona is tied to: it would never be used")
    return warn


def strict_dead_routes(svc, routes: list, routing: Optional[str], backend: Optional[str]) -> list[str]:
    """Enabled routes a strict persona tied to `backend` can never use."""
    if (routing or "") != "strict" or not backend:
        return []
    bmap = backends(svc)
    return [r["model"] for r in routes if r["enabled"] and served_by(bmap, r["model"])
            and backend not in served_by(bmap, r["model"])]


def orphans(svc, turning_off: Iterable[str] = (), turning_on: Iterable[str] = ()) -> list[dict]:
    """Enabled personas that would have no usable route once `turning_off`
    backends are off: [{persona, strict, why, effect}]."""
    bmap = backends(svc)
    off = set(turning_off)
    on = set(turning_on)
    live = {n for n, b in bmap.items() if (b["enabled"] or n in on) and n not in off}
    out = []
    for p in svc.personas.list(enabled_only=True):
        strict = str(p.get("routing") or "").lower() == "strict"
        allow = [str(a) for a in _jl(p.get("model_allowlist"))]
        pins = [str(x) for x in _jl(p.get("pinned_models"))]
        chain = [x for x in pins if x in allow] + [a for a in allow if a not in pins]
        pb = (p.get("backend") or "").strip()
        why = ""
        if strict and pb and not pb.startswith("<") and pb not in live:
            why = f"tied to backend {pb}"
        elif chain:
            usable = [m for m in chain
                      if any(h in live for h in served_by(bmap, m))
                      and (not strict or not pb or pb in served_by(bmap, m))]
            if not usable:
                why = "no enabled route on an enabled backend (" + ", ".join(chain[:4]) + ")"
        if why:
            out.append({"persona": p["virtual_name"], "strict": strict, "why": why,
                        "effect": "requests are refused" if strict
                        else "falls back to any model in the pool"})
    return out
