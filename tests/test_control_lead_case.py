"""Control center, v0.123 lead recovery (Foundry side and the page): the
bridge's `lead` task field stored and exposed in the task view (bounded,
typed), the lead_case_pause / lead_case_takeover / lead_case_instruct actions
(operator only, project isolation, bound to the task's own active case, note
rules, feature-gated to a coordinator declaring `lead_manager`), the
NEEDS_VICTOR alert lifecycle, and the page's Lead recovery section (state
pills, escaping, the journal, the instruct form)."""

import json
import time

import pytest

from foundry_router.control_center import CONTRACT, ControlError
from tests.test_control_center import (COORD, OP_B_KEY, cc, ingest, login,  # noqa: F401
                                       task_obs)

HOSTILE = "<script>alert(1)</script><img src=x onerror=alert(1)>"


def lead(state="DIAGNOSING", case_id="case-1", **kw):
    """The `lead` field a v0.123 bridge sends (telemetry.lead_view)."""
    base = {"case_id": case_id, "state": state, "active": state not in ("RESOLVED", "CANCELLED"),
            "classification": "coordinator", "trigger": "BLOCKED: recovery_escalation",
            "fingerprint": "fp-abc123", "lead_session_id": "ses_lead1",
            "lead_model": "claude-opus-5-5",
            "current_action": {"kind": "diagnose", "status": "started", "seq": 2, "ts": 20.0,
                               "source": "journal"},
            "counters": {"diagnoses": {"used": 1, "max": 3}, "candidates": {"used": 0, "max": 2},
                         "upgrades": {"used": 0, "max": 1}, "repeats": {"used": 0, "max": 1},
                         "quota_calls": {"used": 2, "max": 12},
                         "minutes": {"used": 7, "max": 240}},
            "actions": [{"seq": 1, "kind": "open_case", "actor": "engine", "revision": "a" * 40,
                         "repo": "game", "scope_files": [], "result": "opened", "status": "done",
                         "ts": 10.0},
                        {"seq": 2, "kind": "diagnose", "actor": "lead", "revision": "a" * 40,
                         "repo": "game", "scope_files": ["src/a.luau"], "result": "",
                         "status": "started", "ts": 20.0}],
            "upgrade": None, "paused": False, "instructions_count": 0, "outcome": ""}
    base.update(kw)
    return base


def _coord_beat(client, features=("decision_answer", "lifecycle", "lead_manager")):
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.123.0"},
            "contract": CONTRACT, "coordinator_version": "0.123.0", "features": list(features),
            "sent_ts": time.time()}
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text


def _claim(client):
    return client.post("/v1/foundry/control/claim", headers=COORD,
                       json={"source_id": "coordinator:ws", "projects": ["proj"]}).json()["actions"]


def _act(client, h, kind="lead_case_pause", **kw):
    body = {"kind": kind, "project_id": "proj", "task_id": "T-1", "case_id": "case-1",
            "note": "", "idempotency_key": f"lead-{time.time_ns()}", **kw}
    return client.post("/control/api/actions", headers=h, json=body)


def _view(client):
    return client.get("/control/api/tasks/proj/T-1/view").json()["view"]


# ------------------------------------------------------------------ storage / view

def test_lead_field_is_stored_and_typed_in_the_task_view(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead(
        trigger=HOSTILE, counters={"diagnoses": {"used": "1", "max": True}},
        actions=[{"seq": 3, "kind": "k" * 500, "scope_files": ["f"] * 80, "result": "r" * 5000,
                  "status": "done", "ts": "nope"}] * 40,
        upgrade={"upgrade_id": "up-1", "status": "verifying", "candidate_sha": "c" * 40,
                 "prior_sha": "p" * 40, "extra": "dropped"})))
    login(client)
    row = cc.db.query_one("SELECT data FROM cc_tasks WHERE task_id='T-1'")
    assert json.loads(row["data"])["lead"]["case_id"] == "case-1"           # stored as reported
    v = _view(client)
    lv = v["lead"]
    assert v["lead_reported"] is True
    assert lv["case_id"] == "case-1"
    assert lv["state"] == "DIAGNOSING" and lv["active"] and lv["known_state"]
    assert lv["counters"]["diagnoses"] == {"used": None, "max": None}       # never coerced
    assert len(lv["actions"]) == 30 and len(lv["actions"][0]["kind"]) == 60
    assert len(lv["actions"][0]["scope_files"]) == 40 and len(lv["actions"][0]["result"]) == 800
    assert lv["actions"][0]["ts"] is None
    assert lv["upgrade"] == {"upgrade_id": "up-1", "status": "verifying", "candidate_sha": "c" * 40,
                             "prior_sha": "p" * 40, "updated_ts": None}
    assert lv["pending_actions"] == [] and lv["lead_model"] == "claude-opus-5-5"
    # the overview's task row carries the view, not the raw field twice
    t = client.get("/control/api/overview").json()["tasks"][0]
    assert t["task_view"]["lead"]["case_id"] == "case-1" and "lead" not in t["data"]


