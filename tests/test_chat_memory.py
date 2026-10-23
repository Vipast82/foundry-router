"""Project memory on the router (Team Lead piece 4): the coordinator's `memory`
snapshot is ingested and served, the chat reads it with memory_search /
memory_summary (fake model), memory_add / memory_edit need the confirm card and
become typed actions the router validates and hands only to a coordinator that
declares the project_memory feature."""

import json

from foundry_router.control_center import CONTRACT
from tests.test_chat_engine import call, chat, reply  # noqa: F401
from tests.test_control_center import COORD, cc, ingest, login  # noqa: F401

ENTRIES = [
    {"entry_id": "e1", "file": "DECISIONS.md", "heading": "Studio: stories before the gate", "ord": 0,
     "text": "- Stories run before the gate; the gate never re-runs a passed story.",
     "tags": ["decisions", "studio", "gate", "stories"], "hash": "h1"},
    {"entry_id": "e2", "file": "DECISIONS.md", "heading": "Blender: export scale", "ord": 1,
     "text": "- Blender exports use scale 1.0 (T2).", "tags": ["decisions", "T2", "blender"], "hash": "h2"},
    {"entry_id": "e3", "file": "KNOWN_ISSUES.md", "heading": "Rojo", "ord": 0,
     "text": "- Rojo drops the plugin on a sourcemap rebuild.", "tags": ["known_issues", "rojo"],
     "hash": "h3"},
    {"entry_id": "e4", "file": "ASSETS.md", "heading": "Registry", "ord": 0,
     "text": "| Wolf | assets/wolf.fbx | T2 | imported | ok |", "tags": ["assets", "T2", "wolf"],
     "hash": "h4"},
]


def memory_obs(**kw):
    return {"type": "memory", "project_id": "proj", "scope": "workspace", "dir": "docs/memory",
            "enabled": True, "fingerprint": "fp1",
            "files": [{"file": "DECISIONS.md", "entries": 2, "hash": "a"},
                      {"file": "KNOWN_ISSUES.md", "entries": 1, "hash": "b"},
                      {"file": "ASSETS.md", "entries": 1, "hash": "c"}],
            "entries": ENTRIES,
            "slices": [{"session_id": "ses_1", "task_id": "T-1", "role": "implementer", "track": "",
                        "ts": 1.0, "prompts": 2, "tokens": 120, "budget": 3000, "hash": "s1", "live": True,
                        "entries": [{"file": "DECISIONS.md", "heading": "Studio: stories before the gate",
                                     "hash": "h1", "why": "match 4"}]}],
            "notes": [{"task_id": "T-1", "session_id": "ses_1", "actor": "session ses_1",
                       "file": "DECISIONS.md", "heading": "Studio: stories before the gate",
                       "status": "committed", "detail": "", "sha": "abc123", "ts": 1.0}], **kw}


def _beat(client, features=("project_memory", "project_pause"), sid="coordinator:ws"):
    return client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json={
        "source": {"id": sid, "kind": "coordinator"}, "features": list(features), "projects": ["proj"]})


