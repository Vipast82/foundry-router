"""v0.122.0 helper status vocabulary: every finish path names an explicit
phase + reason_code (a terminal `failed` is never ambiguous), Kilo's own
provider retry reads as waiting_provider, ordinary (non-lane) units get a
disposition, a rejection needs a reason, cross-kind duplicate investigations
are coalesced, and an existing DB migrates additively. Fake Kilo/Foundry and a
simulated clock only; no model calls, no live state."""

from __future__ import annotations

import json
import sqlite3
import types

import pytest

from foundry_coordinator import helpers as H
from foundry_coordinator import lanes as L
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.helpers import HelperManager, unit_status
from foundry_coordinator.store import Store
from tests.coordinator.lane_env import setup

ONLY = '[automation]\nhelpers = \"proactive\"\n[lanes]\nenabled=true\ntranscript_poll_seconds=0\n[lanes.failure_triage]\nenabled=true\n'


@pytest.fixture
def env(tmp_path):
    e = setup(tmp_path, lanes=ONLY)
    try:
        yield e
    finally:
        e["fake"].stop()


class StubClient:
    """A Kilo stand-in that reports one fixed session state."""

    def __init__(self, state: SessionState, abort_ok=True):
        self.st, self.abort_ok, self.aborted = state, abort_ok, []

    def state(self, sid, *, directory, since_ts):
        return self.st

    def abort(self, sid, directory=""):
        self.aborted.append(sid)
        return self.abort_ok


def ctx(e):
    c = e["c"]
    P = c.projects["game"]
    return c, P, c.store.task("game", "T2")


def stub_p(P, client):
    return types.SimpleNamespace(id=P.id, profile=P.profile, client=client, foundry=P.foundry,
                                 adapter=P.adapter, ws=P.ws)


def running_unit(e, resp="S45 ordering test", kind="test_design", files=("src/menu.luau",)):
    c, P, t = ctx(e)
    h = c.helpers.settings(P)
    uid = c.helpers._add("game", "T2", t["run_id"], {"responsibility": resp, "kind": kind,
                                                     "files": list(files)}, "report", h)
    assert uid
    now = e["clock"][0]
    c.store.x("UPDATE helper_units SET status='running', session_id=?, persona='p', "
              "backend='meridian', updated_ts=?, started_ts=?, phase='admitted' WHERE unit_id=?",
              (f"ses-{uid}", now, now, uid))
    return uid


def row(c, uid):
    r = c.store.q("SELECT * FROM helper_units WHERE unit_id=?", (uid,))[0]
    r["files"] = json.loads(r["files"] or "[]")
    return r


def poll(e, uid, state, advance=0.0, abort_ok=True):
    c, P, _ = ctx(e)
    e["clock"][0] += advance
    client = StubClient(state, abort_ok)
    c.helpers._poll(stub_p(P, client), row(c, uid), c.helpers.settings(P))
    return row(c, uid), client


@pytest.mark.parametrize("state,advance,status,code,phase", [
    (SessionState(status="busy", error="waiting for the prompt to be admitted",
                  unanswered=True), 181, "failed", "startup_failed", "timed_out"),
    (SessionState(status="busy"), 31 * 60, "failed", "timeout", "timed_out"),
    (SessionState(status="attention", question={"text": "which?"}), 0, "failed", "needs_input",
     "error"),
    (SessionState(status="missing", error="Kilo has no such session"), 0, "failed",
     "session_missing", "error"),
    (SessionState(status="error", error='{"name":"APIError"}'), 0, "failed", "provider_error",
     "error"),
    (SessionState(status="idle", final_text="   "), 0, "failed", "empty_result", "error"),
    (SessionState(status="idle", final_text="a proposal"), 0, "done", None, "completed"),
    (SessionState(status="busy", error="denied", denial={"category": "budget", "code": "x",
                                                         "retry_after": 60}),
     0, "deferred", "denied_deferred", "waiting_provider"),
])
def test_each_poll_branch_names_phase_and_reason(env, state, advance, status, code, phase):
    uid = running_unit(env)
    r, _ = poll(env, uid, state, advance)
    assert (r["status"], r["reason_code"], r["phase"]) == (status, code, phase)
    s = unit_status(r)
    assert (s["phase"], s["reason_code"]) == (phase, code)
    assert s["phase"] != "failed" and s["phase"] in H.PHASES
    assert code is None or code in H.REASON_CODES


