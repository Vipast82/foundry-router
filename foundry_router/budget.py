"""Budget ledger: shared limits on model calls, enforced before each call.

Every model call through the backend pool, from every client (Kilo, Cline,
OpenWebUI, scripts) and every path (direct, agent rounds, retries, reviews,
judges), is one ledger row:

  reserve  before the call: the matching rules are checked and the row is
           inserted in one synchronous step (no await in between), so parallel
           helpers can't both take the last slot
  settle   after the call: actual input/output tokens; an error still counts
           (the backend may have done the work); a connect failure is released

Rules (config `budgets.rules`, see config.BudgetRule) limit calls, tokens,
calls in flight, and distinct helpers active at once, per client / persona /
session / task / request / backend / account or shared. Pause switches stop
an account, backend, persona or client immediately. Calls from a session a
coordinator registered (orchestration.py) also carry project / work_task /
run / role, so rules can be per project, per sprint, per sprint attempt (run)
or per role, on top of the shared account and backend limits.

A denial is a BudgetDenied (an AllBackendsFailed, so the request takes the
same path as any other denial: a strict persona refuses with budget_denied, a
flexible persona follows its own configured fallback). Denials are recorded
for the monitor with the rule and the reset time.

Guardrails also call `check()` (no reservation) while choosing a model, so a
denial shows up in route notes before anything is sent.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from .pool.base import AllBackendsFailed

log = logging.getLogger(__name__)

COUNTED = ("reserved", "settled", "error")
PAUSE_SCOPES = ("account", "backend", "persona", "client")


class BudgetDenied(AllBackendsFailed):
    def __init__(self, message: str, rule: str, code: str = "budget_denied",
                 reset_s: Optional[float] = None):
        super().__init__(message)
        self.message, self.rule, self.code, self.reset_s = message, rule, code, reset_s


@dataclass
class CallCtx:
    model: str = ""
    backend: str = ""
    account: str = ""
    paid: bool = False
    persona: str = ""
    client_id: str = "anonymous"
    session_id: str = ""
    task_id: str = ""
    parent_task_id: str = ""
    request_id: str = ""
    # From a coordinator's work binding (orchestration.py), when the session has one.
    project_id: str = ""
    work_task_id: str = ""
    run_id: str = ""
    role: str = ""
    est_tokens: int = 0
    extra: dict = field(default_factory=dict)

    def key(self, dim: str) -> str:
        """Group value for a `per` / `active_by` dimension. A helper run is its
        task id, else its session, else client+persona (when the client sends
        no correlation headers)."""
        if dim == "task":
            return self.task_id or self.session_id or f"{self.client_id}/{self.persona}"
        if dim == "session":
            return self.session_id or f"{self.client_id}/{self.persona}"
        if dim == "run":
            # A coordinator run (one sprint attempt) — else the helper key.
            return self.run_id or self.key("task")
        return str(getattr(self, _DIM_ATTR.get(dim, dim), "") or "")


_DIM_ATTR = {"client": "client_id", "request": "request_id", "project": "project_id",
             "work_task": "work_task_id"}


def _row_ctx(r: dict) -> CallCtx:
    return CallCtx(model=r.get("model") or "", backend=r.get("backend") or "",
                   account=r.get("account") or "", paid=bool(r.get("paid")),
                   persona=r.get("persona") or "", client_id=r.get("client_id") or "anonymous",
                   session_id=r.get("session_id") or "", task_id=r.get("task_id") or "",
                   parent_task_id=r.get("parent_task_id") or "",
                   request_id=r.get("request_id") or "",
                   project_id=r.get("project_id") or "", work_task_id=r.get("work_task_id") or "",
                   run_id=r.get("run_id") or "", role=r.get("role") or "")


def _glob(value: str, pattern) -> bool:
    pats = pattern if isinstance(pattern, list) else [pattern]
    return any(fnmatch.fnmatchcase(value or "", str(p)) for p in pats)


_MATCH_ATTR = {"backend": "backend", "persona": "persona", "client": "client_id",
               "account": "account", "model": "model", "session": "session_id",
               "task": "task_id", "project": "project_id", "work_task": "work_task_id",
               "run": "run_id", "role": "role"}


def matches(match: dict, ctx: CallCtx) -> bool:
    for k, v in (match or {}).items():
        if k == "paid":
            if bool(v) != bool(ctx.paid):
                return False
        elif k in _MATCH_ATTR:
            if not _glob(getattr(ctx, _MATCH_ATTR[k]), v):
                return False
    return True


def account_of(backend_cfg, model: str) -> str:
    """'<backend>:<provider>' for subscription backends (which provider
    account a call draws from), the backend name otherwise."""
    from .cliproxy_usage import provider_of
    if getattr(backend_cfg, "type", "") == "anthropic-compatible":
        prof = getattr(backend_cfg, "meridian_profile", None)
        return f"{backend_cfg.name}:claude" + (f":{prof}" if prof else "")
    if getattr(backend_cfg, "is_subscription", False):
        return f"{backend_cfg.name}:{provider_of(model) or 'other'}"
    return backend_cfg.name


def is_paid(backend_cfg) -> bool:
    from .facade.ollama_api import _LOCAL_OPENAI_FLAVORS
    t = getattr(backend_cfg, "type", "")
    if t == "anthropic-compatible" or getattr(backend_cfg, "is_subscription", False):
        return True
    return t == "openai-compatible" and getattr(backend_cfg, "effective_flavor", None) \
        not in _LOCAL_OPENAI_FLAVORS


def context_for(model: str, backend_cfg, est_tokens: int = 0) -> CallCtx:
    """The call's identity from the request context (client key, correlation
    headers, persona from the route record)."""
    from . import client_auth, request_context
    c = client_auth.current()
    r = request_context.route()
    return CallCtx(model=model, backend=backend_cfg.name, account=account_of(backend_cfg, model),
                   paid=is_paid(backend_cfg), persona=r.get("persona") or "",
                   client_id=c.get("client_id") or "anonymous",
                   session_id=c.get("session_id") or "", task_id=c.get("task_id") or "",
                   parent_task_id=c.get("parent_task_id") or "",
                   request_id=request_context.request_id() or "", est_tokens=est_tokens,
                   project_id=c.get("project_id") or "", work_task_id=c.get("work_task_id") or "",
                   run_id=c.get("run_id") or "", role=c.get("role") or "")


def context_from_info(model: str, info: dict, est_tokens: int = 0) -> CallCtx:
    """context_for() from a pool backend_info dict (what guardrails get)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(name=info.get("name") or "", type=info.get("type") or "",
                          is_subscription=bool(info.get("subscription")),
                          effective_flavor=info.get("flavor"),
                          meridian_profile=info.get("meridian_profile"))
    return context_for(model, cfg, est_tokens)


