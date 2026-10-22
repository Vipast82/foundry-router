"""Foundry Client router relay (foundry_client/relay.py): heartbeat payload,
claim loop against a fake router, restart and reload verbs applied through the
manager and reported back, error backoff, key lookup; and the same loop against
the real router app (operator key, kind client)."""
import threading

import pytest

from foundry_client import relay as R


class FakeMgr:
    def __init__(self):
        self.procs = {"kilo": object(), "coordinator": object(), "rojo": object()}
        self.calls = []

    def status(self, name=None):
        return {"overall": "ok", "components": [{"name": "kilo", "label": "Kilo server",
                                                 "state": "running", "detail": "port 4096",
                                                 "enabled": True, "want": True, "pid": 12,
                                                 "uptime_s": 5, "restarts": 0, "pending_restart": ""}],
                "config_problems": [], "events": [{"ts": 1.0, "component": "kilo", "text": "started"}]}

    def restart(self, name, force=False):
        self.calls.append((name, force))
        return {"name": name, "label": name.title(), "state": "stopped" if force else "running",
                "detail": "restarting" if force else "2 session(s) working",
                "pending_restart": "" if force else "safe"}


class FakeRouter:
    def __init__(self, actions=()):
        self.posts = []
        self.actions = list(actions)
        self.reports = []
        self.fail = 0

    def __call__(self, path, body):
        self.posts.append((path, body))
        if self.fail:
            self.fail -= 1
            raise R.RelayError("boom")
        if path.endswith("/heartbeat"):
            return {"ok": True, "compat": "ok", "compat_reason": "", "server_version": "0.134.0"}
        if path.endswith("/claim"):
            acts, self.actions = self.actions, []
            return {"actions": acts}
        if "/actions/" in path:
            self.reports.append((path.split("/")[-2], body))
            return {"status": body["status"]}
        raise AssertionError(path)


def cfg():
    return {"client": {"backend_url": "http://router:11435/", "router_key": "FOUNDRY_CLIENT_KEY"},
            "components": {"kilo": {"port": 4096}}}


def test_heartbeat_declares_client_kind_feature_and_components():
    rt = FakeRouter()
    r = R.Relay(FakeMgr(), cfg(), "k", source_id="client:t", post=rt, clock=lambda: 100.0, log=lambda s: None)
    r.heartbeat()
    path, body = rt.posts[0]
    assert path == "/v1/foundry/telemetry/heartbeat"
    assert body["source"]["kind"] == "client" and body["features"] == ["stack_control"]
    assert body["contract"] == "foundry.cc.v1" and body["components"][0]["state"] == "running"
    assert body["events"][0]["text"] == "started" and r.compat["state"] == "ok"
    assert r.url == "http://router:11435"


def test_claim_loop_applies_restart_and_reload_and_reports(monkeypatch):
    mgr = FakeMgr()
    rt = FakeRouter(actions=[
        {"action_id": "a1", "kind": "client_restart", "payload": {"component": "kilo", "force": False}},
        {"action_id": "a2", "kind": "client_restart", "payload": {"component": "coordinator", "force": True}},
        {"action_id": "a3", "kind": "client_restart", "payload": {"component": "media"}},
        {"action_id": "a4", "kind": "client_reload_kilo_config", "payload": {}},
        {"action_id": "a5", "kind": "client_fly", "payload": {}}])
    disposed = []
    r = R.Relay(mgr, cfg(), "k", source_id="client:t", post=rt, clock=lambda: 100.0, log=lambda s: None,
                kilo_dispose=lambda: (disposed.append(1) or True, "HTTP 200"))
    r.tick()
    assert mgr.calls == [("kilo", False), ("coordinator", True)]
    rep = {aid: body for aid, body in rt.reports}
    assert rep["a1"]["status"] == "applied" and "safe point" in rep["a1"]["detail"]
    assert rep["a1"]["result"]["pending_restart"] == "safe"
    assert rep["a2"]["status"] == "applied" and rep["a2"]["result"]["force"] is True
    assert rep["a3"]["status"] == "rejected" and "media" in rep["a3"]["detail"]
    assert rep["a4"]["status"] == "applied" and rep["a4"]["result"]["method"] == "dispose" and disposed
    assert rep["a5"]["status"] == "rejected"
    assert all(b["source_id"] == "client:t" for _, b in rt.reports)
    assert [p for p, _ in rt.posts][:2] == ["/v1/foundry/telemetry/heartbeat", "/v1/foundry/control/claim"]
    assert rt.posts[1][1] == {"source_id": "client:t", "source_kind": "client", "limit": 10}
    # no dispose endpoint: a safe restart instead, and the report says so
    rt.actions = [{"action_id": "a6", "kind": "client_reload_kilo_config", "payload": {}}]
    r._dispose = lambda: (False, "HTTP 404")
    r.tick()
    body = dict(rt.reports)["a6"]
    assert body["status"] == "applied" and body["result"]["method"] == "restart" and "HTTP 404" in body["detail"]
    assert mgr.calls[-1] == ("kilo", False)


