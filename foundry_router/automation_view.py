"""Read-side helpers for the Automation area (/ui/automation).

Pure functions over one "automation document" (roles -> persona -> ordered
routes -> backend, backend policy, settings, role rules; the shape the C1
config store serves; role rules in foundry_automation.role_rules form). Nothing here writes config,
calls a model or reaches a backend:

  normalize_usage   one usage row per backend (5-hour + weekly windows, cutoff,
                    unknown kept unknown) from the readings Foundry already has
  dry_run           "with this usage, a call for role X goes to ...", with the
                    skipped hops and why
  path_test         may role X read/edit/write this path or run this command?
                    (foundry_automation.role_rules.test when C1 is present)
  rule_warnings     C1 validation plus the shell-with-a-scope bypass warning
  diff_docs         flat list of changed paths between two documents
  seed_document     STUB until C1 is merged: today's settings as a document
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

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
STALE_AFTER_S = 1800          # a passive usage reading older than this is shown as stale
WINDOW_KEYS = ("5h", "weekly")

try:                           # the shared schema and rules (C1); absent until merged
    from foundry_automation import role_rules as _RR
except ImportError:            # pragma: no cover - depends on the C1 merge
    _RR = None

# ---- STUB copies of foundry_automation.role_rules (C1), used only while that
# package is not on this router. Same shapes and values; delete with the stub.
CATEGORIES = ("edit", "write", "bash", "git", "task", "webfetch", "studio_mcp",
              "external_directory")
EDIT_PERMS = ("edit", "patch", "multiedit", "apply_patch")
READ_PERMS = ("read", "list")
_SECRET_FILES = (".env", ".env.*", "*.env", "*.env.*", "*_secrets*", "*secret*")
_GIT_HARD = ("git push*--force*", "git push*-f", "git push*-f *", "git push*--tags*",
             "git push*--delete*", "git push*:refs/tags/*", "git tag -d*", "git tag*--delete*",
             "git commit*--no-verify*")


def _hard() -> list[dict]:
    out = []
    for perm in READ_PERMS + EDIT_PERMS + ("write",):
        for pat in _SECRET_FILES:
            out.append({"permission": perm, "pattern": pat, "action": "deny",
                        "why": "secret files are never readable or editable"})
    for perm in ("grep", "glob", "external_directory"):
        out.append({"permission": perm, "pattern": "*_secrets*", "action": "deny",
                    "why": "secret store"})
    for pat in _GIT_HARD:
        out.append({"permission": "bash", "pattern": pat, "action": "deny",
                    "why": "force push, tag push/deletion and hook bypass are never allowed"})
    out.append({"permission": "bash", "pattern": "*_secrets*", "action": "deny",
                "why": "secret store"})
    return out


HARD_DENIES = tuple(_RR.HARD_DENIES) if _RR else tuple(_hard())

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
                    default_cutoff: float = 95.0) -> dict:
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
            elif unknown and pol.get("fail_closed"):
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

def _norm(p: str) -> str:
    p = str(p or "").strip().replace("\\", "/")
    p = re.sub(r"/+", "/", p)
    return p.casefold()


def glob_to_regex(pattern: str) -> re.Pattern:
    p = _norm(pattern)
    out, i = [], 0
    while i < len(p):
        c = p[i]
        if c == "*":
            if p[i:i + 2] == "**":
                i += 2
                if p[i:i + 1] == "/":            # "**/" = any number of folders, also none
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(pattern: str, path: str) -> bool:
    """`**` crosses folders, `*` and `?` stay inside one. Case-insensitive,
    `\\` and `/` are the same (Windows paths). A relative pattern also matches
    at any depth below an absolute path's folder (`src/**` matches
    `C:\\Repo\\src\\a.lua`) because relative scopes are workspace-relative."""
    rx = glob_to_regex(pattern)
    n = _norm(path)
    if rx.match(n):
        return True
    pn = _norm(pattern)
    if pn and not re.match(r"^([a-z]:/|/|\*\*)", pn):
        return any(rx.match(n[m.end():]) for m in re.finditer("/", n))
    return False


def _rules(doc: dict, role: str) -> dict:
    """The role's rules over its defaults (C1 normalize)."""
    raw = (doc.get("role_rules") or {}).get(role)
    if _RR:
        try:
            return _RR.normalize(role, raw)
        except Exception:                                        # noqa: BLE001
            pass
    out = default_role_rules().get(role) or _base("", "deny")
    for k, v in (raw or {}).items():
        if k == "permissions" and isinstance(v, dict):
            out["permissions"].update(v)
        elif k in ("read_paths", "edit_paths", "bash") and isinstance(v, dict):
            out[k] = {"allow": list(v.get("allow") or []), "deny": list(v.get("deny") or [])}
        else:
            out[k] = v
    return out


def _kilo(value: str, pattern: str) -> bool:
    """Kilo's wildcard (* any run, slashes too); for paths the file name alone
    is tried as well, so ".env" also catches C:/x/.env."""
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    if re.fullmatch(rx, value, re.S | re.I):
        return True
    return "/" in value and bool(re.fullmatch(rx, value.rsplit("/", 1)[-1], re.S | re.I))


