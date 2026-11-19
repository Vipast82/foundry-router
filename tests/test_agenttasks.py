r"""v0.181.206: deliverable tasks, and the refusal to score them.

Every other suite has an answer key. These ask for a thing to be built, which
is what a model is for here and the one thing nothing else measured - and a
deliverable has no key, so the dangerous part is not building the tasks but
making sure nothing scores them. `answer_score` is a length heuristic that
returns 1.0 for any answer over twenty characters and `code_score` only
compiles, so both would fire on every task here and hand it a confident 100.
That is exactly the needle fault of v0.181.199, where a model that found
nothing scored 100, and it is asserted here from the other direction.

The second risk is the run average. It divided the score total by the number of
cases DONE; leaving an unscored case in that denominator would quietly lower
every mixed run, and changing the arithmetic would move every existing suite's
number. Both are pinned.
"""
import json

import pytest

from foundry_router import agenttasks, bench, judgeproto as jp
from foundry_router.db import Database


@pytest.fixture
def svc(tmp_path):
    cfg = type("CFG", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "config_store": type("CS", (), {"config": cfg})(),
    })()


# -- the tasks -----------------------------------------------------------------------

def test_every_task_states_what_would_make_it_good(svc):
    """A task with no criteria turns the comparison into a preference poll, so
    it is dropped at build time rather than shipped."""
    agenttasks.build(svc)
    rows = bench.cases(svc, "agent")
    assert len(rows) == len(agenttasks.TASKS)
    for c in rows:
        crit = (c["reference"] or {}).get("criteria") or []
        assert len(crit) >= 3, c["task_id"]
        assert all(isinstance(x, str) and x.strip() for x in crit)


def test_a_task_without_criteria_is_refused(svc):
    out = agenttasks.build(svc, tasks=[("x", ["t"], "do a thing", [])])
    assert out["cases"] == 0


def test_the_tasks_cover_the_work_this_ecosystem_does(svc):
    """Not a public benchmark's domains: the operator's."""
    agenttasks.build(svc)
    tags = {t for c in bench.cases(svc, "agent") for t in c["tags"]}
    for want in ("luau", "roblox", "blender", "python", "asyncio", "sqlite"):
        assert want in tags, want


def test_no_case_carries_an_answer_key(svc):
    """A key here would be a fiction, and `must_include` or `content` is what
    the scorers look for."""
    agenttasks.build(svc)
    for c in bench.cases(svc, "agent"):
        ref = c["reference"] or {}
        assert ref.get("deliverable") is True
        assert not ref.get("must_include")
        assert not ref.get("content")
        assert not ref.get("py_value")


def test_the_criteria_are_read_from_the_built_suite_not_from_source(svc):
    """A criterion edited after a run would otherwise judge work that was
    never asked for it."""
    agenttasks.build(svc)
    got = agenttasks.criteria_for(svc, "agent", "remote_authority")
    assert got and any("server-side table" in c for c in got)
    # rewrite the file with different criteria; the reader must follow the file
    path = bench._dir(svc) / "agent.jsonl"
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["task_id"] == "remote_authority":
            r["reference"]["criteria"] = ["only this one"]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    assert agenttasks.criteria_for(svc, "agent", "remote_authority") \
        == ["only this one"]


def test_an_unknown_task_has_no_criteria_rather_than_raising(svc):
    agenttasks.build(svc)
    assert agenttasks.criteria_for(svc, "agent", "nope") == []


# -- the refusal to score ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_deliverable_is_not_scored_however_good_it_looks(svc):
    """The needle fault from the other side: a long, well-formatted, entirely
    wrong answer must not come back as a number at all."""
    agenttasks.build(svc)
    case = bench.cases(svc, "agent")[0]
    got = {"content": "# Solution\n\n```lua\n" + "print(1)\n" * 50 + "```\n",
           "tool_calls": []}
    s = await bench.score_one(svc, case, got)
    assert s["score"] is None
    assert s["answer"] is None and s["code"] is None
    assert "not scored" in s["code_detail"]


