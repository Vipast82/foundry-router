"""Control center, project-wide PAUSE / RESUME for operator upgrades (Foundry
side): the `project_pause` / `project_resume` actions (project-scoped, no
task-state stale check, note required to pause, one in flight, operator
sessions only, project isolation, handed only to a coordinator declaring the
`project_pause` feature), the per-project status the coordinator reports
(heartbeat `projects: {pid: status}` or a `project` observation) carried by
the overview and the feed, and the header button / pill / dialog / inbox row
logic of the page."""

import json
import shutil
import time

import pytest

from foundry_router.control_center import CONTRACT, project_status
from tests.test_control_center import (BRIDGE_A_KEY, COORD, OP_B_KEY, batch, cc, ingest,  # noqa: F401
                                       login, task_obs)

STATUS = {"paused": True, "mode": "safe", "note": "upgrading to v0.122.3", "actor": "victor",
          "since": 1000.0, "busy_sessions": 1, "running_helpers": 1, "running_reviews": 0,
          "safe_to_upgrade": False}


def _beat(client, features=("decision_answer", "project_pause"), projects=None, headers=COORD,
          sid="coordinator:ws"):
    body = {"source": {"id": sid, "kind": "coordinator", "version": "0.123.0"},
            "contract": CONTRACT, "coordinator_version": "0.123.0", "features": list(features),
            "sent_ts": time.time()}
    if projects is not None:
        body["projects"] = projects
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _claim(client, sid="coordinator:ws"):
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": sid, "projects": ["proj"]})
    assert r.status_code == 200, r.text
    return r.json()["actions"]


def _pause(client, h, key="pause-1234567", **kw):
    body = {"kind": "project_pause", "project_id": "proj", "mode": "safe",
            "note": "upgrading to v0.122.3", "idempotency_key": key, **kw}
    return client.post("/control/api/actions", headers=h, json=body)


def _report(client, aid, status="applied", sid="coordinator:ws"):
    r = client.post(f"/v1/foundry/control/actions/{aid}/status", headers=COORD,
                    json={"source_id": sid, "status": status, "detail": status})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ actions

def test_project_pause_is_project_scoped_and_skips_the_stale_check(client, cc):
    h = login(client)
    # no task reported at all, no task id: still accepted (project-scoped)
    r = _pause(client, h)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "project_pause" and a["task_id"] == "" and a["actor"] == "victor"
    assert a["status"] == "accepted" and "safe point" in a["summary"]
    assert "task_state" not in a["expected"]
    # a task in some state plus a stale expected task_state is ignored for these kinds
    ingest(client, task_obs("IMPLEMENTING"))
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "task_id": "T-1",
        "expected": {"task_state": "COMPLETE"}, "idempotency_key": "resume-1234567"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "project_resume" and r.json()["task_id"] == "T-1"


def test_project_pause_validation(client, cc):
    h = login(client)
    assert _pause(client, h, note="").status_code == 400                    # note required
    assert _pause(client, h, note="   ").status_code == 400
    assert _pause(client, h, note="x" * 501).status_code == 400              # <= 500
    assert _pause(client, h, mode="later").status_code == 400
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "project_pause", "note": "x", "idempotency_key": "nopid-1234567"})
    assert r.status_code == 400 and "project_id" in r.json()["error"]
    # mode defaults to a safe point
    r = _pause(client, h, mode=None)
    assert r.status_code == 200, r.text
    got = _claim(client) if _beat(client) else []
    assert got[0]["payload"] == {"mode": "safe", "note": "upgrading to v0.122.3"}
    # resume: the note is optional but capped too
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "note": "y" * 501,
        "idempotency_key": "resume-long-123"})
    assert r.status_code == 400


