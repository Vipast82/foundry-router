"""2026-10-29: the automatic training run re-read the whole coordinator DB, Foundry, git and
gate files and re-curated the project for EVERY queued manifest; with 33 queued it held
32 GB for 30 hours. Shared sources are read once per pass; curation runs once per project."""

import json
import os
import time
from types import SimpleNamespace

from foundry_training import cli
from foundry_training.pipeline import Pipeline


def _pipe(calls):
    p = Pipeline.__new__(Pipeline)
    p.projects = {"au": SimpleNamespace(workspace="ws")}
    p._allowed = lambda pid, source: True
    p._cfg = lambda pid: p.projects[pid]
    p.ingest_coordinator = lambda db: calls.append(("coord", db))
    p.ingest_kilo = lambda api: calls.append(("kilo", tuple(api["sessions"])))
    p.ingest_foundry = lambda url, key: calls.append(("foundry",))
    p.ingest_git = lambda pid: calls.append(("git", pid))
    p.ingest_gate = lambda pid: calls.append(("gate", pid))
    p.curate = lambda pid: calls.append(("curate", pid)) or {"episodes": 1}
    p.store = SimpleNamespace(
        events=lambda pid, where: [{"session_id": s} for s in ("s1", "s2", "s3")],
        gap=lambda *a: calls.append(("gap",) + a))
    return p


def test_shared_sources_are_read_once_for_many_manifests(tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    for i in range(3):
        (spool / f"au__T{i}__{i}.json").write_text(json.dumps({
            "project_id": "au", "task_id": f"T{i}", "coordinator_db": "c.sqlite",
            "sessions": [f"s{i + 1}"], "session_roles": {}}))
    calls = []
    out = _pipe(calls).process_queue(str(spool), foundry_url="http://f", kilo_api={"url": "k"})
    kinds = [c[0] for c in calls]
    assert kinds.count("coord") == 1 and kinds.count("foundry") == 1
    assert kinds.count("git") == 1 and kinds.count("gate") == 1 and kinds.count("curate") == 1
    assert kinds.count("kilo") == 3                         # each manifest's own sessions
    assert len(out) == 3 and all(r.get("curation") for r in out)
    assert sorted(os.listdir(spool / "done")) == sorted(f"au__T{i}__{i}.json" for i in range(3))
    # curation runs after every manifest's sessions were ingested
    assert kinds.index("curate") > max(i for i, k in enumerate(kinds) if k == "kilo")


def test_the_watchdog_removes_the_lock_and_ends_the_run(tmp_path, monkeypatch):
    lock = tmp_path / ".auto.lock"
    lock.write_text("123")
    ended = []
    monkeypatch.setattr(os, "_exit", lambda code: ended.append(code))
    cli._watchdog(0.001, str(lock))                          # 60 ms
    deadline = time.time() + 5
    while not ended and time.time() < deadline:
        time.sleep(0.05)
    assert ended == [3] and not lock.exists()
    cli._watchdog(0, str(tmp_path / "x"))                     # 0 = no limit, no timer
