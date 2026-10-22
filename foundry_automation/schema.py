"""Every automation setting, described once.

The coordinator's profile loader (foundry_coordinator/profile.py) uses the
typed table validators below, and the router's config store uses `FIELDS` to
validate drafts and to describe them to the GUI (GET /admin/api/automation/schema).

A field:
  key        dotted path in the profile ("lead_manager.max_diagnoses"); `*` is a
             name the user picks ("helpers.personas.*", "lanes.*.enabled")
  type       bool | int | float | str | enum | list | map | table
  scope      project  lives in the router document
             local    machine bootstrap or a secret's env-var name: TOML only,
                      never sent to or accepted by the router
             router   the router-wide document (_router)
  apply      live          the coordinator's next safe point (no live session)
             next_session  the next session / Kilo agent start
             restart       a coordinator restart
  component  which code reads it (for the GUI's "used by")
  role_binding  the role whose persona this field names (C2 role editor)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional

PROFILE_SCHEMA = 1

# Roles the coordinator's [roles] table accepts (profile.py).
PROFILE_ROLES = ("implementer", "planner", "researcher", "reviewer", "deep_reviewer",
                 "visual_reviewer", "image_generator")
# Roles that carry role rules (permissions / scopes). lead, helper and
# coordinator have no [roles] entry: their personas live in other tables
# (see `role_binding` on the fields).
RULE_ROLES = ("implementer", "lead", "reviewer", "deep_reviewer", "visual_reviewer",
              "planner", "researcher", "helper", "coordinator")
# Lane names (foundry_coordinator/lanes.py LANES). The coordinator also checks
# against its own LANES table; this copy lets the router validate offline.
KNOWN_LANES = ("failure_triage", "incremental_review", "test_design", "proposer", "evidence",
               "handoff", "visual", "research", "preflight", "prework")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SECRETY_KEY = re.compile(r"(api_?key|token|secret|password|passwd)$", re.I)
SECRETY_VAL = re.compile(r"^(sk-[A-Za-z0-9_-]{12,}|[A-Fa-f0-9]{40,}|ghp_[A-Za-z0-9]{20,}|"
                         r"eyJ[A-Za-z0-9_-]{20,}\.)")


class SettingsError(ValueError):
    """A setting failed validation. `path` is the dotted key when known."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(message)
        self.path = path


# --------------------------------------------------------------------------- defaults tables
# (moved from foundry_coordinator/profile.py; profile.py re-exports them)

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

# [fast_lane] (UI-T0): named tasks run as one long session verified by the
# project's studio_gate runner (fast_lane.py). Off by default; a task not in
# `tasks` behaves exactly as without it.
FAST_LANE_DEFAULTS = {
    "enabled": False,
    "tasks": [],                              # explicit task ids (no globs)
    "gate_module": "foundry_coordinator.projects.animaluprising.studio_gate",
    "gate_timeout_minutes": 45,               # a run still going after this = red gate
    "max_red_gates_before_handoff": 2,        # local-primary tasks then auto-request frontier_primary
    "max_vision_rounds": 1,                   # revamp 2026-10-21: one fix round, then accept with notes
    "vision_batch_frames": 20,                # frames per vision call (UI-D1 had ~560 frames)
    # revamp B3/B2/B4 (branch revamp/gate-vision, same names and defaults; that branch
    # also changes max_vision_rounds to 1 and revamp/lead-scope lanes to [] -- take
    # those at the merge, together with the code that reads them)
    "vision_parallel": 4,                     # vision batch calls in flight at once
    "auto_baseline": True,                    # baseline gate at the base commit first
    "early_review": True,                     # review runs alongside vision
    "tick_checklist": True,                   # the coordinator ticks verified checklist rows
    "lanes": [],                              # revamp B5: helpers start on demand only
    # files the runner rewrites on every run ({task} = the task id): never part of the
    # coordinator's clean / unrelated_preserved checks (UI-D1 lost its last attempt to the
    # evidence log, which was already dirty at run start and changed on every gate run)
    "runner_outputs": ["docs/progress/{task}_EVIDENCE_RAW.log"],
    # the runner's capture files: still must be committed (the post-PASS capture commit),
    # but re-rendering or committing one that was dirty at run start is not "touching a
    # pre-existing change"
    "runner_captures": ["docs/evidence/UI-v3/{task}/**"],
}


# [automation] (revamp B5-B13): the coordinator's own policy knobs. Same keys,
# defaults and rules as foundry_coordinator/automation.py on branch
# revamp/lead-scope (at the merge, that module can import these).
HELPER_MODES = ("on_demand", "proactive")
SCOPE_DEFAULTS = ("deny", "approve")
DEFAULT_SCOPE_RULES = [
    {"name": "gate_runner_boot_and_mounted_rows", "path": "tests/GateRunner.lua",
     "blocks": ["EXPECTED_BOOT", "MOUNTED_TESTS"]},
    {"name": "sprint_gate_manifest", "path": "docs/progress/{task}_gate.json"},
    {"name": "rojo_mapping_for_plan_files", "path": "default.project.json",
     "json_paths_in_plan": True},
]
AUTOMATION_DEFAULTS: dict = {
    "helpers": "on_demand",
    "helper_git_measure": True,
    "helper_lane_misses": 2,
    "subagents_allowed": ["explore"],
    "scope_auto_approve": [dict(r) for r in DEFAULT_SCOPE_RULES],
    "scope_unlisted_default": "deny",
    "decision_timeout_minutes": 30,
    "no_progress_sessions": 2,
    "auto_heal_retries": 2,
    "lead_hard_max_calls": 8,
    "lead_report_repairs": 1,
    "infra_retry_max": 5,
    "infra_backoff_seconds": 60,
    "preflight": True,
    "preflight_endpoints": [],
    "context_ceiling_tokens": 160000,
    "memory_delta": True,
    "transcript_poll_seconds": 120,
    "generic_checks_cache": True,
}
_SCOPE_RULE_KEYS = {"name", "path", "lines", "blocks", "json_paths_in_plan", "reason"}


