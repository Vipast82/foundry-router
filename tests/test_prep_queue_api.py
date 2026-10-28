"""Repo changes, router side (self-driving part 3): the Automation page's API
(/admin/api/automation/prep*), the prep_* control actions (validation, handed only to a
coordinator declaring the `prep_queue` feature), the heartbeat's `prep_queue` block, the
[prep_queue] settings in the schema, and the Repo changes tab in automation.html."""

import time
from pathlib import Path

from foundry_automation import document as D
from foundry_router.control_center import CONTRACT
from tests.test_control_center import COORD, cc, login  # noqa: F401

A = "/admin/api/automation/prep"
H = {"x-foundry-automation": "1"}
_n = [0]


def _beat(client, features=("queue", "prep_queue"), block=None):
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.156.0",
                       "capabilities": {"actions": ["prep_add", "prep_remove", "prep_reorder",
                                                    "prep_run_now"]}},
            "contract": CONTRACT, "coordinator_version": "0.156.0", "features": list(features),
            "sent_ts": time.time()}
    if block is not None:
        body["prep_queue"] = block
    r = client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body)
    assert r.status_code == 200, r.text


def _claim(client):
    r = client.post("/v1/foundry/control/claim", headers=COORD,
                    json={"source_id": "coordinator:ws", "projects": ["proj"]})
    assert r.status_code == 200, r.text
    return r.json()["actions"]


def _post(client, action, **kw):
    _n[0] += 1
    return client.post(A + "/actions", headers=H,
                       json={"action": action, "project": "proj",
                             "idempotency_key": f"pq-test-{_n[0]:06d}", **kw})


def test_add_validates_and_is_handed_only_to_a_prep_queue_coordinator(client, cc):
    assert client.post(A + "/actions", json={"action": "add"}).status_code == 403   # no header
    assert _post(client, "explode").status_code == 400
    r = _post(client, "add", entry_kind="patch", message="")
    assert r.status_code == 400 and "message" in r.json()["error"]
    r = _post(client, "add", entry_kind="patch", message="prep: x")
    assert r.status_code == 400 and "file path" in r.json()["error"]
    assert _post(client, "add", entry_kind="script", message="m").status_code == 400
    assert _post(client, "add", entry_kind="patch", message="m", source="p",
                 after_actions="reload_kilo").status_code == 400
    assert _post(client, "reorder", id="pq-0123456789", position=0).status_code == 400
    assert _post(client, "remove", id="nope").status_code == 400
    r = _post(client, "add", entry_kind="patch", message="prep: x", source=r"C:\p.patch",
              body="why", after_task="GF-GATE1", after_actions=["reload_kilo"],
              hold_tasks=["HK-CLOSE1"], hold_minutes=30, position=1)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["status"] == "accepted" and a["kind"] == "prep_add"
    assert "queue repo change (patch)" in a["summary"]
    # a coordinator without the feature is not handed it
    _beat(client, features=("queue",))
    assert [x for x in _claim(client) if x["kind"] == "prep_add"] == []
    _beat(client)
    got = [x for x in _claim(client) if x["kind"] == "prep_add"]
    assert len(got) == 1
    p = got[0]["payload"]
    assert p == {"kind": "patch", "message": "prep: x", "source": r"C:\p.patch", "body": "why",
                 "after_task": "GF-GATE1", "after_actions": ["reload_kilo"],
                 "hold_tasks": ["HK-CLOSE1"], "hold_minutes": 30, "position": 1}
    st = client.get(A + "/actions/" + a["action_id"]).json()
    assert st["kind"] == "prep_add"
    assert client.get(A + "/actions/act-doesnotexist").status_code == 404


def test_reorder_remove_run_now_and_operator_route(client, cc):
    _beat(client)
    for action, kw in (("reorder", {"id": "pq-0123456789", "position": 2}),
                       ("remove", {"id": "pq-0123456789"}), ("run_now", {})):
        r = _post(client, action, **kw)
        assert r.status_code == 200, r.text
    kinds = sorted(x["kind"] for x in _claim(client))
    assert kinds == ["prep_remove", "prep_reorder", "prep_run_now"]
    # the same actions through the operator session route
    h = login(client)
    r = client.post("/control/api/actions", headers=h, json={
        "kind": "prep_run_now", "project_id": "proj", "idempotency_key": "pq-op-1234567"})
    assert r.status_code == 200, r.text


def test_view_reads_the_heartbeat_block(client, cc):
    assert client.get(A).json()["prep_queue"] == {}
    block = {"proj": {"enabled": True, "pending": 1, "state": {"safe": False,
                                                               "reason": "T1 is VERIFYING"},
                      "entries": [{"id": "pq-0123456789", "order": 1, "status": "pending",
                                   "message": "prep: x", "log": [{"ts": 1, "msg": "added"}]}]},
             "other": {"enabled": True, "entries": []}}
    _beat(client, block=block)
    v = client.get(A + "?project=proj").json()
    assert list(v["prep_queue"]) == ["proj"] and v["source"] == "coordinator:ws"
    assert v["prep_queue"]["proj"]["entries"][0]["message"] == "prep: x"
    assert v["prep_queue"]["proj"]["state"]["reason"] == "T1 is VERIFYING"


def test_prep_queue_settings_validate_in_the_schema():
    doc = {"format": "foundry-automation/1", "project": "p",
           "settings": {"roles": {"implementer": "Foundry-Kilo-Local"},
                        "prep_queue": {"enabled": False, "check_every_seconds": 30,
                                       "block_states": ["IMPLEMENTING"]}}}
    from foundry_automation import FORMAT
    doc["format"] = FORMAT
    v = D.validate(doc, "p")
    assert v["ok"], v
    doc["settings"]["prep_queue"]["check_every_seconds"] = 0
    doc["settings"]["prep_queue"]["pull"] = "yes"
    v = D.validate(doc, "p")
    keys = {e["path"] for e in v["errors"]}
    assert {"prep_queue.check_every_seconds", "prep_queue.pull"} <= keys, v


def test_repo_changes_tab_is_on_the_automation_page():
    html = (Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" /
            "automation.html").read_text(encoding="utf-8")
    assert 'id="tab-prep"' in html and 'id="v-prep"' in html
    assert '"prep"' in html.split("const VIEWS =")[1].split("\n")[0]
    for s in ("async function renderPrep", "automation/prep/actions", 'data-act="pq-add"',
              'data-act="pq-run"', 'data-act="pq-up"', 'data-act="pq-remove"', 'data-act="pq-log"',
              'a.startsWith("pq-")'):
        assert s in html, s
