"""The quality signal (W4): request outcome, feedback, perf and the coordinator's
verified labels joined to captured calls by request id; the outcomes endpoint the
coordinator bridge posts to; the `router_ok` / `router_verified` curate tokens that
let a verified output (frontier too, when the project allows it) become an example,
with the default policy unchanged."""

import json
import time

import pytest

from foundry_router import training_quality as tq
from foundry_router.config import ClientConfig, TrainingCaptureConfig
from tests.test_budgets import STATE, ledgered  # noqa: F401 (fixture)
from tests.test_training_capture import COORD, _bind, _chat, _setup


def _bind_role(client, sid, role, task="T1", project="game"):
    assert client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": project, "task_id": task, "run_id": f"{task}-a1",
        "role": role}).status_code == 200


def _records(client, **q):
    return client.get("/v1/foundry/capture/records", headers=COORD, params=q).json()["records"]


def _rid(client):
    return [r for r in _records(client) if r["kind"] == "backend_call"][-1]["request_id"]


def test_records_carry_outcome_perf_and_feedback_by_request_id(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    assert _chat(client, "s1").status_code == 200
    call = [r for r in _records(client) if r["kind"] == "backend_call"][0]
    q = call["quality"]
    assert q["outcome"] == "ok" and q["feedback"] is None and q["labels"] == []
    rid = call["request_id"]
    assert rid
    # thumbs-down on the request log row joins by request id
    row = ledgered.db.query_one("SELECT id FROM request_log WHERE request_id=?", (rid,))
    ledgered.db.execute("INSERT INTO response_feedback (ts, request_log_id, rating, source) "
                        "VALUES (?,?,?,?)", ("t", row["id"], -1, "gui"))
    again = [r for r in _records(client) if r["kind"] == "backend_call"][0]["quality"]
    assert again["feedback"] == -1


def test_outcomes_endpoint_validates_and_is_idempotent(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    _chat(client, "s1")
    rid = _rid(client)
    url = "/v1/foundry/training/outcomes"
    assert client.post(url, headers=COORD, json={"label": "bogus", "request_ids": [rid]}
                       ).status_code == 400
    assert client.post(url, headers=COORD, json={"label": "gate_pass"}).status_code == 400
    ok = client.post(url, headers=COORD, json={"label": "gate_pass", "request_ids": [rid],
                                                "evidence": {"head": "abc123"}})
    assert ok.status_code == 200 and ok.json()["recorded"] == 1
    dup = client.post(url, headers=COORD, json={"label": "gate_pass", "request_ids": [rid]})
    assert dup.json()["recorded"] == 0                       # (request, label) is unique
    q = [r for r in _records(client) if r["kind"] == "backend_call"][0]["quality"]
    assert [l["label"] for l in q["labels"]] == ["gate_pass"]
    assert json.loads(q["labels"][0]["evidence"]) == {"head": "abc123"}
    listed = client.get(url, headers=COORD).json()["outcomes"]
    assert len(listed) == 1 and listed[0]["project_id"] == "game"      # filled from the log
    # it needs the orchestration scope like the rest of /v1/foundry
    assert client.post(url, headers={"Authorization": "Bearer kk"},
                       json={"label": "gate_pass", "request_ids": [rid]}).status_code in (401, 403)


def test_outcome_by_work_labels_only_the_implementers_calls_up_to_the_event(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind_role(client, "impl", "implementer")
    _bind_role(client, "rev", "reviewer")
    _chat(client, "impl")
    t_gate = time.time() + 1
    _chat(client, "rev")
    calls = [r for r in _records(client) if r["kind"] == "backend_call"]
    impl = [c for c in calls if c["session_id"] == "impl"][0]["request_id"]
    rev = [c for c in calls if c["session_id"] == "rev"][0]["request_id"]
    r = client.post("/v1/foundry/training/outcomes", headers=COORD, json={
        "label": "review_approved", "project_id": "game", "task_id": "T1",
        "session_ids": ["impl"], "until_ts": t_gate + 3600})
    assert r.json()["recorded"] == 1 and r.json()["resolved_from_work"] is True
    labelled = {c["request_id"]: [l["label"] for l in c["quality"]["labels"]]
                for c in _records(client) if c["kind"] == "backend_call"}
    assert labelled[impl] == ["review_approved"] and labelled[rev] == []
    # nothing recorded for work that has no calls
    none = client.post("/v1/foundry/training/outcomes", headers=COORD, json={
        "label": "gate_pass", "project_id": "game", "task_id": "NOPE"})
    assert none.status_code == 400
    # until_ts: an event before the call labels nothing
    early = client.post("/v1/foundry/training/outcomes", headers=COORD, json={
        "label": "gate_pass", "project_id": "game", "task_id": "T1", "until_ts": 1000})
    assert early.status_code == 400


def test_download_filters_by_router_quality(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    _chat(client, "s1")
    _chat(client, "s1")
    calls = [r for r in _records(client) if r["kind"] == "backend_call"]
    good, bad = calls[0]["request_id"], calls[1]["request_id"]
    client.post("/v1/foundry/training/outcomes", headers=COORD,
                json={"label": "gate_pass", "request_ids": [good]})
    client.post("/v1/foundry/training/outcomes", headers=COORD,
                json={"label": "gate_fail", "request_ids": [bad]})
    n = lambda qs: len(client.get("/admin/api/training/capture/download" + qs).text.splitlines())
    assert n("") == 2
    assert n("?quality=ok") == 1               # the gate_fail one is not router_ok
    assert n("?quality=verified") == 1         # ok + a positive label
    only = json.loads(client.get("/admin/api/training/capture/download?quality=verified"
                                 ).text.splitlines()[0])
    assert only["meta"]["request_id"] == good
    assert [l["label"] for l in only["meta"]["quality"]["labels"]] == ["gate_pass"]


def test_quality_state_rules():
    from foundry_training.labels import quality_state
    ok = {"outcome": "ok", "feedback": None, "labels": [{"label": "gate_pass"}]}
    assert quality_state(ok)["ok"] and quality_state(ok)["positive"] == ["gate_pass"]
    assert not quality_state({"outcome": "error", "labels": []})["ok"]
    assert not quality_state({"outcome": "ok", "feedback": -1, "labels": []})["ok"]
    assert quality_state({"outcome": "ok", "feedback": 2, "labels": []})["ok"]
    assert not quality_state({"outcome": "ok", "labels": [{"label": "vision_changes"}]})["ok"]
    assert not quality_state(None)["known"] and not quality_state({})["ok"]
    assert tq.passes({"quality": ok}, "verified") and not tq.passes({"quality": {
        "outcome": "ok", "labels": []}}, "verified")


# ---------------------------------------------------------------- the pipeline side

from foundry_training.adapters import foundry as FA  # noqa: E402
from tests.training.helpers import (LOCAL, SESSION, capture_db, capture_rows, kilo_export,  # noqa: E402
                                    pipeline, profile, workspace)

PID = "fixture-game"
POLICY = """
[training.policy.sft]
require = ["{token}", "complete_tool_sequence", "system_and_tools_known"]
[training.export]
allow_frontier_targets = {allow}
"""


def _run(tmp_path, monkeypatch, *, token="router_verified", allow=True, outcome="ok",
         labels=(), model="claude-sonnet-5-5", persona="Foundry-Kilo-Sonnet", policy=True):
    tmp_path.mkdir(exist_ok=True)
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, PID, ws, extra=POLICY.format(token=token, allow=str(allow).lower())
                   if policy else "")
    rows = capture_rows(kilo_export(), model=model, persona=persona)
    path = capture_db(tmp_path, rows)
    recs = list(FA.from_db(path))
    calls = [r for r in recs if r["kind"] == "backend_call"]
    for r in calls:
        r["quality"] = {"outcome": outcome, "status": "ok", "feedback": None, "labels": []}
    outs = [{"id": i + 1, "ts": time.time(), "request_id": c["request_id"], "label": lb,
             "project_id": PID, "task_id": "FX-1", "source": "coordinator", "evidence": ""}
            for i, lb in enumerate(labels) for c in calls[-1:]]
    monkeypatch.setattr(FA, "from_api", lambda url, key, after=0, **kw: iter(
        [r for r in recs if r["id"] > after]))
    monkeypatch.setattr(FA, "outcomes_from_api", lambda url, key, after=0, **kw: iter(
        [o for o in outs if o["id"] > after]))
    p = pipeline(tmp_path, [prof])
    from tests.training.helpers import KILO_EXPORT
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    res = p.ingest_foundry(url="http://router.invalid", key="k")
    assert res.get("outcomes", 0) == len(outs)
    p.curate(PID)
    eps = p.store.cur.q("SELECT * FROM episodes WHERE project_id=? AND kind='trajectory'", (PID,))
    assert len(eps) == 1
    return eps[0]["status"], json.loads(eps[0]["reasons"] or "[]"), json.loads(eps[0]["outcome"])


def test_verified_frontier_output_becomes_an_example_when_the_project_allows_it(
        tmp_path, monkeypatch):
    status, reasons, oc = _run(tmp_path / "a", monkeypatch, labels=["gate_pass"])
    assert oc["router"]["ok"] and oc["router"]["positive"] == ["gate_pass"]
    assert (status, reasons) == ("selected", [])


def test_router_verified_needs_a_positive_label_and_router_ok_does_not(tmp_path, monkeypatch):
    status, reasons, _ = _run(tmp_path, monkeypatch, labels=[])
    assert status == "excluded" and reasons == ["router_unverified"]
    status, reasons, _ = _run(tmp_path / "b", monkeypatch, token="router_ok", labels=[]) \
        if (tmp_path / "b").mkdir() is None else (None, None, None)
    assert (status, reasons) == ("selected", [])


def test_negative_label_or_failed_request_is_not_router_ok(tmp_path, monkeypatch):
    status, reasons, oc = _run(tmp_path / "a", monkeypatch, labels=["gate_pass", "review_changes"])
    assert status == "excluded" and reasons == ["router_not_ok"]
    assert oc["router"]["negative"] == ["review_changes"]
    status, reasons, _ = _run(tmp_path / "e", monkeypatch, outcome="error", labels=["gate_pass"])
    assert status == "excluded" and reasons == ["router_not_ok"]


def test_default_policy_and_frontier_rule_are_unchanged(tmp_path, monkeypatch):
    # allow_frontier_targets false: a verified frontier output is still not a target
    status, reasons, _ = _run(tmp_path / "a", monkeypatch, allow=False, labels=["gate_pass"])
    assert status == "excluded" and "non_local_author" in reasons
    # and without a router token in the policy the default requirements still apply
    status, reasons, oc = _run(tmp_path / "d", monkeypatch, labels=["gate_pass"], policy=False)
    assert status == "excluded" and "unverified" in reasons and "non_local_author" in reasons
    assert oc["router"]["ok"]                                # signal recorded, not demanded
