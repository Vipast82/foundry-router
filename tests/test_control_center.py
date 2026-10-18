"""Coordinator control center, Foundry side (control_center.py, control_api.py):
operator sessions + CSRF, scopes and project isolation, redaction before
storage/broadcast/search, idempotent observations, alerts that acknowledge
without resolving, typed actions that are idempotent and refuse stale
targets, claim/report, watchdog expiry, and the causal route graph."""

import json
import time

import pytest

from foundry_router.config import ClientConfig
from foundry_router.control_center import CONTRACT

OP_KEY = "op-key-123456789"
OP_B_KEY = "op-b-key-123456789"
COORD_KEY = "coord-key-123456789"
BRIDGE_A_KEY = "bridge-a-key-123456"
INF_KEY = "inference-key-12345"
COORD = {"Authorization": f"Bearer {COORD_KEY}"}
SECRET_TOOL_OUTPUT = (
    "curl -H 'Authorization: Bearer abcdefghijklmnop123456' https://x\n"
    "OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA\n"
    "password=hunter2hunter2\n"
    "<script>alert('xss')</script><img src=x onerror=alert(1)>")
SECRET_BYTES = ("abcdefghijklmnop123456", "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA", "hunter2hunter2")


@pytest.fixture()
def cc(app):
    svc = app.state.services
    svc.config_store.config.clients.extend([
        ClientConfig(name="victor", key=OP_KEY, scopes=["operator"]),
        ClientConfig(name="op-b", key=OP_B_KEY, scopes=["operator"], projects=["other"]),
        ClientConfig(name="coordinator", key=COORD_KEY, scopes=["inference", "orchestration"]),
        ClientConfig(name="bridge-a", key=BRIDGE_A_KEY, scopes=["telemetry"], projects=["proj"]),
        ClientConfig(name="kilo", key=INF_KEY, scopes=["inference"]),
    ])
    return svc.control


def login(client, key=OP_KEY):
    r = client.post("/control/api/login", json={"key": key})
    assert r.status_code == 200, r.text
    return {"x-foundry-csrf": r.json()["csrf"]}


def batch(*obs):
    return {"contract": CONTRACT, "source": {"id": "bridge:test", "kind": "bridge"},
            "sent_ts": time.time(), "observations": list(obs)}


def task_obs(state="IMPLEMENTING", session="ses_1", **kw):
    return {"type": "task", "project_id": "proj", "task_id": "T-1", "state": state,
            "session_id": session, "run_id": "T-1-a1", "attempt": 1, "phase": "implement", **kw}


def ingest(client, *obs, headers=COORD):
    r = client.post("/v1/foundry/telemetry/batch", headers=headers, json=batch(*obs))
    assert r.status_code == 200, r.text
    return r.json()


def question(rid="que_1", session="ses_1", resolved=False):
    o = {"type": "pending", "kind": "question", "request_id": rid, "session_id": session,
         "project_id": "proj", "task_id": "T-1",
         "questions": [{"question": "Which frame size?", "options": [{"label": "390x844"},
                                                                     {"label": "1920x1080"}]}]}
    if resolved:
        o["resolved"] = True
    return o


# ------------------------------------------------------------------ auth

def test_operator_login_cookie_csrf_and_scopes(client, cc):
    assert client.get("/control/api/overview").status_code == 401
    assert client.post("/control/api/login", json={"key": INF_KEY}).status_code == 401
    assert client.post("/control/api/login", json={"key": "nope"}).status_code == 401
    r = client.post("/control/api/login", json={"key": OP_KEY})
    assert r.status_code == 200 and "token" not in r.json()
    ck = r.headers["set-cookie"].lower()
    assert "httponly" in ck and "samesite=strict" in ck and "path=/control" in ck
    assert OP_KEY not in r.text
    assert client.get("/control/api/overview").status_code == 200
    h = {"x-foundry-csrf": r.json()["csrf"]}
    # mutation without the CSRF header, with a wrong one, cross-origin, cross-site
    body = {"kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "x",
            "idempotency_key": "k-123456789"}
    assert client.post("/control/api/actions", json=body).status_code == 403
    assert client.post("/control/api/actions", json=body,
                       headers={"x-foundry-csrf": "wrong"}).status_code == 403
    assert client.post("/control/api/actions", json=body,
                       headers={**h, "origin": "http://evil.example"}).status_code == 403
    assert client.post("/control/api/actions", json=body,
                       headers={**h, "sec-fetch-site": "cross-site"}).status_code == 403
    # session info returns the CSRF token for a page reload (same-origin only)
    assert client.get("/control/api/session").json()["actor"] == "victor"


