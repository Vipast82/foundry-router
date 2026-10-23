"""Operator chat track tools (Team Lead): analyze_tracks / propose_tracks /
track_status read; track_start / track_stop / track_merge / set_max_agents are
the `tracks` confirm class (a card, then a typed action the coordinator
claims); the router validates the payloads; the coordinator heartbeat's
`tracks` block feeds track_status and GET /control/api/tracks; the actions are
withheld from a coordinator that does not declare the `tracks` feature."""
import json

from foundry_router import chat_engine as CE
from foundry_router.control_center import TRACK_ACTIONS
from tests.test_chat_engine import FakeModel, call, chat, reply  # noqa: F401
from tests.test_control_center import COORD, cc, login  # noqa: F401

TRACKS_BLOCK = {"proj": {
    "max_agents": 2, "max_agents_override": None, "agents_running": 1,
    "main": {"sprint": "UI-03a", "quiescent": False, "why": "main sprint busy: UI-03a IMPLEMENTING"},
    "tracks": [{"name": "as-p4a", "kind": "parallel", "state": "running", "worktree": "C:/x/.tracks/as-p4a",
                "branch": "track/as-p4a", "base": "abc123", "needs": ["blender"],
                "current_sprint": "AS-P4a", "sprints": [{"task_id": "AS-P4a", "state": "IMPLEMENTING",
                                                         "next": "session working", "blocker": ""}],
                "merge": {}, "lease": False}],
    "studio": {"in_use": "main session used Studio 30s ago", "pending": "", "lease": None,
               "last_main_call_age_s": 30, "idle_minutes": 10, "max_minutes": 45,
               "journal": [{"ts": 1.0, "resource": "studio:ws", "owner": "proj/UI-03a",
                            "event": "acquire", "purpose": "implement", "detail": {}}]}}}


def coord_beat(client, features=("project_pause", "tracks"), tracks=None, sid="coordinator:ws"):
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": sid, "kind": "coordinator", "version": "0.134.2"},
        "contract": "foundry.cc.v1", "coordinator_version": "0.134.2",
        "features": list(features), "projects": ["proj"],
        **({"tracks": tracks} if tracks is not None else {})})
    assert r.status_code == 200, r.text
    return r


def test_tools_are_in_the_catalog_with_the_tracks_class(chat):
    eng = chat.eng
    for name in ("analyze_tracks", "propose_tracks", "track_status", "track_start", "track_stop",
                 "track_merge", "set_max_agents"):
        assert name in eng.tools, name
    assert eng.tools["analyze_tracks"].cls == "read" and eng.tools["track_status"].cls == "read"
    assert eng.tools["propose_tracks"].cls == "draft"
    for name in ("track_start", "track_stop", "track_merge", "set_max_agents"):
        assert eng.tools[name].cls == "tracks"
    assert "tracks" in CE.CONFIRM_CLASSES and "tracks" in CE.CLASS_LABEL
    assert "Parallel tracks" in eng._system()


def test_track_status_reads_the_coordinator_heartbeat_and_the_api(chat, client, app):
    app.state.services.automation.draft("proj")
    t = chat("tracks?", reply("", [call("track_status", project="proj")]), reply("none"))
    res = t["results"][0]["result"]
    assert res["tracks"] == [] and "not reported" in res["note"]
    coord_beat(client, tracks=TRACKS_BLOCK)
    t = chat("tracks?", reply("", [call("track_status", project="proj")]), reply("one"))
    res = t["results"][0]["result"]
    assert res["max_agents"] == 2 and res["tracks"][0]["name"] == "as-p4a"
    assert res["tracks"][0]["sprints"][0]["state"] == "IMPLEMENTING"
    assert res["studio"]["in_use"].startswith("main session used Studio")
    assert res["studio"]["journal"][0]["owner"] == "proj/UI-03a"
    r = client.get("/control/api/tracks?project=proj", headers=chat.h)
    assert r.status_code == 200
    assert r.json()["tracks"]["proj"]["tracks"][0]["branch"] == "track/as-p4a"
    assert r.json()["source"] == "coordinator:ws"


