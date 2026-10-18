"""Optional-helper admission on the real account quota (docs/HELPER_POLICY.md).

A call is gated here only when the session it belongs to was bound by the
coordinator with an OPTIONAL purpose (orchestration.OPTIONAL_PURPOSES), or its
purpose can't be trusted ("unverified"), or it is required work beyond the
per-run allowance. Required work and unbound/manual traffic keep the existing
guardrails (tier conservation, the CLIProxyAPI usage limit, call budgets):
nothing here lowers or replaces them.

For the account that would serve the call, optional work is admitted only
while EVERY applicable window (the subscription session window, the weekly
window and any model-scoped bucket covering the model) is below
helper_policy.cutoff_pct USED, and each reading was OBSERVED upstream recently
(max_observation_age_seconds). Fetch time is not observation time: CLIProxyAPI
re-serves the last headers it saw, possibly hours old. Missing, stale,
malformed, rolled-over-since-observation or unreachable readings are UNKNOWN,
which is never admitted. After a cutoff defer, admission resumes only below
cutoff - hysteresis_pct and after cooldown_seconds.

Accounts:
  Meridian          one Claude account per backend (or per meridian_profile)
  CLIProxyAPI       can't say which signed-in account will serve a request,
                    so EVERY enabled account of the model's provider must pass
  other subscription no quota source: UNKNOWN
  metered paid API  never for optional work
  local             no subscription quota; slot limits still apply
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

ADMIT, DEFER, UNKNOWN, BLOCKED = "admit", "defer", "unknown", "blocked"


def _iso_ts(v) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def redact_account(label: str) -> str:
    """An account label without its identity (e-mail, user name)."""
    s = str(label or "")
    if "@" in s:
        user, _, dom = s.partition("@")
        return (user[:2] + "***@" + dom.split(".")[0][:1] + "***") if user else "***"
    return (s[:3] + "***") if len(s) > 4 else (s or "account")


class QuotaGate:
    def __init__(self, svc, clock=time.time):
        self.svc = svc
        self.clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._alerted: set[str] = set()
        # last successful Meridian reading per backend: a transient usage-endpoint
        # failure (its own HTTP 429/5xx/timeout) falls back to it, and the normal
        # freshness rule judges its observation age (never admitted on old data)
        self._last_good: dict[str, dict] = {}
        self._src_fail_since: dict[str, float] = {}     # key -> first failure of this streak
        self._src_ok_since: dict[str, float] = {}       # key -> first good reading of streak
        self._refresh_ts: dict[str, float] = {}
        self._bg: set = set()                  # background refresh probes in flight
        db = svc.db
        db.execute("CREATE TABLE IF NOT EXISTS helper_quota_state (account TEXT PRIMARY KEY, "
                   "state TEXT, since REAL, reason TEXT, next_eligible REAL, updated REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS helper_admissions (id INTEGER PRIMARY KEY "
                   "AUTOINCREMENT, ts REAL, session_id TEXT, run_id TEXT, project_id TEXT, "
                   "task_id TEXT, purpose TEXT, backend TEXT, model TEXT, account TEXT, "
                   "decision TEXT, reason TEXT, next_eligible REAL, detail TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_helper_adm_ts ON helper_admissions(ts)")

    @property
    def pol(self):
        return self.svc.config_store.config.helper_policy

    # ------------------------------------------------------------------ windows

    async def _meridian_windows(self, cfg, model: str, now: float) -> dict:
        mu = self.svc.meridian_usage
        snap = await mu.snapshot(cfg.url, cfg.api_key)
        raw = mu.last_raw(cfg.url) or {}
        fetched = _iso_ts(raw.get("at"))
        companion = "companion" in str(raw.get("source") or "")
        trusted = cfg.name in (self.pol.trust_fetch_time_for or [])
        out = {"source": raw.get("source") or "Meridian quota endpoint",
               "fetched_at": fetched, "error": snap.get("fetch_error") or "",
               "accounts": []}
        if snap.get("fetch_error"):
            err = str(snap.get("fetch_error"))
            last = self._last_good.get(cfg.name)
            transient = not any(x in err for x in ("401", "403", "invalid", "unauthor"))
            if transient and last and all(w.get("observed_at") for a in last["accounts"]
                                         for w in a["windows"]):
                # same windows, original observation times: freshness decides
                return {**last, "fallback_error": err,
                        "source": f"{last.get('source')} (last good reading; usage endpoint: "
                                  f"{err[:120]})"}
            return out
        if snap.get("oauth_ok") is False:
            out["error"] = "Meridian reports no Anthropic usage figures (sources.oauth null)"
            return out
        if companion:
            observed, how = fetched, "live (auth companion /api/oauth/usage)"
        elif trusted and snap.get("oauth_ok"):
            observed, how = fetched, "assumed = fetch time (helper_policy.trust_fetch_time_for)"
        else:
            observed, how = None, ("observation time not reported by Meridian's quota endpoint "
                                   "(use the auth companion usage source, or list this backend "
                                   "in helper_policy.trust_fetch_time_for)")
        prof = getattr(cfg, "meridian_profile", None)
        windows = []
        m = model.lower()
        for b in snap.get("buckets") or []:
            t = str(b.get("type") or "")
            kind = "session" if t == "five_hour" else "weekly" if t == "seven_day" else "scoped"
            applies = True
            if kind == "scoped":
                slug = t.replace("seven_day_", "")
                applies = bool(slug) and slug.split("_")[0] in m
                if b.get("fable_scoped"):
                    applies = "fable" in m or "mythos" in m
            used = b.get("used")
            windows.append({"name": b.get("label") or t, "kind": kind, "applies": applies,
                            "used": None if used is None else round(float(used) * 100, 1),
                            "observed_at": observed, "observation": how,
                            "resets_in_s": int(_iso_ts(b.get("resets_at")) - now)
                            if _iso_ts(b.get("resets_at")) else None,
                            "rolled_over": bool(b.get("rolled_over")),
                            "contradicted": bool(b.get("contradicted"))})
        out["accounts"].append({"account": f"{cfg.name}:claude" + (f":{prof}" if prof else ""),
                                "label": "Claude" + (f" ({prof})" if prof else ""),
                                "windows": windows, "unavailable": False})
        self._last_good[cfg.name] = out
        return out

    async def _cliproxy_windows(self, cfg, info: dict, model: str, now: float) -> dict:
        from .cliproxy_usage import _applies, provider_of
        cu = self.svc.cliproxy_usage
        provider = provider_of(model)
        out = {"source": "CLIProxyAPI /v0/management/auth-files (each account's last reported "
                         "usage headers)", "fetched_at": None, "error": "", "accounts": []}
        if not cu.configured(info):
            out["error"] = "no management key: CLIProxyAPI usage can't be read"
            return out
        snap = await cu.snapshot(info)
        hit = cu._cache.get(cu._mgmt_root(info))
        out["fetched_at"] = now - (time.monotonic() - hit[0]) if hit else now
        if not snap.get("ok"):
            out["error"] = snap.get("error") or "unreadable"
            return out
        accts = [a for a in snap["accounts"] if a["provider"] == provider and not a["disabled"]]
        for i, a in enumerate(accts, 1):
            observed = (now - a["age_s"]) if a.get("age_s") is not None else None
            windows = []
            for w in a["windows"]:
                windows.append({"name": w["name"], "kind": w.get("kind") or "other",
                                "applies": _applies(w, model), "used": w.get("used"),
                                "observed_at": observed,
                                "observation": "upstream headers at the account's last request",
                                "resets_in_s": w.get("resets_in_s"),
                                "rolled_over": bool(w.get("rolled_over"))})
            out["accounts"].append({"account": f"{cfg.name}:{provider}#{i}",
                                    "label": f"{provider} #{i} {redact_account(a.get('label'))}"
                                             + (f" {a['plan']}" if a.get("plan") else ""),
                                    "windows": windows, "unavailable": bool(a["unavailable"])})
        if not accts:
            out["error"] = f"no enabled {provider or 'matching'} account reported by CLIProxyAPI"
        return out

    # ------------------------------------------------------------------ evaluation

    def _state(self, account: str) -> Optional[dict]:
        return self.svc.db.query_one("SELECT * FROM helper_quota_state WHERE account=?", (account,))

    def _set_state(self, account: str, state: str, reason: str, next_eligible) -> None:
        cur = self._state(account)
        if cur and cur["state"] == state and cur["reason"] == reason:
            return
        now = self.clock()
        since = cur["since"] if cur and cur["state"] == state else now
        self.svc.db.execute("INSERT INTO helper_quota_state (account, state, since, reason, "
                            "next_eligible, updated) VALUES (?,?,?,?,?,?) ON CONFLICT(account) DO "
                            "UPDATE SET state=excluded.state, since=excluded.since, "
                            "reason=excluded.reason, next_eligible=excluded.next_eligible, "
                            "updated=excluded.updated", (account, state, since, reason[:500],
                                                         next_eligible, now))

    def _judge(self, acct: dict, now: float, state_key: str) -> dict:
        pol = self.pol
        ws = [w for w in acct["windows"] if w["applies"]]
        # per account AND model: a model-scoped bucket's cutoff mustn't hold
        # back the account's other models
        prior = self._state(state_key)
        deferred = bool(prior and prior["state"] == DEFER)
        threshold = pol.cutoff_pct - (pol.hysteresis_pct if deferred else 0.0)
        cooldown_end = (prior["since"] + pol.cooldown_seconds) if deferred else None
        problems = []
        refreshable = True        # every problem is one a fresh request would clear
        if acct.get("unavailable"):
            return {"state": DEFER, "reason": "account marked unavailable (rate-limited or "
                                              "signed out)", "next_eligible": None}
        for kind in ("session", "weekly"):
            if not any(w["kind"] == kind for w in ws):
                problems.append(f"no {kind}-window reading")
                refreshable = False
        for w in ws:
            label = f"{w['name']} ({w['kind']})"
            if w.get("used") is None:
                problems.append(f"{label}: no utilization figure")
                refreshable = False
            elif w.get("observed_at") is None:
                problems.append(f"{label}: {w.get('observation') or 'observation time unknown'}")
                # headers from "the account's last request": a request refreshes them;
                # Meridian's quota endpoint without an observation time does not
                refreshable = refreshable and "last request" in str(w.get("observation") or "")
            elif now - w["observed_at"] > pol.max_observation_age_seconds:
                problems.append(f"{label}: observed {int((now - w['observed_at']) // 60)} min ago "
                                f"(limit {pol.max_observation_age_seconds // 60} min)")
                # a stale reading already at/over the cutoff stays over until its
                # window resets: refreshing it can't make the helper admissible
                if w["used"] >= threshold and not w.get("rolled_over"):
                    refreshable = False
            elif w.get("rolled_over"):
                problems.append(f"{label}: window reset after the last observation; usage "
                                f"since the reset is unknown")
            elif w.get("contradicted"):
                problems.append(f"{label}: reading contradicted by a live call")
                refreshable = False
        if problems:
            return {"state": UNKNOWN, "reason": "; ".join(problems[:4]), "next_eligible": None,
                    "refreshable": refreshable}
        over = [w for w in ws if w["used"] >= threshold]
        if over:
            resets = [w["resets_in_s"] for w in over if w.get("resets_in_s") is not None]
            nxt = now + max(resets) if resets else None
            if cooldown_end:
                nxt = max(nxt or 0, cooldown_end)
            return {"state": DEFER, "next_eligible": nxt,
                    "reason": ", ".join(f"{w['name']} {w['used']:.1f}% used" for w in over)
                              + (f" (at or above {pol.cutoff_pct:g}%)" if not deferred else
                                 f" (resumes below {threshold:g}% after the cutoff)")}
        if cooldown_end and now < cooldown_end:
            return {"state": DEFER, "next_eligible": cooldown_end,
                    "reason": f"cooldown after the last cutoff ({int(cooldown_end - now)} s left)"}
        return {"state": ADMIT, "next_eligible": None,
                "reason": ", ".join(f"{w['name']} {w['used']:.1f}%" for w in ws) + " (below "
                          f"{pol.cutoff_pct:g}%)"}

    async def evaluate(self, backend: str, model: str) -> dict:
        """Admission for optional work on this backend + model right now."""
        now = self.clock()
        key = f"{backend}|{model}"
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.pol.decision_cache_seconds:
            return hit[1]
        backends = getattr(self.svc.pool, "backends", None) or {}
        s = backends.get(backend)
        out = {"backend": backend, "model": model, "ts": now, "cutoff_pct": self.pol.cutoff_pct,
               "accounts": [], "state": UNKNOWN, "reason": "", "next_eligible": None,
               "source": "", "fetched_at": None}
        if s is None:
            out["reason"] = "unknown backend"
            return out
        cfg = s.config
        from .budget import is_paid
        info = self.svc.pool.backend_info(model) or {}
        if info.get("name") != backend:
            info = {"name": cfg.name, "flavor": cfg.effective_flavor, "url": cfg.url,
                    "management_key": cfg.management_key, "management_url": cfg.management_url,
                    "usage_limit_pct": cfg.usage_limit_pct}
        try:
            if cfg.type == "anthropic-compatible":
                src = await self._meridian_windows(cfg, model, now)
            elif cfg.effective_flavor == "cliproxyapi":
                src = await self._cliproxy_windows(cfg, info, model, now)
            elif cfg.is_subscription:
                src = {"error": "subscription backend without a quota source", "accounts": []}
            elif is_paid(cfg):
                out.update(state=BLOCKED, reason="metered paid API: optional work never uses it")
                self._cache[key] = (now, out)
                return out
            else:
                out.update(state=ADMIT, reason="local backend: no subscription quota (slot "
                                               "limits still apply)", source="local")
                self._cache[key] = (now, out)
                return out
        except Exception as e:                                   # noqa: BLE001
            src = {"error": f"quota source failed: {type(e).__name__}: {e}"[:300], "accounts": []}
        out["source"] = src.get("source") or ""
        out["fetched_at"] = src.get("fetched_at")
        if src.get("error") or not src.get("accounts"):
            out.update(state=UNKNOWN, reason=src.get("error") or "no account readings")
            self._source_alert(backend, out["reason"])
        else:
            # a fallback reading still counts as a failing source for the
            # (debounced) operator alert; admission is judged on its age below
            self._source_alert(backend, src.get("fallback_error"))
            states = []
            for a in src["accounts"]:
                skey = f"{a['account']}|{model}"
                j = self._judge(a, now, skey)
                self._set_state(skey, j["state"], j["reason"], j["next_eligible"])
                out["accounts"].append({**{k: a[k] for k in ("account", "label")},
                                        "windows": [{**w, "observation_age_s":
                                                     None if w.get("observed_at") is None
                                                     else int(now - w["observed_at"])}
                                                    for w in a["windows"]], **j})
                states.append(j)
            # every account that might serve must pass (CLIProxyAPI can't say which)
            worst = next((j for j in states if j["state"] == UNKNOWN), None) or \
                next((j for j in states if j["state"] == DEFER), None)
            # stale-only: unknown solely because idle readings aged out, nothing deferred
            out["stale_only"] = bool(worst and worst["state"] == UNKNOWN and not any(
                j["state"] == DEFER for j in states) and all(
                j.get("refreshable") for j in states if j["state"] == UNKNOWN))
            if worst is None:
                out.update(state=ADMIT, reason="; ".join(j["reason"] for j in states))
            else:
                nx = [j["next_eligible"] for j in states if j["next_eligible"]]
                out.update(state=worst["state"], reason=worst["reason"],
                           next_eligible=max(nx) if nx else None)
        self._cache[key] = (now, out)
        return out

    async def evaluate_fresh(self, backend: str, model: str, wait: bool = True) -> dict:
        """evaluate(), and if the only problem is a reading that went stale while
        the backend was idle, refresh it with one tiny probe and decide again.
        Bounded: never while over the cutoff, never on a broken source or a
        busy backend, at most once per refresh_probe_min_interval_seconds per
        backend (a CLIProxyAPI pool may need one probe per stale account, cap 4).
        wait=False (helper admission): the probe runs in the background and this
        returns UNKNOWN at once with refresh.started, so a slow probe never holds
        the caller past its own timeout; the next admission poll sees the result."""
        d = await self.evaluate(backend, model)
        pol = self.pol
        if d.get("state") != UNKNOWN or not d.get("stale_only") or not getattr(
                pol, "refresh_probe", False):
            return d
        now = self.clock()
        last = self._refresh_ts.get(backend, 0.0)
        if now - last < max(60, pol.refresh_probe_min_interval_seconds):
            return {**d, "refresh": {"skipped": f"refresh probe already tried "
                                                f"{int(now - last)} s ago"}}
        s = (getattr(self.svc.pool, "backends", None) or {}).get(backend)
        health = getattr(self.svc, "health", None)
        if s is None or health is None or getattr(s, "busy", 0):
            return {**d, "refresh": {"skipped": "backend busy or unknown"}}
        # only when its state is actually unknown: a backend a real call or an
        # earlier probe verified within health.fresh_minutes ("serving") is not
        # probed again; a stale reading there is a usage-source problem a probe
        # wouldn't fix
        try:
            hs = next((r for r in health.status() if r.get("backend") == backend), None)
        except Exception:                                         # noqa: BLE001
            hs = None
        if hs and hs.get("status") == "serving":
            return {**d, "refresh": {"skipped": "backend verified serving recently "
                                                "(real call or probe); no probe needed"}}
        self._refresh_ts[backend] = now
        if not wait:
            import asyncio
            t = asyncio.get_running_loop().create_task(self._refresh(backend, model, d, now))
            self._bg.add(t)
            t.add_done_callback(self._bg.discard)
            return {**d, "reason": f"{d.get('reason')}; refresh probe started, "
                                   "ask again shortly",
                    "refresh": {"started": True, "at": now}}
        return await self._refresh(backend, model, d, now)

    async def _refresh(self, backend: str, model: str, d: dict, now: float) -> dict:
        health = self.svc.health
        stale = sum(1 for a in d.get("accounts") or [] if a.get("state") == UNKNOWN) or 1
        probes = []
        for _ in range(min(4, stale)):
            try:
                p = await health.probe(backend, model)
            except Exception as e:                                # noqa: BLE001
                p = {"ok": False, "detail": str(e)[:200]}
            probes.append({k: p.get(k) for k in ("ok", "ms", "detail", "model")})
            for k in [k for k in self._cache if k.startswith(backend + "|")]:
                self._cache.pop(k, None)
            self._drop_source_cache(backend)
            d = await self.evaluate(backend, model)
            if not p.get("ok") or d.get("state") != UNKNOWN or not d.get("stale_only"):
                break
        self._log_refresh(backend, model, probes, d)
        return {**d, "refresh": {"probes": probes, "at": now}}

    def _drop_source_cache(self, backend: str) -> None:
        """Make the next evaluate() read the usage source again."""
        s = (getattr(self.svc.pool, "backends", None) or {}).get(backend)
        if s is None:
            return
        cfg = s.config
        try:
            if cfg.effective_flavor == "cliproxyapi":
                cu = self.svc.cliproxy_usage
                cu._cache.pop(cu._mgmt_root({"management_url": cfg.management_url,
                                             "url": cfg.url}), None)
            elif cfg.type == "anthropic-compatible":
                self.svc.meridian_usage.clear_cache()
        except Exception:                                         # noqa: BLE001
            pass

    def _log_refresh(self, backend: str, model: str, probes: list, d: dict) -> None:
        try:
            self.svc.db.execute(
                "INSERT INTO helper_admissions (ts, session_id, run_id, project_id, task_id, "
                "purpose, backend, model, account, decision, reason, next_eligible, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.clock(), "", "", "", "", "refresh_probe", backend, model, "",
                 d.get("state"), f"after {len(probes)} refresh probe(s): {d.get('reason')}"[:500],
                 d.get("next_eligible"), json.dumps(probes)[:2000]))
            self.svc.db.log_event("info", "helpers", f"{backend}: stale quota reading refreshed by "
                                                     f"probe -> {str(d.get('state')).upper()}")
        except Exception:                                         # noqa: BLE001
            pass

    def _source_alert(self, backend: str, problem: Optional[str]) -> None:
        cc = getattr(self.svc, "control", None)
        if cc is None:
            return
        key = f"quota_source:{backend}"
        now = self.clock()
        after = float(getattr(self.pol, "source_alert_after_seconds", 0) or 0)
        settle = float(getattr(self.pol, "source_resolve_after_seconds", 0) or 0)
        if problem:
            self._src_ok_since.pop(key, None)
            first = self._src_fail_since.setdefault(key, now)
            if now - first < after:
                return                      # a blip (e.g. the usage endpoint's 429): wait
        else:
            self._src_fail_since.pop(key, None)
            first = self._src_ok_since.setdefault(key, now)
            if key in self._alerted and now - first < settle:
                return                      # one good reading after an outage: wait
        try:
            if problem:
                if key not in self._alerted:
                    cc.raise_alert(key, "quota_source", "warning",
                                   f"Quota reading for {backend} unavailable: optional helpers "
                                   f"paused there", problem + ". Required work is unaffected. "
                                   "Fix the usage source (see Helpers in the control center).", {})
                    self._alerted.add(key)
            elif key in self._alerted:
                cc._resolve(key, "quota readings available again")
                self._alerted.discard(key)
        except Exception:                                        # noqa: BLE001
            pass

    # ------------------------------------------------------------------ ledger hook

    def effective_purpose(self, ctx) -> str:
        """The purpose to enforce: "" (not gated), "required" or "optional"."""
        from .orchestration import OPTIONAL_PURPOSES, REQUIRED_PURPOSES
        p = ctx.purpose or ""
        if not p:
            return ""
        if p in OPTIONAL_PURPOSES or p == "unverified":
            return "optional"
        if p in REQUIRED_PURPOSES and p in ("planning", "blocker_diagnosis") and ctx.run_id:
            n = self.svc.db.query_one(
                "SELECT COUNT(*) AS n FROM work_sessions WHERE run_id=? AND purpose=? AND "
                "created_ts <= (SELECT created_ts FROM work_sessions WHERE session_id=?)",
                (ctx.run_id, p, ctx.session_id))
            if n and n["n"] > self.pol.required_helper_sessions_per_run:
                return "optional"
        return "required"

    async def admit_call(self, ctx) -> Optional[dict]:
        """None to proceed; else a refusal {message, rule, code, reset_s}.
        Called before the ledger reserves a call and again after the backend
        queue lets it start."""
        pin = self._pinned_route(ctx)
        if pin and not _same_route(pin, ctx.backend, ctx.model):
            # belt and braces: routing already serves a pinned session only on
            # its pinned route; a call anywhere else is refused, never served
            d = {"state": BLOCKED, "next_eligible": None, "accounts": [],
                 "reason": f"session pinned to {pin['backend']}/{pin['model']} by helper "
                           f"admission; {ctx.backend}/{ctx.model} would be a re-route"}
            self._log(ctx, d)
            return {"message": f"optional helper work ({ctx.purpose or 'pinned session'}) not "
                               f"admitted on {ctx.backend}: BLOCKED — {d['reason']}",
                    "rule": "helper-policy", "code": "optional_deferred", "reset_s": None}
        eff = self.effective_purpose(ctx)
        if eff != "optional":
            return None
        if not self.pol.optional_enabled:
            d = {"state": BLOCKED, "reason": "optional coding helpers are turned off "
                                             "(helper_policy.optional_enabled)",
                 "next_eligible": None, "accounts": []}
        else:
            d = await self.evaluate(ctx.backend, ctx.model)
        self._log(ctx, d)
        if d["state"] == ADMIT:
            return None
        reset = (d["next_eligible"] - self.clock()) if d.get("next_eligible") else None
        return {"message": f"optional helper work ({ctx.purpose}) not admitted on "
                           f"{ctx.backend}: {d['state'].upper()} — {d['reason']}",
                "rule": "helper-policy", "code": "optional_deferred",
                "reset_s": reset if reset and reset > 0 else None}

    @staticmethod
    def _pinned_route(ctx) -> Optional[dict]:
        if not getattr(ctx, "session_id", ""):
            return None
        from .orchestration import get_registry
        reg = get_registry()
        try:
            return reg.pinned_route(ctx.session_id) if reg is not None else None
        except Exception:                                        # noqa: BLE001
            return None

    def _log(self, ctx, d: dict) -> None:
        last = self.svc.db.query_one("SELECT decision, reason FROM helper_admissions WHERE "
                                     "session_id=? ORDER BY id DESC LIMIT 1", (ctx.session_id,))
        if last and last["decision"] == d["state"] and last["reason"] == d["reason"]:
            return
        accts = ", ".join(a["account"] for a in d.get("accounts") or [])
        self.svc.db.execute(
            "INSERT INTO helper_admissions (ts, session_id, run_id, project_id, task_id, purpose, "
            "backend, model, account, decision, reason, next_eligible, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), ctx.session_id, ctx.run_id, ctx.project_id, ctx.work_task_id,
             ctx.purpose, ctx.backend, ctx.model, accts, d["state"], d["reason"][:500],
             d.get("next_eligible"), ""))

    # ------------------------------------------------------------------ reporting

    def warnings(self) -> list[str]:
        """Where the essential (required-work) policy leaves little reserve."""
        cfg = self.svc.config_store.config
        pol = cfg.helper_policy
        out = []
        for name, s in (getattr(self.svc.pool, "backends", None) or {}).items():
            c = s.config
            if not getattr(c, "enabled", True):
                continue
            if c.effective_flavor == "cliproxyapi":
                lim = float(c.usage_limit_pct or cfg.subscription_usage.limit_pct)
                if lim <= pol.cutoff_pct:
                    out.append(f"{name}: required work stops at {lim:g}% used, at or below the "
                               f"optional cutoff ({pol.cutoff_pct:g}%) — no reserve for required "
                               f"work beyond optional helpers")
                elif lim - pol.cutoff_pct < 10:
                    out.append(f"{name}: only {lim - pol.cutoff_pct:g} points between the optional "
                               f"cutoff and the required-work limit ({lim:g}%)")
            if c.type == "anthropic-compatible":
                m = cfg.meridian
                stop = (1.0 - m.min_window_fraction) * 100
                if m.conserve_premium_at >= 1.0 and m.conserve_strong_at >= 1.0:
                    out.append(f"{name}: tier conservation is off (premium/strong at 1.0); required "
                               f"work runs until the window is {stop:g}% used "
                               f"(meridian.min_window_fraction)")
                if m.usage_credits != "never":
                    out.append(f"{name}: meridian.usage_credits is {m.usage_credits!r}: purchased "
                               f"credits can be spent, which this policy forbids")
        if not pol.backend_preference:
            out.append("helper_policy.backend_preference is empty: coordinator helpers have no "
                       "preferred backend")
        return out

    def status(self, limit: int = 100) -> dict:
        pol = self.pol
        st = self.svc.db.query("SELECT * FROM helper_quota_state ORDER BY account")
        adm = self.svc.db.query("SELECT * FROM helper_admissions ORDER BY id DESC LIMIT ?",
                                (limit,))
        return {"policy": pol.model_dump(), "accounts": st, "admissions": adm,
                "warnings": self.warnings(), "decisions": [v for _, v in self._cache.values()]}


def _same_route(pin: dict, backend: str, model: str) -> bool:
    return pin.get("backend") == backend and pin.get("model") == model


def persona_routes(svc, persona: dict) -> list[tuple[str, str]]:
    """(backend, model) for each reachable route of a strict persona, in order."""
    from .facade.ollama_api import strict_candidates
    out = []
    pinned = (persona.get("backend") or "").strip()
    for m in strict_candidates(svc, persona):
        if pinned:
            out.append((pinned, m))
            continue
        for s in svc.pool._candidates(m) if hasattr(svc.pool, "_candidates") else []:
            out.append((s.config.name, m))
            break
    return out