def test_running_phases_admitted_then_executing(env):
    uid = running_unit(env)
    r, _ = poll(env, uid, SessionState(status="busy", error="waiting for the prompt to be "
                                       "admitted", unanswered=True))
    assert (r["status"], r["phase"], r["reason_code"]) == ("running", "admitted", None)
    r, _ = poll(env, uid, SessionState(status="busy"))
    assert (r["status"], r["phase"]) == ("running", "executing")
    # compaction is Kilo continuing the work, not a provider retry
    r, _ = poll(env, uid, SessionState(status="busy", error="context compacted; waiting",
                                       compaction={"message_id": "m"}))
    assert r["phase"] == "executing"


def test_kilo_retry_without_denial_is_waiting_provider(env):
    uid = running_unit(env)
    before = row(env["c"], uid)["updated_ts"]
    r, client = poll(env, uid, SessionState(status="busy", error="Rate limited (429); retrying "
                                            "in 30s"))
    assert (r["status"], r["phase"], r["reason_code"]) == ("running", "waiting_provider",
                                                           "provider_retry")
    assert not client.aborted                     # not stopped, not deferred: Kilo retries
    assert r["updated_ts"] == before              # phase tracking never resets the timeout
    assert unit_status(r)["label"] == "waiting on provider: provider_retry"
    r, _ = poll(env, uid, SessionState(status="busy"))
    assert (r["phase"], r["reason_code"]) == ("executing", None)
    # the retry still runs on the helper timeout clock
    r, _ = poll(env, uid, SessionState(status="busy", error="Rate limited"), advance=31 * 60)
    assert (r["status"], r["reason_code"], r["phase"]) == ("failed", "timeout", "timed_out")


def test_abort_unconfirmed_keeps_unit_running_with_reason(env):
    uid = running_unit(env)
    r, _ = poll(env, uid, SessionState(status="attention"), abort_ok=False)
    assert (r["status"], r["reason_code"]) == ("running", "abort_unconfirmed")
    assert unit_status(r)["phase"] in ("admitted", "executing")
    r, _ = poll(env, uid, SessionState(status="attention"))
    assert (r["status"], r["reason_code"], r["phase"]) == ("failed", "needs_input", "error")
    # the mid-run refusal path too
    uid2 = running_unit(env, resp="other part", kind="exploration", files=())
    r, _ = poll(env, uid2, SessionState(status="busy", denial={"category": "budget"}),
                abort_ok=False)
    assert (r["status"], r["reason_code"]) == ("running", "abort_unconfirmed")


def test_unreadable_poll_then_timeout(env):
    c, P, _ = ctx(env)
    uid = running_unit(env)
    h = c.helpers.settings(P)
    p = stub_p(P, StubClient(SessionState(status="busy")))
    c.helpers._poll_failed(p, row(c, uid), h, OSError("connection refused"))
    r = row(c, uid)
    assert (r["status"], r["reason_code"]) == ("running", "poll_unreadable")
    env["clock"][0] += 31 * 60
    c.helpers._poll_failed(p, row(c, uid), h, OSError("connection refused"))
    r = row(c, uid)
    assert (r["status"], r["reason_code"], r["phase"]) == ("failed", "poll_unreadable",
                                                           "timed_out")


def test_stop_paths_handoff_cancel_skip_and_lane_end(env):
    c, P, t = ctx(env)
    a = running_unit(env, resp="part a", files=())
    c.helpers.abort_running(stub_p(P, StubClient(SessionState(status="busy"))), "T2",
                            "timed_out", "the primary waited 15 min")
    r = row(c, a)
    assert (r["status"], r["reason_code"], r["phase"]) == ("timed_out", "handoff_timeout",
                                                           "timed_out")
    b = running_unit(env, resp="part b", files=())
    c.helpers.abort_running(P, "T2", "cancelled", "task paused: maintenance")
    r = row(c, b)
    assert (r["status"], r["reason_code"], r["phase"]) == ("cancelled", "task_paused",
                                                           "cancelled")
    d = running_unit(env, resp="part d", files=())
    c.helpers.abort_running(P, "T2", "cancelled", "the primary finished first")
    assert row(c, d)["reason_code"] == "primary_finished"
    h = c.helpers.settings(P)
    w = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "waiting part"}, "report", h)
    assert row(c, w)["phase"] == "queued"
    c.helpers.skip_waiting(P, "T2", "not admitted before the primary was ready")
    r = row(c, w)
    assert (r["status"], r["reason_code"], r["phase"]) == ("skipped", "primary_ready",
                                                           "cancelled")
    lu, _ = c.helpers.lanes.add(P, t, "failure_triage", "sigA", {}, L.settings(P))
    c.store.x("UPDATE helper_units SET status='running', session_id='ses-lane' WHERE unit_id=?",
              (lu,))
    c.helpers.abort_lane_running(P, "T2", "the session ended: idle")
    r = row(c, lu)
    assert (r["status"], r["reason_code"], r["phase"]) == ("cancelled", "session_ended",
                                                           "cancelled")


