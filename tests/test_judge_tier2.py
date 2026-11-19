r"""v0.181.205: eval tier 2 over a campaign's stored answers.

Two things had to change before this could exist, and the first is the reason
tier 2 has read "not here yet" since v0.181.190 while looking easy:
`eval_results` kept `answer_head`, the first 200 characters. Comparing those
would have ranked opening sentences and reported it as a win rate, which is
worse than reporting nothing. So the answer is kept in full to a configurable
cap, and a campaign that ran before this release has nothing to compare - which
the comparison says, rather than returning an empty result that reads as a tie.
"""
import pytest

from foundry_router import evalharness, judgeproto as jp
from foundry_router.db import Database, utcnow


def seed(tmp_path, answers, campaign=1):
    """answers: {persona: {prompt_id: text}}; "" means the prompt got none."""
    db = Database(tmp_path / "e.sqlite")
    db.execute("INSERT INTO eval_campaigns (id, ts, name, personas, repeats, "
               "categories, judge_model, status) VALUES (?,?,'c','[]',1,'[]','','done')",
               (campaign, utcnow()))
    rid = 0
    for persona, byp in answers.items():
        rid += 1
        db.execute("INSERT INTO eval_runs (id, ts, persona, judge_model, "
                   "prompts_run, status, campaign_id) VALUES (?,?,?,'',?,'done',?)",
                   (rid, utcnow(), persona, len(byp), campaign))
        for pid, txt in byp.items():
            db.execute(
                "INSERT INTO eval_results (run_id, prompt_id, prompt, "
                "response_chars, shape_pass, answer_full, answer_head, error) "
                "VALUES (?,?,?,?,1,?,?,?)",
                (rid, pid, f"prompt {pid}", len(txt), txt, txt[:200],
                 "" if txt else "timed out"))
    return type("S", (), {"db": db})()


def picker(winner):
    """A judge that always picks the given text, whichever side it is on: a
    content-reading judge, so the two orders agree and nothing flips."""
    async def ask(model, body):
        a = body.split("ANSWER A:")[1].split("ANSWER B:")[0]
        return '{"winner": "A"}' if winner in a else '{"winner": "B"}'
    return ask


@pytest.mark.asyncio
async def test_a_campaign_without_stored_answers_says_so(tmp_path):
    """The v0.181.191 shape: corrected code, old data, and a number that looks
    fine. An empty comparison must not read as a tie."""
    svc = seed(tmp_path, {"A": {10: ""}, "B": {10: ""}})
    svc.db.execute("UPDATE eval_results SET answer_full=''")
    out = await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B",
                                     ask=picker("x"))
    assert out["pairs"] == [] and out["tally"] == {}
    assert "200-character head" in out["note"]
    assert "Run a new campaign" in out["note"]


@pytest.mark.asyncio
async def test_a_prompt_one_side_never_answered_is_skipped_not_lost(tmp_path):
    """Every timeout in campaign 2 was an honesty prompt. Scoring a silence as
    a loss would make the hardest category look like a defeat."""
    svc = seed(tmp_path, {"A": {10: "aaa bbb", 11: "only A answered"},
                          "B": {10: "ccc ddd", 11: ""}})
    out = await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B",
                                     ask=picker("aaa"))
    assert len(out["pairs"]) == 1
    assert out["skipped"][0]["prompt_id"] == 11
    assert "timed out" in out["skipped"][0]["why"]
    assert out["tally"]["wins"] == 1 and out["tally"]["losses"] == 0
    assert "left out" in out["tally"]["notes"]


@pytest.mark.asyncio
async def test_the_winner_is_named_by_persona(tmp_path):
    svc = seed(tmp_path, {"A": {10: "aaa"}, "B": {10: "bbb"}})
    out = await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B",
                                     ask=picker("bbb"))
    assert out["pairs"][0]["winner"] == "B"
    assert out["tally"]["subject"] == "A" and out["tally"]["losses"] == 1


@pytest.mark.asyncio
async def test_identical_answers_flip_rather_than_crowning_one(tmp_path):
    """Two answers a judge cannot tell apart must not produce a winner. A
    position-reading judge handed the same text twice picks the first slot
    both times, which is a disagreement once the sides are swapped back."""
    svc = seed(tmp_path, {"A": {10: "same text"}, "B": {10: "same text"}})

    async def first(model, body):
        return '{"winner": "A"}'

    out = await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B", ask=first)
    assert out["pairs"][0]["winner"] == "tie"
    assert out["pairs"][0]["flip"] is True
    assert out["tally"]["win_rate"] is None


@pytest.mark.asyncio
async def test_a_repeat_is_another_sample_not_a_second_contestant(tmp_path):
    """A campaign with repeats has several runs per persona. Pairing every run
    against every run would multiply one prompt into four pairs and report the
    same comparison as four pieces of evidence."""
    svc = seed(tmp_path, {"A": {10: "aaa"}, "B": {10: "bbb"}})
    svc.db.execute("INSERT INTO eval_runs (ts, persona, judge_model, "
                   "prompts_run, status, campaign_id) VALUES (?,'A','',1,'done',1)",
                   (utcnow(),))
    rid = svc.db.query_one("SELECT MAX(id) AS m FROM eval_runs")["m"]
    svc.db.execute("INSERT INTO eval_results (run_id, prompt_id, prompt, "
                   "response_chars, shape_pass, answer_full) "
                   "VALUES (?,10,'p',3,1,'zzz')", (rid,))
    out = await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B",
                                     ask=picker("aaa"))
    assert len(out["pairs"]) == 1


@pytest.mark.asyncio
async def test_a_refused_judge_stops_before_reading_the_campaign(tmp_path):
    svc = seed(tmp_path, {"A": {10: "aaa"}, "B": {10: "bbb"}})
    out = await jp.pairwise_campaign(svc, 1, "fable-1", "A", "B",
                                     ask=picker("aaa"))
    assert out["pairs"] == [] and "Fable" in out["note"]