def estimate_tokens(messages) -> int:
    try:
        return len(json.dumps(messages, default=str)) // 4
    except (TypeError, ValueError):
        return 0


def _fmt_wait(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


class Ledger:
    def __init__(self, get_config, db):
        self.get_config = get_config
        self.db = db
        self._active: dict[int, CallCtx] = {}    # event id -> in-flight call
        self._recorded: dict = {}
        # finish anything a previous process left reserved
        db.execute("UPDATE usage_events SET status='error', detail='Foundry restarted during "
                   "the call', settled_ts=? WHERE status='reserved'", (time.time(),))

    @property
    def cfg(self):
        return self.get_config()

    # -- pauses -------------------------------------------------------------------

    def pauses(self) -> list[dict]:
        return self.db.query("SELECT * FROM budget_pauses ORDER BY scope, value")

    def set_pause(self, scope: str, value: str, paused: bool, note: str = "") -> None:
        if scope not in PAUSE_SCOPES or not value:
            raise ValueError(f"scope must be one of {PAUSE_SCOPES}")
        from .db import utcnow
        if paused:
            self.db.execute("INSERT OR REPLACE INTO budget_pauses (scope, value, paused_at, note) "
                            "VALUES (?,?,?,?)", (scope, value, utcnow(), note))
        else:
            self.db.execute("DELETE FROM budget_pauses WHERE scope=? AND value=?", (scope, value))

    def _paused(self, ctx: CallCtx) -> Optional[BudgetDenied]:
        vals = {"account": ctx.account, "backend": ctx.backend, "persona": ctx.persona,
                "client": ctx.client_id}
        for p in self.pauses():
            v = vals.get(p["scope"])
            if v and (v == p["value"] or (p["scope"] == "account" and v.startswith(p["value"] + ":"))):
                return BudgetDenied(f"{p['scope']} {p['value']!r} is paused in Foundry"
                                    + (f" ({p['note']})" if p.get("note") else "")
                                    + ". Not retried elsewhere; resume it on the Usage tab.",
                                    rule="paused", code="paused")
        return None

    # -- checks -------------------------------------------------------------------

    def _rows(self, since: float) -> list[dict]:
        return self.db.query(
            "SELECT * FROM usage_events WHERE ts >= ? AND status IN ('reserved','settled','error')",
            (since,))

    def check(self, ctx: CallCtx) -> Optional[BudgetDenied]:
        """The denial this call would get right now, or None. Observe-mode
        rules never deny (they're recorded as 'observed')."""
        cfg = self.cfg
        if not cfg or not cfg.enabled:
            return None
        paused = self._paused(ctx)
        if paused:
            return paused
        now = time.time()
        cache: dict = {}

        def rows_since(since):
            key = round(since, 0)
            if key not in cache:
                cache[key] = [(r, _row_ctx(r)) for r in self._rows(since)]
            return cache[key]

        for rule in cfg.rules:
            if not rule.enabled or not matches(rule.match, ctx):
                continue
            denial = self._check_rule(rule, ctx, now, rows_since)
            if denial is None:
                continue
            if rule.mode == "observe":
                self.record(ctx, "observed", rule.name, denial.message)
                continue
            return denial
        return None

    def _check_rule(self, rule, ctx: CallCtx, now: float, rows_since) -> Optional[BudgetDenied]:
        group = ctx.key(rule.per) if rule.per else None

        def same_group(c: CallCtx) -> bool:
            return group is None or c.key(rule.per) == group

        where = f" for {rule.per} {group}" if group else ""
        window = max(0, rule.window_minutes) * 60
        if rule.max_calls or rule.max_tokens:
            rows = [(r, c) for r, c in rows_since(now - window if window else 0)
                    if matches(rule.match, c) and same_group(c)]
            reset = (min(r["ts"] for r, _ in rows) + window - now) if rows and window else None
            span = f"in {rule.window_minutes} min" if window else "in total"
            if rule.max_calls and len(rows) + 1 > rule.max_calls:
                return BudgetDenied(
                    f"budget rule {rule.name!r}: {len(rows)}/{rule.max_calls} calls {span}{where}"
                    + (f"; next slot in {_fmt_wait(reset)}" if reset else ""),
                    rule=rule.name, reset_s=reset)
            if rule.max_tokens:
                used = sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                           if r["status"] != "reserved" else (r["est_tokens"] or 0) for r, _ in rows)
                if used + ctx.est_tokens > rule.max_tokens:
                    return BudgetDenied(
                        f"budget rule {rule.name!r}: {used:,} of {rule.max_tokens:,} tokens used "
                        f"{span}{where} (this request needs about {ctx.est_tokens:,})"
                        + (f"; resets in {_fmt_wait(reset)}" if reset else ""),
                        rule=rule.name, reset_s=reset)
        inflight = [c for c in self._active.values() if matches(rule.match, c) and same_group(c)]
        if rule.max_concurrent and len(inflight) >= rule.max_concurrent:
            busy = inflight[0]
            return BudgetDenied(
                f"budget rule {rule.name!r}: {len(inflight)} call(s) already in flight{where} "
                f"(max {rule.max_concurrent}; {busy.persona or busy.model} for "
                f"{busy.client_id}). Try again when it finishes.",
                rule=rule.name, reset_s=None)
        if rule.max_active:
            mine = ctx.key(rule.active_by)
            last: dict[str, float] = {}
            for r, c in rows_since(now - max(1, rule.idle_seconds)):
                if matches(rule.match, c) and same_group(c):
                    k = c.key(rule.active_by)
                    last[k] = max(last.get(k, 0), r.get("settled_ts") or r["ts"])
            for c in inflight:
                last[c.key(rule.active_by)] = now
            others = {k: t for k, t in last.items() if k != mine}
            if len(others) >= rule.max_active:
                k, t = max(others.items(), key=lambda kv: kv[1])
                wait = max(0, t + rule.idle_seconds - now)
                return BudgetDenied(
                    f"budget rule {rule.name!r}: another helper is active ({rule.active_by} "
                    f"{k}); at most {rule.max_active} at a time. Free in about {_fmt_wait(wait)} "
                    f"if it stays idle.", rule=rule.name, reset_s=wait)
        return None

    # -- reserve / settle ------------------------------------------------------------

    def reserve(self, ctx: CallCtx) -> int:
        """Check and insert in one synchronous step. Raises BudgetDenied."""
        denial = self.check(ctx)
        if denial is not None:
            self.record(ctx, "denied", denial.rule, denial.message)
            raise denial
        eid = self._insert(ctx, "reserved")
        self._active[eid] = ctx
        return eid

    async def reserve_wait(self, ctx: CallCtx) -> int:
        """reserve(), waiting up to the longest wait_seconds of the matching
        rules for a concurrency/active slot."""
        import asyncio
        cfg = self.cfg
        wait = max([r.wait_seconds for r in (cfg.rules if cfg and cfg.enabled else [])
                    if r.enabled and matches(r.match, ctx)] or [0])
        deadline = time.monotonic() + wait
        while True:
            denial = self.check(ctx)
            if denial is None:
                return self.reserve(ctx)
            slot = denial.rule != "paused" and (denial.reset_s is None or denial.reset_s <= wait)
            if not slot or time.monotonic() >= deadline:
                self.record(ctx, "denied", denial.rule, denial.message)
                raise denial
            await asyncio.sleep(0.5)

    def settle(self, eid: Optional[int], status: str, input_tokens: int = 0,
               output_tokens: int = 0, detail: str = "") -> None:
        if not eid:
            return
        self._active.pop(eid, None)
        self.db.execute("UPDATE usage_events SET status=?, input_tokens=?, output_tokens=?, "
                        "detail=?, settled_ts=? WHERE id=? AND status='reserved'",
                        (status, int(input_tokens or 0), int(output_tokens or 0),
                         (detail or "")[:300], time.time(), eid))

    def record(self, ctx: CallCtx, status: str, rule: str, detail: str) -> None:
        """Log a denial / observation once per request, model and rule (a
        request may check the same candidate more than once)."""
        key = (ctx.request_id, ctx.model, rule, status)
        if ctx.request_id and key in self._recorded:
            return
        self._recorded[key] = None
        if len(self._recorded) > 2000:
            for k in list(self._recorded)[:1000]:
                self._recorded.pop(k, None)
        self._insert(ctx, status, rule=rule, detail=detail)

    def _insert(self, ctx: CallCtx, status: str, rule: str = "", detail: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO usage_events (ts, request_id, client_id, session_id, task_id, "
            "parent_task_id, persona, backend, model, account, paid, status, est_tokens, rule, "
            "detail, project_id, work_task_id, run_id, role) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), ctx.request_id, ctx.client_id, ctx.session_id, ctx.task_id,
             ctx.parent_task_id, ctx.persona, ctx.backend, ctx.model, ctx.account,
             1 if ctx.paid else 0, status, ctx.est_tokens, rule, (detail or "")[:500],
             ctx.project_id, ctx.work_task_id, ctx.run_id, ctx.role))
        return cur

    def prune(self) -> int:
        days = getattr(self.cfg, "retention_days", 90) or 0
        if days <= 0:
            return 0
        return self.db.execute_count("DELETE FROM usage_events WHERE ts < ? AND status != "
                                     "'reserved'", (time.time() - days * 86400,))

    def active(self) -> list[dict]:
        now = time.time()
        rows = {r["id"]: r for r in self.db.query(
            "SELECT id, ts FROM usage_events WHERE status='reserved'")}
        return [{**{k: v for k, v in asdict(c).items() if k not in ("extra",)},
                 "seconds": round(now - (rows.get(eid) or {}).get("ts", now), 1), "id": eid}
                for eid, c in self._active.items()]


