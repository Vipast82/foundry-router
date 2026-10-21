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


LIFECYCLE_DEFAULTS = {"enabled": False, "evidence": True, "obligations": True, "recovery": True,
                      "readiness": True, "readiness_retry_seconds": 120,
                      "recover_max_parts": 400, "recover_max_seconds": 20,
                      "prompt_cap": 6000}

# [lead_manager] (v0.123): the last automated recovery tier (lead.py). Off by
# default; disabled means the engine behaves exactly as without it.
LEAD_MANAGER_DEFAULTS = {
    "enabled": False,
    "persona": "Foundry-Kilo-Lead-Manager",
    "expected_models": ["claude-opus-5-5"],   # ordered route chain; first = primary route
    "agent": "lead",                          # Kilo agent of the lead session (read-only tools)
    "kilo_client": "kilo",                    # Foundry client name of Kilo's worker key (its
                                              # grant of the lead persona is what readiness checks)
    "repair_agent": "",                       # Kilo agent of a coordinator repair session
                                              # ("" = the profile's client.agent)
    "reviewer": "",                           # independent repair reviewer persona
                                              # ("" = roles.deep_reviewer, else roles.reviewer)
    "max_diagnoses": 3,
    "max_candidates": 2,
    "max_upgrades": 1,
    "max_fingerprint_repeats": 1,
    "max_case_minutes": 240,
    "max_quota_calls": 12,
    # per-TASK caps across all of its cases (a game repair commit moves HEAD,
    # so a returning blocker is a repeat, never a fresh budget)
    "max_cases_per_task": 3,
    "max_lead_calls_per_task": 6,
    "max_extra_attempts_per_task": 2,
    "max_upgrade_minutes": 120,               # UPGRADING longer than this -> Victor
    "max_resuming_minutes": 60,               # RESUMING the engine never applied -> Victor
    "lead_timeout_minutes": 45,
    "extra_attempts": 1,
    "defer_seconds": 300,                     # first DEFERRED backoff (doubles, capped)
    "acceptance_tests": [],                   # game globs a lead assignment may only change
                                              # when its manifest declares them ([] = the
                                              # adapter's test paths, else tests/**,
                                              # *_test.*, *.spec.* ...)
    "repair_broader_tests": ["tests/coordinator"],  # broader suite of a coordinator repair
                                              # when the lead names none
    "repair_root": "",
    "router_repo": "",
    "supervisor": "manual",                   # manual | supervisor
    # Delegated recovery authority (Victor's policy, on by default when the lead
    # is enabled): the lead's `operate` actions -- executed, journaled and
    # verified by the coordinator, never by the read-only lead session -- clean
    # generated scratch files, extend budgets, resolve routine decisions, raise
    # exhausted recovery bounds and record test-change decisions. A counter
    # limit becomes a journaled delegated round that needs a materially
    # different action, never a question for Victor by itself.
    "full_authority": True,
    # Victor 2026-10-21: write_access gives the lead session full Kilo access to
    # the game workspace (edit, shell, git, Studio MCP, subagents) while it holds
    # the workspace lock, so no primary writes at the same time. It fixes blockers
    # itself, commits with the task prefix, and the coordinator re-verifies.
    # Secrets stay unreadable; the router checkout and coordinator DB stay guarded.
    "write_access": False,
    # Victor 2026-10-21: every role must keep working when paid models are gone, so
    # a local model may be the lead chain's last route (also list it in expected_models).
    "allow_local_route": False,
    "max_delegated_rounds": 12,               # runaway guard per task budget round
    "max_extend_attempts": 3,                 # attempts one extend_budget op may grant
    "max_clean_paths": 20,                    # files one clean_untracked op may remove
}
LEAD_SUPERVISOR_MODES = ("manual", "supervisor")


