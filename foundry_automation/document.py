"""The automation document: what the router stores, versions and deploys.

    {"format": "foundry.automation/1", "project": "<id>",
     "settings": {<profile tables, JSON form>}, "role_rules": {<role>: {...}}}

`import_profile` turns a profile (parsed TOML) into a document, dropping the
local-only keys (secrets' env-var names, URLs, paths). `overlay` puts a
document back on top of a local profile: tables in the document replace the
local ones, except that local-only keys always keep their local values.
So `overlay(raw, import_profile(raw)) == raw` (plus role_rules).
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Optional

from . import FORMAT, ROUTER_DOC
from . import role_rules as RR
from .schema import (KNOWN_LANES, LOCAL_KEYS, PROFILE_SCHEMA, SettingsError, TABLES_BY_NAME,
                     automation_table, check_value, fast_lane_table, find_field, is_local_key,
                     lead_manager_table,
                     lifecycle_table, role_overrides_table, roles_table, walk_secrets)

# keys never part of a document (the coordinator's own identity)
_IDENTITY = ("schema_version", "id")


def canonical(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def doc_hash(doc: Any) -> str:
    return hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()[:16]


def empty(project: str) -> dict:
    d = {"format": FORMAT, "project": project, "settings": {}}
    if project != ROUTER_DOC:
        d["role_rules"] = RR.all_defaults()
    return d


# --------------------------------------------------------------------------- flatten helpers

def _walk(d: Any, prefix: str = ""):
    """(dotted path, value) for every leaf; lists are leaves."""
    if isinstance(d, dict) and d:
        for k, v in d.items():
            yield from _walk(v, f"{prefix}.{k}" if prefix else str(k))
    else:
        yield prefix, d


def flatten(d: Any) -> dict:
    return {p: v for p, v in _walk(d) if p}


def _pop_path(d: dict, path: str) -> bool:
    segs = path.split(".")
    cur = d
    for s in segs[:-1]:
        if not isinstance(cur, dict) or s not in cur:
            return False
        cur = cur[s]
    if isinstance(cur, dict) and segs[-1] in cur:
        del cur[segs[-1]]
        return True
    return False


def _get_path(d: dict, path: str, default=None):
    cur = d
    for s in path.split("."):
        if not isinstance(cur, dict) or s not in cur:
            return default
        cur = cur[s]
    return cur


def _set_path(d: dict, path: str, value) -> None:
    segs = path.split(".")
    cur = d
    for s in segs[:-1]:
        nxt = cur.get(s)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[s] = nxt
        cur = nxt
    cur[segs[-1]] = value


def local_paths(raw: dict) -> list[str]:
    """Every local-only key present in a profile (dotted)."""
    out = []
    for path, _ in _walk(raw):
        segs = path.split(".")
        # a local key may be a table-valued leaf's prefix (e.g. animaluprising.python list)
        for n in range(1, len(segs) + 1):
            p = ".".join(segs[:n])
            if is_local_key(p) and p not in out:
                out.append(p)
                break
    return out


# --------------------------------------------------------------------------- import / overlay

def import_profile(raw: dict, project: str = "") -> tuple[dict, list[str], list[str]]:
    """(document, dropped local keys, notes) from a parsed profile."""
    project = project or str(raw.get("id") or "")
    settings = copy.deepcopy({k: v for k, v in raw.items() if k not in _IDENTITY
                              and k != "role_rules"})
    dropped = []
    for p in local_paths(settings):
        if _pop_path(settings, p):
            dropped.append(p)
    # tables left empty by dropping local keys (e.g. [foundry] url+key_env) stay
    # as {} only when the table had project keys; drop fully-local tables
    for t in list(settings):
        if settings[t] == {} and isinstance(raw.get(t), dict) and raw[t]:
            del settings[t]
    notes = []
    if isinstance(raw.get("role_rules"), dict):
        rules = {r: RR.normalize(r, v) for r, v in raw["role_rules"].items()
                 if r in RR.RULE_ROLES}
        for r in RR.RULE_ROLES:
            rules.setdefault(r, RR.default_rules(r))
        notes.append("role_rules taken from the profile")
    else:
        rules = RR.seed_from_profile(raw)
        notes.append("role_rules seeded from defaults, client.permissions.external_directory, "
                     "helpers.deny_tools and lead_manager.write_access")
    doc = {"format": FORMAT, "project": project, "settings": settings, "role_rules": rules}
    return doc, dropped, notes


def overlay(raw: dict, doc: dict) -> dict:
    """A local profile with the document on top (local-only keys stay local)."""
    out = copy.deepcopy(raw)
    settings = (doc or {}).get("settings") or {}
    keep = {p: _get_path(raw, p) for p in local_paths(raw)}
    for t, v in settings.items():
        if t in _IDENTITY:
            continue
        out[t] = copy.deepcopy(v)
    for p, v in keep.items():
        _set_path(out, p, copy.deepcopy(v))
    for p in local_paths(settings):              # never take a local key from the router
        if p not in keep:
            _pop_path(out, p)
    if isinstance((doc or {}).get("role_rules"), dict):
        out["role_rules"] = copy.deepcopy(doc["role_rules"])
    return out


# --------------------------------------------------------------------------- validation

def _e(path: str, message: str, code: str = "invalid") -> dict:
    return {"path": path, "message": message, "code": code}


def validate(doc: Any, project: str = "") -> dict:
    """{ok, errors, warnings}. Shape, local keys, secrets, every known field's
    type and limits, the strict tables, role overrides and role rules."""
    errors: list[dict] = []
    warnings: list[dict] = []
    if not isinstance(doc, dict):
        return {"ok": False, "errors": [_e("", "the document must be a JSON object")],
                "warnings": []}
    if doc.get("format") != FORMAT:
        errors.append(_e("format", f"must be {FORMAT!r}"))
    if project and doc.get("project") not in (project, None, ""):
        errors.append(_e("project", f"document is for {doc.get('project')!r}, not {project!r}"))
    proj = project or str(doc.get("project") or "")
    for k in sorted(set(doc) - {"format", "project", "settings", "role_rules"}):
        errors.append(_e(k, "unknown top-level key", "unknown_key"))
    settings = doc.get("settings", {})
    if not isinstance(settings, dict):
        errors.append(_e("settings", "must be an object"))
        settings = {}
    for b in walk_secrets(settings):
        errors.append(_e("settings." + b.split(" ")[0], f"secret: {b}", "secret"))
    for p in local_paths(settings):
        errors.append(_e(p, "local-only setting: it stays in the coordinator's project.toml "
                            "and is never stored in the router", "local_key"))
    for k in _IDENTITY:
        if k in settings:
            errors.append(_e(k, "identity keys stay in the local profile", "local_key"))

    if proj == ROUTER_DOC:
        for t in settings:
            if t != "backends":
                errors.append(_e(t, "the router document holds only 'backends'", "unknown_key"))
        if doc.get("role_rules"):
            errors.append(_e("role_rules", "role rules belong in a project document",
                             "unknown_key"))
    else:
        if "backends" in settings:
            errors.append(_e("backends", "per-backend cutoffs live in the _router document",
                             "unknown_key"))

    # every leaf against the field registry (the nearest described ancestor:
    # a map/list field is checked whole, a table field covers what's under it)
    checked: set = set()
    local = set(local_paths(settings))
    for path, _ in _walk(settings):
        if not path or any(path == p or path.startswith(p + ".") for p in local):
            continue
        segs = path.split(".")
        f, at = None, ""
        for n in range(len(segs), 0, -1):
            g = find_field(".".join(segs[:n]))
            if g is not None:
                f, at = g, ".".join(segs[:n])
                break
        if f is None:
            t = TABLES_BY_NAME.get(segs[0])
            if t is None or not t.strict:      # strict tables: the validators below report
                warnings.append(_e(path, "not in the schema: kept verbatim, not type-checked",
                                   "unknown_key"))
            continue
        if at in checked or f.type == "table":
            continue
        checked.add(at)
        why = check_value(f, _get_path(settings, at))
        if why:
            errors.append(_e(at, why))

    if proj != ROUTER_DOC:
        where = "draft"
        checks = [("lifecycle", lambda: lifecycle_table(where, settings.get("lifecycle"))),
                  ("lead_manager", lambda: lead_manager_table(where, settings.get("lead_manager"))),
                  ("fast_lane", lambda: fast_lane_table(where, settings.get("fast_lane"),
                                                        KNOWN_LANES)),
                  ("role_overrides", lambda: role_overrides_table(where,
                                                                  settings.get("role_overrides"))),
                  ("roles", lambda: roles_table(where, settings.get("roles"))),
                  ("automation", lambda: automation_table(where, settings.get("automation")))]
        for table, fn in checks:
            if table not in settings and table != "roles":
                continue
            try:
                fn()
            except SettingsError as e:
                msg = str(e).split(": ", 1)[-1]
                errors.append(_e(e.path or table, msg))
        pub = settings.get("publish") or {}
        if isinstance(pub, dict) and (pub.get("force") or pub.get("tags") == "all"):
            errors.append(_e("publish", "force pushes and pushing all tags are never allowed"))
        e2, w2 = RR.validate(doc.get("role_rules"))
        errors += e2
        warnings += w2
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# --------------------------------------------------------------------------- diff

def diff(a: Optional[dict], b: Optional[dict]) -> list[dict]:
    """Changes from document a to b over settings and role_rules (dotted
    paths; lists compared whole)."""
    def flat(d):
        d = d or {}
        out = {}
        for top in ("settings", "role_rules"):
            for p, v in _walk(d.get(top) or {}):
                if p:
                    out[p if top == "settings" else f"role_rules.{p}"] = v
        return out
    fa, fb = flat(a), flat(b)
    out = []
    for p in sorted(set(fa) | set(fb)):
        f = find_field(p)
        apply = f.apply if f else ("next_session" if p.startswith("role_rules.") else "live")
        if p not in fa:
            out.append({"path": p, "op": "added", "from": None, "to": fb[p], "apply": apply})
        elif p not in fb:
            out.append({"path": p, "op": "removed", "from": fa[p], "to": None, "apply": apply})
        elif canonical(fa[p]) != canonical(fb[p]):
            out.append({"path": p, "op": "changed", "from": fa[p], "to": fb[p], "apply": apply})
    return out


def restart_needed(a: Optional[dict], b: Optional[dict]) -> list[str]:
    return [c["path"] for c in diff(a, b) if c["apply"] == "restart"]


__all__ = ["canonical", "doc_hash", "empty", "flatten", "import_profile", "overlay",
           "validate", "diff", "restart_needed", "local_paths", "LOCAL_KEYS", "PROFILE_SCHEMA"]
