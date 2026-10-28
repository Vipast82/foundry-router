"""v0.153.0 "capture everything" on the pipeline side: other-purpose calls never
build an agent's episode, a session that stopped on an unanswered tool call is
trimmed, reasoning survives when include_reasoning is on, `no-system-prompt`
gaps only for sessions no Foundry capture covers (item F), failed attempts are
labelled negatives (outcome.attempt_end) and the coordinator queues them, the
call-level dataset (verified / ok), unbound captures in a pseudo-project, and
preference pairs from router labels."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_coordinator import training_hook as H
from foundry_coordinator.engine import Coordinator as Engine
from foundry_training.adapters import foundry as FA
from foundry_training.export import export
from tests.training.helpers import (LOCAL, SESSION, capture_db, capture_rows, clone_session,
                                    coordinator, kilo_export, pipeline, profile, workspace,
                                    write_json)

PID = "fixture-game"


@pytest.fixture()
def world(tmp_path):
    ws, base, head = workspace(tmp_path)
    return tmp_path, ws, base, head


def _prof(tmp, ws, extra="", unbound=""):
    path = profile(tmp, PID, ws, extra=extra)
    if unbound:
        text = Path(path).read_text()
        text = text.replace('artifact_roots = ["docs/evidence"]',
                            f'artifact_roots = ["docs/evidence"]\nunbound_project = "{unbound}"')
        Path(path).write_text(text)
    return path


def _episode(p, kind="trajectory", session=SESSION):
    for r in p.store.cur.q("SELECT * FROM episodes WHERE project_id=? AND kind=?", (PID, kind)):
        d = json.loads(r["data"])
        if d.get("session_id") == session:
            return {**r, "data": d, "reasons": json.loads(r["reasons"] or "[]")}
    raise AssertionError(f"no {kind} episode for {session}")


def _lines(m, p):
    path = p.store.root / "datasets" / m["dataset_id"] / "data.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _with(rows, i, **body_resp):
    """rows[i] (a backend_call) with its response fields replaced."""
    r = dict(rows[i])
    b = json.loads(r["body"])
    b["response"].update(body_resp)
    r["body"] = json.dumps(b)
    return r


def _ingest_rows(p, rows, quality=None):
    """Capture rows as GET /v1/foundry/capture/records serves them (with the
    router's `quality` join per request id)."""
    out = []
    for i, r in enumerate(rows, 1):
        d = dict(r, id=i, body=json.loads(r["body"]), redaction=json.loads(r["redaction"]))
        if quality and d["kind"] == "backend_call":
            d["quality"] = quality(d)
        out.append(d)
    return p.ing.run("foundry:test", p._filter(FA.records(out, None, p.unbound_project),
                                               "foundry"), FA.normalize)


# ------------------------------------------------------------------ purposes, tail, reasoning

def test_other_purpose_calls_in_the_session_never_build_its_episode(world):
    tmp, ws, base, head = world
    rows = capture_rows(kilo_export())
    helper = _with(rows, -1, content="frontier advice", tool_calls=[])
    helper.update(record_uid="helper-1", ts=time.time(), purpose="frontier_assist")
    b = json.loads(helper["body"])
    b["request"]["messages"] = [{"role": "system", "content": "You are a helper."},
                                {"role": "user", "content": "how do I fix it?"}]
    helper["body"] = json.dumps(b)
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [_prof(tmp, ws)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows + [helper]))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep = _episode(p)
    assert ep["data"]["context_source"] == "foundry-capture"
    assert ep["data"]["messages"][0]["content"] == "You are Kilo, a coding agent."
    assert "frontier advice" not in json.dumps(ep["data"]["messages"])
    calls = p.store.events(PID, "event_type='model_call'")
    assert {e["facts"]["purpose"] for e in calls} == {"agent_turn", "frontier_assist"}


