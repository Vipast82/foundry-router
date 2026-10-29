"""Alerts panel: Dismiss and auto-clear (Victor, 2026-10-28: acknowledged alerts
stayed in the list and kept the bell lit forever: a pause_forgotten for a task
already running again, a coordinator_error HTTP 401 from 10/21, old COMPLETE
and "approved queue has nothing left to run" info alerts).

  dismiss     every alert can be dismissed (UI + API, same auth as acknowledge):
              it leaves the open list and the bell, stays stored (filter
              "dismissed") with who dismissed it
  auto-clear  the watchdog sweep resolves an alert whose condition is gone
              (status resolved, "auto-cleared: ..." reason, never deleted)
"""


import pytest

from foundry_router.control_center import CONTRACT, ControlCenter
from foundry_router.db import Database
from tests.test_control_center import (COORD, OP_B_KEY, cc, ingest as http_ingest,  # noqa: F401
                                       login)

H = 3600


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    clock = Clock()
    db = Database(tmp_path / "a.sqlite")
    return ControlCenter(db, clock=clock), clock


def ingest(c, *obs):
    return c.ingest({"contract": CONTRACT, "source": {"id": "bridge:t", "kind": "bridge"},
                     "observations": list(obs)}, {})


def task(state, tid="HK-CLOSE1", pid="au", **kw):
    return {"type": "task", "project_id": pid, "task_id": tid, "state": state, **kw}


def by_kind(c, kind, status="open"):
    return [a for a in c.alerts([], status) if a["kind"] == kind]


def bell(c):
    """What the bell counts (control.html bellCount): open alerts not acknowledged."""
    return [a for a in c.alerts([], "open") if a["status"] == "new"]


# ------------------------------------------------------------------ auto-clear

def test_pause_forgotten_cleared_by_resume(env):
    c, clock = env
    ingest(c, task("PAUSED"))
    ingest(c, {"type": "coord_event", "kind": "operator_notice", "event_id": "ev-pf1",
               "project_id": "au", "task_id": "HK-CLOSE1",
               "detail": {"alert_class": "pause_forgotten", "text": "PAUSED for 16 h"}})
    [a] = by_kind(c, "pause_forgotten")
    assert a["title"] == "HK-CLOSE1: a pause or hold looks forgotten"
    c.acknowledge(a["alert_id"], "victor", [])
    c.watchdog()
    assert len(by_kind(c, "pause_forgotten")) == 1          # still paused: stays
    clock.t += 60
    ingest(c, task("IMPLEMENTING"))                         # resumed, running
    c.watchdog()
    assert by_kind(c, "pause_forgotten") == []
    [r] = by_kind(c, "pause_forgotten", "resolved")
    assert r["resolution"].startswith("auto-cleared:") and "no longer paused" in r["resolution"]
    assert r["alert_id"] == a["alert_id"]                   # stored, not deleted


def test_old_coordinator_error_resolved(env):
    c, clock = env
    ingest(c, task("IMPLEMENTING", tid="UI-02b"))
    ingest(c, {"type": "coord_event", "kind": "error", "event_id": "ev-401", "project_id": "au",
               "task_id": "UI-02b", "detail": {"error": "GET /session/x: HTTP 401"}})
    [a] = by_kind(c, "coordinator_error")
    assert a["title"] == "Coordinator error on UI-02b"
    clock.t += 23 * H
    c.watchdog()
    assert len(by_kind(c, "coordinator_error")) == 1        # recent: stays
    # it recurs: the quiet window restarts from the newest error on that task
    ingest(c, {"type": "coord_event", "kind": "error", "event_id": "ev-401b",
               "project_id": "au", "task_id": "UI-02b", "detail": {"error": "HTTP 401"}})
    clock.t += 2 * H
    c.watchdog()
    assert len(by_kind(c, "coordinator_error")) == 2
    clock.t += 23 * H
    c.watchdog()
    assert by_kind(c, "coordinator_error") == []
    res = by_kind(c, "coordinator_error", "resolved")
    assert len(res) == 2 and all("no new coordinator error on UI-02b for 24 h" in r["resolution"]
                                 for r in res)


def test_coordinator_error_resolved_when_task_complete(env):
    c, clock = env
    ingest(c, task("IMPLEMENTING", tid="UI-03"))
    ingest(c, {"type": "coord_event", "kind": "error", "event_id": "ev-e3", "project_id": "au",
               "task_id": "UI-03", "detail": {"error": "boom"}})
    ingest(c, task("COMPLETE", tid="UI-03"))
    c.watchdog()
    assert by_kind(c, "coordinator_error") == []
    [r] = by_kind(c, "coordinator_error", "resolved")
    assert "UI-03 is COMPLETE" in r["resolution"]