def test_reads_file_actions_and_wait_for_the_coordinator(chat, client, app, cc):  # noqa: F811
    app.state.services.automation.draft("proj")
    waited = []

    async def _no_wait(aid, s):
        waited.append((aid, s))
        return {"status": "accepted", "status_detail": "waiting"}
    chat.eng._wait_action = _no_wait
    t = chat("what can run in parallel?",
             reply("", [call("analyze_tracks", project="proj"),
                        call("propose_tracks", "c2", project="proj", max_agents=3)]), reply("hm"))
    r1, r2 = t["results"]
    assert r1["ok"] and r1["result"]["status"] == "accepted" and "not answered" in r1["result"]["note"]
    assert cc.action_view(r1["result"]["action_id"])["kind"] == "tracks_analyze"
    a2 = cc.action_view(r2["result"]["action_id"])
    assert a2["kind"] == "tracks_propose" and a2["summary"] == "propose tracks for 3 agents"
    assert [s for _, s in waited] == [60, 60]
    # an applied read comes back with the coordinator's result
    async def _applied(aid, s):
        return {"status": "applied", "result": {"plan_id": "trp-0123456789ab", "agents": 2,
                                                "tracks": [{"name": "as-p4a", "sprints": ["AS-P4a"],
                                                            "needs": ["blender"]}]}}
    chat.eng._wait_action = _applied
    t = chat("propose", reply("", [call("propose_tracks", project="proj")]), reply("ok"))
    assert t["results"][0]["result"]["plan_id"] == "trp-0123456789ab"
    # a rejected one is an error the model sees
    async def _rejected(aid, s):
        return {"status": "rejected", "status_detail": "no plans"}
    chat.eng._wait_action = _rejected
    t = chat("analyze", reply("", [call("analyze_tracks", project="proj")]), reply("ok"))
    assert t["results"][0]["ok"] is False and "rejected it: no plans" in t["results"][0]["error"]


