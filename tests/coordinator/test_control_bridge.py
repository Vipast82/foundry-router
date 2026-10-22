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


def test_implementer_question_is_answered_by_the_engine_not_the_operator(env):
    """W1.1: a Kilo `question` of the implementer never reaches a person: the engine
    answers it with the standing instruction and the task stays IMPLEMENTING."""
    fake, sid, cc, c, store = (env[k] for k in ("fake", "sid", "cc", "c", "store"))
    rid = fake.ask_question(sid, [{"question": "Which size first?", "header": "Size",
                                   "options": [{"label": "390x844"}, {"label": "1920x1080"}]}])
    c.tick()
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"
    assert fake.replies and fake.replies[0]["request_id"] == rid
    assert "BLOCKED report" in fake.replies[0]["body"]["answers"][0][0]
    env["bridge"].step()
    assert not [a for a in cc.alerts([], "open") if a["kind"] == "question"]


def test_permission_answered_through_the_coordinator_resumes_the_session(env):
    """A Kilo permission ask still reaches the operator (the control center's
    reply_permission action), and the reply resumes the same session."""
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    rid = fake.ask_permission(sid, "edit", [".kilo/tally/r1.txt"])
    c.tick()
    assert store.task("game", "T1")["state"] == "AWAITING_HUMAN"
    env["bridge"].step()
    alerts = [a for a in cc.alerts([], "open") if a["kind"] == "permission"]
    assert len(alerts) == 1 and alerts[0]["request_id"] == rid
    a = act(cc, kind="reply_permission", request_id=rid, session_id=sid, reply="once",
            expected={"task_state": "AWAITING_HUMAN"})
    with pytest.raises(ControlError) as e:                # another tab: refused
        act(cc, kind="reply_permission", request_id=rid, session_id=sid, reply="reject")
    assert e.value.code == "duplicate_reply"
    ch.poll()
    assert fake.replies == [{"kind": "permission", "request_id": rid, "session_id": sid,
                             "body": {"reply": "once"}}]
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
    rid = fake.ask_permission(sid, "edit", ["src/a.txt"])
    env["bridge"].step()                                  # bridge saw it first
    a = act(cc, kind="reply_permission", request_id=rid, session_id=sid, reply="once")
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "queued" and fake.replies == []
    c.tick()                                              # engine records the request
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "applied"
    assert fake.replies[0]["request_id"] == rid


def test_changed_session_is_not_silently_retargeted(env):
    fake, sid, cc, c, ch, store = (env[k] for k in ("fake", "sid", "cc", "c", "ch", "store"))
    rid = fake.ask_permission(sid, "edit", ["src/a.txt"])
    c.tick()
    env["bridge"].step()
    a = act(cc, kind="reply_permission", request_id=rid, session_id=sid, reply="once")
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
    fake.ask_permission(sid, "bash", ["git push"])
    c.tick()
    t = store.task("game", "T1")
    assert t["state"] == "AWAITING_HUMAN" and not (t["next_action"] or "").startswith("error")
    assert not [e for e in store.events("game") if e["kind"] == "error"]


# ------------------------------------------------------------------ v0.122 sprint fields

LIFECYCLE_DDL = """
CREATE TABLE IF NOT EXISTS ev_records (record_id TEXT PRIMARY KEY, idem_key TEXT UNIQUE, project_id TEXT,
  task_id TEXT, run_id TEXT, session_id TEXT, message_id TEXT, part_id TEXT, call_id TEXT, tool TEXT,
  source TEXT, check_id TEXT, check_kind TEXT, chain_id TEXT, seq INT, prereqs TEXT, started_ts REAL,
  ended_ts REAL, exec_status TEXT, exclusion TEXT, result TEXT, counts TEXT, tier TEXT, output_sha TEXT,
  output_bytes INT, excerpt TEXT, truncated INT, truncation_note TEXT, source_revision TEXT, deps TEXT,
  deps_fp TEXT, runtime_id TEXT, artifact_id TEXT, artifact_sha TEXT, provenance TEXT, freshness TEXT,
  stale_reason TEXT, fresh_at TEXT, supersedes TEXT, superseded_by TEXT, created_ts REAL);
CREATE TABLE IF NOT EXISTS ev_chains (chain_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  runtime_id TEXT, spec TEXT, status TEXT, gaps TEXT, checked_ts REAL);
CREATE TABLE IF NOT EXISTS obligations (obligation_id TEXT, project_id TEXT, task_id TEXT, phase TEXT,
  title TEXT, source TEXT, deps TEXT, acceptance TEXT, required_evidence TEXT, status TEXT,
  verified_by TEXT, verified_rev TEXT, findings TEXT, blocking_reason TEXT, next_action TEXT,
  package TEXT, created_ts REAL, updated_ts REAL, PRIMARY KEY (project_id, task_id, obligation_id));
CREATE TABLE IF NOT EXISTS findings (fingerprint TEXT, project_id TEXT, task_id TEXT, class TEXT,
  check_id TEXT, path TEXT, tokens TEXT, first_seen_attempt INT, last_seen_attempt INT, count INT,
  obligation_id TEXT, samples TEXT, PRIMARY KEY (project_id, task_id, fingerprint));
CREATE TABLE IF NOT EXISTS repairs (repair_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  attempt INT, run_id TEXT, start_rev TEXT, preexisting_dirty TEXT, intended_files TEXT,
  obligations TEXT, diagnosis TEXT, strategy TEXT, strategy_fp TEXT, validation_plan TEXT,
  end_rev TEXT, diff_files TEXT, verification_id TEXT, unexpected TEXT, quarantined TEXT,
  outcome TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS budget_ledger (id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT,
  counter TEXT, delta INT, reason TEXT, decision_id TEXT, actor TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  kind TEXT, question TEXT, evidence TEXT, options TEXT, recommendation TEXT, blocks TEXT,
  status TEXT, answer TEXT, authority TEXT, actor TEXT, scope TEXT, supersedes TEXT,
  superseded_by TEXT, opened_ts REAL, answered_ts REAL);
CREATE TABLE IF NOT EXISTS task_memory (mem_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  kind TEXT, text TEXT, refs TEXT, revision TEXT, actor TEXT, authority TEXT, status TEXT,
  superseded_by TEXT, created_ts REAL);
"""


