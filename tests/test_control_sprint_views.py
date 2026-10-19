"""Control center, v0.122 sprint-management views (Foundry side): the per-task
view (readiness, required work, evidence, recovered results, findings,
budgets, decisions) with absent fields reported as absent, batch-id
idempotency and no feed churn on re-sends, the version-compatibility gate on
heartbeats (and claim refusing a refused source), the project-isolation fixes,
and the `decision_answer` action (authority always operator, actor from the
session)."""

import json
import time

import pytest

from foundry_router.control_center import CONTRACT, compat_state, helper_phase
from tests.test_control_center import (BRIDGE_A_KEY, COORD, OP_B_KEY, batch, cc, ingest,  # noqa: F401
                                       login, task_obs)


def lifecycle(**kw):
    """The task fields a v0.122 bridge sends (telemetry._lifecycle)."""
    base = {
        "lifecycle_tables": ["ev_records", "obligations", "decisions", "budget_ledger"],
        "readiness": [{"name": "au.rojo.serving", "ok": False, "cls": "environment",
                       "detail": "rojo not reachable on 34872", "blocking": True},
                      {"name": "au.mcp.studio", "ok": None, "cls": "environment",
                       "detail": "/mcp unsupported", "blocking": False}],
        "blocker_class": "environment",
        "obligations": [{"id": "build.gate", "phase": "BUILD", "title": "Gate passes at HEAD",
                         "status": "open", "blocking_reason": "gate not run at this revision",
                         "next_action": "run tools/gate.ps1", "required_evidence": [
                             {"kind": "check", "ref": "gate"}]},
                        {"id": "close.state", "phase": "CLOSE", "title": "STATE.md row",
                         "status": "verified", "verified_rev": "a" * 40}],
        "evidence": {"counts": {"reusable": 1, "stale": 1, "missing": 1, "disputed": 0,
                                "excluded": 0},
                     "checks": [{"check_id": "lune", "state": "reusable", "reason": "fresh verified PASS",
                                 "refs": {"session_id": "ses_old", "part_id": "prt_9", "call_id": "c9",
                                          "tool": "bash", "output_sha": "f" * 64}},
                                {"check_id": "gate", "state": "stale", "reason": "src/a.luau changed"},
                                {"check_id": "studio", "state": "missing", "reason": "no recorded result"}],
                     "chains": []},
        "recovered": [{"kind": "evidence", "check_id": "lune", "result": "PASS", "tier": "verified",
                       "ts": 100.0, "detail": "recovered from session ses_old"}],
        "finding_history": {"findings": [{"fingerprint": "fp1", "class": "product_defect",
                                          "check_id": "gate", "count": 3, "sample": "S45 timing"}],
                            "repeated": 1, "repairs": [], "approach_changed": False},
        "budgets": {"counters": {"ordinary": {"used": 3, "max": 3, "source": "budget_ledger"},
                                 "guided_extra": {"used": 0, "max": 1, "source": "retry_guidance",
                                                  "explained": "retry guidance granted one extra"}},
                    "authorized": [], "ledger": {}, "source": "budget_ledger"},
        "decisions": [{"decision_id": "dec-1", "kind": "scope_change", "status": "open",
                       "question": "Allow edits to <src/b.luau>?", "options": ["allow", "refuse"],
                       "recommendation": "refuse", "blocks": ["build.gate"], "opened_ts": 50.0},
                      {"decision_id": "dec-0", "kind": "budget_extension", "status": "answered",
                       "question": "Extra attempt?", "answer": "yes", "authority": "operator",
                       "actor": "victor"}],
    }
    base.update(kw)
    return base


def view(client, tid="T-1", pid="proj"):
    r = client.get(f"/control/api/tasks/{pid}/{tid}/view")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ task view

def test_task_view_endpoint_and_overview_carry_the_sprint_fields(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="rojo is not serving", **lifecycle()))
    login(client)
    v = view(client)["view"]
    assert v["readiness"]["failing"] == 1 and v["readiness"]["unknown"] == 1
    assert v["blocker_class"] == "environment"
    assert v["checklist"]["open"] == 1 and v["checklist"]["total"] == 2
    assert v["checklist"]["next_action"] == "run tools/gate.ps1"
    assert v["checklist"]["by_phase"] == {"BUILD": {"total": 1, "open": 1},
                                          "CLOSE": {"total": 1, "open": 0}}
    assert v["evidence"]["counts"]["stale"] == 1
    assert v["evidence"]["checks"][0]["refs"]["part_id"] == "prt_9"
    assert v["recovered"][0]["check_id"] == "lune"
    assert v["findings"]["items"][0]["count"] == 3 and v["findings"]["approach_changed"] is False
    assert v["budgets"]["counters"]["guided_extra"]["max"] == 1
    assert [d["decision_id"] for d in v["decisions"]["open"]] == ["dec-1"]
    assert [d["decision_id"] for d in v["decisions"]["closed"]] == ["dec-0"]
    t = client.get("/control/api/overview").json()["tasks"][0]
    assert t["task_view"]["decisions"]["open"][0]["decision_id"] == "dec-1"
    # the live feed carries it too
    items, _ = cc.feed(0, [])
    assert [i for i in items if i["kind"] == "task"][-1]["entity"]["task_view"]["readiness"]["failing"] == 1