def test_completion_and_queue_exhausted_clear_after_ack_or_24h(env):
    c, clock = env
    ingest(c, task("VERIFYING", tid="T-1"))
    ingest(c, task("COMPLETE", tid="T-1", queue_exhausted=1))
    [done] = by_kind(c, "completion")
    [qe] = by_kind(c, "queue_exhausted")
    assert done["title"] == "T-1 COMPLETE (coordinator verdict)"
    # a completion record is not cleared by its own task being COMPLETE
    c.watchdog()
    assert len(by_kind(c, "completion")) == 1
    # acknowledged: it has nothing left to wait on
    c.acknowledge(qe["alert_id"], "victor", [])
    assert c.alert(qe["alert_id"])["status"] == "resolved"
    # the same fact re-reported on the next observation does not ring again
    ingest(c, task("COMPLETE", tid="T-1", queue_exhausted=1, published="x"))
    assert by_kind(c, "queue_exhausted") == []
    # 24 h later the unread completion ages out
    clock.t += 24 * H
    c.watchdog()
    assert by_kind(c, "completion") == []
    assert "older than 24 h" in c.alert(done["alert_id"])["resolution"]


def test_any_task_alert_clears_when_task_complete_or_cancelled(env):
    c, clock = env
    ingest(c, task("FAILED", tid="T-5", attempt=1, reason="tests failed"))
    [f] = by_kind(c, "failure")
    ingest(c, task("CANCELLED", tid="T-5"))
    c.watchdog()
    assert c.alert(f["alert_id"])["status"] == "resolved"


def test_autoclear_windows_are_typed_settings(env):
    c, clock = env
    st = c.settings()
    assert st["alert_autoclear"] is True and st["alert_error_quiet_hours"] == 24 \
        and st["alert_info_hours"] == 24
    with pytest.raises(ValueError):
        c.update_settings({"alert_info_hours": 0}, "victor")
    c.update_settings({"alert_info_hours": 2}, "victor")
    ingest(c, task("VERIFYING", tid="T-1"))
    ingest(c, task("COMPLETE", tid="T-1"))
    clock.t += 2 * H
    c.watchdog()
    assert by_kind(c, "completion") == []
    # switched off: nothing clears itself
    c.update_settings({"alert_autoclear": False}, "victor")
    ingest(c, task("PAUSED", tid="T-2"))
    ingest(c, {"type": "coord_event", "kind": "x", "event_id": "ev-p2", "project_id": "au",
               "task_id": "T-2", "detail": {"alert_class": "pause_forgotten"}})
    ingest(c, task("IMPLEMENTING", tid="T-2"))
    c.watchdog()
    assert len(by_kind(c, "pause_forgotten")) == 1


# ------------------------------------------------------------------ dismiss

def test_dismiss_removes_from_bell_and_open_list_but_keeps_history(env):
    c, clock = env
    a = c.raise_alert("blocked:au:T-7:abc", "blocker", "high", "T-7 is BLOCKED", "x",
                      {"project_id": "au", "task_id": "T-7"})
    b = c.raise_alert("source_stale:x", "source_stale", "high", "silent", "y", {})
    c.acknowledge(b["alert_id"], "victor", [])
    assert [x["alert_id"] for x in bell(c)] == [a["alert_id"]]   # acknowledged: no ring
    assert len(c.alerts([], "open")) == 2                         # ...but still listed
    d = c.dismiss(a["alert_id"], "victor", [])
    c.dismiss(b["alert_id"], "victor", [])
    assert d["status"] == "dismissed" and d["resolution"] == "dismissed by victor"
    assert bell(c) == [] and c.alerts([], "open") == []
    kept = c.alerts([], "dismissed", include_log=True)
    assert {x["alert_id"] for x in kept} == {a["alert_id"], b["alert_id"]}
    assert any(e["event"] == "dismissed" and e["actor"] == "victor"
               for x in kept for e in x["log"])
    # the same occurrence raised again stays dismissed (no new ring)
    assert c.raise_alert("blocked:au:T-7:abc", "blocker", "high", "T-7 is BLOCKED", "x",
                         {"project_id": "au", "task_id": "T-7"})["status"] == "dismissed"
    # once its condition clears, a later occurrence alerts again
    c._resolve("source_stale:x", "heartbeat back")
    assert c.alert(b["alert_id"])["status"] == "resolved"
    clock.t += 7 * H                       # past the acknowledged-flap quiet window
    assert c.raise_alert("source_stale:x", "source_stale", "high", "silent", "y",
                         {})["status"] == "new"