def test_heartbeat_is_rate_limited_and_errors_back_off():
    rt = FakeRouter()
    t = [100.0]
    r = R.Relay(FakeMgr(), cfg(), "k", source_id="client:t", post=rt, clock=lambda: t[0], log=lambda s: None)
    r.tick(); t[0] += 3; r.tick()
    assert sum(1 for p, _ in rt.posts if p.endswith("/heartbeat")) == 1
    t[0] += 15; r.tick()
    assert sum(1 for p, _ in rt.posts if p.endswith("/heartbeat")) == 2
    rt.fail = 2
    stop = threading.Event()
    waits = []
    stop.wait = lambda s: waits.append(s) or (len(waits) >= 3 and stop.set())   # type: ignore[assignment]
    r.run_forever(stop)
    assert waits[:2] == [5, 15] and r.fails == 0 and r.last_error == ""   # recovered on the 3rd


def test_router_key_lookup_and_start_if_enabled(monkeypatch):
    vals = {}
    monkeypatch.setattr(R.secrets, "get", lambda n: vals.get(n))
    c = cfg()
    assert R.router_key(c) == ""
    vals["FOUNDRY_COORDINATOR_KEY"] = "coord"
    assert R.router_key(c) == "coord"                             # fallback
    vals["FOUNDRY_CLIENT_KEY"] = "op"
    assert R.router_key(c) == "op"
    logs = []
    c["client"]["relay_enabled"] = False
    assert R.start_if_enabled(FakeMgr(), c, threading.Event(), log=logs.append) is None
    c["client"]["relay_enabled"] = True
    vals.clear()
    assert R.start_if_enabled(FakeMgr(), c, threading.Event(), log=logs.append) is None
    assert logs and "no router key" in logs[0]
    vals["FOUNDRY_CLIENT_KEY"] = "op"
    started = []
    monkeypatch.setattr(R.Relay, "start_thread", lambda self, stop: started.append(self))
    rel = R.start_if_enabled(FakeMgr(), c, threading.Event(), log=logs.append)
    assert rel is not None and started == [rel] and rel.key == "op"


@pytest.mark.usefixtures("cc")
def test_relay_against_the_real_router(client):
    """The loop end to end: operator key, kind client, a stack action the chat
    filed, claimed and reported through the real routes."""
    from tests.test_control_center import OP_KEY, login
    mgr = FakeMgr()

    def post(path, body):
        r = client.post(path, json=body, headers={"Authorization": f"Bearer {OP_KEY}"})
        if r.status_code != 200:
            raise R.RelayError(f"{path}: HTTP {r.status_code} {r.text[:120]}")
        return r.json()
    rel = R.Relay(mgr, cfg(), OP_KEY, source_id="client:ws", post=post, log=lambda s: None)
    rel.tick()                                                   # heartbeat + empty claim
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "client_restart", "component": "kilo", "force": True, "idempotency_key": "k-123456789"})
    assert r.status_code == 200, r.text
    aid = r.json()["action_id"]
    rel.tick()
    assert mgr.calls == [("kilo", True)]
    a = client.get(f"/control/api/actions/{aid}").json()
    assert a["status"] == "applied" and a["claimed_by"] == "client:ws"
    assert a["result"]["force"] is True and a["log"][-1]["actor"] == "client:ws"
    assert rel.handled[-1]["action_id"] == aid


# the control-center fixtures (operator and coordinator keys)
from tests.test_control_center import cc  # noqa: E402,F401
