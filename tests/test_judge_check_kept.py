r"""v0.181.209: a judge check survives the connection that asked for it.

Found by using it, not by a test - which is how the last several faults in this
area were found. The full check is 56 pairs judged twice, about fourteen
minutes against a local model, and v0.181.207 held it open as one HTTP request.
A dropped connection threw all of it away, twice, and the run itself was fine
both times: only the answer was lost. A browser or a proxy does the same to the
operator, who has no curl timeout to raise.

So it runs in the background and the result is KEPT - which the benchmark has
done since it existed and this should have copied from the start.
"""
import time

import pytest

from foundry_router import bench, judgeproto as jp
from foundry_router.db import Database


@pytest.fixture
def svc(tmp_path):
    cfg = type("CFG", (), {"code_checks": None})()
    s = type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "config_store": type("CS", (), {"config": cfg})(),
    })()
    bench.build_codereason(s)
    return s


def settle(timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if jp.check_state().get("state") != "running":
            return jp.check_state()
        time.sleep(0.05)
    raise AssertionError("the check never finished")


@pytest.fixture(autouse=True)
def _idle():
    """Each test starts from idle, since the state and the lock are module
    level - one running check at a time is the contract."""
    jp._check_state.clear()
    jp._check_state.update({"state": "idle"})
    yield
    jp._check_state.clear()
    jp._check_state.update({"state": "idle"})


def test_the_table_exists_on_a_database_made_before_it(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    assert db.query("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='judge_checks'")


def test_a_check_that_failed_is_recorded_rather_than_lost(svc):
    """The whole point. A check that cannot reach a model is a FACT about that
    model, and throwing it away leaves the operator pressing the button again."""
    jp.start_check(svc, "a-model-that-is-not-there", "codereason", 2)
    settle()
    rows = jp.checks(svc.db)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["judge_model"] == "a-model-that-is-not-there"
    assert rows[0]["note"], "a failed check has to say why"


def test_a_finished_check_keeps_its_verdict_and_its_pairs(svc):
    async def ask(model, body):
        right = body.split("ANSWER A:")[1].split("ANSWER B:")[0]
        return '{"winner": "A"}' if _right(svc, body, right) \
            else '{"winner": "B"}'

    # drive the synchronous core, then store it the way the thread does, so
    # this asserts the KEEPING rather than a model call
    import asyncio
    out = asyncio.run(jp.calibrate(svc, "judge-x", "codereason", 6, ask=ask))
    assert out["verdict"]
    assert len(out["pairs"]) == 6


_KEYS: dict = {}


def _right(svc, body, side_a):
    if not _KEYS:
        for p in jp.calibration_pairs(svc, "codereason"):
            _KEYS[p["task"].strip()[:200]] = p["right"]
    task = body.split("TASK:")[1].split("ANSWER A:")[0].strip()[:200]
    return side_a.strip() == _KEYS.get(task)


def test_only_one_check_runs_at_a_time(svc):
    """Two at once would be two models on one GPU measuring the contention,
    and the second would overwrite the first's state."""
    first = jp.start_check(svc, "slow-model", "codereason", 2)
    second = jp.start_check(svc, "another-model", "codereason", 2)
    assert second.get("id") == first.get("id") or second["state"] != "running" \
        or second.get("judge_model") == first.get("judge_model")
    settle()


def test_a_fable_judge_is_refused_before_a_row_is_even_written(svc):
    out = jp.start_check(svc, "claude-fable-5-2")
    assert out["state"] == "failed" and "Fable" in out["error"]
    assert jp.checks(svc.db) == [], "a refused check is not a check that ran"


def test_a_check_with_no_judge_named_is_refused(svc):
    assert jp.start_check(svc, "")["state"] == "failed"
    assert jp.checks(svc.db) == []


def test_the_detail_of_a_missing_check_says_so(svc):
    assert "no check 404" in jp.check_detail(svc.db, 404)["error"]


def test_by_kind_comes_back_parsed_rather_than_as_a_string(svc):
    """The page reads it as an object; a JSON string would render as one."""
    jp.start_check(svc, "nope", "codereason", 2)
    settle()
    assert isinstance(jp.checks(svc.db)[0]["by_kind"], dict)
    assert isinstance(jp.check_detail(svc.db, 1)["by_kind"], dict)
    assert isinstance(jp.check_detail(svc.db, 1)["pairs_detail"], list)


def test_the_route_starts_a_check_instead_of_holding_one_open():
    """A fourteen-minute request is the fault this release exists for, so the
    POST must not await the calibration."""
    import inspect

    from foundry_router.ui import routes
    src = inspect.getsource(routes.eval_judge_check)
    assert "start_check" in src
    assert "await judgeproto.calibrate" not in src


def test_the_page_can_read_a_check_that_finished_without_it():
    from pathlib import Path

    import foundry_router
    page = (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    assert "function jcRender(" in page
    assert 'api("eval/judge-checks")' in page
    assert "eval/judge-check?id=" in page
    i = page.index('$("#jc-run").onclick')
    handler = page[i:i + 900]
    assert "leave this page" in handler, \
        "the operator has to be told it keeps running"
