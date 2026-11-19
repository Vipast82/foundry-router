"""v0.181.191: the older prompt set had the same fault as the new one.

The audit was asked about the fourteen tier prompts, so the nine that shipped
before them went unread. Three of those nine carried checks that could not fail a
wrong answer:

  * "Summarize the plot of Romeo and Juliet in exactly three sentences" checked
    only `no_refusal` and a 2000-character cap. The instruction the prompt gives
    was not tested at all.
  * the JSON moon-landing prompt checked `valid_json` and nothing else, so
    `{"mission": "", "year": 0}` passed.
  * "In what year was SQLite first released" checked that a URL was cited. Any
    year passed, cited or not.

A fourth has no answer key on purpose, because the current stable Python version
moves, but it now has to look like a version rather than be any prose at all.

The sentence counter is tested on its own because I wrote three bugs into it and
found them by checking rather than by reading: a backtick in the quote-flattening
map, which destroyed every code fence before the strip could run, and a split
that did not require whitespace after a full stop, which made "1.5 seconds" two
sentences and a URL three.
"""
import json

import pytest

from foundry_router import evalharness as E

FENCE = "```"


def checks_for(fragment):
    for _cat, prompt, checks in E.SEED_PROMPTS:
        if fragment in prompt:
            return checks
    raise AssertionError(f"no prompt matches {fragment!r}")


# -- the counter -------------------------------------------------------------------

@pytest.mark.parametrize("want,text", [
    (3, "One thing. Two things. Three things."),
    (3, "Why? Because. Really!"),
    (2, "It took 1.5 seconds. That is fast."),
    (2, "See https://sqlite.org/index.html for more. Done."),
    (2, f"Here it is. {FENCE}py\na = 1. b = 2.\n{FENCE} That is all."),
    (3, "- One.\n- Two.\n- Three."),
    (3, "Written by W. Shakespeare in 1597. Two families feud. They both die."),
    (3, "Romeo and Juliet are lovers from feuding families in Verona. They marry "
        "in secret with the help of Friar Laurence. A failed plan leaves both "
        "dead and their families reconciled."),
    (4, "One. Two. Three. Let me know if you want more."),
])
def test_the_sentence_count(want, text):
    assert E.count_sentences(text) == want, text


def test_a_backtick_is_code_not_a_quote():
    """It was in the quote-flattening map, so `code` became 'code' - which broke
    the code-fence strip here and silently rewrote code in every answer the
    `answer:` check and the fingerprint saw."""
    assert E.flatten_quotes("a `code` b") == "a `code` b"
    # and the curly quotes it is there for still flatten
    assert E.flatten_quotes("it’s “fine”") == "it's \"fine\""


def test_a_bad_sentence_count_is_skipped_not_failed():
    passed, detail = E.run_shape_checks(["sentences:many"], "One. Two.")
    assert passed is True and "bad count" in detail


# -- the four prompts, both directions ---------------------------------------------

def test_three_sentences_is_now_the_test_it_claims_to_be():
    ch = checks_for("Romeo and Juliet in exactly three sentences")
    assert "sentences:3" in ch
    good = ("Two feuding families rule Verona. Romeo and Juliet marry in secret. "
            "A failed plan kills them both.")
    assert E.run_shape_checks(ch, good)[0]
    eight = ("Verona is torn by a feud. Romeo attends a ball. He meets Juliet. "
             "They marry in secret. Romeo is banished. Juliet fakes her death. "
             "Romeo kills himself. Juliet follows.")
    assert not E.run_shape_checks(ch, eight)[0], "an eight-sentence answer passed"


def test_the_json_prompt_checks_the_answer_and_not_only_the_syntax():
    ch = checks_for("crewed Moon landing")
    assert E.run_shape_checks(
        ch, json.dumps({"mission": "Apollo 11", "year": 1969}))[0]
    for wrong in ({"mission": "", "year": 0},
                  {"mission": "Apollo 13", "year": 1970},
                  {"mission": "Apollo 11", "year": 1972}):
        assert not E.run_shape_checks(ch, json.dumps(wrong))[0], wrong


def test_the_sqlite_year_is_checked():
    ch = checks_for("SQLite first released")
    assert E.run_shape_checks(
        ch, "SQLite was first released in 2000. Source: https://sqlite.org/")[0]
    assert not E.run_shape_checks(
        ch, "SQLite was first released in 1998. Source: https://sqlite.org/")[0]


def test_the_python_version_prompt_needs_a_version_but_no_fixed_one():
    """No answer key - the answer moves - but prose with a link is not an
    answer."""
    ch = checks_for("current stable version of Python")
    assert E.run_shape_checks(
        ch, "The current stable release is 3.13.1. See https://python.org/")[0]
    assert E.run_shape_checks(
        ch, "It is 3.14 now. Source: https://www.python.org/downloads/")[0]
    assert not E.run_shape_checks(
        ch, "I looked it up, see https://www.python.org/downloads/ for the "
            "latest release.")[0]


def test_no_shipped_prompt_passes_an_empty_answer():
    """The rule the tier set got at v0.181.190 and this set never did: a check
    that cannot fail a wrong answer measures nothing."""
    for cat, prompt, checks in E.SEED_PROMPTS:
        assert not E.run_shape_checks(checks, "")[0], (cat, prompt[:60])


def test_waffle_passes_only_where_not_knowing_is_the_right_answer():
    """"I'm not sure what you mean" is a non-answer to a question with an answer,
    and the correct answer to one without. The first version of this test
    asserted it should fail everywhere and failed on the honesty prompts, which
    exist precisely to reward it."""
    waffle = "I'm not sure what you mean."
    for cat, prompt, checks in E.SEED_PROMPTS:
        got = E.run_shape_checks(checks, waffle)[0]
        if cat == "honesty":
            assert got, ("an honesty prompt should accept it", prompt[:60])
        else:
            assert not got, (cat, prompt[:60])
