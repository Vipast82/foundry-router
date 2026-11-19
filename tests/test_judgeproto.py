r"""v0.181.205: the judge protocol, asserted against the ways a judge misleads.

The absolute judge (0-10, one answer) has been in the harness since Phase 4 and
cannot be checked against anything. Eval tier 2 (pairwise with style control)
and tier 4 (blind deliverable comparison) have both read "not here yet" since
v0.181.190 because they need a protocol rather than a check, and the agent
tasks need the same one.

Each test here is a way a pairwise judge produces a clean, repeatable,
meaningless number: position bias, self-preference, length, a judge that failed
being read as a tie, and a swap whose un-swap is off by one - the last being the
one that would silently reverse every number on the board.
"""
import pytest

from foundry_router import judgeproto as jp


class FakeJudge:
    """A judge whose bias is chosen by the test. `picks` is read in call
    order; "first" always names whichever answer it read first."""

    def __init__(self, picks):
        self.picks = list(picks)
        self.seen = []

    async def __call__(self, model, body):
        self.seen.append(body)
        p = self.picks.pop(0) if self.picks else "tie"
        if p == "first":
            p = "A"
        elif p == "second":
            p = "B"
        return '{"winner": "%s", "why": "because"}' % p


def pair(left="alpha-7b", right="beta-7b", a="short", b="short"):
    return ({"model": left, "text": a}, {"model": right, "text": b})


# -- both orders, and the un-swap ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_consistent_judge_names_the_model_not_the_side():
    """The judge picks the same ANSWER both times; the protocol has to map A/B
    back through the swap to the model that wrote it. An off-by-one here would
    reverse every result and still look perfectly consistent."""
    # reads left first and picks it; then reads right first and picks second
    j = FakeJudge(["A", "B"])
    l, r = pair()
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["winner"] == "alpha-7b"
    assert out["flip"] is False
    assert out["orders"] == {"ab": "alpha-7b", "ba": "alpha-7b"}


@pytest.mark.asyncio
async def test_the_other_model_winning_is_reported_as_the_other_model():
    j = FakeJudge(["B", "A"])
    l, r = pair()
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["winner"] == "beta-7b"
    assert out["flip"] is False


@pytest.mark.asyncio
async def test_a_judge_that_only_reads_position_decides_nothing():
    """The whole reason for judging twice: a judge that always picks what it
    read first is 100% consistent and knows nothing."""
    j = FakeJudge(["first", "first"])
    l, r = pair()
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["winner"] == "tie"
    assert out["flip"] is True
    assert "disagreed" in out["notes"]


@pytest.mark.asyncio
async def test_a_judge_biased_to_the_second_answer_also_decides_nothing():
    j = FakeJudge(["second", "second"])
    l, r = pair()
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["winner"] == "tie" and out["flip"] is True


@pytest.mark.asyncio
async def test_a_real_tie_is_a_tie_and_not_a_flip():
    j = FakeJudge(["tie", "tie"])
    l, r = pair()
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["winner"] == "tie" and out["flip"] is False


@pytest.mark.asyncio
async def test_every_pair_is_judged_exactly_twice():
    j = FakeJudge(["A", "A"])
    l, r = pair()
    await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert len(j.seen) == 2


@pytest.mark.asyncio
async def test_the_sides_really_are_swapped_between_the_two_calls():
    j = FakeJudge(["A", "A"])
    l, r = pair(a="LEFTTEXT", b="RIGHTTEXT")
    await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    first, second = j.seen
    assert first.index("LEFTTEXT") < first.index("RIGHTTEXT")
    assert second.index("RIGHTTEXT") < second.index("LEFTTEXT")


# -- a judge that failed is not a tie ------------------------------------------------

@pytest.mark.asyncio
async def test_an_unparseable_judge_gives_no_verdict_rather_than_a_tie():
    """A broken judge counted as indecisive looks like two models that cannot
    be separated. The board made this exact mistake with timed-out prompts."""
    class Mute:
        async def __call__(self, model, body):
            return "I would rather not say."

    l, r = pair()
    out = await jp.Pairwise(None, ask=Mute()).compare("judge-x", "task", l, r)
    assert out["winner"] is None
    assert "no verdict" in out["notes"]


@pytest.mark.asyncio
async def test_a_judge_that_raises_does_not_fail_the_run():
    class Broken:
        async def __call__(self, model, body):
            raise RuntimeError("connection reset")

    l, r = pair()
    out = await jp.Pairwise(None, ask=Broken()).compare("judge-x", "task", l, r)
    assert out["winner"] is None and "judge call failed" in out["notes"]


@pytest.mark.asyncio
async def test_one_good_order_and_one_failure_is_still_no_verdict():
    """Half a protocol is not a weaker verdict, it is no verdict: the single
    answer that came back is exactly the position-biased one."""
    class Half:
        def __init__(self):
            self.n = 0

        async def __call__(self, model, body):
            self.n += 1
            return '{"winner": "A"}' if self.n == 1 else "???"

    l, r = pair()
    out = await jp.Pairwise(None, ask=Half()).compare("judge-x", "task", l, r)
    assert out["winner"] is None