def _scope_allow_rules(path: str, v) -> list:
    where = "automation.scope_auto_approve"
    if not isinstance(v, list):
        raise SettingsError(f"{path}: {where} must be a list of tables", where)
    out = []
    for i, r in enumerate(v):
        w = f"{where}[{i}]"
        if not isinstance(r, dict) or not isinstance(r.get("path"), str) or \
                not r["path"].strip():
            raise SettingsError(f"{path}: {w} needs a path", where)
        bad = sorted(set(r) - _SCOPE_RULE_KEYS)
        if bad:
            raise SettingsError(f"{path}: {w}: unknown keys {bad}", where)
        p = r["path"].replace("\\", "/").strip()
        if p.startswith(("/", "../")) or ".." in p.split("/") or ":" in p:
            raise SettingsError(f"{path}: {w}.path must be a workspace-relative glob", where)
        lines = r.get("lines") or []
        if not isinstance(lines, list) or any(not isinstance(x, str) for x in lines):
            raise SettingsError(f"{path}: {w}.lines must be a list of regexes", where)
        for x in lines:
            try:
                re.compile(x)
            except re.error as e:
                raise SettingsError(f"{path}: {w}.lines: {x!r}: {e}", where) from None
        blocks = r.get("blocks") or []
        if not isinstance(blocks, list) or any(
                not isinstance(x, str) or not re.match(r"^[A-Za-z_][\w.]*$", x) for x in blocks):
            raise SettingsError(f"{path}: {w}.blocks must be a list of identifiers", where)
        out.append({"name": str(r.get("name") or p), "path": p, "lines": list(lines),
                    "blocks": list(blocks),
                    "json_paths_in_plan": bool(r.get("json_paths_in_plan", False)),
                    "reason": str(r.get("reason") or "")})
    return out


def automation_table(path: str, raw) -> dict:
    """Validate [automation] (raises SettingsError)."""
    a = raw if raw is not None else {}
    if not isinstance(a, dict):
        raise SettingsError(f"{path}: [automation] must be a table", "automation")
    unknown = sorted(set(a) - set(AUTOMATION_DEFAULTS))
    if unknown:
        raise SettingsError(f"{path}: unknown [automation] keys {unknown}", "automation")
    out = {k: ([dict(x) if isinstance(x, dict) else x for x in v] if isinstance(v, list)
               else v) for k, v in AUTOMATION_DEFAULTS.items()}
    for k, v in a.items():
        want = type(AUTOMATION_DEFAULTS[k])
        where = f"automation.{k}"
        if k == "scope_auto_approve":
            out[k] = _scope_allow_rules(path, v)
            continue
        if want is bool and not isinstance(v, bool):
            raise SettingsError(f"{path}: {where} must be true or false", where)
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise SettingsError(f"{path}: {where} must be a non-negative integer", where)
        if want is str and not isinstance(v, str):
            raise SettingsError(f"{path}: {where} must be a string", where)
        if want is list and (not isinstance(v, list) or
                             any(not isinstance(x, str) or not x.strip() for x in v)):
            raise SettingsError(f"{path}: {where} must be a list of non-empty strings", where)
        out[k] = list(v) if isinstance(v, list) else v
    if out["helpers"] not in HELPER_MODES:
        raise SettingsError(f"{path}: automation.helpers must be one of {HELPER_MODES}",
                            "automation.helpers")
    if out["scope_unlisted_default"] not in SCOPE_DEFAULTS:
        raise SettingsError(f"{path}: automation.scope_unlisted_default must be one of "
                            f"{SCOPE_DEFAULTS}", "automation.scope_unlisted_default")
    return out


def _copy_defaults(d: dict) -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in d.items()}


def _typed_table(name: str, raw, defaults: dict, path: str, nonempty_str: bool) -> dict:
    """Type-check a strict table against its defaults table; unknown keys refused."""
    tbl = raw if raw is not None else {}
    if not isinstance(tbl, dict):
        raise SettingsError(f"{path}: [{name}] must be a table", name)
    unknown = sorted(set(tbl) - set(defaults))
    if unknown:
        raise SettingsError(f"{path}: unknown [{name}] keys {unknown}", name)
    out = _copy_defaults(defaults)
    for k, v in tbl.items():
        want = type(defaults[k])
        where = f"{name}.{k}"
        if want is bool and not isinstance(v, bool):
            raise SettingsError(f"{path}: {where} must be true or false", where)
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise SettingsError(f"{path}: {where} must be a non-negative integer", where)
        if want is str and (not isinstance(v, str) or (nonempty_str and not v.strip())):
            raise SettingsError(f"{path}: {where} must be a "
                                f"{'non-empty ' if nonempty_str else ''}string", where)
        if want is list and (not isinstance(v, list) or
                             any(not isinstance(x, str) or not x.strip() for x in v)):
            raise SettingsError(f"{path}: {where} must be a list of non-empty strings", where)
        out[k] = list(v) if isinstance(v, list) else v
    return out


def lifecycle_table(path: str, raw) -> dict:
    return _typed_table("lifecycle", raw, LIFECYCLE_DEFAULTS, path, nonempty_str=False)


