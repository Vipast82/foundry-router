"""v0.181.246: code that compiles (docs/BENCH_GAPS.md, addition 8, the syntax and
type check the owner chose before the workspace server exists).

Every code case asked what code does; none asked for code. A case here is a
module to write to an exact spec. Nothing a model writes is run: Luau goes to
the operator's Code checks type check (luau-lsp analyze, strict mode, Roblox's
type definitions), Python to its own compiler and parser.
"""
import asyncio
import json
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchwrite as bw, code_checks
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

SPECS = bw.write_specs()
IDS = [sp["task"] for sp in SPECS]
BY_ASK = {sp["messages"][-1]["content"]: sp for sp in SPECS}
ANALYZE = {"name": "luau-types", "languages": ["luau", "lua"], "kind": "service",
           "command": "luau-lsp analyze --definitions=/x.d.luau {file}",
           "url": "http://code-checks:8099", "timeout_seconds": 40, "enabled": True}
STYLUA = dict(ANALYZE, name="luau-syntax", command="stylua --check {file}")


def _svc(tmp_path, pool=None, rules=None):
    checks = type("CC", (), {"rules": list(rules if rules is not None else [STYLUA, ANALYZE]),
                             "enabled": True})()
    cfg = type("Cfg", (), {"code_checks": checks})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _ref(sp):
    return {"lang": sp["lang"], "funcs": sp["funcs"]}


def _ok(code):
    return ""


# -- reading a reply ----------------------------------------------------------------------------

def test_the_code_is_every_block_in_the_language_and_nothing_else():
    text = ("Here:\n```lua\nlocal a = 1\n```\nand\n```luau\nlocal b = 2\n```\n"
            "```python\nx = 1\n```\n```\nplain\n```")
    assert bw.code_of(text, "luau") == "local a = 1\n\nlocal b = 2"
    assert bw.code_of(text, "python") == "x = 1"
    assert bw.code_of("no code at all", "luau") == ""


def test_parameter_names_are_read_through_function_types_and_tables():
    assert bw._params("part: BasePart, callback: (Player) -> ()") == ["part", "callback"]
    assert bw._params("store: DataStore, key: string, data: {[string]: number}") == [
        "store", "key", "data"]
    assert bw._params("") == [] and bw._params("ranges: list[tuple[int, int]]") == ["ranges"]


def test_the_luau_shape_is_strict_returns_its_table_and_has_every_function():
    want = [("paint", "part: BasePart, color: Color3", "()", "")]
    good = "--!strict\nlocal M = {}\nfunction M.paint(part: BasePart, color: Color3)\nend\nreturn M"
    assert bw.luau_shape(good, want) == []
    assert bw.luau_shape(good.replace("function M.paint(", "M.paint = function("), want) == []
    assert bw.luau_shape(good.replace("--!strict\n", ""), want) == ["not_strict"]
    assert "checking_off" in bw.luau_shape(good + "\n--!nocheck", want)
    assert bw.luau_shape(good.replace("return M", "return {}"), want) == ["no_return_M"]
    assert bw.luau_shape(good.replace("paint(", "colour("), want) == ["missing:paint"]
    assert bw.luau_shape(good.replace("color: Color3", "c: Color3"), want) == ["params:paint"]


def test_the_python_shape_is_compiled_read_and_never_run():
    want = [("mean", "values: list[float]", "float", "")]
    good = "def mean(values: list[float]) -> float:\n    return 0.0\n"
    assert bw.py_shape(good, want) == []
    assert bw.py_shape("def mean(values:\n", want)[0].startswith("syntax:")
    assert bw.py_shape("import numpy\n" + good, want) == ["import:numpy"]
    assert bw.py_shape("import os, json\nfrom collections import Counter\n" + good, want) == []
    assert bw.py_shape(good.replace("-> float", ""), want) == ["untyped:mean"]
    assert bw.py_shape(good.replace("values", "xs"), want) == ["params:mean"]
    assert bw.py_shape("x = 1\n", want) == ["missing:mean"]
    assert bw.py_shape("raise SystemExit('ran')\n" + good, want) == [], "compiled, never run"