# -- monitor --------------------------------------------------------------------------

def _agg(rows) -> dict:
    out = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0, "quota_errors": 0}
    for r in rows:
        out["calls"] += 1
        out["input_tokens"] += r["input_tokens"] or 0
        out["output_tokens"] += r["output_tokens"] or 0
        if r["status"] == "error":
            out["errors"] += 1
            if any(k in (r["detail"] or "").lower() for k in ("429", "quota", "rate limit",
                                                               "usage limit", "exhaust")):
                out["quota_errors"] += 1
    return out


def rule_usage(ledger: Ledger, rule, now: Optional[float] = None) -> dict:
    """Current use of one rule: per group (top 10) calls/tokens in its window,
    calls in flight and active helpers."""
    now = now or time.time()
    window = max(0, rule.window_minutes) * 60
    since = now - window if window else now - 7 * 86400
    rows = [(r, _row_ctx(r)) for r in ledger._rows(since)]
    rows = [(r, c) for r, c in rows if matches(rule.match, c)]
    groups: dict = {}
    for r, c in rows:
        g = c.key(rule.per) if rule.per else "(shared)"
        e = groups.setdefault(g, {"group": g, "calls": 0, "tokens": 0})
        e["calls"] += 1
        e["tokens"] += ((r["input_tokens"] or 0) + (r["output_tokens"] or 0)
                        if r["status"] != "reserved" else (r["est_tokens"] or 0))
    inflight = [c for c in ledger._active.values() if matches(rule.match, c)]
    active = set()
    for r, c in rows:
        if (r.get("settled_ts") or r["ts"]) >= now - max(1, rule.idle_seconds):
            active.add(c.key(rule.active_by))
    active |= {c.key(rule.active_by) for c in inflight}
    return {"groups": sorted(groups.values(), key=lambda g: -g["calls"])[:10],
            "in_flight": len(inflight), "active_helpers": sorted(active),
            "window_note": None if window else "no window (totals shown for the last 7 days)"}


