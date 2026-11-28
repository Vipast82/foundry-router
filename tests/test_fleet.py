"""Fleet (fleet.py, GET /admin/api/fleet): one row per machine across all projects, joined from
Access → Machines, the workstation check-ins, the Foundry Client and coordinator heartbeats and
the deployed settings; the three lane checks of a slot kept apart; the consistency warnings."""

import time

from foundry_automation import document as D
from foundry_router import __version__
from foundry_router.config import ClientConfig
from tests.test_control_center import COORD, cc  # noqa: F401


def _deploy(app, project, slots=None, remote=False, kind="kilo"):
    d = D.empty(project)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    d["settings"]["client"] = {"kind": kind}
    d["settings"]["automation"] = {"remote_lanes": remote}
    if slots:
        d["settings"]["slots"] = slots
    app.state.services.automation._append(project, d, "test", "", "test")


def _beat(client, kind, host, version, **extra):
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": f"{kind}:{host}", "kind": kind, "host": host, "version": version},
        "contract": "foundry.cc.v1", "coordinator_version": version, "projects": ["proj"],
        "sent_ts": time.time(), **extra})
    assert r.status_code == 200, r.text


TRACKS = {"proj": {
    "max_agents": 3, "agents_running": 2, "main": {"sprint": "M-1", "quiescent": False},
    "tracks": [{"name": "lane-a", "state": "running", "host": "slot-2", "slot": "slot-2",
                "current_sprint": "S-1", "sprints": [], "needs": ["studio"], "merge": {}},
               {"name": "here-b", "state": "running", "host": "", "slot": "slot-3",
                "current_sprint": "S-2", "sprints": [], "needs": [], "merge": {}},
               {"name": "old", "state": "merged", "host": "slot-2", "current_sprint": "",
                "sprints": [], "needs": [], "merge": {}}],
    "lane_tunnels": {"slot-2": {"state": "up", "since": 1.0, "error": "", "failures": 0},
                     "slot-3": {"state": "down", "since": 2.0, "error": "connection refused",
                                "failures": 4}},
    "slots": {"slot-2": {"lanes": True, "os": "linux", "studio": "container", "disk_pct": 42,
                         "foundry": __version__},
              "slot-3": {"lanes": False, "os": "windows", "studio": "native", "disk_pct": 91,
                         "foundry": "0.181.200"}},
    "remote_lanes": True, "client_kind": "opencode", "junction": {"disk_pct": 61},
    "studio": {"journal": []}}}


def _fleet_env(app, client):
    svc = app.state.services
    _deploy(app, "proj", kind="opencode", remote=True,
            slots={"slot-2": {"container": "studio-slot", "lanes": True},
                   "slot-3": {"os": "windows", "studio": "native", "lanes": False}})
    _deploy(app, "proj2", remote=True)                       # remote lanes, no slot at all
    for slot, ip in (("slot-2", "10.0.0.2"), ("slot-3", "10.0.0.3")):
        r = client.post("/admin/api/machines", json={"kind": "slot", "project": "proj",
                                                     "slot": slot, "ip": ip})
        assert r.status_code == 200, r.text
    # slot-3 has lane keys (a client key named like one) but its lanes setting is off;
    # slot-2 has its lanes setting on but no keys
    svc.config_store.config.clients.append(
        ClientConfig(name="slot-3-kilo", key="slot3-kilo-key-123456", scopes=["inference"],
                     note="lane key"))
    svc.machines.claim("proj", "coordinator", "ws-pc", "10.0.0.9")
    _beat(client, "coordinator", "ws-pc", "0.181.1", features=["tracks", "track_lanes"],
          tracks=TRACKS)
    _beat(client, "client", "ws-pc", __version__, client_version=__version__,
          features=["stack_control"],
          components=[{"name": "kilo", "enabled": True, "state": "running"},
                      {"name": "opencode", "enabled": False, "state": "stopped"}])


def test_fleet_joins_machines_claims_heartbeats_and_settings(app, client, cc):  # noqa: F811
    _fleet_env(app, client)
    r = client.get("/admin/api/fleet")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["router_version"] == __version__
    rows = {m["name"]: m for m in d["machines"]}
    assert set(rows) == {"ws-pc", "slot-2", "slot-3"}
    ws = rows["ws-pc"]
    assert ws["kind"] == "workstation" and ws["active_for"] == ["proj"]
    assert ws["version"] == __version__ and ws["version_ok"] is True   # the client's own version
    assert ws["components"]["opencode"]["enabled"] is False
    assert ws["disk_pct"] == 61
    assert {(x["track"], x["sprint"]) for x in ws["running"]} == {("main", "M-1"), ("here-b", "S-2")}
    s2, s3 = rows["slot-2"], rows["slot-3"]
    assert s2["kind"] == "slot" and s2["projects"] == ["proj"] and s2["os"] == "linux"
    assert s2["lanes"] == {"keys": False, "setting": True, "agent": True, "remote_lanes": True,
                           "enabled": False}
    assert s2["tunnel"]["state"] == "up" and s2["disk_pct"] == 42
    assert s2["version"] == __version__ and s2["version_ok"] is True
    assert [(x["track"], x["sprint"], x.get("lane")) for x in s2["running"]] == [("lane-a", "S-1", True)]
    assert s3["os"] == "windows" and s3["studio"] == "native"
    assert s3["lanes"]["keys"] is True and s3["lanes"]["setting"] is False
    assert s3["lanes"]["agent"] is False and s3["tunnel"]["error"] == "connection refused"
    assert s3["version_ok"] is False and s3["disk_pct"] == 91
    assert [x["track"] for x in s3["running"]] == ["here-b"] and s3["running"][0]["studio_only"]
    for m in d["machines"]:
        assert m["links"] == {"machines": "/ui#Machines", "tracks": "/ui/automation#tracks"}
    projs = {p["id"]: p for p in d["projects"]}
    assert projs["proj"]["client_kind"] == "opencode" and projs["proj"]["remote_lanes"] is True
    assert projs["proj"]["workstation"] == "ws-pc" and projs["proj"]["reported"] is True
    assert projs["proj"]["slots"]["slot-2"]["lanes"] is True


def test_fleet_warns_about_what_does_not_fit(app, client, cc):  # noqa: F811
    _fleet_env(app, client)
    w = client.get("/admin/api/fleet").json()["warnings"]
    kinds = {(x["kind"], x.get("machine") or x.get("project")) for x in w}
    assert ("opencode_off", "ws-pc") in kinds                    # proj is on OpenCode
    assert ("remote_lanes_no_slot", "proj2") in kinds            # proj2: no slot has lanes
    assert ("lane_setting_no_keys", "slot-2") in kinds           # setting on, no keys
    assert ("lane_keys_setting_off", "slot-3") in kinds          # keys, setting off
    assert ("version_mismatch", "slot-3") in kinds               # v0.181.200 vs the router
    assert not any(k == "version_mismatch" and m in ("ws-pc", "slot-2") for k, m in kinds)
    text = " ".join(x["text"] for x in w)
    assert "OpenCode server is off" in text and "remote_lanes on" in text
    # proj has a slot with lanes on, so it gets no remote_lanes warning
    assert ("remote_lanes_no_slot", "proj") not in kinds


def test_fleet_is_empty_but_answers_with_nothing_set_up(client):
    r = client.get("/admin/api/fleet")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["machines"] == [] and d["warnings"] == [] and d["router_version"] == __version__
