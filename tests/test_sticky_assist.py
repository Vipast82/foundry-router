"""Cache-stable behaviour for Cline Act (found by measuring a real 2.85 h session where 53% of the
model's time was prompt re-reads): sticky context guard, sticky thinking, loop breaker (Cline stops a
task at its 5th identical call), and per-request cache-miss tracking on the Scoreboard."""

import hashlib
import json

from foundry_router import context_guard as cg
from foundry_router import turn_assist as ta
from foundry_router.pool.protocols import ChatResult
import pytest

from tests.test_turn_assist import FAIL, FILE, OK, ScriptPool, call, history, post, result, tc

MODEL = "qwen3.8-27b"          # a family name Foundry recognises as a thinking model


@pytest.fixture()
def act(app):
    """Act persona served by a scripted llama.cpp-style model that supports thinking levels."""
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = False

    def make(assist, script, **persona):
        pool = ScriptPool({MODEL: script[next(iter(script))]})
        svc.pool = pool
        svc.personas.upsert("Act", execution_mode="direct", model_allowlist=[MODEL],
                            pinned_models=[], assist=assist, **persona)
        return pool
    return make


# -- sticky context guard ----------------------------------------------------------------

def _round(i, size=3000):
    return [{"role": "assistant", "content": "",
             "tool_calls": [{"id": f"c{i}", "type": "function",
                             "function": {"name": "read_files", "arguments": {"files": [i]}}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + "x" * size}]


def _h(m):
    return hashlib.sha1(json.dumps(m, sort_keys=True, default=str).encode()).hexdigest()


def _run_guard(sticky: bool, rounds: int = 140):
    """Grow one conversation turn by turn through the guard with a wobbling token
    ratio (as production's learn() does); return (cut advances, prompt busts)."""
    cg._ratio["m"] = 3.9
    memo = cg.memo_for("t-sticky") if sticky else None
    if memo is not None:
        memo.clear()
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "the task"}]
    window, reserve = 40_000, 2_000
    prev, prev_n, advances, busts = None, 0, 0, 0
    for i in range(rounds):
        msgs = msgs + _round(i)
        sent, rep = cg.fit(msgs, None, window, reserve, "m", memo=memo)
        cg.learn("m", sent, None, int(cg.estimate(sent, None, "m") * (1 + 0.08 * (((i * 7) % 11) - 5) / 5)))
        hs = [_h(m) for m in sent]
        advanced = bool(rep and rep.get("advanced"))
        advances += advanced
        if sticky:
            n = memo.get("n", 0)
            assert n >= prev_n                                # the cut only moves forward
            if prev_n:
                assert rep is not None                        # never un-trimmed
            prev_n = n
        if prev is not None:
            common = next((k for k, (a, b) in enumerate(zip(prev, hs)) if a != b), min(len(prev), len(hs)))
            changed = common < len(prev)
            busts += changed
            if sticky and not advanced:
                assert not changed, f"turn {i}: prompt changed without a cut advance"
        assert cg.estimate(sent, None, "m") <= window         # still fits
        prev = hs
    return advances, busts


def test_sticky_guard_never_untrims_and_busts_only_when_the_cut_advances():
    advances, busts = _run_guard(sticky=True)
    _, legacy_busts = _run_guard(sticky=False)
    assert busts == advances                                  # every prompt change is a deliberate advance
    assert legacy_busts > 4 * busts                           # the stateless guard changed it far more often


def test_stateless_guard_is_unchanged_without_a_memo():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    cg._ratio["m"] = 3.2
    for i in range(80):
        msgs += _round(i)
    out, rep = cg.fit(msgs, None, 40_000, 2_000, "m")
    assert rep and "advanced" not in rep and rep["dropped"]


def test_sticky_guard_memo_resets_when_history_differs():
    memo = cg.memo_for("t-reset")
    memo.clear()
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(60):
        msgs += _round(i)
    cg.fit(msgs, None, 40_000, 2_000, "m", memo=memo)
    assert memo.get("n")
    other = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(60):
        other += _round(i + 1000)
    out, rep = cg.fit(other, None, 40_000, 2_000, "m", memo=memo)
    assert rep["advanced"] and out[2]["role"] == "user"       # re-derived from the new history


# -- sticky thinking ---------------------------------------------------------------------