def _rec(store, rid, check, **kw):
    row = {"record_id": rid, "idem_key": rid, "project_id": "game", "task_id": "T1", "check_id": check,
           "session_id": "ses_old", "part_id": f"prt_{rid}", "call_id": f"call_{rid}", "tool": "bash",
           "source": "transcript", "exec_status": "completed", "result": "PASS", "tier": "verified",
           "freshness": "fresh", "source_revision": "a" * 40, "output_sha": "e" * 64,
           "created_ts": 100.0, **kw}
    store.x(f"INSERT INTO ev_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()))


def _task_data(cc):
    return json.loads(cc.db.query_one("SELECT data FROM cc_tasks WHERE task_id='T1'")["data"])


def test_bridge_sprint_fields_with_the_tables_missing(env):
    """Lifecycle off / an older coordinator: no lifecycle table exists. The task
    observation still carries the fields, empty, and says no table was there."""
    d = _task_data(env["cc"])
    assert d["lifecycle_tables"] == []
    assert d["readiness"] == [] and d["obligations"] == [] and d["decisions"] == []
    assert d["evidence"]["counts"] == {s: 0 for s in ("reusable", "stale", "missing", "disputed",
                                                       "excluded")}
    assert d["recovered"] == [] and d["finding_history"]["findings"] == []
    b = d["budgets"]["counters"]
    assert b["ordinary"]["used"] == 1 and b["ordinary"]["max"] == 3
    assert b["ordinary"]["source"] == "task row" and b["guided_extra"]["max"] == 0
    assert "lifecycle_error" not in d


def test_bridge_reads_the_lifecycle_tables_into_bounded_redacted_fields(env):
    cc, store, br = env["cc"], env["store"], env["bridge"]
    store.conn.executescript(LIFECYCLE_DDL)
    _rec(store, "r1", "lune")                                              # reusable
    _rec(store, "r2", "gate", freshness="stale", stale_reason="src/a.txt changed since it ran")
    _rec(store, "r3", "studio", result="PASS", source_revision="b" * 40)  # disputed
    _rec(store, "r4", "studio", result="FAIL", source_revision="b" * 40)
    _rec(store, "r5", "boot", exec_status="excluded", exclusion="void_runtime")
    _rec(store, "r6", "unit", tier="observed")                            # only observed
    _rec(store, "r7", "lune", source="recovery", session_id="ses_older")  # a recovered result
    store.x("INSERT INTO ev_chains (chain_id, project_id, task_id, status, gaps) VALUES "
            "(?, ?, ?, ?, ?)", ("runtime", "game", "T1", "gap", json.dumps(["capture"])))
    store.x("INSERT INTO obligations (obligation_id, project_id, task_id, phase, title, status, "
            "required_evidence, blocking_reason, next_action) VALUES (?,?,?,?,?,?,?,?,?)",
            ("build.gate", "game", "T1", "BUILD", "Gate passes", "open",
             json.dumps([{"kind": "check", "ref": "gate"}, {"kind": "check", "ref": "never_ran"}]),
             "gate stale", "rerun gate"))
    store.x("INSERT INTO findings (fingerprint, project_id, task_id, class, check_id, count, "
            "first_seen_attempt, last_seen_attempt, samples) VALUES (?,?,?,?,?,?,?,?,?)",
            ("fp1", "game", "T1", "product_defect", "gate", 2, 1, 2, json.dumps(["S45 off by one"])))
    for att, fp in ((1, "s1"), (2, "s1")):
        store.x("INSERT INTO repairs (repair_id, project_id, task_id, attempt, strategy, strategy_fp, "
                "outcome, ts) VALUES (?,?,?,?,?,?,?,?)", (f"rp{att}", "game", "T1", att,
                                                          "patch the timer", fp, "failed", att))
    for counter, delta, dec in (("attempt_ordinary", 1, None), ("attempt_ordinary", 1, None),
                                ("attempt_authorized", 2, "dec-9"), ("review_calls", 1, None)):
        store.x("INSERT INTO budget_ledger (project_id, task_id, counter, delta, reason, decision_id, "
                "actor, ts) VALUES (?,?,?,?,?,?,?,?)", ("game", "T1", counter, delta, "r", dec,
                                                        "victor", 1.0))
    store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, evidence, "
            "options, recommendation, blocks, status, opened_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("dec-1", "game", "T1", "scope_change", "Edit src/b.txt?", json.dumps(["plan line 4"]),
             json.dumps(["allow", "refuse"]), "refuse", json.dumps(["build.gate"]), "open", 5.0))
    store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, status) VALUES "
            "(?, ?, ?, ?, ?, ?)", ("dec-other", "other", "T1", "scope_change", "not this project",
                                   "open"))
    store.merge_data("game", "T1", blocker_class="environment", readiness=[
        {"name": "au.rojo.serving", "ok": False, "cls": "environment", "blocking": True,
         "detail": "probe sent Authorization: Bearer abcdefghijklmnop123456 and got refused"}])
    br.step()
    d = _task_data(cc)
    assert set(d["lifecycle_tables"]) >= {"ev_records", "obligations", "decisions", "budget_ledger"}
    states = {c["check_id"]: c["state"] for c in d["evidence"]["checks"]}
    assert states == {"lune": "reusable", "gate": "stale", "studio": "disputed", "boot": "excluded",
                      "unit": "missing", "never_ran": "missing", "capture": "missing"}
    by = {c["check_id"]: c for c in d["evidence"]["checks"]}
    assert by["gate"]["reason"] == "src/a.txt changed since it ran"
    assert by["capture"]["reason"] == "chain runtime: gap"
    assert "needs verified" in by["unit"]["reason"]
    assert by["boot"]["excluded_reasons"] == ["void_runtime"]
    assert by["lune"]["refs"]["part_id"] in ("prt_r1", "prt_r7") and by["lune"]["refs"]["output_sha"]
    assert d["evidence"]["counts"]["missing"] == 3
    assert [r["check_id"] for r in d["recovered"] if r["kind"] == "evidence"] == ["lune"]
    assert d["obligations"][0]["next_action"] == "rerun gate"
    fh = d["finding_history"]
    assert fh["repeated"] == 1 and fh["approach_changed"] is False
    assert fh["findings"][0]["last_strategy"] == "patch the timer"
    b = d["budgets"]["counters"]
    assert b["ordinary"] == {"used": 2, "max": 3, "source": "budget_ledger"}
    assert b["authorized"]["used"] == 2 and b["review_calls"]["source"] == "budget_ledger"
    assert d["budgets"]["authorized"][0]["decision_id"] == "dec-9"
    assert [x["decision_id"] for x in d["decisions"]] == ["dec-1"]       # project isolation
    assert d["decisions"][0]["options"] == ["allow", "refuse"]
    assert d["blocker_class"] == "environment" and d["readiness"][0]["ok"] is False
    blob = dump_cc(env["app"])
    assert "abcdefghijklmnop123456" not in blob                          # redacted both sides
    v = cc.task_view_for("game", "T1", [])["view"]
    assert v["decisions"]["open"][0]["decision_id"] == "dec-1"
    assert any(a["kind"] == "decision" for a in cc.alerts([], "open"))


