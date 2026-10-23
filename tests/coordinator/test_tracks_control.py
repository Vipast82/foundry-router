"""control.py track actions (tracks_analyze, tracks_propose, track_start,
track_stop, track_merge, track_status, set_max_agents): project-scoped,
applied through the engine's TrackManager, reported with bounded results;
TrackErrors come back `rejected`; the heartbeat advertises the `tracks`
feature and carries the per-project tracks block the GUI reads."""

from __future__ import annotations

from foundry_coordinator import states as S
from foundry_coordinator.control import TRACK_KINDS, ControlChannel
from tests.coordinator.helpers import (FakeFoundry, by_task, coordinator, make_repo, run_until,
                                       write_ok, write_profile)

TASKS = [
    {"id": "T1", "title": "main", "spec": "x", "scope": ["src/a.txt"]},
    {"id": "B1", "title": "blender", "spec": "x", "scope": ["art/b.txt"], "tags": ["needs:blender"]},
    {"id": "A1", "title": "audio", "spec": "x", "scope": ["docs/a.txt"], "tags": ["needs:audio"]},
]


class Ctl(FakeFoundry):
    def __init__(self):
        super().__init__()
        self.reports = []

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append((action_id, status, detail, result))

    def heartbeat(self, beat):
        self.beat = beat

    def claim_actions(self, source, projects, limit=20):
        return []


def env(tmp_path):
    root, _ = make_repo(tmp_path, "game", TASKS)
    prof = write_profile(tmp_path, "game", root, extra="\n[automation]\nmax_agents = 2\n",
                         publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({
        "T1": write_ok("src/a.txt"), "B1": write_ok("art/b.txt"), "A1": write_ok("docs/a.txt")})},
        max_writers=None)
    ctl = Ctl()
    ch = ControlChannel(c, foundry=ctl, source_id="coordinator:test", heartbeat_every_s=0)
    return c, c.projects["game"], ch, ctl


def act(ch, ctl, kind, payload=None, aid=None):
    aid = aid or f"act-{kind}-{len(ctl.reports)}"
    ch.handle({"action_id": aid, "kind": kind, "project_id": "game", "payload": payload or {},
               "actor": "victor"})
    return ctl.reports[-1]


def test_heartbeat_declares_tracks_and_carries_the_status_block(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    beat = ch._beat()
    assert "tracks" in beat["features"]
    assert set(TRACK_KINDS) <= set(beat["source"]["capabilities"]["actions"])
    st = beat["tracks"]["game"]
    assert st["max_agents"] == 2 and st["tracks"] == [] and st["studio"]["in_use"] == ""
    assert st["main"]["quiescent"] is True


def test_analyze_propose_start_status_stop_through_actions(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    c.approve("game", "T1", [], "victor")
    c.tick()                                                      # T1 is the main sprint
    _, status, detail, res = act(ch, ctl, "tracks_analyze")
    assert status == "applied" and "3 track(s)" in detail
    assert res["tracks"][0]["kind"] == "main" and res["tracks"][0]["sprints"] == ["T1"]
    assert res["max_agents"] == 2 and res["sprints"]["B1"]["needs"] == ["blender"]
    _, status, detail, plan = act(ch, ctl, "tracks_propose", {"max_agents": 3})
    assert status == "applied" and plan["plan_id"].startswith("trp-") and plan["agents"] == 3
    assert {t["name"] for t in plan["tracks"]} == {"b1", "a1"}
    _, status, detail, res = act(ch, ctl, "track_start", {"plan_id": plan["plan_id"],
                                                          "names": ["b1"]})
    assert status == "applied" and detail.startswith("started b1 (B1)")
    assert res["tracks"][0]["branch"] == "track/b1" and res["tracks"][0]["state"] == "running"
    assert c.store.task_track("game", "B1") == "b1"
    # the same action id never applies twice (op key)
    before = len(ctl.reports)
    ch.handle({"action_id": ctl.reports[-1][0], "kind": "track_start", "project_id": "game",
               "payload": {"plan_id": plan["plan_id"]}, "actor": "victor"})
    assert ctl.reports[-1][1] == "applied" and len(c.store.tracks("game")) == 1
    _, status, _, st = act(ch, ctl, "track_status")
    assert status == "applied" and st["tracks"][0]["name"] == "b1"
    assert st["tracks"][0]["sprints"][0]["state"] in (S.QUEUED, S.READY)
    _, status, detail, res = act(ch, ctl, "track_start", {"name": "audio", "sprints": ["A1"]})
    assert status == "applied" and res["tracks"][0]["needs"] == ["audio", "gpu"]
    _, status, detail, res = act(ch, ctl, "track_stop", {"name": "audio", "reason": "later"})
    assert status == "applied" and res["state"] == "stopped"
    assert c.store.task("game", "A1")["state"] == S.CANCELLED


def test_rejections_are_reported_not_raised(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    assert act(ch, ctl, "track_stop", {"name": "nope"})[1:3] == ("rejected", "no track nope")
    assert act(ch, ctl, "track_start", {"name": "Bad Name", "sprints": ["B1"]})[1] == "rejected"
    assert act(ch, ctl, "track_merge", {"name": "x"})[1] == "rejected"
    assert act(ch, ctl, "set_max_agents", {"max_agents": 99})[1] == "rejected"
    assert act(ch, ctl, "track_start", {"plan_id": "trp-000000000000"})[1] == "rejected"
    _, status, detail, res = act(ch, ctl, "set_max_agents", {"max_agents": 3})
    assert status == "applied" and res == {"max_agents": 3} and c.max_writers == 3


def test_merge_request_and_conflict_decision_through_actions(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    act(ch, ctl, "track_start", {"name": "b", "sprints": ["B1"]})
    assert act(ch, ctl, "track_merge", {"name": "b"})[1] == "rejected"      # not finished
    assert run_until(c, lambda: c.store.track("game", "b")["state"] == "merged", limit=60)
    assert act(ch, ctl, "track_merge", {"name": "b"})[1] == "rejected"      # already merged
    # a decision answer on a track without one is rejected with the reason
    _, status, detail, _ = act(ch, ctl, "track_merge", {"name": "b", "option": "retry the merge"})
    assert status == "rejected" and "no open decision" in detail