def _loop_history(n_repeat=3):
    same = ("run_commands", {"commands": ["lune run t"]}, FAIL)
    return history(*[same] * n_repeat)


def test_sticky_thinking_keeps_persona_level_until_stuck_then_stays_escalated():
    cfg = {**ta.ASSIST_DEFAULTS, "think_policy": "sticky", "supervisor": "off"}
    calm = ta.prepare(history(("read_files", {"files": []}, FILE)), cfg)
    assert calm.think is None                                # the persona's own level, no change
    stuck = ta.prepare(_loop_history(3), cfg)
    assert stuck.think == "high" and stuck.think_why.startswith("stuck")
    later = _loop_history(3) + [call(9, "read_files", {"files": []}), result(9, FILE),
                                call(10, "read_files", {"files": []}), result(10, FILE)]
    assert ta.prepare(later, cfg).think == "high"            # still escalated: no flip back
    after_user = later + [{"role": "assistant", "content": "done"},
                          {"role": "user", "content": "now do the next thing"}]
    assert ta.prepare(after_user, cfg).think is None         # the user wrote: one change back


def test_clines_loop_warning_is_not_a_user_message():
    msgs = _loop_history(3) + [{"role": "user", "content":
                                "Detected 3 consecutive identical calls to `run_commands`; "
                                "consider trying a different approach."}]
    cfg = {**ta.ASSIST_DEFAULTS, "think_policy": "sticky"}
    assert ta.prepare(msgs, cfg).think == "high"             # the warning didn't reset escalation


def test_sticky_levels_do_not_change_turn_to_turn_in_a_calm_session():
    cfg = {**ta.ASSIST_DEFAULTS, "think_policy": "sticky"}
    msgs = history(("read_files", {"files": []}, FILE))
    seen = set()
    for i in range(30):
        msgs = msgs + [call(100 + i, "editor" if i % 3 == 0 else "run_commands", {"path": f"f{i}"}),
                       result(100 + i, FAIL if i % 4 == 0 else OK)]
        seen.add(ta.prepare(msgs, cfg).think)
    assert len(seen) == 1                                    # adaptive would flip off/low/high here


# -- loop breaker --------------------------------------------------------------------------

def test_loop_check_blocks_the_fourth_identical_call_with_unchanging_results():
    msgs = _loop_history(3)
    hits = ta.loop_check(msgs, [tc("run_commands", {"commands": ["lune run t"]})], ta.ASSIST_DEFAULTS)
    assert len(hits) == 1 and hits[0].repeats == 4 and hits[0].same_results
    assert not ta.loop_check(msgs, [tc("run_commands", {"commands": ["something else"]})],
                             ta.ASSIST_DEFAULTS)
    assert not ta.loop_check(_loop_history(2), [tc("run_commands", {"commands": ["lune run t"]})],
                             ta.ASSIST_DEFAULTS)           # only the 3rd: let it through


def test_loop_check_lets_polling_with_changing_results_reach_the_fifth_call():
    pairs = [("run_commands", {"commands": ["Get-Content tmp\\run.log"]}, f"line {i}")
             for i in range(3)]
    msgs = history(*pairs)
    call4 = [tc("run_commands", {"commands": ["Get-Content tmp\\run.log"]})]
    assert not ta.loop_check(msgs, call4, ta.ASSIST_DEFAULTS)             # 4th, results changed
    msgs5 = history(*pairs, ("run_commands", {"commands": ["Get-Content tmp\\run.log"]}, "line 3"))
    assert ta.loop_check(msgs5, call4, ta.ASSIST_DEFAULTS)[0].repeats == 5  # Cline stops here


def test_loop_run_must_be_consecutive():
    pairs = [("run_commands", {"commands": ["lune run t"]}, FAIL)] * 3
    msgs = history(*pairs, ("read_files", {"files": []}, FILE))
    assert not ta.loop_check(msgs, [tc("run_commands", {"commands": ["lune run t"]})],
                             ta.ASSIST_DEFAULTS)