def test_telemetry_and_claim_need_their_scopes(client, cc):
    b = batch(task_obs())
    assert client.post("/v1/foundry/telemetry/batch", json=b).status_code == 401
    assert client.post("/v1/foundry/telemetry/batch", json=b,
                       headers={"Authorization": f"Bearer {INF_KEY}"}).status_code == 403
    assert client.post("/v1/foundry/telemetry/batch", json=b,
                       headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"}).status_code == 200
    # a telemetry key can't claim operator actions (orchestration only)
    r = client.post("/v1/foundry/control/claim", json={"source_id": "x", "projects": ["proj"]},
                    headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"})
    assert r.status_code in (401, 403)
    # an operator browser session is not a telemetry credential
    login(client)
    assert client.post("/v1/foundry/telemetry/batch", json=b).status_code == 401


def test_project_isolation(client, cc):
    ingest(client, task_obs(), {"type": "task", "project_id": "other", "task_id": "O-1",
                                "state": "IMPLEMENTING"})
    # a bridge limited to "proj" can't write "other"
    r = client.post("/v1/foundry/telemetry/batch",
                    headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json=batch({"type": "task", "project_id": "other", "task_id": "O-2",
                                "state": "FAILED"}))
    assert r.json()["skipped"] == {"project_not_allowed": 1}
    h = login(client, OP_B_KEY)
    ov = client.get("/control/api/overview").json()
    assert {t["project_id"] for t in ov["tasks"]} == {"other"}
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "x",
        "idempotency_key": "iso-12345678"})
    assert r.status_code == 403


# ------------------------------------------------------------------ redaction / XSS

def test_redaction_before_storage_broadcast_search(client, cc, app):
    ingest(client, task_obs(),
           {"type": "session", "id": "ses_1", "project_id": "proj", "task_id": "T-1",
            "status": {"value": "busy", "source": "test"}},
           {"type": "part", "id": "prt_tool", "message_id": "msg_1", "session_id": "ses_1",
            "project_id": "proj", "kind": "tool", "ts": time.time(),
            "tool": {"name": "bash", "status": "completed", "input": {"command": SECRET_TOOL_OUTPUT},
                     "output": SECRET_TOOL_OUTPUT}},
           {"type": "part", "id": "prt_txt", "message_id": "msg_1", "session_id": "ses_1",
            "project_id": "proj", "kind": "text", "text": "done. " + SECRET_TOOL_OUTPUT})
    db = app.state.services.db
    stored = json.dumps(db.query("SELECT * FROM cc_parts"))
    for s in SECRET_BYTES:
        assert s not in stored
    assert "[REDACTED" in stored
    login(client)
    for url in ("/control/api/sessions/ses_1/parts", "/control/api/sessions/ses_1",
                "/control/api/search?q=curl"):
        body = client.get(url).text
        for s in SECRET_BYTES:
            assert s not in body, url
    items, _ = cc.feed(0, [], ["ses_1"])
    assert all(s not in json.dumps(items) for s in SECRET_BYTES)
    # untrusted HTML is stored as text; the page escapes it (test_control_ui.py)
    assert "<script>" in client.get("/control/api/sessions/ses_1/parts").json()["parts"][1]["text"]


def test_reasoning_collection_switch_is_honest(client, cc, app):
    h = login(client)
    r = client.put("/control/api/settings", headers=h, json={"collect_reasoning": False})
    assert r.json()["settings"]["collect_reasoning"] is False
    ingest(client, {"type": "part", "id": "prt_r", "message_id": "m", "session_id": "ses_1",
                    "project_id": "proj", "kind": "reasoning", "text": "secret plan"})
    row = app.state.services.db.query_one("SELECT text, data FROM cc_parts WHERE part_id='prt_r'")
    assert row["text"] == "" and "collection is off" in row["data"]
    assert client.put("/control/api/settings", headers=h,
                      json={"retention_days": 0}).status_code == 400


