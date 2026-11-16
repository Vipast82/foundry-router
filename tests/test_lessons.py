"""v0.181.169: lessons — the mistakes that recur, grouped by meaning, offered as
rules the operator approves before any of them reaches a prompt."""
import json
from types import SimpleNamespace

import pytest

from foundry_router import lessons
from foundry_router.config import LessonsConfig
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult


# -- reading the findings -----------------------------------------------------------------

class Store:
    """A stand-in prepared store: events by type, facts already unpacked."""

    def __init__(self, events):
        self._events = events

    def projects(self):
        return ["au"]

    def events(self, pid, where="", args=()):
        kind = where.split("event_type='")[1].split("'")[0] if "event_type='" in where else ""
        fail = "verification_status='FAIL'" in where
        out = [e for e in self._events if e["event_type"] == kind]
        if fail:
            out = [e for e in out if e.get("verification_status") == "FAIL"]
        return out


def _ev(kind, facts, task="UI-7", ts=100.0, status=""):
    return {"event_type": kind, "facts": facts, "task_id": task, "timestamp": ts,
            "verification_status": status}


LONG = "keyboard key badges are drawn at the touch sizes where there is no keyboard"


def test_a_finding_comes_from_every_place_that_says_something_is_wrong():
    store = Store([
        _ev("verification", {"check": "au.done_check.GATE", "detail": LONG}, status="FAIL"),
        _ev("verification", {"check": "ok", "detail": LONG}, status="PASS"),
        _ev("review", {"verdict": "CHANGES", "result": {"minor": [{"text": LONG}]}}),
        _ev("review", {"verdict": "APPROVED", "result": {"minor": [{"text": LONG}]}}),
        _ev("human_correction", {"findings": [{"text": LONG}]}),
        _ev("label", {"label": "gate_verdict", "failed": [{"name": "TEST", "detail": LONG}]}),
        _ev("label", {"label": "vision_verdict", "verdict": "CHANGES",
                      "findings": [{"text": LONG}]}),
        _ev("label", {"label": "vision_verdict", "verdict": "APPROVED",
                      "findings": [{"text": LONG}]}),
    ])
    got = lessons.findings(store, "au")
    assert sorted({f["source"] for f in got}) == ["check", "gate", "human", "review", "vision"]
    assert all(LONG in f["text"] for f in got)                 # a passing check says nothing


def test_a_finding_too_short_to_act_on_is_dropped():
    store = Store([_ev("human_correction", {"findings": ["no", "broken", LONG]})])
    assert [f["text"] for f in lessons.findings(store, "au")] == [LONG]


def test_a_review_that_is_a_json_string_is_still_read():
    store = Store([_ev("review", {"verdict": "CHANGES",
                                  "result": json.dumps({"blockers": [LONG]})})])
    got = lessons.findings(store, "au")
    assert len(got) == 1 and got[0]["severity"] == "blockers"


def test_the_same_finding_twice_is_one_finding():
    store = Store([_ev("human_correction", {"findings": [LONG]}),
                   _ev("human_correction", {"findings": [LONG]})])
    got = lessons.findings(store, "au")
    assert len({f["id"] for f in got}) == 1                    # same source, task and text
    other = lessons.findings(Store([_ev("human_correction", {"findings": [LONG]},
                                       task="UI-8")]), "au")
    assert other[0]["id"] != got[0]["id"]                      # a different task is its own


# -- grouping by meaning ------------------------------------------------------------------

def _vec(*v):
    from foundry_router.semcache import _normalize
    return _normalize(list(v))


def _row(i, vec, task="UI-7", ts=1.0, text="a finding long enough to be worth a rule here"):
    return {"id": f"f{i}", "vec": vec, "task_id": task, "ts": ts, "text": text,
            "source": "review", "severity": "minor"}