def test_dispatch_deferral_names_waiting_provider(env):
    c, P, t = ctx(env)
    h = c.helpers.settings(P)
    uid = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "quota part"}, "report", h)
    env["ff"].admission.update({"Foundry-Kilo-Helper-Claude": {
        "admit": False, "state": "deferred", "reason": "5h window over", "next_eligible":
        env["clock"][0] + 600, "routes": [{"source": "fixture"}]}})
    c.helpers._dispatch(P, t, P.adapter.describe("T2"), row(c, uid), h)
    r = row(c, uid)
    assert (r["status"], r["phase"], r["reason_code"]) == ("deferred", "waiting_provider",
                                                           "admission_denied")


def test_stale_phase_from_status_only_writer_is_rederived(env):
    """visual/advisory code writes `status` only; the stored phase must never
    contradict it (column re-derived by sync_status, unit_status never lies)."""
    c, *_ = ctx(env)
    uid = running_unit(env)
    c.store.x("UPDATE helper_units SET status='failed', reason='vision call timed out after 30 "
              "min' WHERE unit_id=?", (uid,))
    s = unit_status(row(c, uid))
    assert (s["phase"], s["reason_code"]) == ("timed_out", "timeout")
    assert c.helpers.sync_status("game", "T2") >= 1
    r = row(c, uid)
    assert (r["phase"], r["reason_code"]) == ("timed_out", "timeout")
    c.store.x("UPDATE helper_units SET status='deferred', reason='Foundry refused at call time "
              "(budget): over', next_eligible=1 WHERE unit_id=?", (uid,))
    assert unit_status(row(c, uid))["phase"] == "waiting_provider"


# ---------------------------------------------------------------- dispositions

def delivered_unit(e, resp="S45 ordering test"):
    c, P, t = ctx(e)
    uid = running_unit(e, resp=resp)
    poll(e, uid, SessionState(status="idle", final_text="diff ..."))
    c.helpers.mark_delivered([row(c, uid)], t["session_id"])
    return uid


def feedback(e, uid, status, note=None):
    c, P, t = ctx(e)
    f = {"unit": uid, "status": status, **({"note": note} if note is not None else {})}
    return c.helpers.record_feedback(P, c.store.task("game", "T2"), {"helper_feedback": [f]})


def test_ordinary_unit_disposition(env):
    c, *_ = ctx(env)
    uid = delivered_unit(env)
    r = row(c, uid)
    assert r["lane"] is None and r["feedback"] == "delivered"
    assert unit_status(r)["disposition"] == "pending"
    assert [u["unit_id"] for u in c.helpers.disposition_pending("game", "T2")] == [uid]
    assert feedback(env, uid, "integrated", "applied the test") == [uid]
    r = row(c, uid)
    s = unit_status(r)
    assert (r["feedback"], s["disposition"], s["phase"]) == ("integrated", "integrated",
                                                             "completed")
    assert json.loads(r["feedback_detail"])["claimed_by"] == "primary"
    assert not c.helpers.disposition_pending("game", "T2")
    assert s["label"] == "completed (integrated)"
    other = delivered_unit(env, resp="another part")
    assert feedback(env, other, "superseded", "a later proposal replaced it") == [other]
    assert unit_status(row(c, other))["disposition"] == "superseded"


