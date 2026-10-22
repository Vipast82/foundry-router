"""Helper events carry numbers (W4): a finished helper's event has its wall time (and
tokens when the lane reported them), so the router's sprint scorecard can total them."""

import json

from foundry_coordinator import lanes as L
from tests.coordinator.test_helper_status_vocab import (  # noqa: F401 (fixture)
    StubClient, ctx, env, row, running_unit, stub_p)


def _event(c, kind):
    return [dict(r, detail=json.loads(r["detail"])) for r in
            c.store.q("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_finished_helper_event_has_duration_and_kind(env):
    c, P, _ = ctx(env)
    uid = running_unit(env)
    began = row(c, uid)["started_ts"]
    assert began
    env["clock"][0] += 95
    c.helpers._finish(P, row(c, uid), "done", "ok", "result text",
                      tokens={"total": 321, "input": 300, "output": 21})
    [ev] = _event(c, "helper_done")
    d = ev["detail"]
    assert d["unit"] == uid and d["kind"] == row(c, uid)["kind"]
    assert 94 <= d["duration_s"] <= 96
    assert d["tokens"]["total"] == 321


def test_helper_event_without_a_start_time_has_no_invented_duration(env):
    c, P, _ = ctx(env)
    uid = running_unit(env)
    c.store.x("UPDATE helper_units SET started_ts=NULL, created_ts=NULL WHERE unit_id=?", (uid,))
    c.helpers._finish(P, row(c, uid), "failed", "boom", None)
    [ev] = _event(c, "helper_failed")
    assert ev["detail"]["duration_s"] is None and "tokens" not in ev["detail"]
