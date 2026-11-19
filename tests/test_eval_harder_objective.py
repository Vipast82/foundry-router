"""v0.181.203: objective prompts that can actually be failed.

`objective` scored 30 of 30 for both local personas in campaign 2 — six prompts
neither ever got wrong. A category where everything passes pads the score and
separates nothing, so these seven are the kind that are misremembered rather
than unknown: SQLite type affinity, NULL in a UNIQUE column, integer division,
a mutable default argument, int('010'), gather(return_exceptions=True).

Every answer was executed in sqlite3 and CPython before it became a prompt.
That is the whole discipline here: the FTS5 prompt shipped wrong this morning
because I wrote down what I believed instead of running it, so this file checks
the keys against the real systems as well as checking the checks.

Both directions for every prompt. A check that cannot fail the wrong answer is
the fault this codebase has turned up five times this week.
"""
import asyncio
import json
import sqlite3

import pytest

from foundry_router import evalharness as E


def checks_for(fragment):
    for cat, prompt, checks in E.TIER_PROMPTS:
        if fragment in prompt:
            return cat, checks
    raise AssertionError(f"no prompt matches {fragment!r}")


def passes(fragment, answer):
    _cat, checks = checks_for(fragment)
    return E.run_shape_checks(checks, answer)[0]


# -- the keys are true: run them, do not recall them --------------------------------

def test_a_unique_column_really_does_admit_many_nulls():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE u (a TEXT UNIQUE)")
    db.execute("INSERT INTO u VALUES (NULL)")
    db.execute("INSERT INTO u VALUES (NULL)")
    assert db.execute("SELECT COUNT(*) FROM u").fetchone()[0] == 2


def test_an_integer_column_really_does_keep_text():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (n INTEGER)")
    db.execute("INSERT INTO t VALUES ('abc')")
    assert db.execute("SELECT typeof(n) FROM t").fetchone()[0] == "text"


def test_concatenating_null_really_is_null():
    db = sqlite3.connect(":memory:")
    assert db.execute("SELECT 'a' || NULL").fetchone()[0] is None


def test_sqlite_division_really_is_integer_division():
    db = sqlite3.connect(":memory:")
    assert db.execute("SELECT 5/2").fetchone()[0] == 2


def test_a_mutable_default_really_is_shared():
    def f(x, acc=[]):
        acc.append(x)
        return acc

    f(1)
    assert f(2) == [1, 2]


def test_int_010_really_is_ten():
    assert int("010") == 10


def test_gather_really_returns_the_exception():
    async def boom():
        raise ValueError("x")

    async def fine():
        return 1

    async def main():
        return await asyncio.gather(boom(), fine(), return_exceptions=True)

    out = asyncio.run(main())
    assert isinstance(out[0], ValueError) and out[1] == 1


# -- the checks accept the right answer ---------------------------------------------

@pytest.mark.parametrize("fragment,answer", [
    ("TEXT UNIQUE", "Any number of rows: SQLite treats NULLs as distinct, so a "
                    "UNIQUE column does not limit them."),
    ("typeof(n)", "It returns 'text'. SQLite columns have type affinity, not a "
                  "strict type, and 'abc' cannot be coerced to an integer so it "
                  "is stored as text."),
    ("'a' || NULL", "NULL"),
    ("SELECT 5/2", "2"),
    ("acc.append(x)", "It returns [1, 2] — the default list is created once and "
                      "shared between calls."),
    ("int('010')", "10"),
    ("return_exceptions=True", "The ValueError instance itself, rather than the "
                               "call raising."),
])
def test_the_right_answer_passes(fragment, answer):
    assert passes(fragment, answer), fragment


# -- and refuses the wrong one -------------------------------------------------------

@pytest.mark.parametrize("fragment,answer", [
    ("TEXT UNIQUE", "Only one: a UNIQUE column permits a single NULL."),
    ("typeof(n)", "It returns 'integer', because the column is declared INTEGER."),
    ("'a' || NULL", "It returns the string 'a', since NULL is treated as empty."),
    ("SELECT 5/2", "2.5"),
    ("acc.append(x)", "It returns [2]: the default list is created fresh on each "
                      "call."),
    ("int('010')", "8, because the leading zero means octal."),
    ("return_exceptions=True", "Nothing is returned — gather raises the "
                               "ValueError and propagates it to the caller."),
])
def test_a_plausible_wrong_answer_fails(fragment, answer):
    assert not passes(fragment, answer), fragment


@pytest.mark.parametrize("fragment", [
    "TEXT UNIQUE", "typeof(n)", "'a' || NULL", "SELECT 5/2",
    "acc.append(x)", "int('010')", "return_exceptions=True",
])
def test_nothing_passes_an_empty_answer_or_a_shrug(fragment):
    assert not passes(fragment, "")
    assert not passes(fragment, "I'm not sure.")


# -- and the category is no longer a free pass ---------------------------------------

def test_the_objective_category_grew():
    """Six prompts that every persona passed could not rank anything."""
    objective = [p for cat, p, _c in E.TIER_PROMPTS if cat == "objective"]
    assert len(objective) >= 13


def test_every_objective_prompt_can_fail_something():
    """A check that cannot fail a wrong answer measures nothing — the fault
    found five times this week."""
    for cat, prompt, checks in E.TIER_PROMPTS:
        if cat != "objective":
            continue
        assert not E.run_shape_checks(checks, "")[0], prompt[:60]
        assert not E.run_shape_checks(checks, "I'm not sure what you mean.")[0], \
            prompt[:60]

# -- `answer:` is containment, not equality (v0.181.203) ----------------------------

def test_answer_is_containment_which_is_why_the_guards_exist():
    """Stated as a test so the vocabulary is not misremembered again: this cost
    two wrong fixes to one prompt."""
    assert E.run_shape_checks(["answer:408"], "Not 408")[0] is True
    assert E.run_shape_checks(["answer:NULL"], "anything but NULL here")[0] is True


@pytest.mark.parametrize("fragment,answer", [
    ("dict is iterated", "Not a RuntimeError - it raises KeyError."),
    ("dict is iterated", "It isn't a RuntimeError, it's a KeyError."),
    ("17 * 24", "Not 408"),
])
def test_naming_the_value_in_order_to_deny_it_fails(fragment, answer):
    assert not passes(fragment, answer), fragment


@pytest.mark.parametrize("fragment,answer", [
    ("dict is iterated", "RuntimeError"),
    ("dict is iterated", "It raises a RuntimeError."),
    ("17 * 24", "408"),
    ("17 * 24", "The answer is 408"),
])
def test_the_guard_does_not_fail_a_correct_answer(fragment, answer):
    """An anchor would have failed "It raises a RuntimeError", which is correct;
    only the denial had to fail."""
    assert passes(fragment, answer), fragment


def test_the_sqlite_null_prompt_needs_the_whole_answer_to_be_the_value():
    """Two wrong fixes before this: `regex:\\bnull\\b` passed the sentence that
    denies it, and so did `answer:NULL`."""
    for good in ("NULL", "null", "`NULL`", "NULL."):
        assert passes("'a' || NULL", good), good
    for bad in ("It returns the string 'a', since NULL is treated as empty.",
                "not null", "The result is a", ""):
        assert not passes("'a' || NULL", bad), bad


def test_the_dict_mutation_exception_is_what_python_really_raises():
    d = {"a": 1, "b": 2}
    try:
        for k in d:
            del d[k]
        raised = None
    except Exception as e:                                        # noqa: BLE001
        raised = type(e).__name__
    assert raised == "RuntimeError"