def test_no_lead_tables_means_no_lead_and_no_case_is_none(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x"))
    login(client)
    v = _view(client)
    assert v["lead"] is None and v["lead_reported"] is False
    ingest(client, task_obs("BLOCKED", blocker="x", lead=None))
    v = _view(client)
    assert v["lead"] is None and v["lead_reported"] is True


# ------------------------------------------------------------------ actions

def test_lead_actions_are_gated_bound_to_the_case_and_claimed_by_lead_manager_only(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead()))
    h = login(client)
    r = _act(client, h, "lead_case_pause", note="hold on")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "lead_case_pause" and a["expected"] == {"case_id": "case-1",
                                                                "case_state": "DIAGNOSING"}
    assert "hold on" not in a["summary"] and "note 7 chars" in a["summary"]
    # the task view shows it in flight for that case
    assert [p["kind"] for p in _view(client)["lead"]["pending_actions"]] == ["lead_case_pause"]
    # a coordinator without lead_manager is not handed it
    _coord_beat(client, ("decision_answer", "lifecycle"))
    assert _claim(client) == []
    st = cc.action_view(a["action_id"])
    assert st["status"] == "accepted" and "lead_manager" in st["status_detail"]
    _coord_beat(client)
    got = _claim(client)
    assert [x["kind"] for x in got] == ["lead_case_pause"]
    assert got[0]["payload"] == {"case_id": "case-1", "note": "hold on"}


def test_lead_action_rules(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead()))
    h = login(client)
    # instruct needs a note, <= 2000 chars
    r = _act(client, h, "lead_case_instruct", note="  ")
    assert r.status_code == 400
    r = _act(client, h, "lead_case_instruct", note="x" * 2001)
    assert r.status_code == 400
    r = _act(client, h, "lead_case_takeover", note="x" * 2001)
    assert r.status_code == 400
    r = _act(client, h, "lead_case_instruct", note="x" * 2000)
    assert r.status_code == 200 and r.json()["kind"] == "lead_case_instruct"
    # another task's / an unknown case id
    r = _act(client, h, "lead_case_pause", case_id="case-other")
    assert r.status_code == 404 and r.json()["code"] == "unknown_case"
    r = _act(client, h, "lead_case_pause", case_id="")
    assert r.status_code == 400
    # the case moved on since the operator looked
    r = _act(client, h, "lead_case_pause", expected={"case_state": "REPAIRING"})
    assert r.status_code == 409 and r.json()["code"] == "stale_case"
    # the task state moving on doesn't make a case action stale
    r = _act(client, h, "lead_case_takeover", expected={"task_state": "IMPLEMENTING"})
    assert r.status_code == 200, r.text
    # a paused case can't be paused again; a closed case takes no action
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead(paused=True)))
    r = _act(client, h, "lead_case_pause")
    assert r.status_code == 409 and r.json()["code"] == "duplicate"
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead("RESOLVED")))
    r = _act(client, h, "lead_case_instruct", note="late")
    assert r.status_code == 409 and r.json()["code"] == "stale_case"
    # no lead field at all
    ingest(client, task_obs("BLOCKED", blocker="x"))
    r = _act(client, h, "lead_case_pause")
    assert r.status_code == 404


def test_lead_actions_operator_only_and_project_isolation(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead()))
    body = {"kind": "lead_case_takeover", "project_id": "proj", "task_id": "T-1",
            "case_id": "case-1", "note": "", "idempotency_key": "scope-12345678"}
    with pytest.raises(ControlError) as e:
        cc.create_action("bridge-a", [], dict(body), scopes=["telemetry"])
    assert e.value.status == 403
    # an operator limited to "other" can't act on proj's case
    h = login(client, OP_B_KEY)
    r = client.post("/control/api/actions", headers=h, json={**body, "idempotency_key": "iso-1234567"})
    assert r.status_code == 403
    # nor see its view
    assert client.get("/control/api/tasks/proj/T-1/view").status_code == 404


# ------------------------------------------------------------------ the NEEDS_VICTOR alert

def _lead_alerts(cc, status="open"):
    return [a for a in cc.alerts([], status) if a["kind"] == "lead_needs_victor"]


