"""Self-driving parts 4-5, router side (foundry_router/alerts.py): coordinator-down
alert and recovery, the push policy per class, the webhook (ntfy / JSON, retry,
no secrets in logs or errors), the daily digest and its API / settings."""

import datetime as dt
import json
import logging
import time

import pytest

from foundry_router.alerts import AlertService, decision_class, is_image_ask
from foundry_router.config import AlertsConfig, ClientConfig
from foundry_router.control_center import CONTRACT, ControlCenter
from foundry_router.db import Database

OP_KEY = "op-key-alerts-123456"
COORD_KEY = "coord-key-alerts-12345"
COORD = {"Authorization": f"Bearer {COORD_KEY}"}
HOOK = "https://ntfy.example.net/victor-secret-topic-9f3a"


def C(**kw):
    """Settings for the non-digest tests: the fake clock is past the digest hour."""
    return AlertsConfig(**{"digest_enabled": False, **kw})


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Post:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        if isinstance(self.status, Exception):
            raise self.status
        if isinstance(self.status, list):
            s = self.status.pop(0)
            if isinstance(s, Exception):
                raise s
            return s
        return self.status


@pytest.fixture()
def env(tmp_path):
    clock = Clock()
    db = Database(tmp_path / "a.sqlite")
    cc = ControlCenter(db, clock=clock)
    holder = {"cfg": C(webhook_url=HOOK)}
    post = Post()
    al = AlertService(cc, lambda: holder["cfg"], clock=clock, post=post,
                      localtime=lambda ts: dt.datetime(2026, 10, 28, 0, 0)
                      + dt.timedelta(seconds=ts - 1_800_000_000.0 + 9 * 3600))
    return {"db": db, "cc": cc, "al": al, "clock": clock, "post": post, "holder": holder}


def beat(cc, kind="coordinator", sid="coord:ws"):
    cc.heartbeat({"source": {"id": sid, "kind": kind}, "contract": CONTRACT}, {})


def ingest(cc, *obs):
    return cc.ingest({"contract": CONTRACT, "source": {"id": "bridge:t", "kind": "bridge"},
                      "observations": list(obs)}, {})


# ------------------------------------------------------------------ coordinator down

def test_coordinator_down_raises_one_alert_and_one_recovery(env):
    cc, al, clock, post = env["cc"], env["al"], env["clock"], env["post"]
    assert al.check_coordinator(al.cfg()) == "unknown"          # never seen: nothing
    beat(cc)
    al.tick()                                                   # sets the cursor
    clock.t += 9 * 60
    assert al.tick()["coordinator"] == "up"
    clock.t += 2 * 60                                           # 11 min silent
    out = al.tick()
    assert out["coordinator"] == "went_down" and out["queued"] == 1 and out["delivered"] == 1
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["headers"]["Title"].startswith("Coordinator down")
    assert call["headers"]["Priority"] == "urgent"
    clock.t += 15 * 60
    out = al.tick()
    assert out["coordinator"] == "down" and len(post.calls) == 1   # once, not every tick
    open_ = [a for a in cc.alerts([], "open") if a["kind"] == "stack_down"]
    assert len(open_) == 1 and open_[0]["severity"] == "critical"
    beat(cc)
    out = al.tick()
    assert out["coordinator"] == "recovered" and len(post.calls) == 2
    assert post.calls[1]["headers"]["Title"].startswith("Coordinator back")
    assert not [a for a in cc.alerts([], "open") if a["kind"] == "stack_down"]
    assert al.tick()["coordinator"] == "up" and len(post.calls) == 2


def test_no_false_down_alert_right_after_a_router_restart(env):
    cc, clock = env["cc"], env["clock"]
    beat(cc)
    clock.t += 3600                                             # router was off for an hour
    fresh = AlertService(cc, lambda: C(webhook_url=HOOK), clock=clock,
                         post=env["post"])
    assert fresh.check_coordinator(fresh.cfg()) == "up"         # grace window
    clock.t += 11 * 60
    assert fresh.check_coordinator(fresh.cfg()) == "went_down"


def test_down_alert_shows_on_page_even_without_a_webhook(env):
    env["holder"]["cfg"] = C()
    cc, al, clock = env["cc"], env["al"], env["clock"]
    beat(cc)
    al.tick()
    clock.t += 11 * 60
    assert al.tick()["coordinator"] == "went_down"
    assert [a for a in cc.alerts([], "open") if a["kind"] == "stack_down"]
    assert env["post"].calls == []


