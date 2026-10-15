"""Training capture wired into the coordinator engine and Foundry: closeout
manifests under each failure policy, preference pairs, and the Foundry
training endpoints / admin view."""

from __future__ import annotations

import json
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator import training_hook
from foundry_training.curate import record_human_preference
from foundry_training.export import export
from tests.coordinator.helpers import coordinator as make_coordinator
from tests.coordinator.helpers import make_repo, run_until, write_ok, write_profile
from tests.training.helpers import (KILO_EXPORT, SESSION, capture_db, capture_rows, clone_session,
                                    coordinator, git, kilo_export, pipeline, profile, workspace,
                                    write_json)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}


def _engine(tmp_path, on_failure="warn", enabled=True):
    root, _ = make_repo(tmp_path, "game", [T1])
    extra = (f'[training]\nenabled = {"true" if enabled else "false"}\n'
             f'spool = "{(tmp_path / "spool").as_posix()}"\n'
             f'[training.capture]\non_failure = "{on_failure}"\n')
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = make_coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor", "pilot")
    return c, root


def test_engine_writes_a_closeout_manifest_on_complete(tmp_path):
    c, root = _engine(tmp_path)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE)
    m, = [json.loads(p.read_text()) for p in (tmp_path / "spool").glob("*.json")]
    head = git(root, "rev-parse", "HEAD")
    assert m["outcome"] == "COMPLETE" and m["head_sha"] == head and m["reviewed_sha"] == head
    assert m["sessions"] and m["verification_sets"] and m["reviews"][0]["verdict"] == "APPROVED"
    assert any(e["kind"] == "training_queued" for e in c.store.events("game", 50))


def test_engine_without_training_writes_nothing(tmp_path):
    c, _ = _engine(tmp_path, enabled=False)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE)
    assert not (tmp_path / "spool").exists()


def test_capture_failure_warns_and_never_blocks_by_default(tmp_path, monkeypatch):
    c, _ = _engine(tmp_path, on_failure="warn")

    def broken(*a, **k):
        raise OSError("spool disk full")
    monkeypatch.setattr(training_hook, "finalize", broken)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE)
    ev = [e for e in c.store.events("game", 100) if e["kind"] == "training_capture_failed"]
    assert ev and "spool disk full" in json.dumps(ev[0]["detail"])


def test_capture_failure_with_hold_keeps_the_task_closing_until_written(tmp_path, monkeypatch):
    c, root = _engine(tmp_path, on_failure="hold")
    real = training_hook.finalize

    def broken(*a, **k):
        raise OSError("spool disk full")
    monkeypatch.setattr(training_hook, "finalize", broken)
    run_until(c, lambda: False, limit=25)
    t = c.store.task("game", "T1")
    assert t["state"] == S.CLOSING and "training capture" in t["next_action"]
    assert t["data"]["training_hold"]["head"] == git(root, "rev-parse", "HEAD")
    n_ver = len(c.store.q("SELECT * FROM verifications WHERE phase='closeout'"))
    run_until(c, lambda: False, limit=3)               # retrying doesn't re-run closeout checks
    assert len(c.store.q("SELECT * FROM verifications WHERE phase='closeout'")) == n_ver
    monkeypatch.setattr(training_hook, "finalize", real)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=3)
    assert "training_hold" not in c.store.task("game", "T1")["data"]
    assert list((tmp_path / "spool").glob("*.json"))


# preference pairs ----------------------------------------------------------------------

