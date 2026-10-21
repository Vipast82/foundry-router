"""Role rules: what each automation role may read, edit and run.

One rule set per role (implementer, lead, reviewer, ...):

  agent              Kilo agent the role runs as ("" = no kilo.jsonc block)
  default            agent block only: the action for "*" (Kilo's catch-all)
  permissions        category -> allow | ask | deny (see CATEGORIES)
  read_paths/edit_paths  {"allow": [...], "deny": [...]} path globs; absolute
                     Windows paths are fine (C:\\Roblox\\** -> C:/Roblox/**)
  bash               {"allow": [...], "deny": [...]} command patterns
  task_agents        subagents the role may start when `task` isn't deny
  deny_tools         tool-name globs denied (MCP tools etc.)
  extra              raw Kilo rules [{permission, pattern, action}]

Two generated forms, from the same rules:
  session_rules(...)  Kilo `permission[]` for POST /session (the coordinator
                      appends them after the adapter's own rules)
  agent_block(...)    `agent.<name>.permission` for kilo.jsonc

Kilo uses the LAST matching rule, so the order is: category bases, scopes,
allow lists, deny lists, extra, and finally HARD_DENIES, which no setting can
remove (secrets, force pushes, tag deletion, hooks bypass).
"""

from __future__ import annotations

import copy
import ntpath
import posixpath
import re
from typing import Any, Optional

from .schema import RULE_ROLES

ACTIONS = ("allow", "ask", "deny")
CATEGORIES = ("edit", "write", "bash", "git", "task", "webfetch", "studio_mcp",
              "external_directory")
EDIT_PERMS = ("edit", "patch", "multiedit", "apply_patch")
READ_PERMS = ("read", "list")
STUDIO_TOOLS = ("Roblox_Studio_*", "robloxstudio-mcp_*", "robloxstudio_mcp_*")
WEB_PERMS = ("webfetch", "websearch")
# Roles a coordinator runs unattended: "ask" waits for the operator.
UNATTENDED = ("implementer", "reviewer", "deep_reviewer", "visual_reviewer", "planner",
              "researcher", "helper", "coordinator", "lead")

_SECRET_FILES = (".env", ".env.*", "*.env", "*.env.*", "*_secrets*", "*secret*")
_SECRET_BASH = ("*_secrets*",)
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
    for pat in _SECRET_BASH:
        out.append({"permission": "bash", "pattern": pat, "action": "deny",
                    "why": "secret store"})
    return out


HARD_DENIES: tuple = tuple(_hard())


class RuleError(ValueError):
    pass


# --------------------------------------------------------------------------- defaults

def _base(agent: str, default: str, **perms) -> dict:
    p = {c: "deny" for c in CATEGORIES}
    p.update(perms)
    return {"agent": agent, "default": default, "permissions": p,
            "read_paths": {"allow": [], "deny": []},
            "edit_paths": {"allow": [], "deny": []},
            "bash": {"allow": [], "deny": []},
            "task_agents": [], "deny_tools": [], "extra": []}


_READ_ONLY_EXTRA = [{"permission": "question", "pattern": "*", "action": "deny"}]


