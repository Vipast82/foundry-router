"""Read-side helpers for the Automation page (/ui/automation).

Pure functions over the page's view of the automation settings: roles and the
persona each is bound to (C1 settings fields), persona route lists and backend
on/off (live router state), per-backend cutoffs and role rules
(foundry_automation). Nothing here writes config, calls a model or reaches a
backend:

  normalize_usage   one usage row per backend (5-hour + weekly windows, the
                    cutoff in force, unknown kept unknown)
  dry_run           "with this usage, a call for role X goes to ...", with the
                    skipped hops and why
  rule_warnings     the config store's role-rule validation plus the shell
                    bypass warning (shell allowed while edits are scoped)
  diff_docs         flat list of changed paths between two views
  seed_document     today's live routing state as the page's base view
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from foundry_automation import role_rules as _RR

ROLES = (
    ("implementer", "Implementer"), ("lead", "Lead"), ("reviewer", "Reviewer"),
    ("deep_reviewer", "Deep reviewer"), ("visual_reviewer", "Visual reviewer"),
    ("planner", "Planner"), ("researcher", "Researcher"), ("helper", "Helper"),
    ("coordinator", "Coordinator"))
# role -> the settings field that names its persona (foundry_automation.schema
# role_binding); helper personas are per backend and the coordinator has none
ROLE_BINDING = {"implementer": "roles.implementer", "planner": "roles.planner",
                "researcher": "roles.researcher", "reviewer": "roles.reviewer",
                "deep_reviewer": "roles.deep_reviewer", "visual_reviewer": "roles.visual_reviewer",
                "lead": "lead_manager.persona"}
STATES = ("allow", "ask", "deny")
CATEGORIES = _RR.CATEGORIES
HARD_DENIES = tuple(_RR.HARD_DENIES)
STALE_AFTER_S = 1800          # a passive usage reading older than this is shown as stale
WINDOW_KEYS = ("5h", "weekly")


# --------------------------------------------------------------------------- usage

def backend_family(name: str, flavor: str = "", typ: str = "") -> str:
    s = f"{name} {flavor} {typ}".lower()
    if "meridian" in s or "anthropic" in s or "claude" in s:
        return "claude"
    if "cliproxy" in s or "openai-codex" in s or "chatgpt" in s:
        return "chatgpt"
    if any(k in s for k in ("llama", "ollama", "strata", "local", "vllm")):
        return "local"
    return "other"


def _pct(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1)


def _window(used: Optional[float], resets: Any = None, age_s: Optional[float] = None,
            label: str = "") -> dict:
    return {"used_pct": used, "resets": resets, "age_s": age_s, "label": label,
            "stale": bool(age_s is not None and age_s > STALE_AFTER_S)}


def _meridian_windows(q: dict) -> dict:
    out = {}
    for b in q.get("buckets") or []:
        if not isinstance(b, dict) or b.get("fable_scoped"):
            continue
        key = {"five_hour": "5h", "seven_day": "weekly"}.get(str(b.get("type")))
        if key:
            used = b.get("used")
            out[key] = _window(None if used is None else _pct(float(used) * 100),
                               b.get("resets_hhmm") or b.get("resets_at"), None, b.get("label") or key)
    return out


def _cliproxy_windows(snap: dict) -> dict:
    """The account the router can still use decides: per window, the lowest
    reading among accounts that are signed in and not disabled."""
    best: dict = {}
    for a in snap.get("accounts") or []:
        if not isinstance(a, dict) or a.get("disabled") or a.get("unavailable"):
            continue
        age = a.get("age_s")
        for w in a.get("windows") or []:
            key = {"session": "5h", "weekly": "weekly"}.get(str(w.get("kind")))
            if not key or w.get("scope"):
                continue
            used = _pct(w.get("used"))
            cur = best.get(key)
            if cur is None or (used is not None and (cur["used_pct"] is None or used < cur["used_pct"])):
                rs = w.get("resets_in_s")
                best[key] = _window(used, f"in {int(rs) // 3600}h {int(rs) % 3600 // 60}m"
                                    if isinstance(rs, (int, float)) and rs > 0 else None,
                                    age, w.get("name") or key)
    return best


def normalize_usage(backends: Iterable[dict], meridian: Iterable[dict] = (),
                    cliproxy: Iterable[dict] = (), cutoffs: Optional[dict] = None,
                    default_cutoff: float = 95.0, fail_closed: bool = False) -> dict:
    """{backend: {family, enabled, healthy, subscription, cutoff_pct,
    windows: {"5h": {...}, "weekly": {...}}, unknown, note}}. A window with no
    reading has used_pct None (drawn hatched, never as 0%)."""
    mer = {m.get("backend"): m for m in meridian or [] if isinstance(m, dict)}
    cli = {c.get("backend"): c for c in cliproxy or [] if isinstance(c, dict)}
    cutoffs = cutoffs or {}
    out = {}
    for b in backends or []:
        name = b.get("name")
        if not name:
            continue
        fam = backend_family(name, b.get("flavor") or "", b.get("type") or "")
        sub = bool(b.get("subscription")) or fam in ("claude", "chatgpt")
        row = {"backend": name, "family": fam, "enabled": b.get("enabled", True) is not False,
               "healthy": b.get("healthy"), "subscription": sub,
               "models": list(b.get("models") or []),
               "cutoff_pct": _pct(cutoffs.get(name)) if cutoffs.get(name) is not None
               else (default_cutoff if sub else None),
               "windows": {}, "note": ""}
        if name in mer:
            row["windows"] = _meridian_windows(mer[name])
            row["note"] = mer[name].get("note") or ""
        elif name in cli:
            c = cli[name]
            row["windows"] = _cliproxy_windows(c)
            if not c.get("ok", True) or c.get("configured") is False:
                row["note"] = c.get("error") or "usage not readable"
        if sub:
            for k in WINDOW_KEYS:
                row["windows"].setdefault(k, _window(None, label=k))
        row["unknown"] = sub and any(row["windows"][k]["used_pct"] is None for k in WINDOW_KEYS)
        row["fail_closed"] = bool(fail_closed and fam == "chatgpt")
        out[name] = row
    return out


# --------------------------------------------------------------------------- dry run

def _role(doc: dict, role: str) -> Optional[dict]:
    return next((r for r in doc.get("roles") or [] if r.get("id") == role), None)


def persona_of(doc: dict, role: str) -> str:
    """The persona a role is bound to: its settings field (C1 role_binding)."""
    cur = doc.get("settings") or {}
    for k in (ROLE_BINDING.get(role) or "").split("."):
        if not k or not isinstance(cur, dict):
            return ""
        cur = cur.get(k)
    return cur if isinstance(cur, str) else ""


def dry_run(doc: dict, role: str, usage: dict) -> dict:
    """Walk the role's route chain the way the budget gate does: the first
    enabled route on an enabled, healthy backend whose 5-hour and weekly
    windows are under that backend's cutoff serves the call. Unknown usage is
    allowed (marked) unless the backend is fail_closed."""
    r = _role(doc, role)
    if not r:
        return {"role": role, "error": f"unknown role {role!r}", "hops": [], "chosen": None}
    persona = r.get("persona") or persona_of(doc, role)
    routes = ((doc.get("personas") or {}).get(persona) or {}).get("routes") or []
    policy = doc.get("backends") or {}
    hops, chosen = [], None
    for i, rt in enumerate(routes):
        be, model = rt.get("backend") or "", rt.get("model") or ""
        hop = {"position": i + 1, "backend": be, "model": model, "outcome": "skipped", "reason": ""}
        if chosen is not None:
            hop.update(outcome="not_reached", reason="an earlier route serves the call")
            hops.append(hop)
            continue
        pol = policy.get(be) or {}
        u = usage.get(be)
        cutoff = pol.get("cutoff_pct", (u or {}).get("cutoff_pct"))
        if rt.get("enabled") is False:
            hop["reason"] = "route turned off"
        elif not be:
            hop["reason"] = f"no backend lists {model} right now"
        elif u is None:
            hop["reason"] = f"backend {be!r} is not in the pool"
        elif pol.get("enabled") is False or not u.get("enabled", True):
            hop["reason"] = "backend turned off"
        elif u.get("healthy") is False:
            hop["reason"] = "backend unhealthy"
        elif u.get("models") and model not in u["models"]:
            hop["reason"] = f"{be} does not serve {model}"
        else:
            over = []
            unknown = []
            if u.get("subscription") and cutoff is not None:
                for k in WINDOW_KEYS:
                    w = (u.get("windows") or {}).get(k) or {}
                    if w.get("used_pct") is None:
                        unknown.append(w.get("label") or k)
                    elif w["used_pct"] >= float(cutoff):
                        over.append(f"{w.get('label') or k} {w['used_pct']:g}% ≥ cutoff {float(cutoff):g}%")
            if over:
                hop["reason"] = "; ".join(over)
            elif unknown and (pol.get("fail_closed") or u.get("fail_closed")):
                hop["reason"] = f"usage unknown ({', '.join(unknown)}) and the backend fails closed"
            else:
                hop["outcome"] = "chosen"
                hop["reason"] = ("usage unknown (" + ", ".join(unknown) + "): allowed, not fail-closed"
                                 if unknown else "under cutoff" if u.get("subscription") else "local, no cutoff")
                chosen = hop
        hops.append(hop)
    if not routes:
        summary = f"{persona or 'no persona'} has no routes"
    elif chosen:
        skipped = sum(1 for h in hops if h["outcome"] == "skipped")
        summary = (f"A call for {r.get('label') or role} goes to {chosen['model']} on {chosen['backend']}"
                   + (f" (after skipping {skipped})" if skipped else ""))
    else:
        summary = f"No route can serve {r.get('label') or role} right now: the work would pause"
    return {"role": role, "persona": persona, "hops": hops, "chosen": chosen, "summary": summary}


# --------------------------------------------------------------------------- role rules

def _rules(doc: dict, role: str) -> dict:
    """The role's rules over its defaults (C1 normalize)."""
    raw = (doc.get("role_rules") or {}).get(role)
    try:
        return _RR.normalize(role, raw)
    except Exception:                                            # noqa: BLE001
        return _RR.normalize(role, None)


