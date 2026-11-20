"""v0.181.217: the coordinator protocol suite.

A model that codes well and ends its turn without a valid coordinator-report
still stalls the sprint. This suite grades the end of a turn exactly, with the
coordinator's own rules - copied into the router, which does not ship the
coordinator package, and held to the originals here.
"""
import asyncio
import json

import pytest

from foundry_router import bench, benchcard, protocolbench as pb
from foundry_router.db import Database

F = "```"


def _rep(d, before="", after=""):
    return f"{before}{F}coordinator-report\n{json.dumps(d)}\n{F}{after}"


# -- the copy is the coordinator's -------------------------------------------------------

SAMPLES = [
    _rep({"status": "DONE", "summary": "s"}),
    _rep({"status": "blocked", "blocker_kind": "Harness", "blocker": "b"}),
    _rep({"status": "NEEDS_HUMAN", "blocker": "sign in"}),
    _rep({"status": "BLOCKED", "blocker_kind": "owner"}),
    f"{F}coordinator-report\n{{not json}}\n{F}",
    "no block at all",
    _rep({"status": "DONE"}) + "\n" + _rep({"status": "NOT DONE", "next": "n"}),
    f"{F}SCOPE_REQUEST\n" + json.dumps({"path": "tests/harness/rows.lua", "reason": "row"})
    + f"\n{F}\n" + f"{F}SCOPE_REQUEST\n" + json.dumps([{"path": "../etc/passwd", "reason": "x"},
                                                       {"path": "C:/x", "reason": "y"}]) + f"\n{F}",
]


def test_the_copy_matches_the_coordinator():
    from foundry_coordinator import scope
    from foundry_coordinator.adapters import base
    for t in SAMPLES:
        assert pb.parse_report(t) == base.parse_report(t), t
        r = base.parse_report(t)
        assert pb.blocker_kind(r) == base.blocker_kind(r), t
        assert pb.report_status(r) == base.report_status(r), t
        assert pb.parse_scope_requests(t, r) == scope.parse_requests(t, r), t
    assert pb.REPORT_FENCE == base.REPORT_FENCE and pb.BLOCKER_KINDS == base.BLOCKER_KINDS
    assert pb.SCOPE_FENCE == scope.FENCE
    # what a session is told: the coordinator's text, which then adds memory notes
    assert base.report_instructions().startswith(pb.report_instructions())
    assert pb.report_pointer() == base.report_pointer()


# -- the scenarios ----------------------------------------------------------------------

def test_every_outcome_the_protocol_has_is_asked_for():
    cases = pb.scenario_cases()
    assert len(cases) == 18
    wants = {next(t for t in c["tags"] if t.startswith("want:")) for c in cases}
    assert wants == {"want:done", "want:not_done", "want:product", "want:tooling",
                     "want:user_action", "want:scope"}
    for c in cases:
        assert c["tools"] == [], "the end of a turn: nothing to call"
        assert pb.REPORT_FENCE in c["messages"][0]["content"]
        assert c["messages"][1]["content"].endswith(pb.report_pointer())
        assert any(t.startswith("tier:") for t in c["tags"])


# -- the grading --------------------------------------------------------------------------

GOOD_PRODUCT = _rep({"status": "BLOCKED", "summary": "minimap orientation", "blocker": "unsettled",
                     "blocker_kind": "product", "options": ["rotate", "north-up"],
                     "recommended": "north-up"})


@pytest.mark.parametrize("text,want,score,failed", [
    (_rep({"status": "DONE", "summary": "done"}), {"status": "DONE"}, 100.0, []),
    (GOOD_PRODUCT, {"status": "BLOCKED", "kind": "product"}, 100.0, []),
    ("I finished everything.", {"status": "DONE"}, 0.0, ["report"]),
    (f"{F}coordinator-report\n{{status: DONE}}\n{F}", {"status": "DONE"}, None, ["parses"]),
    (_rep({"status": "DONE", "summary": "d"}, after="\n\nAnything else?"), {"status": "DONE"},
     None, ["last"]),
    (_rep({"status": "DONE", "summary": "d"}), {"status": "NOT DONE", "next": True}, None,
     ["status"]),
    (_rep({"status": "BLOCKED", "summary": "s", "blocker": "b", "blocker_kind": "product"}),
     {"status": "BLOCKED", "kind": "product"}, None, ["options"]),
    (_rep({"status": "BLOCKED", "summary": "s", "blocker": "rojo", "blocker_kind": "tooling",
           "options": ["a", "b"]}), {"status": "BLOCKED", "kind": "tooling"}, None, ["options"]),
    (_rep({"status": "BLOCKED", "summary": "s", "blocker": "sign in", "blocker_kind": "tooling"}),
     {"status": "BLOCKED", "kind": "user_action"}, None, ["kind"]),
    (_rep({"status": "NOT DONE", "summary": "s", "next": "n"}),
     {"status": "NOT DONE", "next": True, "scope": True}, None, ["scope"]),
])
def test_each_rule_is_checked(text, want, score, failed):
    s = pb.protocol_score(text, want)
    if score is not None:
        assert s["score"] == score, s
    for f in failed:
        assert f in s["failed"], s
    if not failed:
        assert s["failed"] == []