def _lead_manager_table(path: str, raw) -> dict:
    lm_raw = raw if raw is not None else {}
    if not isinstance(lm_raw, dict):
        raise ProfileError(f"{path}: [lead_manager] must be a table")
    unknown = sorted(set(lm_raw) - set(LEAD_MANAGER_DEFAULTS))
    if unknown:
        raise ProfileError(f"{path}: unknown [lead_manager] keys {unknown}")
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in LEAD_MANAGER_DEFAULTS.items()}
    for k, v in lm_raw.items():
        want = type(LEAD_MANAGER_DEFAULTS[k])
        if want is bool and not isinstance(v, bool):
            raise ProfileError(f"{path}: lead_manager.{k} must be true or false")
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ProfileError(f"{path}: lead_manager.{k} must be a non-negative integer")
        if want is str and not isinstance(v, str):
            raise ProfileError(f"{path}: lead_manager.{k} must be a string")
        if want is list and (not isinstance(v, list) or
                             any(not isinstance(x, str) or not x.strip() for x in v)):
            raise ProfileError(f"{path}: lead_manager.{k} must be a list of non-empty strings")
        out[k] = list(v) if isinstance(v, list) else v
    if not out["expected_models"]:
        raise ProfileError(f"{path}: lead_manager.expected_models must name the route chain "
                           f"(first = the primary route)")
    if len(set(out["expected_models"])) != len(out["expected_models"]):
        raise ProfileError(f"{path}: lead_manager.expected_models has duplicates")
    if not out["persona"].strip() or not out["agent"].strip() or not out["kilo_client"].strip():
        raise ProfileError(f"{path}: lead_manager.persona, agent and kilo_client are required")
    if out["supervisor"] not in LEAD_SUPERVISOR_MODES:
        raise ProfileError(f"{path}: lead_manager.supervisor must be manual or supervisor")
    if out["reviewer"] and out["reviewer"].strip().casefold() == out["persona"].strip().casefold():
        raise ProfileError(f"{path}: lead_manager.reviewer must differ from the lead persona")
    for k in ("max_diagnoses", "max_quota_calls", "max_case_minutes", "lead_timeout_minutes",
              "max_cases_per_task", "max_lead_calls_per_task", "max_upgrade_minutes",
              "max_resuming_minutes"):
        if out[k] < 1:
            raise ProfileError(f"{path}: lead_manager.{k} must be at least 1")
    for x in out["repair_broader_tests"]:
        x = x.strip().replace("\\", "/")
        if x.startswith(("-", "/")) or ".." in x.split("/") or ":" in x.split("::")[0]:
            raise ProfileError(f"{path}: lead_manager.repair_broader_tests: {x!r} must be a "
                               f"relative test path or id (no options)")
    return out


# [fast_lane] (UI-T0): named tasks run as one long session verified by the
# project's studio_gate runner (fast_lane.py). Off by default; a task not in
# `tasks` behaves exactly as without it.
FAST_LANE_DEFAULTS = {
    "enabled": False,
    "tasks": [],                              # explicit task ids (no globs)
    "gate_module": "foundry_coordinator.projects.animaluprising.studio_gate",
    "gate_timeout_minutes": 45,               # a run still going after this = red gate
    "max_red_gates_before_handoff": 2,        # local-primary tasks then auto-request frontier_primary
    "max_vision_rounds": 2,                   # CHANGES rounds before accept-with-notes
    "vision_batch_frames": 20,                # frames per vision call (UI-D1 had ~560 frames)
    # revamp B5 (Victor 2026-10-21): helpers start on demand only, so a fast-lane
    # task runs no proactive lane unless the profile lists one here
    "lanes": [],
    # files the runner rewrites on every run ({task} = the task id): never part of the
    # coordinator's clean / unrelated_preserved checks (UI-D1 lost its last attempt to the
    # evidence log, which was already dirty at run start and changed on every gate run)
    "runner_outputs": ["docs/progress/{task}_EVIDENCE_RAW.log"],
    # the runner's capture files: still must be committed (the post-PASS capture commit),
    # but re-rendering or committing one that was dirty at run start is not "touching a
    # pre-existing change"
    "runner_captures": ["docs/evidence/UI-v3/{task}/**"],
}


