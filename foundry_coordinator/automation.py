"""[automation] (revamp B5-B13, 2026-10-21): the coordinator's own policy knobs.

One validated profile table, so the Foundry-Router GUI can expose it as one
section. Every key has a working default; a profile without the table gets
exactly these defaults. Read it with `settings(P)` (P = a Project, a Profile or
anything with a `.profile` / `.raw`), which never raises.

W2.5: the keys, defaults, scope allow-list rules and the validator live ONCE,
in `foundry_automation.schema` (the router validates drafts with the same
code); this module re-exports them and wraps the validator in the
coordinator's own error type. `tests/test_automation_parity.py` keeps them
equal.

  helpers                    "on_demand" (default): proactive helper lanes are off
                             for every task; a helper starts only when the primary
                             asks for one (helper_requests) and it is consumed by
                             that primary. "proactive" = the [lanes] settings apply.
  helper_git_measure         measure a delivered helper's use from git (a commit of
                             the consumer touches a path of the proposal)
  helper_lane_misses         consecutive git-unused results that turn a lane off
                             for the task (0 = never)
  subagents_allowed          Kilo `task` subagents a session may spawn when NO role
                             rules are deployed (deployed role rules: the role's
                             `task_agents`); default ["explore"]
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

from foundry_automation.schema import (AUTOMATION_DEFAULTS, DEFAULT_SCOPE_RULES,  # noqa: F401
                                       HELPER_MODES, SCOPE_DEFAULTS, SettingsError,
                                       automation_table)


class AutomationError(SettingsError):
    pass


def table(path: str, raw) -> dict:
    """Validate the [automation] table (raises AutomationError)."""
    try:
        return automation_table(path, raw)
    except AutomationError:
        raise
    except SettingsError as e:
        raise AutomationError(str(e), getattr(e, "path", "")) from None


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