def test_project_pause_duplicates_and_resume_rules(client, cc):
    h = login(client)
    # resume of a project that isn't paused is refused
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "idempotency_key": "resume-early-1"})
    assert r.status_code == 409 and r.json()["code"] == "not_paused"
    a = _pause(client, h).json()
    # the same submission again (idempotency key): the same action
    again = _pause(client, h)
    assert again.status_code == 200 and again.json()["action_id"] == a["action_id"]
    assert again.json()["duplicate"] is True
    # a second pause (another tab, new key) while one is pending: the existing one
    other = _pause(client, h, key="pause-tab2-1234", mode="now", note="other tab")
    assert other.status_code == 200 and other.json()["action_id"] == a["action_id"]
    assert other.json()["duplicate"] is True
    # applied: a further pause is a 409 duplicate
    _beat(client)
    assert [x["kind"] for x in _claim(client)] == ["project_pause"]
    _report(client, a["action_id"])
    r = _pause(client, h, key="pause-third-123")
    assert r.status_code == 409 and r.json()["code"] == "duplicate"
    assert r.json()["current"] == a["action_id"]
    # resume is allowed now; a second resume while it is pending returns it
    r1 = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "idempotency_key": "resume-1-12345"})
    r2 = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "note": "done",
        "idempotency_key": "resume-2-12345"})
    assert r1.status_code == 200 and r2.json()["action_id"] == r1.json()["action_id"]
    # once resumed (applied) a new pause is accepted again
    assert [x["kind"] for x in _claim(client)] == ["project_resume"]
    _report(client, r1.json()["action_id"])
    r = _pause(client, h, key="pause-again-123")
    assert r.status_code == 200 and r.json()["action_id"] != a["action_id"]


def test_reported_paused_status_blocks_a_second_pause(client, cc):
    """Paused from the coordinator's own CLI (no action here): the reported
    status is enough to refuse another pause and to allow a resume."""
    _beat(client, projects={"proj": STATUS})
    h = login(client)
    r = _pause(client, h)
    assert r.status_code == 409 and r.json()["code"] == "duplicate"
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "project_resume", "project_id": "proj", "idempotency_key": "resume-cli-1234"})
    assert r.status_code == 200, r.text
    assert r.json()["expected"] == {"project_paused": True}


def test_project_pause_needs_an_operator_session_and_project_access(client, cc):
    # no session
    assert client.post("/control/api/actions", json={"kind": "project_pause", "project_id": "proj",
                                                      "note": "x"}).status_code in (401, 403)
    # an operator restricted to "other" can't pause "proj"
    h = login(client, OP_B_KEY)
    r = _pause(client, h)
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert _pause(client, h, project_id="other", key="pause-other-12").status_code == 200
    # a non-operator scope (direct call) is refused
    from foundry_router.control_center import ControlError
    with pytest.raises(ControlError) as e:
        cc.create_action("bot", [], {"kind": "project_pause", "project_id": "proj", "note": "x",
                                     "idempotency_key": "scope-1234567"}, scopes=["inference"])
    assert e.value.status == 403
    # the restricted operator never sees the other project's action
    h = login(client)
    _pause(client, h, key="pause-proj-1234")
    h = login(client, OP_B_KEY)
    acts = client.get("/control/api/actions").json()["actions"]
    assert {a["project_id"] for a in acts} == {"other"}


def test_claim_hands_project_actions_only_to_a_source_with_the_feature(client, cc):
    h = login(client)
    aid = _pause(client, h).json()["action_id"]
    # unknown source (never heart-beat): withheld, with the reason shown
    assert _claim(client) == []
    a = cc.action_view(aid)
    assert a["status"] == "accepted" and "does not declare the project_pause feature" in \
        a["status_detail"]
    # a coordinator with decision_answer but not project_pause: still withheld,
    # and the compat gate is not degraded by a missing optional feature
    assert _beat(client, features=["decision_answer"])["compat"] == "ok"
    assert _claim(client) == []
    assert cc.action_view(aid)["status"] == "accepted"
    # declared (under `capabilities` too): handed out with its payload
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator"}, "contract": CONTRACT,
            "coordinator_version": "0.123.0", "capabilities": ["decision_answer", "project_pause"],
            "sent_ts": time.time()}
    assert client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body).status_code == 200
    got = _claim(client)
    assert [(x["kind"], x["project_id"], x["task_id"]) for x in got] == [("project_pause", "proj", "")]
    assert got[0]["payload"] == {"mode": "safe", "note": "upgrading to v0.122.3"}
    assert got[0]["actor"] == "victor"


# ------------------------------------------------------------------ status ingest