def test_preference_pairs_need_the_same_decision_and_a_justification(tmp_path):
    ws, base, head1 = workspace(tmp_path)
    prof = profile(tmp_path, "fixture-game", ws)
    st = coordinator(tmp_path, "fixture-game", "FX-1", SESSION, base, head1)
    (tmp_path / "fixture-game" / "src" / "greeting.txt").write_text("hello\n")
    git(ws, "commit", "-qam", "FX-1: second try")
    head2 = git(ws, "rev-parse", "HEAD")
    s2 = "ses_second0000000000000000006"
    other = clone_session(kilo_export(), s2, attempt=2, write_content="hello\n",
                          start_ms=int(time.time() * 1000))
    coordinator(tmp_path, "fixture-game", "FX-1", s2, head1, head2, attempt=2, store=st,
                checks=(("fixture-check", "FAIL"),), verdict="")
    p = pipeline(tmp_path, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT), write_json(tmp_path / "o.json", other)])
    p.ingest_foundry(capture_db=capture_db(tmp_path, capture_rows(kilo_export())
                                           + capture_rows(other, session=s2)))
    p.ingest_coordinator(st.path)
    res = p.curate("fixture-game")
    # only the first call shares its request context (system + same prompt);
    # later calls saw different tool results, so they are never paired
    assert res["preference_pairs"] == 1
    pair, = p.store.cur.q("SELECT * FROM preferences")
    calls = {e["event_id"]: e for e in p.store.events("fixture-game", "event_type='model_call'")}
    assert calls[pair["chosen_event"]]["session_id"] == SESSION
    assert calls[pair["rejected_event"]]["session_id"] == s2
    assert "PASS vs FAIL" in pair["basis"]
    m = export(p.store, ["fixture-game"], "preference", "dpo", {"fixture-game": p._cfg("fixture-game")})
    assert m["validation"]["ok"] and m["examples"] == 1
    line = json.loads((p.store.root / "datasets" / m["dataset_id"] / "data.jsonl").read_text())
    chosen = json.loads(line["chosen"][0]["tool_calls"][0]["function"]["arguments"])
    rejected = json.loads(line["rejected"][0]["tool_calls"][0]["function"]["arguments"])
    assert (chosen["content"], rejected["content"]) == ("OK hello\n", "hello\n")
    assert line["prompt"][0]["role"] == "system" and line["tools"]
    # a human preference between different decisions is refused
    later = [e for e in calls.values() if e["session_id"] == SESSION][-1]
    with pytest.raises(ValueError, match="not the same decision"):
        record_human_preference(p.store, "fixture-game", later["event_id"], pair["rejected_event"],
                                "victor", "looks better")


# Foundry endpoints + admin view -----------------------------------------------------------

from foundry_router.config import ClientConfig  # noqa: E402
from tests.test_budgets import STATE, ledgered  # noqa: E402,F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}


def test_foundry_training_view_holds_summaries_only(ledgered, client, tmp_path):
    ledgered.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration", "admin"]))
    status = {"projects": {"fixture-game": {"capture": {"enabled": True, "sources": ["kilo"]},
                                            "episodes": [{"kind": "trajectory", "status": "selected",
                                                          "split": "train", "n": 1}],
                                            "gaps": 0, "duplicates": 0}},
              "datasets": [], "ingest_runs": [], "gaps": []}
    assert client.put("/v1/foundry/training/status/ws1", headers=COORD, json=status).status_code == 200
    eps = [{"episode_id": "ep1", "project_id": "fixture-game", "task_id": "FX-1",
            "kind": "trajectory", "status": "selected", "turns": [{"role": "user", "text": "hi"}]},
           {"episode_id": "ep2", "project_id": "fixture-game", "kind": "trajectory",
            "status": "excluded", "turns": [{"role": "tool", "text":
                                            "token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"}]}]
    r = client.post("/v1/foundry/training/episodes/ws1", headers=COORD, json={"episodes": eps})
    assert r.json()["recorded"] == 1                    # the secret-looking card is refused
    assert client.put("/v1/foundry/training/status/ws1", json=status).status_code in (401, 403)
    v = client.get("/admin/api/training").json()
    assert v["pipelines"][0]["pipeline_id"] == "ws1"
    assert [e["episode_id"] for e in v["episodes"]] == ["ep1"]
    assert "capture" in v and v["capture"]["enabled"] is False
    assert client.get("/admin/api/training/episodes/ep2").status_code == 404