@pytest.mark.asyncio
async def test_a_shrug_is_also_not_scored_zero(svc):
    """Not scoring means not scoring: a zero would be a judgement too, and the
    comparison is what judges these."""
    agenttasks.build(svc)
    case = bench.cases(svc, "agent")[0]
    s = await bench.score_one(svc, case, {"content": "I am not sure.",
                                          "tool_calls": []})
    assert s["score"] is None


@pytest.mark.asyncio
async def test_a_case_with_a_key_is_still_scored_exactly_as_before(svc):
    """The refusal must not leak into the suites that do have answers."""
    bench.build_codereason(svc)
    c = bench.cases(svc, "codereason")[0]
    good = await bench.score_one(
        svc, c, {"content": c["reference"]["content"], "tool_calls": []})
    assert good["score"] == 100.0
    bad = await bench.score_one(svc, c, {"content": "no idea", "tool_calls": []})
    assert bad["score"] == 0.0


def test_the_run_average_divides_by_the_cases_that_have_a_score():
    """Reading the parsed source, because the fault would be arithmetic that
    looks right: `totals["score"] / i` with an unscored case in `i` lowers
    every mixed run silently."""
    import ast
    import inspect

    src = inspect.getsource(bench._run)
    tree = ast.parse(src.strip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "sn" in names or "score_n" in names, \
        "the average needs its own denominator"
    assert 'totals["score"] / i' not in src, \
        "cases done is the wrong denominator once a case can be unscored"


def test_the_deliverable_column_exists_on_an_older_database(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    cols = {r["name"] for r in
            db.query("SELECT name FROM pragma_table_info('bench_results')")}
    assert "deliverable" in cols


# -- tier 4 ---------------------------------------------------------------------------

def seed_runs(svc, left, right, suite="agent"):
    """left/right: {task_id: deliverable text}."""
    db = svc.db
    ids = []
    for model, byt in (("alpha-7b", left), ("beta-7b", right)):
        db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, "
                   "scored, status) VALUES (datetime('now'),?,?,'',?,?,'done')",
                   (suite, model, len(byt), len(byt)))
        rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
        ids.append(rid)
        for tid, txt in byt.items():
            db.execute(
                "INSERT INTO bench_results (run_id, case_id, task_id, tags, "
                "answer_chars, score, seconds, error, deliverable) "
                "VALUES (?,?,?,'[]',?,NULL,1,?,?)",
                (rid, f"AGENT-{tid}", tid, len(txt),
                 "" if txt else "call failed", txt))
    return ids


def picker(text):
    async def ask(model, body):
        a = body.split("DELIVERABLE A:")[1].split("DELIVERABLE B:")[0] \
            if "DELIVERABLE A:" in body else \
            body.split("ANSWER A:")[1].split("ANSWER B:")[0]
        return '{"winner": "A"}' if text in a else '{"winner": "B"}'
    return ask


@pytest.mark.asyncio
async def test_two_runs_of_the_agent_tasks_are_compared(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "server table, validated"},
                     {"remote_authority": "trusts the client"})
    out = await jp.pairwise_runs(svc, l, r, "judge-x",
                                 ask=picker("server table"))
    assert out["pairs"][0]["winner"] == "alpha-7b"
    assert out["tally"]["wins"] == 1
    assert out["pairs"][0]["criteria"] >= 3


@pytest.mark.asyncio
async def test_the_judge_is_shown_the_tasks_requirements(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa"},
                     {"remote_authority": "bbb"})
    seen = []

    async def ask(model, body):
        seen.append(body)
        return '{"winner": "A"}'

    await jp.pairwise_runs(svc, l, r, "judge-x", ask=ask)
    assert seen and "REQUIREMENTS:" in seen[0]
    assert "server-side table" in seen[0]
    assert "do not reward length" in seen[0].lower()


@pytest.mark.asyncio
async def test_a_run_without_deliverables_says_so(svc):
    """A run made before v0.181.206 kept only the answer's length."""
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa"},
                     {"remote_authority": "bbb"})
    svc.db.execute("UPDATE bench_results SET deliverable='' WHERE run_id=?", (r,))
    out = await jp.pairwise_runs(svc, l, r, "judge-x", ask=picker("aaa"))
    assert out["pairs"] == []
    assert "before v0.181.206" in out["note"]


