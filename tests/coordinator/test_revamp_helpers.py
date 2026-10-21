"""Revamp B5: helpers run on demand only, their use is measured from git, and a
lane at its cap writes one `lane_limit` event, not one per tick.
Fake Kilo / Foundry only; no model calls."""

import json
from types import SimpleNamespace

import pytest

from foundry_coordinator import automation as A
from foundry_coordinator import lanes as L
from foundry_coordinator.profile import FAST_LANE_DEFAULTS, ProfileError, load
from tests.coordinator.helpers import git, make_repo, write_profile
from tests.coordinator.lane_env import setup

ALL_LANES = "[lanes]\nenabled = true\n" + "\n".join(
    f"[lanes.{x}]\nenabled = true" for x in L.LANES) + "\n"


def _profile(tmp_path, extra):
    repo, _ = make_repo(tmp_path, "game", [{"id": "T1", "title": "t", "spec": "s",
                                            "scope": ["src/*"]}])
    return load(str(write_profile(tmp_path, "game", repo, extra=extra)))


def test_lanes_are_off_by_default_even_when_the_lanes_table_enables_them(tmp_path):
    P = SimpleNamespace(profile=_profile(tmp_path, ALL_LANES))
    ls = L.settings(P, "T1")
    assert ls["enabled"] is False and ls.get("on_demand") is True
    assert not any(L.lane_on(ls, x) for x in L.LANES)
    assert not any(L.lane_on(L.settings(P), x) for x in L.LANES)


def test_proactive_mode_keeps_the_lanes_table(tmp_path):
    P = SimpleNamespace(profile=_profile(tmp_path, '[automation]\nhelpers = "proactive"\n'
                                         + ALL_LANES))
    assert all(L.lane_on(L.settings(P, "T1"), x) for x in L.LANES)


def test_fast_lane_tasks_list_no_lane_by_default():
    assert FAST_LANE_DEFAULTS["lanes"] == []


def test_automation_table_is_validated(tmp_path):
    p = _profile(tmp_path, "")
    assert p.automation["helpers"] == "on_demand"
    assert p.automation["decision_timeout_minutes"] == 30
    assert p.automation["subagents_allowed"] == ["explore"]
    for bad in ('[automation]\nhelpers = "sometimes"\n', '[automation]\nnope = 1\n',
                '[automation]\ndecision_timeout_minutes = -1\n',
                '[automation]\nscope_auto_approve = [{path = "../x"}]\n',
                '[automation]\nscope_auto_approve = [{path = "a", lines = ["("]}]\n'):
        with pytest.raises(ProfileError):
            _profile(tmp_path / bad.__hash__().__str__(), bad)


def test_transcript_poll_default_is_two_minutes(tmp_path):
    P = SimpleNamespace(profile=_profile(tmp_path, '[automation]\nhelpers = "proactive"\n'
                                         + ALL_LANES))
    assert L.settings(P)["transcript_poll_seconds"] == 120.0
    assert A.get(P, "transcript_poll_seconds") == 120


ONLY_TRIAGE = ('[automation]\nhelpers = "proactive"\n[lanes]\nenabled=true\n'
               'transcript_poll_seconds=0\n[lanes.failure_triage]\nenabled=true\nmax_per_task=1\n')


@pytest.fixture
def env(tmp_path):
    e = setup(tmp_path, lanes=ONLY_TRIAGE)
    try:
        yield e
    finally:
        e["fake"].stop()


def test_lane_limit_is_one_event_per_key_not_one_per_tick(env):
    c = env["c"]
    P, t = c.projects["game"], c.store.task("game", "T2")
    ls = L.settings(P, "T2")
    assert c.helpers.lanes.add(P, t, "failure_triage", "a", {}, ls)[1] == "created"
    for _ in range(25):
        assert c.helpers.lanes.add(P, t, "failure_triage", "b", {}, ls)[1] == "limit"
    rows = c.store.q("SELECT detail FROM events WHERE kind='lane_limit'")
    assert len(rows) == 1
    # a different key is a different fact: one more event, once
    for _ in range(5):
        c.helpers.lanes.add(P, t, "failure_triage", "c", {}, ls)
    assert len(c.store.q("SELECT 1 FROM events WHERE kind='lane_limit'")) == 2