def test_task_view_says_not_reported_instead_of_inventing(client, cc):
    """A pre-0.122 bridge (or lifecycle off) sends none of the fields: the view
    lists nothing as reported and every list is empty, never guessed."""
    ingest(client, task_obs("IMPLEMENTING"))
    login(client)
    v = view(client)["view"]
    assert v["reported"] == [] and v["tables"] == []
    assert v["readiness"]["items"] == [] and v["checklist"]["items"] == []
    assert v["evidence"]["checks"] == [] and v["evidence"]["counts"] == {}
    assert v["budgets"] is None and v["decisions"] == {"open": [], "closed": []}
    # a bridge that read the tables but found them empty/missing: reported, empty
    ingest(client, task_obs("IMPLEMENTING", lifecycle_tables=[], readiness=[], obligations=[],
                            evidence={"counts": {}, "checks": [], "chains": []}, recovered=[],
                            finding_history={"findings": [], "repairs": []}, decisions=[],
                            budgets={"counters": {}}))
    v = view(client)["view"]
    assert set(v["reported"]) == {"readiness", "obligations", "evidence", "recovered",
                                  "finding_history", "budgets", "decisions"}
    assert v["checklist"]["total"] == 0 and v["decisions"]["open"] == []


def test_task_view_isolation_and_unknown_task(client, cc):
    ingest(client, task_obs(**lifecycle()))
    login(client, OP_B_KEY)                              # restricted to "other"
    assert client.get("/control/api/tasks/proj/T-1/view").status_code == 404
    assert client.get("/control/api/tasks/proj/T-1/helpers").status_code == 404
    login(client)
    assert client.get("/control/api/tasks/proj/NOPE/view").status_code == 404


def test_environment_blocker_is_a_readiness_warning_not_a_blocked_alert(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="rojo is not serving", **lifecycle()))
    al = {a["kind"]: a for a in cc.alerts([], "open")}
    assert "readiness" in al and al["readiness"]["severity"] == "warning"
    assert "blocker" not in al
    # a product/scope blocker keeps the high "blocked" alert
    ingest(client, {**task_obs("BLOCKED", blocker="scope violation"), "task_id": "T-2"})
    assert any(a["kind"] == "blocker" and a["task_id"] == "T-2" for a in cc.alerts([], "open"))
    # leaving BLOCKED resolves the readiness alert (same dedupe prefix)
    ingest(client, task_obs("READY", **lifecycle(blocker_class=None)))
    assert not [a for a in cc.alerts([], "open") if a["kind"] == "readiness"]


def test_decision_alerts_are_deduped_and_resolve_when_answered(client, cc):
    ingest(client, task_obs(**lifecycle()))
    ingest(client, task_obs(**lifecycle()))
    dec = [a for a in cc.alerts([], "open") if a["kind"] == "decision"]
    assert len(dec) == 1 and dec[0]["dedupe_key"] == "decision:proj:T-1:dec-1"
    closed = lifecycle()
    closed["decisions"][0] = {**closed["decisions"][0], "status": "answered"}
    ingest(client, task_obs(**closed))
    assert cc.alert(dec[0]["alert_id"])["status"] == "answered"


def test_readiness_detail_secrets_are_redacted_on_the_server(client, cc, app):
    secret = "Bearer abcdefghijklmnop123456"
    ingest(client, task_obs("BLOCKED", **lifecycle(readiness=[
        {"name": "au.creds", "ok": False, "cls": "credentials", "blocking": True,
         "detail": f"curl -H 'Authorization: {secret}' failed"}])))
    stored = json.dumps(app.state.services.db.query("SELECT data FROM cc_tasks"))
    assert "abcdefghijklmnop123456" not in stored and "[REDACTED" in stored
    login(client)
    assert "abcdefghijklmnop123456" not in client.get("/control/api/tasks/proj/T-1/view").text


def test_task_helpers_endpoint_gives_phase_and_disposition(client, cc):
    ingest(client, task_obs(helpers=[
        {"unit_id": "hu-1", "kind": "debug_advice", "status": "failed", "reason_code": "execution_timeout"},
        {"unit_id": "hu-2", "kind": "plan", "status": "delivered", "feedback": None},
        {"unit_id": "hu-3", "kind": "plan", "status": "deferred", "next_eligible": 99.0},
        {"unit_id": "hu-4", "kind": "plan", "status": "failed", "reason_code": "needs_input"}]))
    login(client)
    h = client.get("/control/api/tasks/proj/T-1/helpers").json()
    ph = {u["unit_id"]: u["phase"] for u in h["units"]}
    # helpers.PHASES vocabulary (waiting_provider, cancelled), not a server dialect
    assert ph == {"hu-1": "timed_out", "hu-2": "completed", "hu-3": "waiting_provider",
                  "hu-4": "error"}
    assert "failed" not in ph.values()
    assert h["disposition_pending"] == ["hu-2"]
    assert helper_phase({"status": "running", "phase": "admitted"}) == "admitted"