def test_observations_idempotent_and_unknown_types_safe(client, cc, app):
    p = {"type": "part", "id": "prt_1", "message_id": "m", "session_id": "ses_1",
         "project_id": "proj", "kind": "text", "text": "a"}
    ingest(client, p, p, {**p, "text": "ab"})
    r = ingest(client, {"type": "future.thing", "project_id": "proj", "x": 1})
    assert r["skipped"] == {"unknown:future.thing": 1}
    rows = app.state.services.db.query("SELECT text FROM cc_parts")
    assert rows == [{"text": "ab"}]
    items, last = cc.feed(0, [], ["ses_1"])
    assert [i["id"] for i in items if i["kind"] == "part"] == ["prt_1"]   # coalesced
    assert cc.feed(last, [], ["ses_1"])[0] == []                           # nothing new
    # parts of an unsubscribed session aren't streamed
    assert not [i for i in cc.feed(0, [], [])[0] if i["kind"] == "part"]


def test_contract_mismatch_refused(client, cc):
    r = client.post("/v1/foundry/telemetry/batch", headers=COORD,
                    json={"contract": "foundry.cc.v0", "observations": []})
    assert r.status_code == 400 and r.json()["code"] == "contract_mismatch"


# ------------------------------------------------------------------ alerts + answers

def _ask(client):
    ingest(client, task_obs("AWAITING_HUMAN", question={"request_id": "que_1"}), question())