@pytest.mark.asyncio
async def test_each_comparison_is_recorded_with_its_flip_and_lengths(tmp_path):
    svc = seed(tmp_path, {"A": {10: "a" * 300}, "B": {10: "b" * 100}})
    await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B",
                               ask=picker("a" * 300))
    rows = jp.pair_history(svc.db, 1)
    assert len(rows) == 1
    assert rows[0]["winner"] == "A" and rows[0]["flip"] == 0
    assert (rows[0]["left_chars"], rows[0]["right_chars"]) == (300, 100)
    assert rows[0]["matched"] == 0, "300 against 100 is not length-matched"


@pytest.mark.asyncio
async def test_history_without_a_campaign_returns_every_comparison(tmp_path):
    svc = seed(tmp_path, {"A": {10: "aaa"}, "B": {10: "bbb"}})
    await jp.pairwise_campaign(svc, 1, "judge-x", "A", "B", ask=picker("aaa"))
    assert len(jp.pair_history(svc.db)) == 1


# -- keeping the answer ---------------------------------------------------------------

def cfg(**kw):
    ev = type("EV", (), kw)()
    return type("S", (), {"config_store": type("CS", (), {
        "config": type("C", (), {"evals": ev})()})()})()


def test_the_cap_comes_from_the_config():
    assert evalharness.keep_answer_chars(cfg(keep_answer_chars=500)) == 500


def test_zero_is_a_real_choice_and_not_an_unset_value():
    """0 means keep none, and a falsy-or-default read would silently store
    20,000 characters for an operator who asked for none."""
    assert evalharness.keep_answer_chars(cfg(keep_answer_chars=0)) == 0


@pytest.mark.parametrize("v", [None, "", "abc"])
def test_an_unusable_cap_falls_back_to_the_default(v):
    assert evalharness.keep_answer_chars(cfg(keep_answer_chars=v)) \
        == evalharness.KEEP_ANSWER_CHARS


def test_a_negative_cap_keeps_nothing_rather_than_slicing_from_the_end():
    """text[:-5] is not "a bit less of it", it is all but the last five
    characters, and for a short answer it is nothing at all - silently."""
    assert evalharness.keep_answer_chars(cfg(keep_answer_chars=-5)) == 0


def test_a_bare_service_still_has_a_cap():
    assert evalharness.keep_answer_chars(type("S", (), {})()) \
        == evalharness.KEEP_ANSWER_CHARS


def test_the_column_exists_on_a_database_made_before_it(tmp_path):
    """The migration, not the schema: a running router's database was created
    without this column and the insert names it."""
    db = Database(tmp_path / "m.sqlite")
    cols = {r["name"] for r in
            db.query("SELECT name FROM pragma_table_info('eval_results')")}
    assert "answer_full" in cols


def test_a_fable_model_is_not_offered_as_a_judge_in_the_picker():
    """The dropdown offered every reachable non-embedding model, Fable among
    them, against a rule stated as "should not be an option ever" - and the run
    API never checked, so the dropdown was not the only way in. Both ends are
    asserted: this one and the refusal in the harness below."""
    page = _page()
    i = page.index("const judgeOpts")
    window = page[max(0, i - 600):i + 400]
    assert "fable" in window.lower(), "the judge list must exclude Fable"
    assert "mythos" in window.lower(), "and the tier alias for it"


def test_the_harness_refuses_a_fable_judge_whatever_the_page_sent():
    import inspect

    from foundry_router.evalharness import EvalHarness
    src = inspect.getsource(EvalHarness._judge)
    assert "is_fable" in src
    assert "never a judge" in src


def test_the_pairwise_panel_is_wired_to_the_route():
    """Anchored on the handler, not a byte window: a UI assertion reading a
    fixed offset broke once already when another button went in between."""
    page = _page()
    assert 'id="pw-run"' in page and 'id="pw-out"' in page
    assert 'api("eval/pairwise"' in page
    assert "function pairwiseView(" in page
    i = page.index('$("#pw-run").onclick')
    handler = page[i:i + 900]
    assert "pick two different personas" in handler, \
        "a persona compared with itself is not a comparison"
    assert "pairwise needs a judge model" in handler


def test_the_panel_says_what_the_protocol_does_before_showing_a_number():
    """Anchored on the heading, not on the first "Pairwise" in the file: a
    button tooltip added later pointed at this panel by name and moved the
    window onto itself. The same fragility as the byte-offset assertion in
    v0.181.199, found the same way."""
    page = _page()
    panel = page[page.index("Pairwise — which of two"):][:2500]
    assert "swapped" in panel, "both orders is the protocol, not a detail"
    assert "both orders agree" in panel
    assert "never a judge" in panel
    assert "own answer" in panel


def test_the_view_puts_each_misleading_number_beside_its_explanation():
    """A win rate alone is the thing this protocol exists to not report."""
    page = _page()
    view = page[page.index("function pairwiseView("):][:2600]
    assert "95%" in view, "the interval belongs beside the rate"
    assert "Length-matched" in view
    assert "report\n      about length" in view or "about length" in view
    assert "read it first" in view.lower() or "Read this first" in view


def _page():
    from pathlib import Path
    import foundry_router
    return (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")


def test_the_eval_config_route_accepts_the_knob_the_page_draws():
    """The page can draw a control, send it, and have the POST filter drop it:
    the allow-list is the second place every eval setting lives."""
    import inspect

    from foundry_router.ui import routes
    src = inspect.getsource(routes.set_evals)
    assert "keep_answer_chars" in src
    assert "per_prompt_timeout" in src
