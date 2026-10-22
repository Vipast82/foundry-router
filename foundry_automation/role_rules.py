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
allow lists, deny lists, extra, the config-file denies (every role but the
coordinator) and finally HARD_DENIES, which no setting removes: secrets and
credential files (tools and shell), force pushes, ref deletion, history
rewrites, hook bypasses, and the Studio tools no role may ever call
(monetization, uploads, asset insertion, live script mutation).

Edit scope (W2.2): an EMPTY `edit_paths.allow` means "the sprint's files
only". The coordinator passes the sprint's scope (`scope=`) and it becomes the
allow list; `edit_paths.allow` entries ADD to it (paths every sprint of that
role may touch), `edit_paths.deny` always applies. Without a sprint (the GUI
tester, kilo.jsonc agent blocks) an empty allow list is the workspace. Read
scopes never grant write: a folder opened only for reading is denied to the
edit and write tools explicitly.

Studio (W2.1): `studio_read` governs the Studio MCP servers as a whole,
`studio_write` the tools that change Studio state (set/create/delete/insert/
mass/run/execute...). A document that still says `studio_mcp` is accepted:
its value applies to both until the draft is saved again.
"""

from __future__ import annotations

import copy
import ntpath
import posixpath
import re
from typing import Any, Optional

from .schema import RULE_ROLES

ACTIONS = ("allow", "ask", "deny")
CATEGORIES = ("edit", "write", "bash", "git", "task", "webfetch", "studio_read",
              "studio_write", "external_directory")
# older documents: category -> the categories that replaced it
LEGACY_CATEGORIES = {"studio_mcp": ("studio_read", "studio_write")}
EDIT_PERMS = ("edit", "patch", "multiedit", "apply_patch")
READ_PERMS = ("read", "list")
STUDIO_TOOLS = ("Roblox_Studio_*", "robloxstudio-mcp_*", "robloxstudio_mcp_*")
# tool-name globs (after the server prefix) that change Studio state; the
# rest (list / get / search / screenshot / console) is `studio_read`
STUDIO_WRITE_TOOLS = ("set_*", "create_*", "delete_*", "insert_*", "mass_*", "*duplicate*",
                      "rename_*", "move_*", "run_code*", "run_*", "execute_*", "start_*",
                      "stop_*", "save_*", "publish_*", "clear_*", "undo*", "redo*",
                      "select*", "play*")
# Studio tools NO role may call (account / Robux effects, live script mutation
# outside git): the live kilo.jsonc global denies, now a hard deny
STUDIO_NEVER = ("*manage_monetization", "*upload_image", "*insert_asset", "*multi_edit",
                "*set_script_source", "*edit_script_lines", "*insert_script_lines",
                "*delete_script_lines", "*find_and_replace_in_scripts")
WEB_PERMS = ("webfetch", "websearch")
# Roles a coordinator runs unattended: "ask" waits for the operator.
UNATTENDED = ("implementer", "reviewer", "deep_reviewer", "visual_reviewer", "planner",
              "researcher", "helper", "coordinator", "lead")

_SECRET_FILES = (".env", ".env.*", "*.env", "*.env.*", "*_secrets*", "*secret*", "*credential*")
# shell: secret stores, .env files, credential files and the environment
# itself (FOUNDRY_COORDINATOR_KEY, KILO_SERVER_PASSWORD live there)
_SECRET_BASH = ("*_secrets*", "*credential*", "*.foundry-coordinator*",
                "* .env", "* .env.*", "* .env *", "*/.env", "*/.env.*", "*/.env *",
                "*\\.env", "*\\.env.*", "*\\.env *",
                "env", "printenv*", "set", "export", "*$env:*", "*$Env:*", "*$ENV:*",
                "*Env:*", "*env:*", "*FOUNDRY_COORDINATOR_KEY*", "*KILO_SERVER_PASSWORD*")
# git: force pushes, ref deletion, tag push / deletion, history rewrites, hook
# bypasses. Plain `git push` is a ROLE rule (closeout sessions may publish).
_GIT_HARD = ("git push*--force*", "git push*-f", "git push*-f *", "git push*--tags*",
             "git push*--delete*", "git push*:refs/tags/*", "git push*--mirror*",
             "git push*--prune*", "git push*+*", "git push* :*", "git push*:refs/*",
             "git tag -d*", "git tag*--delete*",
             "git*--no-verify*", "git commit*--amend*", "git commit* -n", "git commit* -n *",
             "git reset*--hard*", "git rebase*", "git update-ref*", "git filter-*",
             "git*hooksPath*", "git -c *", "git reflog expire*", "git gc*--prune*")
# files every role but the coordinator leaves alone: the Kilo config, Kilo's
# own state, git internals and hook folders (relative and absolute forms)
CONFIG_FILES = ("kilo.jsonc", "*/kilo.jsonc", "kilo.json", "*/kilo.json",
                ".kilo/*", "*/.kilo/*", ".git/*", "*/.git/*",
                "tools/hooks/*", "*/tools/hooks/*", ".githooks/*", "*/.githooks/*",
                ".husky/*", "*/.husky/*")


def _hard() -> list[dict]:
    out = []
    for perm in READ_PERMS + EDIT_PERMS + ("write",):
        for pat in _SECRET_FILES:
            out.append({"permission": perm, "pattern": pat, "action": "deny",
                        "why": "secret and credential files are never readable or editable"})
    for perm in ("grep", "glob", "external_directory"):
        for pat in ("*_secrets*", "*secret*", "*credential*"):
            out.append({"permission": perm, "pattern": pat, "action": "deny",
                        "why": "secret store"})
    for pat in _GIT_HARD:
        out.append({"permission": "bash", "pattern": pat, "action": "deny",
                    "why": "force push, ref/tag deletion, history rewrite and hook bypass "
                           "are never allowed"})
    for pat in _SECRET_BASH:
        out.append({"permission": "bash", "pattern": pat, "action": "deny",
                    "why": "secret store, .env / credential files and the environment are "
                           "never readable from the shell"})
    for pat in STUDIO_NEVER:
        out.append({"permission": pat, "pattern": "*", "action": "deny",
                    "why": "Studio monetization, uploads, asset insertion and live script "
                           "mutation are never allowed (scripts change through git)"})
    return out


HARD_DENIES: tuple = tuple(_hard())


def config_denies() -> list[dict]:
    """Edit/write denies for the config files (every role but the coordinator)."""
    return [{"permission": perm, "pattern": pat, "action": "deny"}
            for perm in EDIT_PERMS + ("write",) for pat in CONFIG_FILES]


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
    revamp's B6 rule: no subagents except explore, and W1: no `question` tool
    for an unattended session)."""
    if role not in RULE_ROLES:
        raise RuleError(f"unknown role {role!r} (known: {', '.join(RULE_ROLES)})")
    if role == "implementer":
        r = _base("code", "allow", edit="allow", write="allow", bash="allow", git="allow",
                  task="deny", studio_read="allow", studio_write="allow",
                  external_directory="deny")
        r["task_agents"] = ["explore"]
        r["bash"]["deny"] = ["git tag*"]
        r["deny_tools"] = ["searxng_*", "crawl4ai_*"]
        r["extra"] = copy.deepcopy(_READ_ONLY_EXTRA)
        return r
    if role == "lead":
        r = _base("lead", "allow", edit="allow", write="allow", bash="allow", git="allow",
                  task="deny", studio_read="allow", studio_write="allow",
                  external_directory="allow")
        r["task_agents"] = ["explore"]
        r["bash"]["deny"] = ["git push", "git push *"]
        r["extra"] = copy.deepcopy(_READ_ONLY_EXTRA)
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


