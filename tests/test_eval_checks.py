r"""v0.181.190: the judge-free checks, and the three prompt kinds that use them.

A judge is another model's opinion, which is the right tool for "is this good
writing" and the wrong one for "is this the right answer". These checks have
answers. The categories they serve:

- `triage` is a famous riddle with its trap removed, so the remembered answer is
  the wrong one and a model that pattern-matches fails.
- `objective` has a checkable answer.
- `honesty` asks about things that do not exist, where the only right answer is
  to say so. It is the one a confident model reliably fails.
"""
import tempfile
from pathlib import Path

import pytest

from foundry_router import evalharness as E
from foundry_router.db import Database


def ok(checks, response):
    passed, _detail = E.run_shape_checks(checks, response)
    return passed


# -- the new checks ----------------------------------------------------------------

def test_an_expected_answer_ignores_the_noise_around_it():
    assert ok(["answer:1912"], "The warehouse opened in **1912**.")
    assert ok(["answer:408"], "408")
    assert ok(["answer:RuntimeError"], "It raises a `RuntimeError` during iteration.")
    assert not ok(["answer:1912"], "It opened in 1913.")


def test_a_regex_check_matches_across_lines_and_cases():
    assert ok([r"regex:\b32k\b"], "The window is 32K tokens.")
    assert ok([r"regex:(undefined|border)"], "The result is UNDEFINED for holes.")
    assert not ok([r"regex:^\W*no\b"], "Yes, it succeeds.")
    assert ok([r"regex:^\W*no\b"], "No - the branch is behind.")


def test_a_bad_pattern_is_reported_and_skipped_not_a_failure():
    passed, detail = E.run_shape_checks(["regex:["], "anything")
    assert passed is True and "bad pattern" in detail


def test_not_mentions_is_how_a_trap_is_scored():
    assert ok(["not_mentions:mother"], "Because the boy has two fathers.")
    assert not ok(["not_mentions:mother"], "The surgeon is his mother.")


def test_admitting_uncertainty_is_a_pass_on_a_question_with_no_answer():
    for said in ("I do not know of that paper.", "I cannot verify this exists.",
                 "I'm not certain — I have no record of it."):
        assert ok(["admits_uncertainty"], said), said
    assert not ok(["admits_uncertainty"],
                  "It showed a 12% gain on sparse routing benchmarks.")


def test_refusing_to_guess_fails_if_it_guesses_anyway():
    """A model that hedges and then names the invented thing has not refused."""
    assert ok(["refuses_to_guess:Halvorsen"], "I cannot verify that paper exists.")
    assert not ok(["refuses_to_guess:Halvorsen"],
                  "I am not certain, but Halvorsen argued for gated attention.")
    assert not ok(["refuses_to_guess:Halvorsen"],
                  "Halvorsen and Mbeki showed a 12% gain.")


def test_an_unknown_check_is_still_skipped_rather_than_failing():
    passed, detail = E.run_shape_checks(["no_such_check"], "x")
    assert passed is True and "unknown check" in detail


# -- the fingerprint, which repeatability is measured with -------------------------

def test_the_same_answer_dressed_differently_has_one_fingerprint():
    assert E.fingerprint("  **1912**  ") == E.fingerprint("1912")
    assert E.fingerprint("1912") != E.fingerprint("1913")


# -- token counts, which put a cost beside the score -------------------------------

class _Resp:
    def __init__(self, body):
        self.body = body.encode()


def test_tokens_are_read_from_either_shape():
    assert E._usage(_Resp('{"prompt_eval_count": 1343, "eval_count": 88}')) == \
        {"prompt": 1343, "completion": 88}
    assert E._usage(_Resp('{"usage": {"prompt_tokens": 12, "completion_tokens": 5}}')) == \
        {"prompt": 12, "completion": 5}


def test_a_reply_that_says_nothing_about_tokens_costs_zero_not_a_guess():
    assert E._usage(_Resp("{}")) == {"prompt": 0, "completion": 0}
    assert E._usage(_Resp("not json")) == {"prompt": 0, "completion": 0}
    assert E._usage(object()) == {"prompt": 0, "completion": 0}


# -- the prompts themselves --------------------------------------------------------

def test_every_shipped_prompt_uses_a_check_the_runner_knows():
    for category, prompt, checks in E.SEED_PROMPTS:
        _passed, detail = E.run_shape_checks(checks, "placeholder")
        assert "unknown check" not in detail, (category, checks, detail)
        assert "bad pattern" not in detail, (category, checks, detail)


def test_an_objective_prompt_passes_against_its_own_expected_answer():
    for _cat, _p, checks in E.TIER_PROMPTS:
        for c in checks:
            if c.startswith("answer:"):
                want = c.split(":", 1)[1]
                assert ok([c], f"The answer is {want}."), c


def test_the_three_model_categories_are_asked_of_every_persona():
    for persona in ({"benchmark_category": "coding"}, {"benchmark_category": ""},
                    {"virtual_name": "Foundry-RAG"}):
        cats = E.default_categories(persona)
        assert set(E.MODEL_CATEGORIES) <= set(cats), (persona, cats)
        assert len(cats) == len(set(cats))          # no duplicates


# -- seeding a router that has already seeded --------------------------------------

def test_the_new_set_reaches_a_database_that_seeded_the_old_one(tmp_path):
    """The first seed was flagged "once, never again", which is right for not
    clobbering edits and wrong for a router already running: without a flag per
    set the tier prompts would exist only on a database created after them."""
    db = Database(tmp_path / "s.sqlite")
    db.kv_set("eval_prompts_seed_v1", "already")       # as a live router is
    E.ensure_seed(db)
    cats = {r["category"]: r["n"] for r in db.query(
        "SELECT category, COUNT(*) AS n FROM eval_prompts GROUP BY category")}
    assert set(cats) == set(E.MODEL_CATEGORIES), cats
    assert sum(cats.values()) == len(E.TIER_PROMPTS)


def test_seeding_twice_adds_nothing(tmp_path):
    db = Database(tmp_path / "s2.sqlite")
    E.ensure_seed(db)
    first = db.query_one("SELECT COUNT(*) AS n FROM eval_prompts")["n"]
    E.ensure_seed(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_prompts")["n"] == first
    assert first == len(E.SEED_PROMPTS)


def test_a_prompt_the_operator_deleted_stays_deleted(tmp_path):
    db = Database(tmp_path / "s3.sqlite")
    E.ensure_seed(db)
    db.execute("DELETE FROM eval_prompts WHERE category='triage'")
    E.ensure_seed(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_prompts "
                        "WHERE category='triage'")["n"] == 0