def test_helper_phase_vocabulary_matches_the_coordinator():
    from foundry_coordinator.helpers import PHASES
    # the coordinator's reason code is `timeout` (execution_timeout: legacy spelling)
    assert helper_phase({"status": "failed", "reason_code": "timeout"}) == "timed_out"
    assert helper_phase({"status": "failed", "reason_code": "execution_timeout"}) == "timed_out"
    assert helper_phase({"status": "failed", "reason_code": "handoff_timeout"}) == "timed_out"
    assert helper_phase({"status": "deferred", "reason_code": "admission_denied"}) == \
        "waiting_provider"
    assert helper_phase({"status": "skipped"}) == "cancelled"
    assert helper_phase({"status": "cancelled"}) == "cancelled"
    for st in ("pending", "deferred", "running", "timed_out", "done", "delivered", "skipped",
               "cancelled", "failed"):
        for rc in ("", "timeout", "admission_denied", "needs_input"):
            assert helper_phase({"status": st, "reason_code": rc}) in PHASES, (st, rc)


def test_task_helpers_lists_a_lane_unit_once_with_its_phase(client, cc):
    """The task row's `helpers` may still carry lane units (an older bridge):
    /helpers lists ordinary units from it and lane units from the lane
    observations only, each once, with the lane's phase and reason code."""
    lane = {"unit_id": "hu-lane", "kind": "failure_triage", "lane": "failure_triage",
            "status": "failed", "phase": "timed_out", "reason_code": "timeout"}
    ingest(client, task_obs(helpers=[
        {"unit_id": "hu-1", "kind": "debug_advice", "status": "running"}, dict(lane)]),
        {"type": "lane_unit", "project_id": "proj", "task_id": "T-1", **lane})
    login(client)
    h = client.get("/control/api/tasks/proj/T-1/helpers").json()
    assert [u["unit_id"] for u in h["units"]] == ["hu-1"]
    assert [u["unit_id"] for u in h["lanes"]] == ["hu-lane"]
    assert h["lanes"][0]["phase"] == "timed_out" and h["lanes"][0]["reason_code"] == "timeout"
    assert h["lanes"][0]["kind_family"] == "lane" and h["units"][0]["kind_family"] == "ordinary"


# ------------------------------------------------------------------ batch ids / feed churn

def test_batch_id_is_idempotent_and_a_resend_makes_no_feed_churn(client, cc, app):
    db = app.state.services.db
    b = {**batch(task_obs(**lifecycle())), "batch_id": "boot1-abc", "source": {
        "id": "bridge:test", "kind": "bridge", "boot_id": "boot1"}}
    r1 = client.post("/v1/foundry/telemetry/batch", headers=COORD, json=b).json()
    assert r1["accepted"] == {"task": 1} and not r1.get("duplicate")
    n = db.query_one("SELECT COUNT(*) AS n FROM cc_feed")["n"]
    r2 = client.post("/v1/foundry/telemetry/batch", headers=COORD, json=b).json()
    assert r2["duplicate"] is True and r2["accepted"] == {"task": 1}
    assert db.query_one("SELECT COUNT(*) AS n FROM cc_feed")["n"] == n
    # a restarted bridge (new boot, new batch id) re-sends the same facts:
    # stored again, but an unchanged row writes no feed row (no SSE churn)
    session = {"type": "session", "id": "ses_1", "project_id": "proj", "task_id": "T-1",
               "status": {"value": "idle", "source": "t", "observed_ts": 1.0}}
    part = {"type": "part", "id": "prt_1", "message_id": "m", "session_id": "ses_1",
            "project_id": "proj", "kind": "text", "text": "hello"}
    ingest(client, session, part)
    n = db.query_one("SELECT COUNT(*) AS n FROM cc_feed")["n"]
    b3 = {**batch(task_obs(**lifecycle()), {**session, "status": {
        "value": "idle", "source": "t", "observed_ts": 2.0}}, part),
          "batch_id": "boot2-abc", "source": {"id": "bridge:test", "kind": "bridge", "boot_id": "boot2"}}
    r3 = client.post("/v1/foundry/telemetry/batch", headers=COORD, json=b3).json()
    assert not r3.get("duplicate") and r3["accepted"] == {"task": 1, "session": 1, "part": 1}
    assert db.query_one("SELECT COUNT(*) AS n FROM cc_feed")["n"] == n
    # a real change still reaches the feed
    ingest(client, task_obs("VERIFYING", **lifecycle()))
    assert db.query_one("SELECT COUNT(*) AS n FROM cc_feed")["n"] == n + 1


# ------------------------------------------------------------------ version gate