def migrate_permissions(perms: dict) -> dict:
    """A permissions table with legacy categories mapped onto their
    replacements (an explicit new value wins over the legacy one)."""
    out = {k: v for k, v in perms.items() if k not in LEGACY_CATEGORIES}
    for old, news in LEGACY_CATEGORIES.items():
        if old in perms:
            for n in news:
                out.setdefault(n, perms[old])
    return out


def normalize(role: str, rules: Optional[dict]) -> dict:
    """Defaults for the role, overlaid with `rules` (partial is fine; legacy
    categories migrated)."""
    out = default_rules(role)
    if not rules:
        return out
    for k, v in rules.items():
        if k in ("permissions",) and isinstance(v, dict):
            out["permissions"].update(migrate_permissions(v))
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
            if cat in LEGACY_CATEGORIES:
                if act not in ACTIONS:
                    err(f"{base}.permissions.{cat}", f"must be one of {', '.join(ACTIONS)}")
                else:
                    warn(f"{base}.permissions.{cat}", f"{cat} is now "
                         f"{' + '.join(LEGACY_CATEGORIES[cat])}; the value applies to both "
                         "until the draft is saved again", "legacy_key")
                continue
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
                        pat = norm_path(x) if key != "bash" else x
                        if (perm, pat) in hard or (key == "edit_paths" and
                                                   role != "coordinator" and
                                                   pat in CONFIG_FILES):
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
            elif r.get("action") != "deny" and (
                    (r["permission"], r.get("pattern", "*")) in hard or
                    any(wildcard(r["permission"], n) for n in STUDIO_NEVER)):
                warn(f"{base}.extra[{i}]", "always denied (hard deny)", "hard_deny_overrides")
    return errors, warnings


# --------------------------------------------------------------------------- generation

