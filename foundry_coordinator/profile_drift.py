"""Doctor rows for profile values that contradict the revamp decisions (W1.7).

The live AU profile drifted from what Victor decided on 2026-10-21/22 (audit
04 #6): two vision rounds instead of one, no preflight endpoints for Rojo or the
local model, no adapter infra checks, a lead hard cap below the per-task lead
budget, helper caps sized for proactive lanes, a dead [lanes] table under
on-demand helpers, and a kilo.jsonc that still lets Kilo ask its snapshot
question. `checks(P)` returns (WARN|PASS, name, detail) rows for
`foundry-coordinator doctor`; nothing here changes a profile.
"""

from __future__ import annotations

import os
from typing import Optional

from .automation import settings as auto_settings

PASS, WARN = "PASS", "WARN"


def _raw(P) -> dict:
    return dict(getattr(P.profile, "raw", None) or {})


def checks(P) -> list[tuple[str, str, str]]:
    raw = _raw(P)
    out: list[tuple[str, str, str]] = []
    auto = auto_settings(P)

    # 1. one vision CHANGES round (decision 1)
    fl = raw.get("fast_lane") or {}
    if isinstance(fl, dict) and fl.get("enabled"):
        rounds = fl.get("max_vision_rounds", 1)
        out.append((PASS if rounds == 1 else WARN, "drift: max_vision_rounds",
                    f"[fast_lane] max_vision_rounds = {rounds}"
                    + ("" if rounds == 1 else " (revamp decision 1: one CHANGES round, then "
                                              "accept with notes; set it to 1)")))

    # 2. preflight endpoints for Rojo and the local model
    eps = [str(e) for e in auto.get("preflight_endpoints") or []]
    names = [e.split("=", 1)[0].strip().lower() for e in eps]
    has_rojo = any("rojo" in n or "34872" in e for n, e in zip(names, eps))
    has_local = any(any(k in n for k in ("local", "model", "llama", "strata", "vllm"))
                    for n in names)
    out.append((PASS if has_rojo else WARN, "drift: preflight rojo",
                "Rojo is probed before a session" if has_rojo else
                "[automation] preflight_endpoints has no Rojo entry (e.g. "
                "\"rojo=http://127.0.0.1:34872/api/rojo\"): a Rojo outage costs a session"))
    out.append((PASS if has_local else WARN, "drift: preflight local model",
                "the local model server is probed before a session" if has_local else
                "[automation] preflight_endpoints has no local-model entry (e.g. "
                "\"local-model=http://127.0.0.1:8080/health\")"))

    # 3. adapter infra checks (the AU adapter has them since W1.8)
    fn = getattr(getattr(P, "adapter", None), "infra_checks", None)
    out.append((PASS if callable(fn) else WARN, "drift: adapter infra_checks",
                "adapter.infra_checks() present" if callable(fn) else
                "the adapter has no infra_checks(): Studio / Rojo are not checked live before "
                "a session"))

    # 4. lead hard cap vs per-task budget (W1.5 derives it; the file still misleads)
    hard = int(auto.get("lead_hard_max_calls") or 0)
    lm = raw.get("lead_manager") or {}
    per_task = int((lm.get("max_lead_calls_per_task") if isinstance(lm, dict) else 0) or 0)
    if hard and per_task and hard < per_task:
        out.append((WARN, "drift: lead_hard_max_calls",
                    f"[automation] lead_hard_max_calls = {hard} < [lead_manager] "
                    f"max_lead_calls_per_task = {per_task}: the effective hard cap is "
                    f"{per_task} (derived, W1.5); set the file to match"))
    elif hard:
        out.append((PASS, "drift: lead_hard_max_calls", f"hard cap {hard} >= per-task "
                                                        f"{per_task or 'default'}"))

    # 5. helpers on demand: dead [lanes] config and lane-sized caps
    mode = str(auto.get("helpers") or "on_demand")
    lanes = raw.get("lanes") or {}
    if mode == "on_demand" and isinstance(lanes, dict) and lanes.get("enabled"):
        out.append((WARN, "drift: lanes under on_demand",
                    "[lanes] enabled = true is dead config while [automation] helpers = "
                    "\"on_demand\" (every lane is forced off); it misleads the GUI"))
    helpers = raw.get("helpers") or {}
    if mode == "on_demand" and isinstance(helpers, dict):
        caps = {k: helpers.get(k) for k in ("max_units_per_task", "max_running")
                if isinstance(helpers.get(k), int)}
        high = {k: v for k, v in caps.items() if (k == "max_units_per_task" and v > 8)
                or (k == "max_running" and v > 3)}
        if high:
            out.append((WARN, "drift: helper caps",
                        "[helpers] " + ", ".join(f"{k} = {v}" for k, v in high.items())
                        + " are sized for proactive lanes; on demand, 6 units per task and 2-3 "
                          "running are plenty (UI-02a used 93 helper sessions)"))

    # 6. context ceiling vs telemetry checkpoints
    ceil = int(auto.get("context_ceiling_tokens") or 0)
    if int(auto.get("context_ceiling_pct") or 0) > 0:
        ceil = 0          # the ceiling follows each model's window: no fixed number to align
    cps = (raw.get("telemetry") or {}).get("context_checkpoints") if isinstance(
        raw.get("telemetry"), dict) else None
    if ceil and isinstance(cps, list) and cps:
        try:
            lowest = min(int(x) for x in cps)
        except (TypeError, ValueError):
            lowest = 0
        if lowest and lowest < ceil:
            out.append((WARN, "drift: context thresholds",
                        f"[telemetry] context_checkpoints starts at {lowest} but [automation] "
                        f"context_ceiling_tokens = {ceil}: two thresholds; align them"))

    # 7. kilo.jsonc: Kilo's snapshot question must be off (W1.1)
    off = kilo_snapshot_off(P)
    if off is not None:
        out.append((PASS if off else WARN, "drift: kilo snapshot",
                    "kilo.jsonc: snapshot = false" if off else
                    "kilo.jsonc has no `\"snapshot\": false`: Kilo may ask its snapshot "
                    "question (the implementer's question tool is denied, but the top-level "
                    "key is the clean fix; `kilo-config --apply` writes it)"))
    return out


def kilo_snapshot_off(P) -> Optional[bool]:
    """True when the project's kilo.jsonc sets top-level `snapshot: false`, False
    when the key is absent or anything else, None when the file is missing or
    unreadable."""
    try:
        from foundry_automation import jsonc
        from .kilo_config import kilo_path
        path = kilo_path(P.profile)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            data = jsonc.parse(f.read())
    except Exception:                                            # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return data.get("snapshot") is False