# -- who may judge --------------------------------------------------------------------

@pytest.mark.parametrize("mid", [
    "fable-1", "claude-fable-5-2", "Fable", "anthropic/fable-latest", "mythos-x",
])
def test_a_fable_model_is_never_a_judge(mid):
    """The operator's rule, stated as "should not be an option ever"."""
    assert jp.is_fable(mid) is True
    assert "Fable" in jp.judge_refusal(mid, "a", "b")


@pytest.mark.parametrize("mid", [
    "claude-opus-5", "qwen3-30b", "gpt-5", "llama-3.3-70b", "Foundry-Local",
])
def test_an_ordinary_model_may_judge(mid):
    assert jp.is_fable(mid) is False
    assert jp.judge_refusal(mid, "alpha", "beta") == ""


def test_a_model_may_not_judge_its_own_answer():
    """The one bias swapping the sides cannot cancel."""
    assert "its own" in jp.judge_refusal("qwen3-30b", "qwen3-30b", "beta")
    assert "its own" in jp.judge_refusal("qwen3-30b", "alpha", "qwen3-30b")


def test_self_judging_is_caught_through_a_differently_spelled_id():
    """A persona and the model behind it are the same weights under two names;
    comparing the strings exactly would wave it through."""
    assert jp.judge_refusal("ollama/Qwen3-30B:latest", "qwen3-30b", "beta")
    assert jp.judge_refusal("Qwen3_30B", "x/qwen3-30b", "beta")


def test_no_judge_at_all_is_refused_with_a_reason():
    assert jp.judge_refusal("", "a", "b") == "no judge model given"


@pytest.mark.asyncio
async def test_a_refused_pair_is_not_judged_and_says_why():
    j = FakeJudge(["A", "A"])
    l, r = pair(left="qwen3-30b")
    out = await jp.Pairwise(None, ask=j).compare("qwen3-30b", "task", l, r)
    assert out["winner"] is None
    assert "its own" in out["notes"]
    assert j.seen == [], "a refused pair must not reach the judge"


# -- blinding -------------------------------------------------------------------------

def test_the_model_names_do_not_reach_the_judge():
    got = jp.blind("As Qwen3 I would use claude for this, says Foundry-Kilo-Local",
                   ["Foundry-Kilo-Local", "qwen3-30b"])
    low = got.lower()
    for leak in ("qwen", "claude", "kilo", "foundry"):
        assert leak not in low, leak


def test_blinding_leaves_the_substance_alone():
    text = "Use a dict comprehension: {k: v for k, v in rows}. It is O(n)."
    assert jp.blind(text, ["alpha-7b"]) == text


@pytest.mark.asyncio
async def test_the_judge_never_sees_either_model_id():
    j = FakeJudge(["A", "A"])
    l, r = pair(left="qwen3-30b", right="claude-opus-5",
                a="I am Qwen3 and here is my answer",
                b="Claude here, my answer follows")
    await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    for body in j.seen:
        low = body.lower()
        assert "qwen" not in low and "claude" not in low and "opus" not in low


# -- style is measured and reported ---------------------------------------------------

def test_style_counts_what_a_judge_rewards_that_is_not_substance():
    s = jp.style_of("# Title\n- one\n- two\n\n```py\nx = 1\n```\n")
    assert s["code_blocks"] == 1
    assert s["md_marks"] == 3        # the heading and the two bullets
    assert s["words"] > 0 and s["chars"] > 0


def test_style_of_nothing_does_not_raise():
    assert jp.style_of("")["chars"] == 0
    assert jp.style_of(None)["words"] == 0


@pytest.mark.parametrize("a,b,matched", [
    (1000, 1000, True), (1000, 800, True), (1000, 749, False),
    (1000, 100, False), (0, 0, False),
])
def test_length_matched_pairs(a, b, matched):
    assert jp.length_matched({"chars": a}, {"chars": b}) is matched


@pytest.mark.asyncio
async def test_a_compared_pair_carries_both_sides_style():
    j = FakeJudge(["A", "A"])
    l, r = pair(a="x" * 100, b="y" * 1000)
    out = await jp.Pairwise(None, ask=j).compare("judge-x", "task", l, r)
    assert out["style"]["left"]["chars"] == 100
    assert out["style"]["right"]["chars"] == 1000
    assert out["matched"] is False


# -- the tally ------------------------------------------------------------------------

def row(winner, left="alpha", right="beta", la=500, lb=500, flip=False):
    return {"left": left, "right": right, "winner": winner, "flip": flip,
            "matched": jp.length_matched({"chars": la}, {"chars": lb}),
            "style": {"left": {"chars": la}, "right": {"chars": lb}}}


def test_wins_losses_and_ties_are_counted_for_the_named_subject():
    t = jp.tally([row("alpha"), row("alpha"), row("beta"), row("tie")], "alpha")
    assert (t["wins"], t["losses"], t["ties"]) == (2, 1, 1)
    assert t["decided"] == 3
    assert t["win_rate"] == round(2 / 3, 3)