def test_question_alert_ack_answer_dedupe_and_stale(client, cc):
    _ask(client)
    _ask(client)                                       # same event again: no second alert
    h = login(client)
    al = client.get("/control/api/alerts").json()["alerts"]
    assert len(al) == 1 and al[0]["kind"] == "question" and al[0]["severity"] == "critical"
    # acknowledging answers nothing
    client.post(f"/control/api/alerts/{al[0]['alert_id']}/ack", headers=h)
    a = cc.alert(al[0]["alert_id"])
    assert a["status"] == "acknowledged"
    assert cc.db.query_one("SELECT status FROM cc_pending WHERE request_id='que_1'")["status"] == "open"
    assert cc.actions([]) == []
    body = {"kind": "answer_question", "project_id": "proj", "task_id": "T-1",
            "request_id": "que_1", "session_id": "ses_1",
            "answers": [["Use 390x844 first.\n\nThen, with a longer explanation:\n- a\n- b"]],
            "expected": {"task_state": "AWAITING_HUMAN"}, "idempotency_key": "tab1-abcdef12"}
    r1 = client.post("/control/api/actions", headers=h, json=body)
    assert r1.status_code == 200 and r1.json()["status"] == "accepted"
    # retry of the same submission: same action, no duplicate
    r1b = client.post("/control/api/actions", headers=h, json=body)
    assert r1b.json()["action_id"] == r1.json()["action_id"] and r1b.json()["duplicate"]
    # a second tab answering the same request: refused
    r2 = client.post("/control/api/actions", headers=h, json={**body, "idempotency_key": "tab2-abcdef12"})
    assert r2.status_code == 409 and r2.json()["code"] == "duplicate_reply"
    # the audit summary doesn't hold the answer text
    assert "390x844" not in json.dumps(client.get("/control/api/actions").json())
    # coordinator claims it (with the real answer), applies, reports
    got = client.post("/v1/foundry/control/claim", headers=COORD,
                      json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"]
    assert got[0]["payload"]["answers"][0][0].startswith("Use 390x844")
    assert client.post("/v1/foundry/control/claim", headers=COORD,
                       json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"] == []
    r = client.post(f"/v1/foundry/control/actions/{got[0]['action_id']}/status", headers=COORD,
                    json={"source_id": "coordinator:ws", "status": "applied", "detail": "ok"})
    assert r.json()["status"] == "applied"
    assert cc.alert(al[0]["alert_id"])["status"] == "answered"
    # a stale request (already answered / gone) is refused with where to go now
    ingest(client, question(resolved=True), question("que_2"))
    r3 = client.post("/control/api/actions", headers=h, json={**body, "idempotency_key": "tab3-abcdef12",
                                                               "expected": {"task_state": "AWAITING_HUMAN"}})
    assert r3.status_code == 409 and r3.json()["code"] in ("stale_request", "duplicate_reply")


def test_stale_task_state_and_cancel_before_claim(client, cc):
    ingest(client, task_obs("IMPLEMENTING"))
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "maintenance",
        "expected": {"task_state": "VERIFYING"}, "idempotency_key": "stale-1234567"})
    assert r.status_code == 409 and r.json()["current"] == "IMPLEMENTING"
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "cancel", "project_id": "proj", "task_id": "T-1", "reason": "x",
        "idempotency_key": "cancel-1234567"})
    assert r.status_code == 400 and "cancel T-1" in r.json()["error"]
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "maintenance",
        "idempotency_key": "pause-1234567"})
    aid = r.json()["action_id"]
    c = client.post(f"/control/api/actions/{aid}/cancel", headers=h)
    assert c.json()["status"] == "cancelled"
    assert client.post("/v1/foundry/control/claim", headers=COORD, json={
        "source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"] == []


def test_permission_reply_never_creates_always_rule(client, cc):
    ingest(client, task_obs("AWAITING_HUMAN"),
           {"type": "pending", "kind": "permission", "request_id": "per_1", "session_id": "ses_1",
            "project_id": "proj", "task_id": "T-1", "permission": "bash", "patterns": ["git push"]})
    h = login(client)
    base = {"kind": "reply_permission", "project_id": "proj", "task_id": "T-1",
            "request_id": "per_1", "idempotency_key": "perm-1234567"}
    assert client.post("/control/api/actions", headers=h, json={**base, "reply": "always"}).status_code == 400
    # a question action can't target a permission request
    assert client.post("/control/api/actions", headers=h, json={
        **base, "kind": "answer_question", "answers": [["yes"]]}).status_code == 404
    assert client.post("/control/api/actions", headers=h, json={**base, "reply": "once"}).status_code == 200


def test_approval_bound_to_revision_and_artifacts(client, cc):
    ap = {"kinds": ["visual"], "stage": "after_close", "revision": "abc123",
          "artifacts": {"visual": {"docs/evidence/a.png": "h1"}}}
    ingest(client, task_obs("AWAITING_HUMAN", approval=ap))
    h = login(client)
    al = [a for a in client.get("/control/api/alerts").json()["alerts"] if a["kind"] == "approval"]
    assert al and al[0]["severity"] == "critical"
    body = {"kind": "approve_review", "project_id": "proj", "task_id": "T-1",
            "approval_kind": "visual", "revision": "abc123",
            "artifacts": {"docs/evidence/a.png": "h1"}, "idempotency_key": "apr-12345678"}
    stale = client.post("/control/api/actions", headers=h, json={
        **body, "artifacts": {"docs/evidence/a.png": "OLD"}, "idempotency_key": "apr-stale-1234"})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_evidence"
    assert client.post("/control/api/actions", headers=h, json={
        **body, "kind": "reject_review", "idempotency_key": "rej-1234567"}).status_code == 400
    assert client.post("/control/api/actions", headers=h, json=body).status_code == 200


def test_instruction_rules(client, cc):
    ingest(client, task_obs("VERIFYING"))
    h = login(client)
    base = {"kind": "instruction", "project_id": "proj", "task_id": "T-1", "text": "Also check X",
            "idempotency_key": "ins-12345678"}
    r = client.post("/control/api/actions", headers=h, json={**base, "delivery": "pause_and_steer"})
    assert r.status_code == 409                              # nothing running to steer
    r = client.post("/control/api/actions", headers=h, json={**base, "scope": "session"})
    assert r.status_code == 400                              # session-scoped needs explicit steer
    r = client.post("/control/api/actions", headers=h, json=base)
    assert r.status_code == 200 and r.json()["status"] == "accepted"


def test_rate_limit(client, cc):
    ingest(client, task_obs("IMPLEMENTING"))
    h = login(client)
    cc.update_settings({"rate_per_minute": 2}, "t")
    codes = [client.post("/control/api/actions", headers=h, json={
        "kind": "instruction", "project_id": "proj", "task_id": "T-1", "text": f"n{i}",
        "idempotency_key": f"rate-{i}-123456"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]


# ------------------------------------------------------------------ watchdog / sources

def test_watchdog_notices_silence_without_the_coordinator(client, cc):
    t = [1000.0]
    cc.clock = lambda: t[0]
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": "coordinator:ws", "kind": "coordinator"}, "sent_ts": t[0]})
    assert cc.watchdog() == []
    t[0] += 500                                          # the coordinator just stopped
    assert cc.watchdog() == ["stale:coordinator:ws"]
    al = [a for a in cc.alerts([], "open") if a["kind"] == "source_stale"]
    assert len(al) == 1 and "coordinator" in al[0]["title"]
    cc.watchdog()                                        # no duplicate alert
    assert len([a for a in cc.alerts([], "open") if a["kind"] == "source_stale"]) == 1
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": "coordinator:ws", "kind": "coordinator"}, "sent_ts": t[0]})
    assert [a for a in cc.alerts([], "open") if a["kind"] == "source_stale"] == []


def test_bridge_reports_kilo_down_and_stale_engine_distinctly(client, cc):
    now = time.time()
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": "bridge:ws", "kind": "bridge"}, "kilo": {"ok": False, "error": "refused"},
        "coordinator_owner": {"present": True, "id": "ws:1", "beat_ts": now - 3600}})
    kinds = {a["dedupe_key"].split(":")[0] for a in cc.alerts([], "open")}
    assert kinds == {"kilo_unreachable", "owner_stale"}


def test_unclaimed_actions_expire_and_are_never_sent(client, cc):
    ingest(client, task_obs("IMPLEMENTING"))
    h = login(client)
    aid = client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "x",
        "idempotency_key": "exp-12345678"}).json()["action_id"]
    real = cc.clock
    cc.clock = lambda: real() + 99999
    cc.watchdog()
    assert cc.action_view(aid)["status"] == "expired"
    assert client.post("/v1/foundry/control/claim", headers=COORD, json={
        "source_id": "c", "projects": ["proj"]}).json()["actions"] == []