def _delivered(c, P, t, files, result=""):
    uid = c.helpers._add("game", "T2", t["run_id"], {"responsibility": f"r {files}",
                                                       "kind": "code_proposal",
                                                       "files": files}, "report",
                         c.helpers.settings(P), consumer=f"primary:{t['session_id']}")
    c.store.x("UPDATE helper_units SET status='done', result=? WHERE unit_id=?", (result, uid))
    c.helpers.mark_delivered(c.store.q("SELECT * FROM helper_units WHERE unit_id=?", (uid,)),
                             t["session_id"])
    return uid


def test_use_is_measured_from_the_consumers_commits_not_its_report(env):
    c, root = env["c"], env["root"]
    P, t = c.projects["game"], c.store.task("game", "T2")
    used = _delivered(c, P, t, ["src/menu.luau"])
    unused = _delivered(c, P, t, ["src/tests/menu.spec.luau"])
    diff_only = _delivered(c, P, t, [], result="--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1 +1 @@\n")
    row = c.store.q("SELECT consumer, feedback_detail FROM helper_units WHERE unit_id=?",
                    (used,))[0]
    assert row["consumer"] == f"primary:{t['session_id']}"
    assert json.loads(row["feedback_detail"])["delivered_head"] == P.ws.head()
    assert c.helpers.measure_git(P, "T2") == []        # nothing committed yet, not final
    (root / "src" / "menu.luau").write_text("local M = {OK=1}\nreturn M\n")
    git(root, "commit", "-qam", "T2: menu")
    # the primary CLAIMS it integrated the unused one: the claim is kept, git decides
    c.store.x("UPDATE helper_units SET feedback='integrated' WHERE unit_id=?", (unused,))
    assert set(c.helpers.measure_git(P, "T2")) == {used, diff_only}
    assert set(c.helpers.measure_git(P, "T2", final=True)) == {unused}
    got = {r["unit_id"]: r for r in c.store.q("SELECT unit_id, git_used, feedback, git_detail "
                                              "FROM helper_units")}
    assert got[used]["git_used"] == 1 and got[diff_only]["git_used"] == 1
    assert got[unused]["git_used"] == 0 and got[unused]["feedback"] == "integrated"
    assert "no commit after delivery touched" in json.loads(got[unused]["git_detail"])["why"]
    kinds = [r["kind"] for r in c.store.q("SELECT kind FROM events WHERE kind LIKE 'helper_git%'")]
    assert sorted(kinds) == ["helper_git_unused", "helper_git_used", "helper_git_used"]


def test_a_lane_whose_results_go_unused_turns_itself_off(env):
    c = env["c"]
    P, t = c.projects["game"], c.store.task("game", "T2")
    ls = L.settings(P, "T2")
    ls["lanes"]["failure_triage"]["max_per_task"] = 10
    for k in ("x", "y"):
        uid, how = c.helpers.lanes.add(P, t, "failure_triage", k, {}, ls)
        assert how == "created"
        c.store.x("UPDATE helper_units SET status='delivered', git_used=0, delivered_ts=? "
                  "WHERE unit_id=?", (c.clock(), uid))
    assert c.helpers.lanes.add(P, t, "failure_triage", "z", {}, ls) == (None, "limit")
    ev = [json.loads(r["detail"]) for r in c.store.q("SELECT detail FROM events WHERE "
                                                     "kind='lane_limit'")]
    assert any("not used by any commit" in (d.get("reason") or "") for d in ev)


def test_request_guidance_asks_for_a_declared_consumer(env):
    P = env["c"].projects["game"]
    text = env["c"].helpers.request_guidance(P)
    assert '"consumer"' in text and "measured by whether a later commit" in text
