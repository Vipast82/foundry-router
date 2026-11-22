"""Standing orders, router side (COORDINATOR_AUTOMATION_NEEDS item 28): the order_add /
order_cancel / extend_time control actions (validated, handed only to a coordinator that
declares the `orders` feature), the heartbeat's `orders` and `watch_log` blocks behind
GET /control/api/orders, and the Orders tab of the Automation page."""

import time
from pathlib import Path

from foundry_router.control_center import CONTRACT, ORDER_ACTIONS, ControlCenter, ControlError
from tests.test_control_center import COORD, cc, login  # noqa: F401

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "automation.html"
_n = [0]


def _beat(client, features=("queue", "orders"), orders=None, log=None):
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.181.228",
                       "capabilities": {"actions": list(ORDER_ACTIONS)}},
            "contract": CONTRACT, "coordinator_version": "0.181.228", "features": list(features),
            "sent_ts": time.time()}
    if orders is not None:
        body["orders"] = orders
    if log is not None:
        body["watch_log"] = log
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text


def _claim(client):
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert r.status_code == 200, r.text
    return r.json()["actions"]


def _act(client, h, kind, **kw):
    _n[0] += 1
    return client.post("/control/api/actions", headers=h, json={
        "kind": kind, "project_id": "proj", "idempotency_key": f"so-test-{_n[0]:06d}", **kw})


def test_the_three_orders_of_last_week_are_accepted_and_handed_to_an_orders_coordinator(client, cc):
    h = login(client)
    r = _act(client, h, "order_add", order_kind="keep_going", task="POL-7",
             note="need POL-7 to complete")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "accepted" and a["kind"] == "order_add"
    assert a["summary"] == "standing order: keep POL-7 going until it completes"
    r = _act(client, h, "order_add", order_kind="pause_all", reset="gpt")
    assert r.json()["summary"] == "standing order: pause all sprints until the gpt window resets"
    r = _act(client, h, "order_add", order_kind="claude_agents", n=1, minutes=120)
    assert r.json()["summary"] == "standing order: 1 Claude agent only for 120 min"
    r = _act(client, h, "extend_time", task="POL-7", minutes=240, note="four more hours")
    assert r.json()["summary"] == "240 more minutes for POL-7: four more hours"
    r = _act(client, h, "order_cancel", order_id="so-0123456789", why="done")
    assert r.json()["summary"] == "cancel standing order so-0123456789: done"
    # a coordinator without the feature is not handed them
    _beat(client, features=("queue",))
    assert [x for x in _claim(client) if x["kind"] in ORDER_ACTIONS] == []
    _beat(client)
    got = {x["kind"]: x for x in _claim(client) if x["kind"] in ORDER_ACTIONS}
    assert set(got) == set(ORDER_ACTIONS)
    assert got["extend_time"]["payload"] == {"task": "POL-7", "minutes": 240,
                                             "note": "four more hours"}
    assert got["order_cancel"]["payload"] == {"order_id": "so-0123456789", "why": "done"}


def test_a_bad_order_is_refused_with_the_reason(client, cc):
    h = login(client)
    for kw, msg in (
            ({"order_kind": "explode"}, "order_kind must be one of"),
            ({"order_kind": "keep_going"}, "needs the sprint"),
            ({"order_kind": "keep_going", "task": "T", "minutes": 5}, "ends when its sprint completes"),
            ({"order_kind": "keep_going", "task": "T", "per_day": 500}, "per_day must be a whole number"),
            ({"order_kind": "pause_all"}, "needs an end"),
            ({"order_kind": "pause_all", "minutes": 5, "reset": "gpt"}, "not both"),
            ({"order_kind": "pause_all", "reset": "the gpt one!"}, "reset: the backend"),
            ({"order_kind": "pause_all", "minutes": 0}, "minutes must be a number"),
            ({"order_kind": "claude_agents"}, "n is required"),
            ({"order_kind": "claude_agents", "n": 99}, "n must be a whole number 1..32"),
            ({"order_kind": "claude_agents", "n": 1, "reset": "gpt"}, "minutes or no end")):
        r = _act(client, h, "order_add", **kw)
        assert r.status_code == 400 and msg in r.json()["error"], (kw, r.text)
    assert "order_id" in _act(client, h, "order_cancel", order_id="nope").json()["error"]
    assert "task is required" in _act(client, h, "extend_time", minutes=5).json()["error"]
    assert "minutes" in _act(client, h, "extend_time", task="T", minutes=0).json()["error"]
    assert client.post("/control/api/actions", headers=h, json={
        "kind": "order_add", "idempotency_key": "so-test-noproj1", "order_kind": "pause_all",
        "minutes": 5}).status_code == 400                       # no project
    assert client.post("/control/api/actions", json={
        "kind": "order_add", "project_id": "proj", "idempotency_key": "so-test-nocsrf1",
        "order_kind": "pause_all", "minutes": 5}).status_code == 403    # no CSRF token


def test_the_orders_view_is_the_heartbeats_block_with_the_watch_log(client, cc):
    login(client)
    assert client.get("/control/api/orders").json() == {
        "orders": {}, "watch_log": [], "source": None, "age_s": None}
    block = {"proj": {"active": [{"order_id": "so-0123456789", "kind": "keep_going",
                                  "task_id": "POL-7", "status": "active", "by": "victor",
                                  "order": "keep POL-7 going until it completes", "note": "",
                                  "actions": 3, "actions_today": 2, "waiting": "",
                                  "created_ts": 1.0, "ended_ts": None, "result": ""}],
                      "ended": [], "kinds": ["keep_going", "pause_all", "claude_agents"]}}
    log = ["2026-11-18 21:59  POL-7  order 'keep POL-7 going until it completes': put it back",
           "2026-11-18 22:14  POL-7  BLOCKED -> READY by order:so-0123456789 (budget reset)"]
    _beat(client, orders=block, log=log)
    v = client.get("/control/api/orders?project=proj").json()
    assert v["orders"] == block and v["watch_log"] == log and v["source"] == "coordinator:ws"
    assert client.get("/control/api/orders?project=other").status_code in (200, 404)
    # not signed in: nothing
    client.cookies.clear()
    assert client.get("/control/api/orders").status_code == 401


def test_the_payload_check_is_the_same_for_the_audit_summary():
    p, s = ControlCenter._order_payload("order_add", {"order_kind": "claude_agents", "n": 2})
    assert p == {"kind": "claude_agents", "n": 2}
    assert s == "standing order: 2 Claude agents only until cancelled"
    assert ControlCenter._summary("order_add", p, "") == s
    try:
        ControlCenter._order_payload("order_add", {"order_kind": "pause_all"})
    except ControlError as e:
        assert "needs an end" in str(e)
    else:
        raise AssertionError("an endless pause was accepted")


def test_the_automation_page_has_the_orders_tab_wired():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="tab-orders"' in html and 'id="v-orders"' in html
    assert '"orders", "services"' in html                           # VIEWS
    assert 'if (S.view === "orders") return renderOrders();' in html
    assert 'else if (a.startsWith("so-")) orderClick(a, b);' in html
    for needle in ('api("orders"', 'orderAction("order_add"', 'orderAction("order_cancel"',
                   'orderAction("extend_time"', "order_kind: kind", 'class="wlog"',
                   'data-act="so-new"', 'data-act="so-time"', 'data-act="so-cancel"'):
        assert needle in html, needle
    # the action's kind is never overwritten by the order's
    assert "body = {kind," not in html.split("async function orderClick")[1].split(
        "repo changes")[0]
