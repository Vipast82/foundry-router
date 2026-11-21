"""v0.181.222: rerun chosen suites into an existing profile, or fold one in.

Asked after the protocol suite was fixed: a rerun of protocol and toolflow made
profiles 7 and 8 of their own, while cards 5 and 6 - six suites each, 2 h 15 m
and 3 h 45 m of runs - kept the old protocol and toolflow. A card had no way to
take a new run of one suite, so a fixed suite cost the whole profile again.
Now a rerun goes into the card it names, and a profile already run on its own
folds into an older card; the runs they replace are kept, marked, never deleted.
"""
import asyncio
import json
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchprofile as bp

from tests.test_bench_profile import profile, svc_with


def _run_rows(svc, pid):
    return svc.db.query("SELECT id, suite, score, replaced_by, profile_id FROM bench_runs "
                        "WHERE profile_id=? ORDER BY id", (pid,))


def _fake_run(svc, scores, status="done", seen=None):
    """bench._run as far as a profile sees it: one bench_runs row, its id back."""
    async def run(_svc, suite, model, label, limit):
        rid = svc.db.execute(
            "INSERT INTO bench_runs (ts, suite, model, label, cases, scored, score, "
            "seconds, status) VALUES (datetime('now'),?,?,?,10,10,?,1.0,?)",
            (suite, model, label, scores[suite], status))
        if seen is not None:
            seen.append(suite)
        return {"run_id": rid}
    return run


@pytest.fixture(autouse=True)
def _idle():
    bp._state.clear()
    bp._state.update({"state": "idle"})
    yield
    bp._state.clear()
    bp._state.update({"state": "idle"})


# -- rerun into a card -------------------------------------------------------------------