def test_rejected_without_reason_is_refused(env):
    c, *_ = ctx(env)
    uid = delivered_unit(env)
    assert feedback(env, uid, "rejected") == []
    assert feedback(env, uid, "rejected", "   ") == []
    r = row(c, uid)
    assert r["feedback"] == "delivered" and unit_status(r)["disposition"] == "pending"
    assert "without a reason" in json.loads(r["feedback_detail"])["pending_note"]
    ev = c.store.q("SELECT * FROM events WHERE task_id='T2' AND kind='helper_feedback_refused'")
    assert len(ev) == 2
    assert feedback(env, uid, "rejected", "does not fit the approved scope") == [uid]
    r = row(c, uid)
    det = json.loads(r["feedback_detail"])
    assert r["feedback"] == "rejected" and "scope" in det["note"] and "pending_note" not in det
    assert unit_status(r)["disposition"] == "rejected"


# ---------------------------------------------------------------- duplicates

def events(c, kind):
    return [json.loads(e["detail"]) for e in
            c.store.q("SELECT * FROM events WHERE task_id='T2' AND kind=?", (kind,))]


def test_cross_kind_duplicate_with_a_lane_is_coalesced(env):
    c, P, t = ctx(env)
    lane_uid, how = c.helpers.lanes.add(P, t, "failure_triage", "S45 ordering failure", {},
                                        L.settings(P), files=["src/menu.luau"])
    assert how == "created"
    h = c.helpers.settings(P)
    req = {"kind": "debug_advice", "responsibility": "S45 ordering FAILURE",
           "files": ["./src/menu.luau"]}
    assert c.helpers._add("game", "T2", t["run_id"], req, "report", h) is None
    assert not [u for u in c.helpers.units("game", "T2") if not u.get("lane")]
    [d] = events(c, "helper_duplicate")
    assert d["unit"] == lane_uid and d["existing_lane"] == "failure_triage"
    assert d["requested_kind"] == "debug_advice" and "same investigation" in d["reason"]
    assert row(c, lane_uid)["coalesced"] == 1
    # the same request collected again (task source every tick) is not re-counted
    assert c.helpers._add("game", "T2", t["run_id"], req, "report", h) is None
    assert row(c, lane_uid)["coalesced"] == 1 and len(events(c, "helper_duplicate")) == 1


def test_cross_kind_duplicate_between_ordinary_units(env):
    c, P, t = ctx(env)
    h = c.helpers.settings(P)
    first = c.helpers._add("game", "T2", t["run_id"], {
        "kind": "test_design", "responsibility": "S45 ordering test",
        "files": ["src/tests/menu.spec.luau", "src/menu.luau"]}, "report", h)
    assert first and row(c, first)["investigation_key"]
    assert c.helpers._add("game", "T2", t["run_id"], {
        "kind": "debug_advice", "responsibility": "s45 ordering-test",
        "files": ["src/menu.luau", "src\\tests\\menu.spec.luau"]}, "report", h) is None
    assert events(c, "helper_duplicate")[0]["unit"] == first
    # different files: a different investigation, dispatched normally
    assert c.helpers._add("game", "T2", t["run_id"], {
        "kind": "debug_advice", "responsibility": "s45 ordering-test",
        "files": ["src/menu.luau"]}, "report", h)
    # a failed unit produced nothing: the same investigation may be asked again
    c.store.x("UPDATE helper_units SET status='failed', reason_code='timeout' WHERE unit_id=?",
              (first,))
    assert c.helpers._add("game", "T2", t["run_id"], {
        "kind": "exploration", "responsibility": "S45 ordering test!",
        "files": ["src/menu.luau", "src/tests/menu.spec.luau"]}, "report", h)
    # another run's unit is not this run's duplicate
    assert c.helpers.duplicate_of("game", "T2", "other-run", "S45 ordering test",
                                  ["src/menu.luau"]) is None


def test_same_responsibility_per_kind_rule_is_kept(env):
    c, P, t = ctx(env)
    h = c.helpers.settings(P)
    uid = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "part x"}, "report", h)
    assert c.helpers._add("game", "T2", t["run_id"], {"responsibility": "part x"}, "report",
                          h) is None
    assert not events(c, "helper_duplicate")     # the v0.117 rule answers first
    c.store.x("UPDATE helper_units SET status='skipped', phase='cancelled', "
              "reason_code='primary_ready' WHERE unit_id=?", (uid,))
    assert c.helpers._add("game", "T2", t["run_id"], {"responsibility": "part x"}, "report",
                          h) == uid
    r = row(c, uid)
    assert (r["status"], r["phase"], r["reason_code"]) == ("pending", "queued", None)