def default_rules(role: str) -> dict:
    """Ready-to-use rules per role (mirrors the 2026-10-21 live setup, with the
    revamp's B6 rule: no subagents except explore)."""
    if role not in RULE_ROLES:
        raise RuleError(f"unknown role {role!r} (known: {', '.join(RULE_ROLES)})")
    if role == "implementer":
        r = _base("code", "allow", edit="allow", write="allow", bash="allow", git="allow",
                  task="deny", studio_mcp="allow", external_directory="deny")
        r["task_agents"] = ["explore"]
        r["bash"]["deny"] = ["git tag*"]
        r["deny_tools"] = ["searxng_*", "crawl4ai_*"]
        return r
    if role == "lead":
        r = _base("lead", "allow", edit="allow", write="allow", bash="allow", git="allow",
                  task="allow", studio_mcp="allow", external_directory="allow")
        r["extra"] = [{"permission": "question", "pattern": "*", "action": "deny"}]
        return r
    if role == "coordinator":
        # coordinator repair sessions (lead.py repair_permissions narrows further)
        r = _base("", "deny", edit="allow", write="allow")
        r["extra"] = copy.deepcopy(_READ_ONLY_EXTRA)
        return r
    agents = {"reviewer": "au-review", "deep_reviewer": "au-deep-review",
              "visual_reviewer": "au-visual-review", "planner": "au-planner",
              "researcher": "au-research", "helper": "au-helper"}
    r = _base(agents[role], "deny")
    if role == "researcher":
        r["permissions"]["webfetch"] = "allow"
    r["extra"] = copy.deepcopy(_READ_ONLY_EXTRA)
    if role == "helper":
        r["deny_tools"] = ["background_process", "schedule_wakeup", "cancel_wakeup", "cron_*",
                           "link_pr", "todowrite"]
    return r


def all_defaults() -> dict:
    return {r: default_rules(r) for r in RULE_ROLES}


# --------------------------------------------------------------------------- normalise / validate

def norm_path(p: str) -> str:
    """Forward slashes; drive letter kept (Kilo patterns use C:/Roblox/*)."""
    return str(p).strip().replace("\\", "/")


def is_abs(p: str) -> bool:
    p = norm_path(p)
    return bool(re.match(r"^[A-Za-z]:/", p)) or p.startswith("/")


def normalize(role: str, rules: Optional[dict]) -> dict:
    """Defaults for the role, overlaid with `rules` (partial is fine)."""
    out = default_rules(role)
    if not rules:
        return out
    for k, v in rules.items():
        if k in ("permissions",) and isinstance(v, dict):
            out["permissions"].update(v)
        elif k in ("read_paths", "edit_paths", "bash") and isinstance(v, dict):
            out[k] = {"allow": list(v.get("allow") or []), "deny": list(v.get("deny") or [])}
        else:
            out[k] = copy.deepcopy(v)
    return out


