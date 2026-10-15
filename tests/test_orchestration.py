"""Work bindings for a development coordinator: Kilo's own session header as
the session id, project/task/run/role on every call, per-project and per-run
budget rules on top of shared account limits, recognisable denials, the
orchestration scope, and strict personas never consulting a frontier helper."""

import json

from foundry_router.config import BudgetRule, BudgetsConfig, ClientConfig
from tests.test_budgets import HITS, STATE, ledgered  # noqa: F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}


def _setup_coord(svc):
    svc.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration"]))


def _bind(client, sid, project, task, run, role="implementer", parent=""):
    return client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": project, "task_id": task, "run_id": run,
        "role": role, "parent_session_id": parent, "persona": "Foundry-Kilo-Local"})


def _kilo_chat(client, model, sid, key="kk"):
    """What Kilo actually sends: its session id as x-session-id (captured
    from Kilo 7.8.8 against a header-recording provider)."""
    return client.post("/v1/chat/completions",
                       headers={"Authorization": f"Bearer {key}", "x-session-id": sid,
                                "x-session-affinity": sid},
                       json={"model": model, "stream": False,
                             "messages": [{"role": "user", "content": "hi"}]})


def test_kilo_session_header_and_binding_tag_every_call(ledgered, client):
    _setup_coord(ledgered)
    assert _bind(client, "ses_A", "animaluprising", "UI-02", "run-1").status_code == 200
    assert _bind(client, "ses_A-child", "animaluprising", "UI-02", "run-1", role="researcher",
                 parent="ses_A").status_code == 200
    assert _kilo_chat(client, "Foundry-Kilo-Local", "ses_A").status_code == 200
    assert _kilo_chat(client, "Foundry-Kilo-Local", "ses_A-child").status_code == 200
    assert _kilo_chat(client, "Foundry-Kilo-Local", "ses_unbound").status_code == 200
    ev = ledgered.db.query("SELECT session_id, project_id, work_task_id, run_id, role, "
                           "parent_task_id FROM usage_events WHERE status='settled' ORDER BY id")
    assert [tuple(r.values()) for r in map(dict, ev)] == [
        ("ses_A", "animaluprising", "UI-02", "run-1", "implementer", ""),
        ("ses_A-child", "animaluprising", "UI-02", "run-1", "researcher", "ses_A"),
        ("ses_unbound", "", "", "", "", "")]
    rl = ledgered.db.query("SELECT session_id, project_id, work_task_id, run_id, role FROM "
                           "request_log ORDER BY id")
    assert dict(rl[0]) == {"session_id": "ses_A", "project_id": "animaluprising",
                           "work_task_id": "UI-02", "run_id": "run-1", "role": "implementer"}
    u = client.get("/v1/foundry/work/usage?task=UI-02", headers=COORD).json()
    assert u["calls"] == 2 and u["input_tokens"] == 240 and u["source"].startswith("measured")
    # explicit x-foundry-session still wins over Kilo's header
    client.post("/v1/chat/completions", headers={
        "Authorization": "Bearer kk", "x-session-id": "ses_A", "x-foundry-session": "mine"},
        json={"model": "Foundry-Kilo-Local", "stream": False,
              "messages": [{"role": "user", "content": "hi"}]})
    last = ledgered.db.query("SELECT session_id, project_id FROM usage_events ORDER BY id DESC "
                             "LIMIT 1")[0]
    assert dict(last) == {"session_id": "mine", "project_id": ""}


def test_session_cannot_move_between_projects_and_ids_are_validated(ledgered, client):
    _setup_coord(ledgered)
    assert _bind(client, "ses_X", "game-one", "T1", "r1").status_code == 200
    r = _bind(client, "ses_X", "game-two", "T9", "r9")
    assert r.status_code == 409 and "bound to project" in r.json()["error"]
    assert _bind(client, "ses_Y", "", "T1", "r1").status_code == 400
    assert _bind(client, "bad id!", "game-one", "T1", "r1").status_code == 400
    r = client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": "ses_Z", "project_id": "g", "role": "wizard"})
    assert r.status_code == 400
    # ending a session drops it from the request-time lookup
    assert client.post("/v1/foundry/work/sessions/ses_X/end", headers=COORD,
                       json={"outcome": "done"}).json()["ended"]
    assert ledgered.work.lookup("ses_X") is None


def test_orchestration_scope_required(ledgered, client):
    _setup_coord(ledgered)
    body = {"session_id": "s", "project_id": "p"}
    assert client.post("/v1/foundry/work/sessions", json=body).status_code == 401
    assert client.post("/v1/foundry/work/sessions", json=body,
                       headers={"Authorization": "Bearer kk"}).status_code == 403
    assert client.post("/v1/foundry/work/sessions", json=body, headers=COORD).status_code == 200
    # the coordinator key can't run inference unless it is given that scope too
    assert _kilo_chat(client, "Foundry-Kilo-Local", "s", key="coord-key").status_code == 403


