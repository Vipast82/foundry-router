"""Step 3: the budget ledger — every call reserved and settled, shared rules
(one frontier helper at a time, per-session/token/request caps), pauses,
observe mode, retries and cancellation counted, and the monitor."""

import asyncio
import json
import threading
import time

import httpx
import pytest

from foundry_router.budget import BudgetDenied, CallCtx
from foundry_router.config import (BackendConfig, BackendPoolConfig, BudgetRule,
                                   BudgetsConfig, ClientConfig)
from foundry_router.pool.internal import InternalPool

HITS: list = []
STATE: dict = {}
MODELS = {"strata": ["qwen-local"], "cliproxy": ["gpt-6.1-sol", "gpt-6-astra", "claude-sonnet-5-5"],
          "meridian": ["claude-sonnet-5-5"]}
HOSTS = {"strata-host": "strata", "cpa-host": "cliproxy", "mer-host": "meridian"}


async def _router(req: httpx.Request):
    backend = HOSTS[req.url.host]
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": m} for m in MODELS[backend]]})
    body = json.loads(req.content)
    HITS.append((backend, body["model"]))
    STATE["last_body"] = body
    delay = STATE.get("delay", 0)
    if delay:
        await asyncio.sleep(delay)
    if backend in STATE.get("fail", ()):
        return httpx.Response(500, text="boom")
    if backend in STATE.get("refuse", ()):
        raise httpx.ConnectError("refused", request=req)
    if STATE.get("tool_reply"):          # the session is coming back with the tool result
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [{
                "id": "c1", "type": "function", "function": {
                    "name": STATE["tool_reply"], "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30}})
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}})


@pytest.fixture()
def ledgered(app, client):
    svc = app.state.services
    HITS.clear()
    STATE.clear()
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="strata", type="openai-compatible", url="http://strata-host/v1",
                      flavor="llamacpp", priority=100),
        BackendConfig(name="meridian", type="openai-compatible", url="http://mer-host/v1",
                      subscription=True, priority=90),
        BackendConfig(name="cliproxy", type="openai-compatible", url="http://cpa-host/v1",
                      api_key="k", flavor="cliproxyapi", priority=50)],
        BackendPoolConfig(), http, svc.db)
    for name, models in MODELS.items():
        s = pool.backends[name]
        s.healthy = s.ever_checked = True
        s.models = list(models)
    pool.ledger = svc.ledger
    real_pool, real_http = svc.pool, svc.http
    svc.pool, svc.http = pool, http
    cfg = svc.config_store.config
    cfg.agent_brain.direct_stream = False
    saved = (cfg.budgets, cfg.clients)
    # The rule that used to ship by default (now per-backend helpers_allowed):
    # still a supported rule, and what these tests exercise.
    cfg.budgets = BudgetsConfig(rules=[BudgetRule(
        name="one-frontier-helper", match={"persona": "Foundry-Kilo-*", "paid": True},
        per="account", max_concurrent=1, max_active=1, active_by="task", idle_seconds=60)])
    cfg.clients = [ClientConfig(name="kilo", key="kk"), ClientConfig(name="cline", key="ck")]
    for name, backend, model in (("Foundry-Kilo-Sol", "cliproxy", "gpt-6.1-sol"),
                                 ("Foundry-Kilo-Astra", "cliproxy", "gpt-6-astra"),
                                 ("Foundry-Kilo-Local", "strata", "qwen-local")):
        svc.personas.upsert(name, execution_mode="direct", routing="strict", backend=backend,
                            model_allowlist=[model], pinned_models=[model])
    yield svc
    cfg.budgets, cfg.clients = saved
    svc.pool, svc.http = real_pool, real_http


def _chat(client, model, key="kk", task=None, session=None, path="/v1/chat/completions"):
    h = {"Authorization": f"Bearer {key}"}
    if task:
        h["x-foundry-task"] = task
    if session:
        h["x-foundry-session"] = session
    return client.post(path, headers=h, json={"model": model, "stream": False,
                                               "messages": [{"role": "user", "content": "hi"}]})


def _events(svc, status=None):
    q = "SELECT * FROM usage_events" + (" WHERE status=?" if status else "") + " ORDER BY id"
    return svc.db.query(q, (status,) if status else ())