# ---------------------------------------------------------------- migration

OLD_SCHEMA = """
CREATE TABLE helper_units (
  unit_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, run_id TEXT, responsibility TEXT,
  kind TEXT, value TEXT, prompt TEXT, files TEXT, source TEXT, status TEXT, persona TEXT,
  backend TEXT, session_id TEXT, parent_session TEXT, attempts INTEGER DEFAULT 0, reason TEXT,
  next_check REAL, next_eligible REAL, result TEXT, delivered_session TEXT, created_ts REAL,
  updated_ts REAL, UNIQUE (project_id, task_id, responsibility));
"""


def test_migration_on_an_existing_db(tmp_path):
    db = tmp_path / "c.sqlite"
    Store(str(db)).conn.close()
    con = sqlite3.connect(str(db))
    con.executescript(OLD_SCHEMA)
    rows = [("u1", "failed", "the helper asked for input; helpers can't wait"),
            ("u2", "failed", "no reply to the prompt after 180 s while Kilo reports the session "
                             "idle (startup failed: model, provider or auth)"),
            ("u3", "timed_out", "handoff wait over"),
            ("u4", "delivered", "proposal returned"),
            ("u5", "deferred", "meridian: DEFERRED — 5h window"),
            ("u6", "running", "admitted on meridian"),
            ("u7", "failed", "no proposal text")]
    for uid, st, why in rows:
        con.execute("INSERT INTO helper_units (unit_id, project_id, task_id, run_id, "
                    "responsibility, kind, status, reason, files, updated_ts) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)", (uid, "game", "T2", "r1", f"resp {uid}",
                                              "code_proposal", st, why, "[]", 5.0))
    con.commit()
    con.close()
    store = Store(str(db))
    coord = types.SimpleNamespace(store=store, state_dir=str(tmp_path / "state"))
    mgr = HelperManager(coord, clock=lambda: 100.0)
    cols = {r["name"] for r in store.q("PRAGMA table_info(helper_units)")}
    assert {"phase", "reason_code", "investigation_key", "lane", "feedback",
            "requests"} <= cols
    got = {r["unit_id"]: (r["status"], r["phase"], r["reason_code"], r["updated_ts"])
           for r in store.q("SELECT * FROM helper_units")}
    assert got["u1"] == ("failed", "error", "needs_input", 5.0)
    assert got["u2"] == ("failed", "timed_out", "startup_failed", 5.0)
    assert got["u3"] == ("timed_out", "timed_out", "handoff_timeout", 5.0)
    assert got["u4"][:3] == ("delivered", "completed", None)
    assert got["u5"][:3] == ("deferred", "waiting_provider", "admission_denied")
    assert got["u6"][:3] == ("running", "executing", None)
    assert got["u7"][:3] == ("failed", "error", "empty_result")
    # an old delivered ordinary unit has no feedback: its disposition is pending
    assert mgr.unit_status(store.q("SELECT * FROM helper_units WHERE unit_id='u4'")[0])[
        "disposition"] == "pending"
    # idempotent: a second start changes nothing
    HelperManager(coord, clock=lambda: 200.0)
    again = {r["unit_id"]: (r["status"], r["phase"], r["reason_code"], r["updated_ts"])
             for r in store.q("SELECT * FROM helper_units")}
    assert again == got


# ---------------------------------------------------------------- duplicates (v0.122b review)

REQ = {"kind": "debug_advice", "responsibility": "S45 ordering FAILURE", "files": ["src/menu.luau"]}


def _lane(c, P, t, **cols):
    uid, how = c.helpers.lanes.add(P, t, "failure_triage", "S45 ordering failure", {},
                                   L.settings(P), files=["src/menu.luau"])
    assert how == "created"
    if cols:
        c.store.x("UPDATE helper_units SET " + ", ".join(f"{k}=?" for k in cols) +
                  " WHERE unit_id=?", (*cols.values(), uid))
    return uid


def _usable_env(uid):
    return json.dumps({"structured": True, "assignment": uid, "checks": {}})