def test_session_stopped_on_an_unanswered_call_is_trimmed_not_broken(world):
    tmp, ws, base, head = world
    rows = capture_rows(kilo_export())
    # drop the final answer; the call after the last tool call was cancelled
    calls = [i for i, r in enumerate(rows) if r["kind"] == "backend_call"]
    last = rows[calls[-1]]
    rows = rows[:calls[-1] - 1]
    cancelled = dict(last, record_uid="cancelled-1", status="cancelled",
                     reconstruction="incomplete")
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [_prof(tmp, ws)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows[:-1] + [rows[-1], cancelled]))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep = _episode(p)
    f = ep["data"]["flags"]
    assert f["trimmed_open_tail"] and f["complete_tool_sequence"] is True
    assert "broken_tool_sequence" not in ep["reasons"]
    assert ep["data"]["messages"][-1]["role"] == "tool"
    m = export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})
    # no assistant turn without open calls to end on: left out, never an invalid line
    assert m["validation"]["ok"]
    assert m["examples"] == 0 or all(l["messages"][-1]["role"] == "assistant"
                                     for l in _lines(m, p))


def _reasoning_world(world, on):
    tmp, ws, base, head = world
    rows = capture_rows(kilo_export())
    rows = [_with(rows, i, reasoning=f"thinking step {i}") if r["kind"] == "backend_call"
            else r for i, r in enumerate(rows)]
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    extra = "[training.export]\ninclude_reasoning = true\n" if on else ""
    p = pipeline(tmp, [_prof(tmp, ws, extra=extra)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows))
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    p.curate(PID)
    return p


def test_reasoning_is_kept_per_turn_and_exported_only_when_switched_on(world):
    p = _reasoning_world(world, on=False)
    ep = _episode(p)
    turns = [m for m in ep["data"]["messages"] if m["role"] == "assistant"]
    assert turns and all(m.get("reasoning", "").startswith("thinking step") for m in turns)
    assert ep["status"] == "selected", ep["reasons"]
    m = export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})
    assert m["validation"]["ok"] and m["examples"] == 1 and m["reasoning_included"] is False
    assert "reasoning" not in json.dumps(_lines(m, p))
    c = export(p.store, [PID], "sft", "canonical", {PID: p._cfg(PID)})
    assert c["validation"]["ok"] and '"reasoning":' not in json.dumps(_lines(c, p))


def test_include_reasoning_writes_reasoning_content_on_target_turns(world):
    p = _reasoning_world(world, on=True)
    m = export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})
    assert m["validation"]["ok"], m["validation"]["errors"]
    assert m["reasoning_included"] is True
    line, = _lines(m, p)
    asst = [x for x in line["messages"] if x["role"] == "assistant"]
    assert asst and all(x["reasoning_content"].startswith("thinking step") for x in asst)
    assert all("reasoning_content" not in x for x in line["messages"] if x["role"] != "assistant")


# ------------------------------------------------------------------ item F: capture gaps

def test_no_system_prompt_gap_only_for_sessions_without_a_foundry_capture(world):
    tmp, ws, base, head = world
    other = "ses_nocapture0000000000000009"
    p = pipeline(tmp, [_prof(tmp, ws)])
    p.ingest_kilo(exports=[write_json(tmp / "a.json", kilo_export()),
                           write_json(tmp / "b.json", clone_session(kilo_export(), other))])

    def gaps():
        return sorted(g["session_id"] for g in p.store.gaps(PID) if g["kind"] == "no-system-prompt")
    assert gaps() == sorted([SESSION, other])        # nothing from Foundry yet
    # Foundry is ingested after Kilo (the queue's order): the covered session's gap goes
    p.ingest_foundry(capture_db=capture_db(tmp, capture_rows(kilo_export())))
    res = p.curate(PID)
    assert gaps() == [other]
    assert res["context_gaps"] == {"missing": 1, "resolved": 1}
    # a re-run neither re-adds nor duplicates
    p.ingest_kilo(exports=[write_json(tmp / "a.json", kilo_export())])
    p.curate(PID)
    assert gaps() == [other]


# ------------------------------------------------------------------ failed attempts

def test_blocked_attempt_is_a_labelled_negative_and_a_later_success_does_not_relabel(world):
    tmp, ws, base, head = world
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head, verdict="")
    st._record(PID, "FX-1", "VERIFYING", "BLOCKED", {"blocker": "studio lock held"}, "engine")
    time.sleep(0.05)
    s2 = "ses_secondtry000000000000000007"
    second = clone_session(kilo_export(), s2, attempt=2, start_ms=int(time.time() * 1000) + 50)
    time.sleep(0.1)
    coordinator(tmp, PID, "FX-1", s2, base, head, attempt=2, store=st)
    st._record(PID, "FX-1", "CLOSING", "COMPLETE", {}, "engine")
    p = pipeline(tmp, [_prof(tmp, ws)])
    p.ingest_kilo(exports=[write_json(tmp / "a.json", kilo_export()),
                           write_json(tmp / "b.json", second)])
    p.ingest_foundry(capture_db=capture_db(tmp, capture_rows(kilo_export())))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    first, again = _episode(p), _episode(p, session=s2)
    assert first["data"]["outcome"]["attempt_end"]["state"] == "BLOCKED"
    assert "studio lock" in first["data"]["outcome"]["attempt_end"]["blocker"]
    assert "attempt_blocked" in first["reasons"] and first["status"] == "excluded"
    assert again["data"]["outcome"]["attempt_end"]["state"] == "COMPLETE"
    assert "attempt_blocked" not in again["reasons"]