def test_bridge_heartbeat_declares_contract_versions_and_boot(env):
    cc, br = env["cc"], env["bridge"]
    br._last["beat"] = 0
    br.step()
    src = next(s for s in cc.sources() if s["source_id"] == br.source_id)
    assert src["detail"]["contract"] == "foundry.cc.v1"
    from foundry_coordinator import __version__ as coord_version
    assert src["detail"]["coordinator_version"] == coord_version
    assert src["detail"]["boot_id"] == br.boot_id and "batch_id" in src["detail"]["features"]
    assert src["detail"]["compat"]["state"] == "ok" and br.compat["state"] == "ok"


def test_retried_batch_is_recognized_and_a_restart_causes_no_feed_churn(env):
    cc, store, br = env["cc"], env["store"], env["bridge"]
    direct = br.foundry
    real = direct.telemetry
    results = []

    def lost_reply(b):
        results.append(real(b))
        raise RuntimeError("reply lost")                 # stored by Foundry, but the bridge can't know

    def ok(b):
        results.append(real(b))
        return results[-1]

    store.merge_data("game", "T1", note="changed")
    br._task_cache.clear()
    direct.telemetry = lost_reply
    br.step()
    direct.telemetry = ok
    br.step()                                            # the same chunk again: same batch id
    assert results[0].get("duplicate") is not True and results[1]["duplicate"] is True
    assert results[0]["batch_id"] == results[1]["batch_id"]
    direct.telemetry = real

    def feed_rows():
        return cc.db.query_one("SELECT COUNT(*) AS n FROM cc_feed WHERE kind NOT IN "
                               "('source','alert')")["n"]
    br.step()
    n = feed_rows()
    # a bridge restart: in-memory cursors and fingerprints are gone, everything is re-sent
    br2 = TelemetryBridge([env["c"].projects["game"]], store.path, direct,
                          observers={"game": br.obs["game"]}, use_events=False, poll_s=0.0)
    br2.step()
    assert br2.boot_id != br.boot_id
    assert feed_rows() == n                              # unchanged facts: no new feed rows
    br2.stop()


def test_evidence_summary_never_credits_prose_or_truncated_output():
    from foundry_coordinator.telemetry import evidence_summary
    out = evidence_summary([
        {"check_id": "a", "result": "PASS", "tier": "claim", "freshness": "fresh"},
        {"check_id": "b", "result": "PASS", "tier": "verified", "freshness": "fresh", "truncated": 1},
        {"check_id": "c", "result": "PASS", "tier": "verified", "freshness": None},
        {"check_id": "d", "result": "PASS", "tier": "verified", "freshness": "fresh",
         "superseded_by": "x"}], [], ["e"])
    st = {c["check_id"]: (c["state"], c["reason"]) for c in out["checks"]}
    assert st["a"][0] == "missing" and "claim" in st["a"][1]
    assert st["b"][0] == "missing" and "truncated" in st["b"][1]
    assert st["c"] == ("missing", "freshness unknown")
    assert st["d"][0] == "missing" and st["e"] == ("missing", "no recorded result")
    assert out["counts"]["reusable"] == 0



# ------------------------------------------------------------------ v0.122b review fixes

def test_batch_ids_never_collide_when_a_fact_returns_to_an_earlier_value(env):
    """A -> B -> A: the third send is a new fact, not a duplicate of the first
    (the id carries a send sequence that only advances after a successful
    send; a retry keeps its id)."""
    cc, store, br = env["cc"], env["store"], env["bridge"]
    sent = []
    real = br.foundry.telemetry

    def spy(b):
        sent.append(b["batch_id"])
        return real(b)
    br.foundry.telemetry = spy
    try:
        for v in ("1", "0", "1"):
            store.set_meta("lanes_paused:game", v)
            br.step()
            row = cc.db.query_one("SELECT paused FROM cc_lane_state WHERE project_id='game'")
            assert row["paused"] == (1 if v == "1" else 0), v
        assert len(sent) == len(set(sent)) == 3
        # a failed send keeps the sequence: the retry re-uses the same id
        store.set_meta("lanes_paused:game", "0")
        seq = br.send_seq

        def fail(b):
            sent.append(b["batch_id"])
            raise RuntimeError("network down")
        br.foundry.telemetry = fail
        br.step()
        assert br.send_seq == seq
        br.foundry.telemetry = spy
        br.step()
        assert sent[-1] == sent[-2] and br.send_seq == seq + 1
        assert cc.db.query_one("SELECT paused FROM cc_lane_state WHERE project_id='game'")[
            "paused"] == 0
    finally:
        br.foundry.telemetry = real


def _block(store, data: dict, evidence: dict):
    """Put T1 in BLOCKED the way the engine records it (task data + the BLOCKED
    transition's evidence), directly in the store."""
    t = store.task("game", "T1")
    store.x("UPDATE tasks SET state='BLOCKED', blocker=?, data=?, updated_ts=? WHERE "
            "project_id='game' AND task_id='T1'",
            ("readiness (tooling): rojo missing", json.dumps({**(t["data"] or {}), **data}),
             time.time()))
    store.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, evidence, "
            "actor) VALUES (?,?,?,?,?,?,?)", (time.time(), "game", "T1", t["state"], "BLOCKED",
                                              json.dumps(evidence), "engine"))


