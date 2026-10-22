"""Sprint scorecard (W4): GET /admin/api/work/scorecard joins the coordinator's task
state and events (gates, reviews, vision, helpers, with durations) with the ledger,
perf samples and request log by project and work task."""

import time

import pytest

from foundry_router.config import ClientConfig
from tests.test_budgets import STATE, ledgered  # noqa: F401 (fixture)
from tests.test_training_capture import COORD, _chat

NOW = time.time()


@pytest.fixture(autouse=True)
def _coordinator_client(ledgered):
    ledgered.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration", "admin"]))


def _bind(client, sid, role, task="T1", project="game"):
    assert client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": project, "task_id": task, "run_id": f"{task}-a1",
        "role": role}).status_code == 200


def _events(client, evs, task="T1", project="game"):
    body = [{"event_id": f"e{i}-{task}", "ts": ts, "project_id": project, "task_id": task,
             "kind": kind, "state": "", "detail": d} for i, (ts, kind, d) in enumerate(evs)]
    r = client.post("/v1/foundry/work/events", headers=COORD, json={"events": body})
    assert r.status_code == 200 and r.json()["recorded"] == len(body)


def _task(client, state, task="T1", project="game", **data):
    assert client.put(f"/v1/foundry/work/tasks/{project}/{task}", headers=COORD,
                      json={"state": state, **data}).status_code == 200


def _seed(client):
    _bind(client, "impl", "implementer")
    _bind(client, "rev", "reviewer")
    for sid in ("impl", "impl", "rev"):
        assert _chat(client, sid).status_code == 200
    t0 = NOW - 3600
    _events(client, [
        (t0, "transition", {}),
        (t0 + 100, "fast_lane_gate", {"result": "FAIL", "duration_s": 60.0, "checks_total": 5,
                                      "checks_failed": 2}),
        (t0 + 101, "fast_lane_red_gate", {"n": 1, "duration_s": 60.0}),
        (t0 + 300, "fast_lane_gate", {"result": "PASS", "duration_s": 30.5, "checks_total": 5,
                                      "checks_failed": 0}),
        (t0 + 330, "fast_lane_vision_changes", {"round": 1, "max": 2, "findings": 3}),
        (t0 + 500, "fast_lane_vision_approved", {"rounds": 1, "frames": 12}),
        (t0 + 520, "review", {"verdict": "CHANGES_REQUIRED", "role": "reviewer"}),
        (t0 + 700, "review", {"verdict": "APPROVED", "role": "reviewer"}),
        (t0 + 10, "helper_started", {"unit": "u1", "kind": "test_design"}),
        (t0 + 40, "helper_done", {"unit": "u1", "reason": "ok", "duration_s": 30.0,
                                  "tokens": {"total": 1234}}),
        (t0 + 20, "helper_started", {"unit": "u2", "kind": "exploration"}),
        (t0 + 90, "helper_timed_out", {"unit": "u2", "reason": "waited", "duration_s": 70.0}),
        (t0 + 600, "helper_feedback", {"unit": "u1", "status": "integrated"}),
        (t0 + 601, "helper_feedback", {"unit": "u2", "status": "rejected"}),
        (t0 + 900, "transition", {}),
    ])
    _task(client, "COMPLETE", attempt=2)
    return t0


def test_scorecard_row_for_a_finished_sprint(ledgered, client):
    t0 = _seed(client)
    r = client.get("/admin/api/work/scorecard?project=game").json()
    [s] = r["sprints"]
    assert (s["project_id"], s["task_id"], s["outcome"], s["attempts"]) == ("game", "T1",
                                                                            "COMPLETE", 2)
    assert abs(s["wall_s"] - 900) < 2 or s["wall_s"] >= 900       # first to last event
    assert s["model_calls"] == 3 and s["model_errors"] == 0
    assert s["tokens"]["by_role"]["implementer"]["calls"] == 2
    assert s["tokens"]["by_role"]["reviewer"]["calls"] == 1
    assert s["tokens"]["input"] > 0 and s["tokens"]["output"] > 0
    assert s["tokens"]["by_backend"]["strata"]["calls"] == 3
    assert s["gates"] == {"run": 2, "passed": 1, "failed": 1, "red": 1, "total_s": 90.5,
                          "avg_s": 45.2}
    assert s["reviews"] == {"total": 2, "approved": 1, "changes": 1}
    assert s["vision"] == {"rounds": 1, "approved": 1, "changes": 1}
    h = s["helpers"]
    assert (h["used"], h["finished"], h["integrated"], h["rejected"]) == (2, 2, 1, 1)
    assert h["total_s"] == 100.0 and h["tokens"] == 1234
    # speed comes from perf_samples of this sprint (older rows would join through request_log)
    sp = s["speed"]
    assert sum((sp[k] or {}).get("n", 0) for k in ("measured", "estimated", "unknown")) == 3
    assert s["cost"]["est_cost_usd"] == 0.0 and s["cost"]["sub_cost_usd"] == 0.0
    t = r["totals"]
    assert (t["sprints"], t["complete"], t["model_calls"], t["gates_run"], t["gates_passed"],
            t["reviews"], t["vision_rounds"], t["helpers_used"],
            t["helpers_integrated"]) == (1, 1, 3, 2, 1, 2, 1, 2, 1)


