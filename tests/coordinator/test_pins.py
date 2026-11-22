"""Count pins (tracks/pins.py; item 21 of COORDINATOR_AUTOMATION_NEEDS): a test that pins a
count is set to the real count after a lane is rebased, and a conflict that is only about
the count resolves itself."""

from __future__ import annotations

import os

import pytest

from foundry_coordinator.tracks import pins

LINES = [
    r"tests/GateRunner.lua :: loaded = (\d+),(?=\s*heartbeat = \d+,\s*errors = \d+,\s*line = "
    r'"Boot complete — \d+ services) :: src/server/Services/*.lua',
    r"tests/GateRunner.lua :: heartbeat = (\d+),(?=\s*errors = \d+,\s*line = "
    r'"Boot complete — \d+ services) :: src/server/Services/*.lua :: :Update\(',
    r"tests/GateRunner.lua :: Boot complete — (\d+) services loaded :: src/server/Services/*.lua",
    r"tests/GateRunner.lua :: services loaded, (\d+) driven by the shared Heartbeat :: "
    r"src/server/Services/*.lua :: :Update\(",
    r'tests/lune/specs/MapV2Pacing.spec.luau :: gate:find\("(\d+) services loaded" :: '
    r"src/server/Services/*.lua",
    r"tests/lune/specs/MatchReset.spec.luau :: T\.eq\(count\(sources\), (\d+), :: "
    r"src/server/Services/*.lua",
    r"tests/lune/specs/MatchReset.spec.luau :: exactly the (\d+) service files :: "
    r"src/server/Services/*.lua",
]
GATE = """    server = {
        -- BS-4: +BoonService (no Update) = 53 / 28
        loaded = 53,
        heartbeat = 28,
        errors = 0,
        line = "Boot complete — 53 services loaded, 28 driven by the shared Heartbeat",
    },
    client = {
        loaded = 20,
        heartbeat = 7,
        errors = 0,
        line = "Boot complete — 20 modules loaded, 7 driven",
    },
"""
RESET = """    -- BS-4: + BoonService = 53.
    T.test("LOOP1: the contract covers exactly the 53 service files", function()
        T.eq(count(sources), 53, "service file count changed")
"""
PACING = """            T.ok(
                gate:find("53 services loaded", 1, true) ~= nil,
                "EXPECTED_BOOT"
            )
"""