def test_findings_that_mean_the_same_thing_land_in_one_group():
    rows = [_row(1, _vec(1, 0, 0)), _row(2, _vec(0.97, 0.24, 0)), _row(3, _vec(0, 1, 0))]
    groups = lessons.cluster(rows, 0.8)
    assert len(groups) == 2
    big = max(groups, key=lambda c: len(c["members"]))
    assert [m["id"] for m in big["members"]] == ["f1", "f2"]
    assert big["anchor"] == "f1"                               # named after the first in


def test_a_tighter_bar_splits_them():
    rows = [_row(1, _vec(1, 0, 0)), _row(2, _vec(0.97, 0.24, 0))]
    assert len(lessons.cluster(rows, 0.99)) == 2


def test_nothing_in_nothing_out():
    assert lessons.cluster([], 0.8) == []


# -- candidates and the operator's decision -----------------------------------------------

def _svc(tmp_path, cfg=None, reply=None):
    db = Database(tmp_path / "l.sqlite")

    class Pool:
        def __init__(self):
            self.asked = []

        async def chat(self, model, messages, max_tokens=4096, **kw):
            self.asked.append({"model": model, "messages": messages})
            if isinstance(reply, Exception):
                raise reply
            return (reply or ChatResult(content="Keep key badges out of the touch frames.")), \
                model + "-be"

    return SimpleNamespace(
        db=db, pool=Pool(), capture=SimpleNamespace(data_dir=tmp_path),
        semcache=SimpleNamespace(embed=None, _vec_loaded=False),
        config_store=SimpleNamespace(config=SimpleNamespace(lessons=cfg or LessonsConfig())))


def _candidate(svc, anchor="f1", seen=4, tasks=("UI-7", "UI-8"), examples=(LONG,)):
    svc.db.execute("INSERT INTO lessons (ts, anchor, rule, draft, seen, tasks, sources, "
                   "examples, first_seen, last_seen, state) "
                   "VALUES ('now',?,'','',?,?,'[\"review\"]',?,'a','b','proposed')",
                   (anchor, seen, json.dumps(list(tasks)), json.dumps(list(examples))))
    return svc.db.query_one("SELECT id FROM lessons WHERE anchor=?", (anchor,))["id"]


def test_a_cluster_becomes_a_candidate_and_a_re_mine_only_updates_the_counts(tmp_path):
    svc = _svc(tmp_path)
    c = {"anchor": "f1", "members": [_row(1, None, "UI-7", 10.0), _row(2, None, "UI-8", 20.0)]}
    assert lessons._upsert(svc.db, c) == "new"
    lessons.decide(svc, 1, "active", "Keep key badges out of the touch frames.")
    c["members"].append(_row(3, None, "UI-9", 30.0))
    assert lessons._upsert(svc.db, c) == "active"               # the decision is untouched
    row = svc.db.query_one("SELECT * FROM lessons WHERE anchor='f1'")
    assert row["seen"] == 3 and json.loads(row["tasks"]) == ["UI-7", "UI-8", "UI-9"]
    assert row["rule"] == "Keep key badges out of the touch frames."


def test_a_rule_is_never_active_without_wording(tmp_path):
    svc = _svc(tmp_path)
    lid = _candidate(svc)
    with pytest.raises(ValueError, match="write the rule"):
        lessons.decide(svc, lid, "active")
    assert lessons.block(svc.db) == ""                         # and nothing reaches a prompt


def test_rejecting_and_undeciding(tmp_path):
    svc = _svc(tmp_path)
    lid = _candidate(svc)
    assert lessons.decide(svc, lid, "rejected")["state"] == "rejected"
    assert lessons.status(svc)["rejected"] == 1
    assert lessons.decide(svc, lid, "proposed")["state"] == "proposed"
    with pytest.raises(ValueError, match="proposed, active or rejected"):
        lessons.decide(svc, lid, "maybe")
    with pytest.raises(ValueError, match="no such lesson"):
        lessons.decide(svc, 999, "rejected")
    assert lessons.forget(svc, lid) == 1