def lead_manager_table(path: str, raw) -> dict:
    out = _typed_table("lead_manager", raw, LEAD_MANAGER_DEFAULTS, path, nonempty_str=False)
    if not out["expected_models"]:
        raise SettingsError(f"{path}: lead_manager.expected_models must name the route chain "
                            f"(first = the primary route)", "lead_manager.expected_models")
    if len(set(out["expected_models"])) != len(out["expected_models"]):
        raise SettingsError(f"{path}: lead_manager.expected_models has duplicates",
                            "lead_manager.expected_models")
    if not out["persona"].strip() or not out["agent"].strip() or not out["kilo_client"].strip():
        raise SettingsError(f"{path}: lead_manager.persona, agent and kilo_client are required",
                            "lead_manager.persona")
    if out["supervisor"] not in LEAD_SUPERVISOR_MODES:
        raise SettingsError(f"{path}: lead_manager.supervisor must be manual or supervisor",
                            "lead_manager.supervisor")
    if out["reviewer"] and out["reviewer"].strip().casefold() == out["persona"].strip().casefold():
        raise SettingsError(f"{path}: lead_manager.reviewer must differ from the lead persona",
                            "lead_manager.reviewer")
    for k in ("max_diagnoses", "max_quota_calls", "max_case_minutes", "lead_timeout_minutes",
              "max_cases_per_task", "max_lead_calls_per_task", "max_upgrade_minutes",
              "max_resuming_minutes"):
        if out[k] < 1:
            raise SettingsError(f"{path}: lead_manager.{k} must be at least 1",
                                f"lead_manager.{k}")
    for x in out["repair_broader_tests"]:
        x = x.strip().replace("\\", "/")
        if x.startswith(("-", "/")) or ".." in x.split("/") or ":" in x.split("::")[0]:
            raise SettingsError(f"{path}: lead_manager.repair_broader_tests: {x!r} must be a "
                                f"relative test path or id (no options)",
                                "lead_manager.repair_broader_tests")
    return out


