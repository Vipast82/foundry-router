"""Queue API, Foundry side (review A2 + A3): the queue_approve / queue_reorder /
queue_deps / queue_requeue actions (task-scoped, operator sessions, payload
validation, idempotent, one in flight per task, stale-state refusals, handed
only to a coordinator declaring the `queue` feature), the GET /control/api/queue
view the Automation page's board reads, and the GET /control/api/inbox "Your
decisions" view (escalation reasons, resolved-by-Lead counter)."""

import time

import pytest

from foundry_router.control_center import CONTRACT
from tests.test_control_center import (COORD, OP_B_KEY, cc, ingest,  # noqa: F401
                                       login, task_obs)


def _beat(client, features=("decision_answer", "project_pause", "tracks", "queue"),
          sid="coordinator:ws"):
    body = {"source": {"id": sid, "kind": "coordinator", "version": "0.136.0",
                       "capabilities": {"actions": ["pause", "cancel", "queue_approve",
                                                    "queue_reorder", "queue_deps",
                                                    "queue_requeue"]}},
            "contract": CONTRACT, "coordinator_version": "0.136.0", "features": list(features),
            "sent_ts": time.time()}
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _claim(client, sid="coordinator:ws"):
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": sid, "projects": ["proj"]})
    assert r.status_code == 200, r.text
    return r.json()["actions"]


def _act(client, h, kind, key, **kw):
    body = {"kind": kind, "project_id": "proj", "task_id": "T-1",
            "idempotency_key": key, **kw}
    return client.post("/control/api/actions", headers=h, json=body)


def q_obs(task_id="T-1", state="QUEUED", position=1, deps=(), **kw):
    return task_obs(state, task_id=task_id,
                    queue={"position": position, "deps": list(deps), "approved_by": "victor",
                           "note": "", "track": kw.pop("track", "")}, **kw)


# ------------------------------------------------------------------ validation

def test_queue_action_payload_validation(client, cc):
    ingest(client, q_obs())
    h = login(client)
    r = _act(client, h, "queue_reorder", "qr-bad-1234567")
    assert r.status_code == 400 and "position" in r.json()["error"]
    assert _act(client, h, "queue_reorder", "qr-bad2-123456", position=0).status_code == 400
    assert _act(client, h, "queue_reorder", "qr-bad3-123456", position=True).status_code == 400
    assert _act(client, h, "queue_reorder", "qr-bad4-123456", position="2").status_code == 400
    r = _act(client, h, "queue_requeue", "qq-bad-1234567")
    assert r.status_code == 409                               # QUEUED, not CANCELLED
    r = _act(client, h, "queue_deps", "qd-bad-1234567", deps="T-2")
    assert r.status_code == 400 and "list" in r.json()["error"]
    assert _act(client, h, "queue_deps", "qd-bad2-123456", deps=["T-1"]).status_code == 400  # self
    assert _act(client, h, "queue_deps", "qd-bad3-123456",
                deps=[f"T-{i}" for i in range(2, 25)]).status_code == 400    # over the cap
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "queue_reorder", "project_id": "proj", "position": 2,
        "idempotency_key": "qr-notid-12345"})
    assert r.status_code == 400 and "task_id" in r.json()["error"]


def test_queue_approve_may_name_an_unreported_task_but_others_may_not(client, cc):
    h = login(client)
    # nothing ingested: a planned sprint can still be approved
    r = _act(client, h, "queue_approve", "qa-new-1234567", deps=[], note="first")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "accepted" and "task_state" not in a["expected"]
    assert "approve T-1" in a["summary"]
    # reorder / deps / requeue need the bridge to have reported the task
    r = _act(client, h, "queue_reorder", "qr-unk-1234567", position=1,
             task_id="T-9")
    assert r.status_code == 404 and r.json()["code"] == "unknown_task"


def test_queue_actions_refuse_stale_or_wrong_states(client, cc):
    ingest(client, q_obs(state="IMPLEMENTING"))
    h = login(client)
    # the operator saw QUEUED, the task moved on
    r = _act(client, h, "queue_reorder", "qr-stale-12345", position=1,
             expected={"task_state": "QUEUED"})
    assert r.status_code == 409 and r.json()["code"] == "stale_state"
    # deps only change while QUEUED; approve updates only while QUEUED
    assert _act(client, h, "queue_deps", "qd-impl-12345", deps=[]).status_code == 409
    assert _act(client, h, "queue_approve", "qa-impl-12345", deps=[]).status_code == 409
    # COMPLETE is never re-queued (passed work is never rerun)
    ingest(client, q_obs(state="COMPLETE"))
    r = _act(client, h, "queue_requeue", "qq-done-12345", reason="again")
    assert r.status_code == 409 and "never rerun" in r.json()["error"]


def test_queue_action_idempotency_and_one_in_flight(client, cc):
    ingest(client, q_obs())
    h = login(client)
    r1 = _act(client, h, "queue_reorder", "qr-dup-1234567", position=2)
    assert r1.status_code == 200 and not r1.json().get("duplicate")
    # the same idempotency key returns the stored action
    r2 = _act(client, h, "queue_reorder", "qr-dup-1234567", position=2)
    assert r2.json()["duplicate"] is True and r2.json()["action_id"] == r1.json()["action_id"]
    # a different key while one is live for the task returns the in-flight one
    r3 = _act(client, h, "queue_deps", "qd-while-12345", deps=[])
    assert r3.json()["duplicate"] is True and r3.json()["action_id"] == r1.json()["action_id"]


