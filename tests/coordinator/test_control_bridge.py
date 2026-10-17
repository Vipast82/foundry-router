"""Control center, workstation side, end to end with real code paths: the
coordinator engine with its real Kilo client, the read-only telemetry bridge
and the control channel, against a fake `kilo serve` (devtools/fake_kilo.py)
and Foundry's real ControlCenter (in-process). Nothing here touches a real
Kilo, Studio or the live coordinator."""

import json
import time
import uuid

import pytest

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.control import ControlChannel
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import KiloObserver, TelemetryBridge
from foundry_router.control_center import ControlError
from tests.coordinator.helpers import FakeFoundry, git, make_repo, write_profile

T1 = {"id": "T1", "title": "Frame sizes", "spec": "Do the work.", "scope": ["src/a.txt",
                                                                          "docs/evidence/T1/*"],
      "tags": ["visual"]}
APPROVAL = """
[[approval.rules]]
match = "visual"
kind = "visual"
stage = "after_close"
artifacts = ["docs/evidence/{task}/*.png"]
"""
CLIENT = {"client_id": "coordinator", "scopes": ["inference", "orchestration"], "projects": []}
SECRETS = ("fake-kilo-password", "sk-should-never-leave-kilo", "secret-env-token", "user:pw",
           "ENCRYPTED-REASONING", "abcdefghijklmnop123456")


class DirectFoundry:
    """FoundryClient's control-center methods, straight into a ControlCenter."""
    url = "inproc://foundry"
    key = "coord-key-not-a-real-one"

    def __init__(self, cc):
        self.cc = cc

    def telemetry(self, b):
        return self.cc.ingest(json.loads(json.dumps(b)), CLIENT)

    def heartbeat(self, p):
        return self.cc.heartbeat(json.loads(json.dumps(p)), CLIENT)

    def claim_actions(self, source_id, projects, limit=20):
        return self.cc.claim(source_id, projects, [], limit)

    def action_status(self, action_id, source_id, status, detail, result=None):
        return self.cc.report(action_id, source_id, status, detail, result)


@pytest.fixture()
def env(tmp_path, app):
    fake = FakeKilo()
    url = fake.start()
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"docs/evidence/T1/a.png": "png1"})
    prof = write_profile(tmp_path, "game", root, extra=APPROVAL)
    p = load(str(prof))
    kilo = KiloClient(url, password=fake.password)
    state = str(tmp_path / "state")
    project = build_project(p, state, client=kilo, foundry=FakeFoundry())
    store = Store(str(tmp_path / "state" / "c.sqlite"))
    c = Coordinator(store, [project], state)
    cc = app.state.services.control
    direct = DirectFoundry(cc)
    bridge = TelemetryBridge([project], store.path, direct,
                             observers={"game": KiloObserver(url, password=fake.password)},
                             use_events=False, poll_s=0.0)
    ch = ControlChannel(c, foundry=direct, source_id="coordinator:test")
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    t = store.task("game", "T1")
    assert t["state"] == "IMPLEMENTING"
    bridge.step()
    yield {"fake": fake, "c": c, "cc": cc, "bridge": bridge, "ch": ch, "store": store,
           "sid": t["session_id"], "root": root, "app": app}
    bridge.stop()
    fake.stop()


def act(cc, **body):
    body.setdefault("project_id", "game")
    body.setdefault("task_id", "T1")
    body.setdefault("idempotency_key", f"k-{uuid.uuid4().hex}")
    return cc.create_action("victor", [], body)


def dump_cc(app) -> str:
    db = app.state.services.db
    return json.dumps({t: db.query(f"SELECT * FROM {t}") for t in (
        "cc_parts", "cc_messages", "cc_sessions", "cc_snapshots", "cc_tasks", "cc_pending",
        "cc_sources", "cc_alerts", "cc_actions", "cc_action_log", "cc_alert_log")}, default=str)