def test_status_from_the_heartbeat_reaches_the_overview_and_the_feed(client, cc):
    seq0 = cc.last_seq()
    _beat(client, projects={"proj": {**STATUS, "extra": "dropped", "busy_sessions": "2"}})
    h = login(client)
    ov = client.get("/control/api/overview").json()
    st = ov["project_status"]["proj"]
    assert st["paused"] is True and st["mode"] == "safe" and st["busy_sessions"] == 2
    assert st["safe_to_upgrade"] is False and st["actor"] == "victor" and "extra" not in st
    assert st["source_id"] == "coordinator:ws" and "proj" in ov["projects"]
    # the source keeps the project ids (a list), so restricted operators still see it
    src = next(s for s in ov["sources"] if s["source_id"] == "coordinator:ws")
    assert src["detail"]["projects"] == ["proj"]
    items, _ = cc.feed(seq0, [])
    ps = [i for i in items if i["kind"] == "project_status"]
    assert ps and ps[0]["id"] == "proj" and ps[0]["entity"]["paused"] is True
    # an unchanged re-send makes no feed row; a change does
    seq1 = cc.last_seq()
    _beat(client, projects={"proj": {**STATUS, "busy_sessions": "2"}})
    assert not [i for i in cc.feed(seq1, [])[0] if i["kind"] == "project_status"]
    _beat(client, projects={"proj": {**STATUS, "busy_sessions": 0, "running_helpers": 0,
                                     "safe_to_upgrade": True}})
    got = [i for i in cc.feed(seq1, [])[0] if i["kind"] == "project_status"]
    assert got and got[0]["entity"]["safe_to_upgrade"] is True
    _ = h


def test_status_from_a_project_observation(client, cc):
    ingest(client, {"type": "project", "project_id": "proj", **STATUS})
    # the status may also be nested under `status`
    ingest(client, {"type": "project", "project_id": "other", "status": {**STATUS, "mode": "now"}})
    # an empty project id is refused (project-scoped type)
    r = ingest(client, {"type": "project", "project_id": "", **STATUS})
    assert r["skipped"].get("project_id_required") == 1
    # not a status object at all: invalid, never stored
    r = ingest(client, {"type": "project", "project_id": "junk", "hello": 1})
    assert r["skipped"].get("invalid:project") == 1
    login(client)
    ps = client.get("/control/api/overview").json()["project_status"]
    assert set(ps) == {"proj", "other"} and ps["other"]["mode"] == "now"
    assert ps["proj"]["note"] == "upgrading to v0.122.3"


def test_status_respects_project_isolation_and_redaction(client, cc, app):
    # a bridge limited to "proj" can't report "other"
    r = client.post("/v1/foundry/telemetry/batch", headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json=batch({"type": "project", "project_id": "other", **STATUS}))
    assert r.json()["skipped"].get("project_not_allowed") == 1
    # a heartbeat key limited to "proj" can't either
    _beat(client, headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"}, sid="coordinator:a",
          projects={"proj": STATUS, "other": STATUS})
    ingest(client, {"type": "project", "project_id": "other",
                    **{**STATUS, "note": "OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAA"}})
    h = login(client, OP_B_KEY)
    ps = client.get("/control/api/overview").json()["project_status"]
    assert set(ps) == {"other"}                       # restricted operator: own project only
    assert "sk-proj-AAAA" not in json.dumps(ps)       # redacted before storage
    row = app.state.services.db.query_one("SELECT data FROM cc_project_status WHERE project_id='other'")
    assert "sk-proj-AAAA" not in row["data"]
    _ = h


def test_project_status_normalizer():
    assert project_status(None) is None and project_status({"x": 1}) is None
    s = project_status({"paused": 1, "mode": "weird", "busy_sessions": -3, "running_helpers": "x",
                        "safe_to_upgrade": "yes", "note": "n" * 900})
    assert s["paused"] is True and s["mode"] == "" and s["busy_sessions"] == 0
    assert s["running_helpers"] == 0 and s["safe_to_upgrade"] is False and len(s["note"]) == 500


# ------------------------------------------------------------------ page behavior

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
HOSTILE = "<script>alert(1)</script><img src=x onerror=alert(1)>"
SRC_OK = [{"source_id": "coordinator:ws", "kind": "coordinator",
           "detail": {"features": ["decision_answer", "project_pause"]}}]
SRC_OLD = [{"source_id": "coordinator:ws", "kind": "coordinator",
            "detail": {"features": ["decision_answer"]}}]


# node's stdout is decoded with the console code page on Windows: ASCII only
EMIT = (r'const emit = o => console.log(JSON.stringify(o).replace(/[^\x00-\x7f]/g, '
        r'c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")));' + "\n")


def _ui(tmp_path, body):
    from tests.test_control_ui import _script
    from tests.test_control_ui_behavior import between, run_js
    s = _script()
    return run_js(tmp_path, "const tsText = (t, m) => 'T' + t;\n" + EMIT
                  + between(s, "/*bell-start*/", "/*bell-end*/")
                  + between(s, "/*pause-start*/", "/*pause-end*/") + body)