def test_every_call_is_recorded_with_identity_and_tokens(ledgered, client):
    r = _chat(client, "Foundry-Kilo-Sol", task="t1", session="s1")
    assert r.status_code == 200
    ev, = _events(ledgered, "settled")
    assert (ev["client_id"], ev["session_id"], ev["task_id"], ev["persona"]) == \
        ("kilo", "s1", "t1", "Foundry-Kilo-Sol")
    assert (ev["backend"], ev["model"], ev["account"], ev["paid"]) == \
        ("cliproxy", "gpt-6.1-sol", "cliproxy:codex", 1)
    assert (ev["input_tokens"], ev["output_tokens"]) == (120, 30)
    assert ledgered.ledger.active() == []
    _chat(client, "Foundry-Kilo-Local", key="ck")
    local = _events(ledgered, "settled")[-1]
    assert (local["client_id"], local["paid"], local["account"]) == ("cline", 0, "strata")


def test_one_frontier_helper_at_a_time(ledgered, client):
    STATE["delay"] = 1.0
    out = {}

    def call(name, task):
        out[name] = _chat(client, name, task=task)
    t1 = threading.Thread(target=call, args=("Foundry-Kilo-Sol", "helper-a"))
    t1.start()
    time.sleep(0.4)                        # helper A is in flight
    call("Foundry-Kilo-Astra", "helper-b")
    t1.join()
    assert out["Foundry-Kilo-Sol"].status_code == 200
    b = out["Foundry-Kilo-Astra"]
    assert b.status_code == 429
    err = b.json()["error"]
    assert err["code"] == "budget_denied" and err["type"] == "rate_limit_error"
    assert "one-frontier-helper" in err["message"]
    assert HITS == [("cliproxy", "gpt-6.1-sol")]
    STATE["delay"] = 0
    # A finished, but stays the active helper for idle_seconds: B still waits...
    r = _chat(client, "Foundry-Kilo-Astra", task="helper-b")
    assert r.status_code == 429 and "Free in about" in r.json()["error"]["message"]
    # ...while A's own next round goes through, and local work is never limited
    assert _chat(client, "Foundry-Kilo-Sol", task="helper-a").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", task="helper-b").status_code == 200
    denials = _events(ledgered, "denied")
    assert denials and all(d["rule"] == "one-frontier-helper" for d in denials)



def test_one_helper_per_subscription_account(ledgered, client):
    """A GPT helper on CLIProxyAPI doesn't hold up a Claude helper on Meridian
    (separate subscriptions); a second GPT helper still waits."""
    ledgered.personas.upsert("Foundry-Kilo-Sonnet", execution_mode="direct", routing="strict",
                             backend="meridian", model_allowlist=["claude-sonnet-5-5"],
                             pinned_models=["claude-sonnet-5-5"])
    assert _chat(client, "Foundry-Kilo-Sol", task="gpt-helper").status_code == 200
    assert _chat(client, "Foundry-Kilo-Sonnet", task="claude-helper").status_code == 200
    r = _chat(client, "Foundry-Kilo-Astra", task="gpt-helper-2")
    assert r.status_code == 429 and "for account cliproxy:codex" in r.json()["error"]["message"]

def test_ledger_reservation_is_atomic():
    """Two reservations back to back (as two parallel helpers would make):
    the second is refused, nothing is double-booked."""
    from foundry_router.budget import Ledger
    from foundry_router.db import Database
    import tempfile, os
    db = Database(os.path.join(tempfile.mkdtemp(), "l.sqlite"))
    cfg = BudgetsConfig(rules=[BudgetRule(name="solo", match={"paid": True}, max_concurrent=1)])
    led = Ledger(lambda: cfg, db)
    a = led.reserve(CallCtx(model="m", backend="b", paid=True, task_id="a"))
    with pytest.raises(BudgetDenied):
        led.reserve(CallCtx(model="m", backend="b", paid=True, task_id="b"))
    led.settle(a, "settled", 10, 5)
    led.reserve(CallCtx(model="m", backend="b", paid=True, task_id="b"))
    assert db.query_one("SELECT COUNT(*) AS n FROM usage_events WHERE status='denied'")["n"] == 1


def test_session_call_and_token_caps(ledgered, client):
    ledgered.config_store.config.budgets = BudgetsConfig(rules=[
        BudgetRule(name="kilo-session", match={"client": "kilo"}, per="session", max_calls=2),
        BudgetRule(name="cline-tokens", match={"client": "cline"}, max_tokens=155)])
    for _ in range(2):
        assert _chat(client, "Foundry-Kilo-Local", session="s1").status_code == 200
    r = _chat(client, "Foundry-Kilo-Local", session="s1")
    assert r.status_code == 429
    msg = r.json()["error"]["message"]
    assert "kilo-session" in msg and "2/2 calls" in msg and "session s1" in msg
    assert "next slot in" in msg and r.headers.get("retry-after")
    assert _chat(client, "Foundry-Kilo-Local", session="s2").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", key="ck").status_code == 200     # 150 tokens
    r = _chat(client, "Foundry-Kilo-Local", key="ck")
    assert r.status_code == 429 and "tokens" in r.json()["error"]["message"]