def test_worker_transcript_reasoning_tools_mcp_without_credentials(env):
    fake, sid, cc = env["fake"], env["sid"], env["cc"]
    fake.add_message(sid, "assistant", [
        {"type": "reasoning", "text": "I should check the frame list first.",
         "metadata": {"openai": {"reasoningEncryptedContent": "ENCRYPTED-REASONING"}}},
        {"type": "text", "text": "Checking **frames**.\n```lua\nlocal x = 1\n```"},
        {"type": "tool", "tool": "Roblox_Studio_run_code", "callID": "call_1",
         "state": {"status": "running", "input": {"code": "print(1)"},
                   "time": {"start": int(time.time() * 1000)}}},
        {"type": "tool", "tool": "bash", "callID": "call_2",
         "state": {"status": "completed", "input": {"command": "env"},
                   "output": "Authorization: Bearer abcdefghijklmnop123456\nok",
                   "time": {"start": 1, "end": 2}}}],
        tokens={"input": 1200, "output": 300, "reasoning": 80, "cache": {"read": 50000, "write": 0}},
        completed=False)
    fake.set_todo(sid, [{"id": "1", "content": "Capture 390x844", "status": "in_progress"},
                        {"id": "2", "content": "Vision check", "status": "pending"}])
    env["bridge"].step()
    s = cc.session(sid, [])
    assert s["role"] == "implementer" and s["task_id"] == "T1" and s["status"] == "busy"
    assert s["status_source"] == "kilo /session/status"
    kinds = [p["kind"] for p in cc.parts(sid, [])["parts"]]
    assert {"reasoning", "text", "tool"} <= set(kinds)
    tools = [p["data"]["tool"] for p in cc.parts(sid, [])["parts"] if p["kind"] == "tool"]
    assert {t["mcp_server"] for t in tools} == {"Roblox_Studio", ""}
    assert [i["status"] for i in s["todo"]["data"]["items"]] == ["in_progress", "pending"]
    mcp = cc.snapshot("mcp", "game")["data"]["servers"]
    by = {m["name"]: m for m in mcp}
    assert by["playwright"]["configured_enabled"] is False and by["playwright"]["status"] == "disabled"
    assert by["robloxstudio-mcp"]["status"] == "failed"
    cfg = cc.snapshot("config", "game")["data"]
    assert cfg["models"]["foundry/Foundry-Kilo-Research"]["context"] == 32768
    assert "command" not in json.dumps(cfg["mcp"]) and "options" not in json.dumps(cfg)
    blob = dump_cc(env["app"])
    for secret in SECRETS:
        assert secret not in blob, secret