def fast_lane_table(path: str, raw, known_lanes=KNOWN_LANES) -> dict:
    out = _typed_table("fast_lane", raw, FAST_LANE_DEFAULTS, path, nonempty_str=True)
    if any(c in t for t in out["tasks"] for c in "*?[]") or \
            len(set(out["tasks"])) != len(out["tasks"]):
        raise SettingsError(f"{path}: fast_lane.tasks needs explicit, distinct task IDs",
                            "fast_lane.tasks")
    for k in ("gate_timeout_minutes", "max_red_gates_before_handoff", "vision_parallel"):
        if out[k] < 1:
            raise SettingsError(f"{path}: fast_lane.{k} must be at least 1", f"fast_lane.{k}")
    bad = sorted(set(out["lanes"]) - set(known_lanes))
    if bad:
        raise SettingsError(f"{path}: fast_lane.lanes: unknown lanes {bad} "
                            f"(known: {', '.join(sorted(known_lanes))})", "fast_lane.lanes")
    gm = out["gate_module"].strip()
    if not (gm.endswith(".py") or re.match(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$", gm)):
        raise SettingsError(f"{path}: fast_lane.gate_module must be a dotted module name "
                            f"(or a .py script path)", "fast_lane.gate_module")
    return out


def role_overrides_table(path: str, raw) -> dict:
    overrides = raw or {}
    if not isinstance(overrides, dict):
        raise SettingsError(f"{path}: role_overrides must be named tables", "role_overrides")
    assigned: set = set()
    for name, override in overrides.items():
        where = f"role_overrides.{name}"
        if not isinstance(override, dict) or set(override) - {"tasks", *PROFILE_ROLES}:
            raise SettingsError(f"{path}: {where}: unknown roles or invalid table", where)
        task_ids = override.get("tasks")
        if not isinstance(task_ids, list) or not task_ids or any(
                not isinstance(t, str) or not t or any(c in t for c in "*?[]") for t in task_ids):
            raise SettingsError(f"{path}: {where}.tasks needs explicit task IDs", where + ".tasks")
        if len(set(task_ids)) != len(task_ids) or assigned.intersection(task_ids):
            raise SettingsError(f"{path}: overlapping role override task IDs", where + ".tasks")
        assigned.update(task_ids)
        if any(not isinstance(v, str) or not v for k, v in override.items() if k != "tasks"):
            raise SettingsError(f"{path}: {where} personas must be nonempty strings", where)
    return overrides


def roles_table(path: str, raw) -> dict:
    roles = dict(raw or {})
    unknown = [r for r in roles if r not in PROFILE_ROLES]
    if unknown:
        raise SettingsError(f"{path}: unknown roles {unknown} (known: {', '.join(PROFILE_ROLES)})",
                            "roles")
    if "implementer" not in roles:
        raise SettingsError(f"{path}: roles.implementer is required", "roles.implementer")
    return roles


def walk_secrets(d: Any, where: str = "") -> list[str]:
    """Literal secrets in a profile/document (only `*_env` names are allowed)."""
    bad = []
    if isinstance(d, dict):
        for k, v in d.items():
            loc = f"{where}.{k}" if where else str(k)
            if isinstance(v, str) and SECRETY_KEY.search(str(k)) and v and \
                    not str(k).endswith("_env"):
                bad.append(f"{loc} (put the value in an environment variable and use {k}_env)")
            bad += walk_secrets(v, loc)
    elif isinstance(d, list):
        for i, v in enumerate(d):
            bad += walk_secrets(v, f"{where}[{i}]")
    elif isinstance(d, str) and SECRETY_VAL.match(d.strip()):
        bad.append(f"{where} looks like a literal secret")
    return bad


# --------------------------------------------------------------------------- field registry

@dataclass
class Field:
    key: str
    type: str
    default: Any = None
    description: str = ""
    group: str = ""
    component: str = ""
    apply: str = "live"
    scope: str = "project"
    label: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    enum: Optional[list] = None
    item_type: Optional[str] = None
    role_binding: Optional[str] = None
    locked: bool = False
    since: str = ""

    @property
    def table(self) -> str:
        return self.key.split(".", 1)[0] if "." in self.key else ""

    @property
    def name(self) -> str:
        return self.key.rsplit(".", 1)[-1]

    def to_json(self) -> dict:
        d = asdict(self)
        d["table"] = self.table
        d["name"] = self.name
        if not d["label"]:
            d["label"] = self.name.replace("_", " ").capitalize()
        return d


@dataclass
class Table:
    table: str
    label: str
    group: str
    description: str = ""
    strict: bool = False           # unknown keys are an error (else a warning)
    scope: str = "project"

    def to_json(self) -> dict:
        return asdict(self)


GROUPS = [
    {"id": "identity", "label": "Project", "order": 10},
    {"id": "workspace", "label": "Workspace & scope", "order": 20},
    {"id": "client", "label": "Coding client (Kilo)", "order": 30},
    {"id": "roles", "label": "Roles & personas", "order": 40},
    {"id": "review", "label": "Review & approval", "order": 50},
    {"id": "lead", "label": "Lead manager", "order": 60},
    {"id": "fast_lane", "label": "Fast lane, gate & vision", "order": 70},
    {"id": "helpers", "label": "Helpers & lanes", "order": 80},
    {"id": "recovery", "label": "Retry, frontier & lifecycle", "order": 90},
    {"id": "limits", "label": "Limits", "order": 100},
    {"id": "decisions", "label": "Coordinator policy (scope, decisions, helpers)",
     "order": 110},
    {"id": "git", "label": "Commit, publish & closeout", "order": 120},
    {"id": "telemetry", "label": "Telemetry & training", "order": 130},
    {"id": "router", "label": "Router backends", "order": 140},
]

TABLES = [
    Table("workspace", "Workspace", "workspace", "Where sessions may write at all."),
    Table("adapter", "Project adapter", "identity", scope="local"),
    Table("animaluprising", "Animal Uprising adapter", "identity"),
    Table("client", "Kilo client", "client"),
    Table("roles", "Role personas", "roles",
          "Foundry persona per coordinator role. lead/helper/visual-lane personas live in "
          "their own tables (role_binding)."),
    Table("role_overrides", "Per-task role overrides", "roles",
          "Named tables: explicit task ids and the personas they use instead."),
    Table("foundry", "Foundry router", "identity"),
    Table("review", "Review", "review"),
    Table("approval", "Approval", "review"),
    Table("verify", "Verification checks", "review"),
    Table("lifecycle", "Lifecycle", "recovery", strict=True),
    Table("limits", "Limits", "limits"),
    Table("commit", "Commit policy", "git"),
    Table("publish", "Publish policy", "git"),
    Table("closeout", "Closeout", "git"),
    Table("resources", "Exclusive resources", "workspace"),
    Table("helpers", "Helpers", "helpers"),
    Table("lanes", "Proactive lanes", "helpers"),
    Table("fast_lane", "Fast lane", "fast_lane", strict=True),
    Table("lead_manager", "Lead manager", "lead", strict=True),
    Table("frontier_primary", "Frontier primary", "recovery"),
    Table("retry_guidance", "Retry guidance", "recovery"),
    Table("automation", "Coordinator policy", "decisions",
          "Revamp B5-B13 (branch revamp/lead-scope, foundry_coordinator/automation.py): "
          "helpers on demand, subagents, scope allow-list, decision timeout, auto-heal, "
          "infrastructure retries, preflight, context ceiling, memory delta.", strict=True),
    Table("telemetry", "Telemetry", "telemetry"),
    Table("training", "Training capture", "telemetry"),
    Table("prompts", "Prompts", "client"),
    Table("artifacts", "Artifacts", "review"),
    Table("auto_queue_followups", "Auto-queued follow-ups", "limits"),
    Table("tasks", "Task source", "identity"),
    Table("config_pull", "Config pull", "identity", scope="local"),
    Table("backends", "Backend cutoffs", "router", scope="router"),
]
TABLES_BY_NAME = {t.table: t for t in TABLES}

_LM_DESC = {
    "enabled": "The lead sprint manager: last automated recovery tier before the operator.",
    "persona": "Lead persona.",
    "expected_models": "Ordered route chain the lead persona must be served by (first = primary).",
    "agent": "Kilo agent of the lead session.",
    "kilo_client": "Foundry client name of Kilo's worker key.",
    "repair_agent": "Kilo agent of a coordinator repair session (empty = client.agent).",
    "reviewer": "Independent repair reviewer persona (empty = deep reviewer, else reviewer).",
    "max_diagnoses": "Lead calls per case.",
    "max_candidates": "Repair candidates per case.",
    "max_upgrades": "Supervisor upgrades per case.",
    "max_fingerprint_repeats": "Returns of the same blocker to the lead after a deployed fix.",
    "max_case_minutes": "Case wall time (operator pause/takeover not counted).",
    "max_quota_calls": "Paid calls per case (lead, repair, review).",
    "max_cases_per_task": "Cases per task.",
    "max_lead_calls_per_task": "Lead calls per task across its cases.",
    "max_extra_attempts_per_task": "Delegated extra attempts per task.",
    "max_upgrade_minutes": "UPGRADING longer than this goes to the operator.",
    "max_resuming_minutes": "RESUMING the engine never applied goes to the operator.",
    "lead_timeout_minutes": "Lead session timeout.",
    "extra_attempts": "Ordinary attempts a lead game assignment may use.",
    "defer_seconds": "First DEFERRED backoff (doubles, capped at 1 h).",
    "acceptance_tests": "Game test globs an assignment must declare to change.",
    "repair_broader_tests": "Broader suite of a coordinator repair.",
    "repair_root": "Repair worktree root (default <state_dir>/repairs).",
    "router_repo": "Router checkout for repairs (default the running package).",
    "supervisor": "manual | supervisor.",
    "full_authority": "The lead resolves routine recovery itself (operate actions).",
    "write_access": "The lead session gets full workspace access while it holds the lock.",
    "allow_local_route": "A local model may be the lead chain's last route.",
    "max_delegated_rounds": "Runaway guard per task budget round.",
    "max_extend_attempts": "Attempts one extend_budget op may grant.",
    "max_clean_paths": "Files one clean_untracked op may remove.",
}
_FL_DESC = {
    "enabled": "Fast-lane sprints: one long session verified by the studio gate runner.",
    "tasks": "Explicit task ids that run in the fast lane.",
    "gate_module": "Gate runner module (dotted name or .py path).",
    "gate_timeout_minutes": "A gate run still going after this is a red gate.",
    "max_red_gates_before_handoff": "Red gates before a local primary hands off to frontier.",
    "max_vision_rounds": "Vision CHANGES rounds before accept-with-notes.",
    "vision_batch_frames": "Frames per vision call.",
    "vision_parallel": "Revamp B3: vision batch calls in flight at once.",
    "auto_baseline": "Revamp B2: run the sprint's gate at its base commit before the first "
                     "session (pre-existing reds never block).",
    "early_review": "Revamp B4: start the code review while vision runs, once the gate is "
                    "green; review changes go into the same session.",
    "tick_checklist": "Revamp B4: the coordinator ticks plan checklist rows a green gate "
                      "verifies.",
    "lanes": "Lanes that run for fast-lane tasks.",
    "runner_outputs": "Files the runner rewrites every run (never a hygiene failure).",
    "runner_captures": "Runner capture globs (must be committed).",
}

_AUTO_ENUMS = {"helpers": HELPER_MODES, "scope_unlisted_default": SCOPE_DEFAULTS}
_AUTO_DESC = {
    "helpers": "on_demand: proactive helper lanes are off; a helper starts only when the "
               "primary asks for one and that primary consumes it. proactive: [lanes] apply.",
    "helper_git_measure": "Measure a delivered helper's use from git.",
    "helper_lane_misses": "Consecutive git-unused results that turn a lane off for the task "
                          "(0 = never).",
    "subagents_allowed": "Kilo `task` subagents a session may spawn (all others denied).",
    "scope_auto_approve": "Scope allow-list: harness lines the coordinator adds to a sprint's "
                          "file list itself. Each entry {name, path (glob, {task}), blocks[], "
                          "lines[] (regexes), json_paths_in_plan, reason}.",
    "scope_unlisted_default": "Recommended answer of a scope decision for a path the "
                              "allow-list doesn't cover.",
    "decision_timeout_minutes": "Product decisions apply their recommended option after this "
                                "long (0 = wait for the operator).",
    "no_progress_sessions": "Sessions in a row with no progress before a no-progress decision.",
    "auto_heal_retries": "Deterministic re-runs of a tooling/infra stop before a lead case.",
    "lead_hard_max_calls": "Absolute lead calls per task (delegation never raises it).",
    "lead_report_repairs": "Invalid lead reports sent back into the same session.",
    "infra_retry_max": "Infrastructure failures retried free per task run.",
    "infra_backoff_seconds": "First infra retry delay (doubles, capped at 15 min).",
    "preflight": "Check Kilo / Rojo / Studio MCP / local model before a session starts.",
    "preflight_endpoints": "Extra endpoints the preflight probes (name=url; tcp:// or http(s)://).",
    "context_ceiling_tokens": "A session over this context hands off to a fresh one (0 = off).",
    "memory_delta": "Send operator notes / memory once per session, then only changes.",
    "transcript_poll_seconds": "Lane transcript ingest interval.",
    "generic_checks_cache": "Reuse per-tick coordinator checks while HEAD and the tree are "
                            "unchanged.",
}

C = "coordinator"
F = Field
FIELDS: list[Field] = [
    # identity -----------------------------------------------------------------
    F("schema_version", "int", PROFILE_SCHEMA, "Profile schema version.", "identity",
      "coordinator.profile", "restart", "local", locked=True),
    F("id", "str", "", "Project id (lower-case letters, digits, . _ -).", "identity",
      "coordinator.profile", "restart", "local", locked=True),
    F("name", "str", "", "Display name.", "identity", "coordinator.profile"),
    F("version", "str", "1", "Profile version label.", "identity", "coordinator.profile"),
    F("adapter.module", "str", "", "Project adapter (module:Class).", "identity",
      "coordinator.adapters", "restart", "local"),
    F("tasks.source", "enum", "", "Task source when there is no adapter.", "identity",
      "coordinator.adapters", "restart", enum=["", "json", "toml"]),
    F("tasks.file", "str", "", "Task file (json/toml source).", "identity", "coordinator.adapters",
      "restart"),
    F("animaluprising.require_rule_amendment", "bool", True,
      "Refuse to start a sprint until AGENT_RULES.md carries the coordinator amendment.",
      "identity", "coordinator.au_adapter"),
    F("animaluprising.python", "list", [], "Python command for AU tools (default: the "
      "coordinator's own).", "identity", "coordinator.au_adapter", "restart", "local",
      item_type="str"),
    F("animaluprising.lune", "str", "", "lune.exe path when not on PATH.", "identity",
      "coordinator.au_adapter", "restart", "local"),
    F("animaluprising.runtime_fingerprint", "str", "", "Runtime-vs-revision fingerprint "
      "mode (e.g. fnv1a32-blob); empty = off.", "identity", "coordinator.au_adapter"),
    F("foundry.url", "str", "", "Foundry router URL.", "identity", "coordinator.foundry",
      "restart", "local"),
    F("foundry.key_env", "str", "", "Env var holding the orchestration key.", "identity",
      "coordinator.foundry", "restart", "local"),
    F("foundry.require_binding", "bool", True, "Refuse to run a session Foundry could not "
      "bind.", "identity", "coordinator.engine"),
    F("config_pull.enabled", "bool", True, "Pull the deployed router revision.", "identity",
      "coordinator.config_pull", "restart", "local"),
    F("config_pull.interval_seconds", "int", 60, "Seconds between pulls.", "identity",
      "coordinator.config_pull", "restart", "local", min=10),
    # workspace ------------------------------------------------------------------
    F("workspace.path", "str", "", "Game repository checkout.", "workspace",
      "coordinator.profile", "restart", "local"),
    F("workspace.allowed_dirs", "list", [], "Workspace-relative dirs sessions may write at "
      "all (checked after each session). Role edit scopes live in role_rules.", "workspace",
      "coordinator.engine", item_type="str"),
    F("workspace.branch", "str", "main", "Work branch.", "workspace", "coordinator.git",
      "restart"),
    F("workspace.remote", "str", "origin", "Git remote.", "workspace", "coordinator.git",
      "restart"),
    F("resources.exclusive", "list", [], "Resources one task holds at a time "
      "(e.g. studio:host, rojo:34872).", "workspace", "coordinator.engine", item_type="str"),
    # client ---------------------------------------------------------------------
    F("client.kind", "enum", "kilo", "Coding client.", "client", "coordinator.adapters",
      "restart", enum=["kilo", "fake", "scripted"]),
    F("client.url", "str", "", "Kilo serve URL.", "client", "coordinator.kilo", "restart",
      "local"),
    F("client.password_env", "str", "", "Env var holding the Kilo server password.", "client",
      "coordinator.kilo", "restart", "local"),
    F("client.provider", "str", "foundry", "Provider id in kilo.jsonc.", "client",
      "coordinator.kilo", "restart"),
    F("client.agent", "str", "code", "Kilo agent of the implementer session.", "client",
      "coordinator.kilo", "next_session"),
    F("client.session_timeout_minutes", "int", 240, "Kilo session timeout.", "client",
      "coordinator.kilo", "next_session", min=1),
    F("client.compaction_grace_seconds", "int", 30, "Wait for Kilo's own continuation after "
      "compaction.", "client", "coordinator.engine", min=0),
    F("client.max_compaction_nudges", "int", 2, "Continue prompts after compaction.",
      "client", "coordinator.engine", min=0),
    F("client.permissions", "table", {}, "Legacy per-session Kilo permission rules "
      "(perm -> action or {pattern: action}). Prefer role_rules; kept for compatibility.",
      "client", "coordinator.adapters", "next_session"),
    F("prompts", "table", {}, "Prompt overrides.", "client", "coordinator.engine"),
    # roles ----------------------------------------------------------------------
    *[F(f"roles.{r}", "str", "", f"Foundry persona for the {r.replace('_', ' ')}.", "roles",
        "coordinator.engine", "next_session", role_binding=r) for r in PROFILE_ROLES],
    F("role_overrides.*.tasks", "list", [], "Explicit task ids this override applies to.",
      "roles", "coordinator.engine", "next_session", item_type="str"),
    *[F(f"role_overrides.*.{r}", "str", "", f"Persona for the {r.replace('_', ' ')} on these "
        f"tasks.", "roles", "coordinator.engine", "next_session", role_binding=r)
      for r in PROFILE_ROLES],
    # review & approval ----------------------------------------------------------
    F("review.required", "bool", True, "Every task needs an approving review.", "review",
      "coordinator.review"),
    F("review.deep_when", "list", [], "Task tags that use the deep reviewer.", "review",
      "coordinator.review", item_type="str"),
    F("review.max_diff_bytes", "int", 80000, "Largest diff sent to review.", "review",
      "coordinator.review", min=1000),
    F("review.max_tokens", "int", 4000, "Review answer budget.", "review",
      "coordinator.review", min=256),
    F("review.max_packet_chars", "int", 200000, "Review packet budget fallback.", "review",
      "coordinator.review", min=1000),
    F("approval.deps_accept_pending", "list", [], "Pending approval kinds a dependency may "
      "still have.", "review", "coordinator.engine", item_type="str"),
    F("approval.visual_model", "bool", False, "Model-judged visual approval (vision persona).",
      "review", "coordinator.visual_approval"),
    F("approval.rules", "table", [], "Approval rules [{match, kind, stage, artifacts}].",
      "review", "coordinator.engine"),
    F("verify.checks", "table", [], "Verification checks [{name, command, phase, ...}].",
      "review", "coordinator.engine"),
    F("artifacts", "table", {}, "Artifact locations.", "review", "coordinator.artifacts"),
    # lead -----------------------------------------------------------------------
    *[F(f"lead_manager.{k}",
        {bool: "bool", int: "int", str: "str", list: "list"}[type(v)], v,
        _LM_DESC.get(k, ""), "lead", "coordinator.lead",
        "restart" if k in ("supervisor", "repair_root", "router_repo") else "live",
        "local" if k in ("repair_root", "router_repo") else "project",
        item_type="str" if isinstance(v, list) else None,
        role_binding="lead" if k == "persona" else None,
        min=(1 if k in ("max_diagnoses", "max_quota_calls", "max_case_minutes",
                        "lead_timeout_minutes", "max_cases_per_task", "max_lead_calls_per_task",
                        "max_upgrade_minutes", "max_resuming_minutes")
             else 0 if isinstance(v, int) and not isinstance(v, bool) else None),
        enum=list(LEAD_SUPERVISOR_MODES) if k == "supervisor" else None)
      for k, v in LEAD_MANAGER_DEFAULTS.items()],
    # fast lane ------------------------------------------------------------------
    *[F(f"fast_lane.{k}", {bool: "bool", int: "int", str: "str", list: "list"}[type(v)], v,
        _FL_DESC.get(k, ""), "fast_lane", "coordinator.fast_lane",
        item_type="str" if isinstance(v, list) else None,
        min=(1 if k in ("gate_timeout_minutes", "max_red_gates_before_handoff",
                        "vision_parallel", "vision_batch_frames")
             else 0 if isinstance(v, int) and not isinstance(v, bool) else None))
      for k, v in FAST_LANE_DEFAULTS.items()],
    # helpers & lanes ------------------------------------------------------------
    F("helpers.enabled", "bool", False, "Read-only coding helpers on frontier quota.", "helpers",
      "coordinator.helpers"),
    F("helpers.order", "list", [], "Backend order for helpers.", "helpers",
      "coordinator.helpers", item_type="str"),
    F("helpers.selective", "list", [], "Backends used only for high-value units.", "helpers",
      "coordinator.helpers", item_type="str"),
    F("helpers.agent", "str", "au-helper", "Kilo agent of helper sessions.", "helpers",
      "coordinator.helpers", "next_session"),
    F("helpers.worker_client", "str", "kilo", "Foundry client that runs helper sessions.",
      "helpers", "coordinator.helpers"),
    F("helpers.max_running", "int", 6, "Helpers running at once.", "helpers",
      "coordinator.helpers", min=0),
    F("helpers.backend_max_running", "map", {}, "Per-backend running cap.", "helpers",
      "coordinator.helpers", item_type="int"),
    F("helpers.max_units_per_task", "int", 24, "Helper units per task.", "helpers",
      "coordinator.helpers", min=0),
    F("helpers.poll_seconds", "int", 60, "Helper poll interval.", "helpers",
      "coordinator.helpers", min=1),
    F("helpers.timeout_minutes", "int", 30, "Helper execution timeout.", "helpers",
      "coordinator.helpers", min=1),
    F("helpers.startup_grace_seconds", "int", 180, "Fail an idle helper with no response "
      "after this.", "helpers", "coordinator.helpers", min=0),
    F("helpers.handoff_wait_minutes", "int", 15, "Wait for helpers at handoff.", "helpers",
      "coordinator.helpers", min=0),
    F("helpers.max_unit_requests", "int", 2, "Requests per unit.", "helpers",
      "coordinator.helpers", min=0),
    F("helpers.max_requests_per_report", "int", 4, "Requests per primary report.", "helpers",
      "coordinator.helpers", min=0),
    F("helpers.deny_tools", "list", [], "Tools helper sessions may never use.", "helpers",
      "coordinator.helpers", "next_session", item_type="str"),
    F("helpers.personas.*", "str", "", "Helper persona per backend.", "helpers",
      "coordinator.helpers", "next_session", role_binding="helper"),
    F("lanes.enabled", "bool", False, "Proactive helper lanes.", "helpers", "coordinator.lanes"),
    F("lanes.transcript_poll_seconds", "int", 20, "Transcript poll interval.", "helpers",
      "coordinator.lanes", min=1),
    F("lanes.max_lane_units_per_task", "int", 48, "Lane units per task.", "helpers",
      "coordinator.lanes", min=0),
    F("lanes.max_dispatches_per_task", "int", 32, "Lane dispatches per task.", "helpers",
      "coordinator.lanes", min=0),
    F("lanes.high_value_lanes", "list", [], "Lanes allowed on selective backends.", "helpers",
      "coordinator.lanes", item_type="str"),
    F("lanes.max_cards_per_delivery", "int", 6, "Cards per delivery.", "helpers",
      "coordinator.lanes", min=0),
    F("lanes.max_deliveries_per_session", "int", 3, "Deliveries per session.", "helpers",
      "coordinator.lanes", min=0),
    F("lanes.min_delivery_severity", "enum", "medium", "Lowest severity delivered.",
      "helpers", "coordinator.lanes", enum=["low", "medium", "high"]),
    F("lanes.skill_dirs", "list", [], "Skill folders lanes may read.", "helpers",
      "coordinator.lanes", item_type="str"),
    F("lanes.snapshot_max_bytes", "int", 400000, "Snapshot size cap.", "helpers",
      "coordinator.lanes", min=0),
    F("lanes.*.enabled", "bool", True, "Lane switch.", "helpers", "coordinator.lanes"),
    F("lanes.visual.persona", "str", "", "Visual lane persona.", "helpers", "coordinator.lanes",
      "next_session", role_binding="visual_reviewer"),
    F("lanes.*", "table", {}, "Other per-lane settings (kept verbatim).", "helpers",
      "coordinator.lanes"),
    # recovery -------------------------------------------------------------------
    *[F(f"lifecycle.{k}", "bool" if isinstance(v, bool) else "int", v, "", "recovery",
        "coordinator.engine", min=None if isinstance(v, bool) else 0)
      for k, v in LIFECYCLE_DEFAULTS.items()],
    F("frontier_primary.enabled", "bool", False, "Frontier primary handoff.", "recovery",
      "coordinator.frontier"),
    F("frontier_primary.persona", "str", "", "Frontier primary persona.", "recovery",
      "coordinator.frontier", "next_session", role_binding="implementer"),
    F("frontier_primary.max_turns", "int", 3, "Turns per frontier phase.", "recovery",
      "coordinator.frontier", min=1),
    F("frontier_primary.max_minutes", "int", 120, "Minutes per frontier phase.", "recovery",
      "coordinator.frontier", min=1),
    F("frontier_primary.max_phases_per_task", "int", 1, "Frontier phases per task.",
      "recovery", "coordinator.frontier", min=0),
    F("frontier_primary.cooldown_minutes", "int", 60, "Cooldown between phases.", "recovery",
      "coordinator.frontier", min=0),
    F("retry_guidance.enabled", "bool", False, "One bounded frontier consultation before "
      "repair attempts run out.", "recovery", "coordinator.retry_guidance"),
    F("retry_guidance.persona", "str", "", "Adviser persona (default roles.planner).",
      "recovery", "coordinator.retry_guidance", role_binding="planner"),
    F("retry_guidance.local_fallback", "bool", False, "Fall back to the local model when paid "
      "quota is refused.", "recovery", "coordinator.retry_guidance", since="0.130.20"),
    F("retry_guidance.extra_attempts", "int", 1, "Attempts the guidance may add.", "recovery",
      "coordinator.retry_guidance", min=0),
    F("retry_guidance.max_tokens", "int", 2000, "Answer budget.", "recovery",
      "coordinator.retry_guidance", min=128),
    F("retry_guidance.timeout_seconds", "int", 60, "Call timeout.", "recovery",
      "coordinator.retry_guidance", min=5),
    # limits ---------------------------------------------------------------------
    F("limits.max_attempts", "int", 3, "Attempts per task.", "limits", C, min=1),
    F("limits.max_sessions_per_task", "int", 8, "Sessions per task.", "limits", C, min=1),
    F("limits.max_review_calls", "int", 4, "Review calls per task.", "limits", C, min=1),
    F("limits.max_closeout_attempts", "int", 2, "Closeout attempts.", "limits", C, min=1),
    F("limits.session_timeout_minutes", "int", 240, "Session timeout.", "limits", C, min=1),
    F("limits.max_task_minutes", "int", 1440, "Wall time per task.", "limits", C, min=1),
    F("limits.auto_resume_after_budget_reset", "bool", True, "Resume after a budget window "
      "resets.", "limits", C),
    F("limits.live_checkpoint_seconds", "int", 120, "Kilo checkpoint refresh (0 = off).",
      "limits", C, min=0),
    F("limits.question_withdrawn_grace_seconds", "int", 60, "Resume after a withdrawn "
      "question (0 = never).", "limits", C, min=0),
    F("auto_queue_followups.*", "list", [], "Task -> follow-up tasks queued when it is "
      "approved.", "limits", C, item_type="str"),
    # [automation] (revamp B5-B13) ----------------------------------------------
    *[F(f"automation.{k}",
        "enum" if k in _AUTO_ENUMS else
        {bool: "bool", int: "int", str: "str", list: "list"}[type(v)],
        v, _AUTO_DESC.get(k, ""), "decisions", "coordinator.automation",
        "next_session" if k in ("subagents_allowed", "memory_delta") else "live",
        enum=list(_AUTO_ENUMS[k]) if k in _AUTO_ENUMS else None,
        item_type=("table" if k == "scope_auto_approve" else "str")
        if isinstance(v, list) else None,
        min=0 if isinstance(v, int) and not isinstance(v, bool) else None, since="revamp")
      for k, v in AUTOMATION_DEFAULTS.items()],
    # git ------------------------------------------------------------------------
    F("commit.prefix", "str", "{task}:", "Commit message prefix.", "git", "coordinator.git"),
    F("commit.require_prefix", "bool", True, "Refuse commits without the prefix.", "git",
      "coordinator.git"),
    F("publish.mode", "enum", "none", "Publish mode.", "git", "coordinator.git",
      enum=["none", "push", "verify_only"]),
    F("publish.remote", "str", "origin", "Publish remote.", "git", "coordinator.git"),
    F("publish.branch", "str", "", "Publish branch.", "git", "coordinator.git"),
    F("closeout.mode", "str", "session", "Closeout mode.", "git", "coordinator.engine"),
    F("closeout.exempt_paths", "list", [], "Files a CLOSE session writes; changing them after "
      "review doesn't void it.", "git", "coordinator.engine", item_type="str"),
    # telemetry / training -------------------------------------------------------
    F("telemetry.context_checkpoints", "list", [], "Context checkpoint markers.", "telemetry",
      "coordinator.telemetry", "restart", item_type="int"),
    F("telemetry.key_env", "str", "", "Env var with a telemetry key.", "telemetry",
      "coordinator.telemetry", "restart", "local"),
    F("training", "table", {}, "Training capture (all sub-tables kept verbatim).", "telemetry",
      "coordinator.training_hook"),
    # router-wide ----------------------------------------------------------------
    F("backends.*.cutoff_pct", "float", 95.0, "Stop using this backend's subscription at this "
      "usage % (writes the backend's usage_limit_pct).", "router", "router.quota_gate",
      scope="router", min=1, max=100),
]
del F

FIELDS_BY_KEY = {f.key: f for f in FIELDS}
LOCAL_KEYS = tuple(f.key for f in FIELDS if f.scope == "local")


def find_field(path: str) -> Optional[Field]:
    """The field describing a dotted path (wildcards matched segment-wise;
    an exact key beats a wildcard)."""
    if path in FIELDS_BY_KEY:
        return FIELDS_BY_KEY[path]
    segs = path.split(".")
    best = None
    for f in FIELDS:
        fs = f.key.split(".")
        if len(fs) != len(segs) or "*" not in fs:
            continue
        if all(a == "*" or a == b for a, b in zip(fs, segs)):
            stars = fs.count("*")
            if best is None or stars < best[0]:
                best = (stars, f)
    return best[1] if best else None


def is_local_key(path: str) -> bool:
    f = find_field(path)
    if f is not None:
        return f.scope == "local"
    if path.rsplit(".", 1)[-1].endswith("_env"):
        return True
    # an undeclared key of a local-only table ([config_pull], [adapter]) is local too
    t = TABLES_BY_NAME.get(path.split(".", 1)[0])
    return bool(t is not None and t.scope == "local" and "." in path)


def check_value(f: Field, value: Any) -> Optional[str]:
    """Why `value` doesn't fit field `f` (None when it fits)."""
    t = f.type

    def num_ok(v, kinds):
        return isinstance(v, kinds) and not isinstance(v, bool)

    if t == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if t == "int":
        if not num_ok(value, int):
            return "must be an integer"
    elif t == "float":
        if not num_ok(value, (int, float)):
            return "must be a number"
    elif t == "str":
        return None if isinstance(value, str) else "must be a string"
    elif t == "enum":
        return None if value in (f.enum or []) else f"must be one of {f.enum}"
    elif t == "list":
        if not isinstance(value, list):
            return "must be a list"
        want = {"str": str, "int": int}.get(f.item_type or "")
        if want is str and any(not isinstance(x, str) or not x.strip() for x in value):
            return "must be a list of non-empty strings"
        if want is int and any(not num_ok(x, int) for x in value):
            return "must be a list of integers"
        return None
    elif t == "map":
        if not isinstance(value, dict):
            return "must be a table"
        want = {"str": (str,), "int": (int,), "float": (int, float)}.get(f.item_type or "")
        if want and any(not isinstance(v, want) or isinstance(v, bool) for v in value.values()):
            return f"values must be {f.item_type}"
        return None
    elif t == "table":
        return None if isinstance(value, (dict, list)) else "must be a table"
    if f.min is not None and value < f.min:
        return f"must be at least {f.min:g}"
    if f.max is not None and value > f.max:
        return f"must be at most {f.max:g}"
    return None


def schema_json() -> dict:
    from . import FORMAT, SCHEMA_VERSION
    from .role_rules import CATEGORIES, ACTIONS, HARD_DENIES, default_rules
    return {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "groups": GROUPS,
        "tables": [t.to_json() for t in TABLES],
        "fields": [f.to_json() for f in FIELDS],
        "roles": list(RULE_ROLES),
        "profile_roles": list(PROFILE_ROLES),
        "lanes": list(KNOWN_LANES),
        "role_rules": {
            "categories": list(CATEGORIES),
            "actions": list(ACTIONS),
            "defaults": {r: default_rules(r) for r in RULE_ROLES},
            "hard_denies": [dict(h) for h in HARD_DENIES],
        },
    }
