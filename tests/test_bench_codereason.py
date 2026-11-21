r"""v0.181.204: a local code suite that can actually be failed.

Asked for a local alternative to HumanEval scored by `code_checks`.
`code_checks` cannot score one: `builtin:python` COMPILES the code and never
runs it — its own docstring says only the checker runs, never the model's code —
so it answers "does this parse", which every model passes. `code_score` came
back 100.0 for both personas in run 40, and a check everything passes measures
nothing.

Running a model's code would answer the real question and is a separate
decision with a sandbox behind it, which is the operator's to make.

What is measured instead is whether a model can say what code DOES, and every
expected value is computed by executing the snippet when the suite is BUILT —
this module's own fixed table, never anything from a request. So an answer key
here cannot be wrong the way the FTS5 one was.
"""
import ast

import pytest

from foundry_router import bench
from foundry_router.db import Database


@pytest.fixture
def svc(tmp_path):
    cfg = type("CFG", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "config_store": type("CS", (), {"config": cfg})(),
    })()


# -- the keys are computed, not recalled ---------------------------------------------

def test_every_value_key_is_what_the_snippet_really_produces(svc):
    """The point of the suite: a key cannot disagree with the language."""
    bench.build_codereason(svc)
    by_id = {c["task_id"]: c for c in bench.cases(svc, "codereason")}
    for key, src, expr in bench._CODE_SNIPPETS:
        assert key in by_id, key
        want = ast.literal_eval(by_id[key]["reference"]["py_value"])
        assert bench._run_snippet(src, expr) == want, key


def test_every_exception_key_is_what_python_really_raises(svc):
    bench.build_codereason(svc)
    by_id = {c["task_id"]: c for c in bench.cases(svc, "codereason")}
    for key, src in bench._CODE_EXC:
        assert bench._raised_by(src) == by_id[key]["reference"]["content"], key


def test_a_snippet_that_stopped_raising_is_refused_not_shipped(svc, monkeypatch):
    """A Python upgrade could change one. Shipping it would score every model
    against an answer the language no longer gives."""
    monkeypatch.setattr(bench, "_CODE_EXC", [("quiet", "x = 1")])
    monkeypatch.setattr(bench, "_CODE_SNIPPETS", [])
    # v0.181.211: the traced tiers are tables of their own
    monkeypatch.setattr(bench, "_CODE_HARD", [])
    monkeypatch.setattr(bench, "_CODE_EXPERT", [])
    # v0.181.223: and so are benchhard's generated programs
    monkeypatch.setattr(bench, "_hard_code_cases", lambda top, refused: [])
    out = bench.build_codereason(svc)
    assert out["cases"] == 0
    assert any("raised nothing" in r for r in out["refused"])


def test_a_snippet_that_will_not_run_is_refused(svc, monkeypatch):
    monkeypatch.setattr(bench, "_CODE_SNIPPETS", [("bad", "1/0", "x")])
    monkeypatch.setattr(bench, "_CODE_EXC", [])
    monkeypatch.setattr(bench, "_CODE_HARD", [])
    monkeypatch.setattr(bench, "_CODE_EXPERT", [])
    monkeypatch.setattr(bench, "_hard_code_cases", lambda top, refused: [])
    out = bench.build_codereason(svc)
    assert out["cases"] == 0 and out["refused"]


# -- a value is compared as a value --------------------------------------------------

@pytest.mark.parametrize("want,answer", [
    ("4", "4"), ("4", "The value is 4."), ("4", "`4`"),
    ("[2, 2, 2]", "[2, 2, 2]"),
    ("[2, 2, 2]", "[2,2,2]"),                      # spacing is not the answer
    ("[2, 2, 2]", "The result is [2, 2, 2]."),
    ("'ababab'", "'ababab'"),
    ("'ababab'", "ababab"),                        # a string without its quotes
    ("3.5", "3.5"), ("3.5", "It is 3.5"),
    ("[('a', 3), ('b', 2)]", "[('a',3),('b',2)]"),
    ("(True, False)", "(True,False)"),
])
def test_the_right_value_however_it_is_written(want, answer):
    assert bench.value_score(answer, want) == 1.0, (want, answer)


@pytest.mark.parametrize("want,answer", [
    ("4", "14"), ("4", "40"), ("4", "I am not sure"), ("4", ""),
    ("[2, 2, 2]", "[0, 1, 2]"), ("[2, 2, 2]", "[2, 2]"),
    ("'ababab'", "abab"),
    ("3.5", "3"), ("3.5", "35"),
    ("[('a', 3), ('b', 2)]", "[('a', 1), ('b', 2)]"),
    ("(True, False)", "(False, True)"),
])
def test_a_wrong_value_fails(want, answer):
    assert bench.value_score(answer, want) == 0.0, (want, answer)


def test_one_is_not_true():
    """True == 1 in Python; a benchmark must not take one for the other."""
    assert bench.value_score("1", "True") == 0.0
    assert bench.value_score("True", "1") == 0.0
    assert bench.value_score("True", "True") == 1.0


def test_a_case_with_no_expected_value_is_not_scored_this_way():
    assert bench.value_score("anything", "") is None


def test_rubbish_does_not_raise():
    """It runs inside scoring; a crash there would fail the run, not the case."""
    for junk in ("", "[[[[", "'''", "\\x00", "{1: {2: {3: " + "[" * 200):
        assert bench.value_score(junk, "[1]") in (0.0, 1.0)


# -- end to end ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_perfect_answer_scores_full_and_a_shrug_scores_nothing(svc):
    bench.build_codereason(svc)
    for c in bench.cases(svc, "codereason"):
        good = await bench.score_one(
            svc, c, {"content": c["reference"]["content"], "tool_calls": []})
        assert good["score"] == 100.0, c["task_id"]
        bad = await bench.score_one(
            svc, c, {"content": "I am not sure.", "tool_calls": []})
        assert bad["score"] == 0.0, c["task_id"]


def test_the_suite_covers_both_values_and_exceptions(svc):
    bench.build_codereason(svc)
    tags = [c["tags"] for c in bench.cases(svc, "codereason")]
    assert any("exception" in t for t in tags)
    assert any("exception" not in t for t in tags)
    assert all("codereason" in t for t in tags)


def test_nothing_a_model_writes_is_ever_run():
    """The suite executes snippets from a fixed table at build time; nothing
    from a request reaches the executor, and scoring executes nothing at all.

    Read from the PARSED source: `literal_eval(` contains `eval(`, so matching
    the text tripped on the very call that makes this safe."""
    import ast
    import inspect

    src = inspect.getsource(bench.build_codereason)
    assert "_CODE_SNIPPETS" in src and "_CODE_EXC" in src

    def called(fn):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(fn).strip())):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        return names

    scoring = called(bench.value_score) | called(bench._literals_in)
    assert "exec" not in scoring, "scoring must never execute an answer"
    assert "eval" not in scoring, "a bare eval would run whatever was written"
    assert "literal_eval" in scoring, "a literal is parsed, not evaluated"

    # the snippet runner takes a snippet and an expression, and nothing else
    sig = inspect.signature(bench._run_snippet)
    assert list(sig.parameters) == ["src", "expr"]