def test_loop_breaker_reasks_in_the_same_request_and_thinks_harder(app, client, act):
    pool = act({"loop_block_at": 4, "think_policy": "persona"},
               {"qwen": [ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["lune run t"]})]),
                         ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["python other.py"]}, "t2")])]},
               reasoning_effort="medium", force_reasoning_effort=1)
    r = post(client, _loop_history(3)).json()
    assert r["message"]["tool_calls"][0]["function"]["arguments"]["commands"] == ["python other.py"]
    assert len(pool.sent) == 2
    assert "loop-breaker] blocked before it ran" in pool.sent[1][1][-1]["content"]
    assert pool.kw[1]["think"] != pool.kw[0]["think"]         # the re-ask escalated
    kinds = {x["kind"] for x in app.state.services.db.query("SELECT kind FROM interventions")}
    assert "loop_break" in kinds


def test_loop_breaker_ends_the_turn_gracefully_when_the_model_insists(app, client, act):
    same = tc("run_commands", {"commands": ["lune run t"]})
    pool = act({"loop_block_at": 4, "loop_block_max": 2},
               {"qwen": [ChatResult(content="", tool_calls=[dict(same, id=f"i{i}")]) for i in range(5)]})
    r = post(client, _loop_history(3)).json()
    assert r["message"]["content"].strip().startswith("BLOCKED by Foundry loop-breaker")
    assert not r["message"].get("tool_calls")                 # Cline never sees the 5th identical call
    assert len(pool.sent) == 3


def test_loop_breaker_streaming_path(app, client):
    from tests.test_turn_assist import StreamScriptPool
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = True
    same = tc("run_commands", {"commands": ["lune run t"]})
    svc.pool = StreamScriptPool({"qwen": [ChatResult(content="", tool_calls=[dict(same, id=f"i{i}")])
                                          for i in range(5)]})
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"], pinned_models=[],
                        assist={"loop_block_at": 4, "loop_block_max": 1})
    r = post(client, _loop_history(3), stream=True)
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    content = "".join(x["message"].get("content", "") for x in lines)
    assert "BLOCKED by Foundry loop-breaker" in content
    assert not lines[-1]["message"].get("tool_calls")


def test_plan_persona_without_assist_is_untouched_by_the_loop_breaker(app, client):
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = False
    same = tc("run_commands", {"commands": ["lune run t"]})
    pool = ScriptPool({"qwen": [ChatResult(content="", tool_calls=[same])]})
    svc.pool = pool
    svc.personas.upsert("Plain", execution_mode="direct", model_allowlist=["qwen"], pinned_models=[])
    r = post(client, _loop_history(3), model="Plain").json()
    assert r["message"]["tool_calls"] and len(pool.sent) == 1


# -- cache-miss tracking ---------------------------------------------------------------------

def test_cache_misses_are_recorded_with_their_cause_and_shown_on_the_scoreboard(app, client, act):
    act({"think_policy": "persona"},
        {"qwen": [ChatResult(content="ok", prompt_tokens=60_000, cached_tokens=0, prefill_tokens=60_000,
                             prompt_eval_duration_ns=120_000_000_000),
                  ChatResult(content="ok", prompt_tokens=61_000, cached_tokens=60_900, prefill_tokens=100,
                             prompt_eval_duration_ns=1_000_000_000),
                  ChatResult(content="ok", prompt_tokens=61_500, cached_tokens=0, prefill_tokens=61_500,
                             prompt_eval_duration_ns=125_000_000_000)]})
    h = history(("read_files", {"files": []}, FILE))
    post(client, h)
    post(client, h + [call(5, "run_commands", {"commands": ["x"]}), result(5, OK)])
    app.state.services.personas.upsert("Act", reasoning_effort="low", force_reasoning_effort=1)
    post(client, h + [call(5, "run_commands", {"commands": ["x"]}), result(5, OK),
                      call(6, "run_commands", {"commands": ["y"]}), result(6, OK)])
    rows = app.state.services.db.query(
        "SELECT cache_miss, cache_cause, think FROM assist_turns ORDER BY id")
    assert [r["cache_miss"] for r in rows] == [1, 0, 1]
    assert rows[0]["cache_cause"] == "conversation_start" and rows[2]["cache_cause"] == "think_change"
    assert rows[1]["think"] != rows[2]["think"]
    sb = client.get("/admin/api/scoreboard?hours=1").json()
    assert sb["cache"]["misses"] == 2 and sb["cache"]["miss_minutes"] == 4.1
    assert {c["cause"] for c in sb["cache"]["causes"]} == {"conversation_start", "think_change"}
    assert sb["conversations"][0]["cache_misses"] == 2


