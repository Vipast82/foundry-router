"""v0.181.227: the Benchmark tab leads with where a model stops.

Asked: go over the layout and the cards. Three things were wrong with how the
tab read. The page opened on building a suite and put the results last. The
card led with one number, when - now that tiers are measured - the picture is
which tiers of which suites a model passes. And "how far up each ladder", the
number that moves as models improve, was a text row; the build buttons sat in
the runs toolbar and only appeared once a run existed.
"""
import json
import re
from pathlib import Path

import foundry_router
from foundry_router import benchcard

PAGE = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
        ).read_text(encoding="utf-8")


def _row(ladder, level, score, tier="medium", detail=""):
    return {"score": score, "error": "", "code_detail": detail, "task_id": "x", "case_id": "c",
            "tags": json.dumps(["codereason", f"tier:{tier}", "family:search",
                                f"ladder:{ladder}", f"level:{level}"]),
            "tools_expected": "[]", "tools_called": "[]"}


# -- the data the rungs are drawn from ---------------------------------------------------

def test_a_ladder_row_carries_each_rung_and_what_happened_on_it():
    rows = [_row("sat", 2, 100.0, "easy"), _row("sat", 4, 0.0), _row("sat", 3, 100.0),
            _row("sat", 5, 0.0, "hard"),
            _row("sat", 6, 0.0, "expert", "not_sent=1 stopped_at=5 (2 levels missed in a row)")]
    reach = benchcard.ladder_reach(rows)
    assert len(reach) == 1
    r = reach[0]
    assert (r["name"], r["top"], r["n"], r["passed"]) == ("sat", 3, 5, 2)
    assert [(g["level"], g["state"], g["tier"]) for g in r["rungs"]] == [
        (2, "pass", "easy"), (3, "pass", "medium"), (4, "miss", "medium"),
        (5, "miss", "hard"), (6, "skipped", "expert")], "lowest first, whatever order they ran"


def test_one_ladder_is_not_a_weakness_the_tier_rows_say_it():
    """"Fails at: queue: level 1 of 6" sat beside "Code reasoning: hard 21.4"
    and said the same thing six times."""
    part = {"suite": "codereason", "title": "Code reasoning", "score": 40.0, "ceiling": False,
            "facets": [{"name": "Difficulty", "rows": [
                            {"label": "hard", "n": 14, "passed": 3, "score": 21.4}]},
                       {"name": "How far up each ladder", "rows": [
                            {"label": "queue: level 1 of 6", "n": 6, "passed": 1, "score": 16.7}]}]}
    v = benchcard.verdict({"parts": [part], "unscored": []})
    said = [w["what"] for w in v["weaknesses"]]
    assert any("hard" in s for s in said)
    assert not any("queue" in s for s in said)


# -- the page ----------------------------------------------------------------------------------

def _body(name):
    i = PAGE.index(f"function {name}(")
    return PAGE[i:PAGE.index("\nfunction ", i + 10)]


def test_the_card_opens_on_where_it_stops():
    card = _body("pfCard")
    order = [card.index(x) for x in ("Read this first", "pfProof(c)", "pfRead(c.verdict)",
                                     "pfSpeed(c.speed)", '<div class="rc-tiles">')]
    assert order == sorted(order), "the note, the grid, the verdict, speed, then the tiles"
    proof = _body("pfProof")
    assert "PF_TIERS.map" in proof and "every suite" in proof
    # a suite tiered from measurement (the replay suite) is in the grid too
    assert 'x.name.startsWith("Difficulty")' in PAGE
    for tier in ("easy", "medium", "hard", "expert"):
        assert re.search(rf"\b{tier}: \"", PAGE), f"the grid says what {tier} means"


def test_a_tile_draws_its_ladders_and_folds_its_breakdown():
    tile = _body("pfTile")
    assert "pfRungs(p)" in tile and "<details><summary>Breakdown</summary>" in tile
    assert "pfLadder(p)" not in tile, "the tier boxes repeated the grid above them"
    rungs = _body("pfRungs")
    for state in ("pass", "miss", "skipped"):
        assert f"{state}:" in rungs
    assert ".pip.pass" in PAGE and ".pip.miss" in PAGE and ".pip.skipped" in PAGE
    # the ladder rows are drawn, not listed a second time in the breakdown
    assert "f.name !== PF_LADDER" in _body("pfFacets")


def test_the_page_leads_with_report_cards_and_every_suite_can_always_be_built():
    i = PAGE.index("async function tabBench()")
    tab = PAGE[i:PAGE.index("enhancePickers($(\"#main\"));", i)]
    cards, suites, run_one, runs = (tab.index(x) for x in (
        "Model report card: every suite, one model", '<div class="suites">',
        '<div class="card-h">Run one', "<h2>Runs</h2>"))
    assert cards < suites < run_one < runs
    plates = tab[suites:run_one]
    for control in ('id="bs-build"', 'id="bench-codereason"', 'id="bench-code-top"',
                    'id="bench-needle"', 'id="bench-needle-top"', 'id="bench-toolpick"',
                    'id="bench-pick-top"', 'id="bench-toolflow"', 'id="bench-protocol"',
                    'id="bench-agent"'):
        assert control in plates, control
        assert control not in tab[runs:], "not in the runs toolbar, which needs a run to exist"
    # eight since v0.181.228 added the grounded suite, nine since v0.181.240 the guard suite,
    # ten since v0.181.241 the steps suite, eleven since v0.181.242 the structured suite,
    # twelve since v0.181.243 the held-rule suite, thirteen since v0.181.244 summaries
    assert plates.count('<div class="suite">') + plates.count('<div class="suite wide">') == 13
    assert 'id="bench-summaries"' in plates
    assert 'id="bench-heldrule"' in plates
    assert 'id="bench-structured"' in plates
    assert 'id="bench-steps"' in plates
    assert 'id="bench-guard"' in plates
    assert 'id="bench-grounded"' in plates and 'id="bench-grounded-top"' in plates
    assert tab.count('id="bench-cmp-msg"') == 1 and 'id="suite-msg"' in tab[cards:run_one]


def test_a_build_that_fails_says_so_beside_the_suites():
    for bid in ("bench-needle", "bench-toolpick", "bench-toolflow", "bench-codereason",
                "bench-protocol", "bench-agent"):
        i = PAGE.index(f'if ($("#{bid}")) $("#{bid}").onclick')
        handler = PAGE[i:PAGE.index("  };\n", i)]
        assert '$("#suite-msg").textContent = e.message' in handler, bid


def test_the_layout_holds_at_phone_width():
    assert "minmax(min(340px, 100%), 1fr)" in PAGE and "minmax(min(290px, 100%), 1fr)" in PAGE
    assert re.search(r"@media \(max-width: 640px\) \{\s*\.proof-grid", PAGE)
