"""v0.181.189: a rule reaches only the work it names.

Seven approved rules sat in every prompt of a persona that runs code sprints as
well as asset sprints, because the rulebook had no notion of which kind of work
a request was. The ten-run A/B that asked whether that costs anything came back
null (-0.18 per case, p=0.83) on a suite that turned out to be 55 of 60 asset
cases scored on tool calls - so the cost is unmeasured, not disproved. What is
not in doubt is the budget: seven rules take 1862 of 2000 characters and the
queue holds 168 more candidates, so without scoping the eighth approval starts
evicting rules by finding-count across unrelated work.
"""
import json

import pytest

from foundry_router import lessons
from foundry_router.db import Database


class _Cfg:
    def __init__(self, max_chars=1500):
        self.lessons = type("L", (), {"max_chars": max_chars, "enabled": True})()


class _Svc:
    def __init__(self, tmp_path, max_chars=1500):
        self.db = Database(tmp_path / "s.sqlite")
        self.config_store = type("S", (), {"config": _Cfg(max_chars)})()

    def __getattr__(self, n):
        raise AttributeError(n)


def _cand(svc, anchor, tasks=("VQ-17",), seen=5):
    svc.db.execute(
        "INSERT INTO lessons (ts, anchor, rule, draft, seen, tasks, sources, examples, "
        "first_seen, last_seen, state) VALUES ('now',?,'','',?,?,'[\"human\"]','[\"x\"]',"
        "'a','b','proposed')", (anchor, seen, json.dumps(list(tasks))))
    return svc.db.query_one("SELECT id FROM lessons WHERE anchor=?", (anchor,))["id"]


# -- what counts as a family, and what the miner proposes ---------------------------

@pytest.mark.parametrize("task,family", [
    ("VR-AS09", "VR-AS"),        # letters, then letters+digits: the pair is the family
    ("VR-AS17", "VR-AS"),
    ("VQ-14b", "VQ"),
    ("POL-7a", "POL"),
    ("WIRE-2", "WIRE"),
    ("LOB-1b", "LOB"),
    ("BS-10a", "BS"),
])
def test_the_family_is_the_letters_that_name_the_kind_of_work(task, family):
    assert lessons._family(task) == family


def test_a_scope_is_proposed_from_one_or_two_families_and_not_from_more():
    assert lessons.scope_from(["VQ-17", "VR-AS09", "VQ-14b"]) == "VQ*,VR-AS*"
    assert lessons.scope_from(["WIRE-1", "WIRE-2"]) == "WIRE*"
    # spread across unrelated work is not about one kind of work: everywhere
    assert lessons.scope_from(["VQ-1", "POL-7", "FIN-2b"]) == ""
    assert lessons.scope_from([]) == ""


def test_the_pattern_carries_no_separator_because_the_ids_disagree():
    """VQ-17 puts a dash after the family and VR-AS09 does not. A pattern with a
    dash in it matched half the ids it should, which the first version did."""
    s = lessons.scope_from(["VQ-17", "VR-AS09"])
    assert lessons.in_scope(s, "Sprint VQ-17: build EN-WOLF")
    assert lessons.in_scope(s, "task VR-AS09 asset pass")


# -- matching ----------------------------------------------------------------------

def test_an_empty_scope_is_everywhere():
    assert lessons.in_scope("", "anything at all") is True
    assert lessons.in_scope(None, "anything at all") is True


def test_a_scope_keeps_a_rule_out_of_other_work():
    s = "VQ*,VR-AS*"
    assert lessons.in_scope(s, "Sprint LOB-1b: fix the loader") is False
    assert lessons.in_scope(s, "POL-7 audit sweep") is False


def test_a_pattern_does_not_match_inside_another_word():
    assert lessons.in_scope("VQ*", "a REVQ-17 reference") is False
    assert lessons.in_scope("POL-7", "close POL-71 now") is False      # exact, no star
    assert lessons.in_scope("POL-7", "close POL-7 now") is True


def test_the_request_text_is_the_brief_not_the_whole_conversation():
    msgs = [{"role": "system", "content": "you build models"},
            {"role": "user", "content": "Sprint VQ-17: build EN-WOLF"},
            {"role": "assistant", "content": "x" * 50_000},
            {"role": "user", "content": "LOB-1b now"}]
    text = lessons.request_text(msgs)
    assert "VQ-17" in text and "you build models" in text
    assert "LOB-1b" not in text                      # a later turn is not the brief
    assert len(text) <= lessons.SCOPE_CAP


# -- the rulebook ------------------------------------------------------------------

def test_only_the_rules_that_name_this_work_go_in(tmp_path):
    svc = _Svc(tmp_path)
    art = _cand(svc, "art", seen=9)
    lessons.decide(svc, art, "active", "Build the tail to the reference.", scope="VQ*,VR-AS*")
    any_ = _cand(svc, "any", seen=8)
    lessons.decide(svc, any_, "active", "Tick the plan in the same commit.")
    text, dropped, held = lessons.rulebook(svc.db, 2000, "Sprint LOB-1b: fix the loader")
    assert "Tick the plan" in text and "Build the tail" not in text
    assert held == [art] and dropped == []
    # numbering is contiguous even though a rule was held back
    assert "1. Tick the plan" in text

    text, _d, held = lessons.rulebook(svc.db, 2000, "Sprint VQ-17: build EN-WOLF")
    assert "Build the tail" in text and "Tick the plan" in text and held == []


def test_every_rule_when_no_request_is_given(tmp_path):
    """text=None is the GUI's and the benchmark's view: the whole rulebook,
    regardless of scope, so a count shown to the operator is not about one
    request and the benchmark arm does not drift with the queue."""
    svc = _Svc(tmp_path)
    lid = _cand(svc, "art")
    lessons.decide(svc, lid, "active", "Build the tail.", scope="VQ*")
    text, _d, held = lessons.rulebook(svc.db, 2000, None)
    assert "Build the tail" in text and held == []


def test_a_scope_survives_a_later_decision_unless_it_is_given(tmp_path):
    svc = _Svc(tmp_path)
    lid = _cand(svc, "art")
    lessons.decide(svc, lid, "active", "Build the tail.", scope="VQ*")
    lessons.decide(svc, lid, "proposed")                  # undecide: scope kept
    assert svc.db.query_one("SELECT scope FROM lessons WHERE id=?", (lid,))["scope"] == "VQ*"
    out = lessons.decide(svc, lid, "active", "Build the tail.", scope="")
    assert out["scope"] == "" and lessons.in_scope("", "anything")


def test_the_miner_proposes_a_scope_on_a_new_candidate(tmp_path):
    svc = _Svc(tmp_path)
    c = {"anchor": "f1", "members": [
        {"id": 1, "text": "t", "task_id": "VQ-17", "ts": 10.0, "source": "human"},
        {"id": 2, "text": "t", "task_id": "VR-AS09", "ts": 20.0, "source": "human"}]}
    assert lessons._upsert(svc.db, c) == "new"
    assert svc.db.query_one("SELECT scope FROM lessons WHERE anchor='f1'")["scope"] == "VQ*,VR-AS*"