def test_bridge_reads_the_engines_readiness_block(env):
    """The engine stores `readiness_block: {cls, items, ...}` in task data and the
    class in the transition evidence: the bridge reports both, and the server
    raises a readiness warning (not a product-defect blocker) for `tooling`."""
    cc, store, br = env["cc"], env["store"], env["bridge"]
    items = [{"name": "au.rojo.installed", "cls": "tooling", "detail": "rojo not on PATH",
              "remedy": "install rojo"}]
    _block(store, {"readiness_block": {"cls": "tooling", "classes": ["tooling"], "items": items,
                                       "auto": False, "since": 1.0, "checks": 1}},
           {"blocker": "readiness (tooling)", "blocker_class": "tooling", "readiness": items})
    br._task_cache.clear()
    br.step()
    d = _task_data(cc)
    assert d["blocker_class"] == "tooling"
    assert d["readiness"] == [{"name": "au.rojo.installed", "ok": False, "cls": "tooling",
                               "detail": "rojo not on PATH", "blocking": True,
                               "remedy": "install rojo"}]
    v = cc.task_view_for("game", "T1", [])["view"]
    assert v["readiness"]["failing"] == 1 and v["blocker_class"] == "tooling"
    al = {a["kind"]: a for a in cc.alerts([], "open") if a["task_id"] == "T1"}
    assert "readiness" in al and "blocker" not in al
    tr = cc.db.query("SELECT data FROM cc_transitions WHERE task_id='T1' AND to_state='BLOCKED'")
    assert json.loads(tr[-1]["data"])["blocker_class"] == "tooling"


def test_bridge_falls_back_to_the_transition_evidence_for_the_blocker_class(env):
    cc, store, br = env["cc"], env["store"], env["bridge"]
    _block(store, {}, {"blocker": "x", "blocker_class": "capability",
                       "readiness": [{"name": "au.mcp", "cls": "capability", "detail": "no mcp"}]})
    br._task_cache.clear()
    br.step()
    d = _task_data(cc)
    assert d["blocker_class"] == "capability" and d["readiness"][0]["ok"] is False
    assert any(a["kind"] == "readiness" for a in cc.alerts([], "open") if a["task_id"] == "T1")


def test_bridge_keeps_the_newest_rows_sums_the_whole_ledger_and_every_open_decision(env):
    cc, store, br = env["cc"], env["store"], env["bridge"]
    store.conn.executescript(LIFECYCLE_DDL)
    for i in range(650):                           # more than the 600-row evidence read
        _rec(store, f"old{i}", f"c{i % 5}", created_ts=float(i))
    _rec(store, "newest", "late_check", created_ts=10_000.0)
    rows = [("game", "T1", "attempt_ordinary", 1, "r", None, "e", float(i)) for i in range(2500)]
    store.conn.executemany("INSERT INTO budget_ledger (project_id, task_id, counter, delta, reason, "
                           "decision_id, actor, ts) VALUES (?,?,?,?,?,?,?,?)", rows)
    store.x("INSERT INTO budget_ledger (project_id, task_id, counter, delta, reason, decision_id, "
            "actor, ts) VALUES (?,?,?,?,?,?,?,?)",
            ("game", "T1", "attempt_authorized", 1, "r", "dec-auth", "victor", 0.5))
    store.conn.executemany("INSERT INTO budget_ledger (project_id, task_id, counter, delta, reason, "
                           "decision_id, actor, ts) VALUES (?,?,?,?,?,?,?,?)",
                           [("game", "T1", "review_calls", 0, "r", None, "e", 1.0)] * 300)
    for i in range(140):                           # 140 closed (older), then 40 open
        store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, status, "
                "opened_ts) VALUES (?,?,?,?,?,?,?)", (f"closed-{i}", "game", "T1", "scope_change",
                                                      "old " * 400, "answered", float(i)))
    for i in range(40):
        store.x("INSERT INTO decisions (decision_id, project_id, task_id, kind, question, status, "
                "opened_ts) VALUES (?,?,?,?,?,?,?)", (f"open-{i}", "game", "T1", "scope_change",
                                                      "q " * 1000, "open", 1.0 + i))
    br._task_cache.clear()
    br.step()
    d = _task_data(cc)
    assert "late_check" in {c["check_id"] for c in d["evidence"]["checks"]}
    assert d["budgets"]["counters"]["ordinary"]["used"] == 2500          # SQL sum, not 2000 rows
    assert d["budgets"]["ledger"]["attempt_ordinary"] == 2500
    assert any(a["decision_id"] == "dec-auth" for a in d["budgets"]["authorized"])
    opened = {x["decision_id"] for x in d["decisions"] if x["status"] == "open"}
    assert opened == {f"open-{i}" for i in range(40)}                    # none bounded away


def test_bound_never_drops_open_decisions():
    from foundry_coordinator.telemetry import _bound
    o = {"decisions": [{"decision_id": f"o{i}", "status": "open", "question": "x" * 2000}
                       for i in range(20)] +
                      [{"decision_id": f"c{i}", "status": "answered", "question": "y" * 2000}
                       for i in range(20)],
         "memory": [{"text": "z" * 2000} for _ in range(5)]}
    out = _bound(o, max_bytes=30_000)
    assert out["truncated"] is True
    assert [x["decision_id"] for x in out["decisions"] if x["status"] == "open"] == \
        [f"o{i}" for i in range(20)]


def test_in_place_lifecycle_updates_are_noticed_and_scoped_per_task(env):
    """An obligation UPDATEd in place (same row count / max rowid) still makes
    the task re-read; another task's change does not rebuild this one."""
    cc, store, br = env["cc"], env["store"], env["bridge"]
    store.conn.executescript(LIFECYCLE_DDL)
    store.x("INSERT INTO obligations (obligation_id, project_id, task_id, phase, title, status, "
            "updated_ts) VALUES (?,?,?,?,?,?,?)", ("build.gate", "game", "T1", "BUILD", "Gate",
                                                    "open", 1.0))
    br._task_cache.clear()
    br.step()
    assert _task_data(cc)["obligations"][0]["status"] == "open"
    sig = br.reader.lifecycle_signature()[("game", "T1")]
    store.x("UPDATE obligations SET status='verified', updated_ts=2.0 "
            "WHERE obligation_id='build.gate'")
    assert br.reader.lifecycle_signature()[("game", "T1")] != sig
    br.step()                                       # within the 10 s cache window
    assert _task_data(cc)["obligations"][0]["status"] == "verified"
    # another task's lifecycle row: T1's signature is unchanged
    before = br.reader.lifecycle_signature()[("game", "T1")]
    store.x("INSERT INTO obligations (obligation_id, project_id, task_id, phase, title, status, "
            "updated_ts) VALUES (?,?,?,?,?,?,?)", ("x", "game", "T9", "BUILD", "other", "open", 3.0))
    assert br.reader.lifecycle_signature()[("game", "T1")] == before