def test_memory_snapshot_is_served_and_searchable(client, cc):  # noqa: F811
    ingest(client, memory_obs())
    h = login(client)
    r = client.get("/control/api/memory?project=proj", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["snapshot"]["data"]["files"][0]["file"] == "DECISIONS.md"
    assert d["summary"]["files"] == {"DECISIONS.md": 2, "KNOWN_ISSUES.md": 1, "ASSETS.md": 1}
    assert d["summary"]["recent_decisions"] == ["Studio: stories before the gate", "Blender: export scale"]
    assert d["summary"]["live_slices"][0]["task"] == "T-1"
    r = client.get("/control/api/memory/search?project=proj&q=gate%20stories", headers=h).json()
    assert r["results"][0]["heading"] == "Studio: stories before the gate"
    r = client.get("/control/api/memory/search?project=proj&q=T2&file=ASSETS.md", headers=h).json()
    assert [x["file"] for x in r["results"]] == ["ASSETS.md"]
    # the same snapshot again is deduped, a changed one replaces it
    ingest(client, memory_obs())
    ingest(client, memory_obs(fingerprint="fp2", entries=ENTRIES[:1]))
    assert client.get("/control/api/memory?project=proj", headers=h).json()["summary"]["entries"] == 1
    assert client.get("/control/api/snapshots?kind=memory&project=proj", headers=h).status_code == 200
    # a project the operator may not see
    r = client.get("/control/api/memory?project=other", headers=login(client, key="op-b-key-123456789"))
    assert r.status_code == 200                                   # op-b's own project: nothing reported
    assert r.json()["snapshot"] is None and r.json()["note"]
    r = client.get("/control/api/memory?project=proj", headers=login(client, key="op-b-key-123456789"))
    assert r.status_code == 404


def test_chat_reads_memory_and_gets_the_memory_context_block(chat, client):  # noqa: F811
    chat.eng.svc.automation.draft("proj")                          # a project the chat may act on
    ingest(client, memory_obs())
    t = chat("what do we know about the gate?",
             reply("", [call("memory_search", project="proj", query="gate stories"),
                        call("memory_summary", "c2", project="proj")]),
             reply("Stories run before the gate."))
    assert t["tool_calls"] == 2 and all(r["ok"] for r in t["results"]), t["results"]
    hits = t["results"][0]["result"]["results"]
    assert hits[0]["heading"] == "Studio: stories before the gate" and hits[0]["file"] == "DECISIONS.md"
    summ = t["results"][1]["result"]
    assert summ["files"]["DECISIONS.md"] == 2 and summ["slices"][0]["entries"] == 1
    assert summ["notes"][0]["status"] == "committed"
    assert "memory" in t["context_sent"]
    ctx = next(m["content"] for m in chat.eng._model_fn.seen[0] if m["content"].startswith("Operator:"))
    assert '"memory":' in ctx and "recent_decisions" in ctx
    # a project without memory: a clear refusal, not a crash
    t2 = chat("memory?", reply("", [call("memory_search", project="proj", query="x")]), reply("none"))
    assert t2["results"][0]["ok"]
    chat.eng.svc.automation.draft("empty")
    t3 = chat("memory?", reply("", [call("memory_search", project="empty", query="x")]), reply("none"))
    assert not t3["results"][0]["ok"] and "not reported memory" in t3["results"][0]["error"]


def test_memory_add_needs_the_card_and_becomes_a_typed_action(chat, client, cc, monkeypatch):  # noqa: F811
    chat.eng.svc.automation.draft("proj")
    ingest(client, memory_obs())
    t = chat("remember: stories before the gate",
             reply("", [call("memory_add", project="proj", file="DECISIONS.md",
                             heading="Studio: stories before the gate",
                             text="The gate waits for green stories.")]),
             reply("I filed it for your confirmation."))
    card = t["confirm"]
    assert card and card["calls"][0]["class"] == "memory" and card["calls"][0]["tool"] == "memory_add"
    assert "memory: add to DECISIONS.md > Studio: stories before the gate" in card["calls"][0]["summary"]
    assert cc.actions([], "proj", "", "", 10) == []                  # nothing until the confirm

    async def no_wait(action_id, seconds):
        return cc.action_view(action_id, with_log=False)
    monkeypatch.setattr(chat.eng, "_wait_action", no_wait)
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"], res
    aid = res["result"]["action_id"]
    a = cc.action_view(aid)
    assert a["kind"] == "memory_add" and a["status"] == "accepted" and a["project_id"] == "proj"
    payload = json.loads(cc.db.query_one("SELECT payload FROM cc_actions WHERE action_id=?", (aid,))["payload"])
    assert payload["file"] == "DECISIONS.md" and payload["text"] == "The gate waits for green stories."
    assert payload["chat"]["thread"] == t["_thread"]
    assert a["summary"].startswith("memory add to DECISIONS.md > Studio: stories before the gate (proj)")
    assert "not applied it yet" in res["result"]["note"]
    # a coordinator without the feature is not handed the action; one with it is
    _beat(client, features=("project_pause",), sid="coordinator:old")
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:old", "projects": ["proj"]})
    assert r.json()["actions"] == []
    assert "project_memory" in cc.action_view(aid)["status_detail"]
    _beat(client)
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert [x["action_id"] for x in r.json()["actions"]] == [aid]
    # the router refuses a bad file / empty heading before any card is made
    t2 = chat("note it", reply("", [call("memory_add", project="proj", file="README.md", heading="x",
                                          text="y")]), reply("ok"))
    r = client.post(f"/control/api/chat/confirms/{t2['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    res = r.json()["results"][0]
    assert not res["ok"] and "file must be" in res["error"]
    # memory_edit is the same class, with its own summary
    t3 = chat("fix it", reply("", [call("memory_edit", project="proj", file="KNOWN_ISSUES.md",
                                         heading="Rojo", text="- Fixed in S40.")]), reply("ok"))
    assert t3["confirm"]["calls"][0]["summary"].startswith("memory: replace KNOWN_ISSUES.md > Rojo")


def test_memory_actions_are_validated_on_the_router(client, cc):  # noqa: F811
    h = login(client)
    base = {"kind": "memory_add", "project_id": "proj", "heading": "Rojo", "text": "x",
            "idempotency_key": "mem-12345678"}
    for bad, why in (({"file": "../x.md"}, "file must be"), ({"file": "tracks/a b.md"}, "file must be"),
                     ({"file": "DECISIONS.md", "heading": ""}, "heading"),
                     ({"file": "DECISIONS.md", "heading": "# H"}, "heading"),
                     ({"file": "DECISIONS.md", "text": ""}, "text is empty"),
                     ({"file": "DECISIONS.md", "text": "x" * 4001}, "over 4000")):
        r = client.post("/control/api/actions", headers=h, json={**base, **bad})
        assert r.status_code == 400 and why in r.json()["error"], (bad, r.text)
    r = client.post("/control/api/actions", headers=h, json={**base, "file": "docs/memory/sprints/UI-02b",
                                                              "task_id": "UI-02b"})
    assert r.status_code == 200, r.text
    a = r.json()
    payload = json.loads(cc.db.query_one("SELECT payload FROM cc_actions WHERE action_id=?",
                                         (a["action_id"],))["payload"])
    assert a["status"] == "accepted" and payload["file"] == "sprints/UI-02b" \
        and payload["task_id"] == "UI-02b" and a["task_id"] == ""
    # project-scoped: another operator's project allow-list applies
    r = client.post("/control/api/actions", headers=login(client, key="op-b-key-123456789"),
                    json={**base, "file": "DECISIONS.md", "idempotency_key": "mem-22345678"})
    assert r.status_code == 403
    # the coordinator's ingest of an unknown project is refused by the key's allow-list
    r = client.post("/v1/foundry/telemetry/batch", headers={"Authorization": "Bearer bridge-a-key-123456"},
                    json={"contract": CONTRACT, "source": {"id": "bridge:a", "kind": "bridge"},
                          "observations": [memory_obs(project_id="other")]})
    assert r.json()["skipped"].get("project_not_allowed") == 1
