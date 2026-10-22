"""Project profiles: everything project-specific, as validated TOML.

A profile names the repository, the task source, the coding client and its
session settings, which Foundry persona plays each coordinator role, the
verification commands and the evidence they must leave, review and human
approval policy, commit/push policy, limits, exclusive resources (e.g. one
Roblox Studio instance), and artifact locations.

Credentials are never written in a profile: settings that hold a secret are
given as the NAME of an environment variable (`key_env`, `password_env`).
A literal secret-looking value anywhere in the file is refused.

See foundry_coordinator/projects/*/project.toml and examples/ for complete
profiles, and docs/COORDINATOR.md for every key.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Optional

from foundry_automation.schema import (  # noqa: F401  (re-exported; the shared schema)
    FAST_LANE_DEFAULTS, LEAD_MANAGER_DEFAULTS, LEAD_SUPERVISOR_MODES, LIFECYCLE_DEFAULTS,
    PROFILE_ROLES as ROLES, PROFILE_SCHEMA, SettingsError, fast_lane_table, lead_manager_table,
    lifecycle_table, role_overrides_table, roles_table, walk_secrets as _walk_secrets)
from foundry_automation.schema import ID_RE as _ID  # noqa: F401
from foundry_automation.schema import SECRETY_KEY as _SECRETY_KEY  # noqa: F401
from foundry_automation.schema import SECRETY_VAL as _SECRETY_VAL  # noqa: F401

# The defaults tables and typed validators live in foundry_automation.schema
# (shared with the router's config store and GUI). These wrappers keep the
# coordinator's own error type.


def _lead_manager_table(path: str, raw) -> dict:
    try:
        return lead_manager_table(path, raw)
    except SettingsError as e:
        raise ProfileError(str(e)) from None


def _fast_lane_table(path: str, raw) -> dict:
    from .lanes import LANES                       # (lanes imports profile: lazily)
    try:
        return fast_lane_table(path, raw, tuple(LANES))
    except SettingsError as e:
        raise ProfileError(str(e)) from None


class ProfileError(ValueError):
    pass


@dataclass
class Check:
    name: str
    command: list[str]
    phase: str = "verify"                 # verify | closeout
    required: bool = True
    pass_regex: str = ""                  # must match the output (plus exit code 0)
    fail_regex: str = ""                  # any match = FAIL even with exit 0
    timeout_seconds: int = 900
    evidence: str = ""                    # a file the check must leave behind (relative)


@dataclass
class ApprovalRule:
    match: str                            # a task tag (glob)
    kind: str                             # e.g. "visual", "publish"
    stage: str = "after_close"            # before_close | after_close
    artifacts: list[str] = field(default_factory=list)   # globs hashed into the approval


@dataclass
class Profile:
    id: str
    name: str
    version: str
    path: str
    hash: str
    workspace: str
    allowed_dirs: list[str]
    branch: str
    remote: str
    raw: dict

    # sections (parsed with defaults)
    tasks: dict = field(default_factory=dict)
    adapter: str = ""
    client: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    foundry: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    review: dict = field(default_factory=dict)
    approvals: list[ApprovalRule] = field(default_factory=list)
    deps_accept_pending: list[str] = field(default_factory=list)
    publish: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    resources: list[str] = field(default_factory=list)
    closeout: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    commit: dict = field(default_factory=dict)
    # [lifecycle] (v0.122): evidence ledger, obligations and recovery wiring in
    # the engine. Off by default; the sub-switches apply only when enabled.
    lifecycle: dict = field(default_factory=lambda: dict(LIFECYCLE_DEFAULTS))
    # [lead_manager] (v0.123): validated settings with defaults; off by default.
    lead_manager: dict = field(default_factory=lambda: {
        k: (list(v) if isinstance(v, list) else v) for k, v in LEAD_MANAGER_DEFAULTS.items()})
    # [fast_lane] (UI-T0): validated settings with defaults; off by default.
    # [automation] (revamp B5-B13): validated settings with defaults (automation.py)
    automation: dict = field(default_factory=lambda: __import__(
        "foundry_coordinator.automation", fromlist=["defaults"]).defaults())
    fast_lane: dict = field(default_factory=lambda: {
        k: (list(v) if isinstance(v, list) else v) for k, v in FAST_LANE_DEFAULTS.items()})

    def fast_lane_task(self, task_id: str) -> bool:
        """True when [fast_lane] is enabled and names this task."""
        fl = self.fast_lane or {}
        return bool(fl.get("enabled")) and bool(task_id) and task_id in (fl.get("tasks") or [])

    def lead_manager_on(self) -> bool:
        return bool((self.lead_manager or {}).get("enabled"))

    def lead_setting(self, key: str):
        """One [lead_manager] setting (its default when the profile omits it)."""
        if key not in LEAD_MANAGER_DEFAULTS:
            raise KeyError(f"unknown [lead_manager] setting {key!r}")
        return (self.lead_manager or {}).get(key, LEAD_MANAGER_DEFAULTS[key])

    def lifecycle_on(self, part: str = "") -> bool:
        lc = self.lifecycle or {}
        if not lc.get("enabled"):
            return False
        return bool(lc.get(part, True)) if part else True

    def limit(self, key: str, default: int) -> int:
        try:
            return int(self.limits.get(key, default))
        except (TypeError, ValueError):
            return default

    def persona(self, role: str, task_id: str = "") -> str:
        p = self.roles.get(role) or ""
        for override in (self.raw.get("role_overrides") or {}).values():
            if task_id and task_id in override["tasks"]:
                p = override.get(role, p)
        if not p:
            raise ProfileError(f"{self.id}: no persona mapped for role {role!r}")
        return p

    def exempt(self, task_id: str) -> list[str]:
        """Closeout-only paths: changes there after a review don't invalidate it."""
        return [p.replace("{task}", task_id) for p in self.closeout.get("exempt_paths", [])]

    def in_workspace(self, rel_or_abs: str) -> bool:
        p = os.path.realpath(os.path.join(self.workspace, rel_or_abs))
        root = os.path.realpath(self.workspace)
        return p == root or p.startswith(root + os.sep)

    def secret(self, section: dict, key: str) -> str:
        """Read a secret from the environment variable a profile names."""
        name = section.get(key + "_env") or ""
        return os.environ.get(name, "") if name else ""