def test_needs_victor_alert_lifecycle(client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead()))
    assert _lead_alerts(cc) == []
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead(
        "NEEDS_VICTOR", outcome="bounds exhausted: 3/3 diagnoses")))
    al = _lead_alerts(cc)
    assert len(al) == 1
    a = al[0]
    assert a["project_id"] == "proj" and a["task_id"] == "T-1" and a["severity"] == "high"
    assert a["dedupe_key"] == "lead_needs_victor:proj:T-1:case-1"
    assert "bounds exhausted" in a["detail"] and "needs Victor" in a["title"]
    # the same report again: still one alert
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead(
        "NEEDS_VICTOR", outcome="bounds exhausted: 3/3 diagnoses", instructions_count=1)))
    assert len(_lead_alerts(cc)) == 1
    # the case leaves NEEDS_VICTOR: resolved
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead("RESUMING")))
    assert _lead_alerts(cc) == []
    done = _lead_alerts(cc, "resolved")
    assert len(done) == 1 and "RESUMING" in done[0]["resolution"]
    # a new case that needs Victor alerts again; the task losing its case resolves it
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead("NEEDS_VICTOR", case_id="case-2")))
    assert [a["dedupe_key"] for a in _lead_alerts(cc)] == ["lead_needs_victor:proj:T-1:case-2"]
    ingest(client, task_obs("BLOCKED", blocker="x", lead=None))
    assert _lead_alerts(cc) == []
    # an observation without the field (an older bridge) leaves alerts alone
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead("NEEDS_VICTOR", case_id="case-3")))
    ingest(client, task_obs("BLOCKED", blocker="y"))
    assert len(_lead_alerts(cc)) == 1


# ------------------------------------------------------------------ the page (node)

node = pytest.mark.skipif(not __import__("shutil").which("node"), reason="node not installed")


def _page(tmp_path, body: str):
    from tests.test_control_ui import _script
    from tests.test_control_ui_behavior import between, run_js
    s = _script()
    return run_js(tmp_path, """
const S = {tasks: new Map(), project: "proj", task: "T-1", openProv: new Set(), idem: {}};
const taskKey = (p, t) => p + "/" + t; const curTask = () => S.tasks.get("proj/T-1");
const short = (s, n = 10) => s ? String(s).slice(0, n) : "";
const tsText = t => t ? "T" + t : ""; const nowS = () => 1000; const fullTs = t => t ? "TS" + t : "";
const NA = why => `<span class="dim">N/A</span>`;
const kv = rows => rows.filter(Boolean).map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("");
const stateCls = st => ({BLOCKED: "bad"})[st] || "";
""" + between(s, "/*sprint-start*/", "/*sprint-end*/")
                  + between(s, "/*lead-start*/", "/*lead-end*/") + """
const R = {
""" + between(s, "  /*lead-render-start*/", "  /*lead-render-end*/") + """
};
""" + body)


def _task(lv, reported=True):
    return {"project_id": "proj", "task_id": "T-1", "state": "BLOCKED", "data": {},
            "task_view": {"reported": [], "lead": lv, "lead_reported": reported}}


@node
def test_lead_section_renders_escaped_with_pills_journal_and_controls(tmp_path, client, cc):
    ingest(client, task_obs("BLOCKED", blocker="x", lead=lead(
        "NEEDS_VICTOR", trigger=HOSTILE, outcome=HOSTILE, classification=HOSTILE,
        lead_session_id='ses_"><b>x', lead_model=HOSTILE,
        actions=[{"seq": 1, "kind": HOSTILE, "actor": HOSTILE, "revision": "r" * 40,
                  "repo": HOSTILE, "scope_files": [HOSTILE, "src/b.luau"], "result": HOSTILE,
                  "status": "failed", "ts": 5.0}],
        upgrade={"upgrade_id": "up-1", "status": "rolled_back", "candidate_sha": "c" * 40,
                 "prior_sha": "p" * 40})))
    login(client)
    t = client.get("/control/api/overview").json()["tasks"][0]
    out = _page(tmp_path, """
const T = %s; S.tasks.set("proj/T-1", T);
console.log(JSON.stringify({html: R.lead(null), head: leadHeadPill(T)}));
""" % json.dumps(t))
    h = out["html"]
    assert "<script>" not in h and "<img src=x" not in h and "&lt;script&gt;" in h
    assert '"><b>x' not in h and "ses_&quot;&gt;&lt;b&gt;x" in h        # attribute-safe
    assert 'class="pill lead lead-needs_victor"' in h and "This case needs you." in h
    assert 'data-open="ses_&quot;&gt;&lt;b&gt;x"' in h                   # opens like other sessions
    assert "Counters vs bounds" in h and "Lead diagnoses" in h and "Paid calls" in h
    assert "<details data-prov=\"lead:case-1:1\">" in h and "rrrrrrrrrrrr" in h
    assert "src/b.luau" in h and "Journal (1)" in h
    assert ">rolled_back</span>" in h and 'pill bad' in h
    assert "Pause case" in h and "Take over" in h and "Add instruction" in h
    assert 'maxlength="2000"' in h and 'data-lstate="NEEDS_VICTOR"' in h
    assert "lead-needs_victor" in out["head"]