def _engine(tmp, st):
    calls = []
    fake = SimpleNamespace(store=st, state_dir=str(tmp), log=lambda m: None)
    fake._training = lambda P, t, o, auto=True: (calls.append(o),
                                                 Engine._training(fake, P, t, o, auto))[1]
    return fake, calls


def test_coordinator_queues_blocked_failed_cancelled_attempts_once(world, monkeypatch):
    tmp, ws, base, head = world
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", "0")
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    spool = tmp / "spool"
    P = SimpleNamespace(id=PID, profile=SimpleNamespace(
        raw={"training": {"enabled": True, "spool": str(spool)}}, review={"required": True},
        path=""))
    fake, calls = _engine(tmp, st)
    st.update_task(PID, "FX-1", state="BLOCKED", run_id="FX-1-a1", blocker="gate red")
    st.merge_data(PID, "FX-1", recovery_case="case-1")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is False   # lead still on it
    st.merge_data(PID, "FX-1", recovery_case=None)
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is True
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is False   # once per run
    m, = [json.loads(x.read_text()) for x in spool.glob("*.json")]
    assert m["outcome"] == "BLOCKED" and m["attempt_end"]["state"] == "BLOCKED"
    assert m["attempt_end"]["blocker"] == "gate red" and m["run_id"] == "FX-1-a1"
    assert SESSION in m["sessions"]
    # the next run's stop, and a cancel, are their own manifests
    st.update_task(PID, "FX-1", run_id="FX-1-a2", state="FAILED")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is True
    st.update_task(PID, "FX-1", state="CANCELLED")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is True
    assert calls == ["BLOCKED", "FAILED", "CANCELLED"]
    assert st.task(PID, "FX-1")["data"]["training_manifests"] == [
        "FX-1-a1:BLOCKED", "FX-1-a2:FAILED", "FX-1-a2:CANCELLED"]
    # a COMPLETE task or training off: nothing
    st.update_task(PID, "FX-1", state="COMPLETE")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1")) is False
    off = SimpleNamespace(id=PID, profile=SimpleNamespace(raw={}, review={}))
    st.update_task(PID, "FX-1", state="BLOCKED", run_id="FX-1-a3")
    assert Engine._training_negative(fake, off, st.task(PID, "FX-1")) is False
    assert H.manifest_key({"attempt": 2}, "BLOCKED") == "attempt-2:BLOCKED"
    # inside a tick: manifests written, one auto run per project queued for the tick's end
    queue: dict = {}
    st.update_task(PID, "FX-1", state="BLOCKED", run_id="FX-1-a4")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1"), queue=queue) is True
    st.update_task(PID, "FX-1", state="FAILED")
    assert Engine._training_negative(fake, P, st.task(PID, "FX-1"), queue=queue) is True
    assert list(queue) == [PID] and len(list(spool.glob("*.json"))) == 5


# ------------------------------------------------------------------ the calls kind

def _q(positive=(), negative=(), outcome="ok", feedback=None):
    return {"outcome": outcome, "feedback": feedback,
            "labels": [{"label": x} for x in (*positive, *negative)]}