def _norm(p: str) -> str:
    return re.sub(r"/+", "/", str(p or "").strip().replace("\\", "/")).casefold()


def _restricted(globs: list) -> bool:
    return bool(globs) and not any(_norm(g) in ("**", "**/*", "*") for g in globs)


def _role_of(path: str) -> str:
    parts = path.split(".")
    return parts[1] if len(parts) > 1 else ""


def rule_warnings(doc: dict) -> list[dict]:
    """The config store's own role-rule validation (errors and warnings), plus
    the shell bypass warning: shell allowed while edits are limited to a scope."""
    rr = doc.get("role_rules") or {}
    errors, warns = _RR.validate(rr)
    out = [{"role": _role_of(e["path"]), "severity": "bad", "message": f"{e['path']}: {e['message']}"}
           for e in errors]
    out += [{"role": _role_of(w["path"]), "severity": "warn", "message": f"{w['path']}: {w['message']}"}
            for w in warns]
    for role in rr:
        if role not in _RR.RULE_ROLES:
            continue
        rules = _rules(doc, role)
        p = rules.get("permissions") or {}
        if p.get("bash") in ("allow", "ask") and p.get("edit", "deny") != "deny" and \
                _restricted((rules.get("edit_paths") or {}).get("allow")):
            out.append({"role": role, "severity": "warn",
                        "message": f"{role}: shell is {p['bash']}ed while edits are limited to a "
                                   f"scope. Shell commands can write outside it; the scope check "
                                   f"after the run is what catches that."})
    return out