def test_track_start_needs_the_card_and_the_card_shows_the_proposal(chat, client, app, cc):  # noqa: F811
    app.state.services.automation.draft("proj")
    coord_beat(client, tracks=TRACKS_BLOCK)
    # a proposal the coordinator answered earlier (what the card previews)
    r = client.post("/control/api/actions", headers=chat.h, json={
        "kind": "tracks_propose", "project_id": "proj", "idempotency_key": "prop-abcdefgh"})
    aid = r.json()["action_id"]
    cl = client.post("/v1/foundry/control/claim", headers=COORD,
                     json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()
    assert [x["action_id"] for x in cl["actions"]] == [aid]
    plan = {"plan_id": "trp-0123456789ab", "agents": 2, "max_agents": 2,
            "main": {"sprints": ["UI-03a"]},
            "tracks": [{"name": "as-p4a", "sprints": ["AS-P4a"], "needs": ["blender"]}], "notes": []}
    client.post(f"/v1/foundry/control/actions/{aid}/status", headers=COORD,
                json={"source_id": "coordinator:ws", "status": "applied", "detail": "ok",
                      "result": plan})
    t = chat("start them", reply("", [call("track_start", project="proj",
                                           plan_id="trp-0123456789ab")]), reply("card"))
    card = t["confirm"]
    assert card and card["calls"][0]["class"] == "tracks"
    assert card["calls"][0]["summary"] == "start the tracks of trp-0123456789ab on proj"
    assert card["calls"][0]["preview"]["plan"]["tracks"][0]["name"] == "as-p4a"
    assert t["tool_calls"] == 0                                     # nothing ran yet
    async def _no_wait(aid, s):
        return {"status": "accepted", "status_detail": "waiting"}
    chat.eng._wait_action = _no_wait
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    res = r.json()["results"][0]
    assert res["ok"], res
    a = cc.action_view(res["result"]["action_id"])
    assert a["kind"] == "track_start" and a["project_id"] == "proj"
    acts = client.post("/v1/foundry/control/claim", headers=COORD,
                       json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"]
    assert acts[0]["payload"]["plan_id"] == "trp-0123456789ab"
    assert acts[0]["payload"]["chat"]["thread"] == t["_thread"]


def test_stop_merge_and_max_agents_validate_and_preview(chat, client, app, cc):  # noqa: F811
    app.state.services.automation.draft("proj")
    coord_beat(client, tracks=TRACKS_BLOCK)
    t = chat("stop it, set 3", reply("", [
        call("track_stop", project="proj", name="as-p4a", reason="later"),
        call("track_merge", "c2", project="proj", name="as-p4a", option="retry the merge"),
        call("set_max_agents", "c3", project="proj", max_agents=3),
        call("set_max_agents", "c4", project="proj", max_agents=99),
        call("track_start", "c5", project="proj")]), reply("card"))
    reasons = {r["id"]: r["reason"] for r in t["refused"]}
    assert not reasons                                               # validated at run time
    card = t["confirm"]
    assert [c["summary"] for c in card["calls"]] == [
        "stop track as-p4a on proj: later", "track as-p4a: retry the merge",
        "set max_agents to 3 (proj)", "set max_agents to 99 (proj)", "start track None on proj: "]
    assert card["calls"][0]["preview"]["tracks"][0]["name"] == "as-p4a"
    async def _no_wait(aid, s):
        return {"status": "accepted", "status_detail": "waiting"}
    chat.eng._wait_action = _no_wait
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1", "c2", "c3", "c4", "c5"]})
    res = {x["id"]: x for x in r.json()["results"]}
    assert res["c1"]["ok"] and res["c2"]["ok"] and res["c3"]["ok"]
    assert res["c4"]["ok"] is False and "1 to 16" in res["c4"]["error"]
    assert res["c5"]["ok"] is False and "plan_id" in res["c5"]["error"]
    kinds = {cc.action_view(res[k]["result"]["action_id"])["kind"] for k in ("c1", "c2", "c3")}
    assert kinds == {"track_stop", "track_merge", "set_max_agents"}
    a = cc.action_view(res["c2"]["result"]["action_id"])
    assert a["summary"] == "track as-p4a: retry the merge"


def test_router_validates_track_action_bodies_and_feature_gates_claims(client, cc):  # noqa: F811
    h = login(client)

    def post(body, key):
        return client.post("/control/api/actions", headers=h, json={
            "project_id": "proj", "idempotency_key": key, **body})
    assert post({"kind": "track_start", "name": "Bad Name", "sprints": ["X"]}, "k-00000001").status_code == 400
    assert post({"kind": "track_start", "name": "ok", "sprints": []}, "k-00000002").status_code == 400
    assert post({"kind": "track_start", "plan_id": "nope"}, "k-00000003").status_code == 400
    assert post({"kind": "set_max_agents", "max_agents": 0}, "k-00000004").status_code == 400
    assert post({"kind": "set_max_agents", "max_agents": "2"}, "k-00000005").status_code == 400
    assert post({"kind": "track_stop"}, "k-00000006").status_code == 400
    assert client.post("/control/api/actions", headers=h, json={
        "kind": "tracks_analyze", "idempotency_key": "k-00000007"}).status_code == 400   # no project
    r = post({"kind": "track_start", "name": "AS-P4A", "sprints": ["AS-P4a"], "needs": ["blender"]},
             "k-00000008")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["summary"] == "start track as-p4a: AS-P4a" and a["task_id"] == ""
    # a project-restricted operator may not act on this project
    from tests.test_control_center import OP_B_KEY
    hb = login(client, OP_B_KEY)
    assert client.post("/control/api/actions", headers=hb, json={
        "kind": "track_status", "project_id": "proj", "idempotency_key": "k-00000009"}).status_code == 403
    # a coordinator without the `tracks` feature is not handed track actions
    coord_beat(client, features=("project_pause",), sid="coordinator:old")
    cl = client.post("/v1/foundry/control/claim", headers=COORD,
                     json={"source_id": "coordinator:old", "projects": ["proj"]}).json()
    assert [x["action_id"] for x in cl["actions"]] == []
    assert "does not declare the tracks feature" in cc.action_view(a["action_id"])["status_detail"]
    coord_beat(client, sid="coordinator:new")
    cl = client.post("/v1/foundry/control/claim", headers=COORD,
                     json={"source_id": "coordinator:new", "projects": ["proj"]}).json()
    assert [x["action_id"] for x in cl["actions"]] == [a["action_id"]]
    assert cl["actions"][0]["payload"] == {"name": "as-p4a", "sprints": ["AS-P4a"],
                                           "needs": ["blender"]}
    assert set(TRACK_ACTIONS) == {"tracks_analyze", "tracks_propose", "track_start", "track_stop",
                                  "track_merge", "track_status", "set_max_agents"}