def hb(client, **kw):
    body = {"source": {"id": "bridge:ws", "kind": "bridge", **kw.pop("source", {})},
            "sent_ts": time.time(), **kw}
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_compat_gate_states(client, cc):
    from foundry_router import __version__ as server
    ok = hb(client, contract=CONTRACT, source={"version": server}, coordinator_version="0.122.0",
            boot_id="b1", features=["batch_id"])
    assert ok["compat"] == "ok" and ok["server_version"] == server
    src = next(s for s in cc.sources() if s["source_id"] == "bridge:ws")
    assert src["detail"]["compat"]["state"] == "ok" and src["detail"]["boot_id"] == "b1"
    assert src["detail"]["coordinator_version"] == "0.122.0"
    assert not [a for a in cc.alerts([], "open") if a["kind"] == "version_mismatch"]
    old = hb(client, contract=CONTRACT, source={"version": "0.119.4"})
    assert old["compat"] == "warn" and "below" in old["compat_reason"]
    al = [a for a in cc.alerts([], "open") if a["kind"] == "version_mismatch"]
    assert len(al) == 1 and al[0]["severity"] == "warning"
    bad = hb(client, contract="foundry.cc.v0", source={"version": server})
    assert bad["compat"] == "refuse"
    al = [a for a in cc.alerts([], "open") if a["kind"] == "version_mismatch"]
    assert al and al[0]["severity"] == "high"
    hb(client, contract=CONTRACT, source={"version": server})
    assert not [a for a in cc.alerts([], "open") if a["kind"] == "version_mismatch"]
    # a legacy heartbeat (no contract) is a quiet warning: shown, never alarmed
    assert compat_state({"source": {"version": server}}, server)[0] == "warn"
    assert compat_state({"contract": CONTRACT, "source": {"version": "0.200.0"}}, server)[0] == "warn"


def test_claim_refuses_a_refused_source(client, cc):
    ingest(client, task_obs("IMPLEMENTING"))
    h = login(client)
    client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "x",
        "idempotency_key": "gate-1234567"})
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "contract": "foundry.cc.v9", "source": {"id": "coordinator:ws", "kind": "coordinator"}})
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert r.status_code == 409 and r.json()["code"] == "version_refused"
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "contract": CONTRACT, "source": {"id": "coordinator:ws", "kind": "coordinator"}})
    got = client.post("/v1/foundry/control/claim", headers=COORD,
                      json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"]
    assert [a["kind"] for a in got] == ["pause"]


# ------------------------------------------------------------------ isolation

def test_empty_project_id_observations_are_refused(client, cc, app):
    r = ingest(client, {"type": "task", "project_id": "", "task_id": "X", "state": "FAILED"},
               {"type": "mcp", "project_id": "", "servers": []},
               {"type": "artifact", "artifact_id": "a1"},
               {"type": "config", "agents": {}})
    assert r["skipped"] == {"project_id_required": 4}
    # a project-restricted bridge can't send anything without a project id
    r = client.post("/v1/foundry/telemetry/batch", headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json=batch({"type": "session", "id": "ses_x", "status": {"value": "busy"}}))
    assert r.json()["skipped"] == {"project_id_required": 1}
    assert app.state.services.db.query("SELECT * FROM cc_sessions") == []


def test_sources_are_filtered_for_a_restricted_operator(client, cc):
    hb(client, contract=CONTRACT, projects=["proj"])
    client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "contract": CONTRACT, "source": {"id": "bridge:other", "kind": "bridge", "host": "pc2"},
        "projects": ["other"]})
    login(client, OP_B_KEY)                              # restricted to "other"
    ids = {s["source_id"] for s in client.get("/control/api/overview").json()["sources"]}
    assert ids == {"bridge:other"}
    ids = {s["source_id"] for s in client.get("/control/api/capabilities").json()["sources"]}
    assert ids == {"bridge:other"}
    items, _ = cc.feed(0, ["other"])
    assert {i["id"] for i in items if i["kind"] == "source"} == {"bridge:other"}
    login(client)                                        # unrestricted sees both
    ids = {s["source_id"] for s in client.get("/control/api/overview").json()["sources"]}
    assert ids == {"bridge:ws", "bridge:other"}


def test_helper_policy_is_global_and_refused_for_a_restricted_operator(client, cc):
    h = login(client, OP_B_KEY)
    r = client.put("/control/api/helpers/policy", headers=h, json={"policy": {"cutoff_pct": 50}})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


# ------------------------------------------------------------------ decision_answer