def test_the_score_is_nothing_for_broken_code_half_for_the_wrong_module_and_full_for_both():
    sp = bw.write_spec("luau", single="paint")
    ref = _ref(sp)
    assert bw.write_score(sp["ideal"], ref, _ok)["score"] == 100
    s = bw.write_score(sp["ideal"], ref, lambda c: "snippet.luau(4,2): TypeError: Key 'Colour' "
                                                   "not found in external type 'BasePart'")
    assert s["score"] == 0 and s["why"] == "check" and "first=snippet.luau(4,2):_TypeError" in \
        s["detail"]
    s = bw.write_score(sp["ideal"], ref, lambda c: "x(9,1): SyntaxError: Expected 'end'")
    assert s["why"] == "syntax" and s["score"] == 0
    loose = sp["ideal"].replace("--!strict", "--!nonstrict")
    assert bw.write_score(loose, ref, _ok)["why"] == "loose"
    renamed = sp["ideal"].replace("M.paint(", "M.colour(")
    s = bw.write_score(renamed, ref, _ok)
    assert s["score"] == 50 and s["why"] == "shape" and "shape=missing:paint" in s["detail"]
    assert bw.write_score("I would rather not.", ref, _ok)["why"] == "none"
    assert bw.write_score(sp["ideal"], ref, None)["why"] == "unchecked"
    py = bw.write_spec("python", 2)
    assert bw.write_score(py["ideal"], _ref(py))["score"] == 100
    assert bw.write_score(py["ideal"].replace("def ", "def  (", 1), _ref(py))["why"] == "syntax"


def test_only_an_analyzer_is_used_never_a_formatter():
    cfg = type("C", (), {"rules": [STYLUA, dict(ANALYZE, enabled=False)]})()
    assert bw.luau_rule(cfg) is None
    cfg.rules = [STYLUA, ANALYZE]
    assert bw.luau_rule(cfg)["name"] == "luau-types"
    assert bw.luau_rule(None) is None


# -- every case ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sp", SPECS, ids=IDS)
def test_every_spec_has_a_module_that_meets_it(sp):
    ref = _ref(sp)
    got = bw.write_score(sp["ideal"], ref, _ok if sp["lang"] == "luau" else None)
    assert got["score"] == 100, got["detail"]
    ask = sp["messages"][-1]["content"]
    for name, params, ret, _what in sp["funcs"]:
        assert name in ask and params in ask and ret in ask
    assert ("```luau" in ask) == (sp["lang"] == "luau")


def test_two_ladders_and_every_roblox_function_alone():
    ladders: dict = {}
    for sp in SPECS:
        if sp["ladder"]:
            ladders.setdefault(sp["ladder"], []).append(len(sp["funcs"]))
    assert ladders == {"code:luau module size": list(bw.SIZES),
                       "code:python module size": list(bw.SIZES)}
    singles = [sp["single"] for sp in SPECS if sp["single"]]
    assert singles == [f[0] for f in bw.LUAU_FUNCS] and len(singles) == 24
    assert len(SPECS) == 40 and len({sp["task"] for sp in SPECS}) == 40
    for lang in bw.LANGS:          # each rung is the one below it and more
        rungs = [sp["funcs"] for sp in SPECS if sp["ladder"] == f"code:{lang} module size"]
        assert all(b[:len(a)] == a for a, b in zip(rungs, rungs[1:]))
    assert bw.write_specs() == SPECS
    assert len(bw.write_specs(3)) == 2 * 3 + 24


# -- built and run ------------------------------------------------------------------------------

def test_the_suite_is_built_with_no_tier_of_its_own(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_codewrite(svc)
    cases = bench.cases(svc, "codewrite")
    assert out["cases"] == len(cases) == 40
    for c in cases:
        assert c["reference"]["codewrite"]["funcs"] and c["max_tokens"] == bench.THINK_ROOM
        assert not any(t.startswith("tier:") for t in c["tags"]) and "weight" not in c
    one = next(c for c in cases if c["id"] == "WRITE-luau-paint")
    assert bench.expected_of(one) == ("a strict Luau ModuleScript that passes the type check, "
                                      "defining paint(part: BasePart, color: Color3)")
    assert bench.ladder_of(next(c for c in cases if c["id"] == "WRITE-python-L3")) == (
        "code:python module size", 3)


class _Voice:
    def __init__(self, how):
        self.how = how

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        return ChatResult(content=self.how(BY_ASK[messages[-1]["content"]]),
                          finish_reason="stop", completion_tokens=300,
                          prompt_tokens=400), "b"


def _run(tmp_path, how, keep, said="", rules=None):
    svc = _svc(tmp_path, _Voice(how), rules)
    bench.build_codewrite(svc)
    cases = [c for c in bench.cases(svc, "codewrite") if keep(c)]
    (bench._dir(svc) / "codewrite.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    seen = []

    async def fake(rule, code, lang):
        seen.append((rule["name"], lang))
        if isinstance(said, Exception):
            raise said
        return said
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}), \
            um.patch.object(code_checks, "run_one", fake):
        out = asyncio.run(bench._run(svc, "codewrite", "m", "", 0))
    rows = svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id", (out["run_id"],))
    return out, rows, seen