# regressions found by the live pilot ---------------------------------------------------

def test_coordinator_review_calls_are_labels_not_trajectories(tmp_path):
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, "fixture-game", ws)
    st = coordinator(tmp_path, "fixture-game", "FX-1", SESSION, base, head)
    review = capture_rows(kilo_export(), session="review-FX-1-a1-abc123-0", role="reviewer",
                          persona="Foundry-Kilo-Sonnet", model="claude-sonnet-5-5")[:2]
    p = pipeline(tmp_path, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_foundry(capture_db=capture_db(tmp_path, capture_rows(kilo_export()) + review))
    p.ingest_coordinator(st.path)
    p.curate("fixture-game")
    sessions = {json.loads(r["data"])["session_id"] for r in p.store.cur.q("SELECT data FROM episodes")}
    assert sessions == {SESSION}


def test_tasks_sharing_prompt_boilerplate_are_not_duplicates(tmp_path):
    """AnimalUprising prompts share ~17 KB of rules; different work under the
    same boilerplate must not be collapsed."""
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, "fixture-game", ws)
    rules = "\n".join(f"Rule {i}: follow the project's agent rules section {i} exactly."
                      for i in range(300))
    exps = []
    for n, (sid, task) in enumerate((("ses_boiler000000000000000001", "FX-1"),
                                     ("ses_boiler000000000000000002", "FX-2"))):
        e = clone_session(kilo_export(), sid, task=task, write_content=f"{task}: distinct work\n",
                          final_text=f"{task} finished: wrote the {task} greeting file.",
                          start_ms=int(time.time() * 1000) + n * 1000)
        e["messages"][0]["parts"][0]["text"] = rules + f"\n\n## Task {task}\nDo {task}."
        exps.append(e)
    p = pipeline(tmp_path, [prof])
    p.ingest_kilo(exports=[write_json(tmp_path / f"{i}.json", e) for i, e in enumerate(exps)])
    p.curate("fixture-game")
    rows = p.store.cur.q("SELECT status FROM episodes WHERE kind='trajectory'")
    assert len(rows) == 2 and all(r["status"] != "duplicate" for r in rows)


def test_closeout_session_is_measured_by_the_closeout_checks(tmp_path):
    from foundry_coordinator.adapters.base import VerificationResult
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, "fixture-game", ws)
    st = coordinator(tmp_path, "fixture-game", "FX-1", SESSION, base, head)
    (tmp_path / "fixture-game" / "CLOSE.md").write_text("closed\n")
    git(ws, "add", ".")
    git(ws, "commit", "-qm", "FX-1: closeout")
    close_head = git(ws, "rev-parse", "HEAD")
    cs = "ses_close0000000000000000000007"
    st.add_session(cs, "fixture-game", "FX-1", "FX-1-close0", "implementer", "closeout", 1,
                   "kilo", "Foundry-Kilo-Local")
    st.save_verification("fixture-game", "FX-1", close_head, "closeout",
                         [VerificationResult("coordinator.publish", "PASS")])
    close = clone_session(kilo_export(), cs, start_ms=int(time.time() * 1000),
                          write_content="closed\n", final_text="Closed out.")
    p = pipeline(tmp_path, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT), write_json(tmp_path / "c.json", close)])
    p.ingest_coordinator(st.path)
    p.curate("fixture-game")
    ep = next(json.loads(r["data"]) for r in p.store.cur.q("SELECT data FROM episodes")
              if json.loads(r["data"])["session_id"] == cs)
    assert ep["revision"] == {"base": head, "head": close_head}
    assert ep["outcome"]["verification"]["status"] == "PASS"
    assert ep["outcome"]["verification"]["checks"][0]["check"] == "coordinator.publish"
    assert ep["outcome"]["review"] is None             # the pre-closeout review isn't relabelled
