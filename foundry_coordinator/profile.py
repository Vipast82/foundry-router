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

PROFILE_SCHEMA = 1
ROLES = ("implementer", "planner", "researcher", "reviewer", "deep_reviewer", "visual_reviewer",
         "image_generator")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRETY_KEY = re.compile(r"(api_?key|token|secret|password|passwd)$", re.I)
_SECRETY_VAL = re.compile(r"^(sk-[A-Za-z0-9_-]{12,}|[A-Fa-f0-9]{40,}|ghp_[A-Za-z0-9]{20,}|"
                          r"eyJ[A-Za-z0-9_-]{20,}\.)")


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
    roles = dict(raw.get("roles") or {})
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        raise ProfileError(f"{path}: unknown roles {unknown} (known: {', '.join(ROLES)})")
    if "implementer" not in roles:
        raise ProfileError(f"{path}: roles.implementer is required")
    overrides = raw.get("role_overrides") or {}
    if not isinstance(overrides, dict):
        raise ProfileError(f"{path}: role_overrides must be named tables")
    assigned = set()
    for name, override in overrides.items():
        if not isinstance(override, dict) or set(override) - {"tasks", *ROLES}:
            raise ProfileError(f"{path}: role_overrides.{name}: unknown roles or invalid table")
        task_ids = override.get("tasks")
        if not isinstance(task_ids, list) or not task_ids or any(
                not isinstance(t, str) or not t or any(c in t for c in "*?[]") for t in task_ids):
            raise ProfileError(f"{path}: role_overrides.{name}.tasks needs explicit task IDs")
        if len(set(task_ids)) != len(task_ids) or assigned.intersection(task_ids):
            raise ProfileError(f"{path}: overlapping role override task IDs")
        assigned.update(task_ids)
        if any(not isinstance(v, str) or not v for k, v in override.items() if k != "tasks"):
            raise ProfileError(f"{path}: role_overrides.{name} personas must be nonempty strings")
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
    return Profile(
        id=pid, name=str(raw.get("name") or pid), version=str(raw.get("version") or "1"),
        path=path, hash=hashlib.sha256(blob).hexdigest()[:16], workspace=wpath,
        allowed_dirs=allowed, branch=str(ws.get("branch") or "main"),
        remote=str(ws.get("remote") or "origin"), raw=raw, tasks=tasks, adapter=adapter,
        client=client, roles=roles, foundry=dict(raw.get("foundry") or {}), checks=checks,
        review=dict(raw.get("review") or {}), approvals=rules,
        deps_accept_pending=list(appr.get("deps_accept_pending") or []), publish=publish,
        limits=dict(raw.get("limits") or {}),
        resources=list((raw.get("resources") or {}).get("exclusive") or []),
        closeout=dict(raw.get("closeout") or {}), prompts=dict(raw.get("prompts") or {}),
        artifacts=dict(raw.get("artifacts") or {}), commit=dict(raw.get("commit") or {}))


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