def test_queue_actions_need_an_operator_session_and_the_queue_feature(client, cc):
    ingest(client, q_obs())
    h = login(client)
    # CSRF required like every mutation
    r = client.post("/control/api/actions", json={
        "kind": "queue_reorder", "project_id": "proj", "task_id": "T-1", "position": 1,
        "idempotency_key": "qr-nocsrf-1234"})
    assert r.status_code == 403
    # project isolation
    hb = login(client, OP_B_KEY)
    r = client.post("/control/api/actions", headers=hb, json={
        "kind": "queue_reorder", "project_id": "proj", "task_id": "T-1", "position": 1,
        "idempotency_key": "qr-opb-1234567"})
    assert r.status_code == 403
    # a coordinator that does not declare `queue` is not handed the action
    h = login(client)                       # the op-b login replaced the session cookie
    assert _act(client, h, "queue_reorder", "qr-gate-123456", position=2).status_code == 200
    _beat(client, features=("decision_answer", "project_pause"))
    assert [a["kind"] for a in _claim(client)] == []
    _beat(client)
    got = _claim(client)
    assert [a["kind"] for a in got] == ["queue_reorder"]
    assert got[0]["payload"] == {"position": 2}


# ------------------------------------------------------------------ queue view

def test_queue_view_orders_by_position_and_isolates_projects(client, cc):
    ingest(client, q_obs("T-1", position=2, deps=["T-2"]),
           q_obs("T-2", position=1, track="ui"),
           task_obs("IMPLEMENTING", task_id="T-3"))      # no queue field (older bridge)
    # the queue endpoint needs a session
    assert client.get("/control/api/queue?project=proj").status_code in (401, 403)
    h = login(client)
    r = client.get("/control/api/queue?project=proj", headers=h)
    assert r.status_code == 200, r.text
    tasks = r.json()["tasks"]
    assert [t["task_id"] for t in tasks] == ["T-2", "T-1", "T-3"]
    assert tasks[0]["track"] == "ui" and tasks[1]["deps"] == ["T-2"]
    assert tasks[1]["approved_by"] == "victor" and tasks[2]["position"] is None
    # a project-restricted operator may not read another project's queue
    hb = login(client, OP_B_KEY)
    assert client.get("/control/api/queue?project=proj", headers=hb).status_code == 403


# ------------------------------------------------------------------ PM inbox

def _decision(did="dec-1", status="open", kind="lead_escalation", **kw):
    d = {"decision_id": did, "kind": kind, "status": status,
         "question": "Core contract change?", "options": ["allow", "refuse"],
         "recommended": "\"refuse\"", "recommendation": "refuse it",
         "decision_class": "decision", "timeout_minutes": 60,
         "deadline_ts": time.time() + 3600, "opened_ts": time.time() - 60}
    d.update(kw)
    return d


def test_inbox_lists_decisions_with_reasons_and_the_lead_counter(client, cc):
    now = time.time()
    ingest(client, task_obs(
        "BLOCKED",
        decisions=[
            _decision(escalation_reason="scope_core"),
            _decision("dec-2", kind="budget", escalation_reason=""),
            # answered by the Lead this week, and one answered long ago
            _decision("dec-3", status="answered", kind="gate", authority="lead_policy",
                      answered_ts=now - 3600),
            _decision("dec-4", status="answered", kind="gate", authority="lead_policy",
                      answered_ts=now - 10 * 86400),
            _decision("dec-5", status="answered", kind="gate", authority="operator",
                      answered_ts=now - 3600)],
        lead={"case_id": "case-1", "state": "NEEDS_VICTOR", "decision_id": "dec-1",
              "lead_session_id": "ses_lead", "outcome": "needs_victor: scope_core"}))
    assert client.get("/control/api/inbox").status_code in (401, 403)
    h = login(client)
    r = client.get("/control/api/inbox", headers=h)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["resolved_by_lead_7d"] == 1
    assert [d["decision_id"] for d in v["decisions"]] == ["dec-1", "dec-2"]
    esc = v["decisions"][0]
    assert esc["escalation_reason"] == "out of scope: touches a core contract"
    assert esc["lead"]["case_id"] == "case-1" and esc["lead"]["lead_session_id"] == "ses_lead"
    assert esc["question_hash"] and esc["recommended"] == "\"refuse\""
    assert "escalation_reason" not in v["decisions"][1]
    # an answer in flight is marked so the page shows "waiting for the coordinator"
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "decision_answer", "project_id": "proj", "task_id": "T-1",
        "decision_id": "dec-2", "decision_kind": "budget", "option": "allow",
        "answer": "allow", "question_hash": esc["question_hash"],
        "idempotency_key": "da-inbox-12345"})
    assert r.status_code == 200, r.text
    v = client.get("/control/api/inbox", headers=h).json()
    assert v["decisions"][1]["answer_action"]["status"] == "accepted"


def test_inbox_falls_back_to_the_case_outcome_for_the_reason(client, cc):
    ingest(client, task_obs(
        "BLOCKED",
        decisions=[_decision()],          # no escalation_reason field (older coordinator)
        lead={"case_id": "case-1", "state": "NEEDS_VICTOR", "decision_id": "dec-1",
              "lead_session_id": "", "outcome": "needs_victor: lead_permissions"}))
    h = login(client)
    v = client.get("/control/api/inbox", headers=h).json()
    assert v["decisions"][0]["escalation_reason"] == "permission only you hold"
    assert v["resolved_by_lead_7d"] == 0