def test_evidence_newest_fresh_execution_decides():
    from foundry_coordinator.telemetry import evidence_summary
    base = {"check_id": "gate", "tier": "verified", "freshness": "fresh"}
    out = evidence_summary([
        {**base, "result": "PASS", "tier": "approved", "source_revision": "a", "created_ts": 1.0},
        {**base, "result": "FAIL", "source_revision": "b", "created_ts": 2.0}], [], [])
    c = out["checks"][0]
    assert c["state"] == "missing" and c["result"] == "FAIL" and "FAIL" in c["reason"]
    # a later PASS clears it again
    out = evidence_summary([
        {**base, "result": "FAIL", "source_revision": "b", "created_ts": 2.0},
        {**base, "result": "PASS", "source_revision": "c", "created_ts": 3.0}], [], [])
    assert out["checks"][0]["state"] == "reusable"
    # a newer prose claim is not an execution: the verified FAIL still decides
    out = evidence_summary([
        {**base, "result": "FAIL", "source_revision": "b", "created_ts": 2.0},
        {**base, "result": "PASS", "tier": "claim", "source_revision": "c", "created_ts": 3.0}],
        [], [])
    assert out["checks"][0]["result"] == "FAIL"


def test_task_helpers_from_the_bridge_are_ordinary_units_only(env):
    """Lane units reach Foundry as lane_unit observations; the task row's
    helper list (and /helpers) carries only ordinary units, each once."""
    from foundry_coordinator import lanes as L
    cc, store, br, c = env["cc"], env["store"], env["bridge"], env["c"]
    P = c.projects["game"]
    t = store.task("game", "T1")
    uid, how = c.helpers.lanes.add(P, t, "failure_triage", "S45 ordering", {}, L.settings(P),
                                   files=["src/a.txt"])
    assert how == "created"
    c.helpers._add("game", "T1", t["run_id"], {"kind": "debug_advice",
                                               "responsibility": "something else"}, "report",
                   c.helpers.settings(P))
    br._task_cache.clear()
    br.step()
    d = _task_data(cc)
    assert d["helpers"] and not [u for u in d["helpers"] if u.get("lane")]
    h = cc.task_helpers("game", "T1", [])
    ids = [u["unit_id"] for u in h["units"]] + [u["unit_id"] for u in h["lanes"]]
    assert ids.count(uid) == 1 and len(ids) == len(set(ids))
    lane = next(u for u in h["lanes"] if u["unit_id"] == uid)
    assert lane["phase"] in ("queued", "admitted", "executing", "waiting_provider")


def test_budget_max_includes_operator_authorized_rows():
    """UI-02 showed '4/3 used up' after an unblock authorized +3: the effective
    max is base + authorized:<counter> rows, with the breakdown shown."""
    from foundry_coordinator.telemetry import budgets_view
    v = budgets_view({"attempt": 1, "sessions": 2, "data": {}},
                     {"max_attempts": 3, "max_sessions_per_task": 8}, [], 0, 0.0,
                     sums={"attempt_ordinary": 4, "authorized:attempt_ordinary": 3,
                           "sessions": 5, "authorized:sessions": 3})
    c = v["counters"]
    assert (c["ordinary"]["used"], c["ordinary"]["max"]) == (4, 6)
    assert (c["ordinary"]["base_max"], c["ordinary"]["authorized"]) == (3, 3)
    assert (c["sessions"]["used"], c["sessions"]["max"]) == (5, 11)


def test_coordinator_heartbeat_declares_project_pause_and_its_status(env):
    """The control channel's heartbeat says it supports project_pause /
    project_resume (features + capabilities.actions) and carries each
    project's pause status."""
    ch, store = env["ch"], env["store"]
    beat = ch._beat()
    assert "project_pause" in beat["features"]
    assert {"project_pause", "project_resume"} <= set(beat["source"]["capabilities"]["actions"])
    st = beat["project_pause"]["game"]
    assert st["paused"] is False and st["busy_sessions"] == 1 and not st["safe_to_upgrade"]
    store.set_project_paused("game", True, "upgrade")
    st = ch._beat()["project_pause"]["game"]
    assert st["paused"] and st["mode"] == "safe" and st["note"] == "upgrade"
    assert st["busy_sessions"] == 1 and not st["safe_to_upgrade"]   # the turn still runs


def test_bridge_reports_a_project_pause_observation_only_when_it_changes(env):
    br, store = env["bridge"], env["store"]
    assert "projpause:game" in br.fp                         # sent with the fixture's step
    again = []
    br._project_pause_facts(again, [], time.time())
    assert again == []                                       # unchanged: not re-sent
    store.set_project_paused("game", True, "upgrade")
    obs = []
    br._project_pause_facts(obs, [], time.time())
    o, = obs
    assert o["type"] == "project_pause" and o["project_id"] == "game"
    assert set(o) == {"type", "project_id", "paused", "mode", "note", "actor", "since",
                      "busy_sessions", "running_helpers", "running_reviews", "safe_to_upgrade"}
    assert o["paused"] is True and o["mode"] == "safe" and o["note"] == "upgrade"
    assert o["busy_sessions"] == 1 and o["safe_to_upgrade"] is False
    from foundry_coordinator.telemetry import FEATURES
    assert "project_pause_status" in FEATURES


# -- BUILD continuing (engine 9edd802: committed partial BUILD progress) ----------------------

PROGRESS_MARKER = ("Continue the committed plan's open BUILD steps; "
                   "final gate and review remain required.")


def _build_progress(store, sid, steps, head="e" * 40, total=None):
    """The engine's own record of committed partial BUILD progress, in the exact
    shape engine.py writes it (transition evidence + task fields + data)."""
    t = store.task("game", "T1")
    return store.transition("game", "T1", "CHANGES_REQUIRED", {
        "head_sha": head, "build_progress": True, "open_steps": steps[:12],
        "session_id": sid, "findings": [PROGRESS_MARKER],
        **({"total_steps": total} if total is not None else {})},
        head_sha=head, next_action="continue BUILD from committed plan",
        data={**t["data"], "findings": None, "build_progress": steps[:12], "free_retry": True})