def test_a_scope_request_and_memory_lines_may_follow_the_report():
    text = (_rep({"status": "NOT DONE", "summary": "needs a row", "next": "add it"})
            + f"\n{F}SCOPE_REQUEST\n" + json.dumps({"path": "tests/harness/rows.lua",
                                                     "reason": "new gate row"}) + f"\n{F}\n"
            + 'MEMORY_NOTE: file=DECISIONS.md heading="Shop" text="Keys are coins_v2."')
    s = pb.protocol_score(text, {"status": "NOT DONE", "next": True, "scope": True})
    assert s["score"] == 100.0, s


def test_a_placeholder_option_is_not_an_option():
    t = _rep({"status": "BLOCKED", "summary": "s", "blocker": "b", "blocker_kind": "product",
              "options": ["<product only: 2-4 concrete choices>"], "recommended": "x"})
    assert "options" in pb.protocol_score(t, {"status": "BLOCKED", "kind": "product"})["failed"]


def test_score_one_grades_protocol_cases_and_says_when_it_reached_for_a_tool():
    case = {"reference": {"protocol": {"status": "DONE"}}}
    svc = type("S", (), {})()
    s = asyncio.run(bench.score_one(svc, case, {"content": "", "tool_calls": [{"name": "bash"}]}))
    assert s["score"] == 0.0 and "reached for bash" in s["code_detail"]
    s = asyncio.run(bench.score_one(svc, case, {"content": _rep({"status": "DONE", "summary": "x"})}))
    assert s["score"] == 100.0
    assert bench.expected_of({"reference": {"protocol": {"status": "BLOCKED", "kind": "product"}}}) \
        .startswith("end with a coordinator-report: status BLOCKED, blocker_kind product")


# -- real coordinator turns ----------------------------------------------------------------

def test_real_turns_are_keyed_on_the_verified_sessions_own_report(tmp_path, monkeypatch):
    from foundry_router import training_prep
    store = tmp_path / "store"
    store.mkdir()
    (store / "normalized.sqlite").write_text("")
    monkeypatch.setattr(training_prep, "_dirs", lambda svc: {"store": store})
    import foundry_training.store as fts
    monkeypatch.setattr(fts, "TrainingStore", lambda *a, **k: object())
    ex = {"messages": [
        {"role": "system", "content": "you are kilo"},
        {"role": "user", "content": "Continue task VR-MAP2 ... " + pb.report_pointer()},
        {"role": "assistant", "content": "Gate passed.\n\n" + _rep(
            {"status": "DONE", "summary": "gate PASS", "next": ""})},
        {"role": "user", "content": "Continue task VR-MAP2 again"},
        {"role": "assistant", "content": _rep({"status": "BLOCKED", "summary": "s",
                                               "blocker": "rojo", "blocker_kind": "harness"})}],
          "meta": {"task_id": "VR-MAP2", "tags": ["luau"]}}
    monkeypatch.setattr(bench, "_examples", lambda *a, **k: iter([ex]))
    cases = pb.real_cases(type("S", (), {})(), limit=5)
    assert len(cases) == 1, "one per conversation, the last report in it"
    c = cases[0]
    assert c["reference"]["protocol"] == {"status": "BLOCKED", "kind": "tooling"}
    assert c["tools"] == [] and c["messages"][-1]["role"] == "user"
    assert "real" in c["tags"] and "want:tooling" in c["tags"]


def test_the_suite_builds_with_or_without_a_corpus(tmp_path):
    svc = type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                         "db": Database(tmp_path / "p.sqlite")})()
    out = pb.build_protocol(svc, real=5)
    assert out["scenarios"] == 18 and out["real"] == 0 and out["cases"] == 18
    assert len(bench.cases(svc, "protocol")) == 18


# -- the card -----------------------------------------------------------------------------

def test_the_card_names_the_suite_and_how_it_missed():
    row = {"score": 40.0, "error": "", "tags": '["protocol","want:product"]',
           "code_detail": "failed=options,kind status=BLOCKED kind=tooling",
           "tools_expected": "[]", "tools_called": "[]"}
    assert benchcard.miss_kind("protocol", row) == "options rule broken"
    rows = [dict(row, score=100.0, tags='["protocol","scenario","want:done"]'), row]
    names = [f["name"] for f in benchcard.facets("protocol", rows)]
    assert "What the turn had to say" in names and "Source" in names
    assert benchcard.SUITE_NAMES["protocol"] == "Coordinator protocol"


def test_the_route_and_the_button_exist():
    from pathlib import Path

    import foundry_router
    from foundry_router.ui import routes
    assert "/admin/api/bench/protocol" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert 'id="bench-protocol"' in page and 'api("bench/protocol"' in page
