"""The cross-project playbook on the router (A1): the coordinator's @playbook
snapshot passes the ingest allow-list and is served, the chat reads it with
kb_search (fake model), kb_add / kb_edit need the confirm card and become typed
playbook_add / playbook_edit actions the router validates and hands only to a
coordinator that declares the playbook feature."""

import json

from foundry_router.control_center import CONTRACT
from tests.test_chat_engine import call, chat, reply  # noqa: F401
from tests.test_control_center import BRIDGE_A_KEY, COORD, cc, ingest, login  # noqa: F401

PB_ENTRIES = [
    {"entry_id": "p1", "file": "fixes/rojo.md", "heading": "rojo_stale: Studio behind HEAD",
     "ord": 0, "text": "- Restart Rojo through the Foundry Client; the plugin re-syncs.",
     "tags": ["fixes", "rojo", "rojo_stale", "studio"], "hash": "ph1"},
    {"entry_id": "p2", "file": "howto/evidence.md", "heading": "Evidence packets",
     "ord": 0, "text": "- Every delivered change ships its check output in the packet.",
     "tags": ["howto", "evidence"], "hash": "ph2"},
]


def playbook_obs(**kw):
    return {"type": "memory", "project_id": "@playbook", "scope": "playbook", "dir": "playbook",
            "enabled": True, "fingerprint": "pb1",
            "files": [{"file": "fixes/rojo.md", "entries": 1, "hash": "a"},
                      {"file": "howto/evidence.md", "entries": 1, "hash": "b"}],
            "entries": PB_ENTRIES,
            "slices": [{"session_id": "ses_9", "project_id": "proj", "task_id": "T-1",
                        "role": "implementer", "track": "", "ts": 1.0, "prompts": 1, "tokens": 90,
                        "budget": 2600, "hash": "s9", "live": True,
                        "entries": [{"file": "fixes/rojo.md",
                                     "heading": "rojo_stale: Studio behind HEAD",
                                     "hash": "ph1", "why": "match 7"}]}],
            "notes": [],
            "candidates": [{"key": "fail:session_error|rojo_stale", "kind": "fix", "count": 2,
                            "threshold": 2, "projects": ["proj", "other"], "status": "proposed",
                            "decision_id": "dec-1", "file": "fixes/rojo-stale.md",
                            "heading": "rojo_stale: repaired stop", "draft": "d", "last_ts": 1.0}],
            **kw}


def _beat(client, features=("project_memory", "playbook", "project_pause"), sid="coordinator:ws"):
    return client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": sid, "kind": "coordinator"}, "features": list(features),
        "projects": ["proj"]})


def test_playbook_snapshot_passes_the_allowlist_and_is_served(client, cc):  # noqa: F811
    # bridge-a's key is allow-listed to ["proj"]; the cross-project playbook
    # snapshot is memory-typed and still accepted
    r = client.post("/v1/foundry/telemetry/batch",
                    headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json={"contract": CONTRACT, "source": {"id": "bridge:a", "kind": "bridge"},
                          "observations": [playbook_obs()]})
    assert r.json()["accepted"].get("memory") == 1, r.text
    # any other type under @playbook stays refused by the allow-list
    r = client.post("/v1/foundry/telemetry/batch",
                    headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json={"contract": CONTRACT, "source": {"id": "bridge:a", "kind": "bridge"},
                          "observations": [{"type": "todo", "project_id": "@playbook",
                                            "items": []}]})
    assert r.json()["skipped"].get("project_not_allowed") == 1
    h = login(client)
    d = client.get("/control/api/memory?project=%40playbook", headers=h).json()
    data = d["snapshot"]["data"]
    assert [f["file"] for f in data["files"]] == ["fixes/rojo.md", "howto/evidence.md"]
    assert data["candidates"][0]["key"] == "fail:session_error|rojo_stale"
    assert data["slices"][0]["project_id"] == "proj"
    # an operator scoped to another project may still read the shared playbook
    d2 = client.get("/control/api/memory?project=%40playbook",
                    headers=login(client, key="op-b-key-123456789"))
    assert d2.status_code == 200 and d2.json()["snapshot"] is not None
    r = client.get("/control/api/memory/search?project=%40playbook&q=rojo%20stale", headers=h).json()
    assert r["results"][0]["file"] == "fixes/rojo.md"


def test_chat_kb_search_and_kb_add_confirm_flow(chat, client, cc, monkeypatch):  # noqa: F811
    chat.eng.svc.automation.draft("proj")
    ingest(client, playbook_obs())
    t = chat("how do we fix a stale rojo?",
             reply("", [call("kb_search", query="rojo stale")]),
             reply("Restart Rojo through the client."))
    assert t["results"][0]["ok"], t["results"]
    hits = t["results"][0]["result"]
    assert hits["results"][0]["file"] == "fixes/rojo.md" and hits["candidates_pending"] == 1
    # kb_add: confirm card -> typed playbook_add action
    t2 = chat("record that fix",
              reply("", [call("kb_add", project="proj", file="fixes/rojo.md",
                              heading="rojo_stale: Studio behind HEAD",
                              text="Restart Rojo; never edit in Studio to catch up.")]),
              reply("Filed for your confirmation."))
    card = t2["confirm"]
    assert card and card["calls"][0]["tool"] == "kb_add"
    assert "playbook: add to fixes/rojo.md" in card["calls"][0]["summary"]

    async def no_wait(action_id, seconds):
        return cc.action_view(action_id, with_log=False)
    monkeypatch.setattr(chat.eng, "_wait_action", no_wait)
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    res = r.json()["results"][0]
    assert res["ok"], res
    aid = res["result"]["action_id"]
    a = cc.action_view(aid)
    assert a["kind"] == "playbook_add" and a["status"] == "accepted"
    assert a["summary"].startswith("playbook add to fixes/rojo.md")
    # handed only to a coordinator that declares the playbook feature
    _beat(client, features=("project_memory", "project_pause"), sid="coordinator:old")
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:old", "projects": ["proj"]})
    assert r.json()["actions"] == []
    _beat(client)
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert aid in [x["action_id"] for x in r.json()["actions"]]
    # no playbook reported: kb_search refuses clearly
    cc.db.execute("DELETE FROM cc_snapshots")
    t3 = chat("kb?", reply("", [call("kb_search", query="x")]), reply("none"))
    assert not t3["results"][0]["ok"] and "playbook" in t3["results"][0]["error"]


def test_playbook_actions_are_validated_on_the_router(client, cc):  # noqa: F811
    h = login(client)
    base = {"kind": "playbook_add", "project_id": "proj", "heading": "H", "text": "x",
            "idempotency_key": "pb-12345678"}
    for bad, why in (({"file": "DECISIONS.md"}, "file must be"),
                     ({"file": "fixes/../x.md"}, "file must be"),
                     ({"file": "nothere/x.md"}, "file must be"),
                     ({"file": "fixes/x.md", "heading": ""}, "heading"),
                     ({"file": "fixes/x.md", "text": ""}, "text is empty")):
        r = client.post("/control/api/actions", headers=h, json={**base, **bad})
        assert r.status_code == 400 and why in r.json()["error"], (bad, r.text)
    r = client.post("/control/api/actions", headers=h, json={**base, "file": "fixes/rojo"})
    assert r.status_code == 200, r.text
    a = r.json()
    payload = json.loads(cc.db.query_one("SELECT payload FROM cc_actions WHERE action_id=?",
                                         (a["action_id"],))["payload"])
    assert a["status"] == "accepted" and payload["file"] == "fixes/rojo"