def test_a_rerun_replaces_only_its_suite_and_the_composite_follows(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = profile(svc, "flash", {"protocol": 55.2, "toolflow": 95.0, "needle": 100.0})
    monkeypatch.setattr(bench, "_run", _fake_run(svc, {"protocol": 81.0}))
    card = asyncio.run(bp._run(svc, pid, "flash", ["protocol"], "", 1, 0, into=True))
    scores = {p["suite"]: p["score"] for p in card["parts"]}
    assert scores == {"protocol": 81.0, "toolflow": 95.0, "needle": 100.0}
    assert card["composite"] == round((81.0 + 95.0 + 100.0) / 3, 1)
    rows = _run_rows(svc, pid)
    old = [r for r in rows if r["suite"] == "protocol" and r["score"] == 55.2][0]
    new = [r for r in rows if r["suite"] == "protocol" and r["score"] == 81.0][0]
    assert old["replaced_by"] == new["id"], "kept, marked, not deleted"
    p = svc.db.query_one("SELECT * FROM bench_profiles WHERE id=?", (pid,))
    assert p["score"] == card["composite"] and p["status"] == "done"
    part = [x for x in card["parts"] if x["suite"] == "protocol"][0]
    assert part["replaced"][0]["score"] == 55.2
    assert "rerun into this card: protocol" in card["note"] and "was 55.2" in card["note"]


def test_a_stopped_or_failed_rerun_replaces_nothing(tmp_path, monkeypatch):
    for status in ("stopped", "failed"):
        (tmp_path / status).mkdir()
        svc = svc_with(tmp_path / status)
        pid = profile(svc, "flash", {"protocol": 55.2, "toolflow": 95.0})
        seen = []
        monkeypatch.setattr(bench, "_run", _fake_run(
            svc, {"protocol": 0.0, "toolflow": 0.0}, status=status, seen=seen))
        card = asyncio.run(bp._run(svc, pid, "flash", ["protocol", "toolflow"], "", 1, 0,
                                   into=True))
        assert {p["suite"]: p["score"] for p in card["parts"]} == \
            {"protocol": 55.2, "toolflow": 95.0}
        assert seen == ["protocol"], "nothing after it runs"
        loose = svc.db.query("SELECT profile_id FROM bench_runs WHERE score=0.0")
        assert [r["profile_id"] for r in loose] == [None], "a run of its own"


def test_repeats_replace_what_was_there_and_keep_each_other(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = profile(svc, "m", {"protocol": [40.0, 50.0]}, repeats=2)
    scores = iter([70.0, 90.0])

    async def run(_svc, suite, model, label, limit):
        rid = svc.db.execute(
            "INSERT INTO bench_runs (ts, suite, model, cases, score, seconds, status) "
            "VALUES (datetime('now'),?,?,10,?,1.0,'done')", (suite, model, next(scores)))
        return {"run_id": rid}
    monkeypatch.setattr(bench, "_run", run)
    card = asyncio.run(bp._run(svc, pid, "m", ["protocol"], "", 2, 0, into=True))
    part = card["parts"][0]
    assert part["runs"] == 2 and part["score"] == 80.0
    assert len(part["replaced"]) == 2


def test_start_refuses_what_it_cannot_do(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = profile(svc, "flash", {"protocol": 55.2})
    monkeypatch.setattr(bp, "runnable_suites", lambda s: ["protocol", "toolflow"])
    with pytest.raises(ValueError, match="no profile 99"):
        bp.start(svc, "flash", ["protocol"], into=99)
    with pytest.raises(ValueError, match="same model"):
        bp.start(svc, "brain", ["protocol"], into=pid)
    with pytest.raises(ValueError, match="name the suites"):
        bp.start(svc, "flash", [], into=pid)
    with pytest.raises(ValueError, match="not built: needle"):
        bp.start(svc, "flash", ["needle"], into=pid)


def test_start_into_runs_on_the_cards_own_profile_and_passes(tmp_path, monkeypatch):
    """The function behind the button, end to end on its thread."""
    svc = svc_with(tmp_path)
    pid = profile(svc, "flash", {"protocol": 55.2, "toolflow": 95.0}, repeats=1)
    monkeypatch.setattr(bp, "runnable_suites", lambda s: ["protocol", "toolflow", "agent"])
    monkeypatch.setattr(bench, "_run", _fake_run(svc, {"protocol": 81.0}))
    monkeypatch.setattr(bench, "state", lambda: {"state": "idle"})
    import foundry_router.loopclient as lc
    monkeypatch.setattr(lc, "run_on_own_loop", lambda svc_, coro: asyncio.run(coro))
    before = svc.db.query_one("SELECT COUNT(*) AS n FROM bench_profiles")["n"]
    out = bp.start(svc, "", ["protocol"], into=pid)
    assert out["profile_id"] == pid and out["into"] is True
    for _ in range(100):
        if bp._state.get("state") != "running":
            break
        import time
        time.sleep(0.02)
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM bench_profiles")["n"] == before
    p = svc.db.query_one("SELECT * FROM bench_profiles WHERE id=?", (pid,))
    assert p["score"] == 88.0 and p["status"] == "done"


# -- fold a profile in -------------------------------------------------------------------

def test_fold_moves_the_suites_and_removes_the_empty_profile(tmp_path):
    svc = svc_with(tmp_path)
    five = profile(svc, "flash", {"baseline": 79.9, "protocol": 55.2, "toolflow": 95.0})
    seven = profile(svc, "flash", {"protocol": 81.0, "toolflow": 97.5}, label="v0.181.221")
    out = bp.fold(svc, seven, five)
    assert sorted(out["suites"]) == ["protocol", "toolflow"] and out["replaced"] == 2
    card = bp.build_card(svc, five)
    assert {p["suite"]: p["score"] for p in card["parts"]} == \
        {"baseline": 79.9, "protocol": 81.0, "toolflow": 97.5}
    assert out["composite"] == card["composite"]
    assert svc.db.query_one("SELECT id FROM bench_profiles WHERE id=?", (seven,)) is None
    kept = svc.db.query("SELECT score FROM bench_runs WHERE replaced_by IS NOT NULL")
    assert sorted(r["score"] for r in kept) == [55.2, 95.0], "the replaced runs stay"


def test_fold_refuses_another_model_itself_and_a_running_profile(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "flash", {"protocol": 1.0})
    b = profile(svc, "brain", {"protocol": 2.0})
    with pytest.raises(ValueError, match="same model"):
        bp.fold(svc, b, a)
    with pytest.raises(ValueError, match="itself"):
        bp.fold(svc, a, a)
    c = profile(svc, "flash", {"protocol": 3.0})
    bp._state.update({"state": "running", "profile_id": c})
    with pytest.raises(ValueError, match="running"):
        bp.fold(svc, c, a)


def test_deleting_a_profile_takes_its_replaced_runs_too(tmp_path):
    svc = svc_with(tmp_path)
    five = profile(svc, "flash", {"protocol": 55.2})
    seven = profile(svc, "flash", {"protocol": 81.0})
    bp.fold(svc, seven, five)
    monkey = bench.delete_runs
    out = bp.delete(svc, [five])
    assert out["deleted"] == 1 and out["runs"] == 2
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM bench_runs")["n"] == 0
    assert monkey is bench.delete_runs


# -- the route and the buttons ------------------------------------------------------------

def test_the_routes_take_into_and_fold_and_the_misses_skip_replaced_runs():
    from foundry_router.ui import routes
    paths = {r.path for r in routes.router.routes}
    assert "/admin/api/bench/profile/fold" in paths
    import inspect
    src = inspect.getsource(routes.bench_profile_run)
    assert 'into=int(b.get("into") or 0)' in src
    assert "replaced_by IS NULL" in inspect.getsource(routes.bench_failures)


def test_the_card_has_the_buttons_when_opened():
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert "pfCard(c, {redo: true})" in page and "pfWireRedo(c);" in page
    assert 'id="pf-redo-go"' in page and 'id="pf-fold-go"' in page
    assert 'api("bench/profile/fold"' in page
    assert "into: +go.dataset.id" in page