# --------------------------------------------------------------------------- diff

def diff_docs(before: Any, after: Any, path: str = "") -> list[dict]:
    """Changed leaves as [{path, before, after}]. Lists of scalars and route
    lists compare whole (a reorder is one change, readable as before -> after)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after), key=str):
            if k == "revision":
                continue
            p = f"{path}.{k}" if path else str(k)
            if k not in before:
                out.append({"path": p, "before": None, "after": after[k], "op": "add"})
            elif k not in after:
                out.append({"path": p, "before": before[k], "after": None, "op": "remove"})
            else:
                out.extend(diff_docs(before[k], after[k], p))
        return out
    if isinstance(before, list) and isinstance(after, list) and path.endswith("roles"):
        bi = {r.get("id"): r for r in before if isinstance(r, dict)}
        ai = {r.get("id"): r for r in after if isinstance(r, dict)}
        if len(bi) == len(before) and len(ai) == len(after):
            return diff_docs(bi, ai, path)
    if json.dumps(before, sort_keys=True, default=str) != json.dumps(after, sort_keys=True, default=str):
        return [{"path": path, "before": before, "after": after, "op": "change"}]
    return []


# --------------------------------------------------------------------------- live base view

def default_role_rules() -> dict:
    return _RR.all_defaults()


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    try:
        x = json.loads(v or "[]")
    except (TypeError, ValueError):
        return []
    return x if isinstance(x, list) else []


def persona_routes(p: dict, models_by_backend: dict) -> list[dict]:
    """A persona's route list the way the router reads it: pinned_models is the
    order, model_allowlist the models it may use. A pinned model left out of a
    non-empty allowlist is a route turned off (kept in place); allowed models
    that aren't pinned follow the pinned ones."""
    pins = [m for m in _jlist(p.get("pinned_models")) if isinstance(m, str) and m]
    allow = [m for m in _jlist(p.get("model_allowlist")) if isinstance(m, str) and m]
    order = pins + [m for m in allow if m not in pins]
    out = []
    for m in order:
        be = p.get("backend") if p.get("backend") and m in models_by_backend.get(p["backend"], ()) else ""
        if not be:
            be = next((b for b, ms in models_by_backend.items() if m in ms), p.get("backend") or "")
        out.append({"backend": be, "model": m, "enabled": not allow or m in allow})
    return out