def _scope_match(glob: str, path: str, workspace: str = "") -> bool:
    """A role scope entry the way Kilo reads it: * runs across folders, a
    relative entry sits inside the workspace (C1 _path_forms)."""
    g = str(glob).strip().replace("\\", "/")
    if _kilo(path, g):
        return True
    if not re.match(r"^([A-Za-z]:/|/)", g) and workspace:
        return _kilo(path, workspace.strip().replace("\\", "/").rstrip("/") + "/" + g)
    return False


_OP_PERMS = {"read": READ_PERMS + ("grep", "glob", "external_directory"), "edit": EDIT_PERMS,
             "write": ("write",), "bash": ("bash",)}


def _res(decision: str, rule: str, reason: str, locked: bool = False) -> dict:
    return {"decision": decision, "rule": rule, "reason": reason, "locked": locked,
            "exact": False}


def path_test(doc: dict, role: str, op: str, value: str, workspace: str = "") -> dict:
    """What Kilo would decide for role/op/value: allow | ask | deny, the rule
    that decided it and whether it is a hard (locked) deny. With C1 present
    this is foundry_automation.role_rules.test itself (exact=True); without
    it, a close approximation over the same rules (exact=False)."""
    op = op if op in _OP_PERMS else "edit"
    raw = (doc.get("role_rules") or {}).get(role)
    if _RR:
        out = _RR.test(role, raw, op, value, workspace)
        r = out.get("rule") or {}
        # generated rules drop "why": a hard deny is recognised by its pattern
        hard = next((h for h in HARD_DENIES if r and r.get("action") == "deny" and
                     (h["permission"], h["pattern"]) == (r.get("permission"), r.get("pattern"))),
                    None)
        return {"decision": out.get("decision") or "deny", "exact": True,
                "locked": hard is not None,
                "rule": f"{r.get('permission')} {r.get('pattern')} → {r.get('action')}" if r
                else "no rule matched",
                "reason": (hard or {}).get("why") or r.get("why") or
                ("Kilo uses the last matching rule" if r else "nothing allows it")}
    rules = _rules(doc, role)
    v = str(value).strip()
    if op != "bash":
        v = v.replace("\\", "/")
    for h in doc.get("hard_denies") or HARD_DENIES:
        if h.get("permission") in _OP_PERMS[op] and _kilo(v, h.get("pattern", "")):
            return _res("deny", f"hard deny {h['permission']} {h['pattern']}",
                        h.get("why") or "hard deny", True)
    perms = rules.get("permissions") or {}
    if op == "bash":
        for pat in (rules.get("bash") or {}).get("deny") or []:
            if _kilo(v, pat):
                return _res("deny", f"bash deny {pat}", "a denied command pattern")
        for pat in (rules.get("bash") or {}).get("allow") or []:
            if _kilo(v, pat):
                return _res("allow", f"bash allow {pat}", "an allowed command pattern")
        cat = "git" if v == "git" or v.startswith("git ") else "bash"
        act = perms.get(cat, "deny")
        return _res(act, f"{cat} → {act}", f"the role's {cat} permission")
    if op == "read":
        for g in (rules.get("read_paths") or {}).get("deny") or []:
            if _scope_match(g, v, workspace):
                return _res("deny", f"read deny {g}", "matches a deny glob (deny wins)")
        return _res("allow", "read * → allow", "reads are allowed unless a deny glob matches")
    act = perms.get(op, "deny")
    if act == "deny":
        return _res("deny", f"{op} → deny", f"{op} is denied for this role")
    scope = rules.get("edit_paths") or {}
    for g in scope.get("deny") or []:
        if _scope_match(g, v, workspace):
            return _res("deny", f"{op} deny {g}", "matches a deny glob (deny wins)")
    allow = scope.get("allow") or []
    if not allow:
        return _res(act, f"{op} * → {act}", "no edit scope set: the whole workspace")
    for g in allow:
        if _scope_match(g, v, workspace):
            return _res(act, f"{op} allow {g}", "inside the edit scope")
    return _res("deny", "no allow glob matches", "outside the role's edit scope")


def _restricted(globs: list) -> bool:
    return bool(globs) and not any(_norm(g) in ("**", "**/*", "*") for g in globs)


def _role_of(path: str) -> str:
    parts = path.split(".")
    return parts[1] if len(parts) > 1 else ""


def rule_warnings(doc: dict) -> list[dict]:
    """C1's own validation (errors and warnings) when present, plus the shell
    bypass warning: shell allowed while edits are limited to a scope."""
    out = []
    rr = doc.get("role_rules") or {}
    if _RR:
        errors, warns = _RR.validate(rr)
        out += [{"role": _role_of(e["path"]), "severity": "bad",
                 "message": f"{e['path']}: {e['message']}"} for e in errors]
        out += [{"role": _role_of(w["path"]), "severity": "warn",
                 "message": f"{w['path']}: {w['message']}"} for w in warns]
    for role in rr:
        rules = _rules(doc, role)
        p = rules.get("permissions") or {}
        if p.get("bash") in ("allow", "ask") and p.get("edit", "deny") != "deny" and \
                _restricted((rules.get("edit_paths") or {}).get("allow")):
            out.append({"role": role, "severity": "warn",
                        "message": f"{role}: shell is {p['bash']}ed while edits are limited to a "
                                   f"scope. Shell commands can write outside it; the scope check "
                                   f"after the run is what catches that."})
        if not _RR:
            for t, st in p.items():
                if st not in STATES or t not in CATEGORIES:
                    out.append({"role": role, "severity": "bad",
                                "message": f"role_rules.{role}.permissions.{t}: {st!r} is not "
                                           f"a known category with allow, ask or deny"})
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