# -- the wording --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_model_may_suggest_the_wording_but_it_is_only_a_suggestion(tmp_path):
    svc = _svc(tmp_path, cfg=LessonsConfig(draft_model="qwen"))
    lid = _candidate(svc)
    out = await lessons.draft(svc, lid)
    assert out["ok"] and out["draft"] == "Keep key badges out of the touch frames."
    row = svc.db.query_one("SELECT rule, draft, state FROM lessons WHERE id=?", (lid,))
    assert row["draft"] == out["draft"]
    assert row["rule"] == "" and row["state"] == "proposed"    # still waiting on a person
    assert LONG in svc.pool.asked[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_fable_class_model_is_never_asked(tmp_path):
    svc = _svc(tmp_path, cfg=LessonsConfig(draft_model="claude-fable-5-2"))
    lid = _candidate(svc)
    out = await lessons.draft(svc, lid)
    assert out["ok"] is False and "never used as a helper or a judge" in out["error"]
    assert svc.pool.asked == []
    assert (await lessons.draft(svc, lid, model="mythos-1"))["ok"] is False


@pytest.mark.asyncio
async def test_drafting_says_what_is_missing(tmp_path):
    svc = _svc(tmp_path)
    lid = _candidate(svc)
    assert "no drafting model" in (await lessons.draft(svc, lid))["error"]
    svc.config_store.config.lessons = LessonsConfig(draft_model="qwen")
    assert "no such lesson" in (await lessons.draft(svc, 999))["error"]
    empty = _candidate(svc, anchor="f2", examples=())
    assert "no findings behind it" in (await lessons.draft(svc, empty))["error"]


@pytest.mark.asyncio
async def test_a_model_that_answers_with_nothing_changes_nothing(tmp_path):
    svc = _svc(tmp_path, cfg=LessonsConfig(draft_model="qwen"), reply=ChatResult(content="   "))
    lid = _candidate(svc)
    out = await lessons.draft(svc, lid)
    assert out["ok"] is False and "nothing to use" in out["error"]
    assert svc.db.query_one("SELECT draft FROM lessons WHERE id=?", (lid,))["draft"] == ""


# -- what reaches the model ---------------------------------------------------------------

def test_the_rulebook_is_the_approved_rules_most_seen_first(tmp_path):
    svc = _svc(tmp_path)
    for i, (anchor, seen, rule) in enumerate([
            ("a", 9, "Tick the plan checklist in the same commit as the work."),
            ("b", 4, "Keep key badges out of the touch frames."),
            ("c", 7, "Close the sprint through the gate script so the PASS line lands.")]):
        lid = _candidate(svc, anchor=anchor, seen=seen)
        lessons.decide(svc, lid, "active", rule)
    _candidate(svc, anchor="d")                                # proposed: not in the prompt
    text = lessons.block(svc.db)
    assert "learned the hard way" in text and "approved as a rule by the operator" in text
    assert text.index("Tick the plan") < text.index("Close the sprint") < text.index("Keep key")
    assert "1." in text and "3." in text and "4." not in text


def test_the_rulebook_never_takes_more_prompt_than_it_may(tmp_path):
    svc = _svc(tmp_path)
    for i in range(10):
        lid = _candidate(svc, anchor=f"a{i}")
        lessons.decide(svc, lid, "active", f"Rule number {i} " + "x" * 120)
    short = lessons.block(svc.db, max_chars=400)
    assert len(short) < 600 and short.count("\n") < 5
    assert len(lessons.block(svc.db, max_chars=4000)) > len(short)


def test_a_persona_opts_in():
    on = LessonsConfig(enabled=True)
    assert lessons.on_for(on, {"lessons": 1}) is True
    assert lessons.on_for(on, {"lessons": 0}) is False
    assert lessons.on_for(on, {}) is False
    assert lessons.on_for(LessonsConfig(enabled=False), {"lessons": 1}) is False
    assert lessons.on_for(None, {"lessons": 1}) is False


# -- mining -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mining_with_no_prepared_store_says_so(tmp_path):
    svc = _svc(tmp_path)
    logged = []
    out = await lessons.mine(svc, log_fn=logged.append)
    assert out["state"] == "failed" and "prepared store is empty" in out["error"]
    assert any("prepared store is empty" in m for m in logged)


@pytest.mark.asyncio
async def test_mining_embeds_what_is_new_clusters_it_and_waits_for_you(tmp_path, monkeypatch):
    svc = _svc(tmp_path, cfg=LessonsConfig(min_seen=2, min_tasks=2))
    calls = []

    async def embed(text):
        calls.append(text)
        from foundry_router.semcache import _normalize
        return _normalize([1.0, 0.0] if "touch" in text else [0.0, 1.0])
    svc.semcache.embed = embed
    rows = [{"id": "f1", "text": "key badges at the touch sizes, no keyboard there",
             "source": "review", "severity": "minor", "task_id": "UI-7", "ts": 10.0},
            {"id": "f2", "text": "settings shows key bindings at the touch sizes as well",
             "source": "review", "severity": "minor", "task_id": "UI-8", "ts": 20.0},
            {"id": "f3", "text": "the evidence log has no GATE PASS line in it at all",
             "source": "gate", "severity": "", "task_id": "UI-9", "ts": 30.0}]
    monkeypatch.setattr(lessons, "collect", lambda svc, limit, log_fn: list(rows))
    out = await lessons.mine(svc, log_fn=lambda m: None)
    assert out["state"] == "done" and out["embedded"] == 3 and out["clusters"] == 2
    assert out["new"] == 1                          # only the pair bit two separate tasks
    row = svc.db.query_one("SELECT * FROM lessons")
    assert row["state"] == "proposed" and row["seen"] == 2 and row["rule"] == ""
    assert lessons.block(svc.db) == ""              # nothing is applied on its own
    # a second pass embeds nothing again
    out2 = await lessons.mine(svc, log_fn=lambda m: None)
    assert out2["embedded"] == 0 and out2["new"] == 0 and len(calls) == 3


@pytest.mark.asyncio
async def test_mining_stops_when_the_embedder_is_down(tmp_path, monkeypatch):
    svc = _svc(tmp_path)

    async def embed(text):
        return None
    svc.semcache.embed = embed
    monkeypatch.setattr(lessons, "collect", lambda svc, limit, log_fn: [
        {"id": "f1", "text": "x" * 60, "source": "review", "severity": "", "task_id": "t",
         "ts": 1.0}])
    out = await lessons.mine(svc, log_fn=lambda m: None)
    assert out["state"] == "failed" and "embedding endpoint is not answering" in out["error"]


def test_the_status_reports_the_settings(tmp_path):
    svc = _svc(tmp_path, cfg=LessonsConfig(enabled=True, min_seen=5, draft_model="qwen"))
    st = lessons.status(svc)
    assert st["enabled"] is True and st["min_seen"] == 5 and st["draft_model"] == "qwen"
    assert st["proposed"] == 0 and st["active"] == 0 and st["findings_cached"] == 0


def test_a_scheduled_pass_is_only_due_when_it_is_switched_on(tmp_path):
    svc = _svc(tmp_path, cfg=LessonsConfig(enabled=False, mine_hours=24))
    lessons._state.clear()
    lessons._state.update({"state": "idle"})
    assert lessons.due(svc) is False
    svc.config_store.config.lessons = LessonsConfig(enabled=True, mine_hours=0)
    assert lessons.due(svc) is False                 # 0 = only when the button is pressed
    svc.config_store.config.lessons = LessonsConfig(enabled=True, mine_hours=24)
    assert lessons.due(svc) is True
    lessons._state["finished"] = __import__("time").time()
    assert lessons.due(svc) is False                 # just looked