def test_dismiss_all_info(env):
    c, clock = env
    c.raise_alert("complete:au:T-1", "completion", "info", "T-1 COMPLETE", "", {"project_id": "au"})
    c.raise_alert("queue_exhausted:au:3", "queue_exhausted", "info", "queue empty", "",
                  {"project_id": "au"})
    keep = c.raise_alert("blocked:au:T-2:z", "blocker", "high", "T-2 BLOCKED", "", {})
    r = c.dismiss_info("victor", [])
    assert r["dismissed"] == 2
    assert [a["alert_id"] for a in c.alerts([], "open")] == [keep["alert_id"]]
    assert len(c.alerts([], "dismissed")) == 2


# ------------------------------------------------------------------ API

def test_dismiss_api_auth_same_as_acknowledge(client, cc):
    http_ingest(client, {"type": "coord_event", "kind": "error", "event_id": "ev-api",
                         "project_id": "proj", "task_id": "T-1", "detail": {"error": "401"}})
    [a] = [x for x in cc.alerts([], "open") if x["kind"] == "coordinator_error"]
    aid = a["alert_id"]
    for verb in ("ack", "dismiss"):
        # no operator session: refused like acknowledge
        assert client.post(f"/control/api/alerts/{aid}/{verb}").status_code == 401
    # an inference/coordinator key is not an operator session
    assert client.post(f"/control/api/alerts/{aid}/dismiss", headers=COORD).status_code == 401
    h = login(client)
    # signed in but no CSRF token: refused like acknowledge
    for verb in ("ack", "dismiss"):
        assert client.post(f"/control/api/alerts/{aid}/{verb}").status_code == 403
    assert client.post("/control/api/alerts/dismiss-info").status_code == 403
    # an operator restricted to another project can't see or dismiss it
    hb = login(client, OP_B_KEY)
    assert client.post(f"/control/api/alerts/{aid}/dismiss", headers=hb).status_code == 404
    h = login(client)
    r = client.post(f"/control/api/alerts/{aid}/dismiss", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    assert client.get("/control/api/alerts").json()["alerts"] == []
    got = client.get("/control/api/alerts?status=dismissed").json()["alerts"]
    assert [x["alert_id"] for x in got] == [aid]
    r = client.post("/control/api/alerts/dismiss-info", headers=h)
    assert r.status_code == 200 and r.json()["dismissed"] == 0


def test_control_html_has_dismiss_controls():
    import pathlib
    s = (pathlib.Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static"
         / "control.html").read_text(encoding="utf-8")
    assert 'data-dismiss="' in s and "/dismiss`" in s
    assert 'id="inbox-dismiss-info"' in s and '"alerts/dismiss-info"' in s
    assert '<option value="dismissed">dismissed</option>' in s
    for k in ("alert_autoclear", "alert_error_quiet_hours", "alert_info_hours"):
        assert f"{k}:" in s                                  # labelled in Capture settings


def test_watchdog_runs_autoclear_on_its_own_timer(env):
    c, clock = env
    c.raise_alert("coord_error:old", "coordinator_error", "high", "Coordinator error on X",
                  "HTTP 401", {"project_id": "au", "task_id": "X"})
    clock.t += 25 * H
    assert c.watchdog() == []                               # no stale-source alerts
    assert c.alerts([], "open") == []


def test_a_resent_coordinator_error_never_reopens_its_cleared_alert(env):
    """2026-10-29: a GF-ENT1 'Coordinator error' from the day before kept popping back up: the
    coordinator re-sends recent events on every restart and the cleared alert reopened."""
    c, clock = env
    err = {"type": "coord_event", "kind": "error", "event_id": "ev-ent1", "project_id": "au",
           "task_id": "GF-ENT1", "detail": {"error": "VERIFYING -> BLOCKED needs evidence"}}
    ingest(c, task("VERIFYING", tid="GF-ENT1"))
    ingest(c, err)
    assert len(by_kind(c, "coordinator_error")) == 1
    ingest(c, task("COMPLETE", tid="GF-ENT1"))
    c.watchdog()
    assert by_kind(c, "coordinator_error") == []
    ingest(c, err)                                         # the same event, re-sent
    c.watchdog()
    assert by_kind(c, "coordinator_error") == []
    ingest(c, dict(err, event_id="ev-ent1-b"))              # a NEW error still alerts
    assert len(by_kind(c, "coordinator_error")) == 1