# --------------------------------------------------------------------------- stub seed

def _base(agent: str, default: str, **perms) -> dict:
    p = {c: "deny" for c in CATEGORIES}
    p.update(perms)
    return {"agent": agent, "default": default, "permissions": p,
            "read_paths": {"allow": [], "deny": []}, "edit_paths": {"allow": [], "deny": []},
            "bash": {"allow": [], "deny": []}, "task_agents": [], "deny_tools": [], "extra": []}


def default_role_rules() -> dict:
    """Ready-to-use rules per role: C1's defaults when present, else the STUB
    copy of them (same values)."""
    if _RR:
        return _RR.all_defaults()
    ro = [{"permission": "question", "pattern": "*", "action": "deny"}]
    impl = _base("code", "allow", edit="allow", write="allow", bash="allow", git="allow",
                 studio_mcp="allow")
    impl.update(task_agents=["explore"], bash={"allow": [], "deny": ["git tag*"]},
                deny_tools=["searxng_*", "crawl4ai_*"])
    lead = _base("lead", "allow", edit="allow", write="allow", bash="allow", git="allow",
                 task="allow", studio_mcp="allow", external_directory="allow")
    lead["extra"] = [dict(x) for x in ro]
    out = {"implementer": impl, "lead": lead}
    for role, agent in (("reviewer", "au-review"), ("deep_reviewer", "au-deep-review"),
                        ("visual_reviewer", "au-visual-review"), ("planner", "au-planner"),
                        ("researcher", "au-research"), ("helper", "au-helper")):
        r = _base(agent, "deny")
        r["extra"] = [dict(x) for x in ro]
        if role == "researcher":
            r["permissions"]["webfetch"] = "allow"
        if role == "helper":
            r["deny_tools"] = ["background_process", "schedule_wakeup", "cancel_wakeup",
                               "cron_*", "link_pr", "todowrite"]
        out[role] = r
    coord = _base("", "deny", edit="allow", write="allow")
    coord["extra"] = [dict(x) for x in ro]
    out["coordinator"] = coord
    return out


def _persona_routes(p: dict, models_by_backend: dict) -> list[dict]:
    def parse(v):
        if isinstance(v, list):
            return v
        try:
            x = json.loads(v or "[]")
        except (TypeError, ValueError):
            return []
        return x if isinstance(x, list) else []
    chain = parse(p.get("pinned_models")) or parse(p.get("model_allowlist"))
    out = []
    for m in chain:
        if not isinstance(m, str) or not m:
            continue
        be = p.get("backend") if p.get("backend") and m in models_by_backend.get(p["backend"], ()) else ""
        if not be:
            be = next((b for b, ms in models_by_backend.items() if m in ms), p.get("backend") or "")
        out.append({"backend": be, "model": m, "enabled": True})
    return out


def _set_path(d: dict, path: str, value) -> None:
    *head, last = path.split(".")
    for k in head:
        d = d.setdefault(k, {})
    d[last] = value


def seed_document(personas: list, backends: list, role_personas: dict, cutoffs: dict,
                  default_cutoff: float = 95.0) -> dict:
    """STUB (until C1's config store is on this router): today's personas,
    backends and role bindings as one GUI document. Role personas sit where C1
    keeps them (settings roles.<role>, lead_manager.persona)."""
    models_by_backend = {b["name"]: list(b.get("models") or []) for b in backends if b.get("name")}
    pmap = {}
    for p in personas or []:
        name = p.get("virtual_name") or p.get("name")
        if name:
            pmap[name] = {"routes": _persona_routes(p, models_by_backend),
                          "description": p.get("description") or ""}
    bpol = {}
    for b in backends or []:
        fam = backend_family(b.get("name", ""), b.get("flavor") or "", b.get("type") or "")
        sub = bool(b.get("subscription")) or fam in ("claude", "chatgpt")
        bpol[b["name"]] = {"enabled": b.get("enabled", True) is not False,
                           "cutoff_pct": (cutoffs.get(b["name"]) or default_cutoff) if sub else None,
                           "fail_closed": False}
    settings: dict = {}
    for role, path in ROLE_BINDING.items():
        if role_personas.get(role):
            _set_path(settings, path, role_personas[role])
    return {"revision": 0, "stub": True,
            "roles": [{"id": rid, "label": label} for rid, label in ROLES],
            "personas": pmap, "backends": bpol, "settings": settings,
            "role_rules": default_role_rules(), "hard_denies": [dict(h) for h in HARD_DENIES]}
