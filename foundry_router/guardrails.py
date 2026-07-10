"""Guardrail engine (design doc §4.7) — built in from the start, not bolted on.

Enforced per request: max agent steps, max paid-model calls, optional
daily/weekly spend caps for metered backends, and the Meridian usage-window
check before committing to a Claude call. All globally configured, all
overridable per persona via personas.guardrail_overrides (§4.8).

Authority (§4.3): when guardrails.authority == "defer_to_pool", spend/rate
enforcement is skipped in favor of whatever LiteLLM/Olla already applies —
but max_steps stays enforced regardless, because a runaway agent loop is a
property of THIS service that no external pool can see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import GuardrailsConfig
from .db import Database
from .personas import PersonaStore
from .usage import MeridianUsage, spend_since

log = logging.getLogger(__name__)


@dataclass
class RequestGuardState:
    """Per-request counters, owned by the agent loop."""
    steps: int = 0
    paid_calls: int = 0
    events: list[str] = field(default_factory=list)
    # Usage-credits last-resort handshake: the first Claude attempt after
    # window exhaustion is denied with instructions to try local first; this
    # flag remembers the denial so an insistent second attempt is permitted
    # (and logged as spending purchased credits).
    credits_warned: bool = False


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""


class GuardrailEngine:
    def __init__(self, cfg: GuardrailsConfig, db: Database, meridian_usage: MeridianUsage,
                 pool_mode: str = "internal"):
        self.cfg = cfg
        self.db = db
        self.meridian_usage = meridian_usage
        self.pool_mode = pool_mode

    def _defers(self) -> bool:
        # defer_to_pool is inert under mode: internal (§4.3) — there is no
        # external tool to defer to, so internal enforcement stays on.
        return self.cfg.authority == "defer_to_pool" and self.pool_mode != "internal"

    def effective(self, persona: Optional[dict]) -> dict:
        base = {
            "max_steps_per_request": self.cfg.max_steps_per_request,
            "max_paid_calls_per_request": self.cfg.max_paid_calls_per_request,
            "daily_spend_cap_usd": self.cfg.daily_spend_cap_usd,
            "weekly_spend_cap_usd": self.cfg.weekly_spend_cap_usd,
        }
        base.update({k: v for k, v in PersonaStore.guardrail_overrides(persona).items()
                     if k in base})
        return base

    # -- checks -----------------------------------------------------------------

    def check_step(self, state: RequestGuardState, effective: dict) -> Verdict:
        """Called at the top of every agent-loop iteration. Always enforced —
        loop protection is not delegable."""
        if state.steps >= effective["max_steps_per_request"]:
            msg = f"max agent steps reached ({effective['max_steps_per_request']})"
            state.events.append(msg)
            return Verdict(False, msg)
        return Verdict(True)

    async def check_paid_call(self, model_id: str, backend_info: Optional[dict],
                              model_meta: Optional[dict], state: RequestGuardState,
                              effective: dict) -> Verdict:
        """Called before dispatching to any backend. 'Paid' covers two distinct
        things (§4.7, don't conflate): subscription-window models (Meridian —
        counts against max_paid_calls and the usage window, never dollar caps)
        and metered models (counts against max_paid_calls and spend caps)."""
        backend_type = (backend_info or {}).get("type", "")
        is_subscription = backend_type == "anthropic-compatible"
        is_metered = bool(model_meta and (
            (model_meta.get("cost_per_1k_input") or 0) > 0
            or (model_meta.get("cost_per_1k_output") or 0) > 0))
        if not is_subscription and not is_metered:
            return Verdict(True)  # local models are free — no checks

        if self._defers():
            return Verdict(True)  # LiteLLM/Olla budgets are the authority here

        if state.paid_calls >= effective["max_paid_calls_per_request"]:
            msg = (f"max paid-model calls per request reached "
                   f"({effective['max_paid_calls_per_request']}) — use a local model")
            state.events.append(f"denied {model_id}: {msg}")
            return Verdict(False, msg)

        if is_subscription and backend_info and backend_info.get("url"):
            from .usage import claude_premium_level
            snap = await self.meridian_usage.snapshot(
                backend_info["url"], backend_info.get("api_key"))
            mcfg = self.meridian_usage.cfg
            level = claude_premium_level(model_id)
            fable_used = snap.get("fable_used")

            # Exhaustion: the general window, or — for Fable-class only — its
            # own separate weekly bucket (it must never gate other tiers).
            fable_exhausted = (level >= 4 and fable_used is not None
                               and (1.0 - fable_used) < mcfg.min_window_fraction)
            if not snap["available"] or fable_exhausted:
                which = "Fable weekly budget" if fable_exhausted else "Claude usage window"
                if mcfg.usage_credits == "last_resort" and not fable_exhausted:
                    # Purchased credits exist to be a LAST resort: deny the
                    # first attempt with instructions; permit an insistent
                    # second attempt and log the spend.
                    if state.credits_warned:
                        note = (f"window exhausted — proceeding on PURCHASED USAGE "
                                f"CREDITS for {model_id} (last resort)")
                        state.events.append(note)
                        self.db.log_event("warning", "guardrails", note, snap["note"])
                        state.paid_calls += 1
                        return Verdict(True)
                    state.credits_warned = True
                    msg = (f"{which} exhausted ({snap['note']}). Purchased usage "
                           f"credits are the LAST RESORT: first try a local model; "
                           f"only call this Claude tool again if no local model can "
                           f"possibly handle the task — that retry will be permitted "
                           f"and will spend credits.")
                    state.events.append(f"denied {model_id}: window exhausted (credits offered)")
                    return Verdict(False, msg)
                msg = (f"{which} exhausted ({snap['note']}) — "
                       f"use Opus/Sonnet instead" if fable_exhausted else
                       f"{which} exhausted ({snap['note']}) — route locally instead")
                state.events.append(f"denied {model_id}: {msg}")
                self.db.log_event("info", "guardrails",
                                  f"window-exhausted denial for {model_id}", snap["note"])
                return Verdict(False, msg)

            # Adaptive tier conservation: as real windows fill, deny
            # progressively more expensive Claude tiers so remaining budget
            # goes to work that genuinely needs them. Fable (level 4) answers
            # to BOTH ladders: the general thresholds and its own bucket's.
            worst = snap.get("worst_used")
            if (level >= 4 and fable_used is not None
                    and fable_used >= mcfg.conserve_fable_at):
                msg = (f"Fable budget {fable_used:.0%} used — conserving Fable; "
                       f"use Opus for this task (it handles complex debugging/"
                       f"architecture without touching the Fable budget)")
                state.events.append(f"denied {model_id}: {msg}")
                return Verdict(False, msg)
            if worst is not None:
                if worst >= mcfg.conserve_strong_at and level >= 2:
                    msg = (f"window {worst:.0%} used — conserving: only the cheapest "
                           f"Claude tier (Haiku-class) or local models until reset")
                    state.events.append(f"denied {model_id}: {msg}")
                    return Verdict(False, msg)
                if worst >= mcfg.conserve_premium_at and level >= 3:
                    msg = (f"window {worst:.0%} used — conserving Opus-and-above; "
                           f"use Sonnet/Haiku-class or local for this task")
                    state.events.append(f"denied {model_id}: {msg}")
                    return Verdict(False, msg)

        if is_metered:
            for cap_key, modifier, label in (
                    ("daily_spend_cap_usd", "-1 day", "daily"),
                    ("weekly_spend_cap_usd", "-7 days", "weekly")):
                cap = effective.get(cap_key)
                if cap is not None and spend_since(self.db, modifier) >= cap:
                    msg = f"{label} spend cap (${cap}) reached — use a local model"
                    state.events.append(f"denied {model_id}: {msg}")
                    return Verdict(False, msg)

        state.paid_calls += 1
        return Verdict(True)
