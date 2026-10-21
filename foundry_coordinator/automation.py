"""[automation] (revamp B5-B13, 2026-10-21): the coordinator's own policy knobs.

One validated profile table, so the Foundry-Router GUI can expose it as one
section. Every key has a working default; a profile without the table gets
exactly these defaults. Read it with `settings(P)` (P = a Project, a Profile or
anything with a `.profile` / `.raw`), which never raises.

  helpers                    "on_demand" (default): proactive helper lanes are off
                             for every task; a helper starts only when the primary
                             asks for one (helper_requests) and it is consumed by
                             that primary. "proactive" = the [lanes] settings apply.
  helper_git_measure         measure a delivered helper's use from git (a commit of
                             the consumer touches a path of the proposal)
  helper_lane_misses         consecutive git-unused results that turn a lane off
                             for the task (0 = never)
  subagents_allowed          Kilo `task` subagents a session may spawn (all others
                             are denied); default ["explore"]
  scope_auto_approve         the scope allow-list (see scope.py): harness lines the
                             coordinator adds to a sprint's file list by itself
  scope_unlisted_default     the recommended answer of a scope decision for a path
                             the allow-list doesn't cover: "deny" | "approve"
  decision_timeout_minutes   product decisions apply their recommended option after
                             this long (0 = wait for the operator)
  no_progress_sessions       sessions in a row with no commit, no verified delta and
                             no plan step before a no-progress decision
  auto_heal_retries          deterministic re-runs of a tooling / infra stop before a
                             lead case opens (per head and class)
  lead_hard_max_calls        absolute lead calls per task (delegation never raises it)
  lead_report_repairs        invalid lead reports sent back into the same session
  infra_retry_max            infrastructure failures retried free per task run
  infra_backoff_seconds      first infra retry delay (doubles, capped at 15 min)
  preflight                  check Kilo / Rojo / Studio MCP / local model before a
                             session starts (an infra failure costs no attempt)
  preflight_endpoints        extra endpoints the preflight probes, "name=url" with
                             url tcp://host:port or http(s)://... (e.g. Rojo, the
                             local model server); default none
  context_ceiling_tokens     a session over this context hands off to a fresh one
                             (0 = off)
  memory_delta               send operator notes / memory blocks once per session,
                             then only what changed
  transcript_poll_seconds    lane transcript ingest interval (was 20 s)
  generic_checks_cache       reuse the per-tick coordinator checks while HEAD and the
                             working tree are unchanged
"""

from __future__ import annotations

from typing import Any

HELPER_MODES = ("on_demand", "proactive")
SCOPE_DEFAULTS = ("deny", "approve")

# The harness lines a sprint legitimately needs (Victor 2026-10-21). `path` is a
# glob ({task} = the sprint id). Optional constraints on the change itself:
# `blocks` = every added/removed line lies inside one of these named table
# assignments (e.g. `EXPECTED_BOOT = { ... }`); `lines` = regexes every changed
# line must match; `json_paths_in_plan` = a default.project.json change may only
# add/remove `"$path"` mappings to files the plan lists (plus JSON structure).
DEFAULT_SCOPE_RULES = [
    {"name": "gate_runner_boot_and_mounted_rows", "path": "tests/GateRunner.lua",
     "blocks": ["EXPECTED_BOOT", "MOUNTED_TESTS"]},
    {"name": "sprint_gate_manifest", "path": "docs/progress/{task}_gate.json"},
    {"name": "rojo_mapping_for_plan_files", "path": "default.project.json",
     "json_paths_in_plan": True},
]