def _coord_beat(client, **kw):
    """A v0.122 coordinator heartbeat (control.py): contract, coordinator
    version and the decision_answer feature."""
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.121.10"},
            "contract": CONTRACT, "coordinator_version": "0.122.0",
            "features": ["decision_answer", "lifecycle"], "sent_ts": time.time(), **kw}
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _claim(client, beat=True):
    if beat:
        _coord_beat(client)
    return client.post("/v1/foundry/control/claim", headers=COORD,
                       json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"]


def _qh(client, did="dec-1"):
    """The question hash the page renders with the decision (task_view)."""
    v = view(client)["view"]["decisions"]
    return next(d for d in v["open"] + v["closed"] if d["decision_id"] == did)["question_hash"]


def test_decision_answer_carries_operator_authority_and_the_session_actor(client, cc):
    ingest(client, task_obs("BLOCKED", **lifecycle()))
    h = login(client)
    body = {"kind": "decision_answer", "project_id": "proj", "task_id": "T-1", "decision_id": "dec-1",
            "option": "refuse", "answer": "Keep src/b.luau out of this sprint.", "note": "per plan",
            "actor": "someone-else", "idempotency_key": "dec-12345678",
            "question_hash": _qh(client)}
    r = client.post("/control/api/actions", headers=h, json=body)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "decision_answer" and a["actor"] == "victor"
    assert a["expected"]["decision_id"] == "dec-1" and a["expected"]["decision_status"] == "open"
    assert "Keep src/b.luau" not in a["summary"] and "operator" in a["summary"]
    got = _claim(client)
    p = got[0]["payload"]
    assert got[0]["kind"] == "decision_answer"
    assert p["authority"] == "operator" and p["actor"] == "victor"       # not the body's "actor"
    assert p["option"] == "refuse" and p["answer"].startswith("Keep") and p["decision_kind"] == "scope_change"
    # one live answer per decision
    r2 = client.post("/control/api/actions", headers=h, json={**body, "idempotency_key": "dec-23456789"})
    assert r2.status_code == 409 and r2.json()["code"] == "duplicate_answer"
    # the view shows the answer in flight; applied resolves the decision alert
    assert view(client)["view"]["decisions"]["open"][0]["answer_action"]["status"] == "claimed"
    client.post(f"/v1/foundry/control/actions/{got[0]['action_id']}/status", headers=COORD,
                json={"source_id": "coordinator:ws", "status": "applied", "detail": "answered"})
    assert not [x for x in cc.alerts([], "open") if x["kind"] == "decision"]


def test_decision_answer_refuses_a_non_operator_and_stale_targets(client, cc):
    ingest(client, task_obs("IMPLEMENTING", **lifecycle()))
    h = login(client)
    base = {"kind": "decision_answer", "project_id": "proj", "task_id": "T-1", "decision_id": "dec-1",
            "answer": "ok", "question_hash": _qh(client)}
    for i, authority in enumerate(("delegated_policy", "model", "coordinator")):
        r = client.post("/control/api/actions", headers=h, json={
            **base, "authority": authority, "idempotency_key": f"auth-{i}-1234567"})
        assert r.status_code == 403 and r.json()["code"] == "authority_not_operator", authority
    # a session without the operator scope can't answer (direct call: the HTTP
    # login already refuses such keys)
    from foundry_router.control_center import ControlError
    with pytest.raises(ControlError) as e:
        cc.create_action("bot", [], {**base, "idempotency_key": "scope-1234567"}, scopes=["inference"])
    assert e.value.status == 403
    r = client.post("/control/api/actions", headers=h, json={**base, "decision_id": "nope",
                                                              "idempotency_key": "unk-12345678"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_decision"
    r = client.post("/control/api/actions", headers=h, json={**base, "decision_id": "dec-0",
                                                              "idempotency_key": "old-12345678"})
    assert r.status_code == 409 and r.json()["code"] == "stale_decision"
    r = client.post("/control/api/actions", headers=h, json={**base, "answer": "", "option": "",
                                                              "idempotency_key": "empty-1234567"})
    assert r.status_code == 400
    r = client.post("/control/api/actions", headers=h, json={**base, "option": "maybe",
                                                              "idempotency_key": "opt-12345678"})
    assert r.status_code == 400
    # the task state moving on doesn't make an open decision's answer stale
    r = client.post("/control/api/actions", headers=h, json={
        **base, "expected": {"task_state": "VERIFYING"}, "idempotency_key": "ok-123456789"})
    assert r.status_code == 200, r.text
    assert _claim(client)[0]["payload"]["authority"] == "operator"


# ------------------------------------------------------------------ the page (node)

node = pytest.mark.skipif(not __import__("shutil").which("node"), reason="node not installed")
HOSTILE = "<script>alert(1)</script><img src=x onerror=alert(1)>"


def _render(tmp_path, task, sections, extra=""):
    from tests.test_control_ui import _script
    from tests.test_control_ui_behavior import between, run_js
    s = _script()
    helpers = between(s, "/*sprint-start*/", "/*sprint-end*/")
    methods = between(s, "  /*sprint-render-start*/", "  /*sprint-render-end*/")
    return run_js(tmp_path, """
const S = {tasks: new Map(), project: "proj", task: "T-1", openProv: new Set(%s)};
const taskKey = (p, t) => p + "/" + t; const curTask = () => S.tasks.get("proj/T-1");
const short = (s, n = 10) => s ? String(s).slice(0, n) : "";
const tsText = t => t ? "T" + t : ""; const nowS = () => 1000;
const fullTs = t => t ? "TS" + t : "";
const NA = why => `<span class="dim">N/A</span>`;
const kv = rows => rows.filter(Boolean).map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("");
""" % json.dumps(extra and [extra] or []) + helpers + """
const R = {
""" + methods + """
};
const TASK = """ + json.dumps(task) + """;
if (TASK) S.tasks.set("proj/T-1", TASK);
const out = {};
for (const n of """ + json.dumps(sections) + """) out[n] = R[n](null);
out.inbox = decisionsInboxHtml(S.tasks);
console.log(JSON.stringify(out).replace(/[^\x00-\x7f]/g, c => String.fromCharCode(92) + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")));
""")


SECTIONS = ["readiness", "checklist", "evidence", "recovered", "findings", "budgets", "decisions"]


def _task_with_view(client, cc, **kw):
    ingest(client, task_obs("BLOCKED", blocker="x", **kw))
    login(client)
    return client.get("/control/api/overview").json()["tasks"][0]


@node
def test_sprint_sections_render_escaped_with_links_and_collapsed_provenance(tmp_path, client, cc):
    lc = lifecycle()
    lc["readiness"][0]["detail"] = HOSTILE
    lc["obligations"][0]["title"] = HOSTILE
    lc["evidence"]["checks"][1]["reason"] = HOSTILE
    lc["decisions"][0]["question"] = "Allow <b>edits</b>? " + HOSTILE
    lc["decisions"][0]["evidence"] = [HOSTILE]
    lc["finding_history"]["findings"][0]["sample"] = HOSTILE
    lc["recovered"][0]["detail"] = HOSTILE
    t = _task_with_view(client, cc, **lc)
    out = _render(tmp_path, t, SECTIONS)
    for name, h in out.items():
        assert "<script>" not in h and "<img src=x" not in h, name
        assert "<b>edits</b>" not in h, name
    assert "&lt;script&gt;" in out["readiness"] and "environment" in out["readiness"]
    assert "failed" in out["readiness"] and "unknown" in out["readiness"]
    assert "run tools/gate.ps1" in out["checklist"] and "gate not run at this revision" in out["checklist"]
    ev = out["evidence"]
    assert "1 reusable" in ev and "1 stale" in ev and "1 missing" in ev
    assert "must rerun:" in ev and "no recorded result" in ev
    # provenance: present, collapsed by default, with session + tool-call links
    assert '<details data-prov="ev:lune">' in ev
    assert 'data-open="ses_old"' in ev and 'data-part="prt_9" data-sid="ses_old"' in ev
    assert "ffffffffffffffff" in ev                                 # output blob sha
    assert "repeated" in out["findings"] and "same approach as the last attempt" in out["findings"]
    assert "3 / 3" in out["budgets"] and "used up" in out["budgets"]
    assert "retry guidance granted one extra" in out["budgets"]
    d = out["decisions"]
    assert 'data-dsubmit="dec-1"' in d and 'value="refuse"' in d and "recommended" in d
    assert "authority <b>operator</b>" in d and "build.gate" in d
    assert "1 answered / superseded" in d                           # closed ones collapsed
    assert 'data-decide-id="dec-1"' in out["inbox"] and "scope_change" in out["inbox"]
    # an opened provenance block stays open across re-renders
    again = _render(tmp_path, t, ["evidence"], extra="ev:lune")
    assert '<details data-prov="ev:lune" open>' in again["evidence"]


@node
def test_sprint_sections_empty_and_not_reported_states(tmp_path, client, cc):
    # no task at all
    out = _render(tmp_path, None, SECTIONS)
    assert all("No coordinator task reported" in out[n] for n in SECTIONS)
    assert out["inbox"] == ""
    # a pre-0.122 bridge: nothing reported, nothing invented
    ingest(client, task_obs("IMPLEMENTING"))
    login(client)
    t = client.get("/control/api/overview").json()["tasks"][0]
    out = _render(tmp_path, t, SECTIONS)
    for n in SECTIONS:
        assert "not reported by this coordinator" in out[n], n
        assert "N/A" not in out[n] or n == "budgets"
    # reported but empty
    ingest(client, task_obs("IMPLEMENTING", lifecycle_tables=[], readiness=[], obligations=[],
                            evidence={"counts": {}, "checks": []}, recovered=[],
                            finding_history={"findings": [], "repairs": []}, decisions=[],
                            budgets={"counters": {}}))
    t = client.get("/control/api/overview").json()["tasks"][0]
    out = _render(tmp_path, t, SECTIONS)
    assert "No readiness checks were run" in out["readiness"]
    assert "No obligations recorded" in out["checklist"]
    assert "0 reusable" in out["evidence"] and "No evidence recorded" in out["evidence"]
    assert "Nothing recovered" in out["recovered"] and "No findings recorded" in out["findings"]
    assert "No budget counters reported" in out["budgets"] and "No decisions" in out["decisions"]


@node
def test_answer_form_hides_while_an_answer_is_in_flight(tmp_path, client, cc):
    t = _task_with_view(client, cc, **lifecycle())
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "decision_answer", "project_id": "proj", "task_id": "T-1", "decision_id": "dec-1",
        "option": "allow", "idempotency_key": "form-1234567", "question_hash": _qh(client)})
    assert r.status_code == 200, r.text
    t = client.get("/control/api/overview").json()["tasks"][0]
    out = _render(tmp_path, t, ["decisions"])
    assert 'data-dsubmit="dec-1"' not in out["decisions"]
    assert "Answer accepted by victor" in out["decisions"]
    assert 'data-decide-id="dec-1"' not in out["inbox"] and "answer accepted" in out["inbox"]


def test_sprint_sections_are_registered_and_wired():
    from tests.test_control_ui import _script
    s = _script()
    for n in SECTIONS:
        assert f'"{n}"' in s.split("const SECTIONS =")[1].split("];")[0] or n in s.split(
            "const SECTIONS_V3 =")[1].split("];")[0], n
        assert f"  {n}(s) {{" in s and f"  {n}(sec) {{" in s, n
        assert f"{n}:" in s.split("const SECTION_TITLES =")[1].split("};")[0], n
    assert "const PREF_VERSION = 4;" in s
    assert 'kind: "decision_answer"' in s and "decisionsInboxHtml(S.tasks) + pHtml" in s
    assert "compatPill(" in s



# ------------------------------------------------------------------ v0.122b review fixes

def test_decision_answer_is_bound_to_the_rendered_question(client, cc):
    """The page sends the hash of the question it rendered (task_view carries
    it); the server requires it and refuses another one as stale_decision."""
    ingest(client, task_obs("BLOCKED", **lifecycle()))
    h = login(client)
    qh = _qh(client)
    from foundry_router.control_center import _hash
    assert qh == _hash("Allow edits to <src/b.luau>?")
    base = {"kind": "decision_answer", "project_id": "proj", "task_id": "T-1",
            "decision_id": "dec-1", "option": "refuse"}
    r = client.post("/control/api/actions", headers=h, json={**base, "idempotency_key": "nohash-123"})
    assert r.status_code == 400 and r.json()["code"] == "question_hash_required"
    r = client.post("/control/api/actions", headers=h, json={
        **base, "question_hash": "0" * 24, "idempotency_key": "badhash-123"})
    assert r.status_code == 409 and r.json()["code"] == "stale_decision"
    # the coordinator reworded the question after the page rendered it
    lc = lifecycle()
    lc["decisions"][0] = {**lc["decisions"][0], "question": "Allow edits to src/b.luau AND c?"}
    ingest(client, task_obs("BLOCKED", **lc))
    r = client.post("/control/api/actions", headers=h, json={
        **base, "question_hash": qh, "idempotency_key": "oldhash-123"})
    assert r.status_code == 409 and r.json()["code"] == "stale_decision"
    r = client.post("/control/api/actions", headers=h, json={
        **base, "question_hash": _qh(client), "idempotency_key": "newhash-123"})
    assert r.status_code == 200, r.text
    assert _claim(client)[0]["payload"]["question_hash"] == _qh(client)


def test_page_sends_the_rendered_question_hash():
    from tests.test_control_ui import _script
    s = _script()
    assert 'data-dqh="${esc(d.question_hash || "")}"' in s
    assert "question_hash: qh" in s and "f.dataset.dqh" in s


def test_decision_answer_is_never_handed_to_a_source_without_the_feature(client, cc):
    ingest(client, task_obs("BLOCKED", **lifecycle()))
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "decision_answer", "project_id": "proj", "task_id": "T-1", "decision_id": "dec-1",
        "option": "refuse", "question_hash": _qh(client), "idempotency_key": "feat-1234567"})
    assert r.status_code == 200, r.text
    aid = r.json()["action_id"]
    client.post("/control/api/actions", headers=h, json={
        "kind": "pause", "project_id": "proj", "task_id": "T-1", "reason": "x",
        "idempotency_key": "feat-pause-123"})
    # never heart-beat (unknown) or a legacy source without features: withheld
    assert [a["kind"] for a in _claim(client, beat=False)] == ["pause"]
    a = cc.action_view(aid)
    assert a["status"] == "accepted" and "does not declare the decision_answer" in a["status_detail"]
    # a v0.122 coordinator without the feature: warned at the gate, still withheld
    r = _coord_beat(client, features=["lifecycle"])
    assert r["compat"] == "warn" and "decision_answer" in r["compat_reason"]
    assert _claim(client, beat=False) == []
    assert cc.action_view(aid)["status"] == "accepted"
    # the feature declared under the other field name (`capabilities`) counts
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator"}, "contract": CONTRACT,
            "coordinator_version": "0.122.0", "capabilities": ["decision_answer"],
            "sent_ts": time.time()}
    assert client.post("/v1/foundry/telemetry/heartbeat", headers=COORD,
                       json=body).json()["compat"] == "ok"
    got = _claim(client, beat=False)
    assert [a["kind"] for a in got] == ["decision_answer"]