def test_pause_switches(ledgered, client):
    def pause(scope, value, on=True):
        r = client.post("/admin/api/budgets/pause", json={"scope": scope, "value": value,
                                                          "paused": on, "note": "test"})
        assert r.status_code == 200
    pause("account", "cliproxy:codex")
    r = _chat(client, "Foundry-Kilo-Sol")
    assert r.status_code == 429 and "paused" in r.json()["error"]["message"]
    ledgered.personas.upsert("Claude-Via-CPA", execution_mode="direct", routing="strict",
                             backend="cliproxy", model_allowlist=["claude-sonnet-5-5"])
    assert _chat(client, "Claude-Via-CPA").status_code == 200      # another account: fine
    pause("account", "cliproxy:codex", False)
    assert _chat(client, "Foundry-Kilo-Sol", task="x").status_code == 200
    pause("client", "cline")
    assert _chat(client, "Foundry-Kilo-Local", key="ck").status_code == 429
    assert _chat(client, "Foundry-Kilo-Local", key="kk").status_code == 200
    pause("persona", "Foundry-Kilo-Local")
    assert _chat(client, "Foundry-Kilo-Local", key="kk").status_code == 429
    assert client.post("/admin/api/budgets/pause", json={"scope": "planet", "value": "x"}
                       ).status_code == 400


def test_observe_mode_records_but_allows(ledgered, client):
    ledgered.config_store.config.budgets = BudgetsConfig(rules=[
        BudgetRule(name="watch", mode="observe", match={"client": "kilo"}, max_calls=1)])
    for _ in range(3):
        assert _chat(client, "Foundry-Kilo-Local").status_code == 200
    obs = _events(ledgered, "observed")
    assert len(obs) == 2 and obs[0]["rule"] == "watch"


def test_retries_and_connect_failures(ledgered, client):
    ledgered.personas.upsert("Flexi", execution_mode="direct", model_allowlist=["claude-sonnet-5-5"])
    STATE["fail"] = {"meridian"}
    assert _chat(client, "Flexi").status_code == 200
    rows = [(e["backend"], e["status"]) for e in _events(ledgered) if e["model"] == "claude-sonnet-5-5"]
    assert rows == [("meridian", "error"), ("cliproxy", "settled")]     # the retry counted
    STATE.update(fail=set(), refuse={"meridian"})
    _chat(client, "Flexi")
    last = [(e["backend"], e["status"]) for e in _events(ledgered)][-2:]
    assert last == [("meridian", "released"), ("cliproxy", "settled")]  # never reached: not counted