# -- seeds -----------------------------------------------------------------------------------

def test_seed_v10_upgrades_only_the_untouched_v098_default(tmp_path):
    from foundry_router.db import Database, _CLINE_ACT_ASSIST_V9, CLINE_ACT_ASSIST
    path = tmp_path / "d.sqlite"
    db = Database(path)
    assert json.loads(db.query_one("SELECT assist FROM personas WHERE virtual_name='claude-cline-act'")
                      ["assist"])["think_policy"] == "sticky"          # fresh install
    db.execute("UPDATE personas SET assist=? WHERE virtual_name='claude-cline-act'",
               (json.dumps(_CLINE_ACT_ASSIST_V9),))
    db.execute("DELETE FROM kv WHERE key='persona_seed_v10_cline_sticky'")
    db._seed_cline_sticky()
    assert json.loads(db.query_one("SELECT assist FROM personas WHERE virtual_name='claude-cline-act'")
                      ["assist"]) == CLINE_ACT_ASSIST
    edited = {**_CLINE_ACT_ASSIST_V9, "advisor": True, "frontier_models": ["claude-opus-5-5"]}
    db.execute("UPDATE personas SET assist=? WHERE virtual_name='claude-cline-act'", (json.dumps(edited),))
    db.execute("DELETE FROM kv WHERE key='persona_seed_v10_cline_sticky'")
    db._seed_cline_sticky()
    assert json.loads(db.query_one("SELECT assist FROM personas WHERE virtual_name='claude-cline-act'")
                      ["assist"]) == edited                            # the operator's edit survives


# -- servers without cache numbers / strict output budgets (Strata) ------------------------------

def test_backend_without_cache_numbers_is_not_counted_as_a_miss(app, client, act):
    act({"think_policy": "persona"},
        {"m": [ChatResult(content="ok", prompt_tokens=90_000, cached_tokens=0, prefill_tokens=0)]})
    post(client, history(("read_files", {"files": []}, FILE)))
    row = app.state.services.db.query_one("SELECT cache_miss, cache_cause FROM assist_turns")
    assert row["cache_miss"] == 0 and row["cache_cause"] == "not_reported"


def test_reread_count_stands_in_for_missing_cached_tokens(app, client, act):
    act({"think_policy": "persona"},
        {"m": [ChatResult(content="ok", prompt_tokens=90_000, cached_tokens=0, prefill_tokens=500)]})
    post(client, history(("read_files", {"files": []}, FILE)))
    row = app.state.services.db.query_one("SELECT cache_miss, cached_tokens FROM assist_turns")
    assert row["cache_miss"] == 0 and row["cached_tokens"] == 89_500


def test_output_cap_is_fitted_to_the_room_left_in_the_window(app, client, act):
    pool = act({"think_policy": "persona"}, {"m": [ChatResult(content="ok")]},
               context_window=40_000, max_output_tokens=32_768)
    big = history(*[("read_files", {"files": [i]}, "x" * 20_000) for i in range(5)])
    post(client, big)
    cap = pool.kw[0]["max_tokens"]
    assert 1024 <= cap < 32_768                                # was 32,768: a strict server would 400


def test_output_cap_untouched_when_there_is_room(app, client, act):
    pool = act({"think_policy": "persona"}, {"m": [ChatResult(content="ok")]},
               context_window=262_144, max_output_tokens=16_384)
    post(client, history(("read_files", {"files": []}, FILE)))
    assert pool.kw[0]["max_tokens"] == 16_384


def test_client_sent_sampling_is_logged_beside_the_think_value(app, client, act):
    act({"think_policy": "persona"}, {"m": [ChatResult(content="ok")]})
    client.post("/v1/chat/completions", json={
        "model": "Act", "stream": False, "temperature": 0, "top_p": 1,
        "reasoning_effort": "xhigh", "messages": history(("read_files", {"files": []}, FILE)),
        "tools": [{"type": "function", "function": {"name": "run_commands", "parameters": {}}}]})
    ev = app.state.services.db.query("SELECT message FROM event_log WHERE source='facade'")
    assert any("think='xhigh'" in e["message"] and "'temperature': 0" in e["message"] for e in ev)