def test_compat_gate_compares_the_declared_coordinator_version(client, cc):
    from foundry_router import __version__ as server
    from foundry_router.control_center import source_features
    p = {"contract": CONTRACT, "source": {"version": server, "kind": "bridge"}}
    assert compat_state({**p, "coordinator_version": "0.122.0"}, server) == ("ok", "")
    st, why = compat_state({**p, "coordinator_version": "0.110.0"}, server)
    assert st == "refuse" and "0.110.0" in why
    st, why = compat_state({**p, "coordinator_version": "1.0.0"}, server)
    assert st == "refuse" and "major" in why
    assert compat_state({**p, "coordinator_version": "garbage"}, server)[0] == "refuse"
    # the router's own version is not the coordinator's: an old router string with
    # a current coordinator is ok
    assert compat_state({"contract": CONTRACT, "source": {"version": "0.100.0"},
                         "coordinator_version": "0.122.0"}, server)[0] == "ok"
    # legacy sources (no contract / no coordinator_version) stay at warn, never refused
    assert compat_state({"source": {"version": "0.100.0"},
                         "coordinator_version": "0.050.0"}, server)[0] == "warn"
    assert compat_state({"source": {"version": "0.119.0"}}, server)[0] == "warn"
    # features under either name
    assert source_features({"features": ["a"], "capabilities": {"b": True, "c": False},
                            "source": {"capabilities": {"actions": ["d"]}}}) == ["a", "b", "d"]
    assert source_features({"source": {"capabilities": {"game": {"x": 1}}}}) is None
    r = hb(client, contract=CONTRACT, source={"version": server}, coordinator_version="0.100.0")
    assert r["compat"] == "refuse"
    got = client.post("/v1/foundry/control/claim", headers=COORD,
                      json={"source_id": "bridge:ws", "projects": ["proj"]})
    assert got.status_code == 409 and got.json()["code"] == "version_refused"