def test_scorecard_filters_and_unreported_numbers_are_null(ledgered, client):
    _bind(client, "other", "implementer", task="T9", project="elsewhere")
    _chat(client, "other")
    _events(client, [(NOW - 100, "fast_lane_gate", {"result": "PASS"}),      # no duration sent
                     (NOW - 90, "helper_started", {"unit": "u"}),
                     (NOW - 80, "helper_done", {"unit": "u", "reason": "ok"})],
            task="T9", project="elsewhere")
    _task(client, "IMPLEMENTING", task="T9", project="elsewhere")
    _seed(client)
    assert {s["project_id"] for s in client.get("/admin/api/work/scorecard").json()["sprints"]} \
        == {"game", "elsewhere"}
    only = client.get("/admin/api/work/scorecard?project=elsewhere").json()["sprints"]
    assert [s["task_id"] for s in only] == ["T9"] and only[0]["outcome"] == "IMPLEMENTING"
    assert only[0]["gates"]["total_s"] is None and only[0]["gates"]["run"] == 1
    assert only[0]["helpers"]["total_s"] is None and only[0]["model_calls"] == 1
    assert client.get("/admin/api/work/scorecard?project=game&task=NOPE").json()["sprints"] == []


def test_scorecard_speed_keeps_measured_apart_and_matches_old_samples_by_request(ledgered, client):
    from foundry_router import perf_history as ph
    _bind(client, "impl", "implementer")
    _chat(client, "impl")
    db = ledgered.db
    rid = db.query_one("SELECT request_id FROM request_log WHERE work_task_id='T1'")["request_id"]
    db.execute("DELETE FROM perf_samples")
    # a sample written before perf_samples had task columns: joined through request_log
    ph.record_sample(db, model="qwen", backend="strata", prompt_tokens=100, completion_tokens=50,
                     eval_duration_ns=1_000_000_000, wall_ms=2000, timing_source="server")
    db.execute("UPDATE perf_samples SET request_id=?, project_id=NULL, work_task_id=NULL", (rid,))
    ph.record_sample(db, model="claude", backend="meridian", prompt_tokens=100,
                     completion_tokens=50, eval_duration_ns=50_000_000, wall_ms=5000,
                     timing_source="estimated")
    db.execute("UPDATE perf_samples SET project_id='game', work_task_id='T1' WHERE model='claude'")
    _task(client, "IMPLEMENTING")
    [s] = client.get("/admin/api/work/scorecard?project=game").json()["sprints"]
    assert s["speed"]["measured"]["decode_tps"] == 50.0 and s["speed"]["measured"]["n"] == 1
    assert s["speed"]["estimated"]["decode_tps"] == 1000.0 and s["speed"]["estimated"]["n"] == 1


def test_scorecard_subscription_allocation_from_config(ledgered, client):
    from foundry_router import usage
    usage.set_subscription_rates(lambda: [{"match": "strata", "usd_per_call": 0.5}])
    try:
        _bind(client, "impl", "implementer")
        _chat(client, "impl")
        _task(client, "IMPLEMENTING")
        [s] = client.get("/admin/api/work/scorecard?project=game").json()["sprints"]
        assert s["cost"]["sub_cost_usd"] == 0.5 and s["cost"]["est_cost_usd"] == 0.0
    finally:
        usage.set_subscription_rates(lambda: [])