def _fast_lane_table(path: str, raw) -> dict:
    fl_raw = raw if raw is not None else {}
    if not isinstance(fl_raw, dict):
        raise ProfileError(f"{path}: [fast_lane] must be a table")
    unknown = sorted(set(fl_raw) - set(FAST_LANE_DEFAULTS))
    if unknown:
        raise ProfileError(f"{path}: unknown [fast_lane] keys {unknown}")
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in FAST_LANE_DEFAULTS.items()}
    for k, v in fl_raw.items():
        want = type(FAST_LANE_DEFAULTS[k])
        if want is bool and not isinstance(v, bool):
            raise ProfileError(f"{path}: fast_lane.{k} must be true or false")
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ProfileError(f"{path}: fast_lane.{k} must be a non-negative integer")
        if want is str and (not isinstance(v, str) or not v.strip()):
            raise ProfileError(f"{path}: fast_lane.{k} must be a non-empty string")
        if want is list and (not isinstance(v, list) or
                             any(not isinstance(x, str) or not x.strip() for x in v)):
            raise ProfileError(f"{path}: fast_lane.{k} must be a list of non-empty strings")
        out[k] = list(v) if isinstance(v, list) else v
    if any(c in t for t in out["tasks"] for c in "*?[]") or \
            len(set(out["tasks"])) != len(out["tasks"]):
        raise ProfileError(f"{path}: fast_lane.tasks needs explicit, distinct task IDs")
    for k in ("gate_timeout_minutes", "max_red_gates_before_handoff"):
        if out[k] < 1:
            raise ProfileError(f"{path}: fast_lane.{k} must be at least 1")
    from .lanes import LANES                       # (lanes imports profile: lazily)
    bad = sorted(set(out["lanes"]) - set(LANES))
    if bad:
        raise ProfileError(f"{path}: fast_lane.lanes: unknown lanes {bad} "
                           f"(known: {', '.join(sorted(LANES))})")
    gm = out["gate_module"].strip()
    if not (gm.endswith(".py") or re.match(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$", gm)):
        raise ProfileError(f"{path}: fast_lane.gate_module must be a dotted module name "
                           f"(or a .py script path)")
    return out


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
    fast_lane: dict = field(default_factory=lambda: {
        k: (list(v) if isinstance(v, list) else v) for k, v in FAST_LANE_DEFAULTS.items()})
    # [automation] (revamp B5-B13): validated settings with defaults (automation.py)
    automation: dict = field(default_factory=lambda: __import__(
        "foundry_coordinator.automation", fromlist=["defaults"]).defaults())

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
    lc_raw = raw.get("lifecycle") or {}
    if not isinstance(lc_raw, dict):
        raise ProfileError(f"{path}: [lifecycle] must be a table")
    unknown_lc = sorted(set(lc_raw) - set(LIFECYCLE_DEFAULTS))
    if unknown_lc:
        raise ProfileError(f"{path}: unknown [lifecycle] keys {unknown_lc}")
    lifecycle = dict(LIFECYCLE_DEFAULTS)
    for k, v in lc_raw.items():
        want = type(LIFECYCLE_DEFAULTS[k])
        if want is bool and not isinstance(v, bool):
            raise ProfileError(f"{path}: lifecycle.{k} must be true or false")
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise ProfileError(f"{path}: lifecycle.{k} must be a non-negative integer")
        lifecycle[k] = v
    lead_manager = _lead_manager_table(path, raw.get("lead_manager"))
    fast_lane = _fast_lane_table(path, raw.get("fast_lane"))
    from .automation import AutomationError, table as _automation_table
    try:
        automation = _automation_table(path, raw.get("automation"))
    except AutomationError as e:
        raise ProfileError(str(e)) from None
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