def test_calls_dataset_verified_and_ok_label_strengths(world):
    tmp, ws, base, head = world
    rows = capture_rows(kilo_export())
    frontier = capture_rows(kilo_export(), session="ses_frontier00000000000000001",
                            model="claude-sonnet-5-5", persona="Foundry-Kilo-Sonnet")
    for r in frontier:
        r["record_uid"] = "f-" + r["record_uid"]
    first_call = next(r["request_id"] for r in rows if r["kind"] == "backend_call")

    def quality(d):
        if d["request_id"] == first_call or d["session_id"].startswith("ses_frontier"):
            return _q(positive=["gate_pass"])
        return _q()
    p = pipeline(tmp, [_prof(tmp, ws)])
    _ingest_rows(p, rows + frontier, quality)
    cfgs = {PID: p._cfg(PID)}
    m = export(p.store, [PID], "calls", "chatml-tools", cfgs)
    assert m["validation"]["ok"], m["validation"]["errors"]
    assert m["examples"] == 1 and m["label_strength"] == ["router_verified"]
    assert m["skipped"]["call_non_local_author"] >= 1        # frontier output is never a target
    line, = _lines(m, p)
    assert line["meta"]["label_strength"] == "router_verified" and line["meta"]["kind"] == "call"
    asst = [x for x in line["messages"] if x["role"] == "assistant"]
    assert asst[-1]["weight"] == 1 and all(x["weight"] == 0 for x in asst[:-1])
    assert asst[-1].get("tool_calls")                       # the action itself is the target
    # the weaker router_ok: every local call that succeeded and nobody rated down
    cfgs[PID].export["calls_quality"] = "ok"
    n_local = sum(1 for r in rows if r["kind"] == "backend_call")
    m2 = export(p.store, [PID], "calls", "chatml-tools", cfgs)
    assert m2["validation"]["ok"] and m2["examples"] == n_local
    assert set(m2["label_strength"]) == {"router_ok", "router_verified"}
    c = export(p.store, [PID], "calls", "canonical", cfgs)
    assert c["validation"]["ok"] and c["examples"] == n_local


def test_unbound_captures_feed_the_calls_kind_of_a_pseudo_project(world):
    tmp, ws, base, head = world
    rows = capture_rows(kilo_export(), pid="", tid="", session="cline-chat-1")
    p = pipeline(tmp, [_prof(tmp, ws, unbound="foundry-unbound")])
    assert p.unbound_project == "foundry-unbound"
    assert p._cfg("foundry-unbound").sources == ["foundry"]
    _ingest_rows(p, rows, lambda d: _q())
    assert p.store.events("foundry-unbound", "event_type='model_call'")
    assert p.curate("foundry-unbound") == {"episodes": 0, "unbound": True}
    cfg = p._cfg("foundry-unbound")
    cfg.export["calls_quality"] = "ok"
    m = export(p.store, ["foundry-unbound"], "calls", "chatml-tools", {"foundry-unbound": cfg})
    assert m["validation"]["ok"] and m["examples"] == sum(
        1 for r in rows if r["kind"] == "backend_call")
    # without the setting, unbound rows are still skipped
    q = pipeline(tmp / "other", [_prof(tmp / "other", ws)])
    assert _ingest_rows(q, rows)["skipped"] == len(rows)


def test_router_labels_on_the_same_context_make_a_preference_pair(world):
    tmp, ws, base, head = world
    a = capture_rows(kilo_export())
    b = capture_rows(kilo_export(), session="ses_otherattempt000000000000002")
    for r in b:
        r["record_uid"] = "b-" + r["record_uid"]
    b = [_with(b, i, content="", tool_calls=[{"id": "x1", "function": {
        "name": "bash", "arguments": {"command": "rm -rf build"}}}])
         if i == 1 else r for i, r in enumerate(b)]             # a different first decision
    good = next(r["request_id"] for r in a if r["kind"] == "backend_call")
    bad = next(r["request_id"] for r in b if r["kind"] == "backend_call")

    def quality(d):
        return _q(positive=["review_approved"]) if d["request_id"] == good else \
            _q(negative=["review_changes"]) if d["request_id"] == bad else _q()
    p = pipeline(tmp, [_prof(tmp, ws)])
    _ingest_rows(p, a + b, quality)
    res = p.curate(PID)
    assert res["preference_pairs"] == 1
    pair, = p.store.cur.q("SELECT * FROM preferences")
    assert pair["judge"] == "router_label" and "review_approved vs review_changes" in pair["basis"]
    m = export(p.store, [PID], "preference", "dpo", {PID: p._cfg(PID)})
    assert m["validation"]["ok"] and m["examples"] == 1
    # re-curating rebuilds the derived pairs instead of piling them up
    p.curate(PID)
    assert len(p.store.cur.q("SELECT * FROM preferences")) == 1
    assert LOCAL in p._cfg(PID).local_models
