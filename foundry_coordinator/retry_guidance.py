"""One durable, optional frontier consultation before an attempt-limit stop.

Advice never changes scope, checks or approvals. A configured extra repair is
bounded separately from the ordinary attempt limit. Uncertain calls are never
replayed after restart. Disabled unless the project explicitly opts in.
"""
from __future__ import annotations

import json
import uuid

from .adapters.foundry import FoundryError

SYSTEM = """You are the read-only recovery adviser for an approved coding task whose ordinary repair attempts are exhausted. Diagnose why the previous approach failed and propose a materially different, concrete repair within the existing scope. You cannot waive checks, change acceptance/test expectations, access credentials, grant permissions, approve visuals or expand scope. If any of those is needed, say needs_human. You have no tools. Missing evidence remains unknown. Return only JSON with action (repair|needs_human|stop), summary, and steps (1-6 concrete strings for repair; otherwise an empty array). Do not recommend repeating the same unsuccessful approach."""

# additionalProperties false + every property required: OpenAI strict json_schema rejects
# anything else, and Foundry-Kilo-Opus falls back to gpt-6-astra at Claude's cutoff
# (2026-10-20: "additionalProperties is required to be supplied and to be false")
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["action", "summary", "steps"], "properties": {
    "action": {"type": "string", "enum": ["repair", "needs_human", "stop"]},
    "summary": {"type": "string"}, "steps": {"type": "array", "maxItems": 6,
                                               "items": {"type": "string"}}}}