def test_the_same_rows_from_the_other_side():
    t = jp.tally([row("alpha"), row("alpha"), row("beta"), row("tie")], "beta")
    assert (t["wins"], t["losses"]) == (1, 2)


def test_an_undecided_pair_is_excluded_and_not_a_loss():
    """A judge that did not answer is not evidence against a model."""
    t = jp.tally([row("alpha"), row(None)], "alpha")
    assert t["undecided"] == 1
    assert t["decided"] == 1 and t["win_rate"] == 1.0
    assert "not counted as losses" in t["notes"]


def test_no_decided_pair_gives_no_win_rate_rather_than_zero():
    t = jp.tally([row(None), row("tie")], "alpha")
    assert t["win_rate"] is None
    assert "no win rate" in t["notes"]


def test_a_thin_result_says_how_wide_its_interval_is():
    t = jp.tally([row("alpha"), row("alpha")], "alpha")
    assert t["win_rate"] == 1.0
    assert t["lo"] < 0.5, "two of two is not certainty"
    assert "points wide" in t["notes"]


def test_a_high_flip_rate_is_reported_as_the_judge_reading_position():
    rows = [row("tie", flip=True)] * 4 + [row("alpha")]
    t = jp.tally(rows, "alpha")
    assert t["flips"] == 4 and t["flip_rate"] == 0.8
    assert "reading position" in t["notes"]


def test_a_result_that_is_really_about_length_says_so():
    """A 100% win rate where the winner was always the longer answer is a
    report about length, and the matched rate is the one to read."""
    rows = [row("alpha", la=4000, lb=500) for _ in range(4)]
    t = jp.tally(rows, "alpha")
    assert t["win_rate"] == 1.0
    assert t["longer_won_pct"] == 1.0
    assert t["matched_pairs"] == 0 and t["matched_win_rate"] is None
    assert "read the length-matched rate" in t["notes"]


def test_the_length_matched_rate_can_disagree_with_the_overall_one():
    rows = ([row("alpha", la=4000, lb=500) for _ in range(3)]
            + [row("beta", la=500, lb=500) for _ in range(3)])
    t = jp.tally(rows, "alpha")
    assert t["win_rate"] == 0.5
    assert t["matched_pairs"] == 3 and t["matched_win_rate"] == 0.0


def test_winning_short_is_not_counted_as_the_longer_answer_winning():
    rows = [row("alpha", la=500, lb=4000) for _ in range(3)]
    t = jp.tally(rows, "alpha")
    assert t["longer_won_pct"] == 0.0
    assert "length-matched" not in t["notes"]


def test_equal_lengths_do_not_enter_the_length_confound_at_all():
    t = jp.tally([row("alpha", la=500, lb=500)], "alpha")
    assert t["longer_won_pct"] is None


def test_the_subject_defaults_to_the_first_rows_left_side():
    t = jp.tally([row("alpha")])
    assert t["subject"] == "alpha" and t["wins"] == 1


def test_an_empty_tally_does_not_raise():
    t = jp.tally([])
    assert t["pairs"] == 0 and t["win_rate"] is None


# -- parsing a judge's reply ----------------------------------------------------------

@pytest.mark.parametrize("content,want", [
    ('{"winner": "A"}', "A"),
    ('{"winner": "b", "why": "clearer"}', "B"),
    ('{"winner": "tie"}', "tie"),
    ('{"winner": "neither"}', "tie"),
    ('Here you go: {"winner": "B", "why": "x"} hope that helps', "B"),
    ("Answer A is better overall.", "A"),
    ("B wins.", "B"),
    ("They are equally good.", "tie"),
    ("I cannot separate them.", "tie"),
    ("", None),
    ("I would rather not answer.", None),
    ('{"score": 7}', None),
])
def test_parsing_a_verdict(content, want):
    assert jp.parse_verdict(content) == want


def test_the_prompt_tells_the_judge_not_to_reward_length_or_formatting():
    """The one instruction that makes a pairwise judge worth running."""
    p = jp.PAIRWISE_PROMPT.lower()
    assert "do not reward length" in p
    assert "correctness" in p
    assert "{a}" in jp.PAIRWISE_PROMPT and "{b}" in jp.PAIRWISE_PROMPT


@pytest.mark.parametrize("a,b,same", [
    ("ollama/Qwen3-30B:latest", "qwen3-30b", True),
    ("qwen3:30b", "qwen3-30b", True),
    ("Qwen3_30B", "x/qwen3-30b", True),
    # the tag carries the SIZE, so these are different models and must not
    # cancel each other: taking the name before the colon would merge them
    ("qwen3:30b", "qwen3:8b", False),
    ("claude-opus-5", "claude-sonnet-5-5", False),
    ("", "", False),
    ("alpha", "", False),
])
def test_two_ids_for_the_same_weights(a, b, same):
    assert jp._same_model(a, b) is same


def test_a_latest_tag_does_not_become_the_whole_identity():
    """It did: `ollama/x:latest` normalised to "latest", so every Ollama model
    matched every other one and none of them could be caught judging itself."""
    assert jp._norm_model("ollama/anything:latest") != "latest"
    assert not jp._same_model("ollama/alpha:latest", "ollama/beta:latest")