def test_per_run_and_per_project_rules_with_shared_account_limit(ledgered, client):
    """A per-run cap stops one sprint attempt; a project cap stops one game;
    the shared account rule still covers both projects together."""
    _setup_coord(ledgered)
    cfg = ledgered.config_store.config
    cfg.budgets = BudgetsConfig(rules=[
        BudgetRule(name="review-per-run", match={"role": "reviewer"}, per="run",
                   max_calls=1, window_minutes=0),
        BudgetRule(name="game-two-cap", match={"project": "game-two"}, max_calls=2,
                   window_minutes=0),
        BudgetRule(name="codex-shared", match={"account": "cliproxy:codex"}, max_calls=3,
                   window_minutes=0)])
    _bind(client, "rev1", "game-one", "T1", "run-a", role="reviewer")
    _bind(client, "rev2", "game-one", "T1", "run-b", role="reviewer")
    assert _kilo_chat(client, "Foundry-Kilo-Sol", "rev1").status_code == 200
    r = _kilo_chat(client, "Foundry-Kilo-Sol", "rev1")
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "budget_denied" and err["foundry_category"] == "budget"
    assert "review-per-run" in err["message"]
    assert _kilo_chat(client, "Foundry-Kilo-Sol", "rev2").status_code == 200   # new run
    _bind(client, "g2a", "game-two", "U1", "run-c")
    assert _kilo_chat(client, "Foundry-Kilo-Sol", "g2a").status_code == 200   # 3rd codex call
    r = _kilo_chat(client, "Foundry-Kilo-Sol", "g2a")
    assert r.status_code == 429 and "codex-shared" in r.json()["error"]["message"]
    # local calls aren't on the codex account: game-two's own cap applies
    assert _kilo_chat(client, "Foundry-Kilo-Local", "g2a").status_code == 200
    r = _kilo_chat(client, "Foundry-Kilo-Local", "g2a")
    assert r.status_code == 429 and "game-two-cap" in r.json()["error"]["message"]
    d = client.get("/v1/foundry/work/denials?run=run-a", headers=COORD).json()["denials"]
    assert d and d[0]["rule"] == "review-per-run"


def test_pause_and_route_refusals_are_categorised(ledgered, client):
    ledgered.ledger.set_pause("persona", "Foundry-Kilo-Sol", True)
    r = _kilo_chat(client, "Foundry-Kilo-Sol", "s1")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "paused" and r.json()["error"]["foundry_category"] == "pause"
    ledgered.ledger.set_pause("persona", "Foundry-Kilo-Sol", False)
    ledgered.personas.upsert("Foundry-Kilo-Gone", execution_mode="direct", routing="strict",
                             backend="cliproxy", model_allowlist=["gpt-9"], pinned_models=["gpt-9"])
    r = _kilo_chat(client, "Foundry-Kilo-Gone", "s1")
    assert r.status_code == 503 and r.json()["error"]["foundry_category"] == "route"
    assert HITS == [("cliproxy", "gpt-6.1-sol")] or all(h[1] != "gpt-9" for h in HITS)
    ledgered.personas.upsert("Foundry-Kilo-Off", execution_mode="direct", routing="strict",
                             enabled=0, model_allowlist=["qwen-local"])
    r = _kilo_chat(client, "Foundry-Kilo-Off", "s1")
    assert r.status_code == 403 and r.json()["error"]["foundry_category"] == "config"


def test_coordinator_state_and_events_show_in_work_view(ledgered, client):
    _setup_coord(ledgered)
    _bind(client, "ses_A", "animaluprising", "UI-02", "run-1")
    _kilo_chat(client, "Foundry-Kilo-Local", "ses_A")
    assert client.put("/v1/foundry/work/tasks/animaluprising/UI-02", headers=COORD, json={
        "state": "VERIFYING", "attempt": 1, "commit_sha": "abc123"}).status_code == 200
    ev = [{"event_id": "e1", "project_id": "animaluprising", "task_id": "UI-02",
           "session_id": "ses_A", "kind": "transition", "state": "VERIFYING"}]
    assert client.post("/v1/foundry/work/events", headers=COORD, json={"events": ev}).json()[
        "recorded"] == 1
    assert client.post("/v1/foundry/work/events", headers=COORD, json={"events": ev}).json()[
        "recorded"] == 0                                # resend is deduped
    v = client.get("/admin/api/work").json()
    assert v["projects"] == ["animaluprising"]
    assert v["tasks"][0]["state"] == "VERIFYING" and v["tasks"][0]["data"]["commit_sha"] == "abc123"
    s = next(x for x in v["sessions"] if x["session_id"] == "ses_A")
    assert s["usage"]["calls"] == 1 and "strata/qwen-local" in s["usage"]["routes"]
    assert v["events"][0]["kind"] == "transition"
    assert "not visible to Foundry" in v["note"]


def test_strict_persona_turn_assist_frontier_helpers_stay_off(ledgered, client):
    """A strict local persona with an advisor configured must not consult a
    paid frontier model (another subscription) mid-turn."""
    ledgered.personas.upsert("Foundry-Kilo-Local", assist=json.dumps({
        "advisor": True, "frontier_models": ["gpt-6-astra"], "auditor": "on",
        "dedupe_reads": True}))
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}}}}]
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer kk"}, json={
        "model": "Foundry-Kilo-Local", "stream": False, "tools": tools,
        "messages": [{"role": "user", "content": "done?"}]})
    assert r.status_code == 200
    assert all(b == "strata" for b, _ in HITS), HITS
    sent = [t["function"]["name"] for t in STATE["last_body"].get("tools") or []]
    assert "read_file" in sent and not any("consult" in n for n in sent), sent
    # with the explicit opt-in the advisor tool is offered again
    ledgered.personas.upsert("Foundry-Kilo-Local", assist=json.dumps({
        "advisor": True, "frontier_models": ["gpt-6-astra"], "strict_frontier": True}))
    client.post("/v1/chat/completions", headers={"Authorization": "Bearer kk"}, json={
        "model": "Foundry-Kilo-Local", "stream": False, "tools": tools,
        "messages": [{"role": "user", "content": "done?"}]})
    sent = [t["function"]["name"] for t in STATE["last_body"].get("tools") or []]
    assert any("consult" in n for n in sent), sent