def test_bridge_threads_build_continuing_from_the_engines_record(env):
    cc, store, br, sid = env["cc"], env["store"], env["bridge"], env["sid"]
    steps = [f"- [ ] step {i} <b>x</b>" for i in range(5)]
    _build_progress(store, sid, steps, total=9)
    store.update_session(sid, status="ended", ended_ts=time.time())
    br.step()
    d = _task_data(cc)
    b = d["build_continuing"]
    assert b["total_steps"] == 9                  # the plan's BUILD rows (engine._build_total)
    assert b["open_steps"] == 5 and b["steps"] == steps and b["truncated"] is False
    assert b["head_sha"] == "e" * 40 and b["session_id"] == sid and b["ts"]
    assert d["findings"] == []                    # nothing listed as a finding
    # Foundry: bounded into both views; not an attempt, no decision
    row = cc.task_row("game", "T1", [])
    vv = cc.verification_view(row)
    assert vv["build"] == {"open_steps": 5, "steps": steps, "truncated": False, "total_steps": 9,
                           "head_sha": "e" * 40, "ts": b["ts"]}
    assert vv["attempts"]["used"] == store.task("game", "T1")["attempt"]
    tv = cc.task_view_for("game", "T1", [])
    assert tv["view"]["build"] == vv["build"] and tv["view"]["decisions"]["open"] == []
    # the transition observation carries the marker (bool + count), not the step text
    tr = cc.db.query("SELECT data FROM cc_transitions WHERE task_id='T1' AND "
                     "to_state='CHANGES_REQUIRED'")
    ev = json.loads(tr[-1]["data"])
    assert ev["build_progress"] is True and ev["open_steps"] == 5
    assert not [a for a in cc.alerts([], "open") if a["task_id"] == "T1"
                and a["kind"] in ("blocker", "decision")]
    # the next BUILD session starts (and is paused / resumed): still continuing
    store.transition("game", "T1", "IMPLEMENTING", {"session_id": "ses_next", "base_sha": "e" * 40,
                                                    "run_id": "T1-a1-next"})
    br.step()
    assert _task_data(cc)["build_continuing"]["open_steps"] == 5
    assert cc.verification_view(cc.task_row("game", "T1", []))["build"]["open_steps"] == 5
    # a real verification failure replaces it: a normal CHANGES_REQUIRED again
    store.transition("game", "T1", "VERIFYING", {"head_sha": "f" * 40, "session_idle": True})
    store.transition("game", "T1", "CHANGES_REQUIRED", {"verification_id": "v1",
                                                        "findings": ["unit tests failed"]},
                     data={**store.task("game", "T1")["data"], "findings": ["unit tests failed"]})
    br.step()
    d = _task_data(cc)
    assert d["build_continuing"] is None and d["findings"] == ["unit tests failed"]
    assert cc.verification_view(cc.task_row("game", "T1", []))["build"] is None


def test_build_continuing_is_bounded_and_never_read_from_model_text():
    from foundry_coordinator.telemetry import build_continuation
    tr = lambda frm, to, **ev: {"from_state": frm, "to_state": to, "evidence": ev, "ts": 1.0}  # noqa: E731
    many = [f"- [ ] {'s' * 400} {i}" for i in range(30)]
    b = build_continuation([tr("IMPLEMENTING", "CHANGES_REQUIRED", build_progress=True,
                               open_steps=many, head_sha="a" * 90)])
    assert b["open_steps"] == 30 and len(b["steps"]) == 12 and b["truncated"]
    assert b["total_steps"] is None               # not reported: never guessed
    for bad in (True, "9", -1, 0, 10 ** 6, 2.5):
        assert build_continuation([tr("IMPLEMENTING", "CHANGES_REQUIRED", build_progress=True,
                                      open_steps=["- [ ] a"], total_steps=bad)])["total_steps"] is None
    assert build_continuation([tr("IMPLEMENTING", "CHANGES_REQUIRED", build_progress=True,
                                  open_steps=["- [ ] a"], total_steps=24)])["total_steps"] == 24
    assert all(len(s) <= 240 for s in b["steps"]) and len(b["head_sha"]) == 64
    # only the engine's explicit flag: findings, next_action or a report saying so don't count
    assert build_continuation([tr("IMPLEMENTING", "CHANGES_REQUIRED",
                                  findings=[PROGRESS_MARKER], build_progress="yes")]) is None
    assert build_continuation([]) is None
    # pause / resume and a question inside the BUILD loop keep it; an unblock ends it
    keep = [tr("IMPLEMENTING", "CHANGES_REQUIRED", build_progress=True, open_steps=["- [ ] a"]),
            tr("CHANGES_REQUIRED", "PAUSED"), tr("PAUSED", "CHANGES_REQUIRED", resumed=True),
            tr("CHANGES_REQUIRED", "IMPLEMENTING"), tr("IMPLEMENTING", "AWAITING_HUMAN"),
            tr("AWAITING_HUMAN", "IMPLEMENTING")]
    assert build_continuation(keep)["open_steps"] == 1
    assert build_continuation(keep + [tr("IMPLEMENTING", "BLOCKED", blocker="x"),
                                      tr("BLOCKED", "READY")]) is None


def test_pause_readiness_ignores_held_locks_of_an_idle_task(env):
    """safe_to_upgrade waits for a running writer turn only: a task held idle by
    the pause keeps its workspace / writer locks, and that counts 0."""
    ch, store, br, sid = env["ch"], env["store"], env["bridge"], env["sid"]
    store.set_project_paused("game", True, "upgrade")
    assert store.q("SELECT COUNT(*) AS n FROM locks WHERE project_id='game'")[0]["n"] >= 1
    st = ch._beat()["project_pause"]["game"]
    assert st["busy_sessions"] == 1 and not st["safe_to_upgrade"]    # the turn runs
    store.update_session(sid, status="ended", outcome="idle", ended_ts=time.time())
    assert store.task("game", "T1")["state"] == "IMPLEMENTING"
    assert store.q("SELECT COUNT(*) AS n FROM locks WHERE project_id='game'")[0]["n"] >= 1
    st = ch._beat()["project_pause"]["game"]
    assert st["busy_sessions"] == 0 and st["safe_to_upgrade"] is True
    obs = []
    br._project_pause_facts(obs, [], time.time())
    assert obs and obs[0]["safe_to_upgrade"] is True


# ------------------------------------------------------------------ v0.123 lead recovery

LEAD_DDL = """
CREATE TABLE IF NOT EXISTS recovery_cases (case_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
  run_id TEXT, trigger TEXT, state TEXT, fingerprint TEXT, source_revision TEXT, dossier_sha TEXT,
  lead_session_id TEXT, lead_model TEXT, classification TEXT, proposed_action TEXT, validation TEXT,
  upgrade_id TEXT, rollback_result TEXT, outcome TEXT, counters TEXT, created_ts REAL,
  updated_ts REAL, deadline_ts REAL, paused INTEGER, instructions TEXT);
CREATE TABLE IF NOT EXISTS recovery_actions (action_id TEXT PRIMARY KEY, case_id TEXT, seq INTEGER,
  kind TEXT, actor TEXT, revision TEXT, repo TEXT, scope_files TEXT, detail TEXT, result TEXT,
  status TEXT, op_key TEXT UNIQUE, ts REAL);
CREATE TABLE IF NOT EXISTS upgrade_requests (upgrade_id TEXT PRIMARY KEY, case_id TEXT,
  project_id TEXT, task_id TEXT, candidate_ref TEXT, candidate_sha TEXT, manifest TEXT, status TEXT,
  prior_sha TEXT, backup_path TEXT, log TEXT, created_ts REAL, updated_ts REAL);
"""