# ------------------------------------------------------------------ route graph / passivity

def test_route_graph_uses_causal_ids_only(client, cc, app):
    db = app.state.services.db
    ingest(client, {"type": "session", "id": "ses_1", "project_id": "proj", "task_id": "T-1",
                    "role": "implementer", "status": {"value": "busy"},
                    "last_activity_ts": time.time()},
           {"type": "part", "id": "prt_m", "message_id": "m", "session_id": "ses_1",
            "project_id": "proj", "kind": "tool", "ts": time.time(),
            "tool": {"name": "Roblox_Studio_run", "status": "running", "mcp_server": "Roblox_Studio"}})
    db.execute("INSERT INTO request_log (ts, persona, request_id, session_id, served_backend, "
               "served_model, status, outcome, attempts, models_used) VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("2026-10-17T00:00:00Z", "Foundry-Kilo-Planner", "req-1", "ses_1", "cliproxyapi",
                "gpt-6-astra", "ok", "ok", 2, json.dumps([
                    {"model": "claude-opus-5-5", "backend": "meridian", "error": "usage window"},
                    {"model": "gpt-6-astra", "backend": "cliproxyapi"}])))
    db.execute("INSERT INTO usage_events (ts, request_id, session_id, persona, backend, model, "
               "status) VALUES (?,?,?,?,?,?,?)", (time.time(), "req-2", "ses_1",
                                                   "Foundry-Kilo-Local", "strata", "qwen", "reserved"))
    db.execute("INSERT INTO media_jobs (id, created_ts, op, status, session_id) VALUES (?,?,?,?,?)",
               ("img_aaaa", time.time(), "generate", "running", "ses_1"))
    db.execute("INSERT INTO media_jobs (id, created_ts, op, status, session_id) VALUES (?,?,?,?,?)",
               ("img_bbbb", time.time(), "generate", "succeeded", ""))
    login(client)
    g = client.get("/control/api/graph?project=proj").json()
    edges = {e["id"]: e for e in g["edges"]}
    assert edges["p:Foundry-Kilo-Planner->b:cliproxyapi"]["state"] == "fallback"
    assert edges["p:Foundry-Kilo-Planner->b:meridian"]["state"] == "failed"
    assert edges["s:ses_1->p:Foundry-Kilo-Local"]["state"] == "active"
    assert edges["s:ses_1->mcp:Roblox_Studio"]["kind"] == "tool"
    assert edges["s:ses_1->j:img_aaaa"]["state"] == "active"
    assert [j["id"] for j in g["unassociated_media"]] == ["img_bbbb"]