AUTOMATION_DEFAULTS: dict[str, Any] = {
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

_RULE_KEYS = {"name", "path", "lines", "blocks", "json_paths_in_plan", "reason"}


class AutomationError(ValueError):
    pass


def _rules(path: str, v) -> list[dict]:
    import re
    if not isinstance(v, list):
        raise AutomationError(f"{path}: automation.scope_auto_approve must be a list of tables")
    out = []
    for i, r in enumerate(v):
        if not isinstance(r, dict) or not isinstance(r.get("path"), str) or \
                not r["path"].strip():
            raise AutomationError(f"{path}: automation.scope_auto_approve[{i}] needs a path")
        bad = sorted(set(r) - _RULE_KEYS)
        if bad:
            raise AutomationError(f"{path}: automation.scope_auto_approve[{i}]: unknown keys "
                                  f"{bad}")
        p = r["path"].replace("\\", "/").strip()
        if p.startswith(("/", "../")) or ".." in p.split("/") or ":" in p:
            raise AutomationError(f"{path}: automation.scope_auto_approve[{i}].path must be a "
                                  f"workspace-relative glob")
        lines = r.get("lines") or []
        if not isinstance(lines, list) or any(not isinstance(x, str) for x in lines):
            raise AutomationError(f"{path}: automation.scope_auto_approve[{i}].lines must be a "
                                  f"list of regexes")
        for x in lines:
            try:
                re.compile(x)
            except re.error as e:
                raise AutomationError(f"{path}: automation.scope_auto_approve[{i}].lines: "
                                      f"{x!r}: {e}") from None
        blocks = r.get("blocks") or []
        if not isinstance(blocks, list) or any(
                not isinstance(x, str) or not re.match(r"^[A-Za-z_][\w.]*$", x) for x in blocks):
            raise AutomationError(f"{path}: automation.scope_auto_approve[{i}].blocks must be "
                                  f"a list of identifiers")
        out.append({"name": str(r.get("name") or p), "path": p, "lines": list(lines),
                    "blocks": list(blocks),
                    "json_paths_in_plan": bool(r.get("json_paths_in_plan", False)),
                    "reason": str(r.get("reason") or "")})
    return out


def table(path: str, raw) -> dict:
    """Validate the [automation] table (raises AutomationError)."""
    a = raw if raw is not None else {}
    if not isinstance(a, dict):
        raise AutomationError(f"{path}: [automation] must be a table")
    unknown = sorted(set(a) - set(AUTOMATION_DEFAULTS))
    if unknown:
        raise AutomationError(f"{path}: unknown [automation] keys {unknown}")
    out = defaults()
    for k, v in a.items():
        want = type(AUTOMATION_DEFAULTS[k])
        if k == "scope_auto_approve":
            out[k] = _rules(path, v)
            continue
        if want is bool and not isinstance(v, bool):
            raise AutomationError(f"{path}: automation.{k} must be true or false")
        if want is int and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise AutomationError(f"{path}: automation.{k} must be a non-negative integer")
        if want is str and not isinstance(v, str):
            raise AutomationError(f"{path}: automation.{k} must be a string")
        if want is list and (not isinstance(v, list) or
                             any(not isinstance(x, str) or not x.strip() for x in v)):
            raise AutomationError(f"{path}: automation.{k} must be a list of non-empty strings")
        out[k] = list(v) if isinstance(v, list) else v
    if out["helpers"] not in HELPER_MODES:
        raise AutomationError(f"{path}: automation.helpers must be one of {HELPER_MODES}")
    if out["scope_unlisted_default"] not in SCOPE_DEFAULTS:
        raise AutomationError(f"{path}: automation.scope_unlisted_default must be one of "
                              f"{SCOPE_DEFAULTS}")
    return out


def defaults() -> dict:
    return {k: ([dict(x) if isinstance(x, dict) else x for x in v] if isinstance(v, list)
                else v) for k, v in AUTOMATION_DEFAULTS.items()}


_CACHE: dict = {}


def settings(P) -> dict:
    """The validated [automation] settings of a project (P = Project, Profile or a
    stand-in with .profile/.raw). Never raises: an invalid or missing table gives
    the defaults (profile.load already refused an invalid one)."""
    prof = getattr(P, "profile", None) or P
    raw = getattr(prof, "raw", None) or {}
    if isinstance(raw, dict) and isinstance(raw.get("automation"), dict):
        hit = _CACHE.get(id(raw))
        if hit is not None and hit[0] is raw["automation"]:
            return hit[1]
        try:
            out = table("profile", raw["automation"])
        except AutomationError:
            out = defaults()
        _CACHE[id(raw)] = (raw["automation"], out)
        return out
    got = getattr(prof, "automation", None)
    if isinstance(got, dict) and got:
        return got
    return defaults()


def get(P, key: str):
    if key not in AUTOMATION_DEFAULTS:
        raise KeyError(f"unknown [automation] setting {key!r}")
    return settings(P).get(key, AUTOMATION_DEFAULTS[key])