def _case(store, cid, state="DIAGNOSING", pid="game", tid="T1", **kw):
    row = {"case_id": cid, "project_id": pid, "task_id": tid, "run_id": "T1-a1",
           "trigger": "BLOCKED: recovery_escalation", "state": state, "fingerprint": f"fp-{cid}",
           "source_revision": "a" * 40, "lead_session_id": f"ses_lead_{cid}",
           "lead_model": "claude-opus-5-5", "classification": "coordinator",
           "proposed_action": json.dumps({"action": {"kind": "coordinator_repair"}}),
           "counters": json.dumps({"diagnoses": 2, "candidates": 1, "quota_calls": 5}),
           "created_ts": time.time() - 600, "updated_ts": time.time(), "paused": 0,
           "instructions": json.dumps(["look at telemetry.py"]), "outcome": "", **kw}
    store.x(f"INSERT INTO recovery_cases ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()))


def _lead_act(store, cid, seq, **kw):
    row = {"action_id": f"{cid}-a{seq}", "case_id": cid, "seq": seq, "kind": "diagnose",
           "actor": "lead", "revision": "b" * 40, "repo": "foundry-router",
           "scope_files": json.dumps(["foundry_coordinator/engine.py"]), "detail": "{}",
           "result": f"step {seq}", "status": "done", "op_key": f"lead:{cid}:{seq}", "ts": float(seq),
           **kw}
    store.x(f"INSERT INTO recovery_actions ({', '.join(row)}) VALUES "
            f"({', '.join('?' * len(row))})", tuple(row.values()))


def _features(cc, br):
    br._last["beat"] = 0
    br.step()
    return next(s for s in cc.sources() if s["source_id"] == br.source_id)["detail"]["features"]


def test_bridge_lead_field_absent_without_the_lead_tables(env):
    d = _task_data(env["cc"])
    assert "lead" not in d and "lead_error" not in d
    assert "lead_manager" not in _features(env["cc"], env["bridge"])
    assert "lead_manager" not in env["ch"]._beat()["features"]        # engine has no lead


def test_bridge_reads_the_lead_tables_into_a_bounded_redacted_field(env):
    cc, store, br = env["cc"], env["store"], env["bridge"]
    store.conn.executescript(LEAD_DDL)
    br.step()
    assert _task_data(cc)["lead"] is None                       # tables, but no case yet
    assert "lead_manager" in _features(cc, br)
    _case(store, "case-old", "RESOLVED", updated_ts=time.time() + 50)   # newer, but closed
    _case(store, "case-1", "UPGRADING", upgrade_id="up-1")
    _case(store, "case-x", "NEEDS_VICTOR", pid="other")         # another project, same task id
    for i in range(1, 36):
        _lead_act(store, "case-1", i)
    _lead_act(store, "case-1", 36, status="started", kind="validate",
              result="probe sent Authorization: Bearer abcdefghijklmnop123456",
              scope_files=json.dumps([f"f{i}.py" for i in range(60)]))
    store.x("INSERT INTO upgrade_requests (upgrade_id, case_id, project_id, task_id, candidate_sha, "
            "status, prior_sha, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            ("up-1", "case-1", "game", "T1", "c" * 40, "verifying", "p" * 40, 1.0, 2.0))
    br.step()
    lv = _task_data(cc)["lead"]
    assert lv["case_id"] == "case-1" and lv["state"] == "UPGRADING" and lv["active"] is True
    assert lv["lead_session_id"] == "ses_lead_case-1" and lv["lead_model"] == "claude-opus-5-5"
    assert lv["classification"] == "coordinator" and lv["trigger"].startswith("BLOCKED")
    c = lv["counters"]
    assert c["diagnoses"] == {"used": 2, "max": 3} and c["candidates"] == {"used": 1, "max": 2}
    assert c["upgrades"] == {"used": 0, "max": 1} and c["quota_calls"] == {"used": 5, "max": 12}
    assert c["minutes"]["max"] == 240 and c["minutes"]["used"] >= 9
    seqs = [a["seq"] for a in lv["actions"]]
    assert seqs == list(range(7, 37))                           # the newest 30, oldest first
    last = lv["actions"][-1]
    assert last["status"] == "started" and len(last["scope_files"]) == 40 and last["scope_more"] == 20
    assert set(last) == {"seq", "kind", "actor", "revision", "repo", "scope_files", "scope_more",
                         "result", "status", "ts"}
    assert lv["current_action"]["kind"] == "validate" and lv["current_action"]["source"] == "journal"
    assert lv["upgrade"]["upgrade_id"] == "up-1" and lv["upgrade"]["status"] == "verifying"
    assert lv["upgrade"]["candidate_sha"] == "c" * 40 and lv["upgrade"]["prior_sha"] == "p" * 40
    assert lv["paused"] is False and lv["instructions_count"] == 1
    blob = dump_cc(env["app"])
    assert "abcdefghijklmnop123456" not in blob                 # redacted before it left
    # an in-place update (the case pauses, its state moves) is noticed
    store.x("UPDATE recovery_cases SET paused=1, state='NEEDS_VICTOR', updated_ts=? WHERE "
            "case_id='case-1'", (time.time() + 1,))
    store.x("UPDATE recovery_actions SET status='done' WHERE case_id='case-1' AND seq=36")
    br.step()
    lv = _task_data(cc)["lead"]
    assert lv["paused"] is True and lv["state"] == "NEEDS_VICTOR"
    assert lv["current_action"] == {"kind": "coordinator_repair", "status": "proposed", "seq": None,
                                    "ts": None, "source": "case"}
    assert any(a["kind"] == "lead_needs_victor" and a["task_id"] == "T1"
               for a in cc.alerts([], "open"))
    # only closed cases left: the latest one is shown, not active
    store.x("UPDATE recovery_cases SET state='RESOLVED', outcome='resumed' WHERE case_id='case-1'")
    br.step()
    lv = _task_data(cc)["lead"]
    assert lv["case_id"] == "case-old" and lv["active"] is False
    assert not any(a["kind"] == "lead_needs_victor" for a in cc.alerts([], "open"))


class FakeLead:
    def __init__(self, enabled=True, fail=None):
        self.calls, self._enabled, self.fail = [], enabled, fail

    def enabled(self, P):
        return self._enabled

    def pause_case(self, case_id, actor, note=""):
        self.calls.append(("pause_case", case_id, actor, note))
        return {"state": "DIAGNOSING", "paused": True}

    def takeover(self, case_id, actor):
        if self.fail:
            raise self.fail
        self.calls.append(("takeover", case_id, actor))
        return {"state": "DIAGNOSING"}

    def instruct(self, case_id, text, actor):
        self.calls.append(("instruct", case_id, text, actor))
        return None


def _lead_env(env):
    store = env["store"]
    store.conn.executescript(LEAD_DDL)
    _case(store, "case-1", "DIAGNOSING")
    _case(store, "case-o", "DIAGNOSING", tid="T9")
    env["bridge"].step()
    return env["cc"], env["ch"], env["c"]


def test_control_applies_lead_case_actions_through_engine_lead(env):
    cc, ch, c = _lead_env(env)
    fake = FakeLead()
    c.lead = fake
    beat = ch._beat()
    assert "lead_manager" in beat["features"]
    assert {"lead_case_pause", "lead_case_takeover",
            "lead_case_instruct"} <= set(beat["source"]["capabilities"]["actions"])
    a1 = act(cc, kind="lead_case_instruct", case_id="case-1", note="only touch telemetry.py")
    a2 = act(cc, kind="lead_case_pause", case_id="case-1", note="wait for me")
    a3 = act(cc, kind="lead_case_takeover", case_id="case-1")
    ch._last_beat = 0
    ch.poll()
    assert fake.calls == [("instruct", "case-1", "only touch telemetry.py", "victor (control center)"),
                          ("pause_case", "case-1", "victor (control center)", "wait for me"),
                          ("takeover", "case-1", "victor (control center)")]
    for a in (a1, a2, a3):
        v = cc.action_view(a["action_id"])
        assert v["status"] == "applied", v
        assert v["result"]["case_id"] == "case-1"
    assert cc.action_view(a2["action_id"])["result"]["case_state"] == "DIAGNOSING"
    # applied once: a re-offered action reports the stored outcome, not a second call
    ch.handle({"action_id": a1["action_id"], "kind": "lead_case_instruct", "project_id": "game",
               "task_id": "T1", "payload": {"case_id": "case-1", "note": "x"}})
    assert len(fake.calls) == 3


def test_control_rejects_lead_actions_without_a_lead_or_for_another_task(env):
    cc, ch, c = _lead_env(env)
    reports = []
    ch._report = lambda a, status, detail, result=None: reports.append((a["action_id"], status,
                                                                        detail))
    base = {"kind": "lead_case_pause", "project_id": "game", "task_id": "T1",
            "payload": {"case_id": "case-1", "note": ""}}
    ch.handle({**base, "action_id": "nolead-1"})                      # no engine.lead
    assert reports[-1] == ("nolead-1", "rejected", "lead manager not available in this coordinator")
    assert "lead_manager" not in ch._beat()["features"]
    c.lead = FakeLead(enabled=False)
    assert "lead_manager" not in ch._beat()["features"]                # no project enables it
    fake = c.lead
    ch.handle({**base, "action_id": "other-1", "payload": {"case_id": "case-o", "note": ""}})
    assert reports[-1][1] == "rejected" and "another task" in reports[-1][2]
    ch.handle({**base, "action_id": "empty-1", "kind": "lead_case_instruct",
               "payload": {"case_id": "case-1", "note": " "}})
    assert reports[-1][1] == "rejected" and "needs text" in reports[-1][2]
    ch.handle({**base, "action_id": "long-1", "payload": {"case_id": "case-1", "note": "x" * 2001}})
    assert reports[-1][1] == "rejected"
    ch.handle({**base, "action_id": "nocase-1", "payload": {"note": ""}})
    assert reports[-1][1] == "rejected" and "case_id" in reports[-1][2]
    fake.fail = KeyError("no such case case-1")
    ch.handle({**base, "action_id": "refused-1", "kind": "lead_case_takeover"})
    assert reports[-1][1] == "rejected" and "no such case" in reports[-1][2]
    assert fake.calls == []
    # a lead object without the method: the same honest rejection
    c.lead = object()
    ch.handle({**base, "action_id": "nomethod-1"})
    assert reports[-1] == ("nomethod-1", "rejected", "lead manager not available in this coordinator")


def test_lead_controls_end_to_end_with_the_real_lead_manager(env):
    """The real LeadManager's own tables and operator controls (engine.lead as
    the integrator wires it): bridge field -> Foundry view -> typed action ->
    control channel -> LeadManager, with the journal showing the result."""
    lead_mod = pytest.importorskip("foundry_coordinator.lead")
    cc, store, br, ch, c = env["cc"], env["store"], env["bridge"], env["ch"], env["c"]
    lm = lead_mod.LeadManager(c)                    # migrates recovery_cases / recovery_actions
    lm.enabled = lambda P: True                     # as if [lead_manager] enabled = true
    c.lead = lm
    _case(store, "rc-1", "DIAGNOSING", trigger=json.dumps({"state": "BLOCKED",
                                                           "reason": "recovery_escalation"}))
    br.step()
    lv = cc.task_view_for("game", "T1", [])["view"]["lead"]
    assert lv["case_id"] == "rc-1" and lv["trigger"] == "BLOCKED: recovery_escalation"
    act(cc, kind="lead_case_instruct", case_id="rc-1", note="only touch telemetry.py")
    ch._last_beat = 0
    ch.poll()
    assert "lead_manager" in ch._beat()["features"]
    a = act(cc, kind="lead_case_takeover", case_id="rc-1", note="mine now")
    ch.poll()
    assert cc.action_view(a["action_id"])["status"] == "applied"
    case = lm.case("rc-1")
    assert case["paused"] and case["instructions"][-1]["text"] == "only touch telemetry.py"
    br.step()
    lv = cc.task_view_for("game", "T1", [])["view"]["lead"]
    assert lv["paused"] is True and lv["instructions_count"] == 2      # the seeded one + ours
    assert [x["kind"] for x in lv["actions"]][-2:] == ["instruct", "takeover"]
    assert lv["actions"][-1]["actor"] == "victor (control center)"