def test_question_answered_through_the_coordinator_resumes_the_session(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    rid = fake.ask_question(sid, [{"question": "Which size first?", "header": "Size",
                                   "options": [{"label": "390x844"}, {"label": "1920x1080"}]}])
    c.tick()
    assert store.task("game", "T1")["state"] == "AWAITING_HUMAN"
    env["bridge"].step()
    alerts = [a for a in cc.alerts([], "open") if a["kind"] == "question"]
    assert len(alerts) == 1 and alerts[0]["request_id"] == rid
    text = "Do 390x844 first.\n\nReason: phone layout is the binding check.\n```\nnote\n```"
    a = act(cc, kind="answer_question", request_id=rid, session_id=sid, answers=[[text]],
            expected={"task_state": "AWAITING_HUMAN"})
    with pytest.raises(ControlError) as e:                # another tab: refused
        act(cc, kind="answer_question", request_id=rid, session_id=sid, answers=[["other"]])
    assert e.value.code == "duplicate_reply"
    ch.poll()
    assert fake.replies == [{"kind": "question", "request_id": rid, "session_id": sid,
                             "body": {"answers": [[text]]}}]
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"
    v = cc.action_view(a["action_id"])
    assert v["status"] == "applied" and sid in v["status_detail"]
    env["bridge"].step()                                  # Kilo no longer lists it
    assert cc.alert(alerts[0]["alert_id"])["status"] == "answered"
    assert cc.session(sid, [])["status"] == "busy"        # the same session resumed
    ch.poll()
    assert len(fake.replies) == 1                         # never delivered twice


def test_reply_waits_until_the_engine_has_recorded_the_question(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    rid = fake.ask_question(sid, [{"question": "Go?", "options": [{"label": "yes"}]}])
    env["bridge"].step()                                  # bridge saw it first
    a = act(cc, kind="answer_question", request_id=rid, session_id=sid, answers=[["yes"]])
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "queued" and fake.replies == []
    c.tick()                                              # engine records the question
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "applied"
    assert fake.replies[0]["request_id"] == rid


def test_changed_session_is_not_silently_retargeted(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    rid = fake.ask_question(sid, [{"question": "Go?"}])
    c.tick()
    env["bridge"].step()
    a = act(cc, kind="answer_question", request_id=rid, session_id=sid, answers=[["yes"]])
    t = store.task("game", "T1")
    store.update_task("game", "T1", session_id="ses_other")      # the session changed meanwhile
    ch.poll()
    v = cc.action_view(a["action_id"])
    assert v["status"] == "rejected" and "session changed" in v["status_detail"]
    assert fake.replies == []
    store.update_task("game", "T1", session_id=t["session_id"])


def test_helper_session_tree_model_and_helper_question(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    kid = fake.new_session(title="Plan UI-01 frame order", agent="au-planner",
                           model="Foundry-Kilo-Planner", parent=sid)
    fake.add_message(kid, "user", [{"type": "text", "text": "Plan please"}], agent="au-planner")
    fake.add_message(kid, "assistant", [{"type": "text", "text": "Step 1..."}],
                     model="Foundry-Kilo-Planner", agent="au-planner")
    fake.set_status(sid, "busy")
    c.tick()                                              # engine binds the helper
    env["bridge"].step()
    env["bridge"]._last["children"] = 0
    env["bridge"].step()
    h = cc.session(kid, [])
    assert h["parent_id"] == sid and h["role"] == "helper"
    assert h["model"] == "Foundry-Kilo-Planner" and h["agent"] == "au-planner"
    assert [x["session_id"] for x in cc.session(sid, [])["children"]] == [kid]
    rid = fake.ask_question(kid, [{"question": "Include tablet sizes?"}])
    env["bridge"].step()
    act(cc, kind="answer_question", request_id=rid, session_id=kid, answers=[["No, phone first"]])
    ch.poll()
    assert fake.replies[-1]["session_id"] == kid
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"     # the task wasn't moved


def test_instruction_waits_for_the_next_session_then_follow_switches(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    n_prompts = len(fake.prompts)
    a = act(cc, kind="instruction", text="Capture 390x844 before the gallery.\nKeep S46 timing.")
    ch.poll()
    v = cc.action_view(a["action_id"])
    assert v["status"] == "queued" and "not interrupted" in v["status_detail"]
    assert len(fake.prompts) == n_prompts                 # nothing injected into the busy worker
    fake.finish(sid, "done (but nothing committed)")
    for _ in range(4):
        c.tick()
    t = store.task("game", "T1")
    assert t["state"] == "IMPLEMENTING" and t["attempt"] == 2 and t["session_id"] != sid
    assert "Operator instructions" in fake.prompts[-1]["text"]
    assert "Keep S46 timing." in fake.prompts[-1]["text"]
    assert fake.prompts[-1]["session_id"] == t["session_id"]
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "delivered"
    env["bridge"].step()
    ct = cc.db.query_one("SELECT data FROM cc_tasks WHERE task_id='T1'")
    assert json.loads(ct["data"])["session_id"] == t["session_id"]
    assert cc.session(sid, [])["outcome"] == "idle"       # the old session stays inspectable
    tr = [x["to_state"] for x in cc.transitions("game", "T1")]
    assert "CHANGES_REQUIRED" in tr
    fake.add_message(t["session_id"], "assistant", [{"type": "text", "text": "ok"}])
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "applied"


def test_pause_and_steer_uses_the_engine_pause_and_the_same_session(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    a = act(cc, kind="instruction", text="Stop editing the gallery; fix S45 first.",
            delivery="pause_and_steer")
    ch.poll()
    assert sid in fake.aborts
    assert fake.prompts[-1]["session_id"] == sid and "fix S45 first" in fake.prompts[-1]["text"]
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"
    assert cc.action_view(a["action_id"])["status"] == "delivered"
    hist = [h["to_state"] for h in store.history("game", "T1")]
    assert hist[-2:] == ["PAUSED", "IMPLEMENTING"]


def test_pause_resume_cancel_are_real_transitions(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    act(cc, kind="pause", reason="operator maintenance")
    ch.poll()
    assert store.task("game", "T1")["state"] == "PAUSED" and sid in fake.aborts
    env["bridge"].step()
    act(cc, kind="resume", expected={"task_state": "PAUSED"})
    ch.poll()
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"
    assert fake.prompts[-1]["session_id"] == sid
    env["bridge"].step()
    with pytest.raises(ControlError):
        act(cc, kind="cancel", reason="no longer needed")            # no typed confirmation
    act(cc, kind="cancel", reason="no longer needed", confirm="cancel T1")
    ch.poll()
    assert store.task("game", "T1")["state"] == "CANCELLED"


def test_visual_approval_is_bound_to_revision_and_artifact_bytes(env):
    cc, c, ch, store, root = (env[k] for k in ("cc", "c", "ch", "store", "root"))
    head = git(root, "rev-parse", "HEAD")
    store.update_task("game", "T1", state="AWAITING_HUMAN", head_sha=head,
                      data={"stage": "after_close", "awaiting_kinds": ["visual"]})
    env["bridge"].step()
    card = json.loads(cc.db.query_one("SELECT data FROM cc_tasks")["data"])["approval"]
    assert card["revision"] == head and list(card["artifacts"]["visual"]) == ["docs/evidence/T1/a.png"]
    a = act(cc, kind="approve_review", approval_kind="visual", revision=head,
            artifacts=card["artifacts"]["visual"], expected={"task_state": "AWAITING_HUMAN"})
    (root / "docs/evidence/T1/a.png").write_text("png CHANGED after the card was opened")
    ch.poll()
    v = cc.action_view(a["action_id"])
    assert v["status"] == "rejected" and "evidence changed" in v["status_detail"]
    assert store.approvals("game", "T1") == []
    env["bridge"].step()                                  # the card updates with new hashes
    card = json.loads(cc.db.query_one("SELECT data FROM cc_tasks")["data"])["approval"]
    a2 = act(cc, kind="approve_review", approval_kind="visual", revision=head,
             artifacts=card["artifacts"]["visual"], expected={"task_state": "AWAITING_HUMAN"})
    ch.poll()
    assert cc.action_view(a2["action_id"])["status"] == "applied"
    ap = store.approvals("game", "T1")
    assert len(ap) == 1 and ap[0]["approver"].startswith("victor") and ap[0]["revision"] == head


def test_crash_while_applying_is_reported_uncertain_not_repeated(env):
    cc, ch, store = env["cc"], env["ch"], env["store"]
    a = act(cc, kind="pause", reason="x")
    got = cc.claim("coordinator:test", ["game"], [])
    store.op_begin(f"control:{a['action_id']}", "game", "T1", "control")   # began, then crashed
    real = cc.clock
    cc.clock = lambda: real() + 120                       # Foundry offers it again
    ch.poll()
    cc.clock = real
    v = cc.action_view(a["action_id"])
    assert got and v["status"] == "failed" and "uncertain" in v["status_detail"]
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"


def test_watchdog_alerts_after_the_coordinator_stops(env):
    cc, ch = env["cc"], env["ch"]
    ch.poll()                                             # heartbeat as the engine owner
    assert any(s["source_id"] == "coordinator:test" for s in cc.sources())
    real = cc.clock
    cc.clock = lambda: real() + 1000                      # coordinator gone: no more polls
    cc.watchdog()
    cc.clock = real
    al = [a for a in cc.alerts([], "open") if a["dedupe_key"] == "stale:coordinator:test"]
    assert al and "coordinator" in al[0]["detail"]


def test_bridge_handles_unsupported_endpoints_and_kilo_outage(tmp_path, app):
    fake = FakeKilo(unsupported=("/mcp", "/indexing/status", "/event"))
    url = fake.start()
    root, _ = make_repo(tmp_path, "game", [T1])
    p = load(str(write_profile(tmp_path, "game", root)))
    project = build_project(p, str(tmp_path / "s"), client=KiloClient(url, password=fake.password),
                            foundry=FakeFoundry())
    Store(str(tmp_path / "s" / "c.sqlite"))
    cc = app.state.services.control
    br = TelemetryBridge([project], str(tmp_path / "s" / "c.sqlite"), DirectFoundry(cc),
                         observers={"game": KiloObserver(url, password=fake.password)},
                         use_events=True, poll_s=0.05)
    br._start_events()
    br.step()
    mcp = cc.snapshot("mcp", "game")["data"]
    assert mcp["status_endpoint"] is False
    assert {s["status"] for s in mcp["servers"]} == {"unknown"}
    caps = cc.snapshot("capabilities", "game")["data"]["items"]
    assert caps["indexing"]["supported"] is False and caps["lsp"]["supported"] is True
    for _ in range(50):
        if br.event_state.get("game", "").startswith("unsupported"):
            break
        time.sleep(0.05)
    assert br.event_state["game"].startswith("unsupported")
    fake.stop()
    br.step()                                             # Kilo down
    br._last["beat"] = 0
    br.step()
    src = next(s for s in cc.sources() if s["kind"] == "bridge")
    assert src["detail"]["kilo"]["ok"] is False
    assert any(a["dedupe_key"].startswith("kilo_unreachable") for a in cc.alerts([], "open"))
    br.stop()


def test_event_stream_delivers_streaming_text(env):
    fake, sid, cc, br = env["fake"], env["sid"], env["cc"], env["bridge"]
    br.use_events = True
    br._start_events()
    for _ in range(100):
        if br.event_state.get("game") == "connected":
            break
        time.sleep(0.05)
    br.step()
    mid = fake.add_message(sid, "assistant", [], completed=False)
    part = fake.add_part(sid, {"type": "text", "text": ""}, mid)
    for chunk in ("Checking ", "S45 ", "timing."):
        fake.delta(sid, part["id"], chunk)
    for _ in range(100):
        with br._lock:
            if (br._live.get(part["id"]) or {}).get("text") == "Checking S45 timing.":
                break
        time.sleep(0.05)
    br.step()
    texts = [p["text"] for p in cc.parts(sid, [])["parts"] if p["part_id"] == part["id"]]
    assert texts == ["Checking S45 timing."]


def test_a_question_is_not_recorded_as_an_engine_error(env):
    fake, sid, c, store = env["fake"], env["sid"], env["c"], env["store"]
    fake.ask_question(sid, [{"question": "Go?"}])
    c.tick()
    t = store.task("game", "T1")
    assert t["state"] == "AWAITING_HUMAN" and not (t["next_action"] or "").startswith("error")
    assert not [e for e in store.events("game") if e["kind"] == "error"]
