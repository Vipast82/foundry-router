"""Operator chat (W3, 2026-10-22): persisted threads and turns, deterministic
validation of the model's calls, AUTO vs CONFIRM classes (switchable in
chat_auto_classes), confirm cards, the audit log, the Foundry Client's heartbeat
kind and client_* actions, and the /control/api/chat auth. The model is a fake:
the engine never needs a backend to be tested."""
import json

import pytest

from foundry_router import chat_engine as CE
from tests.test_control_center import COORD, OP_B_KEY, OP_KEY, cc, login  # noqa: F401

CLIENT_HDR = {"Authorization": f"Bearer {OP_KEY}"}


class FakeModel:
    """Replies in order; each is a dict (dumped to JSON) or a raw string."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def __call__(self, messages):
        self.seen.append(messages)
        r = self.replies.pop(0) if self.replies else {"say": "done", "ask": None, "calls": []}
        return (r if isinstance(r, str) else json.dumps(r)), {"served_model": "fake-sonnet",
                                                              "prompt_tokens": 10,
                                                              "completion_tokens": 5}


def reply(say="", calls=(), ask=None):
    return {"say": say, "ask": ask, "calls": list(calls)}


def call(tool, cid="c1", **args):
    return {"id": cid, "tool": tool, "args": args}


@pytest.fixture()
def chat(app, client, cc):  # noqa: F811
    eng = app.state.services.chat
    h = login(client)

    def run(text, *replies, thread=None):
        eng._model_fn = FakeModel(*replies)
        if thread is None:
            thread = client.post("/control/api/chat/threads", headers=h, json={}).json()["thread_id"]
        r = client.post(f"/control/api/chat/threads/{thread}/turns", headers=h, json={"text": text})
        assert r.status_code == 200, r.text
        out = r.json()
        out["_thread"] = thread
        return out
    run.h = h
    run.eng = eng
    return run


def test_the_chat_never_creates_or_uses_a_persona(chat, app):
    """v0.140.0: the chat has its own model chain (operator_chat.routes or the
    built-in chain), so persona edits can't break it (test_chat_router_tools
    covers the chain itself)."""
    chat("hello", reply("hi"))
    assert app.state.services.personas.get(CE.PERSONA) is None


def test_reads_run_at_once_and_feed_the_model_then_the_turn_is_persisted(chat, client):
    t = chat("what is running?", reply("let me look", [call("get_overview"), call("get_stack", "c2")]),
             reply("Nothing runs; no client is attached."))
    assert t["status"] == "ok" and t["model_calls"] == 2 and t["tool_calls"] == 2
    assert [r["tool"] for r in t["results"]] == ["get_overview", "get_stack"]
    assert all(r["ok"] for r in t["results"])
    assert t["results"][1]["result"]["client"] == "none attached"
    assert t["say"].startswith("Nothing runs") and t["confirm"] is None
    # the second model call saw the tool results
    msgs = chat.eng._model_fn.seen[1]
    assert msgs[0]["role"] == "system" and "Tool results:" in msgs[-1]["content"]
    th = client.get(f"/control/api/chat/threads/{t['_thread']}").json()
    assert len(th["turns"]) == 1 and th["turns"][0]["text"] == "what is running?"
    assert th["title"] == "what is running?"
    audit = client.get(f"/control/api/chat/audit?thread={t['_thread']}").json()["audit"]
    kinds = {a["status"] for a in audit}
    assert {"thread_created", "turn", "read", "model"} <= kinds


def test_invalid_calls_are_refused_deterministically(chat, app):
    app.state.services.automation.draft("proj")                  # a known project
    t = chat("do things", reply("ok", [
        call("fly_to_moon"),                                     # unknown tool
        call("get_queue", "c2"),                                 # missing required arg
        call("get_queue", "c3", project="other"),                # project not known/allowed
        call("set_settings", "c4", project="proj", changes={"foundry.url": "http://x"}),   # local
        call("set_settings", "c5", project="proj", changes={"id": "zzz"}),                 # locked+local
        call("set_settings", "c6", project="proj", changes={"no.such": 1}),                # unknown
        call("set_settings", "c7", project="proj", changes={"role_rules.lead": {}}),       # wrong tool
        call("get_queue", "c8", project=123),                    # wrong type
    ]))
    reasons = {r["id"]: r["reason"] for r in t["refused"]}
    assert set(reasons) == {"c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"}
    assert "unknown tool" in reasons["c1"] and "missing argument" in reasons["c2"]
    assert "not one you may act on" in reasons["c3"]
    assert "local" in reasons["c4"] and "local" in reasons["c5"]       # every locked key is local too
    assert "not a known setting" in reasons["c6"] and "set_role_rules" in reasons["c7"]
    assert "must be string" in reasons["c8"]
    assert t["tool_calls"] == 0 and t["model_calls"] == 1


def test_tool_call_cap_per_turn(chat):
    t = chat("read everything ten times",
             reply("", [call("get_stack", f"c{i}") for i in range(10)]), reply("done"))
    assert t["tool_calls"] == CE.MAX_TOOL_CALLS
    assert len(t["refused"]) == 2 and "limit" in t["refused"][0]["reason"]


def test_model_call_cap_and_json_repair(chat):
    t = chat("loop", "this is not json", reply("fixed", [call("get_stack")]),
             reply("", [call("get_stack", "c2")]), reply("", [call("get_stack", "c3")]),
             reply("never reached"))
    assert t["model_calls"] == CE.MAX_MODEL_CALLS and t["status"] == "ok"
    t2 = chat("garbage", "nope", "still nope")
    assert t2["status"] == "error" and "not valid" in t2["error"] and t2["model_calls"] == 2


def test_ask_ends_the_turn(chat):
    t = chat("start it", reply("Which project?", ask={"text": "Which project?",
                                                      "options": ["proj", "other"]}))
    assert t["ask"] == {"text": "Which project?", "options": ["proj", "other"]}
    t2 = chat("start it", reply("", [call("ask_operator", text="Safe or now?", options=["safe", "now"])]))
    assert t2["ask"]["text"] == "Safe or now?" and t2["tool_calls"] == 0


def test_draft_edits_are_auto_and_deploy_needs_the_card(chat, app, client):
    st = app.state.services.automation
    st.draft("proj")
    t = chat("set the review max diff to 2000 and deploy",
             reply("", [call("set_settings", project="proj", changes={"review.max_diff_bytes": 2000,
                                                                      "roles.implementer": "Foundry-Kilo-Local"}),
                        call("deploy_settings", "c2", project="proj", note="chat")]),
             reply("Draft saved; confirm the deploy."))
    assert t["results"][0]["ok"] and "draft" in t["results"][0]["changed"][0]
    assert st.draft("proj")["doc"]["settings"]["review"]["max_diff_bytes"] == 2000
    assert st.deployed("proj") is None                            # not live
    card = t["confirm"]
    assert card["status"] == "pending" and [c["tool"] for c in card["calls"]] == ["deploy_settings"]
    assert {"review.max_diff_bytes", "roles.implementer"} <= {c["path"] for c in card["calls"][0]["preview"]["changes"]}
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c2"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "confirmed" and out["results"][0]["ok"]
    assert st.deployed("proj")["rev"] == 1
    assert st.audit("proj", 5)[0]["actor"] == "victor via operator chat"
    # the card is one-shot, and the thread got a system turn with what changed
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c2"]})
    assert r.status_code == 409
    th = client.get(f"/control/api/chat/threads/{t['_thread']}").json()
    assert th["turns"][-1]["role"] == "system" and "revision 1 deployed" in th["turns"][-1]["changes"][0]
    assert th["pending_confirms"] == []


def test_declined_card_runs_nothing(chat, app, client):
    app.state.services.automation.draft("proj")
    t = chat("deploy", reply("", [call("deploy_settings", project="proj")]), reply("ok"))
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "decline"})
    assert r.json()["status"] == "declined"
    assert app.state.services.automation.deployed("proj") is None


def test_confirm_classes_can_be_switched_to_auto(chat, app, client):
    svc = app.state.services
    svc.config_store.save(lambda raw: raw["backend_pool"]["internal"].setdefault("backends", []).append(
        {"name": "meridian", "type": "anthropic-compatible", "url": "http://127.0.0.1:9",
         "api_key": "k", "models": ["claude-sonnet-5-5"], "enabled": False}))
    before = client.get("/admin/api/config/cutoffs").json()["claude"]
    t = chat("claude cutoff 77", reply("", [call("set_cutoffs", claude=77)]), reply("queued"))
    assert t["confirm"] and t["confirm"]["calls"][0]["class"] == "cutoffs"
    assert client.get("/admin/api/config/cutoffs").json()["claude"] == before
    r = client.put("/control/api/chat/settings", headers=chat.h, json={"auto_classes": {"cutoffs": True}})
    assert r.status_code == 200 and r.json()["auto_classes"]["cutoffs"] is True
    assert client.get("/control/api/chat/settings").json()["auto_classes"]["deploy"] is False
    t = chat("claude cutoff 66", reply("", [call("set_cutoffs", claude=66)]), reply("done"))
    assert t["confirm"] is None and t["results"][0]["ok"]
    assert client.get("/admin/api/config/cutoffs").json()["claude"] == 66.0
    assert client.put("/control/api/chat/settings", headers=chat.h,
                      json={"auto_classes": {"nope": True}}).status_code == 400
    cat = client.get("/control/api/chat/settings").json()["catalog"]
    assert next(x for x in cat if x["name"] == "set_cutoffs")["mode"] == "auto"
    assert next(x for x in cat if x["name"] == "deploy_settings")["mode"] == "confirm"


def test_context_is_sent_once_and_only_changed_blocks_again(chat, app):
    t1 = chat("hi", reply("hi"))
    assert set(t1["context_sent"]) == {"projects", "settings", "usage", "stack", "services", "pending", "memory"}
    t2 = chat("again", reply("hi"), thread=t1["_thread"])
    assert t2["context_sent"] is None                             # nothing changed
    app.state.services.automation.draft("newproj")               # settings block changes
    t3 = chat("again", reply("hi"), thread=t1["_thread"])
    assert "settings" in t3["context_sent"] and "stack" not in t3["context_sent"]
    third = chat.eng._model_fn.seen[0][-1]["content"]
    assert "unchanged: memory, pending, services, stack, usage" in third and '"settings":' in third


def _client_beat(client, sid="client:ws", features=("stack_control",), hdr=CLIENT_HDR):
    return client.post("/v1/foundry/telemetry/heartbeat", headers=hdr, json={
        "source": {"id": sid, "kind": "client", "version": "0.134.0"}, "contract": "foundry.cc.v1",
        "features": list(features), "overall": "ok",
        "components": [{"name": "kilo", "label": "Kilo server", "state": "running", "detail": "port 4096",
                        "enabled": True}, {"name": "coordinator", "state": "running"}]})


def test_client_heartbeat_with_an_operator_key_and_stack_actions(chat, client, cc):  # noqa: F811
    # before any client: the tool says so, nothing is filed
    t = chat("restart kilo", reply("", [call("restart_kilo", force=False)]), reply("no client"))
    assert t["confirm"] and t["confirm"]["calls"][0]["class"] == "restart"
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    assert r.json()["results"][0]["ok"] is False and "no Foundry Client" in r.json()["results"][0]["error"]
    # an operator key may heart-beat as a client only
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json={
        "source": {"id": "x", "kind": "coordinator"}})
    assert r.status_code == 403
    r = _client_beat(client)
    assert r.status_code == 200 and r.json()["compat"] == "ok", r.text
    stack = chat("stack?", reply("", [call("get_stack")]), reply("ok"))["results"][0]["result"]
    assert stack["client"] == "client:ws" and stack["components"][0]["state"] == "running"
    # the chat files a client_restart; the card runs it; the client claims it
    t = chat("restart kilo now", reply("", [call("restart_kilo", force=True)]), reply("card"))
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    res = r.json()["results"][0]
    assert res["ok"], res
    aid = res["result"]["action_id"]
    a = cc.action_view(aid)
    assert a["kind"] == "client_restart" and a["status"] == "accepted" and a["project_id"] == ""
    assert a["summary"] == "restart kilo (now)"
    # the coordinator's claim never sees stack actions
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert [x["action_id"] for x in r.json()["actions"]] == []
    # a client source that did not heart-beat, or lacks the feature, gets nothing
    r = client.post("/v1/foundry/control/claim", headers=CLIENT_HDR,
                    json={"source_id": "client:other", "source_kind": "client"})
    assert r.status_code == 409
    _client_beat(client, sid="client:old", features=())
    r = client.post("/v1/foundry/control/claim", headers=CLIENT_HDR,
                    json={"source_id": "client:old", "source_kind": "client"})
    assert r.status_code == 403
    # the real client claims and reports
    r = client.post("/v1/foundry/control/claim", headers=CLIENT_HDR,
                    json={"source_id": "client:ws", "source_kind": "client"})
    acts = r.json()["actions"]
    assert [x["action_id"] for x in acts] == [aid] and acts[0]["payload"]["force"] is True
    assert acts[0]["payload"]["chat"]["thread"] == t["_thread"]
    r = client.post(f"/v1/foundry/control/actions/{aid}/status", headers=CLIENT_HDR,
                    json={"source_id": "client:ws", "status": "applied", "detail": "Kilo: starting",
                          "result": {"state": "starting"}})
    assert r.status_code == 200 and cc.action_view(aid)["status"] == "applied"
    # an operator key cannot report a coordinator action
    chat.h.update(login(client))                                 # a new session, new CSRF
    r = client.post("/control/api/actions", headers=chat.h, json={
        "kind": "assistant_plan", "project_id": "proj", "text": "x", "idempotency_key": "plan-123456"})
    assert r.status_code == 200, r.text
    pid = r.json()["action_id"]
    r = client.post(f"/v1/foundry/control/actions/{pid}/status", headers=CLIENT_HDR,
                    json={"source_id": "client:ws", "status": "applied", "detail": ""})
    assert r.status_code == 403
    # one live stack action per component
    t = chat("restart kilo", reply("", [call("restart_kilo")]), reply("card"))
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    first = r.json()["results"][0]["result"]["action_id"]
    t = chat("restart kilo again", reply("", [call("restart_kilo")]), reply("card"))
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    assert r.json()["results"][0]["result"]["action_id"] == first
    # a project-restricted operator may not touch the stack
    hb = login(client, OP_B_KEY)
    r = client.post("/control/api/actions", headers=hb, json={
        "kind": "client_restart", "component": "rojo", "idempotency_key": "rojo-1234567"})
    assert r.status_code == 403


def test_sprint_tools_go_through_the_coordinator_actions(chat, app, client, cc):  # noqa: F811
    from tests.test_control_center import ingest, task_obs
    ingest(client, task_obs(state="IMPLEMENTING"))
    app.state.services.automation.draft("proj")
    t = chat("pause T-1", reply("", [call("task_action", project="proj", task_id="T-1", kind="pause",
                                          note="upgrading")]), reply("card"))
    card = t["confirm"]
    assert card["calls"][0]["summary"] == "pause T-1 (proj): upgrading"
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    aid = r.json()["results"][0]["result"]["action_id"]
    a = cc.action_view(aid)
    assert a["kind"] == "pause" and a["expected"]["task_state"] == "IMPLEMENTING"
    # plan_sprint_steps is a draft: it runs at once, files assistant_plan and waits briefly
    async def _no_wait(aid, s):
        return {"status": "accepted", "status_detail": "waiting"}
    chat.eng._wait_action = _no_wait
    t = chat("plan", reply("", [call("plan_sprint_steps", project="proj", request="start the next sprint")]),
             reply("waiting"))
    res = t["results"][0]
    assert res["ok"] and res["result"]["status"] == "accepted"
    assert cc.action_view(res["result"]["action_id"])["kind"] == "assistant_plan"
    # the old actions still work directly
    r = client.post("/control/api/actions", headers=chat.h, json={
        "kind": "assistant_run", "project_id": "proj", "plan_id": "asp-0123456789ab",
        "idempotency_key": "run-abcdefgh"})
    assert r.status_code == 200


def test_decision_answers_carry_the_question_hash(chat, client, cc):  # noqa: F811
    from tests.test_control_center import ingest, task_obs
    ingest(client, task_obs(state="IMPLEMENTING", decisions=[
        {"decision_id": "d1", "kind": "tooling_limit", "status": "open",
         "question": "Lead budget hit. Park or extend?", "options": ["park", "extend"]}]))
    pend = chat("what waits?", reply("", [call("list_pending")]), reply("one"))["results"][0]["result"]
    assert pend["decisions"][0]["decision_id"] == "d1"
    t = chat("park it", reply("", [call("answer_decision", project="proj", task_id="T-1",
                                        decision_id="d1", option="park")]), reply("card"))
    card = t["confirm"]
    assert card["calls"][0]["preview"]["question"].startswith("Lead budget")
    r = client.post(f"/control/api/chat/confirms/{card['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1"]})
    res = r.json()["results"][0]
    assert res["ok"], res
    a = cc.action_view(res["result"]["action_id"])
    assert a["kind"] == "decision_answer" and a["expected"]["decision_id"] == "d1"


def test_api_needs_the_operator_session_and_csrf(client, cc):  # noqa: F811
    assert client.get("/control/api/chat/threads").status_code == 401
    assert client.post("/control/api/chat/threads", json={}).status_code == 401
    h = login(client)
    assert client.post("/control/api/chat/threads", json={}).status_code == 403        # no CSRF
    assert client.post("/control/api/chat/threads", json={}, headers={**h, "origin":
                       "http://evil.example"}).status_code == 403
    t = client.post("/control/api/chat/threads", headers=h, json={"title": "mine"}).json()
    assert client.get(f"/control/api/chat/threads/{t['thread_id']}").status_code == 200
    # another operator cannot read or use it
    hb = login(client, OP_B_KEY)
    assert client.get(f"/control/api/chat/threads/{t['thread_id']}").status_code == 404
    assert client.post(f"/control/api/chat/threads/{t['thread_id']}/turns", headers=hb,
                       json={"text": "hi"}).status_code == 404
    assert client.put("/control/api/chat/settings", headers=hb,
                      json={"auto_classes": {"deploy": True}}).status_code == 403
    # a bare client key is not an operator session
    client.cookies.clear()
    assert client.get("/control/api/chat/threads", headers=CLIENT_HDR).status_code == 401


# ---------------------------------------------------------------- services (Team Lead 5)

SERVICES_BEAT = {
    "source": {"id": "client:ws", "kind": "client", "version": "0.134.0"}, "contract": "foundry.cc.v1",
    "features": ["stack_control"], "overall": "ok",
    "components": [{"name": "rojo", "state": "stopped", "enabled": False}],
    "mcp": [{"name": "kokoro-tts", "type": "remote", "enabled": False, "available": False,
             "detail": "192.168.0.115:8934 is not reachable", "target": "192.168.0.115:8934",
             "headers": "dropped"}],
    "services": [
        {"name": "rojo", "label": "Rojo serve", "kind": "process", "check": "port",
         "provider": "client", "component": "rojo", "enabled": False, "component_state": "stopped",
         "needed_by": ["studio"], "status": "down", "detail": "port 34872 is closed", "note": ""},
        {"name": "kokoro-tts", "label": "Kokoro speech", "kind": "mcp", "check": "mcp",
         "provider": "external", "component": "", "enabled": None, "needed_by": ["audio"],
         "status": "down", "detail": "192.168.0.115:8934 is not reachable", "note": ""},
        {"name": "kilo", "label": "Kilo server", "kind": "process", "check": "port",
         "provider": "client", "component": "kilo", "enabled": True, "component_state": "running",
         "needed_by": ["studio"], "status": "up", "detail": "port 4096 is open", "note": ""}]}


def _confirm(client, chat, t, picked=("c1",)):
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": list(picked)})
    return r.json()["results"][0]


def test_services_status_tool_and_the_small_context_block(chat, client, cc, app):  # noqa: F811
    from tests.test_control_center import ingest, task_obs
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json=SERVICES_BEAT)
    assert r.status_code == 200, r.text
    ingest(client, task_obs(state="READY", needs=["audio"],
                            service_wait={"missing": ["kokoro-tts"], "needs": ["audio"], "n": 1}))
    t = chat("what is down?", reply("", [call("services_status")]), reply("kokoro"))
    res = t["results"][0]["result"]
    by = {s["name"]: s for s in res["services"]}
    assert by["rojo"]["status"] == "down" and by["rojo"]["provider"] == "client"
    assert by["kokoro-tts"]["provider"] == "external" and "component" not in by["kokoro-tts"]
    assert by["kilo"]["status"] == "up"
    assert res["waiting_sprints"] == [{"project": "proj", "task": "T-1",
                                      "waiting_for": ["kokoro-tts"], "needs": ["audio"]}]
    assert res["mcp"] == [{"name": "kokoro-tts", "type": "remote", "enabled": False,
                           "available": False, "detail": "192.168.0.115:8934 is not reachable"}]
    # the context block is small and stable: no timestamps, no free-text for services that are up
    blk = chat.eng.services_view(["proj"])
    assert "detail" not in {s["name"]: s for s in blk["services"]}["kilo"]
    assert chat.eng.services_view(["proj"]) == blk


def test_service_restart_and_enable_file_client_actions(chat, client, cc):  # noqa: F811
    client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json=SERVICES_BEAT)
    t = chat("restart rojo", reply("", [call("service_restart", name="rojo", force=True)]), reply("card"))
    assert t["confirm"]["calls"][0]["class"] == "restart"
    assert t["confirm"]["calls"][0]["summary"] == "restart service rojo (now)"
    res = _confirm(client, chat, t)
    assert res["ok"], res
    a = cc.action_view(res["result"]["action_id"])
    assert a["kind"] == "client_restart" and a["summary"] == "restart rojo (now)"
    assert a["expected"] == {"component": "rojo"}

    t = chat("turn rojo on", reply("", [call("service_enable", name="rojo", on=True)]), reply("card"))
    assert t["confirm"]["calls"][0]["summary"] == "enable service rojo"
    res = _confirm(client, chat, t)
    assert res["ok"], res
    a = cc.action_view(res["result"]["action_id"])
    assert a["kind"] == "client_service_enable" and a["summary"] == "enable rojo"
    # the client claims it like any stack action, payload intact
    r = client.post("/v1/foundry/control/claim", headers=CLIENT_HDR,
                    json={"source_id": "client:ws", "source_kind": "client"})
    got = {x["kind"]: x["payload"] for x in r.json()["actions"]}
    assert got["client_service_enable"]["component"] == "rojo"
    assert got["client_service_enable"]["enabled"] is True
    assert got["client_restart"]["component"] == "rojo" and got["client_restart"]["force"] is True


def test_external_and_unknown_services_cannot_be_restarted(chat, client, cc):  # noqa: F811
    client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json=SERVICES_BEAT)
    t = chat("restart kokoro", reply("", [call("service_restart", name="kokoro-tts")]), reply("card"))
    res = _confirm(client, chat, t)
    assert res["ok"] is False and "external" in res["error"] and "nothing to restart" in res["error"]
    t = chat("enable bogus", reply("", [call("service_enable", name="bogus", on=True)]), reply("card"))
    res = _confirm(client, chat, t)
    assert res["ok"] is False and "no service 'bogus'" in res["error"]
    # nothing was filed
    assert not [a for a in cc.actions([], "", "", "", 50) if a["kind"] == "client_service_enable"]


def test_service_enable_validation_in_the_control_center(client, cc, chat):  # noqa: F811
    client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json=SERVICES_BEAT)
    h = chat.h
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "client_service_enable", "component": "kilo", "enabled": True,
        "idempotency_key": "svc-enable-1"})
    assert r.status_code == 400 and "component must be one of" in r.text
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "client_service_enable", "component": "blender", "enabled": "yes",
        "idempotency_key": "svc-enable-2"})
    assert r.status_code == 400 and "true or false" in r.text
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "client_service_enable", "component": "blender", "enabled": True,
        "idempotency_key": "svc-enable-3"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"


def test_services_http_route_for_the_gui(client, chat, cc):  # noqa: F811
    client.post("/v1/foundry/telemetry/heartbeat", headers=CLIENT_HDR, json=SERVICES_BEAT)
    r = client.get("/control/api/services", headers=chat.h)
    assert r.status_code == 200
    assert {s["name"] for s in r.json()["services"]} == {"rojo", "kokoro-tts", "kilo"}
    client.cookies.clear()
    r = client.get("/control/api/services")                      # no session
    assert r.status_code == 401


def test_install_help_tools_read_the_guide_and_the_install_state(chat, client, monkeypatch):
    import foundry_router.client_guide as CG
    from foundry_router import __version__

    cat = {t["name"]: t for t in chat.eng.catalog()}
    assert cat["read_install_guide"]["mode"] == "auto"           # read class: runs at once
    assert cat["client_install_status"]["mode"] == "auto"
    # the guide: list the sections, then read one (text straight from the doc)
    t = chat("how do I install the client?",
             reply("", [call("read_install_guide"),
                        call("read_install_guide", "c2", section="install")]),
             reply("here is how"))
    assert all(r["ok"] for r in t["results"]), t["results"]
    ids = [s["id"] for s in t["results"][0]["result"]["sections"]]
    assert ids[:3] == ["install", "first-run", "router-key-for-the-relay"]
    assert "troubleshooting" in ids
    assert "FoundryClientSetup" in t["results"][1]["result"]["text"]
    # install state: release availability from a (faked) GitHub lookup plus the
    # heart-beating client's version compared against the router's
    tags = f"https://api.github.com/repos/o/r/releases/tags/v{__version__}"

    async def fetch(url):
        assert url == tags, url
        return 200, {"tag_name": f"v{__version__}", "assets": [
            {"name": f"FoundryClientSetup-{__version__}.exe", "size": 9,
             "browser_download_url": "https://github.com/dl/win"}]}

    monkeypatch.setattr(CG, "_SOURCE", CG.ReleaseSource("o/r", fetch))
    _client_beat(client)                                         # version 0.134.0
    t = chat("is the client connected?", reply("", [call("client_install_status")]),
             reply("yes"))
    d = t["results"][0]["result"]
    assert d["router_version"] == __version__
    assert d["release"]["state"] == "available"
    assert d["release"]["assets"][0]["url"] == "https://github.com/dl/win"
    assert d["clients"][0]["source_id"] == "client:ws"
    assert d["clients"][0]["version"] == "0.134.0" and d["clients"][0]["match"] == "older"
