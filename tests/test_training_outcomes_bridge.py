"""The coordinator bridge posts verified outcomes (W4): a gate PASS, review APPROVED or
vision approval event becomes one training-outcome post naming the work and the
implementer session that produced it; failures retry, final refusals drop."""

from types import SimpleNamespace

from foundry_coordinator.adapters.foundry import FoundryError
from foundry_coordinator.telemetry import TelemetryBridge


class Foundry:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def training_outcome(self, post):
        if self.fail:
            raise self.fail
        self.sent.append(post)
        return {"ok": True}


def _bridge(foundry, sessions):
    b = TelemetryBridge.__new__(TelemetryBridge)
    b.projects = {"game": SimpleNamespace(id="game")}
    b.reader = SimpleNamespace(sessions=lambda: sessions)
    b.foundry = foundry
    b.pending_outcomes = []
    return b


SESSIONS = [
    {"session_id": "a1", "project_id": "game", "task_id": "T1", "role": "implementer",
     "run_id": "T1-a1", "created_ts": 100.0},
    {"session_id": "a2", "project_id": "game", "task_id": "T1", "role": "implementer",
     "run_id": "T1-a2", "created_ts": 500.0},
    {"session_id": "r1", "project_id": "game", "task_id": "T1", "role": "reviewer",
     "run_id": "T1-r1", "created_ts": 600.0},
    {"session_id": "x", "project_id": "game", "task_id": "T2", "role": "implementer",
     "run_id": "T2-a1", "created_ts": 50.0},
]


def ev(kind, ts, detail, task="T1", project="game", i=[0]):
    i[0] += 1
    return {"id": i[0], "event_id": f"ev-{i[0]}", "ts": ts, "kind": kind, "project_id": project,
            "task_id": task, "detail": detail}


def test_verdict_events_become_posts_for_the_session_that_made_the_head():
    b = _bridge(Foundry(), SESSIONS)
    posts = b._outcome_posts([
        ev("fast_lane_gate", 300, {"result": "PASS", "head": "abc", "summary": "ok"}),
        ev("fast_lane_gate", 700, {"result": "FAIL", "head": "def"}),
        ev("fast_lane_gate", 710, {"result": "ERROR"}),               # tooling error: no label
        ev("review", 800, {"verdict": "APPROVED", "reviewed_commit_sha": "def"}),
        ev("review", 810, {"verdict": "CHANGES_REQUIRED"}),
        ev("fast_lane_vision_approved", 820, {"head": "def", "actor": "model:v/x"}),
        ev("fast_lane_vision_changes", 830, {"round": 1}),
        ev("helper_started", 840, {}),                                # not an outcome
        ev("review", 850, {"verdict": "APPROVED"}, project="other"),  # not a watched project
    ])
    assert [p["label"] for p in posts] == ["gate_pass", "gate_fail", "review_approved",
                                           "review_changes", "vision_approved", "vision_changes"]
    first = posts[0]
    assert first["project_id"] == "game" and first["task_id"] == "T1" and first["until_ts"] == 300
    assert first["session_ids"] == ["a1"] and first["run_id"] == "T1-a1"      # not the later attempt
    assert first["evidence"]["head"] == "abc" and first["evidence"]["event"] == "fast_lane_gate"
    assert posts[1]["session_ids"] == ["a2"]            # attempt 2 had started by then
    assert all("r1" not in p["session_ids"] for p in posts)    # the reviewer's session never


def test_posts_are_sent_retried_and_final_refusals_dropped():
    f = Foundry()
    b = _bridge(f, SESSIONS)
    posts = b._outcome_posts([ev("fast_lane_gate", 300, {"result": "PASS"})])
    b._send_outcomes(posts)
    assert [p["label"] for p in f.sent] == ["gate_pass"] and b.pending_outcomes == []
    down = Foundry(fail=FoundryError("POST /v1/foundry/training/outcomes: [Errno 111] refused"))
    b2 = _bridge(down, SESSIONS)
    b2._send_outcomes(posts)
    assert len(b2.pending_outcomes) == 1                 # connection trouble: kept for the next step
    b2.foundry = f
    b2._flush_outcomes()
    assert b2.pending_outcomes == [] and len(f.sent) == 2
    gone = Foundry(fail=FoundryError("POST /v1/foundry/training/outcomes: HTTP 404 not found"))
    b3 = _bridge(gone, SESSIONS)
    b3._send_outcomes(posts)
    assert b3.pending_outcomes == []                     # an older Foundry: dropped, not retried forever
    b4 = _bridge(SimpleNamespace(), SESSIONS)            # a client without the method
    b4._send_outcomes(posts)
    assert b4.pending_outcomes == []