def validate(role_rules: Any, where: str = "role_rules") -> tuple[list[dict], list[dict]]:
    """(errors, warnings): each {path, message, code}."""
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(path, msg, code="invalid"):
        errors.append({"path": path, "message": msg, "code": code})

    def warn(path, msg, code):
        warnings.append({"path": path, "message": msg, "code": code})

    if role_rules is None:
        return errors, warnings
    if not isinstance(role_rules, dict):
        err(where, "must be a table of role -> rules")
        return errors, warnings
    hard = {(h["permission"], h["pattern"]) for h in HARD_DENIES}
    for role, rules in role_rules.items():
        base = f"{where}.{role}"
        if role not in RULE_ROLES:
            err(base, f"unknown role (known: {', '.join(RULE_ROLES)})", "unknown_role")
            continue
        if not isinstance(rules, dict):
            err(base, "must be a table")
            continue
        known = {"agent", "default", "permissions", "read_paths", "edit_paths", "bash",
                 "task_agents", "deny_tools", "extra"}
        for k in sorted(set(rules) - known):
            err(f"{base}.{k}", "unknown key", "unknown_key")
        if "agent" in rules and not isinstance(rules["agent"], str):
            err(f"{base}.agent", "must be a string (empty = no kilo.jsonc block)")
        if "default" in rules and rules["default"] not in ACTIONS:
            err(f"{base}.default", f"must be one of {', '.join(ACTIONS)}")
        perms = rules.get("permissions", {})
        if not isinstance(perms, dict):
            err(f"{base}.permissions", "must be a table")
            perms = {}
        for cat, act in perms.items():
            if cat not in CATEGORIES:
                err(f"{base}.permissions.{cat}", f"unknown category (known: "
                    f"{', '.join(CATEGORIES)})", "unknown_key")
            elif act not in ACTIONS:
                err(f"{base}.permissions.{cat}", f"must be one of {', '.join(ACTIONS)}")
            elif act == "ask" and role in UNATTENDED:
                warn(f"{base}.permissions.{cat}", "'ask' in an unattended session waits for "
                     "the operator to answer", "ask_unattended")
        for key in ("read_paths", "edit_paths", "bash"):
            if key not in rules:
                continue
            v = rules[key]
            if not isinstance(v, dict) or set(v) - {"allow", "deny"}:
                err(f"{base}.{key}", "must be {allow: [...], deny: [...]}")
                continue
            for side in ("allow", "deny"):
                lst = v.get(side) or []
                if not isinstance(lst, list) or any(not isinstance(x, str) or not x.strip()
                                                    for x in lst):
                    err(f"{base}.{key}.{side}", "must be a list of non-empty strings")
                    continue
                for x in lst:
                    if key != "bash" and not is_abs(x) and ".." in norm_path(x).split("/"):
                        err(f"{base}.{key}.{side}", f"{x!r}: a relative path may not leave the "
                            "workspace (give an absolute path instead)", "path_escape")
                    if side == "allow":
                        perm = "bash" if key == "bash" else ("read" if key == "read_paths"
                                                             else "edit")
                        if (perm, norm_path(x) if key != "bash" else x) in hard:
                            warn(f"{base}.{key}.allow", f"{x!r} is always denied (hard deny)",
                                 "hard_deny_overrides")
        for key in ("task_agents", "deny_tools"):
            if key in rules and (not isinstance(rules[key], list) or any(
                    not isinstance(x, str) or not x.strip() for x in rules[key])):
                err(f"{base}.{key}", "must be a list of non-empty strings")
        extra = rules.get("extra", [])
        if not isinstance(extra, list):
            err(f"{base}.extra", "must be a list of {permission, pattern, action}")
            extra = []
        for i, r in enumerate(extra):
            if not isinstance(r, dict) or not isinstance(r.get("permission"), str) or \
                    not r.get("permission") or not isinstance(r.get("pattern", "*"), str) or \
                    r.get("action") not in ACTIONS or set(r) - {"permission", "pattern", "action"}:
                err(f"{base}.extra[{i}]", "must be {permission, pattern, action: allow|ask|deny}")
            elif r.get("action") != "deny" and (r["permission"], r.get("pattern", "*")) in hard:
                warn(f"{base}.extra[{i}]", "always denied (hard deny)", "hard_deny_overrides")
    return errors, warnings


# --------------------------------------------------------------------------- generation

def _path_forms(p: str, workspace: str) -> list[str]:
    """Patterns for one scope entry. Kilo matches edit paths relative to the
    session's worktree and external_directory against absolute paths, so an
    absolute entry also yields its workspace-relative form."""
    p = norm_path(p)
    forms = [p]
    if workspace and is_abs(p):
        ws = norm_path(workspace).rstrip("/")
        try:
            rel = ntpath.relpath(p.replace("/", "\\"), ws.replace("/", "\\")).replace("\\", "/")
        except ValueError:              # another drive
            rel = ""
        if rel and rel != ".":
            forms.append(rel)
        # a glob whose directory holds the workspace covers the workspace itself
        head = re.split(r"[*?\[]", p, maxsplit=1)[0].rstrip("/")
        if p.endswith("*") and head and (ws.lower() + "/").startswith(head.lower() + "/"):
            forms.append("*")
    return list(dict.fromkeys(forms))


def _abs_dirs(globs: list[str]) -> list[str]:
    """external_directory patterns for absolute scope entries: the directory
    before the first wildcard, plus everything below it."""
    out = []
    for g in globs:
        g = norm_path(g)
        if not is_abs(g):
            continue
        head = re.split(r"[*?\[]", g, maxsplit=1)[0].rstrip("/")
        if not re.search(r"[*?\[]", g):
            head = posixpath.dirname(head) if posixpath.splitext(head)[1] else head
        if head:
            out += [head, head + "/*"]
    return list(dict.fromkeys(out))