def _rel(p: str, workspace: str) -> str:
    """`p` relative to the workspace as Kilo shows edit paths ("" = another
    drive or no workspace)."""
    if not workspace:
        return ""
    ws = norm_path(workspace).rstrip("/")
    try:
        rel = ntpath.relpath(p.replace("/", "\\"), ws.replace("/", "\\")).replace("\\", "/")
    except ValueError:                  # another drive
        return ""
    return "" if rel == "." else rel


def _ups(rel: str) -> int:
    n = 0
    while rel.startswith("../"):
        rel = rel[3:]
        n += 1
    return n


def _path_forms(p: str, workspace: str) -> list[str]:
    """Patterns for one scope entry: the entry itself and, for an absolute
    entry, its workspace-relative form (Kilo matches edit paths relative to
    the session's worktree, external_directory against absolute paths)."""
    p = norm_path(p)
    forms = [p]
    if workspace and is_abs(p):
        rel = _rel(p, workspace)
        if rel:
            forms.append(rel)
    return list(dict.fromkeys(forms))


def _glob_head(p: str) -> str:
    return re.split(r"[*?\[]", p, maxsplit=1)[0].rstrip("/")


def _covers_workspace(p: str, workspace: str) -> bool:
    """An absolute glob whose directory holds the workspace (C:/Roblox/** for
    C:/Roblox/AnimalUprising) covers every workspace file."""
    if not (workspace and is_abs(p) and p.endswith("*")):
        return False
    head = _glob_head(p).lower()
    ws = norm_path(workspace).rstrip("/").lower()
    return bool(head) and (ws + "/").startswith(head + "/")


def _scope_rules(perm: str, action: str, allow: list[str], deny: list[str],
                 workspace: str, closed: bool = False) -> list[dict]:
    """Edit/write scope as Kilo rules (last match wins). Kilo's `*` matches
    `/` and `..` too, so every relative allow is followed by a deny for paths
    that climb further out than it does; allows are emitted narrowest first
    (fewest `../`) so a broader entry later re-allows what it covers.
    `closed`: an empty allow list denies everything (a sprint with no files)
    instead of allowing the workspace."""
    if action != "deny" and not allow and closed:
        out = [{"permission": perm, "pattern": "*", "action": "deny"}]
    else:
        out = [{"permission": perm, "pattern": "*",
                "action": action if not allow or action == "deny" else "deny"}]

    def add(pattern, act):
        out.append({"permission": perm, "pattern": pattern, "action": act})

    if action != "deny" and allow:
        inside, outside = [], []                     # inside: no "../"; outside: (ups, rel)
        for g in allow:
            n = norm_path(g)
            if is_abs(n):
                add(n, action)                       # absolute form (abs paths, if shown)
                if _covers_workspace(n, workspace):
                    inside.append("*")
                rel = _rel(n, workspace)
                if rel:
                    (outside.append((_ups(rel), rel)) if rel.startswith("../")
                     else inside.append(rel))
            else:
                inside.append(n)
        for pat in dict.fromkeys(inside):
            add(pat, action)
        if inside:
            add("../*", "deny")
        for ups, rel in sorted(dict.fromkeys(outside)):
            add(rel, action)
            add("../" * (ups + 1) + "*", "deny")
    for g in deny:
        for form in _path_forms(g, workspace):
            add(form, "deny")
    return out


def kilo_path(value: str, workspace: str) -> str:
    """The path Kilo checks for an edit: relative to the workspace when it can be."""
    v = norm_path(value)
    if is_abs(v):
        return _rel(v, workspace) or v
    return v


def _abs_dirs(globs: list[str]) -> list[str]:
    """external_directory patterns for absolute scope entries: the directory
    before the first wildcard, plus everything below it."""
    out = []
    for g in globs:
        g = norm_path(g)
        if not is_abs(g):
            continue
        head = _glob_head(g)
        if not re.search(r"[*?\[]", g):
            head = posixpath.dirname(head) if posixpath.splitext(head)[1] else head
        if head:
            out += [head, head + "/*"]
    return list(dict.fromkeys(out))


def _read_only_dirs(read_allow: list[str], edit_allow: list[str], workspace: str) -> list[str]:
    """Absolute folders opened for reading that no absolute edit entry covers:
    edit/write deny patterns for them (absolute and workspace-relative forms),
    so a read grant never becomes a write grant through external_directory."""
    heads = [_glob_head(norm_path(g)).lower() for g in edit_allow if is_abs(norm_path(g))]
    heads = [h for h in heads if h]
    ws = norm_path(workspace).rstrip("/").lower() if workspace else ""
    out: list[str] = []
    for d in _abs_dirs(read_allow):
        if d.endswith("/*"):
            continue
        dl = d.lower()
        if any((dl + "/").startswith(h + "/") for h in heads):
            continue                               # the edit scope opens it too
        rel = _rel(d, workspace)
        if ws and rel and not rel.startswith("../") and rel != "..":
            continue                               # inside the workspace: the edit scope rules
        out.append(d + "/*")
        if rel and (rel.startswith("../") or rel == ".."):
            out.append(rel + "/*")
        elif ws and (ws + "/").startswith(dl + "/"):
            out.append("../*")                     # the folder holds the workspace
    return list(dict.fromkeys(out))


