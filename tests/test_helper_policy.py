"""Helper policy (quota_gate.py): optional coding work is admitted only while
every applicable window of the account that serves it is below the cutoff and
freshly observed; UNKNOWN is never admitted; required work and unbound traffic
keep the existing guardrails; purpose comes only from the coordinator's binding.
No real quota is read or spent: every source is a fixture with a fixed clock."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from foundry_router.budget import BudgetDenied, CallCtx
from foundry_router.config import BackendConfig, ClientConfig, HelperPolicyConfig
from foundry_router.quota_gate import ADMIT, BLOCKED, DEFER, UNKNOWN, QuotaGate, redact_account
from tests.test_persona_profiles import fleet  # noqa: F401

NOW = 1_800_000_000.0
COORD = {"Authorization": "Bearer coord-key"}


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class FakeMeridian:
    def __init__(self):
        self.snap = {}
        self.raw = {}

    async def snapshot(self, url, key=None):
        return self.snap

    def last_raw(self, url):
        return self.raw


class FakeCliproxy:
    def __init__(self):
        self.snap = {"ok": True, "accounts": []}
        self._cache = {"http://cpa": (0.0, None)}

    @staticmethod
    def configured(info):
        return True

    @staticmethod
    def _mgmt_root(info):
        return "http://cpa"

    async def snapshot(self, info, force=False):
        import time
        self._cache["http://cpa"] = (time.monotonic(), self.snap)
        return self.snap


def make_gate(app, **pol):
    svc = app.state.services
    cfg = svc.config_store.config
    cfg.helper_policy = HelperPolicyConfig(optional_enabled=True, **pol)
    backends = {
        "meridian": BackendConfig(name="meridian", type="anthropic-compatible", url="http://mer",
                                  helpers_allowed=3, max_concurrent=4),
        "cpa": BackendConfig(name="cpa", type="openai-compatible", url="http://cpa/v1",
                             flavor="cliproxyapi", management_key="mk", helpers_allowed=3,
                             max_concurrent=4),
        "strata": BackendConfig(name="strata", type="openai-compatible", url="http://s/v1",
                                flavor="llamacpp", helpers_allowed=0, max_concurrent=1),
        "openai": BackendConfig(name="openai", type="openai-compatible", url="http://oa/v1",
                                api_key="k", flavor="openai"),
    }
    fake = SimpleNamespace(
        db=svc.db, config_store=svc.config_store, control=svc.control,
        pool=SimpleNamespace(backends={n: SimpleNamespace(config=c) for n, c in backends.items()},
                             backend_info=lambda m: {}),
        meridian_usage=FakeMeridian(), cliproxy_usage=FakeCliproxy())
    clock = [NOW]
    gate = QuotaGate(fake, clock=lambda: clock[0])
    return gate, fake, clock


def meridian_companion(fake, five, seven, scoped=None, fetched=NOW, resets=NOW + 3600):
    buckets = [{"type": "five_hour", "label": "5-hour", "used": None if five is None else five / 100,
                "resets_at": iso(resets), "rolled_over": False},
               {"type": "seven_day", "label": "weekly", "used": None if seven is None else seven / 100,
                "resets_at": iso(resets + 86400), "rolled_over": False}]
    for t, v in (scoped or {}).items():
        buckets.append({"type": t, "label": t, "used": v / 100, "resets_at": iso(resets + 86400),
                        "rolled_over": False})
    fake.meridian_usage.snap = {"available": True, "buckets": buckets, "oauth_ok": True}
    fake.meridian_usage.raw = {"at": iso(fetched), "source": "companion (Anthropic /api/oauth/usage)"}


def codex_account(label, session, weekly, age_s=60, reset_s=3600, plan="plus"):
    windows = []
    if session is not None:
        windows.append({"name": "5-hour", "kind": "session", "used": session, "scope": None,
                        "resets_in_s": reset_s, "rolled_over": False})
    if weekly is not None:
        windows.append({"name": "weekly", "kind": "weekly", "used": weekly, "scope": None,
                        "resets_in_s": reset_s * 24, "rolled_over": False})
    return {"provider": "codex", "label": label, "plan": plan, "disabled": False,
            "unavailable": False, "age_s": age_s, "windows": windows}


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ------------------------------------------------------------------ boundaries

@pytest.mark.parametrize("five,seven,state", [
    (69.9, 20, ADMIT), (70.0, 20, DEFER), (70.1, 20, DEFER),
    (11, 69.9, ADMIT), (11, 70.0, DEFER), (11, 70.1, DEFER)])
def test_meridian_cutoff_boundaries_in_either_window(app, five, seven, state):
    gate, fake, _ = make_gate(app)
    meridian_companion(fake, five, seven)
    d = run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    assert d["state"] == state, d["reason"]
    assert d["accounts"][0]["windows"][0]["observation_age_s"] == 0


@pytest.mark.parametrize("five,seven,state", [
    (69.9, 35, ADMIT), (70.0, 35, DEFER), (35, 70.1, DEFER)])
def test_cliproxy_cutoff_boundaries(app, five, seven, state):
    gate, fake, _ = make_gate(app)
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [codex_account("a", five, seven)]}
    assert run(gate.evaluate("cpa", "gpt-6.1-sol"))["state"] == state


def test_model_scoped_bucket_applies_only_to_its_model(app):
    gate, fake, _ = make_gate(app)
    meridian_companion(fake, 11, 22, scoped={"seven_day_sonnet": 80})
    assert run(gate.evaluate("meridian", "claude-sonnet-5-5"))["state"] == DEFER
    gate._cache.clear()
    assert run(gate.evaluate("meridian", "claude-opus-5-5"))["state"] == ADMIT


# ------------------------------------------------------------------ unknown is not zero

def test_stale_upstream_observation_despite_a_fresh_fetch(app):
    gate, fake, _ = make_gate(app)
    # Foundry just fetched it; the account last saw usage headers 6.7 h ago
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [codex_account("a", 10, 35, age_s=24120)]}
    d = run(gate.evaluate("cpa", "gpt-6.1-sol"))
    assert d["state"] == UNKNOWN and "observed 402 min ago" in d["reason"]
    w = d["accounts"][0]["windows"][1]
    assert w["observation_age_s"] == 24120 and d["fetched_at"] is not None


@pytest.mark.parametrize("mutate,needle", [
    (lambda s: s["buckets"].pop(1), "no weekly-window reading"),
    (lambda s: s["buckets"][0].update(used=None), "no utilization figure"),
    (lambda s: s["buckets"][1].update(rolled_over=True), "window reset after the last observation"),
    (lambda s: s.update(fetch_error="connection refused"), "connection refused"),
    (lambda s: s.update(oauth_ok=False), "sources.oauth null"),
])
def test_missing_malformed_rolled_over_or_unreachable_is_unknown(app, mutate, needle):
    gate, fake, _ = make_gate(app)
    meridian_companion(fake, 11, 22)
    mutate(fake.meridian_usage.snap)
    d = run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    assert d["state"] == UNKNOWN and needle in d["reason"]


def test_meridian_quota_endpoint_without_observation_time_is_unknown_unless_trusted(app):
    gate, fake, _ = make_gate(app)
    meridian_companion(fake, 11, 22)
    fake.meridian_usage.raw = {"at": iso(NOW), "source": "Meridian /v1/usage/quota"}
    d = run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    assert d["state"] == UNKNOWN and "observation time not reported" in d["reason"]
    gate2, fake2, _ = make_gate(app, trust_fetch_time_for=["meridian"])
    meridian_companion(fake2, 11, 22)
    fake2.meridian_usage.raw = {"at": iso(NOW), "source": "Meridian /v1/usage/quota"}
    d2 = run(gate2.evaluate("meridian", "claude-sonnet-5-5"))
    assert d2["state"] == ADMIT
    assert "assumed = fetch time" in d2["accounts"][0]["windows"][0]["observation"]


def test_cliproxy_unknown_serving_account_is_judged_conservatively(app):
    gate, fake, _ = make_gate(app)
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [
        codex_account("low@example.com", 10, 20), codex_account("high@example.com", 75, 20)]}
    d = run(gate.evaluate("cpa", "gpt-6.1-sol"))
    assert d["state"] == DEFER                          # not admitted on the low account
    labels = [a["label"] for a in d["accounts"]]
    assert all("@example.com" not in lab for lab in labels)    # identity redacted
    fake.cliproxy_usage.snap = {"ok": True, "accounts": []}
    gate._cache.clear()
    assert run(gate.evaluate("cpa", "gpt-6.1-sol"))["state"] == UNKNOWN


def test_hysteresis_and_cooldown_after_a_cutoff(app):
    gate, fake, clock = make_gate(app, cutoff_pct=70, hysteresis_pct=5, cooldown_seconds=600,
                                  decision_cache_seconds=0)
    def at(five):
        meridian_companion(fake, five, 20, fetched=clock[0])
        return run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    assert at(72)["state"] == DEFER
    clock[0] += 60
    d = at(68)                                          # below cutoff, but in hysteresis band
    assert d["state"] == DEFER and "resumes below 65" in d["reason"]
    clock[0] += 60
    d = at(60)                                          # below the band, cooldown not over
    assert d["state"] == DEFER and "cooldown" in d["reason"]
    clock[0] += 600
    assert at(60)["state"] == ADMIT
    clock[0] += 10
    assert at(69)["state"] == ADMIT                     # back to the plain cutoff


def test_reset_rollover_then_fresh_reading(app):
    gate, fake, clock = make_gate(app, decision_cache_seconds=0)
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [codex_account("a", 0, 35)]}
    fake.cliproxy_usage.snap["accounts"][0]["windows"][0].update(rolled_over=True, used=0.0)
    assert run(gate.evaluate("cpa", "gpt-6.1-sol"))["state"] == UNKNOWN
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [codex_account("a", 3, 35, age_s=30)]}
    assert run(gate.evaluate("cpa", "gpt-6.1-sol"))["state"] == ADMIT


def test_metered_blocked_and_local_admitted(app):
    gate, _, _ = make_gate(app)
    assert run(gate.evaluate("openai", "gpt-x"))["state"] == BLOCKED
    assert run(gate.evaluate("strata", "qwen"))["state"] == ADMIT


def test_source_failure_alert_is_persistent_and_not_repeated(app):
    gate, fake, _ = make_gate(app, decision_cache_seconds=0)
    meridian_companion(fake, 11, 22)
    fake.meridian_usage.snap["fetch_error"] = "refused"
    for _ in range(3):
        run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    al = [a for a in fake.control.alerts([], "open") if a["kind"] == "quota_source"]
    assert len(al) == 1 and al[0]["severity"] == "warning"
    del fake.meridian_usage.snap["fetch_error"]
    run(gate.evaluate("meridian", "claude-sonnet-5-5"))
    assert not [a for a in fake.control.alerts([], "open") if a["kind"] == "quota_source"]


def test_redact_account():
    assert redact_account("victor@gmail.com") == "vi***@g***"
    assert "victor" not in redact_account("victorwork")


# ------------------------------------------------------------------ purposes

def test_effective_purpose_and_required_allowance(app):
    gate, fake, _ = make_gate(app, required_helper_sessions_per_run=1)
    db = fake.db
    for i, sid in enumerate(("p1", "p2")):
        db.execute("INSERT INTO work_sessions (session_id, project_id, run_id, purpose, created_ts) "
                   "VALUES (?,?,?,?,?)", (sid, "g", "r1", "planning", NOW + i))
    ep = lambda **k: gate.effective_purpose(CallCtx(**k))           # noqa: E731
    assert ep(purpose="") == ""                          # unbound: existing guardrails only
    assert ep(purpose="review") == "required"
    assert ep(purpose="code_proposal") == "optional"
    assert ep(purpose="unverified") == "optional"
    assert ep(purpose="planning", run_id="r1", session_id="p1") == "required"
    assert ep(purpose="planning", run_id="r1", session_id="p2") == "optional"   # over allowance


def test_optional_turned_off_fails_closed(app):
    gate, fake, _ = make_gate(app)
    fake.config_store.config.helper_policy.optional_enabled = False
    d = run(gate.admit_call(CallCtx(purpose="code_proposal", backend="meridian",
                                    model="claude-sonnet-5-5", session_id="h1")))
    assert d and d["code"] == "optional_deferred" and "turned off" in d["message"]
    assert run(gate.admit_call(CallCtx(purpose="review", backend="meridian",
                                       model="claude-sonnet-5-5"))) is None


def _bind(client, sid, purpose, worker_client=""):
    return client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": "g", "task_id": "T1", "run_id": "r1", "role": "helper",
        "persona": "x", "purpose": purpose, "worker_client": worker_client})


def test_purpose_only_from_the_coordinator_binding(app, client):
    svc = app.state.services
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"]),
        ClientConfig(name="kilo", key="kilo-key", scopes=["inference"]),
        ClientConfig(name="other", key="other-key", scopes=["inference"])]
    # an inference key can't bind or relabel a session
    r = client.post("/v1/foundry/work/sessions", headers={"Authorization": "Bearer kilo-key"},
                    json={"session_id": "s", "project_id": "g", "purpose": "review"})
    assert r.status_code in (401, 403)
    assert _bind(client, "h1", "nonsense").status_code == 400
    assert _bind(client, "h1", "review", worker_client="kilo").status_code == 200
    from foundry_router.client_auth import correlation
    assert correlation({"x-session-id": "h1"}, "kilo")["purpose"] == "review"
    # another client presenting that session id doesn't get the required purpose
    assert correlation({"x-session-id": "h1"}, "other")["purpose"] == "unverified"
    # and a client header can't set one
    assert "purpose" not in correlation({"x-session-id": "nope", "x-foundry-purpose": "review"}, "kilo")


# ------------------------------------------------------------------ call path

def test_ledger_defers_optional_and_rechecks_while_waiting(app):
    svc = app.state.services
    ledger = svc.ledger
    calls = []

    async def admit_call(ctx):
        calls.append(ctx.purpose)
        return None if ctx.purpose != "code_proposal" else {
            "message": "deferred", "rule": "helper-policy", "code": "optional_deferred",
            "reset_s": 120}
    ledger.gate = SimpleNamespace(admit_call=admit_call)
    with pytest.raises(BudgetDenied) as e:
        run(ledger.reserve_wait(CallCtx(model="m", backend="b", purpose="code_proposal",
                                        session_id="h1")))
    assert e.value.code == "optional_deferred" and e.value.reset_s == 120
    eid = run(ledger.reserve_wait(CallCtx(model="m", backend="b", purpose="review",
                                          session_id="r1")))
    assert eid
    row = svc.db.query_one("SELECT purpose, status FROM usage_events WHERE id=?", (eid,))
    assert row == {"purpose": "review", "status": "reserved"}
    denied = svc.db.query_one("SELECT rule, purpose FROM usage_events WHERE status='denied'")
    assert denied == {"rule": "helper-policy", "purpose": "code_proposal"}


def test_queued_call_is_readmitted_when_it_starts(app):
    from foundry_router.pool.internal import InternalPool
    svc = app.state.services
    ledger = svc.ledger
    state = {"defer": False}

    async def admit_call(ctx):
        return {"message": "cutoff reached while queued", "rule": "helper-policy",
                "code": "optional_deferred", "reset_s": None} if state["defer"] else None
    ledger.gate = SimpleNamespace(admit_call=admit_call)
    pool = InternalPool.__new__(InternalPool)
    pool.ledger = ledger
    pool.gates = {}
    s = SimpleNamespace(config=SimpleNamespace(name="meridian", max_concurrent=None))
    eid = ledger.reserve(CallCtx(model="m", backend="meridian", purpose="code_proposal",
                                 session_id="h1"))
    state["defer"] = True                               # quota crossed the cutoff meanwhile
    with pytest.raises(BudgetDenied):
        run(pool._recheck_optional(s, eid))
    assert svc.db.query_one("SELECT status FROM usage_events WHERE id=?", (eid,))["status"] == "released"


def test_fallback_into_a_deferred_account_is_refused_not_served(fleet, client):
    """A strict persona's fallback is gated like its first route: optional work
    whose first account fails and whose fallback account is over the cutoff is
    refused, never served there."""
    from tests.test_persona_profiles import HITS, STATE
    svc = fleet
    svc.config_store.config.helper_policy.optional_enabled = True
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"])]

    async def evaluate(backend, model):
        st = ADMIT if backend == "meridian" else DEFER
        return {"state": st, "reason": f"{backend} fixture", "next_eligible": None,
                "accounts": []}
    svc.quota_gate.evaluate = evaluate
    svc.pool.ledger = svc.ledger                        # as main.py wires the real pool
    assert _bind(client, "helper-1", "code_proposal").status_code == 200
    STATE["fail"] = ("meridian",)                       # first route errors -> fallback
    r = client.post("/v1/chat/completions", headers={"x-session-id": "helper-1"}, json={
        "model": "Foundry-Kilo-Review", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code >= 400
    assert ("cliproxy", "gpt-6.1-sol") not in HITS     # the deferred account was never called
    denied = svc.db.query("SELECT backend, rule FROM usage_events WHERE status='denied'")
    assert {"backend": "cliproxy", "rule": "helper-policy"} in denied
    # required work on the same chain is unaffected by the optional gate
    HITS.clear()
    assert _bind(client, "review-1", "review").status_code == 200
    r = client.post("/v1/chat/completions", headers={"x-session-id": "review-1"}, json={
        "model": "Foundry-Kilo-Review", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and HITS[-1] == ("cliproxy", "gpt-6.1-sol")


def test_three_helpers_plus_main_and_local_one(app):
    svc = app.state.services
    ledger = svc.ledger
    caps = {"meridian": SimpleNamespace(helpers_allowed=3, queue_timeout_seconds=0),
            "strata": SimpleNamespace(helpers_allowed=0, queue_timeout_seconds=0)}
    ledger.get_backends = lambda: caps
    for i in range(4):
        ledger.reserve(CallCtx(model="m", backend="meridian", session_id=f"s{i}", client_id="kilo"))
    with pytest.raises(BudgetDenied) as e:
        ledger.reserve(CallCtx(model="m", backend="meridian", session_id="s5", client_id="kilo"))
    assert e.value.rule == "backend-helpers"
    ledger.reserve(CallCtx(model="q", backend="strata", session_id="main", client_id="kilo"))
    with pytest.raises(BudgetDenied):
        ledger.reserve(CallCtx(model="q", backend="strata", session_id="explore", client_id="kilo"))


# ------------------------------------------------------------------ control center

def test_control_settings_csrf_persist_and_status(app, client):
    svc = app.state.services
    svc.config_store.config.clients.append(ClientConfig(name="victor", key="op-key-1234567",
                                                        scopes=["operator"]))
    r = client.post("/control/api/login", json={"key": "op-key-1234567"})
    h = {"x-foundry-csrf": r.json()["csrf"]}
    st = client.get("/control/api/helpers").json()
    assert st["policy"]["cutoff_pct"] == 70.0 and st["policy"]["optional_enabled"] is False
    assert "ceiling" in st["note"]
    body = {"policy": {"optional_enabled": True, "cutoff_pct": 70, "backend_preference":
                       ["meridian", "cpa"], "selective_backends": ["cpa"]}}
    assert client.put("/control/api/helpers/policy", json=body).status_code == 403   # no CSRF
    r = client.put("/control/api/helpers/policy", json=body, headers=h)
    assert r.status_code == 200, r.text
    pol = svc.config_store.config.helper_policy
    assert pol.optional_enabled and pol.backend_preference == ["meridian", "cpa"]
    assert svc.config_store.raw["helper_policy"]["selective_backends"] == ["cpa"]
    bad = client.put("/control/api/helpers/policy", json={"policy": {"cutoff_pct": 150}}, headers=h)
    assert bad.status_code == 400
    assert client.get("/control/api/helpers").status_code == 200


def test_warnings_surface_thin_essential_reserve(app):
    gate, fake, _ = make_gate(app)
    cfg = fake.config_store.config
    cfg.subscription_usage.limit_pct = 85.0
    cfg.meridian.conserve_premium_at = 1.0
    cfg.meridian.conserve_strong_at = 1.0
    w = " ".join(gate.warnings())
    assert "tier conservation is off" in w
    fake.pool.backends["cpa"].config.usage_limit_pct = 70
    assert any("at or below the optional cutoff" in x for x in gate.warnings())


def test_admission_endpoint_is_orchestration_only_and_inference_free(app, client):
    svc = app.state.services
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"]),
        ClientConfig(name="kilo", key="kilo-key", scopes=["inference"])]
    before = svc.db.query_one("SELECT COUNT(*) n FROM usage_events")["n"]
    assert client.get("/v1/foundry/helpers/admission?persona=Foundry-Kilo-Review",
                      headers={"Authorization": "Bearer kilo-key"}).status_code == 403
    r = client.get("/v1/foundry/helpers/admission?persona=Foundry-Kilo-Review", headers=COORD)
    assert r.status_code == 200 and r.json()["admit"] is False
    assert svc.db.query_one("SELECT COUNT(*) n FROM usage_events")["n"] == before
    _ = json


def test_lane_purposes_are_optional_and_bindable(app, client):
    gate, _, _ = make_gate(app)
    from foundry_router.orchestration import OPTIONAL_PURPOSES
    for p in ("visual_observation", "failure_triage", "incremental_review", "research",
              "evidence_reconciliation", "handoff_prep", "preflight", "frontier_primary"):
        assert p in OPTIONAL_PURPOSES
        assert gate.effective_purpose(CallCtx(purpose=p)) == "optional"   # never required
    svc = app.state.services
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"])]
    assert _bind(client, "v1", "visual_observation").status_code == 200


def test_admission_reports_image_modality_per_route(app, client):
    svc = app.state.services
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"])]
    real = svc.registry.get
    svc.registry.get = lambda m: ({"capabilities": '["vision"]'} if m == "seer" else real(m))
    try:
        r = client.get("/v1/foundry/helpers/admission?backend=meridian&model=seer", headers=COORD)
        assert r.status_code == 200 and r.json()["routes"][0]["vision"] is True
        r = client.get("/v1/foundry/helpers/admission?backend=meridian&model=plain-text",
                       headers=COORD)
        assert r.json()["routes"][0]["vision"] is False
    finally:
        svc.registry.get = real
