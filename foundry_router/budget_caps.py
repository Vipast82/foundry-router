"""The router's half of the budget mode switch (COORDINATOR_AUTOMATION_NEEDS item 16).

A project's `[automation] budget_mode` (normal / save_claude / save_all) caps how many
sprints the coordinator runs; the usage cutoffs it names belong here, where a call is
refused and the persona's next route takes over:

  save_claude   Claude's cutoff is at most the project's `save_claude_cutoff_pct`.
  save_all      the same, and every other backend with a cutoff under Router settings is at
                most `save_all_cutoff_pct`.

The Router settings document stays the source: its `backends.<name>.cutoff_pct` is what
"normal" means. What is written to config.yaml (`usage_limit_pct`, the one value every gate
already reads) is the lower of the document's value and the mode's, worked out again each
time either document gets a new revision - so going back to normal puts the document's
values back with nothing remembered and nothing to restore. Only backends that have an
entry in the Router settings document are written; one without an entry is named in the
notes and left alone (its value in config.yaml may be a hand-set one).

A cutoff here is router-wide: chat is held to it too, as it is to the Router settings value.
"""

from __future__ import annotations

from typing import Optional

from foundry_automation import ROUTER_DOC

MODES = ("normal", "save_claude", "save_all")


def _pct(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 1.0 <= f <= 100.0 else None


def modes(store) -> dict:
    """{project: {"mode", "claude", "other"}} for each deployed project document that is in
    a saving mode (cutoffs None where the mode sets none)."""
    out: dict = {}
    for row in store.projects():
        p = row["project"]
        if p == ROUTER_DOC or row.get("deployed_rev") is None:
            continue
        try:
            a = (((store.deployed(p) or {}).get("doc") or {}).get("settings") or {}).get(
                "automation") or {}
        except Exception:                                        # noqa: BLE001
            continue
        mode = str(a.get("budget_mode") or "normal")
        if mode not in MODES or mode == "normal":
            continue
        out[p] = {"mode": mode, "claude": _pct(a.get("save_claude_cutoff_pct", 75)),
                  "other": _pct(a.get("save_all_cutoff_pct", 75)) if mode == "save_all" else None}
    return out


def caps(store) -> dict:
    """The lowest cutoff any project's mode asks for: {"claude": pct|None, "other": pct|None,
    "by": {project: mode}}."""
    ms = modes(store)

    def low(key):
        vals = [m[key] for m in ms.values() if m[key] is not None]
        return min(vals) if vals else None
    return {"claude": low("claude"), "other": low("other"),
            "by": {p: m["mode"] for p, m in ms.items()}}


def plan(config, raw_backends: list, want: dict, cap: dict) -> tuple:
    """What each backend's usage_limit_pct should be: ({name: pct | None}, notes). `want` is
    the Router settings document's `backends` table; only its backends are planned."""
    notes: list = []
    out: dict = {}
    names = {b.get("name") for b in raw_backends if isinstance(b, dict)}
    for name in want:
        if name not in names:
            notes.append(f"backend {name!r} is not configured; its cutoff waits")
    who = ", ".join(f"{p} ({m})" for p, m in sorted((cap.get("by") or {}).items()))
    for b in raw_backends:
        if not isinstance(b, dict) or not b.get("name"):
            continue
        name = b["name"]
        claude = b.get("type") == "anthropic-compatible"
        limit = cap.get("claude") if claude else cap.get("other")
        if name not in want:
            if limit is not None and b.get("type") != "ollama":
                notes.append(f"backend {name!r} has no cutoff under Router settings: the "
                             f"budget mode of {who} does not change it")
            continue
        base = _pct((want[name] or {}).get("cutoff_pct"))
        if limit is None:
            out[name] = base
            continue
        try:                             # what applies when the document sets no cutoff
            default = (float(config.meridian.conserve_strong_at) * 100.0 if claude
                       else float(config.subscription_usage.limit_pct))
        except Exception:                                        # noqa: BLE001
            default = 100.0
        now = base if base is not None else default
        if limit < now:
            out[name] = limit
            notes.append(f"{name}: cutoff {limit:g}% (budget mode of {who}; Router settings "
                         f"say {now:g}%)")
        else:
            out[name] = base
    return out, notes


async def apply(svc, doc: Optional[dict] = None) -> list:
    """Write the cutoffs in force (Router settings, lowered by a project's budget mode) to
    config.yaml and rebuild the pool when one changed. `doc` = the Router settings document
    when the caller has it. The notes say what was lowered and what could not be."""
    store = svc.automation
    if doc is None:
        doc = (store.deployed(ROUTER_DOC) or {}).get("doc") or {}
    want = ((doc or {}).get("settings") or {}).get("backends") or {}
    try:
        cap = caps(store)
    except Exception as e:                                       # noqa: BLE001
        cap = {"claude": None, "other": None, "by": {}}
        extra = [f"budget modes not read: {type(e).__name__}: {e}"]
    else:
        extra = []
    notes: list = []
    changed: list = []

    def mutate(raw: dict) -> None:
        backends = (((raw.get("backend_pool") or {}).get("internal") or {}).get("backends")
                    or [])
        values, ns = plan(svc.config_store.config, backends, want, cap)
        notes.extend(ns)
        for b in backends:
            if not isinstance(b, dict) or b.get("name") not in values:
                continue
            pct, before = values[b["name"]], b.get("usage_limit_pct")
            if pct is None:
                b.pop("usage_limit_pct", None)
            else:
                b["usage_limit_pct"] = float(pct)
            if b.get("usage_limit_pct") != before:
                changed.append(b["name"])

    # look first: a settings revision that changes no cutoff must not rewrite config.yaml
    probe = {"backend_pool": {"internal": {"backends": [
        {"name": b.name, "type": b.type, **({"usage_limit_pct": b.usage_limit_pct}
                                           if b.usage_limit_pct else {})}
        for b in svc.config_store.config.backend_pool.internal.backends]}}}
    mutate(probe)
    if not changed:
        return notes + extra
    notes.clear()
    changed.clear()
    svc.config_store.save(mutate)
    try:
        await svc.rebuild_pool()
    except Exception as e:                                       # noqa: BLE001
        notes.append(f"pool rebuild failed: {type(e).__name__}: {e}")
    return notes + extra