def _studio_rules(p: dict) -> list[dict]:
    out = []
    for tool in STUDIO_TOOLS:
        out.append({"permission": tool, "pattern": "*", "action": p.get("studio_read", "deny")})
    for tool in STUDIO_TOOLS:
        for w in STUDIO_WRITE_TOOLS:
            out.append({"permission": tool[:-1] + w, "pattern": "*",
                        "action": p.get("studio_write", "deny")})
    return out


def session_rules(role: str, rules: Optional[dict] = None, workspace: str = "",
                  hard: bool = True, scope: Optional[list] = None) -> list[dict]:
    """Kilo session `permission[]` for one role. No "*" catch-all: the
    adapter's own rules (agent_manager, doom_loop ... denies) stay in force.
    `scope`: the sprint's files (workspace-relative globs) the coordinator
    injects; they become the edit/write allow list, `edit_paths.allow` adds
    to them, an empty result denies every edit. None = no sprint (GUI tester,
    agent blocks): `edit_paths` alone, an empty allow list being the workspace."""
    r = normalize(role, rules)
    p = r["permissions"]
    out: list[dict] = []

    def add(perm, pattern, action):
        out.append({"permission": perm, "pattern": pattern, "action": action})

    # edit / write with path scope
    edit_allow = list(r["edit_paths"]["allow"])
    if scope is not None:
        edit_allow = list(dict.fromkeys([norm_path(s) for s in scope if str(s).strip()]
                                        + edit_allow))
    for cat, perms in (("edit", EDIT_PERMS), ("write", ("write",))):
        for perm in perms:
            out += _scope_rules(perm, p.get(cat, "deny"), edit_allow, r["edit_paths"]["deny"],
                                workspace, closed=scope is not None)
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
    # a folder opened only for reading is never writable (W2.2)
    for pat in _read_only_dirs(r["read_paths"]["allow"], edit_allow, workspace):
        for perm in EDIT_PERMS + ("write",):
            add(perm, pat, "deny")
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
    out += _studio_rules(p)
    # outside the workspace
    add("external_directory", "*", p.get("external_directory", "deny"))
    scoped = r["read_paths"]["allow"] + (edit_allow if p.get("edit") != "deny" else [])
    for d in _abs_dirs(scoped):
        add("external_directory", d, "allow")
    for tool in r["deny_tools"]:
        add(tool, "*", "deny")
    for x in r["extra"]:
        add(x["permission"], x.get("pattern", "*"), x["action"])
    if role != "coordinator":
        out += config_denies()
    if hard:
        for h in HARD_DENIES:
            add(h["permission"], h["pattern"], "deny")
    return out


def floor_rules(role: str, workspace: str = "", scope: Optional[list] = None) -> list[dict]:
    """What applies to a session even when NO role rules are deployed: the
    sprint's edit scope (when given), the config-file denies (every role but
    the coordinator) and the hard denies. Nothing else: a profile without
    deployed role rules keeps its own client rules."""
    out: list[dict] = []
    if scope is not None:
        allow = list(dict.fromkeys(norm_path(s) for s in scope if str(s).strip()))
        for perm in EDIT_PERMS + ("write",):
            out += _scope_rules(perm, "allow", allow, [], workspace, closed=True)
    if role != "coordinator":
        out += config_denies()
    out += [{"permission": h["permission"], "pattern": h["pattern"], "action": "deny"}
            for h in HARD_DENIES]
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
    if op not in ("read", "edit", "write"):
        d, rr = decide(lst, op, value)
        return {"decision": d or "deny", "rule": rr, "checked": value}
    v = norm_path(value)
    seen = kilo_path(v, workspace)            # what Kilo matches the edit rules against
    decision, rule = decide(lst, op, seen)
    # a file outside the workspace also needs external_directory for its folder
    absolute = v if is_abs(v) else (
        posixpath.normpath(norm_path(workspace).rstrip("/") + "/" + v) if workspace else "")
    ws = norm_path(workspace).rstrip("/").lower()
    if decision != "deny" and absolute and ws and \
            not (absolute.lower() + "/").startswith(ws + "/"):
        d, rr = decide(lst, "external_directory", posixpath.dirname(absolute))
        if d != "allow":
            decision, rule = d or "deny", rr
    return {"decision": decision or "deny", "rule": rule, "checked": seen}


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