def test_no_coalescing_onto_a_rejected_unit(env):
    c, P, t = ctx(env)
    uid = _lane(c, P, t)
    c.store.x("UPDATE helper_units SET feedback='rejected' WHERE unit_id=?", (uid,))
    h = c.helpers.settings(P)
    assert c.helpers._add("game", "T2", t["run_id"], dict(REQ), "report", h)
    assert not events(c, "helper_duplicate") and row(c, uid)["coalesced"] in (0, None)


def test_answered_lane_result_needs_a_usable_envelope_and_the_same_snapshot(env):
    c, P, t = ctx(env)
    uid = _lane(c, P, t, status="done", snapshot_id="snap-1")
    c.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?", (_usable_env(uid), uid))
    dup = c.helpers.duplicate_of
    files = ["src/menu.luau"]
    resp = "S45 ordering failure"
    # an ordinary request can't say which snapshot it is about: never coalesced onto a done unit
    assert dup("game", "T2", t["run_id"], resp, files) is None
    assert dup("game", "T2", t["run_id"], resp, files, snapshot_id="snap-0") is None   # older
    assert dup("game", "T2", t["run_id"], resp, files, snapshot_id="snap-1")["unit_id"] == uid
    # a result proactive's usable() would refuse: unstructured, someone else's, base mismatch
    for env_ in ({"structured": False, "assignment": uid},
                 {"structured": True, "assignment": "hu-other"},
                 {"structured": True, "assignment": uid, "checks": {"base_mismatch": "x"}}):
        c.store.x("UPDATE helper_units SET envelope=? WHERE unit_id=?", (json.dumps(env_), uid))
        assert dup("game", "T2", t["run_id"], resp, files, snapshot_id="snap-1") is None, env_
    # expired: never
    c.store.x("UPDATE helper_units SET envelope=?, feedback='expired' WHERE unit_id=?",
              (_usable_env(uid), uid))
    assert dup("game", "T2", t["run_id"], resp, files, snapshot_id="snap-1") is None
    # and the real request path dispatches its own unit instead
    h = c.helpers.settings(P)
    c.store.x("UPDATE helper_units SET feedback=NULL WHERE unit_id=?", (uid,))
    assert c.helpers._add("game", "T2", t["run_id"], dict(REQ), "report", h)
    assert not events(c, "helper_duplicate")


def test_answered_ordinary_result_needs_the_same_head(env):
    c, P, t = ctx(env)
    h = c.helpers.settings(P)
    first = c.helpers._add("game", "T2", t["run_id"], {
        "kind": "test_design", "responsibility": "S45 ordering test",
        "files": ["src/menu.luau"]}, "report", h)
    c.store.x("UPDATE helper_units SET status='done' WHERE unit_id=?", (first,))
    args = ("game", "T2", t["run_id"], "s45 ordering-test", ["src/menu.luau"])
    # no recorded head: not determinable, not reused
    assert c.helpers.duplicate_of(*args, head="abc") is None
    c.store.x("UPDATE helper_units SET feedback_detail=? WHERE unit_id=?",
              (json.dumps({"head": "abc"}), first))
    assert c.helpers.duplicate_of(*args, head="abc")["unit_id"] == first
    assert c.helpers.duplicate_of(*args, head=lambda: "abc")["unit_id"] == first
    assert c.helpers.duplicate_of(*args, head="def") is None            # the code moved on
    assert c.helpers.duplicate_of(*args) is None                        # requester head unknown
    # a live (pending/running) unit is still coalesced without any head
    c.store.x("UPDATE helper_units SET status='running', feedback_detail=NULL WHERE unit_id=?",
              (first,))
    assert c.helpers.duplicate_of(*args)["unit_id"] == first


def test_duplicate_once_marker_is_an_ops_key_not_an_events_scan(env):
    c, P, t = ctx(env)
    lane_uid = _lane(c, P, t)
    h = c.helpers.settings(P)
    assert c.helpers._add("game", "T2", t["run_id"], dict(REQ), "report", h) is None
    ops = c.store.q("SELECT * FROM ops WHERE kind='helper_duplicate'")
    assert len(ops) == 1 and ops[0]["status"] == "done"
    # the events row may be pruned; the marker still holds
    c.store.x("DELETE FROM events WHERE kind='helper_duplicate'")
    assert c.helpers._add("game", "T2", t["run_id"], dict(REQ), "report", h) is None
    assert row(c, lane_uid)["coalesced"] == 1