@node
def test_button_and_pill_follow_the_status(tmp_path):
    cases = {
        "running": [{}, []],
        "requested": [{}, [{"kind": "project_pause", "project_id": "proj", "status": "claimed",
                            "created_ts": 1}]],
        "other_project_pending": [{}, [{"kind": "project_pause", "project_id": "x", "status": "accepted"}]],
        "pausing": [{"proj": {"paused": True, "busy_sessions": 1, "running_helpers": 1,
                              "running_reviews": 0, "safe_to_upgrade": False}}, []],
        "safe": [{"proj": {"paused": True, "busy_sessions": 0, "safe_to_upgrade": True,
                           "actor": "victor", "note": "up"}}, []],
        "resuming": [{"proj": {"paused": True, "safe_to_upgrade": True}},
                     [{"kind": "project_resume", "project_id": "proj", "status": "accepted"}]],
        "applied_pause_old": [{}, [{"kind": "project_pause", "project_id": "proj", "status": "applied"}]],
    }
    out = _ui(tmp_path, """
const C = %s, SRC = %s, out = {};
for (const [k, [st, acts]] of Object.entries(C)) { const v = projectPauseView("proj", st, acts, SRC); out[k] = {v, html: pauseCtlHtml(v)}; }
emit(out);
""" % (json.dumps(cases), json.dumps(SRC_OK)))
    r = out["running"]["v"]
    assert r["label"] == "Pause" and r["kind"] == "project_pause" and not r["disabled"] and r["pill"] is None
    assert ">⏸ Pause</button>" in out["running"]["html"]
    q = out["requested"]["v"]
    assert q["pill"]["text"] == "pause requested…" and q["disabled"] and q["label"] == "Pause"
    assert out["other_project_pending"]["v"]["pill"] is None
    p = out["pausing"]["v"]
    assert p["label"] == "Resume" and p["kind"] == "project_resume" and not p["disabled"]
    assert p["pill"] == {"text": "PAUSING — 2 running", "cls": "warn", "title": p["pill"]["title"]}
    s = out["safe"]["v"]
    assert s["pill"]["text"] == "PAUSED — safe to upgrade" and s["pill"]["cls"] == "ok"
    assert "Paused by victor" in s["pill"]["title"] and ': up' in s["pill"]["title"]
    assert 'class="pill ok"' in out["safe"]["html"] and ">▶ Resume</button>" in out["safe"]["html"]
    rs = out["resuming"]["v"]
    assert rs["pill"]["text"] == "resume requested…" and rs["disabled"]
    # only a live action shows as requested; an applied one says nothing without a status
    assert out["applied_pause_old"]["v"]["pill"] is None


@node
def test_button_disabled_when_no_source_declares_the_feature(tmp_path):
    out = _ui(tmp_path, """
const a = projectPauseView("proj", {}, [], %s), b = projectPauseView("proj", {}, [], []), c = projectPauseView("", {}, [], %s);
emit({a, b, c, html: pauseCtlHtml(a)});
""" % (json.dumps(SRC_OLD), json.dumps(SRC_OK)))
    for k in ("a", "b"):
        assert out[k]["disabled"] and out[k]["title"] == "coordinator doesn't support project pause (update it)"
    assert out["c"]["disabled"]
    assert " disabled" in out["html"]
    assert 'title="coordinator doesn&#39;t support project pause (update it)"' in out["html"]


@node
def test_dialog_requires_a_note_to_pause(tmp_path):
    out = _ui(tmp_path, """
emit({
  empty: pauseFormError("project_pause", "safe", ""), blank: pauseFormError("project_pause", "now", "   "),
  long: pauseFormError("project_pause", "safe", "x".repeat(501)), mode: pauseFormError("project_pause", "later", "x"),
  ok: pauseFormError("project_pause", "safe", "upgrading to v0.122.3"), now: pauseFormError("project_pause", "now", "x"),
  resume: pauseFormError("project_resume", "", ""), resumeLong: pauseFormError("project_resume", "", "x".repeat(501))});
""")
    assert out["empty"] and out["blank"] and out["long"] and out["mode"] and out["resumeLong"]
    assert out["ok"] == "" and out["now"] == "" and out["resume"] == ""