@pytest.mark.asyncio
async def test_two_runs_of_different_suites_are_refused(svc):
    agenttasks.build(svc)
    l, _ = seed_runs(svc, {"remote_authority": "aaa"}, {"x": "bbb"})
    _, r = seed_runs(svc, {"y": "ccc"}, {"z": "ddd"}, suite="codereason")
    out = await jp.pairwise_runs(svc, l, r, "judge-x", ask=picker("aaa"))
    assert "would not line up" in out["note"]


@pytest.mark.asyncio
async def test_a_run_compared_with_itself_is_refused(svc):
    agenttasks.build(svc)
    l, _ = seed_runs(svc, {"remote_authority": "aaa"}, {"remote_authority": "b"})
    out = await jp.pairwise_runs(svc, l, l, "judge-x", ask=picker("aaa"))
    assert "with itself" in out["note"]


@pytest.mark.asyncio
async def test_a_missing_run_is_named(svc):
    out = await jp.pairwise_runs(svc, 999, 998, "judge-x", ask=picker("a"))
    assert "no run 999" in out["note"]


@pytest.mark.asyncio
async def test_a_task_only_one_run_produced_is_skipped_not_lost(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa", "connection_leak": "ccc"},
                     {"remote_authority": "bbb"})
    out = await jp.pairwise_runs(svc, l, r, "judge-x", ask=picker("aaa"))
    assert len(out["pairs"]) == 1
    assert out["skipped"][0]["task_id"] == "connection_leak"
    assert "left out" in out["tally"]["notes"]


@pytest.mark.asyncio
async def test_a_task_whose_call_failed_is_skipped_with_its_reason(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa"},
                     {"remote_authority": ""})
    out = await jp.pairwise_runs(svc, l, r, "judge-x", ask=picker("aaa"))
    assert out["pairs"] == [] or out["skipped"]
    if out["skipped"]:
        assert "call failed" in out["skipped"][0]["why"]


@pytest.mark.asyncio
async def test_a_fable_judge_is_refused_here_too(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa"},
                     {"remote_authority": "bbb"})
    out = await jp.pairwise_runs(svc, l, r, "fable-2", ask=picker("aaa"))
    assert "Fable" in out["note"] and out["pairs"] == []


@pytest.mark.asyncio
async def test_identical_deliverables_do_not_crown_a_winner(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "same"},
                     {"remote_authority": "same"})

    async def first(model, body):
        return '{"winner": "A"}'

    out = await jp.pairwise_runs(svc, l, r, "judge-x", ask=first)
    assert out["pairs"][0]["winner"] == "tie"
    assert out["pairs"][0]["flip"] is True


@pytest.mark.asyncio
async def test_each_comparison_is_recorded(svc):
    agenttasks.build(svc)
    l, r = seed_runs(svc, {"remote_authority": "aaa"},
                     {"remote_authority": "bbb"})
    await jp.pairwise_runs(svc, l, r, "judge-x", ask=picker("aaa"))
    rows = jp.pair_history(svc.db)
    assert len(rows) == 1 and rows[0]["winner"] == "alpha-7b"
    assert rows[0]["prompt"].startswith("agent:")


def test_a_comparison_with_no_pairs_does_not_claim_it_used_no_criteria():
    """An empty comparison compared nothing; saying it fell back on taste
    would be a sentence about work that never happened."""
    assert jp.crit_seen([]) is False
    assert jp.crit_seen([{"criteria": 0}]) is False
    assert jp.crit_seen([{"criteria": 4}]) is True


def test_the_build_result_says_the_suite_is_not_scored(svc):
    out = agenttasks.build(svc)
    assert out["scored"] is False
    assert "not scored" in out["note"]


def test_the_routes_and_buttons_are_wired():
    import inspect
    from pathlib import Path

    import foundry_router
    from foundry_router.ui import routes
    assert "agenttasks" in inspect.getsource(routes.bench_agent)
    assert "pairwise_runs" in inspect.getsource(routes.eval_pairwise_runs)
    page = (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    assert 'id="bench-agent"' in page and 'api("bench/agent"' in page
    assert 'id="pw-runs"' in page and 'api("eval/pairwise-runs"' in page
    i = page.index('$("#pw-runs").onclick')
    assert "needs a judge model" in page[i:i + 700]