@pytest.mark.parametrize("cls", ["tooling", "capability", "scope", "deps", "environment"])
def test_adapter_readiness_classes_are_readiness_warnings(client, cc, cls):
    from foundry_router.control_center import ENV_BLOCKER_CLASSES
    assert cls in ENV_BLOCKER_CLASSES
    ingest(client, task_obs("BLOCKED", blocker=f"readiness ({cls})", **lifecycle(
        blocker_class=cls, readiness=[{"name": "x", "ok": False, "cls": cls, "blocking": True}])))
    kinds = {a["kind"] for a in cc.alerts([], "open")}
    assert "readiness" in kinds and "blocker" not in kinds


def test_overview_and_feed_send_task_view_not_the_raw_lifecycle_lists_too(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", **lifecycle()))
    login(client)
    t = client.get("/control/api/overview").json()["tasks"][0]
    for k in ("readiness", "obligations", "evidence", "recovered", "finding_history",
              "budgets", "decisions"):
        assert k not in t["data"], k
    assert t["task_view"]["readiness"]["failing"] == 1
    assert t["task_view"]["decisions"]["open"][0]["decision_id"] == "dec-1"
    assert t["data"]["state"] == "BLOCKED" or t["state"] == "BLOCKED"
    items, _ = cc.feed(0, [])
    ent = [i for i in items if i["kind"] == "task"][-1]["entity"]
    assert "decisions" not in ent["data"] and ent["task_view"]["checklist"]["total"] == 2
    # the per-task view route still serves the normalized view
    assert view(client)["view"]["evidence"]["counts"]["stale"] == 1


@node
def test_checklist_shows_build_continuing_from_the_server_view(tmp_path, client, cc):
    """The bridge's build_continuing (the engine's record) is bounded by
    Foundry into task_view.build and shown above the checklist as info; a
    task that left the BUILD loop (BLOCKED) shows none even if the field lingers."""
    b = {"open_steps": 3, "steps": ["- [ ] a", HOSTILE, "- [ ] c"] + ["x"] * 20,
         "truncated": False, "head_sha": "d" * 90, "ts": 5}
    ingest(client, task_obs("CHANGES_REQUIRED", build_continuing=b, **lifecycle()))
    login(client)
    t = client.get("/control/api/overview").json()["tasks"][0]
    v = t["task_view"]["build"]
    assert v["open_steps"] == 3 and len(v["steps"]) == 12 and len(v["head_sha"]) == 64
    assert t["verification_view"]["build"] == v
    h = _render(tmp_path, t, ["checklist"])["checklist"]
    assert 'class="infobox" data-build="continuing"' in h and "3 checklist steps still open" in h
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert h.index("data-build") < h.index("open of")
    ingest(client, task_obs("BLOCKED", blocker="x", build_continuing=b, **lifecycle()))
    t = client.get("/control/api/overview").json()["tasks"][0]
    assert t["task_view"]["build"] is None and t["verification_view"]["build"] is None
    assert "data-build" not in _render(tmp_path, t, ["checklist"])["checklist"]