@node
def test_pause_text_is_escaped_and_the_inbox_row_does_not_light_the_bell(tmp_path):
    st = {"proj": {"paused": True, "actor": HOSTILE, "note": HOSTILE, "mode": "now", "since": 5,
                   "busy_sessions": 1, "safe_to_upgrade": False},
          HOSTILE: {"paused": True, "actor": "a", "safe_to_upgrade": True},
          "running": {"paused": False}}
    out = _ui(tmp_path, """
const ST = %s;
const v = projectPauseView("proj", ST, [], %s), w = projectPauseView(%s, ST, [], %s);
const inbox = pauseInboxHtml(ST);
emit({ctl: pauseCtlHtml(v) + pauseCtlHtml(w), inbox, bell: bellCount([], []).badge, empty: pauseInboxHtml({})});
""" % (json.dumps(st), json.dumps(SRC_OK), json.dumps(HOSTILE), json.dumps(SRC_OK)))
    for k in ("ctl", "inbox"):
        assert "<script>" not in out[k] and "<img src=x" not in out[k], k
        assert "&lt;script&gt;" in out[k], k
    inbox = out["inbox"]
    assert "safe to upgrade: no" in inbox and "safe to upgrade: yes" in inbox
    assert "since T5" in inbox and "data-paused=\"proj\"" in inbox
    assert 'data-paused="running"' not in inbox                 # not paused: no row
    assert out["empty"] == "" and out["bell"] == 0               # info only, never counted


def test_page_wires_the_header_control_dialog_feed_and_inbox():
    from tests.test_control_ui import PAGE, _script
    html = PAGE.read_text(encoding="utf-8")
    s = _script()
    # header: next to Layout / Graph; the dialog with both modes and a required note
    hdr = html[html.index("<header>"):html.index("</header>")]
    assert hdr.index('id="pause-ctl"') < hdr.index('id="layout-preset"') < hdr.index('id="btn-graph"')
    assert "At a safe point (finish the current step; nothing new starts)" in html
    assert "Now (abort running sessions and helpers; state kept)" in html
    assert 'value="safe" checked' in html and 'id="pause-note" maxlength="500"' in html
    # the feed, overview and inbox wiring
    assert 'case "project_status": S.projectStatus[e.project_id] = e;' in s
    assert "S.projectStatus = S.ov.project_status || {};" in s
    assert "pauseInboxHtml(S.projectStatus) + decisionsInboxHtml(S.tasks) + pHtml" in s
    assert "submitAction(body, `${kind}:${pid}`)" in s and "expected: {}" in s
    # the bell still counts only alerts and pending requests
    bell = s[s.index("/*bell-start*/"):s.index("/*bell-end*/")]
    assert "paused" not in bell and "projectStatus" not in bell


def test_the_coordinators_own_shapes_reach_the_overview(client, cc):
    """The coordinator sends `project_pause: {pid: status}` in its heartbeat and
    the bridge sends `{"type": "project_pause", ...}` observations: both land."""
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.122.3"},
            "contract": CONTRACT, "coordinator_version": "0.122.3",
            "features": ["project_pause"], "sent_ts": time.time(),
            "project_pause": {"proj": {**STATUS, "busy_sessions": 2}}}
    assert client.post("/v1/foundry/telemetry/heartbeat", headers=COORD,
                       json=body).status_code == 200
    login(client)
    st = client.get("/control/api/overview").json()["project_status"]["proj"]
    assert st["paused"] is True and st["busy_sessions"] == 2
    ingest(client, {"type": "project_pause", "project_id": "proj",
                    **{**STATUS, "busy_sessions": 0, "running_helpers": 0,
                       "safe_to_upgrade": True}})
    st = client.get("/control/api/overview").json()["project_status"]["proj"]
    assert st["safe_to_upgrade"] is True
    r = ingest(client, {"type": "project_pause", "project_id": "", **STATUS})
    assert r["skipped"].get("project_id_required") == 1        # project-scoped


# ------------------------------------------------------------------ resume after an upgrade

def test_resume_after_upgrade_is_accepted_and_passed_only_when_true(client, cc):
    h = login(client)
    r = _pause(client, h, resume_after_upgrade="yes")
    assert r.status_code == 400 and "resume_after_upgrade" in r.json()["error"]
    r = _pause(client, h, resume_after_upgrade=True)
    assert r.status_code == 200, r.text
    assert "resume automatically after an upgrade" in r.json()["summary"]
    _beat(client)
    got = _claim(client)
    assert got[0]["payload"] == {"mode": "safe", "note": "upgrading to v0.122.3",
                                 "resume_after_upgrade": True}