def test_per_request_cap_and_passthrough(ledgered, client):
    ledgered.config_store.config.budgets = BudgetsConfig(rules=[
        BudgetRule(name="raw-models", match={"persona": ""}, max_calls=1, window_minutes=60)])
    r = client.post("/api/chat", json={"model": "qwen-local", "stream": False,
                                       "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    r = client.post("/api/chat", json={"model": "qwen-local", "stream": False,
                                       "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 429 and r.json()["code"] == "budget_denied"


def test_cancelled_call_is_settled(ledgered, client):
    STATE["delay"] = 5

    async def run():
        task = asyncio.create_task(ledgered.pool.chat("gpt-6.1-sol", [{"role": "user", "content": "x"}]))
        await asyncio.sleep(0.3)
        assert len(ledgered.ledger.active()) == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ledgered.ledger.active()
    assert client.portal.call(run) == []
    assert _events(ledgered)[-1]["status"] == "error"


def test_monitor_and_config_endpoints(ledgered, client):
    _chat(client, "Foundry-Kilo-Sol", task="a")
    _chat(client, "Foundry-Kilo-Astra", task="b")               # denied: helper a active
    m = client.get("/admin/api/budgets").json()
    accts = {a["account"]: a for a in m["accounts"]}
    codex = accts["cliproxy:codex"]
    assert codex["measured_5h"]["calls"] == 1 and codex["measured_5h"]["input_tokens"] == 120
    assert codex["provider_windows"] is None                     # unknown, not zero
    assert "management_key" in codex["provider_error"]
    assert accts["strata"]["kind"] == "local"
    assert m["totals"]["denied_24h"] == 1
    assert m["by_client"][0]["name"] == "kilo"
    rule = m["rules"][0]
    assert rule["name"] == "one-frontier-helper" and rule["usage"]["active_helpers"] == ["a"]
    assert m["denials"][0]["rule"] == "one-frontier-helper"
    assert "NOT subscription" in m["sources"]["estimate"]
    # config: validated, unique names, saved
    bad = client.post("/admin/api/config/budgets", json={"rules": [{"name": "x"}, {"name": "x"}]})
    assert bad.status_code == 400
    bad = client.post("/admin/api/config/budgets", json={"rules": [{"name": "x", "per": "galaxy"}]})
    assert bad.status_code == 400
    ok = client.post("/admin/api/config/budgets", json={"rules": [
        {"name": "sol-day", "match": {"persona": "Foundry-Kilo-Sol"}, "max_calls": 50,
         "window_minutes": 1440}]})
    assert ok.status_code == 200
    assert [r.name for r in ledgered.config_store.config.budgets.rules] == ["sol-day"]


# -- per-backend streams (helpers_allowed) -------------------------------------------

@pytest.fixture()
def streams(ledgered, monkeypatch):
    """No budget rules; strata limited by helpers_allowed, short hold/timeout.
    Replies end in a tool call (the session is coming back), so it holds."""
    ledgered.config_store.config.budgets = BudgetsConfig(rules=[])
    strata = ledgered.pool.backends["strata"].config
    strata.queue_timeout_seconds = 0.3
    strata.session_hold_seconds = 1
    STATE["tool_reply"] = "read_file"
    return strata


def test_backend_serves_main_plus_its_helpers(streams, client):
    streams.helpers_allowed = 1
    assert _chat(client, "Foundry-Kilo-Local", session="main").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", session="helper-1").status_code == 200
    r = _chat(client, "Foundry-Kilo-Local", session="helper-2")
    assert r.status_code == 429
    msg = r.json()["error"]["message"]
    assert "backend 'strata' serves the main stream plus 1 helper" in msg and "2 session(s)" in msg
    # the sessions already holding a slot keep going
    assert _chat(client, "Foundry-Kilo-Local", session="main").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", session="helper-1").status_code == 200
    # other backends have their own setting (none here)
    assert _chat(client, "Foundry-Kilo-Sol", session="helper-2").status_code == 200


def test_zero_helpers_means_main_only_and_probes_are_free(streams, client):
    from foundry_router import client_auth
    streams.helpers_allowed = 0
    assert _chat(client, "Foundry-Kilo-Local", session="main").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", session="sub").status_code == 429
    tok = client_auth._client.set({"client_id": "foundry:health", "internal": True,
                                   "scopes": ["inference"], "personas": []})
    try:
        ctx = CallCtx(model="qwen-local", backend="strata", client_id="foundry:health")
        streams_ledger = client.app.state.services.ledger
        assert streams_ledger.check(ctx) is None
        assert set(streams_ledger.sessions("strata")) == {"main"}
    finally:
        client_auth._client.reset(tok)


def test_helper_waits_for_a_slot_instead_of_failing(streams, client):
    streams.helpers_allowed = 0
    streams.queue_timeout_seconds = 5
    assert _chat(client, "Foundry-Kilo-Local", session="main").status_code == 200
    t = time.monotonic()
    assert _chat(client, "Foundry-Kilo-Local", session="sub").status_code == 200   # main went idle
    assert 0.5 < time.monotonic() - t < 4


def test_streams_shown_in_health_and_route_gates(streams, client):
    streams.helpers_allowed = 2
    _chat(client, "Foundry-Kilo-Local", session="main")
    fh = {b["backend"]: b for b in client.get("/admin/api/health/functional").json()["backends"]}
    st = fh["strata"]["streams"]
    assert st["streams_allowed"] == 3 and [a["session"] for a in st["active"]] == ["main"]
    assert fh["cliproxy"]["streams"]["streams_allowed"] is None
    g = client.get("/admin/api/routes?hours=24&all=1").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    assert "strata: main + 2 helpers" in nodes["persona:Foundry-Kilo-Local"]["gates"]


# -- 2026-10-31: a finished session frees its stream at once --------------------------
# Strata (helpers_allowed 0) held every new Kilo task ~60 s: the previous task's
# session counted as active until 60 s after its last call, although its last
# reply was a final answer.

def test_a_final_answer_frees_the_stream_at_once(streams, client):
    streams.helpers_allowed = 0
    streams.queue_timeout_seconds = 5
    streams.session_hold_seconds = 60
    STATE.pop("tool_reply")
    assert _chat(client, "Foundry-Kilo-Local", session="task-1").status_code == 200
    t = time.monotonic()
    assert _chat(client, "Foundry-Kilo-Local", session="task-2").status_code == 200
    assert time.monotonic() - t < 1.0                     # no 60 s hold for a finished task
    [row] = [r for r in _events(client.app.state.services, "settled")
             if r["session_id"] == "task-1"]
    assert row["final"] == 1


def test_a_completion_tool_call_is_a_final_answer(streams, client):
    streams.helpers_allowed = 0
    STATE["tool_reply"] = "attempt_completion"
    assert _chat(client, "Foundry-Kilo-Local", session="task-1").status_code == 200
    assert _chat(client, "Foundry-Kilo-Local", session="task-2").status_code == 200


def test_a_tool_call_keeps_the_hold_for_session_hold_seconds(streams, client):
    streams.helpers_allowed = 0
    streams.session_hold_seconds = 60
    assert _chat(client, "Foundry-Kilo-Local", session="main").status_code == 200
    r = _chat(client, "Foundry-Kilo-Local", session="other")
    assert r.status_code == 429 and "Free in about" in r.json()["error"]["message"]
    streams.session_hold_seconds = 0                      # the operator turns the hold off
    assert _chat(client, "Foundry-Kilo-Local", session="other").status_code == 200


def test_a_call_in_flight_always_holds_and_its_latest_call_decides():
    from foundry_router.budget import ended_final
    assert ended_final([]) and ended_final(None)
    assert ended_final([{"name": "attempt_completion"}])
    assert ended_final([{"function": {"name": "attempt_completion"}}])
    assert not ended_final([{"name": "read_file"}])
    assert not ended_final([{"name": "attempt_completion"}, {"name": "task"}])


def test_in_flight_call_holds_even_after_a_final_answer(streams, client):
    svc = client.app.state.services
    streams.helpers_allowed = 0
    STATE.pop("tool_reply")
    assert _chat(client, "Foundry-Kilo-Local", session="task-1").status_code == 200
    assert svc.ledger.sessions("strata") == {}
    ctx = CallCtx(model="qwen-local", backend="strata", client_id="kilo", session_id="task-1")
    eid = svc.ledger.reserve(ctx)
    try:
        assert set(svc.ledger.sessions("strata")) == {"task-1"}
    finally:
        svc.ledger.settle(eid, "settled", final=False)
    assert set(svc.ledger.sessions("strata")) == {"task-1"}    # latest call: a tool call


def test_session_hold_is_set_in_the_limits_ui(streams, client):
    svc = client.app.state.services
    raw_names = [b.name for b in svc.config_store.config.backend_pool.internal.backends or []]
    d = client.get("/admin/api/setup/limits").json()
    for b in d["backends"]:
        assert "session_hold_seconds" in b
    if raw_names:
        name = raw_names[0]
        r = client.post("/admin/api/setup/limits", json={"name": name, "session_hold_seconds": 5})
        assert r.status_code == 200, r.text
        r = client.post("/admin/api/setup/limits", json={"name": name, "session_hold_seconds": -1})
        assert r.status_code == 400
    html = open(__import__("foundry_router").__path__[0] + "/ui/static/index.html",
                encoding="utf-8").read()
    assert 'data-f="session_hold_seconds"' in html


def test_a_rule_denial_does_not_wait_the_backends_queue_timeout():
    """v0.181.116 (2026-11-10): a "deny paid for the Implementer" rule made each paid
    candidate wait the backend's 300 s queue_timeout before failover, 10 minutes per request.
    Only a stream-slot denial (backend-helpers) waits queue_timeout; a rule waits its own
    wait_seconds (0 here)."""
    from foundry_router.budget import Ledger
    from foundry_router.db import Database
    from types import SimpleNamespace
    import tempfile, os
    db = Database(os.path.join(tempfile.mkdtemp(), "w.sqlite"))
    cfg = BudgetsConfig(rules=[BudgetRule(name="impl-local", match={"paid": True},
                                          window_minutes=1, max_tokens=1)])
    backends = {"meridian": SimpleNamespace(helpers_allowed=0, queue_timeout_seconds=300,
                                            session_hold_seconds=60)}
    led = Ledger(lambda: cfg, db, get_backends=lambda: backends)
    t = time.monotonic()
    with pytest.raises(BudgetDenied):
        asyncio.run(led.reserve_wait(CallCtx(model="claude-sonnet-5-5", backend="meridian",
                                             paid=True, est_tokens=25000)))
    assert time.monotonic() - t < 2