def test_down_class_switched_off_is_not_pushed(env):
    env["holder"]["cfg"] = C(webhook_url=HOOK, classes={"stack_down": False})
    cc, al, clock = env["cc"], env["al"], env["clock"]
    beat(cc)
    al.tick()
    clock.t += 11 * 60
    al.tick()
    assert env["post"].calls == []


# ------------------------------------------------------------------ policy

def _task(state="IMPLEMENTING", **kw):
    return {"type": "task", "project_id": "au", "task_id": "T-1", "state": state,
            "session_id": "s1", **kw}


def _dec(did, kind="decision", **kw):
    return {"decision_id": did, "kind": kind, "status": "open", "question": "Which?",
            "options": ["a", "b"], **kw}


def test_policy_pushes_product_user_action_and_2d_but_not_automation(env):
    cc, al, post = env["cc"], env["al"], env["post"]
    al.tick()
    ingest(cc, _task(decisions=[
        _dec("d-prod", "scope_request", **{"class": "product"}),
        _dec("d-auto", "review_access", **{"class": "automation"}),
        _dec("d-legacy-auto", "no_progress"),
        _dec("d-user", "implementer_blocked", **{"class": "user_action"}),
        _dec("d-2d", "approval", question="T-1 needs 2D image assets (icons)",
             options=["I will provide the 2D assets (ChatGPT); run it without Media_Gen image "
                      "tools", "Use Media_Gen's image tools for the 2D assets", "Hold"])]))
    out = al.tick()
    assert out["queued"] == 3
    classes = sorted(r["cls"] for r in al.outbox())
    assert classes == ["image_ask", "product_decision", "user_action"]
    assert len(post.calls) == 3


def test_policy_toggles_and_other_alert_kinds(env):
    env["holder"]["cfg"] = C(webhook_url=HOOK, classes={"product_decision": False})
    cc, al = env["cc"], env["al"]
    al.tick()
    ingest(cc, _task(decisions=[_dec("d-prod", **{"class": "product"})]))
    ingest(cc, {"type": "task", "project_id": "au", "task_id": "T-2", "state": "COMPLETE"})
    ingest(cc, {"type": "task", "project_id": "au", "task_id": "T-3", "state": "BLOCKED",
                "blocker": "click Connect", "blocker_kind": "user_action"})
    ingest(cc, {"type": "task", "project_id": "au", "task_id": "T-4", "state": "BLOCKED",
                "blocker": "review budget"})
    al.tick()
    assert [r["cls"] for r in al.outbox()] == ["user_action"]   # T-3 only


def test_coordinator_events_raise_their_alert_classes(env):
    cc, al = env["cc"], env["al"]
    al.tick()
    ingest(cc,
           {"type": "coord_event", "kind": "self_heal", "event_id": "ev-1", "project_id": "au",
            "task_id": "T-1", "ts": env["clock"].t,
            "detail": {"action": "release_lock", "reason": "stale studio.lock"}},
           {"type": "coord_event", "kind": "self_heal", "event_id": "ev-2", "project_id": "au",
            "task_id": "T-1", "detail": {"alert_class": "self_repair_exhausted",
                                         "text": "caps exhausted, queue stuck"}},
           {"type": "coord_event", "kind": "prep_failed", "event_id": "ev-3", "project_id": "au",
            "task_id": "", "detail": {"text": "hook refused the commit"}},
           {"type": "coord_event", "kind": "self_heal", "event_id": "ev-4", "project_id": "au",
            "task_id": "T-9", "detail": {"alert_class": "pause_forgotten", "text": "16 h hold"}})
    al.tick()
    assert sorted(r["cls"] for r in al.outbox()) == ["pause_forgotten", "prep_failed",
                                                     "self_repair_exhausted"]
    # every event is kept for the digest, the plain repair too
    assert env["db"].query_one("SELECT COUNT(*) AS n FROM cc_coord_events")["n"] == 4


def test_decision_class_fallbacks():
    assert decision_class({"class": "automation", "kind": "approval"}) == "automation"
    assert decision_class({"decision_class": "user_action"}) == "user_action"
    assert decision_class({"decision_class": "decision", "kind": "lead_escalation"}) == \
        "automation"
    assert decision_class({"kind": "plan_question"}) == "product"
    assert decision_class({"kind": "implementer_blocked",
                           "evidence": ["blocker_kind: user_action"]}) == "user_action"
    assert is_image_ask({"options": ["Use Media_Gen's image tools for the 2D assets"]})


