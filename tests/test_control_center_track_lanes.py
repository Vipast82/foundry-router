"""Distributed Foundry, the router side of the Tracks tab's lane buttons: POST
/control/api/actions takes track_bring_home {name, force_local?}, track_set_slot {name, slot}
and track_start's `slot` / `slots`, refuses bad input with a 400, and hands the two new kinds
only to a coordinator that declares `track_lanes` (an older one would refuse them)."""

from foundry_router.control_center import (ACTION_KINDS, FEATURE_GATED_ACTIONS, TRACK_ACTIONS,
                                           TRACK_LANE_ACTIONS)
from tests.test_chat_tracks import coord_beat
from tests.test_control_center import COORD, cc, login  # noqa: F401


def _post(client, h, body, key):
    return client.post("/control/api/actions", headers=h, json={
        "project_id": "proj", "idempotency_key": key, **body})


def _claim(client, sid):
    return client.post("/v1/foundry/control/claim", headers=COORD,
                       json={"source_id": sid, "projects": ["proj"]}).json()["actions"]


def test_the_new_kinds_are_known_and_gated_on_track_lanes():
    for k in ("track_bring_home", "track_set_slot"):
        assert k in ACTION_KINDS and k in TRACK_ACTIONS and k in TRACK_LANE_ACTIONS
        assert FEATURE_GATED_ACTIONS[k] == "track_lanes"
    assert FEATURE_GATED_ACTIONS["track_start"] == "tracks"


def test_bad_lane_action_bodies_are_refused(client, cc):  # noqa: F811
    h = login(client)
    bad = [
        {"kind": "track_bring_home"},                                       # no name
        {"kind": "track_bring_home", "name": "Bad Name"},
        {"kind": "track_bring_home", "name": "l1", "force_local": "yes"},  # not a bool
        {"kind": "track_set_slot", "name": "l1"},                           # no slot at all
        {"kind": "track_set_slot", "name": "l1", "slot": "slot 2; rm -rf"},
        {"kind": "track_set_slot", "name": "l1", "slot": 3},
        {"kind": "track_start", "name": "l1", "sprints": ["S1"], "slot": "../x"},
        {"kind": "track_start", "plan_id": "trp-0123456789ab", "slots": ["slot-2"]},
        {"kind": "track_start", "plan_id": "trp-0123456789ab", "slots": {"Bad": "slot-2"}},
        {"kind": "track_start", "plan_id": "trp-0123456789ab", "slots": {"a": "slot-2", "b": "slot-2"}},
    ]
    for i, body in enumerate(bad):
        r = _post(client, h, body, f"lane-bad-{i:04d}")
        assert r.status_code == 400, (body, r.text)


def test_lane_actions_are_stored_and_handed_only_to_a_coordinator_with_track_lanes(client, cc):  # noqa: F811
    h = login(client)
    r = _post(client, h, {"kind": "track_bring_home", "name": "L1", "force_local": True}, "lane-ok-0001")
    assert r.status_code == 200, r.text
    home = r.json()
    assert home["summary"] == "bring lane l1 home from the last commit fetched (its slot is away)"
    r = _post(client, h, {"kind": "track_set_slot", "name": "l1", "slot": "none"}, "lane-ok-0002")
    unbind = r.json()
    assert r.status_code == 200 and unbind["summary"] == "unbind track l1 from its slot"
    r = _post(client, h, {"kind": "track_set_slot", "name": "l1", "slot": "slot-3"}, "lane-ok-0003")
    move = r.json()
    assert move["summary"] == "move track l1 to slot-3"
    r = _post(client, h, {"kind": "track_start", "name": "l2", "sprints": ["S2"], "slot": "slot-2"},
              "lane-ok-0004")
    start = r.json()
    assert r.status_code == 200 and start["summary"] == "start track l2: S2 on slot-2"
    r = _post(client, h, {"kind": "track_start", "plan_id": "trp-0123456789ab",
                          "slots": {"b1": "slot-2", "a1": ""}}, "lane-ok-0005")
    plan = r.json()
    assert r.status_code == 200 and plan["summary"].endswith("; b1 on slot-2")
    # a coordinator with `tracks` but not `track_lanes` gets the starts, not the lane kinds
    coord_beat(client, features=("project_pause", "tracks"), sid="coordinator:old")
    got = {a["action_id"]: a for a in _claim(client, "coordinator:old")}
    assert set(got) == {start["action_id"], plan["action_id"]}
    assert got[start["action_id"]]["payload"] == {"name": "l2", "sprints": ["S2"], "slot": "slot-2"}
    assert got[plan["action_id"]]["payload"] == {"plan_id": "trp-0123456789ab",
                                                 "slots": {"b1": "slot-2"}}
    assert "track_lanes" in cc.action_view(home["action_id"])["status_detail"]
    coord_beat(client, features=("project_pause", "tracks", "track_lanes"), sid="coordinator:new")
    got = {a["action_id"]: a for a in _claim(client, "coordinator:new")}
    assert set(got) == {home["action_id"], unbind["action_id"], move["action_id"]}
    assert got[home["action_id"]]["payload"] == {"name": "l1", "force_local": True}
    assert got[unbind["action_id"]]["payload"] == {"name": "l1", "slot": "none"}
    assert got[move["action_id"]]["payload"] == {"name": "l1", "slot": "slot-3"}


def test_the_tracks_tab_reads_the_lane_fields_the_coordinator_sends(client, cc):  # noqa: F811
    block = {"proj": {"max_agents": 2, "agents_running": 1, "main": {"sprint": "", "quiescent": True},
                      "tracks": [{"name": "l1", "state": "running", "host": "slot-2", "slot": "slot-2",
                                  "sprints": [], "needs": ["studio"], "merge": {}}],
                      "lane_tunnels": {"slot-2": {"state": "up", "since": 1.0, "error": "",
                                                  "failures": 0, "remote": "studio@10.0.0.2"}},
                      "slots": {"slot-2": {"lanes": True, "os": "linux", "studio": "container"}},
                      "remote_lanes": True, "studio": {"journal": []}}}
    coord_beat(client, features=("project_pause", "tracks", "track_lanes"), tracks=block)
    h = login(client)
    r = client.get("/control/api/tracks?project=proj", headers=h)
    assert r.status_code == 200, r.text
    st = r.json()["tracks"]["proj"]
    assert st["tracks"][0]["host"] == "slot-2" and st["lane_tunnels"]["slot-2"]["state"] == "up"
    assert st["slots"]["slot-2"]["lanes"] is True and st["remote_lanes"] is True