def _walk_secrets(d: Any, where: str = "") -> list[str]:
    bad = []
    if isinstance(d, dict):
        for k, v in d.items():
            loc = f"{where}.{k}" if where else k
            if isinstance(v, str) and _SECRETY_KEY.search(k) and v and not k.endswith("_env"):
                bad.append(f"{loc} (put the value in an environment variable and use {k}_env)")
            bad += _walk_secrets(v, loc)
    elif isinstance(d, list):
        for i, v in enumerate(d):
            bad += _walk_secrets(v, f"{where}[{i}]")
    elif isinstance(d, str) and _SECRETY_VAL.match(d.strip()):
        bad.append(f"{where} looks like a literal secret")
    return bad


def matches_any(path: str, patterns: list[str]) -> bool:
    path = path.replace("\\", "/")
    for p in patterns:
        p = p.replace("\\", "/")
        if fnmatch.fnmatchcase(path, p) or path == p or path.startswith(p.rstrip("/") + "/"):
            return True
    return False


def load(path: str) -> Profile:
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as f:
            blob = f.read()
        raw = tomllib.loads(blob.decode("utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ProfileError(f"{path}: {e}") from None
    return from_raw(raw, path, hashlib.sha256(blob).hexdigest()[:16])


def from_raw(raw: dict, path: str, digest: str = "") -> Profile:
    """Validate a parsed profile (a TOML file, or the local TOML with the
    router's deployed settings overlaid, see config_pull.py)."""
    path = os.path.abspath(path)
    if not digest:
        import json
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()
                                ).hexdigest()[:16]
    bad = _walk_secrets(raw)
    if bad:
        raise ProfileError(f"{path}: secrets must not be in a profile: {'; '.join(bad)}")
    if raw.get("schema_version") != PROFILE_SCHEMA:
        raise ProfileError(f"{path}: schema_version must be {PROFILE_SCHEMA}")
    pid = str(raw.get("id") or "")
    if not _ID.match(pid):
        raise ProfileError(f"{path}: id must be lower-case letters, digits, . _ - (got {pid!r})")
    base = os.path.dirname(path)
    ws = raw.get("workspace") or {}
    wpath = os.path.expandvars(os.path.expanduser(str(ws.get("path") or "")))
    if not wpath:
        raise ProfileError(f"{path}: workspace.path is required")
    wpath = os.path.abspath(os.path.join(base, wpath))
    allowed = [str(a).replace("\\", "/").strip("/") for a in ws.get("allowed_dirs") or []]
    if not allowed:
        raise ProfileError(f"{path}: workspace.allowed_dirs must list where sessions may write")
    for a in allowed:
        if a.startswith("..") or os.path.isabs(a):
            raise ProfileError(f"{path}: allowed_dirs entry {a!r} leaves the workspace")
    try:
        roles = roles_table(path, raw.get("roles"))
        role_overrides_table(path, raw.get("role_overrides"))
    except SettingsError as e:
        raise ProfileError(str(e)) from None
    if raw.get("role_rules") is not None:
        from foundry_automation.role_rules import validate as _rr_validate
        errs, _ = _rr_validate(raw.get("role_rules"))
        if errs:
            raise ProfileError(f"{path}: " + "; ".join(f"{e['path']}: {e['message']}"
                                                       for e in errs[:5]))
    checks = []
    for c in (raw.get("verify") or {}).get("checks") or []:
        if not c.get("name") or not c.get("command"):
            raise ProfileError(f"{path}: every verify.checks entry needs name and command")
        cmd = c["command"] if isinstance(c["command"], list) else [str(c["command"])]
        if c.get("phase", "verify") not in ("verify", "closeout"):
            raise ProfileError(f"{path}: check {c['name']}: phase must be verify or closeout")
        checks.append(Check(name=str(c["name"]), command=[str(x) for x in cmd],
                            phase=c.get("phase", "verify"), required=bool(c.get("required", True)),
                            pass_regex=str(c.get("pass_regex") or ""),
                            fail_regex=str(c.get("fail_regex") or ""),
                            timeout_seconds=int(c.get("timeout_seconds") or 900),
                            evidence=str(c.get("evidence") or "")))
    appr = raw.get("approval") or {}
    if "visual_model" in appr and not isinstance(appr["visual_model"], bool):
        raise ProfileError(f"{path}: approval.visual_model must be true or false")
    rules = []
    for r in appr.get("rules") or []:
        if r.get("stage", "after_close") not in ("before_close", "after_close"):
            raise ProfileError(f"{path}: approval stage must be before_close or after_close")
        rules.append(ApprovalRule(match=str(r["match"]), kind=str(r["kind"]),
                                  stage=r.get("stage", "after_close"),
                                  artifacts=list(r.get("artifacts") or [])))
    tasks = dict(raw.get("tasks") or {})
    adapter = str((raw.get("adapter") or {}).get("module") or "")
    if not adapter and tasks.get("source") not in ("json", "toml"):
        raise ProfileError(f"{path}: give tasks.source = json|toml, or an adapter.module")
    client = dict(raw.get("client") or {})
    if client.get("kind", "kilo") not in ("kilo", "fake", "scripted"):
        raise ProfileError(f"{path}: client.kind must be kilo (or fake/scripted for tests)")
    publish = dict(raw.get("publish") or {})
    if publish.get("mode", "none") not in ("none", "push", "verify_only"):
        raise ProfileError(f"{path}: publish.mode must be none, push or verify_only")
    if publish.get("force") or publish.get("tags") == "all":
        raise ProfileError(f"{path}: force pushes and pushing all tags are never allowed")
    try:
        lifecycle = lifecycle_table(path, raw.get("lifecycle"))
    except SettingsError as e:
        raise ProfileError(str(e)) from None
    lead_manager = _lead_manager_table(path, raw.get("lead_manager"))
    fast_lane = _fast_lane_table(path, raw.get("fast_lane"))
    # [automation] (revamp B5-B13): validated settings with defaults (automation.py)
    from .automation import AutomationError, table as _automation_table
    try:
        automation = _automation_table(path, raw.get("automation"))
    except AutomationError as e:
        raise ProfileError(str(e)) from None
    return Profile(
        id=pid, name=str(raw.get("name") or pid), version=str(raw.get("version") or "1"),
        path=path, hash=digest, workspace=wpath,
        allowed_dirs=allowed, branch=str(ws.get("branch") or "main"),
        remote=str(ws.get("remote") or "origin"), raw=raw, tasks=tasks, adapter=adapter,
        client=client, roles=roles, foundry=dict(raw.get("foundry") or {}), checks=checks,
        review=dict(raw.get("review") or {}), approvals=rules,
        deps_accept_pending=list(appr.get("deps_accept_pending") or []), publish=publish,
        limits=dict(raw.get("limits") or {}),
        resources=list((raw.get("resources") or {}).get("exclusive") or []),
        closeout=dict(raw.get("closeout") or {}), prompts=dict(raw.get("prompts") or {}),
        artifacts=dict(raw.get("artifacts") or {}), commit=dict(raw.get("commit") or {}),
        lifecycle=lifecycle, lead_manager=lead_manager, fast_lane=fast_lane,
        automation=automation)


def check_isolation(profiles: list[Profile]) -> None:
    """Two projects may not share (or nest) a workspace: their edits, scope
    checks and git state would mix."""
    seen: list[tuple[str, str]] = []
    ids = set()
    for p in profiles:
        if p.id in ids:
            raise ProfileError(f"two profiles use the id {p.id!r}")
        ids.add(p.id)
        root = os.path.realpath(p.workspace)
        for other_id, other in seen:
            if root == other or root.startswith(other + os.sep) or other.startswith(root + os.sep):
                raise ProfileError(f"projects {other_id!r} and {p.id!r} share a workspace "
                                   f"({other} / {root})")
        seen.append((p.id, root))


def find_profiles(paths: list[str]) -> list[Profile]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                cand = os.path.join(p, name, "project.toml")
                if os.path.isfile(cand):
                    out.append(load(cand))
        else:
            out.append(load(p))
    check_isolation(out)
    return out


def get(profiles: list[Profile], pid: str) -> Optional[Profile]:
    return next((p for p in profiles if p.id == pid), None)