def session_rules(role: str, rules: Optional[dict] = None, workspace: str = "",
                  hard: bool = True) -> list[dict]:
    """Kilo session `permission[]` for one role. No "*" catch-all: the
    adapter's own rules (agent_manager, doom_loop ... denies) stay in force."""
    r = normalize(role, rules)
    p = r["permissions"]
    out: list[dict] = []

    def add(perm, pattern, action):
        out.append({"permission": perm, "pattern": pattern, "action": action})

    # edit / write with path scope
    for cat, perms in (("edit", EDIT_PERMS), ("write", ("write",))):
        act = p.get(cat, "deny")
        allow = r["edit_paths"]["allow"]
        for perm in perms:
            add(perm, "*", act if not allow or act == "deny" else "deny")
            if act != "deny":
                for g in allow:
                    for form in _path_forms(g, workspace):
                        add(perm, form, act)
            for g in r["edit_paths"]["deny"]:
                for form in _path_forms(g, workspace):
                    add(perm, form, "deny")
    # reads: allowed by default (a reviewer's agent block starts from "*": deny)
    for perm in READ_PERMS + ("glob", "grep"):
        add(perm, "*", "allow")
    for g in r["read_paths"]["allow"]:
        for perm in READ_PERMS:
            for form in _path_forms(g, workspace):
                add(perm, form, "allow")
    for g in r["read_paths"]["deny"]:
        for perm in READ_PERMS:
            for form in _path_forms(g, workspace):
                add(perm, form, "deny")
    # shell, then git on top of it
    add("bash", "*", p.get("bash", "deny"))
    git = p.get("git", "deny")
    add("bash", "git", git)
    add("bash", "git *", git)
    for pat in r["bash"]["allow"]:
        add("bash", pat, "allow")
    for pat in r["bash"]["deny"]:
        add("bash", pat, "deny")
    # subagents
    add("task", "*", p.get("task", "deny"))
    if p.get("task") != "deny" or r["task_agents"]:
        for a in r["task_agents"]:
            add("task", a, "allow")
    # web, Studio
    for perm in WEB_PERMS:
        add(perm, "*", p.get("webfetch", "deny"))
    for tool in STUDIO_TOOLS:
        add(tool, "*", p.get("studio_mcp", "deny"))
    # outside the workspace
    add("external_directory", "*", p.get("external_directory", "deny"))
    scoped = r["read_paths"]["allow"] + (r["edit_paths"]["allow"]
                                         if p.get("edit") != "deny" else [])
    for d in _abs_dirs(scoped):
        add("external_directory", d, "allow")
    for tool in r["deny_tools"]:
        add(tool, "*", "deny")
    for x in r["extra"]:
        add(x["permission"], x.get("pattern", "*"), x["action"])
    if hard:
        for h in HARD_DENIES:
            add(h["permission"], h["pattern"], "deny")
    return out


def fold(rules: list[dict]) -> dict:
    """Kilo's object form of a rule list, order kept (a repeated
    permission/pattern moves to the end so the last rule still wins)."""
    obj: dict = {}
    for r in rules:
        perm, pat, act = r["permission"], r.get("pattern", "*"), r["action"]
        cur = obj.get(perm)
        if cur is None:
            obj[perm] = act if pat == "*" else {pat: act}
            continue
        if isinstance(cur, str):
            cur = {"*": cur}
        cur.pop(pat, None)
        cur[pat] = act
        # a perm whose only pattern is * stays a plain string
        obj.pop(perm)
        obj[perm] = cur["*"] if list(cur) == ["*"] else cur
    return obj


def agent_block(role: str, rules: Optional[dict] = None, workspace: str = "") -> dict:
    """kilo.jsonc `agent.<name>.permission` for one role."""
    r = normalize(role, rules)
    lst = [{"permission": "*", "pattern": "*", "action": r.get("default") or "deny"}]
    lst += session_rules(role, r, workspace)
    return fold(lst)


def agent_blocks(role_rules: Optional[dict], workspace: str = "") -> dict:
    """{agent name: permission block} for every role with an agent."""
    out = {}
    for role in RULE_ROLES:
        r = normalize(role, (role_rules or {}).get(role))
        if r.get("agent"):
            out[r["agent"]] = agent_block(role, r, workspace)
    return out


