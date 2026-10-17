"""Stage 4: proactive lanes in the control center, with the real bridge,
Foundry's real ControlCenter (in-process) and the real control channel: lane
units with assignment/parent/model/defer reason/findings/diff/feedback,
artifact metadata + thumbnail, the observed-only lineage graph, and the
operational kill switch (authorized, CSRF/scope rules unchanged)."""

import json
import uuid

import pytest

from foundry_coordinator.control import ControlChannel
from foundry_coordinator.telemetry import KiloObserver, TelemetryBridge
from foundry_router.control_center import ControlError
from tests.coordinator.lane_env import CLAUDE, VISION, admit, answer_helper, finish_primary, \
    setup
from tests.coordinator.test_control_bridge import DirectFoundry
from tests.coordinator.test_lane_visual import HIGH, SKILL, VIS, VROUTES, capture, step, \
    vision_says


@pytest.fixture()
def lenv(tmp_path, app):
    from tests.coordinator.lane_env import png
    (tmp_path / "outside_ref.png").write_bytes(png(2, 2))
    e = setup(tmp_path, lanes=VIS, extra_files={".kilo/skills/au-ui-review/SKILL.md": SKILL})
    e["ff"].admission[VISION] = admit(routes=VROUTES)
    e["ff"].admission[CLAUDE] = admit()
    cc = app.state.services.control
    direct = DirectFoundry(cc)
    P = list(e["c"].projects.values())[0]
    bridge = TelemetryBridge([P], e["c"].store.path, direct,
                             observers={"game": KiloObserver(P.client.url,
                                                             password=e["fake"].password)},
                             use_events=False, poll_s=0.0)
    ch = ControlChannel(e["c"], foundry=direct, source_id="coordinator:test")
    e.update(cc=cc, bridge=bridge, ch=ch, app=app)
    yield e
    bridge.stop()
    e["fake"].stop()


def flow(e):
    """capture -> observer -> proposer answered -> one delivery."""
    vision_says(e, HIGH)
    capture(e)
    step(e, 2)
    prop = [u for u in e["c"].helpers.units("game", "T2") if u.get("lane") == "proposer"][0]
    answer_helper(e, prop["session_id"], {
        "assignment": prop["unit_id"], "base": prop["snapshot_id"], "summary": "inset",
        "findings": [{"severity": "high", "confidence": 0.8, "observed": "clipped"}],
        "patch": "--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1 +1 @@\n-local M = {}\n+local M = {i=1}\n"})
    step(e)
    finish_primary(e, {"status": "NOT DONE", "summary": "pass"})
    step(e)
    return prop


def test_lanes_reach_the_control_center_with_lineage(lenv):
    e = lenv
    prop = flow(e)
    e["bridge"].step()
    lanes = e["cc"].lanes([], "game", "T2")
    by = {u["lane"]: u for u in lanes["units"]}
    obs, pr = by["visual"], by["proposer"]
    assert obs["persona"] == VISION and obs["backend"] == "vision"
    assert obs["parent_session"] == e["primary"] and pr["parent_unit"] == obs["unit_id"]
    assert obs["assignment"]["sent"][0]["sha256"] and obs["assignment"]["skills_loaded"]
    assert obs["envelope"]["image_seen"] is True and obs["envelope"]["findings"]
    assert pr["status"] == "delivered" and pr["feedback"] == "delivered"
    assert "+local M = {i=1}" in pr["envelope"]["patch"]
    [art] = [a for a in lanes["artifacts"] if a["kind"] == "capture"]
    assert art["sha256"] and art["generation"] == 1 and art["has_thumb"]
    data, mime = e["cc"].artifact_thumb(art["artifact_id"], [])
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    with pytest.raises(ControlError):
        e["cc"].artifact_thumb(art["artifact_id"], ["other-project"])   # project isolation
    g = e["cc"].route_graph([], "game", "T2", minutes=60 * 24 * 365)
    kinds = {(x["from"].split(":")[0], x["to"].split(":")[0], x["label"]) for x in g["edges"]
             if x["kind"] in ("lineage", "delivery")}
    assert ("a", "u", "observe") in kinds and ("u", "u", "propose") in kinds
    assert ("u", "s", "delivered") in kinds
    # nothing is animated: no in-flight ledger row and no busy helper session
    assert not [x for x in g["edges"] if x["kind"] == "lineage" and x["state"] == "active"]


def test_deferred_unit_shows_its_quota_reason(lenv):
    e = lenv
    e["ff"].admission[VISION] = admit("defer", "Weekly 72.0% used (at or above 70%)",
                                      routes=VROUTES)
    capture(e)
    step(e, 2)
    e["bridge"].step()
    [u] = e["cc"].lanes([], "game", "T2")["units"]
    assert u["status"] == "deferred" and "72.0%" in u["reason"]
    g = e["cc"].route_graph([], "game", "T2", minutes=60 * 24 * 365)
    assert [x for x in g["edges"] if x["kind"] == "lineage" and x["state"] == "deferred"]


def test_running_unit_is_active_only_while_observed_in_flight(lenv):
    e = lenv
    e["ff"].vision_answer = None
    capture(e)
    e["c"].tick()
    e["c"].tick()
    [u] = [x for x in e["c"].helpers.units("game", "T2") if x["lane"] == "visual"]
    e["c"].store.x("UPDATE helper_units SET status='running' WHERE unit_id=?", (u["unit_id"],))
    e["bridge"].step()
    g = e["cc"].route_graph([], "game", "T2", minutes=60 * 24 * 365)
    st = {x["state"] for x in g["edges"] if x["kind"] == "lineage"}
    assert "active" not in st and "waiting" in st
    db = e["app"].state.services.db
    db.execute("INSERT INTO usage_events (ts, session_id, persona, backend, model, status) VALUES "
               "(?,?,?,?,?,?)", (0, u["session_id"], VISION, "meridian", "claude-sonnet",
                                 "reserved"))
    g = e["cc"].route_graph([], "game", "T2", minutes=60 * 24 * 365)
    assert "active" in {x["state"] for x in g["edges"] if x["kind"] == "lineage"}


def test_lane_kill_switch_is_an_authorized_coordinator_action(lenv):
    e = lenv
    e["bridge"].step()
    with pytest.raises(ControlError) as ei:
        e["cc"].create_action("victor", ["other"], {"kind": "lanes", "project_id": "game",
                                                    "task_id": "T2", "paused": True,
                                                    "idempotency_key": "k-" + uuid.uuid4().hex})
    assert ei.value.code == "forbidden"
    a = e["cc"].create_action("victor", [], {"kind": "lanes", "project_id": "game", "task_id": "T2",
                                             "paused": True, "note": "pilot hold",
                                             "idempotency_key": "k-" + uuid.uuid4().hex})
    assert a["status"] == "accepted" and "paused" in a["summary"]
    e["ch"].poll()
    assert e["cc"].action_view(a["action_id"])["status"] == "applied"
    assert e["c"].helpers.lanes.paused("game")
    capture(e)
    step(e, 2)
    assert not [u for u in e["c"].helpers.units("game", "T2") if u.get("lane")]
    e["bridge"].step()
    assert e["cc"].lanes([], "game")["state"]["paused"] is True