def _set_path(d: dict, path: str, value) -> None:
    *head, last = path.split(".")
    for k in head:
        d = d.setdefault(k, {})
    d[last] = value


def seed_document(personas: list, backends: list, role_personas: dict, cutoffs: dict,
                  default_cutoff: float = 95.0) -> dict:
    """Today's live routing state as the page's base view: persona routes,
    backend on/off and the cutoff in force. The versioned settings and role
    rules come from the config store and replace `settings`/`role_rules`;
    `role_personas` (what recent sessions ran with) is only a hint for roles
    the settings don't bind yet."""
    models_by_backend = {b["name"]: list(b.get("models") or []) for b in backends if b.get("name")}
    pmap = {}
    for p in personas or []:
        name = p.get("virtual_name") or p.get("name")
        if name:
            pmap[name] = {"routes": persona_routes(p, models_by_backend),
                          "description": p.get("description") or ""}
    bpol = {}
    for b in backends or []:
        fam = backend_family(b.get("name", ""), b.get("flavor") or "", b.get("type") or "")
        sub = bool(b.get("subscription")) or fam in ("claude", "chatgpt")
        bpol[b["name"]] = {"enabled": b.get("enabled", True) is not False,
                           "cutoff_pct": (cutoffs.get(b["name"]) or default_cutoff) if sub else None}
    settings: dict = {}
    for role, path in ROLE_BINDING.items():
        if role_personas.get(role):
            _set_path(settings, path, role_personas[role])
    return {"roles": [{"id": rid, "label": label} for rid, label in ROLES],
            "personas": pmap, "backends": bpol, "settings": settings,
            "role_rules": default_role_rules(), "hard_denies": [dict(h) for h in HARD_DENIES]}