# --------------------------------------------------------------------------- what Kilo decides

def wildcard(value: str, pattern: str) -> bool:
    """Kilo's wildcard: * any run (slashes too), ? one char; a trailing " *"
    also matches the bare command ("git *" matches "git")."""
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    if pattern.endswith(" *"):
        rx = rx[: -len(re.escape(" ") + ".*")] + "(?: .*)?"
    return re.fullmatch(rx, value, re.S) is not None


def decide(rules: list[dict], perm: str, value: str) -> tuple[Optional[str], Optional[dict]]:
    """(action, rule) Kilo picks for `perm` on `value`: the last match wins."""
    hit = None
    for r in rules:
        if wildcard(perm, str(r.get("permission"))) and \
                wildcard(value, str(r.get("pattern", "*"))):
            hit = r
    return (hit["action"] if hit else None), hit


def test(role: str, rules: Optional[dict], op: str, value: str, workspace: str = "") -> dict:
    """The GUI path tester: op read | edit | write | bash | <permission>."""
    lst = agent_block_rules(role, rules, workspace)
    perm = {"read": "read", "edit": "edit", "write": "write", "bash": "bash"}.get(op, op)
    v = norm_path(value) if op in ("read", "edit", "write") else value
    candidates = [v]
    if op in ("read", "edit", "write"):
        candidates = _path_forms(v, workspace)
    decision, rule = None, None
    for c in candidates:
        d, rr = decide(lst, perm, c)
        if d == "deny":
            decision, rule = d, rr
            break
        if d and decision is None:
            decision, rule = d, rr
    if op in ("read", "edit", "write") and is_abs(v) and workspace and decision != "deny":
        ws = norm_path(workspace).rstrip("/")
        if not v.lower().startswith(ws.lower() + "/"):
            d, rr = decide(lst, "external_directory", posixpath.dirname(v))
            if d != "allow":
                decision, rule = d or "deny", rr
    return {"decision": decision or "deny", "rule": rule}


def agent_block_rules(role: str, rules: Optional[dict], workspace: str = "") -> list[dict]:
    r = normalize(role, rules)
    return ([{"permission": "*", "pattern": "*", "action": r.get("default") or "deny"}]
            + session_rules(role, r, workspace))


# --------------------------------------------------------------------------- import

def seed_from_profile(raw: dict) -> dict:
    """Role rules for a profile that has none: the defaults, plus the legacy
    profile settings that already said the same thing."""
    out = all_defaults()
    perms = ((raw.get("client") or {}).get("permissions") or {})
    ext = perms.get("external_directory")
    dirs = []
    if isinstance(ext, dict):
        for pat, act in ext.items():
            if act == "allow" and is_abs(pat):
                d = norm_path(pat).rstrip("*").rstrip("/")
                if d and d + "/**" not in dirs:
                    dirs.append(d + "/**")
    for role in out:
        if role in ("lead",):
            continue
        out[role]["read_paths"]["allow"] = list(dirs)
    deny_tools = (raw.get("helpers") or {}).get("deny_tools")
    if isinstance(deny_tools, list):
        out["helper"]["deny_tools"] = list(dict.fromkeys(
            [str(t) for t in deny_tools] + out["helper"]["deny_tools"]))
    lm = raw.get("lead_manager") or {}
    if lm.get("agent"):
        out["lead"]["agent"] = str(lm["agent"])
    if lm and not lm.get("write_access", False):
        out["lead"] = _base(out["lead"]["agent"], "deny")
        out["lead"]["extra"] = copy.deepcopy(_READ_ONLY_EXTRA)
        out["lead"]["permissions"]["git"] = "deny"
    h = raw.get("helpers") or {}
    if h.get("agent"):
        out["helper"]["agent"] = str(h["agent"])
    c = raw.get("client") or {}
    if c.get("agent"):
        out["implementer"]["agent"] = str(c["agent"])
    return out