@node
def test_lead_state_pills_are_distinct_and_unknown_is_safe(tmp_path):
    out = _page(tmp_path, """
const states = ["DIAGNOSING", "REPAIRING", "UPGRADING", "RESUMING", "NEEDS_VICTOR", "DEFERRED", "RESOLVED", "<x>"];
console.log(JSON.stringify(states.map(s => leadStatePill({state: s}))));
""")
    classes = [p.split('class="')[1].split('"')[0] for p in out]
    assert len(set(classes[:7])) == 7                                      # each its own style
    assert classes[-1] == "pill lead lead-unknown" and "&lt;x&gt;" in out[-1]
    from tests.test_control_ui import PAGE
    css = PAGE.read_text(encoding="utf-8")
    for c in ("diagnosing", "repairing", "upgrading", "resuming", "needs_victor", "deferred",
              "resolved"):
        assert f".pill.lead-{c}" in css, c


@node
def test_lead_section_says_what_is_missing_and_closed_cases_have_no_controls(tmp_path):
    out = _page(tmp_path, """
const res = {};
S.tasks.set("proj/T-1", %s); res.off = R.lead(null);
S.tasks.set("proj/T-1", %s); res.none = R.lead(null);
S.tasks.set("proj/T-1", %s); res.closed = R.lead(null); res.closedHead = leadHeadPill(curTask());
S.tasks.delete("proj/T-1"); res.notask = R.lead(null);
console.log(JSON.stringify(res));
""" % (json.dumps(_task(None, False)), json.dumps(_task(None)),
       json.dumps(_task({**lead("RESOLVED"), "active": False, "pending_actions": []}))))
    assert "not reported by this coordinator" in out["off"]
    assert "No recovery case for this task." in out["none"]
    assert "lead-resolved" in out["closed"] and "Pause case" not in out["closed"]
    assert out["closedHead"] == "" and "No coordinator task" in out["notask"]


@node
def test_instruct_form_validates_and_sends_the_case_bound_body(tmp_path):
    lv = {**lead(), "pending_actions": [{"kind": "lead_case_pause", "status": "claimed",
                                         "by": "victor", "ts": 1}]}
    out = _page(tmp_path, """
S.tasks.set("proj/T-1", %s);
const html = R.lead(null);
const errs = [leadFormError("lead_case_instruct", "  "), leadFormError("lead_case_instruct", "x".repeat(2001)),
              leadFormError("lead_case_pause", ""), leadFormError("lead_case_instruct", "do X")];
const body = leadBody("lead_case_instruct", "proj", "T-1", "case-1", "DIAGNOSING", "check <b>");
console.log(JSON.stringify({html, errs, body}));
""" % json.dumps(_task(lv)))
    h = out["html"]
    # a pause already in flight: its button is disabled and the request is shown
    assert 'data-lead-act="lead_case_pause" disabled' in h and "pause claimed by victor" in h
    assert 'data-lead-act="lead_case_instruct" >' in h or 'data-lead-act="lead_case_instruct">' in h
    assert out["errs"][0] and out["errs"][1] and out["errs"][2] == "" and out["errs"][3] == ""
    assert out["body"] == {"kind": "lead_case_instruct", "project_id": "proj", "task_id": "T-1",
                           "case_id": "case-1", "note": "check <b>",
                           "expected": {"case_state": "DIAGNOSING"}}


def test_lead_section_is_registered_wired_and_migrated():
    from tests.test_control_ui import _script
    s = _script()
    assert '"lead"' in s.split("const SECTIONS_V4 =")[1].split("];")[0]
    assert "...SECTIONS_V4" in s.split("const SECTIONS =")[1].split("];")[0]
    assert "  lead(s) {" in s and "  lead(sec) { wireSprint(sec); wireLead(sec); }" in s
    assert 'lead: "Lead recovery"' in s and "const PREF_VERSION = 4;" in s
    assert "leadHeadPill(t)" in s.split("function taskStatePill")[1].split("function ")[0]
    assert 'lead_needs_victor: "approval"' in s


def test_lead_case_resume_is_registered_and_wired():
    from foundry_router import control_center as CC
    assert "lead_case_resume" in CC.ACTION_KINDS and "lead_case_resume" in CC.LEAD_ACTIONS
    page = open("foundry_router/ui/static/control.html", encoding="utf-8").read()
    assert 'data-lead-act="lead_case_resume"' in page and "Resume case" in page