# ------------------------------------------------------------------ webhook

def test_json_webhook_body_and_token(env):
    env["holder"]["cfg"] = C(webhook_url="https://hooks.example.com/x",
                                        webhook_token="tok-abcdef")
    r = env["al"].test_alert("victor")
    assert r["webhook"] == "sent" and r["format"] == "json"
    call = env["post"].calls[0]
    body = json.loads(call["body"])
    assert body["title"] == "Foundry test alert" and "victor" in body["text"]
    assert call["headers"]["Authorization"] == "Bearer tok-abcdef"
    assert [a for a in env["cc"].alerts([], "open") if a["kind"] == "alert_test"]


def test_retry_with_backoff_then_success_and_no_secret_in_logs(env, caplog):
    cc, al, clock, post = env["cc"], env["al"], env["clock"], env["post"]
    post.status = [ConnectionError(f"connect to {HOOK} refused"), 503, 200]
    al.tick()
    beat(cc)
    clock.t += 11 * 60
    with caplog.at_level(logging.WARNING):
        al.tick()
    row = al.outbox()[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert HOOK not in (row["last_error"] or "") and "victor-secret-topic" not in caplog.text
    al.tick()                                                   # not due yet
    assert len(post.calls) == 1
    clock.t += 31
    al.tick()
    assert al.outbox()[0]["attempts"] == 2 and al.outbox()[0]["last_error"] == "HTTP 503"
    clock.t += 61
    al.tick()
    assert al.outbox()[0]["status"] == "sent" and len(post.calls) == 3
    assert "victor-secret-topic" not in caplog.text


def test_permanent_4xx_gives_up_at_once(env):
    env["post"].status = 404
    al = env["al"]
    al.tick()
    al._enqueue("x", "product_decision", "t", "b", "high")
    al.deliver(al.cfg())
    assert al.outbox()[0]["status"] == "failed" and al.outbox()[0]["attempts"] == 1


# ------------------------------------------------------------------ digest

def test_digest_contents(env):
    cc, al, clock, db = env["cc"], env["al"], env["clock"], env["db"]
    now = clock.t
    ingest(cc,
           {"type": "transition", "project_id": "au", "task_id": "T-1", "src_id": 1,
            "ts": now - 3 * 3600, "from": "READY", "to": "IMPLEMENTING"},
           {"type": "transition", "project_id": "au", "task_id": "T-1", "src_id": 2,
            "ts": now - 3600, "from": "IMPLEMENTING", "to": "COMPLETE"},
           {"type": "task", "project_id": "au", "task_id": "T-2", "state": "BLOCKED",
            "blocker": "needs Studio Connect", "blocker_kind": "user_action"},
           _task(decisions=[
               {"decision_id": "d1", "kind": "review_access", "status": "answered",
                "class": "automation", "authority": "lead_policy", "opened_ts": now - 7200,
                "answered_ts": now - 7000},
               {"decision_id": "d2", "kind": "scope_request", "status": "open",
                "class": "product", "opened_ts": now - 600}]),
           {"type": "coord_event", "kind": "self_heal", "event_id": "ev-9", "project_id": "au",
            "task_id": "T-1", "ts": now - 1800,
            "detail": {"action": "budget_extend", "reason": "review +1: code never reviewed"}})
    db.execute("INSERT INTO usage_events (ts, project_id, work_task_id, status, input_tokens, "
               "output_tokens) VALUES (?,?,?,?,?,?)", (now - 100, "au", "T-1", "settled",
                                                       120000, 3000))
    d = al.build_digest("manual", "victor")
    data = d["data"]
    assert [c["task_id"] for c in data["completed"]] == ["T-1"]
    assert data["self_heal"][0]["reason"].startswith("review +1")
    assert data["parked"][0]["task_id"] == "T-2"
    assert data["decisions_opened"] == {"automation": 1, "product": 1}
    assert data["notices_avoided"]["count"] == 1
    assert data["time_per_state_s"]["IMPLEMENTING"] == 7200
    assert data["tokens_per_sprint"][0]["input"] == 120000
    assert "Sprints completed: 1 (T-1)" in d["text"] and "Self-repairs: 1" in d["text"]
    assert d["sent"] == "queued" and env["post"].calls[-1]["headers"]["Tags"] == "newspaper"


def test_digest_runs_once_a_day_at_the_hour(env):
    al, clock = env["al"], env["clock"]
    env["holder"]["cfg"] = AlertsConfig(digest_hour=10)
    assert al.tick()["digest"] is None                          # 09:00 local
    clock.t += 3600                                             # 10:00
    first = al.tick()["digest"]
    assert first
    clock.t += 3600
    assert al.tick()["digest"] is None                          # once per day
    clock.t += 23 * 3600                                        # next day 10:00
    assert al.tick()["digest"] not in (None, first)
    assert len(al.digests(7)) == 2


# ------------------------------------------------------------------ API + settings

@pytest.fixture()
def app_cc(app):
    svc = app.state.services
    svc.config_store.config.clients.extend([
        ClientConfig(name="victor", key=OP_KEY, scopes=["operator"]),
        ClientConfig(name="coordinator", key=COORD_KEY, scopes=["inference", "orchestration"]),
    ])
    return svc


def test_settings_api_roundtrip_and_token_is_write_only(client, app_cc):
    r = client.get("/admin/api/alerts/settings")
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["coordinator_down_minutes"] == 10 and s["digest_hour"] == 8
    assert set(s["classes"]) >= {"product_decision", "stack_down", "prep_failed"}
    r = client.put("/admin/api/alerts/settings", json={
        "webhook_url": HOOK, "webhook_token": "tk_secret_123", "coordinator_down_minutes": 15,
        "digest_hour": 7, "classes": {"prep_failed": False}})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["webhook_token_set"] and "tk_secret_123" not in r.text
    assert s["classes"]["prep_failed"] is False and s["classes"]["stack_down"] is True
    raw = app_cc.config_store.raw["alerts"]
    assert raw["coordinator_down_minutes"] == 15 and raw["webhook_token"] == "tk_secret_123"
    # an empty token keeps the stored one; clear_token drops it
    client.put("/admin/api/alerts/settings", json={"webhook_token": ""})
    assert app_cc.config_store.config.alerts.webhook_token == "tk_secret_123"
    client.put("/admin/api/alerts/settings", json={"clear_token": True})
    assert app_cc.config_store.config.alerts.webhook_token is None
    assert client.put("/admin/api/alerts/settings",
                      json={"digest_hour": 25}).status_code == 400
    assert client.put("/admin/api/alerts/settings",
                      json={"webhook_url": "ftp://x"}).status_code == 400
    assert client.put("/admin/api/alerts/settings",
                      json={"classes": {"nope": True}}).status_code == 400


def test_test_alert_and_digest_endpoints(client, app_cc):
    r = client.post("/admin/api/alerts/test")
    assert r.status_code == 200 and r.json() == {"page": True, "webhook": "not set"}
    r = client.post("/admin/api/alerts/digest")
    assert r.status_code == 200 and "Sprints completed" in r.json()["digest"]["text"]
    assert len(client.get("/admin/api/alerts/digests").json()["digests"]) == 1
    # control center: operator session reads and builds digests
    lr = client.post("/control/api/login", json={"key": OP_KEY})
    h = {"x-foundry-csrf": lr.json()["csrf"]}
    assert len(client.get("/control/api/digests").json()["digests"]) == 1
    assert client.post("/control/api/digests", headers=h).status_code == 200
    assert len(client.get("/control/api/digests").json()["digests"]) == 2
    assert client.post("/control/api/digests").status_code == 403   # no CSRF token


def test_coord_event_reaches_the_router_table_through_the_api(client, app_cc):
    obs = {"type": "coord_event", "kind": "self_heal", "event_id": "ev-api", "project_id": "au",
           "task_id": "T-1", "ts": time.time(), "detail": {"action": "resume_hold"}}
    r = client.post("/v1/foundry/telemetry/batch", headers=COORD,
                    json={"contract": CONTRACT, "source": {"id": "bridge:x", "kind": "bridge"},
                          "observations": [obs]})
    assert r.status_code == 200 and r.json()["accepted"] == {"coord_event": 1}
    d = client.post("/admin/api/alerts/digest").json()["digest"]
    assert d["data"]["self_heal"][0]["action"] == "resume_hold"
