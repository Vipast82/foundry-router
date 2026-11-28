"""Distributed Foundry, the Tracks tab's lane buttons at their entry point (control.py):
track_bring_home -> TrackManager.bring_home, track_set_slot -> set_slot, track_start's
`slot` / a proposal's `slots` -> create(slot=...), and the tracks status block the Tracks tab
and the router's Fleet view read (each track's `slot`, the project's `slots`, `remote_lanes`,
`client_kind`, `junction`). The heartbeat declares `track_lanes` so the router hands the new
kinds only to a coordinator that knows them."""

from __future__ import annotations

from foundry_coordinator.control import TRACK_KINDS, TRACK_LANES_FEATURE
from tests.coordinator.test_tracks_control import TASKS, Ctl, act
from tests.coordinator.helpers import by_task, coordinator, make_repo, write_ok, write_profile
from foundry_coordinator.control import ControlChannel

SLOTS = '\n[slots.slot-1]\ncontainer = "studio-slot-a"\n\n[slots.slot-2]\ncontainer = "studio-slot-b"\nlanes = true\nos = "linux"\n'


def env(tmp_path, slots=SLOTS):
    root, _ = make_repo(tmp_path, "game", TASKS)
    prof = write_profile(tmp_path, "game", root,
                         extra="\n[automation]\nmax_agents = 3\n" + slots,
                         publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({
        "T1": write_ok("src/a.txt"), "B1": write_ok("art/b.txt"), "A1": write_ok("docs/a.txt")})},
        max_writers=None)
    ctl = Ctl()
    ch = ControlChannel(c, foundry=ctl, source_id="coordinator:test", heartbeat_every_s=0)
    return c, c.projects["game"], ch, ctl