async def monitor(svc) -> dict:
    """Everything the Usage tab's budget monitor shows. Each figure says where
    it comes from; values nobody reports are None ("unknown"), never 0."""
    ledger: Ledger = svc.ledger
    cfg = ledger.cfg
    now = time.time()
    rows_7d = ledger.db.query("SELECT * FROM usage_events WHERE ts >= ?", (now - 7 * 86400,))
    calls = [r for r in rows_7d if r["status"] in COUNTED]
    in5 = [r for r in calls if r["ts"] >= now - 5 * 3600]
    in24 = [r for r in calls if r["ts"] >= now - 86400]

    # accounts: every backend, plus provider accounts seen in the ledger
    accounts: dict = {}

    def acct(key, backend, kind):
        return accounts.setdefault(key, {"account": key, "backend": backend, "kind": kind,
                                         "provider_windows": None, "provider_source": None,
                                         "provider_age_s": None, "provider_error": None,
                                         "foundry_caps": {}, "image_jobs_5h": None,
                                         "image_jobs_24h": None})
    for s in (getattr(svc.pool, "backends", None) or {}).values():
        c = s.config
        paid = is_paid(c)
        kind = "subscription" if (c.type == "anthropic-compatible" or c.is_subscription) \
            else "paid API" if paid else "local"
        if c.type == "anthropic-compatible":
            a = acct(account_of(c, "claude"), c.name, kind)
            try:
                snap = await svc.meridian_usage.snapshot(c.url, c.api_key)
                bk = [b for b in snap.get("buckets") or [] if b.get("used") is not None]
                a["provider_windows"] = [{"name": b.get("label") or b.get("type"),
                                          "used_pct": round(b["used"] * 100, 1),
                                          "resets_at": b.get("resets_at")} for b in bk] or None
                a["provider_source"] = "provider-reported (Meridian quota)" if bk else None
                if not bk:
                    a["provider_error"] = snap.get("note") or "Meridian reports no usage figures"
            except Exception as e:                                # noqa: BLE001
                a["provider_error"] = str(e)[:160]
        elif c.effective_flavor == "cliproxyapi":
            info = {"name": c.name, "url": c.url, "flavor": "cliproxyapi",
                    "management_key": c.management_key, "management_url": c.management_url,
                    "usage_limit_pct": c.usage_limit_pct}
            if svc.cliproxy_usage.configured(info):
                snap = await svc.cliproxy_usage.snapshot(info)
                for ac in snap.get("accounts") or []:
                    a = acct(f"{c.name}:{ac['provider']}", c.name, kind)
                    wins = [{"name": w["name"], "used_pct": w["used"],
                             "resets_in_s": w.get("resets_in_s"),
                             "account_label": ac.get("label")} for w in ac.get("windows") or []]
                    a["provider_windows"] = (a["provider_windows"] or []) + wins or None
                    a["provider_source"] = "provider-reported (CLIProxyAPI management API)"
                    a["provider_age_s"] = ac.get("age_s")
                    a["plan"] = ac.get("plan")
                if not snap.get("ok"):
                    acct(f"{c.name}:codex", c.name, kind)["provider_error"] = snap.get("error")
            else:
                acct(f"{c.name}:codex", c.name, kind)["provider_error"] = \
                    "no management_key: provider usage unknown"
        else:
            acct(c.name, c.name, kind)
        caps = {k: getattr(c, k) for k in ("call_budget_5h", "call_budget_daily")
                if getattr(c, k, None)}
        for a in accounts.values():
            if a["backend"] == c.name and caps:
                a["foundry_caps"] = caps
    for r in calls:
        if r["account"] and r["account"] not in accounts:
            acct(r["account"], r["backend"], "subscription" if r["paid"] else "local")
    for key, a in accounts.items():
        mine = [r for r in calls if r["account"] == key]
        a["measured_5h"] = _agg([r for r in mine if r["ts"] >= now - 5 * 3600])
        a["measured_7d"] = _agg(mine)
        a["last_call_age_s"] = round(now - max(r["ts"] for r in mine)) if mine else None
    # image jobs (media.py), attributed to the image backend's codex account
    try:
        for r in ledger.db.query(
                "SELECT backend, SUM(created_ts >= ?) AS h5, COUNT(*) AS h24 FROM media_jobs "
                "WHERE created_ts >= ? AND charged=1 GROUP BY backend", (now - 5 * 3600, now - 86400)):
            for a in accounts.values():
                if a["backend"] == r["backend"] and a["account"].endswith(":codex"):
                    a["image_jobs_5h"], a["image_jobs_24h"] = r["h5"] or 0, r["h24"] or 0
    except Exception:                                             # noqa: BLE001
        pass

    def breakdown(field_):
        out: dict = {}
        for r in in24:
            k = r[field_] or "(none)"
            out.setdefault(k, []).append(r)
        return sorted(({"name": k, **_agg(v), "calls_5h": sum(1 for x in v if x["ts"] >= now - 5 * 3600),
                        "paid_calls": sum(1 for x in v if x["paid"])} for k, v in out.items()),
                      key=lambda e: -e["calls"])

    denials = [r for r in rows_7d if r["status"] in ("denied", "observed")
               and r["ts"] >= now - 86400]
    by_rule: dict = {}
    for r in denials:
        k = (r["rule"] or "?", r["status"])
        by_rule[k] = by_rule.get(k, 0) + 1
    rules = []
    for rule in (cfg.rules if cfg else []):
        rules.append({**rule.model_dump(), "usage": rule_usage(ledger, rule, now)})
    # retries: requests that needed more than one attempt at the same model
    attempts: dict = {}
    for r in in24:
        if r["request_id"]:
            k = (r["request_id"], r["model"])
            attempts[k] = attempts.get(k, 0) + 1
    return {
        "enabled": bool(cfg and cfg.enabled),
        "sources": {"measured": "measured locally: every call Foundry sent (all clients), "
                                "tokens as the backend reported them",
                    "provider": "provider-reported: the plan's own usage windows, as fresh as "
                                "the age shown",
                    "estimate": "API-price estimates (Usage log 'cost') are NOT subscription "
                                "consumption or charges"},
        "totals": {"5h": _agg(in5), "24h": _agg(in24), "7d": _agg(calls),
                   "retries_24h": sum(n - 1 for n in attempts.values() if n > 1),
                   "denied_24h": sum(1 for r in denials if r["status"] == "denied"),
                   "observed_24h": sum(1 for r in denials if r["status"] == "observed")},
        "accounts": sorted(accounts.values(), key=lambda a: (a["kind"] == "local", a["account"])),
        "active": ledger.active(),
        "by_client": breakdown("client_id"),
        "by_persona": breakdown("persona"),
        "rules": rules,
        "denials": [{"ts": r["ts"], "status": r["status"], "rule": r["rule"], "detail": r["detail"],
                     "client": r["client_id"], "persona": r["persona"], "model": r["model"]}
                    for r in sorted(denials, key=lambda r: -r["ts"])[:30]],
        "denials_by_rule": [{"rule": k[0], "status": k[1], "count": v} for k, v in by_rule.items()],
        "pauses": ledger.pauses(),
    }