def test_resume_after_upgrade_false_is_not_sent(client, cc):
    h = login(client)
    assert _pause(client, h, resume_after_upgrade=False).status_code == 200
    _beat(client)
    assert _claim(client)[0]["payload"] == {"mode": "safe", "note": "upgrading to v0.122.3"}


def test_a_pause_the_coordinator_resumed_after_an_upgrade_no_longer_blocks_a_new_one(client, cc):
    h = login(client)
    a = _pause(client, h, resume_after_upgrade=True).json()
    _beat(client)
    _claim(client)
    _report(client, a["action_id"])
    _beat(client, projects={"proj": STATUS})
    assert _pause(client, h, key="pause-blocked-12").status_code == 409
    # the coordinator restarted on a newer build and resumed it by itself
    up = {"ts": time.time() + 1, "actor": "coordinator (after upgrade, authorized by victor)",
          "authorized_by": "victor", "from_version": "0.122.4", "to_version": "0.122.5",
          "resumed": ["T1 -> IMPLEMENTING"], "left_paused": [], "failed": []}
    _beat(client, projects={"proj": {**STATUS, "paused": False, "upgrade_resume": up}})
    st = client.get("/control/api/overview").json()["project_status"]["proj"]
    assert st["paused"] is False and st["upgrade_resume"]["to_version"] == "0.122.5"
    assert st["upgrade_resume"]["authorized_by"] == "victor"
    r = _pause(client, h, key="pause-after-upg-1")
    assert r.status_code == 200 and r.json()["action_id"] != a["action_id"]


def test_project_status_normalizer_bounds_the_upgrade_fields():
    s = project_status({"paused": True, "resume_after_upgrade": "yes", "paused_version": "v" * 99,
                        "upgrade_resume": {"to_version": "9" * 99, "resumed": ["x" * 999] * 50,
                                           "ts": "bad", "failed": "nope"}})
    assert s["resume_after_upgrade"] is False and len(s["paused_version"]) == 40
    u = s["upgrade_resume"]
    assert len(u["to_version"]) == 40 and len(u["resumed"]) == 20
    assert all(len(x) <= 200 for x in u["resumed"]) and u["ts"] is None and u["failed"] == []
    assert project_status({"paused": False, "upgrade_resume": "x"})["upgrade_resume"] is None
    assert project_status({"resume_after_upgrade": True})["resume_after_upgrade"] is True


@node
def test_dialog_offers_resume_after_upgrade_and_the_pill_reports_it(tmp_path):
    from tests.test_control_ui import PAGE
    html = PAGE.read_text(encoding="utf-8")
    assert ('<input type="checkbox" id="pause-rau"> Resume automatically when the coordinator '
            'restarts on a newer version') in html
    up = {"to_version": "0.122.5", "from_version": "0.122.4", "authorized_by": HOSTILE, "ts": 9}
    out = _ui(tmp_path, """
const UP = %s, SRC = %s;
const on = pauseBody("proj", "T-1", "safe", " up ", true), off = pauseBody("proj", "", "now", "x", false);
const resumed = projectPauseView("proj", {proj: {paused: false, upgrade_resume: UP}}, [], SRC);
const pausedAgain = projectPauseView("proj", {proj: {paused: true, safe_to_upgrade: true, upgrade_resume: UP}}, [], SRC);
const opt = pauseDetail({paused: true, resume_after_upgrade: true, paused_version: "0.122.4"});
emit({on, off, resumed, html: pauseCtlHtml(resumed), pausedAgain, opt, txt: upgradeResumeText({paused: false, upgrade_resume: UP})});
""" % (json.dumps(up), json.dumps(SRC_OK)))
    assert out["on"] == {"kind": "project_pause", "project_id": "proj", "task_id": "T-1",
                         "expected": {}, "mode": "safe", "note": "up", "resume_after_upgrade": True}
    assert "resume_after_upgrade" not in out["off"]
    assert out["resumed"]["pill"]["text"] == "resumed after upgrade"
    assert out["resumed"]["pill"]["cls"] == "ok" and out["resumed"]["label"] == "Pause"
    assert "resumed after upgrade to v0.122.5 (from v0.122.4)" in out["txt"] and "T9" in out["txt"]
    assert "<script>" not in out["html"] and "&lt;script&gt;" in out["html"]
    assert out["pausedAgain"]["pill"]["text"] == "PAUSED — safe to upgrade"
    assert "resumes automatically when the coordinator restarts on a newer version than v0.122.4" \
        in out["opt"]