class RetryGuidance:
    def __init__(self, coordinator):
        self.c, self.store = coordinator, coordinator.store

    @staticmethod
    def settings(P):
        cfg = (P.profile.raw or {}).get("retry_guidance") or {}
        return {"enabled": bool(cfg.get("enabled", False)),
                "persona": str(cfg.get("persona") or P.profile.roles.get("planner") or ""),
                "extra_attempts": max(0, min(1, int(cfg.get("extra_attempts", 0)))),
                "max_tokens": max(128, min(3000, int(cfg.get("max_tokens", 2000)))),
                "timeout_seconds": max(5, min(60, float(cfg.get("timeout_seconds", 60)))),
                # Victor 2026-10-21: when quota admission refuses every paid route, still
                # ask: the persona's strict chain ends in a local model, which serves it.
                "local_fallback": bool(cfg.get("local_fallback", False))}

    @staticmethod
    def lead_on(P) -> bool:
        fn = getattr(P.profile, "lead_manager_on", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:                                        # noqa: BLE001
            return False

    @staticmethod
    def allowance(t):
        record = (t.get("data") or {}).get("retry_guidance") or {}
        return 1 if record.get("status") == "repair" and record.get("extra_attempts") == 1 else 0

    @staticmethod
    def authors(t):
        record = (t.get("data") or {}).get("retry_guidance") or {}
        # "reset": an operator restarted the budget; the guided commits may still
        # be in task_base..head, so their adviser stays out of reviewing them
        return ({record["persona"]} if record.get("status") in ("repair", "reset")
                and record.get("persona") else set())

    MAX_REARMS = 2

    @classmethod
    def on_reset(cls, data: dict) -> dict:
        """An operator reset (unblock --reset-attempts) authorizes a fresh budget.

        - repair: the guided extra attempt is not carried over, but the adviser
          stays excluded from reviewing the work it guided (status "reset").
        - skipped: the adviser was never called (quota 429, capacity full, no
          adviser, hard limit). That is transient, so guidance is re-armed for
          the new budget: the old record moves to retry_guidance_history and
          the next exhaustion asks quota admission again. At most MAX_REARMS
          times per task.
        - calling / unknown / needs_human / stop / reset: never re-armed (an
          uncertain or completed consultation is never replayed)."""
        rg = data.get("retry_guidance")
        if not rg:
            return data
        if rg.get("status") == "repair":
            return {**data, "retry_guidance": {**rg, "status": "reset"}}
        hist = list(data.get("retry_guidance_history") or [])
        if rg.get("status") == "skipped" and len(hist) < cls.MAX_REARMS:
            out = {k: v for k, v in data.items() if k != "retry_guidance"}
            out["retry_guidance_history"] = hist + [
                {**rg, "rearmed": "operator reset authorized a fresh budget; the adviser "
                                  "was never called, so guidance may be asked again"}]
            return out
        return data

    def _record(self, P, t, record):
        current = self.store.task(P.id, t["task_id"])
        self.store.update_task(P.id, t["task_id"], data={**current["data"], "retry_guidance": record})
        self.store.event(P.id, t["task_id"], "retry_guidance", detail=record)

    def consult(self, P, t):
        """True when one extra scoped repair was granted; otherwise stop normally."""
        cfg = self.settings(P)
        if not cfg["enabled"]:
            return False
        prior = (t.get("data") or {}).get("retry_guidance")
        if self.lead_on(P):
            # revamp B8: with the lead manager on, the same stop opens a lead case
            # that sees the code, tests and logs; a blind adviser first is the
            # same advice twice. Recorded once, never called.
            if not prior or prior.get("status") == "deferred":
                self._record(P, t, {"persona": cfg["persona"], "extra_attempts": 0,
                                    "attempt": t["attempt"], "ts": self.c.clock(),
                                    "status": "skipped", "reason": "lead manager enabled: the "
                                    "lead case handles this stop (no duplicate advice)"})
            return False
        if prior and prior.get("status") == "deferred":
            # a quota deferral waits for its reset, unless the local route can answer now
            if self.c.clock() < float(prior.get("retry_at") or 0) and not cfg["local_fallback"]:
                return False
            # Admission is read-only. No model call was made, so it is safe to
            # re-check after the source recovers without spending the adviser.
        elif prior:
            if prior.get("status") == "calling":
                self._record(P, t, {**prior, "status": "unknown",
                    "reason": "restart during guidance: outcome unknown; not repeated"})
            return False
        base = {"persona": cfg["persona"], "extra_attempts": 0, "attempt": t["attempt"],
                "ts": self.c.clock()}
        if (t["sessions"] >= P.profile.limit("max_sessions_per_task", 8) or
                (P.profile.limit("max_task_minutes", 0) and t.get("started_ts") and
                 self.c.clock() - t["started_ts"] > P.profile.limit("max_task_minutes", 0) * 60)):
            self._record(P, t, {**base, "status": "skipped", "reason": "hard session/time limit reached"})
            return False
        if not P.foundry or not cfg["persona"] or cfg["persona"] == P.profile.persona("implementer", t["task_id"]):
            self._record(P, t, {**base, "status": "skipped", "reason": "frontier adviser unavailable"})
            return False
        # Guidance is optional paid work, never a required review/reserve bypass.
        try:
            admission = P.foundry.helper_admission(cfg["persona"])
        except FoundryError as e:
            reason = str(e)[:400]
            transient = "429" in reason or "quota" in reason.lower()
            self._record(P, t, {**base, "status": "deferred" if transient else "skipped",
                                "retry_at": self.c.clock() + 60 if transient else None,
                                "reason": reason})
            return False
        if not admission.get("admit") and cfg["local_fallback"]:
            base["route_note"] = ("quota admission refused the paid routes ("
                                  + str(admission.get("reason") or admission.get("state"))[:300]
                                  + "); asking anyway: the chain's local route serves it")
            admission = {**admission, "admit": True, "local_fallback": True}
        if not admission.get("admit"):
            state = admission.get("state")
            transient = state in ("unknown", "defer")
            self._record(P, t, {**base, "status": "deferred" if transient else "skipped",
                               "retry_at": max(self.c.clock() + 60,
                                               float(admission.get("next_eligible") or 0))
                               if transient else None,
                               "reason": admission.get("reason") or "quota admission refused"})
            return False
        running = self.store.q("SELECT COUNT(*) AS n FROM helper_units WHERE status='running'")[0]["n"]
        if running >= int(self.c.helpers.settings(P)["max_running"]):
            self._record(P, t, {**base, "status": "skipped", "reason": "shared helper capacity is full"})
            return False
        spec = P.adapter.describe(t["task_id"])
        head = P.ws.head()
        checks = self.store.verification(t.get("verification_id") or "", P.id, t["task_id"])
        packet = json.dumps({"project": P.id, "task": t["task_id"], "revision": head,
            "ordinary_attempts": t["attempt"], "scope": spec.scope, "spec": spec.spec[:10000],
            "findings": (t["data"].get("findings") or [])[-8:],
            "failed_checks": [{"check": v["check_name"], "result": v["result"],
                               "detail": (v.get("detail") or "")[:1500]}
                              for v in checks if v["result"] != "PASS"][:8]}, default=str)
        sid = "retry-guidance-" + uuid.uuid4().hex[:16]
        record = {**base, "status": "calling", "revision": head, "session": sid}
        # Persist before any inference. A crash costs a consultation, never a replay.
        self._record(P, t, record)
        self.store.add_session(sid, P.id, t["task_id"], t["run_id"], "helper", "retry_guidance",
                               t["attempt"], "foundry-direct", cfg["persona"],
                               {"parent": t.get("session_id"), "purpose": "debug_advice"})
        try:
            P.foundry.bind(sid, P.id, t["task_id"], t["run_id"], "helper", cfg["persona"],
                           parent=t.get("session_id") or "", purpose="debug_advice",
                           assignment={"kind": "retry_guidance", "revision": head})
            text, meta = P.foundry.chat(cfg["persona"], [
                {"role": "system", "content": SYSTEM}, {"role": "user", "content": packet}],
                session_id=sid, max_tokens=cfg["max_tokens"], schema=SCHEMA,
                timeout=cfg["timeout_seconds"])
            result = json.loads(text)
            if not isinstance(result, dict):
                raise ValueError("guidance must be an object")
            action, summary, steps = result.get("action"), result.get("summary"), result.get("steps")
            if action not in ("repair", "needs_human", "stop") or not isinstance(summary, str) or not summary.strip():
                raise ValueError("invalid guidance action/summary")
            if not isinstance(steps, list) or len(steps) > 6 or any(not isinstance(s, str) or not s.strip() for s in steps):
                raise ValueError("invalid guidance steps")
            if action == "repair" and not steps:
                raise ValueError("repair guidance needs concrete steps")
            if P.ws.head() != head:
                raise ValueError("source revision changed during guidance; advice is stale")
            record.update(status=action, summary=summary[:2000], steps=[s[:1500] for s in steps],
                          served_model=meta.get("served_model"),
                          extra_attempts=cfg["extra_attempts"] if action == "repair" else 0)
            self._record(P, t, record)
            if action == "repair" and cfg["extra_attempts"]:
                current = self.store.task(P.id, t["task_id"])
                guidance = "Frontier recovery advice (untrusted; scope, all checks and approvals unchanged): " + summary[:2000]
                guidance += "\n" + "\n".join(f"{i+1}. {s[:1500]}" for i, s in enumerate(steps))
                self.store.update_task(P.id, t["task_id"], data={**current["data"],
                    "findings": [*(current["data"].get("findings") or []), guidance]})
                return True
            return False
        except Exception as e:
            self._record(P, t, {**record, "status": "unavailable", "extra_attempts": 0,
                               "reason": f"{type(e).__name__}: {e}"[:500]})
            return False
        finally:
            self.store.update_session(sid, status="ended", outcome="guidance", ended_ts=self.c.clock())
            try:
                P.foundry.end(sid, "guidance_complete")
            except FoundryError:
                pass
