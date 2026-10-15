"""Step 5: request-level route/attempt/outcome fields, functional health,
the route graph + traces, and the client comparison report."""

import json

from tests.test_budgets import HITS, STATE, _chat, ledgered  # noqa: F401 (fixture)


def _last_req(svc):
    return svc.db.query("SELECT * FROM request_log ORDER BY id DESC LIMIT 1")[0]


def test_request_row_records_route_attempts_outcome(ledgered, client):
    _chat(client, "Foundry-Kilo-Sol", task="t1", session="s1")
    r = _last_req(ledgered)
    assert (r["served_model"], r["served_backend"], r["routing"]) == ("gpt-6.1-sol", "cliproxy", "strict")
    assert (r["attempts"], r["retries"], r["outcome"]) == (1, 0, "ok")
    perf = ledgered.db.query("SELECT request_id, client_id FROM perf_samples ORDER BY id DESC LIMIT 1")[0]
    assert perf["request_id"] == r["request_id"] and perf["client_id"] == "kilo"
    # a failed attempt followed by another backend = one retry
    ledgered.personas.upsert("Flexi", execution_mode="direct", model_allowlist=["claude-sonnet-5-5"])
    STATE["fail"] = {"meridian"}
    _chat(client, "Flexi")
    r = _last_req(ledgered)
    assert (r["attempts"], r["retries"], r["served_backend"], r["routing"]) == (2, 1, "cliproxy", "flexible")
    # a budget refusal is outcome "denied"
    STATE.clear()
    _chat(client, "Foundry-Kilo-Astra", task="t2")          # helper t1 still active
    r = _last_req(ledgered)
    assert r["outcome"] == "denied" and r["attempts"] is None
    flags = json.loads(_last_req(ledgered)["flags"] or "{}")
    assert flags.get("client_tools") is None                 # no tools sent: not recorded


def test_functional_health(ledgered, client):
    def status():
        return {b["backend"]: b for b in client.get("/admin/api/health/functional").json()["backends"]}
    st = status()
    assert st["strata"]["status"] == "unverified" and st["strata"]["reachable"] is True
    assert st["cliproxy"]["paid"] and not st["cliproxy"]["auto_probe"] and st["strata"]["auto_probe"]
    _chat(client, "Foundry-Kilo-Local")
    st = status()
    assert st["strata"]["status"] == "serving" and st["strata"]["last_ok"]["source"] == "real call"
    assert st["strata"]["calls_1h"] == 1
    # a probe hits exactly the named backend
    HITS.clear()
    p = client.post("/admin/api/health/probe", json={"backend": "meridian"}).json()
    assert p["ok"] and p["model"] == "claude-sonnet-5-5"
    assert HITS == [("meridian", "claude-sonnet-5-5")]
    assert status()["meridian"]["last_ok"]["source"] in ("probe", "real call")
    STATE["fail"] = {"meridian"}
    p = client.post("/admin/api/health/probe", json={"backend": "meridian"}).json()
    assert not p["ok"]
    assert HITS == [("meridian", "claude-sonnet-5-5")] * 2   # never fell over to cliproxy
    assert status()["meridian"]["status"] == "failing"
    events = ledgered.db.query("SELECT message FROM event_log WHERE source='health'")
    assert any("serving → failing" in e["message"] for e in events)
    assert client.post("/admin/api/health/probe", json={"backend": "nope"}).status_code == 404


def test_route_graph_edges_and_trace(ledgered, client):
    _chat(client, "Foundry-Kilo-Sol", task="a", session="s1")
    _chat(client, "Foundry-Kilo-Astra", task="b")                 # denied
    _chat(client, "Foundry-Kilo-Local", key="ck")
    g = client.get("/admin/api/routes?hours=24").json()
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["persona:Foundry-Kilo-Sol"]["strict"] and nodes["persona:Foundry-Kilo-Sol"]["backend"] == "cliproxy"
    assert "one-frontier-helper" in nodes["persona:Foundry-Kilo-Sol"]["gates"]
    edges = {(e["kind"], e["from"], e["to"]): e for e in g["edges"]}
    assert edges[("traffic", "client:kilo", "persona:Foundry-Kilo-Sol")]["count"] == 1
    assert ("traffic", "persona:Foundry-Kilo-Sol", "model:gpt-6.1-sol") in edges
    assert ("traffic", "model:gpt-6.1-sol", "backend:cliproxy") in edges
    assert ("config", "persona:Foundry-Kilo-Astra", "model:gpt-6-astra") in edges
    assert ("config", "model:gpt-6-astra", "backend:cliproxy") in edges     # pinned backend
    den = edges[("denied", "persona:Foundry-Kilo-Astra", "model:gpt-6-astra")]
    assert den["count"] == 1 and "one-frontier-helper" in den["rules"]
    assert g["requests"] == 3 and g["denials"] == 1
    reqs = client.get("/admin/api/routes/edge", params={
        "kind": "traffic", "src": "client:kilo", "dst": "persona:Foundry-Kilo-Sol"}).json()["requests"]
    assert len(reqs) == 1 and reqs[0]["backend"] == "cliproxy" and reqs[0]["input_tokens"] == 120
    dreqs = client.get("/admin/api/routes/edge", params={
        "kind": "denied", "src": "persona:Foundry-Kilo-Astra", "dst": "model:gpt-6-astra"}).json()["requests"]
    assert dreqs[0]["outcome"] == "denied" and "one-frontier-helper" in dreqs[0]["error"]
    t = client.get(f"/admin/api/requests/{reqs[0]['request_id']}/trace").json()
    assert t["request"]["persona"] == "Foundry-Kilo-Sol"
    assert [l["status"] for l in t["ledger"]] == ["settled"] and t["ledger"][0]["account"] == "cliproxy:codex"
    assert t["model_calls"] and t["model_calls"][0]["model"] == "gpt-6.1-sol"
    assert client.get("/admin/api/requests/nope/trace").status_code == 404


def test_compare_clients(ledgered, client):
    for i in range(3):
        _chat(client, "Foundry-Kilo-Local", session="k1")
    _chat(client, "Foundry-Kilo-Sol", session="k1", task="h1")
    _chat(client, "Foundry-Kilo-Astra", session="k1", task="h2")    # denied
    STATE["fail"] = {"strata"}
    _chat(client, "Foundry-Kilo-Local", key="ck", session="c1")
    STATE.clear()
    _chat(client, "Foundry-Kilo-Local", key="ck", session="c1")
    r = client.get("/admin/api/compare?hours=24").json()
    g = {x["group"]: x for x in r["groups"]}
    kilo, cline = g["kilo"], g["cline"]
    assert kilo["requests"] == 5 and kilo["sessions"] == 1 and kilo["requests_per_session"] == 5
    assert kilo["paid_calls"] == 1 and kilo["paid_calls_per_session"] == 1 and kilo["denied"] == 1
    assert kilo["input_tokens"] == 4 * 120
    assert cline["requests"] == 2 and cline["error_rate"] == 50.0 and cline["error_streak_max"] == 1
    assert cline["paid_calls"] == 0
    assert "human interventions" in r["not_observable"]
    by_persona = client.get("/admin/api/compare?hours=24&group=persona").json()["groups"]
    assert {x["group"] for x in by_persona} >= {"Foundry-Kilo-Local", "Foundry-Kilo-Sol"}