def test_heartbeat_declares_the_lane_kinds_and_the_status_carries_slots(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    beat = ch._beat()
    assert TRACK_LANES_FEATURE in beat["features"] and "tracks" in beat["features"]
    assert {"track_bring_home", "track_set_slot"} <= set(beat["source"]["capabilities"]["actions"])
    assert {"track_bring_home", "track_set_slot"} <= set(TRACK_KINDS)
    st = beat["tracks"]["game"]
    assert st["slots"]["slot-1"] == {"lanes": False, "os": "linux", "studio": "container"}
    assert st["slots"]["slot-2"]["lanes"] is True
    assert st["remote_lanes"] is False and st["client_kind"] == "fake"
    assert isinstance(st["junction"].get("disk_pct"), int)
    assert st["lane_tunnels"] == {} or isinstance(st["lane_tunnels"], dict)


def test_track_start_with_a_slot_binds_the_track_and_set_slot_moves_and_unbinds_it(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    _, status, detail, res = act(ch, ctl, "track_start", {"name": "b1", "sprints": ["B1"],
                                                          "slot": "slot-1"})
    assert status == "applied", detail
    row = c.store.track("game", "b1")
    assert row["data"]["slot"] == "slot-1" and "studio" in row["needs"]
    # remote_lanes is off: a slot with lanes on is still only the track's Studio, not a lane
    assert not row["data"].get("host")
    st = ch._beat()["tracks"]["game"]
    t = next(x for x in st["tracks"] if x["name"] == "b1")
    assert t["slot"] == "slot-1" and t["host"] == ""
    # Move slot: through the action, to the manager
    _, status, detail, res = act(ch, ctl, "track_set_slot", {"name": "b1", "slot": "slot-2"})
    assert status == "applied" and res == {"name": "b1", "slot": "slot-2",
                                           "needs": c.store.track("game", "b1")["needs"]}
    assert c.store.track("game", "b1")["data"]["slot"] == "slot-2"
    # unbind ("none", as the router sends it)
    _, status, detail, res = act(ch, ctl, "track_set_slot", {"name": "b1", "slot": "none"})
    assert status == "applied" and res["slot"] == "" and "bound to no slot" in detail
    assert not c.store.track("game", "b1")["data"].get("slot")
    # a slot that is not configured is refused, not raised
    _, status, detail, _ = act(ch, ctl, "track_set_slot", {"name": "b1", "slot": "slot-9"})
    assert status == "rejected" and "no enabled Studio slot 'slot-9'" in detail
    _, status, detail, _ = act(ch, ctl, "track_start", {"name": "a1", "sprints": ["A1"],
                                                        "slot": "slot-9"})
    assert status == "rejected" and "slot-9" in detail and c.store.track("game", "a1") is None


def test_bring_home_reaches_the_manager_with_force_local(tmp_path, monkeypatch):
    c, P, ch, ctl = env(tmp_path)
    act(ch, ctl, "track_start", {"name": "b1", "sprints": ["B1"]})
    # a local track: the real manager says it is already here
    _, status, detail, res = act(ch, ctl, "track_bring_home", {"name": "b1"})
    assert status == "applied" and "already here" in detail and res["ok"] is True
    calls, real = [], c.tracks.bring_home

    def fake(P_, name, actor="coordinator", why="", force_local=False):
        calls.append((P_.id, name, actor, why, force_local))
        return {"ok": False, "detail": "lane b1: its last commits are not here yet (away)"}
    monkeypatch.setattr(c.tracks, "bring_home", fake)
    _, status, detail, res = act(ch, ctl, "track_bring_home", {"name": "b1", "force_local": True})
    assert calls == [("game", "b1", "victor (control center)", "operator", True)]
    assert status == "rejected" and "not here yet" in detail and res["force_local"] is True
    act(ch, ctl, "track_bring_home", {"name": "b1", "force_local": "yes"})   # not a bool: off
    assert calls[-1][-1] is False
    # an unknown track: the manager's TrackError comes back rejected
    monkeypatch.setattr(c.tracks, "bring_home", real)
    _, status, detail, _ = act(ch, ctl, "track_bring_home", {"name": "nope"})
    assert status == "rejected" and "no track nope" in detail


def test_a_proposal_started_with_slots_creates_each_track_on_its_slot(tmp_path, monkeypatch):
    c, P, ch, ctl = env(tmp_path)
    c.approve("game", "T1", [], "victor")
    c.tick()
    _, status, _, plan = act(ch, ctl, "tracks_propose", {"max_agents": 3})
    assert status == "applied" and {t["name"] for t in plan["tracks"]} == {"b1", "a1"}
    made = []
    real = c.tracks.create

    def spy(P_, name, sprints, needs=None, actor="operator", kind="parallel", slot=""):
        made.append((name, slot))
        return real(P_, name, sprints, needs, actor, kind, slot)
    monkeypatch.setattr(c.tracks, "create", spy)
    _, status, detail, res = act(ch, ctl, "track_start", {"plan_id": plan["plan_id"],
                                                          "slots": {"b1": "slot-1"}})
    assert status == "applied", detail
    assert sorted(made) == [("a1", ""), ("b1", "slot-1")]
    assert c.store.track("game", "b1")["data"]["slot"] == "slot-1"
    assert not c.store.track("game", "a1")["data"].get("slot")
    # the proposal is spent, as with a plain start
    _, status, detail, _ = act(ch, ctl, "track_start", {"plan_id": plan["plan_id"],
                                                        "slots": {"b1": "slot-1"}})
    assert status == "rejected" and "already started" in detail


def test_a_proposal_with_a_slot_for_a_track_it_does_not_have_is_refused(tmp_path):
    c, P, ch, ctl = env(tmp_path)
    _, _, _, plan = act(ch, ctl, "tracks_propose", {"max_agents": 3})
    _, status, detail, _ = act(ch, ctl, "track_start", {"plan_id": plan["plan_id"],
                                                        "slots": {"zz": "slot-1"}})
    assert status == "rejected" and "has no track zz" in detail
    assert c.store.tracks("game") == []
