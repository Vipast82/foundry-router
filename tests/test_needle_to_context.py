"""v0.181.216: the needle suite runs out to each local model's own window.

Asked: "bump the needle testing all the way to the max context the model
supports". Strata holds 262,144 tokens in one slot and the brain lane 65,536,
and both share one suite, so it is sized to every local window - half of it
and nearly all of it - and a case longer than the running model's window is not
sent: a 233k-token case to a 65k model would fail and score 0, which says
nothing about recall and is shown as the end of its grid instead.
"""
import asyncio
import json

from foundry_router import bench, benchcard
from foundry_router.db import Database


def test_lengths_reach_each_window_and_never_past_it():
    lengths = bench.context_lengths([65536, 262144])
    assert len(lengths) == 4
    for c, (half, most) in ((65536, lengths[:2]), (262144, lengths[2:])):
        est_most = most / bench.CHARS_PER_TOKEN + bench.OUTPUT_ROOM
        assert est_most <= c, "sized at 3.3 chars a token, it can never be over"
        real_most = most / 3.5                        # what this filler measured
        assert real_most > 0.85 * c, "and it still reaches most of the window"
        assert half < most


def test_a_small_window_adds_nothing_the_defaults_already_cover():
    assert bench.context_lengths([8192, 16384]) == []
    assert bench.context_lengths([None, "x"]) == []


def _svc(tmp_path, contexts=None):
    meta = {m: {"context_length": c} for m, c in (contexts or {}).items()}
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "n.sqlite"),
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: meta.get(m))})(),
    })()


def test_the_haystack_is_as_long_as_the_longest_case(tmp_path):
    svc = _svc(tmp_path)
    bench.build_needle(svc, per_cell=1, depths=[50], lengths=[4000, 420000], decoys=0)
    longest = max(bench.cases(svc, "needle"),
                  key=lambda c: len(c["messages"][1]["content"]))
    assert len(longest["messages"][1]["content"]) >= 420000


def test_long_lengths_get_one_case_per_depth_and_one_decoy(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_needle(svc, per_cell=3, lengths=[4000, 200000], decoys=3)
    short = 5 * 3 + 3
    long = 5 * 1 + 1
    assert out["cases"] == short + long


def test_every_case_carries_its_token_estimate(tmp_path):
    svc = _svc(tmp_path)
    bench.build_needle(svc, per_cell=1, lengths=[16000], decoys=1)
    for c in bench.cases(svc, "needle"):
        t = next(t for t in c["tags"] if t.startswith("tokens:"))
        est = int(t.split(":")[1])
        assert est == bench.case_tokens(c)
        assert est >= len(c["messages"][1]["content"]) / 3.5, "never an under-count"


def test_a_case_past_the_window_is_not_sent_and_not_a_miss(tmp_path, monkeypatch):
    svc = _svc(tmp_path, {"small": 8192})
    bench.build_needle(svc, per_cell=1, depths=[50], lengths=[4000, 64000], decoys=0)
    sent = []

    async def fake_ask(svc_, model, case, *a, **k):
        sent.append(case["id"])
        code = case["reference"]["must_include"][0]
        return {"content": code, "seconds": 1.0}

    monkeypatch.setattr(bench, "ask", fake_ask)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    out = asyncio.run(bench._run(svc, "needle", "small", "", 0))
    assert sent == ["NEEDLE-4k-d50-0"], "the 64k case does not fit 8,192 tokens"
    assert out["score"] == 100.0, "an unsent case is not averaged in as a 0"
    assert out["beyond"] == 1
    note = svc.db.query_one("SELECT note FROM bench_runs WHERE id=?", (out["run_id"],))["note"]
    assert "longer than this model's 8,192-token context were not sent" in note
    grid = {(c["chars"], c["depth"]): c for c in bench.needle_breakdown(svc.db, out["run_id"])}
    assert grid[(64000, 50)]["beyond"] is True and grid[(64000, 50)]["score"] is None
    assert grid[(4000, 50)]["score"] == 100.0
    row = svc.db.query_one("SELECT * FROM bench_results WHERE case_id='NEEDLE-64k-d50-0'")
    assert benchcard.miss_kind("needle", row) is None


def test_a_model_with_no_known_window_gets_every_case(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    bench.build_needle(svc, per_cell=1, depths=[50], lengths=[4000, 64000], decoys=0)
    sent = []

    async def fake_ask(svc_, model, case, *a, **k):
        sent.append(case["id"])
        return {"content": "", "seconds": 1.0}

    monkeypatch.setattr(bench, "ask", fake_ask)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    asyncio.run(bench._run(svc, "needle", "unknown-model", "", 0))
    assert len(sent) == 2


def test_only_needle_cases_are_ever_held_back(tmp_path, monkeypatch):
    """The replay's long prompts are sized by its own builder; nothing else is
    skipped on an estimate."""
    svc = _svc(tmp_path, {"small": 1000})
    (bench._dir(svc) / "alpha.jsonl").write_text(json.dumps(
        {"id": "c1", "task_id": "t", "tags": ["luau"],
         "messages": [{"role": "user", "content": "x" * 20000}],
         "reference": {"content": "ok"}}) + "\n", encoding="utf-8")
    sent = []

    async def fake_ask(svc_, model, case, *a, **k):
        sent.append(case["id"])
        return {"content": "ok", "seconds": 1.0}

    monkeypatch.setattr(bench, "ask", fake_ask)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    asyncio.run(bench._run(svc, "alpha", "small", "", 0))
    assert sent == ["c1"]


def test_the_card_grid_and_labels_show_it():
    from pathlib import Path

    import foundry_router
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert "c.beyond" in page
    assert benchcard._kchars(815000) == "815k chars (~233k tokens)"