def tree(tmp_path, services=55, with_update=30):
    wt = str(tmp_path)
    os.makedirs(os.path.join(wt, "src", "server", "Services"))
    os.makedirs(os.path.join(wt, "tests", "lune", "specs"))
    for i in range(services):
        body = "function S:Update(dt) end\n" if i < with_update else "return {}\n"
        with open(os.path.join(wt, "src", "server", "Services", f"S{i}.lua"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(body)
    for rel, text in (("tests/GateRunner.lua", GATE),
                      ("tests/lune/specs/MatchReset.spec.luau", RESET),
                      ("tests/lune/specs/MapV2Pacing.spec.luau", PACING)):
        with open(os.path.join(wt, *rel.split("/")), "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("\n", "\r\n"))
    return wt


def read(wt, rel):
    with open(os.path.join(wt, *rel.split("/")), encoding="utf-8", newline="") as f:
        return f.read()


def test_replay_two_lanes_each_added_a_service_and_every_pin_is_set_to_the_real_count(tmp_path):
    """VQ-02: both lanes wrote 53; merged there were 55 services and 30 with an Update, the
    file said 53 / 28, and the merge gate failed its boot check."""
    wt = tree(tmp_path)
    ps = pins.parse(LINES)
    changes = pins.fix(wt, ps)
    gate = read(wt, "tests/GateRunner.lua")
    assert "loaded = 55,\r\n        heartbeat = 30," in gate
    assert 'line = "Boot complete — 55 services loaded, 30 driven by the shared Heartbeat"' in gate
    # the client block has the same words and is not a pin: untouched
    assert "loaded = 20,\r\n        heartbeat = 7," in gate and "20 modules loaded, 7 driven" in gate
    assert "-- BS-4: +BoonService (no Update) = 53 / 28" in gate          # comments are history
    reset = read(wt, "tests/lune/specs/MatchReset.spec.luau")
    assert "exactly the 55 service files" in reset and "T.eq(count(sources), 55," in reset
    assert 'gate:find("55 services loaded"' in read(wt, "tests/lune/specs/MapV2Pacing.spec.luau")
    assert sorted((c["file"], c["from"], c["to"]) for c in changes) == sorted(
        [("tests/GateRunner.lua", 53, 55), ("tests/GateRunner.lua", 28, 30)] * 2
        + [("tests/lune/specs/MapV2Pacing.spec.luau", 53, 55)]
        + [("tests/lune/specs/MatchReset.spec.luau", 53, 55)] * 2)
    assert pins.fix(wt, ps) == []                              # already right: nothing written


def test_a_count_that_cannot_be_taken_never_overwrites_a_pin(tmp_path):
    wt = tree(tmp_path)
    ps = pins.parse([r"tests/GateRunner.lua :: Boot complete — (\d+) services loaded :: "
                     r"src/server/Nowhere/*.lua"])
    assert pins.count(wt, ps[0]) is None and pins.fix(wt, ps) == []
    assert "53 services loaded" in read(wt, "tests/GateRunner.lua")
    # a file with an unresolved conflict is left alone
    with open(os.path.join(wt, "tests", "GateRunner.lua"), "a", encoding="utf-8") as f:
        f.write("<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n")
    assert pins.fix(wt, pins.parse(LINES[:1])) == []


def test_a_conflict_about_the_count_only_resolves_itself(tmp_path):
    ps = pins.parse(LINES)
    text = """    T.test("x", function() end)
<<<<<<< HEAD
    -- REL-1a: + LiveConfigService = 54.
    T.test("LOOP1: the contract covers exactly the 54 service files", function()
        T.eq(count(sources), 54, "service file count changed")
=======
    -- BS-4: + BoonService = 53.
    -- GF-MUT1: + MutatorService = 54.
    T.test("LOOP1: the contract covers exactly the 54 service files", function()
        T.eq(count(sources), 54, "service file count changed")
>>>>>>> lane
    done()
"""
    mine = [p for p in ps if p.file == "tests/lune/specs/MatchReset.spec.luau"]
    out = pins.resolve(text, mine)
    assert "<<<<<<<" not in out and ">>>>>>>" not in out and "=======" not in out
    # master's side, with the lane's own comment line kept after master's comments
    assert out.index("-- GF-MUT1") < out.index("-- REL-1a") < out.index("T.test(\"LOOP1")
    assert out.count("T.eq(count(sources), 54,") == 1 and out.rstrip().endswith("done()")
    # a hunk that differs in more than the count stops for a person
    other = text.replace('"service file count changed")\n=======', '"another message")\n=======')
    with pytest.raises(ValueError):
        pins.resolve(other, mine)
    # both sides wrote the same count: only their comment lines conflict, and both are kept
    both = pins.resolve("<<<<<<< a\n-- x\n=======\n-- y\n>>>>>>> b\nfoo()\n", mine)
    assert both == "-- y\n-- x\nfoo()\n"


def test_parse_refuses_a_line_it_cannot_use_and_a_project_falls_back_to_its_adapter():
    for bad in ("only :: two", r"f :: no group :: g/*.lua", r"f :: (a)(b) :: g", "f :: ( :: g"):
        with pytest.raises(ValueError):
            pins.parse([bad])
    assert pins.parse([]) == [] and len(pins.parse(LINES)) == 7

    class A:
        def count_pins(self):
            return LINES[:2]

    class P:
        adapter = A()
    assert len(pins.for_project(P, [])) == 2
    assert len(pins.for_project(P, LINES[:1])) == 1            # the setting wins
    assert pins.for_project(P, ["garbage"]) == []              # never raises
    assert pins.for_project(object(), []) == []


def test_the_au_adapter_lists_the_pins_the_document_names():
    from foundry_coordinator.projects.animaluprising import adapter as au
    ps = pins.parse(au.COUNT_PINS)
    files = {p.file for p in ps}
    assert files == {"tests/GateRunner.lua", "tests/lune/specs/MapV2Pacing.spec.luau",
                     "tests/lune/specs/MatchReset.spec.luau"}
    assert all(p.glob == "src/server/Services/*.lua" for p in ps)


def _git(cwd, *a):
    import subprocess
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, env=env)


def _lanes(tmp_path, lane_extra=""):
    """master and a lane each add a service and each write 2 into the pin."""
    wt = str(tmp_path / "repo")
    os.makedirs(os.path.join(wt, "svc"))
    os.makedirs(os.path.join(wt, "tests"))

    def put(rel, text):
        with open(os.path.join(wt, *rel.split("/")), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    _git(wt, "init", "-q", "-b", "master")
    put("svc/A.lua", "return {}\n")
    put("tests/Count.lua", "-- base: A = 1\nT.eq(count(sources), 1, \"count\")\nreturn 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    _git(wt, "checkout", "-q", "-b", "lane")
    put("svc/C.lua", "return {}\n")
    put("tests/Count.lua", "-- base: A = 1\n-- LANE: + C = 2\nT.eq(count(sources), 2, "
                           "\"count\")\nreturn 1\n" + lane_extra)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "lane: C")
    _git(wt, "checkout", "-q", "master")
    put("svc/B.lua", "return {}\n")
    put("tests/Count.lua", "-- base: A = 1\n-- MAIN: + B = 2\nT.eq(count(sources), 2, "
                           "\"count\")\nreturn 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "main: B")
    _git(wt, "checkout", "-q", "lane")
    return wt, pins.parse([r"tests/Count.lua :: T\.eq\(count\(sources\), (\d+), :: svc/*.lua"])


def test_replay_a_lane_rebase_that_conflicts_on_the_count_goes_through_and_counts_three(tmp_path):
    from foundry_coordinator.tracks import merge as MG
    wt, ps = _lanes(tmp_path)
    assert _git(wt, "rebase", "master").returncode != 0        # the count line conflicts
    assert MG._resolve_memory_conflicts(wt, "rebase", pins=ps) is True
    assert _git(wt, "status", "--porcelain").stdout.strip() == ""
    text = open(os.path.join(wt, "tests", "Count.lua"), encoding="utf-8").read()
    assert "<<<<<<<" not in text and "-- MAIN: + B = 2" in text and "-- LANE: + C = 2" in text
    assert "T.eq(count(sources), 2," in text                   # both said 2; there are 3
    changes = pins.fix(wt, ps)
    assert [(c["from"], c["to"]) for c in changes] == [(2, 3)]
    assert "T.eq(count(sources), 3," in open(os.path.join(wt, "tests", "Count.lua"),
                                             encoding="utf-8").read()


def test_a_conflict_with_more_than_the_count_still_stops_and_the_rebase_can_be_aborted(tmp_path):
    from foundry_coordinator.tracks import merge as MG
    wt, ps = _lanes(tmp_path)
    # without the project's pins the file is not one the merge may resolve
    assert _git(wt, "rebase", "master").returncode != 0
    assert MG._resolve_memory_conflicts(wt, "rebase") is False
    _git(wt, "rebase", "--abort")
    # with them, but the lane also changed code in the hunk: a person or the Lead
    _git(wt, "checkout", "-q", "master")
    with open(os.path.join(wt, "tests", "Count.lua"), "a", encoding="utf-8", newline="\n") as f:
        f.write("check(1)\n")
    _git(wt, "commit", "-q", "-am", "main: a check")
    _git(wt, "checkout", "-q", "lane")
    with open(os.path.join(wt, "tests", "Count.lua"), "a", encoding="utf-8", newline="\n") as f:
        f.write("check(2)\n")
    _git(wt, "commit", "-q", "-am", "lane: another check")
    assert _git(wt, "rebase", "master").returncode != 0
    assert MG._resolve_memory_conflicts(wt, "rebase", pins=ps) is False
    assert _git(wt, "rebase", "--abort").returncode == 0
    assert _git(wt, "status", "--porcelain").stdout.strip() == ""


def test_the_merge_commits_the_fixed_counts_on_the_lane_and_says_so(tmp_path):
    from tests.coordinator.test_budget_policy import game
    c, P, clk = game(tmp_path / "g")
    wt, ps = _lanes(tmp_path)
    _git(wt, "rebase", "master")
    from foundry_coordinator.tracks import merge as MG
    assert MG._resolve_memory_conflicts(wt, "rebase", pins=ps)
    row = {"name": "lane-c", "sprints": ["REL-1a", "REL-1b"]}
    changes = c.tracks.merger._fix_pins(P, row, wt, ps)
    assert [(x["from"], x["to"]) for x in changes] == [(2, 3)]
    assert _git(wt, "log", "-1", "--format=%s").stdout.strip() == \
        "REL-1b: count pins set to the real counts (2 -> 3)"
    assert _git(wt, "status", "--porcelain").stdout.strip() == ""
    import json
    [e] = [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind='count_pins_fixed'")]
    assert e["track"] == "lane-c" and "tests/Count.lua" in e["text"]
    assert c.tracks.merger._fix_pins(P, row, wt, ps) == []     # right already: no commit
    assert c.tracks.merger._fix_pins(P, row, wt, []) == []