def test_a_module_that_checks_is_full_and_luau_goes_to_the_analyzer(tmp_path):
    out, rows, seen = _run(tmp_path, lambda sp: sp["ideal"],
                           lambda c: c["id"] in ("WRITE-luau-L2", "WRITE-python-L2",
                                                 "WRITE-luau-heal"))
    assert out["score"] == 100.0 and len(rows) == 3
    assert seen == [("luau-types", "luau")] * 2, "the analyzer, never stylua; python not sent"
    assert all(benchcard.miss_kind("codewrite", r) is None for r in rows)


def test_a_type_error_is_nothing_and_the_card_says_so(tmp_path):
    out, rows, _ = _run(tmp_path, lambda sp: sp["ideal"],
                        lambda c: c["id"] == "WRITE-luau-heal",
                        said="snippet.luau(5,2): TypeError: Key 'Helth' not found")
    assert out["score"] == 0.0 and "why=check" in rows[0]["code_detail"]
    assert benchcard.miss_kind("codewrite", rows[0]) == "does not type check"


def test_without_a_luau_checker_the_case_is_not_scored_and_says_why(tmp_path):
    (tmp_path / "a").mkdir()
    _out, rows, seen = _run(tmp_path / "a", lambda sp: sp["ideal"],
                            lambda c: c["id"] == "WRITE-luau-heal", rules=[STYLUA])
    assert seen == [] and rows[0]["score"] is None
    assert "no Luau type check is set up" in rows[0]["error"]
    (tmp_path / "b").mkdir()
    _out, rows, _ = _run(tmp_path / "b", lambda sp: sp["ideal"],
                         lambda c: c["id"] == "WRITE-luau-heal",
                         said=code_checks.Unavailable("the code-checks service is not up"))
    assert rows[0]["score"] is None and "could not run" in rows[0]["error"]


def test_the_wrong_module_is_half_and_no_code_is_nothing(tmp_path):
    (tmp_path / "a").mkdir()
    out, rows, _ = _run(tmp_path / "a", lambda sp: sp["ideal"].replace("def ", "def x_"),
                        lambda c: c["id"] == "WRITE-python-L2")
    assert out["score"] == 50.0 and benchcard.miss_kind("codewrite", rows[0]) == \
        "not the module asked for"
    (tmp_path / "b").mkdir()
    out, rows, _ = _run(tmp_path / "b", lambda sp: "Sorry.", lambda c: c["id"] == "WRITE-luau-L1")
    assert out["score"] == 0.0 and benchcard.miss_kind("codewrite", rows[0]) == \
        "wrote no code in the language"


# -- the card, the route, the page --------------------------------------------------------------

def test_the_card_names_the_suite_and_breaks_it_down():
    def row(lang, funcs, score, detail, func=""):
        tags = ["codewrite", f"lang:{lang}", f"funcs:{funcs}"] + ([f"func:{func}"] if func else [])
        return {"score": score, "error": "", "code_detail": detail, "tags": json.dumps(tags),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x", "case_id": "c"}
    rows = [row("luau", 1, 100.0, "lines=5 funcs=1", "heal"),
            row("luau", 1, 0.0, "lines=5 funcs=1 why=check", "raycastDown"),
            row("luau", 13, 0.0, "lines=90 funcs=13 why=syntax"),
            row("python", 4, 50.0, "lines=20 funcs=4 why=shape shape=missing:mean"),
            row("python", 24, 100.0, "lines=120 funcs=24")]
    f = {x["name"]: x["rows"] for x in benchcard.facets("codewrite", rows)}
    assert [(r["label"], r["n"]) for r in f["The language"]] == [
        ("strict Luau (Roblox)", 3), ("Python", 2)]
    assert [(r["label"], r["n"]) for r in f["Size of the module"]] == [
        ("one function", 2), ("2 to 6 functions", 1), ("9 to 24 functions", 2)]
    assert {r["label"] for r in f["Each Roblox function alone"]} == {"heal", "raycastDown"}
    assert [benchcard.miss_kind("codewrite", r) for r in rows] == [
        None, "does not type check", "does not compile", "not the module asked for", None]
    assert benchcard.SUITE_NAMES["codewrite"] == "Code that compiles"
    assert "codewrite" in benchcard.SUITE_ASKS


def test_the_route_builds_it_and_the_page_has_its_plate(tmp_path):
    from foundry_router.ui import routes
    svc = _svc(tmp_path)

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body
    import unittest.mock as um
    with um.patch.object(routes, "_svc", lambda request: svc):
        out = asyncio.run(routes.bench_codewrite(Req({})))
        less = asyncio.run(routes.bench_codewrite(Req({"top_level": 4, "name": "w4"})))
    assert out["ok"] and out["cases"] == 40 and less["cases"] == 32 and less["name"] == "w4"
    assert "/admin/api/bench/codewrite" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-codewrite"', 'id="bench-codewrite-top"', 'api("bench/codewrite"',
                 'suiteNote("codewrite")'):
        assert want in page, want