def test_viewing_changes_nothing(client, cc, app):
    _ask(client)
    db = app.state.services.db
    before = (db.query_one("SELECT COUNT(*) n FROM request_log")["n"],
              db.query_one("SELECT COUNT(*) n FROM cc_actions")["n"])
    login(client)
    for url in ("/control/api/overview", "/control/api/alerts", "/control/api/graph",
                "/control/api/sessions/ses_1/parts", "/control/api/actions",
                "/control/api/capabilities", "/control/api/snapshots?kind=mcp&project=proj",
                "/ui/control"):
        assert client.get(url).status_code in (200, 404), url
    after = (db.query_one("SELECT COUNT(*) n FROM request_log")["n"],
             db.query_one("SELECT COUNT(*) n FROM cc_actions")["n"])
    assert before == after


def test_snapshot_history_is_dated_not_today(client, cc):
    t = [1000.0]
    cc.clock = lambda: t[0]
    mcp = {"type": "mcp", "project_id": "proj", "scope": "workspace", "observed_ts": 1000.0,
           "servers": [{"name": "Roblox_Studio", "configured_enabled": True, "status": "connected"}]}
    ingest(client, mcp)
    t[0] = 5000.0
    ingest(client, {**mcp, "observed_ts": 5000.0, "servers": [
        {"name": "Roblox_Studio", "configured_enabled": True, "status": "failed"}]})
    old = cc.snapshot("mcp", "proj", at=2000.0)
    assert old["data"]["servers"][0]["status"] == "connected" and old["source_ts"] == 1000.0
    assert cc.snapshot("mcp", "proj", at=500.0) is None      # before capture began: unavailable
    assert cc.snapshot("mcp", "proj")["data"]["servers"][0]["status"] == "failed"


def test_lanes_action_is_project_wide_and_skips_the_stale_state_check(client, cc):
    """'Pause proactive lanes' routes through any task of the project; the task's
    state is irrelevant, so a stale expected.task_state never refuses it."""
    ingest(client, task_obs("IMPLEMENTING"))
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "lanes", "project_id": "proj", "task_id": "T-1", "paused": True,
        "expected": {"task_state": "VERIFYING"}, "idempotency_key": "lanes-1234567"})
    assert r.status_code == 200, r.text
    r2 = client.post("/control/api/actions", headers=h, json={
        "kind": "lanes", "project_id": "proj", "task_id": "T-1", "paused": False,
        "idempotency_key": "lanes-2345678"})
    assert r2.status_code == 200, r2.text


def test_thumbnail_malformed_base64_is_404_and_image_mimes_pass(client, cc):
    import base64
    good = base64.b64encode(b"RIFF....WEBPVP8 ").decode()
    ingest(client,
           {"type": "artifact", "project_id": "proj", "task_id": "T-1", "artifact_id": "art-bad",
            "rel_path": "docs/evidence/T-1/a.png", "generation": 1, "sha256": "a" * 64,
            "status": "current", "mime": "image/png", "thumbnail_b64": "@@not*base64@@"},
           {"type": "artifact", "project_id": "proj", "task_id": "T-1", "artifact_id": "art-webp",
            "rel_path": "docs/evidence/T-1/b.webp", "generation": 1, "sha256": "b" * 64,
            "status": "current", "mime": "image/webp", "thumbnail_b64": good},
           {"type": "artifact", "project_id": "proj", "task_id": "T-1", "artifact_id": "art-html",
            "rel_path": "docs/evidence/T-1/c.png", "generation": 1, "sha256": "c" * 64,
            "status": "current", "mime": "text/html", "thumbnail_b64": good})
    login(client)
    bad = client.get("/control/api/artifacts/art-bad/thumbnail")
    assert bad.status_code == 404 and "unreadable" in bad.json()["error"]
    ok = client.get("/control/api/artifacts/art-webp/thumbnail")
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("image/webp")
    assert ok.content == b"RIFF....WEBPVP8 "
    html = client.get("/control/api/artifacts/art-html/thumbnail")
    assert html.headers["content-type"].startswith("application/octet-stream")
